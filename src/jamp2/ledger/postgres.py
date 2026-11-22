"""PostgreSQL-backed append-only evidence and verdict ledgers."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Identity,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jamp2.evidence.digest import (
    compute_record_hash,
    compute_sha256,
    compute_verdict_hash,
)
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation, JSONValue, LedgerError
from jamp2.evidence.verdict import VerdictRecord


class DuplicateEvidence(LedgerError):
    """Raised when evidence_id already exists."""


class DuplicateVerdict(LedgerError):
    """Raised when verdict_id already exists."""


class LedgerUnavailable(LedgerError):
    """Raised when the PostgreSQL ledger cannot be reached or used."""


class Base(DeclarativeBase):
    """SQLAlchemy metadata for the JAMP 2 persistence layer."""


class EvidenceRow(Base):
    """One immutable source observation; sequence_id defines append order."""

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
    # Deprecated compatibility field; the separate VerdictRow is authoritative.
    verdict_status: Mapped[str] = mapped_column(String(64))
    parent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonicalization_version: Mapped[str] = mapped_column(String(64))
    source_timestamp: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body_complete: Mapped[bool] = mapped_column(Boolean)
    provider_adapter_version: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    hash_schema_version: Mapped[str] = mapped_column(String(8), server_default="v1")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64))


class VerdictRow(Base):
    """One immutable decision with exact evidence and version references."""

    __tablename__ = "verdicts"

    sequence_id: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    verdict_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32))
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB)
    policy_id: Mapped[str] = mapped_column(String(255))
    policy_version: Mapped[str] = mapped_column(String(128))
    code_version: Mapped[str] = mapped_column(String(128))
    canonicalization_version: Mapped[str] = mapped_column(String(64))
    decided_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)
    explanation: Mapped[str] = mapped_column(Text)
    verification_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyRow(Base):
    """Mutable request coordination state; keys are stored only as SHA-256 digests."""

    __tablename__ = "idempotency_records"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    claim_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    verdict_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="1")


_LOCK_KEY = 731_942_001
_VERDICT_LOCK_KEY = 731_942_002


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
        canonicalization_version=row.canonicalization_version,
        source_timestamp=row.source_timestamp,
        body_complete=row.body_complete,
        provider_adapter_version=row.provider_adapter_version,
        parser_version=row.parser_version,
        hash_schema_version=row.hash_schema_version,
        fetched_at=row.fetched_at,
        provider_observed_at=row.provider_observed_at,
        source_date=row.source_date,
        record_hash=row.record_hash,
    )


def _to_verdict(row: VerdictRow) -> VerdictRecord:
    return VerdictRecord(
        verdict_id=row.verdict_id,
        claim_id=row.claim_id,
        status=row.status,
        evidence_ids=tuple(row.evidence_ids),
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        code_version=row.code_version,
        canonicalization_version=row.canonicalization_version,
        decided_at_utc=row.decided_at_utc,
        reason_codes=tuple(row.reason_codes),
        explanation=row.explanation,
        parent_hash=row.parent_hash,
        verification_mode=row.verification_mode,
    )


class PostgresLedger:
    """Serialize append operations and verify both immutable hash chains on reads."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if engine is not None:
            self.engine = engine
            return
        url = database_url or os.getenv("DATABASE_URL")
        if not url:
            raise ValueError("DATABASE_URL is not set")
        self.engine = create_async_engine(url, pool_pre_ping=True)

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        """Atomically link and append an evidence record."""
        if compute_sha256(record.raw_body_bytes) != record.raw_sha256:
            raise IntegrityViolation("raw_sha256 does not match raw_body_bytes")
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _LOCK_KEY},
                )
                result = await connection.execute(
                    select(EvidenceRow.__table__)
                    .order_by(EvidenceRow.sequence_id.asc())
                    .with_for_update()
                )
                rows = [EvidenceRow(**dict(row)) for row in result.mappings().all()]
                self._verify_rows(rows)
                if any(row.evidence_id == record.evidence_id for row in rows):
                    raise DuplicateEvidence(record.evidence_id)
                parent_hash = rows[-1].record_hash if rows else None
                linked_record = replace(record, parent_hash=parent_hash)
                record_hash = compute_record_hash(linked_record)
                await connection.execute(
                    insert(EvidenceRow).values(
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
                        canonicalization_version=linked_record.canonicalization_version,
                        source_timestamp=linked_record.source_timestamp,
                        body_complete=linked_record.body_complete,
                        provider_adapter_version=linked_record.provider_adapter_version,
                        parser_version=linked_record.parser_version,
                        hash_schema_version=linked_record.hash_schema_version,
                        fetched_at=linked_record.fetched_at,
                        provider_observed_at=linked_record.provider_observed_at,
                        source_date=linked_record.source_date,
                        record_hash=record_hash,
                    )
                )
                return replace(linked_record, record_hash=record_hash)
        except (DuplicateEvidence, IntegrityViolation):
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL evidence append failed") from exc

    async def append_verdict(self, record: VerdictRecord) -> VerdictRecord:
        """Append a verdict only when all referenced evidence exists for its claim."""
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _VERDICT_LOCK_KEY},
                )
                all_evidence_result = await connection.execute(
                    select(EvidenceRow.__table__).order_by(EvidenceRow.sequence_id.asc())
                )
                all_evidence_rows = [
                    EvidenceRow(**dict(row)) for row in all_evidence_result.mappings().all()
                ]
                self._verify_rows(all_evidence_rows)
                evidence_by_id = {
                    row.evidence_id: row for row in all_evidence_rows
                    if row.evidence_id in record.evidence_ids
                }
                evidence_rows = list(evidence_by_id.values())
                if set(evidence_by_id) != set(record.evidence_ids) or any(
                    row.claim_id != record.claim_id for row in evidence_rows
                ):
                    raise IntegrityViolation(
                        "verdict references missing evidence or evidence from another claim"
                    )
                result = await connection.execute(
                    select(VerdictRow.__table__)
                    .order_by(VerdictRow.sequence_id.asc())
                    .with_for_update()
                )
                rows = [VerdictRow(**dict(row)) for row in result.mappings().all()]
                self._verify_verdict_rows(rows)
                if any(row.verdict_id == record.verdict_id for row in rows):
                    raise DuplicateVerdict(record.verdict_id)
                parent_hash = rows[-1].record_hash if rows else None
                linked_record = replace(record, parent_hash=parent_hash)
                record_hash = compute_verdict_hash(linked_record)
                await connection.execute(
                    insert(VerdictRow).values(
                        verdict_id=linked_record.verdict_id,
                        claim_id=linked_record.claim_id,
                        status=linked_record.status,
                        evidence_ids=list(linked_record.evidence_ids),
                        policy_id=linked_record.policy_id,
                        policy_version=linked_record.policy_version,
                        code_version=linked_record.code_version,
                        canonicalization_version=linked_record.canonicalization_version,
                        decided_at_utc=linked_record.decided_at_utc,
                        reason_codes=list(linked_record.reason_codes),
                        explanation=linked_record.explanation,
                        verification_mode=linked_record.verification_mode,
                        parent_hash=linked_record.parent_hash,
                        record_hash=record_hash,
                    )
                )
                return linked_record
        except (DuplicateVerdict, IntegrityViolation):
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL verdict append failed") from exc

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        """Return evidence only after verifying the complete evidence hash chain."""
        try:
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    select(EvidenceRow.__table__).order_by(EvidenceRow.sequence_id.asc())
                )
                rows = [EvidenceRow(**dict(row)) for row in result.mappings().all()]
            self._verify_rows(rows)
            for row in rows:
                if row.evidence_id == evidence_id:
                    return _to_record(row)
            return None
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL evidence read failed") from exc

    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None:
        """Return a verdict after verifying its chain and every referenced evidence link."""
        try:
            async with self.engine.connect() as connection:
                evidence_result = await connection.execute(
                    select(EvidenceRow.__table__).order_by(EvidenceRow.sequence_id.asc())
                )
                evidence_rows = [
                    EvidenceRow(**dict(row)) for row in evidence_result.mappings().all()
                ]
                verdict_result = await connection.execute(
                    select(VerdictRow.__table__).order_by(VerdictRow.sequence_id.asc())
                )
                verdict_rows = [
                    VerdictRow(**dict(row)) for row in verdict_result.mappings().all()
                ]
            self._verify_rows(evidence_rows)
            self._verify_verdict_rows(verdict_rows)
            evidence_by_id = {row.evidence_id: row for row in evidence_rows}
            for row in verdict_rows:
                if any(
                    evidence_id not in evidence_by_id
                    or evidence_by_id[evidence_id].claim_id != row.claim_id
                    for evidence_id in row.evidence_ids
                ):
                    raise IntegrityViolation("verdict evidence reference is invalid")
                if row.verdict_id == verdict_id:
                    return _to_verdict(row)
            return None
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL verdict read failed") from exc

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        """Return claim evidence in append order after verifying the full chain."""
        try:
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    select(EvidenceRow.__table__).order_by(EvidenceRow.sequence_id.asc())
                )
                rows = [EvidenceRow(**dict(row)) for row in result.mappings().all()]
            self._verify_rows(rows)
            return [_to_record(row) for row in rows if row.claim_id == claim_id]
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL evidence list failed") from exc

    async def verify_chain(self) -> bool:
        """Verify all evidence digests, record hashes, and parent links."""
        try:
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    select(EvidenceRow.__table__).order_by(EvidenceRow.sequence_id.asc())
                )
                rows = [EvidenceRow(**dict(row)) for row in result.mappings().all()]
            self._verify_rows(rows)
            return True
        except IntegrityViolation:
            return False
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL evidence verification failed") from exc

    async def verify_verdict_chain(self) -> bool:
        """Verify all verdict record hashes and parent links."""
        try:
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    select(VerdictRow.__table__).order_by(VerdictRow.sequence_id.asc())
                )
                rows = [VerdictRow(**dict(row)) for row in result.mappings().all()]
            self._verify_verdict_rows(rows)
            return True
        except IntegrityViolation:
            return False
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("PostgreSQL verdict verification failed") from exc


    async def begin_idempotency(
        self,
        *,
        key_hash: str,
        request_digest: str,
        claim_id: str,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Reserve a key or atomically take over an expired matching request."""
        owner_token = uuid4().hex
        lease_deadline = func.clock_timestamp() + text("INTERVAL '60 seconds'")
        try:
            async with self.engine.begin() as connection:
                statement = (
                    pg_insert(IdempotencyRow)
                    .values(
                        key_hash=key_hash,
                        request_digest=request_digest,
                        claim_id=claim_id,
                        status="PENDING",
                        owner_token=owner_token,
                        lease_expires_at=lease_deadline,
                        attempt_count=1,
                    )
                    .on_conflict_do_update(
                        index_elements=[IdempotencyRow.key_hash],
                        set_={
                            "status": "PENDING",
                            "owner_token": owner_token,
                            "lease_expires_at": lease_deadline,
                            "claim_id": claim_id,
                            "attempt_count": IdempotencyRow.attempt_count + 1,
                        },
                        where=(
                            (IdempotencyRow.status == "PENDING")
                            & (IdempotencyRow.lease_expires_at < func.clock_timestamp())
                            & (IdempotencyRow.request_digest == request_digest)
                        ),
                    )
                    .returning(
                        IdempotencyRow.status,
                        IdempotencyRow.owner_token,
                        IdempotencyRow.response_payload,
                    )
                )
                acquired = await connection.execute(statement)
                owned = acquired.mappings().one_or_none()
                if owned is not None:
                    return "NEW", owned["owner_token"], None

                result = await connection.execute(
                    select(IdempotencyRow.__table__)
                    .where(IdempotencyRow.key_hash == key_hash)
                    .with_for_update()
                )
                row = IdempotencyRow(**dict(result.mappings().one()))
                if row.request_digest != request_digest:
                    return "KEY_REUSED", None, None
                if row.status == "COMPLETED":
                    return "COMPLETED", None, row.response_payload
                if row.status == "PENDING":
                    return "IN_PROGRESS", None, None
                return "FAILED", None, None
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("Idempotency reservation failed") from exc

    async def complete_idempotency(
        self,
        *,
        key_hash: str,
        request_digest: str,
        owner_token: str,
        verdict_id: str,
        response_payload: dict[str, Any],
    ) -> None:
        """Complete only while this owner still holds an unexpired lease."""
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.key_hash == key_hash,
                        IdempotencyRow.request_digest == request_digest,
                        IdempotencyRow.owner_token == owner_token,
                        IdempotencyRow.status == "PENDING",
                        IdempotencyRow.lease_expires_at > func.clock_timestamp(),
                    )
                    .values(
                        status="COMPLETED",
                        verdict_id=verdict_id,
                        response_payload=response_payload,
                        completed_at_utc=func.clock_timestamp(),
                    )
                )
                if result.rowcount != 1:
                    raise IntegrityViolation("idempotency lease lost or no longer pending")
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("Idempotency completion failed") from exc

    async def fail_idempotency(
        self, *, key_hash: str, request_digest: str, owner_token: str
    ) -> None:
        """Fail only while this owner still holds an unexpired lease."""
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.key_hash == key_hash,
                        IdempotencyRow.request_digest == request_digest,
                        IdempotencyRow.owner_token == owner_token,
                        IdempotencyRow.status == "PENDING",
                        IdempotencyRow.lease_expires_at > func.clock_timestamp(),
                    )
                    .values(status="FAILED")
                )
                if result.rowcount != 1:
                    raise IntegrityViolation("lease lost or already failed")
        except IntegrityViolation:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("Idempotency failure recording failed") from exc

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

    @staticmethod
    def _verify_verdict_rows(rows: Sequence[VerdictRow]) -> None:
        expected_parent: str | None = None
        seen_ids: set[str] = set()
        for row in rows:
            record = _to_verdict(row)
            if row.verdict_id in seen_ids:
                raise IntegrityViolation("duplicate verdict_id in persisted ledger")
            if record.parent_hash != expected_parent:
                raise IntegrityViolation(f"verdict parent hash mismatch: {record.verdict_id}")
            actual_hash = compute_verdict_hash(record)
            if actual_hash != row.record_hash:
                raise IntegrityViolation(f"verdict record hash mismatch: {record.verdict_id}")
            seen_ids.add(row.verdict_id)
            expected_parent = actual_hash
