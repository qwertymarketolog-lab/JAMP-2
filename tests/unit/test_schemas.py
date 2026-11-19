"""API transport schema tests."""

from datetime import UTC, datetime

from jamp2.api.schemas import EvidenceCreate


def test_evidence_create_accepts_nested_json() -> None:
    obj = EvidenceCreate(
        evidence_id="nested-json",
        claim_id="claim-1",
        provider="test-provider",
        url="https://example.test/evidence",
        raw_body_base64="e30=",
        raw_sha256="0" * 64,
        captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
        parsed_value={"a": [1, {"b": True}], "c": None},
        verdict_status="E4_VERIFIED",
    )
    assert obj.parsed_value == {"a": [1, {"b": True}], "c": None}
