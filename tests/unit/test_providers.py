import base64
import hashlib
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

from jamp2.domain.claim import Claim
from jamp2.providers import coingecko, moex

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
def test_moex_valid_raw_capture_sha256(mock_open):
    mock_open.return_value = response(securities(["SBER"]))
    result = moex.verify_claim(Claim("SBER exists", "exists", "SBER", None, "RUB"))
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
    result = moex.verify_claim(Claim("unknown", "exists", "NOPE", None, "RUB"))
    assert result.status == moex.E4_OUT_OF_SCOPE
    mock_open.side_effect = urllib.error.URLError("offline")
    assert moex.verify_claim(Claim("SBER", "exists", "SBER", None)).status == moex.E4_INCONCLUSIVE
    mock_open.side_effect = None
    mock_open.return_value = response([])
    assert moex.verify_claim(Claim("SBER", "exists", "SBER", None)).status == moex.E4_INCONCLUSIVE

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
    unavailable = coingecko.verify_claim(Claim("BTC exists", "existence", "bitcoin", None))
    assert unavailable.status == coingecko.E4_INCONCLUSIVE
    assert unavailable.raw_evidence[0]["status_code"] == 503
    mock_open.side_effect = None
    mock_open.return_value = response([])
    malformed = coingecko.verify_claim(Claim("BTC exists", "existence", "bitcoin", None))
    assert malformed.status == coingecko.E4_INCONCLUSIVE

def test_currency_mismatch_is_out_of_scope_without_http():
    with patch("urllib.request.urlopen") as mock_open:
        result = moex.verify_claim(Claim("SBER USD", "price", "SBER", 300, "USD"))
    assert result.status == moex.E4_OUT_OF_SCOPE
    mock_open.assert_not_called()
