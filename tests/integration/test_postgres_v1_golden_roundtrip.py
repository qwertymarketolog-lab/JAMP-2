"""PostgreSQL round-trip proof for the frozen v1 Evidence and Verdict vectors."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jamp2.evidence.digest import (
    compute_record_hash,
    compute_sha256,
    compute_verdict_hash,
)
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for PostgreSQL tests",
)

RAW_BODY = b'{"price":100}'
RAW_SHA256 = "fbcfc7a6ca136b21ff0a7101016fc91230798ffc2e6f5fc856df2fc29adca67a"

EVIDENCE_GOLDEN_HASHES = (
    "98e099906bb786bd99ee160735a3e2155be931fbac686f35d8be9b956939bd97",
    "4fba110cd8e3160fb2360423df557dd9d3aee95103c371b3efdfd119677ae90c",
    "a6c0c4b6715a03f60e0e00003af44ecaebf53a891f5d1178228b1cd9b0e8eebc",
)

VERDICT_GOLDEN_HASHES = (
    "b7da3622b835993860b47d679c12eb06705a633d89ca260f692b182d07481064",
    "90a9e7bf2b32babf5924e60797e56749c14d9fa95e9309518a9e327fade71756",
)


def make_evidence(evidence_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-1",
        provider="moex.iss",
        url="https://example.test/marketdata",
        http_status=200,
        raw_body_bytes=RAW_BODY,
        raw_sha256=compute_sha256(RAW_BODY),
        captured_at_utc=datetime(2026, 11, 19, 20, 0, tzinfo=UTC),
        parsed_value={"price": 100},
        verdict_status="E4_VERIFIED",
        parent_hash=None,
    )


def make_verdict(verdict_id: str = "verdict-1") -> VerdictRecord:
    return VerdictRecord(
        verdict_id=verdict_id,
        claim_id="claim-1",
        status="VERIFIED",
        evidence_ids=("evidence-1", "evidence-2"),
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="commit-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Source observation matched the claim under the recorded policy.",
    )


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        # Isolated CI database cleanup; production ledger paths never disable triggers.
        await connection.execute(text("ALTER TABLE evidence DISABLE TRIGGER USER"))
        await connection.execute(text("ALTER TABLE verdicts DISABLE TRIGGER USER"))
        try:
            await connection.execute(text("TRUNCATE TABLE idempotency_records"))
            await connection.execute(text("TRUNCATE TABLE verdicts, evidence RESTART IDENTITY"))
        finally:
            await connection.execute(text("ALTER TABLE evidence ENABLE TRIGGER USER"))
            await connection.execute(text("ALTER TABLE verdicts ENABLE TRIGGER USER"))


def test_v1_golden_evidence_and_verdict_chains_survive_postgres_reopen() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        try:
            await reset_ledger(ledger)

            evidence_inputs = tuple(
                make_evidence(f"evidence-{index}") for index in range(1, 4)
            )
            for index, record in enumerate(evidence_inputs):
                stored = await ledger.append(record)
                expected_parent = EVIDENCE_GOLDEN_HASHES[index - 1] if index else None
                assert stored.parent_hash == expected_parent
                assert compute_sha256(stored.raw_body_bytes) == RAW_SHA256
                assert compute_record_hash(stored) == EVIDENCE_GOLDEN_HASHES[index]

            verdict_inputs = (
                make_verdict(),
                replace(
                    make_verdict("verdict-2"),
                    parent_hash=VERDICT_GOLDEN_HASHES[0],
                ),
            )
            for index, record in enumerate(verdict_inputs):
                stored = await ledger.append_verdict(record)
                expected_parent = VERDICT_GOLDEN_HASHES[index - 1] if index else None
                assert stored.parent_hash == expected_parent
                assert compute_verdict_hash(stored) == VERDICT_GOLDEN_HASHES[index]
        finally:
            await ledger.close()

        reopened = PostgresLedger(DATABASE_URL)
        try:
            assert await reopened.verify_chain()
            assert await reopened.verify_verdict_chain()

            evidence_records = await reopened.list_by_claim("claim-1")
            assert [record.evidence_id for record in evidence_records] == [
                "evidence-1",
                "evidence-2",
                "evidence-3",
            ]
            for index, record in enumerate(evidence_records):
                expected_parent = EVIDENCE_GOLDEN_HASHES[index - 1] if index else None
                assert record.raw_body_bytes == RAW_BODY
                assert record.raw_sha256 == RAW_SHA256
                assert compute_sha256(record.raw_body_bytes) == record.raw_sha256
                assert record.parent_hash == expected_parent
                assert compute_record_hash(record) == EVIDENCE_GOLDEN_HASHES[index]

            verdict_records = []
            for index, verdict_id in enumerate(("verdict-1", "verdict-2")):
                record = await reopened.get_verdict(verdict_id)
                assert record is not None
                expected_parent = VERDICT_GOLDEN_HASHES[index - 1] if index else None
                assert record.parent_hash == expected_parent
                assert compute_verdict_hash(record) == VERDICT_GOLDEN_HASHES[index]
                verdict_records.append(record)
            assert [record.verdict_id for record in verdict_records] == [
                "verdict-1",
                "verdict-2",
            ]

            async with reopened.engine.connect() as connection:
                evidence_rows = (
                    await connection.execute(
                        text(
                            "SELECT evidence_id, raw_body_bytes, raw_sha256, parent_hash, "
                            "record_hash, sequence_id FROM evidence ORDER BY sequence_id"
                        )
                    )
                ).mappings().all()
                verdict_rows = (
                    await connection.execute(
                        text(
                            "SELECT verdict_id, parent_hash, record_hash, sequence_id "
                            "FROM verdicts ORDER BY sequence_id"
                        )
                    )
                ).mappings().all()

            assert [row["evidence_id"] for row in evidence_rows] == [
                "evidence-1",
                "evidence-2",
                "evidence-3",
            ]
            assert [row["sequence_id"] for row in evidence_rows] == sorted(
                row["sequence_id"] for row in evidence_rows
            )
            for index, row in enumerate(evidence_rows):
                expected_parent = EVIDENCE_GOLDEN_HASHES[index - 1] if index else None
                assert bytes(row["raw_body_bytes"]) == RAW_BODY
                assert row["raw_sha256"] == RAW_SHA256
                assert row["parent_hash"] == expected_parent
                assert row["record_hash"] == EVIDENCE_GOLDEN_HASHES[index]

            assert [row["verdict_id"] for row in verdict_rows] == [
                "verdict-1",
                "verdict-2",
            ]
            assert [row["sequence_id"] for row in verdict_rows] == sorted(
                row["sequence_id"] for row in verdict_rows
            )
            for index, row in enumerate(verdict_rows):
                expected_parent = VERDICT_GOLDEN_HASHES[index - 1] if index else None
                assert row["parent_hash"] == expected_parent
                assert row["record_hash"] == VERDICT_GOLDEN_HASHES[index]
        finally:
            await reopened.close()

    asyncio.run(run())
