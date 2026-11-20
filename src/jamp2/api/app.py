"""FastAPI application factory; PostgreSQL and API-key auth are fail-closed defaults."""

from __future__ import annotations

from fastapi import FastAPI

from jamp2.api.routes import EvidenceLedger, build_router
from jamp2.evidence.multisource import MultiSourcePolicy
from jamp2.ledger.postgres import PostgresLedger
from jamp2.providers.base import Provider


def create_app(
    ledger: EvidenceLedger | None = None,
    *,
    api_key: str | None = None,
    providers: dict[str, Provider] | None = None,
    code_version: str | None = None,
    policies: dict[tuple[str, str], MultiSourcePolicy] | None = None,
) -> FastAPI:
    """Create the API without silently falling back to memory or disabling auth."""
    application = FastAPI(title="JAMP 2 Trust Layer API", version="1.0.0")
    application.include_router(
        build_router(
            ledger if ledger is not None else PostgresLedger(),
            api_key=api_key,
            providers=providers,
            code_version=code_version,
            policies=policies,
        )
    )
    return application


app = create_app()
