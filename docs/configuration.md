# Configuration

All settings are environment variables prefixed with `ADGUARDHUB_`. In a container they go in
`environment:`; on a native install they go in `/etc/adguardhub/adguardhub.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADGUARDHUB_SECRET_KEY` | *(generated into `/data/secret.key`)* | Signs sessions and derives the credential encryption key. Optional — see [The encryption key](install.md#the-encryption-key). |
| `ADGUARDHUB_DATA_DIR` | `/data` | Where `adguardhub.db` lives. |
| `ADGUARDHUB_ADMIN_USERNAME` | — | Creates/updates the admin account on start. |
| `ADGUARDHUB_ADMIN_PASSWORD` | — | Password for the above. |
| `ADGUARDHUB_RECONCILE_INTERVAL` | `900` | Seconds between drift checks. Your own changes are pushed instantly and never wait for this — see [How often the safety net runs](replication.md#how-often-the-safety-net-runs). |
| `ADGUARDHUB_RETRY_INTERVAL` | `30` | Seconds between retry-queue passes. |
| `ADGUARDHUB_QUERYLOG_POLL_INTERVAL` | `5` | Seconds between query log polls. |
| `ADGUARDHUB_QUERYLOG_BUFFER_SIZE` | `2000` | Entries kept in the in-memory log buffer. |
| `ADGUARDHUB_SESSION_MAX_AGE` | `1209600` | Session lifetime in seconds. |
| `ADGUARDHUB_HTTP_TIMEOUT` | `10` | Per-request timeout when talking to instances. |
| `ADGUARDHUB_UPDATE_CHECK` | `true` | Seeds whether the hub looks for new releases — see [Staying up to date](install.md#staying-up-to-date). |
| `ADGUARDHUB_LOG_LEVEL` | `INFO` | `DEBUG` adds the per-instance diagnostics — see [Logs](operations.md#logs). |
| `ADGUARDHUB_LOG_FILE_ENABLED` | `true` | Keep a rotating log file. Off means stderr only — see [Keeping the log](operations.md#keeping-the-log). |
| `ADGUARDHUB_LOG_FILE` | `<data dir>/adguardhub.log` | Where that file goes. |
| `ADGUARDHUB_LOG_FILE_MAX_BYTES` | `5242880` | Rotate the log file once it reaches this size. |
| `ADGUARDHUB_LOG_FILE_BACKUPS` | `3` | How many rotated log files to keep. |
| `ADGUARDHUB_DRIFT_LOG_ENABLED` | `true` | Keep the drift archive — see [Logs](operations.md#logs). |
| `ADGUARDHUB_DRIFT_LOG_FILE` | — | Where to keep it. Empty means `drift.log` beside the database. |
| `ADGUARDHUB_DRIFT_LOG_MAX_BYTES` | `2097152` | Rotate the archive once it reaches this size. |
| `ADGUARDHUB_DRIFT_LOG_BACKUPS` | `3` | How many rotated archives to keep. |

`ADGUARDHUB_VERSION` is not in that list on purpose: it is build metadata, baked into the
image from the release tag by the Release workflow, not something to set at runtime.

The four interval/buffer timers only seed the initial values. Once the hub has started they are
edited under *Settings → Sync & timers* and take effect on the next worker cycle — no restart.
