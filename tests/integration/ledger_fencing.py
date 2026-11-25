"""Helpers for integration tests that exercise fenced Ledger append APIs."""

from __future__ import annotations

import uuid
from typing import Any

from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord


async def reserve_test_owner(ledger: Any, claim_id: str) -> tuple[str, str]:
    nonce = uuid.uuid4().hex
    key_hash = compute_sha256(f"test-key:{nonce}".encode())
    request_digest = compute_sha256(f"test-request:{nonce}".encode())
    state, owner_token, _ = await ledger.begin_idempotency(
        key_hash=key_hash,
        request_digest=request_digest,
        claim_id=claim_id,
    )
    assert state == "NEW" and owner_token
    return key_hash, owner_token


async def append_evidence_owned(ledger: Any, record: EvidenceRecord) -> EvidenceRecord:
    key_hash, owner_token = await reserve_test_owner(ledger, record.claim_id)
    return await ledger.append(record, key_hash=key_hash, owner_token=owner_token)


async def append_verdict_owned(ledger: Any, record: VerdictRecord) -> VerdictRecord:
    key_hash, owner_token = await reserve_test_owner(ledger, record.claim_id)
    return await ledger.append_verdict(record, key_hash=key_hash, owner_token=owner_token)
