"""Application settings, read from the environment."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADGUARDHUB_", extra="ignore")

    # Where the SQLite file lives. The Docker image mounts /data as a volume.
    data_dir: str = "./data"

    # Master secret. Used both for the session cookie signature and to derive the
    # Fernet key that encrypts instance credentials at rest. Never persisted to the DB.
    secret_key: str = ""

    # Optional bootstrap of the single admin account. When unset, the UI walks the
    # operator through a first-run setup instead.
    admin_username: str = ""
    admin_password: str = ""

    session_cookie: str = "adguardhub_session"
    session_max_age: int = 60 * 60 * 24 * 14  # 14 days

    # Background workers (seconds).
    #
    # Fifteen minutes rather than five, because propagation does not ride on this
    # timer: every change is pushed the moment it is made, and a node that was
    # unreachable is caught by the retry queue on ``retry_interval``. What is left
    # for the timer is drift somebody caused outside the hub — and correcting that
    # within a quarter of an hour is ample, where re-reading both nodes' entire
    # configuration 288 times a day to find nothing is not.
    reconcile_interval: int = 900
    retry_interval: int = 30
    querylog_poll_interval: int = 5
    querylog_buffer_size: int = 2000
    querylog_fetch_limit: int = 100

    http_timeout: float = 10.0

    # Whether to ask GitHub for the newest release. The default for a fresh
    # database; after that the UI owns it.
    update_check: bool = True

    # Serve the built frontend from this directory when it exists.
    static_dir: str = "./static"

    # DEBUG turns on the per-instance diagnostics — why a node's stats or query
    # log came back empty — which are too chatty to carry at INFO but are exactly
    # what is wanted while something is misbehaving.
    log_level: str = "INFO"

    # On by default and beside the database, for the same reason the drift
    # archive below is: stderr is captured by whatever started the hub, and what
    # that keeps is not the hub's decision. `docker logs` holds the current
    # container's output and loses it when the container is replaced, which is
    # exactly what an upgrade does; a native install lands in the journal, which
    # may be volatile. Both of those go away at the moment somebody restarts the
    # hub — and restarting is the first thing anyone does when something is
    # wrong, so the log was routinely destroyed by the act of investigating it.
    #
    # Empty means <data_dir>/adguardhub.log; see log_path.
    log_file_enabled: bool = True
    log_file: str = ""
    log_file_max_bytes: int = 5 * 1024 * 1024
    log_file_backups: int = 3

    # The drift archive: every reconciliation finding, appended as it happens,
    # untrimmed. On by default and beside the database, unlike the application
    # log file above — an archive nobody switched on is empty exactly when they
    # discover they needed it, and it costs a line only when something drifts.
    drift_log_enabled: bool = True
    # Empty means <data_dir>/drift.log; see drift_log_path.
    drift_log_file: str = ""
    drift_log_max_bytes: int = 2 * 1024 * 1024
    drift_log_backups: int = 3

    @property
    def database_path(self) -> str:
        return os.path.join(self.data_dir.rstrip("/"), "adguardhub.db")

    @property
    def log_path(self) -> str:
        """Where the hub's own log is written, or empty when it is switched off."""
        if not self.log_file_enabled:
            return ""
        return self.log_file or os.path.join(self.data_dir.rstrip("/"), "adguardhub.log")

    @property
    def drift_log_path(self) -> str:
        """Where the archive is written, or empty when it is switched off."""
        if not self.drift_log_enabled:
            return ""
        return self.drift_log_file or os.path.join(self.data_dir.rstrip("/"), "drift.log")

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
