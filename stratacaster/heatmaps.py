"""Heatmap data for PMO-published task alignments (roster workflow).

Computed inside Delivery for the acting principal, from records that principal may see. Each function returns
tidy rows (one per cell) with counts only: no MSR text, no statement text.

1. potential_new_tasks  - potential new tasks for an employee's REQ, and where they align elsewhere on the contract
2. cross_collaboration  - REQ tasks performed by people in more than one TTO on the contract
3. req_task_coverage    - every REQ task on the contract against every work-role REQ, for a period
"""
from __future__ import annotations

from collections import Counter

NOT_ELSEWHERE = "Not aligned elsewhere"


def _published(env, p, contract_id: str, period: str | None) -> list[tuple]:
    """(record, source MSR) for published records on the contract, optionally in one reporting month."""
    out = []
    for r in env.evidence_store.list_records(p):
        if r.disposition != "published" or r.contract_id != contract_id:
            continue
        msr = env.delivery_store.get_input(p, r.source_ref.input_id, r.source_ref.input_version)
        if msr and (not period or msr.period_start[:7] == period):
            out.append((r, msr))
    return out


def contracts(env, p) -> list[str]:
    return sorted({r.contract_id for r in env.evidence_store.list_records(p) if r.disposition == "published"})


def periods(env, p, contract_id: str) -> list[str]:
    return sorted({m.period_start[:7] for _, m in _published(env, p, contract_id, None)})


def _req_label(reg, role_id: str, tto_id: str) -> str:
    req = reg.role_req_for(role_id, tto_id)
    if req:
        return req["role_req_id"]
    title = reg.roles.get(role_id, {}).get("title", role_id).split(",")[0]
    return f"{tto_id} · {title} (no REQ)"


def potential_new_tasks(env, p, contract_id: str, period: str | None = None) -> list[dict]:
    """Rows: the REQ the potential new task came from. Columns: REQ tasks elsewhere on the contract, plus
    'Not aligned elsewhere'. Value: statements. Counts only TTO-approved inputs; an elsewhere-alignment counts
    once its collaboration-opportunity record is published."""
    reg = env.registry
    published = {(r.source_ref.input_id, r.source_ref.input_version, r.req_task_id)
                 for r, _ in _published(env, p, contract_id, period) if r.alignment_scope == "collaboration-opportunity"}
    cells: Counter = Counter()
    for msr in env.delivery_store.list_inputs(p):
        if msr.contract_id != contract_id or (period and msr.period_start[:7] != period) \
                or env.delivery.tto_status(msr.input_id, msr.input_version) != "approved":
            continue
        a = env.delivery.assessment(p, msr.input_id, msr.input_version)
        if not a:
            continue
        origin = _req_label(reg, msr.role_id, msr.tto_id)
        elsewhere = {row["statement_id"]: row for row in (a.get("contract_wide") or {}).get("statements", [])
                     if row["signal"] == "collaboration-opportunity"}
        for s in a["statements"]:
            if s["tag"] != "potential-new-task-requirement":
                continue
            row = elsewhere.get(s["statement_id"])
            hits = [x for x in (row or {}).get("elsewhere", [])
                    if (msr.input_id, msr.input_version, x["req_task_id"]) in published]
            if not hits:
                cells[(origin, NOT_ELSEWHERE)] += 1
            for x in hits:
                cells[(origin, x["req_task_id"])] += 1
    return [{"origin_req": o, "target": t,
             "target_tto": reg.req_tasks[t]["tto_id"] if t in reg.req_tasks else "",
             "target_title": reg.req_tasks[t]["title"] if t in reg.req_tasks else "No REQ task on the contract matches",
             "statements": n} for (o, t), n in sorted(cells.items())]


def cross_collaboration(env, p, contract_id: str, period: str | None = None, cross_only: bool = True) -> list[dict]:
    """Rows: REQ tasks (owning TTO). Columns: the TTO of the people who performed them. Value: published
    alignments of any scope. cross_only keeps tasks performed by people in more than one TTO, or by anyone
    outside the owning TTO."""
    reg = env.registry
    cells: Counter = Counter()
    people: dict = {}
    for r, msr in _published(env, p, contract_id, period):
        cells[(r.req_task_id, msr.tto_id)] += 1
        people.setdefault((r.req_task_id, msr.tto_id), set()).add(msr.subject_id)
    by_task: dict = {}
    for (task, tto), n in cells.items():
        by_task.setdefault(task, {})[tto] = n
    rows = []
    for task, perf in sorted(by_task.items()):
        owner = reg.req_tasks[task]["tto_id"]
        if cross_only and len(perf) < 2 and owner in perf:
            continue
        for tto, n in sorted(perf.items()):
            rows.append({"req_task": task, "title": reg.req_tasks[task]["title"], "owning_tto": owner,
                         "performing_tto": tto, "alignments": n, "people": len(people[(task, tto)]),
                         "relation": "own TTO" if tto == owner else "cross-TTO"})
    return rows


def req_task_coverage(env, p, contract_id: str, period: str | None = None) -> list[dict]:
    """Every REQ task on the contract x every work-role REQ on the contract. Value: published alignments from
    employees in that REQ (own-REQ and contract-wide). in_req marks the task as part of that REQ, so an
    in-REQ zero (no work reported) differs from 'not part of this REQ'."""
    reg = env.registry
    reqs = sorted((r for r in reg.role_reqs.values()
                   if reg.ttos.get(r["tto_id"], {}).get("contract_id") == contract_id),
                  key=lambda r: r["role_req_id"])
    tasks = sorted(t["req_task_id"] for t in reg.contract_req_tasks(contract_id))
    work: Counter = Counter()
    for r, msr in _published(env, p, contract_id, period):
        work[(r.req_task_id, _req_label(reg, msr.role_id, msr.tto_id))] += 1
    rows = []
    for t in tasks:
        for q in reqs:
            in_req = t in q["req_task_ids"]
            n = work.get((t, q["role_req_id"]), 0)
            if in_req or n:
                rows.append({"req_task": t, "title": reg.req_tasks[t]["title"], "task_tto": reg.req_tasks[t]["tto_id"],
                             "req": q["role_req_id"], "role": reg.roles.get(q["role_id"], {}).get("title", q["role_id"]),
                             "in_req": in_req, "alignments": n})
    return rows
