"""Which list blocked this.

A query log row names the rule that matched and never the list it came from.
With twenty subscriptions that is the difference between "the banking app is
broken" and "turn off this one list, or add this one exception" — and the rule
text alone does not narrow it down at all.

AdGuard does send the answer, in ``rules[0].filter_list_id``. The trap is what
that number means: **it belongs to the node.** AdGuard assigns it from a
clock-seeded counter at `add_url`, so one subscription is a different number on
each node, and the aggregated log mixes rows from all of them. Most of what is
pinned here is therefore that the hub reads each row against the node that wrote
it, and says nothing at all rather than guessing.
"""

from __future__ import annotations

import httpx

from app.adapters.base import QueryLogEntry, RemoteFilterList
from app.services import filtersource
from app.services.filtersource import BUILT_IN, FilterSources
from app.services.querylog import buffer, poll_once

from .fakes import FakeAdapter
from .test_sync import A, B, add_instance

EASYLIST = RemoteFilterList("EasyList", "https://e.com/l.txt", True, "blocklist", remote_id=101)
HAGEZI = RemoteFilterList("HaGeZi Threat Feeds", "https://h.com/t.txt", True, "blocklist", 0, 202)


def entry(question: str, *, rule: str = "", filter_list_id: int | None = None) -> QueryLogEntry:
    return QueryLogEntry(
        time="2026-09-15T10:00:00Z",
        question=question,
        question_type="A",
        client="10.0.0.5",
        answer_status="FilteredBlackList",
        blocked=True,
        rule=rule,
        filter_list_id=filter_list_id,
    )


async def logged(client: httpx.AsyncClient, question: str) -> dict:
    rows = (await client.get("/api/querylog")).json()
    return next(row for row in rows if row["question"] == question)


# --------------------------------------------------------------------------
# The map itself
# --------------------------------------------------------------------------


def test_a_built_in_id_needs_no_lookup() -> None:
    """These are constants in AdGuard's source, identical on every node."""
    sources = FilterSources()

    assert sources.label(1, 0) == "Your own rules"
    assert sources.label(1, -2) == "Blocked services"
    assert set(BUILT_IN) == {0, -1, -2, -3, -4, -5}


def test_zero_means_the_hubs_own_rules_and_absent_means_nothing() -> None:
    """The distinction the adapter has to preserve.

    ``0`` is AdGuard's id for the custom rule set — the very set the hub owns, so
    it is the most useful answer of all. Treating a *missing* field as 0 would
    credit the hub for every block a built-in module made.
    """
    sources = FilterSources()

    assert sources.label(1, 0) == "Your own rules"
    assert sources.label(1, None) == ""


def test_an_id_is_read_against_the_node_that_cited_it() -> None:
    """The trap. The same number is a different list on another node."""
    sources = FilterSources()
    sources.remember(1, [EASYLIST])
    other = RemoteFilterList("Something else", "https://x/y.txt", True, "blocklist", 0, 101)
    sources.remember(2, [other])

    assert sources.label(1, 101) == "EasyList"
    assert sources.label(2, 101) == "Something else"


def test_an_id_the_hub_cannot_name_stays_blank() -> None:
    """"Unknown list" in the column you read to decide what to change is worse
    than nothing, because it reads as an answer."""
    sources = FilterSources()
    sources.remember(1, [EASYLIST])

    assert sources.label(1, 999) == ""
    assert sources.label(99, 101) == "", "a node with no map names nothing"


def test_an_unknown_id_asks_for_a_refresh_but_not_on_every_poll() -> None:
    """A list added in a node's native UI would otherwise fetch the whole filter
    status every five seconds for as long as its rows kept arriving."""
    sources = FilterSources()
    sources.remember(1, [EASYLIST])

    assert sources.needs_refresh(1, {999}) is True
    sources.note_attempt(1)
    assert sources.needs_refresh(1, {999}) is False


def test_a_built_in_id_never_triggers_a_refresh() -> None:
    sources = FilterSources()
    sources.remember(1, [EASYLIST])

    assert sources.needs_refresh(1, {0, -4}) is False


# --------------------------------------------------------------------------
# End to end, through the poll
# --------------------------------------------------------------------------


async def test_the_log_names_the_list_a_block_came_from(
    auth_client: httpx.AsyncClient,
) -> None:
    """The whole point, from the node's answer to the row an operator reads."""
    await add_instance(auth_client, "a", A)
    state = FakeAdapter.state_for(A)
    state.filter_lists = [EASYLIST, HAGEZI]
    state.query_log = [entry("ads.example.com", rule="||ads.example.com^", filter_list_id=202)]

    await poll_once()

    assert (await logged(auth_client, "ads.example.com"))["filter_list"] == "HaGeZi Threat Feeds"


async def test_a_rule_the_operator_wrote_is_named_as_theirs(
    auth_client: httpx.AsyncClient,
) -> None:
    """Answered without any lookup, and it is the answer asked for most."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).query_log = [
        entry("tracker.example.com", rule="||tracker.example.com^", filter_list_id=0)
    ]

    await poll_once()

    assert (await logged(auth_client, "tracker.example.com"))["filter_list"] == "Your own rules"


async def test_two_nodes_numbering_their_lists_differently_still_read_correctly(
    auth_client: httpx.AsyncClient,
) -> None:
    """The failure a single global map would produce, and it would look right.

    Both nodes carry the same two subscriptions under swapped ids — which is the
    ordinary case, since each node numbers them in the order it happened to add
    them.
    """
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)

    FakeAdapter.state_for(A).filter_lists = [EASYLIST, HAGEZI]
    FakeAdapter.state_for(A).query_log = [entry("one.example.com", filter_list_id=202)]
    # Same two lists, opposite numbers.
    FakeAdapter.state_for(B).filter_lists = [
        RemoteFilterList("EasyList", "https://e.com/l.txt", True, "blocklist", 0, 202),
        RemoteFilterList("HaGeZi Threat Feeds", "https://h.com/t.txt", True, "blocklist", 0, 101),
    ]
    FakeAdapter.state_for(B).query_log = [entry("two.example.com", filter_list_id=202)]

    await poll_once()

    assert (await logged(auth_client, "one.example.com"))["filter_list"] == "HaGeZi Threat Feeds"
    assert (await logged(auth_client, "two.example.com"))["filter_list"] == "EasyList"


async def test_a_node_that_will_not_list_its_filters_still_logs(
    auth_client: httpx.AsyncClient,
) -> None:
    """The explanation is worth having and never worth losing the entry over."""
    await add_instance(auth_client, "a", A)
    state = FakeAdapter.state_for(A)
    state.query_log = [entry("ads.example.com", rule="||ads.example.com^", filter_list_id=202)]
    state.unsupported_sections = set()

    async def refuse(self) -> list[RemoteFilterList]:
        raise ValueError("this node will not say")

    original = FakeAdapter.pull_filter_lists
    FakeAdapter.pull_filter_lists = refuse  # type: ignore[method-assign]
    try:
        await poll_once()
    finally:
        FakeAdapter.pull_filter_lists = original  # type: ignore[method-assign]

    row = await logged(auth_client, "ads.example.com")
    assert row["rule"] == "||ads.example.com^", "the entry itself has to survive"
    assert row["filter_list"] == ""


async def test_an_unfiltered_answer_names_no_list(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).query_log = [entry("example.com")]

    await poll_once()

    assert (await logged(auth_client, "example.com"))["filter_list"] == ""


async def test_the_lists_are_not_fetched_when_no_row_cites_one(
    auth_client: httpx.AsyncClient,
) -> None:
    """A poll runs every five seconds; it must not pull the filter status each time."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).query_log = [entry("example.com")]

    calls = 0
    original = FakeAdapter.pull_filter_lists

    async def counted(self) -> list[RemoteFilterList]:
        nonlocal calls
        calls += 1
        return await original(self)

    FakeAdapter.pull_filter_lists = counted  # type: ignore[method-assign]
    try:
        await poll_once()
        await buffer.clear()
        filtersource.sources.reset()
        await poll_once()
    finally:
        FakeAdapter.pull_filter_lists = original  # type: ignore[method-assign]

    assert calls == 0


# --------------------------------------------------------------------------
# The map outliving the node it belongs to
# --------------------------------------------------------------------------

#: The same two lists as EASYLIST/HAGEZI, numbered the other way round — which is
#: the ordinary case, since each node numbers its subscriptions in the order it
#: happened to add them.
SWAPPED = [
    RemoteFilterList("EasyList", "https://e.com/l.txt", True, "blocklist", 0, 202),
    RemoteFilterList("HaGeZi Threat Feeds", "https://h.com/t.txt", True, "blocklist", 0, 101),
]


async def test_a_deleted_nodes_names_are_not_inherited_by_the_next_node(
    auth_client: httpx.AsyncClient,
) -> None:
    """The map is keyed by row id, and SQLite hands a deleted id to the next row.

    So "node 1" after a delete and an add is a different machine with the same
    key, and the names left behind are the previous one's. Nothing would catch it
    either: an id both nodes happen to use is *known*, so no refresh is due, and
    the column would name a list the operator does not even subscribe to on that
    node.
    """
    old_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).filter_lists = [EASYLIST, HAGEZI]
    FakeAdapter.state_for(A).query_log = [entry("first.example.com", filter_list_id=202)]
    await poll_once()
    assert (await logged(auth_client, "first.example.com"))["filter_list"] == "HaGeZi Threat Feeds"

    assert (await auth_client.delete(f"/api/instances/{old_id}")).status_code == 204

    new_id = await add_instance(auth_client, "b", B)
    assert new_id == old_id, "the premise: SQLite reuses the deleted row's id"
    FakeAdapter.state_for(B).filter_lists = SWAPPED
    FakeAdapter.state_for(B).query_log = [entry("second.example.com", filter_list_id=202)]

    await poll_once()

    assert (await logged(auth_client, "second.example.com"))["filter_list"] == "EasyList"


async def test_pointing_a_node_at_another_url_drops_its_remembered_names(
    auth_client: httpx.AsyncClient,
) -> None:
    """Same row, different AdGuard — and its counter has nothing to do with the old one."""
    instance_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).filter_lists = [EASYLIST, HAGEZI]
    FakeAdapter.state_for(A).query_log = [entry("first.example.com", filter_list_id=202)]
    await poll_once()
    assert (await logged(auth_client, "first.example.com"))["filter_list"] == "HaGeZi Threat Feeds"

    moved = await auth_client.patch(f"/api/instances/{instance_id}", json={"base_url": B})
    assert moved.status_code == 200, moved.text
    FakeAdapter.state_for(B).filter_lists = SWAPPED
    FakeAdapter.state_for(B).query_log = [entry("second.example.com", filter_list_id=202)]

    await poll_once()

    assert (await logged(auth_client, "second.example.com"))["filter_list"] == "EasyList"
