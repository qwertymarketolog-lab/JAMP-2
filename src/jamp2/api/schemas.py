"""Strict transport schemas for server-side claim verification."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClaimSubject(BaseModel):
    """Typed provider-independent subject; identifiers are interpreted by adapters."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["equity_security", "crypto_asset"]
    identifier: str = Field(min_length=1, max_length=120)


class ClaimVerifyRequest(BaseModel):
    """Only caller-authored claim data; source evidence is always server-acquired."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    subject: ClaimSubject
    predicate: Literal["price"]
    expected_value: float = Field(gt=0, allow_inf_nan=False)
    unit: Literal["RUB", "USD"]
