"""Dashboard authentication and evidence rendering contract tests."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx

from jamp2.api.app import create_app
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord


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
        assert "integrity ≠ truth" in evidence_page.text
        assert json.dumps({"bitcoin": {"usd": 60974}}, indent=2) in evidence_page.text
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
