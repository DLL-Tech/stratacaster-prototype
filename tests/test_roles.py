"""Acting in one role at a time, and separation of duties for managers who also submit MSRs (PR-18)."""
import copy

import pytest

from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

PREP, APPROVE, PUBLISH = "2026-10-23T16:00:00Z", "2026-10-23T16:15:00Z", "2026-10-23T16:20:00Z"


@pytest.fixture
def renv(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    yield e
    e.close()


def test_role_options_match_each_persons_roles(renv):
    r = renv.registry
    assert [k for k, _ in r.role_options("SIM-EMP-002")] == ["employee", "operator", "tto"]  # Mission Tech. Director
    assert [k for k, _ in r.role_options("SIM-EMP-001")] == ["employee", "pmo"]        # Program Manager
    assert [k for k, _ in r.role_options("SIM-EMP-005")] == ["employee"]
    assert [k for k, _ in r.role_options("SIM-EMP-210")] == ["solutions"]
    with pytest.raises(KeyError):
        r.principal("SIM-EMP-005", "pmo")  # cannot claim a role one does not hold


def test_manager_acting_as_employee_has_no_management_authority(renv):
    renv.load_all_cases()
    maya = renv.principal("SIM-EMP-002", "employee")
    assert maya.roles == {"employee_reviewer"} and not maya.delivery_scopes
    assert [m.input_id for m in renv.delivery_store.list_inputs(maya)] == ["SIM-MSR-202610-002"]
    out = renv.delivery.prepare_candidates(maya, "SIM-MSR-202610-005")
    assert not out.ok and "Delivery operator" in out.reason
    avery = renv.principal("SIM-EMP-001", "employee")
    assert [m.input_id for m in renv.delivery_store.list_inputs(avery)] == ["SIM-MSR-202610-001"]
    assert renv.evidence_store.list_records(avery) == []


def test_operator_cannot_prepare_own_msr(renv):
    renv.load_all_cases()
    assert renv.delivery.employee_review(renv.principal("SIM-EMP-002", "employee"), "SIM-MSR-202610-002", 1,
                                         "accepted").ok
    renv.clock.set(PREP)
    own = renv.delivery.prepare_candidates(renv.principal("SIM-EMP-002", "operator"), "SIM-MSR-202610-002")
    assert not own.ok and "separation-of-duties" in own.reason
    other = renv.delivery.prepare_candidates(renv.principal("SIM-EMP-003", "operator"), "SIM-MSR-202610-002")
    assert other.ok  # another operator in the same contract may re-run the assessment


def _avery_evidence(renv):
    """An MSR whose subject is the PMO reviewer, aligned to a REQ task so it produces a draft record."""
    base = next(c for c in renv.msr_cases() if c["case_id"] == "SIM-MSR-202610-005")["input"]
    inp = copy.deepcopy(base) | {"input_id": "SIM-MSR-TEST-SOD", "subject_id": "SIM-EMP-001",
                                 "source_locator": "delivery-local:SIM-MSR-TEST-SOD:v1",
                                 "raw_narrative": "I checked the reported fixes again and recorded what had changed."}
    assert renv.delivery.load_input(renv.principal("SIM-EMP-002", "operator"), inp).ok
    renv.clock.set(PREP)
    out = renv.delivery.employee_review(renv.principal("SIM-EMP-001", "employee"), "SIM-MSR-TEST-SOD", 1, "accepted")
    assert out.ok and out.data["assessment"]["created"]  # REQ assessment runs on the employee's acceptance
    assert renv.delivery.tto_review(renv.principal("SIM-EMP-002", "tto"), "SIM-MSR-TEST-SOD", 1, "approved").ok
    return out.data["assessment"]["created"][0]["record_id"]


def test_pmo_cannot_approve_evidence_from_own_msr(renv):
    rid = _avery_evidence(renv)
    renv.clock.set(APPROVE)
    out = renv.delivery.pmo_decide(renv.principal("SIM-EMP-001", "pmo"), rid, 1, "approved", "self approval")
    assert not out.ok and "separation-of-duties" in out.reason
    rec = renv.evidence_store.get_record(renv.principal("SIM-EMP-001", "pmo"), rid)
    assert rec.pmo_approval.status == "pending" and rec.disposition == "draft"


def test_policy_switch_lets_management_authority_cover_own_msr(renv):
    renv.cfg["governance"]["management_authority_covers_own_msr"] = True
    rid = _avery_evidence(renv)
    avery = renv.principal("SIM-EMP-001", "pmo")
    renv.clock.set(APPROVE)
    a = renv.delivery.pmo_decide(avery, rid, 1, "approved", "allowed by policy")
    assert a.ok
    renv.clock.set(PUBLISH)
    assert renv.delivery.publish(avery, rid, 1, a.data["event_id"]).ok


def test_acting_role_is_audited(renv):
    renv.load_all_cases()
    renv.delivery.employee_review(renv.principal("SIM-EMP-002", "employee"), "SIM-MSR-202610-002", 1, "accepted")
    entry = next(e for e in renv.audit.entries() if e["action"] == "employee_review")
    assert entry["actor"] == "SIM-EMP-002" and entry["meta"]["acting_role"] == "employee"
