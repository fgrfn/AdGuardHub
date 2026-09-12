"""Where the master key comes from, and what it refuses to be.

The hub encrypts the AdGuard admin passwords it stores, which is only worth
anything if the key is not in the same file. Two things made that untrue in
practice:

* with no key configured, one was generated per *process*, so the credentials
  became unreadable on the next restart — and the documented workaround was to
  paste the placeholder out of the compose file;
* that placeholder is published in a public repository, so a hub running with it
  had encryption that anyone could undo, and nothing anywhere said so.

So the key is now generated once and kept, and the published placeholders are
refused outright. The property that matters is the last test here: the same data
directory, a second process, credentials still readable.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.security import (
    PLACEHOLDER_KEYS,
    Crypto,
    SecretKeyError,
    is_ephemeral_key,
    resolve_secret_key,
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A clean data directory with no key configured."""
    get_settings.cache_clear()
    monkeypatch.setenv("ADGUARDHUB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADGUARDHUB_SECRET_KEY", raising=False)
    yield tmp_path
    get_settings.cache_clear()


def _configure(monkeypatch, value: str) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("ADGUARDHUB_SECRET_KEY", value)


# --------------------------------------------------------------------------
# A configured key
# --------------------------------------------------------------------------


def test_a_configured_key_is_used_as_given(monkeypatch) -> None:
    _configure(monkeypatch, "a-perfectly-good-long-secret-key")
    assert resolve_secret_key() == "a-perfectly-good-long-secret-key"
    get_settings.cache_clear()


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_KEYS))
def test_every_documented_placeholder_is_refused(monkeypatch, placeholder: str) -> None:
    """These are readable on GitHub, so the encryption would protect nobody."""
    _configure(monkeypatch, placeholder)
    with pytest.raises(SecretKeyError) as caught:
        resolve_secret_key()
    # The message has to say what to do, since this stops the hub from starting.
    assert "openssl rand" in str(caught.value)
    get_settings.cache_clear()


def test_the_compose_files_own_placeholder_is_among_them() -> None:
    """Pins the specific string that shipped, so renaming it cannot lose the guard."""
    assert "change-me-to-a-long-random-string" in PLACEHOLDER_KEYS


def test_a_short_key_is_warned_about_but_still_works(monkeypatch, caplog) -> None:
    """Weak-but-private is a different problem from published; refusing would be
    an outage for someone whose key is merely short."""
    _configure(monkeypatch, "short")
    with caplog.at_level("WARNING"):
        assert resolve_secret_key() == "short"
    assert any("characters" in record.message for record in caplog.records)
    get_settings.cache_clear()


def test_surrounding_whitespace_does_not_smuggle_a_placeholder_through(monkeypatch) -> None:
    _configure(monkeypatch, "  change-me-to-a-long-random-string\n")
    with pytest.raises(SecretKeyError):
        resolve_secret_key()
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# No key configured
# --------------------------------------------------------------------------


def test_a_key_is_generated_and_stored(data_dir) -> None:
    key = resolve_secret_key()
    assert not is_ephemeral_key(key)
    assert len(key) >= 32
    assert (data_dir / "secret.key").read_text(encoding="utf-8").strip() == key


def test_the_key_file_is_not_world_readable(data_dir) -> None:
    resolve_secret_key()
    mode = os.stat(data_dir / "secret.key").st_mode & 0o777
    assert mode == 0o600, f"expected 0600, found {mode:o}"


def test_the_same_key_comes_back_on_the_next_start(data_dir) -> None:
    first = resolve_secret_key()
    second = resolve_secret_key()
    assert first == second


def test_credentials_survive_a_restart_without_any_configuration(data_dir) -> None:
    """The whole point. Two processes, one data directory, no environment.

    Before this, each start invented its own key, so the stored AdGuard password
    became undecryptable the moment the container was recreated.
    """
    secret = Crypto(resolve_secret_key()).encrypt("the-adguard-admin-password")

    # A second start, with nothing carried over but the directory.
    restarted = Crypto(resolve_secret_key())
    assert restarted.decrypt(secret) == "the-adguard-admin-password"


def test_an_empty_key_file_is_replaced_rather_than_used(data_dir) -> None:
    """A half-finished write from a killed start must not become the key."""
    (data_dir / "secret.key").write_text("", encoding="utf-8")
    key = resolve_secret_key()
    assert key
    assert not is_ephemeral_key(key)
    assert (data_dir / "secret.key").read_text(encoding="utf-8").strip() == key


def test_an_unwritable_directory_still_starts_the_hub(data_dir, monkeypatch, caplog) -> None:
    """Falling back keeps the old behaviour — a warning, not an outage."""

    def refuse(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "open", refuse)
    with caplog.at_level("WARNING"):
        key = resolve_secret_key()
    assert is_ephemeral_key(key)
    assert any("NOT survive a restart" in record.message for record in caplog.records)


def test_an_existing_key_file_is_preferred_over_generating_one(data_dir) -> None:
    (data_dir / "secret.key").write_text("a-key-written-by-an-earlier-version\n", encoding="utf-8")
    assert resolve_secret_key() == "a-key-written-by-an-earlier-version"


# --------------------------------------------------------------------------
# What the diagnostics bundle says about it
# --------------------------------------------------------------------------


def _source(monkeypatch, *, ephemeral: bool = False) -> str:
    from app import runtime
    from app.services import diagnostics

    monkeypatch.setattr(runtime, "using_ephemeral_secret", lambda: ephemeral)
    monkeypatch.setattr(diagnostics, "using_ephemeral_secret", lambda: ephemeral)
    return diagnostics._secret_key_source()


def test_a_configured_key_is_reported_as_coming_from_the_environment(monkeypatch) -> None:
    _configure(monkeypatch, "a-perfectly-good-long-secret-key")
    assert _source(monkeypatch) == "environment"


def test_an_unset_key_is_reported_as_generated_not_as_missing(monkeypatch, data_dir) -> None:
    """The correction this field exists for.

    `secret_key_set: false` was read — by the person reading the bundle — as "no
    key, so credentials are re-encrypted on every boot". That has not been true
    since the hub started keeping a generated one, and acting on it meant fixing
    something that was working.
    """
    assert _source(monkeypatch) == "generated"


def test_only_an_unwritable_data_directory_is_reported_as_ephemeral(
    monkeypatch, data_dir
) -> None:
    """The one state of the three that actually loses credentials on restart."""
    assert _source(monkeypatch, ephemeral=True) == "ephemeral"
