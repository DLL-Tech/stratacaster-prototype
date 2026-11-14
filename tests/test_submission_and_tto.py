"""Employee MSR submission, REQ assessment, TTO-scoped lead review, and task-only publication (PR-19 to PR-21)."""
import json

import pytest

from stratacaster.delivery import POTENTIAL_NEW_TASK
from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

PREP, APPROVE, PUBLISH = "2026-10-23T16:00:00Z", "2026-10-23T16:15:00Z", "2026-10-23T16:20:00Z"
# Rowan (SIM-EMP-005, WR-001, SIM-TTO-1-01). Bullet 1 aligns to two REQ tasks; bullet 3 aligns to none.
BULLETS = ["• I reran checks on the systems that were missed and added them to the patching queue.", "",
           "I organized the team picnic."]


@pytest.fixture
def renv(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    e.clock.set(PREP)
    yield e
    e.close()


def rowan(env):
    return env.principal("SIM-EMP-005", "employee")


def _submit(env, emp="SIM-EMP-005", bullets=BULLETS):
    out = env.delivery.submit_msr(env.principal(emp, "employee"), "2026-11", bullets)
    assert out.ok, out.reason
    return out


def test_submission_is_labeled_like_the_dataset(renv):
    out = _submit(renv)
    assert out.data["input_id"] == "SIM-MSR-202611-005" and out.data["input_version"] == 1
    msr = renv.delivery_store.get_input(rowan(renv), "SIM-MSR-202611-005")
    assert (msr.contract_id, msr.tto_id, msr.role_id, msr.delivery_scope_id) == (
        "SIM-FC-001", "SIM-TTO-1-01", "WR-001", "SIM-DELIVERY-FC-001")
    assert (msr.period_start, msr.period_end) == ("2026-11-01", "2026-11-30")
    assert msr.source_locator == "delivery-local:SIM-MSR-202611-005:v1"
    assert msr.review.status == "accepted" and msr.review.reviewer_id == "SIM-EMP-005"  # submission = attestation
    assert msr.classification == "synthetic-demo" and "delivery-only" in msr.restrictions


def test_current_month_reports_through_today_and_resubmission_is_next_version(renv):
    renv.load_all_cases()  # SIM-MSR-202610-005 v1 already exists
    out = renv.delivery.submit_msr(rowan(renv), "2026-10", BULLETS)
    assert out.ok and out.data["input_version"] == 2
    store = renv.delivery_store
    assert store.get_input(rowan(renv), "SIM-MSR-202610-005").period_end == "2026-10-23"  # demo clock date
    assert store.get_input(rowan(renv), "SIM-MSR-202610-005", 1).source_status == "superseded"


@pytest.mark.parametrize("who,role", [("SIM-EMP-210", "solutions"), ("SIM-EMP-002", "operator")])
def test_only_employees_acting_as_employees_submit(renv, who, role):
    out = renv.delivery.submit_msr(renv.principal(who, role), "2026-11", BULLETS)
    assert not out.ok


def test_bad_month_and_empty_narrative_are_rejected(renv):
    assert not renv.delivery.submit_msr(rowan(renv), "2026-13", BULLETS).ok
    assert not renv.delivery.submit_msr(rowan(renv), "2026-11", ["", "  "]).ok


# ------------------------------------------------------------------ REQ assessment on submission

def test_each_work_role_in_a_tto_has_an_individual_req(renv):
    req = renv.registry.role_req_for("WR-001", "SIM-TTO-1-01")
    assert req["role_req_id"] == "SIM-REQ-1-01-WR-001" and len(req["req_task_ids"]) == 7
    assert all(renv.registry.req_tasks[t]["tto_id"] == "SIM-TTO-1-01" for t in req["req_task_ids"])
    assert renv.registry.role_req_for("LR-MISSION-TECHNICAL-DIRECTOR", "SIM-TTO-1-01") is None


def test_missing_req_is_generated_from_requirement_tasks(renv):
    renv.registry.role_reqs.clear()  # no REQ identified: fall back to the Requirement Tasks
    req = renv.registry.role_req_for("WR-001", "SIM-TTO-1-01")
    assert req["generated"] and req["role_req_id"] == "SIM-REQ-1-01-WR-001" and len(req["req_task_ids"]) == 7


def test_submission_is_assessed_and_tagged(renv):
    a = _submit(renv).data["assessment"]["assessment"]
    assert a["role_req_id"] == "SIM-REQ-1-01-WR-001"
    s1, s2 = a["statements"]
    assert s1["tag"] == "aligned" and len(s1["req_task_ids"]) >= 2  # one input, several tasks
    assert s2["tag"] == POTENTIAL_NEW_TASK and s2["req_task_ids"] == []
    assert a["input_tag"] == f"aligned+{POTENTIAL_NEW_TASK}" and a["potential_new_task"]
    stored = renv.delivery.assessment(rowan(renv), "SIM-MSR-202611-005")
    assert stored == a


def test_one_draft_task_alignment_per_aligned_task(renv):
    out = _submit(renv).data["assessment"]
    tasks = out["assessment"]["aligned_req_task_ids"]
    assert sorted(c["req_task_id"] for c in out["created"]) == tasks


# ------------------------------------------------------------------ TTO lead review: only the employee's own TTO

def lead(env, uid):
    return env.principal(uid, "tto")


def test_tto_lead_sees_and_reviews_only_their_own_tto(renv):
    renv.load_all_cases()
    maya = lead(renv, "SIM-EMP-002")  # lead of SIM-TTO-1-01
    assert maya.tto_authority == {"SIM-TTO-1-01"}
    assert {m.tto_id for m in renv.delivery_store.list_inputs(maya)} == {"SIM-TTO-1-01"}
    iid = _submit(renv).data["input_id"]
    out = renv.delivery.tto_review(lead(renv, "SIM-EMP-004"), iid, 1, "approved")  # lead of SIM-TTO-1-03
    assert not out.ok and "own TTO" in out.reason
    out = renv.delivery.tto_review(maya, iid, 1, "approved")
    assert out.ok and out.data["tto_id"] == "SIM-TTO-1-01" and out.data["own_submission"] is False


def test_tto_lead_from_another_contract_is_denied(renv):
    iid = _submit(renv).data["input_id"]
    out = renv.delivery.tto_review(lead(renv, "SIM-EMP-052"), iid, 1, "approved")  # SIM-FC-002 lead
    assert not out.ok and "own TTO" in out.reason


def test_tto_lead_assigned_to_the_tto_may_approve_own_submission(renv):
    assert renv.cfg["governance"]["management_authority_covers_own_msr"] is False  # even with separation of duties
    iid = _submit(renv, "SIM-EMP-002").data["input_id"]  # Maya, assigned to and lead of SIM-TTO-1-01
    out = renv.delivery.tto_review(lead(renv, "SIM-EMP-002"), iid, 1, "approved")
    assert out.ok and out.data["own_submission"] is True


def test_tto_lead_needs_employee_acceptance_first(renv):
    renv.load_all_cases()  # dataset MSRs arrive pending until the employee accepts
    out = renv.delivery.tto_review(lead(renv, "SIM-EMP-002"), "SIM-MSR-202610-005", 1, "approved")
    assert not out.ok and "employee must accept" in out.reason


def test_contract_pmo_staff_have_no_tto_lead(renv):
    assert renv.registry.tto_lead_for("SIM-TTO-1-PMO") is None
    assert renv.registry.tto_lead_for("SIM-TTO-1-02") == "SIM-EMP-003"


def test_only_tto_lead_role_can_decide_and_decision_is_final(renv):
    iid = _submit(renv).data["input_id"]
    assert not renv.delivery.tto_review(renv.principal("SIM-EMP-002", "operator"), iid, 1, "approved").ok
    assert not renv.delivery.tto_review(renv.principal("SIM-EMP-001", "pmo"), iid, 1, "approved").ok
    maya = lead(renv, "SIM-EMP-002")
    assert renv.delivery.tto_review(maya, iid, 1, "returned", "please keep it professional").ok
    again = renv.delivery.tto_review(maya, iid, 1, "approved")
    assert not again.ok and "already recorded" in again.reason


# ------------------------------------------------------------------ PMO approval and task-only publication

def test_pmo_needs_tto_approval_then_publishes_every_alignment_without_msr_content(renv):
    out = _submit(renv).data
    rids = [c["record_id"] for c in out["assessment"]["created"]]
    avery = renv.principal("SIM-EMP-001", "pmo")
    renv.clock.set(APPROVE)
    blocked = renv.delivery.pmo_decide(avery, rids[0], 1, "approved", "before TTO review")
    assert not blocked.ok and "tto-lead-review-not-current" in blocked.reason
    assert renv.delivery.tto_review(lead(renv, "SIM-EMP-002"), out["input_id"], 1, "approved").ok
    events = {}
    for rid in rids:
        a = renv.delivery.pmo_decide(avery, rid, 1, "approved", "final view approved")
        assert a.ok, a.reason
        events[rid] = a.data["event_id"]
    renv.clock.set(PUBLISH)
    for rid, eid in events.items():
        assert renv.delivery.publish(avery, rid, 1, eid).ok
    published = [r for r in renv.evidence_store.list_records(avery) if r.disposition == "published"]
    assert len(published) == len(rids) >= 2  # all alignments of the one input are pushed to Solutions
    msr = renv.delivery_store.get_input(rowan(renv), out["input_id"])
    blob = json.dumps([r.model_dump() for r in published])
    for word in ("reran", "picnic", "patching queue", msr.raw_narrative):
        assert word not in blob
    for r in published:
        assert r.evidence_summary.startswith(
            "Employee Rowan Bennett (SIM-EMP-005, WR-001, SIM-TTO-1-01) reported work aligned to")
        assert r.req_task_id in r.evidence_summary


# ------------------------------------------------------------------ contract-wide analytic (PR-23)

CROSS = ["I updated the scan settings for the assigned systems.",
         "I retested the claimed fix after the changes and compared notes with the defenders.",
         "I added the newly visible addresses to the system list and repeated the checks."]


def test_contract_wide_analytic_tracks_similar_work_and_collaboration_separately(renv):
    out = _submit(renv, "SIM-EMP-006", CROSS).data["assessment"]
    a = out["assessment"]
    own = set(a["aligned_req_task_ids"])
    assert all(t.startswith("SIM-REQ-1-01-") for t in own)
    cw = a["contract_wide"]
    assert cw["counts"] == {"similar-work": 1, "collaboration-opportunity": 1}
    collab = next(r for r in cw["statements"] if r["signal"] == "collaboration-opportunity")
    assert collab["statement_id"].endswith("-2") and {x["tto_id"] for x in collab["elsewhere"]} == {"SIM-TTO-1-02"}
    similar = next(r for r in cw["statements"] if r["signal"] == "similar-work")
    assert similar["statement_id"].endswith("-3") and similar["elsewhere"][0]["tto_id"] == "SIM-TTO-1-03"
    scopes = {c["req_task_id"]: c["alignment_scope"] for c in out["created"]}
    assert {scopes[t] for t in own} == {"own-req"}
    assert set(scopes.values()) == {"own-req", "similar-work", "collaboration-opportunity"}
    assert not (own & {x["req_task_id"] for r in cw["statements"] for x in r["elsewhere"]})  # tracked separately


def test_cross_req_records_are_pmo_approved_and_reach_solutions_as_related_work(renv):
    data = _submit(renv, "SIM-EMP-006", CROSS).data
    assert renv.delivery.tto_review(lead(renv, "SIM-EMP-002"), data["input_id"], 1, "approved").ok
    avery = renv.principal("SIM-EMP-001", "pmo")
    renv.clock.set(APPROVE)
    events = {c["record_id"]: renv.delivery.pmo_decide(avery, c["record_id"], 1, "approved", "ok").data["event_id"]
              for c in data["assessment"]["created"]}
    renv.clock.set(PUBLISH)
    assert all(renv.delivery.publish(avery, rid, 1, eid).ok for rid, eid in events.items())
    tasks = [t for t, v in renv.registry.req_tasks.items() if v["tto_id"] == "SIM-TTO-1-02"]
    r = renv.orchestrator.handle(renv.principal("SIM-EMP-210"), {
        "query_id": "Q-CROSS", "requester_id": "SIM-EMP-210", "audience_id": "SIM-AUD-SOLUTIONS",
        "purpose": "requirement-evidence-explanation", "req_task_ids": tasks, "question": "Related work?"})
    assert {x["alignment_scope"] for x in r["related_work"]} == {"collaboration-opportunity"}
    assert all("Leila Bennett" in x["evidence_summary"] for x in r["related_work"])
    assert not r["citations"]  # related work never counts as coverage
    assert all(c["status"] == "unsupported" for c in r["coverage"])
    msr = renv.delivery_store.get_input(renv.principal("SIM-EMP-006", "employee"), data["input_id"])
    assert msr.raw_narrative not in str(r) and "defenders" not in str(r)


def test_guide_dataset_keeps_its_original_flow(tmp_path):
    e = Environment(var_dir=tmp_path / "g", llm=MockLLM(), dataset="guide")
    try:
        assert not (e.registry.tto_review_required or e.registry.assess_on_submission
                    or e.registry.publish_task_alignment_only)
    finally:
        e.close()
