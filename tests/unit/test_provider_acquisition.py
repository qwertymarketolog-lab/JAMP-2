"""Offline integration tests for MOEX acquisition diagnostics and evidence persistence."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx

from jamp2.api.app import create_app
from jamp2.evidence.digest import compute_record_hash
from jamp2.evidence.model import EvidenceRecord
from jamp2.evidence.verdict import VerdictRecord
from jamp2.providers.moex import MoexProvider


class FrozenHttpResponse:
    """Minimal deterministic HTTP response; no socket or network is used."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FrozenHttpResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class MemoryLedger:
    """Small append-only ledger exercising the API persistence boundary."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []
        self.verdicts: list[VerdictRecord] = []
        self.idempotency: dict[str, dict[str, Any]] = {}

    async def begin_idempotency(
        self, *, key_hash: str, request_digest: str, claim_id: str
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        owner_token = uuid.uuid4().hex
        self.idempotency[key_hash] = {
            "request_digest": request_digest,
            "claim_id": claim_id,
            "owner_token": owner_token,
        }
        return "NEW", owner_token, None

    async def append(
        self, record: EvidenceRecord, *, key_hash: str, owner_token: str
    ) -> EvidenceRecord:
        reservation = self.idempotency[key_hash]
        assert reservation["owner_token"] == owner_token
        assert reservation["claim_id"] == record.claim_id
        persisted = replace(record, record_hash=compute_record_hash(record))
        self.records.append(persisted)
        return persisted

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        return next(
            (record for record in self.records if record.evidence_id == evidence_id),
            None,
        )

    async def list_by_claim(self, claim_id: str) -> list[EvidenceRecord]:
        return [record for record in self.records if record.claim_id == claim_id]

    async def append_verdict(
        self, record: VerdictRecord, *, key_hash: str, owner_token: str
    ) -> VerdictRecord:
        reservation = self.idempotency[key_hash]
        assert reservation["owner_token"] == owner_token
        self.verdicts.append(record)
        return record

    async def get_verdict(self, verdict_id: str) -> VerdictRecord | None:
        return next(
            (record for record in self.verdicts if record.verdict_id == verdict_id),
            None,
        )

    async def complete_idempotency(
        self, *, key_hash: str, request_digest: str, verdict_id: str,
        response_payload: dict[str, Any], owner_token: str
    ) -> None:
        reservation = self.idempotency[key_hash]
        assert reservation["request_digest"] == request_digest
        assert reservation["owner_token"] == owner_token
        reservation["verdict_id"] = verdict_id
        reservation["response_payload"] = response_payload

    async def fail_idempotency(
        self, *, key_hash: str, request_digest: str, owner_token: str
    ) -> None:
        reservation = self.idempotency[key_hash]
        assert reservation["request_digest"] == request_digest
        assert reservation["owner_token"] == owner_token


def _claim_payload() -> dict[str, Any]:
    return {
        "text": "SBER last price is 300 RUB",
        "subject": {"kind": "equity_security", "identifier": "SBER"},
        "predicate": "price",
        "expected_value": 300.0,
        "unit": "RUB",
    }


def _securities_payload() -> dict[str, Any]:
    return {"securities": {"columns": ["secid"], "data": [["SBER"]]}}


def _marketdata_payload() -> dict[str, Any]:
    return {
        "marketdata": {
            "columns": [
                "SECID", "LAST", "MARKETPRICE", "MARKETPRICE2",
                "BOARDID", "TRADEDATE", "SYSTIME",
            ],
            "data": [["SBER", 300.0, None, None, "TQBR", "2026-11-25", "18:00:00"]],
        }
    }


def test_moex_frozen_http_responses_reach_evidence_ledger() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        app = create_app(
            ledger,
            api_key="test-key",
            providers={"moex": MoexProvider()},
            code_version="test-code",
        )
        frozen = [
            FrozenHttpResponse(_securities_payload()),
            FrozenHttpResponse(_marketdata_payload()),
        ]
        with patch("urllib.request.urlopen", side_effect=frozen):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={
                    "X-API-Key": "test-key",
                    "Idempotency-Key": "moex-frozen-success",
                },
            ) as client:
                response = await client.post("/v1/claims/verify", json=_claim_payload())

        assert response.status_code == 201
        assert len(ledger.records) == 2
        assert [record.http_status for record in ledger.records] == [200, 200]
        assert all(record.body_complete is True for record in ledger.records)
        assert [record.raw_body_bytes for record in ledger.records] == [
            frozen[0].body,
            frozen[1].body,
        ]
        assert all(record.raw_sha256 for record in ledger.records)

    asyncio.run(run())


def test_moex_failure_phase_and_original_cause_survive_to_ledger() -> None:
    async def run() -> None:
        ledger = MemoryLedger()
        app = create_app(
            ledger,
            api_key="test-key",
            providers={"moex": MoexProvider()},
            code_version="test-code",
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(
                socket.gaierror(-3, "Temporary failure in name resolution")
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={
                    "X-API-Key": "test-key",
                    "Idempotency-Key": "moex-frozen-failure",
                },
            ) as client:
                response = await client.post("/v1/claims/verify", json=_claim_payload())

        assert response.status_code == 201
        assert response.json()["provider_status"] == "EVIDENCE_NOT_CAPTURED"
        assert len(ledger.records) == 1
        failure = ledger.records[0]
        assert failure.verdict_status == "SOURCE_FAILURE"
        assert failure.http_status is None
        assert failure.raw_body_bytes == b""
        assert failure.body_complete is False
        assert isinstance(failure.parsed_value, dict)
        details = failure.parsed_value["provider_result"]
        assert isinstance(details, dict)
        assert details["reason"] == "MOEX_SOURCE_UNAVAILABLE_OR_INVALID"
        assert details["error_type"] == "URLError"
        assert details["phase"] == "securities"
        assert details["request_url"] == (
            "https://iss.moex.com/iss/securities.json?"
            "q=SBER&iss.meta=off&iss.only=securities&"
            "securities.columns=secid%2Cshortname%2Clatinname%2Cemitent_title"
        )
        assert details["error_reason_type"] == "gaierror"
        assert details["error_reason"] == (
            "[Errno -3] Temporary failure in name resolution"
        )
        assert details["error_reason_repr"] == (
            "gaierror(-3, 'Temporary failure in name resolution')"
        )
        assert details["error_message"] == (
            "<urlopen error [Errno -3] Temporary failure in name resolution>"
        )
        assert details["capture_status"] == "EVIDENCE_NOT_CAPTURED"
        assert details["capture_reason"] == "SOURCE_EVIDENCE_NOT_CAPTURED"

    asyncio.run(run())


def _make_real_peer_chain(tmp_path: Path) -> tuple[list[dict[str, Any]], str]:
    import hashlib
    import shutil
    import ssl

    from scripts.moex_tls_diagnostics import _certificate_metadata

    openssl = shutil.which("openssl")
    assert openssl is not None, "OpenSSL is required for real X.509 fixture tests"
    intermediate_key = tmp_path / "intermediate.key"
    intermediate_pem = tmp_path / "intermediate.pem"
    leaf_key = tmp_path / "leaf.key"
    leaf_csr = tmp_path / "leaf.csr"
    leaf_pem = tmp_path / "leaf.pem"
    extensions = tmp_path / "leaf.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\\n"
        "subjectAltName=DNS:iss.moex.test\\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(intermediate_key), "-out", str(intermediate_pem),
            "-days", "2", "-subj", "/CN=JAMP Test Intermediate CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(leaf_key), "-out", str(leaf_csr),
            "-subj", "/CN=iss.moex.test",
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            openssl, "x509", "-req", "-in", str(leaf_csr),
            "-CA", str(intermediate_pem), "-CAkey", str(intermediate_key),
            "-CAcreateserial", "-out", str(leaf_pem), "-days", "2",
            "-extfile", str(extensions),
        ],
        capture_output=True,
        check=True,
    )
    chain = [
        _certificate_metadata(path.read_bytes(), openssl=openssl, runner=subprocess.run)
        for path in (leaf_pem, intermediate_pem)
    ]
    intermediate_der = ssl.PEM_cert_to_DER_cert(intermediate_pem.read_text(encoding="ascii"))
    expected = hashlib.sha256(intermediate_der).hexdigest()
    return chain, expected


def test_peer_intermediate_pin_matches_real_x509_der(tmp_path: Path) -> None:
    from scripts.moex_tls_diagnostics import _identify_peer_intermediate, _verify_peer_intermediate

    chain, expected = _make_real_peer_chain(tmp_path)
    intermediate = _identify_peer_intermediate(chain)
    assert intermediate is not None
    assert intermediate["basic_constraints_ca"] is True
    assert intermediate["subject"] == chain[0]["issuer"]
    assert intermediate["certificate_sha256"] == expected
    assert _verify_peer_intermediate(chain, expected)["status"] == "MATCH"


def test_peer_intermediate_pin_rejects_wrong_der_sha256(tmp_path: Path) -> None:
    from scripts.moex_tls_diagnostics import _verify_peer_intermediate

    chain, _ = _make_real_peer_chain(tmp_path)
    result = _verify_peer_intermediate(chain, "0" * 64)
    assert result["status"] == "MISMATCH"
    assert result["actual_sha256_der"] != result["expected_sha256_der"]


def test_peer_intermediate_is_unknown_without_issuer_ca_match() -> None:
    from scripts.moex_tls_diagnostics import _identify_peer_intermediate, _verify_peer_intermediate

    leaf_only = [{
        "subject": "CN=iss.moex.test",
        "issuer": "CN=JAMP Test Intermediate CA",
        "basic_constraints_ca": False,
        "certificate_sha256": "a" * 64,
    }]
    assert _identify_peer_intermediate(leaf_only) is None
    assert _verify_peer_intermediate(leaf_only, "a" * 64)["status"] == "UNKNOWN"


def test_certificate_sha256_uses_der_not_pem_bytes(tmp_path: Path) -> None:
    import hashlib
    import shutil

    openssl = shutil.which("openssl")
    assert openssl is not None
    chain, expected = _make_real_peer_chain(tmp_path)
    assert chain[1]["certificate_sha256"] == expected
    pem_hash = hashlib.sha256(chain[1]["pem"].encode("ascii")).hexdigest()
    assert chain[1]["certificate_sha256"] != pem_hash
