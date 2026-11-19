"""PostgreSQL-backed append-only evidence ledger."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Integer,
    LargeBinary,
    String,
    Text,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord, JSONValue


class LedgerError(Exception):
    """Base class for expected ledger failures."""


class DuplicateEvidence(LedgerError):
    """Raised when evidence_id already exists."""


class IntegrityViolation(LedgerError):
    """Raised when stored evidence or its hash-chain is inconsistent."""


class LedgerUnavailable(LedgerError):
    """Raised when the PostgreSQL ledger cannot be reached or used."""


class Base(DeclarativeBase):
    """SQLAlchemy metadata for the JAMP 2 persistence layer."""


class EvidenceRow(Base):
    """One immutable evidence row; sequence_id defines append order."""

    __tablename__ = "evidence"

    sequence_id: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    evidence_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_body_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parsed_value: Mapped[JSONValue] = mapped_column(JSONB)
    verdict_status: Mapped[str] = mapped_column(String(64))
    parent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64))


_LOCK_KEY = 731_942_001


def _to_record(row: EvidenceRow) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row.evidence_id,
        claim_id=row.claim_id,
        provider=row.provider,
        url=row.url,
        http_status=row.http_status,
        raw_body_bytes=bytes(row.raw_body_bytes),
        raw_sha256=row.raw_sha256,
        captured_at_utc=row.captured_at_utc,
        parsed_value=row.parsed_value,
        verdict_status=row.verdict_status,
        parent_hash=row.parent_hash,
    )


class PostgresLedger:
    """Serialize appends with a transaction-scoped advisory lock and verify reads."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        url = database_url or os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://jamp2:jamp2@localhost:5432/jamp2",
        )
        self.engine = engine or create_async_engine(url, pool_pre_ping=True)

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        """Atomically link and append a record; parent_hash is assigned by the ledger."""
        if compute_sha256(record.raw_body_bytes) != record.raw_sha256:
            raise IntegrityViolation("raw_sha256 does not match raw_body_bytes")
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _LOCK_KEY},
                )
                rows = (
                    await connection.execute(
                        select(EvidenceRow)
                        .order_by(EvidenceRow.sequence_id.asc())
                        .with_for_update()
                    )
                ).scalars().all()
                self._verify_rows(rows)
                if any(row.evidence_id == record.evidence_id for row in rows):
                    raise DuplicateEvidence(record.evidence_id)
                parent_hash = rows[-1].record_hash if rows else None
                linked_record = replace(record, parent_hash=parent_hash)
                record_hash = compute_record_hash(linked_record)
                await connection.execute(
                    EvidenceRow.__table__.insert().values(
                        evidence_id=linked_record.evidence_id,
                        claim_id=linked_record.claim_id,
                        provider=linked_record.provider,
                        url=linked_record.url,
                        http_status=linked_record.http_status,
                        raw_body_bytes=linked_record.raw_body_bytes,
                        raw_sha256=linked_record.raw_sha256,
                        captured_at_utc=linked_record.captured_at_utc,
                        parsed_value=linked_record.parsed_value,
                        verdict_status=linked_record.verdict_status,
                        parent_hash=linked_record.parent_hash,
                        record_hash=record_hash,
                    )
                )
                return linked_record
        except (DuplicateEvidence, IntegrityViolation):
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL ledger append failed") from exc

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        """Return a record only after verifying every link from genesis."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(EvidenceRow).order_by(EvidenceRow.sequence_id.asc())
                    )
                ).scalars().all()
            self._verify_rows(rows)
            for row in rows:
                if row.evidence_id == evidence_id:
                    return _to_record(row)
            return None
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL ledger read failed") from exc

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        """Return claim records in append order after verifying the full chain."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(EvidenceRow).order_by(EvidenceRow.sequence_id.asc())
                    )
                ).scalars().all()
            self._verify_rows(rows)
            return [_to_record(row) for row in rows if row.claim_id == claim_id]
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL ledger read failed") from exc

    async def verify_chain(self) -> bool:
        """Return true only when all persisted hashes and parent links are valid."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(EvidenceRow).order_by(EvidenceRow.sequence_id.asc())
                    )
                ).scalars().all()
            self._verify_rows(rows)
            return True
        except IntegrityViolation:
            return False
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL ledger verification failed") from exc

    async def close(self) -> None:
        """Dispose the async connection pool."""
        await self.engine.dispose()

    @staticmethod
    def _verify_rows(rows: Sequence[EvidenceRow]) -> None:
        expected_parent: str | None = None
        seen_ids: set[str] = set()
        for row in rows:
            record = _to_record(row)
            if row.evidence_id in seen_ids:
                raise IntegrityViolation("duplicate evidence_id in persisted ledger")
            if compute_sha256(record.raw_body_bytes) != record.raw_sha256:
                raise IntegrityViolation(f"raw digest mismatch: {record.evidence_id}")
            if record.parent_hash != expected_parent:
                raise IntegrityViolation(f"parent hash mismatch: {record.evidence_id}")
            actual_hash = compute_record_hash(record)
            if actual_hash != row.record_hash:
                raise IntegrityViolation(f"record hash mismatch: {record.evidence_id}")
            seen_ids.add(row.evidence_id)
            expected_parent = actual_hash
