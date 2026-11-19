"""Transport schemas for evidence ingestion and retrieval."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from jamp2.evidence.model import JSONValue


class EvidenceCreate(BaseModel):
    """Client-supplied evidence fields; chain linkage remains server-owned."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=255)
    claim_id: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1)
    http_status: int | None = Field(default=None, ge=100, le=599)
    raw_body_base64: str
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at_utc: datetime
    parsed_value: JSONValue
    verdict_status: str = Field(min_length=1, max_length=64)
