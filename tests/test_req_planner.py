"""REQ-change planning, the Tree of Thought pilot (CR-SC-2026-003, PR-29): the TTO Coordination Agent proposes,
deterministic code searches and prunes, and only a human TTO lead (or PMO peer) decides. Baselines never change."""
import json

import pytest

from stratacaster.environment import Environment
from stratacaster.llm import LLMError, MockLLM
from stratacaster.registry import TTO_COORDINATION_SERVICE
from stratacaster.req_planner import PARSIMONY, ReqPlanner
from stratacaster.stores import find_delivery_only_keys

PREP = "2026-10-23T16:00:00Z"
REQ = "SIM-REQ-1-01-WR-001"


@pytest.fixture
def penv(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    e.load_all_cases()
    e.clock.set(PREP)
    for c in e.msr_cases():
        rv = c["scripted_review"]
        if rv and rv["decision"] == "accepted":
            e.delivery.employee_review(e.principal(rv["reviewer_id"], "employee"), c["input"]["input_id"], 1,
                                       "accepted")
    lead = e.principal("SIM-EMP-002", "tto")
    q = e.delivery.tto_review_queue(lead)
    e.delivery.bulk_tto_review(lead, [(r["input_id"], r["version"]) for r in q], "approved")
    yield e
    e.close()


def lead(env):
    return env.principal("SIM-EMP-002", "tto")


def planner(env, **params):
    return ReqPlanner(registry=env.registry, store=env.delivery_store, delivery=env.delivery, ops=env.ops,
                      audit=env.audit, clock=env.clock, agent=env.coordination_agent, params=params)


def latest(env, p=None, req=REQ):
    return (p or env.req_planner).plans(lead(env), req)[-1]


def test_search_respects_depth_beam_and_budget_and_ranks_plans(penv):
    out = penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    assert out.ok and out.reason == "awaiting-tto-endorsement" and 1 <= out.data["plans"] <= 3
    plan = latest(penv)
    assert len(plan["searched_clusters"]) <= 3 and plan["depth_reached"] <= 3
    assert plan["nodes_generated"] <= 60 and not plan["budget_exhausted"]
    for depth in (1, 2, 3):
        assert sum(r.get("depth") == depth and r["status"] == "kept" for r in plan["trace"]) <= 3
    scores = [p["plan_score"] for p in plan["plans"]]
    assert scores == sorted(scores, reverse=True) and plan["recommended_rank"] == 1
    assert all(len(p["decisions"]) == len(plan["searched_clusters"]) for p in plan["plans"])
    assert plan["baseline_changed"] is False and plan["review_basis"] == "tto-lead"


def test_plan_record_carries_no_raw_statement_text(penv):
    penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    plan = latest(penv)
    blob = json.dumps(plan)
    assert not find_delivery_only_keys(plan) and "_texts" not in blob and "_seed" not in blob
    texts = [t for m in penv.delivery_store.list_inputs(lead(penv)) for _, t in
             penv.delivery_store.get_statements(lead(penv), m.input_id, m.input_version)]
    assert not any(t in blob for t in texts)


def test_flagged_statements_never_reach_the_agent(penv):
    penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    plan = latest(penv)
    assert plan["excluded_statements"]
    assert all(any("BLOCKING" in f for f in x["flags"]) for x in plan["excluded_statements"])
    ids = {s for c in plan["clusters"] for s in c["statement_ids"]}
    assert not ids & {x["statement_id"] for x in plan["excluded_statements"]}


def test_node_budget_stops_the_search(penv):
    p = planner(penv, node_budget=4)
    p.plan_req(lead(penv), REQ, "2026-10")
    plan = latest(penv, p)
    assert plan["budget_exhausted"] and plan["nodes_generated"] == 4


def test_prune_reasons_are_recorded(penv):
    penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    pruned = [r for r in latest(penv)["trace"] if r["status"] == "pruned"]
    assert any(r["reason"].startswith("new-req: needs at least 3 employees") for r in pruned)
    p = planner(penv, prune_below=0.6)
    p.plan_req(lead(penv), REQ, "2026-10")
    assert any("< 0.6" in r.get("reason", "") for r in latest(penv, p)["trace"])


def test_empty_beam_means_no_plan_and_an_escalation(penv):
    p = planner(penv, prune_below=1.01)
    out = p.plan_req(lead(penv), REQ, "2026-10")
    assert out.ok and out.reason == "no-viable-plan" and out.data["escalation_id"]
    assert latest(penv, p)["plans"] == [] and latest(penv, p)["recommended_rank"] is None


def test_agent_failure_is_safe(penv, monkeypatch):
    class Down:
        mode, model = "mock", "down"

        def run(self, *a, **k):
            raise LLMError("unavailable")
    monkeypatch.setattr(penv.coordination_agent, "llm", Down())
    out = penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    assert out.reason == "no-viable-plan" and latest(penv)["agent_errors"]


# ------------------------------------------------------------------ constraints and scoring, unit level

def _cluster(texts, employees, elsewhere=()):
    return {"cluster_id": "C1", "employees": employees, "elsewhere": list(elsewhere), "_texts": texts}


def _opt(op, wording, target=None):
    return {"operator": op, "target_req_task_id": target, "wording": wording, "rationale": "test"}


def test_coordination_work_credits_no_change(penv):
    p, req = penv.req_planner, penv.registry.role_reqs[REQ]
    tasks = penv.registry.contract_req_tasks("SIM-FC-001")
    c = _cluster(["I talked through the plan with a colleague.", "I joined the weekly meeting to coordinate."], 3)
    keep = p._score(_opt("no-change", "No REQ change"), c, tasks)
    add = p._score(_opt("add-task", "Plan colleague meeting record"), c, tasks)
    assert keep["parts"]["coverage"] == 1.0 and add["parts"]["coverage"] == 0.0 and keep["score"] > add["score"]


def test_hard_constraints(penv):
    p, req = penv.req_planner, penv.registry.role_reqs[REQ]
    tasks = penv.registry.contract_req_tasks("SIM-FC-001")
    text = "I updated the walkthrough and shared the remaining gaps with the review team."
    c = _cluster([text], 1)
    assert any(v.startswith("wording-copy") for v in p._violations(_opt("add-task", text), c, req, [], tasks))
    title = penv.registry.req_tasks[req["req_task_ids"][0]]["title"]
    assert any(v.startswith("overlap") for v in p._violations(_opt("add-task", title), c, req, [], tasks))
    assert any(v.startswith("new-req") for v in p._violations(_opt("new-req", "Walkthrough scope"), c, req, [], tasks))
    two = _cluster([text], 3)
    first = [{"operator": "new-req", "wording": "Other scope"}]
    assert "new-req: at most one new REQ per plan" in p._violations(_opt("new-req", "Gap scope"), two, req, first, tasks)


def test_req_size_limit_is_an_optional_contract_setting(penv, monkeypatch):
    """REQs are not size-limited unless the contract's PMO sets req_max_tasks in the reference data."""
    p, req = penv.req_planner, penv.registry.role_reqs[REQ]
    tasks = penv.registry.contract_req_tasks("SIM-FC-001")
    c = _cluster(["I kept a running log of the gaps."], 2)
    big = req | {"req_task_ids": [f"T{i}" for i in range(30)]}
    assert p.req_size_limit(req) is None
    assert not any(v.startswith("req-size") for v in p._violations(_opt("add-task", "Gap log"), c, big, [], tasks))
    monkeypatch.setitem(penv.registry.contracts["SIM-FC-001"], "req_max_tasks", 10)
    assert p.req_size_limit(req) == 10
    assert any("limit of 10" in v for v in p._violations(_opt("add-task", "Gap log"), c, big, [], tasks))
    other = next(r for r in penv.registry.role_reqs.values()
                 if penv.registry.ttos[r["tto_id"]]["contract_id"] != "SIM-FC-001")
    assert p.req_size_limit(other) is None  # one contract's setting does not apply to another


def test_route_elsewhere_is_scored_from_contract_wide_alignment(penv):
    p, req = penv.req_planner, penv.registry.role_reqs[REQ]
    other = next(t for t in penv.registry.contract_req_tasks("SIM-FC-001") if t["tto_id"] != req["tto_id"])
    c = _cluster(["a", "b"], 2, [{"req_task_id": other["req_task_id"], "tto_id": other["tto_id"], "statements": 2}])
    s = p._score(_opt("route-elsewhere", other["title"], other["req_task_id"]), c,
                 penv.registry.contract_req_tasks("SIM-FC-001"))
    assert s["parts"]["coverage"] == 1.0
    result, _ = penv.coordination_agent.propose({"text": "triage defect closure"}, [], [
        {"req_task_id": other["req_task_id"], "title": other["title"], "tto_id": other["tto_id"]}])
    assert any(o.operator == "route-elsewhere" for o in result.parsed.options)


def test_one_employee_cannot_outrank_no_change(penv):
    """Regression for the first live Claude smoke test: one employee's single statement, with a weak cross-REQ match,
    ranked a re-route above leaving the REQ alone."""
    p, req = penv.req_planner, penv.registry.role_reqs[REQ]
    tasks = penv.registry.contract_req_tasks("SIM-FC-001")
    other = next(t for t in tasks if t["tto_id"] != req["tto_id"])
    c = _cluster(["I included the feedback I received and noted where I had not yet checked it."], 1,
                 [{"req_task_id": other["req_task_id"], "tto_id": other["tto_id"], "statements": 1}]) | {"cluster_id": "C1"}
    options = {"C1": [_opt("no-change", "No REQ change"), _opt("route-elsewhere", other["title"], other["req_task_id"]),
                      _opt("add-task", "Feedback verification log")]}
    found = p.search(req, [c], options)
    assert found["plans"][0]["decisions"][0]["operator"] == "no-change"
    capped = [r for r in found["trace"] if r.get("capped")]
    assert capped and all(r["operator"] != "no-change" for r in capped)
    two = c | {"employees": 2}
    route = p._score(_opt("route-elsewhere", other["title"], other["req_task_id"]), two, tasks)
    assert "capped" not in route and route["parts"]["coverage"] == 0.5  # single matching statement counts half


def test_agent_validator_rejects_targets_it_was_not_offered(penv):
    from stratacaster.schemas import PlanCandidates, PlanOption
    bad = PlanCandidates(options=[PlanOption(operator="route-elsewhere", target_req_task_id="SIM-REQ-9-99-01",
                                             wording="Somewhere else", rationale="x")])

    class Fixed:
        mode, model = "mock", "fixed"

        def run(self, *a, **k):
            from stratacaster.llm import LLMResult
            return LLMResult(bad, "mock", "fixed", 0)
    penv.coordination_agent.llm = Fixed()
    result, errors = penv.coordination_agent.propose({"text": "x"}, [], [])
    assert result is None and "was not offered" in errors[-1]


def test_ties_prefer_the_smaller_change_then_more_evidence_then_key():
    d = lambda op, ev: {"operator": op, "parts": {"evidence": ev}}
    small = {"score": 0.7, "key": "b", "decisions": [d("no-change", 0.1)]}
    big = {"score": 0.7, "key": "a", "decisions": [d("add-task", 0.9)]}
    assert sorted([big, small], key=ReqPlanner._rank_key)[0] is small
    x = {"score": 0.7, "key": "b", "decisions": [d("add-task", 0.9)]}
    y = {"score": 0.7, "key": "a", "decisions": [d("add-task", 0.2)]}
    assert sorted([y, x], key=ReqPlanner._rank_key)[0] is x
    z = {"score": 0.7, "key": "a", "decisions": [d("add-task", 0.9)]}
    assert sorted([x, z], key=ReqPlanner._rank_key)[0] is z
    assert PARSIMONY["no-change"] > PARSIMONY["widen-task"] > PARSIMONY["add-task"] > PARSIMONY["new-req"]


# ------------------------------------------------------------------ the human gate

def test_only_the_reqs_tto_lead_runs_and_decides(penv):
    other = penv.registry.tto_lead_for("SIM-TTO-1-02")
    stranger = penv.principal(other, "tto")
    assert not penv.req_planner.plan_req(stranger, REQ, "2026-10").ok
    assert not penv.req_planner.plan_req(penv.principal("SIM-EMP-005", "employee"), REQ, "2026-10").ok
    penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    pid = latest(penv)["plan_id"]
    assert not penv.req_planner.decide_req_plan(stranger, pid, "endorsed", 1).ok
    assert penv.req_planner.plans(stranger, REQ) == []
    assert not penv.req_planner.decide_req_plan(lead(penv), pid, "accepted", 1).ok  # endorse, not accept
    assert not penv.req_planner.decide_req_plan(lead(penv), pid, "endorsed", 9).ok
    assert not penv.req_planner.decide_req_plan(lead(penv), pid, "rejected").ok  # reason required
    assert penv.req_planner.decide_req_plan(lead(penv), pid, "rejected", reason="not yet; revisit next month").ok
    assert not penv.req_planner.decide_req_plan(lead(penv), pid, "endorsed", 1).ok  # one decision per plan
    assert latest(penv)["status"] == "rejected-by-tto"


def pmo(env, i=0):
    return env.principal(env.registry.pmo_team("SIM-PMO-FC-001")[i], "pmo")


def endorsed_plan(env):
    env.req_planner.plan_req(lead(env), REQ, "2026-10")
    pid = latest(env)["plan_id"]
    assert env.req_planner.decide_req_plan(lead(env), pid, "endorsed", 1, "top plan").ok
    return pid


def test_the_pmo_approves_req_changes_and_the_baseline_is_not_rewritten(penv):
    before = list(penv.registry.role_reqs[REQ]["req_task_ids"])
    pid = endorsed_plan(penv)
    assert latest(penv)["status"] == "awaiting-pmo-approval"
    assert [q["plan_id"] for q in penv.req_planner.pmo_queue(pmo(penv))] == [pid]
    out = penv.req_planner.pmo_decide_req_plan(pmo(penv), pid, "approved")
    assert out.ok and out.data["change_id"].startswith("SIM-RQC-")
    plan = latest(penv)
    assert plan["status"] == "approved" and plan["pmo_decision"]["pmo_id"] == "SIM-PMO-FC-001"
    change = plan["pmo_decision"]["req_change"]
    assert change["baseline_req_task_ids"] == before and all(c["operator"] != "no-change" for c in change["changes"])
    assert penv.registry.role_reqs[REQ]["req_task_ids"] == before and plan["pmo_decision"]["baseline_changed"] is False
    assert not penv.req_planner.pmo_decide_req_plan(pmo(penv, 1), pid, "rejected", "late").ok  # one PMO decision
    assert penv.req_planner.pmo_queue(pmo(penv)) == []
    actions = [a["action"] for a in penv.audit.entries()]
    assert {"plan_req", "decide_req_plan", "pmo_decide_req_plan"} <= set(actions)


def test_pmo_gate_refusals(penv):
    penv.req_planner.plan_req(lead(penv), REQ, "2026-10")
    pid = latest(penv)["plan_id"]
    assert "not been endorsed" in penv.req_planner.pmo_decide_req_plan(pmo(penv), pid, "approved").reason
    penv.req_planner.decide_req_plan(lead(penv), pid, "endorsed", 1)
    assert not penv.req_planner.pmo_decide_req_plan(lead(penv), pid, "approved").ok  # a TTO lead is not the PMO
    other = penv.principal(penv.registry.pmo_team("SIM-PMO-FC-002")[0], "pmo")
    assert not penv.req_planner.pmo_decide_req_plan(other, pid, "approved").ok  # another contract's PMO
    assert not penv.req_planner.pmo_decide_req_plan(pmo(penv), pid, "returned").ok  # reason required
    out = penv.req_planner.pmo_decide_req_plan(pmo(penv), pid, "returned", "split cluster C1 first")
    assert out.ok and latest(penv)["status"] == "returned-by-pmo"
    assert latest(penv)["pmo_decision"]["next_step"] == "returned to the TTO stage for a new plan"


def test_endorser_may_not_also_approve(penv):
    pid = endorsed_plan(penv)
    p = penv.req_planner
    plan = p._plan(pid)
    # A PMO staff REQ is endorsed by a PMO peer; simulate that the same PMO member then tries to approve.
    event = dict(p.store.req_plan_decision(pid, "tto"), decided_by=pmo(penv).user_id, plan_id="SIM-RQP-SOD")
    p.store.append_req_plan(TTO_COORDINATION_SERVICE, dict(plan, plan_id="SIM-RQP-SOD"))
    p.store.append_req_plan_decision(lead(penv), event)
    out = p.pmo_decide_req_plan(pmo(penv), "SIM-RQP-SOD", "approved")
    assert not out.ok and "separation of duties" in out.reason
    assert p.pmo_decide_req_plan(pmo(penv, 1), "SIM-RQP-SOD", "approved").ok


def test_monitor_shows_planning_endorsement_and_approval(penv):
    from stratacaster.monitor import classify
    pid = endorsed_plan(penv)
    penv.req_planner.pmo_decide_req_plan(pmo(penv), pid, "approved")
    steps = [s for row in penv.audit.entries() for s, _, _ in classify(row)]
    assert {"RQP", "RQD", "RQA"} <= set(steps)


def test_req_review_tab_runs_and_records_a_decision(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    app = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-rqp"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(app, default_timeout=120)
    at.run()
    try:
        env = at.session_state.env
        env.load_all_cases()
        env.clock.set(PREP)
        for i in ("005", "006", "007"):
            env.delivery.employee_review(env.principal(f"SIM-EMP-{i}", "employee"), f"SIM-MSR-202610-{i}", 1,
                                         "accepted")
            env.delivery.tto_review(lead(env), f"SIM-MSR-202610-{i}", 1, "approved")
        at.sidebar.selectbox[0].select("SIM-EMP-002").run()
        next(s for s in at.sidebar.selectbox if s.label == "Acting in role").select("tto").run()
        next(b for b in at.button if b.label == "Run REQ-change planning").click().run()
        assert not at.exception
        assert any("Ranked plans" in m.value for m in at.markdown)
        next(b for b in at.button if b.label.startswith("Endorse plan")).click().run()
        assert not at.exception
        assert env.req_planner.plans(lead(env), REQ)[-1]["status"] == "awaiting-pmo-approval"
        at.sidebar.selectbox[0].select("SIM-EMP-001").run()  # Program Manager, SIM-PMO-FC-001
        next(s for s in at.sidebar.selectbox if s.label == "Acting in role").select("pmo").run()
        assert any("REQ changes awaiting PMO approval** (1)" in m.value for m in at.markdown)
        next(b for b in at.button if b.label == "Approve REQ change").click().run()
        assert not at.exception
        assert env.req_planner.plans(lead(env), REQ)[-1]["status"] == "approved"
    finally:
        at.session_state.env.close()


def test_pmo_staff_reqs_are_decided_by_a_pmo_peer(penv):
    reg = penv.registry
    req = next(r for r in reg.role_reqs.values() if reg.is_pmo_staff_tto(r["tto_id"]))
    pmo = reg.pmo_for_contract(reg.ttos[req["tto_id"]]["contract_id"])
    peer = penv.principal(reg.pmo_team(pmo)[0], "pmo")
    assert penv.req_planner._basis(peer, req) == "pmo-peer"
    assert penv.req_planner._basis(lead(penv), req) is None
    assert req in penv.req_planner.reviewable_reqs(peer)
