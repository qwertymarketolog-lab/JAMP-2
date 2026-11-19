"""FastAPI application factory; PostgreSQL is the default and only runtime store."""

from __future__ import annotations

from fastapi import FastAPI

from jamp2.api.routes import EvidenceLedger, build_router
from jamp2.ledger.postgres import PostgresLedger


def create_app(ledger: EvidenceLedger | None = None) -> FastAPI:
    """Create the API without silently falling back to in-memory storage."""
    application = FastAPI(title="JAMP 2 Trust Layer API", version="1.0.0")
    application.include_router(build_router(ledger or PostgresLedger()))
    return application


app = create_app()
