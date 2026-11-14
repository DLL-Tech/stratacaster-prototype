"""Consequential actions and personnel judgments are out of scope. Traces: PR-12."""
import pytest

from helpers import publish_inputs, query
from stratacaster import content_filter as cf
from stratacaster.llm import LLMResult
from stratacaster.schemas import CoverageItem, ExpertiseExplanation


@pytest.mark.parametrize("purpose", ["personnel-ranking", "staffing-recommendation", "made-up-purpose"])
def test_prohibited_purposes_denied(env, purpose):
    publish_inputs(env, "SYN-MSR-001")
    r = query(env, purpose=purpose)
    assert r["status"] == "denied" and r["citations"] == []


class RankingLLM:
    mode, model = "test", "ranking-stub"

    def run(self, task, system, payload, output_model):
        rec = payload["records"][0]
        return LLMResult(ExpertiseExplanation(
            coverage=[CoverageItem(req_task_id=rec["req_task_id"], status="supported",
                                   cited_record_ids=[rec["record_id"]], note="Top performer on this task.")],
            explanation=f"SYN-SUBJECT-001 should be promoted [{rec['record_id']} v1].",
            uncertainty="low", needs_human_review=False), self.mode, self.model, 0)


def test_personnel_judgment_output_is_rejected(env):
    publish_inputs(env, "SYN-MSR-001")
    env.expertise_agent.llm = RankingLLM()
    r = query(env)
    assert r["status"] == "failed_safe" and r["citations"] == []


def test_no_consequential_action_surface():
    from stratacaster.solutions import SolutionsOrchestrator
    public = {m for m in dir(SolutionsOrchestrator) if not m.startswith("_")}
    # expertise_search is read-only: it ranks records the gateway already allows and takes no action
    assert public == {"handle", "expertise_search", "acknowledge", "resolve_escalation", "agent_id"}


@pytest.mark.parametrize("text", ["ranked first", "should be hired", "best engineer", "performance rating of 4"])
def test_filter_detects_personnel_language(text):
    assert cf.check_explanation(text)
