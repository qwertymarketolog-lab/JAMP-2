"""SHA-256 digests for raw bytes and complete evidence records."""

from __future__ import annotations

import base64
import hashlib

from jamp2.evidence.canonicalize import canonicalize
from jamp2.evidence.model import EvidenceRecord, JSONValue
from jamp2.evidence.verdict import VerdictRecord


def compute_sha256(value: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of exact bytes."""
    return hashlib.sha256(value).hexdigest()


def compute_record_hash(record: EvidenceRecord) -> str:
    """Hash every record field, including raw bytes and the preceding chain hash."""
    payload = {
        "evidence_id": record.evidence_id,
        "claim_id": record.claim_id,
        "provider": record.provider,
        "url": record.url,
        "http_status": record.http_status,
        "raw_body_base64": base64.b64encode(record.raw_body_bytes).decode("ascii"),
        "raw_sha256": record.raw_sha256,
        "captured_at_utc": record.captured_at_utc.isoformat(),
        "parsed_value": record.parsed_value,
        "verdict_status": record.verdict_status,
        "parent_hash": record.parent_hash,
    }
    return compute_sha256(canonicalize(payload))


def compute_verdict_hash(record: VerdictRecord) -> str:
    """Hash every immutable verdict field and its preceding verdict-chain hash."""
    payload: JSONValue = {
        "verdict_id": record.verdict_id,
        "claim_id": record.claim_id,
        "status": record.status,
        "evidence_ids": list(record.evidence_ids),
        "policy_id": record.policy_id,
        "policy_version": record.policy_version,
        "code_version": record.code_version,
        "canonicalization_version": record.canonicalization_version,
        "decided_at_utc": record.decided_at_utc.isoformat(),
        "reason_codes": list(record.reason_codes),
        "explanation": record.explanation,
        "parent_hash": record.parent_hash,
    }
    return compute_sha256(canonicalize(payload))
