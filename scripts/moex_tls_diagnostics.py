"""Capture a verified TLS handshake and Python's default CA trust configuration.

This is a diagnostic probe only. Certificate verification remains enabled; a
verification failure is recorded in the JSON report and does not get bypassed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import ssl
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CERTIFICATE_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_VERIFY_RE = re.compile(r"Verify return code:\s*(\d+)\s*\(([^)]*)\)")


def python_trust_store_report() -> dict[str, Any]:
    """Describe the default SSL context used by urllib's HTTPS handler."""
    paths = ssl.get_default_verify_paths()
    context = ssl.create_default_context()
    cafile = paths.cafile
    capath = paths.capath
    certifi_report: dict[str, Any] = {"installed": False}
    try:
        certifi = importlib.import_module("certifi")
    except ImportError:
        pass
    else:
        certifi_path = str(certifi.where())
        active_paths = {
            os.path.realpath(value)
            for value in (cafile, os.environ.get("SSL_CERT_FILE"))
            if value
        }
        certifi_report = {
            "installed": True,
            "bundle_path": certifi_path,
            "active_as_default_cafile": os.path.realpath(certifi_path) in active_paths,
        }

    return {
        "context": "ssl.create_default_context (urllib HTTPS default)",
        "verify_mode": context.verify_mode.name,
        "check_hostname": context.check_hostname,
        "cert_store_stats": context.cert_store_stats(),
        "default_verify_paths": {
            "cafile": cafile,
            "cafile_exists": bool(cafile and os.path.isfile(cafile)),
            "capath": capath,
            "capath_exists": bool(capath and os.path.isdir(capath)),
            "openssl_cafile_env": paths.openssl_cafile_env,
            "openssl_cafile": paths.openssl_cafile,
            "openssl_capath_env": paths.openssl_capath_env,
            "openssl_capath": paths.openssl_capath,
        },
        "environment": {
            "SSL_CERT_FILE": os.environ.get("SSL_CERT_FILE"),
            "SSL_CERT_DIR": os.environ.get("SSL_CERT_DIR"),
        },
        "certifi": certifi_report,
    }


def _certificate_metadata(
    pem: bytes, *, openssl: str, runner: Any
) -> dict[str, str]:
    result = runner(
        [
            openssl,
            "x509",
            "-noout",
            "-subject",
            "-issuer",
            "-serial",
            "-fingerprint",
            "-sha256",
        ],
        input=pem,
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip().lower().replace(" ", "_")] = value.strip()
    return {
        "subject": fields.get("subject", "UNAVAILABLE"),
        "issuer": fields.get("issuer", "UNAVAILABLE"),
        "serial": fields.get("serial", "UNAVAILABLE"),
        "sha256_fingerprint": fields.get("sha256_fingerprint", "UNAVAILABLE"),
        "certificate_sha256": hashlib.sha256(pem).hexdigest(),
        "pem": pem.decode("ascii"),
    }


def collect_tls_diagnostics(
    host: str,
    *,
    runner: Any = subprocess.run,
    openssl_path: str | None = None,
) -> dict[str, Any]:
    """Collect peer-sent certificates while retaining normal TLS verification."""
    openssl = openssl_path or shutil.which("openssl")
    report: dict[str, Any] = {
        "host": host,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "chain_source": (
            "Certificates sent by TLS peer via openssl s_client -showcerts; "
            "trust anchors are not necessarily sent by the peer."
        ),
        "python_trust_store": python_trust_store_report(),
        "certificate_chain": [],
        "openssl_path": openssl,
    }
    if openssl is None:
        report["probe_error"] = "openssl executable not found"
        return report

    command = [
        openssl,
        "s_client",
        "-connect",
        f"{host}:443",
        "-servername",
        host,
        "-showcerts",
        "-verify_return_error",
        "-verify_hostname",
        host,
    ]
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.isfile(paths.cafile):
        command.extend(["-CAfile", paths.cafile])
    if paths.capath and os.path.isdir(paths.capath):
        command.extend(["-CApath", paths.capath])
    report["command"] = command

    try:
        result = runner(
            command,
            input=b"",
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["probe_error"] = f"{type(exc).__name__}: {exc}"
        return report

    transcript_bytes = result.stdout + b"\n" + result.stderr
    transcript = transcript_bytes.decode("utf-8", errors="replace")
    report["openssl_exit_code"] = result.returncode
    report["verification_transcript"] = transcript
    verify_match = _VERIFY_RE.search(transcript)
    if verify_match:
        report["verification_result"] = {
            "code": int(verify_match.group(1)),
            "message": verify_match.group(2),
        }
    else:
        report["verification_result"] = {
            "code": None,
            "message": "No Verify return code emitted; inspect transcript",
        }

    certificates = _CERTIFICATE_RE.findall(transcript_bytes)
    report["certificate_chain"] = [
        _certificate_metadata(pem, openssl=openssl, runner=runner)
        for pem in certificates
    ]
    report["certificate_count"] = len(certificates)
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write the complete diagnostic record as deterministic UTF-8 JSON."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = collect_tls_diagnostics(args.host)
    write_report(report, args.output)
    print(
        json.dumps(
            {
                "host": report["host"],
                "certificate_count": report.get("certificate_count", 0),
                "verification_result": report.get("verification_result"),
                "probe_error": report.get("probe_error"),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    # A failed certificate verification is diagnostic evidence, not a reason
    # to suppress the report or to retry with verification disabled.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
