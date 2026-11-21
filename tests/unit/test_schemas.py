"""Strict claim request schema tests."""

import pytest
from pydantic import ValidationError

from jamp2.api.schemas import ClaimVerifyRequest


def test_claim_verify_accepts_supported_provider_independent_shape() -> None:
    claim = ClaimVerifyRequest.model_validate(
        {
            "text": "SBER share price is 300 RUB",
            "subject": {"kind": "equity_security", "identifier": "SBER"},
            "predicate": "price",
            "expected_value": 300,
            "unit": "RUB",
        }
    )
    assert claim.subject.identifier == "SBER"
    assert claim.expected_value == 300


@pytest.mark.parametrize(
    "extra",
    [
        {"provider": "moex.iss"},
        {"raw_body_base64": "e30="},
        {"raw_sha256": "0" * 64},
        {"verdict_status": "VERIFIED"},
        {"captured_at_utc": "2026-11-20T00:00:00Z"},
    ],
)
def test_claim_verify_rejects_client_authored_evidence_fields(extra: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "text": "SBER share price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
        **extra,
    }
    with pytest.raises(ValidationError):
        ClaimVerifyRequest.model_validate(payload)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_claim_verify_rejects_non_positive_or_non_finite_price(value: float) -> None:
    with pytest.raises(ValidationError):
        ClaimVerifyRequest.model_validate(
            {
                "text": "BTC price",
                "subject": {"kind": "crypto_asset", "identifier": "bitcoin"},
                "predicate": "price",
                "expected_value": value,
                "unit": "USD",
            }
        )
