"""Offline tests for TLS chain diagnostics and JSON evidence persistence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.moex_tls_diagnostics import (
    _OFFICIAL_CA,
    _scan_trust_store,
    collect_tls_diagnostics,
    collect_official_ca_audit,
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

    def offline_downloader(*_: Any, **__: Any) -> Any:
        raise OSError("network disabled in unit test")

    report = collect_tls_diagnostics(
        "iss.moex.com", runner=fake_runner, openssl_path="/usr/bin/openssl",
        downloader=offline_downloader,
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

    with patch("scripts.moex_tls_diagnostics.shutil.which", return_value=None):
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



def test_official_ca_download_failures_are_explicit_and_fail_closed() -> None:
    def offline_downloader(*_: Any, **__: Any) -> Any:
        raise OSError("offline")

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, b"", b"")

    report = collect_official_ca_audit(
        [], "iss.moex.com", runner=fake_runner,
        openssl="/usr/bin/openssl", downloader=offline_downloader,
    )

    assert report["certificates"]["root"]["downloaded"] is False
    assert "OSError: offline" == report["certificates"]["root"]["error"]
    assert report["certificates"]["root"]["fingerprint_match"] is False
    assert report["trust_store_membership"] is None
    assert report["chain_verification"]["status"] == "NOT_RUN"
    assert report["chain_verification"]["trust_store_modified"] is False


def test_trust_store_membership_uses_exact_root_der_fingerprint(tmp_path: Path) -> None:
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_bytes(_FAKE_PEM)
    capath = tmp_path / "certs"
    capath.mkdir()

    expected = _OFFICIAL_CA["root"]["sha256"]

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command, 0, f"sha256 Fingerprint={expected}\n".encode(), b""
        )

    report = _scan_trust_store(
        openssl="/usr/bin/openssl", runner=fake_runner,
        cafile=str(bundle), capath=str(capath),
    )

    assert report["scanned_files"] == 1
    assert report["root_sha256_present"] is True
    assert report["root_sha256"] == expected
