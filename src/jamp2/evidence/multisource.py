"""Evidence-first provider acquisition and deterministic source arbitration."""
from __future__ import annotations

import asyncio
import math
import warnings
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Sequence

from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.conflict import SourceObservation, detect_conflicts
from jamp2.evidence.model import EvidenceRecord
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
    value = details.get("observed_last")
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
                _fetch_one(source, providers.get(source.provider_key), claim, policy.timeout_seconds)
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
        max_age_by_source={"moex.iss": 1200, "coingecko": 300},
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


def arbitrate_from_evidence(
    claim: NormalizedClaim,
    policy: MultiSourcePolicy,
    outcomes: tuple[SourceOutcome, ...],
    evidence_records: Sequence[EvidenceRecord],
) -> ArbitrationResult:
    """Apply policy once to already-fetched outcomes; never performs network access."""
    _ = evidence_records
    expected_ids = tuple(source.provider_id for source in policy.required_sources)
    actual_ids = tuple(outcome.source.provider_id for outcome in outcomes)
    if actual_ids != expected_ids:
        raise ValueError("outcomes must contain every required source in policy order")

    source_policy = _source_policy(claim, policy)
    policy_inputs: list[SourceObservationInput] = []
    for outcome in outcomes:
        latest = outcome.raw_evidence[-1] if outcome.raw_evidence else {}
        identifiers = outcome.details.get("source_identifiers", ())
        policy_inputs.append(
            SourceObservationInput(
                provider_id=outcome.source.provider_id,
                provider_observed_at=latest.get("provider_observed_at"),
                fetched_at=_parse_capture_time(latest.get("fetched_at")),
                body_complete=bool(outcome.raw_evidence)
                and all(item.get("body_complete") is True for item in outcome.raw_evidence),
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
    return arbitrate_from_evidence(claim, policy, outcomes, ())
