"""Fail-closed sanitization for provider diagnostics persisted as evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_NAMES = {
    "apikey",
    "accesstoken",
    "auth",
    "authtoken",
    "authorization",
    "clientsecret",
    "cookie",
    "password",
    "passwd",
    "proxyauthorization",
    "secret",
    "setcookie",
    "signature",
    "sig",
    "token",
    "xapikey",
    "xauthtoken",
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?ix)(?P<quote>[\"']?)(?P<key>[a-z0-9_.-]*(?:api[_-]?key|access[_-]?token|auth(?:orization)?|"
    r"client[_-]?secret|cookie|password|passwd|secret|signature|sig|token)"
    r"[a-z0-9_.-]*)(?P=quote)\s*(?P<sep>[:=])\s*"
    r'(?P<value>"[^"]*"|\'[^\']*\'|[^\s,;&]+)'
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_sensitive_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return normalized in _SENSITIVE_NAMES or any(
        marker in normalized
        for marker in ("apikey", "token", "secret", "password", "authorization", "auth", "cookie")
    )


def sanitize_url(value: str) -> str:
    """Remove URL credentials, secret query values, and fragments."""
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return "[REDACTED_URL]"
        hostname = parts.hostname
        if not hostname:
            return "[REDACTED_URL]"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parts.port
        netloc = f"{hostname}:{port}" if port is not None else hostname
        query = [
            (key, "[REDACTED]" if _is_sensitive_name(key) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def sanitize_text(value: str) -> str:
    """Redact common credential assignments, bearer tokens, and embedded URLs."""
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", value)
    cleaned = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('quote')}{match.group('key')}{match.group('quote')}"
            f"{match.group('sep')}[REDACTED]"
        ),
        cleaned,
    )
    return _URL_RE.sub(lambda match: sanitize_url(match.group(0)), cleaned)


def sanitize_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively sanitize structured diagnostic values before persistence."""
    if key is not None and _is_sensitive_name(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    return value
