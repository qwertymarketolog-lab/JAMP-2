"""Provider-independent bounded factual claim."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Claim:
    """A manually constructed claim; extraction is deliberately outside this package."""

    text: str
    type: str
    ticker: str
    value: float | int | None
    currency: str | None = None
    critical: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("type must be a non-empty string")
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        if self.currency is not None and not isinstance(self.currency, str):
            raise ValueError("currency must be a string or null")
        if not isinstance(self.critical, bool):
            raise ValueError("critical must be a boolean")
        if isinstance(self.value, bool) or (
            self.value is not None and not isinstance(self.value, (int, float))
        ):
            raise ValueError("value must be numeric or null")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedClaim:
    """Provider-independent normalized claim passed to provider resolution."""

    text: str
    subject_kind: str
    subject_identifier: str
    predicate: str
    expected_value: float | int
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        if self.subject_kind not in {"equity_security", "crypto_asset"}:
            raise ValueError("unsupported normalized subject kind")
        if not isinstance(self.subject_identifier, str) or not self.subject_identifier.strip():
            raise ValueError("subject_identifier must be a non-empty string")
        if self.predicate != "price":
            raise ValueError("unsupported normalized predicate")
        if (
            isinstance(self.expected_value, bool)
            or not isinstance(self.expected_value, (int, float))
            or not math.isfinite(float(self.expected_value))
            or float(self.expected_value) <= 0
        ):
            raise ValueError("expected_value must be a finite positive number")
        if self.unit not in {"RUB", "USD"}:
            raise ValueError("unsupported normalized unit")
