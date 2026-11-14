"""Test P-VERSION-001 - Reject approval reuse across evidence versions (Build Guide Section 5).

Uses the Section 2 fixtures verbatim: SYN-EVID-001 v1, SYN-PMO-APPROVAL-001 (v1), SYN-PMO-APPROVAL-002 (v2).
Test clock fixed at 2026-10-16T16:19:00Z so expiration cannot explain any denial. Traces: PR-03, PR-04.
"""
import copy

import pytest

from helpers import OPS, PMO1, guide, load_and_review
from stratacaster.schemas import FilteredEvidence, PmoApprovalEvent

TEST_CLOCK = "2026-10-16T16:19:00Z"
V2_SUMMARY = "Synthetic API validation yielded a test report documenting execution of the validation procedure."


def _setup_v1(env):
    """Reference fixtures + reviewed SYN-MSR-001 v1 + SYN-EVID-001 v1 as a pending draft."""
    load_and_review(env, "SYN-MSR-001")  # fictional review at 2026-10-16T15:00:00Z
    ops = env.principal(OPS)
    narrative = env.delivery_store.get_input(ops, "SYN-MSR-001").raw_narrative
    env.delivery_store.put_statements(ops, "SYN-MSR-001", 1, [("SYN-STMT-001", narrative)])
    data = guide("evidence_v1_example.json") | {
        "pmo_approval": {"status": "pending", "approver_id": None, "approved_at": None, "approved_record_version": None},
        "disposition": "draft", "published_at": None}
    env.clock.set("2026-10-16T16:00:00Z")
    env.evidence_store.put_record(ops, FilteredEvidence.model_validate(data), action="fixture-load")
    env.clock.set(TEST_CLOCK)


def _event(name):
    return PmoApprovalEvent.model_validate(guide(name))


def _rec(env, v):
    return env.evidence_store.get_record(env.principal(OPS), "SYN-EVID-001", v)


def _assert_v2_untouched(env):
    v2 = _rec(env, 2)
    assert v2.pmo_approval.status == "pending" and v2.pmo_approval.approved_record_version is None
    assert v2.disposition == "draft" and v2.published_at is None


@pytest.fixture
def v1_approved(env):
    _setup_v1(env)
    pos = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"))
    assert pos.ok, pos.reason
    return env


def test_positive_control_v1_is_eligible_but_not_published(v1_approved):
    env = v1_approved
    assert env.delivery.publication_check(env.principal(PMO1), "SYN-EVID-001", 1, "SYN-PMO-APPROVAL-001") == []
    v1 = _rec(env, 1)
    assert (v1.pmo_approval.status, v1.pmo_approval.approver_id, v1.pmo_approval.approved_at,
            v1.pmo_approval.approved_record_version) == ("approved", PMO1, "2026-10-16T16:15:00Z", 1)
    assert v1.disposition == "draft" and v1.published_at is None  # approval alone is not publication


def test_version_mismatch_direct_comparison(env):
    """Event 001 submitted for v2 before it was ever processed: compares event.record_version with target."""
    _setup_v1(env)
    assert env.delivery.revise_evidence(env.principal(OPS), "SYN-EVID-001", V2_SUMMARY).ok
    out = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"),
                                              target_record_version=2)
    assert not out.ok and "version-mismatch: event approves version 1, target is version 2" in out.reason
    _assert_v2_untouched(env)
    assert env.evidence_store.get_approval_event(env.principal(OPS), "SYN-PMO-APPROVAL-001") is None


def test_version_mismatch_after_v1_approval_and_replay(v1_approved):
    env = v1_approved
    ops = env.principal(OPS)
    original = env.evidence_store.get_approval_event(ops, "SYN-PMO-APPROVAL-001").model_dump()
    assert env.delivery.revise_evidence(ops, "SYN-EVID-001", V2_SUMMARY).ok
    _assert_v2_untouched(env)

    mismatch = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"),
                                                   target_record_version=2)
    assert not mismatch.ok and "version-mismatch" in mismatch.reason
    _assert_v2_untouched(env)

    replay_v2 = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"),
                                                    target_record_version=2)
    assert not replay_v2.ok and "version-mismatch" in replay_v2.reason
    replay_v1 = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"))
    assert replay_v1.ok and replay_v1.reason == "duplicate-ignored"

    assert len(env.evidence_store.all_approval_events(ops)) == 1  # no new approval created
    assert env.evidence_store.get_approval_event(ops, "SYN-PMO-APPROVAL-001").model_dump() == original
    assert original == _event("pmo_approval_001.json").model_dump()
    _assert_v2_untouched(env)
    denials = [x for x in env.evidence_store.processing_log(ops, "SYN-PMO-APPROVAL-001") if x["outcome"] == "denied"]
    assert len(denials) == 2 and all("version-mismatch" in d["reason"] for d in denials)


@pytest.mark.parametrize("tamper_version", [1, 2])
def test_publication_bypass_with_copied_approval_fields(v1_approved, tamper_version):
    """Disposable fixture: v1's approval fields are copied into v2, then publication is requested."""
    env = v1_approved
    ops = env.principal(OPS)
    assert env.delivery.revise_evidence(ops, "SYN-EVID-001", V2_SUMMARY).ok
    tampered = copy.deepcopy(_rec(env, 2).model_dump())
    tampered["pmo_approval"] = _rec(env, 1).pmo_approval.model_dump() | {"approved_record_version": tamper_version}
    env.evidence_store.put_record(ops, FilteredEvidence.model_validate(tampered), action="TEST-tamper")

    out = env.delivery.publish(env.principal(PMO1), "SYN-EVID-001", 2, "SYN-PMO-APPROVAL-001")
    assert not out.ok and "version-mismatch" in out.reason
    v2 = _rec(env, 2)
    assert v2.disposition == "draft" and v2.published_at is None
    r = env.orchestrator.handle(env.principal("SYN-SOLUTIONS-USER-001"), {
        "requester_id": "SYN-SOLUTIONS-USER-001", "audience_id": "SYN-SOLUTIONS-DEMO",
        "purpose": "requirement-evidence-explanation", "req_task_ids": ["SYN-REQ-API-001"], "question": "q"})
    assert r["citations"] == []
    assert env.evidence_store.publications(ops, "SYN-EVID-001")[-1]["outcome"] == "denied"


def test_recovery_control_requires_distinct_v2_approval(v1_approved):
    env = v1_approved
    ops = env.principal(OPS)
    v1_before = _rec(env, 1).model_dump()
    assert env.delivery.revise_evidence(ops, "SYN-EVID-001", V2_SUMMARY).ok
    assert not env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_001.json"),
                                                   target_record_version=2).ok
    assert env.delivery.publication_check(env.principal(PMO1), "SYN-EVID-001", 2, "SYN-PMO-APPROVAL-001")

    rec = env.delivery.process_approval_event(env.principal(PMO1), _event("pmo_approval_002.json"))
    assert rec.ok, rec.reason
    v2 = _rec(env, 2)
    assert (v2.pmo_approval.status, v2.pmo_approval.approver_id, v2.pmo_approval.approved_at,
            v2.pmo_approval.approved_record_version) == ("approved", PMO1, "2026-10-16T16:18:00Z", 2)
    assert v2.disposition == "draft" and v2.published_at is None  # approval processing alone does not publish
    assert env.delivery.publication_check(env.principal(PMO1), "SYN-EVID-001", 2, "SYN-PMO-APPROVAL-002") == []
    assert _rec(env, 1).model_dump() == v1_before  # v1 approval remains bound to v1
    assert env.evidence_store.get_approval_event(ops, "SYN-PMO-APPROVAL-001").record_version == 1


def test_asserted_validation_snapshot_is_not_trusted(env):
    """An event claiming all checks passed is still denied when real checks fail."""
    _setup_v1(env)
    ops = env.principal(OPS)
    src = env.delivery_store.get_input(ops, "SYN-MSR-001")
    env.delivery_store.update_input_metadata(ops, src.model_copy(update={"source_status": "superseded"}))
    event = _event("pmo_approval_001.json")
    assert event.validation_snapshot.source_current is True  # the event asserts success
    out = env.delivery.process_approval_event(env.principal(PMO1), event)
    assert not out.ok and "prerequisites-failed" in out.reason and "source_current" in out.reason


def test_event_submitted_by_wrong_identity_is_denied(env):
    _setup_v1(env)
    for who in ("SYN-REVIEWER-PMO-002", "AGT-ROLE-EXECUTION", OPS):
        assert not env.delivery.process_approval_event(env.principal(who), _event("pmo_approval_001.json")).ok
