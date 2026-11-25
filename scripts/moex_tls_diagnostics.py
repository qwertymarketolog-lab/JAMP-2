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
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CERTIFICATE_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_VERIFY_RE = re.compile(r"Verify return code:\s*(\d+)\s*\(([^)]*)\)")

_OFFICIAL_CA = {
    "root": {
        "url": "http://nuc-cdp.digital.gov.ru/cdp/rootca_ssl_rsa2022.crt",
        "sha256": "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31",
    },
    "sub": {
        "url": "http://nuc-cdp.digital.gov.ru/cdp/subca_ssl_rsa2024.crt",
        "sha256": "2155785036C900DBB5F1BB2A1569C80C55595BD6BF94867A29BBDDBC7D88A3F2",
    },
}

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



def _pem_certificates(data: bytes) -> list[bytes]:
    """Return PEM blocks from PEM or DER certificate input."""
    matches = _CERTIFICATE_RE.findall(data)
    if matches:
        return matches
    return [ssl.DER_cert_to_PEM_cert(data).encode("ascii")]


def _openssl_fingerprint(
    pem: bytes, *, openssl: str, runner: Any
) -> str | None:
    result = runner(
        [openssl, "x509", "-noout", "-fingerprint", "-sha256"],
        input=pem,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    match = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", output)
    return match.group(1).replace(":", "").upper() if match else None


def _scan_trust_store(
    *, openssl: str, runner: Any, cafile: str | None, capath: str | None
) -> dict[str, Any]:
    """Scan the configured bundle and hashed CA directory by certificate fingerprint."""
    fingerprints: set[str] = set()
    scanned_files = 0
    unreadable_files = 0
    candidates: list[Path] = []
    if cafile and Path(cafile).is_file():
        candidates.append(Path(cafile))
    if capath and Path(capath).is_dir():
        candidates.extend(path for path in Path(capath).iterdir() if path.is_file())

    for path in candidates:
        try:
            data = path.read_bytes()
            certs = _pem_certificates(data)
        except (OSError, ValueError, ssl.SSLError):
            unreadable_files += 1
            continue
        scanned_files += 1
        for pem in certs:
            fingerprint = _openssl_fingerprint(pem, openssl=openssl, runner=runner)
            if fingerprint:
                fingerprints.add(fingerprint)
    return {
        "scanned_files": scanned_files,
        "unreadable_files": unreadable_files,
        "unique_certificate_fingerprints": len(fingerprints),
        "root_sha256_present": _OFFICIAL_CA["root"]["sha256"] in fingerprints,
        "root_sha256": _OFFICIAL_CA["root"]["sha256"],
    }


def collect_official_ca_audit(
    peer_chain: list[dict[str, Any]],
    host: str,
    *,
    runner: Any = subprocess.run,
    openssl: str | None = None,
    downloader: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    """Download AIA-published CA files, pin their DER hashes, and audit runner trust."""
    openssl = openssl or shutil.which("openssl")
    report: dict[str, Any] = {
        "sources": "AIA URIs embedded in the observed MOEX peer certificate chain",
        "certificates": {},
        "trust_store_membership": None,
        "chain_verification": None,
    }
    if openssl is None:
        report["error"] = "openssl executable not found"
        return report

    downloaded_pem: dict[str, bytes] = {}
    for name, spec in _OFFICIAL_CA.items():
        entry: dict[str, Any] = {
            "url": spec["url"],
            "expected_sha256_der": spec["sha256"],
            "downloaded": False,
            "fingerprint_match": False,
        }
        try:
            with downloader(spec["url"], timeout=20) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("certificate download exceeded 1 MiB")
            pem = _pem_certificates(raw)[0]
            der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
            actual = hashlib.sha256(der).hexdigest().upper()
            openssl_fingerprint = _openssl_fingerprint(
                pem, openssl=openssl, runner=runner
            )
            entry.update(
                {
                    "downloaded": True,
                    "sha256_der": actual,
                    "openssl_sha256_fingerprint": openssl_fingerprint,
                    "fingerprint_match": (
                        actual == spec["sha256"]
                        and openssl_fingerprint == spec["sha256"]
                    ),
                    "pem": pem.decode("ascii"),
                }
            )
            downloaded_pem[name] = pem
        except Exception as exc:  # diagnostics must retain per-source failures
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report["certificates"][name] = entry

    paths = ssl.get_default_verify_paths()
    if "root" in downloaded_pem:
        report["trust_store_membership"] = _scan_trust_store(
            openssl=openssl,
            runner=runner,
            cafile=paths.cafile,
            capath=paths.capath,
        )

    root_ok = report["certificates"].get("root", {}).get("fingerprint_match")
    sub_ok = report["certificates"].get("sub", {}).get("fingerprint_match")
    leaf = next(
        (item for item in peer_chain if "MOEX" in item.get("subject", "").upper()
         or "*.MOEX.COM" in item.get("subject", "").upper()),
        None,
    )
    intermediate = next(
        (item for item in peer_chain if "RUSSIAN TRUSTED SUB CA" in item.get("subject", "").upper()),
        None,
    )
    if root_ok and sub_ok and leaf and intermediate:
        with tempfile.TemporaryDirectory(prefix="jamp2-moex-ca-audit-") as temp:
            root_path = Path(temp) / "root.pem"
            sub_path = Path(temp) / "sub.pem"
            leaf_path = Path(temp) / "leaf.pem"
            root_path.write_bytes(downloaded_pem["root"])
            sub_path.write_text(intermediate["pem"], encoding="ascii")
            leaf_path.write_text(leaf["pem"], encoding="ascii")
            verify = runner(
                [
                    openssl, "verify", "-verbose", "-CAfile", str(root_path),
                    "-no-CApath", "-no-CAstore", "-untrusted", str(sub_path),
                    "-verify_hostname", host, str(leaf_path),
                ],
                capture_output=True,
                check=False,
            )
            report["chain_verification"] = {
                "exit_code": verify.returncode,
                "stdout": verify.stdout.decode("utf-8", errors="replace"),
                "stderr": verify.stderr.decode("utf-8", errors="replace"),
                "trust_store_modified": False,
            }
    else:
        report["chain_verification"] = {
            "status": "NOT_RUN",
            "reason": "Official root/sub fingerprints or peer leaf/intermediate unavailable",
            "trust_store_modified": False,
        }
    return report

def collect_tls_diagnostics(
    host: str,
    *,
    runner: Any = subprocess.run,
    openssl_path: str | None = None,
    downloader: Any = urllib.request.urlopen,
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
    report["official_ca_audit"] = collect_official_ca_audit(
        report["certificate_chain"], host, runner=runner, openssl=openssl,
        downloader=downloader,
    )
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
