"""Deterministic no-network replay from stored raw provider observations."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from jamp2.evidence.canonicalize import CANONICALIZATION_VERSION
from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
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
        or not record.body_complete
        or compute_sha256(record.raw_body_bytes) != record.raw_sha256
        for record in records
    ):
        return _inconclusive("EVIDENCE_INTEGRITY_OR_VERSION_MISMATCH", verdict)
    claim = _claim_snapshot(records)
    if claim is None or claim.get("predicate") != "price":
        return _inconclusive("CLAIM_SNAPSHOT_INVALID", verdict)

    providers = {record.provider for record in records}
    if providers == {"moex.iss"} and claim.get("subject_kind") == "equity_security":
        status, reason, observed = _replay_moex(verdict, records, claim)
    elif providers == {"coingecko"} and claim.get("subject_kind") == "crypto_asset":
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
