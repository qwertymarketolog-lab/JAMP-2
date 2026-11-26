"""Deterministic no-network replay from stored raw provider observations."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.canonicalize import CANONICALIZATION_VERSION
from jamp2.evidence.conflict import SourceObservation
from jamp2.evidence.digest import (
    compute_policy_snapshot_sha256,
    compute_record_hash,
    compute_sha256,
)
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation
from jamp2.evidence.multisource import (
    MultiSourcePolicy,
    SourceOutcome,
    SourceSpec,
    arbitrate_from_evidence,
)
from jamp2.evidence.policy import (
    SourcePolicyRecord,
    SourceProvenance,
    provenance_is_independent,
)
from jamp2.evidence.verdict import VerdictRecord

POLICY_ID = "jamp2.provider-price-comparison"
POLICY_VERSION = "provider-defaults-v1"
PARSER_VERSION = "jamp-provider-json-v1"

SUPPORTED_ADAPTER_VERSIONS = frozenset({
    "moex-iss-adapter-v1",
    "coingecko-adapter-v1",
    "test-adapter-v1",
})


def _inconclusive(reason: str, verdict: VerdictRecord) -> dict[str, object]:
    return {
        "verdict_id": verdict.verdict_id,
        "claim_id": verdict.claim_id,
        "original_status": verdict.status,
        "replay_status": "INCONCLUSIVE",
        "matches_original": False,
        "reason_codes_match": False,
        "policy_gate_revalidated": False,
        "reason_code": reason,
        "policy_id": verdict.policy_id,
        "policy_version": verdict.policy_version,
        "code_version": verdict.code_version,
        "canonicalization_version": verdict.canonicalization_version,
        "evidence_ids": list(verdict.evidence_ids),
        "network_calls": 0,
    }


def _claim_snapshot(records: Sequence[EvidenceRecord]) -> dict[str, Any] | None:
    snapshot: dict[str, Any] | None = None
    for record in records:
        parsed = record.parsed_value
        if not isinstance(parsed, dict):
            return None
        claim = parsed.get("claim")
        if not isinstance(claim, dict):
            return None
        if snapshot is None:
            snapshot = claim
        elif claim != snapshot:
            return None
    return snapshot


def _table_rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]] | None:
    table = payload.get(table_name)
    if not isinstance(table, dict):
        return None
    columns = table.get("columns")
    data = table.get("data")
    if (
        not isinstance(columns, list)
        or not isinstance(data, list)
        or any(not isinstance(column, str) for column in columns)
    ):
        return None
    return [
        dict(zip(columns, row, strict=True))
        for row in data
        if isinstance(row, list) and len(row) == len(columns)
    ]


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _parse_raw_source_value(
    source: SourceSpec,
    records: Sequence[EvidenceRecord],
    claim: dict[str, Any],
    source_policy: dict[str, Any],
) -> tuple[float, tuple[str, ...]] | None:
    """Derive provider value and identity from exact persisted response bytes."""
    identifier = claim.get("subject_identifier")
    if not isinstance(identifier, str) or not identifier:
        return None
    if source.adapter_version == "moex-iss-adapter-v1":
        catalog_seen = False
        identity_seen = False
        observed: float | None = None
        for record in records:
            try:
                payload = json.loads(record.raw_body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict):
                return None
            securities = _table_rows(payload, "securities")
            if securities is not None:
                catalog_seen = True
                identity_seen = any(
                    isinstance(row.get("secid"), str)
                    and row["secid"].upper() == identifier.upper()
                    for row in securities
                )
            marketdata = _table_rows(payload, "marketdata")
            if marketdata is not None:
                for row in marketdata:
                    if (
                        isinstance(row.get("SECID"), str)
                        and row["SECID"].upper() == identifier.upper()
                    ):
                        value = _finite_positive(row.get("LAST"))
                        if value is not None and observed is None:
                            # MOEX adapter selects candidates[0]; preserve first valid row.
                            observed = value
        if catalog_seen and identity_seen and observed is not None:
            return observed, (identifier,)
        return None
    if source.adapter_version == "coingecko-adapter-v1":
        for record in records:
            try:
                payload = json.loads(record.raw_body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict):
                return None
            coin = payload.get(identifier)
            if isinstance(coin, dict):
                observed = _finite_positive(coin.get("usd"))
                if observed is not None:
                    return observed, (identifier,)
        return None
    if source.adapter_version == "test-adapter-v1":
        for record in records:
            try:
                payload = json.loads(record.raw_body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict):
                return None
            if (
                payload.get("provider") != source.provider_id
                or payload.get("identifier") != identifier
                or payload.get("currency") != claim.get("unit")
                or payload.get("price_type") != source_policy.get("price_type")
            ):
                return None
            observed = _finite_positive(payload.get("price"))
            if observed is not None:
                return observed, (identifier,)
        return None
    return None


def _historical_validity_reason(
    verdict: VerdictRecord,
    records: Sequence[EvidenceRecord],
    snapshot: dict[str, Any],
) -> str | None:
    """Validate evidence eligibility only at the original immutable decision time."""
    policy_data = snapshot.get("policy")
    if not isinstance(policy_data, dict):
        return "POLICY_SNAPSHOT_INVALID"
    source_policy = policy_data.get("source_policy")
    required_sources = policy_data.get("required_sources")
    if not isinstance(source_policy, dict) or not isinstance(required_sources, list):
        return "POLICY_SNAPSHOT_INVALID"
    if source_policy.get("policy_id") != policy_data.get("policy_id"):
        return "POLICY_SNAPSHOT_INVALID"
    if source_policy.get("version") != 1:
        return "SOURCE_POLICY_VERSION_UNSUPPORTED"
    policy_source_ids = source_policy.get("required_sources")
    if (
        not isinstance(policy_source_ids, list)
        or not policy_source_ids
        or any(not isinstance(item, str) or not item for item in policy_source_ids)
        or len(set(policy_source_ids)) != len(policy_source_ids)
    ):
        return "POLICY_SNAPSHOT_INVALID"
    snapshot_source_ids = [
        item.get("provider_id") for item in required_sources if isinstance(item, dict)
    ]
    if (
        len(snapshot_source_ids) != len(required_sources)
        or any(not isinstance(item, str) or not item for item in snapshot_source_ids)
        or len(set(snapshot_source_ids)) != len(snapshot_source_ids)
        or set(snapshot_source_ids) != set(policy_source_ids)
    ):
        return "POLICY_SNAPSHOT_INVALID"
    decision_time = verdict.decided_at_utc.astimezone(UTC)
    provider_times: list[datetime] = []
    by_provider: dict[str, list[EvidenceRecord]] = {}
    for record in records:
        by_provider.setdefault(record.provider, []).append(record)
    max_age_by_source = source_policy.get("max_age_by_source", {})
    default_max_age = source_policy.get("max_age_seconds")
    if (
        not isinstance(max_age_by_source, dict)
        or isinstance(default_max_age, bool)
        or not isinstance(default_max_age, int)
    ):
        return "POLICY_SNAPSHOT_INVALID"
    for source in required_sources:
        if not isinstance(source, dict):
            return "POLICY_SNAPSHOT_INVALID"
        provider_id = source.get("provider_id")
        adapter_version = source.get("adapter_version")
        if not isinstance(provider_id, str) or not isinstance(adapter_version, str):
            return "POLICY_SNAPSHOT_INVALID"
        if adapter_version not in SUPPORTED_ADAPTER_VERSIONS:
            return "EVIDENCE_INTERPRETATION_VERSION_UNSUPPORTED"
        source_records = by_provider.get(provider_id, [])
        if not source_records:
            return "REQUIRED_SOURCE_EVIDENCE_MISSING"
        if any(
            record.provider_adapter_version != adapter_version
            or record.parser_version != snapshot.get("parser_version")
            or record.canonicalization_version != snapshot.get("canonicalization_version")
            or record.hash_schema_version != "v2"
            for record in source_records
        ):
            return "EVIDENCE_INTERPRETATION_VERSION_UNSUPPORTED"
        if any(not record.body_complete or not record.raw_body_bytes for record in source_records):
            return "SOURCE_BODY_INCOMPLETE"
        if any(
            record.http_status is None or not 200 <= record.http_status < 300
            for record in source_records
        ):
            return "SOURCE_HTTP_STATUS_INVALID"
        for record in source_records:
            observed_at = record.provider_observed_at
            fetched_at = record.fetched_at
            if observed_at is None:
                return "PROVIDER_OBSERVED_AT_MISSING"
            if fetched_at is None:
                return "FETCHED_AT_MISSING"
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                return "PROVIDER_OBSERVED_AT_INVALID"
            if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                return "FETCHED_AT_INVALID"
            observed_utc = observed_at.astimezone(UTC)
            fetched_utc = fetched_at.astimezone(UTC)
            if observed_utc > fetched_utc:
                return "PROVIDER_OBSERVED_AFTER_FETCH"
            if fetched_utc > decision_time:
                return "FETCHED_AFTER_DECISION"
            if observed_utc > decision_time:
                return "PROVIDER_OBSERVED_AFTER_DECISION"
            age_limit = max_age_by_source.get(provider_id, default_max_age)
            if isinstance(age_limit, bool) or not isinstance(age_limit, int) or age_limit <= 0:
                return "POLICY_SNAPSHOT_INVALID"
            if (decision_time - observed_utc).total_seconds() > age_limit:
                return "SOURCE_AGE_EXCEEDED_AT_DECISION"
        latest_observed_at = source_records[-1].provider_observed_at
        if latest_observed_at is None:
            return "PROVIDER_OBSERVED_AT_MISSING"
        provider_times.append(latest_observed_at.astimezone(UTC))
    if len(provider_times) > 1:
        max_skew = source_policy.get("max_time_skew_seconds")
        if isinstance(max_skew, bool) or not isinstance(max_skew, int) or max_skew < 0:
            return "POLICY_SNAPSHOT_INVALID"
        if (max(provider_times) - min(provider_times)).total_seconds() > max_skew:
            return "SOURCE_TIME_SKEW_EXCEEDED"
    if source_policy.get("verification_mode") == "MULTI_SOURCE":
        provenance_data = source_policy.get("provenance")
        if not isinstance(provenance_data, list):
            return "POLICY_SNAPSHOT_INVALID"
        try:
            provenance = tuple(SourceProvenance(**item) for item in provenance_data)
        except (TypeError, ValueError):
            return "POLICY_SNAPSHOT_INVALID"
        provenance_ids = {item.provider_id for item in provenance}
        if (
            len(provenance) != len(policy_source_ids)
            or provenance_ids != set(policy_source_ids)
            or not provenance_is_independent(provenance)
        ):
            return "SOURCE_PROVENANCE_INDEPENDENCE_UNVERIFIED"
    calendar = snapshot.get("market_calendar")
    if not isinstance(calendar, dict):
        return "POLICY_SNAPSHOT_INVALID"
    if calendar.get("required") is not source_policy.get("market_calendar_required"):
        return "POLICY_SNAPSHOT_INVALID"
    if calendar.get("required") is True:
        qualification = calendar.get("qualification")
        if not isinstance(qualification, dict) or any(
            qualification.get(source.get("provider_id")) is not True
            for source in required_sources if isinstance(source, dict)
        ):
            return "SOURCE_MARKET_CALENDAR_UNCONFIRMED"
    return None


def _replay_multi_source_conflict(
    verdict: VerdictRecord,
    records: Sequence[EvidenceRecord],
    claim_snapshot: dict[str, Any],
    policy_snapshot: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Re-run the persisted Policy Gate and reducer without provider/network access."""
    if verdict.verification_mode not in {"SINGLE_SOURCE", "MULTI_SOURCE"}:
        return "INCONCLUSIVE", "VERIFICATION_MODE_UNSUPPORTED", {}

    parsed_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record.parsed_value, dict):
            return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_MISSING", {}
        parsed_records.append(record.parsed_value)
    contexts = [item.get("replay_context") for item in parsed_records]
    if (
        any(not isinstance(item, dict) for item in contexts)
        or any(item != contexts[0] for item in contexts[1:])
    ):
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_MISSING_OR_CONFLICTING", {}
    context = contexts[0]
    if not isinstance(context, dict):
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_MISSING", {}
    if context.get("schema_version") != "jamp2-policy-gate-replay-v1":
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_VERSION_UNSUPPORTED", {}
    if context.get("policy") != policy_snapshot.get("policy"):
        return "INCONCLUSIVE", "POLICY_SNAPSHOT_CONTEXT_MISMATCH", {}

    try:
        evaluation_time = verdict.decided_at_utc.astimezone(UTC)
        stored_evaluation_time = datetime.fromisoformat(
            str(context["evaluation_time_utc"]).replace("Z", "+00:00")
        )
        if stored_evaluation_time.tzinfo is None or stored_evaluation_time.utcoffset() is None:
            raise ValueError("stored evaluation timestamp must be timezone-aware")
        if stored_evaluation_time.astimezone(UTC) != evaluation_time:
            raise ValueError("stored evaluation time differs from Verdict decision time")
        policy_data = policy_snapshot["policy"]
        source_policy_data = policy_data["source_policy"]
        provenance_data = source_policy_data["provenance"]
        required_sources_data = policy_data["required_sources"]
        if not isinstance(provenance_data, list) or not isinstance(required_sources_data, list):
            raise ValueError("policy source arrays are invalid")
        provenance = tuple(SourceProvenance(**item) for item in provenance_data)
        source_policy = SourcePolicyRecord(
            policy_id=source_policy_data["policy_id"],
            version=source_policy_data["version"],
            asset_class=source_policy_data["asset_class"],
            required_identifiers=tuple(source_policy_data["required_identifiers"]),
            market_calendar_required=source_policy_data["market_calendar_required"],
            quote_currency=source_policy_data["quote_currency"],
            price_type=source_policy_data["price_type"],
            required_sources=tuple(source_policy_data["required_sources"]),
            independence_group=source_policy_data["independence_group"],
            max_age_seconds=source_policy_data["max_age_seconds"],
            max_time_skew_seconds=source_policy_data["max_time_skew_seconds"],
            comparison_mode=source_policy_data["comparison_mode"],
            tolerance=source_policy_data["tolerance"],
            timeout_seconds=source_policy_data["timeout_seconds"],
            verification_mode=source_policy_data["verification_mode"],
            provenance=provenance,
            max_age_by_source=dict(source_policy_data["max_age_by_source"]),
        )
        source_specs = tuple(SourceSpec(**item) for item in required_sources_data)
        policy = MultiSourcePolicy(
            policy_id=policy_data["policy_id"],
            version=policy_data["version"],
            required_sources=source_specs,
            timeout_seconds=policy_data["timeout_seconds"],
            comparison_mode=policy_data["comparison_mode"],
            source_policy=source_policy,
        )
        if policy.policy_id != verdict.policy_id or policy.version != verdict.policy_version:
            raise ValueError("persisted policy identity does not match the verdict")
        claim = NormalizedClaim(
            text=claim_snapshot["text"],
            subject_kind=claim_snapshot["subject_kind"],
            subject_identifier=claim_snapshot["subject_identifier"],
            predicate=claim_snapshot["predicate"],
            expected_value=claim_snapshot["expected_value"],
            unit=claim_snapshot["unit"],
        )
        if (
            source_policy.asset_class != claim.subject_kind
            or claim.subject_identifier not in source_policy.required_identifiers
            or claim.unit != source_policy.quote_currency
        ):
            raise ValueError("claim identity differs from the bound source policy")

        outcomes: list[SourceOutcome] = []
        for source in source_specs:
            source_records = [record for record in records if record.provider == source.provider_id]
            if not source_records:
                raise ValueError("required source has no persisted evidence")
            parsed: list[dict[str, Any]] = []
            for record in source_records:
                if not isinstance(record.parsed_value, dict):
                    raise ValueError("source evidence parsed metadata is invalid")
                parsed.append(record.parsed_value)
            source_statuses = {item.get("source_status") for item in parsed}
            provider_results = [item.get("provider_result") for item in parsed]
            if len(source_statuses) != 1 or any(
                item != provider_results[0] for item in provider_results[1:]
            ) or not isinstance(provider_results[0], dict):
                raise ValueError("source outcome metadata is inconsistent")
            source_status = next(iter(source_statuses))
            details = provider_results[0]
            calendar = policy_snapshot.get("market_calendar")
            qualifications = calendar.get("qualification") if isinstance(calendar, dict) else None
            if (
                not isinstance(qualifications, dict)
                or source.provider_id not in qualifications
                or qualifications[source.provider_id] != details.get("market_calendar_valid")
            ):
                raise ValueError(
                    "market calendar qualification differs from persisted source result"
                )
            if not isinstance(source_status, str):
                raise ValueError("source status is missing")
            derived = _parse_raw_source_value(
                source, source_records, claim_snapshot, source_policy_data
            )
            if source_status in {"E4_VERIFIED", "E4_CONTRADICTED"}:
                if derived is None:
                    raise ValueError(
                        "raw provider bytes are not interpretable by the bound adapter"
                    )
                expected_value = _finite_positive(claim_snapshot.get("expected_value"))
                if expected_value is None:
                    raise ValueError("claim expected value is invalid")
                raw_value, raw_identifiers = derived
                recorded_value = _finite_positive(
                    details.get("observed_last", details.get("observed_value"))
                )
                if recorded_value is None or recorded_value != raw_value:
                    raise ValueError("provider result differs from raw response bytes")
                if tuple(details.get("source_identifiers", ())) != raw_identifiers:
                    raise ValueError("provider identity differs from raw response bytes")
                if details.get("quote_currency") != source_policy_data.get("quote_currency"):
                    raise ValueError("provider quote currency differs from policy snapshot")
                if details.get("price_type") != source_policy_data.get("price_type"):
                    raise ValueError("provider price type differs from policy snapshot")
                comparison = policy_snapshot.get("comparison_parameters")
                if not isinstance(comparison, dict):
                    raise ValueError("comparison parameters are missing")
                parameters = comparison.get(source.adapter_version)
                if not isinstance(parameters, dict):
                    parameters = comparison.get(source.provider_id)
                if not isinstance(parameters, dict):
                    raise ValueError("provider comparison parameters are unsupported")
                threshold = parameters.get("threshold")
                mode = parameters.get("mode")
                if (
                    isinstance(threshold, bool)
                    or not isinstance(threshold, (int, float))
                    or threshold < 0
                ):
                    raise ValueError("provider comparison threshold is invalid")
                delta = abs(expected_value - raw_value) / raw_value
                if mode == "RELATIVE_DELTA_PERCENT":
                    delta *= 100.0
                elif mode != "RELATIVE_DELTA":
                    raise ValueError("provider comparison mode is unsupported")
                calculated_status = "E4_CONTRADICTED" if delta > float(threshold) else "E4_VERIFIED"
                if calculated_status != source_status:
                    raise ValueError("provider status differs from raw-byte recalculation")
            captures: list[dict[str, Any]] = []
            for record in source_records:
                if record.verdict_status == "SOURCE_FAILURE":
                    continue
                captures.append(
                    {
                        "source": record.provider,
                        "url": record.url,
                        "status_code": record.http_status,
                        "body_complete": record.body_complete,
                        "raw_body_base64": base64.b64encode(record.raw_body_bytes).decode("ascii"),
                        "sha256_body": record.raw_sha256,
                        "fetched_at": record.fetched_at.isoformat() if record.fetched_at else None,
                        "provider_observed_at": (
                            record.provider_observed_at.isoformat()
                            if record.provider_observed_at else None
                        ),
                        "source_date": record.source_date,
                    }
                )
            observed = _finite_positive(details.get("observed_last", details.get("observed_value")))
            usable = observed is not None and source_status in {"E4_VERIFIED", "E4_CONTRADICTED"}
            outcomes.append(
                SourceOutcome(
                    source=source,
                    status=source_status,
                    details=details,
                    raw_evidence=captures,
                    observation=SourceObservation(
                        source_id=source.provider_id,
                        fact_key=(
                            f"{claim.subject_kind}:{claim.subject_identifier}:{claim.predicate}"
                        ),
                        value=observed if observed is not None else 0,
                        unit=claim.unit,
                        usable=usable,
                    ),
                )
            )

        arbitration = arbitrate_from_evidence(
            claim, policy, tuple(outcomes), records,
            expected_claim_id=verdict.claim_id,
            now=verdict.decided_at_utc,
        )
    except (IntegrityViolation, KeyError, TypeError, ValueError, OverflowError):
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_INVALID", {}
    gate_observations = [
        {
            "provider_id": outcome.source.provider_id,
            "observation_status": outcome.details.get("observation_status"),
            "reason_code": outcome.details.get("observation_reason_code"),
        }
        for outcome in arbitration.outcomes
    ]
    return arbitration.verdict_status, "POLICY_GATE_AND_REDUCER_REPLAYED", {
        "status": arbitration.reducer.get("status"),
        "usable_observation_count": arbitration.reducer.get("usable_observation_count", 0),
        "verification_mode": arbitration.verification_mode,
        "independent_cross_check": arbitration.independent_cross_check,
        "policy_gate_observations": gate_observations,
        "reason_codes": list(arbitration.reason_codes),
    }


def replay_verdict(
    verdict: VerdictRecord,
    records: Sequence[EvidenceRecord],
) -> dict[str, object]:
    """Recalculate a recorded verdict from exact stored bytes without network access."""
    if verdict.policy_snapshot is None or verdict.policy_snapshot_sha256 is None:
        return _inconclusive("POLICY_SNAPSHOT_MISSING", verdict)
    if not isinstance(verdict.policy_snapshot, dict):
        return _inconclusive("POLICY_SNAPSHOT_INVALID", verdict)
    if verdict.policy_snapshot.get("canonicalization_version") != CANONICALIZATION_VERSION:
        return _inconclusive("POLICY_SNAPSHOT_CANONICALIZATION_UNSUPPORTED", verdict)
    try:
        snapshot_digest = compute_policy_snapshot_sha256(verdict.policy_snapshot)
    except (TypeError, ValueError):
        return _inconclusive("POLICY_SNAPSHOT_INVALID", verdict)
    if snapshot_digest != verdict.policy_snapshot_sha256:
        return _inconclusive("POLICY_SNAPSHOT_DIGEST_MISMATCH", verdict)
    if verdict.policy_snapshot.get("schema_version") != "jamp2-policy-snapshot-v1":
        return _inconclusive("POLICY_SNAPSHOT_VERSION_UNSUPPORTED", verdict)
    if verdict.policy_snapshot.get("policy_evaluator_version") != "jamp-source-policy-v1":
        return _inconclusive("POLICY_EVALUATOR_VERSION_UNSUPPORTED", verdict)
    if verdict.policy_snapshot.get("required_fields") != [
        "raw_body_bytes", "raw_sha256", "body_complete", "fetched_at",
        "provider_observed_at", "provider_adapter_version", "parser_version",
        "source_status", "provider_result", "source_identifiers", "quote_currency",
        "price_type", "market_calendar_qualification",
    ]:
        return _inconclusive("POLICY_SNAPSHOT_REQUIRED_FIELDS_UNSUPPORTED", verdict)
    if verdict.policy_id != POLICY_ID or verdict.policy_version != POLICY_VERSION:
        return _inconclusive("POLICY_VERSION_UNSUPPORTED", verdict)
    if verdict.canonicalization_version != CANONICALIZATION_VERSION:
        return _inconclusive("CANONICALIZATION_VERSION_UNSUPPORTED", verdict)
    if not records or tuple(record.evidence_id for record in records) != verdict.evidence_ids:
        return _inconclusive("EVIDENCE_REFERENCE_MISMATCH", verdict)
    if any(
        record.claim_id != verdict.claim_id
        or record.canonicalization_version != verdict.canonicalization_version
        or record.parser_version != PARSER_VERSION
        or not record.record_hash
        or compute_record_hash(record) != record.record_hash
        or compute_sha256(record.raw_body_bytes) != record.raw_sha256
        for record in records
    ):
        return _inconclusive("EVIDENCE_INTEGRITY_OR_VERSION_MISMATCH", verdict)
    historical_reason = _historical_validity_reason(verdict, records, verdict.policy_snapshot)
    if historical_reason is not None:
        return _inconclusive(historical_reason, verdict)
    claim = _claim_snapshot(records)
    if claim is None or claim.get("predicate") != "price":
        return _inconclusive("CLAIM_SNAPSHOT_INVALID", verdict)

    if verdict.verification_mode not in {"SINGLE_SOURCE", "MULTI_SOURCE"}:
        return _inconclusive("VERIFICATION_MODE_UNSUPPORTED", verdict)
    policy_data = verdict.policy_snapshot.get("policy")
    if (
        not isinstance(policy_data, dict)
        or not isinstance(policy_data.get("source_policy"), dict)
        or policy_data["source_policy"].get("verification_mode") != verdict.verification_mode
    ):
        return _inconclusive("POLICY_SNAPSHOT_CONTEXT_MISMATCH", verdict)
    status, reason, reducer = _replay_multi_source_conflict(
        verdict, records, claim, verdict.policy_snapshot
    )
    return {
        "verdict_id": verdict.verdict_id,
        "claim_id": verdict.claim_id,
        "original_status": verdict.status,
        "replay_status": status,
        "reason_codes_match": reducer.get("reason_codes") == list(verdict.reason_codes),
        "matches_original": (
            status != "INCONCLUSIVE"
            and status == verdict.status
            and reducer.get("reason_codes") == list(verdict.reason_codes)
        ),
        "reason_code": reason,
        "reducer": reducer,
        "replay_scope": "POLICY_GATE_AND_REDUCER",
        "policy_gate_revalidated": reason == "POLICY_GATE_AND_REDUCER_REPLAYED",
        "policy_id": verdict.policy_id,
        "policy_version": verdict.policy_version,
        "code_version": verdict.code_version,
        "canonicalization_version": verdict.canonicalization_version,
        "evidence_ids": list(verdict.evidence_ids),
        "network_calls": 0,
    }
