"""Contract tests for immutable versioned VerdictRecord values."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jamp2.evidence.digest import compute_verdict_hash
from jamp2.evidence.verdict import VerdictRecord


def make_verdict() -> VerdictRecord:
    return VerdictRecord(
        verdict_id="verdict-1",
        claim_id="claim-1",
        status="VERIFIED",
        evidence_ids=("evidence-1", "evidence-2"),
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="commit-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Source observation matched the claim under the recorded policy.",
    )


def test_verdict_requires_evidence_and_immutable_version_references() -> None:
    record = make_verdict()
    assert record.status == "VERIFIED"
    assert record.evidence_ids == ("evidence-1", "evidence-2")
    assert record.policy_version == "provider-defaults-v1"
    assert record.code_version == "commit-sha"
    assert record.canonicalization_version == "jamp-json-nfc-v1"


def test_verdict_hash_binds_evidence_and_policy_versions() -> None:
    original = make_verdict()
    assert compute_verdict_hash(original) == compute_verdict_hash(original)
    assert compute_verdict_hash(original) != compute_verdict_hash(
        replace(original, evidence_ids=("evidence-1",))
    )
    assert compute_verdict_hash(original) != compute_verdict_hash(
        replace(original, policy_version="provider-defaults-v2")
    )
    assert compute_verdict_hash(original) != compute_verdict_hash(
        replace(original, code_version="other-commit")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "PASS"},
        {"evidence_ids": ()},
        {"evidence_ids": ("evidence-1", "evidence-1")},
        {"policy_version": ""},
        {"code_version": ""},
        {"canonicalization_version": ""},
        {"reason_codes": ()},
        {"parent_hash": ""},
    ],
)
def test_verdict_rejects_incomplete_or_invalid_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(make_verdict(), **changes)


def test_ambiguous_is_a_first_class_verdict_status() -> None:
    assert replace(make_verdict(), status="AMBIGUOUS").status == "AMBIGUOUS"
