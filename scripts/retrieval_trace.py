"""Capture a real retrieval trace (capstone checkpoint 3.1 feedback) and a prompt-only comparison.

Runs the roster demo, sets the clock to the demo's query time, runs one governed semantic expertise search, and writes
evidence/traces/<timestamp>/ with trace.json and summary.md.

The prompt-only comparison measures what each approach puts in front of the model for the same question, not model
quality: a prompt-only approach has no retrieval or gates, so its context is the month's raw MSRs as pasted.

Usage:  python scripts/retrieval_trace.py ["question"]
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stratacaster.demo import run_roster_demo  # noqa: E402
from stratacaster.environment import Environment  # noqa: E402
from stratacaster.llm import MockLLM  # noqa: E402
from stratacaster.semantic import prompt_only_context  # noqa: E402

DEFAULT_QUESTION = "Who has confirmed that reported fixes were actually closed?"


def main(question: str) -> Path:
    env = Environment(var_dir=Path(tempfile.mkdtemp()), llm=MockLLM(), dataset="roster")
    try:
        run_roster_demo(env, say=lambda *_: None)
        env.clock.set(env.registry.demo["times"]["query"])  # the demo ends past expiry; query at its query time
        body = env.orchestrator.expertise_search(env.principal("SIM-EMP-210"), question, "SIM-AUD-SOLUTIONS",
                                                 query_id="SIM-SEARCH-TRACE-001")
        trace = body.get("trace") or body
        baseline = prompt_only_context(env, "SIM-FC-001", "2026-10")
        governed = {"raw_narratives_exposed": 0,
                    "ineligible_records_ranked": 0,
                    "ineligible_records_excluded": len(trace.get("excluded_from_ranking", [])),
                    "citable_approved_records": sum(r["kind"] == "evidence" for r in trace.get("results", []))}
        trace["comparison"] = {"prompt_only_context": baseline, "governed_retrieval": governed,
                               "note": "Context comparison only; model quality needs a live-model run."}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = ROOT / "evidence" / "traces" / stamp
        out.mkdir(parents=True, exist_ok=True)
        (out / "trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        lines = [f"# Retrieval trace {stamp}", "", f"- Question: {question}", f"- Status: {trace.get('status')}",
                 f"- Vector store: {trace.get('vector_store')}", f"- Embedding model: {trace.get('embedding_model')}",
                 f"- Corpus: {trace.get('corpus')}", f"- Retry: {trace.get('retry')}", "", "## Candidates (8)", ""]
        lines += [f"- {c['id']} ({c['kind']}) similarity {c['similarity']}" for c in trace.get("candidates", [])]
        lines += ["", "## Excluded before ranking", ""]
        lines += [f"- {b['id']} similarity {b['similarity']}: {', '.join(b['reasons'])}"
                  for b in trace.get("excluded_from_ranking", [])] or ["- none"]
        lines += ["", "## Selected (4)", ""]
        lines += [f"- {r['kind']} {r['id']} score {r['score']}: {r.get('title') or r.get('summary')}"
                  for r in trace.get("results", [])]
        lines += ["", "## Prompt-only comparison (context, not model quality)", "",
                  f"- Prompt-only: {baseline}", f"- Governed: {governed}"]
        (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return out
    finally:
        env.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION)
