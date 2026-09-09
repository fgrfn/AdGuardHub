"""The drift log's archive on disk, and how it is read back.

The drift log in the database answers "what is wrong now". It is capped at 500
rows, a repeated finding is folded onto one entry, the rule lists in it are
trimmed to 25 items, and *Clear log* empties it on purpose. Every one of those
is right for a live view and wrong for evidence: by the time somebody sits down
to work out when a fault started, the rows that would have said so are the ones
that scrolled off.

So each finding is also appended here, once per pass, untrimmed, in the order it
happened. One JSON object per line, which is the format that survives being
grepped, tailed, piped into `jq` and pasted into an issue — and which a later
version can add a field to without invalidating what is already written.

**On by default**, unlike the application log file, and that is the whole point:
an archive nobody switched on is empty exactly when they discover they needed
it. It costs a line only when something drifts, so a healthy hub writes nothing
for weeks. ``ADGUARDHUB_DRIFT_LOG_ENABLED=false`` turns it off for a deployment
that would rather not write to its flash at all.

Rotation is handled by ``RotatingFileHandler`` rather than by hand: it already
solves the size cap, the rename dance and the locking, and it is in the standard
library. The logger it hangs on does not propagate, so none of this reaches the
application log, the in-memory buffer or stderr — three places that would each
be the wrong home for a wall of JSON.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Its own logger, deliberately not under the app's tree — see the module docstring.
ARCHIVE_LOGGER = "adguardhub.driftarchive"

#: How many entries one read serves at most. The interface pages through with a
#: cursor rather than asking for everything: an archive is allowed to be large,
#: and that is the reason it exists.
MAX_PAGE = 200

_archive: logging.Logger | None = None
_paths: list[str] = []


@dataclass(frozen=True, slots=True)
class Entry:
    """One archived finding, as it will be served to the interface."""

    #: Position from the newest end, so a cursor stays valid as the file grows:
    #: numbering from the start would renumber everything on rotation.
    offset: int
    at: str
    instance: str
    payload_kind: str
    summary: str
    corrected: bool
    took_ms: int
    details: dict[str, Any]


def configure(path: str, *, max_bytes: int, backups: int) -> str:
    """Point the archive at ``path``. Safe to call more than once.

    Returns the path in use, or an empty string when the archive could not be
    opened — which is reported and then survived. A hub that cannot write its
    archive still has every other reason to run.
    """
    global _archive, _paths
    _archive = None
    _paths = []
    if not path:
        return ""

    archive = logging.getLogger(ARCHIVE_LOGGER)
    for handler in list(archive.handlers):
        archive.removeHandler(handler)
        handler.close()

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Cannot write the drift archive %s: %s", path, exc)
        return ""

    # The line is already the whole record; a level and a timestamp in front of
    # it would make every line invalid JSON.
    rotating.setFormatter(logging.Formatter("%(message)s"))
    archive.addHandler(rotating)
    archive.setLevel(logging.INFO)
    # Nothing here belongs in the application log, the in-memory buffer or on
    # stderr, and all three are what propagation would mean.
    archive.propagate = False

    _archive = archive
    # Newest first, which is the order they are read back in.
    _paths = [path] + [f"{path}.{index}" for index in range(1, backups + 1)]
    return path


def record(entry: dict[str, Any]) -> None:
    """Append one finding. Does nothing when the archive is off or unopenable."""
    if _archive is None:
        return
    try:
        _archive.info(json.dumps(entry, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — an archive must never break a reconcile pass
        # Not logger.exception: this runs inside the pass that is already
        # reporting a problem, and a stack trace per finding would bury it.
        logger.warning("Could not append to the drift archive")


def enabled() -> bool:
    return _archive is not None


def read(limit: int = MAX_PAGE, after: int = 0) -> tuple[list[Entry], bool]:
    """The newest entries, newest first, starting ``after`` positions back.

    Returns the page and whether anything older exists behind it. Reading is
    deliberately independent of writing: the file is opened, read and closed, so
    a reader can never hold the handler's file or wedge a reconcile pass.
    """
    if not _paths:
        return [], False

    wanted = max(1, min(limit, MAX_PAGE))
    lines: list[str] = []
    # Newest file first, and within a file the newest line is the last one. Stop
    # as soon as there is a full page plus one — the extra is only there to
    # answer "is there more" without reading the whole archive to find out.
    for path in _paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(reversed(handle.read().splitlines()))
        except OSError:
            # A rotation that has not happened yet is a missing file, not a fault.
            continue
        if len(lines) > after + wanted:
            break

    window = lines[after : after + wanted]
    return [entry for entry in map(_parse, enumerate(window, start=after)) if entry], len(
        lines
    ) > after + wanted


def _parse(numbered: tuple[int, str]) -> Entry | None:
    """One line into an entry, or ``None`` if it cannot be read.

    A half-written last line is possible — the process can be killed between the
    write and the newline — and one unreadable line must not cost the reader the
    whole page.
    """
    offset, line = numbered
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    details = raw.get("details")
    return Entry(
        offset=offset,
        at=str(raw.get("at", "")),
        instance=str(raw.get("instance", "")),
        payload_kind=str(raw.get("payload_kind", "")),
        summary=str(raw.get("summary", "")),
        corrected=bool(raw.get("corrected", False)),
        took_ms=int(raw.get("took_ms", 0) or 0),
        details=details if isinstance(details, dict) else {},
    )
