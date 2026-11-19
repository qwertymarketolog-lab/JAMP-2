"""HTTP contract tests against the PostgreSQL-backed evidence API."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from jamp2.api.app import create_app
from jamp2.evidence.digest import compute_sha256
from jamp2.ledger.postgres import LedgerUnavailable, PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for API integration tests",
)


def payload(evidence_id: str) -> dict[str, object]:
    raw = b'{"price":100}'
    return {
        "evidence_id": evidence_id,
        "claim_id": "api-claim",
        "provider": "moex.iss",
        "url": "https://example.test/price",
        "http_status": 200,
        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
        "raw_sha256": compute_sha256(raw),
        "captured_at_utc": datetime(2026, 11, 20, tzinfo=UTC).isoformat(),
        "parsed_value": {"price": 100},
        "verdict_status": "E4_VERIFIED",
    }


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE evidence RESTART IDENTITY"))


def test_api_create_read_duplicate_and_missing() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(ledger)),
            base_url="http://test",
        ) as client:
            created = await client.post("/v1/evidence", json=payload("api-record"))
            assert created.status_code == 201
            assert created.json()["parent_hash"] is None
            fetched = await client.get("/v1/evidence/api-record")
            assert fetched.status_code == 200
            assert fetched.json()["record_hash"] == created.json()["record_hash"]
            duplicate = await client.post("/v1/evidence", json=payload("api-record"))
            assert duplicate.status_code == 409
            assert duplicate.json() == {"error": "EVIDENCE_ALREADY_EXISTS"}
            missing = await client.get("/v1/evidence/not-found")
            assert missing.status_code == 404
            assert missing.json() == {"error": "EVIDENCE_NOT_FOUND"}
        await ledger.close()
    asyncio.run(run())


def test_api_returns_503_when_ledger_is_unavailable() -> None:
    class UnavailableLedger:
        async def append(self, record: object) -> object:
            raise LedgerUnavailable("database offline")

        async def get(self, evidence_id: str) -> None:
            raise LedgerUnavailable("database offline")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(UnavailableLedger())),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/evidence/unavailable")
            assert response.status_code == 503
            assert response.json() == {"error": "LEDGER_UNAVAILABLE"}
    asyncio.run(run())


def test_api_returns_500_on_integrity_violation() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        await ledger.append(
            __import__("jamp2.evidence.model", fromlist=["EvidenceRecord"]).EvidenceRecord(
                evidence_id="api-tamper",
                claim_id="api-claim",
                provider="moex.iss",
                url="https://example.test/price",
                http_status=200,
                raw_body_bytes=b'{"price":100}',
                raw_sha256=compute_sha256(b'{"price":100}'),
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(ledger)),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/evidence/api-tamper")
            assert response.status_code == 500
            assert response.json() == {"error": "INTEGRITY_VIOLATION"}
        await ledger.close()
    asyncio.run(run())
