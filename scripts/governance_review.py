"""Model-assisted AI-governance review of the corpus, prototype documents, and prototype governance artifacts (FC-03).

Each document (split into parts if long) is reviewed by Claude against the governance criteria below; a final call
synthesizes a control matrix, cross-document conflicts, and top risks. Findings are advisory: every one is written
with disposition "pending" for the volume owner to accept, reject, or defer. Quotes are checked deterministically
against the source text. Tracked changes are read as if accepted.

Default is a free plan (document list, token and cost estimate, no API calls). Model calls need --run and a key set
in your own shell (never commit it):
    .\\.venv\\Scripts\\python.exe scripts\\governance_review.py                 # plan only
    $env:ANTHROPIC_API_KEY = "<key>"
    .\\.venv\\Scripts\\python.exe scripts\\governance_review.py --run [--max-usd 15] [--only V01,P03]
Writes evidence/governance_review/<stamp>/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stratacaster.llm import LLMError, make_llm  # noqa: E402

DOC_ROOT = ROOT.parent / "Documentation"
PROMPTS = ROOT / "stratacaster" / "prompts"
CANONICAL_CLAUSE = (
    "StrataCaster agents shall provide evidence-based decision support only. Authorized human officials retain review, "
    "validation, approval, accountability, and final authority for consequential decisions. Agents shall not "
    "independently make, approve, or execute consequential employment, staffing, compensation, disciplinary, "
    "contracting, pricing, bid, customer-contact, disclosure, access, authorization, release, security, compliance, or "
    "similarly material decisions.")
CRITERIA = {
    "G-01": "Human oversight: agents provide decision support only; the canonical clause is reproduced verbatim and "
            "cited where required; no agent makes, approves, or executes a consequential decision.",
    "G-02": "Approval authority and separation of duties: named human approvers (TTO lead review, PMO approval of "
            "evidence and REQ changes), endorser cannot approve, publication separate from approval.",
    "G-03": "Data minimization and containment: raw MSRs never leave Delivery; permission domains; only approved "
            "task alignments are shared; disclosure controls.",
    "G-04": "Agent authorization and scope: each agent's purpose, inputs, outputs, version, and authorization status "
            "are defined; unauthorized agents are not operated.",
    "G-05": "Prohibited purposes and fairness: no ranking, staffing, compensation, or disciplinary use of agent "
            "outputs; people-related outputs need human review and a fairness basis.",
    "G-06": "Evaluation and evidence integrity: results are tied to a recorded run, test, or record; synthetic data is "
            "labeled; limitations are stated.",
    "G-07": "Failure handling: refusals, malformed output, model errors, and prompt injection fail closed and are "
            "logged; humans are notified.",
    "G-08": "Traceability and change control: append-only records, versioned prompts and REQs, change requests, and "
            "baselines that are never rewritten.",
    "G-09": "Model and vendor posture: model-agnostic interface, what data reaches the model provider, cost and usage "
            "controls, exit options.",
}
PART_TOKENS = 60_000         # split documents larger than this (estimated tokens)
OUT_TOKENS_PER_REVIEW = 4_000  # conservative estimate, including thinking, for cost planning
OUT_TOKENS_SYNTHESIS = 8_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ===================================================================== structured outputs

class Finding(BaseModel):
    criterion_id: str
    severity: Literal["high", "medium", "low"]
    quote: str = Field(description="Shortest exact passage from the text that locates the finding")
    issue: str
    recommendation: str


class Strength(BaseModel):
    criterion_id: str
    quote: str
    note: str


class DocReview(BaseModel):
    findings: list[Finding]
    strengths: list[Strength]


class ControlRow(BaseModel):
    criterion_id: str
    defined_in: str
    implemented_in: str
    evidence: str
    gap: str


class Conflict(BaseModel):
    description: str
    document_ids: list[str]


class Risk(BaseModel):
    rank: int
    risk: str
    finding_ids: list[str]


class Synthesis(BaseModel):
    executive_summary: str
    control_matrix: list[ControlRow]
    cross_document_conflicts: list[Conflict]
    top_risks: list[Risk]


# ===================================================================== documents

def docx_text(path: Path) -> str:
    """Paragraph text as if tracked changes were accepted: inserted text kept, deleted text (w:delText) dropped."""
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(W + "p"):
        parts = [(n.text or "") if n.tag == W + "t" else "\t" for n in p.iter() if n.tag in (W + "t", W + "tab")]
        line = "".join(parts).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def collect_documents(doc_root: Path = DOC_ROOT, include_baseline: bool = False) -> list[dict]:
    docs = []
    for f in sorted((doc_root / "1 Corpus").glob("[0-9][0-9]_*.docx")):
        docs.append({"id": "V" + f.name[:2], "set": "corpus", "title": f.stem, "text": docx_text(f)})
    # 2 Prototype holds the build plan and one folder per increment (P01-P07 and P1.08 in Prototype 1, P<n>.00
    # charters in later ones); the document ID is the name before " — ", e.g. P03, P1.08, P2.00.
    for f in sorted((doc_root / "2 Prototype").rglob("*.docx")):
        if f.name.startswith("~$"):
            continue
        did = f.stem.split(" — ", 1)[0].strip()
        if did == "P00" and not include_baseline:
            continue  # frozen design baseline, superseded by the corpus
        docs.append({"id": "BUILD-PLAN" if did == "00" else did, "set": "prototype_docs", "title": f.stem,
                     "text": docx_text(f)})
    artifacts = [("A-REG", "Agent registry", ROOT / "config" / "agent_registry.json"),
                 ("A-TRC", "Traceability", ROOT / "docs" / "traceability.md"),
                 ("A-RDM", "Roadmap", ROOT / "docs" / "roadmap.md"),
                 ("A-RME", "README", ROOT / "README.md")]
    for did, title, f in artifacts:
        docs.append({"id": did, "set": "prototype_artifacts", "title": title, "text": f.read_text(encoding="utf-8")})
    prompts = sorted(p for p in PROMPTS.glob("*.md") if not p.name.startswith("governance_"))
    docs.append({"id": "A-PRM", "set": "prototype_artifacts", "title": "Agent prompts",
                 "text": "\n\n".join(f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}" for p in prompts)})
    return docs


def est_tokens(s: str) -> int:
    return len(s) // 4 + 1


def split_parts(docs: list[dict], part_tokens: int = PART_TOKENS) -> list[dict]:
    """One review unit per document, or several split on paragraph boundaries."""
    units = []
    for d in docs:
        chunks, cur = [], []
        for para in d["text"].split("\n"):
            if cur and est_tokens("\n".join(cur + [para])) > part_tokens:
                chunks.append("\n".join(cur))
                cur = []
            cur.append(para)
        chunks.append("\n".join(cur))
        for i, c in enumerate(chunks, 1):
            units.append({"doc": d, "part": i, "parts": len(chunks), "text": c})
    return units


def payload(unit: dict) -> dict:
    d = unit["doc"]
    return {"document": {"id": d["id"], "title": d["title"], "set": d["set"]}, "part": unit["part"],
            "parts": unit["parts"], "canonical_clause": CANONICAL_CLAUSE, "criteria": CRITERIA, "text": unit["text"]}


def plan(units: list[dict], price: dict) -> dict:
    system = (PROMPTS / "governance_review_v1.md").read_text(encoding="utf-8")
    tin = sum(est_tokens(system) + est_tokens(json.dumps(payload(u), indent=2)) for u in units)
    tout = OUT_TOKENS_PER_REVIEW * len(units)
    syn_in = 2_000 + 1_500 * len(units)  # findings carried into the synthesis call
    cost = ((tin + syn_in) * price["input"] + (tout + OUT_TOKENS_SYNTHESIS) * price["output"]) / 1_000_000
    return {"documents": len({u["doc"]["id"] for u in units}), "calls": len(units) + 1,
            "est_input_tokens": tin + syn_in, "est_output_tokens": tout + OUT_TOKENS_SYNTHESIS,
            "est_cost_usd": round(cost, 2)}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip("\"“”").lower()


# ===================================================================== review

def review(llm, units: list[dict], max_usd: float, price: dict) -> dict:
    """Per-unit reviews, then the synthesis. Stops before a call that could pass the spending cap."""
    review_sys = (PROMPTS / "governance_review_v1.md").read_text(encoding="utf-8")
    calls, findings, strengths = [], [], []

    def spent() -> float:
        return sum(c.get("cost_usd", 0.0) for c in calls)

    def guarded(task, system, data, model, out_tokens):
        est = (est_tokens(system) + est_tokens(json.dumps(data, indent=2))) * price["input"] / 1e6 \
            + out_tokens * price["output"] / 1e6
        if spent() + est > max_usd:
            raise LLMError(f"spending cap ${max_usd} would be passed (spent ${spent():.2f}, next ~${est:.2f})")
        try:
            r = llm.run(task, system, data, model)
        except LLMError as e:
            calls.append({"task": task, "ok": False, "error": str(e)})
            raise
        calls.append({"task": task, "ok": True, **r.meta()})
        return r.parsed

    stopped = None
    counters: dict[str, int] = {}
    for u in units:
        did = u["doc"]["id"]
        try:
            out = guarded(f"governance_review:{did}:{u['part']}", review_sys, payload(u), DocReview,
                          OUT_TOKENS_PER_REVIEW)
        except LLMError as e:
            if "spending cap" in str(e):
                stopped = str(e)
                break
            continue  # recorded as a failed call; the document is listed as not reviewed
        text = norm(u["text"])
        for f in out.findings:
            counters[did] = counters.get(did, 0) + 1
            findings.append({"finding_id": f"F-{did}-{counters[did]:02d}", "document_id": did, "part": u["part"],
                             **f.model_dump(), "quote_verified": norm(f.quote) in text, "disposition": "pending"})
        strengths += [{"document_id": did, **s.model_dump()} for s in out.strengths]

    synthesis = None
    if findings or strengths:
        syn_sys = (PROMPTS / "governance_synthesis_v1.md").read_text(encoding="utf-8")
        data = {"criteria": CRITERIA, "findings": findings, "strengths": strengths}
        try:
            synthesis = guarded("governance_synthesis", syn_sys, data, Synthesis, OUT_TOKENS_SYNTHESIS).model_dump()
        except LLMError as e:
            stopped = stopped or str(e)
    reviewed = {c["task"].split(":")[1] for c in calls if c["ok"] and c["task"].startswith("governance_review:")}
    return {"calls": calls, "findings": findings, "strengths": strengths, "synthesis": synthesis,
            "not_reviewed": sorted({u["doc"]["id"] for u in units} - reviewed), "stopped": stopped,
            "cost_usd": round(spent(), 4)}


def write_outputs(out_dir: Path, record: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "review_record.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    rows = ["| ID | Doc | Criterion | Severity | Issue | Recommendation | Quote verified | Disposition |",
            "|---|---|---|---|---|---|---|---|"]
    order = {"high": 0, "medium": 1, "low": 2}
    for f in sorted(record["findings"], key=lambda f: (order[f["severity"]], f["finding_id"])):
        cells = [f["finding_id"], f["document_id"], f["criterion_id"], f["severity"], f["issue"], f["recommendation"],
                 "yes" if f["quote_verified"] else "NO - check", f["disposition"]]
        rows.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |")
    (out_dir / "findings.md").write_text(
        f"# Governance review findings {record['review_id']}\n\nModel-assisted and advisory. Each finding needs a "
        "disposition (accept / reject / defer) from the volume owner.\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    s = record["synthesis"] or {}
    lines = [f"# Governance assurance summary {record['review_id']}", "",
             f"- Model {record['model']}; {len(record['calls'])} calls; cost ${record['cost_usd']}",
             f"- Findings: {len(record['findings'])} "
             f"({sum(f['severity'] == 'high' for f in record['findings'])} high); quotes not verified: "
             f"{sum(not f['quote_verified'] for f in record['findings'])}",
             f"- Not reviewed: {', '.join(record['not_reviewed']) or 'none'}"
             + (f"; stopped: {record['stopped']}" if record["stopped"] else ""), "",
             "## Executive summary", "", s.get("executive_summary", "(no synthesis)"), "",
             "## Control matrix", "", "| Criterion | Defined in | Implemented in | Evidence | Gap |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(str(r[k]).replace("|", "/") for k in
                                 ("criterion_id", "defined_in", "implemented_in", "evidence", "gap")) + " |"
              for r in s.get("control_matrix", [])]
    lines += ["", "## Top risks", ""] + [f"{r['rank']}. {r['risk']} ({', '.join(r['finding_ids'])})"
                                          for r in s.get("top_risks", [])]
    lines += ["", "## Cross-document conflicts", ""] + [f"- {c['description']} ({', '.join(c['document_ids'])})"
                                                         for c in s.get("cross_document_conflicts", [])]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="make model calls (default: plan and estimate only)")
    ap.add_argument("--max-usd", type=float, default=15.0, help="stop before a call that could pass this spend")
    ap.add_argument("--only", help="comma-separated document ids, e.g. V01,V04,P03,P2.00,BUILD-PLAN,A-REG")
    ap.add_argument("--include-baseline", action="store_true", help="also review the frozen P00 build guide")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config" / "prototype_config.json").read_text(encoding="utf-8"))
    price = cfg["llm"]["price_per_mtok"]
    docs = collect_documents(include_baseline=args.include_baseline)
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        docs = [d for d in docs if d["id"] in keep]
    units = split_parts(docs)
    p = plan(units, price)
    print(f"Documents: {', '.join(d['id'] for d in docs)}")
    print(f"Plan: {p['calls']} calls, ~{p['est_input_tokens']:,} input / ~{p['est_output_tokens']:,} output tokens, "
          f"about ${p['est_cost_usd']} at {cfg['llm']['model']} prices (cap ${args.max_usd})")
    if not args.run:
        print("Plan only; nothing was called. Add --run to make model calls.")
        return
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("ANTHROPIC_API_KEY is not set in this shell; nothing was called.")
    os.environ["STRATACASTER_LLM_MODE"] = "claude"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = review(make_llm(cfg["llm"]), units, args.max_usd, price)
    record = {"review_id": f"GOV-{stamp}", "model": cfg["llm"]["model"], "effort": cfg["llm"]["effort"],
              "prompts": ["governance_review_v1", "governance_synthesis_v1"], "criteria": CRITERIA,
              "documents": [{"id": d["id"], "title": d["title"], "set": d["set"]} for d in docs], "plan": p, **result,
              "note": "Model-assisted, advisory review of synthetic-data project documents. Dispositions are made by "
                      "the volume owner; no finding changes a controlled document by itself."}
    out_dir = ROOT / "evidence" / "governance_review" / stamp
    write_outputs(out_dir, record)
    print((out_dir / "summary.md").read_text(encoding="utf-8"))
    print(f"\nEvidence written to {out_dir}")


if __name__ == "__main__":
    main()
