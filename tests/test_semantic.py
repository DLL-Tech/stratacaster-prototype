"""Semantic expertise search behind the gateway: Chroma + all-MiniLM-L6-v2, 8 -> 4 rerank, coverage, retry (PR-28)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from stratacaster.demo import run_roster_demo  # noqa: E402
from stratacaster.environment import Environment  # noqa: E402
from stratacaster.llm import MockLLM  # noqa: E402

CLOSED = "Who has confirmed that reported fixes were actually closed?"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Environment(var_dir=tmp_path_factory.mktemp("sem") / "var", llm=MockLLM(), dataset="roster")
    run_roster_demo(e, say=lambda *_: None)
    e.clock.set(e.registry.demo["times"]["query"])  # the demo ends past expiry
    try:
        e.orchestrator.expertise_search(e.principal("SIM-EMP-210"), "warm up", "SIM-AUD-SOLUTIONS")
    except Exception as exc:  # pragma: no cover - model download unavailable offline
        pytest.skip(f"embedding model unavailable: {exc}")
    yield e
    e.close()


def search(env, q=CLOSED, who="SIM-EMP-210", aud="SIM-AUD-SOLUTIONS", **kw):
    return env.orchestrator.expertise_search(env.principal(who), q, aud, **kw)


def test_eight_candidates_reranked_to_four_with_requirement_and_evidence(env):
    t = search(env)["trace"]
    assert len(t["candidates"]) == 8 and len(t["results"]) == 4
    assert {r["kind"] for r in t["results"]} == {"requirement", "evidence"} and t["status"] == "complete"
    assert "Chroma" in t["vector_store"] and "all-MiniLM-L6-v2" in t["embedding_model"]
    scores = [r["score"] for r in t["results"]]
    assert scores == sorted(scores, reverse=True)


def test_revoked_evidence_is_excluded_before_ranking_with_reasons(env):
    t = search(env)["trace"]
    revoked = next(b for b in t["excluded_from_ranking"] if "disposition revoked" in b["reasons"])
    assert revoked["id"] not in {f"{r['id']}:v{r.get('version')}" for r in t["results"]}
    assert all(r["kind"] == "requirement" or r["published_at"] for r in t["results"])


def test_missing_category_gets_one_targeted_retry(env):
    t = search(env)["trace"]
    assert t["retry"] and t["retry"]["missing"] == "requirement" and t["retry"]["found"] == ["SIM-REQ-1-01-07"]
    assert any(r["kind"] == "requirement" and r["id"] == "SIM-REQ-1-01-07" for r in t["results"])


def test_expired_evidence_leaves_search_incomplete_and_escalates(env):
    env.clock.set("2026-11-24T17:00:00Z")  # past the 31-day freshness window
    try:
        body = search(env)
        t = body["trace"]
        assert t["status"] == "incomplete" and t["corpus"]["eligible_records"] == 0
        assert all(r["kind"] == "requirement" for r in t["results"]) and body["escalations"]
    finally:
        env.clock.set(env.registry.demo["times"]["query"])


def test_denied_paths(env):
    assert search(env, who="SIM-EMP-221")["status"] == "denied"  # Growth user, Solutions audience
    assert search(env, aud="SIM-AUD-SOLUTIONS", purpose="personnel-ranking")["status"] == "denied"
    assert search(env, q="   ")["status"] == "denied"


def test_trace_is_written_and_carries_no_msr_text(env):
    body = search(env, query_id="SIM-SEARCH-T1")
    path = Path(body["trace"]["trace_file"])
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    trace = json.loads(text)
    for key in ("question", "vector_store", "embedding_model", "corpus", "filters", "candidates",
                "excluded_from_ranking", "rerank", "retry", "results", "status"):
        assert key in trace
    for c in env.msr_cases("all"):
        assert c["input"]["raw_narrative"] not in text
