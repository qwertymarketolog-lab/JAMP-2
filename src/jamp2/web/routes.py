"""Private server-rendered Dashboard; the browser never receives the API key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import TemplateNotFound
from pydantic import ValidationError
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from jamp2.api.routes import EvidenceLedger
from jamp2.api.schemas import ClaimVerifyRequest
from jamp2.evidence.model import EvidenceRecord, IntegrityViolation
from jamp2.ledger.postgres import LedgerUnavailable

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic(auto_error=False)
STATUS_CLASS = {
    "VERIFIED": "status-verified", "CONTRADICTED": "status-contradicted",
    "INCONCLUSIVE": "status-inconclusive", "OUT_OF_SCOPE": "status-out-of-scope",
    "AMBIGUOUS": "status-ambiguous",
}
STATUS_TEXT = {
    "VERIFIED": "Проверка выполнена, результат положительный",
    "CONTRADICTED": "Источник противоречит заявленному значению",
    "INCONCLUSIVE": "Проверка выполнена, результат неопределён",
    "OUT_OF_SCOPE": "Утверждение вне области поддержки провайдера",
    "AMBIGUOUS": "Конфликтующие наблюдения сохранены",
}


def secure(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    return response


def raise_ledger_error(exc: Exception) -> None:
    if isinstance(exc, LedgerUnavailable):
        raise HTTPException(503, "LEDGER_UNAVAILABLE") from exc
    if isinstance(exc, IntegrityViolation):
        raise HTTPException(500, "INTEGRITY_VIOLATION") from exc
    raise exc


def claim_snapshot(records: list[EvidenceRecord]) -> dict[str, Any]:
    for record in records:
        if isinstance(record.parsed_value, dict):
            claim = record.parsed_value.get("claim")
            if isinstance(claim, dict):
                return claim
    return {}


def raw_body_text(record: EvidenceRecord) -> str:
    """Return the exact decoded bytes without JSON re-serialization.

    The Dashboard must display the same bytes whose SHA-256 is shown as the
    integrity digest. Re-parsing and re-serializing JSON would silently drop
    duplicate keys, reorder fields, change whitespace, and alter number
    formatting, producing a mismatch between what the user sees and what the
    hash verifies.
    """
    try:
        return record.raw_body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "Base64: " + base64.b64encode(record.raw_body_bytes).decode("ascii")


def build_dashboard_router(
    ledger: EvidenceLedger, *, api_key: str | None, dashboard_password: str | None
) -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    async def require_dashboard(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
    ) -> None:
        if not dashboard_password:
            raise HTTPException(503, "DASHBOARD_AUTH_NOT_CONFIGURED")
        valid = (
            credentials is not None
            and secrets.compare_digest(credentials.username, "pilot")
            and hmac.compare_digest(
                credentials.password.encode(), dashboard_password.encode()
            )
        )
        if not valid:
            raise HTTPException(
                401, "DASHBOARD_UNAUTHORIZED", headers={"WWW-Authenticate": "Basic"}
            )

    async def require_same_origin_post(request: Request) -> None:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if (
            request.headers.get("origin") != expected
            or request.headers.get("x-jamp-dashboard-request") != "1"
        ):
            raise HTTPException(403, "DASHBOARD_CSRF_CHECK_FAILED")

    def render(request: Request, name: str, context: dict[str, Any]) -> Response:
        try:
            response = templates.TemplateResponse(
                request=request, name=name, context=context
            )
        except TemplateNotFound as exc:
            raise HTTPException(500, "DASHBOARD_TEMPLATE_MISSING") from exc
        return secure(response)

    async def call_internal_api(
        request: Request, method: str, path: str,
        *, body: dict[str, Any] | None = None, idempotency_key: str | None = None,
    ) -> httpx.Response:
        if not api_key:
            raise HTTPException(503, "API_KEY_NOT_CONFIGURED")
        headers = {"X-API-Key": api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        transport = httpx.ASGITransport(app=request.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=f"{request.url.scheme}://{request.url.netloc}",
        ) as client:
            return await client.request(method, path, json=body, headers=headers)

    @router.get("/", response_class=HTMLResponse)
    async def home(
        request: Request, _: None = Depends(require_dashboard)
    ) -> Response:
        return render(request, "index.html", {"request": request})

    @router.post("/claims/verify")
    async def submit_claim(
        request: Request,
        _: None = Depends(require_dashboard),
        __: None = Depends(require_same_origin_post),
    ) -> Response:
        try:
            payload = ClaimVerifyRequest.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            detail = (
                jsonable_encoder(exc.errors())
                if isinstance(exc, ValidationError)
                else "INVALID_JSON"
            )
            return secure(JSONResponse(
                {"error": "VALIDATION_ERROR", "detail": detail}, status_code=422
            ))
        valid_pairs = {("crypto_asset", "USD"), ("equity_security", "RUB")}
        if (payload.subject.kind, payload.unit) not in valid_pairs:
            return secure(JSONResponse(
                {"error": "UNSUPPORTED_SUBJECT_UNIT_COMBINATION"}, status_code=422
            ))
        upstream = await call_internal_api(
            request, "POST", "/v1/claims/verify",
            body=payload.model_dump(mode="json"), idempotency_key=str(uuid.uuid4()),
        )
        try:
            result = upstream.json()
        except ValueError:
            result = {"error": "UPSTREAM_INVALID_RESPONSE"}
        if upstream.status_code == 201 and isinstance(result, dict):
            verdict_id = result.get("verdict_id")
            if isinstance(verdict_id, str) and verdict_id:
                return secure(JSONResponse(
                    {"redirect_url": f"/verdicts/{verdict_id}"}, status_code=201
                ))
            return secure(JSONResponse({"error": "VERDICT_ID_MISSING"}, status_code=502))
        error = (
            result.get("error", "CLAIM_VERIFICATION_FAILED")
            if isinstance(result, dict)
            else "CLAIM_VERIFICATION_FAILED"
        )
        detail = result.get("detail") if isinstance(result, dict) else None
        return secure(JSONResponse(
            {"error": error, "detail": detail}, status_code=upstream.status_code
        ))

    @router.get("/verdicts/{verdict_id}", response_class=HTMLResponse)
    async def verdict_detail(
        request: Request, verdict_id: str, _: None = Depends(require_dashboard)
    ) -> Response:
        try:
            verdict = await ledger.get_verdict(verdict_id)
            if verdict is None:
                raise HTTPException(404, "VERDICT_NOT_FOUND")
            records = await ledger.list_by_claim(verdict.claim_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise_ledger_error(exc)
            raise
        by_id = {record.evidence_id: record for record in records}
        selected = [by_id[item] for item in verdict.evidence_ids if item in by_id]
        if len(selected) != len(verdict.evidence_ids):
            raise HTTPException(500, "VERDICT_EVIDENCE_LINK_MISSING")
        return render(request, "verdict.html", {
            "request": request, "verdict": verdict,
            "status_class": STATUS_CLASS.get(verdict.status, "status-unknown"),
            "status_text": STATUS_TEXT.get(
                verdict.status, "Неизвестный статус: требуется отдельная проверка"
            ),
            "claim": claim_snapshot(selected), "evidence_records": selected,
        })

    @router.post("/verdicts/{verdict_id}/replay")
    async def replay(
        request: Request, verdict_id: str,
        _: None = Depends(require_dashboard),
        __: None = Depends(require_same_origin_post),
    ) -> Response:
        upstream = await call_internal_api(
            request, "POST", f"/v1/verdicts/{verdict_id}/replay"
        )
        try:
            result = upstream.json()
        except ValueError:
            result = {"error": "UPSTREAM_INVALID_RESPONSE"}
        return secure(JSONResponse(result, status_code=upstream.status_code))

    @router.get("/evidence/{evidence_id}", response_class=HTMLResponse)
    async def evidence_detail(
        request: Request, evidence_id: str, _: None = Depends(require_dashboard)
    ) -> Response:
        try:
            record = await ledger.get(evidence_id)
        except Exception as exc:
            raise_ledger_error(exc)
            raise
        if record is None:
            raise HTTPException(404, "EVIDENCE_NOT_FOUND")
        digest = hashlib.sha256(record.raw_body_bytes).hexdigest()
        if not hmac.compare_digest(digest, record.raw_sha256):
            raise HTTPException(500, "RAW_BODY_DIGEST_MISMATCH")
        return render(request, "evidence.html", {
            "request": request, "record": record,
            "raw_body_text": raw_body_text(record), "integrity_verified": True,
        })

    return router
