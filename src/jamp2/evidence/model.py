"""Immutable evidence records captured from bounded provider observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeAlias

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)


class LedgerError(Exception):
    """Base class for evidence ledger failures."""


class IntegrityViolation(LedgerError):
    """Raised when persisted evidence metadata or its integrity contract is invalid."""


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One immutable raw observation and its derived verification metadata."""

    evidence_id: str
    claim_id: str
    provider: str
    url: str
    http_status: int | None
    raw_body_bytes: bytes
    raw_sha256: str
    captured_at_utc: datetime
    parsed_value: JSONValue
    verdict_status: str
    parent_hash: str | None
    canonicalization_version: str = "jamp-json-nfc-v1"
    source_timestamp: str | None = None
    body_complete: bool = True
    provider_adapter_version: str = "provider-adapter-v1"
    parser_version: str = "provider-parser-v1"
    hash_schema_version: str = "v1"
    fetched_at: datetime | None = None
    provider_observed_at: datetime | None = None
    source_date: str | None = None

    def __post_init__(self) -> None:
        required_strings = (
            ("evidence_id", self.evidence_id),
            ("claim_id", self.claim_id),
            ("provider", self.provider),
            ("url", self.url),
            ("raw_sha256", self.raw_sha256),
            ("verdict_status", self.verdict_status),
            ("canonicalization_version", self.canonicalization_version),
            ("provider_adapter_version", self.provider_adapter_version),
            ("parser_version", self.parser_version),
        )
        for field_name, value in required_strings:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be an HTTP status code or null")
        if self.source_timestamp is not None and not isinstance(self.source_timestamp, str):
            raise ValueError("source_timestamp must be a string or null")
        if not isinstance(self.body_complete, bool):
            raise ValueError("body_complete must be a boolean")
        if not isinstance(self.raw_body_bytes, bytes):
            raise ValueError("raw_body_bytes must be bytes")
        if not isinstance(self.captured_at_utc, datetime):
            raise ValueError("captured_at_utc must be a datetime")
        if (
            self.captured_at_utc.tzinfo is None
            or self.captured_at_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("captured_at_utc must be timezone-aware UTC")
        if self.hash_schema_version not in {"v1", "v2"}:
            raise IntegrityViolation("unsupported evidence hash schema version")
        if self.hash_schema_version == "v1" and any(
            value is not None
            for value in (self.fetched_at, self.provider_observed_at, self.source_date)
        ):
            raise ValueError("source metadata requires hash_schema_version v2")
        for field_name, metadata_value in (
            ("fetched_at", self.fetched_at),
            ("provider_observed_at", self.provider_observed_at),
        ):
            if metadata_value is not None and (
                not isinstance(metadata_value, datetime)
                or metadata_value.tzinfo is None
                or metadata_value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware or null")
        if self.source_date is not None and (
            not isinstance(self.source_date, str) or not self.source_date.strip()
        ):
            raise ValueError("source_date must be a non-empty string or null")
        if self.parent_hash is not None and (
            not isinstance(self.parent_hash, str) or not self.parent_hash
        ):
            raise ValueError("parent_hash must be a non-empty hash or null")
