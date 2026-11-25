"""Offline tests for TLS chain diagnostics and JSON evidence persistence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from scripts.moex_tls_diagnostics import (
    collect_tls_diagnostics,
    python_trust_store_report,
    write_report,
)

_FAKE_PEM = (
    b"-----BEGIN CERTIFICATE-----\n"
    b"ZmFrZS1jZXJ0aWZpY2F0ZQ==\n"
    b"-----END CERTIFICATE-----"
)


def test_tls_chain_and_trust_store_fields_are_saved(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "s_client" in command:
            output = (
                b"CONNECTED(00000003)\n"
                + _FAKE_PEM
                + b"\nVerify return code: 19 (self-signed certificate in certificate chain)\n"
            )
        else:
            output = (
                b"subject=CN = iss.moex.com\n"
                b"issuer=CN = Corporate TLS Inspection CA\n"
                b"serial=01A2B3\n"
                b"sha256 Fingerprint=AA:BB:CC:DD\n"
            )
        return subprocess.CompletedProcess(command, 0, output, b"")

    report = collect_tls_diagnostics(
        "iss.moex.com", runner=fake_runner, openssl_path="/usr/bin/openssl"
    )
    output = tmp_path / "artifacts" / "moex-tls-diagnostics.json"
    write_report(report, output)
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert saved["host"] == "iss.moex.com"
    assert saved["openssl_exit_code"] == 0
    assert saved["verification_result"] == {
        "code": 19,
        "message": "self-signed certificate in certificate chain",
    }
    assert saved["certificate_count"] == 1
    certificate = saved["certificate_chain"][0]
    assert certificate["subject"] == "CN = iss.moex.com"
    assert certificate["issuer"] == "CN = Corporate TLS Inspection CA"
    assert certificate["serial"] == "01A2B3"
    assert certificate["sha256_fingerprint"] == "AA:BB:CC:DD"
    assert certificate["pem"] == _FAKE_PEM.decode("ascii")
    assert len(certificate["certificate_sha256"]) == 64
    assert saved["python_trust_store"]["check_hostname"] is True
    assert saved["python_trust_store"]["verify_mode"] == "CERT_REQUIRED"
    assert "cert_store_stats" in saved["python_trust_store"]
    assert "default_verify_paths" in saved["python_trust_store"]
    assert "certifi" in saved["python_trust_store"]
    assert "-verify_return_error" in calls[0]
    assert "-verify_hostname" in calls[0]
    assert "-showcerts" in calls[0]
    assert calls[1][1:3] == ["x509", "-noout"]


def test_tls_diagnostics_reports_missing_openssl_without_network() -> None:
    def unexpected_runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("runner must not be called when openssl is unavailable")

    report = collect_tls_diagnostics(
        "iss.moex.com", runner=unexpected_runner, openssl_path=None
    )
    assert report["openssl_path"] is None
    assert report["probe_error"] == "openssl executable not found"
    assert report["certificate_chain"] == []


def test_python_trust_store_report_identifies_verified_default_context() -> None:
    report = python_trust_store_report()

    assert report["context"] == "ssl.create_default_context (urllib HTTPS default)"
    assert report["check_hostname"] is True
    assert report["verify_mode"] == "CERT_REQUIRED"
    assert set(report["environment"]) == {"SSL_CERT_FILE", "SSL_CERT_DIR"}
    assert set(report["default_verify_paths"]) >= {
        "cafile",
        "capath",
        "cafile_exists",
        "capath_exists",
    }
