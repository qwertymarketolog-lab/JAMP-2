"""Real-PostgreSQL tests for immutable verdict persistence and evidence linkage."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import IntegrityViolation, PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for PostgreSQL tests",
)


def make_evidence(evidence_id: str = "evidence-verdict-1") -> EvidenceRecord:
    raw = b'{"price":100}'
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-verdict",
        provider="coingecko",
        url="https://api.coingecko.com/api/v3/simple/price",
        http_status=200,
        raw_body_bytes=raw,
        raw_sha256=compute_sha256(raw),
        captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        parsed_value={"bitcoin": {"usd": 100}},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
    )


def make_verdict(evidence_id: str = "evidence-verdict-1") -> VerdictRecord:
    return VerdictRecord(
        verdict_id="verdict-postgres-1",
        claim_id="claim-verdict",
        status="VERIFIED",
        evidence_ids=(evidence_id,),
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="integration-test-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Test verdict linked to captured provider evidence.",
    )


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE idempotency_records"))
        await connection.execute(text("TRUNCATE TABLE verdicts RESTART IDENTITY"))
        await connection.execute(text("TRUNCATE TABLE evidence RESTART IDENTITY"))


def test_verdict_persists_references_evidence_and_survives_restart() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        await ledger.append(make_evidence())
        stored = await ledger.append_verdict(make_verdict())
        assert stored.parent_hash is None
        assert await ledger.verify_verdict_chain()
        await ledger.close()

        restarted = PostgresLedger(DATABASE_URL)
        found = await restarted.get_verdict("verdict-postgres-1")
        assert found is not None
        assert found.status == "VERIFIED"
        assert found.evidence_ids == ("evidence-verdict-1",)
        assert found.policy_version == "provider-defaults-v1"
        assert found.code_version == "integration-test-sha"
        assert await restarted.verify_verdict_chain()
        await restarted.close()

    asyncio.run(run())


def test_verdict_rejects_missing_or_cross_claim_evidence() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        with pytest.raises(IntegrityViolation, match="missing evidence"):
            await ledger.append_verdict(make_verdict("not-present"))
        await ledger.append(make_evidence())
        wrong_claim = replace(make_verdict(), claim_id="different-claim")
        with pytest.raises(IntegrityViolation, match="another claim"):
            await ledger.append_verdict(wrong_claim)
        await ledger.close()

    asyncio.run(run())


def test_verdict_database_trigger_rejects_update_and_delete() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        await ledger.append(make_evidence())
        await ledger.append_verdict(make_verdict())
        with pytest.raises(DBAPIError):
            async with ledger.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE verdicts SET status = 'CONTRADICTED' "
                        "WHERE verdict_id = 'verdict-postgres-1'"
                    )
                )
        with pytest.raises(DBAPIError):
            async with ledger.engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM verdicts WHERE verdict_id = 'verdict-postgres-1'")
                )
        assert await ledger.get_verdict("verdict-postgres-1") is not None
        await ledger.close()

    asyncio.run(run())


def test_verdict_hash_tampering_is_detected_on_read() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        await ledger.append(make_evidence())
        await ledger.append_verdict(make_verdict())
        async with ledger.engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(
                text(
                    "UPDATE verdicts SET explanation = 'rewritten' "
                    "WHERE verdict_id = 'verdict-postgres-1'"
                )
            )
        with pytest.raises(IntegrityViolation, match="verdict record hash mismatch"):
            await ledger.get_verdict("verdict-postgres-1")
        await ledger.close()

    asyncio.run(run())
