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
from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation
from jamp2.evidence.multisource import (
    MultiSourcePolicy,
    SourceOutcome,
    SourceSpec,
    arbitrate_from_evidence,
)
from jamp2.evidence.policy import SourcePolicyRecord, SourceProvenance
from jamp2.evidence.verdict import VerdictRecord

POLICY_ID = "jamp2.provider-price-comparison"
POLICY_VERSION = "provider-defaults-v1"
PARSER_VERSION = "jamp-provider-json-v1"


def _inconclusive(reason: str, verdict: VerdictRecord) -> dict[str, object]:
    return {
        "verdict_id": verdict.verdict_id,
        "claim_id": verdict.claim_id,
        "original_status": verdict.status,
        "replay_status": "INCONCLUSIVE",
        "matches_original": verdict.status == "INCONCLUSIVE",
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


def _replay_moex(
    verdict: VerdictRecord,
    records: Sequence[EvidenceRecord],
    claim: dict[str, Any],
) -> tuple[str, str, float | None]:
    ticker = claim.get("subject_identifier")
    expected = _finite_positive(claim.get("expected_value"))
    if not isinstance(ticker, str) or expected is None or claim.get("unit") != "RUB":
        return "INCONCLUSIVE", "CLAIM_SNAPSHOT_INVALID", None

    catalog_seen = False
    security_found = False
    observed_price: float | None = None
    for record in records:
        if record.http_status != 200:
            continue
        try:
            payload = json.loads(record.raw_body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "INCONCLUSIVE", "RAW_PROVIDER_JSON_INVALID", None
        if not isinstance(payload, dict):
            return "INCONCLUSIVE", "RAW_PROVIDER_JSON_INVALID", None
        securities = _table_rows(payload, "securities")
        if securities is not None:
            catalog_seen = True
            security_found = any(
                isinstance(row.get("secid"), str)
                and row["secid"].upper() == ticker.upper()
                for row in securities
            )
        marketdata = _table_rows(payload, "marketdata")
        if marketdata is not None:
            for row in marketdata:
                if (
                    isinstance(row.get("SECID"), str)
                    and row["SECID"].upper() == ticker.upper()
                ):
                    observed_price = _finite_positive(row.get("LAST"))
                    if observed_price is not None:
                        break
    if catalog_seen and not security_found:
        return "OUT_OF_SCOPE", "MOEX_SECURITY_NOT_IN_CATALOG", None
    if not catalog_seen or not security_found or observed_price is None:
        return "INCONCLUSIVE", "MOEX_PRICE_OBSERVATION_INCOMPLETE", observed_price
    relative_delta = abs(expected - observed_price) / observed_price
    status = "CONTRADICTED" if relative_delta > 0.05 else "VERIFIED"
    return status, "MOEX_PRICE_POLICY_V1", observed_price


def _replay_coingecko(
    records: Sequence[EvidenceRecord],
    claim: dict[str, Any],
) -> tuple[str, str, float | None]:
    coin_id = claim.get("subject_identifier")
    expected = _finite_positive(claim.get("expected_value"))
    if not isinstance(coin_id, str) or expected is None or claim.get("unit") != "USD":
        return "INCONCLUSIVE", "CLAIM_SNAPSHOT_INVALID", None
    if any(record.http_status == 404 for record in records):
        return "OUT_OF_SCOPE", "COINGECKO_COIN_NOT_FOUND", None
    if any(record.http_status != 200 for record in records):
        return "INCONCLUSIVE", "COINGECKO_SOURCE_UNAVAILABLE", None
    for record in records:
        try:
            payload = json.loads(record.raw_body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "INCONCLUSIVE", "RAW_PROVIDER_JSON_INVALID", None
        if not isinstance(payload, dict):
            return "INCONCLUSIVE", "RAW_PROVIDER_JSON_INVALID", None
        coin = payload.get(coin_id)
        if isinstance(coin, dict):
            observed = _finite_positive(coin.get("usd"))
            if observed is None:
                return "OUT_OF_SCOPE", "COINGECKO_PRICE_NOT_AVAILABLE", None
            relative_delta = abs(expected - observed) / observed * 100.0
            status = "CONTRADICTED" if relative_delta > 10.0 else "VERIFIED"
            return status, "COINGECKO_PRICE_POLICY_V1", observed
    return "OUT_OF_SCOPE", "COINGECKO_COIN_OR_PRICE_NOT_FOUND", None


def _replay_multi_source_conflict(
    verdict: VerdictRecord,
    records: Sequence[EvidenceRecord],
    claim_snapshot: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Re-run the persisted Policy Gate and reducer without provider/network access."""
    if verdict.verification_mode != "MULTI_SOURCE":
        return "INCONCLUSIVE", "MULTI_SOURCE_MODE_MISSING", {}
    if len(records) < 2:
        return "INCONCLUSIVE", "MULTI_SOURCE_EVIDENCE_SHAPE_UNSUPPORTED", {}

    parsed_records = [record.parsed_value for record in records]
    if any(not isinstance(item, dict) for item in parsed_records):
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_MISSING", {}
    contexts = [item.get("replay_context") for item in parsed_records]
    if (
        any(not isinstance(item, dict) for item in contexts)
        or any(item != contexts[0] for item in contexts[1:])
    ):
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_MISSING_OR_CONFLICTING", {}
    context = contexts[0]
    if context.get("schema_version") != "jamp2-policy-gate-replay-v1":
        return "INCONCLUSIVE", "POLICY_REPLAY_CONTEXT_VERSION_UNSUPPORTED", {}

    try:
        evaluation_time = datetime.fromisoformat(
            str(context["evaluation_time_utc"]).replace("Z", "+00:00")
        )
        if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
            raise ValueError("evaluation timestamp must be timezone-aware")
        evaluation_time = evaluation_time.astimezone(UTC)
        policy_data = context["policy"]
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

        outcomes: list[SourceOutcome] = []
        for source in source_specs:
            source_records = [record for record in records if record.provider == source.provider_id]
            if not source_records:
                raise ValueError("required source has no persisted evidence")
            parsed = [record.parsed_value for record in source_records]
            source_statuses = {item.get("source_status") for item in parsed}
            provider_results = [item.get("provider_result") for item in parsed]
            if len(source_statuses) != 1 or any(
                item != provider_results[0] for item in provider_results[1:]
            ) or not isinstance(provider_results[0], dict):
                raise ValueError("source outcome metadata is inconsistent")
            source_status = next(iter(source_statuses))
            details = provider_results[0]
            if not isinstance(source_status, str):
                raise ValueError("source status is missing")
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
            claim, policy, tuple(outcomes), records, now=evaluation_time
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
        or (verdict.verification_mode != "MULTI_SOURCE" and not record.body_complete)
        or compute_sha256(record.raw_body_bytes) != record.raw_sha256
        for record in records
    ):
        return _inconclusive("EVIDENCE_INTEGRITY_OR_VERSION_MISMATCH", verdict)
    claim = _claim_snapshot(records)
    if claim is None or claim.get("predicate") != "price":
        return _inconclusive("CLAIM_SNAPSHOT_INVALID", verdict)

    providers = {record.provider for record in records}
    adapter_versions = {record.provider_adapter_version for record in records}
    if verdict.verification_mode == "MULTI_SOURCE":
        status, reason, reducer = _replay_multi_source_conflict(verdict, records, claim)
        return {
            "verdict_id": verdict.verdict_id,
            "claim_id": verdict.claim_id,
            "original_status": verdict.status,
            "replay_status": status,
            "matches_original": status == verdict.status,
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
    if (
        providers == {"moex.iss"}
        and adapter_versions == {"moex-iss-adapter-v1"}
        and claim.get("subject_kind") == "equity_security"
    ):
        status, reason, observed = _replay_moex(verdict, records, claim)
    elif (
        providers == {"coingecko"}
        and adapter_versions == {"coingecko-adapter-v1"}
        and claim.get("subject_kind") == "crypto_asset"
    ):
        status, reason, observed = _replay_coingecko(records, claim)
    else:
        return _inconclusive("PROVIDER_SUBJECT_MISMATCH", verdict)

    return {
        "verdict_id": verdict.verdict_id,
        "claim_id": verdict.claim_id,
        "original_status": verdict.status,
        "replay_status": status,
        "matches_original": status == verdict.status,
        "reason_code": reason,
        "observed_value": observed,
        "policy_id": verdict.policy_id,
        "policy_version": verdict.policy_version,
        "code_version": verdict.code_version,
        "canonicalization_version": verdict.canonicalization_version,
        "evidence_ids": list(verdict.evidence_ids),
        "network_calls": 0,
    }
