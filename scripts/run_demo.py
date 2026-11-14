"""Scripted end-to-end demonstration (Build Guide Section 6, "Demonstrate").

Resets the environment, runs Workflow A and Workflow B on the synthetic dataset, and shows the deny
paths: raw-MSR request, wrong audience, prohibited purpose, cross-PMO approval, blocked content,
revocation, and expiration.

Usage:  python scripts/run_demo.py [--var-dir DIR] [--pace SECONDS] [--dataset roster|guide] [--no-dialogue]
The roster dataset (default) runs stratacaster/demo.py on the notional roster workbook; the guide dataset runs
the Build Guide fixture sequence below.
Use --pace (for example 2) with the Workflow Monitor page open so the audience can watch each step light up.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stratacaster.environment import Environment  # noqa: E402

PACE = 0.0


def pause() -> None:
    if PACE:
        time.sleep(PACE)


def show(title: str, outcome) -> None:
    ok = outcome.ok if hasattr(outcome, "ok") else outcome.get("ok", outcome.get("status"))
    reason = outcome.reason if hasattr(outcome, "reason") else outcome.get("reason", "")
    print(f"  [{'OK ' if ok is True else 'NO ' if ok is False else ok}] {title}: {reason}", flush=True)
    pause()


def records_for(env: Environment, input_id: str):
    ops = env.principal("SYN-DELIVERY-OPS-001")
    return [r for r in env.evidence_store.list_records(ops) if r.source_ref.input_id == input_id]


def run(env: Environment) -> None:
    ops = env.principal("SYN-DELIVERY-OPS-001")
    pmo1, pmo2 = env.principal("SYN-REVIEWER-PMO-001"), env.principal("SYN-REVIEWER-PMO-002")
    sol1, sol2 = env.principal("SYN-SOLUTIONS-USER-001"), env.principal("SYN-SOLUTIONS-USER-002")
    print(f"Versions: {env.versions()}\nClock: {env.clock.now_str()}\n")

    print("1. Load synthetic MSR inputs into Delivery")
    for case in env.msr_cases():
        out = env.delivery.load_input(ops, case["input"], correlation_id=f"SYN-LOAD-{case['case_id']}")
        show(f"{case['case_id']}", out)

    print("\n2. Employee review (authenticated fictional reviewers)")
    for case in env.msr_cases():
        rv = case["scripted_review"]
        if not rv:
            continue
        env.clock.set(rv["at"])
        out = env.delivery.employee_review(env.principal(rv["reviewer_id"]), case["input"]["input_id"], 1,
                                           rv["decision"], correlation_id=f"SYN-WF-A-{case['case_id']}")
        show(f"{case['input']['input_id']} -> {rv['decision']}", out)

    print("\n3. Role Execution Agent prepares alignment candidates (Delivery only)")
    env.clock.set("2026-10-16T16:00:00Z")
    for iid in ["SYN-MSR-001", "SYN-MSR-002", "SYN-MSR-003", "SYN-MSR-006", "SYN-MSR-007", "SYN-MSR-008",
                "SYN-MSR-011", "SYN-MSR-009", "SYN-MSR-010"]:
        out = env.delivery.prepare_candidates(ops, iid, correlation_id=f"SYN-WF-A-{iid}")
        extra = ""
        if out.ok:
            extra = f" created={[(c['record_id'], c['req_task_id'], c['blocking_exceptions']) for c in out.data['created']]}"
            extra += f" gaps={len(out.data['gaps'])}"
        show(iid, out)
        if extra:
            print(f"        {extra}")

    print("\n4. Human PMO review and separate publication")
    env.clock.set("2026-10-16T16:15:00Z")
    events = {}
    for iid, reviewer in [("SYN-MSR-001", pmo1), ("SYN-MSR-002", pmo1), ("SYN-MSR-006", pmo1),
                          ("SYN-MSR-007", pmo1), ("SYN-MSR-008", pmo1), ("SYN-MSR-011", pmo1),
                          ("SYN-MSR-011", pmo2)]:
        for rec in records_for(env, iid):
            out = env.delivery.pmo_decide(reviewer, rec.record_id, rec.record_version, "approved",
                                          "Fictional reviewer approves the minimized synthetic summary.")
            show(f"{reviewer.user_id} approves {rec.record_id} ({iid})", out)
            if out.ok:
                events[rec.record_id] = out.data["event_id"]
    env.clock.set("2026-10-16T16:20:00Z")
    for rid, eid in events.items():
        show(f"publish {rid}", env.delivery.publish(pmo1 if rid != "SYN-EVID-007" else pmo2, rid, 1, eid))

    print("\n5. Workflow B: permitted requirement-focused query")
    env.clock.set("2026-10-16T17:00:00Z")
    q = {"query_id": "SYN-QUERY-001", "requester_id": sol1.user_id, "audience_id": "SYN-SOLUTIONS-DEMO",
         "purpose": "requirement-evidence-explanation",
         "req_task_ids": ["SYN-REQ-API-001", "SYN-REQ-API-002", "SYN-REQ-DATA-001", "SYN-REQ-DATA-002"],
         "question": "What approved evidence covers these API and data requirements?"}
    resp = env.orchestrator.handle(sol1, q)
    print(f"  status={resp['status']}  ({resp['label']})")
    for c in resp["coverage"]:
        print(f"    {c['req_task_id']}: {c['status']} cites {c['cited_record_ids']}")
    print("  escalations:", resp["escalations"], flush=True)
    pause()

    print("\n6. Deny paths")
    for title, who, extra in [
        ("raw MSR request", sol1, {"include_raw_sources": True}),
        ("other audience's requester", sol2, {"requester_id": sol2.user_id}),
        ("prohibited purpose (personnel-ranking)", sol1, {"purpose": "personnel-ranking"}),
    ]:
        r = env.orchestrator.handle(sol1 if who is sol1 else sol2, q | {"query_id": None} | extra)
        print(f"  [{r['status']}] {title}: {r.get('reason', '')}", flush=True)
        pause()
    try:
        env.delivery_store.list_inputs(sol1)
    except Exception as e:
        print(f"  [denied] Solutions user reads Delivery store directly: {e}", flush=True)
        pause()

    print("\n7. Revocation: PMO revokes the contradicting record; cached evidence is invalidated")
    contra = records_for(env, "SYN-MSR-006")[0]
    show(f"revoke {contra.record_id}", env.delivery.revoke(pmo1, contra.record_id, 1, "Fictional correction."))
    r = env.orchestrator.handle(sol1, q | {"query_id": "SYN-QUERY-002", "req_task_ids": ["SYN-REQ-API-001"]})
    print(f"  re-query SYN-REQ-API-001: {[(c['status'], c['cited_record_ids']) for c in r['coverage']]}", flush=True)
    pause()
    show("acknowledge SYN-QUERY-002", env.orchestrator.acknowledge(sol1, "SYN-QUERY-002"))

    print("\n8. Staleness: advance the clock past expiration")
    env.clock.set("2026-11-17T00:00:00Z")
    r = env.orchestrator.handle(sol1, q | {"query_id": "SYN-QUERY-003", "req_task_ids": ["SYN-REQ-API-001"]})
    print(f"  after expiry: {[(c['status'], c['cited_record_ids']) for c in r['coverage']]}; "
          f"withheld={r.get('records_withheld')}")
    print(f"\nAudit entries recorded: {len(env.audit.entries(limit=10000))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--var-dir", default=None)
    ap.add_argument("--pace", type=float, default=0.0, help="seconds to pause after each step")
    ap.add_argument("--dataset", choices=["roster", "guide"], default=None,
                    help="roster (default from config) = notional roster workbook; guide = Build Guide fixtures")
    ap.add_argument("--no-dialogue", action="store_true",
                    help="roster dataset: skip the new MSR submissions through the employee feedback dialogue")
    args = ap.parse_args()
    PACE = args.pace
    env = Environment(var_dir=Path(args.var_dir) if args.var_dir else None, reset=True, dataset=args.dataset)
    try:
        if env.dataset == "roster":
            from stratacaster.demo import run_roster_demo
            run_roster_demo(env, pace=PACE, say=lambda s: print(s, flush=True), dialogue=not args.no_dialogue)
        else:
            run(env)
    finally:
        env.close()
