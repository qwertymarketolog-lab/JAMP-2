"""Fail-closed authentication and server-owned evidence acquisition tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import httpx

from jamp2.api.app import create_app
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.providers.base import ProviderResult


def request_payload() -> dict[str, object]:
    return {
        "text": "SBER share price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
    }


class MemoryLedger:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []
        self.verdicts: list[VerdictRecord] = []

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        self.records.append(record)
        return record

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        return next(
            (record for record in self.records if record.evidence_id == evidence_id),
            None,
        )

    async def append_verdict(self, record: VerdictRecord) -> VerdictRecord:
        self.verdicts.append(record)
        return record

    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None:
        return next(
            (record for record in self.verdicts if record.verdict_id == verdict_id),
            None,
        )


class StubProvider:
    def __init__(self, *, valid_digest: bool = True, url: str = "https://iss.moex.com/iss/x"):
        self.calls = 0
        self.valid_digest = valid_digest
        self.url = url

    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        self.calls += 1
        raw = b'{"last":300}'
        digest = (
            "a" * 64
            if not self.valid_digest
            else hashlib.sha256(raw).hexdigest()
        )
        return ProviderResult(
            "E4_VERIFIED",
            {"observed_last": 300.0},
            [
                {
                    "source": "moex.iss",
                    "url": self.url,
                    "status_code": 200,
                    "body_complete": True,
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "sha256_body": digest,
                }
            ],
        )


def test_missing_or_invalid_api_key_has_no_provider_or_ledger_side_effects() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        provider = StubProvider()
        app = create_app(
            ledger, api_key="correct-key", providers={"moex": provider}, code_version="test-code"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            missing = await client.post("/v1/claims/verify", json=request_payload())
            invalid = await client.post(
                "/v1/claims/verify",
                json=request_payload(),
                headers={"X-API-Key": "wrong-key"},
            )
        assert missing.status_code == 401
        assert invalid.status_code == 401
        assert provider.calls == 0
        assert ledger.records == []

    asyncio.run(run())


def test_missing_server_api_key_configuration_fails_closed(monkeypatch: Any) -> None:
    async def run() -> None:
        monkeypatch.delenv("JAMP2_API_KEY", raising=False)
        ledger = MemoryLedger()
        provider = StubProvider()
        app = create_app(ledger, providers={"moex": provider})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": "anything"},
        ) as client:
            response = await client.post("/v1/claims/verify", json=request_payload())
        assert response.status_code == 503
        assert provider.calls == 0
        assert ledger.records == []
        assert ledger.verdicts == []

    asyncio.run(run())


def test_old_client_evidence_endpoint_is_not_available() -> None:
    async def run() -> None:
        app = create_app(MemoryLedger(), api_key="correct-key")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": "correct-key"},
        ) as client:
            response = await client.post(
                "/v1/evidence",
                json={"provider": "fake", "verdict_status": "VERIFIED"},
            )
        assert response.status_code == 404

    asyncio.run(run())


def test_digest_mismatch_and_untrusted_provider_url_are_rejected() -> None:
    async def run() -> None:
        for provider in (
            StubProvider(valid_digest=False),
            StubProvider(url="https://attacker.example/fake"),
        ):
            ledger = MemoryLedger()
            app = create_app(
                ledger,
                api_key="correct-key",
                providers={"moex": provider},
                code_version="test-code",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"X-API-Key": "correct-key"},
            ) as client:
                response = await client.post("/v1/claims/verify", json=request_payload())
            assert response.status_code == 500
            assert response.json() == {"error": "INTEGRITY_VIOLATION"}
            assert ledger.records == []
            assert ledger.verdicts == []

    asyncio.run(run())
