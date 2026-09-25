"""Aggregated query log endpoints and the SSE stream (spec §9)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..deps import CurrentUser
from ..schemas import QueryLogEntryOut
from ..services import querylog
from ..services.events import bus

router = APIRouter(prefix="/api", tags=["querylog"])


@router.get("/querylog", response_model=list[QueryLogEntryOut])
async def get_querylog(
    _: CurrentUser,
    limit: int = Query(200, ge=1, le=2000),
    search: str = "",
    instance: str = "",
    status: str = Query("", pattern="^(blocked|allowlisted|processed)?$"),
    filter_list: str = "",
    blocked_only: bool = False,
) -> list[dict[str, object]]:
    return await querylog.buffer.snapshot(
        limit,
        search=search,
        instance=instance,
        status=status,
        filter_list=filter_list,
        blocked_only=blocked_only,
    )


@router.get("/querylog/lists")
async def querylog_lists(_: CurrentUser) -> dict[str, list[str]]:
    """The lists the buffered entries actually name, for the *From list* filter."""
    return {"lists": await querylog.buffer.filter_lists()}


@router.post("/querylog/refresh")
async def refresh_querylog(_: CurrentUser) -> dict[str, int]:
    """Poll every instance right now instead of waiting for the next tick."""
    return {"new_entries": await querylog.poll_once()}


@router.get("/stream")
async def stream(request: Request, _: CurrentUser) -> StreamingResponse:
    """Server-sent events: query log entries, instance status, drift and sync results."""

    async def event_source():
        async for chunk in bus.subscribe():
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
