"""PR-32: the evaluation's report-only feedback-check section, its statement export, and the human sample review."""
import importlib.util
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evaluation = load("run_evaluation")
review = load("build_sample_review")


class Metered(MockLLM):
    def __init__(self):
        self.calls = 0

    def run(self, task, system, payload, output_model):
        self.calls += 1
        return super().run(task, system, payload, output_model)


@pytest.fixture
def renv(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATACASTER_FEEDBACK_MODE", "local")
    llm = Metered()
    e = Environment(var_dir=tmp_path / "var", llm=llm, dataset="roster")
    yield e, llm
    e.close()


def row(sid, tag, req="REQ-A", test_case=False):
    return {"statement_id": sid, "input_id": "SIM-MSR-1", "text": f"statement {sid}", "role_req_id": req,
            "tto_id": "T", "tag": tag, "aligned": [{"req_task_id": "T1", "title": "Task"}] if tag == "aligned" else [],
            "req_tasks": [{"req_task_id": "T1", "title": "Task"}], "test_case": test_case}


def test_feedback_section_meets_every_gate_on_the_free_check(renv):
    env, llm = renv
    out = evaluation.run_feedback_check(env, llm)
    assert out["status"] == "completed", out
    assert out["gate_checks_met"] == len(out["gate_checks"]) == 7, out["gate_checks"]
    assert out["model_calls_in_check"] == 0 and out["drafts_submitted"] == 2 and out["statements"] == 6
    assert 0 <= out["check_agreed_with_assessment"] <= out["statements"]


def test_feedback_section_reports_a_failure_rather_than_raising(renv):
    env, llm = renv
    env.delivery.preview_msr = None  # any internal failure
    assert evaluation.run_feedback_check(env, llm)["status"] == "error"


def test_export_lists_each_statement_with_the_result_and_no_names(renv):
    env, llm = renv
    me, month = env.principal("SIM-EMP-005", "employee"), "2026-11"
    env.clock.set(evaluation.FEEDBACK_TIME)
    sub = env.delivery.submit_msr(me, month, ["I reran checks on the systems that were missed.",
                                              "I organized the team picnic."])
    assert sub.ok, sub.reason
    op = env.principal("SIM-EMP-002", "operator")
    rows = evaluation.export_alignments(env, {(sub.data["input_id"], sub.data["input_version"]): op})
    assert len(rows) == 2 and rows[0]["text"].startswith("I reran") and rows[0]["req_tasks"]
    assert {r["tag"] for r in rows} == {"aligned", "potential-new-task-requirement"}
    assert not any(k in rows[0] for k in ("subject_id", "name", "employee"))
    assert rows[1]["aligned"] == [] and not rows[0]["test_case"]


def test_sample_is_repeatable_balanced_and_excludes_test_cases():
    rows = [row(f"A{i}", "aligned", f"REQ-{i % 3}") for i in range(50)]
    rows += [row(f"N{i}", "potential-new-task-requirement", f"REQ-{i % 3}") for i in range(50)]
    rows += [row("T1", "aligned", test_case=True), row("E1", "aligned") | {"text": "  "}]
    pool = review.eligible(rows)
    assert len(pool) == 100
    one, two = review.select(pool, 10, 7), review.select(pool, 10, 7)
    assert [r["statement_id"] for r in one] == [r["statement_id"] for r in two]
    assert [r["statement_id"] for r in one] != [r["statement_id"] for r in review.select(pool, 10, 8)]
    assert sum(review.group_of(r) == review.ALIGNED for r in one) == 10 and len(one) == 20
    assert len({r["statement_id"] for r in one}) == 20
    # round-robin: no REQ dominates a group
    reqs = [r["role_req_id"] for r in one if review.group_of(r) == review.ALIGNED]
    assert max(reqs.count(x) for x in set(reqs)) - min(reqs.count(x) for x in set(reqs)) <= 1


def test_sample_takes_everything_when_a_group_is_small():
    rows = [row(f"A{i}", "aligned") for i in range(3)] + [row("N0", "potential-new-task-requirement")]
    assert len(review.select(rows, 30, 1)) == 4


def test_workbook_marks_a_stand_in_run_as_a_dry_run_and_uses_live_formulas(tmp_path):
    rows = [row(f"A{i}", "aligned") for i in range(4)] + [row(f"N{i}", "potential-new-task-requirement") for i in range(4)]
    export = {"run_id": "EVAL-X", "llm_mode": "mock", "model": "mock"}
    out = tmp_path / "s.xlsx"
    review.build(export, review.select(rows, 2, 1), {review.ALIGNED: 4, review.NEW: 4}, 0.9, 1, out)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Instructions", "Review", "Summary"]
    assert "DRY RUN" in wb["Instructions"]["A2"].value and "DRY RUN" in wb["Summary"]["A2"].value
    assert wb["Summary"]["C5"].value.startswith("=COUNTIF") and wb["Summary"]["I5"].value.startswith("=IF")
    assert wb["Review"].max_row == 5 and wb["Review"]["H2"].value is None
    assert wb["Review"].data_validations.dataValidation[0].sqref is not None


def test_workbook_for_a_live_run_is_not_labeled_a_dry_run(tmp_path):
    export = {"run_id": "EVAL-Y", "llm_mode": "claude", "model": "claude-opus-5-5"}
    out = tmp_path / "s.xlsx"
    review.build(export, review.select([row("A0", "aligned")], 1, 1), {review.ALIGNED: 1, review.NEW: 0}, 0.9, 1, out)
    assert "DRY RUN" not in load_workbook(out)["Instructions"]["A2"].value
