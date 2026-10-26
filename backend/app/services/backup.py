"""Export and restore the hub's configuration as one file.

Everything AdGuardHub knows lives in a single SQLite file. Lose it — a wiped
volume, a mistyped ``docker run -v`` — and the rules, subscriptions, instance
settings and the whole version history go with it, leaving a re-import from
whichever node happens to still have them as the only way back. This turns that
into a file the operator holds.

The document is deliberately the same snapshot the version history already
records, so export, rollback and restore all move the same shape of data and
there is only one thing to keep correct.

Three rules govern what goes in and how it comes back:

* **No secrets, ever.** An export is downloaded through a browser, and an
  instance password must never be serialised there — not in the clear and not as
  ciphertext, which would only be one leaked ADGUARDHUB_SECRET_KEY away from the
  same thing. Instances are exported without their password, and come back
  needing it typed again.
* **Validate before destroying.** Restoring replaces the rule and subscription
  tables wholesale. A malformed file must be rejected while the hub still has
  its data, not halfway through replacing it.
* **Never overwrite an instance that already works.** A restore onto a running
  hub adds what is missing and leaves the rest alone, so it cannot strip the
  credentials off a node that was connected fine a moment ago.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import ADAPTERS
from ..models import Instance, ListKind, RuleKind, RuleOrigin
from ..schemas import normalise_instance_url
from .config import build_snapshot, restore_snapshot

FORMAT = "adguardhub-backup"
FORMAT_VERSION = 1

# Instance columns that describe *which* node it is, as opposed to how to log in
# or what happened to it last. Status, errors and timestamps are deliberately
# absent: they describe a moment that has passed, and restoring them would put
# stale claims on screen before anything has been probed.
INSTANCE_FIELDS = ("name", "base_url", "adapter", "username", "verify_tls", "enabled")


class BackupError(ValueError):
    """The file is not a backup this hub can restore, with the reason why."""


async def export_document(session: AsyncSession, *, hub_version: str) -> dict[str, Any]:
    result = await session.execute(select(Instance).order_by(Instance.id.asc()))
    instances = [
        {field: getattr(row, field) for field in INSTANCE_FIELDS}
        for row in result.scalars().all()
    ]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "hub_version": hub_version,
        # Passwords are not here, and their absence is part of the format rather
        # than an omission — see the module docstring.
        "instances": instances,
        "snapshot": await build_snapshot(session),
    }


def validate(payload: Any) -> dict[str, Any]:
    """Check a parsed file thoroughly, or raise BackupError saying what is wrong.

    Everything a restore will touch is checked here, before the restore has
    deleted anything. The messages name the problem rather than the code that
    found it, because the person reading them is holding a file they hoped was a
    backup and needs to know whether it is.
    """
    if not isinstance(payload, dict):
        raise BackupError("This is not an AdGuardHub backup file.")
    if payload.get("format") != FORMAT:
        raise BackupError("This is not an AdGuardHub backup file.")

    version = payload.get("format_version")
    if version != FORMAT_VERSION:
        raise BackupError(
            f"This backup is in format version {version!r}, and this hub reads "
            f"version {FORMAT_VERSION}. Restore it with the version of AdGuardHub "
            "that wrote it."
        )

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise BackupError("The backup has no configuration in it.")
    for key in ("rules", "filter_lists"):
        if not isinstance(snapshot.get(key, []), list):
            raise BackupError(f"The backup's {key!r} is damaged.")
    if not isinstance(snapshot.get("sections", {}), dict):
        raise BackupError("The backup's 'sections' is damaged.")
    if not isinstance(payload.get("instances", []), list):
        raise BackupError("The backup's 'instances' is damaged.")

    # The shape of the lists was checked; now what is in them. A restore reads
    # each entry with .get() and stores what it finds, so an entry that is not
    # an object crashed it halfway through, and a value the rest of the hub
    # never produces — a rule kind of "sideways", an instance with an ftp://
    # address or an adapter that does not exist — was written and then broke
    # whatever read it back.
    for index, entry in enumerate(snapshot.get("rules") or []):
        what = f"rule {index + 1}"
        _expect_object(entry, what)
        _expect_string(entry, "text", what)
        _expect_choice(entry, "kind", RuleKind, what)
        _expect_choice(entry, "origin", RuleOrigin, what)
    for index, entry in enumerate(snapshot.get("filter_lists") or []):
        what = f"subscription {index + 1}"
        _expect_object(entry, what)
        _expect_string(entry, "url", what)
        _expect_string(entry, "name", what)
        _expect_choice(entry, "kind", ListKind, what)
    for name, entry in (snapshot.get("sections") or {}).items():
        what = f"section {name!r}"
        _expect_object(entry, what)
        data = entry.get("data")
        if data is not None and not isinstance(data, dict):
            raise BackupError(f"The backup's {what} is damaged.")
    for index, entry in enumerate(payload.get("instances") or []):
        what = f"instance {index + 1}"
        _expect_object(entry, what)
        for key in ("name", "base_url", "username", "adapter"):
            _expect_string(entry, key, what)
        base_url = str(entry.get("base_url") or "").strip()
        if base_url:
            try:
                normalise_instance_url(base_url)
            except ValueError as exc:
                raise BackupError(
                    f"The backup's {what} has an invalid address: {base_url!r}."
                ) from exc
        adapter = entry.get("adapter")
        if adapter is not None and adapter not in ADAPTERS:
            raise BackupError(
                f"The backup's {what} names an adapter this hub does not have: {adapter!r}."
            )
    return payload


def _expect_object(entry: Any, what: str) -> None:
    if not isinstance(entry, dict):
        raise BackupError(f"The backup's {what} is damaged.")


def _expect_string(entry: dict[str, Any], key: str, what: str) -> None:
    """Absent or null is fine — the restore has a default — but not another type."""
    value = entry.get(key)
    if value is not None and not isinstance(value, str):
        raise BackupError(f"The backup's {what} has a {key!r} that is not text.")


def _expect_choice(entry: dict[str, Any], key: str, choices: type[StrEnum], what: str) -> None:
    value = entry.get(key)
    if value is None:
        return
    allowed = [item.value for item in choices]
    if value not in allowed:
        raise BackupError(
            f"The backup's {what} has a {key!r} of {value!r}; expected one of "
            f"{', '.join(allowed)}."
        )


async def import_document(session: AsyncSession, payload: dict[str, Any]) -> dict[str, int]:
    """Replace the central configuration, and add back any missing instances."""
    validate(payload)
    counts = await restore_snapshot(session, payload["snapshot"])

    rows = (await session.execute(select(Instance.base_url, Instance.name))).all()
    urls = {url for url, _ in rows}
    names = {name for _, name in rows}

    added = 0
    for entry in payload.get("instances") or []:
        if not isinstance(entry, dict):
            continue
        base_url = str(entry.get("base_url") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not base_url or not name or base_url in urls:
            # Already here: leave it exactly as it is. It may well be connected
            # right now, and the backup has no password to put back anyway.
            continue
        name = _free_name(name, names)
        urls.add(base_url)
        names.add(name)
        session.add(
            Instance(
                name=name,
                base_url=base_url,
                adapter=str(entry.get("adapter") or "adguard"),
                username=str(entry.get("username") or ""),
                verify_tls=bool(entry.get("verify_tls", True)),
                enabled=bool(entry.get("enabled", True)),
            )
        )
        added += 1

    await session.commit()
    # Every restored instance needs its password typed in, since the backup
    # deliberately carries none. Saying how many is what turns that from a
    # surprise into a task.
    return {**counts, "instances_added": added, "instances_need_password": added}


def _free_name(name: str, taken: set[str]) -> str:
    """Instance names are unique in the schema, so a clash would abort the restore.

    A name already in use with a *different* URL is a genuinely different node,
    so the restored one is suffixed rather than dropped or merged: losing an
    instance silently would be worse than an operator renaming it afterwards.
    """
    if name not in taken:
        return name
    for suffix in range(2, 1000):
        candidate = f"{name} ({suffix})"
        if candidate not in taken:
            return candidate
    raise BackupError(f"Cannot find a free name for the restored instance {name!r}.")
