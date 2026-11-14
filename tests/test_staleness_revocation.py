"""Staleness, revocation, correction, and withdrawal. Traces: PR-05, PR-09, PR-15."""
from helpers import OPS, PMO1, SOL1, publish_inputs, query


def test_expired_record_is_denied(env):
    publish_inputs(env, "SYN-MSR-001")
    assert query(env, at="2026-11-16T15:59:59Z")["coverage"][0]["status"] == "supported"
    r = query(env, at="2026-11-16T16:00:00Z")  # at expires_at is already expired
    assert r["coverage"][0]["status"] == "unsupported" and r["records_withheld"] == 1


def test_revoked_record_ceases_to_appear_and_cache_is_invalidated(env):
    publish_inputs(env, "SYN-MSR-001")
    query(env)
    query(env)
    assert env.gateway.cache_hits >= 1
    out = env.delivery.revoke(env.principal(PMO1), "SYN-EVID-001", 1, "Fictional revocation.")
    assert out.ok
    r = query(env)
    assert r["citations"] == []
    rec = env.evidence_store.get_record(env.principal(OPS), "SYN-EVID-001")
    assert rec.disposition == "revoked" and rec.pmo_approval.status == "revoked"


def test_revocation_does_not_overwrite_original_approval_event(env):
    publish_inputs(env, "SYN-MSR-001")
    before = env.evidence_store.all_approval_events(env.principal(OPS))
    env.delivery.revoke(env.principal(PMO1), "SYN-EVID-001", 1, "Fictional revocation.")
    after = env.evidence_store.all_approval_events(env.principal(OPS))
    assert after[0] == before[0] and len(after) == 2 and after[1].decision == "revoked"


def test_source_correction_supersedes_dependents(env):
    publish_inputs(env, "SYN-MSR-001")
    out = env.delivery.correct_input(env.principal("SYN-REVIEWER-EMP-001"), "SYN-MSR-001",
                                     "Corrected synthetic statement about the API validation procedure.")
    assert out.ok and out.data["invalidated_records"] == 1
    assert env.evidence_store.get_record(env.principal(OPS), "SYN-EVID-001").disposition == "superseded"
    assert query(env)["citations"] == []
    new = env.delivery_store.get_input(env.principal(OPS), "SYN-MSR-001")
    assert new.input_version == 2 and new.review.status == "pending"


def test_source_withdrawal_revokes_dependents(env):
    publish_inputs(env, "SYN-MSR-001")
    assert env.delivery.withdraw_input(env.principal(OPS), "SYN-MSR-001").ok
    assert env.evidence_store.get_record(env.principal(OPS), "SYN-EVID-001").disposition == "revoked"
    assert query(env)["citations"] == []


def test_acknowledgement_detects_stale_citation(env):
    publish_inputs(env, "SYN-MSR-001")
    query(env, query_id="Q-STALE")
    env.delivery.revoke(env.principal(PMO1), "SYN-EVID-001", 1, "Fictional revocation.")
    ack = env.orchestrator.acknowledge(env.principal(SOL1), "Q-STALE")
    assert not ack["ok"] and ack["stale"][0]["record_id"] == "SYN-EVID-001"
    assert env.ops.get_response("Q-STALE")["status"] == "stale"


def test_expired_record_cannot_be_published(env):
    from helpers import load_and_review, prepare, records_for
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    env.clock.set("2026-10-16T16:15:00Z")
    a = env.delivery.pmo_decide(env.principal(PMO1), rec.record_id, 1, "approved", "ok")
    env.clock.set("2026-12-01T00:00:00Z")
    pub = env.delivery.publish(env.principal(PMO1), rec.record_id, 1, a.data["event_id"])
    assert not pub.ok and "expired" in pub.reason
