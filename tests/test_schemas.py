"""Section 2 contracts and the 'Synthetic Validation Checks'. Traces: PR-06."""
import copy

import pytest
from pydantic import ValidationError

from helpers import guide
from stratacaster.schemas import FilteredEvidence, MsrInput, PmoApprovalEvent


def test_guide_examples_validate():
    MsrInput.model_validate(guide("msr_input_example.json"))
    FilteredEvidence.model_validate(guide("evidence_v1_example.json"))
    PmoApprovalEvent.model_validate(guide("pmo_approval_001.json"))
    PmoApprovalEvent.model_validate(guide("pmo_approval_002.json"))


def test_guide_evidence_example_passes_reference_fixture(env):
    rec = FilteredEvidence.model_validate(guide("evidence_v1_example.json"))
    assert env.registry.evidence_reference_errors(rec) == []


def test_raw_narrative_in_filtered_object_is_rejected():
    data = guide("evidence_v1_example.json") | {"raw_narrative": "copied source text"}
    with pytest.raises(ValidationError, match="raw_narrative"):
        FilteredEvidence.model_validate(data)


@pytest.mark.parametrize("field", ["source_locator", "notes"])
def test_other_delivery_only_fields_rejected(field):
    data = guide("evidence_v1_example.json")
    data["source_ref"][field] = "x"
    with pytest.raises(ValidationError):
        FilteredEvidence.model_validate(data)


def test_input_version_2_with_review_on_version_1_is_rejected():
    data = guide("msr_input_example.json") | {"input_version": 2}
    with pytest.raises(ValidationError, match="review references input version 1"):
        MsrInput.model_validate(data)


def test_evidence_referencing_input_v2_while_review_on_v1_is_rejected():
    data = guide("evidence_v1_example.json")
    data["source_ref"]["input_version"] = 2
    with pytest.raises(ValidationError, match="employee review must reference"):
        FilteredEvidence.model_validate(data)


def test_unknown_req_task_fails_reference_validation(env):
    rec = FilteredEvidence.model_validate(guide("evidence_v1_example.json") | {"req_task_id": "SYN-REQ-INVENTED-999"})
    assert any("unknown req_task_id" in e for e in env.registry.evidence_reference_errors(rec))


def test_missing_required_field_rejected():
    data = guide("msr_input_example.json")
    del data["subject_id"]
    with pytest.raises(ValidationError, match="subject_id"):
        MsrInput.model_validate(data)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        MsrInput.model_validate(guide("msr_input_example.json") | {"extra": 1})


@pytest.mark.parametrize("ts", ["2026-10-16T14:00:00", "2026-10-16 14:00:00Z", "not-a-date"])
def test_timestamps_require_strict_utc(ts):
    with pytest.raises(ValidationError):
        MsrInput.model_validate(guide("msr_input_example.json") | {"source_created_at": ts})


def test_period_end_before_start_rejected():
    with pytest.raises(ValidationError, match="period_end"):
        MsrInput.model_validate(guide("msr_input_example.json") | {"period_end": "2026-09-01"})


def test_pending_review_must_use_nulls():
    data = guide("msr_input_example.json")
    data["review"] = {"status": "pending", "reviewer_id": "X", "reviewed_input_version": None, "reviewed_at": None}
    with pytest.raises(ValidationError, match="pending review"):
        MsrInput.model_validate(data)


def test_wildcard_audience_rejected():
    with pytest.raises(ValidationError, match="wildcard"):
        FilteredEvidence.model_validate(guide("evidence_v1_example.json") | {"audience_ids": ["*"]})


def test_empty_audience_rejected():
    with pytest.raises(ValidationError):
        FilteredEvidence.model_validate(guide("evidence_v1_example.json") | {"audience_ids": []})


def test_published_requires_timestamp_not_before_approval():
    data = guide("evidence_v1_example.json")
    with pytest.raises(ValidationError):
        FilteredEvidence.model_validate(data | {"published_at": None})
    with pytest.raises(ValidationError, match="must not precede"):
        FilteredEvidence.model_validate(data | {"published_at": "2026-10-16T16:00:00Z"})


def test_approved_requires_approver_fields():
    data = copy.deepcopy(guide("evidence_v1_example.json"))
    data["pmo_approval"]["approver_id"] = None
    with pytest.raises(ValidationError):
        FilteredEvidence.model_validate(data)


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        FilteredEvidence.model_validate(guide("evidence_v1_example.json") | {"confidence": 1.5})
    FilteredEvidence.model_validate(guide("evidence_v1_example.json") | {"confidence": 0.5})


def test_expiration_must_follow_verification():
    data = guide("evidence_v1_example.json")
    data["freshness"]["expires_at"] = data["freshness"]["verified_at"]
    with pytest.raises(ValidationError, match="expires_at"):
        FilteredEvidence.model_validate(data)
