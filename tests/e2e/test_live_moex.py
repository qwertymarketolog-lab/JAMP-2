"""MOEX live capture path; unknown calendar/time contract must remain INCONCLUSIVE."""

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


def test_live_moex_capture_is_inconclusive_without_calendar(
    db_engine, http_client, artifact_dir
) -> None:
    claim_payload = {
        "text": "SBER last price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300.0,
        "unit": "RUB",
    }
    artifact: dict[str, Any] = {
        "scenario": "moex",
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "commit_sha": os.getenv("GITHUB_SHA"),
        "adapter_version_expected": "moex-iss-adapter-v1",
        "request": claim_payload,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "scope_note": (
            "MOEX raw capture is tested. SYSTIME is not accepted as provider_observed_at "
            "without an authoritative endpoint contract; calendar qualification is not asserted."
        ),
        "test_status": "RUNNING",
    }
    artifact_path = artifact_dir / "moex_live_e2e.json"

    async def run() -> None:
        response = http_client.post(
            "/v1/claims/verify",
            json=claim_payload,
            headers={"Idempotency-Key": f"live-moex-{uuid.uuid4()}"},
        )
        artifact["api_http_status"] = response.status_code
        artifact["api_response"] = response.json()
        assert response.status_code == 201, (
            f"Expected API 201 after MOEX request; got {response.status_code}: {response.text}"
        )
        result = response.json()
        assert result.get("verdict") == "INCONCLUSIVE", (
            "MOEX without a confirmed calendar/upstream timestamp must remain INCONCLUSIVE; "
            f"received {result.get('verdict')}"
        )
        reason_codes = result.get("arbitration", {}).get("reason_codes", [])
        assert reason_codes, "INCONCLUSIVE MOEX verdict must expose reason codes"

        claim_id = result["claim_id"]
        verdict_id = result["verdict_id"]
        evidence_ids = result.get("evidence_ids", [])
        assert evidence_ids, "MOEX Evidence must be persisted even for INCONCLUSIVE"

        evidence_artifacts: list[dict[str, Any]] = []
        successful_http_observation = False
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
                assert row is not None, f"MOEX Evidence {evidence_id} absent from PostgreSQL"
                raw_body = bytes(row["raw_body_bytes"])
                recomputed_sha = hashlib.sha256(raw_body).hexdigest()
                assert recomputed_sha == row["raw_sha256"], (
                    f"Independent raw SHA-256 mismatch for {evidence_id}"
                )
                assert row["claim_id"] == claim_id
                assert row["provider"] == "moex.iss"
                assert row["body_complete"] is True
                assert row["provider_adapter_version"] == "moex-iss-adapter-v1"
                if row["http_status"] == 200:
                    successful_http_observation = True
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
                        "fetched_at": (
                            row["fetched_at"].isoformat() if row["fetched_at"] else None
                        ),
                        "provider_observed_at": (
                            row["provider_observed_at"].isoformat()
                            if row["provider_observed_at"] else None
                        ),
                        "captured_at_utc": row["captured_at_utc"].isoformat(),
                        "body_complete": row["body_complete"],
                        "provider_adapter_version": row["provider_adapter_version"],
                        "record_parent_hash": row["parent_hash"],
                        "record_hash": row["record_hash"],
                    }
                )
        assert successful_http_observation, "No real MOEX HTTP 200 observation was persisted"
        artifact["evidence"] = evidence_artifacts

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
            assert verdict is not None, "MOEX Verdict absent from PostgreSQL"
            assert verdict["claim_id"] == claim_id
            assert verdict["status"] == "INCONCLUSIVE"
            assert list(verdict["evidence_ids"]) == evidence_ids
            artifact["verdict"] = {
                "verdict_id": verdict["verdict_id"],
                "claim_id": verdict["claim_id"],
                "status": verdict["status"],
                "evidence_ids": list(verdict["evidence_ids"]),
                "policy_id": verdict["policy_id"],
                "policy_version": verdict["policy_version"],
                "code_version": verdict["code_version"],
                "reason_codes": reason_codes,
                "parent_hash": verdict["parent_hash"],
                "record_hash": verdict["record_hash"],
            }

        for evidence_id in evidence_ids:
            read_evidence = http_client.get(f"/v1/evidence/{evidence_id}")
            assert read_evidence.status_code == 200, (
                f"MOEX Evidence chain-verifying API read failed: {read_evidence.text}"
            )
        read_verdict = http_client.get(f"/v1/verdicts/{verdict_id}")
        assert read_verdict.status_code == 200, (
            f"MOEX Verdict chain-verifying API read failed: {read_verdict.text}"
        )
        artifact["chain_integrity"] = {
            "evidence_api_reads": "VERIFIED",
            "verdict_api_read": "VERIFIED",
            "basis": "PostgresLedger read paths validate full append-only hash chains",
        }

    try:
        asyncio.run(run())
        artifact["test_status"] = "PASS"
    except Exception as exc:
        artifact["test_status"] = "FAIL"
        artifact["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write_artifact(artifact_path, artifact)
