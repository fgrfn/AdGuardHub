<p align="center">
  <img src="./logo.svg" width="140" alt="AdGuardHub logo" />
</p>

<h1 align="center">AdGuardHub</h1>

<p align="center">
  One dashboard to manage multiple AdGuard&nbsp;Home instances as a single system.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-in%20development-yellow" alt="status" />
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license" />
  <img src="https://img.shields.io/badge/stack-FastAPI%20%2B%20React-4C9A6A" alt="stack" />
</p>

<p align="center">
  <img src="./docs/screenshots/dashboard-light.png" width="900" alt="The AdGuardHub dashboard: traffic summed across every node, top domains and clients, and the hub's own sync state" />
</p>

<p align="center">
  <sub>From a local demo against two test instances, so the numbers are made up — the interface is not.
  <a href="./docs/interface.md#screenshots">More screenshots</a>.</sub>
</p>

---

## The problem

If you run more than one AdGuard Home instance for DNS failover, you've probably hit this: a
client fails over to instance **B**, you whitelist a domain there, and the next config sync from
instance **A** silently overwrites it. Simple A→B sync tools only push config in one direction —
they don't know (or care) that the whitelist you just added is the one that matters.

**AdGuardHub** fixes this by removing the need to sync between instances at all.

## How it works

AdGuardHub becomes the **single source of truth** for filtering rules, blocklist subscriptions,
and instance settings. You never touch the native AdGuard UI again — every change goes through
the hub and is pushed to **all** connected instances at once.

- **Instant push** on every change, to every instance
- **Best-effort, no rollback** — an unreachable instance never delays the others; its update
  goes to a retry queue and is applied as soon as it comes back
- **Reconciliation job** as a safety net — detects and auto-corrects drift (e.g. after downtime,
  or a change made in the native UI anyway), and logs every correction so nothing happens
  silently
- **Aggregated query log** across all instances, so you can whitelist a blocked domain no matter
  which instance saw it
- **Dynamic instance management** — add, remove, or disable AdGuard instances from the UI, no
  config file edits
- **Full configuration replication** — not just filtering rules: DNS and upstreams, clients,
  encryption, access control, rewrites, blocked services, protection toggles and logging.
  DHCP is deliberately excluded, since leases and interface bindings are per-host state
- **Version history** — every change is snapshotted, so you can see what a sync carried,
  diff any two points, and roll back (the rollback is recorded too, so it can be undone)
- **Backup and restore** — everything the hub owns as one JSON document, validated in full
  before anything is written
- **AdGuard-compatible API** — point an existing client (a phone remote, Home Assistant, a
  script) at the hub instead of at one node, and what it changes is pushed everywhere
- **Notifications** via configurable webhooks to Home Assistant, Discord, or Gotify

## Quick start

With Docker Compose. Save this as **`docker-compose.yml`** in a directory of your choice — that
directory becomes the hub's home, and every `docker compose` command, including the ones that
upgrade it later, is run from inside it:

```yaml
services:
  adguardhub:
    image: ghcr.io/fgrfn/adguardhub:latest
    container_name: adguardhub
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./data:/data
    environment:
      PUID: "1000"   # Unraid: 99
      PGID: "1000"   # Unraid: 100
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

```bash
docker compose up -d
```

`./data` is the directory you will be backing up — put it where your other service data lives if
you would rather not have it next to the compose file. [The options](./docs/install.md#where-the-data-goes),
including the one that matches the native install.

Or, on Debian/Ubuntu with systemd, without Docker — read it first, it runs as root:

```bash
curl -fsSL https://raw.githubusercontent.com/fgrfn/adguardhub/main/install.sh | sudo sh
```

Then open the hub in a browser — `http://<host>`, the machine you just started it on, whose
address the native installer prints when it finishes — and create the admin account. Add your
AdGuard Home instances under *Instances*, import one of them as the master, and work only in the
hub from then on.

Those two are the supported ways in. [Installing](./docs/install.md) covers the native path in
full, file permissions on a bind mount, and the encryption key that protects the stored
credentials.

## Documentation

| | |
| --- | --- |
| [Installing](./docs/install.md) | Compose, the native installer, first run, and how to upgrade both |
| [Configuration](./docs/configuration.md) | Every `ADGUARDHUB_*` environment variable |
| [The interface](./docs/interface.md) | What the pages do, and screenshots of all of them |
| [Replication](./docs/replication.md) | How push and reconcile work, which settings areas are replicated, version history |
| [Running it](./docs/operations.md) | Logs, backup and restore, notifications, security |
| [AdGuard-compatible API](./docs/api.md) | Pointing a phone remote or Home Assistant at the hub |
| [Development](./docs/development.md) | Local setup, the test suites, translations, project layout |
| [Releases and roadmap](./docs/roadmap.md) | What each version brought, and what is next |
| [Security policy](./SECURITY.md) | Reporting a vulnerability privately, and what is in scope |

## Status

Pre-1.0 and in daily use. Breaking changes are allowed between minor versions until v1.0, which
is when the feature set has settled rather than when a particular feature lands. Per-client rule
scoping and multi-user accounts are not planned before then, and support for DNS filters other
than AdGuard Home is not planned at all — see the [roadmap](./docs/roadmap.md).

## License

[MIT](./LICENSE)
