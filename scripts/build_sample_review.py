"""Build the human sample review workbook from one evaluation run's alignment_export.json.

The roster has no answer key, so a person scores a stratified sample of the agent's statement-level results. The sample
is drawn with a fixed seed, so it is repeatable. Test-case MSRs are excluded: they are adversarial inputs with their own
checks. The sheet shows no employee names, because it scores the agent, not the people.

Output: evidence/sample_review/<run stamp>/sample_review.xlsx
Usage:  python scripts/build_sample_review.py --run 20261105T120000Z [--per-group 30] [--seed 20261105]
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
ALIGNED, NEW = "Aligned", "Potential new task"
VERDICTS = ["Correct", "Partly correct", "Incorrect", "Can't tell"]
FONT = "Arial"
NAVY, TINT, INPUT, WARN = "12284A", "EEF3F8", "FFFF00", "FDF1EA"
THIN = Side(style="thin", color="D3DCE8")


def group_of(row: dict) -> str:
    return ALIGNED if row["tag"] == "aligned" else NEW


def eligible(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r["test_case"] and r["text"].strip()]


def select(rows: list[dict], per_group: int, seed: int) -> list[dict]:
    """Equal samples of each result, because they fail differently: an aligned result can claim too much, and a
    potential new task can miss a task the REQ does cover. Within a group, draw round-robin across REQs so one REQ
    cannot dominate. Order on the sheet is shuffled so the groups are not scored in blocks."""
    rng = random.Random(seed)
    chosen = []
    for group in (ALIGNED, NEW):
        by_req = defaultdict(list)
        for r in sorted((r for r in rows if group_of(r) == group), key=lambda x: x["statement_id"]):
            by_req[r["role_req_id"] or "no REQ"].append(r)
        for items in by_req.values():
            rng.shuffle(items)
        reqs = sorted(by_req)
        rng.shuffle(reqs)
        taken = 0
        while taken < per_group and any(by_req.values()):
            for req in reqs:
                if by_req[req] and taken < per_group:
                    chosen.append(by_req[req].pop())
                    taken += 1
    rng.shuffle(chosen)
    return chosen


def tasks_text(items: list[dict]) -> str:
    return "\n".join(f"{t['req_task_id']}: {t['title']}" for t in items)


def _font(**kw) -> Font:
    return Font(name=FONT, size=kw.pop("size", 10), **kw)


def _header(ws, row: int, values: list[str]) -> None:
    for c, v in enumerate(values, 1):
        cell = ws.cell(row, c, v)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(wrap_text=True, vertical="center")


def build(export: dict, sample: list[dict], population: dict[str, int], bar: float, seed: int, out: Path) -> None:
    wb = Workbook()
    is_mock = export["llm_mode"] == "mock"
    banner = (f"DRY RUN: stand-in model ({export['run_id']}). These results come from keyword matching, so scoring them "
              "says nothing about Claude. Use this sheet to try the form; score the sheet built from a live Claude run."
              if is_mock else f"Claude results from {export['run_id']} ({export['model']}).")

    # ------------------------------------------------------------------ Instructions
    ws = wb.active
    ws.title = "Instructions"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 96
    ws["A1"] = "StrataCaster human sample review"
    ws["A1"].font = _font(size=16, bold=True, color=NAVY)
    ws.merge_cells("A2:B2")
    ws["A2"] = banner
    ws["A2"].font = _font(bold=True, color="B3471B" if is_mock else NAVY)
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws["A2"].fill = PatternFill("solid", fgColor=WARN if is_mock else TINT)
    ws.row_dimensions[2].height = 44
    lines = [
        ("Why", "The roster has no answer key, so the evaluation cannot say whether the agent's roster results are right. "
                "A person scoring a sample is how that gets measured."),
        ("What you score", "The agent, never the employee. Judge only whether the agent's result fits the statement and "
                           "the REQ tasks shown. The sheet leaves out employee names for that reason."),
        ("How", "On the Review sheet, fill in the yellow cells only: Verdict, Should align to (if the agent was wrong), "
                "and Notes. The Summary sheet calculates itself. About a minute a statement."),
        ("Keep it independent", "Score alone, and before looking at the Summary sheet. Someone who did not build the "
                                "agent is the better scorer, if one is available."),
        ("Handling", "The sheet holds statement text from the synthetic MSRs. It is Delivery-domain material: keep it out "
                     "of anything Solutions-facing."),
        ("Sample", f"{len(sample)} statements, equal numbers from each result group, drawn round-robin across REQs (seed "
                   f"{seed}, so it is repeatable). Test-case MSRs are excluded."),
    ]
    row = 4
    for k, v in lines:
        ws.cell(row, 1, k).font = _font(bold=True, color=NAVY)
        ws.cell(row, 2, v).font = _font()
        ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row, 1).alignment = Alignment(vertical="top")
        ws.row_dimensions[row].height = 30
        row += 1
    row += 1
    _header(ws, row, ["Verdict", "What it means"])
    meanings = [
        ("Correct", "Aligned: every task listed fits the statement, and none is missing. Potential new task: no task in "
                    "this REQ covers the statement."),
        ("Partly correct", "Aligned: at least one listed task fits, but one is wrong or missing. Potential new task: a "
                           "REQ task covers part of it."),
        ("Incorrect", "Aligned: no listed task fits. Potential new task: a task in this REQ does cover it (name the task)."),
        ("Can't tell", "The statement is too vague to judge either way. It is left out of the accuracy figures."),
    ]
    for k, v in meanings:
        row += 1
        ws.cell(row, 1, k).font = _font(bold=True)
        ws.cell(row, 2, v).font = _font()
        ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row, 1).alignment = Alignment(vertical="top")
        ws.row_dimensions[row].height = 30
    row += 2
    ws.cell(row, 1, "Example (not counted)").font = _font(bold=True, color=NAVY)
    ws.cell(row, 2, 'Statement: "I rebuilt the weekly scan schedule." Agent result: Aligned to "Asset and scan plans." '
                    'Verdict: Correct. Should align to: (blank). Notes: (blank).').font = _font(italic=True)
    ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = 30

    # ------------------------------------------------------------------ Review
    rv = wb.create_sheet("Review")
    headers = ["#", "Statement ID", "Agent's result", "REQ", "Statement", "Tasks the agent aligned it to",
               "All tasks in this REQ", "Verdict", "Should align to", "Notes"]
    _header(rv, 1, headers)
    rv.row_dimensions[1].height = 30
    for c, w in zip("ABCDEFGHIJ", (5, 25, 14, 22, 52, 36, 44, 14, 24, 30)):
        rv.column_dimensions[c].width = w
    first = 2
    for i, r in enumerate(sample):
        at = first + i
        vals = [i + 1, r["statement_id"], group_of(r), r["role_req_id"] or "no REQ", r["text"],
                tasks_text(r["aligned"]) or "(none)", tasks_text(r["req_tasks"]) or "(no REQ for this role)"]
        for c, v in enumerate(vals, 1):
            cell = rv.cell(at, c, v)
            cell.font = _font()
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=THIN)
        for c in (8, 9, 10):
            cell = rv.cell(at, c)
            cell.fill = PatternFill("solid", fgColor=INPUT)
            cell.font = _font(color="0000FF")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=THIN)
    last = first + len(sample) - 1
    dv = DataValidation(type="list", formula1='"' + ",".join(VERDICTS) + '"', allow_blank=True, showErrorMessage=True,
                        errorTitle="Verdict", error="Choose a verdict from the list.")
    dv.add(f"H{first}:H{last}")
    rv.add_data_validation(dv)
    rv.freeze_panes = "A2"
    rv.auto_filter.ref = f"A1:J{last}"

    # ------------------------------------------------------------------ Summary
    sm = wb.create_sheet("Summary")
    sm.sheet_view.showGridLines = False
    sm["A1"] = "Sample review summary"
    sm["A1"].font = _font(size=14, bold=True, color=NAVY)
    sm["A2"] = banner
    sm.merge_cells("A2:L2")
    sm["A2"].font = _font(bold=True, color="B3471B" if is_mock else NAVY)
    sm["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    sm.row_dimensions[2].height = 32
    cols = ["Agent's result", "Statements in run", "Sampled", "Scored", "Correct", "Partly correct", "Incorrect",
            "Can't tell", "Accuracy (Correct only)", "Margin (95%)", "Proposed bar", "Result"]
    _header(sm, 4, cols)
    sm.row_dimensions[4].height = 42
    for c, w in zip("ABCDEFGHIJKL", (20, 11, 9, 8, 8, 8, 9, 7, 11, 9, 9, 14)):
        sm.column_dimensions[c].width = w
    r_c, r_h = f"Review!$C${first}:$C${last}", f"Review!$H${first}:$H${last}"
    for at, group in ((5, ALIGNED), (6, NEW)):
        sm.cell(at, 1, group)
        sm.cell(at, 2, population[group])
        sm.cell(at, 2).font = _font(color="0000FF")
        sm.cell(at, 3, f'=COUNTIF({r_c},A{at})')
        sm.cell(at, 4, f'=SUM(E{at}:H{at})')
        for c, verdict in zip((5, 6, 7, 8), VERDICTS):
            sm.cell(at, c, f'=COUNTIFS({r_c},$A{at},{r_h},"{verdict}")')
        sm.cell(at, 9, f'=IF(SUM(E{at}:G{at})=0,"",E{at}/SUM(E{at}:G{at}))').number_format = "0%"
        sm.cell(at, 10, f'=IF(I{at}="","",1.96*SQRT(I{at}*(1-I{at})/SUM(E{at}:G{at})))').number_format = "0%"
        bar_cell = sm.cell(at, 11, bar)
        bar_cell.number_format = "0%"
        bar_cell.fill = PatternFill("solid", fgColor=INPUT)
        bar_cell.font = _font(color="0000FF")
        sm.cell(at, 12, f'=IF(D{at}<C{at},"Not finished",IF(I{at}="","No verdicts",IF(I{at}>=K{at},"Met","Not met")))')
    sm["A7"] = "Whole run (weighted by result)"
    sm["A7"].alignment = Alignment(wrap_text=True)
    sm["B7"] = "=SUM(B5:B6)"
    sm["C7"] = "=SUM(C5:C6)"
    sm["D7"] = "=SUM(D5:D6)"
    for c in "EFGH":
        sm[f"{c}7"] = f"=SUM({c}5:{c}6)"
    sm["I7"] = '=IF(OR(I5="",I6=""),"",(B5*I5+B6*I6)/B7)'
    sm["I7"].number_format = "0%"
    sm.row_dimensions[7].height = 28
    for row_cells in sm["A5:L7"]:
        for cell in row_cells:
            if cell.font.color is None or cell.font.color.rgb != "000000FF":
                cell.font = _font(bold=cell.row == 7)
            cell.border = Border(bottom=THIN)
    notes = [
        "Statements in run: counted from this run's alignment_export.json, excluding test-case MSRs.",
        f"Proposed bar: {bar:.0%}, matching the evaluation's alignment precision and recall minimums in "
        "data/eval/expected_outcomes.json. Change the yellow cells if you decide on a different bar, and do it before "
        "you score. Thresholds are still to be confirmed against the course rubric.",
        "Margin: normal approximation. With about 30 statements a group it is roughly 10 points either way, so treat a "
        "result near the bar as inconclusive and report the margin with the figure.",
        "Accuracy counts Correct only, so Partly correct lowers it. Can't tell is left out.",
    ]
    for i, n in enumerate(notes):
        cell = sm.cell(9 + i, 1, n)
        sm.merge_cells(start_row=9 + i, start_column=1, end_row=9 + i, end_column=12)
        cell.font = _font(size=9, color="55657D")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        sm.row_dimensions[9 + i].height = 26
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="evaluation run stamp, a folder under evidence/runs")
    ap.add_argument("--per-group", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20261105)
    args = ap.parse_args()
    export = json.loads((ROOT / "evidence" / "runs" / args.run / "alignment_export.json").read_text(encoding="utf-8"))
    rows = eligible(export["statements"])
    population = {g: sum(group_of(r) == g for r in rows) for g in (ALIGNED, NEW)}
    sample = select(rows, args.per_group, args.seed)
    labels = json.loads((ROOT / "data" / "eval" / "expected_outcomes.json").read_text(encoding="utf-8"))
    bar = labels["thresholds"]["alignment_precision_min"]
    out = ROOT / "evidence" / "sample_review" / args.run / "sample_review.xlsx"
    build(export, sample, population, bar, args.seed, out)
    print(f"{export['run_id']} ({export['llm_mode']}): {population} eligible; sampled {len(sample)}; wrote {out}")


if __name__ == "__main__":
    main()
