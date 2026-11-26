from __future__ import annotations

import socket
import urllib.error

from jamp2.providers.moex import _failure_diagnostics
from jamp2.providers.sanitization import sanitize_text, sanitize_url, sanitize_value


def test_url_sanitizer_removes_userinfo_query_secrets_and_fragment() -> None:
    value = "https://user:pass@example.test/path?api_key=abc&symbol=SBER#token=fragment-secret"
    sanitized = sanitize_url(value)
    assert "user" not in sanitized
    assert "pass" not in sanitized
    assert "abc" not in sanitized
    assert "fragment-secret" not in sanitized
    assert "symbol=SBER" in sanitized
    assert "api_key=%5BREDACTED%5D" in sanitized


def test_text_sanitizer_redacts_bearer_json_and_assignment_secrets() -> None:
    value = (
        'Authorization: Bearer abc.def.ghi; api_key = "top-secret"; '
        '{"client_secret":"json-secret"} '
        "https://example.test/path?token=url-secret"
    )
    sanitized = sanitize_text(value)
    for secret in ("abc.def.ghi", "top-secret", "json-secret", "url-secret"):
        assert secret not in sanitized


def test_structured_sanitizer_redacts_sensitive_keys_recursively() -> None:
    value = {"outer": [{"api_key": "raw", "safe": "ok"}], "Authorization": "Bearer raw"}
    sanitized = sanitize_value(value)
    assert sanitized == {
        "outer": [{"api_key": "[REDACTED]", "safe": "ok"}],
        "Authorization": "[REDACTED]",
    }


def test_moex_failure_diagnostics_sanitize_url_and_exception_text() -> None:
    secret = "provider-secret-123"
    exc = urllib.error.URLError(socket.gaierror(-3, f"token={secret}"))
    diagnostics = _failure_diagnostics(
        exc=exc,
        request_url=f"https://iss.moex.com/path?api_key={secret}&q=SBER#secret={secret}",
        phase="securities",
    )
    serialized = repr(diagnostics)
    assert secret not in serialized
    assert diagnostics["request_url"].endswith("api_key=%5BREDACTED%5D&q=SBER")
    assert "[REDACTED]" in diagnostics["error_reason"]
    assert "[REDACTED]" in diagnostics["error_reason_repr"]
