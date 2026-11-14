"""Solutions domain: Workflow B (retrieve and explain approved evidence).

The orchestrator and gateway are constructed without any reference to the Delivery store. The only
Delivery-owned dependency is SourceStatusService, which returns status metadata, never content.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydantic import ValidationError

from . import PROTOTYPE_LABEL
from . import content_filter as cf
from .agents import AgentRegistry, SolutionsExpertiseAgent
from .clock import Clock, parse_utc
from .delivery import SourceStatusService, StatusUnavailable
from .registry import GATEWAY_SERVICE, Principal, ReferenceRegistry
from .schemas import EvidenceQuery, ExpertiseExplanation, FilteredEvidence
from .stores import AuditLog, EvidenceStore, OpsStore, find_delivery_only_keys

KNOWN_RESTRICTIONS = {"prototype-only", "no-external-use", "no-personnel-ranking"}


class GatewayUnavailable(Exception):
    pass


class RetrievalGateway:
    """Permission-checking retrieval. Rechecks authoritative status on every request; fails closed."""

    def __init__(self, *, evidence: EvidenceStore, source_status: SourceStatusService, registry: ReferenceRegistry,
                 audit: AuditLog, clock: Clock):
        self.evidence, self.source_status, self.registry = evidence, source_status, registry
        self.audit, self.clock = audit, clock
        self._cache: dict[tuple, tuple[int, list[FilteredEvidence]]] = {}
        self.cache_hits = 0

    def _candidates(self, req_task_ids: tuple[str, ...]) -> list[FilteredEvidence]:
        gen = self.evidence.generation(GATEWAY_SERVICE)
        key = req_task_ids
        hit = self._cache.get(key)
        if hit and hit[0] == gen:
            self.cache_hits += 1
            return hit[1]
        recs = [r for r in self.evidence.list_records(GATEWAY_SERVICE)
                if r.req_task_id in req_task_ids and r.disposition == "published"]
        self._cache = {k: v for k, v in self._cache.items() if v[0] == gen}  # drop stale generations
        self._cache[key] = (gen, recs)
        return recs

    def check_record(self, rec: FilteredEvidence, audience_id: str, purpose: str) -> list[str]:
        """Per-record retrieval rules (Build Guide Section 2). Empty list means retrievable."""
        why = []
        if rec.disposition != "published":
            why.append(f"disposition {rec.disposition}")
        pa = rec.pmo_approval
        if pa.status != "approved" or pa.approved_record_version != rec.record_version:
            why.append("approval not current for this version")
        events = self.evidence.approval_events_for(GATEWAY_SERVICE, rec.record_id, rec.record_version)
        approved = [e for e in events if e.decision == "approved" and e.decided_at == pa.approved_at
                    and e.approver_id == pa.approver_id]
        if not approved:
            why.append("no authoritative approval event")
        if any(e.decision in ("rejected", "revoked") for e in events):
            why.append("approval revoked or rejected")
        status = self.source_status.status(rec.source_ref.input_id)  # may raise StatusUnavailable
        if status is None or status["current_version"] != rec.source_ref.input_version \
                or status["source_status"] != "active" or status["review_status"] != "accepted" \
                or status["reviewed_input_version"] != rec.source_ref.input_version:
            why.append("source not current, active, and employee-reviewed")
        elif self.registry.tto_review_required and status.get("tto_review_status") != "approved":
            why.append("source not approved by a TTO lead")
        if rec.source_ref.version_status != "current" or rec.source_ref.source_status != "active":
            why.append("record source reference not current")
        if self.clock.now() >= parse_utc(rec.freshness.expires_at):
            why.append("expired")
        if audience_id not in rec.audience_ids:
            why.append("audience not permitted")
        if purpose not in rec.permitted_purposes:
            why.append("purpose not permitted")
        for r in rec.restrictions:
            if r not in KNOWN_RESTRICTIONS:
                why.append(f"unknown restriction {r} (fail closed)")
        aud = self.registry.audiences.get(audience_id)
        if "no-external-use" in rec.restrictions and (aud is None or aud.get("external", False)):
            why.append("no-external-use restriction")
        if "no-personnel-ranking" in rec.restrictions and purpose == "personnel-ranking":
            why.append("no-personnel-ranking restriction")
        if cf.blocking(rec.exceptions):
            why.append("blocking exceptions present")
        return why

    def eligibility(self, requester: Principal, audience_id: str, purpose: str,
                    correlation_id: str) -> tuple[list[FilteredEvidence], list[tuple[FilteredEvidence, list[str]]]]:
        """For semantic search: every record that has been published, split into retrievable records and excluded
        records with reasons. The same per-record checks as retrieve(); the vector search may only rank the first set."""
        if requester.domain != "solutions" or not self.registry.is_audience_member(requester.user_id, audience_id):
            self.audit.record(GATEWAY_SERVICE, "retrieve", "denied", reason="requester not in audience",
                              correlation_id=correlation_id, refs={"requester": requester.user_id, "mode": "semantic"})
            raise PermissionError("requester not in audience")
        allowed, why = self.registry.purpose_allowed(purpose)
        if not allowed:
            self.audit.record(GATEWAY_SERVICE, "retrieve", "denied", reason=why or "purpose not allowed",
                              correlation_id=correlation_id, refs={"mode": "semantic"})
            raise PermissionError(why or "purpose not allowed")
        ok, excluded = [], []
        latest: dict[str, FilteredEvidence] = {}
        for rec in self.evidence.list_records(GATEWAY_SERVICE):
            if rec.record_id not in latest or rec.record_version > latest[rec.record_id].record_version:
                latest[rec.record_id] = rec
        try:
            for rec in latest.values():
                if rec.published_at is None:
                    continue  # never published: not part of the shared corpus at all
                reasons = self.check_record(rec, audience_id, purpose)
                (excluded.append((rec, reasons)) if reasons else ok.append(rec))
        except StatusUnavailable as e:
            self.audit.record(GATEWAY_SERVICE, "retrieve", "failed-closed", reason=str(e),
                              correlation_id=correlation_id, refs={"mode": "semantic"})
            raise GatewayUnavailable(str(e)) from e
        self.audit.record(GATEWAY_SERVICE, "retrieve", "ok", correlation_id=correlation_id,
                          refs={"mode": "semantic", "eligible": len(ok), "excluded": len(excluded)})
        return ok, excluded

    def retrieve(self, requester: Principal, audience_id: str, purpose: str, req_task_ids: list[str],
                 correlation_id: str) -> tuple[list[dict], list[dict]]:
        if requester.domain != "solutions" or not self.registry.is_audience_member(requester.user_id, audience_id):
            self.audit.record(GATEWAY_SERVICE, "retrieve", "denied", reason="requester not in audience",
                              correlation_id=correlation_id, refs={"requester": requester.user_id})
            return [], [{"reason": "requester not in audience"}]
        allowed, why = self.registry.purpose_allowed(purpose)
        if not allowed:
            self.audit.record(GATEWAY_SERVICE, "retrieve", "denied", reason=why or "purpose not allowed",
                              correlation_id=correlation_id)
            return [], [{"reason": why or "purpose not allowed"}]
        try:
            out, denied = [], []
            for rec in self._candidates(tuple(sorted(req_task_ids))):
                why = self.check_record(rec, audience_id, purpose)
                if why:
                    denied.append({"record_id": rec.record_id, "record_version": rec.record_version, "reasons": why})
                else:
                    out.append(rec.model_dump())
        except StatusUnavailable as e:
            self.audit.record(GATEWAY_SERVICE, "retrieve", "failed-closed", reason=str(e),
                              correlation_id=correlation_id)
            raise GatewayUnavailable(str(e)) from e
        leaked = find_delivery_only_keys(out)
        if leaked:  # defense in depth; the schema already forbids these fields
            raise GatewayUnavailable(f"boundary violation blocked: {leaked}")
        self.audit.record(GATEWAY_SERVICE, "retrieve", "ok", correlation_id=correlation_id,
                          refs={"returned": [f"{r['record_id']}v{r['record_version']}" for r in out],
                                "denied": [f"{d['record_id']}v{d['record_version']}" for d in denied]})
        return out, denied


class SolutionsOrchestrator:
    agent_id = "AGT-SOLUTIONS-ORCHESTRATOR"

    def __init__(self, *, gateway: RetrievalGateway, expertise: SolutionsExpertiseAgent, registry: ReferenceRegistry,
                 agents: AgentRegistry, ops: OpsStore, audit: AuditLog, clock: Clock, cfg: dict):
        agents.require_authorized(self.agent_id)
        agents.require_authorized(expertise.agent_id)
        self.gateway, self.expertise, self.registry = gateway, expertise, registry
        self.ops, self.audit, self.clock, self.cfg = ops, audit, clock, cfg
        self.principal = Principal(self.agent_id, "agent", "solutions", frozenset({"agent"}))

    # ------------------------------------------------------------------ Workflow B

    def handle(self, p: Principal, query: dict) -> dict:
        qid = query.get("query_id") or f"SYN-QUERY-{uuid.uuid4().hex[:8].upper()}"
        cid = f"SYN-WF-B-{qid}"
        try:
            q = EvidenceQuery.model_validate(query | {"query_id": qid})
        except ValidationError as e:
            return self._stop(p, qid, cid, "denied", f"invalid query: {e.error_count()} error(s)")
        if p.kind != "human" or p.domain != "solutions" or not p.has_role("solutions_requester") \
                or p.user_id != q.requester_id:
            return self._stop(p, qid, cid, "denied", "requester is not the authenticated Solutions user")
        if q.include_raw_sources:
            return self._stop(p, qid, cid, "denied",
                              "raw MSR access is prohibited outside Delivery; use the Delivery restricted review path")
        if not self.registry.is_audience_member(p.user_id, q.audience_id):
            return self._stop(p, qid, cid, "denied", f"requester is not a member of audience {q.audience_id}")
        allowed, why = self.registry.purpose_allowed(q.purpose)
        if not allowed:
            return self._stop(p, qid, cid, "denied", why or f"purpose {q.purpose} is not permitted")
        unknown = [t for t in q.req_task_ids if t not in self.registry.req_tasks]
        if unknown:
            return self._stop(p, qid, cid, "denied", f"unknown REQ tasks {unknown}")

        try:
            records, denied = self.gateway.retrieve(p, q.audience_id, q.purpose, q.req_task_ids, cid)
        except GatewayUnavailable as e:
            esc = self._escalate(cid, "retrieval-failure", self._route("solutions_reviewer"), "solutions",
                                 f"Retrieval failed closed: {e}", {"query_id": qid})
            return self._stop(p, qid, cid, "failed_safe", f"retrieval failed closed: {e}", escalations=[esc])

        requested = [{"req_task_id": t, "title": self.registry.req_tasks[t]["title"]} for t in q.req_task_ids]
        # Contract-wide analytic records (similar work / collaboration opportunities) are tracked separately: they
        # are reported alongside the answer but never count as coverage of the requested REQ tasks.
        related = [r for r in records if r.get("alignment_scope", "own-req") != "own-req"]
        records = [r for r in records if r.get("alignment_scope", "own-req") == "own-req"]
        rec_ids = {r["record_id"] for r in records}

        def validate(out: ExpertiseExplanation) -> list[str]:
            probs = []
            got = [c.req_task_id for c in out.coverage]
            if sorted(got) != sorted(q.req_task_ids):
                probs.append(f"coverage must list each requested task exactly once (got {got})")
            for c in out.coverage:
                bad = [x for x in c.cited_record_ids if x not in rec_ids]
                if bad:
                    probs.append(f"cites records that were not retrieved: {bad}")
                if c.status in ("supported", "partial", "conflict") and not c.cited_record_ids:
                    probs.append(f"{c.req_task_id} is {c.status} without a citation")
            mentioned = set(re.findall(r"\b[A-Z]+-EVID-\d+\b", out.explanation))
            if mentioned - rec_ids:
                probs.append(f"explanation mentions unretrieved records {sorted(mentioned - rec_ids)}")
            text = " ".join([out.explanation, out.uncertainty] + [c.note for c in out.coverage])
            probs += [f"prohibited content: {x}" for x in cf.check_explanation(text)]
            return probs

        result, errors = self.expertise.explain(records, requested, q.question, validate)
        if result is None:
            esc = self._escalate(cid, "agent-failure", self._route("solutions_reviewer"), "solutions",
                                 "Expertise Agent produced no valid output; terminated safely.", {"query_id": qid})
            return self._stop(p, qid, cid, "failed_safe", "expertise agent failed validation or errored",
                              escalations=[esc], errors=errors)
        out: ExpertiseExplanation = result.parsed
        # Deterministic conflict rule: when a task's approved records disagree about completion, the status is
        # conflict whatever the model said. Conflicts are a safety signal and must not depend on model judgment.
        for c in out.coverage:
            recs = [r for r in records if r["req_task_id"] == c.req_task_id]
            done = [r for r in recs if "not completed" not in r["evidence_summary"].lower()]
            not_done = [r for r in recs if r not in done]
            if done and not_done and c.status != "conflict":
                c.status = "conflict"
                c.cited_record_ids = sorted({*c.cited_record_ids, *(r["record_id"] for r in recs)})
                c.note = (c.note + " Set to conflict by the deterministic rule: approved records disagree about "
                          "completion.").strip()

        escalations = []
        for c in out.coverage:
            if c.status == "conflict":
                escalations.append(self._escalate(
                    cid, "evidence-conflict", self._route("solutions_reviewer"), "solutions",
                    f"Approved records conflict for {c.req_task_id}; human resolution required.",
                    {"query_id": qid, "req_task_id": c.req_task_id, "record_ids": c.cited_record_ids}))
            elif c.status in ("unsupported", "partial"):
                escalations.append(self._escalate(
                    cid, "insufficient-filtered-evidence",
                    self.registry.delivery_owner_for(c.req_task_id) or self._route("delivery_review_owner"), "delivery",
                    f"Filtered evidence is insufficient for {c.req_task_id}. Route to the Delivery-only restricted "
                    "review path; only a human-approved result may be exported.",
                    {"query_id": qid, "req_task_id": c.req_task_id}))
        needs_review = out.needs_human_review or bool(escalations)
        status = "escalated" if needs_review else "awaiting_acknowledgement"
        by_id = {r["record_id"]: r for r in records}
        citations = [{k: by_id[i][k] for k in ("record_id", "record_version", "owning_pmo_id", "req_task_id",
                                               "evidence_summary", "published_at")}
                     | {"approved_at": by_id[i]["pmo_approval"]["approved_at"],
                        "expires_at": by_id[i]["freshness"]["expires_at"]}
                     for i in sorted({x for c in out.coverage for x in c.cited_record_ids})]
        body = {
            "label": PROTOTYPE_LABEL, "query_id": qid, "correlation_id": cid, "status": status,
            "requester_id": p.user_id, "audience_id": q.audience_id, "purpose": q.purpose,
            "coverage": [c.model_dump() for c in out.coverage], "explanation": out.explanation,
            "uncertainty": out.uncertainty, "citations": citations,
            "related_work": [{k: r[k] for k in ("record_id", "record_version", "owning_pmo_id", "req_task_id",
                                                 "tto_id", "alignment_scope", "evidence_summary", "published_at")}
                             | {"approved_at": r["pmo_approval"]["approved_at"]} for r in related],
            "records_withheld": len(denied), "escalations": escalations, "attempt_errors": errors,
            "model": result.meta() | {"prompt_version": self.expertise.prompt_version},
        }
        self.ops.put_response(qid, cid, p.user_id, status, body)
        self.audit.record(self.principal, "workflow_b", status, correlation_id=cid,
                          refs={"query_id": qid, "cited": [f"{c['record_id']}v{c['record_version']}" for c in citations],
                                "escalations": escalations, "requester_id": p.user_id}, meta=body["model"])
        return body

    # ------------------------------------------------------------------ expertise search (semantic retrieval)

    _trace_dir = None
    _semantic = None

    def expertise_search(self, p: Principal, question: str, audience_id: str,
                         purpose: str = "requirement-evidence-explanation", query_id: str | None = None) -> dict:
        """Find the controlling requirement and approved evidence most related to a free-text question, through the
        gateway. Same authority rules as handle(); the vector search only ranks records the gateway already allows."""
        from . import semantic
        qid = query_id or f"SYN-SEARCH-{uuid.uuid4().hex[:8].upper()}"
        cid = f"SYN-WF-B-{qid}"
        if p.kind != "human" or p.domain != "solutions" or not p.has_role("solutions_requester"):
            return self._stop(p, qid, cid, "denied", "requester is not an authenticated Solutions user")
        if not (question or "").strip():
            return self._stop(p, qid, cid, "denied", "a question is required")
        allowed, why = self.registry.purpose_allowed(purpose)
        if not allowed:
            return self._stop(p, qid, cid, "denied", why or f"purpose {purpose} is not permitted")
        try:
            if self._semantic is None:
                self._semantic = semantic.SemanticIndex(self.registry)
            trace = semantic.search(self._semantic, self.gateway, self.registry, self.clock, p, question.strip(),
                                    audience_id, purpose, qid, cid)
        except semantic.SemanticUnavailable as e:
            return self._stop(p, qid, cid, "failed_safe", f"semantic retrieval unavailable: {e}")
        except PermissionError as e:
            return self._stop(p, qid, cid, "denied", str(e))
        except GatewayUnavailable as e:
            esc = self._escalate(cid, "retrieval-failure", self._route("solutions_reviewer"), "solutions",
                                 f"Retrieval failed closed: {e}", {"query_id": qid})
            return self._stop(p, qid, cid, "failed_safe", f"retrieval failed closed: {e}", escalations=[esc])
        escalations = []
        if trace["status"] == "incomplete":
            task = next((r["id"] for r in trace["results"] if r["kind"] == "requirement"), None)
            escalations.append(self._escalate(
                cid, "insufficient-filtered-evidence",
                (self.registry.delivery_owner_for(task) if task else None) or self._route("delivery_review_owner"),
                "delivery", f"No eligible approved evidence for {task or 'the question'} after one targeted retry. "
                            "Route to the Delivery-only restricted review path.", {"query_id": qid, "req_task_id": task}))
        trace["escalations"] = escalations
        if self._trace_dir is not None:
            trace["trace_file"] = str(semantic.write_trace(trace, Path(self._trace_dir)))
        body = {"label": PROTOTYPE_LABEL, "query_id": qid, "correlation_id": cid, "status": trace["status"],
                "requester_id": p.user_id, "audience_id": audience_id, "purpose": purpose, "mode": "semantic",
                "trace": trace, "escalations": escalations, "coverage": [], "citations": []}
        self.ops.put_response(qid, cid, p.user_id, trace["status"], body)
        self.audit.record(self.principal, "semantic_search", trace["status"], correlation_id=cid,
                          refs={"query_id": qid, "selected": [r["id"] for r in trace["results"]],
                                "excluded": [b["id"] for b in trace["excluded_from_ranking"]],
                                "retry": bool(trace["retry"]), "escalations": escalations})
        return body

    def acknowledge(self, p: Principal, query_id: str) -> dict:
        """Responsible reviewer acknowledgement. Complete only if still current, traceable, and permissioned."""
        resp = self.ops.get_response(query_id)
        if resp is None:
            return {"ok": False, "reason": "unknown query"}
        cid = resp["correlation_id"]
        if p.kind != "human" or not p.has_role("solutions_reviewer"):
            self.audit.record(p, "acknowledge", "denied", reason="not a Solutions reviewer", correlation_id=cid)
            return {"ok": False, "reason": "only the responsible Solutions reviewer may acknowledge"}
        open_esc = [e for e in self.ops.escalations(status="open") if e["correlation_id"] == cid]
        if open_esc:
            self.audit.record(p, "acknowledge", "denied", reason="open escalations", correlation_id=cid)
            return {"ok": False, "reason": f"{len(open_esc)} escalation(s) still open"}
        body = resp["body"]
        stale = []
        for c in body["citations"] + body.get("related_work", []):
            rec = self.gateway.evidence.get_record(GATEWAY_SERVICE, c["record_id"], c["record_version"])
            try:
                why = self.gateway.check_record(rec, body["audience_id"], body["purpose"]) if rec else ["missing"]
            except StatusUnavailable:
                why = ["status unavailable (fail closed)"]
            if why:
                stale.append({"record_id": c["record_id"], "reasons": why})
        if stale:
            self.ops.acknowledge(query_id, p.user_id, "stale")
            self.audit.record(p, "acknowledge", "stale", correlation_id=cid, refs={"stale": stale})
            return {"ok": False, "reason": "cited evidence is no longer current", "stale": stale}
        self.ops.acknowledge(query_id, p.user_id, "complete")
        self.audit.record(p, "acknowledge", "complete", correlation_id=cid)
        return {"ok": True, "reason": "complete"}

    def resolve_escalation(self, p: Principal, escalation_id: str, resolution: str) -> dict:
        esc = next((e for e in self.ops.escalations() if e["escalation_id"] == escalation_id), None)
        if esc is None:
            return {"ok": False, "reason": "unknown escalation"}
        if p.kind != "human" or p.user_id != esc["route_to"]:
            self.audit.record(p, "resolve_escalation", "denied", reason="not the named reviewer",
                              correlation_id=esc["correlation_id"])
            return {"ok": False, "reason": f"only {esc['route_to']} may resolve this escalation"}
        self.ops.resolve_escalation(escalation_id, p.user_id, resolution)
        self.audit.record(p, "resolve_escalation", "resolved", correlation_id=esc["correlation_id"],
                          refs={"escalation_id": escalation_id})
        return {"ok": True, "reason": "resolved"}

    # ------------------------------------------------------------------ helpers

    def _route(self, key: str) -> str:
        return self.registry.routing.get(key) or self.cfg["routing"][key]

    def _escalate(self, cid: str, kind: str, route_to: str, domain: str, summary: str, refs: dict) -> str:
        eid = f"ESC-{uuid.uuid4().hex[:8].upper()}"
        self.ops.add_escalation(eid, cid, kind, route_to, domain, summary, refs)
        self.audit.record(self.principal, "escalate", kind, correlation_id=cid,
                          refs={"escalation_id": eid, "route_to": route_to})
        return eid

    def _stop(self, p: Principal, qid: str, cid: str, status: str, reason: str, **extra) -> dict:
        body = {"label": PROTOTYPE_LABEL, "query_id": qid, "correlation_id": cid, "status": status,
                "reason": reason, "coverage": [], "citations": [], **extra}
        self.ops.put_response(qid, cid, p.user_id, status, body)
        self.audit.record(p, "workflow_b", status, reason=reason, correlation_id=cid, refs={"query_id": qid})
        return body

