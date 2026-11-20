"""Provider-independent normalized Claim domain contract tests."""

import pytest

from jamp2.domain.claim import NormalizedClaim


def test_normalized_claim_contains_no_provider_specific_identifier_field() -> None:
    claim = NormalizedClaim(
        text="BTC price is 67000 USD",
        subject_kind="crypto_asset",
        subject_identifier="bitcoin",
        predicate="price",
        expected_value=67000,
        unit="USD",
    )
    assert claim.subject_identifier == "bitcoin"
    assert claim.subject_kind == "crypto_asset"
    assert not hasattr(claim, "ticker")
    assert not hasattr(claim, "provider")


@pytest.mark.parametrize(
    "changes",
    [
        {"subject_kind": "provider_name"},
        {"subject_identifier": ""},
        {"predicate": "prediction"},
        {"expected_value": 0},
        {"expected_value": float("inf")},
        {"unit": "EUR"},
    ],
)
def test_normalized_claim_rejects_unsupported_or_invalid_values(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "text": "SBER price",
        "subject_kind": "equity_security",
        "subject_identifier": "SBER",
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
    }
    values.update(changes)
    with pytest.raises(ValueError):
        NormalizedClaim(**values)
