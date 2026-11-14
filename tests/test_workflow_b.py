"""Workflow B - retrieve and explain approved evidence. Traces: PR-05, PR-08, PR-15."""
from helpers import SOL1, SOL2, publish_inputs, query


def test_supported_query_returns_cited_current_records(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env)
    assert r["status"] == "awaiting_acknowledgement"
    assert r["coverage"] == [{"req_task_id": "SYN-REQ-API-001", "status": "supported",
                              "cited_record_ids": ["SYN-EVID-001"], "note": r["coverage"][0]["note"]}]
    assert [c["record_id"] for c in r["citations"]] == ["SYN-EVID-001"]
    assert "[SYN-EVID-001 v1]" in r["explanation"]
    assert r["label"].startswith("PROTOTYPE OUTPUT")


def test_missing_evidence_states_gap_and_routes_to_delivery(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, req_task_ids=["SYN-REQ-DATA-002"])
    assert r["status"] == "escalated"
    assert r["coverage"][0]["status"] == "unsupported" and r["coverage"][0]["cited_record_ids"] == []
    (esc,) = env.ops.escalations()
    assert esc["kind"] == "insufficient-filtered-evidence" and esc["route_domain"] == "delivery"
    assert esc["route_to"] == "SYN-DELIVERY-OPS-001"


def test_contradictory_evidence_is_preserved_and_escalated(env):
    publish_inputs(env, "SYN-MSR-001", "SYN-MSR-006")
    r = query(env)
    (c,) = r["coverage"]
    assert c["status"] == "conflict" and sorted(c["cited_record_ids"]) == ["SYN-EVID-001", "SYN-EVID-002"]
    assert r["status"] == "escalated"
    (esc,) = env.ops.escalations()
    assert esc["kind"] == "evidence-conflict" and esc["route_to"] == SOL1


def test_different_audience_does_not_retrieve(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, user=SOL2, audience_id="SYN-SOLUTIONS-OTHER")
    assert r["coverage"][0]["status"] == "unsupported" and r["citations"] == []
    assert r["records_withheld"] == 1


def test_requester_must_be_audience_member(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, user=SOL2)  # SOL2 claims the demo audience
    assert r["status"] == "denied" and "not a member" in r["reason"]


def test_requester_identity_must_match(env):
    r = env.orchestrator.handle(env.principal(SOL2), {
        "requester_id": SOL1, "audience_id": "SYN-SOLUTIONS-DEMO", "purpose": "requirement-evidence-explanation",
        "req_task_ids": ["SYN-REQ-API-001"], "question": "q"})
    assert r["status"] == "denied"


def test_pending_record_is_not_retrieved(env):
    from helpers import load_and_review, prepare
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    r = query(env)
    assert r["citations"] == [] and r["records_withheld"] == 0  # drafts are never candidates


def test_unknown_task_denied(env):
    r = query(env, req_task_ids=["SYN-REQ-NOPE"])
    assert r["status"] == "denied"


def test_completion_requires_reviewer_acknowledgement(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, query_id="Q-ACK")
    assert env.ops.get_response("Q-ACK")["status"] == "awaiting_acknowledgement"
    assert not env.orchestrator.acknowledge(env.principal(SOL2), "Q-ACK")["ok"]
    assert env.orchestrator.acknowledge(env.principal(SOL1), "Q-ACK")["ok"]
    assert env.ops.get_response("Q-ACK")["status"] == "complete"
    assert r["status"] == "awaiting_acknowledgement"


def test_cannot_complete_with_open_escalation_until_named_reviewer_resolves(env):
    publish_inputs(env, "SYN-MSR-001", "SYN-MSR-006")
    query(env, query_id="Q-CONFLICT")
    assert not env.orchestrator.acknowledge(env.principal(SOL1), "Q-CONFLICT")["ok"]
    (esc,) = env.ops.escalations(status="open")
    assert not env.orchestrator.resolve_escalation(env.principal(SOL2), esc["escalation_id"], "x")["ok"]
    assert env.orchestrator.resolve_escalation(env.principal(SOL1), esc["escalation_id"], "Reviewed.")["ok"]
    assert env.orchestrator.acknowledge(env.principal(SOL1), "Q-CONFLICT")["ok"]
