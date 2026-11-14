"""Execute the evaluation and record ACTUAL results (Build Guide Section 5).

1. Runs the pytest suite (scenario tests, including P-VERSION-001) and saves the JUnit report.
2. Runs a metric pass on the synthetic dataset with the configured model service (mock or Claude):
   alignment precision/recall, coverage-status accuracy, citation correctness, disclosure checks,
   latency, and cost.
3. Compares measured values with the predeclared thresholds in data/eval/expected_outcomes.json.

Output: evidence/runs/<UTC timestamp>/ with run_record.json, junit.xml, summary.md, and alignment_export.json (the
statement-level results for the human sample review; see scripts/build_sample_review.py).
Usage:  python scripts/run_evaluation.py --executor "Your Name" [--reviewer "Reviewer"] [--skip-tests]
The reviewer verdict stays "pending review" until a human reviewer records it.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stratacaster.environment import Environment  # noqa: E402
from stratacaster.llm import LLMError, make_llm  # noqa: E402


class Meter:
    """Counts every model call, its latency, tokens and cost (DEF-008: the roster section was not metered)."""

    def __init__(self):
        cfg = json.loads((ROOT / "config" / "prototype_config.json").read_text(encoding="utf-8"))
        self.inner = make_llm(cfg["llm"])
        self.mode, self.model = self.inner.mode, self.inner.model
        self.calls, self.failed, self.latency_ms, self.tokens_in, self.tokens_out, self.cost = 0, 0, 0, 0, 0, 0.0

    def run(self, task, system, payload, output_model):
        self.calls += 1
        try:
            r = self.inner.run(task, system, payload, output_model)
        except LLMError:
            self.failed += 1
            raise
        self.latency_ms += r.latency_ms
        self.tokens_in += r.input_tokens
        self.tokens_out += r.output_tokens
        self.cost += r.cost_usd
        return r

    def totals(self) -> dict:
        return {"model_calls": self.calls, "failed_calls": self.failed, "total_model_latency_ms": self.latency_ms,
                "input_tokens": self.tokens_in, "output_tokens": self.tokens_out,
                "total_cost_usd": round(self.cost, 6)}


def run_tests(out_dir: Path) -> dict:
    junit = out_dir / "junit.xml"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
                          cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "STRATACASTER_LLM_MODE": "mock"})
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    failures = [f"{c.get('classname')}::{c.get('name')}" for c in suite.iter("testcase")
                if c.find("failure") is not None or c.find("error") is not None]
    return {"tests": int(suite.get("tests")), "failures": int(suite.get("failures")),
            "errors": int(suite.get("errors")), "skipped": int(suite.get("skipped")),
            "failed_cases": failures, "exit_code": proc.returncode, "junit": junit.name}


def run_metrics(var_dir: Path) -> dict:
    labels = json.loads((ROOT / "data" / "eval" / "expected_outcomes.json").read_text(encoding="utf-8"))
    meter = Meter()
    env = Environment(var_dir=var_dir, reset=True, dataset="guide", llm=meter)  # the labeled dataset
    try:
        ops = env.principal("SYN-DELIVERY-OPS-001")
        env.load_all_cases()
        for case in env.msr_cases():
            rv = case["scripted_review"]
            if rv:
                env.clock.set(rv["at"])
                env.delivery.employee_review(env.principal(rv["reviewer_id"]), case["input"]["input_id"], 1,
                                             rv["decision"])
        env.clock.set("2026-10-16T16:00:00Z")
        tp = fp = fn = 0
        per_input = {}
        for iid, expected in labels["alignment_labels"].items():
            out = env.delivery.prepare_candidates(ops, iid)
            got = sorted({c["req_task_id"] for c in out.data.get("created", [])})
            tp += len(set(got) & set(expected))
            fp += len(set(got) - set(expected))
            fn += len(set(expected) - set(got))
            per_input[iid] = {"expected": expected, "got": got, "ok": out.ok}

        env.clock.set("2026-10-16T16:15:00Z")
        events, blocked_published = {}, 0
        for rec in env.evidence_store.list_records(ops):
            iid = rec.source_ref.input_id
            reviewer = "SYN-REVIEWER-PMO-002" if rec.owning_pmo_id == "SYN-PMO-002" else "SYN-REVIEWER-PMO-001"
            a = env.delivery.pmo_decide(env.principal(reviewer), rec.record_id, rec.record_version, "approved",
                                        "Evaluation run: fictional approval attempt.")
            if a.ok:
                events[rec.record_id] = (reviewer, a.data["event_id"], iid)
        env.clock.set("2026-10-16T16:20:00Z")
        published_inputs = set()
        for rid, (reviewer, eid, iid) in events.items():
            if env.delivery.publish(env.principal(reviewer), rid, 1, eid).ok:
                published_inputs.add(iid)
                blocked_published += iid in labels["blocked_inputs"]

        env.clock.set("2026-10-16T17:00:00Z")
        expected_status = labels["workflow_b_expected_status"]
        resp = env.orchestrator.handle(env.principal("SYN-SOLUTIONS-USER-001"), {
            "query_id": "EVAL-QUERY-001", "requester_id": "SYN-SOLUTIONS-USER-001",
            "audience_id": "SYN-SOLUTIONS-DEMO", "purpose": "requirement-evidence-explanation",
            "req_task_ids": list(expected_status), "question": "Evaluation: coverage for all bounded REQ tasks."})
        got_status = {c["req_task_id"]: c["status"] for c in resp.get("coverage", [])}
        status_acc = sum(got_status.get(t) == s for t, s in expected_status.items()) / len(expected_status)
        published = {r.record_id: r for r in env.evidence_store.list_records(ops) if r.disposition == "published"}
        cites = [(c["req_task_id"], rid) for c in resp.get("coverage", []) for rid in c["cited_record_ids"]]
        correct = sum(rid in published and published[rid].req_task_id == t for t, rid in cites)
        blob = json.dumps(resp)
        disclosures = sum(c["input"]["raw_narrative"] in blob or c["input"]["source_locator"] in blob
                          for c in env.msr_cases())
        return {
            "versions": env.versions(), "dataset": labels["dataset_version"],
            "alignment": {"tp": tp, "fp": fp, "fn": fn,
                          "precision": tp / (tp + fp) if tp + fp else 1.0,
                          "recall": tp / (tp + fn) if tp + fn else 1.0, "per_input": per_input},
            "publication": {"published_inputs": sorted(published_inputs),
                            "expected_publishable": labels["publishable_inputs"],
                            "blocked_inputs_published": blocked_published},
            "workflow_b": {"status": resp["status"], "expected": expected_status, "got": got_status,
                           "coverage_status_accuracy": status_acc,
                           "citations": len(cites),
                           "citation_correctness": correct / len(cites) if cites else 1.0,
                           "raw_content_disclosures": disclosures, "escalations": len(resp.get("escalations", []))},
            "cost_latency": meter.totals(),
            "thresholds": labels["thresholds"],
        }
    finally:
        env.close()


def run_roster(var_dir: Path) -> dict:
    """Roster dataset (unlabeled MSRs): test-case outcomes against the workbook's predeclared expectations,
    alignment coverage, publication safety, and disclosure checks. No precision/recall: there is no answer key."""
    from stratacaster.demo import run_roster_demo

    meter = Meter()
    env = Environment(var_dir=var_dir, reset=True, dataset="roster", llm=meter)
    try:
        s = run_roster_demo(env, say=lambda *_: None)
        reg = env.registry
        # Read as each contract's PMO reviewer / operator: no single Delivery user may see every contract.
        recs = list({(r.record_id, r.record_version): r for p in reg.pmos.values()
                     for r in env.evidence_store.list_records(env.principal(p["reviewer_id"]))}.values())
        by_input = {}
        for r in recs:
            by_input.setdefault(r.source_ref.input_id, []).append(r)
        blocked_cases = {cid for cid, _ in s["blocked"].values()}
        tc10 = by_input.get("SIM-MSR-TEST-10", [])
        checks = {
            "TC-01 injection blocked": "TC-01" in blocked_cases,
            "TC-02 restricted content blocked": "TC-02" in blocked_cases,
            "TC-03 contradiction escalates as conflict": s["coverage"].get("SIM-REQ-1-01-07") == "conflict",
            "TC-04 unreviewed not prepared": "TC-04" in s["denied_prep"],
            "TC-05 rejected not prepared": "TC-05" in s["denied_prep"],
            "TC-06 missing field rejected": "schema-invalid" in s["rejected"].get("TC-06", ""),
            "TC-07 duplicate rejected": "duplicate-conflict" in s["rejected"].get("TC-07", ""),
            "TC-08 bad reference rejected": "reference-invalid" in s["rejected"].get("TC-08", ""),
            "TC-09 ambiguous yields no record": not by_input.get("SIM-MSR-TEST-09"),
            "TC-10 cross-PMO denied, owning PMO publishes": "no-pmo-authority" in s.get("cross_pmo_denial", "")
                                                            and any(r.disposition == "published" for r in tc10),
        }
        # DEF-006: the denominator covers every assessed input, including MSRs submitted during the run, so it
        # matches the numerator (s["gaps"] counts unaligned statements from all of them).
        assessed = {}
        for scope in {c["delivery_scope_id"] for c in reg.contracts.values()}:
            op = env.principal(reg.operator_for(scope))
            for m in env.delivery_store.list_inputs(op):
                assessed.setdefault((m.input_id, m.input_version), op)
        total_statements = sum(len(env.delivery_store.get_statements(op, iid, v)) for (iid, v), op in assessed.items())
        blob = json.dumps(env.ops.responses())
        disclosures = sum(c["input"]["raw_narrative"] in blob for c in env.msr_cases())
        published_blocked = sum(1 for rid in s["blocked"] if any(
            r.record_id == rid and r.disposition == "published" for r in recs))
        from stratacaster.delivery import split_statements
        pub_blob = json.dumps([r.model_dump() for r in recs if r.disposition == "published"])
        multi = [iid for iid, rs in by_input.items()
                 if len({r.req_task_id for r in rs}) > 1 and all(r.disposition == "published" for r in rs)]
        workflow_checks = {
            "Each submitted MSR is assessed and tagged against the employee's REQ": s["prepared"] == sum(
                s["tags"].values()) and s["prepared"] > 0,
            "Only the TTO's own lead may approve (another TTO's lead denied)":
                "own TTO" in s.get("tto", {}).get("other_tto_denial", ""),
            "Published records carry no MSR statement text": not any(
                st in pub_blob for c in env.msr_cases() for st in split_statements(c["input"]["raw_narrative"])),
            "Every alignment of a multi-task MSR is published": bool(multi),
            # Counted across every assessed MSR, not one scripted submission whose wording suited the mock (DEF-008).
            "Contract-wide analytic flags similar work and collaboration opportunities, delivered as related work":
                all(s.get("contract_wide_all", {}).get(k) for k in ("similar-work", "collaboration-opportunity"))
                and bool(s.get("related_work")),
        }
        semantic = run_semantic(env)  # report only: no threshold depends on it
        req_planning = run_req_planning(env)  # report only (checkpoint 4.1)
        alignment_export = export_alignments(env, assessed)  # before the feedback section adds its own submissions
        feedback_check = run_feedback_check(env, meter)  # report only (PR-30)
        return {
            "semantic_retrieval": semantic, "req_planning": req_planning, "feedback_check": feedback_check,
            "alignment_export": alignment_export,
            "dataset": env.versions()["reference_fixture"], "scope": env.registry.demo["scope"],
            "inputs_loaded": len(s["loaded"]), "inputs_rejected": len(s["rejected"]),
            "statements": total_statements, "unaligned_statements": s["gaps"],
            "aligned_statement_share": round(1 - s["gaps"] / total_statements, 3) if total_statements else None,
            "draft_records": s["records"], "approved": s["approved"], "published": s["published"],
            "blocked_records_published": published_blocked, "query_coverage": s["coverage"],
            "escalations": s["escalations"], "raw_content_disclosures": disclosures,
            "test_case_checks": checks, "test_cases_met": sum(checks.values()),
            "assessment_tags": s["tags"], "tto_review": s.get("tto", {}),
            "contract_wide_signals": {"all_assessed": s.get("contract_wide_all", {}),
                                      "scripted_submission": s.get("contract_wide", {}),
                                      "related_work_records": len(s.get("related_work", []))},
            "workflow_checks": workflow_checks, "workflow_checks_met": sum(workflow_checks.values()),
            "cost_latency": meter.totals(),  # includes the semantic search and REQ-planning sections
        }
    finally:
        env.close()


SEMANTIC_QUESTION = "Who has confirmed that reported fixes were actually closed?"


def run_semantic(env) -> dict:
    """Checkpoint 3.1: one governed semantic expertise search with its full trace, plus the prompt-only context
    comparison. Optional: reported as unavailable if chromadb or the embedding model is missing."""
    try:
        from stratacaster.semantic import prompt_only_context
        env.clock.set(env.registry.demo["times"]["query"])  # the demo ends past expiry; query at its query time
        body = env.orchestrator.expertise_search(env.principal("SIM-EMP-210"), SEMANTIC_QUESTION,
                                                 "SIM-AUD-SOLUTIONS", query_id="SIM-SEARCH-EVAL-001")
        t = body.get("trace")
        if not t:
            return {"status": body["status"], "reason": body.get("reason")}
        return {"status": t["status"], "question": SEMANTIC_QUESTION, "vector_store": t["vector_store"],
                "embedding_model": t["embedding_model"], "candidates": len(t["candidates"]),
                "selected": [r["id"] for r in t["results"]],
                "excluded": [{"id": b["id"], "reasons": b["reasons"]} for b in t["excluded_from_ranking"]],
                "retry": t["retry"], "prompt_only_context": prompt_only_context(env, "SIM-FC-001", "2026-10"),
                "trace": t}
    except Exception as e:  # noqa: BLE001 - optional capability; never fails the run
        return {"status": "unavailable", "reason": str(e)}


REQ_PLAN_REQ, REQ_PLAN_PERIOD = "SIM-REQ-1-01-WR-001", "2026-10"


def run_req_planning(env) -> dict:
    """Checkpoint 4.1: one REQ-change planning run (Tree of Thought pilot) on the demo's TTO-approved MSRs, the TTO
    endorsement, and the PMO decision, with the gate refusals that must hold. Report only."""
    try:
        reg, rp = env.registry, env.req_planner
        req = rp.req(REQ_PLAN_REQ)
        lead = env.principal(reg.tto_lead_for(req["tto_id"]), "tto")
        pmo_id = reg.pmo_for_contract(reg.ttos[req["tto_id"]]["contract_id"])
        pmo = env.principal(reg.pmo_team(pmo_id)[0], "pmo")
        other_lead = next(env.principal(u, "tto") for t in reg.ttos if t != req["tto_id"]
                          and (u := reg.tto_lead_for(t)))
        out = rp.plan_req(lead, REQ_PLAN_REQ, REQ_PLAN_PERIOD, correlation_id="SIM-RQP-EVAL-001")
        if not out.data.get("plan_id"):
            return {"status": out.reason}
        pid = out.data["plan_id"]
        gates = {"Another TTO's lead may not run planning": not rp.plan_req(other_lead, REQ_PLAN_REQ,
                                                                           REQ_PLAN_PERIOD).ok,
                 "PMO may not approve before TTO endorsement": not rp.pmo_decide_req_plan(pmo, pid, "approved").ok}
        rp.decide_req_plan(lead, pid, "endorsed", 1, "Evaluation: endorse the top-ranked plan.")
        gates["A TTO lead may not approve REQ changes"] = not rp.pmo_decide_req_plan(lead, pid, "approved").ok
        decision = rp.pmo_decide_req_plan(pmo, pid, "approved", "Evaluation: PMO approval.")
        plan = rp.plans(lead, REQ_PLAN_REQ)[-1]
        gates["REQ baseline unchanged after PMO approval"] = req["req_task_ids"] == plan["baseline_req_task_ids"]
        pruned = {}
        for r in plan["trace"]:
            if r["status"] == "pruned":
                key = r["reason"].split(":")[0] if not r["reason"].startswith("score") else "score below threshold"
                pruned[key] = pruned.get(key, 0) + 1
        return {"status": plan["status"], "role_req_id": REQ_PLAN_REQ, "period": REQ_PLAN_PERIOD,
                "clusters": len(plan["clusters"]), "searched": plan["searched_clusters"],
                "excluded_statements": len(plan["excluded_statements"]), "nodes": plan["nodes_generated"],
                "budget_exhausted": plan["budget_exhausted"], "pruned": pruned,
                "plans": [{"rank": p["rank"], "score": p["plan_score"], "key": p["key"]} for p in plan["plans"]],
                "endorsed_by": plan["tto_decision"]["decided_by"], "approved_by": plan["pmo_decision"]["decided_by"],
                "change_id": decision.data.get("change_id"), "gate_checks": gates,
                "gate_checks_met": sum(gates.values()), "plan_record": plan}
    except Exception as e:  # noqa: BLE001 - report-only pilot; never fails the run
        return {"status": "error", "reason": str(e)}


FEEDBACK_TIME = "2026-10-23T16:00:00Z"


def run_feedback_check(env, meter) -> dict:
    """PR-30: the employee feedback check on the demo's dialogue drafts, submitted through the check with a response
    to every statement, with the refusals that must hold. Report only; no threshold depends on it."""
    try:
        from stratacaster.demo import ACCURATE_OR_NEW, DIALOGUES
        rowan_script, other_script = DIALOGUES[0], DIALOGUES[-1]
        d, month = env.delivery, rowan_script["month"]
        rowan, other = env.principal(rowan_script["who"], "employee"), env.principal(other_script["who"], "employee")
        ops = env.principal("SIM-EMP-002", "operator")
        lead = env.principal("SIM-EMP-002", "tto")
        env.clock.set(FEEDBACK_TIME)

        def footprint():
            return (len(env.delivery_store.list_inputs(ops)), len(env.evidence_store.list_records(ops)),
                    len(env.audit.entries()))

        def responses(script, rows):
            return {r["index"]: dict(script["responses"].get(r["index"], ACCURATE_OR_NEW)[r["tag"]]) for r in rows}

        before, calls = footprint(), meter.calls
        first = d.preview_msr(rowan, month, rowan_script["draft"])
        if not first.ok:
            return {"status": "unavailable", "reason": first.reason}
        writes_nothing, model_calls_in_check = footprint() == before, meter.calls - calls
        edited = list(rowan_script["draft"])
        for idx, text in rowan_script["edits"].items():
            edited[idx - 1] = text
        stale = d.submit_msr(rowan, month, edited, preview_id=first.data["preview_id"],
                             responses=responses(rowan_script, first.data["statements"]))
        check = d.preview_msr(rowan, month, edited)
        no_answers = d.submit_msr(rowan, month, edited, preview_id=check.data["preview_id"], responses={})
        not_yours = d.submit_msr(other, month, edited, preview_id=check.data["preview_id"],
                                 responses=responses(rowan_script, check.data["statements"]))
        submitted = [d.submit_msr(rowan, month, edited, preview_id=check.data["preview_id"],
                                  responses=responses(rowan_script, check.data["statements"]))]
        other_check = d.preview_msr(other, month, other_script["draft"])
        submitted.append(d.submit_msr(other, month, other_script["draft"], preview_id=other_check.data["preview_id"],
                                      responses=responses(other_script, other_check.data["statements"])))
        queue = {r["input_id"]: r for r in d.tto_review_queue(lead)}
        tags = [t for s in submitted if s.ok for t in s.data["assessment"]["assessment"]["statements"]]
        audit_blob = json.dumps(env.audit.entries(), default=str)
        drafted = rowan_script["draft"] + edited + other_script["draft"]
        gates = {"The check writes nothing (no input, record, or audit entry)": writes_nothing,
                 "An edited draft is refused until it is checked again": not stale.ok,
                 "A response is required for every statement": not no_answers.ok,
                 "One employee cannot submit with another's check": not not_yours.ok,
                 "The TTO lead sees the employee's responses": all(
                     s.ok and queue.get(s.data["input_id"], {}).get("employee_feedback") for s in submitted),
                 "No draft statement text reaches the audit trail": not any(t in audit_blob for t in drafted)}
        if env.feedback_mode == "local":
            gates["The free check makes no model call"] = model_calls_in_check == 0
        return {"status": "completed", "engine": env.feedback_mode, "drafts_submitted": sum(s.ok for s in submitted),
                "statements": len(tags), "check_agreed_with_assessment": sum(t["check_agreed"] for t in tags),
                "model_calls_in_check": model_calls_in_check, "gate_checks": gates,
                "gate_checks_met": sum(gates.values())}
    except Exception as e:  # noqa: BLE001 - report-only section; never fails the run
        return {"status": "error", "reason": str(e)}


def export_alignments(env, assessed: dict) -> list[dict]:
    """Every assessed statement with the agent's result, for the human sample review (scripts/build_sample_review.py).
    It holds raw statement text, so it is a Delivery-domain file written beside the run record, never inside it."""
    reg, rows = env.registry, []
    for (iid, version), op in sorted(assessed.items()):
        a, msr = env.delivery.assessment(op, iid, version), env.delivery_store.get_input(op, iid, version)
        if not a or msr is None:
            continue
        text = dict(env.delivery_store.get_statements(op, iid, version))
        req = reg.role_req_for(msr.role_id, msr.tto_id)
        catalog = [{"req_task_id": t, "title": reg.req_tasks[t]["title"]}
                   for t in (req["req_task_ids"] if req else []) if t in reg.req_tasks]
        for st in a["statements"]:
            rows.append({"statement_id": st["statement_id"], "input_id": iid, "text": text.get(st["statement_id"], ""),
                         "role_req_id": a.get("role_req_id"), "tto_id": msr.tto_id, "tag": st["tag"],
                         "aligned": [{"req_task_id": t, "title": reg.req_tasks[t]["title"]}
                                     for t in st["req_task_ids"] if t in reg.req_tasks],
                         "req_tasks": catalog, "test_case": "-TEST-" in iid})
    return rows


def verdicts(m: dict, tests: dict | None, roster: dict | None = None) -> dict:
    t = m["thresholds"]
    v = {
        "alignment_precision": m["alignment"]["precision"] >= t["alignment_precision_min"],
        "alignment_recall": m["alignment"]["recall"] >= t["alignment_recall_min"],
        "citation_correctness": m["workflow_b"]["citation_correctness"] >= t["citation_correctness_min"],
        "coverage_status_accuracy": m["workflow_b"]["coverage_status_accuracy"] >= t["coverage_status_accuracy_min"],
        "blocked_inputs_published": m["publication"]["blocked_inputs_published"] <= t["blocked_inputs_published_max"],
        "raw_content_disclosures": m["workflow_b"]["raw_content_disclosures"] <= t["raw_content_disclosures_max"],
    }
    if tests is not None:
        v["test_failures"] = tests["failures"] + tests["errors"] <= t["test_failures_max"]
    if roster is not None:
        v["roster_test_cases_met"] = roster["test_cases_met"] == len(roster["test_case_checks"])
        v["roster_blocked_records_published"] = roster["blocked_records_published"] == 0
        v["roster_raw_content_disclosures"] = roster["raw_content_disclosures"] == 0
        v["roster_workflow_checks_met"] = roster["workflow_checks_met"] == len(roster["workflow_checks"])
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--executor", default="UNASSIGNED")
    ap.add_argument("--reviewer", default=None,
                    help="who will review this run; the decision is always recorded as pending review")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "evidence" / "runs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    tests = None if args.skip_tests else run_tests(out_dir)
    with tempfile.TemporaryDirectory() as tmp:
        metrics = run_metrics(Path(tmp) / "var")
        roster = run_roster(Path(tmp) / "roster")
    alignment_export = roster.pop("alignment_export")
    v = verdicts(metrics, tests, roster)
    record = {
        "run_id": f"EVAL-{stamp}", "executed_at": stamp, "executor": args.executor,
        "reviewer": args.reviewer, "reviewer_decision": "pending review",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "tests": tests, "metrics": metrics, "roster": roster, "threshold_checks": v,
        "all_thresholds_met": all(v.values()),
        "limitations": [
            "Synthetic, small dataset; results do not generalize to real MSRs.",
            "Mock model results measure the pipeline, not language-model quality; rerun in Claude mode for that.",
            "Absence of observed failures is not proof of safety.",
        ],
    }
    (out_dir / "run_record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (out_dir / "alignment_export.json").write_text(json.dumps({
        "run_id": record["run_id"], "llm_mode": metrics["versions"]["llm_mode"], "model": metrics["versions"]["model"],
        "notice": "Synthetic. Delivery-domain file: contains raw statement text. For the human sample review only.",
        "statements": alignment_export}, indent=2), encoding="utf-8")
    fb = roster.get("feedback_check") or {}
    sem = roster.get("semantic_retrieval") or {}
    if sem.get("trace"):
        (out_dir / "retrieval_trace.json").write_text(json.dumps(sem["trace"], indent=2), encoding="utf-8")
    rqp = roster.get("req_planning") or {}
    if rqp.get("plan_record"):
        (out_dir / "req_plan_trace.json").write_text(json.dumps(rqp["plan_record"], indent=2), encoding="utf-8")
    lines = [f"# Evaluation run {record['run_id']}", "",
             f"- Executor: {args.executor} · Reviewer: {args.reviewer or 'not yet assigned'} · Decision: pending review",
             f"- Model service: {metrics['versions']['llm_mode']} ({metrics['versions']['model']})",
             f"- Dataset: {metrics['dataset']}", ""]
    if tests:
        lines.append(f"- Tests: {tests['tests']} run, {tests['failures']} failed, {tests['errors']} errors")
    lines += [f"- Alignment precision {metrics['alignment']['precision']:.2f}, recall {metrics['alignment']['recall']:.2f}",
              f"- Coverage-status accuracy {metrics['workflow_b']['coverage_status_accuracy']:.2f}; "
              f"citation correctness {metrics['workflow_b']['citation_correctness']:.2f}",
              f"- Blocked inputs published: {metrics['publication']['blocked_inputs_published']}; "
              f"raw-content disclosures: {metrics['workflow_b']['raw_content_disclosures']}",
              f"- Model latency {metrics['cost_latency']['total_model_latency_ms']} ms; "
              f"cost ${metrics['cost_latency']['total_cost_usd']}", "",
              f"## Roster dataset ({roster['dataset']})", "",
              f"- Inputs loaded {roster['inputs_loaded']}, rejected {roster['inputs_rejected']}; "
              f"statements aligned {roster['aligned_statement_share']:.0%} "
              f"({roster['statements'] - roster['unaligned_statements']} of {roster['statements']})",
              f"- Draft records {roster['draft_records']}, approved {roster['approved']}, published {roster['published']}; "
              f"blocked records published {roster['blocked_records_published']}",
              f"- Test cases meeting expected outcomes: {roster['test_cases_met']} of {len(roster['test_case_checks'])}",
              f"- REQ assessment tags: {roster['assessment_tags']}; TTO lead review: "
              f"{ {k: v for k, v in roster['tto_review'].items() if k != 'other_tto_denial'} }",
              f"- Workflow checks met: {roster['workflow_checks_met']} of {len(roster['workflow_checks'])}",
              f"- Query coverage: {roster['query_coverage']}",
              f"- Escalations {roster['escalations']}; raw-content disclosures {roster['raw_content_disclosures']}",
              f"- Contract-wide signals: {roster['contract_wide_signals']}",
              f"- Roster model calls {roster['cost_latency']['model_calls']} ({roster['cost_latency']['failed_calls']} "
              f"failed); latency {roster['cost_latency']['total_model_latency_ms']} ms; cost "
              f"${roster['cost_latency']['total_cost_usd']}",
              "", "## Semantic retrieval (report only; checkpoint 3.1)", "",
              f"- Status: {sem.get('status')}; {sem.get('vector_store', '')} · {sem.get('embedding_model', '')}",
              f"- Selected: {sem.get('selected')}; excluded before ranking: {sem.get('excluded')}; "
              f"retry: {sem.get('retry')}",
              f"- Prompt-only context for the same question: {sem.get('prompt_only_context')}",
              "", "## REQ-change planning, Tree of Thought pilot (report only; checkpoint 4.1)", "",
              f"- Status: {rqp.get('status')}; {rqp.get('role_req_id', '')} {rqp.get('period', '')}; clusters "
              f"{rqp.get('clusters')}, searched {rqp.get('searched')}, flagged statements excluded "
              f"{rqp.get('excluded_statements')}",
              f"- Nodes generated {rqp.get('nodes')} (budget exhausted: {rqp.get('budget_exhausted')}); pruned by "
              f"reason: {rqp.get('pruned')}",
              f"- Ranked plans: {rqp.get('plans')}",
              f"- Endorsed by {rqp.get('endorsed_by')} (TTO lead); approved by {rqp.get('approved_by')} (PMO); "
              f"change {rqp.get('change_id')}",
              f"- Gate checks met: {rqp.get('gate_checks_met')} of {len(rqp.get('gate_checks') or {})}",
              "", "## Employee feedback check (report only; PR-30)", "",
              f"- Status: {fb.get('status')}; engine {fb.get('engine')}; drafts submitted {fb.get('drafts_submitted')}; "
              f"model calls during the check {fb.get('model_calls_in_check')}",
              f"- Check agreed with the assessment on {fb.get('check_agreed_with_assessment')} of "
              f"{fb.get('statements')} statements",
              f"- Gate checks met: {fb.get('gate_checks_met')} of {len(fb.get('gate_checks') or {})}",
              *[f"  - {k}: {'met' if ok else 'NOT MET'}" for k, ok in (fb.get("gate_checks") or {}).items()],
              *([f"- Reason: {fb['reason']}"] if fb.get("reason") else []),
              "", "## Threshold checks", ""]
    lines += [f"- {k}: {'met' if ok else 'NOT MET'}" for k, ok in v.items()]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nEvidence written to {out_dir}")


if __name__ == "__main__":
    main()
