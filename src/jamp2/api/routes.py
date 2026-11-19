"""Evidence API routes with explicit fail-closed error mapping."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC
from typing import Protocol

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from jamp2.api.schemas import EvidenceCreate
from jamp2.evidence.digest import compute_record_hash
from jamp2.evidence.model import EvidenceRecord
from jamp2.ledger.postgres import DuplicateEvidence, IntegrityViolation, LedgerUnavailable


class EvidenceLedger(Protocol):
    async def append(self, record: EvidenceRecord) -> EvidenceRecord: ...
    async def get(self, evidence_id: str) -> EvidenceRecord | None: ...


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


def _ledger_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DuplicateEvidence):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "EVIDENCE_ALREADY_EXISTS"},
        )
    if isinstance(exc, LedgerUnavailable):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": "LEDGER_UNAVAILABLE"},
        )
    if isinstance(exc, IntegrityViolation):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "INTEGRITY_VIOLATION"},
        )
    raise exc


def build_router(ledger: EvidenceLedger) -> APIRouter:
    """Bind a fresh router instance to one ledger implementation."""
    router = APIRouter(prefix="/v1/evidence", tags=["evidence"])

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_evidence(
        payload: EvidenceCreate,
    ) -> dict[str, object] | JSONResponse:
        try:
            raw_body = base64.b64decode(payload.raw_body_base64, validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse(
                status_code=422,
                content={"error": "INVALID_RAW_BODY_BASE64"},
            )
        captured_at = payload.captured_at_utc
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            return JSONResponse(
                status_code=422,
                content={"error": "CAPTURE_TIME_MUST_BE_UTC"},
            )
        if captured_at.utcoffset().total_seconds() != 0:
            return JSONResponse(
                status_code=422,
                content={"error": "CAPTURE_TIME_MUST_BE_UTC"},
            )
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
            return _ledger_error(exc)
        return _serialize(stored)

    @router.get("/{evidence_id}")
    async def read_evidence(evidence_id: str) -> dict[str, object] | JSONResponse:
        try:
            record = await ledger.get(evidence_id)
        except (DuplicateEvidence, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        if record is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "EVIDENCE_NOT_FOUND"},
            )
        return _serialize(record)

    return router
