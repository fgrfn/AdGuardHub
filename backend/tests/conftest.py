from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Isolate the test run from any real deployment before app modules read settings.
os.environ["ADGUARDHUB_DATA_DIR"] = tempfile.mkdtemp(prefix="adguardhub-tests-")
os.environ["ADGUARDHUB_SECRET_KEY"] = "test-secret-key"
os.environ.pop("ADGUARDHUB_ADMIN_USERNAME", None)
os.environ.pop("ADGUARDHUB_ADMIN_PASSWORD", None)

import httpx  # noqa: E402

from app import db  # noqa: E402
from app.adapters import ADAPTERS  # noqa: E402
from app.adapters import session as adapter_session  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import (
    driftarchive,  # noqa: E402
    filtersizes,  # noqa: E402
    hubsettings,  # noqa: E402
)
from app.services.aggregate import invalidate_stats_cache  # noqa: E402
from app.services.querylog import buffer  # noqa: E402
from app.services.sync import drain_background, reset_push_locks  # noqa: E402

from .fakes import FakeAdapter  # noqa: E402


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch) -> AsyncIterator[None]:
    """Point the engine at a per-test SQLite file and create the schema.

    The app's lifespan is deliberately not run: its background workers would poll
    real instances, and each test drives the pieces it cares about explicitly.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("ADGUARDHUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADGUARDHUB_SECRET_KEY", "test-secret-key")
    await db.dispose_db()
    await db.init_db()
    await buffer.clear()
    # Cached AdGuard sessions are process-wide; keep them from leaking between tests.
    adapter_session.store.reset()
    # So are the per-node push locks, and a lock is bound to the first event
    # loop that waits on it — which is the previous test's.
    reset_push_locks()
    # Runtime settings are cached process-wide; reload them per test.
    hubsettings._cache = None
    # So is the aggregated-statistics cache — one test's numbers must not be
    # served to the next.
    invalidate_stats_cache()
    filtersizes.invalidate()
    # The drift archive is a file handler opened once at import, against the data
    # directory the whole session shares. Pointed at this test's directory
    # instead, so one test's findings are never read back by the next — and so
    # the tests that read it are not looking at everything the run has written.
    driftarchive.configure(str(tmp_path / "drift.log"), max_bytes=1024 * 1024, backups=1)
    yield
    await drain_background()
    await db.dispose_db()
    get_settings.cache_clear()


@pytest.fixture
def fake_adapter(monkeypatch) -> type[FakeAdapter]:
    """Swap the AdGuard adapter for an in-memory double."""
    FakeAdapter.reset()
    monkeypatch.setitem(ADAPTERS, "adguard", FakeAdapter)
    return FakeAdapter


@pytest_asyncio.fixture
async def client(fresh_db, fake_adapter) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def auth_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """A client that has completed first-run setup and holds a session cookie."""
    response = await client.post(
        "/api/auth/setup", json={"username": "admin", "password": "supersecret"}
    )
    assert response.status_code == 200, response.text
    return client
