"""Authenticated claim verification routes with server-side provider acquisition."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from jamp2.api.schemas import ClaimVerifyRequest
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.canonicalize import CANONICALIZATION_VERSION
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import (
    DuplicateEvidence,
    DuplicateVerdict,
    IntegrityViolation,
    LedgerUnavailable,
)
from jamp2.providers.base import Provider, ProviderResult
from jamp2.providers.coingecko import CoinGeckoProvider
from jamp2.providers.moex import MoexProvider


class EvidenceLedger(Protocol):
    async def append(self, record: EvidenceRecord) -> EvidenceRecord: ...
    async def get(self, evidence_id: str) -> EvidenceRecord | None: ...
    async def append_verdict(self, record: VerdictRecord) -> VerdictRecord: ...
    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None: ...


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
        "canonicalization_version": record.canonicalization_version,
        "parent_hash": record.parent_hash,
        "record_hash": compute_record_hash(record),
    }


def _ledger_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DuplicateEvidence):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "EVIDENCE_ALREADY_EXISTS"},
        )
    if isinstance(exc, DuplicateVerdict):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "VERDICT_ALREADY_EXISTS"},
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


def _provider_configuration(
    subject_kind: str, unit: str
) -> tuple[str, str, str] | None:
    """Resolve only explicit supported subject kinds to fixed provider identities."""
    if subject_kind == "equity_security" and unit == "RUB":
        return "moex", "moex.iss", "iss.moex.com"
    if subject_kind == "crypto_asset" and unit == "USD":
        return "coingecko", "coingecko", "api.coingecko.com"
    return None


def _evidence_record(
    *,
    claim_id: str,
    provider_id: str,
    allowed_host: str,
    observation: dict[str, object],
    parsed_value: dict[str, Any],
    verdict_status: str,
) -> EvidenceRecord:
    """Validate adapter capture metadata and construct a server-owned record."""
    url = observation.get("url")
    source = observation.get("source")
    body_encoded = observation.get("raw_body_base64")
    source_digest = observation.get("sha256_body")
    http_status = observation.get("status_code")
    body_complete = observation.get("body_complete")
    if (
        not isinstance(url, str)
        or not isinstance(source, str)
        or not isinstance(body_encoded, str)
        or not isinstance(source_digest, str)
        or not isinstance(http_status, int)
        or not isinstance(body_complete, bool)
        or not body_complete
    ):
        raise IntegrityViolation("provider returned incomplete evidence metadata")
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != allowed_host:
        raise IntegrityViolation("provider returned a URL outside its fixed source host")
    try:
        raw_body = base64.b64decode(body_encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityViolation("provider returned invalid base64 evidence") from exc
    actual_digest = compute_sha256(raw_body)
    if not hmac.compare_digest(actual_digest, source_digest):
        raise IntegrityViolation("provider raw response digest mismatch")
    if source != provider_id:
        raise IntegrityViolation("provider identity mismatch")
    return EvidenceRecord(
        evidence_id=str(uuid.uuid4()),
        claim_id=claim_id,
        provider=provider_id,
        url=url,
        http_status=http_status,
        raw_body_bytes=raw_body,
        raw_sha256=actual_digest,
        captured_at_utc=datetime.now(UTC),
        parsed_value=parsed_value,
        verdict_status=verdict_status,
        parent_hash=None,
    )


def build_router(
    ledger: EvidenceLedger,
    *,
    api_key: str | None,
    providers: dict[str, Provider] | None = None,
    code_version: str | None = None,
) -> APIRouter:
    """Bind authenticated claim verification to a ledger and fixed provider adapters."""
    configured_key = api_key if api_key is not None else os.getenv("JAMP2_API_KEY")
    immutable_code_version = (
        code_version or os.getenv("JAMP2_CODE_VERSION") or os.getenv("GITHUB_SHA")
    )
    provider_map: dict[str, Provider] = (
        providers
        if providers is not None
        else {"moex": MoexProvider(), "coingecko": CoinGeckoProvider()}
    )
    router = APIRouter(prefix="/v1", tags=["trust-layer"])

    async def require_api_key(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        if not configured_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "API_KEY_NOT_CONFIGURED"},
            )
        if not isinstance(x_api_key, str) or not hmac.compare_digest(
            x_api_key.encode("utf-8"), configured_key.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "UNAUTHORIZED"},
            )

    @router.post("/claims/verify", status_code=status.HTTP_201_CREATED, response_model=None)
    async def verify_claim(
        payload: ClaimVerifyRequest,
        _: None = Depends(require_api_key),
    ) -> dict[str, object] | JSONResponse:
        configuration = _provider_configuration(payload.subject.kind, payload.unit)
        if configuration is None:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": "UNSUPPORTED_SUBJECT_UNIT_COMBINATION"},
            )
        provider_key, provider_id, allowed_host = configuration
        if not immutable_code_version:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "CODE_VERSION_NOT_CONFIGURED"},
            )
        provider = provider_map.get(provider_key)
        if provider is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "PROVIDER_NOT_CONFIGURED"},
            )
        claim_id = str(uuid.uuid4())
        claim = NormalizedClaim(
            text=payload.text,
            subject_kind=payload.subject.kind,
            subject_identifier=payload.subject.identifier,
            predicate=payload.predicate,
            expected_value=payload.expected_value,
            unit=payload.unit,
        )
        try:
            result: ProviderResult = await run_in_threadpool(provider.verify_claim, claim)
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"error": "PROVIDER_VERIFICATION_FAILED"},
            )
        status_map = {
            "E4_VERIFIED": "VERIFIED",
            "E4_CONTRADICTED": "CONTRADICTED",
            "E4_OUT_OF_SCOPE": "OUT_OF_SCOPE",
            "E4_INCONCLUSIVE": "INCONCLUSIVE",
        }
        verdict = status_map.get(result.status, "INCONCLUSIVE")
        if not result.raw_evidence:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "error": "SOURCE_EVIDENCE_NOT_CAPTURED",
                    "provider_status": result.status,
                },
            )
        records: list[EvidenceRecord] = []
        try:
            for observation in result.raw_evidence:
                record = _evidence_record(
                    claim_id=claim_id,
                    provider_id=provider_id,
                    allowed_host=allowed_host,
                    observation=observation,
                    parsed_value=result.details,
                    verdict_status="OBSERVATION_CAPTURED",
                )
                records.append(await ledger.append(record))
        except (DuplicateEvidence, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        reason = result.details.get("reason")
        reason_code = reason if isinstance(reason, str) and reason else result.status
        verdict_record = VerdictRecord(
            verdict_id=str(uuid.uuid4()),
            claim_id=claim_id,
            status=verdict,
            evidence_ids=tuple(record.evidence_id for record in records),
            policy_id="jamp2.provider-price-comparison",
            policy_version="provider-defaults-v1",
            code_version=immutable_code_version,
            canonicalization_version=CANONICALIZATION_VERSION,
            decided_at_utc=datetime.now(UTC),
            reason_codes=(reason_code,),
            explanation=(
                f"{provider_id} provider comparison returned {result.status}; "
                "see linked raw evidence and version metadata"
            ),
        )
        try:
            stored_verdict = await ledger.append_verdict(verdict_record)
        except (DuplicateVerdict, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        return {
            "claim_id": claim_id,
            "verdict_id": stored_verdict.verdict_id,
            "verdict": stored_verdict.status,
            "provider_status": result.status,
            "provider": provider_id,
            "evidence_ids": [record.evidence_id for record in records],
            "evidence_count": len(records),
            "details": result.details,
        }

    @router.get("/verdicts/{verdict_id}", response_model=None)
    async def read_verdict(
        verdict_id: str,
        _: None = Depends(require_api_key),
    ) -> dict[str, object] | JSONResponse:
        try:
            record = await ledger.get_verdict(verdict_id)
        except (DuplicateEvidence, DuplicateVerdict, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        if record is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "VERDICT_NOT_FOUND"},
            )
        return {
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

    @router.get("/evidence/{evidence_id}", response_model=None)
    async def read_evidence(
        evidence_id: str,
        _: None = Depends(require_api_key),
    ) -> dict[str, object] | JSONResponse:
        try:
            record = await ledger.get(evidence_id)
        except (DuplicateEvidence, DuplicateVerdict, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        if record is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "EVIDENCE_NOT_FOUND"},
            )
        return _serialize(record)

    return router
