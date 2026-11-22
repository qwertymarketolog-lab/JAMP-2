"""Contract tests for append-only evidence storage and hash-chain integrity."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jamp2.evidence.canonicalize import canonicalize
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord, JSONValue
from jamp2.ledger.repository import InMemoryLedger


def make_record(
    evidence_id: str,
    *,
    claim_id: str = "claim-1",
    raw_body_bytes: bytes = b'{"price":100}',
    parent_hash: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id=claim_id,
        provider="moex.iss",
        url="https://example.test/marketdata",
        http_status=200,
        raw_body_bytes=raw_body_bytes,
        raw_sha256=compute_sha256(raw_body_bytes),
        captured_at_utc=datetime(2026, 11, 19, 20, 0, tzinfo=UTC),
        parsed_value={"price": 100},
        verdict_status="E4_VERIFIED",
        parent_hash=parent_hash,
    )


def test_append_get_and_list_by_claim() -> None:
    ledger = InMemoryLedger()
    first = make_record("evidence-1")
    second = make_record(
        "evidence-2",
        claim_id="claim-2",
        parent_hash=compute_record_hash(first),
    )

    appended_first = ledger.append(first)
    appended_second = ledger.append(second)
    assert appended_first.record_hash == compute_record_hash(first)
    assert appended_second.record_hash == compute_record_hash(second)
    assert ledger.get("evidence-1") == appended_first
    assert ledger.get("missing") is None
    assert ledger.list_by_claim("claim-2") == [appended_second]
    assert ledger.verify_chain()


def test_hash_chain_links_each_record_to_previous_record() -> None:
    ledger = InMemoryLedger()
    first = make_record("evidence-1")
    second = make_record(
        "evidence-2",
        parent_hash=compute_record_hash(first),
    )
    third = make_record(
        "evidence-3",
        parent_hash=compute_record_hash(second),
    )

    ledger.append(first)
    ledger.append(second)
    ledger.append(third)

    assert second.parent_hash == compute_record_hash(first)
    assert third.parent_hash == compute_record_hash(second)
    assert ledger.verify_chain()


def test_tampering_with_raw_body_is_detected() -> None:
    ledger = InMemoryLedger()
    first = make_record("evidence-1")
    second = make_record(
        "evidence-2",
        parent_hash=compute_record_hash(first),
    )
    third = make_record(
        "evidence-3",
        parent_hash=compute_record_hash(second),
    )
    ledger.append(first)
    ledger.append(second)
    ledger.append(third)

    ledger._records[1] = replace(second, raw_body_bytes=b'{"price":999}')

    assert ledger.verify_chain() is False


def test_duplicate_evidence_id_is_rejected_without_overwrite() -> None:
    ledger = InMemoryLedger()
    original = make_record("evidence-1")
    ledger.append(original)

    with pytest.raises(ValueError, match="Duplicate evidence_id"):
        ledger.append(make_record("evidence-1", raw_body_bytes=b'{"price":200}'))

    assert ledger.get("evidence-1") == original
    assert ledger.list_by_claim("claim-1") == [original]
    assert ledger.verify_chain()


def test_append_rejects_invalid_raw_digest_and_parent_hash() -> None:
    ledger = InMemoryLedger()
    first = make_record("evidence-1")
    invalid_digest = replace(first, raw_sha256="0" * 64)
    with pytest.raises(ValueError, match="raw_sha256"):
        ledger.append(invalid_digest)

    invalid_parent = make_record("evidence-2", parent_hash="f" * 64)
    with pytest.raises(ValueError, match="parent_hash"):
        ledger.append(invalid_parent)

    assert ledger.verify_chain()


def test_canonicalization_ignores_key_order_and_normalizes_unicode() -> None:
    left: JSONValue = {"e\u0301": "cafe\u0301", "nested": {"b": 2, "a": 1}}
    right: JSONValue = {"nested": {"a": 1, "b": 2}, "\u00e9": "café"}

    assert canonicalize(left) == canonicalize(right)


def test_different_raw_bytes_have_different_digests_and_record_hashes() -> None:
    first = make_record("evidence-1", raw_body_bytes=b'{"price":100}')
    changed = make_record("evidence-1", raw_body_bytes=b'{"price":101}')

    assert compute_sha256(first.raw_body_bytes) != compute_sha256(changed.raw_body_bytes)
    assert compute_record_hash(first) != compute_record_hash(changed)


def test_evidence_record_hash_binds_canonicalization_version() -> None:
    record = make_record("versioned-evidence")
    changed_version = replace(record, canonicalization_version="future-json-v2")
    assert compute_record_hash(record) != compute_record_hash(changed_version)
