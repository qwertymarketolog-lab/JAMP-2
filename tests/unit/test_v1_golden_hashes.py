"""Frozen v1 golden vectors for Evidence and Verdict record hashes.

These constants are compatibility anchors. Do not regenerate them from the
implementation under test when changing canonicalization behavior.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from jamp2.evidence.digest import compute_record_hash, compute_sha256, compute_verdict_hash
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord

RAW_BODY = b'{"price":100}'
RAW_SHA256 = "fbcfc7a6ca136b21ff0a7101016fc91230798ffc2e6f5fc856df2fc29adca67a"

EVIDENCE_GOLDEN_HASHES = (
    "98e099906bb786bd99ee160735a3e2155be931fbac686f35d8be9b956939bd97",
    "4fba110cd8e3160fb2360423df557dd9d3aee95103c371b3efdfd119677ae90c",
    "a6c0c4b6715a03f60e0e00003af44ecaebf53a891f5d1178228b1cd9b0e8eebc",
)

VERDICT_GOLDEN_HASHES = (
    "b7da3622b835993860b47d679c12eb06705a633d89ca260f692b182d07481064",
    "90a9e7bf2b32babf5924e60797e56749c14d9fa95e9309518a9e327fade71756",
)


def make_evidence(evidence_id: str, parent_hash: str | None = None) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-1",
        provider="moex.iss",
        url="https://example.test/marketdata",
        http_status=200,
        raw_body_bytes=RAW_BODY,
        raw_sha256=compute_sha256(RAW_BODY),
        captured_at_utc=datetime(2026, 11, 19, 20, 0, tzinfo=UTC),
        parsed_value={"price": 100},
        verdict_status="E4_VERIFIED",
        parent_hash=parent_hash,
    )


def make_verdict() -> VerdictRecord:
    return VerdictRecord(
        verdict_id="verdict-1",
        claim_id="claim-1",
        status="VERIFIED",
        evidence_ids=("evidence-1", "evidence-2"),
        policy_id="jamp2.provider-price-comparison",
        policy_version="provider-defaults-v1",
        code_version="commit-sha",
        canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        reason_codes=("E4_VERIFIED",),
        explanation="Source observation matched the claim under the recorded policy.",
    )


def test_v1_evidence_raw_bytes_and_three_record_hash_chain_are_golden() -> None:
    first = make_evidence("evidence-1")
    second = make_evidence("evidence-2", parent_hash=EVIDENCE_GOLDEN_HASHES[0])
    third = make_evidence("evidence-3", parent_hash=EVIDENCE_GOLDEN_HASHES[1])

    assert first.raw_body_bytes == RAW_BODY
    assert first.raw_sha256 == RAW_SHA256
    assert tuple(compute_record_hash(item) for item in (first, second, third)) == (
        *EVIDENCE_GOLDEN_HASHES,
    )
    assert second.parent_hash == compute_record_hash(first)
    assert third.parent_hash == compute_record_hash(second)


def test_v1_verdict_hashes_and_two_record_chain_are_golden() -> None:
    first = make_verdict()
    second = replace(
        first,
        verdict_id="verdict-2",
        parent_hash=VERDICT_GOLDEN_HASHES[0],
    )

    assert tuple(compute_verdict_hash(item) for item in (first, second)) == (
        *VERDICT_GOLDEN_HASHES,
    )
    assert second.parent_hash == compute_verdict_hash(first)
