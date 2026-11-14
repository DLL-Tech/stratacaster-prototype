"""Failure and recovery: model errors, invalid output, retrieval failure. Traces: PR-05, PR-11."""
from helpers import SOL1, load_and_review, prepare, publish_inputs, query, records_for
from stratacaster.llm import LLMError, LLMResult, MockLLM
from stratacaster.schemas import CoverageItem, ExpertiseExplanation


class FailingLLM:
    mode, model = "test", "failing-stub"

    def __init__(self):
        self.calls = 0

    def run(self, *a, **k):
        self.calls += 1
        raise LLMError("simulated timeout")


class FlakyLLM(MockLLM):
    """Fails the first call, then behaves."""

    def __init__(self):
        self.calls = 0

    def run(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            raise LLMError("simulated transient error")
        return super().run(*a, **k)


class HallucinatingLLM:
    mode, model = "test", "hallucinating-stub"

    def run(self, task, system, payload, output_model):
        t = payload["requested_tasks"][0]["req_task_id"]
        return LLMResult(ExpertiseExplanation(
            coverage=[CoverageItem(req_task_id=t, status="supported", cited_record_ids=["SYN-EVID-999"], note="x")],
            explanation="Strong evidence [SYN-EVID-999 v1].", uncertainty="none", needs_human_review=False),
            self.mode, self.model, 0)


def test_role_agent_failure_terminates_without_partial_drafts(env):
    stub = FailingLLM()
    env.role_agent.llm = stub
    load_and_review(env, "SYN-MSR-001")
    out = prepare(env, "SYN-MSR-001")
    assert not out.ok and "failed-safe" in out.reason
    assert stub.calls == env.cfg["llm"]["max_attempts"]  # bounded retries
    assert records_for(env, "SYN-MSR-001") == []
    (esc,) = env.ops.escalations()
    assert esc["kind"] == "agent-failure"


def test_transient_failure_recovers_within_retry_budget(env):
    env.role_agent.llm = FlakyLLM()
    load_and_review(env, "SYN-MSR-001")
    out = prepare(env, "SYN-MSR-001")
    assert out.ok and len(out.data["created"]) == 1


def test_expertise_failure_is_safe(env):
    publish_inputs(env, "SYN-MSR-001")
    env.expertise_agent.llm = FailingLLM()
    r = query(env)
    assert r["status"] == "failed_safe" and r["coverage"] == [] and r["citations"] == []


def test_hallucinated_citations_are_rejected(env):
    publish_inputs(env, "SYN-MSR-001")
    env.expertise_agent.llm = HallucinatingLLM()
    r = query(env)
    assert r["status"] == "failed_safe"
    assert any("not retrieved" in e for e in r["errors"])


def test_status_service_outage_fails_closed(env):
    publish_inputs(env, "SYN-MSR-001")
    env.source_status.available = False
    r = query(env)
    assert r["status"] == "failed_safe" and r["citations"] == []
    env.source_status.available = True
    assert query(env)["status"] == "awaiting_acknowledgement"


def test_failed_publication_does_not_mark_published(env):
    load_and_review(env, "SYN-MSR-001")
    prepare(env, "SYN-MSR-001")
    (rec,) = records_for(env, "SYN-MSR-001")
    out = env.delivery.publish(env.principal("SYN-REVIEWER-PMO-001"), rec.record_id, 1, "SYN-PMO-APPROVAL-NONE")
    assert not out.ok
    after = env.evidence_store.get_record(env.principal("SYN-DELIVERY-OPS-001"), rec.record_id)
    assert after.disposition == "draft" and after.published_at is None
    assert SOL1  # requester unaffected
