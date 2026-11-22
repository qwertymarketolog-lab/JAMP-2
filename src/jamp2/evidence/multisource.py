"""Evidence-first provider acquisition and deterministic source arbitration."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.conflict import SourceObservation, detect_conflicts
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation
from jamp2.evidence.reason_codes import EVIDENCE_LEDGER_REQUIRED
from jamp2.evidence.policy import (
    SourceObservationInput,
    SourcePolicyRecord,
    evaluate_source_policy,
)
from jamp2.providers.base import Provider, ProviderResult


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """A required provider identity and its fixed evidence boundary."""

    provider_key: str
    provider_id: str
    allowed_host: str
    adapter_version: str

    def __post_init__(self) -> None:
        for name in ("provider_key", "provider_id", "allowed_host", "adapter_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class MultiSourcePolicy:
    """Versioned all-required-source policy; exact comparison is explicit."""

    policy_id: str
    version: str
    required_sources: tuple[SourceSpec, ...]
    timeout_seconds: float = 5.0
    comparison_mode: str = "exact-v1"
    source_policy: SourcePolicyRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id is required")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version is required")
        if not self.required_sources:
            raise ValueError("at least one required source is mandatory")
        keys = [source.provider_key for source in self.required_sources]
        ids = [source.provider_id for source in self.required_sources]
        if len(set(keys)) != len(keys) or len(set(ids)) != len(ids):
            raise ValueError("required provider keys and identities must be unique")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if self.comparison_mode != "exact-v1":
            raise ValueError("only the explicitly versioned exact-v1 comparison is supported")
        if self.source_policy is not None and set(self.source_policy.required_sources) != set(ids):
            raise ValueError("source policy required_sources must match the orchestrator policy")


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """One required source outcome, including failures and captured raw responses."""

    source: SourceSpec
    status: str
    details: dict[str, Any]
    raw_evidence: list[dict[str, Any]]
    observation: SourceObservation


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    """Deterministic aggregate result before immutable ledger persistence."""

    verdict_status: str
    reducer: dict[str, Any]
    outcomes: tuple[SourceOutcome, ...]
    reason_codes: tuple[str, ...]
    verification_mode: str | None = None
    independent_cross_check: bool | None = None


def _numeric_value(details: dict[str, Any]) -> int | float | str | None:
    value = details.get("observed_last", details.get("observed_value"))
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(number) and number > 0 else None


async def _fetch_one(
    source: SourceSpec,
    provider: Provider | None,
    claim: NormalizedClaim,
    timeout_seconds: float,
) -> SourceOutcome:
    if provider is None:
        status = "PROVIDER_NOT_CONFIGURED"
        details: dict[str, Any] = {"reason": status}
        raw_evidence: list[dict[str, Any]] = []
    else:
        try:
            result: ProviderResult = await asyncio.wait_for(
                asyncio.to_thread(provider.verify_claim, claim),
                timeout=timeout_seconds,
            )
            status, details, raw_evidence = result.status, result.details, result.raw_evidence
            if not raw_evidence:
                status = "EVIDENCE_NOT_CAPTURED"
                details = {"reason": "SOURCE_EVIDENCE_NOT_CAPTURED"}
            elif any(item.get("body_complete") is not True for item in raw_evidence):
                status = "INCOMPLETE_EVIDENCE"
                details = {"reason": "SOURCE_BODY_INCOMPLETE"}
        except TimeoutError:
            status = "TIMEOUT"
            details = {"reason": "PROVIDER_TIMEOUT"}
            raw_evidence = []
        except Exception as exc:
            status = "PROVIDER_ERROR"
            details = {"reason": "PROVIDER_EXCEPTION", "error_type": type(exc).__name__}
            raw_evidence = []

    value = _numeric_value(details)
    usable = value is not None and status in {"E4_VERIFIED", "E4_CONTRADICTED"}
    observation = SourceObservation(
        source_id=source.provider_id,
        fact_key=f"{claim.subject_kind}:{claim.subject_identifier}:{claim.predicate}",
        value=value if value is not None else 0,
        unit=claim.unit,
        usable=usable,
    )
    return SourceOutcome(source, status, details, raw_evidence, observation)


async def fetch_provider_results(
    claim: NormalizedClaim,
    policy: MultiSourcePolicy,
    providers: dict[str, Provider],
) -> tuple[SourceOutcome, ...]:
    """Fetch every required provider concurrently; no policy or reducer work occurs here."""
    return tuple(
        await asyncio.gather(
            *(
                _fetch_one(
                    source, providers.get(source.provider_key), claim, policy.timeout_seconds
                )
                for source in policy.required_sources
            )
        )
    )


def _source_policy(claim: NormalizedClaim, policy: MultiSourcePolicy) -> SourcePolicyRecord:
    return policy.source_policy or SourcePolicyRecord(
        policy_id=policy.policy_id,
        version=1,
        asset_class=claim.subject_kind,
        required_identifiers=(claim.subject_identifier,),
        market_calendar_required=claim.subject_kind == "equity_security",
        quote_currency=claim.unit,
        price_type="LAST_DEAL" if claim.subject_kind == "equity_security" else "SPOT",
        required_sources=tuple(source.provider_id for source in policy.required_sources),
        independence_group=policy.policy_id,
        max_age_seconds=60,
        max_time_skew_seconds=5,
        max_age_by_source={
            source.provider_id: {"moex.iss": 1200, "coingecko": 300}[source.provider_id]
            for source in policy.required_sources
            if source.provider_id in {"moex.iss", "coingecko"}
        },
        timeout_seconds=policy.timeout_seconds,
        verification_mode=(
            "SINGLE_SOURCE" if len(policy.required_sources) == 1 else "MULTI_SOURCE"
        ),
    )


def _parse_capture_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _verified_source_records(
    claim: NormalizedClaim,
    outcomes: tuple[SourceOutcome, ...],
    evidence_records: Sequence[EvidenceRecord],
) -> dict[str, tuple[EvidenceRecord, ...]]:
    """Bind every provider outcome to hash-verified records returned by the ledger."""
    if not evidence_records:
        raise IntegrityViolation("evidence ledger records are required for arbitration")

    evidence_ids: set[str] = set()
    claim_ids: set[str] = set()
    for record in evidence_records:
        if record.evidence_id in evidence_ids:
            raise IntegrityViolation("duplicate evidence_id in arbitration input")
        evidence_ids.add(record.evidence_id)
        claim_ids.add(record.claim_id)
        if not record.record_hash or not hmac.compare_digest(
            compute_record_hash(record), record.record_hash
        ):
            raise IntegrityViolation("evidence record_hash is missing or invalid")
        if not hmac.compare_digest(compute_sha256(record.raw_body_bytes), record.raw_sha256):
            raise IntegrityViolation("evidence raw_sha256 is invalid")
    if len(claim_ids) != 1:
        raise IntegrityViolation("arbitration evidence must belong to exactly one claim")

    unused = {record.evidence_id: record for record in evidence_records}
    by_source: dict[str, tuple[EvidenceRecord, ...]] = {}
    for outcome in outcomes:
        matched: list[EvidenceRecord] = []
        captures = outcome.raw_evidence
        if captures:
            for capture in captures:
                encoded = capture.get("raw_body_base64")
                digest = capture.get("sha256_body")
                source = capture.get("source")
                url = capture.get("url")
                http_status = capture.get("status_code")
                complete = capture.get("body_complete")
                if (
                    not isinstance(encoded, str)
                    or not isinstance(digest, str)
                    or source != outcome.source.provider_id
                    or not isinstance(url, str)
                    or not isinstance(http_status, int)
                    or isinstance(http_status, bool)
                    or not isinstance(complete, bool)
                ):
                    raise IntegrityViolation("provider capture metadata is incomplete")
                try:
                    raw_body = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise IntegrityViolation("provider capture body is invalid base64") from exc
                actual_digest = compute_sha256(raw_body)
                if not hmac.compare_digest(actual_digest, digest):
                    raise IntegrityViolation("provider capture raw digest mismatch")
                parsed_url = urlsplit(url)
                if parsed_url.scheme != "https" or parsed_url.hostname != outcome.source.allowed_host:
                    raise IntegrityViolation("provider capture URL is outside the fixed source host")
                fetched_at = _parse_capture_time(capture.get("fetched_at"))
                observed_at = _parse_capture_time(capture.get("provider_observed_at"))
                source_date = capture.get("source_date")
                if not isinstance(source_date, str):
                    source_date = None
                expected_verdict = (
                    "SOURCE_OBSERVATION_CAPTURED"
                    if outcome.observation.usable
                    else "SOURCE_RESPONSE_CAPTURED"
                )
                candidates = [
                    record for record in unused.values()
                    if record.provider == outcome.source.provider_id
                    and record.url == url
                    and record.http_status == http_status
                    and record.raw_body_bytes == raw_body
                    and record.raw_sha256 == actual_digest
                    and record.body_complete is complete
                    and record.provider_adapter_version == outcome.source.adapter_version
                    and record.parser_version == "jamp-provider-json-v1"
                    and record.verdict_status == expected_verdict
                    and record.fetched_at == fetched_at
                    and record.provider_observed_at == observed_at
                    and record.source_date == source_date
                    and isinstance(record.parsed_value, dict)
                    and record.parsed_value.get("claim") == {
                        "text": claim.text,
                        "subject_kind": claim.subject_kind,
                        "subject_identifier": claim.subject_identifier,
                        "predicate": claim.predicate,
                        "expected_value": claim.expected_value,
                        "unit": claim.unit,
                    }
                    and record.parsed_value.get("source_status") == outcome.status
                    and record.parsed_value.get("provider_result") == outcome.details
                ]
                if len(candidates) != 1:
                    raise IntegrityViolation(
                        "provider outcome does not match exactly one persisted evidence record"
                    )
                selected = candidates[0]
                unused.pop(selected.evidence_id)
                matched.append(selected)
        else:
            candidates = [
                record for record in unused.values()
                if record.provider == outcome.source.provider_id
                and record.verdict_status == "SOURCE_FAILURE"
                and record.raw_body_bytes == b""
                and record.body_complete is False
                and record.http_status is None
                and record.provider_adapter_version == outcome.source.adapter_version
                and isinstance(record.parsed_value, dict)
                and record.parsed_value.get("claim") == {
                    "text": claim.text,
                    "subject_kind": claim.subject_kind,
                    "subject_identifier": claim.subject_identifier,
                    "predicate": claim.predicate,
                    "expected_value": claim.expected_value,
                    "unit": claim.unit,
                }
                and record.parsed_value.get("source_status") == outcome.status
                and record.parsed_value.get("provider_result") == outcome.details
            ]
            if len(candidates) != 1:
                raise IntegrityViolation(
                    "provider failure does not match exactly one persisted failure record"
                )
            selected = candidates[0]
            unused.pop(selected.evidence_id)
            matched.append(selected)
        if not matched:
            raise IntegrityViolation("required provider has no persisted evidence")
        by_source[outcome.source.provider_id] = tuple(matched)

    if unused:
        raise IntegrityViolation("arbitration received unreferenced evidence records")
    return by_source


def arbitrate_from_evidence(
    claim: NormalizedClaim,
    policy: MultiSourcePolicy,
    outcomes: tuple[SourceOutcome, ...],
    evidence_records: Sequence[EvidenceRecord],
) -> ArbitrationResult:
    """Apply policy only to provider outcomes proven against persisted ledger records."""
    expected_ids = tuple(source.provider_id for source in policy.required_sources)
    actual_ids = tuple(outcome.source.provider_id for outcome in outcomes)
    if actual_ids != expected_ids:
        raise ValueError("outcomes must contain every required source in policy order")
    records_by_source = _verified_source_records(claim, outcomes, evidence_records)

    source_policy = _source_policy(claim, policy)
    policy_inputs: list[SourceObservationInput] = []
    for outcome in outcomes:
        if (
            outcome.observation.source_id != outcome.source.provider_id
            or outcome.observation.fact_key
            != f"{claim.subject_kind}:{claim.subject_identifier}:{claim.predicate}"
            or outcome.observation.unit != claim.unit
        ):
            raise IntegrityViolation("reducer observation identity does not match the claim")
        expected_value = _numeric_value(outcome.details)
        if outcome.observation.usable and (
            expected_value is None
            or float(outcome.observation.value) != float(expected_value)
        ):
            raise IntegrityViolation("reducer observation value does not match ledger details")
        source_records = records_by_source[outcome.source.provider_id]
        latest = source_records[-1]
        identifiers = outcome.details.get("source_identifiers", ())
        policy_inputs.append(
            SourceObservationInput(
                provider_id=outcome.source.provider_id,
                provider_observed_at=latest.provider_observed_at,
                fetched_at=latest.fetched_at,
                body_complete=all(item.body_complete for item in source_records),
                request_succeeded=outcome.status in {"E4_VERIFIED", "E4_CONTRADICTED"},
                identifiers=(
                    tuple(identifiers)
                    if isinstance(identifiers, (list, tuple))
                    and all(isinstance(item, str) for item in identifiers)
                    else ()
                ),
                quote_currency=str(outcome.details.get("quote_currency", "")),
                price_type=str(outcome.details.get("price_type", "")),
                market_calendar_valid=(
                    outcome.details.get("market_calendar_valid")
                    if isinstance(outcome.details.get("market_calendar_valid"), bool)
                    else None
                ),
            )
        )

    gate = evaluate_source_policy(
        source_policy, tuple(policy_inputs), now=datetime.now(UTC)
    )
    eligibility = {item.provider_id: item for item in gate.observations}
    annotated = tuple(
        replace(
            outcome,
            details={
                **outcome.details,
                "observation_status": eligibility[outcome.source.provider_id].observation_status,
                "observation_reason_code": eligibility[outcome.source.provider_id].reason_code,
                "verification_mode": gate.verification_mode,
                "independent_cross_check": gate.independent_cross_check,
            },
        )
        for outcome in outcomes
    )

    if not gate.eligible_for_reducer:
        return ArbitrationResult(
            gate.verdict_status or "AMBIGUOUS",
            {"status": "POLICY_GATE_BLOCKED", "usable_observation_count": 0},
            annotated,
            gate.decision_reason_codes,
            gate.verification_mode,
            gate.independent_cross_check,
        )

    eligible = tuple(
        outcome
        for outcome in annotated
        if eligibility[outcome.source.provider_id].observation_status == "VALID"
    )
    reducer = detect_conflicts([outcome.observation for outcome in eligible])
    count_value = reducer.get("usable_observation_count", 0)
    usable_count = (
        count_value
        if isinstance(count_value, int) and not isinstance(count_value, bool)
        else 0
    )
    failures = [outcome for outcome in eligible if not outcome.observation.usable]
    reasons = list(gate.decision_reason_codes)

    if failures and usable_count == 0:
        verdict = "INCONCLUSIVE"
        reasons.append("ALL_REQUIRED_SOURCES_UNAVAILABLE")
    elif failures:
        verdict = "AMBIGUOUS"
        reasons.append("REQUIRED_SOURCE_QUORUM_INCOMPLETE")
    elif reducer.get("status") == "AMBIGUOUS":
        verdict = "AMBIGUOUS"
        reasons.append("SOURCE_VALUES_DIVERGE")
    else:
        statuses = {outcome.status for outcome in eligible}
        if statuses == {"E4_VERIFIED"}:
            verdict = "VERIFIED"
            reasons.append("ALL_REQUIRED_SOURCES_AGREE")
        elif statuses == {"E4_CONTRADICTED"}:
            verdict = "CONTRADICTED"
            reasons.append("ALL_REQUIRED_SOURCES_CONTRADICT_CLAIM")
        elif len(statuses) > 1:
            verdict = "AMBIGUOUS"
            reasons.append("SOURCE_VERDICTS_DISAGREE")
        else:
            verdict = "INCONCLUSIVE"
            reasons.append("SOURCE_RESULTS_NOT_QUALIFIED")

    for outcome in annotated:
        if not outcome.observation.usable:
            reason = outcome.details.get("reason")
            reason_text = reason if isinstance(reason, str) else outcome.status
            reasons.append(f"{outcome.source.provider_id}:{reason_text}")
    return ArbitrationResult(
        verdict,
        reducer,
        annotated,
        tuple(dict.fromkeys(reasons)),
        gate.verification_mode,
        gate.independent_cross_check,
    )


async def arbitrate(
    claim: NormalizedClaim,
    policy: MultiSourcePolicy,
    providers: dict[str, Provider],
) -> ArbitrationResult:
    """Deprecated compatibility wrapper: fetch first, then arbitrate captured evidence."""
    warnings.warn(
        "arbitrate() is deprecated; use fetch_provider_results() and "
        "arbitrate_from_evidence() after ledger append",
        DeprecationWarning,
        stacklevel=2,
    )
    outcomes = await fetch_provider_results(claim, policy, providers)
    return ArbitrationResult(
        "INCONCLUSIVE",
        {"status": "POLICY_GATE_BLOCKED", "usable_observation_count": 0},
        outcomes,
        (EVIDENCE_LEDGER_REQUIRED,),
    )
