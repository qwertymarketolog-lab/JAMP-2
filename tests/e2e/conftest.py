"""Fixtures for explicitly authorized live provider tests."""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ALLOWED_PROVIDER_HOSTS = frozenset({"api.coingecko.com", "iss.moex.com"})


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: requires real outbound provider HTTP")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live tests are opt-in; selected workflow targets must execute, not silently skip."""
    if os.getenv("JAMP_RUN_LIVE") != "1":
        skip = pytest.mark.skip(reason="live provider tests require JAMP_RUN_LIVE=1")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)
        return

    target = os.getenv("JAMP_LIVE_TARGET", "both")
    if target not in {"coingecko", "moex", "both"}:
        raise pytest.UsageError("JAMP_LIVE_TARGET must be coingecko, moex, or both")
    if target != "both":
        selected = f"test_live_{target}"
        skip = pytest.mark.skip(reason=f"scenario not selected: {target}")
        for item in items:
            if "live" in item.keywords and selected not in item.nodeid:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def network_guard():
    """Constrain provider-adapter urllib traffic to the two approved HTTPS hosts."""
    original_urlopen = urllib.request.urlopen

    def guarded_urlopen(url, *args, **kwargs):
        request_url = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        parsed = urlsplit(request_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_PROVIDER_HOSTS:
            raise RuntimeError(f"live network guard rejected URL: {request_url}")
        return original_urlopen(url, *args, **kwargs)

    urllib.request.urlopen = guarded_urlopen
    try:
        yield
    finally:
        urllib.request.urlopen = original_urlopen


@pytest.fixture(scope="session")
def live_server(network_guard):
    """Run the real FastAPI app over local HTTP, backed by the workflow PostgreSQL."""
    if os.getenv("JAMP_RUN_LIVE") != "1":
        pytest.skip("live API server is disabled")

    import uvicorn

    from jamp2.api.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, name="jamp2-live-api", daemon=True)
    thread.start()

    ready = False
    try:
        for _ in range(60):
            if not thread.is_alive():
                raise RuntimeError("JAMP API server exited before becoming ready")
            try:
                response = httpx.get("http://127.0.0.1:8765/openapi.json", timeout=1.0)
                if response.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not ready:
            raise RuntimeError("JAMP API server did not become ready within 30 seconds")
        yield "http://127.0.0.1:8765"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def http_client(live_server):
    """HTTP client exercises the running API through its actual local TCP boundary."""
    with httpx.Client(
        base_url=live_server,
        timeout=45.0,
        headers={"X-API-Key": os.environ["JAMP2_API_KEY"]},
    ) as client:
        yield client


@pytest.fixture
def db_engine():
    """Independent PostgreSQL SELECT connection; NullPool avoids cross-loop reuse."""
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        import asyncio

        asyncio.run(engine.dispose())


@pytest.fixture(scope="session")
def artifact_dir() -> Path:
    path = Path("tests/e2e/artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path
