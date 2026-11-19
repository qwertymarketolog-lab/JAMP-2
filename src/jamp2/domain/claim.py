"""Provider-independent bounded factual claim."""
from __future__ import annotations

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
