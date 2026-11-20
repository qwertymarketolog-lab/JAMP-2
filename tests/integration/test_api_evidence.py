"""HTTP contract tests for authenticated server-side Claim verification."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from jamp2.api.app import create_app
from jamp2.domain.claim import Claim
from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.ledger.postgres import LedgerUnavailable, PostgresLedger
from jamp2.providers.base import ProviderResult

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for API integration tests",
)


class StubProvider:
    """Deterministic adapter fixture; HTTP transport is tested separately."""

    def verify_claim(self, claim: Claim) -> ProviderResult:
        assert claim.type == "price"
        assert claim.ticker == "SBER"
        raw = b'{"marketdata":{"LAST":300}}'
        return ProviderResult(
            "E4_VERIFIED",
            {"observed_last": 300.0, "trade_date": "2026-11-19"},
            [
                {
                    "source": "moex.iss",
                    "url": "https://iss.moex.com/iss/marketdata.json",
                    "status_code": 200,
                    "body_complete": True,
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "sha256_body": compute_sha256(raw),
                }
            ],
        )


def claim_payload() -> dict[str, object]:
    return {
        "text": "SBER share price is 300 RUB",
        "subject": {"kind": "moex_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
    }


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE evidence RESTART IDENTITY"))


def test_api_server_verifies_claim_persists_raw_evidence_and_reads_it() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        application = create_app(
            ledger,
            api_key="integration-test-key",
            providers={"moex": StubProvider()},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={"X-API-Key": "integration-test-key"},
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            body = created.json()
            assert body["verdict"] == "VERIFIED"
            assert body["provider"] == "moex.iss"
            assert body["evidence_count"] == 1
            evidence_id = body["evidence_ids"][0]

            fetched = await client.get(f"/v1/evidence/{evidence_id}")
            assert fetched.status_code == 200
            evidence = fetched.json()
            raw = base64.b64decode(evidence["raw_body_base64"], validate=True)
            assert raw == b'{"marketdata":{"LAST":300}}'
            assert evidence["raw_sha256"] == compute_sha256(raw)
            assert evidence["captured_at_utc"].endswith("+00:00")

            missing = await client.get("/v1/evidence/not-found")
            assert missing.status_code == 404
            assert missing.json() == {"error": "EVIDENCE_NOT_FOUND"}
        await ledger.close()

    asyncio.run(run())


def test_api_returns_503_when_ledger_is_unavailable() -> None:
    class UnavailableLedger:
        async def append(self, record: EvidenceRecord) -> EvidenceRecord:
            raise LedgerUnavailable("database offline")

        async def get(self, evidence_id: str) -> None:
            raise LedgerUnavailable("database offline")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(UnavailableLedger(), api_key="integration-test-key")
            ),
            base_url="http://test",
            headers={"X-API-Key": "integration-test-key"},
        ) as client:
            response = await client.get("/v1/evidence/unavailable")
            assert response.status_code == 503
            assert response.json() == {"error": "LEDGER_UNAVAILABLE"}

    asyncio.run(run())


def test_api_returns_500_on_integrity_violation() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        raw = b'{"price":100}'
        await ledger.append(
            EvidenceRecord(
                evidence_id="api-tamper",
                claim_id="api-claim",
                provider="moex.iss",
                url="https://iss.moex.com/iss/price",
                http_status=200,
                raw_body_bytes=raw,
                raw_sha256=compute_sha256(raw),
                captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
                parsed_value={"price": 100},
                verdict_status="E4_VERIFIED",
                parent_hash=None,
            )
        )
        async with ledger.engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(
                text("UPDATE evidence SET raw_body_bytes = :body WHERE evidence_id = :id"),
                {"body": b'{"price":101}', "id": "api-tamper"},
            )
        application = create_app(ledger, api_key="integration-test-key")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={"X-API-Key": "integration-test-key"},
        ) as client:
            response = await client.get("/v1/evidence/api-tamper")
            assert response.status_code == 500
            assert response.json() == {"error": "INTEGRITY_VIOLATION"}
        await ledger.close()

    asyncio.run(run())
