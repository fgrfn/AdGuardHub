"""FastAPI application: API, background workers and the built frontend."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.responses import Response
from starlette.types import Scope

from .api import (
    auth,
    backup,
    blocklists,
    config,
    control,
    diagnostics,
    instances,
    ops,
    querylog,
    rules,
)
from .api import settings as settings_api
from .config import get_settings
from .db import DataDirError, dispose_db, init_db, session_scope
from .deps import admin_exists
from .logging_setup import configure as configure_logging
from .models import User
from .runtime import using_ephemeral_secret
from .security import SecretKeyError, hash_password, verify_password
from .services import hubsettings
from .services.driftarchive import configure as configure_drift_archive
from .services.events import bus
from .services.querylog import querylog_worker
from .services.reconcile import reconcile_worker
from .services.sync import retry_worker
from .version import VERSION

_settings = get_settings()
configure_logging(
    _settings.log_level,
    log_file=_settings.log_file,
    max_bytes=_settings.log_file_max_bytes,
    backups=_settings.log_file_backups,
)
# After the handlers exist, so a path that cannot be opened is reported through
# them rather than into a logger nothing is listening to yet.
configure_drift_archive(
    _settings.drift_log_path,
    max_bytes=_settings.drift_log_max_bytes,
    backups=_settings.drift_log_backups,
)
logger = logging.getLogger("adguardhub")


async def bootstrap_admin() -> None:
    """Create the admin account from the environment, if configured and none exists."""
    config = get_settings()
    if not (config.admin_username and config.admin_password):
        return
    async with session_scope() as session:
        if await admin_exists(session):
            existing = await session.execute(
                select(User).where(User.username == config.admin_username)
            )
            user = existing.scalars().first()
            # Only when the password actually moved. bcrypt salts every hash, so
            # rehashing an unchanged password on every start would still change
            # the hash — and every session cookie is tied to it, so each restart
            # would sign everyone out of a hub whose password never changed.
            if user is not None and not verify_password(config.admin_password, user.password_hash):
                user.password_hash = hash_password(config.admin_password)
                await session.commit()
            return
        session.add(
            User(
                username=config.admin_username,
                password_hash=hash_password(config.admin_password),
            )
        )
        await session.commit()
        logger.info("Created admin account '%s' from the environment", config.admin_username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Resolved first, and deliberately not lazily: a refused key has to stop the
    # start here, with one readable line, rather than surfacing later as a 500 on
    # whichever request happened to need the crypto first.
    try:
        ephemeral = using_ephemeral_secret()
    except SecretKeyError as exc:
        logger.error("Startup aborted: %s", exc)
        raise
    if ephemeral:
        logger.warning(
            "No usable key store, so a random key is in use. Sessions and stored instance "
            "credentials will NOT survive a restart — see the warning above for why."
        )
    # Printed before anything can fail, so a pasted log always identifies the build
    # and the user it runs as — the two things a startup problem turns on.
    logger.info(
        "AdGuardHub %s starting as uid=%d gid=%d, data dir %s",
        VERSION,
        os.getuid(),
        os.getgid(),
        get_settings().data_dir,
    )
    try:
        await init_db()
    except DataDirError as exc:
        # Surface the remedy as a single readable line before uvicorn's traceback.
        logger.error("Startup aborted: %s", exc)
        raise
    await bootstrap_admin()
    # Seed the runtime settings from the environment, then let the UI own them.
    async with session_scope() as session:
        await hubsettings.load(session)

    stop = asyncio.Event()
    workers = [
        asyncio.create_task(retry_worker(stop), name="retry"),
        asyncio.create_task(reconcile_worker(stop), name="reconcile"),
        asyncio.create_task(querylog_worker(stop), name="querylog"),
    ]
    app.state.stop_event = stop
    try:
        yield
    finally:
        stop.set()
        # Before the workers: an open event stream is a request uvicorn waits
        # for, and one that never ends on its own holds the whole shutdown until
        # systemd loses patience. See EventBus.close.
        await bus.close()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await dispose_db()


app = FastAPI(title="AdGuardHub", version=VERSION, lifespan=lifespan)

app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(rules.router)
app.include_router(blocklists.router)
app.include_router(querylog.router)
app.include_router(settings_api.router)
app.include_router(ops.router)
app.include_router(config.router)
app.include_router(config.versions_router)
app.include_router(backup.router)
app.include_router(diagnostics.router)
# Registered before the SPA fallback so /control/* is not swallowed by it.
app.include_router(control.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}


# How the two halves of a built frontend may be cached. Getting this wrong is
# not a performance question, it is an outage: index.html names its scripts by
# content hash, and an upgrade deletes the old ones. A browser holding a stale
# index.html therefore asks for bundles that no longer exist and renders nothing
# — the hub is up, answering, and the page is blank.
#
# Without a Cache-Control header a browser is free to invent one (RFC 9111
# §4.2.2, commonly a tenth of the file's age), so it can serve that stale page
# for a long time without ever asking. It has to be told.
INDEX_CACHE = "no-cache"
# Hashed names, so an old name can never refer to new content. These are the
# files worth caching hard: without it every page load refetches the bundle.
ASSET_CACHE = "public, max-age=31536000, immutable"


class HashedAssets(StaticFiles):
    """The build's own output, under /assets, named by content hash."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["cache-control"] = ASSET_CACHE
        return response


def mount_frontend(application: FastAPI) -> None:
    """Serve the built React app, falling back to index.html for client-side routes."""
    static_dir = get_settings().static_dir
    index = os.path.join(static_dir, "index.html")
    if not os.path.isfile(index):
        logger.info("No built frontend at %s — serving the API only", static_dir)
        return

    assets = os.path.join(static_dir, "assets")
    if os.path.isdir(assets):
        application.mount("/assets", HashedAssets(directory=assets), name="assets")

    @application.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> FileResponse | JSONResponse:
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = os.path.normpath(os.path.join(static_dir, path))
        if (
            path
            and os.path.isfile(candidate)
            and os.path.commonpath([os.path.abspath(static_dir), os.path.abspath(candidate)])
            == os.path.abspath(static_dir)
        ):
            # Everything reachable here — the favicon, the logo — carries no hash
            # in its name either, so it is revalidated like index.html rather
            # than cached under a name that outlives its contents.
            return FileResponse(candidate, headers={"cache-control": INDEX_CACHE})
        return FileResponse(index, headers={"cache-control": INDEX_CACHE})


mount_frontend(app)
