"""FastAPI application factory; PostgreSQL and API-key auth are fail-closed defaults."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jamp2.api.routes import EvidenceLedger, build_router
from jamp2.evidence.multisource import MultiSourcePolicy
from jamp2.ledger.postgres import PostgresLedger
from jamp2.providers.base import Provider
from jamp2.web.routes import build_dashboard_router


def create_app(
    ledger: EvidenceLedger | None = None,
    *,
    api_key: str | None = None,
    dashboard_password: str | None = None,
    providers: dict[str, Provider] | None = None,
    code_version: str | None = None,
    policies: dict[tuple[str, str], MultiSourcePolicy] | None = None,
) -> FastAPI:
    """Create the API without silently falling back to memory or disabling auth."""
    active_ledger = ledger if ledger is not None else PostgresLedger()
    configured_api_key = api_key if api_key is not None else os.getenv("JAMP2_API_KEY")
    configured_dashboard_password = (
        dashboard_password if dashboard_password is not None
        else os.getenv("JAMP2_DASHBOARD_PASSWORD")
    )
    application = FastAPI(title="JAMP 2 Trust Layer API", version="1.0.0")
    application.include_router(build_router(
        active_ledger,
        api_key=configured_api_key,
        providers=providers,
        code_version=code_version,
        policies=policies,
    ))
    web_directory = Path(__file__).parents[1] / "web"
    application.mount(
        "/static", StaticFiles(directory=str(web_directory / "static")), name="dashboard-static"
    )
    application.include_router(build_dashboard_router(
        active_ledger,
        api_key=configured_api_key,
        dashboard_password=configured_dashboard_password,
    ))
    return application


app = create_app()
