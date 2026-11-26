"""PostgreSQL migration rollback guards preserve populated append-only ledgers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from alembic import command
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for PostgreSQL migration tests",
)


def _connect_kwargs(url: URL, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host,
        "port": url.port or 5432,
        "database": database,
    }


@pytest.fixture
def migration_database(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Config, URL]]:
    assert DATABASE_URL is not None
    base_url = make_url(DATABASE_URL)
    database_name = f"jamp2_migration_{uuid.uuid4().hex}"
    admin_kwargs = _connect_kwargs(base_url, "postgres")

    async def create_database() -> None:
        connection = await asyncpg.connect(**admin_kwargs)
        try:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await connection.close()

    asyncio.run(create_database())
    database_url = base_url.set(database=database_name)
    monkeypatch.setenv(
        "DATABASE_URL",
        database_url.render_as_string(hide_password=False),
    )
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


async def _insert_evidence(database_url: URL) -> None:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    raw_body = b'{"price":100}'
    try:
        await connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, claim_id, provider, url, http_status, raw_body_bytes,
                raw_sha256, captured_at_utc, parsed_value, verdict_status,
                parent_hash, record_hash
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
            """,
            "evidence-1",
            "claim-1",
            "test-provider",
            "https://example.test/evidence",
            200,
            raw_body,
            hashlib.sha256(raw_body).hexdigest(),
            datetime.now(UTC),
            '{"price": 100}',
            "OBSERVATION_CAPTURED",
            None,
            "a" * 64,
        )
    finally:
        await connection.close()


async def _insert_verdict(database_url: URL) -> None:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        await connection.execute(
            """
            INSERT INTO verdicts (
                verdict_id, claim_id, status, evidence_ids, policy_id, policy_version,
                code_version, canonicalization_version, decided_at_utc, reason_codes,
                explanation, parent_hash, record_hash
            )
            VALUES (
                $1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13
            )
            """,
            "verdict-1",
            "claim-1",
            "VERIFIED",
            '["evidence-1"]',
            "test-policy",
            "test-v1",
            "test-code",
            "jamp-json-nfc-v1",
            datetime.now(UTC),
            '["E4_VERIFIED"]',
            "Test verdict",
            None,
            "b" * 64,
        )
    finally:
        await connection.close()


async def _count(database_url: URL, table: str) -> int:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        return await connection.fetchval(f"SELECT count(*) FROM {table}")
    finally:
        await connection.close()


async def _current_revision(database_url: URL) -> str:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        return await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()


async def _table_exists(database_url: URL, table: str) -> bool:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        return await connection.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            f"public.{table}",
        )
    finally:
        await connection.close()


def test_populated_verdict_ledger_blocks_002_downgrade_without_data_loss(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "002_verdict_records")
    asyncio.run(_insert_evidence(database_url))
    asyncio.run(_insert_verdict(database_url))

    with pytest.raises(RuntimeError, match="populated verdict ledger"):
        command.downgrade(config, "001_initial_ledger")

    assert asyncio.run(_current_revision(database_url)) == "002_verdict_records"
    assert asyncio.run(_count(database_url, "verdicts")) == 1
    assert asyncio.run(_count(database_url, "evidence")) == 1


def test_empty_verdict_ledger_allows_002_downgrade(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "002_verdict_records")

    command.downgrade(config, "001_initial_ledger")

    assert asyncio.run(_current_revision(database_url)) == "001_initial_ledger"
    assert asyncio.run(_count(database_url, "evidence")) == 0
    assert not asyncio.run(_table_exists(database_url, "verdicts"))


def test_populated_evidence_ledger_blocks_001_downgrade_without_data_loss(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "001_initial_ledger")
    asyncio.run(_insert_evidence(database_url))

    with pytest.raises(RuntimeError, match="populated evidence ledger"):
        command.downgrade(config, "base")

    assert asyncio.run(_current_revision(database_url)) == "001_initial_ledger"
    assert asyncio.run(_count(database_url, "evidence")) == 1


def test_empty_evidence_ledger_allows_001_downgrade(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "001_initial_ledger")

    command.downgrade(config, "base")

    assert not asyncio.run(_table_exists(database_url, "evidence"))


async def _trigger_exists(database_url: URL, table: str, trigger: str) -> bool:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        return await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgrelid = to_regclass($1)
                  AND tgname = $2
                  AND NOT tgisinternal
            )
            """,
            f"public.{table}",
            trigger,
        )
    finally:
        await connection.close()


async def _truncate_function_exists(database_url: URL) -> bool:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        return await connection.fetchval(
            "SELECT to_regprocedure('public.prevent_truncate()') IS NOT NULL"
        )
    finally:
        await connection.close()


def test_006_truncate_protection_upgrade_and_downgrade(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "head")

    assert asyncio.run(_current_revision(database_url)) == "009_verdict_policy_snapshot"
    assert asyncio.run(_trigger_exists(database_url, "evidence", "evidence_no_truncate"))
    assert asyncio.run(_trigger_exists(database_url, "verdicts", "verdicts_no_truncate"))
    assert asyncio.run(_truncate_function_exists(database_url))

    command.downgrade(config, "005_idempotency")

    assert asyncio.run(_current_revision(database_url)) == "005_idempotency"
    assert not asyncio.run(_trigger_exists(database_url, "evidence", "evidence_no_truncate"))
    assert not asyncio.run(_trigger_exists(database_url, "verdicts", "verdicts_no_truncate"))
    assert not asyncio.run(_truncate_function_exists(database_url))

async def _idempotency_columns(database_url: URL) -> set[str]:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        rows = await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'idempotency_records'
            """
        )
        return {row["column_name"] for row in rows}
    finally:
        await connection.close()


def test_downgrade_007_pending_lease(
    migration_database: tuple[Config, URL],
) -> None:
    """Verify migration 007 downgrades cleanly and removes all new columns."""
    config, database_url = migration_database
    command.upgrade(config, "head")
    assert asyncio.run(_current_revision(database_url)) == "009_verdict_policy_snapshot"

    columns_before = asyncio.run(_idempotency_columns(database_url))
    assert {"owner_token", "lease_expires_at", "attempt_count"} <= columns_before

    command.downgrade(config, "006_truncate_protection")
    assert asyncio.run(_current_revision(database_url)) == "006_truncate_protection"

    columns_after = asyncio.run(_idempotency_columns(database_url))
    assert "owner_token" not in columns_after
    assert "lease_expires_at" not in columns_after
    assert "attempt_count" not in columns_after


async def _insert_head_evidence(database_url: URL) -> None:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    raw_body = b'{"price":100}'
    try:
        await connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, claim_id, provider, url, http_status, raw_body_bytes,
                raw_sha256, captured_at_utc, parsed_value, verdict_status,
                canonicalization_version, body_complete, provider_adapter_version,
                parser_version, parent_hash, record_hash
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10,
                    $11, $12, $13, $14, $15, $16)
            """,
            "evidence-1",
            "claim-1",
            "test-provider",
            "https://example.test/evidence",
            200,
            raw_body,
            hashlib.sha256(raw_body).hexdigest(),
            datetime.now(UTC),
            '{"price":100}',
            "OBSERVATION_CAPTURED",
            "jamp-json-nfc-v1",
            True,
            "provider-adapter-v1",
            "provider-parser-v1",
            None,
            "a" * 64,
        )
    finally:
        await connection.close()


async def _insert_head_v2_evidence(database_url: URL) -> None:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    captured = datetime.now(UTC)
    raw_body = b'{"price":100}'
    record = EvidenceRecord(
        evidence_id="evidence-1",
        claim_id="claim-1",
        provider="test-provider",
        url="https://example.test/evidence",
        http_status=200,
        raw_body_bytes=raw_body,
        raw_sha256=compute_sha256(raw_body),
        captured_at_utc=captured,
        parsed_value={"price": 100},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
        canonicalization_version="jamp-json-nfc-v1",
        body_complete=True,
        provider_adapter_version="provider-adapter-v1",
        parser_version="provider-parser-v1",
        hash_schema_version="v2",
        fetched_at=captured,
        provider_observed_at=captured,
        source_date="2026-11-22",
    )
    try:
        await connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, claim_id, provider, url, http_status, raw_body_bytes,
                raw_sha256, captured_at_utc, parsed_value, verdict_status,
                canonicalization_version, body_complete, provider_adapter_version,
                parser_version, parent_hash, record_hash, hash_schema_version,
                fetched_at, provider_observed_at, source_date
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
            """,
            record.evidence_id,
            record.claim_id,
            record.provider,
            record.url,
            record.http_status,
            record.raw_body_bytes,
            record.raw_sha256,
            record.captured_at_utc,
            '{"price":100}',
            record.verdict_status,
            record.canonicalization_version,
            record.body_complete,
            record.provider_adapter_version,
            record.parser_version,
            record.parent_hash,
            compute_record_hash(record),
            record.hash_schema_version,
            record.fetched_at,
            record.provider_observed_at,
            record.source_date,
        )
    finally:
        await connection.close()


async def _insert_head_v2_verdict(database_url: URL) -> None:
    connection = await asyncpg.connect(
        **_connect_kwargs(database_url, database_url.database or "")
    )
    try:
        await connection.execute(
            """
            INSERT INTO verdicts (
                verdict_id, claim_id, status, evidence_ids, policy_id, policy_version,
                code_version, canonicalization_version, decided_at_utc, reason_codes,
                explanation, parent_hash, record_hash, verification_mode
            )
            VALUES (
                $1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14
            )
            """,
            "verdict-1",
            "claim-1",
            "VERIFIED",
            '["evidence-1"]',
            "test-policy",
            "test-v1",
            "test-code",
            "jamp-json-nfc-v1",
            datetime.now(UTC),
            '["E4_VERIFIED"]',
            "Test verdict",
            None,
            "b" * 64,
            "SINGLE_SOURCE",
        )
    finally:
        await connection.close()


def test_008_downgrade_blocks_populated_v2_metadata_without_data_loss(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "head")
    asyncio.run(_insert_head_v2_evidence(database_url))
    asyncio.run(_insert_head_v2_verdict(database_url))

    with pytest.raises(RuntimeError, match="v2 evidence or verification_mode metadata"):
        command.downgrade(config, "007_pending_lease")

    assert asyncio.run(_current_revision(database_url)) == "009_verdict_policy_snapshot"
    assert asyncio.run(_count(database_url, "evidence")) == 1
    assert asyncio.run(_count(database_url, "verdicts")) == 1


def test_008_downgrade_preserves_populated_legacy_v1_rows(
    migration_database: tuple[Config, URL],
) -> None:
    config, database_url = migration_database
    command.upgrade(config, "head")
    asyncio.run(_insert_head_evidence(database_url))
    asyncio.run(_insert_verdict(database_url))

    command.downgrade(config, "007_pending_lease")

    assert asyncio.run(_current_revision(database_url)) == "007_pending_lease"
    assert asyncio.run(_count(database_url, "evidence")) == 1
    assert asyncio.run(_count(database_url, "verdicts")) == 1
