import base64
import hashlib
import io
import json
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from jamp2.domain.claim import Claim, NormalizedClaim
from jamp2.providers import coingecko, moex
from jamp2.providers.base import ProviderResult


def response(payload):
    item = MagicMock()
    item.read.return_value = json.dumps(payload).encode()
    item.status = 200
    return MagicMock(__enter__=MagicMock(return_value=item))


def securities(*rows):
    return {"securities": {"columns": ["secid"], "data": list(rows)}}


def marketdata(*rows):
    return {
        "marketdata": {
            "columns": ["SECID", "LAST", "BOARDID", "TRADEDATE"],
            "data": list(rows),
        }
    }


@patch("urllib.request.urlopen")
def test_moex_returns_inconclusive_on_missing_value(mock_open):
    claim = Claim(
        text="SBER is listed",
        type="price",
        ticker="SBER",
        value=None,
        currency="RUB",
    )
    result = moex.verify_claim(claim)
    assert result.status == "E4_INCONCLUSIVE"
    assert result.details["reason"] == "MISSING_VALUE"
    mock_open.assert_not_called()


@patch("urllib.request.urlopen")
def test_coingecko_returns_inconclusive_on_missing_value(mock_open):
    claim = Claim(
        text="BTC exists",
        type="price",
        ticker="bitcoin",
        value=None,
        currency="USD",
    )
    result = coingecko.verify_claim(claim)
    assert result.status == "E4_INCONCLUSIVE"
    assert result.details["reason"] == "MISSING_VALUE"
    mock_open.assert_not_called()


@patch("urllib.request.urlopen")
def test_moex_rejects_non_price_claim(mock_open):
    claim = Claim(
        text="FakeTech exists",
        type="entity_existence",
        ticker="FAKETECH",
        value=None,
    )
    result = moex.verify_claim(claim)
    assert result.status == "E4_OUT_OF_SCOPE"
    assert result.details["reason"] == "NOT_A_PRICE_CLAIM"
    mock_open.assert_not_called()


@patch("urllib.request.urlopen")
def test_coingecko_rejects_non_price_claim(mock_open):
    claim = Claim(
        text="FakeTech exists",
        type="entity_existence",
        ticker="faketech",
        value=None,
    )
    result = coingecko.verify_claim(claim)
    assert result.status == "E4_OUT_OF_SCOPE"
    assert result.details["reason"] == "NOT_A_PRICE_CLAIM"
    mock_open.assert_not_called()


@patch("urllib.request.urlopen")
def test_moex_valid_raw_capture_sha256(mock_open):
    mock_open.side_effect = [
        response(securities(["SBER"])),
        response(marketdata(["SBER", 100.0, "TQBR", "2026-11-19"])),
    ]
    result = moex.verify_claim(Claim("SBER 100 RUB", "price", "SBER", 100, "RUB"))
    assert result.status == moex.E4_VERIFIED and result.raw_evidence
    raw = base64.b64decode(result.raw_evidence[0]["raw_body_base64"])
    assert hashlib.sha256(raw).hexdigest() == result.sha256[0]


@patch("urllib.request.urlopen")
def test_moex_contradicted_price(mock_open):
    mock_open.side_effect = [
        response(securities(["SBER"])),
        response(marketdata(["SBER", 100.0, "TQBR", "2026-11-19"])),
    ]
    result = moex.verify_claim(Claim("SBER 5000 RUB", "price", "SBER", 5000, "RUB"))
    assert result.status == moex.E4_CONTRADICTED and len(result.raw_evidence) == 2


@patch("urllib.request.urlopen")
def test_moex_out_of_scope_unavailable_and_malformed(mock_open):
    mock_open.return_value = response(securities())
    result = moex.verify_claim(Claim("unknown", "price", "NOPE", 100, "RUB"))
    assert result.status == moex.E4_OUT_OF_SCOPE
    mock_open.side_effect = urllib.error.URLError("offline")
    assert moex.verify_claim(Claim("SBER", "price", "SBER", 100)).status == moex.E4_INCONCLUSIVE
    mock_open.side_effect = None
    mock_open.return_value = response([])
    assert moex.verify_claim(Claim("SBER", "price", "SBER", 100)).status == moex.E4_INCONCLUSIVE


@patch("urllib.request.urlopen")
def test_coingecko_valid_capture_and_sha256(mock_open):
    mock_open.return_value = response({"bitcoin": {"usd": 67000}})
    result = coingecko.verify_claim(Claim("BTC 67000 USD", "price", "bitcoin", 67000, "USD"))
    assert result.status == coingecko.E4_VERIFIED
    raw = base64.b64decode(result.raw_evidence[0]["raw_body_base64"])
    assert hashlib.sha256(raw).hexdigest() == result.sha256[0]


@patch("urllib.request.urlopen")
def test_coingecko_contradicted(mock_open):
    mock_open.return_value = response({"bitcoin": {"usd": 67000}})
    result = coingecko.verify_claim(
        Claim("BTC 100000 USD", "price", "bitcoin", 100000, "USD")
    )
    assert result.status == coingecko.E4_CONTRADICTED


@patch("urllib.request.urlopen")
def test_coingecko_out_of_scope_unavailable_and_malformed(mock_open):
    mock_open.return_value = response({})
    out_of_scope = coingecko.verify_claim(
        Claim("unknown", "price", "not-a-coin", 10, "USD")
    )
    assert out_of_scope.status == coingecko.E4_OUT_OF_SCOPE
    mock_open.side_effect = urllib.error.HTTPError(
        "https://api.coingecko.com", 503, "Unavailable", {}, io.BytesIO(b"down")
    )
    unavailable = coingecko.verify_claim(Claim("BTC price", "price", "bitcoin", 10))
    assert unavailable.status == coingecko.E4_INCONCLUSIVE
    assert unavailable.raw_evidence[0]["status_code"] == 503
    mock_open.side_effect = None
    mock_open.return_value = response([])
    malformed = coingecko.verify_claim(Claim("BTC price", "price", "bitcoin", 10))
    assert malformed.status == coingecko.E4_INCONCLUSIVE


def test_currency_mismatch_is_out_of_scope_without_http():
    with patch("urllib.request.urlopen") as mock_open:
        result = moex.verify_claim(Claim("SBER USD", "price", "SBER", 300, "USD"))
    assert result.status == moex.E4_OUT_OF_SCOPE
    mock_open.assert_not_called()


@patch("jamp2.providers.moex.verify_claim")
def test_moex_provider_resolves_only_equity_security(mock_verify):
    mock_verify.return_value = ProviderResult(moex.E4_VERIFIED, {})
    claim = NormalizedClaim(
        "SBER price",
        "equity_security",
        "SBER",
        "price",
        300,
        "RUB",
    )
    result = moex.MoexProvider().verify_claim(claim)
    assert result.status == moex.E4_VERIFIED
    legacy_claim = mock_verify.call_args.args[0]
    assert legacy_claim.ticker == "SBER"
    assert legacy_claim.currency == "RUB"


@patch("jamp2.providers.moex.verify_claim")
def test_moex_provider_rejects_crypto_subject_without_source_call(mock_verify):
    claim = NormalizedClaim(
        "BTC price",
        "crypto_asset",
        "bitcoin",
        "price",
        67000,
        "USD",
    )
    result = moex.MoexProvider().verify_claim(claim)
    assert result.status == moex.E4_OUT_OF_SCOPE
    mock_verify.assert_not_called()


@patch("jamp2.providers.coingecko.verify_claim")
def test_coingecko_provider_resolves_only_crypto_asset(mock_verify):
    mock_verify.return_value = ProviderResult(coingecko.E4_VERIFIED, {})
    claim = NormalizedClaim(
        "BTC price",
        "crypto_asset",
        "bitcoin",
        "price",
        67000,
        "USD",
    )
    result = coingecko.CoinGeckoProvider().verify_claim(claim)
    assert result.status == coingecko.E4_VERIFIED
    legacy_claim = mock_verify.call_args.args[0]
    assert legacy_claim.ticker == "bitcoin"
    assert legacy_claim.currency == "USD"


@patch("jamp2.providers.coingecko.verify_claim")
def test_coingecko_provider_rejects_equity_subject_without_source_call(mock_verify):
    claim = NormalizedClaim(
        "SBER price",
        "equity_security",
        "SBER",
        "price",
        300,
        "RUB",
    )
    result = coingecko.CoinGeckoProvider().verify_claim(claim)
    assert result.status == coingecko.E4_OUT_OF_SCOPE
    mock_verify.assert_not_called()



def test_coingecko_last_updated_at_parser_is_explicit_and_fail_closed():
    cases = [
        ("valid", 1_700_000_000, "VALID", "2023-11-14T22:13:20+00:00"),
        ("missing", None, "MISSING", None),
        ("null", None, "MISSING", None),
        ("string", "1700000000", "INVALID_TYPE", None),
        ("boolean", True, "INVALID_TYPE", None),
        ("out_of_range", 1e100, "OUT_OF_RANGE", None),
        ("non_finite", float("inf"), "NON_FINITE", None),
    ]
    with patch("urllib.request.urlopen") as mock_open:
        for case, timestamp, expected_status, expected_observed in cases:
            coin_data = {"usd": 67_000}
            if case != "missing":
                coin_data["last_updated_at"] = timestamp
            mock_open.return_value = response({"bitcoin": coin_data})

            result = coingecko.verify_claim(
                Claim("BTC 67000 USD", "price", "bitcoin", 67_000, "USD")
            )
            capture = result.raw_evidence[0]

            assert capture["provider_observed_at_status"] == expected_status
            assert capture.get("provider_observed_at") == expected_observed
            assert "include_last_updated_at=true" in mock_open.call_args.args[0]
            if case not in {"missing", "null"}:
                expected_raw = "inf" if case == "non_finite" else timestamp
                assert capture["provider_observed_at_raw"] == expected_raw

            if expected_observed is not None:
                assert datetime.fromisoformat(expected_observed).tzinfo == UTC
