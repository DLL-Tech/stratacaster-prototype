"""Boundary violation scenarios. Traces: PR-01, PR-05, PR-14."""
import inspect
import json

import pytest

from helpers import OPS, SOL1, publish_inputs, query
from stratacaster import solutions
from stratacaster.agents import AgentNotAuthorized
from stratacaster.registry import GATEWAY_SERVICE, Principal
from stratacaster.stores import PermissionDenied


@pytest.mark.parametrize("method,args", [
    ("list_inputs", ()), ("get_input", ("SYN-MSR-001",)), ("get_statements", ("SYN-MSR-001", 1)),
    ("alignment_runs", ()), ("review_events", ("SYN-MSR-001",))])
def test_solutions_cannot_read_delivery_store(env, method, args):
    env.load_all_cases()
    with pytest.raises(PermissionDenied):
        getattr(env.delivery_store, method)(env.principal(SOL1), *args)


@pytest.mark.parametrize("who", [SOL1, "AGT-SOLUTIONS-ORCHESTRATOR", "AGT-SOLUTIONS-EXPERTISE"])
def test_solutions_cannot_read_evidence_store_directly(env, who):
    with pytest.raises(PermissionDenied):
        env.evidence_store.list_records(env.principal(who))


def test_gateway_cannot_read_delivery_store(env):
    with pytest.raises(PermissionDenied):
        env.delivery_store.list_inputs(GATEWAY_SERVICE)


def test_shared_service_without_gateway_role_denied(env):
    impostor = Principal("SVC-OTHER", "service", "shared", frozenset({"something"}))
    with pytest.raises(PermissionDenied):
        env.evidence_store.list_records(impostor)


def test_solutions_cannot_write_evidence(env):
    publish_inputs(env, "SYN-MSR-001")
    rec = env.evidence_store.get_record(env.principal(OPS), "SYN-EVID-001")
    with pytest.raises(PermissionDenied):
        env.evidence_store.put_record(env.principal(SOL1), rec, "tamper")


def test_raw_source_request_denied(env):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, include_raw_sources=True)
    assert r["status"] == "denied" and "raw MSR" in r["reason"]


def test_response_contains_no_raw_narrative_or_locator(env):
    publish_inputs(env, "SYN-MSR-001", "SYN-MSR-002")
    r = query(env, req_task_ids=["SYN-REQ-API-001", "SYN-REQ-DATA-001", "SYN-REQ-DATA-002"])
    blob = json.dumps(r)
    for case in env.msr_cases():
        assert case["input"]["raw_narrative"] not in blob
        assert case["input"]["source_locator"] not in blob
    for key in ("raw_narrative", "source_locator", "notes", "statement_text"):
        assert f'"{key}"' not in blob


def test_orchestrator_has_no_delivery_store_reference(env):
    for obj in (env.orchestrator, env.gateway, env.expertise_agent):
        for value in vars(obj).values():
            assert value is not env.delivery_store
    assert "DeliveryStore" not in inspect.getsource(solutions)


def test_boundary_key_scanner_finds_nested_fields():
    """The scanner used by the evidence store, gateway, audit log, and escalations finds nested keys."""
    from stratacaster.stores import find_delivery_only_keys
    assert find_delivery_only_keys({"a": [{"raw_narrative": 1}]}) == ["a[0].raw_narrative"]


def test_unregistered_agent_cannot_be_invoked(env):
    with pytest.raises(AgentNotAuthorized):
        env.agent_registry.require_authorized("AGT-PMO-CONTRACT")
    assert {a["agent_id"] for a in env.agent_registry.records.values()} == {
        "AGT-ROLE-EXECUTION", "AGT-SOLUTIONS-ORCHESTRATOR", "AGT-SOLUTIONS-EXPERTISE",
        "AGT-TTO-COORDINATION"}  # REQ-change planning pilot (CR-SC-2026-003)
