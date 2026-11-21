"""Conflict reducer tests: disagreements are preserved, never averaged away."""

import pytest

from jamp2.evidence.conflict import SourceObservation, detect_conflicts


def test_disagreeing_sources_produce_ambiguous_with_all_observations() -> None:
    result = detect_conflicts(
        [
            SourceObservation("moex", "SBER.price", 300, "RUB"),
            SourceObservation("broker", "SBER.price", 302, "RUB"),
            SourceObservation("exchange-feed", "SBER.price", 300, "RUB"),
        ]
    )
    assert result["status"] == "AMBIGUOUS"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["observations"] == [
        {"source_id": "moex", "value": "300"},
        {"source_id": "broker", "value": "302"},
        {"source_id": "exchange-feed", "value": "300"},
    ]


def test_matching_sources_only_report_no_conflict_observed() -> None:
    result = detect_conflicts(
        [
            SourceObservation("source-a", "BTC.price", "67000.0", "USD"),
            SourceObservation("source-b", "BTC.price", 67000, "USD"),
        ]
    )
    assert result["status"] == "NO_CONFLICT_OBSERVED"
    assert result["conflicts"] == []


def test_unusable_observation_is_not_a_conflicting_fact() -> None:
    result = detect_conflicts(
        [
            SourceObservation("source-a", "BTC.price", 67000, "USD"),
            SourceObservation("source-b", "BTC.price", 1, "USD", usable=False),
        ]
    )
    assert result["status"] == "NO_CONFLICT_OBSERVED"
    assert result["usable_observation_count"] == 1
    assert result["source_ids"] == ["source-a"]


def test_different_units_are_not_compared_without_conversion_policy() -> None:
    result = detect_conflicts(
        [
            SourceObservation("source-a", "asset.price", 100, "USD"),
            SourceObservation("source-b", "asset.price", 9000, "RUB"),
        ]
    )
    assert result["status"] == "NO_CONFLICT_OBSERVED"
    assert result["conflicts"] == []


def test_no_usable_observations_is_inconclusive() -> None:
    result = detect_conflicts(
        [SourceObservation("source-a", "asset.price", 100, "USD", usable=False)]
    )
    assert result["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "value",
    [True, "not-a-number"],
)
def test_non_numeric_values_are_not_promoted_to_usable(value: object) -> None:
    result = detect_conflicts([SourceObservation("source-a", "fact", value, "unit")])
    assert result["status"] == "INCONCLUSIVE"


def test_observation_identity_and_fact_key_are_required() -> None:
    with pytest.raises(ValueError):
        SourceObservation("", "fact", 1, "unit")
    with pytest.raises(ValueError):
        SourceObservation("source", "", 1, "unit")
