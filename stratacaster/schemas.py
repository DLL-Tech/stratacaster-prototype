"""Proposed prototype contracts (Build Guide Section 2), enforced as closed Pydantic models.

Unknown fields are rejected, required-but-nullable fields must be present, timestamps must be
UTC with a Z suffix, and dates must be strict ISO dates. These are prototype contracts, not an
approved production schema.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .clock import parse_utc

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _nonempty(v: str) -> str:
    if not v or not v.strip():
        raise ValueError("identifier must be non-empty")
    return v


def _utc(v: str) -> str:
    parse_utc(v)
    return v


def _iso_date(v: str) -> str:
    if not _DATE_RE.match(v):
        raise ValueError(f"date must be YYYY-MM-DD: {v!r}")
    date.fromisoformat(v)
    return v


def _no_wildcards(v: List[str]) -> List[str]:
    if not v:
        raise ValueError("allow-list must be non-empty")
    for item in v:
        _nonempty(item)
        if "*" in item or item.lower() in {"all", "any", "everyone"}:
            raise ValueError(f"wildcard access is not permitted: {item!r}")
    return v


Id = Annotated[str, AfterValidator(_nonempty)]
UtcTimestamp = Annotated[str, AfterValidator(_utc)]
IsoDate = Annotated[str, AfterValidator(_iso_date)]
PositiveInt = Annotated[int, Field(gt=0)]
AllowList = Annotated[List[str], AfterValidator(_no_wildcards)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=False)


# ---------------------------------------------------------------- MSR input (Delivery only)

class MsrReview(Closed):
    status: Literal["pending", "accepted", "rejected", "correction_requested"]
    reviewer_id: Optional[Id]
    reviewed_input_version: Optional[PositiveInt]
    reviewed_at: Optional[UtcTimestamp]
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _status_rules(self):
        if self.status == "pending":
            if self.reviewer_id is not None or self.reviewed_input_version is not None or self.reviewed_at is not None:
                raise ValueError("pending review must use null reviewer_id, reviewed_input_version, reviewed_at")
        else:
            if self.reviewer_id is None or self.reviewed_at is None:
                raise ValueError(f"{self.status} review requires reviewer_id and reviewed_at")
            if self.status in ("accepted", "rejected") and self.reviewed_input_version is None:
                raise ValueError(f"{self.status} review requires reviewed_input_version")
        return self


class MsrInput(Closed):
    schema_version: Literal["prototype-msr-input/1.0"]
    input_id: Id
    input_version: PositiveInt
    data_mode: Literal["synthetic"]
    delivery_scope_id: Id
    subject_id: Id
    period_start: IsoDate
    period_end: IsoDate
    role_id: Id
    tto_id: Id
    contract_id: Id
    source_status: Literal["active", "superseded", "withdrawn"]
    source_created_at: UtcTimestamp
    raw_narrative: Annotated[str, AfterValidator(_nonempty)]
    source_locator: Id
    review: MsrReview
    classification: Literal["synthetic-demo"]
    restrictions: List[str]

    @model_validator(mode="after")
    def _cross_field(self):
        if date.fromisoformat(self.period_end) < date.fromisoformat(self.period_start):
            raise ValueError("period_end must not precede period_start")
        if "delivery-only" not in self.restrictions:
            raise ValueError("restrictions must include delivery-only")
        rv = self.review.reviewed_input_version
        if rv is not None and rv != self.input_version:
            raise ValueError(
                f"review references input version {rv} but input is version {self.input_version}")
        return self


# ---------------------------------------------------------------- Filtered evidence (cross-boundary)

class SourceRef(Closed):
    input_id: Id
    input_version: PositiveInt
    source_status: Literal["active", "superseded", "withdrawn"]
    version_status: Literal["current", "superseded", "unknown"]


class Freshness(Closed):
    verified_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def _order(self):
        if parse_utc(self.expires_at) <= parse_utc(self.verified_at):
            raise ValueError("expires_at must follow verified_at")
        return self


class EmployeeReviewRef(Closed):
    status: Literal["accepted"]
    reviewer_id: Id
    reviewed_at: UtcTimestamp
    reviewed_input_version: PositiveInt


class PmoApproval(Closed):
    status: Literal["pending", "approved", "rejected", "revoked"]
    approver_id: Optional[Id]
    approved_at: Optional[UtcTimestamp]
    approved_record_version: Optional[PositiveInt]

    @model_validator(mode="after")
    def _rules(self):
        fields = (self.approver_id, self.approved_at, self.approved_record_version)
        if self.status == "approved" and any(f is None for f in fields):
            raise ValueError("approved requires approver_id, approved_at, approved_record_version")
        if self.status == "pending" and any(f is not None for f in fields):
            raise ValueError("pending approval must use null approver_id, approved_at, approved_record_version")
        return self


class Provenance(Closed):
    source_input_id: Id
    source_input_version: PositiveInt
    source_statement_id: Id
    transformation_version: Id


class FilteredEvidence(Closed):
    schema_version: Literal["prototype-filtered-evidence/1.0"]
    record_id: Id
    record_version: PositiveInt
    data_mode: Literal["synthetic"]
    classification: Literal["synthetic-demo"]
    owning_pmo_id: Id
    delivery_scope_id: Id
    role_id: Id
    tto_id: Id
    contract_id: Id
    req_task_id: Id
    role_task_id: Id
    evidence_summary: Annotated[str, AfterValidator(_nonempty)]
    source_ref: SourceRef
    freshness: Freshness
    employee_review: EmployeeReviewRef
    pmo_approval: PmoApproval
    audience_ids: AllowList
    permitted_purposes: AllowList
    restrictions: List[str]
    provenance: Annotated[List[Provenance], Field(min_length=1)]
    confidence: Optional[Annotated[float, Field(ge=0.0, le=1.0)]]
    exceptions: List[str]
    disposition: Literal["draft", "published", "superseded", "expired", "revoked"]
    published_at: Optional[UtcTimestamp]
    # own-req: the task is in the employee's REQ. The other two come from the contract-wide analytic and are tracked
    # separately: similar-work (the statement also aligns to the employee's REQ) or collaboration-opportunity (the
    # statement is a potential new task for the employee's REQ but aligns to another REQ on the contract).
    alignment_scope: Literal["own-req", "similar-work", "collaboration-opportunity"] = "own-req"

    @model_validator(mode="after")
    def _cross_field(self):
        if self.employee_review.reviewed_input_version != self.source_ref.input_version:
            raise ValueError("employee review must reference the source input version")
        for p in self.provenance:
            if (p.source_input_id, p.source_input_version) != (self.source_ref.input_id, self.source_ref.input_version):
                raise ValueError("provenance must identify the current reviewed input version")
        if self.disposition == "published":
            if self.published_at is None:
                raise ValueError("published disposition requires published_at")
            if self.pmo_approval.status != "approved" or self.pmo_approval.approved_at is None:
                raise ValueError("published disposition requires approved PMO review")
            if parse_utc(self.published_at) < parse_utc(self.pmo_approval.approved_at):
                raise ValueError("published_at must not precede approval")
        return self


# ---------------------------------------------------------------- PMO approval event (human action)

class EmployeeReviewEventRef(Closed):
    input_id: Id
    input_version: PositiveInt
    reviewer_id: Id
    reviewed_at: UtcTimestamp
    status: Literal["accepted"]


class ApprovedScope(Closed):
    audience_ids: AllowList
    permitted_purposes: AllowList
    restrictions: List[str]
    expires_at: UtcTimestamp


class ValidationSnapshot(Closed):
    schema_valid: bool
    references_valid: bool
    source_current: bool
    employee_review_current: bool
    content_filter_passed: bool
    blocking_exceptions: List[str]


class PmoApprovalEvent(Closed):
    schema_version: Literal["prototype-pmo-approval-event/1.0"]
    event_id: Id
    data_mode: Literal["synthetic"]
    event_type: Literal["pmo_evidence_approval"]
    record_id: Id
    record_version: PositiveInt
    owning_pmo_id: Id
    delivery_scope_id: Id
    decision: Literal["approved", "rejected", "returned_for_correction", "revoked"]
    approver_id: Id
    approver_role: Literal["human_pmo_reviewer"]
    decided_at: UtcTimestamp
    employee_review_ref: EmployeeReviewEventRef
    approved_scope: ApprovedScope
    validation_snapshot: ValidationSnapshot
    decision_reason: Annotated[str, AfterValidator(_nonempty)]
    correlation_id: Id
    publication_authorized: bool
    publication_executed: bool


# ---------------------------------------------------------------- Workflow B query/response

class EvidenceQuery(Closed):
    schema_version: Literal["prototype-evidence-query/1.0"] = "prototype-evidence-query/1.0"
    query_id: Id
    requester_id: Id
    audience_id: Id
    purpose: Id
    req_task_ids: Annotated[List[Id], Field(min_length=1)]
    question: Annotated[str, Field(max_length=2000)]
    include_raw_sources: bool = False


class CoverageItem(Closed):
    req_task_id: str
    status: Literal["supported", "partial", "unsupported", "conflict"]
    cited_record_ids: List[str]
    note: str


class ExpertiseExplanation(BaseModel):
    """Structured output from the Solutions Expertise Agent (model-generated, validated before use)."""
    model_config = ConfigDict(extra="forbid")

    coverage: List[CoverageItem]
    explanation: str
    uncertainty: str
    needs_human_review: bool


class AlignmentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    req_task_id: Optional[str]
    evidence_summary: str
    rationale: str
    # Structured completion status (DEF-007): deterministic code reads this, never the wording of the summary.
    reported_status: Literal["completed", "in_progress", "not_completed", "unclear"]


class NewTaskSuggestion(BaseModel):
    """What a new REQ task could look like for a statement that matched no task (employee feedback, PR-30)."""
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    title: str
    description: str
    rationale: str


class AlignmentResult(BaseModel):
    """Structured output from the Role Execution Agent (model-generated, validated before use)."""
    model_config = ConfigDict(extra="forbid")

    candidates: List[AlignmentCandidate]
    unresolved_gaps: List[str]
    # Only requested for the employee's pre-submission feedback check; empty otherwise.
    new_task_suggestions: List[NewTaskSuggestion] = Field(default_factory=list)


PLAN_OPERATORS = ("add-task", "widen-task", "route-elsewhere", "new-req", "no-change")


class PlanOption(BaseModel):
    """One candidate thought for a recurring cluster of potential-new-task statements (TTO Coordination Agent)."""
    model_config = ConfigDict(extra="forbid")

    operator: Literal["add-task", "widen-task", "route-elsewhere", "new-req", "no-change"]
    target_req_task_id: Optional[str]
    wording: str
    rationale: str


class PlanCandidates(BaseModel):
    """Structured output from the TTO Coordination Agent's thought generator (validated before use)."""
    model_config = ConfigDict(extra="forbid")

    options: List[PlanOption]


# Fields that must never cross the Delivery boundary, at any nesting level.
DELIVERY_ONLY_FIELDS = frozenset({"raw_narrative", "source_locator", "notes", "raw_excerpt", "statement_text"})
