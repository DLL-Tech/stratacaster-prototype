"""DEF-007, found in the first full Claude-mode run (EVAL-20261031T195018Z): "reported as not completed" was read from
the wording of the model's summary, so Claude's phrasing missed real conflicts and an innocent "failed" created a false
one. Status now comes from a structured field, and conflicts are decided by code. Traces: PR-08, PR-21."""
from helpers import load_and_review, prepare, publish_inputs, query, records_for

from stratacaster.llm import LLMResult
from stratacaster.schemas import AlignmentCandidate, AlignmentResult, CoverageItem, ExpertiseExplanation


class FixedAlignment:
    """Stub Role Execution Agent model: one candidate per statement with chosen wording and status."""
    mode, model = "test", "fixed-alignment"

    def __init__(self, summary, status):
        self.summary, self.status = summary, status

    def run(self, task, system, payload, output_model):
        task_id = payload["task_catalog"][0]["req_task_id"]
        out = AlignmentResult(candidates=[
            AlignmentCandidate(statement_id=st["statement_id"], req_task_id=task_id, evidence_summary=self.summary,
                               rationale="stub", reported_status=self.status) for st in payload["statements"][:1]],
            unresolved_gaps=[])
        return LLMResult(out, self.mode, self.model, 0)


class AlwaysSupported:
    """Stub Expertise Agent model that calls every task with records 'supported', even when they disagree."""
    mode, model = "test", "always-supported"

    def run(self, task, system, payload, output_model):
        cov = []
        for t in payload["requested_tasks"]:
            ids = [r["record_id"] for r in payload["records"] if r["req_task_id"] == t["req_task_id"]]
            cov.append(CoverageItem(req_task_id=t["req_task_id"], status="supported" if ids else "unsupported",
                                    cited_record_ids=ids, note="stub"))
        out = ExpertiseExplanation(coverage=cov, explanation="Prototype output: coverage summary.",
                                   uncertainty="stub", needs_human_review=False)
        return LLMResult(out, self.mode, self.model, 0)


def test_status_comes_from_the_structured_field_not_the_wording(env):
    load_and_review(env, "SYN-MSR-001")
    env.role_agent.llm = FixedAlignment("Rechecked the systems that failed last month and closed the work.", "completed")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    assert "not completed" not in rec.evidence_summary.lower()  # "failed" in the wording no longer flips it


def test_not_completed_status_adds_the_fixed_marker(env):
    load_and_review(env, "SYN-MSR-001")
    env.role_agent.llm = FixedAlignment("The retest did not confirm that the fix held.", "not_completed")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    assert rec.evidence_summary.endswith("(reported as not completed).")


def test_conflict_is_decided_by_code_even_if_the_explainer_says_supported(env):
    publish_inputs(env, "SYN-MSR-001", "SYN-MSR-006")  # one record done, one reported not completed (mock alignment)
    env.expertise_agent.llm = AlwaysSupported()
    r = query(env)
    (c,) = r["coverage"]
    assert c["status"] == "conflict" and "deterministic rule" in c["note"]
    assert sorted(c["cited_record_ids"]) == ["SYN-EVID-001", "SYN-EVID-002"]
    assert any(e["kind"] == "evidence-conflict" for e in env.ops.escalations())
