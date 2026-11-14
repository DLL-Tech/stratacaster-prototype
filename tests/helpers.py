"""Shared test helpers. Identifiers and times follow the synthetic fixtures."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = "SYN-DELIVERY-OPS-001"
PMO1, PMO2 = "SYN-REVIEWER-PMO-001", "SYN-REVIEWER-PMO-002"
SOL1, SOL2 = "SYN-SOLUTIONS-USER-001", "SYN-SOLUTIONS-USER-002"
PREP_AT, APPROVE_AT, PUBLISH_AT, QUERY_AT = ("2026-10-16T16:00:00Z", "2026-10-16T16:15:00Z",
                                             "2026-10-16T16:20:00Z", "2026-10-16T17:00:00Z")


def guide(name: str) -> dict:
    return json.loads((ROOT / "data" / "fixtures" / "guide_examples" / name).read_text(encoding="utf-8"))


def p(env, user_id):
    return env.principal(user_id)


def load_and_review(env, *input_ids: str) -> None:
    """Loads every case, then applies the scripted employee review for the listed inputs."""
    env.load_all_cases()
    for case in env.msr_cases():
        rv, iid = case["scripted_review"], case["input"]["input_id"]
        if rv and iid in input_ids:
            env.clock.set(rv["at"])
            out = env.delivery.employee_review(p(env, rv["reviewer_id"]), iid, 1, rv["decision"])
            assert out.ok, out.reason


def prepare(env, input_id: str):
    env.clock.set(PREP_AT)
    return env.delivery.prepare_candidates(p(env, OPS), input_id)


def records_for(env, input_id: str):
    return [r for r in env.evidence_store.list_records(p(env, OPS)) if r.source_ref.input_id == input_id]


def approve_publish(env, rec, reviewer: str = PMO1):
    env.clock.set(APPROVE_AT)
    a = env.delivery.pmo_decide(p(env, reviewer), rec.record_id, rec.record_version, "approved", "Fictional approval.")
    if not a.ok:
        return a, None
    env.clock.set(PUBLISH_AT)
    return a, env.delivery.publish(p(env, reviewer), rec.record_id, rec.record_version, a.data["event_id"])


def publish_inputs(env, *input_ids: str) -> None:
    load_and_review(env, *input_ids)
    for iid in input_ids:
        assert prepare(env, iid).ok
        for rec in records_for(env, iid):
            a, pub = approve_publish(env, rec)
            assert a.ok and pub.ok, (a.reason, pub and pub.reason)


def query(env, user: str = SOL1, **kw) -> dict:
    env.clock.set(kw.pop("at", QUERY_AT))
    q = {"requester_id": user, "audience_id": "SYN-SOLUTIONS-DEMO", "purpose": "requirement-evidence-explanation",
         "req_task_ids": ["SYN-REQ-API-001"], "question": "Which approved evidence covers this requirement?"} | kw
    return env.orchestrator.handle(p(env, user), q)
