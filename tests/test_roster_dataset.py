"""Roster dataset (notional organizational roster workbook v2): import, demo slice, and governed outcomes."""
import importlib.util
import json
from pathlib import Path

import pytest

from stratacaster.demo import run_roster_demo
from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "fixtures" / "roster"
_WB = "StrataCaster_Notional_Organizational_Roster_270_Employees_v3.xlsx"
WORKBOOK = next((p for p in (ROOT / "data" / "source" / _WB, ROOT.parent / "Documentation" / "3 Data" / _WB) if p.exists()),
                ROOT / "data" / "source" / _WB)   # in-repo copy first, sibling Documentation as fallback


@pytest.fixture
def renv(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    yield e
    e.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    e = Environment(var_dir=tmp_path_factory.mktemp("roster") / "var", llm=MockLLM(), dataset="roster")
    try:
        yield e, run_roster_demo(e, say=lambda *_: None)
    finally:
        e.close()


def test_registry_reflects_workbook(renv):
    r = renv.registry
    assert len(r.pmos) == 4 and len(r.contracts) == 4
    assert len(r.ttos) == 17  # 13 task orders + 4 Contract PMO labels
    assert len(r.req_tasks) == 135 and len(r.subjects) == 220  # 107 TTO + 28 PMO tasks; 4 contracts x 55 staff
    assert r.pmos["SIM-PMO-FC-001"]["reviewer_id"] == "SIM-EMP-001"
    assert r.principal("SIM-EMP-001").pmo_authority == {"SIM-PMO-FC-001"}
    assert r.principal("SIM-EMP-002").delivery_scopes == {"SIM-DELIVERY-FC-001"}
    assert r.is_audience_member("SIM-EMP-210", "SIM-AUD-SOLUTIONS")
    company = [u for u in r.users if u.startswith("SIM-EMP-") and 201 <= int(u[-3:]) <= 250]  # SIM-EMP-201..250
    assert company and all(r.users[u]["domain"] == "solutions" for u in company)  # only Solutions/Growth staff
    assert len(company) == 6


def test_each_contract_has_a_six_person_pmo_review_team(renv):
    r = renv.registry
    titles = ["Cybersecurity Program Manager", "Deputy Program Manager", "Operations Manager",
              "Portfolio Lead, Delivery Performance", "Portfolio Lead, Requirements and Change",
              "Portfolio Lead, Workforce and Quality"]
    for pmo_id, pmo in r.pmos.items():
        team = r.pmo_team(pmo_id)
        assert len(team) == 6 and team[0] == pmo["reviewer_id"]  # Program Manager first
        assert sorted(r.users[u]["display"].split(" — ")[1].rsplit(" (", 1)[0] for u in team) == sorted(titles)
        for u in team:
            p = r.principal(u, "pmo")
            assert p.pmo_authority == {pmo_id}
            assert r.subjects[u]["tto_id"].endswith("-PMO")  # assigned directly to the contract


def test_any_pmo_team_member_can_approve_and_publish_but_not_another_pmo(renv):
    renv.load_all_cases()
    renv.clock.set("2026-10-23T16:00:00Z")
    out = renv.delivery.employee_review(renv.principal("SIM-EMP-005", "employee"), "SIM-MSR-202610-005", 1, "accepted")
    rid = out.data["assessment"]["created"][0]["record_id"]
    assert renv.delivery.tto_review(renv.principal("SIM-EMP-002", "tto"), "SIM-MSR-202610-005", 1, "approved").ok
    renv.clock.set("2026-10-23T16:15:00Z")
    other_pmo = renv.delivery.pmo_decide(renv.principal("SIM-EMP-256", "pmo"), rid, 1, "approved", "FC-002 deputy")
    assert not other_pmo.ok
    portfolio_lead = renv.principal("SIM-EMP-254", "pmo")  # FC-001 Portfolio Lead, Requirements and Change
    assert {m.input_id for m in renv.delivery_store.list_inputs(portfolio_lead)} >= {"SIM-MSR-202610-005"}
    a = renv.delivery.pmo_decide(portfolio_lead, rid, 1, "approved", "Portfolio lead approves")
    assert a.ok
    renv.clock.set("2026-10-23T16:20:00Z")
    assert renv.delivery.publish(portfolio_lead, rid, 1, a.data["event_id"]).ok


def test_demo_slice_is_bounded(renv):
    cases = renv.msr_cases()
    roster = [c for c in cases if c["category"] == "roster-msr"]
    assert len(roster) == 55 and len(cases) == 65  # 50 original + 5 PMO staff (roster v3)
    assert {c["contract_id"] for c in roster} == {"SIM-FC-001"} and {c["month"] for c in roster} == {"2026-10"}
    assert len(renv.msr_cases("all")) == 450  # 440 Delivery MSRs + 10 test cases; Company MSRs excluded


def test_pmo_staff_msrs_align_to_pmo_reqs_not_a_task_order(renv):
    """PMO staff are not assigned to a TTO (like HR), but each PMO work role has a contract PMO REQ (Volume 02 §3), so
    their growth and development is captured too."""
    pmo = [c for c in renv.msr_cases("all") if 251 <= int(c["input"].get("subject_id", "X-000")[-3:]) <= 270]
    assert len(pmo) == 40 and {c["month"] for c in pmo} == {"2026-09", "2026-10"}
    assert {c["input"]["tto_id"] for c in pmo} == {f"SIM-TTO-{n}-PMO" for n in range(1, 5)}
    assert all(renv.registry.tto_lead_for(c["input"]["tto_id"]) is None for c in pmo)
    assert renv.registry.role_req_for("WR-101", "SIM-TTO-1-PMO")["role_req_id"] == "SIM-REQ-1-PMO-WR-101"
    renv.load_all_cases()
    renv.clock.set("2026-10-23T16:00:00Z")
    out = renv.delivery.employee_review(renv.principal("SIM-EMP-252", "employee"), "SIM-MSR-202610-252", 1, "accepted")
    a = out.data["assessment"]["assessment"]
    assert a["role_req_id"] == "SIM-REQ-1-PMO-WR-101" and a["aligned_req_task_ids"]
    assert all(t.startswith("SIM-REQ-1-PMO-") for t in a["aligned_req_task_ids"])
    assert out.data["assessment"]["created"]  # task alignments to publish


def test_pmo_peer_review_replaces_the_tto_lead_for_pmo_staff(renv):
    renv.load_all_cases()
    renv.clock.set("2026-10-23T16:00:00Z")
    rid = renv.delivery.employee_review(renv.principal("SIM-EMP-252", "employee"), "SIM-MSR-202610-252", 1,
                                        "accepted").data["assessment"]["created"][0]["record_id"]
    tr = renv.delivery.tto_review
    assert "own MSR" in tr(renv.principal("SIM-EMP-252", "pmo"), "SIM-MSR-202610-252", 1, "approved").reason
    assert not tr(renv.principal("SIM-EMP-002", "tto"), "SIM-MSR-202610-252", 1, "approved").ok  # a TTO lead
    assert not tr(renv.principal("SIM-EMP-256", "pmo"), "SIM-MSR-202610-252", 1, "approved").ok  # another PMO
    peer = renv.principal("SIM-EMP-253", "pmo")
    assert [r["input_id"] for r in renv.delivery.tto_review_queue(peer)] == ["SIM-MSR-202610-252"]
    out = tr(peer, "SIM-MSR-202610-252", 1, "approved")
    assert out.ok and out.data["review_basis"] == "pmo-peer"
    renv.clock.set("2026-10-23T16:15:00Z")
    own = renv.delivery.pmo_decide(renv.principal("SIM-EMP-252", "pmo"), rid, 1, "approved", "self")
    assert not own.ok and "separation-of-duties" in own.reason
    a = renv.delivery.pmo_decide(peer, rid, 1, "approved", "PMO teammate approves")
    renv.clock.set("2026-10-23T16:20:00Z")
    assert a.ok and renv.delivery.publish(peer, rid, 1, a.data["event_id"]).ok
    rec = renv.evidence_store.get_record(peer, rid)
    assert rec.evidence_summary.startswith("Employee Quinn Mercer (SIM-EMP-252, WR-101, SIM-TTO-1-PMO)")


def test_employees_review_only_their_own_msr(renv):
    renv.load_all_cases()
    other = renv.delivery.employee_review(renv.principal("SIM-EMP-006"), "SIM-MSR-202610-005", 1, "accepted")
    assert not other.ok and "not assigned" in other.reason
    own = renv.delivery.employee_review(renv.principal("SIM-EMP-005"), "SIM-MSR-202610-005", 1, "accepted")
    assert own.ok


def test_employee_sees_only_own_msrs(renv):
    from stratacaster.stores import PermissionDenied
    renv.load_all_cases()
    rowan = renv.principal("SIM-EMP-005")
    assert sorted(m.input_id for m in renv.delivery_store.list_inputs(rowan)) == [
        "SIM-MSR-202610-005", "SIM-MSR-TEST-01"]  # both belong to SIM-EMP-005
    for call in (lambda: renv.delivery_store.get_input(rowan, "SIM-MSR-202610-006"),
                 lambda: renv.delivery_store.get_statements(rowan, "SIM-MSR-202610-006", 1),
                 lambda: renv.delivery_store.review_events(rowan, "SIM-MSR-202610-006")):
        with pytest.raises(PermissionDenied):
            call()
    assert any(e["outcome"] == "denied" and e["refs"].get("input_id") == "SIM-MSR-202610-006"
               for e in renv.audit.entries())


def test_operator_and_pmo_see_only_their_contract(renv):
    renv.load_all_cases()  # 62 inputs: 61 in SIM-FC-001 (incl. 5 PMO staff MSRs), TC-10 in SIM-FC-002
    for uid, expected in (("SIM-EMP-002", 61), ("SIM-EMP-001", 61), ("SIM-EMP-051", 1)):
        visible = renv.delivery_store.list_inputs(renv.principal(uid))
        assert len(visible) == expected, uid
    assert {m.input_id for m in renv.delivery_store.list_inputs(renv.principal("SIM-EMP-051"))} == {"SIM-MSR-TEST-10"}


def test_employee_sees_only_records_from_own_msrs(demo):
    env, _ = demo
    seen = {r.source_ref.input_id for r in env.evidence_store.list_records(env.principal("SIM-EMP-005"))}
    assert seen and seen <= {"SIM-MSR-202610-005", "SIM-MSR-TEST-01"}
    other = next(r for r in env.evidence_store.list_records(env.principal("SIM-EMP-001"))
                 if r.source_ref.input_id == "SIM-MSR-202610-006")
    assert env.evidence_store.get_record(env.principal("SIM-EMP-005"), other.record_id) is None


def test_operator_scope_is_enforced(renv):
    case = next(c for c in renv.msr_cases() if c["case_id"] == "SIM-MSR-202610-005")
    fc2_operator = renv.registry.operator_for("SIM-DELIVERY-FC-002")
    out = renv.delivery.load_input(renv.principal(fc2_operator), case["input"])
    assert not out.ok and "operator scope" in out.reason


def test_test_cases_meet_expected_outcomes(demo):
    _, s = demo
    assert set(s["rejected"]) == {"TC-06", "TC-07", "TC-08"}
    assert "schema-invalid" in s["rejected"]["TC-06"] and "duplicate-conflict" in s["rejected"]["TC-07"]
    assert "reference-invalid" in s["rejected"]["TC-08"]
    assert set(s["denied_prep"]) == {"TC-04", "TC-05"}
    blocked_cases = {cid for cid, _ in s["blocked"].values()}
    assert blocked_cases == {"TC-01", "TC-02"}
    assert "no-pmo-authority" in s["cross_pmo_denial"]
    assert len(s["approve_denials"]) == len(s["blocked"])
    assert s["published"] == s["approved"]


def test_workflow_b_on_roster_evidence(demo):
    _, s = demo
    assert s["coverage"]["SIM-REQ-1-01-07"] == "conflict"  # TC-03 contradicts positive closure evidence
    assert s["coverage"]["SIM-REQ-1-01-01"] == "supported"
    assert s["after_revoke"] == ["supported"] and s["ack"] == "complete"
    assert s["deny"]["other_audience"][1] == 0
    assert s["deny"]["raw MSR request"] == "denied" and s["deny"]["personnel-ranking purpose"] == "denied"
    assert s["deny"]["store"] == "denied" and "operator scope" in s["deny"]["operator_scope"]
    assert s["after_expiry_citations"] == 0


def test_no_roster_narrative_leaves_delivery(demo):
    env, _ = demo
    blob = json.dumps(env.ops.responses()) + json.dumps(env.audit.entries(limit=100000))
    for c in env.msr_cases():
        assert c["input"]["raw_narrative"] not in blob


def test_import_is_reproducible(tmp_path):
    if not WORKBOOK.exists():
        pytest.skip(f"roster workbook {WORKBOOK.name} not present")
    try:
        WORKBOOK.open("rb").close()
    except PermissionError:
        pytest.skip(f"roster workbook {WORKBOOK.name} is open in another program (locked)")
    spec = importlib.util.spec_from_file_location("import_roster", ROOT / "scripts" / "import_roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(WORKBOOK, tmp_path)
    for name in ("reference_registry.json", "msr_cases.json", "import_manifest.json"):
        assert (tmp_path / name).read_text(encoding="utf-8") == (FIXTURES / name).read_text(encoding="utf-8"), name


