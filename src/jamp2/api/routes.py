"""Evidence API routes with explicit fail-closed error mapping."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC
from typing import Protocol

from fastapi import APIRouter, HTTPException, status

from jamp2.api.schemas import EvidenceCreate
from jamp2.evidence.digest import compute_record_hash
from jamp2.evidence.model import EvidenceRecord
from jamp2.ledger.postgres import DuplicateEvidence, IntegrityViolation, LedgerUnavailable


class EvidenceLedger(Protocol):
    async def append(self, record: EvidenceRecord) -> EvidenceRecord: ...
    async def get(self, evidence_id: str) -> EvidenceRecord | None: ...


router = APIRouter(prefix="/v1/evidence", tags=["evidence"])


def _serialize(record: EvidenceRecord) -> dict[str, object]:
    return {
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
        "record_hash": compute_record_hash(record),
    }


def _raise_ledger_error(exc: Exception) -> None:
    if isinstance(exc, DuplicateEvidence):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "EVIDENCE_ALREADY_EXISTS"},
        ) from exc
    if isinstance(exc, LedgerUnavailable):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "LEDGER_UNAVAILABLE"},
        ) from exc
    if isinstance(exc, IntegrityViolation):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "INTEGRITY_VIOLATION"},
        ) from exc
    raise exc


def build_router(ledger: EvidenceLedger) -> APIRouter:
    """Bind API routes to a ledger implementation."""

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_evidence(payload: EvidenceCreate) -> dict[str, object]:
        try:
            raw_body = base64.b64decode(payload.raw_body_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"error": "INVALID_RAW_BODY_BASE64"}) from exc
        captured_at = payload.captured_at_utc
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail={"error": "CAPTURE_TIME_MUST_BE_UTC"})
        if captured_at.utcoffset().total_seconds() != 0:
            raise HTTPException(status_code=422, detail={"error": "CAPTURE_TIME_MUST_BE_UTC"})
        record = EvidenceRecord(
            evidence_id=payload.evidence_id,
            claim_id=payload.claim_id,
            provider=payload.provider,
            url=payload.url,
            http_status=payload.http_status,
            raw_body_bytes=raw_body,
            raw_sha256=payload.raw_sha256,
            captured_at_utc=captured_at.astimezone(UTC),
            parsed_value=payload.parsed_value,
            verdict_status=payload.verdict_status,
            parent_hash=None,
        )
        try:
            stored = await ledger.append(record)
        except (DuplicateEvidence, IntegrityViolation, LedgerUnavailable) as exc:
            _raise_ledger_error(exc)
        return _serialize(stored)

    @router.get("/{evidence_id}")
    async def read_evidence(evidence_id: str) -> dict[str, object]:
        try:
            record = await ledger.get(evidence_id)
        except (DuplicateEvidence, IntegrityViolation, LedgerUnavailable) as exc:
            _raise_ledger_error(exc)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "EVIDENCE_NOT_FOUND"},
            )
        return _serialize(record)

    return router
