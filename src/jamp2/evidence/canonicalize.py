"""Deterministic JSON canonicalization for evidence metadata."""

from __future__ import annotations

import json
import unicodedata

from jamp2.evidence.model import JSONValue


def _normalize(value: JSONValue) -> JSONValue:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, JSONValue] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("Object keys collide after Unicode NFC normalization")
            normalized[normalized_key] = _normalize(item)
        return normalized
    return value


def canonicalize(value: JSONValue) -> bytes:
    """Serialize JSON-compatible data with NFC strings and sorted object keys."""
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
