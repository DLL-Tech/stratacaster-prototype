"""Free, local engine for the employee's pre-submission feedback check (PR-30).

Each draft statement is compared with the employee's REQ tasks two ways: a keyword hit from the task catalog, or
cosine similarity at or above a threshold using the same local embedding model as semantic search
(all-MiniLM-L6-v2). There is no model call and no cost. Statements are embedded in memory and discarded; nothing is
indexed or stored. The result is preliminary: the Role Execution Agent makes the assessment at submission.

Calibration (2026-11-01, roster October MSRs, workbook keyword alignments as labels; task text = title plus keywords):
at 0.45 the similarity part alone has precision 0.48 and recall 0.35, and the combined check agrees with the labels on
aligned-versus-new for about 85% of statements. Keyword labels flatter keyword matching; compare against live
assessments before relying on these numbers in deployment.
"""
from __future__ import annotations

import re

import numpy as np

from .schemas import AlignmentCandidate, AlignmentResult

_NEGATIVE = re.compile(r"\b(not completed|was not|were not|failed|incomplete|blocked)\b", re.I)


class LocalAligner:
    engine = "local"

    def __init__(self, threshold: float = 0.45, max_tasks: int = 2, elsewhere_floor: float = 0.40):
        self.threshold, self.max_tasks, self.elsewhere_floor = threshold, max_tasks, elsewhere_floor
        self._ef = None
        self._cache: dict[str, np.ndarray] = {}
        self.semantic = True
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            self._ef = DefaultEmbeddingFunction()
        except Exception:  # optional dependency: fall back to keyword hits only
            self.semantic = False

    @staticmethod
    def _task_text(t: dict) -> str:
        return f"{t['title']}. Typical work: {', '.join(t.get('keywords', []))}."

    def _vectors(self, texts: list[str], cache: bool) -> list[np.ndarray]:
        todo = [x for x in texts if not (cache and x in self._cache)]
        fresh = dict(zip(todo, (np.asarray(v, dtype=float) for v in self._ef(todo)))) if todo else {}
        if cache:
            self._cache.update(fresh)
            return [self._cache[x] for x in texts]
        return [fresh[x] for x in texts]

    def _similarities(self, statements: list[str], catalog: list[dict]) -> list[dict[str, float]]:
        if not self.semantic or not catalog or not statements:
            return [{} for _ in statements]
        tv = self._vectors([self._task_text(t) for t in catalog], cache=True)  # reference data: cached
        sv = self._vectors(statements, cache=False)  # draft text: never cached
        out = []
        for v in sv:
            out.append({t["req_task_id"]: float(v @ w / (np.linalg.norm(v) * np.linalg.norm(w)))
                        for t, w in zip(catalog, tv)})
        return out

    def align(self, statements: list[tuple[str, str]], catalog: list[dict]) -> AlignmentResult:
        sims = self._similarities([s for _, s in statements], catalog)
        candidates, gaps = [], []
        for (sid, text), sim in zip(statements, sims):
            low = text.lower()
            hits = {t["req_task_id"] for t in catalog if any(k in low for k in t.get("keywords", []))}
            ranked = sorted({*hits, *(t for t, x in sim.items() if x >= self.threshold)},
                            key=lambda t: (t not in hits, -sim.get(t, 0.0)))[:self.max_tasks]
            if not ranked:
                gaps.append(f"{sid}: no task in the REQ matched (keywords or similarity ≥ {self.threshold})")
            status = "not_completed" if _NEGATIVE.search(text) else "unclear"
            for t in ranked:
                why = "keyword match" if t in hits else f"similarity {sim[t]:.2f}"
                candidates.append(AlignmentCandidate(statement_id=sid, req_task_id=t, evidence_summary="",
                                                     rationale=why, reported_status=status))
        return AlignmentResult(candidates=candidates, unresolved_gaps=gaps)

    def nearest(self, statement: str, catalog: list[dict], n: int = 2) -> list[tuple[str, float]]:
        """The closest tasks in another catalog (e.g. elsewhere on the contract) above the floor."""
        sim = self._similarities([statement], catalog)[0]
        best = sorted(sim.items(), key=lambda kv: -kv[1])[:n]
        return [(t, round(x, 2)) for t, x in best if x >= self.elsewhere_floor]
