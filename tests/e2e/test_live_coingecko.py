"""Positive CoinGecko live path; baseline replay is not ADR-0009 conformance."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.live


def _write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n")


def test_live_coingecko_bitcoin_persists_and_replays(
    db_engine, http_client, artifact_dir
) -> None:
    """Real HTTP 200 -> immutable Evidence/Verdict -> SELECT -> baseline Replay."""
    claim_payload = {
        "text": "Bitcoin spot price is 95000 USD",
        "subject": {"kind": "crypto_asset", "identifier": "bitcoin"},
        "predicate": "price",
        "expected_value": 95000.0,
        "unit": "USD",
    }
    artifact: dict[str, Any] = {
        "scenario": "coingecko",
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "commit_sha": os.getenv("GITHUB_SHA"),
        "adapter_version_expected": "coingecko-adapter-v1",
        "request": claim_payload,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "scope_note": (
            "Baseline replay only. This artifact does not claim ADR-0009 Replay v1 "
            "conformance, historical freshness, policy snapshot binding, or "
            "wall-clock independence."
        ),
        "test_status": "RUNNING",
    }
    artifact_path = artifact_dir / "coingecko_live_e2e.json"

    async def run() -> None:
        idempotency_key = f"live-coingecko-{uuid.uuid4()}"
        response = http_client.post(
            "/v1/claims/verify",
            json=claim_payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        artifact["api_http_status"] = response.status_code
        artifact["api_response"] = response.json()
        assert response.status_code == 201, (
            f"Expected API 201 after provider call; got {response.status_code}: {response.text}"
        )
        result = response.json()
        verdict_status = result.get("verdict")
        artifact["verdict_status"] = verdict_status
        assert verdict_status in {"VERIFIED", "CONTRADICTED"}, (
            f"CoinGecko did not produce a conclusive live verdict: {verdict_status}; "
            f"reason_codes={result.get('arbitration', {}).get('reason_codes')}"
        )

        claim_id = result["claim_id"]
        verdict_id = result["verdict_id"]
        evidence_ids = result["evidence_ids"]
        assert evidence_ids, "API returned no persisted evidence identifiers"

        evidence_artifacts: list[dict[str, Any]] = []
        async with db_engine.connect() as connection:
            for evidence_id in evidence_ids:
                query = await connection.execute(
                    text(
                        "SELECT evidence_id, claim_id, provider, url, http_status, "
                        "raw_body_bytes, raw_sha256, captured_at_utc, fetched_at, "
                        "provider_observed_at, body_complete, provider_adapter_version, "
                        "parsed_value, parent_hash, record_hash "
                        "FROM evidence WHERE evidence_id = :evidence_id"
                    ),
                    {"evidence_id": evidence_id},
                )
                row = query.mappings().first()
                assert row is not None, f"Evidence {evidence_id} absent from PostgreSQL"
                raw_body = bytes(row["raw_body_bytes"])
                recomputed_sha = hashlib.sha256(raw_body).hexdigest()
                assert recomputed_sha == row["raw_sha256"], (
                    f"Independent raw SHA-256 mismatch for {evidence_id}"
                )
                assert row["claim_id"] == claim_id
                assert row["provider"] == "coingecko"
                assert row["http_status"] == 200, "Provider HTTP response was not 200"
                assert row["body_complete"] is True
                assert row["provider_observed_at"] is not None, (
                    "CoinGecko upstream last_updated_at was not captured"
                )
                assert row["fetched_at"] is not None, "Server fetched_at was not captured"
                assert row["provider_adapter_version"] == "coingecko-adapter-v1"

                parsed = row["parsed_value"]
                provider_result = (
                    parsed.get("provider_result", {}) if isinstance(parsed, dict) else {}
                )
                evidence_artifacts.append(
                    {
                        "evidence_id": evidence_id,
                        "claim_id": row["claim_id"],
                        "provider": row["provider"],
                        "request_url": row["url"],
                        "http_status": row["http_status"],
                        "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
                        "raw_body_size_bytes": len(raw_body),
                        "raw_body_sha256": row["raw_sha256"],
                        "fetched_at": row["fetched_at"].isoformat(),
                        "provider_observed_at": row["provider_observed_at"].isoformat(),
                        "captured_at_utc": row["captured_at_utc"].isoformat(),
                        "body_complete": row["body_complete"],
                        "provider_adapter_version": row["provider_adapter_version"],
                        "price_usd": (
                            provider_result.get("observed_value")
                            if isinstance(provider_result, dict)
                            else None
                        ),
                        "record_parent_hash": row["parent_hash"],
                        "record_hash": row["record_hash"],
                    }
                )
        artifact["evidence"] = evidence_artifacts
        assert evidence_artifacts, "No PostgreSQL evidence rows were captured"

        async with db_engine.connect() as connection:
            query = await connection.execute(
                text(
                    "SELECT verdict_id, claim_id, status, evidence_ids, policy_id, "
                    "policy_version, code_version, parent_hash, record_hash "
                    "FROM verdicts WHERE verdict_id = :verdict_id"
                ),
                {"verdict_id": verdict_id},
            )
            verdict = query.mappings().first()
            assert verdict is not None, "Verdict absent from PostgreSQL"
            assert verdict["claim_id"] == claim_id
            assert verdict["status"] == verdict_status
            assert list(verdict["evidence_ids"]) == evidence_ids
            artifact["verdict"] = {
                "verdict_id": verdict["verdict_id"],
                "claim_id": verdict["claim_id"],
                "status": verdict["status"],
                "evidence_ids": list(verdict["evidence_ids"]),
                "policy_id": verdict["policy_id"],
                "policy_version": verdict["policy_version"],
                "code_version": verdict["code_version"],
                "parent_hash": verdict["parent_hash"],
                "record_hash": verdict["record_hash"],
            }

        # Public reads verify the complete evidence and verdict hash chains.
        for evidence_id in evidence_ids:
            read_evidence = http_client.get(f"/v1/evidence/{evidence_id}")
            assert read_evidence.status_code == 200, (
                f"Evidence API chain-verifying read failed: {read_evidence.text}"
            )
        read_verdict = http_client.get(f"/v1/verdicts/{verdict_id}")
        assert read_verdict.status_code == 200, (
            f"Verdict API chain-verifying read failed: {read_verdict.text}"
        )
        read_payload = read_verdict.json()
        assert read_payload["claim_id"] == claim_id
        assert read_payload["evidence_ids"] == evidence_ids
        artifact["chain_integrity"] = {
            "evidence_api_reads": "VERIFIED",
            "verdict_api_read": "VERIFIED",
            "basis": "PostgresLedger read paths validate full append-only hash chains",
        }

        replay = http_client.post(f"/v1/verdicts/{verdict_id}/replay")
        artifact["baseline_replay"] = (
            replay.json() if replay.headers.get("content-type", "").startswith("application/json")
            else {"body": replay.text}
        )
        assert replay.status_code == 200, f"Baseline replay API failed: {replay.text}"
        replay_result = replay.json()
        assert replay_result.get("replay_status") == verdict_status, (
            f"Baseline replay mismatch: {replay_result.get('replay_status')} != {verdict_status}"
        )
        assert replay_result.get("matches_original") is True
        assert replay_result.get("network_calls") == 0

    try:
        asyncio.run(run())
        artifact["test_status"] = "PASS"
    except Exception as exc:
        artifact["test_status"] = "FAIL"
        artifact["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write_artifact(artifact_path, artifact)
