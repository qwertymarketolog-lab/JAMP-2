"""Immutable evidence records captured from bounded provider observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeAlias

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)


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

    def __post_init__(self) -> None:
        required_strings = (
            "evidence_id",
            "claim_id",
            "provider",
            "url",
            "raw_sha256",
            "verdict_status",
        )
        for field_name in required_strings:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be an HTTP status code or null")
        if not isinstance(self.raw_body_bytes, bytes):
            raise ValueError("raw_body_bytes must be bytes")
        if not isinstance(self.captured_at_utc, datetime):
            raise ValueError("captured_at_utc must be a datetime")
        if (
            self.captured_at_utc.tzinfo is None
            or self.captured_at_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("captured_at_utc must be timezone-aware UTC")
        if self.parent_hash is not None and (
            not isinstance(self.parent_hash, str) or not self.parent_hash
        ):
            raise ValueError("parent_hash must be a non-empty hash or null")
