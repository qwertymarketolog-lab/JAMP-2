"""Pure replay tests: exact stored observations, no live provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.replay import replay_verdict
from jamp2.evidence.verdict import VerdictRecord


def claim_snapshot(
    subject_kind: str = "equity_security",
    subject_identifier: str = "SBER",
    unit: str = "RUB",
    expected_value: float = 100.0,
) -> dict[str, object]:
    return {
        "text": "bounded price claim",
        "subject_kind": subject_kind,
        "subject_identifier": subject_identifier,
        "predicate": "price",
        "expected_value": expected_value,
        "unit": unit,
    }


def evidence(
    evidence_id: str,
    provider: str,
    url: str,
    raw: bytes,
    claim: dict[str, object],
    *,
    http_status: int = 200,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-replay",
        provider=provider,
        url=url,
        http_status=http_status,
        raw_body_bytes=raw,
        raw_sha256=compute_sha256(raw),
        captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        parsed_value={"claim": claim, "provider_result": {}},
        verdict_status="OBSERVATION_CAPTURED",
        parent_hash=None,
        source_timestamp="2026-11-19",
        body_complete=True,
        provider_adapter_version="moex-iss-adapter-v1"
        if provider == "moex.iss"
        else "coingecko-adapter-v1",
        parser_version="jamp-provider-json-v1",
    )


def verdict(evidence_ids: tuple[str, ...], status: str = "VERIFIED") -> VerdictRecord:
    return VerdictRecord(
        verdict_id="verdict-replay",
        claim_id="claim-replay",
        status=status,
        evidence_ids=evidence_ids,
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="recorded-commit-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Original recorded decision.",
    )


def test_moex_replay_reparses_stored_catalog_and_marketdata_bytes() -> None:
    claim = claim_snapshot()
    catalog = json.dumps(
        {"securities": {"columns": ["secid"], "data": [["SBER"]]}}
    ).encode()
    marketdata = json.dumps(
        {"marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 100.0]]}}
    ).encode()
    records = [
        evidence(
            "evidence-catalog",
            "moex.iss",
            "https://iss.moex.com/iss/securities.json",
            catalog,
            claim,
        ),
        evidence(
            "evidence-price",
            "moex.iss",
            "https://iss.moex.com/iss/marketdata.json",
            marketdata,
            claim,
        ),
    ]
    result = replay_verdict(verdict(("evidence-catalog", "evidence-price")), records)
    assert result["replay_status"] == "VERIFIED"
    assert result["matches_original"] is True
    assert result["observed_value"] == 100.0
    assert result["network_calls"] == 0


def test_moex_replay_preserves_contradiction() -> None:
    claim = claim_snapshot(expected_value=500.0)
    catalog = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
    marketdata = b'{"marketdata":{"columns":["SECID","LAST"],"data":[["SBER",100.0]]}}'
    records = [
        evidence(
            "evidence-catalog",
            "moex.iss",
            "https://iss.moex.com/iss/securities.json",
            catalog,
            claim,
        ),
        evidence(
            "evidence-price",
            "moex.iss",
            "https://iss.moex.com/iss/marketdata.json",
            marketdata,
            claim,
        ),
    ]
    result = replay_verdict(
        verdict(("evidence-catalog", "evidence-price"), status="CONTRADICTED"),
        records,
    )
    assert result["replay_status"] == "CONTRADICTED"
    assert result["matches_original"] is True


def test_coingecko_replay_uses_stored_usd_response() -> None:
    claim = claim_snapshot("crypto_asset", "bitcoin", "USD", 67000.0)
    raw = b'{"bitcoin":{"usd":67000.0}}'
    record = evidence(
        "evidence-coin",
        "coingecko",
        "https://api.coingecko.com/api/v3/simple/price",
        raw,
        claim,
    )
    result = replay_verdict(verdict(("evidence-coin",)), [record])
    assert result["replay_status"] == "VERIFIED"
    assert result["observed_value"] == 67000.0
    assert result["network_calls"] == 0


def test_replay_fails_closed_for_unknown_policy_or_bad_digest() -> None:
    claim = claim_snapshot()
    raw = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
    record = evidence(
        "evidence-catalog",
        "moex.iss",
        "https://iss.moex.com/iss/securities.json",
        raw,
        claim,
    )
    unknown_policy = replace(
        verdict(("evidence-catalog",)),
        policy_version="unrecognized-policy",
    )
    assert replay_verdict(unknown_policy, [record])["replay_status"] == "INCONCLUSIVE"
    corrupted = replace(record, raw_sha256="0" * 64)
    assert (
        replay_verdict(verdict(("evidence-catalog",)), [corrupted])["reason_code"]
        == "EVIDENCE_INTEGRITY_OR_VERSION_MISMATCH"
    )
