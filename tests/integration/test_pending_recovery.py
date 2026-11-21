"""PostgreSQL integration tests for lease-based idempotency recovery."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.ledger.postgres import IntegrityViolation, PostgresLedger

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for PostgreSQL tests",
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


def make_evidence(evidence_id: str, claim_id: str) -> EvidenceRecord:
    body = b'{"price":100}'
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id=claim_id,
        provider="test-provider",
        url="https://example.test/price",
        http_status=200,
        raw_body_bytes=body,
        raw_sha256=compute_sha256(body),
        captured_at_utc=datetime(2026, 11, 21, tzinfo=UTC),
        parsed_value={"price": 100},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
    )


def make_verdict(verdict_id: str, claim_id: str, evidence_id: str) -> VerdictRecord:
    return VerdictRecord(
        verdict_id=verdict_id,
        claim_id=claim_id,
        status="INCONCLUSIVE",
        evidence_ids=(evidence_id,),
        policy_id="test-policy",
        policy_version="test-v1",
        code_version="test-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 21, tzinfo=UTC),
        reason_codes=("TEST_ONLY",),
        explanation="Lease recovery integration test.",
    )


async def expire_lease(ledger: PostgresLedger, key_hash: str) -> None:
    async with ledger.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE idempotency_records "
                "SET lease_expires_at = clock_timestamp() - INTERVAL '1 second' "
                "WHERE key_hash = :key_hash"
            ),
            {"key_hash": key_hash},
        )


def test_happy_path() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"happy-path")
        digest = compute_sha256(b"same-request")
        state, token, cached = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-happy"
        )
        assert state == "NEW" and token is not None and cached is None
        await ledger.complete_idempotency(
            key_hash=key,
            request_digest=digest,
            owner_token=token,
            verdict_id="verdict-happy",
            response_payload={"claim_id": "claim-happy"},
        )
        state2, token2, cached2 = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-repeat"
        )
        assert state2 == "COMPLETED"
        assert token2 is None
        assert cached2 == {"claim_id": "claim-happy"}
        await ledger.close()
    asyncio.run(run())


def test_lease_expiry_allows_takeover() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"takeover")
        digest = compute_sha256(b"same-request")
        state_a, token_a, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-a"
        )
        assert state_a == "NEW" and token_a is not None
        await expire_lease(ledger, key)
        state_b, token_b, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-b"
        )
        assert state_b == "NEW" and token_b is not None and token_b != token_a
        await ledger.close()
    asyncio.run(run())


def test_claim_id_updated_on_takeover() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"claim-update")
        digest = compute_sha256(b"same-request")
        await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-a"
        )
        await expire_lease(ledger, key)
        state, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-b"
        )
        assert state == "NEW" and token is not None
        async with ledger.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT claim_id FROM idempotency_records WHERE key_hash=:key"),
                {"key": key},
            )
            assert result.scalar_one() == "claim-b"
        await ledger.close()
    asyncio.run(run())


def test_zombie_verdict_and_evidence_orphaned() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"zombie")
        digest = compute_sha256(b"same-request")
        state_a, token_a, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-zombie-a"
        )
        assert state_a == "NEW" and token_a is not None
        await expire_lease(ledger, key)
        state_b, token_b, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-owner-b"
        )
        assert state_b == "NEW" and token_b is not None
        evidence = await ledger.append(make_evidence("zombie-evidence", "claim-zombie-a"))
        verdict = await ledger.append_verdict(
            make_verdict("zombie-verdict", "claim-zombie-a", evidence.evidence_id)
        )
        with pytest.raises(IntegrityViolation, match="lease lost"):
            await ledger.complete_idempotency(
                key_hash=key,
                request_digest=digest,
                owner_token=token_a,
                verdict_id=verdict.verdict_id,
                response_payload={"claim_id": "claim-zombie-a"},
            )
        assert await ledger.get("zombie-evidence") is not None
        assert await ledger.get_verdict("zombie-verdict") is not None
        async with ledger.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT claim_id FROM idempotency_records WHERE key_hash=:key"),
                {"key": key},
            )
            assert result.scalar_one() == "claim-owner-b"
        await ledger.close()
    asyncio.run(run())


def test_digest_mismatch_returns_key_reused() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"digest-mismatch")
        state, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=compute_sha256(b"request-a"), claim_id="claim-a"
        )
        assert state == "NEW" and token is not None
        await expire_lease(ledger, key)
        state_b, token_b, cached = await ledger.begin_idempotency(
            key_hash=key, request_digest=compute_sha256(b"request-b"), claim_id="claim-b"
        )
        assert state_b == "KEY_REUSED"
        assert token_b is None and cached is None
        await ledger.close()
    asyncio.run(run())


def test_concurrent_takeover_single_winner() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"concurrent-takeover")
        digest = compute_sha256(b"same-request")
        state, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-initial"
        )
        assert state == "NEW" and token is not None
        await expire_lease(ledger, key)
        results = await asyncio.gather(
            ledger.begin_idempotency(
                key_hash=key, request_digest=digest, claim_id="claim-winner-a"
            ),
            ledger.begin_idempotency(
                key_hash=key, request_digest=digest, claim_id="claim-winner-b"
            ),
        )
        assert sum(result[0] == "NEW" for result in results) == 1
        assert sum(result[0] == "IN_PROGRESS" for result in results) == 1
        assert sum(result[1] is not None for result in results) == 1
        await ledger.close()
    asyncio.run(run())
