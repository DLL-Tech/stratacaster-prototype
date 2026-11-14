"""Semantic expertise search (capstone checkpoint 3.1): a Chroma vector store with all-MiniLM-L6-v2 embeddings,
placed behind the retrieval gateway.

The vector index is a rebuildable cache over two shared, non-raw corpora: requirement tasks (reference data) and
records that have been published (PMO-approved, filtered; never MSR text). Governance decides eligibility first: only
records that pass every gateway check may be ranked. The search then takes the eight most similar candidates and
reranks them to four with a coverage rule (the controlling requirement and approved evidence). If a category is
missing it makes one targeted retry, then escalates. Every search writes a trace.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .clock import parse_utc

EMBEDDING_MODEL = "all-MiniLM-L6-v2 (ONNX build via chromadb DefaultEmbeddingFunction; 384 dimensions)"
K_CANDIDATES, K_SELECTED = 8, 4
WEIGHTS = {"similarity": 0.60, "authority": 0.25, "recency": 0.15}
AUTHORITY = {"requirement": 1.00, "own-req": 1.00, "similar-work": 0.80, "collaboration-opportunity": 0.70}
FRESHNESS_DAYS = 31


class SemanticUnavailable(Exception):
    pass


class SemanticIndex:
    def __init__(self, registry):
        try:
            import chromadb
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        except ImportError as e:  # optional dependency: the governed workflows do not need it
            raise SemanticUnavailable("chromadb is not installed") from e
        self.registry = registry
        self.chroma_version = chromadb.__version__
        self.client = chromadb.EphemeralClient()  # a cache: rebuilt from the authoritative stores, never trusted alone
        ef = DefaultEmbeddingFunction()
        space = {"hnsw:space": "cosine"}
        name = f"sc-{uuid.uuid4().hex[:8]}"
        self.req = self.client.create_collection(f"{name}-req", embedding_function=ef, metadata=space)
        self.ev = self.client.create_collection(f"{name}-ev", embedding_function=ef, metadata=space)
        self._req_loaded = False
        self._ev_keys: set[str] = set()

    def sync(self, records) -> None:
        """Index requirement tasks once and any newly published record versions (incremental)."""
        reg = self.registry
        if not self._req_loaded:
            ids, docs, metas = [], [], []
            for t in reg.req_tasks.values():
                tto = reg.ttos.get(t["tto_id"], {})
                roles = sorted({reg.role_tasks.get(rt, {}).get("role_id", rt) for rt in reg.eligible_role_tasks(t)})
                ids.append(t["req_task_id"])
                docs.append(f"{t['title']}. Task order: {tto.get('title', t['tto_id'])}.")
                metas.append({"kind": "requirement", "req_task_id": t["req_task_id"], "tto_id": t["tto_id"],
                              "contract_id": tto.get("contract_id", ""), "roles": ", ".join(roles)})
            self.req.add(ids=ids, documents=docs, metadatas=metas)
            self._req_loaded = True
        new = [r for r in records if f"{r.record_id}:v{r.record_version}" not in self._ev_keys]
        if new:
            self.ev.add(ids=[f"{r.record_id}:v{r.record_version}" for r in new],
                        documents=[self._evidence_text(r) for r in new],
                        metadatas=[{"kind": "evidence", "key": f"{r.record_id}:v{r.record_version}",
                                    "req_task_id": r.req_task_id, "tto_id": r.tto_id, "contract_id": r.contract_id,
                                    "scope": r.alignment_scope} for r in new])
            self._ev_keys.update(f"{r.record_id}:v{r.record_version}" for r in new)

    def _evidence_text(self, rec) -> str:
        """What is embedded for a record: the task, the work role, and the task order, from reference data. The
        templated summary (names and IDs) would otherwise dominate similarity."""
        reg = self.registry
        task = reg.req_tasks.get(rec.req_task_id, {}).get("title", rec.req_task_id)
        role = reg.roles.get(rec.role_id, {}).get("title", rec.role_id)
        tto = reg.ttos.get(rec.tto_id, {}).get("title", rec.tto_id)
        status = " Reported as not completed." if "not completed" in rec.evidence_summary else ""
        return f"{task}. Work reported by a {role} in {tto}.{status}"

    @staticmethod
    def _rows(res):
        return [{"id": i, "distance": round(d, 4), "similarity": round(1 - d, 4), "meta": m, "text": t}
                for i, d, m, t in zip(res["ids"][0], res["distances"][0], res["metadatas"][0], res["documents"][0])]

    def query_requirements(self, question: str, n: int, where: dict | None = None):
        return self._rows(self.req.query(query_texts=[question], n_results=min(n, self.req.count()), where=where))

    def query_evidence(self, question: str, n: int, keys: list[str] | None = None, where: dict | None = None):
        if keys is not None and not keys:
            return []
        clause = {"key": {"$in": keys}} if keys is not None else None
        if where:
            clause = {"$and": [clause, where]} if clause else where
        return self._rows(self.ev.query(query_texts=[question], n_results=min(n, self.ev.count()), where=clause))


def _recency(published_at: str | None, now) -> float:
    if not published_at:
        return 1.0  # requirement tasks: the current controlling version
    age = (now - parse_utc(published_at)).total_seconds() / 86400
    return round(max(0.0, min(1.0, 1 - age / FRESHNESS_DAYS)), 4)


def search(index: SemanticIndex, gateway, registry, clock, requester, question: str, audience_id: str, purpose: str,
           query_id: str, correlation_id: str) -> dict:
    """Run one governed semantic search and return its trace (results are inside the trace)."""
    now = clock.now()
    eligible, excluded = gateway.eligibility(requester, audience_id, purpose, correlation_id)
    index.sync([r for r in eligible] + [r for r, _ in excluded])
    by_key = {f"{r.record_id}:v{r.record_version}": r for r in eligible}
    excluded_reasons = {f"{r.record_id}:v{r.record_version}": reasons for r, reasons in excluded}

    reqs = index.query_requirements(question, K_CANDIDATES)
    evs = index.query_evidence(question, K_CANDIDATES, keys=sorted(by_key))
    # What an ungoverned similarity search would have returned, so the trace can show what was kept out and why.
    raw = index.query_evidence(question, K_CANDIDATES)
    blocked = [{"id": r["id"], "similarity": r["similarity"], "reasons": excluded_reasons.get(r["id"], [])}
               for r in raw if r["id"] not in by_key]

    pool = sorted(reqs + evs, key=lambda r: r["distance"])[:K_CANDIDATES]

    def score(row):
        meta = row["meta"]
        rec = by_key.get(row["id"])
        authority = AUTHORITY["requirement"] if meta["kind"] == "requirement" else AUTHORITY.get(meta["scope"], 0.5)
        recency = _recency(rec.published_at if rec else None, now)
        total = (WEIGHTS["similarity"] * row["similarity"] + WEIGHTS["authority"] * authority
                 + WEIGHTS["recency"] * recency)
        return {"authority": authority, "recency": recency, "score": round(total, 4)}

    for row in pool:
        row.update(score(row))
    retry = None
    kinds = {r["meta"]["kind"] for r in pool}
    if "evidence" not in kinds and reqs:  # targeted retry: evidence for the best-scored requirement task
        top_req = max((r for r in pool if r["meta"]["kind"] == "requirement"), key=lambda r: r["score"])
        extra = index.query_evidence(question, K_CANDIDATES, keys=sorted(by_key),
                                     where={"req_task_id": top_req["meta"]["req_task_id"]})
        retry = {"missing": "evidence", "filter": {"req_task_id": top_req["meta"]["req_task_id"]},
                 "found": [r["id"] for r in extra]}
        for row in extra:
            row.update(score(row))
        pool += extra
    elif "requirement" not in kinds and evs:  # targeted retry: the controlling requirement for the best evidence
        top_ev = max((r for r in pool if r["meta"]["kind"] == "evidence"), key=lambda r: r["score"])
        extra = index.query_requirements(question, 1, where={"req_task_id": top_ev["meta"]["req_task_id"]})
        retry = {"missing": "requirement", "filter": {"req_task_id": top_ev["meta"]["req_task_id"]},
                 "found": [r["id"] for r in extra]}
        for row in extra:
            row.update(score(row))
        pool += extra

    ranked = sorted(pool, key=lambda r: -r["score"])
    selected = []
    for kind in ("requirement", "evidence"):  # coverage rule: one of each when available
        best = next((r for r in ranked if r["meta"]["kind"] == kind), None)
        if best:
            selected.append(best)
    for r in ranked:
        if len(selected) >= K_SELECTED:
            break
        if r not in selected:
            selected.append(r)
    selected.sort(key=lambda r: -r["score"])
    covered = {r["meta"]["kind"] for r in selected}
    status = "complete" if covered >= {"requirement", "evidence"} else "incomplete"

    results = []
    for r in selected:
        meta = r["meta"]
        if meta["kind"] == "requirement":
            t = registry.req_tasks[meta["req_task_id"]]
            n_ev = sum(1 for rec in eligible if rec.req_task_id == meta["req_task_id"])
            results.append({"kind": "requirement", "id": meta["req_task_id"], "title": t["title"],
                            "tto_id": meta["tto_id"], "eligible_records_for_task": n_ev, "score": r["score"]})
        else:
            rec = by_key[r["id"]]
            results.append({"kind": "evidence", "id": rec.record_id, "version": rec.record_version,
                            "req_task_id": rec.req_task_id, "alignment_scope": rec.alignment_scope,
                            "summary": rec.evidence_summary, "published_at": rec.published_at,
                            "approved_at": rec.pmo_approval.approved_at, "expires_at": rec.freshness.expires_at,
                            "score": r["score"]})
    return {
        "query_id": query_id, "correlation_id": correlation_id, "executed_at": clock.now_str(),
        "question": question, "requester_id": requester.user_id, "audience_id": audience_id, "purpose": purpose,
        "vector_store": f"Chroma {index.chroma_version} (in-process, rebuilt from the evidence store)",
        "embedding_model": EMBEDDING_MODEL,
        "corpus": {"requirement_tasks": index.req.count(), "indexed_record_versions": index.ev.count(),
                   "eligible_records": len(eligible), "excluded_records": len(excluded),
                   "reference_fixture": registry.version},
        "filters": {"hard": "gateway checks: published, current approval, current reviewed and TTO-approved source, "
                            "unexpired, audience, purpose, restrictions"},
        "candidates": [{"id": r["id"], "kind": r["meta"]["kind"], "similarity": r["similarity"],
                        "distance": r["distance"]} for r in pool[:K_CANDIDATES]],
        "excluded_from_ranking": blocked,
        "rerank": {"weights": WEIGHTS, "authority": AUTHORITY, "recency": f"1 - age_days/{FRESHNESS_DAYS}",
                   "scored": [{"id": r["id"], "similarity": r["similarity"], "authority": r["authority"],
                               "recency": r["recency"], "score": r["score"]} for r in ranked]},
        "retry": retry, "coverage": sorted(covered), "status": status, "results": results,
        "label": "PROTOTYPE OUTPUT - synthetic data - advisory and non-authoritative",
    }


def write_trace(trace: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{trace['query_id']}-{stamp}.json"
    path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return path



def prompt_only_context(env, contract_id: str, month: str) -> dict:
    """Evaluation helper (Delivery-side): what a prompt-only approach would place in the model's context for the same
    question. With no retrieval or gates, that is every MSR for the contract and month, whatever its review state."""
    from . import content_filter as cf
    op = env.principal(env.registry.operator_for(env.registry.contracts[contract_id]["delivery_scope_id"]))
    inputs = [m for m in env.delivery_store.list_inputs(op)
              if m.contract_id == contract_id and m.period_start.startswith(month)]
    revoked = sorted({r.source_ref.input_id for r in env.evidence_store.list_records(op) if r.disposition == "revoked"})
    return {"msrs_in_context": len(inputs), "raw_narratives_exposed": len(inputs),
            "not_employee_accepted": [m.input_id for m in inputs if m.review.status != "accepted"],
            "flagged_by_content_filter": [m.input_id for m in inputs if cf.scan_source(m.raw_narrative)],
            "sources_of_revoked_evidence": revoked, "citable_approved_records": 0}
