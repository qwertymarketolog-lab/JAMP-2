"""Bounded factual verification against the Moscow Exchange ISS API.

This module verifies only explicit claims about a MOEX security. A missing ticker
is OUT_OF_SCOPE, never proof that a company does not exist.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from typing import Any

from jamp2.domain.claim import Claim as Jamp2Claim
from jamp2.domain.claim import NormalizedClaim
from jamp2.providers.base import ProviderResult
from jamp2.providers.sanitization import sanitize_text, sanitize_url, sanitize_value

MOEX_ISS_BASE = "https://iss.moex.com/iss"
E4_VERIFIED = "E4_VERIFIED"
E4_CONTRADICTED = "E4_CONTRADICTED"
E4_OUT_OF_SCOPE = "E4_OUT_OF_SCOPE"
E4_INCONCLUSIVE = "E4_INCONCLUSIVE"
PRICE_FIELDS = ["LAST", "MARKETPRICE", "MARKETPRICE2"]

logger = logging.getLogger(__name__)


def _moex_capture_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"provider_observed_at": None}
    table = payload.get("marketdata")
    if not isinstance(table, dict):
        return {
            **metadata,
            "provider_observed_at_status": "TABLE_MISSING",
            "source_date_status": "TABLE_MISSING",
        }
    columns, rows = table.get("columns"), table.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return {
            **metadata,
            "provider_observed_at_status": "TABLE_INVALID",
            "source_date_status": "TABLE_INVALID",
        }

    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            continue
        values = dict(zip(columns, row, strict=True))
        trade_date = values.get("TRADEDATE")
        if trade_date is None:
            metadata["source_date_status"] = "MISSING"
        elif not isinstance(trade_date, str):
            metadata["source_date_status"] = "INVALID_TYPE"
            metadata["source_date_raw"] = trade_date
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", trade_date) is None:
            metadata["source_date_status"] = "INVALID_FORMAT"
            metadata["source_date_raw"] = trade_date
        else:
            try:
                date.fromisoformat(trade_date)
            except ValueError:
                metadata["source_date_status"] = "INVALID_CALENDAR_DATE"
                metadata["source_date_raw"] = trade_date
            else:
                metadata["source_date"] = trade_date
                metadata["source_date_status"] = "VALID"

        systime = values.get("SYSTIME")
        if systime is None:
            metadata["provider_observed_at_status"] = "MISSING"
        elif isinstance(systime, str) and systime.strip():
            metadata["provider_observed_at_status"] = "UNVERIFIED_SOURCE_CONTRACT"
            metadata["provider_observed_at_raw"] = systime
        elif isinstance(systime, str):
            metadata["provider_observed_at_status"] = "EMPTY"
            metadata["provider_observed_at_raw"] = systime
        else:
            metadata["provider_observed_at_status"] = "INVALID_TYPE"
            metadata["provider_observed_at_raw"] = systime
        return metadata

    return {
        **metadata,
        "provider_observed_at_status": "NO_VALID_ROW",
        "source_date_status": "NO_VALID_ROW",
    }


def _get_json(
    url: str,
    timeout: float,
    phase: str,
    raw_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "JAMP-Trust-Layer/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status_code = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if raw_evidence is not None:
            raw_evidence.append({
                "source": "moex.iss",
                "url": sanitize_url(url),
                "provider_observed_at": None,
                "provider_observed_at_status": "HTTP_ERROR_NO_SOURCE_TIMESTAMP",
                "fetched_at": datetime.now(UTC).isoformat(),
                "status_code": exc.code,
                "body_complete": True,
                "raw_body_base64": base64.b64encode(body).decode("ascii"),
                "sha256_body": hashlib.sha256(body).hexdigest(),
            })
        raise
    except urllib.error.URLError as exc:
        # Connection-establishment timeouts are wrapped in URLError(reason=timeout).
        # DNS, refused-connection, and TLS errors are re-raised without timeout logging.
        is_timeout = isinstance(exc.reason, (TimeoutError, socket.timeout))
        if is_timeout:
            logger.error(
                "MOEX timeout (URLError): url=%s, timeout=%.1fs, phase=%s, reason=%r",
                sanitize_url(url), timeout, phase, sanitize_text(repr(exc.reason)),
            )
        raise
    except TimeoutError:
        # Response-read timeouts may propagate directly as TimeoutError.
        logger.error(
            "MOEX timeout (direct): url=%s, timeout=%.1fs, phase=%s",
            sanitize_url(url), timeout, phase,
        )
        raise
    capture = None
    if raw_evidence is not None:
        capture = {
            "source": "moex.iss", "url": sanitize_url(url), "status_code": status_code,
            "fetched_at": datetime.now(UTC).isoformat(), "body_complete": True,
            "raw_body_base64": base64.b64encode(body).decode("ascii"),
            "sha256_body": hashlib.sha256(body).hexdigest(),
        }
        raw_evidence.append(capture)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MOEX ISS response must be a JSON object")
    if capture is not None:
        capture.update(_moex_capture_metadata(payload))
    return payload


def _table_rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    table = payload.get(table_name)
    if not isinstance(table, dict):
        raise ValueError(f"MOEX ISS response missing {table_name} table")
    columns = table.get("columns")
    rows = table.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError(f"MOEX ISS {table_name} table has invalid schema")
    if any(not isinstance(column, str) for column in columns):
        raise ValueError(f"MOEX ISS {table_name} columns are invalid")
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list) and len(row) == len(columns):
            result.append(dict(zip(columns, row, strict=True)))
    return result


def _failure_diagnostics(
    *,
    exc: Exception | None = None,
    http_status: int | None = None,
    board_id: str | None = None,
    marketdata_columns: list[str] | None = None,
    marketdata_rows_count: int | None = None,
    securities_columns: list[str] | None = None,
    parse_error: str | None = None,
    tried_fields: list[str] | None = None,
    phase: str | None = None,
    request_url: str | None = None,
) -> dict[str, Any]:
    if isinstance(exc, urllib.error.HTTPError):
        http_status = exc.code
    if (
        exc is not None
        and parse_error is None
        and isinstance(exc, (ValueError, json.JSONDecodeError))
    ):
        parse_error = f"{type(exc).__name__}: {exc}"
    diagnostics: dict[str, Any] = {
        "http_status": http_status,
        "board_id": board_id,
        "marketdata_columns": marketdata_columns,
        "marketdata_rows_count": marketdata_rows_count,
        "securities_columns": securities_columns,
        "parse_error": sanitize_text(parse_error) if parse_error else None,
        "tried_fields": list(tried_fields or []),
        "phase": phase,
    }
    if request_url is not None:
        diagnostics["request_url"] = sanitize_url(request_url)
    if isinstance(exc, urllib.error.URLError):
        diagnostics.update(
            {
                "error_message": sanitize_text(str(exc)),
                "error_reason": sanitize_text(str(exc.reason)),
                "error_reason_type": type(exc.reason).__name__,
                "error_reason_repr": sanitize_text(repr(exc.reason)),
            }
        )
    return sanitize_value(diagnostics)


def _verify_claim_with_moex(
    ticker: str,
    claim_type: str,
    claimed_value: float | int | None,
    *,
    timeout: float = 5.0,
    price_tolerance: float = 0.05,
    raw_evidence: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Verify an explicit MOEX ticker or last-price claim.

    Returns one of E4_VERIFIED, E4_CONTRADICTED, E4_OUT_OF_SCOPE, or
    E4_INCONCLUSIVE together with bounded source evidence.
    """
    if claim_type != "price":
        return E4_OUT_OF_SCOPE, {
            "reason": "NOT_A_PRICE_CLAIM",
            "claim_type": claim_type,
            "checked_source": "moex.iss",
        }
    if claimed_value is None:
        return E4_INCONCLUSIVE, {
            "reason": "MISSING_VALUE_INTERNAL",
            "checked_source": "moex.iss",
        }
    normalized_ticker = ticker.strip().upper() if isinstance(ticker, str) else ""
    if not normalized_ticker or not normalized_ticker.replace(".", "").replace("-", "").isalnum():
        return E4_INCONCLUSIVE, {
            "reason": "INVALID_TICKER",
            "checked_source": "moex.iss",
            **_failure_diagnostics(),
        }
    if claim_type not in {"exists", "price"}:
        return E4_INCONCLUSIVE, {
            "reason": "UNSUPPORTED_CLAIM_TYPE",
            "claim_type": claim_type,
            "checked_source": "moex.iss",
            **_failure_diagnostics(),
        }
    if timeout <= 0 or not math.isfinite(timeout):
        return E4_INCONCLUSIVE, {
            "reason": "INVALID_TIMEOUT",
            "checked_source": "moex.iss",
            **_failure_diagnostics(),
        }
    if not math.isfinite(price_tolerance) or not 0 <= price_tolerance < 1:
        return E4_INCONCLUSIVE, {
            "reason": "INVALID_PRICE_TOLERANCE",
            "checked_source": "moex.iss",
            **_failure_diagnostics(),
        }
    if claim_type == "price" and (
        isinstance(claimed_value, bool)
        or not isinstance(claimed_value, (int, float))
        or not math.isfinite(float(claimed_value))
        or float(claimed_value) <= 0
    ):
        return E4_INCONCLUSIVE, {
            "reason": "INVALID_CLAIMED_PRICE",
            "claim": {"ticker": normalized_ticker, "type": claim_type, "value": claimed_value},
            "checked_source": "moex.iss",
            **_failure_diagnostics(),
        }

    query = urllib.parse.urlencode(
        {
            "q": normalized_ticker,
            "iss.meta": "off",
            "iss.only": "securities",
            "securities.columns": "secid,shortname,latinname,emitent_title",
        }
    )
    securities_url = f"{MOEX_ISS_BASE}/securities.json?{query}"
    securities_columns: list[str] | None = None
    try:
        catalog = _get_json(
            securities_url,
            timeout,
            phase="securities",
            raw_evidence=raw_evidence,
        )
        securities_table = catalog.get("securities")
        if isinstance(securities_table, dict) and isinstance(securities_table.get("columns"), list):
            securities_columns = [
                col for col in securities_table["columns"] if isinstance(col, str)
            ]
        securities = _table_rows(catalog, "securities")
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return E4_INCONCLUSIVE, {
            "reason": "MOEX_SOURCE_UNAVAILABLE_OR_INVALID",
            "error_type": type(exc).__name__,
            "checked_source": "moex.iss",
            **_failure_diagnostics(
                exc=exc,
                securities_columns=securities_columns,
                phase="securities",
                request_url=securities_url,
            ),
        }

    match = next(
        (
            row
            for row in securities
            if isinstance(row.get("secid"), str) and row["secid"].upper() == normalized_ticker
        ),
        None,
    )
    if match is None:
        return E4_OUT_OF_SCOPE, {
            "claim": {"ticker": normalized_ticker, "type": claim_type, "value": claimed_value},
            "checked_source": "moex.iss",
            "result": "OUT_OF_SCOPE",
            "reason": (
                "Ticker not found in MOEX catalog; cannot verify existence of unlisted "
                "or foreign company"
            ),
            **_failure_diagnostics(securities_columns=securities_columns),
        }

    if claim_type == "exists":
        return E4_VERIFIED, {
            "claim": {"ticker": normalized_ticker, "type": "exists"},
            "checked_source": "moex.iss",
            "result": "VERIFIED",
            "security": match,
            **_failure_diagnostics(securities_columns=securities_columns),
        }

    price_query = urllib.parse.urlencode(
        {
            "iss.meta": "off",
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,LAST,MARKETPRICE,MARKETPRICE2,BOARDID,TRADEDATE,SYSTIME",
        }
    )
    price_url = (
        f"{MOEX_ISS_BASE}/engines/stock/markets/shares/securities/"
        f"{urllib.parse.quote(normalized_ticker, safe='')}.json?{price_query}"
    )
    marketdata_columns: list[str] | None = None
    marketdata_rows_count: int | None = None
    try:
        market = _get_json(
            price_url,
            timeout,
            phase="marketdata",
            raw_evidence=raw_evidence,
        )
        marketdata_table = market.get("marketdata")
        if isinstance(marketdata_table, dict) and isinstance(marketdata_table.get("columns"), list):
            marketdata_columns = [
                col for col in marketdata_table["columns"] if isinstance(col, str)
            ]
        market_rows = _table_rows(market, "marketdata")
        marketdata_rows_count = len(market_rows)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return E4_INCONCLUSIVE, {
            "reason": "MOEX_MARKETDATA_UNAVAILABLE_OR_INVALID",
            "error_type": type(exc).__name__,
            "claim": {"ticker": normalized_ticker, "type": "price", "value": claimed_value},
            "checked_source": "moex.iss",
            **_failure_diagnostics(
                exc=exc,
                securities_columns=securities_columns,
                marketdata_columns=marketdata_columns,
                marketdata_rows_count=marketdata_rows_count,
                tried_fields=PRICE_FIELDS,
                phase="marketdata",
                request_url=price_url,
            ),
        }

    candidates = [
        row
        for row in market_rows
        if isinstance(row.get("SECID"), str)
        and row["SECID"].upper() == normalized_ticker
        and isinstance(row.get("LAST"), (int, float))
        and not isinstance(row.get("LAST"), bool)
        and math.isfinite(float(row["LAST"]))
        and float(row["LAST"]) > 0
    ]
    if not candidates:
        observed_board = next(
            (
                row.get("BOARDID")
                for row in market_rows
                if isinstance(row.get("SECID"), str)
                and row["SECID"].upper() == normalized_ticker
                and isinstance(row.get("BOARDID"), str)
            ),
            None,
        )
        return E4_INCONCLUSIVE, {
            "reason": "MOEX_LAST_PRICE_UNAVAILABLE",
            "claim": {"ticker": normalized_ticker, "type": "price", "value": claimed_value},
            "checked_source": "moex.iss",
            "security": match,
            **_failure_diagnostics(
                http_status=200,
                board_id=observed_board,
                securities_columns=securities_columns,
                marketdata_columns=marketdata_columns,
                marketdata_rows_count=marketdata_rows_count,
                tried_fields=PRICE_FIELDS,
            ),
        }

    latest = candidates[0]
    last_price = float(latest["LAST"])
    claimed_price = float(claimed_value)
    relative_delta = abs(claimed_price - last_price) / last_price
    details = {
        "claim": {"ticker": normalized_ticker, "type": "price", "value": claimed_price},
        "checked_source": "moex.iss",
        "observed_last": last_price,
        "relative_delta": relative_delta,
        "tolerance": price_tolerance,
        "board_id": latest.get("BOARDID"),
        "trade_date": latest.get("TRADEDATE"),
        "market_calendar_valid": None,
        **_failure_diagnostics(
            http_status=200,
            securities_columns=securities_columns,
            marketdata_columns=marketdata_columns,
            marketdata_rows_count=marketdata_rows_count,
            tried_fields=PRICE_FIELDS,
        ),
    }
    if relative_delta > price_tolerance:
        return E4_CONTRADICTED, {**details, "result": "CONTRADICTED"}
    return E4_VERIFIED, {**details, "result": "VERIFIED"}


def verify_claim_with_moex(
    ticker: str,
    claim_type: str,
    claimed_value: float | int | None,
    *,
    timeout: float = 5.0,
    price_tolerance: float = 0.05,
    capture_raw: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Backward-compatible public verifier with opt-in exact HTTP evidence."""
    raw_evidence: list[dict[str, Any]] = []
    status, details = _verify_claim_with_moex(
        ticker,
        claim_type,
        claimed_value,
        timeout=timeout,
        price_tolerance=price_tolerance,
        raw_evidence=raw_evidence if capture_raw else None,
    )
    if capture_raw:
        details = {**details, "raw_evidence": raw_evidence}
    return status, details


# JAMP 2 adapter boundary; E4 decision semantics above are carried forward.

def verify_claim(claim: Jamp2Claim) -> ProviderResult:
    """Verify a manually constructed claim and retain every raw MOEX response."""
    if claim.type != "price":
        return ProviderResult(E4_OUT_OF_SCOPE, {"reason": "NOT_A_PRICE_CLAIM"})
    if claim.value is None:
        return ProviderResult(E4_INCONCLUSIVE, {"reason": "MISSING_VALUE"})
    if claim.type == "price" and claim.currency not in {None, "RUB"}:
        return ProviderResult(
            E4_OUT_OF_SCOPE,
            {"reason": "MOEX_PRICE_CURRENCY_OUT_OF_SCOPE", "currency": claim.currency},
        )
    status, details = verify_claim_with_moex(
        claim.ticker, claim.type, claim.value, capture_raw=True
    )
    raw_evidence = details.pop("raw_evidence", [])
    return ProviderResult(
        status,
        {
            **details,
            "source_identifiers": (
                [claim.ticker] if status in {E4_VERIFIED, E4_CONTRADICTED} else []
            ),
            "quote_currency": claim.currency or "RUB",
            "price_type": "LAST_DEAL",
        },
        raw_evidence,
    )

class MoexProvider:
    def verify_claim(self, claim: NormalizedClaim) -> ProviderResult:
        if claim.subject_kind != "equity_security":
            return ProviderResult(E4_OUT_OF_SCOPE, {"reason": "SUBJECT_KIND_NOT_SUPPORTED"})
        provider_claim = Jamp2Claim(
            claim.text,
            claim.predicate,
            claim.subject_identifier,
            claim.expected_value,
            claim.unit,
        )
        return verify_claim(provider_claim)
