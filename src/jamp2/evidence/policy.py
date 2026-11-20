"""Versioned source policy and temporal/provenance eligibility gate.

This module is deliberately independent from provider transport and the legacy
conflict reducer. It decides whether observations are eligible to reach that
reducer; it never upgrades missing source metadata into evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

VerificationMode = Literal["SINGLE_SOURCE", "MULTI_SOURCE"]
ObservationStatus = Literal["VALID", "EXPIRED", "INCOMPLETE", "FAILED"]
ComparisonMode = Literal["EXACT_V1"]


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_provider_time(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        try:
            return _utc(value, "provider_observed_at")
        except ValueError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(parsed, "provider_observed_at")
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Documented upstream and failure domains for one provider."""

    provider_id: str
    upstream_entity: str | None
    aggregation_methodology: str | None
    transport_failure_domain: str | None
    calculation_failure_domain: str | None
    independence_status: Literal["CONFIRMED", "UNKNOWN"] = "UNKNOWN"

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id is required")
        for name in (
            "upstream_entity",
            "aggregation_methodology",
            "transport_failure_domain",
            "calculation_failure_domain",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or null")
        if self.independence_status not in {"CONFIRMED", "UNKNOWN"}:
            raise ValueError("independence_status must be CONFIRMED or UNKNOWN")

    @property
    def independence_evidenced(self) -> bool:
        return (
            self.independence_status == "CONFIRMED"
            and self.upstream_entity is not None
            and self.aggregation_methodology is not None
            and self.transport_failure_domain is not None
            and self.calculation_failure_domain is not None
        )


def provenance_is_independent(sources: tuple[SourceProvenance, ...]) -> bool:
    """Require distinct upstreams and no shared transport/calculation failure domain."""
    if len(sources) < 2 or any(not item.independence_evidenced for item in sources):
        return False
    upstreams = [item.upstream_entity for item in sources]
    transports = [item.transport_failure_domain for item in sources]
    calculations = [item.calculation_failure_domain for item in sources]
    return (
        len(set(upstreams)) == len(upstreams)
        and len(set(transports)) == len(transports)
        and len(set(calculations)) == len(calculations)
    )


@dataclass(frozen=True, slots=True)
class SourcePolicyRecord:
    """Immutable policy contract used to gate a set of source observations."""

    policy_id: str
    version: int
    asset_class: str
    required_identifiers: tuple[str, ...]
    market_calendar_required: bool
    quote_currency: str
    price_type: str
    required_sources: tuple[str, ...]
    independence_group: str
    max_age_seconds: int
    max_time_skew_seconds: int
    comparison_mode: ComparisonMode = "EXACT_V1"
    tolerance: float | None = None
    timeout_seconds: float = 3.0
    verification_mode: VerificationMode = "MULTI_SOURCE"
    provenance: tuple[SourceProvenance, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "asset_class",
            "quote_currency",
            "price_type",
            "independence_group",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version must be a positive integer")
        if not self.required_identifiers or any(
            not item.strip() for item in self.required_identifiers
        ):
            raise ValueError("required_identifiers must be non-empty")
        if not self.required_sources or any(not item.strip() for item in self.required_sources):
            raise ValueError("required_sources must be non-empty")
        if len(set(self.required_sources)) != len(self.required_sources):
            raise ValueError("required_sources must be unique")
        if self.verification_mode not in {"SINGLE_SOURCE", "MULTI_SOURCE"}:
            raise ValueError("unsupported verification_mode")
        if self.verification_mode == "SINGLE_SOURCE" and len(self.required_sources) != 1:
            raise ValueError("SINGLE_SOURCE requires exactly one required source")
        if self.verification_mode == "MULTI_SOURCE" and len(self.required_sources) < 2:
            raise ValueError("MULTI_SOURCE requires at least two required sources")
        if self.comparison_mode != "EXACT_V1":
            raise ValueError("only EXACT_V1 comparison is supported")
        if self.tolerance is not None:
            raise ValueError("EXACT_V1 does not accept a tolerance")
        if self.max_age_seconds <= 0 or self.max_time_skew_seconds < 0:
            raise ValueError("invalid temporal limits")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        provenance_ids = [item.provider_id for item in self.provenance]
        if len(set(provenance_ids)) != len(provenance_ids):
            raise ValueError("provenance provider IDs must be unique")
        if any(item not in self.required_sources for item in provenance_ids):
            raise ValueError("provenance contains a non-required source")


@dataclass(frozen=True, slots=True)
class SourceObservationInput:
    """Provider observation metadata presented to the policy gate."""

    provider_id: str
    provider_observed_at: str | datetime | None
    fetched_at: datetime | None
    body_complete: bool
    request_succeeded: bool
    identifiers: tuple[str, ...]
    quote_currency: str
    price_type: str
    market_calendar_valid: bool | None = None


@dataclass(frozen=True, slots=True)
class SourceEligibility:
    provider_id: str
    observation_status: ObservationStatus
    provider_observed_at: datetime | None
    fetched_at: datetime
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyGateResult:
    verification_mode: VerificationMode
    independent_cross_check: bool
    eligible_for_reducer: bool
    verdict_status: str | None
    observations: tuple[SourceEligibility, ...]
    decision_reason_codes: tuple[str, ...]


def evaluate_source_policy(
    policy: SourcePolicyRecord,
    observations: tuple[SourceObservationInput, ...],
    *,
    now: datetime,
) -> PolicyGateResult:
    """Classify required observations and fail closed before conflict reduction."""
    current = _utc(now, "now")
    by_id = {item.provider_id: item for item in observations}
    if len(by_id) != len(observations):
        raise ValueError("duplicate source observation")
    provenance_by_id = {item.provider_id: item for item in policy.provenance}
    eligibility: list[SourceEligibility] = []

    for provider_id in policy.required_sources:
        item = by_id.get(provider_id)
        if item is None or not item.request_succeeded:
            eligibility.append(
                SourceEligibility(provider_id, "FAILED", None, current, "SOURCE_FAILED")
            )
            continue
        try:
            fetched_at = _utc(item.fetched_at, "fetched_at") if item.fetched_at else None
        except ValueError:
            fetched_at = None
        observed_at = _parse_provider_time(item.provider_observed_at)
        if not item.body_complete or observed_at is None or fetched_at is None:
            eligibility.append(
                SourceEligibility(
                    provider_id,
                    "INCOMPLETE",
                    observed_at,
                    fetched_at or current,
                    "SOURCE_TIMESTAMP_OR_BODY_INCOMPLETE",
                )
            )
            continue
        if (
            item.quote_currency != policy.quote_currency
            or item.price_type != policy.price_type
            or any(
                identifier not in item.identifiers
                for identifier in policy.required_identifiers
            )
        ):
            eligibility.append(
                SourceEligibility(
                    provider_id,
                    "INCOMPLETE",
                    observed_at,
                    fetched_at,
                    "SOURCE_IDENTITY_OR_QUOTE_MISMATCH",
                )
            )
            continue
        if policy.market_calendar_required and item.market_calendar_valid is not True:
            eligibility.append(
                SourceEligibility(
                    provider_id,
                    "INCOMPLETE",
                    observed_at,
                    fetched_at,
                    "SOURCE_MARKET_CALENDAR_UNCONFIRMED",
                )
            )
            continue
        age = (current - observed_at).total_seconds()
        if age < 0 or age > policy.max_age_seconds:
            eligibility.append(
                SourceEligibility(
                    provider_id, "EXPIRED", observed_at, fetched_at, "SOURCE_AGE_OUT_OF_POLICY"
                )
            )
            continue
        if (fetched_at - observed_at).total_seconds() < 0:
            eligibility.append(
                SourceEligibility(
                    provider_id, "EXPIRED", observed_at, fetched_at, "SOURCE_TIME_IN_FUTURE"
                )
            )
            continue
        eligibility.append(SourceEligibility(provider_id, "VALID", observed_at, fetched_at))

    statuses = [item.observation_status for item in eligibility]
    reasons: list[str] = []
    valid = [item for item in eligibility if item.observation_status == "VALID"]
    independent = False
    if policy.verification_mode == "MULTI_SOURCE":
        if valid:
            provenances = tuple(
                provenance_by_id[source]
                for source in policy.required_sources
                if source in provenance_by_id
            )
            independent = (
                len(provenances) == len(policy.required_sources)
                and provenance_is_independent(provenances)
            )
            if not independent:
                reasons.append("ERR_INCOMPATIBLE_PROVENANCE")
    else:
        reasons.append("ERR_SINGLE_SOURCE_FORCED")

    valid_times = [
        item.provider_observed_at for item in valid if item.provider_observed_at is not None
    ]
    if len(valid_times) >= 2:
        skew = (max(valid_times) - min(valid_times)).total_seconds()
        if skew > policy.max_time_skew_seconds:
            reasons.append("ERR_TEMPORAL_SKEW_EXCEEDED")
            eligibility = [
                SourceEligibility(
                    item.provider_id,
                    "EXPIRED" if item.observation_status == "VALID" else item.observation_status,
                    item.provider_observed_at,
                    item.fetched_at,
                    (
                        "SOURCE_TIME_SKEW_EXCEEDED"
                        if item.observation_status == "VALID"
                        else item.reason_code
                    ),
                )
                for item in eligibility
            ]
            statuses = [item.observation_status for item in eligibility]
            valid = []

    if all(item == "FAILED" for item in statuses):
        verdict = "INCONCLUSIVE"
        reasons.append("ALL_REQUIRED_SOURCES_UNAVAILABLE")
    elif any(item != "VALID" for item in statuses):
        verdict = "AMBIGUOUS"
        reasons.append("ERR_QUORUM_NOT_MET")
    elif policy.verification_mode == "MULTI_SOURCE" and not independent:
        verdict = "AMBIGUOUS"
        reasons.append("ERR_QUORUM_NOT_MET")
    else:
        verdict = None

    return PolicyGateResult(
        verification_mode=policy.verification_mode,
        independent_cross_check=independent,
        eligible_for_reducer=verdict is None,
        verdict_status=verdict,
        observations=tuple(eligibility),
        decision_reason_codes=tuple(dict.fromkeys(reasons)),
    )
