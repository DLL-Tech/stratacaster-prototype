"""Audit trail: append-only, correlated, and free of raw content. Traces: PR-13."""
import json
import sqlite3

import pytest

from helpers import publish_inputs, query


def test_audit_is_append_only(env):
    publish_inputs(env, "SYN-MSR-001")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        env.audit.db.execute("UPDATE audit SET outcome='edited'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        env.audit.db.execute("DELETE FROM audit")
    for table in ("approval_events", "publication_events", "evidence_history", "approval_processing"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            env.evidence_store.db.execute(f"DELETE FROM {table}")


def test_audit_contains_no_raw_content(env):
    from helpers import approve_publish, load_and_review, prepare, records_for
    publish_inputs(env, "SYN-MSR-001")
    load_and_review(env, "SYN-MSR-007", "SYN-MSR-008")
    for iid in ("SYN-MSR-007", "SYN-MSR-008"):
        prepare(env, iid)
        assert not approve_publish(env, records_for(env, iid)[0])[0].ok
    query(env)
    blob = json.dumps(env.audit.entries(limit=100000))
    for case in env.msr_cases():
        assert case["input"]["raw_narrative"] not in blob
        assert case["input"]["source_locator"] not in blob


def test_audit_refuses_raw_fields(env):
    with pytest.raises(ValueError, match="Delivery-only"):
        env.audit.record("x", "test", "ok", refs={"raw_narrative": "secret"})


def test_workflow_b_entries_are_correlated_with_model_metadata(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, query_id="Q-AUD")
    entries = env.audit.entries(correlation_id=r["correlation_id"])
    actions = {e["action"] for e in entries}
    assert {"retrieve", "workflow_b"} <= actions
    wf = next(e for e in entries if e["action"] == "workflow_b")
    assert wf["meta"]["prompt_version"] == "expertise_v1" and "latency_ms" in wf["meta"]


def test_denials_are_audited(env):
    from stratacaster.stores import PermissionDenied
    with pytest.raises(PermissionDenied):
        env.delivery_store.list_inputs(env.principal("SYN-SOLUTIONS-USER-001"))
    assert any(e["outcome"] == "denied" and e["action"] == "delivery_store.list_inputs"
               for e in env.audit.entries())
