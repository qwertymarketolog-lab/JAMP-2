"""Dashboard authentication and evidence rendering contract tests."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from typing import Any

import httpx

from jamp2.api.app import create_app
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.providers.base import ProviderResult
from jamp2.web.routes import raw_body_text


class DashboardLedger:
    def __init__(self, evidence: EvidenceRecord, verdict: VerdictRecord) -> None:
        self.evidence = evidence
        self.verdict = verdict

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self.evidence if evidence_id == self.evidence.evidence_id else None

    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None:
        return self.verdict if verdict_id == self.verdict.verdict_id else None

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        return [self.evidence] if claim_id == self.evidence.claim_id else []


def seeded_records() -> tuple[EvidenceRecord, VerdictRecord]:
    raw = b'{"bitcoin":{"usd":60974}}'
    evidence = EvidenceRecord(
        evidence_id="dashboard-evidence-1", claim_id="dashboard-claim-1",
        provider="coingecko", url="https://api.coingecko.com/api/v3/simple/price",
        http_status=200, raw_body_bytes=raw, raw_sha256=hashlib.sha256(raw).hexdigest(),
        captured_at_utc=datetime(2026, 11, 26, tzinfo=UTC),
        parsed_value={"claim": {"text": "Bitcoin price is 95000 USD",
            "subject_kind": "crypto_asset", "subject_identifier": "bitcoin",
            "expected_value": 95000, "unit": "USD"}},
        verdict_status="SOURCE_OBSERVATION_CAPTURED", parent_hash=None,
        body_complete=True, provider_adapter_version="coingecko-adapter-v1",
        parser_version="jamp-provider-json-v1",
    )
    verdict = VerdictRecord(
        verdict_id="dashboard-verdict-1", claim_id=evidence.claim_id,
        status="CONTRADICTED", evidence_ids=(evidence.evidence_id,),
        policy_id="jamp2.provider-price-comparison", policy_version="provider-defaults-v1",
        code_version="test-code-version", canonicalization_version="jamp-json-nfc-v1",
        decided_at_utc=datetime(2026, 11, 26, tzinfo=UTC),
        reason_codes=("COINGECKO_PRICE_CONTRADICTED",),
        explanation="Fixture verdict for Dashboard rendering tests.",
    )
    return evidence, verdict


def _evidence_with_raw(raw: bytes, *, evidence_id: str = "raw-test-evidence") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id, claim_id="raw-test-claim",
        provider="test", url="https://test.example/e",
        http_status=200, raw_body_bytes=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        captured_at_utc=datetime(2026, 11, 26, tzinfo=UTC),
        parsed_value={}, verdict_status="SOURCE_OBSERVATION_CAPTURED",
        parent_hash=None, body_complete=True,
        provider_adapter_version="test-v1", parser_version="test-v1",
    )


def test_dashboard_fails_closed_without_password_configuration() -> None:
    async def run() -> None:
        evidence, verdict = seeded_records()
        app = create_app(DashboardLedger(evidence, verdict), api_key="server-secret")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/")
        assert response.status_code == 503
        assert response.json()["detail"] == "DASHBOARD_AUTH_NOT_CONFIGURED"
    asyncio.run(run())


def test_dashboard_renders_home_verdict_evidence_and_static_assets() -> None:
    async def run() -> None:
        evidence, verdict = seeded_records()
        app = create_app(DashboardLedger(evidence, verdict), api_key="server-secret",
                         dashboard_password="dashboard-test-password")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test",
                                     auth=("pilot", "dashboard-test-password")) as client:
            home = await client.get("/")
            verdict_page = await client.get("/verdicts/dashboard-verdict-1")
            evidence_page = await client.get("/evidence/dashboard-evidence-1")
            static = await client.get("/static/style.css")
        assert home.status_code == 200 and "Проверка фактического утверждения" in home.text
        assert "server-secret" not in home.text
        assert verdict_page.status_code == 200
        assert "CONTRADICTED" in verdict_page.text
        assert "COINGECKO_PRICE_CONTRADICTED" in verdict_page.text
        assert evidence_page.status_code == 200
        assert "Byte integrity confirmed" in evidence_page.text
        assert "integrity \u2260 truth" in evidence_page.text
        assert '{"bitcoin":{"usd":60974}}' in unescape(evidence_page.text)
        assert static.status_code == 200 and "status-ambiguous" in static.text
    asyncio.run(run())


def test_dashboard_rejects_invalid_credentials_and_cross_origin_post() -> None:
    async def run() -> None:
        evidence, verdict = seeded_records()
        app = create_app(DashboardLedger(evidence, verdict), api_key="server-secret",
                         dashboard_password="dashboard-test-password")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test",
                                     auth=("pilot", "wrong-password")) as client:
            unauthorized = await client.get("/")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test",
                                     auth=("pilot", "dashboard-test-password")) as client:
            csrf = await client.post("/verdicts/dashboard-verdict-1/replay",
                headers={"Origin": "https://attacker.example",
                         "X-JAMP-Dashboard-Request": "1"})
        assert unauthorized.status_code == 401
        assert csrf.status_code == 403
        assert csrf.json()["detail"] == "DASHBOARD_CSRF_CHECK_FAILED"
    asyncio.run(run())


class ApiMemoryLedger(DashboardLedger):
    """In-memory implementation of the API ledger contract for BFF behavior tests."""

    def __init__(self) -> None:
        evidence, verdict = seeded_records()
        super().__init__(evidence, verdict)
        self.records: list[EvidenceRecord] = []
        self.verdicts: list[VerdictRecord] = []
        self.idempotency: dict[str, dict[str, Any]] = {}

    async def begin_idempotency(
        self, *, key_hash: str, request_digest: str, claim_id: str
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        existing = self.idempotency.get(key_hash)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                return "KEY_REUSED", None, None
            if existing["status"] == "COMPLETED":
                return "COMPLETED", None, existing["response_payload"]
            if existing["status"] == "PENDING":
                return "IN_PROGRESS", None, None
            return "FAILED", None, None
        owner = uuid.uuid4().hex
        self.idempotency[key_hash] = {
            "request_digest": request_digest, "claim_id": claim_id,
            "owner_token": owner, "status": "PENDING",
        }
        return "NEW", owner, None

    async def append(
        self, record: EvidenceRecord, *, key_hash: str, owner_token: str
    ) -> EvidenceRecord:
        reservation = self.idempotency[key_hash]
        assert reservation["owner_token"] == owner_token
        stored = replace(record, record_hash=compute_record_hash(record))
        self.records.append(stored)
        return stored

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        return next((item for item in self.records if item.evidence_id == evidence_id), None)

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        return [item for item in self.records if item.claim_id == claim_id]

    async def append_verdict(
        self, record: VerdictRecord, *, key_hash: str, owner_token: str
    ) -> VerdictRecord:
        assert self.idempotency[key_hash]["owner_token"] == owner_token
        self.verdicts.append(record)
        return record

    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None:
        return next((item for item in self.verdicts if item.verdict_id == verdict_id), None)

    async def complete_idempotency(
        self, *, key_hash: str, request_digest: str, verdict_id: str,
        response_payload: dict[str, Any], owner_token: str
    ) -> None:
        row = self.idempotency[key_hash]
        assert row["request_digest"] == request_digest
        assert row["owner_token"] == owner_token
        row["status"] = "COMPLETED"
        row["response_payload"] = response_payload
        row["verdict_id"] = verdict_id

    async def fail_idempotency(
        self, *, key_hash: str, request_digest: str, owner_token: str
    ) -> None:
        row = self.idempotency[key_hash]
        assert row["request_digest"] == request_digest
        assert row["owner_token"] == owner_token
        row["status"] = "FAILED"


class DashboardStubProvider:
    def __init__(self) -> None:
        self.calls = 0

    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        self.calls += 1
        assert claim.subject_identifier == "SBER"
        raw_market = b'{"marketdata":{"columns":["SECID","LAST"],"data":[["SBER",300]]}}'
        raw_catalog = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
        observed_at = datetime.now(UTC)
        return ProviderResult(
            "E4_VERIFIED",
            {
                "observed_last": 300.0,
                "trade_date": "2026-11-19",
                "source_identifiers": [claim.subject_identifier],
                "quote_currency": claim.unit,
                "price_type": "LAST_DEAL",
                "market_calendar_valid": True,
            },
            [
                {
                    "source": "moex.iss",
                    "url": "https://iss.moex.com/iss/marketdata.json",
                    "status_code": 200,
                    "body_complete": True,
                    "fetched_at": observed_at.isoformat(),
                    "provider_observed_at": observed_at.isoformat(),
                    "raw_body_base64": base64.b64encode(raw_market).decode("ascii"),
                    "sha256_body": compute_sha256(raw_market),
                },
                {
                    "source": "moex.iss",
                    "url": "https://iss.moex.com/iss/securities.json",
                    "status_code": 200,
                    "body_complete": True,
                    "fetched_at": observed_at.isoformat(),
                    "provider_observed_at": observed_at.isoformat(),
                    "raw_body_base64": base64.b64encode(raw_catalog).decode("ascii"),
                    "sha256_body": compute_sha256(raw_catalog),
                },
            ],
        )


def test_dashboard_claim_and_replay_use_server_side_api_key() -> None:
    async def run() -> None:
        ledger = ApiMemoryLedger()
        provider = DashboardStubProvider()
        app = create_app(
            ledger,
            api_key="never-send-this-to-browser",
            dashboard_password="dashboard-test-password",
            providers={"moex": provider},
            code_version="dashboard-test-sha",
        )
        payload = {
            "text": "SBER share price is 300 RUB",
            "subject": {"kind": "equity_security", "identifier": "SBER"},
            "predicate": "price",
            "expected_value": 300,
            "unit": "RUB",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            auth=("pilot", "dashboard-test-password"),
        ) as client:
            created = await client.post(
                "/claims/verify",
                json=payload,
                headers={
                    "Origin": "http://test",
                    "X-JAMP-Dashboard-Request": "1",
                    "Idempotency-Key": "dashboard-claim-retry-001",
                },
            )
            assert created.status_code == 201
            assert "never-send-this-to-browser" not in created.text
            verdict_id = created.json()["redirect_url"].rsplit("/", 1)[1]
            retried = await client.post(
                "/claims/verify",
                json=payload,
                headers={
                    "Origin": "http://test",
                    "X-JAMP-Dashboard-Request": "1",
                    "Idempotency-Key": "dashboard-claim-retry-001",
                },
            )
            assert retried.status_code == 201
            assert retried.json()["redirect_url"] == f"/verdicts/{verdict_id}"
            assert ledger.idempotency and len(ledger.records) == 2
            assert len(ledger.verdicts) == 1
            assert len(ledger.idempotency) == 1
            assert provider.calls == 1
            verdict_page = await client.get(f"/verdicts/{verdict_id}")
            replay = await client.post(
                f"/verdicts/{verdict_id}/replay",
                headers={
                    "Origin": "http://test",
                    "X-JAMP-Dashboard-Request": "1",
                },
            )
        assert verdict_page.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["replay_status"] == "VERIFIED"
        assert replay.json()["matches_original"] is True
        assert replay.json()["network_calls"] == 0
        assert len(ledger.verdicts) == 1

    asyncio.run(run())


def test_raw_body_text_preserves_duplicate_json_keys() -> None:
    """Duplicate JSON keys must be visible exactly as stored, not deduplicated."""
    raw = b'{"price":1,"price":2}'
    evidence = _evidence_with_raw(raw)
    assert raw_body_text(evidence) == '{"price":1,"price":2}'


def test_raw_body_text_preserves_original_formatting_and_order() -> None:
    """Whitespace, key order, and number format must match stored bytes."""
    raw = b'{"b":1,"a":2, "x" : 3.0}'
    evidence = _evidence_with_raw(raw)
    assert raw_body_text(evidence) == '{"b":1,"a":2, "x" : 3.0}'


def test_raw_body_text_returns_base64_for_invalid_utf8() -> None:
    """Non-UTF-8 bytes must be displayed as Base64, not decoded."""
    raw = b'\xff\xfe\x00invalid'
    evidence = _evidence_with_raw(raw)
    result = raw_body_text(evidence)
    assert result.startswith("Base64: ")
    assert base64.b64encode(raw).decode("ascii") in result


def test_raw_body_text_returns_plain_text_for_non_json() -> None:
    """Non-JSON UTF-8 text must be returned as-is."""
    raw = b'plain text, not json'
    evidence = _evidence_with_raw(raw)
    assert raw_body_text(evidence) == 'plain text, not json'


def test_raw_body_text_preserves_unicode_escapes() -> None:
    """JSON unicode escapes must not be decoded; show exact stored bytes."""
    raw = b'{"key":"\\u0041"}'
    evidence = _evidence_with_raw(raw)
    assert raw_body_text(evidence) == '{"key":"\\u0041"}'
