"""Blocklist / allowlist subscription management (spec §3, §12).

AdGuardHub tracks the subscription URL and its enabled state only — resolving the
700k-domain lists behind them stays AdGuard's job.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..deps import CurrentUser, SessionDep
from ..models import FilterList, ListKind, PayloadKind
from ..schemas import (
    FilterListCreate,
    FilterListOut,
    FilterListUpdate,
    FilterSizesOut,
    ListSizeOut,
    RefreshReportOut,
    RefreshResultOut,
)
from ..services import filtersizes
from ..services.sync import schedule_sync
from ..services.versions import record as _record

router = APIRouter(prefix="/api/filter-lists", tags=["filter-lists"])

LIST_KINDS = (PayloadKind.filters,)


@router.get("", response_model=list[FilterListOut])
async def list_filter_lists(
    user: CurrentUser, session: SessionDep, kind: ListKind | None = None
) -> list[FilterList]:
    statement = select(FilterList).order_by(FilterList.id.asc())
    if kind is not None:
        statement = statement.where(FilterList.kind == kind.value)
    result = await session.execute(statement)
    return list(result.scalars().all())


@router.get("/sizes", response_model=FilterSizesOut)
async def list_sizes(user: CurrentUser) -> FilterSizesOut:
    """How many rules each subscription holds, collected from the instances.

    Declared before ``/{list_id}`` has no bearing here — that path takes no GET —
    but the order is kept deliberately so a later read route cannot swallow it.
    """
    sizes = await filtersizes.cached()
    return FilterSizesOut(
        lists=[
            ListSizeOut(
                url=item.url,
                kind=item.kind,
                rules_count=item.rules_count,
                agreed=item.agreed,
                last_updated=item.last_updated or None,
                per_instance=[
                    {
                        "instance_id": entry.instance_id,
                        "instance_name": entry.instance_name,
                        "rules_count": entry.rules_count,
                        "last_updated": entry.last_updated or None,
                    }
                    for entry in item.per_instance
                ],
            )
            for item in sizes.lists
        ],
        total_rules=sizes.total_rules,
        instances_reporting=sizes.instances_reporting,
        instances_total=sizes.instances_total,
    )


@router.post("/refresh", response_model=RefreshReportOut)
async def refresh_lists(user: CurrentUser) -> RefreshReportOut:
    """Ask every node to re-download its subscriptions now.

    The hub cannot do this itself — it holds URLs, not contents — so the button
    is a fan-out, and the answer says what each node managed rather than one
    number that hides an unreachable one. The headline is the largest a single
    node updated, not the sum: these are one set of subscriptions replicated
    everywhere, so adding the nodes together would count the same list twice.
    """
    results = await filtersizes.refresh_all()
    return RefreshReportOut(
        instances=[
            RefreshResultOut(
                instance_id=item.instance_id,
                instance_name=item.instance_name,
                updated=item.updated,
                error=item.error,
            )
            for item in results
        ],
        updated=max((item.updated for item in results), default=0),
    )


@router.post("", response_model=FilterListOut, status_code=status.HTTP_201_CREATED)
async def create_filter_list(
    payload: FilterListCreate, user: CurrentUser, session: SessionDep
) -> FilterList:
    item = FilterList(
        name=payload.name, url=payload.url, kind=payload.kind.value, enabled=payload.enabled
    )
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That subscription already exists") from exc
    await record_version(session, f"subscription added: {item.url}", user)
    schedule_sync(LIST_KINDS, f"subscription added: {item.url}")
    filtersizes.invalidate()
    return item


@router.patch("/{list_id}", response_model=FilterListOut)
async def update_filter_list(
    list_id: int, payload: FilterListUpdate, user: CurrentUser, session: SessionDep
) -> FilterList:
    item = await session.get(FilterList, list_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(item, field, value)
    await session.commit()
    await record_version(session, f"subscription updated: {item.url}", user)
    schedule_sync(LIST_KINDS, f"subscription updated: {item.url}")
    filtersizes.invalidate()
    return item


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_filter_list(list_id: int, user: CurrentUser, session: SessionDep) -> None:
    item = await session.get(FilterList, list_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    url = item.url
    await session.delete(item)
    await session.commit()
    await record_version(session, f"subscription removed: {url}", user)
    schedule_sync(LIST_KINDS, f"subscription removed: {url}")
    filtersizes.invalidate()


async def record_version(session: SessionDep, label: str, user: CurrentUser) -> None:
    """Snapshot the central state so the change can be diffed and rolled back."""
    await _record(session, label, author=user.username)
