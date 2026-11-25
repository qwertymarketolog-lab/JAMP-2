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
from jamp2.ledger.postgres import PostgresLedger, StaleLeaseError

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


async def wait_for_checked_out_connections(
    ledger: PostgresLedger,
    expected: int,
) -> None:
    for _ in range(200):
        if ledger.engine.pool.checkedout() >= expected:
            return
        await asyncio.sleep(0.01)

    raise AssertionError(
        f"expected {expected} checked-out connections; "
        f"observed {ledger.engine.pool.checkedout()}"
    )


async def wait_for_lock_waiters(ledger: PostgresLedger, expected: int) -> None:
    last_rows = []

    async with ledger.engine.connect() as connection:
        for _ in range(100):
            result = await connection.execute(
                text(
                    """
                    SELECT pid, state, wait_event_type, wait_event,
                           pg_blocking_pids(pid) AS blocked_by,
                           left(query, 300) AS query
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND state = 'active'
                    ORDER BY pid
                    """
                )
            )
            last_rows = [dict(row) for row in result.mappings().all()]
            waiters = [
                row for row in last_rows
                if row["wait_event_type"] == "Lock"
            ]
            if len(waiters) >= expected:
                return
            await asyncio.sleep(0.02)

    details = "\\n".join(repr(row) for row in last_rows)
    raise AssertionError(
        f"expected {expected} PostgreSQL lock waiter(s); "
        f"observed sessions:\\n{details}"
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


def test_zombie_verdict_and_evidence_are_fenced_after_takeover() -> None:
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
        assert state_b == "NEW" and token_b is not None and token_b != token_a

        with pytest.raises(StaleLeaseError):
            await ledger.append(
                make_evidence("zombie-evidence", "claim-zombie-a"),
                key_hash=key,
                owner_token=token_a,
            )
        assert await ledger.get("zombie-evidence") is None

        evidence_b = await ledger.append(
            make_evidence("current-evidence", "claim-owner-b"),
            key_hash=key,
            owner_token=token_b,
        )
        with pytest.raises(StaleLeaseError):
            await ledger.append_verdict(
                make_verdict("zombie-verdict", "claim-zombie-a", evidence_b.evidence_id),
                key_hash=key,
                owner_token=token_a,
            )
        assert await ledger.get_verdict("zombie-verdict") is None

        verdict_b = await ledger.append_verdict(
            make_verdict("current-verdict", "claim-owner-b", evidence_b.evidence_id),
            key_hash=key,
            owner_token=token_b,
        )
        assert verdict_b.verdict_id == "current-verdict"
        assert await ledger.get("current-evidence") is not None
        assert await ledger.get_verdict("current-verdict") is not None
        async with ledger.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT claim_id FROM idempotency_records WHERE key_hash=:key"),
                {"key": key},
            )
            assert result.scalar_one() == "claim-owner-b"
        await ledger.close()
    asyncio.run(run())


def test_append_rejects_missing_wrong_claim_and_terminal_reservations() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        missing_key = compute_sha256(b"missing-reservation")
        with pytest.raises(StaleLeaseError, match="missing"):
            await ledger.append(
                make_evidence("missing-reservation-evidence", "claim-missing"),
                key_hash=missing_key,
                owner_token="missing-owner",
            )
        with pytest.raises(StaleLeaseError, match="missing"):
            await ledger.append_verdict(
                make_verdict("missing-reservation-verdict", "claim-missing", "none"),
                key_hash=missing_key,
                owner_token="missing-owner",
            )

        key = compute_sha256(b"wrong-owner")
        digest = compute_sha256(b"same-request")
        _, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-owner"
        )
        assert token is not None
        with pytest.raises(StaleLeaseError, match="owner"):
            await ledger.append(
                make_evidence("wrong-owner-evidence", "claim-owner"),
                key_hash=key,
                owner_token="not-the-owner",
            )
        with pytest.raises(StaleLeaseError, match="owner"):
            await ledger.append_verdict(
                make_verdict("wrong-owner-verdict", "claim-owner", "none"),
                key_hash=key,
                owner_token="not-the-owner",
            )
        with pytest.raises(StaleLeaseError, match="claim"):
            await ledger.append(
                make_evidence("wrong-claim-evidence", "claim-other"),
                key_hash=key,
                owner_token=token,
            )
        with pytest.raises(StaleLeaseError, match="claim"):
            await ledger.append_verdict(
                make_verdict("wrong-claim-verdict", "claim-other", "none"),
                key_hash=key,
                owner_token=token,
            )
        await ledger.fail_idempotency(
            key_hash=key, request_digest=digest, owner_token=token
        )
        with pytest.raises(StaleLeaseError, match="not pending"):
            await ledger.append(
                make_evidence("terminal-evidence", "claim-owner"),
                key_hash=key,
                owner_token=token,
            )
        assert await ledger.get("missing-reservation-evidence") is None
        assert await ledger.get("wrong-owner-evidence") is None
        assert await ledger.get("wrong-claim-evidence") is None
        assert await ledger.get("terminal-evidence") is None
        await ledger.close()
    asyncio.run(run())


def test_expired_lease_is_rejected_without_inserting_evidence_or_verdict() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"expired-append")
        digest = compute_sha256(b"same-request")
        _, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-expired"
        )
        assert token is not None
        await expire_lease(ledger, key)
        with pytest.raises(StaleLeaseError, match="expired"):
            await ledger.append(
                make_evidence("expired-evidence", "claim-expired"),
                key_hash=key,
                owner_token=token,
            )
        with pytest.raises(StaleLeaseError, match="expired"):
            await ledger.append_verdict(
                make_verdict("expired-verdict", "claim-expired", "no-evidence"),
                key_hash=key,
                owner_token=token,
            )
        assert await ledger.get("expired-evidence") is None
        assert await ledger.get_verdict("expired-verdict") is None
        await ledger.close()
    asyncio.run(run())


def test_append_waiting_first_linearizes_before_takeover() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"append-before-takeover")
        digest = compute_sha256(b"same-request")
        _, token_a, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-a"
        )
        assert token_a is not None
        async with ledger.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            await lock_connection.execute(
                text(
                    "SELECT key_hash FROM idempotency_records "
                    "WHERE key_hash = :key_hash FOR UPDATE"
                ),
                {"key_hash": key},
            )
            append_task = asyncio.create_task(
                ledger.append(
                    make_evidence("append-before-takeover-evidence", "claim-a"),
                    key_hash=key,
                    owner_token=token_a,
                )
            )
            await wait_for_lock_waiters(ledger, 1)
            takeover_task = asyncio.create_task(
                ledger.begin_idempotency(
                    key_hash=key, request_digest=digest, claim_id="claim-b"
                )
            )
            await wait_for_checked_out_connections(ledger, 3)
            await wait_for_lock_waiters(ledger, 2)
            await transaction.commit()
            stored = await append_task
            assert stored.evidence_id == "append-before-takeover-evidence"
            takeover_state, takeover_token, _ = await takeover_task
            assert takeover_state == "IN_PROGRESS" and takeover_token is None

        await expire_lease(ledger, key)
        state_b, token_b, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-b"
        )
        assert state_b == "NEW" and token_b is not None and token_b != token_a
        assert await ledger.get("append-before-takeover-evidence") is not None
        await ledger.close()
    asyncio.run(run())


def test_takeover_waiting_first_fences_queued_stale_append() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"takeover-before-append")
        digest = compute_sha256(b"same-request")
        _, token_a, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-a"
        )
        assert token_a is not None
        await expire_lease(ledger, key)
        async with ledger.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            await lock_connection.execute(
                text(
                    "SELECT key_hash FROM idempotency_records "
                    "WHERE key_hash = :key_hash FOR UPDATE"
                ),
                {"key_hash": key},
            )
            takeover_task = asyncio.create_task(
                ledger.begin_idempotency(
                    key_hash=key, request_digest=digest, claim_id="claim-b"
                )
            )
            await wait_for_lock_waiters(ledger, 1)
            append_task = asyncio.create_task(
                ledger.append(
                    make_evidence("stale-queued-evidence", "claim-a"),
                    key_hash=key,
                    owner_token=token_a,
                )
            )
            await wait_for_checked_out_connections(ledger, 3)
            await wait_for_lock_waiters(ledger, 2)
            await transaction.commit()
            state_b, token_b, _ = await takeover_task
            assert state_b == "NEW" and token_b is not None and token_b != token_a
            with pytest.raises(StaleLeaseError):
                await append_task
        assert await ledger.get("stale-queued-evidence") is None
        await ledger.close()
    asyncio.run(run())


def test_lease_is_rechecked_after_waiting_for_reservation_row_lock() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"lease-expires-while-waiting")
        digest = compute_sha256(b"same-request")
        _, token, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-waiter"
        )
        assert token is not None

        async with ledger.engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "SELECT key_hash FROM idempotency_records "
                    "WHERE key_hash = :key_hash FOR UPDATE"
                ),
                {"key_hash": key},
            )
            await connection.execute(
                text(
                    "UPDATE idempotency_records "
                    "SET lease_expires_at = clock_timestamp() + INTERVAL '250 milliseconds' "
                    "WHERE key_hash = :key_hash"
                ),
                {"key_hash": key},
            )
            append_task = asyncio.create_task(
                ledger.append(
                    make_evidence("expired-while-waiting", "claim-waiter"),
                    key_hash=key,
                    owner_token=token,
                )
            )
            await wait_for_lock_waiters(ledger, 1)
            await asyncio.sleep(0.4)
            await transaction.commit()
            with pytest.raises(StaleLeaseError, match="expired"):
                await append_task
        assert await ledger.get("expired-while-waiting") is None
        await ledger.close()
    asyncio.run(run())


def test_each_evidence_append_revalidates_owner_after_takeover() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        key = compute_sha256(b"multiple-evidence-fencing")
        digest = compute_sha256(b"same-request")
        _, token_a, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-a"
        )
        assert token_a is not None
        await ledger.append(
            make_evidence("first-evidence-a", "claim-a"),
            key_hash=key,
            owner_token=token_a,
        )
        await expire_lease(ledger, key)
        state_b, token_b, _ = await ledger.begin_idempotency(
            key_hash=key, request_digest=digest, claim_id="claim-b"
        )
        assert state_b == "NEW" and token_b is not None
        with pytest.raises(StaleLeaseError):
            await ledger.append(
                make_evidence("second-evidence-a", "claim-a"),
                key_hash=key,
                owner_token=token_a,
            )
        await ledger.append(
            make_evidence("first-evidence-b", "claim-b"),
            key_hash=key,
            owner_token=token_b,
        )
        assert await ledger.get("first-evidence-a") is not None
        assert await ledger.get("second-evidence-a") is None
        assert await ledger.get("first-evidence-b") is not None
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
