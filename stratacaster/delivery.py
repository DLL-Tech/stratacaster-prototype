"""Delivery domain: Workflow A (publish filtered evidence) and the source-status metadata service.

Every public method takes the acting Principal. Human gates (employee review, PMO approval) are
explicit recorded actions by authenticated humans; model output never changes approval state.
"""
from __future__ import annotations

import calendar
import dataclasses
import re
import uuid
from collections import Counter
from datetime import date, timedelta

from pydantic import ValidationError

from . import content_filter as cf
from .agents import RoleExecutionAgent
from .clock import Clock, format_utc, parse_utc
from .common import Outcome, allow, deny
from .registry import ROLE_EXECUTION_SERVICE, SOURCE_STATUS_SERVICE, Principal, ReferenceRegistry
from .schemas import AlignmentResult, FilteredEvidence, MsrInput, PmoApprovalEvent
from .stores import AuditLog, DeliveryStore, EvidenceStore, OpsStore, PermissionDenied


SOD = ("separation-of-duties: management authority does not apply to the user's own MSR "
       "(governance.management_authority_covers_own_msr is false)")
POTENTIAL_NEW_TASK = "potential-new-task-requirement"
# Reported status comes from the agent's structured reported_status (DEF-007), not from the wording of its summary,
# and is written into the record as a fixed marker so Solutions can still see conflicts.
NOT_DONE_MARKER = "not completed"
# Employee feedback before submission (PR-30): the responses allowed for each statement tag.
FEEDBACK_RESPONSES = {"aligned": ("accurate", "not_intended"), POTENTIAL_NEW_TASK: ("confirm_new", "not_new")}
PREVIEW_TTL_MINUTES = 60
MAX_PREVIEWS = 100


def _with_marker(summary: str, not_done: bool) -> str:
    """Model-written summaries (Build Guide path) carry the fixed marker when the work was reported not completed."""
    if not not_done or NOT_DONE_MARKER in summary.lower():
        return summary
    return summary.rstrip(" .")[:270] + " (reported as not completed)."


class StatusUnavailable(Exception):
    pass


class SourceStatusService:
    """Delivery-owned metadata endpoint. Exposes status only, never content or locators."""

    def __init__(self, store: DeliveryStore):
        self.store = store
        self.available = True

    def status(self, input_id: str) -> dict | None:
        if not self.available:
            raise StatusUnavailable("source status service unavailable")
        inp = self.store.get_input(SOURCE_STATUS_SERVICE, input_id)
        if inp is None:
            return None
        tto = self.store._tto_latest(inp.input_id, inp.input_version)
        return {"input_id": inp.input_id, "current_version": inp.input_version,
                "source_status": inp.source_status, "review_status": inp.review.status,
                "reviewed_input_version": inp.review.reviewed_input_version,
                "tto_review_status": tto["decision"] if tto else "pending"}


def split_statements(narrative: str) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", narrative.strip()) if s.strip()]
    return parts or [narrative.strip()]


def _copies(text: str, source: str, n: int = 5) -> bool:
    """True if text repeats a run of n words (or all of a shorter text of 3+ words) from source."""
    w = re.findall(r"[a-z0-9]+", text.lower())
    s = " " + " ".join(re.findall(r"[a-z0-9]+", source.lower())) + " "
    k = min(n, len(w))
    return k >= 3 and any(f" {' '.join(w[i:i + k])} " in s for i in range(len(w) - k + 1))


class DeliveryService:
    def __init__(self, *, registry: ReferenceRegistry, store: DeliveryStore, evidence: EvidenceStore,
                 ops: OpsStore, audit: AuditLog, clock: Clock, cfg: dict, role_agent: RoleExecutionAgent,
                 feedback_mode: str = "model", local_aligner=None, suggest_agent: RoleExecutionAgent | None = None):
        self.registry, self.store, self.evidence, self.ops = registry, store, evidence, ops
        self.audit, self.clock, self.cfg, self.role_agent = audit, clock, cfg, role_agent
        # Employee feedback check (PR-30): "local" (free), "hybrid" (free alignment + small-model task wording), or
        # "model" (full Role Execution Agent check, reused at submission).
        self.feedback_mode, self.local_aligner, self.suggest_agent = feedback_mode, local_aligner, suggest_agent
        if feedback_mode not in ("local", "hybrid", "model") or (feedback_mode != "model" and local_aligner is None):
            raise ValueError(f"unsupported feedback check mode {feedback_mode!r}")
        self._previews: dict[str, dict] = {}  # employee feedback checks: in memory only, never stored

    # ================================================================== inputs and employee review

    def load_input(self, p: Principal, raw: dict, correlation_id: str = "") -> Outcome:
        if not p.has_role("delivery_operator"):
            return self._deny(p, "load_input", "only a Delivery operator may load inputs", correlation_id)
        try:
            msr = MsrInput.model_validate(raw)
        except ValidationError as e:
            errs = [f"{'.'.join(map(str, x['loc'])) or '<root>'}: {x['msg']}" for x in e.errors()]
            return self._deny(p, "load_input", "schema-invalid", correlation_id,
                              refs={"input_id": raw.get("input_id")}, errors=errs)
        refs = {"input_id": msr.input_id, "input_version": msr.input_version}
        if not p.in_scope(msr.delivery_scope_id):
            return self._deny(p, "load_input", f"operator scope does not include {msr.delivery_scope_id}",
                              correlation_id, refs=refs)
        ref_errs = self.registry.input_reference_errors(msr.role_id, msr.tto_id, msr.contract_id,
                                                        msr.delivery_scope_id, msr.subject_id)
        if ref_errs:
            return self._deny(p, "load_input", "reference-invalid", correlation_id, refs=refs, errors=ref_errs)
        if msr.review.status != "pending":
            return self._deny(p, "load_input", "review must be recorded through the employee-review action",
                              correlation_id, refs=refs)
        existing = self.store.get_input(p, msr.input_id, msr.input_version)
        if existing is not None:
            if existing.model_dump() == msr.model_dump():
                self.audit.record(p, "load_input", "duplicate-ignored", correlation_id=correlation_id, refs=refs)
                return allow("duplicate-identical-ignored", **refs)
            return self._deny(p, "load_input", "duplicate-conflict: identifier and version exist with different "
                              "content", correlation_id, refs=refs)
        if self.store.get_input(p, msr.input_id) is not None:
            return self._deny(p, "load_input", "input exists; use correct_input to create a new version",
                              correlation_id, refs=refs)
        self.store.insert_input(p, msr)
        self.audit.record(p, "load_input", "accepted", correlation_id=correlation_id, refs=refs)
        return allow("loaded", **refs)

    def employee_review(self, p: Principal, input_id: str, input_version: int, decision: str,
                        notes: str | None = None, correlation_id: str = "", *, feedback: dict | None = None) -> Outcome:
        refs = {"input_id": input_id, "input_version": input_version, "decision": decision}
        if decision not in ("accepted", "rejected", "correction_requested"):
            return self._deny(p, "employee_review", f"invalid decision {decision}", correlation_id, refs=refs)
        if p.kind != "human" or not p.has_role("employee_reviewer"):
            return self._deny(p, "employee_review", "employee review requires an authenticated human reviewer",
                              correlation_id, refs=refs)
        msr, hidden = self._visible_input(p, input_id)
        if hidden:
            return self._deny(p, "employee_review", "reviewer is not assigned to this subject", correlation_id,
                              refs=refs)
        if msr is None:
            return self._deny(p, "employee_review", "unknown input", correlation_id, refs=refs)
        if msr.input_version != input_version:
            return self._deny(p, "employee_review", f"review must target current version {msr.input_version}",
                              correlation_id, refs=refs)
        if self.registry.employee_reviewer_for(msr.subject_id) != p.user_id:
            return self._deny(p, "employee_review", "reviewer is not assigned to this subject", correlation_id,
                              refs=refs)
        if msr.review.status != "pending":
            return self._deny(p, "employee_review", f"review already recorded ({msr.review.status}); a change "
                              "requires a new input version", correlation_id, refs=refs)
        now = self.clock.now_str()
        review = {"status": decision, "reviewer_id": p.user_id, "reviewed_input_version": input_version,
                  "reviewed_at": now}
        if notes:
            review["notes"] = notes
        updated = MsrInput.model_validate(msr.model_dump() | {"review": review})
        self.store.update_input_metadata(p, updated)
        self.store.append_review_event(p, {"input_id": input_id, "input_version": input_version,
                                           "decision": decision, "reviewer_id": p.user_id, "reviewed_at": now})
        self.audit.record(p, "employee_review", "recorded", correlation_id=correlation_id, refs=refs)
        extra = {}
        if decision == "accepted" and self.registry.assess_on_submission:
            extra["assessment"] = self.assess_input(updated, correlation_id, feedback=feedback).data
        return allow("review-recorded", **refs, **extra)

    # ================================================================== employee submission and TTO lead review

    def _submission_basics(self, p: Principal, month: str, bullets: list[str]) -> tuple[str | None, dict, list[str]]:
        """Checks shared by the feedback check and the submission: (reason to refuse or None, profile, lines)."""
        if p.kind != "human" or not p.has_role("employee_reviewer"):
            return "only an employee, acting as an employee, may submit an MSR", {}, []
        prof = self.registry.profile(p.user_id)
        if prof is None:
            return "no roster assignment for this employee", {}, []
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
            return "reporting month must be YYYY-MM", prof, []
        lines = [b.strip().lstrip("•").strip() for b in bullets if b and b.strip()]
        if not lines:
            return "at least one narrative bullet is required", prof, []
        return None, prof, lines

    def preview_msr(self, p: Principal, month: str, bullets: list[str]) -> Outcome:
        """Employee feedback before submission (PR-30). The Role Execution Agent checks the draft against the
        employee's REQ: each statement is shown as aligned to a REQ task, or as a potential new task with a
        recommended task. Nothing is stored, drafted, escalated, or audited; the result is held in memory so an
        unchanged draft is not assessed twice when the employee submits it."""
        why, prof, lines = self._submission_basics(p, month, bullets)
        if why:
            return deny(why)
        if not self.registry.assess_on_submission:
            return deny("the feedback check needs the roster workflow (REQ assessment on submission)")
        req = self.registry.role_req_for(prof["role_id"], prof["tto_id"])
        catalog = [self.registry.req_tasks[t] for t in req["req_task_ids"]] if req else []
        narrative = " ".join(lines)
        statements = [(f"DRAFT-{i}", s) for i, s in enumerate(split_statements(narrative), 1)]
        mode, meta = self.feedback_mode, {"engine": self.feedback_mode, "cost_usd": 0.0}
        if mode == "model":
            result, errors = self.role_agent.align(statements, catalog, suggest=True)
            if result is None:
                return deny("feedback unavailable: the Role Execution Agent returned no usable result; check again "
                            "or submit without feedback", errors=errors)
            parsed, meta = result.parsed, meta | result.meta()
        else:
            result, parsed = None, self.local_aligner.align(statements, catalog)
            matched = {c.statement_id for c in parsed.candidates}
            unmatched = [s for s in statements if s[0] not in matched]
            if mode == "hybrid" and unmatched:
                wording, errors = self.suggest_agent.align(unmatched, catalog, suggest=True)
                if wording is not None:  # a failure only means no recommended wording in the check
                    parsed = parsed.model_copy(update={"new_task_suggestions": [
                        s for s in wording.parsed.new_task_suggestions if s.statement_id not in matched]})
                    meta |= wording.meta() | {"engine": "hybrid"}
        rows = self._feedback_rows(statements, catalog, parsed)
        if mode != "model" and self.local_aligner.semantic:  # where unmatched work already exists on the contract
            others = self.registry.contract_req_tasks(prof["contract_id"],
                                                      exclude={t["req_task_id"] for t in catalog})
            for r in rows:
                if r["tag"] == POTENTIAL_NEW_TASK:
                    r["similar_elsewhere"] = [
                        {"req_task_id": t, "title": self.registry.req_tasks[t]["title"],
                         "tto_id": self.registry.req_tasks[t]["tto_id"], "similarity": x}
                        for t, x in self.local_aligner.nearest(r["text"], others)]
        pid = f"PRV-{uuid.uuid4().hex[:10].upper()}"
        while len(self._previews) >= MAX_PREVIEWS:
            self._previews.pop(next(iter(self._previews)))
        ttl = self.cfg.get("feedback_check", {}).get("ttl_minutes", PREVIEW_TTL_MINUTES)
        self._previews[pid] = {"user_id": p.user_id, "month": month, "narrative": narrative, "result": result,
                               "engine": mode, "statements": rows, "catalog_ids": {t["req_task_id"] for t in catalog},
                               "expires": self.clock.now() + timedelta(minutes=ttl)}
        return allow("feedback-ready", preview_id=pid, role_req_id=req["role_req_id"] if req else None,
                     statements=rows, engine=mode, preliminary=mode != "model", model=meta)

    def _feedback_rows(self, statements: list[tuple[str, str]], catalog: list[dict], out: AlignmentResult) -> list:
        titles = {t["req_task_id"]: t["title"] for t in catalog}
        aligned: dict[str, list[dict]] = {}
        for c in out.candidates:
            if c.req_task_id in titles and all(x["req_task_id"] != c.req_task_id for x in aligned.get(c.statement_id,
                                                                                                         [])):
                aligned.setdefault(c.statement_id, []).append({"req_task_id": c.req_task_id,
                                                               "title": titles[c.req_task_id],
                                                               "reported_status": c.reported_status})
        suggestions = {s.statement_id: s for s in out.new_task_suggestions}
        rows = []
        for i, (sid, text) in enumerate(statements, 1):
            row = {"index": i, "text": text, "tag": "aligned" if aligned.get(sid) else POTENTIAL_NEW_TASK,
                   "aligned": aligned.get(sid, []), "suggested_task": None}
            s = suggestions.get(sid)
            if row["tag"] == POTENTIAL_NEW_TASK and s and self._usable_suggestion(s.title, s.description, text):
                row["suggested_task"] = {"title": s.title.strip()[:120], "description": s.description.strip()[:300],
                                         "rationale": s.rationale.strip()[:300]}
            rows.append(row)
        return rows

    @staticmethod
    def _usable_suggestion(title: str, description: str, statement: str) -> bool:
        """Deterministic checks on model-written task wording: present, no personnel or instruction content, and
        not a copy of the employee's statement."""
        if not title.strip() or not description.strip():
            return False
        if any(cf._PERSONNEL.search(x) or cf._INJECTION.search(x) for x in (title, description)):
            return False
        return not (_copies(title, statement) or _copies(description, statement))

    def _take_preview(self, p: Principal, preview_id: str | None, month: str, narrative: str) -> tuple[dict | None, str]:
        pv = self._previews.get(preview_id or "")
        if pv is None:
            return None, "unknown or expired feedback check; check the draft again"
        if pv["user_id"] != p.user_id:
            return None, "the feedback check belongs to another employee"
        if self.clock.now() > pv["expires"]:
            self._previews.pop(preview_id, None)
            return None, "the feedback check has expired; check the draft again"
        if pv["month"] != month or pv["narrative"] != narrative:
            return None, "the draft changed after the feedback check; check it again before submitting"
        return pv, ""

    @staticmethod
    def _feedback_responses(pv: dict, responses: dict | None) -> tuple[str | None, list[dict]]:
        """The employee's response to each statement in the feedback check, validated against its tag."""
        given = {int(k): v for k, v in (responses or {}).items()}
        out = []
        for st in pv["statements"]:
            r = given.get(st["index"]) or {}
            allowed = FEEDBACK_RESPONSES[st["tag"]]
            if r.get("response") not in allowed:
                return f"statement {st['index']}: response must be one of {', '.join(allowed)}", []
            intended = r.get("intended_req_task_id") or None
            if intended and intended not in pv["catalog_ids"]:
                return f"statement {st['index']}: {intended} is not a task in your REQ", []
            out.append({"index": st["index"], "tag": st["tag"], "response": r["response"],
                        "intended_req_task_id": intended, "notes": (r.get("note") or "").strip()[:300] or None,
                        "suggested_task": st["suggested_task"]})
        return None, out

    def submit_msr(self, p: Principal, month: str, bullets: list[str], correlation_id: str = "",
                   preview_id: str | None = None, responses: dict | None = None) -> Outcome:
        """An employee submits their own MSR for a reporting month. It is labeled like the dataset's MSRs:
        <prefix>-MSR-<YYYYMM>-<employee number>, with contract, TTO, role, and scope from the roster. A second
        submission for the same month becomes the next version and supersedes the earlier one. With a feedback check
        (preview_id) the employee's response to each statement is required and goes to the TTO lead with the REQ
        assessment, which reuses the checked result because the draft is unchanged."""
        why, prof, lines = self._submission_basics(p, month, bullets)
        if why:
            return self._deny(p, "submit_msr", why, correlation_id)
        pv, feedback = None, None
        if preview_id is not None or responses:
            if not self.registry.assess_on_submission:
                return self._deny(p, "submit_msr", "feedback responses need the roster workflow", correlation_id)
            pv, why = self._take_preview(p, preview_id, month, " ".join(lines))
            if pv is None:
                return self._deny(p, "submit_msr", why, correlation_id)
            why, feedback = self._feedback_responses(pv, responses)
            if why:
                return self._deny(p, "submit_msr", why, correlation_id)
        year, mon = int(month[:4]), int(month[5:])
        start = date(year, mon, 1)
        end = date(year, mon, calendar.monthrange(year, mon)[1])
        today = self.clock.now().date()
        if start <= today < end:
            end = today  # reporting through the current day, like the dataset's October reports
        pre = self.registry.id_prefix
        input_id = f"{pre}-MSR-{year:04d}{mon:02d}-{p.user_id.rsplit('-', 1)[-1]}"
        existing = self.store._latest(input_id)
        version = existing.input_version + 1 if existing else 1
        try:
            msr = MsrInput.model_validate({
                "schema_version": "prototype-msr-input/1.0", "input_id": input_id, "input_version": version,
                "data_mode": "synthetic", "delivery_scope_id": prof["delivery_scope_id"], "subject_id": p.user_id,
                "period_start": start.isoformat(), "period_end": end.isoformat(), "role_id": prof["role_id"],
                "tto_id": prof["tto_id"], "contract_id": prof["contract_id"], "source_status": "active",
                "source_created_at": self.clock.now_str(), "raw_narrative": " ".join(lines),
                "source_locator": f"delivery-local:{input_id}:v{version}",
                "review": {"status": "pending", "reviewer_id": None, "reviewed_input_version": None,
                           "reviewed_at": None},
                "classification": "synthetic-demo", "restrictions": ["delivery-only", "no-external-use"]})
        except ValidationError as e:
            return self._deny(p, "submit_msr", f"schema-invalid: {e.error_count()} error(s)", correlation_id)
        ref_errs = self.registry.input_reference_errors(msr.role_id, msr.tto_id, msr.contract_id,
                                                        msr.delivery_scope_id, msr.subject_id)
        if ref_errs:
            return self._deny(p, "submit_msr", "reference-invalid", correlation_id, errors=ref_errs)
        invalidated = 0
        if existing:
            self.store.update_input_metadata(p, MsrInput.model_validate(existing.model_dump()
                                                                         | {"source_status": "superseded"}))
            self.store.insert_input(p, msr)
            invalidated = self._invalidate_dependents(p, input_id, "superseded", "employee resubmitted",
                                                      correlation_id)
        else:
            self.store.insert_input(p, msr)
        refs = {"input_id": input_id, "input_version": version}
        counts = dict(Counter(f["response"] for f in feedback)) if feedback else None
        self.audit.record(p, "submit_msr", "submitted", correlation_id=correlation_id,
                          refs=refs | {"invalidated_records": invalidated}
                          | ({"feedback_check": preview_id, "employee_feedback": counts} if feedback else {}))
        if not self.registry.assess_on_submission:
            return allow("submitted", **refs, invalidated_records=invalidated)
        # Roster workflow: submitting is the employee's own attestation, so the MSR goes straight to assessment
        # against the employee's REQ and then to the TTO lead.
        out = self.employee_review(p, input_id, version, "accepted", "submitted by the employee", correlation_id,
                                   feedback={"preview_id": preview_id, "check": pv, "responses": feedback}
                                   if feedback else None)
        self._previews.pop(preview_id or "", None)
        return allow("submitted", **refs, invalidated_records=invalidated, assessment=out.data.get("assessment"))

    # ================================================================== REQ assessment (Role Execution Agent)

    def assess_input(self, msr: MsrInput, correlation_id: str = "", feedback: dict | None = None) -> Outcome:
        """The Role Execution Agent assesses a submitted MSR against the employee's work-role REQ. Each statement
        is tagged with the REQ task(s) it aligns to, or as a potential new task requirement. One draft task-alignment
        record is created per aligned task; all of them go forward for TTO lead and PMO review. With the employee's
        feedback check, its result is reused and the employee's responses are recorded with the assessment."""
        return self._align(ROLE_EXECUTION_SERVICE, msr, correlation_id, action="assess_msr", feedback=feedback)

    def assessment(self, p: Principal, input_id: str, version: int | None = None) -> dict | None:
        """Latest completed REQ assessment for an input version (Delivery-only; contains statement IDs and tags)."""
        msr = self.store.get_input(p, input_id, version)
        if msr is None:
            return None
        runs = [r for r in self.store.alignment_runs(p, input_id)
                if r.get("input_version") == msr.input_version and r.get("status") == "completed"]
        return runs[-1].get("assessment") if runs else None

    def tto_status(self, input_id: str, version: int) -> str:
        ev = self.store._tto_latest(input_id, version)
        return ev["decision"] if ev else "pending"

    def tto_review(self, p: Principal, input_id: str, input_version: int, decision: str, reason: str = "",
                   correlation_id: str = "") -> Outcome:
        """TTO-stage review after the employee's submission and REQ assessment. Only the lead of the employee's own
        TTO may decide; a lead assigned to that same TTO may decide on their own MSR. Contract PMO staff have no TTO:
        a member of that contract's PMO team other than the author decides instead (PMO peer review)."""
        refs = {"input_id": input_id, "input_version": input_version, "decision": decision}
        if decision not in ("approved", "returned", "rejected"):
            return self._deny(p, "tto_review", f"invalid decision {decision}", correlation_id, refs=refs)
        if p.kind != "human" or not (p.has_role("tto_lead") or p.has_role("human_pmo_reviewer")):
            return self._deny(p, "tto_review", "TTO-stage review requires a human TTO lead, or a PMO team member for "
                              "contract PMO staff, acting in that role", correlation_id, refs=refs)
        msr, hidden = self._visible_input(p, input_id)
        if msr is not None and self.registry.is_pmo_staff_tto(msr.tto_id):
            pmo = self.registry.pmo_for_contract(msr.contract_id)
            if not p.has_role("human_pmo_reviewer") or pmo not in p.pmo_authority:
                return self._deny(p, "tto_review", f"{msr.tto_id} staff have no TTO lead; a member of the {pmo} team "
                                  "other than the author reviews their MSRs", correlation_id, refs=refs)
            if msr.subject_id == p.user_id:
                return self._deny(p, "tto_review", "PMO peer review: a PMO team member may not review their own MSR",
                                  correlation_id, refs=refs)
            basis = "pmo-peer"
        elif hidden or msr is None or not p.has_role("tto_lead") or msr.tto_id not in p.tto_authority:
            if msr is None and not hidden:
                return self._deny(p, "tto_review", "unknown input", correlation_id, refs=refs)
            tto = msr.tto_id if msr else "this MSR's TTO"
            lead = self.registry.tto_lead_for(msr.tto_id) if msr else None
            return self._deny(p, "tto_review", f"TTO lead authority covers only MSRs from employees in the lead's "
                              f"own TTO ({', '.join(sorted(p.tto_authority)) or 'none'}); {tto} is reviewed by "
                              f"{lead or 'its own TTO lead'}", correlation_id, refs=refs)
        else:
            basis = "tto-lead"
        refs |= {"tto_id": msr.tto_id, "review_basis": basis}
        if msr.input_version != input_version:
            return self._deny(p, "tto_review", f"review must target current version {msr.input_version}",
                              correlation_id, refs=refs)
        if msr.review.status != "accepted" or msr.review.reviewed_input_version != input_version:
            return self._deny(p, "tto_review", f"employee review is {msr.review.status}; the employee must accept "
                              "the MSR before TTO lead review", correlation_id, refs=refs)
        if self.store._tto_latest(input_id, input_version):
            return self._deny(p, "tto_review", "TTO lead decision already recorded for this version",
                              correlation_id, refs=refs)
        if self.registry.assess_on_submission and self.assessment(p, input_id, input_version) is None:
            return self._deny(p, "tto_review", "no completed REQ assessment for this version; an operator must "
                              "re-run the Role Execution Agent", correlation_id, refs=refs)
        own = msr.subject_id == p.user_id
        self.store.append_tto_review(p, {"input_id": input_id, "input_version": input_version, "decision": decision,
                                         "reviewer_id": p.user_id, "reviewed_at": self.clock.now_str(),
                                         "tto_id": msr.tto_id, "review_basis": basis, "own_submission": own,
                                         "reason": reason[:200]})
        self.audit.record(p, "tto_review", decision, correlation_id=correlation_id,
                          refs=refs | {"own_submission": own})
        return allow(f"tto-{decision}", **refs, own_submission=own)

    def correct_input(self, p: Principal, input_id: str, new_narrative: str, correlation_id: str = "") -> Outcome:
        """Creates a new input version (review resets to pending) and invalidates dependent evidence."""
        if not (p.has_role("delivery_operator") or p.has_role("employee_reviewer")):
            return self._deny(p, "correct_input", "not permitted", correlation_id)
        msr, hidden = self._visible_input(p, input_id)
        if hidden:
            return self._deny(p, "correct_input", "only the assigned reviewer or an in-scope operator may correct",
                              correlation_id)
        if msr is None:
            return self._deny(p, "correct_input", "unknown input", correlation_id)
        own = self.registry.employee_reviewer_for(msr.subject_id) == p.user_id
        if not own and not (p.has_role("delivery_operator") and p.in_scope(msr.delivery_scope_id)):
            return self._deny(p, "correct_input", "only the assigned reviewer or an in-scope operator may correct",
                              correlation_id)
        new_v = msr.input_version + 1
        try:
            corrected = MsrInput.model_validate(msr.model_dump() | {
                "input_version": new_v, "raw_narrative": new_narrative, "source_status": "active",
                "source_created_at": self.clock.now_str(),
                "source_locator": f"delivery-local:{input_id}:v{new_v}",
                "review": {"status": "pending", "reviewer_id": None, "reviewed_input_version": None,
                           "reviewed_at": None}})
        except ValidationError as e:
            return self._deny(p, "correct_input", f"schema-invalid: {e.error_count()} error(s)", correlation_id)
        old = MsrInput.model_validate(msr.model_dump() | {"source_status": "superseded"})
        self.store.update_input_metadata(p, old)
        self.store.insert_input(p, corrected)
        n = self._invalidate_dependents(p, input_id, "superseded", "source corrected", correlation_id)
        self.audit.record(p, "correct_input", "new-version", correlation_id=correlation_id,
                          refs={"input_id": input_id, "new_version": new_v, "invalidated_records": n})
        return allow("corrected", input_id=input_id, input_version=new_v, invalidated_records=n)

    def withdraw_input(self, p: Principal, input_id: str, correlation_id: str = "") -> Outcome:
        if not p.has_role("delivery_operator"):
            return self._deny(p, "withdraw_input", "only a Delivery operator may withdraw inputs", correlation_id)
        msr, hidden = self._visible_input(p, input_id)
        if hidden:
            return self._deny(p, "withdraw_input", "operator scope does not include this input", correlation_id)
        if msr is None:
            return self._deny(p, "withdraw_input", "unknown input", correlation_id)
        if not p.in_scope(msr.delivery_scope_id):
            return self._deny(p, "withdraw_input", f"operator scope does not include {msr.delivery_scope_id}",
                              correlation_id)
        if self._self_conflict(p, msr.subject_id):
            return self._deny(p, "withdraw_input", SOD, correlation_id)
        self.store.update_input_metadata(p, MsrInput.model_validate(msr.model_dump() | {"source_status": "withdrawn"}))
        n = self._invalidate_dependents(p, input_id, "revoked", "source withdrawn", correlation_id)
        self.audit.record(p, "withdraw_input", "withdrawn", correlation_id=correlation_id,
                          refs={"input_id": input_id, "invalidated_records": n})
        return allow("withdrawn", input_id=input_id, invalidated_records=n)

    def _invalidate_dependents(self, p: Principal, input_id: str, disposition: str, why: str,
                               correlation_id: str) -> int:
        n = 0
        src_status = "withdrawn" if disposition == "revoked" else "superseded"
        for rec in self.evidence.list_records(p):
            if rec.source_ref.input_id != input_id or rec.disposition not in ("draft", "published"):
                continue
            data = rec.model_dump()
            data["disposition"] = disposition
            data["source_ref"] |= {"source_status": src_status, "version_status": "superseded"}
            if disposition == "revoked" and data["pmo_approval"]["status"] == "approved":
                data["pmo_approval"]["status"] = "revoked"
            self.evidence.put_record(p, FilteredEvidence.model_validate(data), action=f"invalidated: {why}")
            self.audit.record(p, "invalidate_evidence", disposition, reason=why, correlation_id=correlation_id,
                              refs={"record_id": rec.record_id, "record_version": rec.record_version})
            n += 1
        return n

    # ================================================================== Role Execution Agent

    def prepare_candidates(self, p: Principal, input_id: str, correlation_id: str = "") -> Outcome:
        if not p.has_role("delivery_operator"):
            return self._deny(p, "prepare_candidates", "only a Delivery operator may trigger preparation",
                              correlation_id)
        msr, hidden = self._visible_input(p, input_id)
        refs = {"input_id": input_id}
        if hidden:
            return self._deny(p, "prepare_candidates", "operator scope does not include this input", correlation_id,
                              refs=refs)
        if msr is None:
            return self._deny(p, "prepare_candidates", "unknown input", correlation_id, refs=refs)
        refs["input_version"] = msr.input_version
        if not p.in_scope(msr.delivery_scope_id):
            return self._deny(p, "prepare_candidates", f"operator scope does not include {msr.delivery_scope_id}",
                              correlation_id, refs=refs)
        if msr.review.status != "accepted" or msr.review.reviewed_input_version != msr.input_version:
            return self._deny(p, "prepare_candidates", f"employee review is {msr.review.status} for the current "
                              "version; only employee-accepted inputs may become evidence", correlation_id, refs=refs)
        if msr.source_status != "active":
            return self._deny(p, "prepare_candidates", f"source is {msr.source_status}", correlation_id, refs=refs)
        if self._self_conflict(p, msr.subject_id):
            return self._deny(p, "prepare_candidates", SOD, correlation_id, refs=refs)
        if self.registry.tto_review_required and not self.registry.assess_on_submission:
            tto = self.tto_status(msr.input_id, msr.input_version)
            if tto != "approved":
                return self._deny(p, "prepare_candidates", f"TTO lead review is {tto} for the current version; "
                                  "TTO lead approval is required before alignment", correlation_id, refs=refs)
        return self._align(p, msr, correlation_id)

    @staticmethod
    def _from_check(check: dict, statements: list[tuple[str, str]]):
        """The feedback check's agent result with its draft statement IDs replaced by the stored ones (same text,
        same split, so the same order)."""
        ids = {f"DRAFT-{i}": sid for i, (sid, _) in enumerate(statements, 1)}
        out = check["result"].parsed
        sub = lambda s: re.sub(r"DRAFT-\d+", lambda m: ids.get(m.group(0), m.group(0)), s)  # noqa: E731
        parsed = AlignmentResult(
            candidates=[c.model_copy(update={"statement_id": ids[c.statement_id]}) for c in out.candidates],
            unresolved_gaps=[sub(g) for g in out.unresolved_gaps],
            new_task_suggestions=[s.model_copy(update={"statement_id": ids[s.statement_id]})
                                  for s in out.new_task_suggestions])
        return dataclasses.replace(check["result"], parsed=parsed)

    def _align(self, p: Principal, msr: MsrInput, correlation_id: str, action: str = "prepare_candidates",
               feedback: dict | None = None) -> Outcome:
        refs = {"input_id": msr.input_id, "input_version": msr.input_version}
        pre = self.registry.id_prefix
        suffix = msr.input_id.removeprefix(f"{pre}-MSR-")
        statements = [(f"{pre}-STMT-{suffix}-V{msr.input_version}-{i}", s)
                      for i, s in enumerate(split_statements(msr.raw_narrative), 1)]
        self.store.put_statements(p, msr.input_id, msr.input_version, statements)
        req = self.registry.role_req_for(msr.role_id, msr.tto_id)
        catalog = [self.registry.req_tasks[t] for t in req["req_task_ids"]] if req else []

        if feedback and len(feedback["check"]["statements"]) != len(statements):
            feedback = None
        if feedback and feedback["check"]["engine"] == "model":
            result, errors = self._from_check(feedback["check"], statements), []
        else:  # a free (preliminary) check is never reused: the agent assesses, writing the recommended tasks too
            result, errors = self.role_agent.align(statements, catalog, suggest=bool(feedback))
        run_id = f"RUN-{uuid.uuid4().hex[:10].upper()}"
        if result is None:
            self.store.put_alignment_run(p, run_id, msr.input_id, msr.input_version, self.clock.now_str(),
                                         {"run_id": run_id, "input_version": msr.input_version, "status": "failed",
                                          "errors": errors})
            esc = self.ops.add_escalation(f"ESC-{uuid.uuid4().hex[:8].upper()}", correlation_id, "agent-failure",
                                          self._route("delivery_review_owner"), "delivery",
                                          "Role Execution Agent failed; no drafts were created.", refs)
            self.audit.record(p, action, "failed-safe", reason="; ".join(errors),
                              correlation_id=correlation_id, refs=refs | {"run_id": run_id, "escalation_id": esc})
            return deny("agent-failed-safe: no drafts created", errors=errors, escalation_id=esc)

        catalog_ids = {t["req_task_id"] for t in catalog}
        stmt_text = dict(statements)
        source_exc = cf.scan_source(msr.raw_narrative)
        created, rejected, gaps = [], [], list(result.parsed.unresolved_gaps)
        aligned: dict[str, list[str]] = {sid: [] for sid, _ in statements}
        existing = {(r.source_ref.input_id, r.source_ref.input_version, r.req_task_id)
                    for r in self.evidence.list_records(p) if r.disposition != "superseded"}
        for c in result.parsed.candidates:
            if c.req_task_id is None:
                gaps.append(f"{c.statement_id}: no task match")
                continue
            if c.req_task_id not in catalog_ids:
                rejected.append(f"{c.statement_id}: {c.req_task_id} is outside the assigned task subset")
                continue
            if c.req_task_id not in aligned.setdefault(c.statement_id, []):
                aligned[c.statement_id].append(c.req_task_id)
            if (msr.input_id, msr.input_version, c.req_task_id) in existing:
                continue  # idempotent re-run
            existing.add((msr.input_id, msr.input_version, c.req_task_id))
            not_done = c.reported_status == "not_completed"
            summary = _with_marker(c.evidence_summary, not_done)
            if self.registry.publish_task_alignment_only:
                summary = self._alignment_summary(msr, c.req_task_id, not_done)
            self._draft(p, msr, c.req_task_id, c.statement_id, summary, stmt_text, source_exc, created)
        tags = [{"statement_id": sid, "req_task_ids": aligned.get(sid, []),
                 "tag": "aligned" if aligned.get(sid) else POTENTIAL_NEW_TASK} for sid, _ in statements]
        tasks = sorted({t for x in tags for t in x["req_task_ids"]})
        new = any(x["tag"] == POTENTIAL_NEW_TASK for x in tags)
        input_tag = POTENTIAL_NEW_TASK if not tasks else f"aligned+{POTENTIAL_NEW_TASK}" if new else "aligned"
        assessment = {"role_req_id": req["role_req_id"] if req else None, "aligned_req_task_ids": tasks,
                      "potential_new_task": new, "input_tag": input_tag, "statements": tags}
        if feedback:
            final = {r["index"]: r for r in self._feedback_rows(statements, catalog, result.parsed)}
            for t, f in zip(tags, feedback["responses"]):
                wording = final[f["index"]]["suggested_task"] or f["suggested_task"]
                t |= {"employee_response": f["response"], "intended_req_task_id": f["intended_req_task_id"],
                      "notes": f["notes"], "check_tag": f["tag"], "check_agreed": f["tag"] == t["tag"],
                      "suggested_task": wording if t["tag"] == POTENTIAL_NEW_TASK else None}
            c = Counter(f["response"] for f in feedback["responses"])
            differed = sum(not t["check_agreed"] for t in tags)
            assessment["employee_feedback"] = {
                "feedback_check": feedback["preview_id"], "check_engine": feedback["check"]["engine"],
                **{k: c[k] for k in ("accurate", "not_intended", "confirm_new", "not_new")},
                "check_differed": differed,
                "needs_higher_review": bool(c["not_intended"] or c["confirm_new"] or c["not_new"] or differed)}
        if self.registry.contract_wide_analytic:
            assessment["contract_wide"] = self._contract_wide(p, msr, statements, catalog_ids, aligned, existing,
                                                              stmt_text, source_exc, created, errors)
        self.store.put_alignment_run(p, run_id, msr.input_id, msr.input_version, self.clock.now_str(), {
            "run_id": run_id, "input_version": msr.input_version, "status": "completed",
            "candidates": [c.model_dump() for c in result.parsed.candidates],
            "unresolved_gaps": gaps, "rejected": rejected, "errors": errors, "model": result.meta(),
            "prompt_version": self.role_agent.prompt_version, "assessment": assessment,
            "reused_feedback_check": feedback["preview_id"] if feedback else None})
        self.audit.record(p, action, "completed", correlation_id=correlation_id,
                          refs=refs | {"run_id": run_id, "records": [c["record_id"] for c in created],
                                       "aligned_tasks": tasks, "input_tag": assessment["input_tag"],
                                       "gaps": len(gaps), "rejected": len(rejected),
                                       "contract_wide": (assessment.get("contract_wide") or {}).get("counts")},
                          meta=result.meta() | {"prompt_version": self.role_agent.prompt_version,
                                                "agent_id": self.role_agent.agent_id})
        return allow("candidates-prepared", run_id=run_id, created=created, gaps=gaps, rejected=rejected,
                     assessment=assessment)

    def _draft(self, p, msr, req_task_id, statement_id, summary, stmt_text, source_exc, created,
               scope="own-req") -> None:
        rec = self._build_draft(p, msr, req_task_id, statement_id, summary,
                                source_exc + cf.check_summary(summary, stmt_text[statement_id])
                                + cf.check_summary(summary, msr.raw_narrative), scope)
        self.evidence.put_record(p, rec, action="draft-created")
        created.append({"record_id": rec.record_id, "req_task_id": rec.req_task_id, "alignment_scope": scope,
                        "blocking_exceptions": cf.blocking(rec.exceptions)})

    def _contract_wide(self, p, msr, statements, own_ids, aligned, existing, stmt_text, source_exc, created,
                       errors) -> dict:
        """Analytic: the same statements against every other REQ on the contract, tracked separately. A statement
        that also aligns elsewhere signals similar work; a potential new task for the employee's REQ that aligns
        elsewhere signals a collaboration opportunity with the area doing that work. Each such alignment becomes
        its own draft record for PMO review and, once approved, goes to Solutions."""
        catalog = self.registry.contract_req_tasks(msr.contract_id, exclude=own_ids)
        result, errs = self.role_agent.align(statements, catalog)
        if result is None:
            errors += [f"contract-wide analytic: {e}" for e in errs]
            return {"status": "failed", "statements": [], "counts": {}}
        ids = {t["req_task_id"] for t in catalog}
        found: dict[str, list[str]] = {}
        for c in result.parsed.candidates:
            if c.req_task_id not in ids or c.req_task_id in found.get(c.statement_id, []):
                continue
            found.setdefault(c.statement_id, []).append(c.req_task_id)
            scope = "similar-work" if aligned.get(c.statement_id) else "collaboration-opportunity"
            if (msr.input_id, msr.input_version, c.req_task_id) in existing:
                continue
            existing.add((msr.input_id, msr.input_version, c.req_task_id))
            summary = self._alignment_summary(msr, c.req_task_id, c.reported_status == "not_completed", scope)
            self._draft(p, msr, c.req_task_id, c.statement_id, summary, stmt_text, source_exc, created, scope)
        rows = [{"statement_id": sid, "signal": "similar-work" if aligned.get(sid) else "collaboration-opportunity",
                 "elsewhere": [{"req_task_id": t, "tto_id": self.registry.req_tasks[t]["tto_id"],
                                "reqs": self.registry.reqs_containing(t)} for t in found[sid]]}
                for sid, _ in statements if found.get(sid)]
        counts = {k: sum(r["signal"] == k for r in rows) for k in ("similar-work", "collaboration-opportunity")}
        return {"status": "completed", "statements": rows, "counts": counts}

    def _alignment_summary(self, msr: MsrInput, req_task_id: str, not_done: bool, scope: str = "own-req") -> str:
        """What crosses to Solutions on the roster workflow: the task and the aligned employee data (name, ID, role,
        TTO), built from reference data only. The MSR input itself is never forwarded."""
        t = self.registry.req_tasks[req_task_id]
        name = self.registry.subjects.get(msr.subject_id, {}).get("name")
        who = f"Employee {name} ({msr.subject_id}, {msr.role_id}, {msr.tto_id})" if name else \
            f"Employee {msr.subject_id} ({msr.role_id}, {msr.tto_id})"
        if scope == "own-req":
            status = "reported as not completed" if not_done else "reported work aligned to"
            return f"{who} {status} {req_task_id}: {t['title']}"[:300]
        status = "reported as not completed work similar to" if not_done else "reported work similar to"
        tail = " (similar work on the contract)" if scope == "similar-work" else \
            " (collaboration opportunity: potential new task for the employee's REQ)"
        return f"{who} {status} {req_task_id} in {t['tto_id']}: {t['title']}{tail}"[:300]

    def _build_draft(self, p, msr: MsrInput, req_task_id: str, statement_id: str, summary: str,
                     exceptions: list[str], scope: str = "own-req") -> FilteredEvidence:
        ev = self.cfg["evidence"] | self.registry.evidence_defaults
        role_task_id = self.registry.role_task_for(req_task_id, msr.role_id) if scope == "own-req" else \
            self.registry.eligible_role_tasks(self.registry.req_tasks[req_task_id])[0]  # the other REQ's role task
        now = self.clock.now()
        data = {
            "schema_version": "prototype-filtered-evidence/1.0",
            "record_id": self.evidence.next_record_id(p, f"{self.registry.id_prefix}-EVID"), "record_version": 1,
            "data_mode": "synthetic", "classification": "synthetic-demo",
            "owning_pmo_id": self.registry.pmo_for_contract(msr.contract_id),
            "delivery_scope_id": msr.delivery_scope_id, "role_id": msr.role_id, "tto_id": msr.tto_id,
            "contract_id": msr.contract_id, "req_task_id": req_task_id, "role_task_id": role_task_id,
            "evidence_summary": summary,
            "source_ref": {"input_id": msr.input_id, "input_version": msr.input_version,
                           "source_status": msr.source_status, "version_status": "current"},
            "freshness": {"verified_at": format_utc(now),
                          "expires_at": format_utc(now + timedelta(days=ev["freshness_days"]))},
            "employee_review": {"status": "accepted", "reviewer_id": msr.review.reviewer_id,
                                "reviewed_at": msr.review.reviewed_at,
                                "reviewed_input_version": msr.review.reviewed_input_version},
            "pmo_approval": {"status": "pending", "approver_id": None, "approved_at": None,
                             "approved_record_version": None},
            "audience_ids": list(ev["default_audience_ids"]),
            "permitted_purposes": list(ev["default_purposes"]),
            "restrictions": list(ev["default_restrictions"]),
            "provenance": [{"source_input_id": msr.input_id, "source_input_version": msr.input_version,
                            "source_statement_id": statement_id,
                            "transformation_version": ev["transformation_version"]}],
            "confidence": None,
            "exceptions": sorted(set(exceptions)),
            "disposition": "draft", "published_at": None, "alignment_scope": scope,
        }
        rec = FilteredEvidence.model_validate(data)
        ref_errs = self.registry.evidence_reference_errors(rec)
        if ref_errs:
            rec = FilteredEvidence.model_validate(data | {"exceptions": sorted(set(
                exceptions + [f"BLOCKING:reference: {e}" for e in ref_errs]))})
        return rec

    def revise_evidence(self, p: Principal, record_id: str, new_summary: str, correlation_id: str = "") -> Outcome:
        """Creates the next record version with a revised summary; PMO approval resets to pending."""
        if not (p.has_role("delivery_operator") or p.has_role("human_pmo_reviewer")):
            return self._deny(p, "revise_evidence", "not permitted", correlation_id)
        cur = self.evidence.get_record(p, record_id)
        if cur is None:
            return self._deny(p, "revise_evidence", "unknown record", correlation_id)
        if cur.disposition in ("superseded", "revoked"):
            return self._deny(p, "revise_evidence", f"record is {cur.disposition}", correlation_id)
        if self._self_conflict(p, self.store._subject_of(cur.source_ref.input_id)):
            return self._deny(p, "revise_evidence", SOD, correlation_id)
        src = self.store.get_input(p, cur.source_ref.input_id, cur.source_ref.input_version)
        stmt = dict(self.store.get_statements(p, src.input_id, src.input_version))
        stmt_text = stmt.get(cur.provenance[0].source_statement_id, src.raw_narrative)
        exc = cf.scan_source(src.raw_narrative) + cf.check_summary(new_summary, stmt_text) \
            + cf.check_summary(new_summary, src.raw_narrative)
        data = cur.model_dump() | {
            "record_version": cur.record_version + 1, "evidence_summary": new_summary,
            "pmo_approval": {"status": "pending", "approver_id": None, "approved_at": None,
                             "approved_record_version": None},
            "exceptions": sorted(set(exc)), "disposition": "draft", "published_at": None}
        rec = FilteredEvidence.model_validate(data)
        self.evidence.put_record(p, rec, action="revised")
        self.audit.record(p, "revise_evidence", "new-version", correlation_id=correlation_id,
                          refs={"record_id": record_id, "record_version": rec.record_version})
        return allow("revised", record_id=record_id, record_version=rec.record_version)

    # ================================================================== validation used by both gates

    def validate_for_approval(self, p: Principal, rec: FilteredEvidence) -> dict:
        """Recomputes every prerequisite from authoritative state. Never trusts an asserted snapshot."""
        try:
            FilteredEvidence.model_validate(rec.model_dump())
            schema_valid = True
        except ValidationError:
            schema_valid = False
        ref_errs = self.registry.evidence_reference_errors(rec)
        try:
            src_latest = self.store.get_input(p, rec.source_ref.input_id)
            src = self.store.get_input(p, rec.source_ref.input_id, rec.source_ref.input_version)
        except PermissionDenied:  # caller may not see the source: every source check fails closed
            src_latest = src = None
        source_current = bool(src_latest and src and src_latest.input_version == rec.source_ref.input_version
                              and src.source_status == "active")
        review_current = bool(src and src.review.status == "accepted"
                              and src.review.reviewed_input_version == src.input_version
                              and src.review.reviewer_id == rec.employee_review.reviewer_id
                              and src.review.reviewed_at == rec.employee_review.reviewed_at)
        content_exc = []
        if src:
            stmt = dict(self.store.get_statements(p, src.input_id, src.input_version))
            for prov in rec.provenance:
                content_exc += cf.check_summary(rec.evidence_summary, stmt.get(prov.source_statement_id, ""))
            content_exc += cf.check_summary(rec.evidence_summary, src.raw_narrative) + cf.scan_source(src.raw_narrative)
        workflow_exc = []
        if self.registry.tto_review_required and src and self.tto_status(src.input_id, src.input_version) != "approved":
            workflow_exc.append("BLOCKING:tto-lead-review-not-current")
        blocking = sorted(set(cf.blocking(rec.exceptions) + cf.blocking(content_exc) + workflow_exc))
        return {"schema_valid": schema_valid, "references_valid": not ref_errs, "source_current": source_current,
                "employee_review_current": review_current,
                "content_filter_passed": bool(src) and not cf.blocking(content_exc),
                "blocking_exceptions": blocking}

    @staticmethod
    def snapshot_ok(s: dict) -> bool:
        return all(s[k] for k in ("schema_valid", "references_valid", "source_current", "employee_review_current",
                                  "content_filter_passed")) and not s["blocking_exceptions"]

    # ================================================================== PMO human gate

    def pmo_decide(self, p: Principal, record_id: str, record_version: int, decision: str, reason: str,
                   correlation_id: str = "") -> Outcome:
        """Authenticated human-review action that constructs and processes an approval event."""
        rec = self.evidence.get_record(p, record_id, record_version) if p.domain == "delivery" else None
        if rec is None:
            return self._deny(p, "pmo_decide", "no-pmo-authority for this record, or unknown record", correlation_id,
                              refs={"record_id": record_id, "record_version": record_version})
        snap = self.validate_for_approval(p, rec)
        event = PmoApprovalEvent.model_validate({
            "schema_version": "prototype-pmo-approval-event/1.0",
            "event_id": f"SYN-PMO-APPROVAL-{uuid.uuid4().hex[:8].upper()}",
            "data_mode": "synthetic", "event_type": "pmo_evidence_approval",
            "record_id": record_id, "record_version": record_version, "owning_pmo_id": rec.owning_pmo_id,
            "delivery_scope_id": rec.delivery_scope_id, "decision": decision, "approver_id": p.user_id,
            "approver_role": "human_pmo_reviewer", "decided_at": self.clock.now_str(),
            "employee_review_ref": {"input_id": rec.source_ref.input_id,
                                    "input_version": rec.source_ref.input_version,
                                    "reviewer_id": rec.employee_review.reviewer_id,
                                    "reviewed_at": rec.employee_review.reviewed_at, "status": "accepted"},
            "approved_scope": {"audience_ids": rec.audience_ids, "permitted_purposes": rec.permitted_purposes,
                               "restrictions": rec.restrictions, "expires_at": rec.freshness.expires_at},
            "validation_snapshot": snap, "decision_reason": reason or f"{decision} by human PMO reviewer",
            "correlation_id": correlation_id or f"SYN-WF-A-{record_id}",
            "publication_authorized": decision == "approved", "publication_executed": False})
        return self.process_approval_event(p, event, correlation_id=correlation_id)

    def process_approval_event(self, p: Principal, event: PmoApprovalEvent | dict,
                               target_record_version: int | None = None, correlation_id: str = "") -> Outcome:
        if isinstance(event, dict):
            try:
                event = PmoApprovalEvent.model_validate(event)
            except ValidationError as e:
                return self._deny(p, "process_approval_event", f"schema-invalid: {e.error_count()} error(s)",
                                  correlation_id)
        cid = correlation_id or event.correlation_id
        target_v = target_record_version if target_record_version is not None else event.record_version
        refs = {"event_id": event.event_id, "record_id": event.record_id, "event_record_version": event.record_version,
                "target_record_version": target_v, "decision": event.decision}

        def reject(reason: str, snapshot: dict | None = None) -> Outcome:
            if p.domain == "delivery":
                self.evidence.append_processing(p, event.event_id, event.record_id, target_v, "denied", reason,
                                                snapshot or {})
            return self._deny(p, "process_approval_event", reason, cid, refs=refs)

        if p.kind != "human":
            return reject("agent-self-approval-prohibited: approvals require an authenticated human action")
        if not p.has_role("human_pmo_reviewer"):
            return reject("actor is not a human PMO reviewer")
        if p.user_id != event.approver_id:
            return reject("approver-identity-mismatch: authenticated user is not the event approver")
        if event.owning_pmo_id not in p.pmo_authority:
            return reject(f"no-pmo-authority: {p.user_id} has no authority for {event.owning_pmo_id}")

        if self._self_conflict(p, self.store._subject_of(event.employee_review_ref.input_id)):
            return reject(SOD)

        prior = self.evidence.get_approval_event(p, event.event_id)
        if prior is not None:
            if prior.model_dump() == event.model_dump() and prior.record_version == target_v:
                self.evidence.append_processing(p, event.event_id, event.record_id, target_v, "duplicate-ignored",
                                                "idempotent replay of an already-processed event", {})
                self.audit.record(p, "process_approval_event", "duplicate-ignored", correlation_id=cid, refs=refs)
                return allow("duplicate-ignored", **refs)
            return reject(f"version-mismatch: replayed event {event.event_id} is bound to record version "
                          f"{prior.record_version}, target is version {target_v}")

        rec = self.evidence.get_record(p, event.record_id, target_v)
        if rec is None:
            return reject("unknown record version")
        if event.record_version != target_v:
            return reject(f"version-mismatch: event approves version {event.record_version}, target is "
                          f"version {target_v}")
        latest = self.evidence.get_record(p, event.record_id)
        if event.decision != "revoked" and latest.record_version != target_v:
            return reject(f"not-current-record-version: latest is {latest.record_version}")
        if (rec.owning_pmo_id, rec.delivery_scope_id) != (event.owning_pmo_id, event.delivery_scope_id):
            return reject("owning PMO or delivery scope does not match the record")
        er = event.employee_review_ref
        if (er.input_id, er.input_version, er.reviewer_id, er.reviewed_at) != (
                rec.source_ref.input_id, rec.source_ref.input_version, rec.employee_review.reviewer_id,
                rec.employee_review.reviewed_at):
            return reject("employee-review reference does not match the record")
        sc = event.approved_scope
        if (sorted(sc.audience_ids), sorted(sc.permitted_purposes), sorted(sc.restrictions), sc.expires_at) != (
                sorted(rec.audience_ids), sorted(rec.permitted_purposes), sorted(rec.restrictions),
                rec.freshness.expires_at):
            return reject("scope-mismatch: approved scope differs from the record")
        if parse_utc(event.decided_at) > self.clock.now():
            return reject("decision time is in the future")

        if event.decision == "revoked":
            if rec.pmo_approval.status != "approved":
                return reject("only an approved record can be revoked")
        elif rec.pmo_approval.status != "pending" or rec.disposition != "draft":
            return reject(f"record is not pending PMO review (status {rec.pmo_approval.status}, "
                          f"disposition {rec.disposition})")

        snap = self.validate_for_approval(p, rec)
        if event.decision == "approved" and not self.snapshot_ok(snap):
            failed = [k for k, v in snap.items() if v is False] + snap["blocking_exceptions"]
            return reject(f"prerequisites-failed: {failed}", snap)

        self.evidence.append_approval_event(p, event)
        self.evidence.append_processing(p, event.event_id, event.record_id, target_v, "accepted", "", snap)
        data = rec.model_dump()
        if event.decision == "approved":
            data["pmo_approval"] = {"status": "approved", "approver_id": event.approver_id,
                                    "approved_at": event.decided_at, "approved_record_version": target_v}
        elif event.decision == "rejected":
            data["pmo_approval"] = {"status": "rejected", "approver_id": event.approver_id, "approved_at": None,
                                    "approved_record_version": None}
        elif event.decision == "returned_for_correction":
            data["exceptions"] = sorted(set(data["exceptions"]) | {"BLOCKING:returned-for-correction"})
        elif event.decision == "revoked":
            data["pmo_approval"]["status"] = "revoked"
            data["disposition"] = "revoked"
        self.evidence.put_record(p, FilteredEvidence.model_validate(data), action=f"pmo-{event.decision}")
        self.audit.record(p, "process_approval_event", "accepted", correlation_id=cid, refs=refs)
        return allow(f"pmo-{event.decision}-recorded", **refs)

    def revoke(self, p: Principal, record_id: str, record_version: int, reason: str, correlation_id: str = "") -> Outcome:
        return self.pmo_decide(p, record_id, record_version, "revoked", reason, correlation_id)

    # ================================================================== publication (separate from approval)

    def publication_check(self, p: Principal, record_id: str, record_version: int,
                          approval_event_id: str) -> list[str]:
        """Side-effect-free eligibility check for the bounded publication path. Empty list = eligible."""
        return self._publication_reasons(p, record_id, record_version, approval_event_id)[1]

    def _publication_reasons(self, p: Principal, record_id: str, record_version: int,
                             approval_event_id: str) -> tuple[FilteredEvidence | None, list[str]]:
        rec = self.evidence.get_record(p, record_id, record_version)
        reasons = []
        if rec is None:
            reasons.append("unknown record version")
        else:
            if p.has_role("human_pmo_reviewer") and not p.has_role("delivery_operator") \
                    and rec.owning_pmo_id not in p.pmo_authority:
                reasons.append("no PMO authority for this record")
            if self._self_conflict(p, self.store._subject_of(rec.source_ref.input_id)):
                reasons.append(SOD)
            if rec.disposition != "draft":
                reasons.append(f"disposition is {rec.disposition}, expected draft")
            latest = self.evidence.get_record(p, record_id)
            if latest.record_version != record_version:
                reasons.append(f"not the current record version (latest {latest.record_version})")
            event = self.evidence.get_approval_event(p, approval_event_id)
            if event is None:
                reasons.append("approval event not found in the authoritative event store")
            else:
                if event.record_id != record_id or event.record_version != record_version:
                    reasons.append(f"version-mismatch: event {approval_event_id} approves {event.record_id} "
                                   f"v{event.record_version}, not v{record_version}")
                if event.decision != "approved":
                    reasons.append(f"event decision is {event.decision}")
                processed = [x for x in self.evidence.processing_log(p, approval_event_id)
                             if x["outcome"] == "accepted" and x["target_record_version"] == record_version]
                if not processed:
                    reasons.append("approval event was never accepted for this record version")
                later = [e for e in self.evidence.approval_events_for(p, record_id, record_version)
                         if e.decision in ("rejected", "revoked")]
                if later:
                    reasons.append(f"authorization superseded by {later[-1].decision} event {later[-1].event_id}")
                pa = rec.pmo_approval
                if (pa.status, pa.approver_id, pa.approved_at, pa.approved_record_version) != (
                        "approved", event.approver_id, event.decided_at, record_version):
                    reasons.append("record approval fields do not match the authoritative event")
                sc = event.approved_scope
                if (sorted(sc.audience_ids), sorted(sc.permitted_purposes), sorted(sc.restrictions),
                        sc.expires_at) != (sorted(rec.audience_ids), sorted(rec.permitted_purposes),
                                           sorted(rec.restrictions), rec.freshness.expires_at):
                    reasons.append("scope-mismatch with approved scope")
                if self.clock.now() < parse_utc(event.decided_at):
                    reasons.append("publication cannot precede approval")
            if self.clock.now() >= parse_utc(rec.freshness.expires_at):
                reasons.append("evidence freshness has expired")
            snap = self.validate_for_approval(p, rec)
            if not self.snapshot_ok(snap):
                reasons.append(f"prerequisites-failed: "
                               f"{[k for k, v in snap.items() if v is False] + snap['blocking_exceptions']}")
        return rec, reasons

    def publish(self, p: Principal, record_id: str, record_version: int, approval_event_id: str,
                correlation_id: str = "") -> Outcome:
        refs = {"record_id": record_id, "record_version": record_version, "approval_event_id": approval_event_id}
        if p.kind != "human" or not (p.has_role("human_pmo_reviewer") or p.has_role("delivery_operator")):
            return self._deny(p, "publish", "publication requires an authenticated Delivery/PMO human",
                              correlation_id, refs=refs)
        rec, reasons = self._publication_reasons(p, record_id, record_version, approval_event_id)
        if reasons:
            if rec is not None:
                self.evidence.append_publication(p, record_id, record_version, approval_event_id, "denied",
                                                 "; ".join(reasons), rec.disposition)
            return self._deny(p, "publish", "; ".join(reasons), correlation_id, refs=refs)

        now = self.clock.now_str()
        for older in self.evidence.list_records(p):
            if older.record_id == record_id and older.record_version != record_version \
                    and older.disposition == "published":
                self.evidence.put_record(p, FilteredEvidence.model_validate(
                    older.model_dump() | {"disposition": "superseded"}), action="superseded-by-publication")
        published = FilteredEvidence.model_validate(rec.model_dump() | {"disposition": "published",
                                                                         "published_at": now})
        self.evidence.put_record(p, published, action="published")
        self.evidence.append_publication(p, record_id, record_version, approval_event_id, "published", "",
                                         "published")
        self.audit.record(p, "publish", "published", correlation_id=correlation_id, refs=refs)
        return allow("published", published_at=now, **refs)

    # ================================================================== bulk review (TTO lead and PMO)
    # Each item still passes through the single-item action with all of its checks; bulk only batches the calls,
    # shares one correlation ID, and reports a per-item outcome.

    def _alignment_text(self, a: dict | None) -> tuple[str, str]:
        if not a:
            return "not assessed", ""
        reg = self.registry
        own = "; ".join(f"{t} {reg.req_tasks[t]['title']}" for t in a["aligned_req_task_ids"]) or "none"
        new = sum(s["tag"] == POTENTIAL_NEW_TASK for s in a["statements"])
        cw = (a.get("contract_wide") or {}).get("statements", [])
        elsewhere = "; ".join(f"{r['signal']}: " + ", ".join(x["req_task_id"] for x in r["elsewhere"]) for r in cw)
        text = f"{own}" + (f" | {new} potential new task(s)" if new else "")
        return text, elsewhere

    def _feedback_text(self, a: dict | None) -> str:
        """The employee's responses from the feedback check, for the TTO lead (empty if there was no check)."""
        fb = (a or {}).get("employee_feedback")
        if not fb:
            return ""
        parts = []
        if fb["confirm_new"]:
            titles = [s["suggested_task"]["title"] for s in a["statements"]
                      if s.get("employee_response") == "confirm_new" and s.get("suggested_task")]
            parts.append(f"{fb['confirm_new']} new task(s) confirmed" + (f" ({'; '.join(titles)})" if titles else ""))
        if fb["not_intended"]:
            parts.append(f"{fb['not_intended']} alignment(s) disputed")
        if fb["not_new"]:
            parts.append(f"{fb['not_new']} flagged as existing work")
        if fb.get("check_differed"):
            parts.append(f"{fb['check_differed']} statement(s) assessed differently from the employee's preliminary "
                         "check")
        return ("Needs review: " + "; ".join(parts)) if parts else "Employee confirmed every alignment"

    def tto_review_queue(self, p: Principal) -> list[dict]:
        """MSRs awaiting this reviewer's TTO-stage decision: employee-accepted, assessed, current, and either in the
        lead's TTO(s) or, for a PMO team member, from that PMO's contract PMO staff other than the reviewer."""
        def mine(msr):
            if self.registry.is_pmo_staff_tto(msr.tto_id):
                return (p.has_role("human_pmo_reviewer") and msr.subject_id != p.user_id
                        and self.registry.pmo_for_contract(msr.contract_id) in p.pmo_authority)
            return p.has_role("tto_lead") and msr.tto_id in p.tto_authority

        rows = []
        for msr in self.store.list_inputs(p):
            if not mine(msr) or msr.source_status != "active" \
                    or msr.review.status != "accepted" or self.tto_status(msr.input_id, msr.input_version) != "pending":
                continue
            a = self.assessment(p, msr.input_id, msr.input_version)
            aligned, elsewhere = self._alignment_text(a)
            rows.append({"input_id": msr.input_id, "version": msr.input_version, "tto": msr.tto_id,
                         "employee": f"{self.registry.subjects.get(msr.subject_id, {}).get('name', '')} "
                                     f"({msr.subject_id})",
                         "own_msr": msr.subject_id == p.user_id, "msr_input": msr.raw_narrative,
                         "req_alignment": aligned, "input_tag": (a or {}).get("input_tag", "not assessed"),
                         "employee_feedback": self._feedback_text(a),
                         "elsewhere_on_contract": elsewhere})
        return rows

    def bulk_tto_review(self, p: Principal, items: list[tuple[str, int]], decision: str, reason: str = "",
                        correlation_id: str = "") -> Outcome:
        cid = correlation_id or f"SIM-BULK-TTO-{uuid.uuid4().hex[:8].upper()}"
        results = [{"input_id": i, "ok": (o := self.tto_review(p, i, v, decision, reason, cid)).ok, "reason": o.reason}
                   for i, v in items]
        return self._bulk_outcome(p, "bulk_tto_review", decision, results, cid)

    def pmo_queue(self, p: Principal) -> list[dict]:
        """Current draft records awaiting this PMO reviewer: pending approval, or approved and not yet published."""
        latest: dict[str, FilteredEvidence] = {}
        for r in self.evidence.list_records(p):
            if r.owning_pmo_id in p.pmo_authority and (r.record_id not in latest
                                                       or r.record_version > latest[r.record_id].record_version):
                latest[r.record_id] = r
        rows = []
        for r in latest.values():
            if r.disposition != "draft" or r.pmo_approval.status not in ("pending", "approved"):
                continue
            try:
                msr = self.store.get_input(p, r.source_ref.input_id, r.source_ref.input_version)
            except PermissionDenied:
                continue
            subject = self.store._subject_of(r.source_ref.input_id)
            if self._self_conflict(p, subject):
                continue  # separation of duties: records from the reviewer's own MSR go to a teammate
            rows.append({
                "record_id": r.record_id, "version": r.record_version,
                "status": "awaiting approval" if r.pmo_approval.status == "pending" else "approved, not published",
                "source_msr": r.source_ref.input_id,
                "employee": f"{self.registry.subjects.get(subject, {}).get('name', '')} ({subject})",
                "msr_input": msr.raw_narrative if msr else "",
                "aligned_task": f"{r.req_task_id} {self.registry.req_tasks[r.req_task_id]['title']}",
                "alignment": r.alignment_scope, "solutions_receives": r.evidence_summary,
                "tto_review": self.tto_status(r.source_ref.input_id, r.source_ref.input_version)
                if self.registry.tto_review_required else "n/a",
                "blocking": ", ".join(cf.blocking(r.exceptions))})
        return sorted(rows, key=lambda x: (x["source_msr"], x["record_id"]))

    def bulk_pmo_approve(self, p: Principal, records: list[tuple[str, int]], reason: str = "",
                         correlation_id: str = "") -> Outcome:
        cid = correlation_id or f"SIM-BULK-PMO-{uuid.uuid4().hex[:8].upper()}"
        results = []
        for rid, v in records:
            o = self.pmo_decide(p, rid, v, "approved", reason or "Bulk approval by the human PMO reviewer.", cid)
            results.append({"record_id": rid, "ok": o.ok, "reason": o.reason, "event_id": o.data.get("event_id")})
        return self._bulk_outcome(p, "bulk_pmo_approve", "approved", results, cid)

    def bulk_publish(self, p: Principal, records: list[tuple[str, int]], correlation_id: str = "") -> Outcome:
        """Publication stays a separate action from approval: each record publishes with its own approval event."""
        cid = correlation_id or f"SIM-BULK-PUB-{uuid.uuid4().hex[:8].upper()}"
        results = []
        for rid, v in records:
            ev = [e for e in self.evidence.approval_events_for(p, rid, v) if e.decision == "approved"]
            if not ev:
                results.append({"record_id": rid, "ok": False, "reason": "not approved yet"})
                continue
            o = self.publish(p, rid, v, ev[-1].event_id, cid)
            results.append({"record_id": rid, "ok": o.ok, "reason": o.reason})
        return self._bulk_outcome(p, "bulk_publish", "published", results, cid)

    def _bulk_outcome(self, p: Principal, action: str, decision: str, results: list[dict], cid: str) -> Outcome:
        done = sum(r["ok"] for r in results)
        self.audit.record(p, action, f"{done}-of-{len(results)}", correlation_id=cid,
                          refs={"decision": decision, "items": len(results), "succeeded": done})
        return Outcome(done == len(results) and bool(results),
                       f"{decision}: {done} of {len(results)} succeeded", {"results": results, "correlation_id": cid})

    # ================================================================== helpers

    def _self_conflict(self, p: Principal, subject_id: str | None) -> bool:
        """True when management authority would apply to the user's own MSR and policy does not allow it."""
        if self.cfg.get("governance", {}).get("management_authority_covers_own_msr", False):
            return False
        return bool(subject_id) and subject_id == p.user_id

    def _visible_input(self, p: Principal, input_id: str) -> tuple[MsrInput | None, bool]:
        """Returns (input, hidden). hidden=True when the input exists but this principal may not see it."""
        try:
            return self.store.get_input(p, input_id), False
        except PermissionDenied:
            return None, True

    def _route(self, key: str) -> str:
        return self.registry.routing.get(key) or self.cfg["routing"][key]

    def _deny(self, p: Principal, action: str, reason: str, correlation_id: str, refs: dict | None = None,
              **data) -> Outcome:
        self.audit.record(p, action, "denied", reason=reason, correlation_id=correlation_id, refs=refs or {},
                          meta={"errors": data.get("errors", [])})
        return deny(reason, **(refs or {}), **data)
