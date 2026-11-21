"""Immutable, independently versioned verdict records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

VERDICT_STATUSES = frozenset(
    {"VERIFIED", "CONTRADICTED", "INCONCLUSIVE", "OUT_OF_SCOPE", "AMBIGUOUS"}
)


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """Decision record bound to exact evidence and versioned policy/code."""

    verdict_id: str
    claim_id: str
    status: str
    evidence_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    code_version: str
    canonicalization_version: str
    decided_at_utc: datetime
    reason_codes: tuple[str, ...]
    explanation: str
    parent_hash: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "verdict_id",
            "claim_id",
            "policy_id",
            "policy_version",
            "code_version",
            "canonicalization_version",
            "explanation",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.status not in VERDICT_STATUSES:
            raise ValueError("Unsupported verdict status")
        if (
            not isinstance(self.evidence_ids, tuple)
            or not self.evidence_ids
            or any(not isinstance(item, str) or not item.strip() for item in self.evidence_ids)
            or len(set(self.evidence_ids)) != len(self.evidence_ids)
        ):
            raise ValueError("evidence_ids must be a non-empty tuple of unique identifiers")
        if (
            not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or any(not isinstance(item, str) or not item.strip() for item in self.reason_codes)
        ):
            raise ValueError("reason_codes must be a non-empty tuple of strings")
        if not isinstance(self.decided_at_utc, datetime):
            raise ValueError("decided_at_utc must be a datetime")
        if (
            self.decided_at_utc.tzinfo is None
            or self.decided_at_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("decided_at_utc must be timezone-aware UTC")
        if self.parent_hash is not None and (
            not isinstance(self.parent_hash, str) or not self.parent_hash
        ):
            raise ValueError("parent_hash must be a non-empty hash or null")
