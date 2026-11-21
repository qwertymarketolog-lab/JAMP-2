"""Authenticated claim verification routes with server-side provider acquisition."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from jamp2.api.schemas import ClaimVerifyRequest
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.canonicalize import CANONICALIZATION_VERSION, canonicalize
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord, JSONValue
from jamp2.evidence.multisource import MultiSourcePolicy, SourceSpec, arbitrate
from jamp2.evidence.replay import replay_verdict
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import (
    DuplicateEvidence,
    DuplicateVerdict,
    IntegrityViolation,
    LedgerUnavailable,
)
from jamp2.providers.base import Provider
from jamp2.providers.coingecko import CoinGeckoProvider
from jamp2.providers.moex import MoexProvider


class EvidenceLedger(Protocol):
    async def append(self, record: EvidenceRecord) -> EvidenceRecord: ...
    async def get(self, evidence_id: str) -> EvidenceRecord | None: ...
    async def append_verdict(self, record: VerdictRecord) -> VerdictRecord: ...
    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None: ...
    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]: ...
    async def begin_idempotency(
        self, *, key_hash: str, request_digest: str, claim_id: str
    ) -> tuple[str, str | None, dict[str, Any] | None]: ...
    async def complete_idempotency(
        self, *, key_hash: str, request_digest: str, verdict_id: str,
        response_payload: dict[str, Any], owner_token: str
    ) -> None: ...
    async def fail_idempotency(
        self, *, key_hash: str, request_digest: str, owner_token: str
    ) -> None: ...


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
        "source_timestamp": record.source_timestamp,
        "body_complete": record.body_complete,
        "provider_adapter_version": record.provider_adapter_version,
        "parser_version": record.parser_version,
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
) -> tuple[str, str, str, str] | None:
    """Resolve only explicit supported subject kinds to fixed provider identities."""
    if subject_kind == "equity_security" and unit == "RUB":
        return "moex", "moex.iss", "iss.moex.com", "moex-iss-adapter-v1"
    if subject_kind == "crypto_asset" and unit == "USD":
        return "coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1"
    return None


def _evidence_record(
    *,
    claim_id: str,
    provider_id: str,
    allowed_host: str,
    adapter_version: str,
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
        source_timestamp=None,
        body_complete=body_complete,
        provider_adapter_version=adapter_version,
        parser_version="jamp-provider-json-v1",
    )


def build_router(
    ledger: EvidenceLedger,
    *,
    api_key: str | None,
    providers: dict[str, Provider] | None = None,
    code_version: str | None = None,
    policies: dict[tuple[str, str], MultiSourcePolicy] | None = None,
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
    policy_map = policies or {
        ("equity_security", "RUB"): MultiSourcePolicy(
            "jamp2.provider-price-comparison",
            "provider-defaults-v1",
            (SourceSpec("moex", "moex.iss", "iss.moex.com", "moex-iss-adapter-v1"),),
        ),
        ("crypto_asset", "USD"): MultiSourcePolicy(
            "jamp2.provider-price-comparison",
            "provider-defaults-v1",
            (SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1"),),
        ),
    }
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
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object] | JSONResponse:
        policy = policy_map.get((payload.subject.kind, payload.unit))
        if policy is None:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": "UNSUPPORTED_SUBJECT_UNIT_COMBINATION"},
            )
        if not immutable_code_version:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "CODE_VERSION_NOT_CONFIGURED"},
            )
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "IDEMPOTENCY_KEY_REQUIRED"},
            )
        key_hash = compute_sha256(idempotency_key.encode("utf-8"))
        request_digest = compute_sha256(
            canonicalize(cast(JSONValue, payload.model_dump(mode="json")))
        )
        claim_id = str(uuid.uuid4())
        try:
            reservation = await ledger.begin_idempotency(
                key_hash=key_hash,
                request_digest=request_digest,
                claim_id=claim_id,
            )
            if len(reservation) == 2:  # Compatibility for existing in-memory test ledgers.
                idempotency_state, cached_response = reservation
                owner_token = None
            else:
                idempotency_state, owner_token, cached_response = reservation
        except LedgerUnavailable as exc:
            return _ledger_error(exc)
        if idempotency_state == "COMPLETED":
            if cached_response is None:
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={"error": "IDEMPOTENCY_RESPONSE_MISSING"},
                )
            return cached_response
        if idempotency_state == "KEY_REUSED":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "IDEMPOTENCY_KEY_REUSED"},
            )
        if idempotency_state == "IN_PROGRESS":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "IDEMPOTENCY_REQUEST_IN_PROGRESS"},
            )
        if idempotency_state == "FAILED":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "IDEMPOTENCY_REQUEST_FAILED"},
            )
        if idempotency_state != "NEW":
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "IDEMPOTENCY_STATE_INVALID"},
            )

        async def mark_failed() -> JSONResponse | None:
            try:
                await ledger.fail_idempotency(
                    key_hash=key_hash,
                    request_digest=request_digest,
                    **({"owner_token": owner_token} if owner_token is not None else {}),
                )
            except (LedgerUnavailable, IntegrityViolation) as exc:
                return _ledger_error(exc)
            return None

        claim = NormalizedClaim(
            text=payload.text,
            subject_kind=payload.subject.kind,
            subject_identifier=payload.subject.identifier,
            predicate=payload.predicate,
            expected_value=payload.expected_value,
            unit=payload.unit,
        )
        try:
            arbitration = await arbitrate(claim, policy, provider_map)
        except Exception:
            failure_response = await mark_failed()
            if failure_response is not None:
                return failure_response
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"error": "SOURCE_ARBITRATION_FAILED"},
            )

        claim_snapshot: dict[str, Any] = {
            "text": claim.text,
            "subject_kind": claim.subject_kind,
            "subject_identifier": claim.subject_identifier,
            "predicate": claim.predicate,
            "expected_value": claim.expected_value,
            "unit": claim.unit,
        }
        records: list[EvidenceRecord] = []
        try:
            for outcome in arbitration.outcomes:
                source_payload: dict[str, Any] = {
                    "claim": claim_snapshot,
                    "source_status": outcome.status,
                    "source_details": outcome.details,
                    "provider_result": outcome.details,
                    "trade_date": outcome.details.get("trade_date"),
                }
                if outcome.raw_evidence:
                    for observation in outcome.raw_evidence:
                        record = _evidence_record(
                            claim_id=claim_id,
                            provider_id=outcome.source.provider_id,
                            allowed_host=outcome.source.allowed_host,
                            adapter_version=outcome.source.adapter_version,
                            observation=observation,
                            parsed_value=source_payload,
                            verdict_status=(
                                "SOURCE_OBSERVATION_CAPTURED"
                                if outcome.observation.usable
                                else "SOURCE_RESPONSE_CAPTURED"
                            ),
                        )
                        records.append(await ledger.append(record))
                else:
                    # A failed/no-response attempt is still a durable ledger observation.
                    empty_body = b""
                    record = EvidenceRecord(
                        evidence_id=str(uuid.uuid4()),
                        claim_id=claim_id,
                        provider=outcome.source.provider_id,
                        url=f"https://{outcome.source.allowed_host}/",
                        http_status=None,
                        raw_body_bytes=empty_body,
                        raw_sha256=compute_sha256(empty_body),
                        captured_at_utc=datetime.now(UTC),
                        parsed_value=source_payload,
                        verdict_status="SOURCE_FAILURE",
                        parent_hash=None,
                        body_complete=False,
                        provider_adapter_version=outcome.source.adapter_version,
                        parser_version="jamp-provider-json-v1",
                    )
                    records.append(await ledger.append(record))
        except (DuplicateEvidence, IntegrityViolation, LedgerUnavailable) as exc:
            failure_response = await mark_failed()
            if failure_response is not None:
                return failure_response
            return _ledger_error(exc)

        verdict_record = VerdictRecord(
            verdict_id=str(uuid.uuid4()),
            claim_id=claim_id,
            status=arbitration.verdict_status,
            evidence_ids=tuple(record.evidence_id for record in records),
            policy_id=policy.policy_id,
            policy_version=policy.version,
            code_version=immutable_code_version,
            canonicalization_version=CANONICALIZATION_VERSION,
            decided_at_utc=datetime.now(UTC),
            reason_codes=arbitration.reason_codes,
            explanation=(
                f"Required-source arbitration returned {arbitration.verdict_status}; "
                f"{len(arbitration.outcomes)} required source outcome(s) are linked "
                "to the immutable evidence ledger."
            ),
        )
        try:
            stored_verdict = await ledger.append_verdict(verdict_record)
        except (DuplicateVerdict, IntegrityViolation, LedgerUnavailable) as exc:
            failure_response = await mark_failed()
            if failure_response is not None:
                return failure_response
            return _ledger_error(exc)
        response_payload: dict[str, Any] = {
            "claim_id": claim_id,
            "verdict_id": stored_verdict.verdict_id,
            "verdict": stored_verdict.status,
            "provider_status": (
                arbitration.outcomes[0].status
                if len(arbitration.outcomes) == 1
                else {
                    outcome.source.provider_id: outcome.status
                    for outcome in arbitration.outcomes
                }
            ),
            "provider": (
                arbitration.outcomes[0].source.provider_id
                if len(arbitration.outcomes) == 1
                else [outcome.source.provider_id for outcome in arbitration.outcomes]
            ),
            "evidence_ids": [record.evidence_id for record in records],
            "evidence_count": len(records),
            "details": (
                arbitration.outcomes[0].details
                if len(arbitration.outcomes) == 1
                else {
                    outcome.source.provider_id: outcome.details
                    for outcome in arbitration.outcomes
                }
            ),
            "arbitration": {
                "verdict": arbitration.verdict_status,
                "reason_codes": list(arbitration.reason_codes),
                "reducer": arbitration.reducer,
                "required_sources": [
                    outcome.source.provider_id for outcome in arbitration.outcomes
                ],
            },
        }
        try:
            await ledger.complete_idempotency(
                key_hash=key_hash,
                request_digest=request_digest,
                verdict_id=stored_verdict.verdict_id,
                response_payload=response_payload,
                **({"owner_token": owner_token} if owner_token is not None else {}),
            )
        except (IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        return response_payload

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

    @router.post("/verdicts/{verdict_id}/replay", response_model=None)
    async def replay_recorded_verdict(
        verdict_id: str,
        _: None = Depends(require_api_key),
    ) -> dict[str, object] | JSONResponse:
        try:
            verdict_record = await ledger.get_verdict(verdict_id)
            if verdict_record is None:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={"error": "VERDICT_NOT_FOUND"},
                )
            claim_records = await ledger.list_by_claim(verdict_record.claim_id)
        except (DuplicateEvidence, DuplicateVerdict, IntegrityViolation, LedgerUnavailable) as exc:
            return _ledger_error(exc)
        evidence_by_id = {record.evidence_id: record for record in claim_records}
        selected_records = [
            evidence_by_id[evidence_id]
            for evidence_id in verdict_record.evidence_ids
            if evidence_id in evidence_by_id
        ]
        return replay_verdict(verdict_record, selected_records)

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
