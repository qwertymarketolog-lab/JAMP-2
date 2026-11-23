"""Offline source metadata and evidence-first arbitration integration checks."""
import asyncio
import base64
import hashlib
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from alembic import command
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.conflict import SourceObservation
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation
from jamp2.evidence.multisource import (
    MultiSourcePolicy,
    SourceOutcome,
    SourceSpec,
    arbitrate_from_evidence,
)
from jamp2.evidence.policy import SourcePolicyRecord
from jamp2.providers.moex import _moex_capture_metadata


def _claim(kind: str, identifier: str, unit: str) -> NormalizedClaim:
    return NormalizedClaim(
        text="current price claim", subject_kind=kind, subject_identifier=identifier,
        predicate="price", expected_value=100, unit=unit,
    )



def _outcome(source: SourceSpec, identifier: str, *, quote: str,
             price_type: str, calendar: bool | None, age_seconds: int = 5):
    fetched_at = datetime.now(UTC)
    raw = b'{"price":100}'
    captured = {
        "source": source.provider_id,
        "url": f"https://{source.allowed_host}/price",
        "status_code": 200,
        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
        "sha256_body": compute_sha256(raw),
        "fetched_at": fetched_at.isoformat(),
        "provider_observed_at": (
            fetched_at - timedelta(seconds=age_seconds)
        ).isoformat(),
        "body_complete": True,
    }
    details = {
        "observed_last": 100, "source_identifiers": [identifier],
        "quote_currency": quote, "price_type": price_type,
        "market_calendar_valid": calendar,
    }
    asset_kind = "equity_security" if source.provider_id == "moex.iss" else "crypto_asset"
    observation = SourceObservation(
        source_id=source.provider_id,
        fact_key=f"{asset_kind}:{identifier}:price",
        value=100,
        unit=quote,
        usable=True,
    )
    return SourceOutcome(source, "E4_VERIFIED", details, [captured], observation)


def _evidence_records(
    outcomes: tuple[SourceOutcome, ...], claim: NormalizedClaim,
    *, claim_id: str = "claim-offline",
) -> tuple[EvidenceRecord, ...]:
    records: list[EvidenceRecord] = []
    for outcome in outcomes:
        for capture in outcome.raw_evidence:
            raw = base64.b64decode(capture["raw_body_base64"], validate=True)
            fetched_value = capture.get("fetched_at")
            observed_value = capture.get("provider_observed_at")
            fetched_at = (
                datetime.fromisoformat(fetched_value)
                if isinstance(fetched_value, str)
                else None
            )
            observed_at = (
                datetime.fromisoformat(observed_value)
                if isinstance(observed_value, str)
                else None
            )
            source_date_value = capture.get("source_date")
            source_date = (
                source_date_value if isinstance(source_date_value, str) else None
            )
            record = EvidenceRecord(
                evidence_id=f"evidence-{outcome.source.provider_id}",
                claim_id=claim_id,
                provider=outcome.source.provider_id,
                url=capture["url"],
                http_status=capture["status_code"],
                raw_body_bytes=raw,
                raw_sha256=compute_sha256(raw),
                captured_at_utc=fetched_at or datetime.now(UTC),
                parsed_value={
                    "claim": {
                        "text": claim.text,
                        "subject_kind": claim.subject_kind,
                        "subject_identifier": claim.subject_identifier,
                        "predicate": claim.predicate,
                        "expected_value": claim.expected_value,
                        "unit": claim.unit,
                    },
                    "source_status": outcome.status,
                    "provider_result": outcome.details,
                },
                verdict_status=(
                    "SOURCE_OBSERVATION_CAPTURED"
                    if outcome.observation.usable
                    else "SOURCE_RESPONSE_CAPTURED"
                ),
                parent_hash=None,
                body_complete=True,
                provider_adapter_version=outcome.source.adapter_version,
                parser_version="jamp-provider-json-v1",
                hash_schema_version="v2",
                fetched_at=fetched_at,
                provider_observed_at=observed_at,
                source_date=source_date,
            )
            records.append(replace(record, record_hash=compute_record_hash(record)))
    return tuple(records)


def _policy(source: SourceSpec, *, asset: str, identifier: str, quote: str,
            price_type: str, calendar_required: bool) -> MultiSourcePolicy:
    source_policy = SourcePolicyRecord(
        policy_id="offline-e2e", version=1, asset_class=asset,
        required_identifiers=(identifier,), market_calendar_required=calendar_required,
        quote_currency=quote, price_type=price_type,
        required_sources=(source.provider_id,), independence_group="offline-e2e",
        max_age_seconds=3600,
        max_age_by_source={
            source.provider_id: {"moex.iss": 1200, "coingecko": 300}[source.provider_id]
        },
        max_time_skew_seconds=5, verification_mode="SINGLE_SOURCE",
    )
    return MultiSourcePolicy("offline-e2e", "v1", (source,), source_policy=source_policy)



def test_moex_without_confirmed_calendar_is_ambiguous_and_blocked():
    source = SourceSpec("moex", "moex.iss", "iss.moex.com", "moex-iss-adapter-v1")
    claim = _claim("equity_security", "SBER", "RUB")
    outcome = _outcome(source, "SBER", quote="RUB", price_type="LAST_DEAL", calendar=None)
    result = arbitrate_from_evidence(
        claim,
        _policy(source, asset="equity_security", identifier="SBER", quote="RUB",
                price_type="LAST_DEAL", calendar_required=True),
        (outcome,),
        _evidence_records((outcome,), claim),
        expected_claim_id="claim-offline",
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert "SOURCE_MARKET_CALENDAR_UNCONFIRMED" in result.reason_codes
    assert "ERR_QUORUM_NOT_MET" in result.reason_codes
    assert result.reducer["status"] == "POLICY_GATE_BLOCKED"



def test_fresh_coingecko_source_is_verified_without_network():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    result = arbitrate_from_evidence(
        claim,
        _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                price_type="SPOT", calendar_required=False),
        (outcome,),
        _evidence_records((outcome,), claim),
        expected_claim_id="claim-offline",
    )
    assert result.verdict_status == "VERIFIED"
    assert result.verification_mode == "SINGLE_SOURCE"



def test_arbitration_refuses_empty_ledger_evidence():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    with pytest.raises(IntegrityViolation, match="ledger records are required"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (outcome,),
            (),
            expected_claim_id="claim-offline",
        )


def test_arbitration_rejects_tampered_record_hash():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    valid_record = _evidence_records((outcome,), claim)[0]
    tampered = replace(valid_record, parsed_value={"provider_result": {"observed_last": 101}})
    with pytest.raises(IntegrityViolation, match="record_hash is missing or invalid"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (outcome,),
            (tampered,),
            expected_claim_id="claim-offline",
        )


def test_arbitration_rejects_outcome_not_matching_ledger_payload():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    records = _evidence_records((outcome,), claim)
    changed = replace(outcome, details={**outcome.details, "observed_last": 101})
    with pytest.raises(IntegrityViolation, match="does not match exactly one persisted"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (changed,),
            records,
            expected_claim_id="claim-offline",
        )


def test_stale_coingecko_source_is_ambiguous_not_inconclusive():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(
        source, "bitcoin", quote="USD", price_type="SPOT", calendar=None, age_seconds=301
    )
    result = arbitrate_from_evidence(
        claim,
        _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                price_type="SPOT", calendar_required=False),
        (outcome,),
        _evidence_records((outcome,), claim),
        expected_claim_id="claim-offline",
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.reason_codes


DATABASE_URL = os.getenv("DATABASE_URL")



def _connect_kwargs(url: URL, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host,
        "port": url.port or 5432,
        "database": database,
    }


@pytest.fixture
def populated_migration_database(
    monkeypatch: pytest.MonkeyPatch,
):
    assert DATABASE_URL is not None
    base_url = make_url(DATABASE_URL)
    database_name = f"jamp2_source_meta_{uuid.uuid4().hex}"
    admin_kwargs = _connect_kwargs(base_url, "postgres")

    async def create_database() -> None:
        connection = await asyncpg.connect(**admin_kwargs)
        try:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await connection.close()

    asyncio.run(create_database())
    database_url = base_url.set(database=database_name)
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(hide_password=False))
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        yield config, database_url
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(**admin_kwargs)
            try:
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    database_name,
                )
                await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
            finally:
                await connection.close()

        asyncio.run(drop_database())



@pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is required for PostgreSQL migration tests"
)
def test_migration_008_preserves_populated_evidence_and_verdict_ledgers(
    populated_migration_database,
) -> None:
    config, database_url = populated_migration_database
    command.upgrade(config, "007_pending_lease")

    async def insert_legacy_rows() -> None:
        connection = await asyncpg.connect(
            **_connect_kwargs(database_url, database_url.database or "")
        )
        try:
            raw_body = b'{"price":100}'
            await connection.execute(
                """
                INSERT INTO evidence (
                    evidence_id, claim_id, provider, url, http_status, raw_body_bytes,
                    raw_sha256, captured_at_utc, parsed_value, verdict_status,
                    canonicalization_version, body_complete, provider_adapter_version,
                    parser_version, parent_hash, record_hash
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15,$16
                )
                """,
                "evidence-legacy", "claim-legacy", "test-provider",
                "https://example.test/evidence", 200, raw_body,
                hashlib.sha256(raw_body).hexdigest(), datetime.now(UTC),
                '{"price":100}', "OBSERVATION_CAPTURED", "jamp-json-nfc-v1",
                True, "provider-adapter-v1", "provider-parser-v1", None, "a" * 64,
            )
            await connection.execute(
                """
                INSERT INTO verdicts (
                    verdict_id, claim_id, status, evidence_ids, policy_id, policy_version,
                    code_version, canonicalization_version, decided_at_utc, reason_codes,
                    explanation, parent_hash, record_hash
                ) VALUES (
                    $1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13
                )
                """,
                "verdict-legacy", "claim-legacy", "VERIFIED", '["evidence-legacy"]',
                "test-policy", "test-v1", "test-code", "jamp-json-nfc-v1",
                datetime.now(UTC), '["E4_VERIFIED"]', "legacy verdict", None, "b" * 64,
            )
        finally:
            await connection.close()

    asyncio.run(insert_legacy_rows())
    command.upgrade(config, "008_source_metadata")

    async def inspect_migrated_rows() -> tuple[dict, dict]:
        connection = await asyncpg.connect(
            **_connect_kwargs(database_url, database_url.database or "")
        )
        try:
            evidence = await connection.fetchrow(
                "SELECT hash_schema_version, fetched_at, provider_observed_at, source_date, "
                "record_hash FROM evidence WHERE evidence_id = 'evidence-legacy'"
            )
            verdict = await connection.fetchrow(
                "SELECT verification_mode, record_hash FROM verdicts "
                "WHERE verdict_id = 'verdict-legacy'"
            )
            return dict(evidence), dict(verdict)
        finally:
            await connection.close()

    evidence, verdict = asyncio.run(inspect_migrated_rows())
    assert evidence == {
        "hash_schema_version": "v1",
        "fetched_at": None,
        "provider_observed_at": None,
        "source_date": None,
        "record_hash": "a" * 64,
    }
    assert verdict == {"verification_mode": None, "record_hash": "b" * 64}



def test_unknown_evidence_hash_schema_is_integrity_violation():
    with pytest.raises(IntegrityViolation, match="unsupported evidence hash schema"):
        EvidenceRecord(
            evidence_id="e-1", claim_id="c-1", provider="p",
            url="https://example.test/e", http_status=200,
            raw_body_bytes=b"{}", raw_sha256=compute_sha256(b"{}"),
            captured_at_utc=datetime.now(UTC), parsed_value={},
            verdict_status="CAPTURED", parent_hash=None,
            hash_schema_version="v3",
        )



def test_v2_evidence_hash_binds_source_capture_metadata():
    captured = datetime.now(UTC)
    record = EvidenceRecord(
        evidence_id="e-v2", claim_id="c-v2", provider="moex.iss",
        url="https://iss.moex.com/iss/e", http_status=200,
        raw_body_bytes=b"{}", raw_sha256=compute_sha256(b"{}"),
        captured_at_utc=captured, parsed_value={}, verdict_status="CAPTURED",
        parent_hash=None, hash_schema_version="v2",
        fetched_at=captured, provider_observed_at=captured,
        source_date="2026-11-22",
    )
    changed = EvidenceRecord(
        evidence_id=record.evidence_id, claim_id=record.claim_id,
        provider=record.provider, url=record.url, http_status=record.http_status,
        raw_body_bytes=record.raw_body_bytes, raw_sha256=record.raw_sha256,
        captured_at_utc=record.captured_at_utc, parsed_value=record.parsed_value,
        verdict_status=record.verdict_status, parent_hash=record.parent_hash,
        hash_schema_version="v2", fetched_at=record.fetched_at,
        provider_observed_at=captured + timedelta(seconds=1),
        source_date=record.source_date,
    )
    assert compute_record_hash(record) != compute_record_hash(changed)



def test_moex_systime_is_optional_and_uses_moscow_timezone_when_full_timestamp():
    no_time = _moex_capture_metadata({
        "marketdata": {
            "columns": ["TRADEDATE", "SYSTIME"],
            "data": [["2026-11-22", None]],
        }
    })
    assert no_time == {"source_date": "2026-11-22"}

    with_time = _moex_capture_metadata({
        "marketdata": {
            "columns": ["TRADEDATE", "SYSTIME"],
            "data": [["2026-11-22", "2026-11-22T13:00:00"]],
        }
    })
    assert with_time["source_date"] == "2026-11-22"
    assert with_time["provider_observed_at"] == "2026-11-22T10:00:00+00:00"



def test_arbitration_rejects_evidence_from_another_claim_even_when_payload_matches():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    foreign_records = _evidence_records((outcome,), claim, claim_id="claim-B")

    with pytest.raises(IntegrityViolation, match="claim_id does not match"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (outcome,),
            foreign_records,
            expected_claim_id="claim-A",
        )


def test_arbitration_rejects_mixed_claim_ids_even_when_payload_matches():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    own_record = _evidence_records((outcome,), claim, claim_id="claim-A")[0]
    foreign_record = _evidence_records((outcome,), claim, claim_id="claim-B")[0]
    foreign_record = replace(
        foreign_record, evidence_id="evidence-foreign", record_hash=None
    )
    foreign_record = replace(
        foreign_record, record_hash=compute_record_hash(foreign_record)
    )

    with pytest.raises(IntegrityViolation, match="claim_id does not match"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (outcome,),
            (own_record, foreign_record),
            expected_claim_id="claim-A",
        )


@pytest.mark.parametrize(
    ("drop_fetched_at", "drop_provider_observed_at", "expected_verdict"),
    [
        (False, False, "VERIFIED"),
        (False, True, "AMBIGUOUS"),
        (True, False, "AMBIGUOUS"),
        (True, True, "AMBIGUOUS"),
    ],
)
def test_v2_schema_version_does_not_certify_capture_completeness(
    drop_fetched_at: bool,
    drop_provider_observed_at: bool,
    expected_verdict: str,
) -> None:
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    capture = dict(outcome.raw_evidence[0])
    if drop_fetched_at:
        capture["fetched_at"] = None
    if drop_provider_observed_at:
        capture["provider_observed_at"] = None
    outcome = replace(outcome, raw_evidence=[capture])
    records = _evidence_records((outcome,), claim)

    assert records[0].hash_schema_version == "v2"
    if drop_fetched_at:
        assert records[0].fetched_at is None
    else:
        assert records[0].fetched_at is not None
    assert (
        records[0].provider_observed_at is None
        if drop_provider_observed_at
        else records[0].provider_observed_at is not None
    )
    result = arbitrate_from_evidence(
        claim,
        _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                price_type="SPOT", calendar_required=False),
        (outcome,),
        records,
        expected_claim_id="claim-offline",
    )
    assert result.verdict_status == expected_verdict
    if expected_verdict == "AMBIGUOUS":
        assert "SOURCE_TIMESTAMP_OR_BODY_INCOMPLETE" in result.reason_codes
        assert result.reducer["status"] == "POLICY_GATE_BLOCKED"


def _record_with_source_date(source_date: object) -> EvidenceRecord:
    captured = datetime.now(UTC)
    return EvidenceRecord(
        evidence_id="source-date-test",
        claim_id="claim-source-date",
        provider="moex.iss",
        url="https://iss.moex.com/iss/evidence",
        http_status=200,
        raw_body_bytes=b"{}",
        raw_sha256=compute_sha256(b"{}"),
        captured_at_utc=captured,
        parsed_value={},
        verdict_status="SOURCE_RESPONSE_CAPTURED",
        parent_hash=None,
        hash_schema_version="v2",
        source_date=cast(Any, source_date),
    )


@pytest.mark.parametrize(
    "source_date",
    ["2026-02-30", "2026/02/28", "2026-W09-6", "2026-2-8", ""],
)
def test_source_date_rejects_noncanonical_or_impossible_calendar_dates(
    source_date: str,
) -> None:
    with pytest.raises(IntegrityViolation, match="source_date"):
        _record_with_source_date(source_date)


def test_source_date_rejects_non_string_values() -> None:
    with pytest.raises(IntegrityViolation, match="source_date"):
        _record_with_source_date(20261122)



def test_arbitration_rejects_non_string_source_date_capture():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    claim = _claim("crypto_asset", "bitcoin", "USD")
    outcome = _outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None)
    capture = dict(outcome.raw_evidence[0])
    capture["source_date"] = 20261122
    outcome = replace(outcome, raw_evidence=[capture])
    records = _evidence_records((outcome,), claim)

    with pytest.raises(IntegrityViolation, match="source_date must be a string or null"):
        arbitrate_from_evidence(
            claim,
            _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                    price_type="SPOT", calendar_required=False),
            (outcome,),
            records,
            expected_claim_id="claim-offline",
        )
