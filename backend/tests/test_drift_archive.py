"""The drift log's archive on disk.

The table on the dashboard is built to stay readable: 500 rows, one entry per
finding however often it repeats, rule lists trimmed to 25, and a button that
empties it. Every one of those is right for a live view and wrong for evidence
— by the time somebody works out when a fault started, the rows that would have
said so are the ones that went.

So the same findings are appended to a file, once per pass, untrimmed, in order.
These pin the four properties that make it worth having: it keeps the repeats
the table folds, it keeps the items the table cuts, it survives *Clear log*, and
a line nobody can parse costs one line rather than the page.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.services import driftarchive
from app.services.reconcile import diff_rules
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance


async def reconcile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/reconcile")).status_code == 200


async def refusing_node(client: httpx.AsyncClient) -> None:
    await add_instance(client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()


async def archived(client: httpx.AsyncClient) -> list[dict]:
    body = (await client.get("/api/settings/drift-archive")).json()
    return body["entries"]


async def test_it_keeps_the_repeats_the_table_folds(
    auth_client: httpx.AsyncClient,
) -> None:
    """The one property the database view cannot have.

    Three passes found the same refusal three times. The table says so with a
    count on one row, which is right for reading; the archive holds the three
    sightings themselves, which is what "when did this start, and did it ever
    stop" needs.
    """
    await refusing_node(auth_client)

    await reconcile(auth_client)
    await reconcile(auth_client)
    await reconcile(auth_client)

    refusals = [row for row in await archived(auth_client) if "did not keep" in row["summary"]]
    assert len(refusals) == 3
    # Newest first: a log is read from the end.
    assert refusals[0]["at"] >= refusals[-1]["at"]


async def test_it_survives_clearing_the_drift_log(auth_client: httpx.AsyncClient) -> None:
    """*Clear log* deletes the record; the archive is the record it does not reach."""
    await refusing_node(auth_client)
    await reconcile(auth_client)

    assert (await auth_client.delete("/api/drift")).status_code == 200
    assert (await auth_client.get("/api/drift")).json() == []

    assert len(await archived(auth_client)) >= 1


async def test_it_keeps_the_rules_the_table_trims(auth_client: httpx.AsyncClient) -> None:
    """Forty missing rules, and the archive has all forty.

    The table caps them at 25 plus a marker, because a node that has lost its
    whole rule set must not put its whole rule set in a table cell or an API
    response. The file has no such constraint, and the point of a file is the
    thing that was too long to show.
    """
    await add_instance(auth_client, "a", A)
    for index in range(40):
        await auth_client.post("/api/rules", json={"text": f"||ads{index}.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []  # the node lost the lot

    await reconcile(auth_client)

    entry = next(row for row in await archived(auth_client) if row["payload_kind"] == "rules")
    assert len(entry["details"]["missing"]) == 40

    shown = next(row for row in (await auth_client.get("/api/drift")).json())
    assert len(json.loads(shown["details"])["missing"]) == 26  # 25 plus "… and 15 more"


def test_the_summary_counts_before_the_cap_not_after() -> None:
    """"26 rule(s) missing" on a node missing 4000 is a wrong number.

    Not a shortened one — the cap belongs to the list being displayed, and
    saying so in the sentence beside it made the sentence untrue.
    """
    difference = diff_rules([f"||ads{index}.example^" for index in range(4000)], [])

    assert difference is not None
    assert difference.summary.startswith("4000 rule(s) missing")
    assert len(difference.details["missing"]) == 26
    assert len(difference.full_details["missing"]) == 4000


async def test_a_page_says_whether_there_is_more_behind_it(
    auth_client: httpx.AsyncClient,
) -> None:
    """An archive is allowed to be large; that is what it is for."""
    await refusing_node(auth_client)
    for _ in range(4):
        await reconcile(auth_client)

    body = (await auth_client.get("/api/settings/drift-archive?limit=2")).json()

    assert len(body["entries"]) == 2
    assert body["more"] is True

    older = (await auth_client.get("/api/settings/drift-archive?limit=2&after=2")).json()
    assert older["entries"]
    # The cursor counts back from the newest, so the pages do not overlap.
    assert older["entries"][0]["at"] <= body["entries"][-1]["at"]


def test_an_unreadable_line_costs_one_line_not_the_page(tmp_path: Path) -> None:
    """The process can be killed between the write and the newline.

    A half-written last line is therefore normal rather than exceptional, and an
    archive that refuses to open because of one is an archive that fails exactly
    when the hub has just died — which is when it is being read.
    """
    path = tmp_path / "drift.log"
    path.write_text(
        '{"at": "2026-09-08T19:44:00Z", "instance": "a", "summary": "first"}\n'
        "{not json at all\n"
        '{"at": "2026-09-08T19:49:00Z", "instance": "a", "summary": "third"}\n',
        encoding="utf-8",
    )
    driftarchive.configure(str(path), max_bytes=1024 * 1024, backups=1)

    entries, _ = driftarchive.read()

    assert [entry.summary for entry in entries] == ["third", "first"]


def test_switching_it_off_is_a_no_op_rather_than_an_error() -> None:
    """A deployment that would rather not write to its flash at all."""
    driftarchive.configure("", max_bytes=1024, backups=1)

    assert driftarchive.enabled() is False
    driftarchive.record({"summary": "nowhere to go"})  # must not raise
    assert driftarchive.read() == ([], False)


def test_an_unwritable_path_is_survived_rather_than_fatal(tmp_path: Path) -> None:
    """A hub that cannot write its archive still has every reason to run."""
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    assert driftarchive.configure(str(blocked / "drift.log"), max_bytes=1024, backups=1) == ""
    assert driftarchive.enabled() is False
