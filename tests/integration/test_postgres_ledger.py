"""Real-PostgreSQL integration tests for persistence and hash-chain safety."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.ledger.postgres import IntegrityViolation, PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is required for PostgreSQL tests")


def make_record(evidence_id: str, body: bytes | None = None) -> EvidenceRecord:
    raw = body or b'{"value":1}'
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-postgres",
        provider="test-provider",
        url="https://example.test/evidence",
        http_status=200,
        raw_body_bytes=raw,
        raw_sha256=compute_sha256(raw),
        captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        parsed_value={"value": 1},
        verdict_status="E4_VERIFIED",
        parent_hash=None,
    )


def reset_ledger(ledger: PostgresLedger) -> None:
    async def run() -> None:
        async with ledger.engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE evidence RESTART IDENTITY"))
    asyncio.run(run())


def test_postgres_append_get_and_restart_persistence() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await ledger.append(make_record("persisted-record"))
        await ledger.close()
        restarted = PostgresLedger(DATABASE_URL)
        found = await restarted.get("persisted-record")
        assert found is not None
        assert found.evidence_id == "persisted-record"
        assert found.parent_hash is None
        assert await restarted.verify_chain()
        await restarted.close()
    asyncio.run(run())


def test_concurrent_appends_are_serialized_into_one_valid_chain() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        reset_ledger(ledger)
        stored = await asyncio.gather(
            *(ledger.append(make_record(f"parallel-{index}")) for index in range(10))
        )
        assert len(stored) == 10
        assert await ledger.verify_chain()
        await ledger.close()
    asyncio.run(run())


def test_direct_sql_tampering_is_detected_on_read() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        reset_ledger(ledger)
        await ledger.append(make_record("tamper-me"))
        async with ledger.engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(
                text("UPDATE evidence SET raw_body_bytes = :body WHERE evidence_id = :id"),
                {"body": base64.b64decode("eyJ2YWx1ZSI6Mn0="), "id": "tamper-me"},
            )
        with pytest.raises(IntegrityViolation):
            await ledger.get("tamper-me")
        await ledger.close()
    asyncio.run(run())


def test_database_trigger_rejects_update_and_delete() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        reset_ledger(ledger)
        await ledger.append(make_record("immutable"))
        with pytest.raises(DBAPIError):
            async with ledger.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE evidence SET verdict_status = 'CHANGED' WHERE evidence_id = 'immutable'")
                )
        await ledger.close()
    asyncio.run(run())
