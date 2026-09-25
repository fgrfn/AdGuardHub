"""Narrowing the aggregated log down to the question being asked.

There used to be one choice — *blocked only* — and it is a boolean over a field
that is not one. The node sends a *reason*, and the adapter reduces it: anything
in ``_ALLOWED_REASONS`` is "not blocked". ``NotFilteredWhiteList`` is in that set,
so a query that one of the operator's **own allow rules** let through came out
identical, in every column, to a query nothing had an opinion about.

That is the one question this hub exists to answer. Whitelisting is why it was
written (spec §5): rules are added from this very page, and whether they are
firing was not askable here at all.

The other half is the list. Since the log learned which subscription a block came
from, "turn off this list, or write one exception" is one step away — and the step
in between, seeing only that list's blocks, was missing.
"""

from __future__ import annotations

import httpx

from app.adapters.base import QueryLogEntry, RemoteFilterList

from .fakes import FakeAdapter
from .test_sync import A, add_instance

HAGEZI = RemoteFilterList("HaGeZi Threat Feeds", "https://h.com/t.txt", True, "blocklist", 0, 202)
EASYLIST = RemoteFilterList("EasyList", "https://e.com/l.txt", True, "blocklist", 0, 101)


def row(
    host: str,
    when: str,
    *,
    reason: str,
    rule: str = "",
    filter_list_id: int | None = None,
) -> QueryLogEntry:
    return QueryLogEntry(
        time=when,
        question=host,
        question_type="A",
        client="10.0.0.5",
        answer_status=reason,
        # Exactly the adapter's own rule, so these fixtures cannot drift away
        # from what a real node produces.
        blocked=reason not in {"NotFilteredWhiteList", "NotFilteredNotFound", "NotFilteredError"},
        rule=rule,
        filter_list_id=filter_list_id,
    )


async def a_log(client: httpx.AsyncClient) -> None:
    """One of each kind: blocked by a list, allowed by a rule, untouched."""
    await add_instance(client, "a", A)
    state = FakeAdapter.state_for(A)
    state.filter_lists = [EASYLIST, HAGEZI]
    state.query_log = [
        row(
            "ads.example.com",
            "2026-01-01T10:00:00Z",
            reason="FilteredBlackList",
            rule="||ads.example.com^",
            filter_list_id=202,
        ),
        row(
            "tracker.example.com",
            "2026-01-01T10:00:01Z",
            reason="FilteredBlackList",
            rule="||tracker.example.com^",
            filter_list_id=101,
        ),
        row(
            "bank.example.com",
            "2026-01-01T10:00:02Z",
            reason="NotFilteredWhiteList",
            rule="@@||bank.example.com^",
            filter_list_id=0,
        ),
        row("plain.example.com", "2026-01-01T10:00:03Z", reason="NotFilteredNotFound"),
    ]
    assert (await client.post("/api/querylog/refresh")).status_code == 200


async def asked(client: httpx.AsyncClient, query: str) -> list[str]:
    response = await client.get(f"/api/querylog{query}")
    assert response.status_code == 200, response.text
    return sorted(entry["question"] for entry in response.json())


# --------------------------------------------------------------------------
# The response a row got
# --------------------------------------------------------------------------


async def test_a_query_an_allow_rule_let_through_can_be_found(
    auth_client: httpx.AsyncClient,
) -> None:
    """The question the old filter could not express.

    It is not blocked, so *blocked only* hid it; it was not filtered either, so
    the unfiltered view drowned it in every ordinary lookup on the network.
    """
    await a_log(auth_client)

    assert await asked(auth_client, "?status=allowlisted") == ["bank.example.com"]


async def test_the_three_statuses_partition_the_log(auth_client: httpx.AsyncClient) -> None:
    """Every row belongs to exactly one, and together they are the whole log."""
    await a_log(auth_client)

    blocked = await asked(auth_client, "?status=blocked")
    allowed = await asked(auth_client, "?status=allowlisted")
    plain = await asked(auth_client, "?status=processed")

    assert blocked == ["ads.example.com", "tracker.example.com"]
    assert allowed == ["bank.example.com"]
    assert plain == ["plain.example.com"]
    assert sorted(blocked + allowed + plain) == await asked(auth_client, "")


async def test_an_allowed_query_is_not_counted_as_merely_unfiltered(
    auth_client: httpx.AsyncClient,
) -> None:
    """``processed`` is the remainder, not "everything that is not blocked"."""
    await a_log(auth_client)

    assert "bank.example.com" not in await asked(auth_client, "?status=processed")


async def test_blocked_only_still_works(auth_client: httpx.AsyncClient) -> None:
    """A documented query parameter an automation may already be passing."""
    await a_log(auth_client)

    assert await asked(auth_client, "?blocked_only=true") == [
        "ads.example.com",
        "tracker.example.com",
    ]


async def test_an_explicit_status_wins_over_blocked_only(
    auth_client: httpx.AsyncClient,
) -> None:
    """The more specific of the two requests, when something sends both."""
    await a_log(auth_client)

    assert await asked(auth_client, "?blocked_only=true&status=allowlisted") == [
        "bank.example.com"
    ]


async def test_an_unknown_status_is_refused(auth_client: httpx.AsyncClient) -> None:
    """Rather than quietly answering with everything, which reads as a filter that
    found nothing to exclude."""
    await a_log(auth_client)

    assert (await auth_client.get("/api/querylog?status=unblocked")).status_code == 422


# --------------------------------------------------------------------------
# The list a block came from
# --------------------------------------------------------------------------


async def test_the_log_can_be_narrowed_to_one_list(auth_client: httpx.AsyncClient) -> None:
    await a_log(auth_client)

    assert await asked(auth_client, "?filter_list=HaGeZi%20Threat%20Feeds") == [
        "ads.example.com"
    ]


async def test_the_offered_lists_are_the_ones_the_entries_name(
    auth_client: httpx.AsyncClient,
) -> None:
    """Including the built-in AdGuard sets, which are not subscriptions at all —
    and never the empty string, which is what a row the hub cannot name carries."""
    await a_log(auth_client)

    response = await auth_client.get("/api/querylog/lists")
    assert response.status_code == 200
    assert response.json()["lists"] == ["EasyList", "HaGeZi Threat Feeds", "Your own rules"]


async def test_a_list_filter_combines_with_a_status(auth_client: httpx.AsyncClient) -> None:
    await a_log(auth_client)

    assert await asked(auth_client, "?filter_list=EasyList&status=allowlisted") == []
    assert await asked(auth_client, "?filter_list=EasyList&status=blocked") == [
        "tracker.example.com"
    ]


# --------------------------------------------------------------------------
# What the search box says it searches
# --------------------------------------------------------------------------


async def test_the_search_finds_a_rule(auth_client: httpx.AsyncClient) -> None:
    """The field offered "a domain, a client or a rule" and searched the first two.

    So the one syntax that identifies an allow rule — ``@@`` — found nothing, on
    the page where allow rules are written.
    """
    await a_log(auth_client)

    assert await asked(auth_client, "?search=%40%40") == ["bank.example.com"]


async def test_the_search_finds_a_list_by_name(auth_client: httpx.AsyncClient) -> None:
    await a_log(auth_client)

    assert await asked(auth_client, "?search=hagezi") == ["ads.example.com"]


async def test_the_search_still_finds_a_domain_and_a_client(
    auth_client: httpx.AsyncClient,
) -> None:
    await a_log(auth_client)

    assert await asked(auth_client, "?search=tracker") == ["tracker.example.com"]
    assert len(await asked(auth_client, "?search=10.0.0.5")) == 4
