"""Live Claude smoke test: a handful of real model calls before any full Claude-mode evaluation.

One roster MSR (SIM-MSR-202610-005) goes through REQ assessment (2 calls), the deterministic TTO, PMO and publication
steps, one Solutions coverage query (1 call), and REQ-change planning for the employee's REQ (up to 3 calls). The
same steps then run on the mock so the outputs can be compared. Writes evidence/smoke/<stamp>/.

Usage (key set in your own shell; never commit it):
    $env:ANTHROPIC_API_KEY = "<key>"
    .\\.venv\\Scripts\\python.exe scripts\\smoke_claude.py [--max-usd 2.0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stratacaster.environment import Environment  # noqa: E402
from stratacaster.llm import LLMError, MockLLM, make_llm  # noqa: E402

MSR, LEAD, PMO, SOLUTIONS = "SIM-MSR-202610-005", "SIM-EMP-002", "SIM-EMP-001", "SIM-EMP-210"
REQ, TTO = "SIM-REQ-1-01-WR-001", "SIM-TTO-1-01"
FULL_RUN_CALLS = 150  # approximate model calls in one Claude-mode run_evaluation.py


class Recorder:
    """Wraps the model service, records every call, and stops before the spending cap is passed."""

    def __init__(self, inner, max_usd: float):
        self.inner, self.max_usd = inner, max_usd
        self.mode, self.model = inner.mode, inner.model
        self.calls: list[dict] = []

    def spent(self) -> float:
        return sum(c.get("cost_usd", 0.0) for c in self.calls)

    def run(self, task, system, payload, output_model):
        if self.spent() >= self.max_usd:
            raise LLMError(f"smoke-test spending cap ${self.max_usd} reached")
        try:
            result = self.inner.run(task, system, payload, output_model)
        except LLMError as e:
            self.calls.append({"task": task, "ok": False, "error": str(e)})
            raise
        self.calls.append({"task": task, "ok": True, **result.meta()})
        return result


def pipeline(env: Environment) -> dict:
    """The smoke-test steps; returns what the agents produced (no raw MSR text)."""
    reg, t = env.registry, env.registry.demo["times"]
    env.load_all_cases()
    env.clock.set(t["prepare"])
    out = {}
    acc = env.delivery.employee_review(env.principal("SIM-EMP-005", "employee"), MSR, 1, "accepted")
    a = (acc.data.get("assessment") or {}).get("assessment") or {}  # outcome data -> the assessment itself
    out["assessment"] = {"ok": acc.ok, "input_tag": a.get("input_tag"), "aligned_req_task_ids": a.get(
        "aligned_req_task_ids"), "statements": a.get("statements"), "contract_wide": (a.get("contract_wide") or {}).get(
        "counts")}
    lead = env.principal(LEAD, "tto")
    out["tto_review"] = env.delivery.tto_review(lead, MSR, 1, "approved", "Smoke test.").reason
    env.clock.set(t["approve"])
    pmo = env.principal(PMO, "pmo")
    records = [r for r in env.evidence_store.list_records(pmo) if r.source_ref.input_id == MSR]
    events, out["records"] = {}, []
    for r in records:
        d = env.delivery.pmo_decide(pmo, r.record_id, r.record_version, "approved", "Smoke test approval.")
        out["records"].append({"record_id": r.record_id, "req_task_id": r.req_task_id, "scope": r.alignment_scope,
                               "summary": r.evidence_summary, "approved": d.ok, "reason": d.reason})
        if d.ok:
            events[r.record_id] = (r.record_version, d.data["event_id"])
    env.clock.set(t["publish"])
    out["published"] = sum(env.delivery.publish(pmo, rid, v, eid).ok for rid, (v, eid) in events.items())
    env.clock.set(t["query"])
    q = {"query_id": "SIM-SMOKE-001", "requester_id": SOLUTIONS, "audience_id": "SIM-AUD-SOLUTIONS",
         "purpose": "requirement-evidence-explanation",
         "req_task_ids": [k for k, v in reg.req_tasks.items() if v["tto_id"] == TTO],
         "question": "Which approved evidence covers the vulnerability-management deliverables?"}
    resp = env.orchestrator.handle(env.principal(SOLUTIONS), q)
    out["query"] = {"status": resp.get("status"), "coverage": [(c["req_task_id"], c["status"])
                                                               for c in resp.get("coverage", [])],
                    "explanation": resp.get("explanation"), "uncertainty": resp.get("uncertainty")}
    plan = env.req_planner.plan_req(lead, REQ, "2026-10", correlation_id="SIM-SMOKE-RQP")
    rec = env.req_planner.plans(lead, REQ)[-1] if plan.data.get("plan_id") else {}
    out["req_planning"] = {"status": plan.reason, "clusters": len(rec.get("clusters", [])),
                           "options": rec.get("options"), "agent_errors": rec.get("agent_errors"),
                           "plans": [{"rank": p["rank"], "score": p["plan_score"], "key": p["key"],
                                      "decisions": [(d["operator"], d["wording"]) for d in p["decisions"]]}
                                     for p in rec.get("plans", [])],
                           "pruned": [r["reason"] for r in rec.get("trace", []) if r["status"] == "pruned"]}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-usd", type=float, default=2.0, help="stop making model calls past this spend")
    args = ap.parse_args()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("ANTHROPIC_API_KEY is not set in this shell; nothing was called.")
    os.environ["STRATACASTER_LLM_MODE"] = "claude"
    cfg = json.loads((ROOT / "config" / "prototype_config.json").read_text(encoding="utf-8"))
    rec = Recorder(make_llm(cfg["llm"]), args.max_usd)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "evidence" / "smoke" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env = Environment(var_dir=Path(tmp) / "claude", llm=rec, dataset="roster", reset=True)
        try:
            live = pipeline(env)
        finally:
            env.close()
        env = Environment(var_dir=Path(tmp) / "mock", llm=MockLLM(), dataset="roster", reset=True)
        try:
            mock = pipeline(env)
        finally:
            env.close()
    ok = [c for c in rec.calls if c["ok"]]
    per_call = rec.spent() / len(ok) if ok else 0.0
    record = {
        "smoke_id": f"SMOKE-{stamp}", "model": cfg["llm"]["model"], "effort": cfg["llm"]["effort"],
        "price_per_mtok": cfg["llm"]["price_per_mtok"], "calls": rec.calls,
        "totals": {"calls": len(rec.calls), "failed": len(rec.calls) - len(ok), "cost_usd": round(rec.spent(), 4),
                   "input_tokens": sum(c.get("input_tokens", 0) for c in ok),
                   "output_tokens": sum(c.get("output_tokens", 0) for c in ok),
                   "latency_ms": sum(c.get("latency_ms", 0) for c in ok)},
        "projection": {"full_run_calls": FULL_RUN_CALLS, "cost_usd": round(per_call * FULL_RUN_CALLS, 2),
                       "minutes": round(sum(c.get("latency_ms", 0) for c in ok) / max(len(ok), 1)
                                        * FULL_RUN_CALLS / 60000, 1)},
        "claude": live, "mock": mock,
        "note": "Synthetic data only. Cost is computed from the configured prices and reported token usage.",
    }
    (out_dir / "smoke_record.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    t = record["totals"]
    lines = [f"# Claude smoke test {record['smoke_id']}", "",
             f"- Model {record['model']} (effort {record['effort']}); {t['calls']} calls, {t['failed']} failed",
             f"- Tokens in/out {t['input_tokens']}/{t['output_tokens']}; cost ${t['cost_usd']}; "
             f"model time {t['latency_ms'] / 1000:.1f} s",
             f"- Projected full Claude-mode evaluation (~{FULL_RUN_CALLS} calls): about "
             f"${record['projection']['cost_usd']} and {record['projection']['minutes']} min", "",
             "| Step | Claude | Mock |", "|---|---|---|",
             f"| REQ assessment tag | {live['assessment']['input_tag']} | {mock['assessment']['input_tag']} |",
             f"| Aligned REQ tasks | {live['assessment']['aligned_req_task_ids']} | "
             f"{mock['assessment']['aligned_req_task_ids']} |",
             f"| Contract-wide signals | {live['assessment']['contract_wide']} | {mock['assessment']['contract_wide']} |",
             f"| Records approved / published | {sum(r['approved'] for r in live['records'])} / {live['published']} | "
             f"{sum(r['approved'] for r in mock['records'])} / {mock['published']} |",
             f"| Query status | {live['query']['status']} | {mock['query']['status']} |",
             f"| REQ planning | {live['req_planning']['status']} | {mock['req_planning']['status']} |", "",
             "## Claude REQ-change options", ""]
    for cid, opts in (live["req_planning"].get("options") or {}).items():
        lines += [f"- {cid}: " + "; ".join(f"{o['operator']}: {o['wording']}" for o in opts)]
    lines += ["", "## Claude coverage explanation", "", str(live["query"]["explanation"])]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nEvidence written to {out_dir}")


if __name__ == "__main__":
    main()
