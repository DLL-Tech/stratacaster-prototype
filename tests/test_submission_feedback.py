"""PR-30: employee feedback before submission. The Role Execution Agent checks a draft MSR against the employee's REQ
and recommends a new task for unmatched statements; nothing is stored or audited until the employee submits with a
response to each statement, and the TTO lead then sees those responses."""
import pytest

from stratacaster.delivery import POTENTIAL_NEW_TASK
from stratacaster.environment import Environment
from stratacaster.llm import LLMResult, MockLLM
from stratacaster.schemas import AlignmentResult, NewTaskSuggestion

PREP = "2026-10-23T16:00:00Z"
# Rowan (SIM-EMP-005, WR-001, SIM-TTO-1-01). Bullet 1 aligns to REQ tasks; bullet 2 aligns to none.
BULLETS = ["I reran checks on the systems that were missed and added them to the patching queue.",
           "I organized the team picnic."]


class CountingLLM(MockLLM):
    def __init__(self):
        self.tasks = []

    def run(self, task, system, payload, output_model):
        self.tasks.append((task, payload.get("suggest_new_tasks", False)))
        return super().run(task, system, payload, output_model)


@pytest.fixture
def llm():
    return CountingLLM()


def make_env(tmp_path, llm, mode, monkeypatch):
    monkeypatch.setenv("STRATACASTER_FEEDBACK_MODE", mode)
    e = Environment(var_dir=tmp_path / f"var-{mode}", llm=llm, dataset="roster")
    e.clock.set(PREP)
    return e


@pytest.fixture
def renv(tmp_path, llm, monkeypatch):
    """The 'model' deployment option: the full agent check, reused at submission."""
    e = make_env(tmp_path, llm, "model", monkeypatch)
    yield e
    e.close()


@pytest.fixture
def lenv(tmp_path, llm, monkeypatch):
    """The prototype default: the free local check."""
    e = make_env(tmp_path, llm, "local", monkeypatch)
    yield e
    e.close()


def rowan(env):
    return env.principal("SIM-EMP-005", "employee")


def footprint(env):
    ops = env.principal("SIM-EMP-002", "operator")
    return (len(env.delivery_store.list_inputs(ops)), len(env.evidence_store.list_records(ops)),
            len(env.audit.entries()))


def answers(rows, aligned="accurate", new="confirm_new"):
    return {r["index"]: {"response": aligned if r["tag"] == "aligned" else new} for r in rows}


def test_feedback_check_shows_alignment_and_a_recommended_task_and_writes_nothing(renv):
    before = footprint(renv)
    out = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    assert out.ok, out.reason
    first, second = out.data["statements"]
    assert first["tag"] == "aligned" and first["aligned"] and first["suggested_task"] is None
    assert second["tag"] == POTENTIAL_NEW_TASK and not second["aligned"]
    assert second["suggested_task"]["title"] and second["suggested_task"]["description"]
    assert footprint(renv) == before  # no input, no draft, no audit entry


def test_submitting_after_the_check_reuses_it_and_records_responses_for_the_tto_lead(renv, llm):
    pv = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    calls = len(llm.tasks)
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", BULLETS, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]))
    assert out.ok, out.reason
    assert all(not suggest for _, suggest in llm.tasks[calls:])  # only the contract-wide analytic ran again
    a = out.data["assessment"]["assessment"]
    assert a["employee_feedback"]["confirm_new"] == 1 and a["employee_feedback"]["needs_higher_review"]
    new = next(s for s in a["statements"] if s["tag"] == POTENTIAL_NEW_TASK)
    assert new["employee_response"] == "confirm_new" and new["suggested_task"]["title"]
    lead = renv.principal("SIM-EMP-002", "tto")
    row = next(r for r in renv.delivery.tto_review_queue(lead) if r["input_id"] == out.data["input_id"])
    assert row["employee_feedback"].startswith("Needs review: 1 new task(s) confirmed")


def test_disputed_alignment_names_the_intended_task(renv):
    pv = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    rows = pv.data["statements"]
    task = rows[0]["aligned"][0]["req_task_id"]
    resp = answers(rows, new="not_new") | {1: {"response": "not_intended", "intended_req_task_id": task,
                                               "note": "Meant the retest task."}}
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", BULLETS, preview_id=pv.data["preview_id"], responses=resp)
    a = out.data["assessment"]["assessment"]
    assert a["statements"][0]["intended_req_task_id"] == task and a["statements"][0]["notes"] == "Meant the retest task."
    assert a["employee_feedback"]["not_intended"] == 1 and a["employee_feedback"]["not_new"] == 1


def test_an_edited_draft_must_be_checked_again(renv):
    pv = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    edited = BULLETS[:1] + ["I organized the team picnic and the holiday party."]
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", edited, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]))
    assert not out.ok and "changed after the feedback check" in out.reason


@pytest.mark.parametrize("bad,why", [
    ({1: {"response": "confirm_new"}}, "response must be one of accurate, not_intended"),
    ({1: {"response": "not_intended", "intended_req_task_id": "SIM-REQ-TASK-NOPE"}}, "not a task in your REQ"),
])
def test_responses_are_validated_against_each_statement(renv, bad, why):
    pv = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", BULLETS, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]) | bad)
    assert not out.ok and why in out.reason


def test_a_check_belongs_to_its_employee_and_expires(renv):
    pv = renv.delivery.preview_msr(rowan(renv), "2026-11", BULLETS)
    other = renv.principal("SIM-EMP-006", "employee")
    out = renv.delivery.submit_msr(other, "2026-11", BULLETS, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]))
    assert not out.ok and "another employee" in out.reason
    renv.clock.advance(hours=2)
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", BULLETS, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]))
    assert not out.ok and "expired" in out.reason


class CopyingLLM(MockLLM):
    """Returns a 'recommended task' that copies the employee's statement, plus personnel wording."""

    def run(self, task, system, payload, output_model):
        res = super().run(task, system, payload, output_model)
        if not payload.get("suggest_new_tasks"):
            return res
        st = payload["statements"][-1]
        bad = [NewTaskSuggestion(statement_id=st["statement_id"], title="Team picnic", description=st["text"],
                                 rationale="copied")]
        return LLMResult(AlignmentResult(candidates=res.parsed.candidates, unresolved_gaps=res.parsed.unresolved_gaps,
                                         new_task_suggestions=bad), "test", "copying", 0)


def test_copied_or_personnel_suggestions_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATACASTER_FEEDBACK_MODE", "model")
    env = Environment(var_dir=tmp_path / "var", llm=CopyingLLM(), dataset="roster")
    try:
        env.clock.set(PREP)
        out = env.delivery.preview_msr(rowan(env), "2026-11", BULLETS)
        assert out.ok and out.data["statements"][-1]["suggested_task"] is None
    finally:
        env.close()
    assert env.delivery._usable_suggestion("Vulnerability retest records", "Document retest outcomes.", BULLETS[0])
    assert not env.delivery._usable_suggestion("Top performer tracking", "Rank the best engineer.", BULLETS[0])


def test_submission_without_a_check_still_works(renv):
    out = renv.delivery.submit_msr(rowan(renv), "2026-11", BULLETS)
    assert out.ok and "employee_feedback" not in out.data["assessment"]["assessment"]


# ---------------------------------------------------------------- the free local check (prototype default)

def test_local_check_is_free_preliminary_and_writes_nothing(lenv, llm):
    before = footprint(lenv)
    out = lenv.delivery.preview_msr(rowan(lenv), "2026-11", BULLETS)
    assert out.ok and out.data["engine"] == "local" and out.data["preliminary"]
    assert llm.tasks == [] and out.data["model"]["cost_usd"] == 0.0  # no model call at all
    first, second = out.data["statements"]
    assert first["tag"] == "aligned" and second["tag"] == POTENTIAL_NEW_TASK
    assert second["suggested_task"] is None  # wording comes from the assessment at submission
    assert footprint(lenv) == before


def test_local_check_matches_by_meaning_not_only_keywords(lenv):
    """A paraphrase with none of the task's keywords still aligns when the local embedding is similar enough."""
    if not lenv.local_aligner.semantic:
        pytest.skip("chromadb not installed")
    req = lenv.registry.role_req_for("WR-001", "SIM-TTO-1-01")
    task = lenv.registry.req_tasks[req["req_task_ids"][0]]
    res = lenv.local_aligner.align([("S1", f"Worked on {task['title'].lower()} this month.")], [task])
    assert [c.req_task_id for c in res.candidates] == [task["req_task_id"]]
    assert res.candidates[0].rationale.startswith(("keyword", "similarity"))


def test_submitting_after_a_local_check_runs_the_agent_once_with_task_wording(lenv, llm):
    pv = lenv.delivery.preview_msr(rowan(lenv), "2026-11", BULLETS)
    out = lenv.delivery.submit_msr(rowan(lenv), "2026-11", BULLETS, preview_id=pv.data["preview_id"],
                                   responses=answers(pv.data["statements"]))
    assert out.ok, out.reason
    assert ("align", True) in llm.tasks  # the assessment wrote the recommended task
    a = out.data["assessment"]["assessment"]
    fb = a["employee_feedback"]
    assert fb["check_engine"] == "local" and fb["confirm_new"] == 1
    new = next(s for s in a["statements"] if s["tag"] == POTENTIAL_NEW_TASK)
    assert new["suggested_task"]["title"] and new["check_agreed"]


def test_a_check_that_disagrees_with_the_assessment_is_flagged(lenv):
    pv = lenv.delivery.preview_msr(rowan(lenv), "2026-11", BULLETS)
    check = lenv.delivery._previews[pv.data["preview_id"]]
    check["statements"][1]["tag"] = "aligned"  # pretend the free check had called the picnic aligned
    resp = answers(pv.data["statements"]) | {2: {"response": "accurate"}}
    out = lenv.delivery.submit_msr(rowan(lenv), "2026-11", BULLETS, preview_id=pv.data["preview_id"], responses=resp)
    a = out.data["assessment"]["assessment"]
    assert a["employee_feedback"]["check_differed"] == 1 and a["employee_feedback"]["needs_higher_review"]
    assert "assessed differently" in lenv.delivery._feedback_text(a)


def test_hybrid_option_asks_the_small_model_only_for_unmatched_statements(tmp_path, llm, monkeypatch):
    env = make_env(tmp_path, llm, "hybrid", monkeypatch)
    try:
        out = env.delivery.preview_msr(rowan(env), "2026-11", BULLETS)
        assert out.ok and out.data["engine"] == "hybrid"
        assert llm.tasks == [("align", True)]  # one wording call
        first, second = out.data["statements"]
        assert first["suggested_task"] is None and second["suggested_task"]["title"]
    finally:
        env.close()


def test_unknown_feedback_mode_is_refused(tmp_path, llm, monkeypatch):
    with pytest.raises(ValueError):
        make_env(tmp_path, llm, "turbo", monkeypatch)
