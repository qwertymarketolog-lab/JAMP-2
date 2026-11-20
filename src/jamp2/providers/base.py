"""Shared provider result and protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jamp2.domain.claim import NormalizedClaim


@dataclass(frozen=True)
class ProviderResult:
    """One bounded provider outcome plus exact captured HTTP observations."""

    status: str
    details: dict[str, Any]
    raw_evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sha256(self) -> list[str]:
        return [
            str(item["sha256_body"])
            for item in self.raw_evidence
            if isinstance(item.get("sha256_body"), str)
        ]


class Provider(Protocol):
    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult: ...
