"""Pure replay tests: exact stored observations, no live provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.digest import (
    compute_policy_snapshot_sha256,
    compute_record_hash,
    compute_sha256,
)
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.multisource import (
    MultiSourcePolicy,
    SourceSpec,
    build_policy_replay_context,
)
from jamp2.evidence.policy import SourcePolicyRecord, SourceProvenance
from jamp2.evidence.replay import replay_verdict
from jamp2.evidence.verdict import VerdictRecord


def claim_snapshot(
    subject_kind: str = "equity_security",
    subject_identifier: str = "SBER",
    unit: str = "RUB",
    expected_value: float = 100.0,
) -> dict[str, object]:
    return {
        "text": "bounded price claim",
        "subject_kind": subject_kind,
        "subject_identifier": subject_identifier,
        "predicate": "price",
        "expected_value": expected_value,
        "unit": unit,
    }


def evidence(
    evidence_id: str,
    provider: str,
    url: str,
    raw: bytes,
    claim: dict[str, object],
    *,
    http_status: int = 200,
) -> EvidenceRecord:
    record = EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-replay",
        provider=provider,
        url=url,
        http_status=http_status,
        raw_body_bytes=raw,
        raw_sha256=compute_sha256(raw),
        captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        parsed_value={"claim": claim, "provider_result": {}},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
        source_timestamp="2026-11-19",
        body_complete=True,
        provider_adapter_version="moex-iss-adapter-v1"
        if provider == "moex.iss"
        else "coingecko-adapter-v1",
        parser_version="jamp-provider-json-v1",
    )
    return replace(record, record_hash=compute_record_hash(record))


def multi_source_replay_context() -> dict[str, object]:
    claim = NormalizedClaim(
        text="bounded price claim",
        subject_kind="equity_security",
        subject_identifier="SBER",
        predicate="price",
        expected_value=100.0,
        unit="USD",
    )
    sources = (
        SourceSpec("source_a", "source-a", "source-a.example", "test-adapter-v1"),
        SourceSpec("source_b", "source-b", "source-b.example", "test-adapter-v1"),
    )
    source_policy = SourcePolicyRecord(
        policy_id="jamp2.provider-price-comparison",
        version=1,
        asset_class="equity_security",
        required_identifiers=("SBER",),
        market_calendar_required=False,
        quote_currency="USD",
        price_type="SPOT",
        required_sources=("source-a", "source-b"),
        independence_group="unit-test-independent-sources",
        max_age_seconds=120,
        max_time_skew_seconds=5,
        verification_mode="MULTI_SOURCE",
        provenance=(
            SourceProvenance(
                provider_id="source-a",
                upstream_entity="upstream-a",
                aggregation_methodology="direct-spot-observation",
                transport_failure_domain="transport-a",
                calculation_failure_domain="calculation-a",
                independence_status="CONFIRMED",
            ),
            SourceProvenance(
                provider_id="source-b",
                upstream_entity="upstream-b",
                aggregation_methodology="direct-spot-observation",
                transport_failure_domain="transport-b",
                calculation_failure_domain="calculation-b",
                independence_status="CONFIRMED",
            ),
        ),
    )
    policy = MultiSourcePolicy(
        "jamp2.provider-price-comparison",
        "provider-defaults-v1",
        sources,
        source_policy=source_policy,
    )
    return build_policy_replay_context(
        claim,
        policy,
        evaluated_at=datetime(2026, 11, 20, 0, 0, 10, tzinfo=UTC),
    )


def multi_source_policy_snapshot() -> dict[str, Any]:
    context = multi_source_replay_context()
    return {
        "schema_version": "jamp2-policy-snapshot-v1",
        "canonicalization_version": "jamp-json-nfc-v1",
        "policy_evaluator_version": "jamp-source-policy-v1",
        "parser_version": "jamp-provider-json-v1",
        "policy": context["policy"],
        "required_fields": [
            "raw_body_bytes", "raw_sha256", "body_complete", "fetched_at",
            "provider_observed_at", "provider_adapter_version", "parser_version",
        ],
        "comparison_parameters": {
            "test-adapter-v1": {"mode": "RELATIVE_DELTA", "threshold": 0.05},
        },
        "market_calendar": {
            "required": False,
            "qualification": {"source-a": None, "source-b": None},
        },
    }


def verdict(
    evidence_ids: tuple[str, ...],
    status: str = "VERIFIED",
    policy_snapshot: dict[str, Any] | None = None,
) -> VerdictRecord:
    return VerdictRecord(
        verdict_id="verdict-replay",
        claim_id="claim-replay",
        status=status,
        evidence_ids=evidence_ids,
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="recorded-commit-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, 0, 0, 10, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Original recorded decision.",
        policy_snapshot=policy_snapshot,
        policy_snapshot_sha256=(
            compute_policy_snapshot_sha256(policy_snapshot)
            if policy_snapshot is not None else None
        ),
    )


def test_moex_replay_reparses_stored_catalog_and_marketdata_bytes() -> None:
    claim = claim_snapshot()
    catalog = json.dumps(
        {"securities": {"columns": ["secid"], "data": [["SBER"]]}}
    ).encode()
    marketdata = json.dumps(
        {"marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 100.0]]}}
    ).encode()
    records = [
        evidence(
            "evidence-catalog",
            "moex.iss",
            "https://iss.moex.com/iss/securities.json",
            catalog,
            claim,
        ),
        evidence(
            "evidence-price",
            "moex.iss",
            "https://iss.moex.com/iss/marketdata.json",
            marketdata,
            claim,
        ),
    ]
    result = replay_verdict(verdict(("evidence-catalog", "evidence-price")), records)
    assert result["replay_status"] == "INCONCLUSIVE"
    assert result["matches_original"] is False
    assert result["reason_code"] == "POLICY_SNAPSHOT_MISSING"
    assert result["network_calls"] == 0


def test_moex_replay_preserves_contradiction() -> None:
    claim = claim_snapshot(expected_value=500.0)
    catalog = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
    marketdata = b'{"marketdata":{"columns":["SECID","LAST"],"data":[["SBER",100.0]]}}'
    records = [
        evidence(
            "evidence-catalog",
            "moex.iss",
            "https://iss.moex.com/iss/securities.json",
            catalog,
            claim,
        ),
        evidence(
            "evidence-price",
            "moex.iss",
            "https://iss.moex.com/iss/marketdata.json",
            marketdata,
            claim,
        ),
    ]
    result = replay_verdict(
        verdict(("evidence-catalog", "evidence-price"), status="CONTRADICTED"),
        records,
    )
    assert result["replay_status"] == "INCONCLUSIVE"
    assert result["matches_original"] is False
    assert result["reason_code"] == "POLICY_SNAPSHOT_MISSING"


def test_coingecko_replay_uses_stored_usd_response() -> None:
    claim = claim_snapshot("crypto_asset", "bitcoin", "USD", 67000.0)
    raw = b'{"bitcoin":{"usd":67000.0}}'
    record = evidence(
        "evidence-coin",
        "coingecko",
        "https://api.coingecko.com/api/v3/simple/price",
        raw,
        claim,
    )
    result = replay_verdict(verdict(("evidence-coin",)), [record])
    assert result["replay_status"] == "INCONCLUSIVE"
    assert result["reason_code"] == "POLICY_SNAPSHOT_MISSING"
    assert result["network_calls"] == 0


def test_replay_fails_closed_for_unknown_policy_or_bad_digest() -> None:
    claim = claim_snapshot()
    raw = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
    record = evidence(
        "evidence-catalog",
        "moex.iss",
        "https://iss.moex.com/iss/securities.json",
        raw,
        claim,
    )
    unknown_policy = replace(
        verdict(("evidence-catalog",)),
        policy_version="unrecognized-policy",
    )
    assert replay_verdict(unknown_policy, [record])["reason_code"] == "POLICY_SNAPSHOT_MISSING"
    corrupted = replace(record, raw_sha256="0" * 64)
    assert (
        replay_verdict(
            verdict(
                ("evidence-catalog",),
                policy_snapshot=multi_source_policy_snapshot(),
            ),
            [corrupted],
        )["reason_code"]
        == "EVIDENCE_INTEGRITY_OR_VERSION_MISMATCH"
    )


def test_multi_source_replay_reduces_persisted_conflicting_observations() -> None:
    claim = claim_snapshot(unit="USD")
    replay_context = multi_source_replay_context()
    observed_at = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    fetched_at = datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)
    records = []
    for evidence_id, provider, value in (
        ("evidence-source-a", "source-a", 100.0),
        ("evidence-source-b", "source-b", 105.0),
    ):
        raw = json.dumps({
            "provider": provider, "identifier": "SBER", "currency": "USD",
            "price_type": "SPOT", "price": value,
        }).encode()
        record = evidence(
            evidence_id,
            provider,
            f"https://{provider}.example/price",
            raw,
            claim,
        )
        records.append(
            replace(
                record,
                verdict_status="SOURCE_OBSERVATION_CAPTURED",
                parsed_value={
                    "claim": claim,
                    "replay_context": replay_context,
                    "source_status": "E4_VERIFIED",
                    "provider_result": {
                        "observed_last": value,
                        "source_identifiers": ["SBER"],
                        "quote_currency": "USD",
                        "price_type": "SPOT",
                    },
                },
                hash_schema_version="v2",
                fetched_at=fetched_at,
                provider_observed_at=observed_at,
                provider_adapter_version="test-adapter-v1",
            )
        )
        records[-1] = replace(
            records[-1], record_hash=compute_record_hash(records[-1])
        )
    original = replace(
        verdict(
            ("evidence-source-a", "evidence-source-b"),
            status="AMBIGUOUS",
            policy_snapshot=multi_source_policy_snapshot(),
        ),
        verification_mode="MULTI_SOURCE",
        reason_codes=("SOURCE_VALUES_DIVERGE",),
    )

    result = replay_verdict(original, records)

    assert result["replay_status"] == "AMBIGUOUS"
    assert result["matches_original"] is True
    assert result["reason_codes_match"] is True
    assert result["reason_code"] == "POLICY_GATE_AND_REDUCER_REPLAYED"
    assert result["replay_scope"] == "POLICY_GATE_AND_REDUCER"
    assert result["policy_gate_revalidated"] is True
    assert result["reducer"]["usable_observation_count"] == 2
    assert result["reducer"]["independent_cross_check"] is True
    assert result["network_calls"] == 0


def test_multi_source_replay_detects_when_original_conflict_is_not_reproduced() -> None:
    claim = claim_snapshot(unit="USD")
    replay_context = multi_source_replay_context()
    observed_at = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    fetched_at = datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)
    records = []
    for evidence_id, provider in (
        ("evidence-source-a", "source-a"),
        ("evidence-source-b", "source-b"),
    ):
        raw = json.dumps({
            "provider": provider, "identifier": "SBER", "currency": "USD",
            "price_type": "SPOT", "price": 100,
        }).encode()
        record = evidence(
            evidence_id,
            provider,
            f"https://{provider}.example/price",
            raw,
            claim,
        )
        records.append(
            replace(
                record,
                verdict_status="SOURCE_OBSERVATION_CAPTURED",
                parsed_value={
                    "claim": claim,
                    "replay_context": replay_context,
                    "source_status": "E4_VERIFIED",
                    "provider_result": {
                        "observed_last": 100.0,
                        "source_identifiers": ["SBER"],
                        "quote_currency": "USD",
                        "price_type": "SPOT",
                    },
                },
                hash_schema_version="v2",
                fetched_at=fetched_at,
                provider_observed_at=observed_at,
                provider_adapter_version="test-adapter-v1",
            )
        )
        records[-1] = replace(
            records[-1], record_hash=compute_record_hash(records[-1])
        )
    original = replace(
        verdict(
            ("evidence-source-a", "evidence-source-b"),
            status="AMBIGUOUS",
            policy_snapshot=multi_source_policy_snapshot(),
        ),
        verification_mode="MULTI_SOURCE",
        reason_codes=("SOURCE_VALUES_DIVERGE",),
    )

    result = replay_verdict(original, records)

    assert result["replay_status"] == "VERIFIED"
    assert result["matches_original"] is False
    assert result["reason_codes_match"] is False
    assert result["reason_code"] == "POLICY_GATE_AND_REDUCER_REPLAYED"
    assert result["policy_gate_revalidated"] is True
    assert result["network_calls"] == 0


def test_multi_source_replay_fails_closed_without_policy_context() -> None:
    claim = claim_snapshot(unit="USD")
    records = []
    for evidence_id, provider, value in (
        ("evidence-source-a", "source-a", 100.0),
        ("evidence-source-b", "source-b", 105.0),
    ):
        raw = json.dumps({"provider": provider, "price": value}).encode()
        record = evidence(
            evidence_id,
            provider,
            f"https://{provider}.example/price",
            raw,
            claim,
        )
        records.append(
            replace(
                record,
                verdict_status="SOURCE_OBSERVATION_CAPTURED",
                parsed_value={
                    "claim": claim,
                    "source_status": "E4_VERIFIED",
                    "provider_result": {
                        "observed_last": value,
                        "source_identifiers": ["SBER"],
                        "quote_currency": "USD",
                        "price_type": "SPOT",
                    },
                },
            )
        )
    original = replace(
        verdict(("evidence-source-a", "evidence-source-b"), status="AMBIGUOUS"),
        verification_mode="MULTI_SOURCE",
        reason_codes=("SOURCE_VALUES_DIVERGE",),
    )

    result = replay_verdict(original, records)

    assert result["replay_status"] == "INCONCLUSIVE"
    assert result["policy_gate_revalidated"] is False
    assert result["reason_code"] == "POLICY_SNAPSHOT_MISSING"
    assert result["network_calls"] == 0

