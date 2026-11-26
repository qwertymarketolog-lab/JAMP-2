"""HTTP contract tests for authenticated server-side Claim verification."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import UTC, datetime

import httpx
import pytest
from ledger_fencing import append_evidence_owned
from sqlalchemy import text

from jamp2.api.app import create_app
from jamp2.domain.claim import NormalizedClaim
from jamp2.evidence.digest import compute_sha256
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.multisource import MultiSourcePolicy, SourceSpec
from jamp2.evidence.policy import SourcePolicyRecord, SourceProvenance
from jamp2.ledger.postgres import LedgerUnavailable, PostgresLedger
from jamp2.providers.base import ProviderResult

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is required for API integration tests",
)


class StubProvider:
    """Deterministic adapter fixture; HTTP transport is tested separately."""

    def __init__(self) -> None:
        self.calls = 0

    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        self.calls += 1
        assert claim.predicate == "price"
        assert claim.subject_identifier == "SBER"
        raw = b'{"marketdata":{"columns":["SECID","LAST"],"data":[["SBER",300]]}}'
        catalog_raw = b'{"securities":{"columns":["secid"],"data":[["SBER"]]}}'
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
                    "provider_observed_at": datetime.now(UTC).isoformat(),
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "sha256_body": compute_sha256(raw),
                },
                {
                    "source": "moex.iss",
                    "url": "https://iss.moex.com/iss/securities.json",
                    "status_code": 200,
                    "body_complete": True,
                    "provider_observed_at": datetime.now(UTC).isoformat(),
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "raw_body_base64": base64.b64encode(catalog_raw).decode("ascii"),
                    "sha256_body": compute_sha256(catalog_raw),
                },
            ],
        )


def claim_payload() -> dict[str, object]:
    return {
        "text": "SBER share price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300,
        "unit": "RUB",
    }


async def reset_ledger(ledger: PostgresLedger) -> None:
    async with ledger.engine.begin() as connection:
        # Test cleanup bypasses user triggers; production paths never disable them.
        await connection.execute(text("ALTER TABLE evidence DISABLE TRIGGER USER"))
        await connection.execute(text("ALTER TABLE verdicts DISABLE TRIGGER USER"))
        try:
            await connection.execute(text("TRUNCATE TABLE idempotency_records"))
            await connection.execute(text("TRUNCATE TABLE verdicts, evidence RESTART IDENTITY"))
        finally:
            await connection.execute(text("ALTER TABLE evidence ENABLE TRIGGER USER"))
            await connection.execute(text("ALTER TABLE verdicts ENABLE TRIGGER USER"))


def test_api_server_verifies_claim_persists_raw_evidence_and_reads_it() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        provider = StubProvider()
        application = create_app(
            ledger,
            api_key="integration-test-key",
            providers={"moex": provider},
            code_version="integration-test-sha",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={
                "X-API-Key": "integration-test-key",
                "Idempotency-Key": "integration-claim-key-0001",
            },
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            body = created.json()
            assert body["verdict"] == "VERIFIED"
            assert body["provider"] == "moex.iss"
            assert body["evidence_count"] == 2
            evidence_id = body["evidence_ids"][0]
            verdict_response = await client.get(f"/v1/verdicts/{body['verdict_id']}")
            assert verdict_response.status_code == 200
            verdict_record = verdict_response.json()
            assert verdict_record["evidence_ids"] == body["evidence_ids"]
            assert verdict_record["policy_version"] == "provider-defaults-v1"
            assert verdict_record["code_version"] == "integration-test-sha"
            assert verdict_record["canonicalization_version"] == "jamp-json-nfc-v1"
            assert verdict_record["policy_snapshot"]["schema_version"] == "jamp2-policy-snapshot-v1"
            assert len(verdict_record["policy_snapshot_sha256"]) == 64
            verdict_id = body["verdict_id"]
            replay = await client.post(f"/v1/verdicts/{verdict_id}/replay")
            assert replay.status_code == 200
            assert replay.json()["replay_status"] == "VERIFIED"
            assert replay.json()["matches_original"] is True
            assert replay.json()["network_calls"] == 0
            assert provider.calls == 1
            duplicate_request = await client.post(
                "/v1/claims/verify",
                json=claim_payload(),
            )
            assert duplicate_request.status_code == 201
            assert duplicate_request.json() == body
            assert provider.calls == 1

            changed_claim = claim_payload()
            changed_claim["expected_value"] = 301
            reused_key = await client.post("/v1/claims/verify", json=changed_claim)
            assert reused_key.status_code == 409
            assert reused_key.json() == {"error": "IDEMPOTENCY_KEY_REUSED"}
            assert provider.calls == 1

            fetched = await client.get(f"/v1/evidence/{evidence_id}")
            assert fetched.status_code == 200
            evidence = fetched.json()
            raw = base64.b64decode(evidence["raw_body_base64"], validate=True)
            assert b'"LAST"' in raw
            assert evidence["raw_sha256"] == compute_sha256(raw)
            assert evidence["canonicalization_version"] == "jamp-json-nfc-v1"
            assert evidence["source_timestamp"] is None
            assert evidence["body_complete"] is True
            assert evidence["provider_adapter_version"] == "moex-iss-adapter-v1"
            assert evidence["parser_version"] == "jamp-provider-json-v1"
            assert evidence["captured_at_utc"].endswith("+00:00")

            missing = await client.get("/v1/evidence/not-found")
            assert missing.status_code == 404
            assert missing.json() == {"error": "EVIDENCE_NOT_FOUND"}
        await ledger.close()

    asyncio.run(run())


def test_api_returns_503_when_ledger_is_unavailable() -> None:
    class UnavailableLedger:
        async def append(
            self, record: EvidenceRecord, *, key_hash: str, owner_token: str
        ) -> EvidenceRecord:
            raise LedgerUnavailable("database offline")

        async def get(self, evidence_id: str) -> None:
            raise LedgerUnavailable("database offline")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(UnavailableLedger(), api_key="integration-test-key")
            ),
            base_url="http://test",
            headers={"X-API-Key": "integration-test-key"},
        ) as client:
            response = await client.get("/v1/evidence/unavailable")
            assert response.status_code == 503
            assert response.json() == {"error": "LEDGER_UNAVAILABLE"}

    asyncio.run(run())


def test_api_returns_500_on_integrity_violation() -> None:
    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        raw = b'{"price":100}'
        await append_evidence_owned(
            ledger,
            EvidenceRecord(
                evidence_id="api-tamper",
                claim_id="api-claim",
                provider="moex.iss",
                url="https://iss.moex.com/iss/price",
                http_status=200,
                raw_body_bytes=raw,
                raw_sha256=compute_sha256(raw),
                captured_at_utc=datetime(2026, 11, 20, tzinfo=UTC),
                parsed_value={"price": 100},
                verdict_status="E4_VERIFIED",
                parent_hash=None,
            )
        )
        async with ledger.engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(
                text("UPDATE evidence SET raw_body_bytes = :body WHERE evidence_id = :id"),
                {"body": b'{"price":101}', "id": "api-tamper"},
            )
        application = create_app(
            ledger, api_key="integration-test-key", code_version="integration-test-sha"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={"X-API-Key": "integration-test-key"},
        ) as client:
            response = await client.get("/v1/evidence/api-tamper")
            assert response.status_code == 500
            assert response.json() == {"error": "INTEGRITY_VIOLATION"}
        await ledger.close()

    asyncio.run(run())


def test_partial_multi_source_timeout_is_ambiguous_in_postgres_ledger() -> None:
    class FixedProvider:
        def __init__(self, source: str, host: str, *, timeout: bool = False) -> None:
            self.source = source
            self.host = host
            self.timeout = timeout

        def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
            if self.timeout:
                time.sleep(0.05)
                raise TimeoutError("deterministic integration timeout")
            raw = b'{"price":300}'
            return ProviderResult(
                "E4_VERIFIED",
                {"observed_last": 300.0},
                [
                    {
                        "source": self.source,
                        "url": f"https://{self.host}/price",
                        "status_code": 200,
                        "body_complete": True,
                        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                        "sha256_body": compute_sha256(raw),
                    }
                ],
            )

    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        policy = MultiSourcePolicy(
            "jamp2.integration-two-source",
            "integration-exact-v1",
            (
                SourceSpec("source_a", "source-a", "source-a.example", "test-adapter-v1"),
                SourceSpec("source_b", "source-b", "source-b.example", "test-adapter-v1"),
            ),
            timeout_seconds=0.01,
        )
        application = create_app(
            ledger,
            api_key="integration-test-key",
            providers={
                "source_a": FixedProvider("source-a", "source-a.example"),
                "source_b": FixedProvider("source-b", "source-b.example", timeout=True),
            },
            code_version="integration-test-sha",
            policies={("equity_security", "RUB"): policy},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers={
                "X-API-Key": "integration-test-key",
                "Idempotency-Key": "postgres-multisource-timeout-01",
            },
        ) as client:
            created = await client.post("/v1/claims/verify", json=claim_payload())
            assert created.status_code == 201
            body = created.json()
            assert body["verdict"] == "AMBIGUOUS"
            assert body["evidence_count"] == 2

            evidence_responses = [
                await client.get(f"/v1/evidence/{evidence_id}")
                for evidence_id in body["evidence_ids"]
            ]
            assert all(response.status_code == 200 for response in evidence_responses)
            failed = next(
                response.json()
                for response in evidence_responses
                if response.json()["verdict_status"] == "SOURCE_FAILURE"
            )
            assert failed["provider"] == "source-b"
            assert failed["http_status"] is None
            assert failed["body_complete"] is False
            assert base64.b64decode(failed["raw_body_base64"], validate=True) == b""

            verdict_response = await client.get(f"/v1/verdicts/{body['verdict_id']}")
            assert verdict_response.status_code == 200
            assert verdict_response.json()["status"] == "AMBIGUOUS"
            assert verdict_response.json()["policy_version"] == "integration-exact-v1"
        await ledger.close()

    asyncio.run(run())


def test_api_multi_source_conflict_is_persisted_and_replayed_from_postgres() -> None:
    class PriceProvider:
        def __init__(self, provider_id: str, host: str, price: float) -> None:
            self.provider_id = provider_id
            self.host = host
            self.price = price
            self.calls = 0

        def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
            self.calls += 1
            observed_at = datetime.now(UTC).isoformat()
            raw = (
                f'{{"provider":"{self.provider_id}","price":{self.price:.2f},"currency":"USD"}}'
            ).encode()
            return ProviderResult(
                "E4_VERIFIED",
                {
                    "observed_last": self.price,
                    "source_identifiers": [claim.subject_identifier],
                    "quote_currency": claim.unit,
                    "price_type": "SPOT",
                },
                [
                    {
                        "source": self.provider_id,
                        "fixture_provenance": "SYNTHETIC",
                        "url": f"https://{self.host}/price",
                        "status_code": 200,
                        "body_complete": True,
                        "provider_observed_at": observed_at,
                        "source_date": "2026-11-25",
                        "fetched_at": observed_at,
                        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                        "sha256_body": compute_sha256(raw),
                    }
                ],
            )

    async def run() -> None:
        ledger = PostgresLedger(DATABASE_URL)
        await reset_ledger(ledger)
        source_a = PriceProvider("source-a", "source-a.example", 100.0)
        source_b = PriceProvider("source-b", "source-b.example", 105.0)
        source_policy = SourcePolicyRecord(
            policy_id="jamp2.provider-price-comparison",
            version=1,
            asset_class="equity_security",
            required_identifiers=("SBER",),
            market_calendar_required=False,
            quote_currency="USD",
            price_type="SPOT",
            required_sources=("source-a", "source-b"),
            independence_group="integration-independent-sources",
            max_age_seconds=120,
            max_time_skew_seconds=5,
            verification_mode="MULTI_SOURCE",
            provenance=(
                SourceProvenance(
                    provider_id="source-a",
                    upstream_entity="upstream-a",
                    aggregation_methodology="direct-spot-observation",
                    transport_failure_domain="transport-a",
                    calculation_failure_domain="calculation-a",
                    independence_status="CONFIRMED",
                ),
                SourceProvenance(
                    provider_id="source-b",
                    upstream_entity="upstream-b",
                    aggregation_methodology="direct-spot-observation",
                    transport_failure_domain="transport-b",
                    calculation_failure_domain="calculation-b",
                    independence_status="CONFIRMED",
                ),
            ),
        )
        policy = MultiSourcePolicy(
            "jamp2.provider-price-comparison",
            "provider-defaults-v1",
            (
                SourceSpec("source_a", "source-a", "source-a.example", "test-adapter-v1"),
                SourceSpec("source_b", "source-b", "source-b.example", "test-adapter-v1"),
            ),
            source_policy=source_policy,
        )
        application = create_app(
            ledger,
            api_key="integration-test-key",
            providers={"source_a": source_a, "source_b": source_b},
            code_version="integration-test-sha",
            policies={("equity_security", "USD"): policy},
        )
        payload = claim_payload()
        payload["unit"] = "USD"
        payload["text"] = "SBER spot price is disputed at 100.00 or 105.00 USD"
        headers = {
            "X-API-Key": "integration-test-key",
            "Idempotency-Key": "postgres-multisource-conflict-001",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers=headers,
        ) as client:
            created = await client.post("/v1/claims/verify", json=payload)
            assert created.status_code == 201
            body = created.json()
            assert body["verdict"] == "AMBIGUOUS"
            assert body["arbitration"]["reason_codes"].count("SOURCE_VALUES_DIVERGE") == 1
            assert body["arbitration"]["verification_mode"] == "MULTI_SOURCE"
            assert body["arbitration"]["independent_cross_check"] is True
            assert body["arbitration"]["reducer"]["status"] == "AMBIGUOUS"
            assert body["evidence_count"] == 2
            assert set(body["provider"]) == {"source-a", "source-b"}
            assert source_a.calls == 1
            assert source_b.calls == 1

            evidence_responses = [
                await client.get(f"/v1/evidence/{evidence_id}")
                for evidence_id in body["evidence_ids"]
            ]
            assert all(response.status_code == 200 for response in evidence_responses)
            evidence_rows = [response.json() for response in evidence_responses]
            assert {row["provider"] for row in evidence_rows} == {"source-a", "source-b"}
            for row in evidence_rows:
                raw = base64.b64decode(row["raw_body_base64"], validate=True)
                assert row["raw_sha256"] == compute_sha256(raw)
                assert row["body_complete"] is True
                assert row["parsed_value"]["provider_result"]["quote_currency"] == "USD"
                assert row["parsed_value"]["fixture_provenance"] == "SYNTHETIC"
            observed_values = {
                row["parsed_value"]["provider_result"]["observed_last"]
                for row in evidence_rows
            }
            assert observed_values == {100.0, 105.0}
            replay_contexts = [row["parsed_value"]["replay_context"] for row in evidence_rows]
            assert all(
                context["schema_version"] == "jamp2-policy-gate-replay-v1"
                for context in replay_contexts
            )
            assert replay_contexts[0] == replay_contexts[1]
            assert replay_contexts[0]["policy"]["source_policy"]["provenance"]
            assert all(row["record_hash"] for row in evidence_rows)

            verdict_response = await client.get(f"/v1/verdicts/{body['verdict_id']}")
            assert verdict_response.status_code == 200
            verdict_row = verdict_response.json()
            assert verdict_row["status"] == "AMBIGUOUS"
            assert verdict_row["verification_mode"] == "MULTI_SOURCE"
            assert "SOURCE_VALUES_DIVERGE" in verdict_row["reason_codes"]
            assert set(verdict_row["evidence_ids"]) == set(body["evidence_ids"])

            replay = await client.post(f"/v1/verdicts/{body['verdict_id']}/replay")
            assert replay.status_code == 200
            replay_body = replay.json()
            assert replay_body["replay_status"] == "AMBIGUOUS"
            assert replay_body["matches_original"] is True
            assert replay_body["reason_codes_match"] is True
            assert replay_body["reason_code"] == "POLICY_GATE_AND_REDUCER_REPLAYED"
            assert replay_body["replay_scope"] == "POLICY_GATE_AND_REDUCER"
            assert replay_body["policy_gate_revalidated"] is True
            assert replay_body["reducer"]["status"] == "AMBIGUOUS"
            assert replay_body["reducer"]["usable_observation_count"] == 2
            assert {
                item["provider_id"]: item["observation_status"]
                for item in replay_body["reducer"]["policy_gate_observations"]
            } == {"source-a": "VALID", "source-b": "VALID"}
            assert replay_body["network_calls"] == 0
            assert source_a.calls == 1
            assert source_b.calls == 1

            duplicate = await client.post("/v1/claims/verify", json=payload)
            assert duplicate.status_code == 201
            assert duplicate.json() == body
            assert source_a.calls == 1
            assert source_b.calls == 1

            changed_payload = dict(payload)
            changed_payload["expected_value"] = 301
            reused_key = await client.post("/v1/claims/verify", json=changed_payload)
            assert reused_key.status_code == 409
            assert reused_key.json() == {"error": "IDEMPOTENCY_KEY_REUSED"}
            assert source_a.calls == 1
            assert source_b.calls == 1
        await ledger.close()

    asyncio.run(run())
