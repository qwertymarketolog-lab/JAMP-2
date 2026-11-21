"""Real-PostgreSQL integration tests for ledger TRUNCATE protection."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for PostgreSQL tests",
)


def make_evidence() -> EvidenceRecord:
    raw = b'{"price":100}'
    return EvidenceRecord(
        evidence_id="truncate-evidence",
        claim_id="truncate-claim",
        provider="test-provider",
        url="https://example.test/evidence",
        http_status=200,
        raw_body_bytes=raw,
        raw_sha256=compute_sha256(raw),
        captured_at_utc=datetime(2026, 11, 21, tzinfo=UTC),
        parsed_value={"price": 100},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
    )


def make_verdict() -> VerdictRecord:
    return VerdictRecord(
        verdict_id="truncate-verdict",
        claim_id="truncate-claim",
        status="VERIFIED",
        evidence_ids=("truncate-evidence",),
        policy_id="test-policy",
        policy_version="test-v1",
        code_version="truncate-protection-test",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 21, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Verdict used to verify TRUNCATE protection.",
    )


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        # Test-only bypass; production code never disables ledger triggers.
        await connection.execute(text("ALTER TABLE evidence DISABLE TRIGGER USER"))
        await connection.execute(text("ALTER TABLE verdicts DISABLE TRIGGER USER"))
        try:
            await connection.execute(text("TRUNCATE TABLE idempotency_records"))
            await connection.execute(text("TRUNCATE TABLE verdicts, evidence RESTART IDENTITY"))
        finally:
            await connection.execute(text("ALTER TABLE evidence ENABLE TRIGGER USER"))
            await connection.execute(text("ALTER TABLE verdicts ENABLE TRIGGER USER"))


def test_truncate_evidence_is_blocked_and_record_survives() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        record = await ledger.append(make_evidence())
        with pytest.raises(DBAPIError, match="TRUNCATE not allowed"):
            async with ledger.engine.begin() as connection:
                await connection.execute(text("TRUNCATE TABLE evidence"))
        found = await ledger.get(record.evidence_id)
        assert found is not None
        assert found.evidence_id == record.evidence_id
        await ledger.close()

    asyncio.run(run())


def test_truncate_verdicts_is_blocked_and_record_survives() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        await ledger.append(make_evidence())
        verdict = await ledger.append_verdict(make_verdict())
        with pytest.raises(DBAPIError, match="TRUNCATE not allowed"):
            async with ledger.engine.begin() as connection:
                await connection.execute(text("TRUNCATE TABLE verdicts"))
        found = await ledger.get_verdict(verdict.verdict_id)
        assert found is not None
        assert found.verdict_id == verdict.verdict_id
        await ledger.close()

    asyncio.run(run())
