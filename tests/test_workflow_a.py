"""Workflow A - publish filtered evidence. Traces: PR-02, PR-03, PR-04, PR-06, PR-07."""
from helpers import (APPROVE_AT, OPS, PMO1, PMO2, PUBLISH_AT, approve_publish, load_and_review, p, prepare,
                     records_for)


def test_happy_path_publishes_filtered_record(env):
    load_and_review(env, "SYN-MSR-001")
    out = prepare(env, "SYN-MSR-001")
    assert out.ok and len(out.data["created"]) == 1
    (rec,) = records_for(env, "SYN-MSR-001")
    assert rec.disposition == "draft" and rec.pmo_approval.status == "pending"
    assert rec.req_task_id == "SYN-REQ-API-001" and rec.role_task_id == "SYN-ROLE-TASK-VALIDATE"
    a, pub = approve_publish(env, rec)
    assert a.ok and pub.ok
    final = env.evidence_store.get_record(p(env, OPS), rec.record_id, 1)
    assert final.disposition == "published" and final.published_at == PUBLISH_AT
    assert final.pmo_approval.approved_at == APPROVE_AT and final.pmo_approval.approved_record_version == 1


def test_load_rejects_missing_duplicate_and_bad_reference(env):
    results = {r["case_id"]: r for r in env.load_all_cases()}
    assert not results["CASE-04-MISSING-FIELD"]["ok"]
    assert "duplicate-conflict" in results["CASE-05-DUPLICATE-CONFLICT"]["reason"]
    assert "reference-invalid" in results["CASE-12-UNKNOWN-REFERENCE"]["reason"]
    again = env.load_all_cases()  # identical reload is idempotent
    assert next(r for r in again if r["case_id"] == "CASE-01-SUPPORTED")["reason"] == "duplicate-identical-ignored"


def test_load_refuses_prereviewed_input(env):
    from helpers import guide
    out = env.delivery.load_input(p(env, OPS), guide("msr_input_example.json"))
    assert not out.ok and "employee-review action" in out.reason


def test_unreviewed_input_cannot_prepare(env):
    load_and_review(env)  # nothing reviewed
    out = prepare(env, "SYN-MSR-009")
    assert not out.ok and "pending" in out.reason
    assert records_for(env, "SYN-MSR-009") == []


def test_rejected_input_cannot_prepare(env):
    load_and_review(env, "SYN-MSR-010")
    out = prepare(env, "SYN-MSR-010")
    assert not out.ok and "rejected" in out.reason


def test_only_assigned_reviewer_may_review(env):
    env.load_all_cases()
    out = env.delivery.employee_review(p(env, "SYN-REVIEWER-EMP-002"), "SYN-MSR-001", 1, "accepted")
    assert not out.ok and "not assigned" in out.reason
    out = env.delivery.employee_review(p(env, "AGT-ROLE-EXECUTION"), "SYN-MSR-001", 1, "accepted")
    assert not out.ok and "human" in out.reason


def test_review_decision_is_immutable(env):
    load_and_review(env, "SYN-MSR-001")
    out = env.delivery.employee_review(p(env, "SYN-REVIEWER-EMP-001"), "SYN-MSR-001", 1, "rejected")
    assert not out.ok and "already recorded" in out.reason


def test_ambiguous_input_records_gap_and_creates_nothing(env):
    load_and_review(env, "SYN-MSR-003")
    out = prepare(env, "SYN-MSR-003")
    assert out.ok and out.data["created"] == [] and len(out.data["gaps"]) == 1


def test_pmo_reject_blocks_publication(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set(APPROVE_AT)
    r = env.delivery.pmo_decide(p(env, PMO1), rec.record_id, 1, "rejected", "Fictional rejection.")
    assert r.ok
    env.clock.set(PUBLISH_AT)
    pub = env.delivery.publish(p(env, PMO1), rec.record_id, 1, r.data["event_id"])
    assert not pub.ok and "event decision is rejected" in pub.reason
    assert env.evidence_store.get_record(p(env, OPS), rec.record_id).disposition == "draft"


def test_return_for_correction_then_revision_and_new_approval(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set(APPROVE_AT)
    r = env.delivery.pmo_decide(p(env, PMO1), rec.record_id, 1, "returned_for_correction", "Tighten the summary.")
    assert r.ok
    blocked = env.delivery.pmo_decide(p(env, PMO1), rec.record_id, 1, "approved", "Try anyway.")
    assert not blocked.ok
    rev = env.delivery.revise_evidence(p(env, OPS), rec.record_id, "Synthetic validation evidence for the API task.")
    assert rev.ok and rev.data["record_version"] == 2
    v2 = env.evidence_store.get_record(p(env, OPS), rec.record_id, 2)
    a, pub = approve_publish(env, v2)
    assert a.ok and pub.ok


def test_cross_pmo_reviewer_cannot_approve(env):
    load_and_review(env, "SYN-MSR-011")
    prepare(env, "SYN-MSR-011")
    (rec,) = records_for(env, "SYN-MSR-011")
    a, _ = approve_publish(env, rec, PMO1)
    assert not a.ok and "no-pmo-authority" in a.reason
    a, pub = approve_publish(env, rec, PMO2)
    assert a.ok and pub.ok


def test_non_pmo_users_cannot_approve(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set(APPROVE_AT)
    for who in (OPS, "SYN-REVIEWER-EMP-001", "AGT-ROLE-EXECUTION"):
        assert not env.delivery.pmo_decide(p(env, who), rec.record_id, 1, "approved", "x").ok


def test_publication_without_approval_event_is_denied(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set(PUBLISH_AT)
    pub = env.delivery.publish(p(env, PMO1), rec.record_id, 1, "SYN-PMO-APPROVAL-FORGED")
    assert not pub.ok and "not found" in pub.reason
    pubs = env.evidence_store.publications(p(env, OPS), rec.record_id)
    assert pubs[-1]["outcome"] == "denied"


def test_approval_is_not_publication(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set(APPROVE_AT)
    a = env.delivery.pmo_decide(p(env, PMO1), rec.record_id, 1, "approved", "ok")
    after = env.evidence_store.get_record(p(env, OPS), rec.record_id)
    assert a.ok and after.pmo_approval.status == "approved"
    assert after.disposition == "draft" and after.published_at is None


def test_restricted_content_cannot_be_approved(env):
    load_and_review(env, "SYN-MSR-007")
    prepare(env, "SYN-MSR-007")
    (rec,) = records_for(env, "SYN-MSR-007")
    assert any(e.startswith("BLOCKING:source-email") for e in rec.exceptions)
    a, _ = approve_publish(env, rec)
    assert not a.ok and "prerequisites-failed" in a.reason


def test_summary_copying_narrative_is_blocked(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    narrative = env.delivery_store.get_input(p(env, OPS), "SYN-MSR-001").raw_narrative
    rev = env.delivery.revise_evidence(p(env, OPS), rec.record_id, narrative)
    v2 = env.evidence_store.get_record(p(env, OPS), rec.record_id, rev.data["record_version"])
    assert "BLOCKING:verbatim-narrative-in-summary" in v2.exceptions
    a, _ = approve_publish(env, v2)
    assert not a.ok


def test_rerun_preparation_is_idempotent(env):
    load_and_review(env, "SYN-MSR-002")
    prepare(env, "SYN-MSR-002")
    out = prepare(env, "SYN-MSR-002")
    assert out.ok and out.data["created"] == []
    assert len(records_for(env, "SYN-MSR-002")) == 2
