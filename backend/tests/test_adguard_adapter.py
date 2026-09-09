"""The AdGuard Home adapter, driven against a mocked /control API."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.adguard import LIST_FETCH_TIMEOUT, AdGuardAdapter
from app.adapters.base import AdapterError, RemoteFilterList
from app.adapters.session import SessionStore

FILTERING_STATUS = {
    "enabled": True,
    "user_rules": ["||ads.example.com^", "@@||shop.example.com^"],
    "filters": [
        {"url": "https://example.com/block.txt", "name": "Block", "enabled": True, "id": 1}
    ],
    "whitelist_filters": [
        {"url": "https://example.com/allow.txt", "name": "Allow", "enabled": False, "id": 2}
    ],
}


def make_adapter(handler, sessions: SessionStore | None = None, **kwargs) -> AdGuardAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="http://adguard.local", transport=transport)
    return AdGuardAdapter(
        "http://adguard.local",
        "admin",
        "pw",
        client=client,
        # A private store by default, so tests never leak sessions into each other.
        sessions=sessions if sessions is not None else SessionStore(),
        **kwargs,
    )


def login_ok(handler):
    """Wrap a handler so /control/login succeeds and sets a session cookie."""

    async def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/login":
            return httpx.Response(200, json={}, headers={"Set-Cookie": "agh_session=abc; Path=/"})
        return await handler(request)

    return wrapped


async def test_check_reads_the_version() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/control/status"
        return httpx.Response(200, json={"version": "v0.107.55"})

    assert await make_adapter(login_ok(handler)).check() == "v0.107.55"


async def test_pull_rules_and_filter_lists() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FILTERING_STATUS)

    adapter = make_adapter(login_ok(handler))
    assert await adapter.pull_rules() == ["||ads.example.com^", "@@||shop.example.com^"]

    lists = await adapter.pull_filter_lists()
    assert [(item.kind, item.enabled) for item in lists] == [
        ("blocklist", True),
        ("allowlist", False),
    ]


async def test_push_rules_replaces_the_whole_set() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"user_rules": ["||stale.example^"]})
        assert request.url.path == "/control/filtering/set_rules"
        seen["body"] = request.content
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_rules(["||a.com^"])
    assert b'"rules"' in seen["body"]
    assert b"||a.com^" in seen["body"]


async def test_a_rule_set_the_node_already_holds_is_not_written_again() -> None:
    """Every accepted write is a reconfigure and a YAML rewrite on the node.

    Sending a rule set the node already has costs exactly what sending a changed
    one costs, and achieves nothing. The read that avoids it is one GET of the
    endpoint the caller is about to read anyway.
    """
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"user_rules": ["||a.com^", "||b.com^"]})
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_rules(["||a.com^", "||b.com^"])

    assert ("POST", "/control/filtering/set_rules") not in seen


async def test_rule_order_still_counts_as_a_difference() -> None:
    """The hub decides the order, and AdGuard applies rules in the order given.

    A set-based comparison here would call two different rule sets equal and
    leave the node evaluating them the other way round.
    """
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"user_rules": ["||b.com^", "||a.com^"]})
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_rules(["||a.com^", "||b.com^"])

    assert seen == ["/control/filtering/set_rules"]


async def test_push_filter_lists_adds_updates_and_removes() -> None:
    calls: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json=FILTERING_STATUS)
        import json

        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    desired = [
        # unchanged
        RemoteFilterList("Block", "https://example.com/block.txt", True, "blocklist"),
        # newly added
        RemoteFilterList("Extra", "https://example.com/extra.txt", True, "blocklist"),
        # the allowlist subscription is dropped entirely
    ]
    await make_adapter(login_ok(handler)).push_filter_lists(desired)

    paths = [path for path, _ in calls]
    assert "/control/filtering/add_url" in paths
    assert "/control/filtering/remove_url" in paths
    removed = next(body for path, body in calls if path.endswith("remove_url"))
    assert removed == {"url": "https://example.com/allow.txt", "whitelist": True}


async def test_a_list_the_node_renamed_is_left_alone() -> None:
    """AdGuard owns the title; the hub owns the URL, the kind and enabled.

    AdGuard overwrites a subscription's name with the `! Title:` from the file it
    downloads, so the node's answer is the list's own title rather than what the
    hub sent. Comparing the two fired a `set_url` for every list on every push,
    for ever — and it was not drift either: diff_filter_lists has never compared
    names, so the push was writing for a difference the hub does not consider
    one.
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json=FILTERING_STATUS)
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    desired = [
        # Same URL, same kind, same enabled state — only the name differs,
        # because the node took its own from the downloaded file.
        RemoteFilterList(
            "What the hub calls it", "https://example.com/block.txt", True, "blocklist"
        ),
        RemoteFilterList("Allow", "https://example.com/allow.txt", False, "allowlist"),
    ]
    await make_adapter(login_ok(handler)).push_filter_lists(desired)

    assert calls == []


async def test_a_list_the_node_has_disabled_is_still_corrected() -> None:
    """Dropping the name from the comparison must not drop the enabled state."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json=FILTERING_STATUS)
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    desired = [
        RemoteFilterList("Block", "https://example.com/block.txt", True, "blocklist"),
        # The node has this one switched off; the hub wants it on.
        RemoteFilterList("Allow", "https://example.com/allow.txt", True, "allowlist"),
    ]
    await make_adapter(login_ok(handler)).push_filter_lists(desired)

    assert calls == ["/control/filtering/set_url"]


async def test_query_log_parsing_marks_blocked_entries() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "time": "2026-01-01T10:00:00Z",
                        "question": {"name": "ads.example.com", "type": "A"},
                        "client": "192.168.1.10",
                        "reason": "FilteredBlackList",
                        # The name AdGuard Home actually uses, and it sends a string.
                        "elapsedMs": "1.5",
                        "rules": [{"text": "||ads.example.com^"}],
                    },
                    {
                        "time": "2026-01-01T10:00:01Z",
                        "question": {"name": "shop.example.com", "type": "A"},
                        "client": "192.168.1.11",
                        "reason": "NotFilteredWhiteList",
                        "elapsedMs": "bogus",
                    },
                    {
                        "time": "2026-01-01T10:00:02Z",
                        "question": {"name": "old.example.com", "type": "A"},
                        "client": "192.168.1.12",
                        "reason": "NotFilteredNotFound",
                        "elapsed_ms": "2.5",
                    },
                ]
            },
        )

    entries = await make_adapter(login_ok(handler)).query_log(100)
    assert entries[0].blocked is True
    assert entries[0].rule == "||ads.example.com^"
    assert entries[0].elapsed_ms == 1.5
    assert entries[1].blocked is False
    assert entries[1].elapsed_ms == 0.0
    # The snake_case spelling is still accepted, for anything that sends it.
    assert entries[2].elapsed_ms == 2.5


async def test_section_pull_is_limited_to_managed_keys() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "upstream_dns": ["1.1.1.1"],
                "dnssec_enabled": True,
                "ratelimit_whitelist": [],
                "something_adguard_added_later": 1,  # not in the managed key set
            },
        )

    data = await make_adapter(login_ok(handler)).pull_section("dns")
    assert data == {
        "upstream_dns": ["1.1.1.1"],
        "dnssec_enabled": True,
        "ratelimit_whitelist": [],
    }


async def test_section_push_sends_only_managed_keys() -> None:
    """A key the section does not declare is never forwarded to the node."""
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(200, json={})
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_section(
        "dns", {"upstream_dns": ["9.9.9.9"], "ratelimit": 20, "not_a_dns_key": True}
    )
    assert captured["path"] == "/control/dns_config"
    assert captured["body"] == {"upstream_dns": ["9.9.9.9"], "ratelimit": 20}


async def test_a_partly_filled_section_leaves_the_rest_of_the_node_alone() -> None:
    """A section configured by hand holds only the keys the operator filled in.

    Sending those alone would be read by the node as "everything else is now
    unset", so a hub built without importing a master would quietly flatten the
    DNS config of every node it pushed to. The push overlays instead.
    """
    target = {
        "upstream_dns": ["1.1.1.1"],
        "bootstrap_dns": ["9.9.9.9"],
        "cache_size": 4194304,
        "dnssec_enabled": True,
    }
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=target)
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    # Only one key has ever been set in the hub.
    await make_adapter(login_ok(handler)).push_section("dns", {"upstream_dns": ["9.9.9.9"]})

    body = captured["body"]
    assert body["upstream_dns"] == ["9.9.9.9"], "the hub's own value must win"
    assert body["bootstrap_dns"] == ["9.9.9.9"]
    assert body["cache_size"] == 4194304
    assert body["dnssec_enabled"] is True


async def test_tls_push_keeps_the_targets_own_certificate() -> None:
    """/control/tls/configure replaces the whole object, so the push has to merge.

    Sending {"enabled": true} alone would wipe the node's certificate and hostname.
    """
    target = {
        "enabled": False,
        "server_name": "node-b.lan",
        "certificate_path": "/etc/ssl/node-b.crt",
        "private_key_path": "/etc/ssl/node-b.key",
        "port_https": 443,
        "private_key_saved": True,
    }
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=target)
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_section("tls", {"enabled": True})

    body = captured["body"]
    assert body["enabled"] is True
    assert body["server_name"] == "node-b.lan"
    assert body["certificate_path"] == "/etc/ssl/node-b.crt"
    assert body["private_key_path"] == "/etc/ssl/node-b.key"


async def test_tls_pull_reads_only_the_enabled_flag() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"enabled": True, "server_name": "master.lan", "private_key_saved": True},
        )

    assert await make_adapter(login_ok(handler)).pull_section("tls") == {"enabled": True}


async def test_a_section_the_instance_lacks_reads_as_none() -> None:
    """AdGuard versions differ; a missing endpoint must not fail the whole sync."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    assert await make_adapter(login_ok(handler)).pull_section("blocked_services") is None


async def test_toggle_sections_use_the_enable_and_disable_endpoints() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # The opposite of what is about to be pushed, so the write is needed.
            return httpx.Response(200, json={"enabled": "safebrowsing" not in request.url.path})
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    adapter = make_adapter(login_ok(handler))
    await adapter.push_section("safebrowsing", {"enabled": True})
    await adapter.push_section("parental", {"enabled": False})
    assert seen == ["/control/safebrowsing/enable", "/control/parental/disable"]


async def test_a_toggle_already_in_the_wanted_state_is_not_written() -> None:
    """Switching something on that is already on is still a reconfigure."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": True})
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_section("safebrowsing", {"enabled": True})

    assert seen == []


async def test_clients_are_added_updated_and_removed() -> None:
    calls: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/clients":
            return httpx.Response(
                200,
                json={
                    "clients": [
                        {"name": "keep", "ids": ["10.0.0.1"]},
                        {"name": "change", "ids": ["10.0.0.2"]},
                        {"name": "drop", "ids": ["10.0.0.3"]},
                    ]
                },
            )
        import json

        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_section(
        "clients",
        {
            "clients": [
                {"name": "keep", "ids": ["10.0.0.1"]},
                {"name": "change", "ids": ["10.0.0.9"]},
                {"name": "new", "ids": ["10.0.0.4"]},
            ]
        },
    )

    paths = [path for path, _ in calls]
    assert paths.count("/control/clients/add") == 1
    assert paths.count("/control/clients/update") == 1
    assert paths.count("/control/clients/delete") == 1
    deleted = next(body for path, body in calls if path.endswith("delete"))
    assert deleted == {"name": "drop"}


async def test_the_session_is_established_once_not_per_request() -> None:
    """The bug behind the HTTP 429 reports: a login on every single call.

    Adapters are rebuilt constantly (the query log poller makes one every few
    seconds per instance), so the cached cookie has to survive across them.
    """
    seen: list[str] = []
    sessions = SessionStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/control/login":
            return httpx.Response(200, json={}, headers={"Set-Cookie": "agh_session=abc"})
        return httpx.Response(200, json={"version": "v0.107.55"})

    for _ in range(5):
        # A fresh adapter each time, exactly as the workers build them.
        await make_adapter(handler, sessions=sessions).check()

    assert seen.count("/control/login") == 1
    assert seen.count("/control/status") == 5


async def test_credentials_are_never_sent_as_basic_auth() -> None:
    """Rejected Basic-auth requests count against AdGuard's brute-force limit."""
    headers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        if request.url.path == "/control/login":
            return httpx.Response(200, json={}, headers={"Set-Cookie": "agh_session=abc"})
        return httpx.Response(200, json={"version": "v0.107.55"})

    await make_adapter(handler).check()
    assert all(value is None for value in headers)


async def test_an_expired_session_is_renewed_once() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/control/login":
            return httpx.Response(200, json={}, headers={"Set-Cookie": "agh_session=abc"})
        if seen.count("/control/status") == 1:
            return httpx.Response(401, text="session expired")
        return httpx.Response(200, json={"version": "v0.107.55"})

    assert await make_adapter(handler).check() == "v0.107.55"
    assert seen == [
        "/control/login",
        "/control/status",
        "/control/login",
        "/control/status",
    ]


async def test_a_429_login_explains_the_rate_limit_and_backs_off() -> None:
    attempts: list[str] = []
    sessions = SessionStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(429, text="too many requests")

    with pytest.raises(AdapterError, match="429") as caught:
        await make_adapter(handler, sessions=sessions).check()
    assert "brute-force" in str(caught.value)
    assert "credentials may well be correct" in str(caught.value)

    # The cooldown must stop the next caller from hammering the login endpoint.
    before = len(attempts)
    with pytest.raises(AdapterError, match="rate-limiting"):
        await make_adapter(handler, sessions=sessions).check()
    assert len(attempts) == before


async def test_wrong_credentials_say_so_rather_than_blaming_rate_limits() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(AdapterError, match="rejected the credentials"):
        await make_adapter(handler).check()


async def test_http_errors_surface_as_adapter_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(AdapterError, match="500"):
        await make_adapter(login_ok(handler)).check()


async def test_transport_errors_surface_as_adapter_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AdapterError, match="connection refused"):
        await make_adapter(login_ok(handler)).check()


async def test_a_timeout_says_what_happened() -> None:
    """httpx raises timeouts with no message, which rendered as "failed:" and nothing.

    A silent host and a refusing host need different fixes, so the message has to
    say which one it was.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    adapter = make_adapter(handler)
    with pytest.raises(AdapterError) as caught:
        await adapter.check()

    # Before the fix this read "Login failed: " and stopped there.
    assert str(caught.value) == (
        "Login failed: no answer while opening the connection (connect timeout) after 10s"
    )


async def test_an_error_with_its_own_message_keeps_it() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    with pytest.raises(AdapterError, match="All connection attempts failed"):
        await make_adapter(handler).check()


async def test_query_log_and_stats_config_write_to_the_update_path() -> None:
    """AdGuard answers 405 on the read path; the write lives under /update."""
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            # Switched off on the node, so both pushes have work to do.
            return httpx.Response(200, json={"enabled": False, "ignored": []})
        return httpx.Response(200, json={})

    adapter = make_adapter(login_ok(handler))
    await adapter.push_section("querylog_config", {"enabled": True})
    await adapter.push_section("stats_config", {"enabled": True})

    writes = [entry for entry in seen if entry[0] == "PUT"]
    assert writes == [
        ("PUT", "/control/querylog/config/update"),
        ("PUT", "/control/stats/config/update"),
    ]


async def test_a_section_the_node_already_matches_is_not_written() -> None:
    """The change that started this: a settings edit rewrote every section.

    Nine of the eleven managed areas were written unconditionally on every push,
    each one a `reconfiguring server` and a rewrite of AdGuardHome.yaml — while
    the drift log, using this very comparison, correctly reported that nothing
    had changed. The push and the drift log now agree.
    """
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": True, "ignored": []})
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler)).push_section("querylog_config", {"enabled": True})

    assert [entry for entry in seen if entry[0] != "GET"] == []


async def test_a_section_the_node_does_not_implement_is_skipped_not_failed() -> None:
    """One missing endpoint used to cost the node every other section.

    push_kind raises on the first section that fails, so an AdGuard build without
    one area failed the whole settings payload. Reconciliation has always
    reported that as a capability gap rather than drift; this now agrees.
    """
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={})

    # Must not raise.
    await make_adapter(login_ok(handler)).push_section("querylog_config", {"enabled": True})

    assert [entry for entry in seen if entry[0] != "GET"] == []


# --------------------------------------------------------------------------
# Adding a subscription is not an ordinary request
# --------------------------------------------------------------------------


async def test_adding_a_list_waits_far_longer_than_a_status_check() -> None:
    """AdGuard downloads and parses the list before it answers `add_url`.

    A threat-intelligence feed is millions of rules, which is a minute or more
    of work on a small host — all of it before the first byte of the response.
    Held to the ten seconds that decide whether a node is alive, adding such a
    list simply cannot succeed, and a hub that keeps trying reports the same
    read timeout every five minutes forever.
    """
    seen: dict[str, float | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"filters": [], "whitelist_filters": []})
        seen[request.url.path] = request.extensions.get("timeout", {}).get("read")
        return httpx.Response(200, json={})

    await make_adapter(login_ok(handler), timeout=10.0).push_filter_lists(
        [RemoteFilterList("Huge", "https://example.com/huge.txt", True, "blocklist")]
    )

    assert seen["/control/filtering/add_url"] == LIST_FETCH_TIMEOUT
    assert LIST_FETCH_TIMEOUT > 10.0


async def test_one_list_that_times_out_does_not_abandon_the_other_nineteen() -> None:
    """The failure that made this unrecoverable.

    Stopping at the first error meant a node missing twenty lists failed on the
    first, skipped the rest, and began again at the same first list on the next
    run — never getting one list further, for as long as the hub ran.
    """
    added: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"filters": [], "whitelist_filters": []})
        import json

        url = json.loads(request.content)["url"]
        if url.endswith("first.txt"):
            raise httpx.ReadTimeout("", request=request)
        added.append(url)
        return httpx.Response(200, json={})

    desired = [
        RemoteFilterList("First", "https://example.com/first.txt", True, "blocklist"),
        *[
            RemoteFilterList(f"L{n}", f"https://example.com/{n}.txt", True, "blocklist")
            for n in range(19)
        ],
    ]

    with pytest.raises(AdapterError) as caught:
        await make_adapter(login_ok(handler)).push_filter_lists(desired)

    # The nineteen that could be applied were applied...
    assert len(added) == 19
    # ...and the one that could not is named, with its reason.
    assert "1 of 20" in str(caught.value)
    assert "first.txt" in str(caught.value)
    assert "read timeout" in str(caught.value)


async def test_many_failures_are_summarised_rather_than_listed_in_full() -> None:
    """Twenty failures must not become twenty error messages in one status field."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/filtering/status":
            return httpx.Response(200, json={"filters": [], "whitelist_filters": []})
        raise httpx.ReadTimeout("", request=request)

    desired = [
        RemoteFilterList(f"L{n}", f"https://example.com/{n}.txt", True, "blocklist")
        for n in range(20)
    ]

    with pytest.raises(AdapterError) as caught:
        await make_adapter(login_ok(handler)).push_filter_lists(desired)

    message = str(caught.value)
    assert "20 of 20" in message
    assert "and 17 more" in message
