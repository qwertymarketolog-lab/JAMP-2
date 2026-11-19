"""Append-only in-memory ledger with a verifiable SHA-256 hash chain."""

from __future__ import annotations

from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord


class InMemoryLedger:
    """Store immutable evidence records in insertion order."""

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []
        self._evidence_ids: set[str] = set()

    def append(self, record: EvidenceRecord) -> EvidenceRecord:
        """Append a record only when its identity, raw digest, and parent hash are valid."""
        if record.evidence_id in self._evidence_ids:
            raise ValueError(f"Duplicate evidence_id: {record.evidence_id}")
        if compute_sha256(record.raw_body_bytes) != record.raw_sha256:
            raise ValueError("raw_sha256 does not match raw_body_bytes")
        expected_parent = (
            compute_record_hash(self._records[-1]) if self._records else None
        )
        if record.parent_hash != expected_parent:
            raise ValueError("parent_hash does not match the current ledger tip")
        self._records.append(record)
        self._evidence_ids.add(record.evidence_id)
        return record

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        """Return one record by its unique evidence identifier."""
        for record in self._records:
            if record.evidence_id == evidence_id:
                return record
        return None

    def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        """Return records for a claim in append order."""
        return [record for record in self._records if record.claim_id == claim_id]

    def verify_chain(self) -> bool:
        """Verify raw-byte digests, unique identities, and every parent link."""
        seen_ids: set[str] = set()
        expected_parent: str | None = None
        for record in self._records:
            if record.evidence_id in seen_ids:
                return False
            if compute_sha256(record.raw_body_bytes) != record.raw_sha256:
                return False
            if record.parent_hash != expected_parent:
                return False
            seen_ids.add(record.evidence_id)
            expected_parent = compute_record_hash(record)
        return seen_ids == self._evidence_ids
