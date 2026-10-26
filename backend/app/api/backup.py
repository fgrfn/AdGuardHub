"""Download the hub's configuration as a file, and put it back."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..deps import CurrentUser, SessionDep
from ..services import backup as backup_service
from ..services import versions as version_service
from ..services.sync import ALL_KINDS, schedule_sync
from ..version import VERSION

router = APIRouter(prefix="/api/backup", tags=["backup"])

# A backup of a large rule set is a few megabytes at worst, and this endpoint is
# handed whatever a browser was pointed at. Parsing an arbitrarily large body to
# find out it was a film would be the hub's problem, not the sender's.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


async def _read_limited_body(request: Request) -> bytes:
    """Read a request without buffering more than the documented upload limit."""
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "That file is too large to be an AdGuardHub backup.",
                )
        except ValueError:
            # An intermediary may omit or mangle the advisory header. The stream
            # limit below remains authoritative in either case.
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "That file is too large to be an AdGuardHub backup.",
            )
        body.extend(chunk)
    return bytes(body)


@router.get("")
async def download(_: CurrentUser, session: SessionDep) -> JSONResponse:
    """The whole central configuration, as one JSON file."""
    document = await backup_service.export_document(session, hub_version=VERSION)
    stamp = str(document["created_at"])[:10]
    return JSONResponse(
        content=document,
        headers={
            "Content-Disposition": f'attachment; filename="adguardhub-backup-{stamp}.json"'
        },
    )


@router.post("/restore")
async def restore(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    push: bool = True,
) -> dict[str, Any]:
    """Replace the central configuration with the contents of a backup file.

    The file arrives as the raw request body rather than a multipart upload: it
    is a JSON document either way, the browser is the one place already holding
    it as text, and this keeps both the size check and the parse error in our
    hands instead of behind a framework's default message.
    """
    raw = await _read_limited_body(request)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That file is not valid JSON."
        ) from exc

    # Checked in full before anything is written, so a bad file leaves the hub
    # exactly as it was rather than half-replaced.
    try:
        backup_service.validate(payload)
    except backup_service.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # No snapshot is taken of the state about to be replaced: every change already
    # records one, so the state before a restore is by construction the newest
    # version in the history, and record() would drop an identical second copy
    # anyway. Restoring the wrong file is undone by rolling back to it.
    try:
        counts = await backup_service.import_document(session, payload)
    except backup_service.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await version_service.record(
        session, "restored from a backup file", author=user.username, kind="restore"
    )
    if push:
        schedule_sync(ALL_KINDS, "restored from a backup file")
    return {**counts, "pushed": push}
