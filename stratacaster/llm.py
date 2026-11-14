"""Model service adapter.

ClaudeLLM calls the Anthropic API with structured outputs. MockLLM is a deterministic stand-in so the
environment, tests, and demo run offline and reproducibly. Both return the same LLMResult, and every
result is validated by deterministic code before it is used. The model never receives permissions,
approval authority, or tools.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import (AlignmentCandidate, AlignmentResult, CoverageItem, ExpertiseExplanation, NewTaskSuggestion,
                      PlanCandidates, PlanOption)


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    parsed: BaseModel
    mode: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def meta(self) -> dict:
        return {"llm_mode": self.mode, "model": self.model, "latency_ms": self.latency_ms,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 6)}


# ===================================================================== Claude

class ClaudeLLM:
    mode = "claude"

    def __init__(self, model: str, effort: str, timeout_s: float, price_in: float, price_out: float):
        import anthropic  # imported lazily so mock mode needs no SDK configuration

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout_s, max_retries=2)
        self.model = model
        self.effort = effort
        self.price_in, self.price_out = price_in, price_out

    def run(self, task: str, system: str, payload: dict, output_model: type[BaseModel]) -> LLMResult:
        a = self._anthropic
        t0 = time.monotonic()
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
                output_format=output_model,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        except a.RateLimitError as e:
            raise LLMError("rate limited") from e
        except a.APIStatusError as e:
            raise LLMError(f"API status {e.status_code}") from e
        latency = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens")
        if resp.parsed_output is None:
            raise LLMError("no structured output returned")
        u = resp.usage
        cost = (u.input_tokens * self.price_in + u.output_tokens * self.price_out) / 1_000_000
        return LLMResult(resp.parsed_output, self.mode, resp.model, latency, u.input_tokens, u.output_tokens, cost)


# ===================================================================== deterministic mock

_NEGATIVE = re.compile(r"\b(not completed|was not|were not|failed|no [\w\s-]{0,30}produced|incomplete|blocked)\b", re.I)
_STOP = frozenset("i me my we our the a an and or to of for in on at with by from that this those these it its as was "
                  "were be been is are had has have did do so then than into over after before about what which who "
                  "where when how why last month again also more any each other their them they he she his her "
                  "would could should can will not no kept yet still enough else".split())


class MockLLM:
    """Deterministic keyword-based stand-in. It reads source text only as data and has no tools."""
    mode = "mock"
    model = "mock-deterministic/1.0"

    def run(self, task: str, system: str, payload: dict, output_model: type[BaseModel]) -> LLMResult:
        t0 = time.monotonic()
        if task == "align":
            parsed = self._align(payload)
        elif task == "explain":
            parsed = self._explain(payload)
        elif task == "propose":
            parsed = self._propose(payload)
        else:
            raise LLMError(f"unknown task {task}")
        try:
            parsed = output_model.model_validate(parsed.model_dump())
        except ValidationError as e:
            raise LLMError(f"mock produced invalid output: {e}") from e
        return LLMResult(parsed, self.mode, self.model, int((time.monotonic() - t0) * 1000))

    @staticmethod
    def _align(payload: dict) -> AlignmentResult:
        candidates, gaps = [], []
        for st in payload["statements"]:
            text = st["text"].lower()
            matched = [t for t in payload["task_catalog"] if any(k in text for k in t["keywords"])]
            if not matched:
                gaps.append(f"{st['statement_id']}: no REQ task in the assigned subset matches this statement")
                continue
            for t in matched:
                negative = bool(_NEGATIVE.search(text))
                if negative:
                    summary = f"Synthetic status report states this REQ task was not completed: {t['title']}."
                else:
                    summary = f"Synthetic work evidence relevant to REQ task: {t['title']}."
                candidates.append(AlignmentCandidate(
                    statement_id=st["statement_id"], req_task_id=t["req_task_id"], evidence_summary=summary,
                    rationale=f"keyword match on {[k for k in t['keywords'] if k in text]}",
                    reported_status="not_completed" if negative else "completed"))
        suggestions = []
        if payload.get("suggest_new_tasks"):
            matched_ids = {c.statement_id for c in candidates}
            for st in payload["statements"]:
                if st["statement_id"] in matched_ids:
                    continue
                words = [w for w in re.findall(r"[a-z]+", st["text"].lower())
                         if w not in _STOP and len(w) > 2 and not w.endswith("ed")][:4]
                phrase = " ".join(words) or "recurring work"
                suggestions.append(NewTaskSuggestion(
                    statement_id=st["statement_id"], title=f"{phrase.capitalize()} support",
                    description=f"Plan, perform, and document {phrase} activities for the task order.",
                    rationale="No task in the assigned REQ covers this work (templated mock suggestion)."))
        return AlignmentResult(candidates=candidates, unresolved_gaps=gaps, new_task_suggestions=suggestions)

    @staticmethod
    def _propose(payload: dict) -> PlanCandidates:
        """Thought generator stand-in: one option per operator that the inputs make possible. Wording is a short,
        templated phrase from content words (never a copied sentence); a real model writes proper task wording."""
        words = [w for w in re.findall(r"[a-z]+", payload["cluster"]["text"].lower())
                 if w not in _STOP and len(w) > 2 and not w.endswith("ed")][:4]
        phrase = " ".join(words).capitalize() or "Recurring work"
        options = [PlanOption(operator="no-change", target_req_task_id=None, wording="No REQ change",
                              rationale="Treat as coordination or one-off work, not a deliverable.")]
        tokens = set(words)
        best = max(payload["req_tasks"], key=lambda t: len(tokens & set(t["title"].lower().split())), default=None)
        if best and tokens & set(best["title"].lower().split()):
            options.append(PlanOption(operator="widen-task", target_req_task_id=best["req_task_id"],
                                      wording=f"{best['title']} (including {phrase.lower()})",
                                      rationale="Closest existing task in this REQ."))
        for t in payload.get("elsewhere", []):
            options.append(PlanOption(operator="route-elsewhere", target_req_task_id=t["req_task_id"],
                                      wording=t["title"], rationale=f"Already a task in {t['tto_id']}."))
        options.append(PlanOption(operator="add-task", target_req_task_id=None, wording=f"{phrase} record",
                                  rationale="Recurring work with no matching task in this REQ."))
        options.append(PlanOption(operator="new-req", target_req_task_id=None, wording=f"{phrase} role scope",
                                  rationale="Enough recurring work to justify a separate REQ."))
        return PlanCandidates(options=options)

    @staticmethod
    def _explain(payload: dict) -> ExpertiseExplanation:
        coverage, lines, review = [], [], False
        for task in payload["requested_tasks"]:
            recs = [r for r in payload["records"] if r["req_task_id"] == task["req_task_id"]]
            cites = [r["record_id"] for r in recs]
            neg = [r for r in recs if _NEGATIVE.search(r["evidence_summary"])]
            pos = [r for r in recs if r not in neg]
            if not recs:
                status, note = "unsupported", "No approved filtered evidence was retrieved for this task."
            elif pos and neg:
                status, note = "conflict", "Approved records disagree about completion; a human must resolve this."
            elif neg:
                status, note = "unsupported", "Approved evidence states the task was not completed."
            else:
                status, note = "supported", f"{len(pos)} approved record(s) describe relevant synthetic work."
            review = review or status != "supported"
            coverage.append(CoverageItem(req_task_id=task["req_task_id"], status=status,
                                         cited_record_ids=cites, note=note))
            cite_txt = ", ".join(f"[{r['record_id']} v{r['record_version']}]" for r in recs) or "[no citation]"
            lines.append(f"{task['req_task_id']} ({task['title']}): {status}. {note} {cite_txt}")
        return ExpertiseExplanation(
            coverage=coverage,
            explanation="Requirement-to-evidence coverage based only on PMO-approved filtered records:\n"
                        + "\n".join(f"- {l}" for l in lines),
            uncertainty="Coverage reflects keyword-level alignment of synthetic summaries; confidence is not "
                        "calibrated and this is not an assessment of any person.",
            needs_human_review=review)


def make_llm(cfg: dict) -> Any:
    mode = os.environ.get("STRATACASTER_LLM_MODE", cfg.get("mode", "auto")).lower()
    has_creds = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if mode == "mock" or (mode == "auto" and not has_creds):
        return MockLLM()
    return ClaudeLLM(cfg["model"], cfg.get("effort", "medium"), cfg.get("timeout_s", 120),
                     cfg["price_per_mtok"]["input"], cfg["price_per_mtok"]["output"])
