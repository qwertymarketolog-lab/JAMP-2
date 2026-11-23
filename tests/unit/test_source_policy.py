"""Deterministic tests for Source Policy v1.1 eligibility rules."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jamp2.evidence.policy import (
    SourceObservationInput,
    SourcePolicyRecord,
    SourceProvenance,
    evaluate_source_policy,
    provenance_is_independent,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def provenance(provider: str, upstream: str, transport: str, calculation: str):
    return SourceProvenance(
        provider_id=provider,
        upstream_entity=upstream,
        aggregation_methodology=f"method-{provider}",
        transport_failure_domain=transport,
        calculation_failure_domain=calculation,
        independence_status="CONFIRMED",
    )


def policy(mode="MULTI_SOURCE", provenance_items=None):
    sources = ("a", "b") if mode == "MULTI_SOURCE" else ("a",)
    return SourcePolicyRecord(
        policy_id="policy-test",
        version=1,
        asset_class="equity_security",
        required_identifiers=("isin", "ticker"),
        market_calendar_required=True,
        quote_currency="RUB",
        price_type="LAST_DEAL",
        required_sources=sources,
        independence_group="test-group",
        max_age_seconds=60,
        max_time_skew_seconds=5,
        verification_mode=mode,
        provenance=tuple(provenance_items or ()),
    )


def observation(provider, observed_at=NOW - timedelta(seconds=2), **overrides):
    values = {
        "provider_id": provider,
        "provider_observed_at": observed_at,
        "fetched_at": NOW,
        "body_complete": True,
        "request_succeeded": True,
        "identifiers": ("isin", "ticker"),
        "quote_currency": "RUB",
        "price_type": "LAST_DEAL",
        "market_calendar_valid": True,
    }
    values.update(overrides)
    return SourceObservationInput(**values)


def independent_pair():
    return (
        provenance("a", "exchange-a", "network-a", "calc-a"),
        provenance("b", "exchange-b", "network-b", "calc-b"),
    )


def test_provenance_requires_distinct_upstream_and_failure_domains():
    assert provenance_is_independent(independent_pair())
    assert not provenance_is_independent(
        (provenance("a", "same", "network-a", "calc-a"),
         provenance("b", "same", "network-b", "calc-b"))
    )
    assert not provenance_is_independent(
        (provenance("a", "up-a", "shared-network", "calc-a"),
         provenance("b", "up-b", "shared-network", "calc-b"))
    )


def test_unknown_provenance_disqualifies_multi_source_policy():
    result = evaluate_source_policy(
        policy(provenance_items=(
            provenance("a", "up-a", "net-a", "calc-a"),
            SourceProvenance("b", None, None, None, None, "UNKNOWN"),
        )),
        (observation("a"), observation("b")),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert not result.eligible_for_reducer
    assert "ERR_INCOMPATIBLE_PROVENANCE" in result.decision_reason_codes


def test_missing_provider_observation_timestamp_is_incomplete():
    result = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (observation("a", None), observation("b")),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.observations[0].observation_status == "INCOMPLETE"


def test_date_only_timestamp_is_not_accepted_as_observation_time():
    result = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (observation("a", "2026-11-20"), observation("b")),
        now=NOW,
    )
    assert result.observations[0].observation_status == "INCOMPLETE"


def test_expired_observation_is_ambiguous():
    result = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (observation("a", NOW - timedelta(seconds=61)), observation("b")),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.observations[0].observation_status == "EXPIRED"


def test_skew_exceeding_policy_expires_valid_observations():
    result = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (
            observation("a", NOW - timedelta(seconds=10)),
            observation("b", NOW - timedelta(seconds=1)),
        ),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert "ERR_TEMPORAL_SKEW_EXCEEDED" in result.decision_reason_codes
    assert all(item.observation_status == "EXPIRED" for item in result.observations)


def test_partial_failure_is_ambiguous_but_all_failed_is_inconclusive():
    partial = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (observation("a"), observation("b", request_succeeded=False)),
        now=NOW,
    )
    all_failed = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (
            observation("a", request_succeeded=False),
            observation("b", request_succeeded=False),
        ),
        now=NOW,
    )
    assert partial.verdict_status == "AMBIGUOUS"
    assert all_failed.verdict_status == "INCONCLUSIVE"


def test_single_source_is_explicit_and_has_no_independent_cross_check():
    result = evaluate_source_policy(
        policy("SINGLE_SOURCE"),
        (observation("a"),),
        now=NOW,
    )
    assert result.eligible_for_reducer
    assert result.verification_mode == "SINGLE_SOURCE"
    assert not result.independent_cross_check
    assert not result.decision_reason_codes


def test_multi_source_requires_at_least_two_sources():
    with pytest.raises(ValueError, match="at least two"):
        SourcePolicyRecord(
            policy_id="bad",
            version=1,
            asset_class="equity_security",
            required_identifiers=("ticker",),
            market_calendar_required=False,
            quote_currency="RUB",
            price_type="LAST_DEAL",
            required_sources=("a",),
            independence_group="g",
            max_age_seconds=60,
            max_time_skew_seconds=5,
            verification_mode="MULTI_SOURCE",
        )


def test_required_market_calendar_must_be_confirmed():
    result = evaluate_source_policy(
        policy(provenance_items=independent_pair()),
        (
            observation("a", market_calendar_valid=None),
            observation("b"),
        ),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.observations[0].observation_status == "INCOMPLETE"
    assert (
        result.observations[0].reason_code
        == "SOURCE_MARKET_CALENDAR_UNCONFIRMED"
    )


def test_per_source_ttl_overrides_global_max_age():
    source_policy = SourcePolicyRecord(
        policy_id="ttl-test", version=1, asset_class="crypto_asset",
        required_identifiers=("bitcoin",), market_calendar_required=False,
        quote_currency="USD", price_type="SPOT", required_sources=("a",),
        independence_group="ttl", max_age_seconds=3600, max_time_skew_seconds=0,
        verification_mode="SINGLE_SOURCE", max_age_by_source={"a": 10},
    )
    result = evaluate_source_policy(
        source_policy,
        (observation("a", NOW - timedelta(seconds=11), identifiers=("bitcoin",),
            quote_currency="USD", price_type="SPOT", market_calendar_valid=None),),
        now=NOW,
    )
    assert result.verdict_status == "AMBIGUOUS"
    assert result.observations[0].observation_status == "EXPIRED"


def test_per_source_ttl_must_be_positive():
    with pytest.raises(ValueError, match="positive integer"):
        SourcePolicyRecord(
            policy_id="ttl-test", version=1, asset_class="crypto_asset",
            required_identifiers=("bitcoin",), market_calendar_required=False,
            quote_currency="USD", price_type="SPOT", required_sources=("a",),
            independence_group="ttl", max_age_seconds=60, max_time_skew_seconds=0,
            verification_mode="SINGLE_SOURCE", max_age_by_source={"a": 0},
        )



def test_per_source_ttl_mapping_is_immutable_and_detached_from_input():
    supplied_ttls = {"a": 10}
    source_policy = replace(policy("SINGLE_SOURCE"), max_age_by_source=supplied_ttls)

    supplied_ttls["a"] = 20
    assert source_policy.max_age_by_source["a"] == 10

    with pytest.raises(TypeError):
        source_policy.max_age_by_source["a"] = 30
