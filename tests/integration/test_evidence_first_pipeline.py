"""PostgreSQL-backed end-to-end evidence-first pipeline contract tests."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from jamp2.api.app import create_app
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.multisource import (
    MultiSourcePolicy,
    SourceSpec,
)
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import (
    EvidenceRow,
    IntegrityViolation,
    PostgresLedger,
)
from jamp2.providers.base import ProviderResult

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for Evidence-First PostgreSQL integration tests",
)


class StubProvider:
    """Deterministic provider; TEST_STUB marks this adapter as non-production."""

    _source = "TEST_STUB"

    def __init__(self, *, missing_observed_at: bool = False) -> None:
        self.calls = 0
        self.missing_observed_at = missing_observed_at

    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        self.calls += 1
        assert claim.subject_identifier == "SBER"
        captured_at = datetime.now(UTC).isoformat()
        observed_at = None if self.missing_observed_at else captured_at
        marketdata = (
            b'{"marketdata":{"columns":["SECID","LAST"],"data":[["SBER",300]]}}'
        )
        securities = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'

        def capture(path: str, raw: bytes) -> dict[str, object]:
            return {
                "source": "moex.iss",
                "url": f"https://iss.moex.com/iss/{path}.json",
                "status_code": 200,
                "body_complete": True,
                "provider_observed_at": observed_at,
                "fetched_at": captured_at,
                "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                "sha256_body": compute_sha256(raw),
            }

        return ProviderResult(
            "E4_VERIFIED",
            {
                "observed_last": 300.0,
                "trade_date": "2026-11-24",
                "source_identifiers": ["SBER"],
                "quote_currency": "RUB",
                "price_type": "LAST_DEAL",
                "market_calendar_valid": True,
            },
            [
                capture("marketdata", marketdata),
                capture("securities", securities),
            ],
        )


def claim_payload() -> dict[str, object]:
    return {
        "text": "SBER share price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
    }


def make_verdict(evidence_id: str) -> VerdictRecord:
    return VerdictRecord(
        verdict_id="missing-evidence-verdict",
        claim_id="missing-evidence-claim",
        status="VERIFIED",
        evidence_ids=(evidence_id,),
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="j2-002-integration-test",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime.now(UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Negative integration test for a missing evidence reference.",
    )


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE evidence DISABLE TRIGGER USER"))
        await connection.execute(text("ALTER TABLE verdicts DISABLE TRIGGER USER"))
        try:
            await connection.execute(text("TRUNCATE TABLE idempotency_records"))
            await connection.execute(text("TRUNCATE TABLE verdicts, evidence RESTART IDENTITY"))
        finally:
            await connection.execute(text("ALTER TABLE evidence ENABLE TRIGGER USER"))
            await connection.execute(text("ALTER TABLE verdicts ENABLE TRIGGER USER"))


def make_app(ledger: PostgresLedger, provider: StubProvider):
    return create_app(
        ledger,
        api_key="j2-002-integration-key",
        providers={"moex": provider},
        code_version="j2-002-integration-test",
    )


def test_full_claim_to_verdict_with_evidence_persisted_first() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-full-pipeline-0001",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=claim_payload())
            assert response.status_code == 201
            payload = response.json()
            assert payload["verdict"] == "VERIFIED"
            assert payload["evidence_count"] == 2
            records = await ledger.list_by_claim(payload["claim_id"])
            verdict = await ledger.get_verdict(payload["verdict_id"])
            assert len(records) == 2
            assert verdict is not None
            assert set(verdict.evidence_ids) == {record.evidence_id for record in records}
            async with ledger.engine.connect() as connection:
                evidence_count = await connection.scalar(
                    text("SELECT count(*) FROM evidence WHERE claim_id = :claim_id"),
                    {"claim_id": payload["claim_id"]},
                )
                verdict_count = await connection.scalar(
                    text("SELECT count(*) FROM verdicts WHERE claim_id = :claim_id"),
                    {"claim_id": payload["claim_id"]},
                )
            assert evidence_count == 2
            assert verdict_count == 1
            assert provider.calls == 1
        await ledger.close()

    asyncio.run(run())


def test_policy_gate_blocks_reducer_on_invalid_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        import jamp2.evidence.multisource as multisource

        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider(missing_observed_at=True)
        reducer_calls = 0
        original_reducer = multisource.detect_conflicts

        def reducer_spy(observations):
            nonlocal reducer_calls
            reducer_calls += 1
            return original_reducer(observations)

        monkeypatch.setattr(multisource, "detect_conflicts", reducer_spy)
        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-invalid-metadata-0001",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=claim_payload())
            assert response.status_code == 201
            payload = response.json()
            assert payload["verdict"] == "AMBIGUOUS"
            assert payload["arbitration"]["reducer"]["status"] == "POLICY_GATE_BLOCKED"
            assert "SOURCE_TIMESTAMP_OR_BODY_INCOMPLETE" in payload["arbitration"]["reason_codes"]
            assert reducer_calls == 0
            assert await ledger.verify_chain()
            assert await ledger.verify_verdict_chain()
        await ledger.close()

    asyncio.run(run())


def test_replay_uses_persisted_evidence_no_network() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-replay-no-network-0001",
            },
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            verdict_id = created.json()["verdict_id"]
            calls_before_replay = provider.calls
            replay = await client.post(f"/v1/verdicts/{verdict_id}/replay")
            assert replay.status_code == 200
            assert replay.json()["network_calls"] == 0
            assert provider.calls == calls_before_replay == 1
        await ledger.close()

    asyncio.run(run())


def test_verdict_evidence_ids_link_to_persisted_records() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-evidence-links-0001",
            },
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            verdict = await ledger.get_verdict(created.json()["verdict_id"])
            assert verdict is not None
            records = await ledger.list_by_claim(verdict.claim_id)
            persisted_ids = {record.evidence_id for record in records}
            assert set(verdict.evidence_ids) <= persisted_ids
            assert len(verdict.evidence_ids) == len(persisted_ids) == 2
        await ledger.close()

    asyncio.run(run())


def test_tampered_evidence_breaks_chain() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-tamper-evidence-0001",
            },
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            evidence_id = created.json()["evidence_ids"][0]
            async with ledger.engine.begin() as connection:
                await connection.execute(text("SET LOCAL session_replication_role = replica"))
                await connection.execute(
                    text(
                        "UPDATE evidence SET raw_body_bytes = :body "
                        "WHERE evidence_id = :evidence_id"
                    ),
                    {"body": b'{"tampered":true}', "evidence_id": evidence_id},
                )
            assert await ledger.verify_chain() is False
        await ledger.close()

    asyncio.run(run())


def test_missing_evidence_rejects_verdict() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        with pytest.raises(IntegrityViolation, match="missing evidence"):
            await ledger.append_verdict(make_verdict("evidence-does-not-exist"))
        assert await ledger.verify_verdict_chain()
        await ledger.close()

    asyncio.run(run())


def test_evidence_append_before_verdict_append(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        import jamp2.evidence.multisource as multisource

        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        events: list[str] = []
        original_append = ledger.append
        original_list_by_claim = ledger.list_by_claim
        original_append_verdict = ledger.append_verdict
        original_gate = multisource.evaluate_source_policy
        original_reducer = multisource.detect_conflicts

        async def append_spy(record):
            stored = await original_append(record)
            events.append("append")
            return stored

        async def read_back_spy(claim_id: str):
            records = await original_list_by_claim(claim_id)
            events.append("read_back")
            assert len(records) == 2
            return records

        def gate_spy(policy, observations, *, now):
            events.append("gate")
            return original_gate(policy, observations, now=now)

        def reducer_spy(observations):
            events.append("reducer")
            return original_reducer(observations)

        async def append_verdict_spy(record):
            async with ledger.engine.connect() as connection:
                persisted_count = await connection.scalar(
                    text("SELECT count(*) FROM evidence WHERE claim_id = :claim_id"),
                    {"claim_id": record.claim_id},
                )
            assert persisted_count == len(record.evidence_ids) == 2
            events.append("append_verdict")
            return await original_append_verdict(record)

        monkeypatch.setattr(ledger, "append", append_spy)
        monkeypatch.setattr(ledger, "list_by_claim", read_back_spy)
        monkeypatch.setattr(ledger, "append_verdict", append_verdict_spy)
        monkeypatch.setattr(multisource, "evaluate_source_policy", gate_spy)
        monkeypatch.setattr(multisource, "detect_conflicts", reducer_spy)

        app = make_app(ledger, provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "j2-002-integration-key",
                "Idempotency-Key": "j2-002-order-of-operations-0001",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=claim_payload())
            assert response.status_code == 201
            assert response.json()["verdict"] == "VERIFIED"

        assert events == [
            "append",
            "append",
            "read_back",
            "gate",
            "reducer",
            "append_verdict",
        ]
        await ledger.close()

    asyncio.run(run())
