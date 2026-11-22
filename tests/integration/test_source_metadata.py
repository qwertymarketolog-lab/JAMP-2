"""Offline source metadata and evidence-first arbitration integration checks."""
from datetime import UTC, datetime, timedelta

from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.conflict import SourceObservation
from jamp2.evidence.multisource import (
    MultiSourcePolicy, SourceOutcome, SourceSpec, arbitrate_from_evidence,
)
from jamp2.evidence.policy import SourcePolicyRecord

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def _claim(kind: str, identifier: str, unit: str) -> NormalizedClaim:
    return NormalizedClaim(
        text="current price claim", subject_kind=kind, subject_identifier=identifier,
        predicate="price", expected_value=100, unit=unit,
    )


def _outcome(source: SourceSpec, identifier: str, *, quote: str,
             price_type: str, calendar: bool | None, age_seconds: int = 5):
    captured = {
        "fetched_at": NOW.isoformat(),
        "provider_observed_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "body_complete": True,
    }
    details = {
        "observed_last": 100, "source_identifiers": [identifier],
        "quote_currency": quote, "price_type": price_type,
        "market_calendar_valid": calendar,
    }
    observation = SourceObservation(
        source_id=source.provider_id, fact_key=f"{identifier}:price",
        value=100, unit=quote, usable=True,
    )
    return SourceOutcome(source, "E4_VERIFIED", details, [captured], observation)


def _policy(source: SourceSpec, *, asset: str, identifier: str, quote: str,
            price_type: str, calendar_required: bool) -> MultiSourcePolicy:
    source_policy = SourcePolicyRecord(
        policy_id="offline-e2e", version=1, asset_class=asset,
        required_identifiers=(identifier,), market_calendar_required=calendar_required,
        quote_currency=quote, price_type=price_type,
        required_sources=(source.provider_id,), independence_group="offline-e2e",
        max_age_seconds=3600,
        max_age_by_source={"moex.iss": 1200, "coingecko": 300},
        max_time_skew_seconds=5, verification_mode="SINGLE_SOURCE",
    )
    return MultiSourcePolicy("offline-e2e", "v1", (source,), source_policy=source_policy)


def test_moex_without_confirmed_calendar_is_ambiguous_and_blocked():
    source = SourceSpec("moex", "moex.iss", "iss.moex.com", "moex-iss-adapter-v1")
    result = arbitrate_from_evidence(
        _claim("equity_security", "SBER", "RUB"),
        _policy(source, asset="equity_security", identifier="SBER", quote="RUB",
                price_type="LAST_DEAL", calendar_required=True),
        (_outcome(source, "SBER", quote="RUB", price_type="LAST_DEAL", calendar=None),),
        (),
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert "SOURCE_MARKET_CALENDAR_UNCONFIRMED" in result.reason_codes
    assert "ERR_QUORUM_NOT_MET" in result.reason_codes
    assert result.reducer["status"] == "POLICY_GATE_BLOCKED"


def test_fresh_coingecko_source_is_verified_without_network():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    result = arbitrate_from_evidence(
        _claim("crypto_asset", "bitcoin", "USD"),
        _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                price_type="SPOT", calendar_required=False),
        (_outcome(source, "bitcoin", quote="USD", price_type="SPOT", calendar=None),),
        (),
    )
    assert result.verdict_status == "VERIFIED"
    assert result.verification_mode == "SINGLE_SOURCE"


def test_stale_coingecko_source_is_ambiguous_not_inconclusive():
    source = SourceSpec("coingecko", "coingecko", "api.coingecko.com", "coingecko-adapter-v1")
    result = arbitrate_from_evidence(
        _claim("crypto_asset", "bitcoin", "USD"),
        _policy(source, asset="crypto_asset", identifier="bitcoin", quote="USD",
                price_type="SPOT", calendar_required=False),
        (_outcome(source, "bitcoin", quote="USD", price_type="SPOT",
                  calendar=None, age_seconds=301),),
        (),
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.reason_codes
