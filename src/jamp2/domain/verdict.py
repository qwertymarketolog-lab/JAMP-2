"""Signed bounded verdicts and deterministic claim aggregation."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from jamp2.domain.claim import Claim

CLAIM_STATUSES = frozenset({"VERIFIED", "CONTRADICTED", "INCONCLUSIVE"})
AGGREGATE_STATUSES = frozenset({"VERIFIED", "CONTRADICTED", "INCONCLUSIVE", "PARTIAL"})

def aggregate_verdict(claims: list[dict[str, Any]]) -> str:
    """Aggregate provider outcomes without promoting unknown or out-of-scope claims."""
    if not claims:
        return "PARTIAL"
    statuses = [str(item.get("status", "INCONCLUSIVE")) for item in claims]
    if any(status in {"CONTRADICTED", "E4_CONTRADICTED"} for status in statuses):
        return "CONTRADICTED"
    if any(
        bool(item.get("critical", False))
        and item.get("status", "INCONCLUSIVE") not in {"VERIFIED", "E4_VERIFIED"}
        for item in claims
    ):
        return "INCONCLUSIVE"
    if all(status in {"VERIFIED", "E4_VERIFIED"} for status in statuses):
        return "VERIFIED"
    return "PARTIAL"

def critical_unverified_claims(claims: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("text", "")) for item in claims
        if bool(item.get("critical", False))
        and item.get("status", "INCONCLUSIVE") not in {"VERIFIED", "E4_VERIFIED"}
    ]

@dataclass(frozen=True)
class JampVerdict:
    """HMAC-SHA256 signed verdict bound to claim results and evidence digest."""
    trace_id: str
    status: str
    provider_model_id: str | None
    evidence_hash: str
    timestamp: float
    signature: str | None = None
    execution_status: str = "EXECUTE"
    model_verification: str | None = None
    claims: list[Claim | dict[str, Any]] = field(default_factory=list)
    aggregate_status: str = "PARTIAL"
    critical_unverified: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.model_verification is None:
            object.__setattr__(self, "model_verification", self.status)

    def _payload(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id, "status": self.status,
            "provider_model_id": self.provider_model_id, "evidence_hash": self.evidence_hash,
            "timestamp": self.timestamp, "execution_status": self.execution_status,
            "model_verification": self.model_verification,
            "claims": [item.to_dict() if isinstance(item, Claim) else item for item in self.claims],
            "aggregate_status": self.aggregate_status,
            "critical_unverified": list(self.critical_unverified),
        }

    def sign(self, secret: str) -> JampVerdict:
        if not secret:
            raise ValueError("Signing secret must not be empty")
        encoded = json.dumps(
            self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
        return JampVerdict(**{**asdict(self), "signature": signature})

    def verify(self, secret: str) -> bool:
        if not secret or not isinstance(self.signature, str):
            return False
        expected = self.sign(secret).signature
        return hmac.compare_digest(self.signature, expected or "")

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JampVerdict:
        if not isinstance(value, dict):
            raise ValueError("Verdict must be a JSON object")
        trace_id, status = value.get("trace_id"), value.get("status")
        provider_model_id = value.get("provider_model_id")
        evidence_hash = value.get("evidence_hash")
        timestamp, signature = value.get("timestamp"), value.get("signature")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("trace_id must be a non-empty string")
        if status not in CLAIM_STATUSES:
            raise ValueError("Unsupported verdict status")
        if provider_model_id is not None and not isinstance(provider_model_id, str):
            raise ValueError("provider_model_id must be a string or null")
        if not isinstance(evidence_hash, str) or not evidence_hash:
            raise ValueError("evidence_hash must be a non-empty string")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError("timestamp must be a number")
        if signature is not None and not isinstance(signature, str):
            raise ValueError("signature must be a string or null")
        claims = value.get("claims", [])
        if not isinstance(claims, list) or any(not isinstance(item, dict) for item in claims):
            raise ValueError("claims must be a list of objects")
        critical = value.get("critical_unverified", [])
        if not isinstance(critical, list) or any(not isinstance(item, str) for item in critical):
            raise ValueError("critical_unverified must be a list of strings")
        execution = value.get("execution_status", "EXECUTE")
        model = value.get("model_verification", status)
        aggregate = value.get("aggregate_status", "PARTIAL")
        if not isinstance(execution, str) or not isinstance(model, str):
            raise ValueError("execution_status and model_verification must be strings")
        if aggregate not in AGGREGATE_STATUSES:
            raise ValueError("Unsupported aggregate_status")
        return cls(
            trace_id, status, provider_model_id, evidence_hash, float(timestamp), signature,
            execution, model, claims, aggregate, critical
        )
