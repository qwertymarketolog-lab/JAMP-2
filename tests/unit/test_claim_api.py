"""Fail-closed authentication and server-owned evidence acquisition tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jamp2.api.app import create_app
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.digest import compute_record_hash
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.multisource import MultiSourcePolicy, SourceSpec, arbitrate
from jamp2.evidence.policy import SourcePolicyRecord, SourceProvenance
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
        self.idempotency: dict[str, dict[str, Any]] = {}

    async def append(self, record: EvidenceRecord) -> EvidenceRecord:
        persisted = EvidenceRecord(
            **{
                field: getattr(record, field)
                for field in record.__dataclass_fields__
                if field != "record_hash"
            },
            record_hash=compute_record_hash(record),
        )
        self.records.append(persisted)
        return persisted

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

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        return [record for record in self.records if record.claim_id == claim_id]

    async def begin_idempotency(
        self, *, key_hash: str, request_digest: str, claim_id: str
    ) -> tuple[str, dict[str, Any] | None]:
        existing = self.idempotency.get(key_hash)
        if existing is None:
            self.idempotency[key_hash] = {
                "request_digest": request_digest,
                "claim_id": claim_id,
                "status": "PENDING",
                "response_payload": None,
            }
            return "NEW", None
        if existing["request_digest"] != request_digest:
            return "KEY_REUSED", None
        if existing["status"] == "COMPLETED":
            return "COMPLETED", existing["response_payload"]
        if existing["status"] == "PENDING":
            return "IN_PROGRESS", None
        return "FAILED", None

    async def complete_idempotency(
        self, *, key_hash: str, request_digest: str, verdict_id: str,
        response_payload: dict[str, Any]
    ) -> None:
        row = self.idempotency[key_hash]
        assert row["request_digest"] == request_digest
        row["status"] = "COMPLETED"
        row["verdict_id"] = verdict_id
        row["response_payload"] = response_payload

    async def fail_idempotency(self, *, key_hash: str, request_digest: str) -> None:
        row = self.idempotency[key_hash]
        assert row["request_digest"] == request_digest
        row["status"] = "FAILED"



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
        observed_at = datetime.now(UTC)
        return ProviderResult(
            "E4_VERIFIED",
            {
                "observed_last": 300.0,
                "source_identifiers": [claim.subject_identifier],
                "quote_currency": claim.unit,
                "price_type": "LAST_DEAL",
                "market_calendar_valid": True,
            },
            [
                {
                    "source": "moex.iss",
                    "url": self.url,
                    "status_code": 200,
                    "body_complete": True,
                    "provider_observed_at": observed_at.isoformat(),
                    "fetched_at": (observed_at + timedelta(milliseconds=10)).isoformat(),
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
                headers={"X-API-Key": "correct-key", "Idempotency-Key": "unit-test-key-001"},
            ) as client:
                response = await client.post("/v1/claims/verify", json=request_payload())
            assert response.status_code == 500
            assert response.json() == {"error": "INTEGRITY_VIOLATION"}
            assert ledger.records == []
            assert ledger.verdicts == []

    asyncio.run(run())


def test_missing_idempotency_key_is_rejected_before_provider_fetch() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        provider = StubProvider()
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
        assert response.status_code == 400
        assert response.json() == {"error": "IDEMPOTENCY_KEY_REQUIRED"}
        assert provider.calls == 0
        assert ledger.records == []
        assert ledger.idempotency == {}

    asyncio.run(run())


class DeterministicSourceProvider:
    """Test-only provider with explicit value, timeout, and exception scenarios."""

    def __init__(
        self,
        source_id: str,
        host: str,
        *,
        value: int | None = 300,
        failure: str | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.source_id = source_id
        self.host = host
        self.value = value
        self.failure = failure
        self.delay_seconds = delay_seconds
        self.calls = 0

    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.failure == "timeout":
            raise TimeoutError("deterministic provider timeout")
        if self.failure == "error":
            raise RuntimeError("deterministic provider error")
        raw = f'{{"price":{self.value}}}'.encode()
        observed_at = datetime.now(UTC)
        return ProviderResult(
            "E4_VERIFIED",
            {
                "observed_last": self.value,
                "source_identifiers": [claim.subject_identifier],
                "quote_currency": claim.unit,
                "price_type": "LAST_DEAL",
                "market_calendar_valid": True,
            },
            [
                {
                    "source": self.source_id,
                    "url": f"https://{self.host}/price",
                    "status_code": 200,
                    "body_complete": True,
                    "provider_observed_at": observed_at.isoformat(),
                    "fetched_at": (observed_at + timedelta(milliseconds=10)).isoformat(),
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "sha256_body": hashlib.sha256(raw).hexdigest(),
                }
            ],
        )


def _two_source_policy(timeout_seconds: float = 0.2) -> MultiSourcePolicy:
    required_sources = (
        SourceSpec("source_a", "source-a", "source-a.example", "test-adapter-v1"),
        SourceSpec("source_b", "source-b", "source-b.example", "test-adapter-v1"),
    )
    source_policy = SourcePolicyRecord(
        policy_id="jamp2.test-two-source",
        version=1,
        asset_class="equity_security",
        required_identifiers=("SBER",),
        market_calendar_required=True,
        quote_currency="RUB",
        price_type="LAST_DEAL",
        required_sources=("source-a", "source-b"),
        independence_group="test-independent-exchanges",
        max_age_seconds=60,
        max_time_skew_seconds=5,
        timeout_seconds=timeout_seconds,
        verification_mode="MULTI_SOURCE",
        provenance=(
            SourceProvenance(
                "source-a", "exchange-a", "direct-last", "network-a", "calc-a", "CONFIRMED"
            ),
            SourceProvenance(
                "source-b", "exchange-b", "direct-last", "network-b", "calc-b", "CONFIRMED"
            ),
        ),
    )
    return MultiSourcePolicy(
        policy_id="jamp2.test-two-source",
        version="test-exact-v1",
        required_sources=required_sources,
        timeout_seconds=timeout_seconds,
        source_policy=source_policy,
    )


def test_multi_source_agreement_creates_verified_verdict_and_two_ledger_records() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        source_a = DeterministicSourceProvider("source-a", "source-a.example", value=300)
        source_b = DeterministicSourceProvider("source-b", "source-b.example", value=300)
        app = create_app(
            ledger,
            api_key="correct-key",
            providers={"source_a": source_a, "source_b": source_b},
            code_version="test-code",
            policies={("equity_security", "RUB"): _two_source_policy()},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "correct-key",
                "Idempotency-Key": "multi-source-agreement",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=request_payload())
        assert response.status_code == 201
        assert response.json()["verdict"] == "VERIFIED"
        assert response.json()["evidence_count"] == 2
        assert len(ledger.records) == 2
        assert len(ledger.verdicts) == 1
        assert ledger.verdicts[0].policy_version == "test-exact-v1"
        assert source_a.calls == source_b.calls == 1

    asyncio.run(run())


def test_multi_source_divergence_is_ambiguous_and_preserves_both_observations() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        providers = {
            "source_a": DeterministicSourceProvider("source-a", "source-a.example", value=300),
            "source_b": DeterministicSourceProvider("source-b", "source-b.example", value=302),
        }
        app = create_app(
            ledger,
            api_key="correct-key",
            providers=providers,
            code_version="test-code",
            policies={("equity_security", "RUB"): _two_source_policy()},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "correct-key",
                "Idempotency-Key": "multi-source-divergence",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=request_payload())
        assert response.status_code == 201
        assert response.json()["verdict"] == "AMBIGUOUS"
        assert len(ledger.records) == 2
        assert ledger.verdicts[0].reason_codes == ("SOURCE_VALUES_DIVERGE",)

    asyncio.run(run())


def test_multi_source_partial_timeout_is_ambiguous_and_timeout_is_ledgered() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        providers = {
            "source_a": DeterministicSourceProvider("source-a", "source-a.example", value=300),
            "source_b": DeterministicSourceProvider(
                "source-b", "source-b.example", failure="timeout", delay_seconds=0.05
            ),
        }
        app = create_app(
            ledger,
            api_key="correct-key",
            providers=providers,
            code_version="test-code",
            policies={("equity_security", "RUB"): _two_source_policy(0.01)},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "correct-key",
                "Idempotency-Key": "multi-source-partial-timeout",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=request_payload())
        assert response.status_code == 201
        assert response.json()["verdict"] == "AMBIGUOUS"
        assert len(ledger.records) == 2
        failure = next(
            record for record in ledger.records if record.verdict_status == "SOURCE_FAILURE"
        )
        assert failure.provider == "source-b"
        assert failure.http_status is None
        assert failure.body_complete is False
        assert isinstance(failure.parsed_value, dict)
        assert failure.parsed_value["source_status"] == "TIMEOUT"
        assert "ERR_QUORUM_NOT_MET" in ledger.verdicts[0].reason_codes

    asyncio.run(run())


def test_multi_source_full_failure_is_inconclusive_and_both_failures_are_ledgered() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        providers = {
            "source_a": DeterministicSourceProvider(
                "source-a", "source-a.example", failure="error"
            ),
            "source_b": DeterministicSourceProvider(
                "source-b", "source-b.example", failure="error"
            ),
        }
        app = create_app(
            ledger,
            api_key="correct-key",
            providers=providers,
            code_version="test-code",
            policies={("equity_security", "RUB"): _two_source_policy()},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-API-Key": "correct-key",
                "Idempotency-Key": "multi-source-full-failure",
            },
        ) as client:
            response = await client.post("/v1/claims/verify", json=request_payload())
        assert response.status_code == 201
        assert response.json()["verdict"] == "INCONCLUSIVE"
        assert len(ledger.records) == 2
        assert all(record.verdict_status == "SOURCE_FAILURE" for record in ledger.records)
        assert ledger.verdicts[0].reason_codes[0] == "ALL_REQUIRED_SOURCES_UNAVAILABLE"

    asyncio.run(run())


def test_missing_provider_observation_time_blocks_conflict_reducer(monkeypatch: Any) -> None:
    class TimestampMissingProvider:
        def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
            now = datetime.now(UTC)
            raw = b'{"price":300}'
            return ProviderResult(
                "E4_VERIFIED",
                {
                    "observed_last": 300.0,
                    "source_identifiers": [claim.subject_identifier],
                    "quote_currency": claim.unit,
                    "price_type": "LAST_DEAL",
                },
                [
                    {
                        "source": "moex.iss",
                        "url": "https://iss.moex.com/iss/price",
                        "status_code": 200,
                        "body_complete": True,
                        "provider_observed_at": None,
                        "fetched_at": now.isoformat(),
                        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                        "sha256_body": hashlib.sha256(raw).hexdigest(),
                    }
                ],
            )

    def reducer_must_not_run(_observations: object) -> dict[str, object]:
        pytest.fail("conflict reducer must not receive an observation rejected by policy")

    monkeypatch.setattr("jamp2.evidence.multisource.detect_conflicts", reducer_must_not_run)
    claim = NormalizedClaim(
        "SBER share price is 300 RUB",
        "equity_security",
        "SBER",
        "price",
        300,
        "RUB",
    )
    policy = MultiSourcePolicy(
        "jamp2.single-source-missing-time",
        "single-source-test-v1",
        (SourceSpec("moex", "moex.iss", "iss.moex.com", "test-adapter-v1"),),
    )
    result = asyncio.run(arbitrate(claim, policy, {"moex": TimestampMissingProvider()}))
    assert result.verdict_status == "AMBIGUOUS"
    assert result.reducer["status"] == "POLICY_GATE_BLOCKED"
    assert result.outcomes[0].details["observation_status"] == "INCOMPLETE"
    assert "ERR_QUORUM_NOT_MET" in result.reason_codes
