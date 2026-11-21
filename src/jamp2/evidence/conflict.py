"""Conflict-preserving reducer for independent source observations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """One source's value for a normalized fact; it is not a truth verdict."""

    source_id: str
    fact_key: str
    value: Decimal | int | float | str
    unit: str
    usable: bool = True

    def __post_init__(self) -> None:
        for field_name in ("source_id", "fact_key", "unit"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.usable, bool):
            raise ValueError("usable must be a boolean")


def _normalized_number(value: Decimal | int | float | str) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def detect_conflicts(observations: list[SourceObservation]) -> dict[str, Any]:
    """Return AMBIGUOUS whenever usable observations disagree without a policy."""
    usable: list[tuple[SourceObservation, Decimal]] = []
    for observation in observations:
        if not observation.usable:
            continue
        normalized = _normalized_number(observation.value)
        if normalized is not None:
            usable.append((observation, normalized))

    groups: dict[tuple[str, str], list[tuple[SourceObservation, Decimal]]] = {}
    for observation, normalized in usable:
        key = (observation.fact_key, observation.unit)
        groups.setdefault(key, []).append((observation, normalized))

    conflicts: list[dict[str, Any]] = []
    for (fact_key, unit), rows in sorted(groups.items()):
        distinct_values = {value for _, value in rows}
        if len(distinct_values) > 1:
            conflicts.append(
                {
                    "fact_key": fact_key,
                    "unit": unit,
                    "observations": [
                        {
                            "source_id": observation.source_id,
                            "value": str(value),
                        }
                        for observation, value in rows
                    ],
                }
            )

    if conflicts:
        status = "AMBIGUOUS"
    elif usable:
        status = "NO_CONFLICT_OBSERVED"
    else:
        status = "INCONCLUSIVE"
    return {
        "status": status,
        "conflicts": conflicts,
        "usable_observation_count": len(usable),
        "source_ids": [observation.source_id for observation, _ in usable],
    }
