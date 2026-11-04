"""Telling an operator a newer release exists, without being wrong or noisy.

The check is a convenience, and the failure modes are all worse than not having
it: nagging a build that is not on the release track, hammering github.com, or
turning a hub that has no internet into a hub that shows an error banner about a
website it was never going to reach.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import httpx
import pytest

from app.semver import is_newer, parse_version
from app.services import updates
from app.services.updates import UpdateChecker, install_method


class FakeTransport(httpx.AsyncBaseTransport):
    """Answers the one request the checker makes, and counts them."""

    def __init__(self, *, status: int = 200, payload: Any = None, boom: bool = False):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.boom = boom
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.boom:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(self.status, json=self.payload, request=request)


@pytest.fixture
def fake_github(monkeypatch):
    """Install a transport into whatever client the checker builds."""

    def install(**kwargs):
        transport = FakeTransport(**kwargs)
        original = httpx.AsyncClient

        def factory(*args, **client_kwargs):
            client_kwargs["transport"] = transport
            return original(*args, **client_kwargs)

        monkeypatch.setattr(updates.httpx, "AsyncClient", factory)
        return transport

    return install


RELEASE = {
    "tag_name": "v0.9.0",
    "html_url": "https://github.com/fgrfn/adguardhub/releases/tag/v0.9.0",
    "published_at": "2026-08-30T10:00:00Z",
}


# --------------------------------------------------------------------------
# Which builds are "out of date"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v0.3.1", (0, 3, 1)),
        ("0.3.1", (0, 3, 1)),
        ("v1.0.0-rc.1", (1, 0, 0)),
        ("dev", None),
        ("", None),
        ("v1.2", None),
    ],
)
def test_version_parsing(text: str, expected: tuple[int, ...] | None) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("v0.3.0", "v0.2.9", True),
        ("v0.10.0", "v0.9.0", True),  # not a string comparison
        ("v0.2.0", "v0.2.0", False),
        ("v0.1.0", "v0.2.0", False),  # a downgrade is not an update
        ("v0.3.0", "dev", False),  # a checkout is not behind, it is elsewhere
        ("", "v0.2.0", False),
    ],
)
def test_which_builds_are_behind(latest: str, current: str, expected: bool) -> None:
    assert is_newer(latest, current) is expected


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


async def test_a_newer_release_is_reported_with_where_to_read_about_it(fake_github) -> None:
    fake_github(payload=RELEASE)
    status = await UpdateChecker("v0.2.0").get(enabled=True)
    assert status.update_available
    assert status.latest == "v0.9.0"
    assert status.release_url == RELEASE["html_url"]
    assert status.error == ""


async def test_the_answer_is_cached_rather_than_asked_on_every_page_load(fake_github) -> None:
    transport = fake_github(payload=RELEASE)
    checker = UpdateChecker("v0.2.0")
    for _ in range(5):
        await checker.get(enabled=True)
    assert transport.calls == 1


async def test_check_now_gets_past_the_cache(fake_github) -> None:
    transport = fake_github(payload=RELEASE)
    checker = UpdateChecker("v0.2.0")
    await checker.get(enabled=True)
    await checker.get(enabled=True, force=True)
    assert transport.calls == 2


async def test_no_internet_is_reported_as_such_and_retried_sooner(fake_github) -> None:
    """A hub on a network with no route out must not look broken.

    The failure is cached for minutes rather than hours, so a hub that started
    before its network did recovers on its own.
    """
    transport = fake_github(boom=True)
    checker = UpdateChecker("v0.2.0")
    status = await checker.get(enabled=True)

    assert status.error == "could not reach github.com"
    assert status.latest == ""
    assert status.update_available is False

    # Cached, so a broken network is not hammered either.
    await checker.get(enabled=True)
    assert transport.calls == 1

    # ...but for minutes rather than the hours a real answer is kept, so a hub
    # that started before its network did recovers without being told to.
    aged = dataclasses.replace(
        checker._cached,  # noqa: SLF001
        checked_at=time.time() - updates.FAILURE_CACHE_SECONDS - 1,
    )
    checker._cached = aged  # noqa: SLF001
    await checker.get(enabled=True)
    assert transport.calls == 2

    # A *successful* answer that old would still have been served from cache.
    assert updates.FAILURE_CACHE_SECONDS < updates.CACHE_SECONDS


async def test_a_repository_with_no_releases_is_not_an_error(fake_github) -> None:
    """404 from the releases endpoint means "none published", not "broken"."""
    fake_github(status=404, payload={"message": "Not Found"})
    status = await UpdateChecker("v0.2.0").get(enabled=True)
    assert status.error == ""
    assert status.latest == ""


async def test_a_refusal_says_what_github_answered(fake_github) -> None:
    fake_github(status=403, payload={"message": "rate limited"})
    status = await UpdateChecker("v0.2.0").get(enabled=True)
    assert "403" in status.error


@pytest.mark.parametrize("payload", [[RELEASE], {"tag_name": ["v0.9.0"]}])
async def test_invalid_release_documents_are_reported_without_breaking_the_page(
    fake_github, payload: Any
) -> None:
    fake_github(payload=payload)

    status = await UpdateChecker("v0.2.0").get(enabled=True)

    assert status.error == "GitHub returned an invalid response"
    assert status.latest == ""
    assert status.update_available is False


async def test_switched_off_means_no_request_at_all(fake_github) -> None:
    transport = fake_github(payload=RELEASE)
    status = await UpdateChecker("v0.2.0").get(enabled=False)
    assert transport.calls == 0
    assert status.latest == ""
    assert status.error == ""


async def test_concurrent_callers_share_one_request(fake_github) -> None:
    """Three open tabs are three requests to the hub, not three to GitHub."""
    import asyncio

    transport = fake_github(payload=RELEASE)
    checker = UpdateChecker("v0.2.0")
    await asyncio.gather(*(checker.get(enabled=True) for _ in range(3)))
    assert transport.calls == 1


# --------------------------------------------------------------------------
# What this install can do about it
# --------------------------------------------------------------------------


def test_the_installer_declares_a_native_install(monkeypatch) -> None:
    monkeypatch.setenv("ADGUARDHUB_INSTALL_METHOD", "native")
    assert install_method() == "native"


def test_a_container_is_detected_even_when_it_declares_nothing(monkeypatch) -> None:
    """An image built from this repo by somebody else still cannot update itself."""
    monkeypatch.delenv("ADGUARDHUB_INSTALL_METHOD", raising=False)
    monkeypatch.setenv("ADGUARDHUB_DOCKER", "1")
    assert install_method() == "docker"


def test_anything_else_is_a_checkout(monkeypatch) -> None:
    monkeypatch.delenv("ADGUARDHUB_INSTALL_METHOD", raising=False)
    monkeypatch.delenv("ADGUARDHUB_DOCKER", raising=False)
    monkeypatch.setattr(updates.os.path, "exists", lambda path: False)
    assert install_method() == "source"


@pytest.mark.parametrize(
    ("method", "expected"),
    [("native", True), ("docker", False), ("source", False)],
)
async def test_only_a_native_install_offers_to_update_itself(
    fake_github, monkeypatch, method: str, expected: bool
) -> None:
    fake_github(payload=RELEASE)
    monkeypatch.setenv("ADGUARDHUB_INSTALL_METHOD", method)
    status = await UpdateChecker("v0.2.0").get(enabled=True)
    assert status.self_update is expected


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


async def test_the_endpoint_needs_a_session(client) -> None:
    assert (await client.get("/api/settings/update")).status_code == 401


async def test_the_endpoint_reports_the_setting_alongside_the_answer(
    auth_client, fake_github
) -> None:
    fake_github(payload=RELEASE)
    body = (await auth_client.get("/api/settings/update")).json()
    assert body["enabled"] is True
    assert body["current"]
    assert "install_method" in body


async def test_turning_the_check_off_is_remembered_and_obeyed(auth_client, fake_github) -> None:
    transport = fake_github(payload=RELEASE)
    saved = await auth_client.put(
        "/api/settings/hub", json={"update_check_enabled": False}
    )
    assert saved.json()["update_check_enabled"] is False

    body = (await auth_client.get("/api/settings/update?force=true")).json()
    assert body["enabled"] is False
    assert transport.calls == 0, "the hub asked GitHub after being told not to"
