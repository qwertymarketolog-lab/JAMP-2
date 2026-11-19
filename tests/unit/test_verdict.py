from jamp2.domain.claim import Claim
from jamp2.domain.verdict import (
    JampVerdict,
    aggregate_verdict,
    critical_unverified_claims,
)


def test_claim_is_manual_and_extractor_independent():
    assert Claim("SBER 300 RUB", "price", "SBER", 300, "RUB", True).ticker == "SBER"


def test_aggregate_precedence_and_fail_closed():
    assert aggregate_verdict([]) == "PARTIAL"
    assert aggregate_verdict([{"status": "E4_VERIFIED", "critical": True}]) == "VERIFIED"
    mixed = [
        {"status": "E4_INCONCLUSIVE", "critical": True},
        {"status": "E4_CONTRADICTED", "critical": False},
    ]
    assert aggregate_verdict(mixed) == "CONTRADICTED"
    assert aggregate_verdict([{"status": "E4_OUT_OF_SCOPE", "critical": True}]) == "INCONCLUSIVE"
    assert aggregate_verdict([{"status": "E4_INCONCLUSIVE", "critical": False}]) == "PARTIAL"


def test_critical_unverified_claims():
    rows = [
        {"text": "unknown", "critical": True, "status": "E4_INCONCLUSIVE"},
        {"text": "ok", "critical": True, "status": "E4_VERIFIED"},
    ]
    assert critical_unverified_claims(rows) == ["unknown"]


def test_hmac_round_trip_wrong_key_and_tampering():
    item = JampVerdict(
        "trace-1", "VERIFIED", None, "sha256:abc", 1800000000.0,
        claims=[{"text": "SBER", "status": "E4_VERIFIED", "critical": True}],
        aggregate_status="VERIFIED",
    )
    signed = item.sign("secret")
    assert signed.signature and signed.verify("secret")
    assert not signed.verify("wrong")
    assert JampVerdict.from_dict(signed.to_dict()).verify("secret")
    tampered = JampVerdict.from_dict({**signed.to_dict(), "trace_id": "trace-2"})
    assert not tampered.verify("secret")


def test_empty_secret_rejected():
    item = JampVerdict("trace", "INCONCLUSIVE", None, "sha256:x", 1.0)
    try:
        item.sign("")
    except ValueError:
        pass
    else:
        raise AssertionError("empty HMAC secret must be rejected")
