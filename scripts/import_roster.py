"""Import the notional organizational roster workbook (v3) as the prototype's demonstration dataset.

Reads the workbook's Excel tables (StrataRoster, StrataTaskOrders, StrataMSRs, StrataReqTasks, StrataPMOs,
StrataAudiences, StrataTestCases) and writes:
  data/fixtures/roster/reference_registry.json   people, PMOs, contracts, task orders, roles, REQ tasks, users
  data/fixtures/roster/msr_cases.json            400 Delivery MSRs + the workbook's prototype test cases
  data/fixtures/roster/import_manifest.json      source path, SHA-256, counts, and import rules

Usage:  python scripts/import_roster.py [path\\to\\StrataCaster_..._v3.xlsx]
"""
from __future__ import annotations

import hashlib
import json
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
_WB = "StrataCaster_Notional_Organizational_Roster_270_Employees_v3.xlsx"
# The synthetic workbook ships in the repo (data/source/); the sibling Documentation copy is the fallback.
DEFAULT_SRC = next((p for p in (ROOT / "data" / "source" / _WB, ROOT.parent / "Documentation" / "3 Data" / _WB) if p.exists()),
                   ROOT / "data" / "source" / _WB)
OUT = ROOT / "data" / "fixtures" / "roster"
IMPORT_VERSION = "roster-import/1.1"
AGENTS = [("AGT-ROLE-EXECUTION", "delivery", "Role Execution Agent"),
          ("AGT-SOLUTIONS-ORCHESTRATOR", "solutions", "Solutions Orchestrator"),
          ("AGT-SOLUTIONS-EXPERTISE", "solutions", "Solutions Expertise Agent")]
DEMO_SCOPE = {"contracts": ["SIM-FC-001"], "months": ["2026-10"]}


def table(wb, name: str) -> list[dict]:
    for ws in wb.worksheets:
        if name in ws.tables:
            rows = list(ws[ws.tables[name].ref])
            hdr = [c.value for c in rows[0]]
            return [dict(zip(hdr, (c.value for c in r))) for r in rows[1:] if r[0].value is not None]
    raise KeyError(f"table {name} not found")


def iso(d) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def stamp(d: date, hh: int, mm: int = 0) -> str:
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}:00Z"


def pmo_tto(contract_id: str) -> str:
    return f"SIM-TTO-{contract_id.removeprefix('SIM-FC-').lstrip('0')}-PMO"


def main(src: Path, out: Path = OUT) -> None:
    warnings.filterwarnings("ignore")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    wb = openpyxl.load_workbook(src, data_only=True)
    roster = table(wb, "StrataRoster")
    ttos = table(wb, "StrataTaskOrders")
    msrs = table(wb, "StrataMSRs")
    reqs = table(wb, "StrataReqTasks")
    pmos = table(wb, "StrataPMOs")
    auds = table(wb, "StrataAudiences")
    tests = table(wb, "StrataTestCases")
    emp = {r["Employee ID"]: r for r in roster}
    pmo_by_contract = {p["Contract ID"]: p for p in pmos}

    # ------------------------------------------------------------------ reference registry
    delivery = [r for r in roster if r["Prototype Domain"] == "Delivery"]
    roles = {}
    for r in delivery:
        roles.setdefault(r["Prototype Role ID"], r["Job Title"].split(",")[0] if r["Prototype Role ID"].startswith("LR-")
                         else r["Job Title"])
    tto_list = [{"tto_id": t["Task Order ID"], "contract_id": t["Contract ID"], "title": t["Task-Order Scope"],
                 "technical_director_id": t["Technical Director ID"]} for t in ttos]
    for cid in pmo_by_contract:
        tto_list.append({"tto_id": pmo_tto(cid), "contract_id": cid,
                         "title": "Contract PMO (direct contract assignment; PMO REQ tasks; reviewed by a PMO "
                                  "teammate in place of a TTO lead)",
                         "technical_director_id": None, "pmo_review": True})
    req_tasks = []
    for q in reqs:
        eligible = [x.strip() for x in (q["Eligible Prototype Role IDs"] or "").split(";") if x.strip()]
        req_tasks.append({"req_task_id": q["REQ Task ID"], "tto_id": q["Task Order ID"], "title": q["Requirement Task (Deliverable)"],
                          "role_task_ids": [f"RT-{wr}" for wr in eligible],
                          "keywords": [k.strip().lower() for k in q["Mock Alignment Keywords"].split(";") if k.strip()],
                          "source": q["Source"]})
    role_tasks = [{"role_task_id": f"RT-{rid}", "role_id": rid,
                   "title": f"{title}: representative work tasks (Vol06)"}
                  for rid, title in sorted(roles.items()) if rid.startswith("WR-")]

    pm_ids = {p["Human PMO Reviewer ID"] for p in pmos}  # each contract's Program Manager (primary reviewer)
    tto_leads: dict[str, list[str]] = {}  # Contract Task Orders: each TTO's Technical Director leads that TTO
    for t in ttos:
        tto_leads.setdefault(t["Technical Director ID"], []).append(t["Task Order ID"])
    # Each work role within a TTO has an individual REQ: the Requirement Tasks that name the role as eligible.
    role_reqs: dict[tuple, dict] = {}
    for q in req_tasks:
        for rt in q["role_task_ids"]:
            wr = rt.removeprefix("RT-")
            key = (q["tto_id"], wr)
            role_reqs.setdefault(key, {
                "role_req_id": f"SIM-REQ-{q['tto_id'].removeprefix('SIM-TTO-')}-{wr}", "role_id": wr,
                "tto_id": q["tto_id"], "title": f"{roles.get(wr, wr)} REQ for {q['tto_id']}",
                "req_task_ids": [], "source": "Requirement Tasks tab"})["req_task_ids"].append(q["req_task_id"])
    users = []
    featured = set()
    for r in delivery:
        eid, acc = r["Employee ID"], r["Prototype Access Role(s)"]
        u = {"user_id": eid, "kind": "human", "domain": "delivery", "roles": ["employee_reviewer"],
             "display": f"{r['Fictional Name']} — {r['Job Title']} ({r['Contract ID']})"}
        if "Human PMO reviewer" in acc:
            pmo = acc.split("Human PMO reviewer (")[1].split(")")[0]
            u["roles"].append("human_pmo_reviewer")
            u["pmo_authority"] = [pmo]  # every member of the contract's PMO team holds PMO review authority
            if eid in pm_ids or r["Contract ID"] in DEMO_SCOPE["contracts"]:
                featured.add(eid)
        if "Delivery operator" in acc:
            u["roles"].append("delivery_operator")
            u["delivery_scopes"] = [pmo_by_contract[r["Contract ID"]]["Delivery Scope ID"]]
            if r["Contract ID"] in ("SIM-FC-001", "SIM-FC-002"):
                featured.add(eid)
        if eid in tto_leads:  # TTO lead: review authority only over the TTO(s) this person leads
            u["roles"].append("tto_lead")
            u["tto_authority"] = sorted(tto_leads[eid])
        users.append(u)
    for r in roster:
        if r["Prototype Domain"] == "Solutions":
            acc = r["Prototype Access Role(s)"]
            rl = ["solutions_requester"] + (["solutions_reviewer"] if "Solutions reviewer" in acc else [])
            users.append({"user_id": r["Employee ID"], "kind": "human", "domain": "solutions", "roles": rl,
                          "display": f"{r['Fictional Name']} — {r['Job Title']} ({acc.split('(')[-1].rstrip(')')})"})
            featured.add(r["Employee ID"])
    for aid, dom, disp in AGENTS:
        users.append({"user_id": aid, "kind": "agent", "domain": dom, "roles": ["agent"], "display": disp})
    featured.update({"SIM-EMP-005", "SIM-EMP-006", "SIM-EMP-007", "SIM-EMP-008", "SIM-EMP-009", "SIM-EMP-016",
                     "SIM-EMP-055"})
    for u in users:
        if u["user_id"] in featured:
            u["featured"] = True

    registry = {
        "fixture_version": f"roster-reference/3.0+{digest[:12]}",
        "data_mode": "synthetic",
        "note": "Generated by scripts/import_roster.py from the notional roster workbook v3. All people, contracts, "
                "task orders and reports are fictional. Prototype roles are demonstration roles only.",
        "id_prefix": "SIM",
        "pmos": [{"pmo_id": p["PMO ID"], "name": f"{p['Delivery Mission']} PMO", "reviewer_id": p["Human PMO Reviewer ID"]}
                 for p in pmos],
        "contracts": [{"contract_id": p["Contract ID"], "pmo_id": p["PMO ID"], "delivery_scope_id": p["Delivery Scope ID"]}
                      for p in pmos],
        "ttos": tto_list,
        "roles": [{"role_id": rid, "title": title} for rid, title in sorted(roles.items())],
        "role_tasks": role_tasks,
        "req_tasks": req_tasks,
        "role_reqs": sorted(role_reqs.values(), key=lambda x: x["role_req_id"]),
        "subjects": [{"subject_id": r["Employee ID"], "employee_reviewer_id": r["Employee ID"],
                      "name": r["Fictional Name"], "role_id": r["Prototype Role ID"], "contract_id": r["Contract ID"],
                      "tto_id": pmo_tto(r["Contract ID"]) if r["Task Order ID"] == "Contract PMO" else r["Task Order ID"],
                      "delivery_scope_id": pmo_by_contract[r["Contract ID"]]["Delivery Scope ID"]} for r in delivery],
        "workflow": {"tto_lead_review": True, "assess_on_submission": True, "publish_task_alignment_only": True,
                     "contract_wide_analytic": True,
                     "note": "Employee submits (or accepts) the MSR; the Role Execution Agent assesses it against the "
                             "employee's work-role REQ and tags aligned tasks or a potential new task requirement, and "
                             "separately against every other REQ on the contract (similar work, collaboration "
                             "opportunities); the "
                             "lead of the employee's TTO reviews (including the lead's own MSR); the PMO approves and "
                             "publishes task alignments with employee data (name, ID, role, TTO), never the MSR input. Contract PMO "
                             "staff have no TTO; a PMO teammate other than the author reviews their MSRs instead."},
        "audiences": [{"audience_id": a["Audience ID"],
                       "members": [m.strip() for m in a["Member Employee IDs"].split(";")]} for a in auds],
        "purposes": [
            {"purpose_id": "requirement-evidence-explanation", "allowed": True},
            {"purpose_id": "personnel-ranking", "allowed": False, "reason": "Prohibited: the prototype must not rank personnel."},
            {"purpose_id": "staffing-recommendation", "allowed": False,
             "reason": "Prohibited: no staffing or employment outcomes."}],
        "users": users,
        "routing": {"solutions_reviewer": "SIM-EMP-210", "delivery_review_owner": "SIM-EMP-001"},
        "evidence_defaults": {"default_audience_ids": [pmos[0]["Default Evidence Audience"]]},
        "demo": {"clock_start": "2026-10-23T13:55:00Z", "scope": DEMO_SCOPE,
                 "times": {"prepare": "2026-10-23T16:00:00Z", "approve": "2026-10-23T16:15:00Z",
                           "publish": "2026-10-23T16:20:00Z", "query": "2026-10-23T17:00:00Z"}},
    }

    # ------------------------------------------------------------------ MSR cases
    def build_input(msr_id, e, month_start: date, through: date, narrative, tto=None):
        cid = e["Contract ID"]
        tto_id = tto or (pmo_tto(cid) if e["Task Order ID"] == "Contract PMO" else e["Task Order ID"])
        return {
            "schema_version": "prototype-msr-input/1.0", "input_id": msr_id, "input_version": 1,
            "data_mode": "synthetic", "delivery_scope_id": pmo_by_contract[cid]["Delivery Scope ID"],
            "subject_id": e["Employee ID"], "period_start": month_start.isoformat(), "period_end": through.isoformat(),
            "role_id": e["Prototype Role ID"], "tto_id": tto_id, "contract_id": cid, "source_status": "active",
            "source_created_at": stamp(through + timedelta(days=1), 14), "raw_narrative": narrative,
            "source_locator": f"delivery-local:{msr_id}:v1",
            "review": {"status": "pending", "reviewer_id": None, "reviewed_input_version": None, "reviewed_at": None},
            "classification": "synthetic-demo", "restrictions": ["delivery-only", "no-external-use"]}

    cases = []
    for m in sorted(msrs, key=lambda x: x["MSR ID"]):
        if not str(m["Prototype Scope"]).startswith("Delivery MSR"):
            continue
        e = emp[m["Employee ID"]]
        start, through = m["Reporting Month"].date(), m["Reporting Through"].date()
        narrative = " ".join(str(m[f"Employee Narrative — Bullet {i}"]).lstrip("• ").strip() for i in (1, 2, 3))
        cases.append({
            "case_id": m["MSR ID"], "category": "roster-msr", "contract_id": e["Contract ID"],
            "month": start.strftime("%Y-%m"),
            "expected_outcome": "Unlabeled roster MSR: alignment has not been adjudicated.",
            "scripted_review": {"decision": "accepted", "reviewer_id": e["Employee ID"],
                                "at": stamp(through + timedelta(days=1), 15)},
            "input": build_input(m["MSR ID"], e, start, through, narrative)})
    oct_start, oct_through = date(2026, 10, 1), date(2026, 10, 22)
    for t in tests:
        e = emp[t["Employee ID"]]
        inp = build_input(t["MSR ID"], e, oct_start, oct_through, t["Narrative"], tto=t["Task Order Override"] or None)
        if (t["Field Override"] or "") == "omit subject_id":
            del inp["subject_id"]
        review = None
        if t["Scripted Review"] in ("accepted", "rejected"):
            review = {"decision": t["Scripted Review"], "reviewer_id": e["Employee ID"],
                      "at": stamp(oct_through + timedelta(days=1), 15, 5)}
        cases.append({"case_id": t["Case ID"], "category": t["Category"], "contract_id": e["Contract ID"],
                      "month": "2026-10", "expected_outcome": t["Expected Prototype Outcome"],
                      "scripted_review": review, "input": inp})

    out.mkdir(parents=True, exist_ok=True)
    (out / "reference_registry.json").write_text(json.dumps(registry, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "msr_cases.json").write_text(json.dumps({
        "dataset_version": f"roster-msr-cases/3.0+{digest[:12]}", "data_mode": "synthetic",
        "note": "Generated from the roster workbook. Inputs arrive with review pending; scripted reviews are the "
                "fictional employee's own review applied by the demo. Expected outcomes for test cases are predeclared "
                "in the workbook and are not results.",
        "cases": cases}, indent=1, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "import_version": IMPORT_VERSION, "source_file": src.name, "source_sha256": digest,
        "counts": {"employees": len(roster), "delivery_employees": len(delivery), "task_orders": len(ttos),
                   "req_tasks": len(req_tasks), "role_reqs": len(role_reqs), "users": len(users), "delivery_msrs": sum(c["category"] == "roster-msr" for c in cases),
                   "test_cases": len(tests)},
        "rules": ["Company-staff MSRs (Prototype Scope = Outside prototype) are not imported.",
                  "Bullets are joined into raw_narrative, which stays in the Delivery store.",
                  "Contract PMO staff map to SIM-TTO-<n>-PMO (direct contract assignment). Its REQ tasks "
                  "(SIM-REQ-<n>-PMO-01..07) come from Volume 02 §3 program-management responsibilities; a PMO "
                  "teammate other than the author reviews their MSRs in place of a TTO lead.",
                  "role_task_id is RT-<WR>; LR- leadership roles have no REQ eligibility.",
                  "Each employee is the reviewer of their own MSR; review is applied in the prototype, never at load.",
                  "TTO leads are the Technical Directors in Contract Task Orders; each may review only MSRs from "
                  "employees in the TTO(s) they lead, including their own MSR.",
                  "Each contract's PMO team (Program Manager, Deputy Program Manager, Operations Manager, three "
                  "Portfolio Leads) holds PMO review authority for that contract's PMO; the Program Manager is the "
                  "primary reviewer. Like HR and other function staff, PMO staff are not aligned to a task order; their "
                  "MSRs (v3) describe PMO work across the contract and map to SIM-TTO-<n>-PMO.",
                  "Each work role in a TTO has an individual REQ (SIM-REQ-<c>-<tt>-<WR>) generated from the "
                  "Requirement Tasks tab."],
        "default_demo_scope": DEMO_SCOPE,
    }
    (out / "import_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps(manifest["counts"]), "sha256", digest[:16])


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC)

