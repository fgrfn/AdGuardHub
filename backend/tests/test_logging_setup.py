"""What the hub writes to its log, and what it deliberately does not.

The default was INFO on everything, which made `docker logs` close to useless:
on a real two-node run, 446 of 519 lines were httpx narrating its own requests
and two were the hub's. Meanwhile the things an operator actually wants — a
wrong password, a lockout — were not logged at all.

So the properties worth pinning down are the two failure modes of a logging
change: going quiet about something important, and drowning it in something that
is not.
"""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from app import logging_setup
from app.runtime import get_login_throttle

BAD = {"username": "admin", "password": "wrong"}
GOOD = {"username": "admin", "password": "supersecret"}


@pytest.fixture(autouse=True)
def _restore_logging():
    """Reinstate whatever pytest had set up, or one test blinds the next."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    saved_libraries = {
        name: logging.getLogger(name).level
        for name in logging_setup.CHATTY_LIBRARIES + logging_setup.ALWAYS_QUIET
    }
    saved_servers = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in logging_setup.SERVER_LOGGERS
    }
    yield
    for name, (handlers, propagate) in saved_servers.items():
        server = logging.getLogger(name)
        for handler in list(server.handlers):
            server.removeHandler(handler)
        for handler in handlers:
            server.addHandler(handler)
        server.propagate = propagate
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])
    for name, level in saved_libraries.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def _clear_throttle():
    get_login_throttle()._sources.clear()  # noqa: SLF001
    yield
    get_login_throttle()._sources.clear()  # noqa: SLF001


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_level_comes_from_the_setting() -> None:
    logging_setup.configure("WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_the_level_is_read_case_insensitively() -> None:
    """`ADGUARDHUB_LOG_LEVEL=debug` is what an operator will actually type."""
    logging_setup.configure("debug")
    assert logging.getLogger().level == logging.DEBUG


def test_an_unreadable_level_falls_back_to_info_and_complains() -> None:
    """Silence would be the worst answer: a typo must not switch logging off."""
    logging_setup.configure("verbose-please")
    root = logging.getLogger()
    assert root.level == logging.INFO

    # Collected on the hub's own logger: configure() clears the root's handlers,
    # so anything attached there would be swept away by the call being tested.
    records: list[logging.LogRecord] = []
    collector = _Collector(records)
    logging.getLogger("adguardhub").addHandler(collector)
    try:
        logging_setup.configure("verbose-please")
    finally:
        logging.getLogger("adguardhub").removeHandler(collector)
    assert any("verbose-please" in record.getMessage() for record in records)


def test_httpx_is_quiet_unless_someone_is_debugging() -> None:
    """The whole reason the log was unreadable."""
    logging_setup.configure("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING

    logging_setup.configure("DEBUG")
    assert logging.getLogger("httpx").level == logging.DEBUG


def test_configuring_twice_does_not_double_the_handlers() -> None:
    """Otherwise every line would appear once per call."""
    logging_setup.configure("INFO")
    first = len(logging.getLogger().handlers)
    logging_setup.configure("INFO")
    assert len(logging.getLogger().handlers) == first


def test_a_log_file_is_written_when_one_is_asked_for(tmp_path) -> None:
    path = tmp_path / "logs" / "adguardhub.log"
    logging_setup.configure("INFO", log_file=str(path))
    logging.getLogger("adguardhub").info("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert path.exists(), "the directory should have been created too"
    assert "hello from the test" in path.read_text(encoding="utf-8")


def test_a_log_file_that_cannot_be_opened_is_not_fatal(tmp_path) -> None:
    """A bad path is a misconfiguration, not a reason to refuse to start.

    stderr still carries everything, so the hub runs and says what went wrong.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    records: list[logging.LogRecord] = []
    collector = _Collector(records)
    logging.getLogger("adguardhub").addHandler(collector)
    try:
        logging_setup.configure("INFO", log_file=str(blocked / "adguardhub.log"))
        logging.getLogger("adguardhub").info("still running")
    finally:
        logging.getLogger("adguardhub").removeHandler(collector)

    assert logging.getLogger().level == logging.INFO
    assert any("Cannot write the log file" in r.getMessage() for r in records)
    assert any(r.getMessage() == "still running" for r in records), "logging must carry on"


def test_the_file_handler_rotates(tmp_path) -> None:
    """Unbounded is the one thing a log file must not be."""
    path = tmp_path / "adguardhub.log"
    logging_setup.configure("INFO", log_file=str(path), max_bytes=2048, backups=2)
    for index in range(200):
        logging.getLogger("adguardhub").info("line %d padded out %s", index, "x" * 80)
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["adguardhub.log", "adguardhub.log.1", "adguardhub.log.2"]
    total = sum(os.path.getsize(tmp_path / name) for name in written)
    assert total < 3 * 2048 + 4096, "rotation should be holding the total down"


class _Collector(logging.Handler):
    def __init__(self, into: list[logging.LogRecord]) -> None:
        super().__init__()
        self._into = into

    def emit(self, record: logging.LogRecord) -> None:
        self._into.append(record)


# --------------------------------------------------------------------------
# Sign-ins
# --------------------------------------------------------------------------


async def test_a_wrong_password_is_logged_with_its_source(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """It was logged nowhere at all before, which is how guessing stays invisible."""
    await auth_client.post("/api/auth/logout")
    with caplog.at_level(logging.WARNING, logger="adguardhub.auth"):
        await auth_client.post("/api/auth/login", json=BAD)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "Failed sign-in" in message
    assert "hub login" in message
    assert "127.0.0.1" in message, "the source address is the whole point of the line"


async def test_the_password_never_reaches_the_log(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A log an operator cannot safely share is a log they will not keep.

    Checked at DEBUG, the worst case, and against every logger rather than only
    the hub's — the first draft of this passed on the hub's own lines while
    aiosqlite quietly reproduced the attempted username as a bound parameter of
    the SELECT that looked it up.
    """
    logging_setup.configure("DEBUG")
    await auth_client.post("/api/auth/logout")
    with caplog.at_level(logging.DEBUG):
        await auth_client.post(
            "/api/auth/login", json={"username": "hunter2-in-the-wrong-box", "password": "s3cret!"}
        )

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "s3cret!" not in written
    # The username goes unlogged for the same reason: it is where a mistyped
    # password lands, and with one admin account the name says nothing anyway.
    assert "hunter2-in-the-wrong-box" not in written


def test_the_servers_own_lines_join_the_same_stream() -> None:
    """Uvicorn keeps its own handlers, so its access log used to be a second log.

    Same output on stderr but a different format, and — the part that actually
    matters — absent from the log file entirely.
    """
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.NullHandler())
    access.propagate = False

    logging_setup.configure("INFO")

    assert access.handlers == []
    assert access.propagate is True


def test_the_database_layer_stays_quiet_even_at_debug() -> None:
    """It logs every statement with its bound parameters — see ALWAYS_QUIET."""
    logging_setup.configure("DEBUG")
    assert logging.getLogger("aiosqlite").level == logging.WARNING


async def test_the_lockout_is_announced_once_not_once_per_attempt(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Whoever tripped the throttle decides how many refusals follow.

    Logging each one at warning would hand an attacker a way to flood the log —
    the same drowning-out this change exists to fix, from the other direction.
    """
    await auth_client.post("/api/auth/logout")
    throttle = get_login_throttle()

    with caplog.at_level(logging.WARNING, logger="adguardhub.auth"):
        for _ in range(throttle.max_failures + 20):
            await auth_client.post("/api/auth/login", json=BAD)

    lockouts = [r for r in caplog.records if "Locked out" in r.getMessage()]
    assert len(lockouts) == 1
    assert str(throttle.max_failures) in lockouts[0].getMessage()
    # And the 20 refusals afterwards added nothing at warning.
    assert len(caplog.records) == throttle.max_failures


async def test_every_door_reports_which_one_it_was(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Three ways in; an operator needs to know which is being knocked on."""
    await auth_client.post("/api/auth/logout")

    with caplog.at_level(logging.WARNING, logger="adguardhub.auth"):
        await auth_client.post("/control/login", json={"name": "admin", "password": "wrong"})
        await auth_client.get("/control/status", auth=("admin", "wrong"))

    doors = "\n".join(record.getMessage() for record in caplog.records)
    assert "AdGuard-compatible login" in doors
    assert "Basic Auth" in doors


async def test_a_successful_sign_in_is_logged_too(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await auth_client.post("/api/auth/logout")
    with caplog.at_level(logging.INFO, logger="adguardhub.auth"):
        assert (await auth_client.post("/api/auth/login", json=GOOD)).status_code == 200

    assert any("Signed in" in record.getMessage() for record in caplog.records)


async def test_basic_auth_success_is_not_logged_per_request(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """It re-authenticates on every request, so this would bury everything else."""
    await auth_client.post("/api/auth/logout")
    with caplog.at_level(logging.INFO, logger="adguardhub.auth"):
        for _ in range(5):
            response = await auth_client.get("/control/status", auth=("admin", "supersecret"))
            assert response.status_code == 200

    assert [r for r in caplog.records if "Signed in" in r.getMessage()] == []


def test_the_log_is_kept_on_disk_by_default(tmp_path, monkeypatch) -> None:
    """The default that the incident this changed was really about.

    stderr is captured by whatever started the hub, and what that keeps is not
    the hub's decision: `docker logs` holds the current container's output and an
    upgrade replaces the container; a native install lands in a journal that may
    be volatile. Both vanish when somebody restarts the hub — and restarting is
    the first thing anyone does when something is wrong, so the evidence was
    routinely destroyed by the act of looking for it.
    """
    from app.config import Settings

    settings = Settings(data_dir=str(tmp_path))
    assert settings.log_path == os.path.join(str(tmp_path), "adguardhub.log")


def test_an_explicit_path_still_wins(tmp_path) -> None:
    from app.config import Settings

    chosen = str(tmp_path / "elsewhere" / "hub.log")
    assert Settings(data_dir=str(tmp_path), log_file=chosen).log_path == chosen


def test_the_log_file_can_be_switched_off(tmp_path) -> None:
    """An operator who keeps their own copy should not be made to keep two."""
    from app.config import Settings

    settings = Settings(data_dir=str(tmp_path), log_file_enabled=False)
    assert settings.log_path == ""


def test_switching_it_off_beats_an_explicit_path(tmp_path) -> None:
    """Off means off — the same rule the drift archive follows."""
    from app.config import Settings

    settings = Settings(
        data_dir=str(tmp_path), log_file_enabled=False, log_file=str(tmp_path / "hub.log")
    )
    assert settings.log_path == ""
