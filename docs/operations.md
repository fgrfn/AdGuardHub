# Running it

- [Logs](#logs)
- [Reporting a problem](#reporting-a-problem)
- [Backup and restore](#backup-and-restore)
- [Notifications](#notifications)
- [Security](#security)

## Logs

Three different things are called a log here, and they are not the same:

- **The query log** is DNS traffic — what your clients asked for, aggregated across every
  instance. It lives in the UI and is what you use day to day.
- **The application log** is the hub talking about itself: what it did on start, a push that
  failed, a wrong password. It goes to stderr, so `docker logs adguardhub` (or
  `docker compose logs -f`) reads it back — **and to `<data dir>/adguardhub.log`**, which is
  the copy that survives a restart. See [Keeping the log](#keeping-the-log) below.
- **The drift archive** is every reconciliation finding, written to a file as it happens. See
  [The drift archive](#the-drift-archive) below.

The last 500 lines are also readable in the interface under *Settings → Log*, which follows
along live. That is a window, not a replacement: it lives in memory, is cleared on restart, and
your container or systemd log remains the record. Its own polling is deliberately left out of
it, so watching the log does not push what you came to read out of the buffer.

Those 500 lines are mostly the web server reporting requests, so the page narrows them three
ways: a search over the line, a minimum level (*Warnings and above* is the one you want when
something went wrong at a time you can name), and a source — the part of the hub that spoke,
`services.sync` and `services.reconcile` being the two the sync engine writes under. The
filters stack, the count on the right always says how many lines are being held back, and
*Show all* clears them. A level the hub does not recognise is never hidden by the level filter.

At the default `INFO` you get startup, schema migrations, notification failures, sign-ins, and
what the sync engine did: each push and where it went, a push that failed and was queued, a
push a node accepted and did not keep, a node going unreachable and coming back, and every
reconciliation pass that found something.

What is **not** at INFO matters as much. A reconciliation pass that found nothing says nothing,
and a node that has been down for an hour is reported once rather than on every pass — two
nodes on a five-minute timer would otherwise write several hundred lines a day to report that
nothing happened, which is how a log stops being read.

`ADGUARDHUB_LOG_LEVEL=DEBUG` adds both of those, and the per-instance diagnostics — why a node's stats
came back empty, why a query log poll returned nothing — which is what you want while something
is misbehaving and nothing but noise the rest of the time. It also turns on the HTTP client's
own narration, which is verbose: on a hub polling two nodes it accounts for the large majority
of all output.

**Sign-ins are logged.** A wrong password writes a `WARNING` naming the source address and
which door was knocked on (the hub's login form, the AdGuard-compatible one, or Basic Auth),
and the attempt that trips the rate limit says so once. The attempted username is deliberately
not logged: with a single admin account it tells you nothing you don't know, and logging it
would write a password to disk the first time someone types one into the wrong box.

### Keeping the log

The hub writes a rotating file to `<data dir>/adguardhub.log` (5 MB, three backups), alongside
stderr. **On by default**, and the reason is worth stating because it was learned the hard way.

stderr is captured by whatever started the hub, and what that keeps is not the hub's decision.
`docker logs` holds the current container's output — an upgrade replaces the container and the
history goes with it. A native install lands in the systemd journal, which on many systems is
volatile. The 500 lines under *Settings → Log* live in memory and are cleared on restart.

Every one of those disappears at a restart, and restarting is the first thing anybody does when
something looks wrong. So the log was routinely destroyed by the act of investigating it: a
reconciler stopped one Saturday afternoon, the operator restarted the hub, and the two hours
that would have said why were gone before anyone could read them. A log nobody switched on is
empty exactly when they discover they needed it — the same argument the drift archive is on by
default for.

`ADGUARDHUB_LOG_FILE` puts it somewhere else; `ADGUARDHUB_LOG_FILE_ENABLED=false` switches it
off, for a deployment that keeps its own copy and would rather not keep two. If the path cannot
be written, the hub says so and carries on with stderr rather than refusing to start.

Docker's default json-file driver keeps its copy **without any size limit**, which on a
long-running hub is a disk that fills quietly. The `logging:` block in the
[Compose example](install.md#docker-compose) caps it at three files of 10 MB; keep it.

### When a background timer stops

The hub runs three timers: the retry queue, reconciliation, and the query log poll. Each one
already survives a pass that goes wrong — a node that will not answer, a payload it rejects.
What none of them used to survive was the *loop itself* ending, because a cancellation is not
an ordinary error and slipped past the handler that catches those.

A worker that ended that way ended for good, and nothing said so. The hub carried on serving
requests exactly as though all three were running; the only thing in the system that knew was
the *Reconciliation* card saying the last pass was hours old.

Each timer is now supervised. One that ends for any reason other than shutdown is logged at
`ERROR` — naming which one, and whether it raised, was cancelled, or simply returned — and
started again. There is no attempt limit: a worker that keeps dying is broken and should keep
saying so, and a limit would restore the silence this exists to remove. The delay between
restarts grows to a minute, so a worker failing in a tight loop costs one log line a minute
rather than a busy CPU.

Which means: if *Reconciliation* ever reads **may have stopped** again, the log now says why.

There is one ending supervision cannot see, and it is the quietest: a pass that **hangs**. A
coroutine waiting for something that never comes never ends, so there is nothing to catch and
nothing to restart — it would sit there for weeks while every page in the hub rendered perfectly.

So a pass has a deadline: four fifths of the reconciliation interval, and never less than two
minutes. Most of the interval rather than all of it, because two passes overlapping is the worse
failure — both want the same per-node push lock, so the second would block behind the first for as
long as it ran. Never less than two minutes, because the interval can be set to 30 seconds and a
node fetching a large blocklist legitimately takes longer than that.

A pass that runs out of time is abandoned, said so plainly — *exceeded … and was abandoned* rather
than the word *failed*, because a pass that never came back needs a different answer from one that
went wrong — and the next runs on schedule. Nothing inside a pass is known to hang today; this is
the backstop for the one that is not known.

### Being told, rather than noticing

Between them, supervision and that deadline mean a stopped timer now ends and says so. Neither
says anything about a reconciler **switched off months ago and forgotten**, and neither puts the
news anywhere but the log. Until now the only thing in the hub that reported it at all was the
*Reconciliation* card: a panel somebody has to be looking at.

So the hub watches its own safety net, from outside it, because the reconciler cannot be the thing
that reports its own failure. When no pass has completed in three intervals it says so **once** —
a stall is true on every check by definition, and a message a minute is how a notifier stops being
read — and says so again once when it resumes. Both are notifier events like any other
(*Settings → Notifications*), so they reach Home Assistant, Discord or Gotify with everything else.

`GET /api/health` carries the same answer, unauthenticated, for an external monitor:

```json
{
  "status": "ok",
  "version": "0.7.6",
  "reconcile": {
    "enabled": true, "replicating": true,
    "last_pass_age_s": 42, "overdue_after_s": 2700, "stalled": false
  }
}
```

That path matters precisely when the hub is the part that is unwell: a webhook needs the hub
healthy enough to send one, and a monitor polling this does not. `status` stays `ok` regardless —
it is also what the container's own start-up check waits for, and a hub with nothing configured
yet is not unhealthy.

Three states are **not** a stall, and each is one a real hub spends time in: reconciliation
switched off (a decision), a hub with nothing to replicate (it deliberately does not reconcile at
all), and a hub that has not recorded a first pass yet (a restart has not missed anything). A
watchdog that cried wolf on any of those would be muted within a week, which would cost more than
it saved.

## The drift archive

The drift log on the dashboard is built to stay readable: 500 rows at most, one entry per
finding however often it repeats, rule lists cut to 25 items, and a *Clear log* button that
empties it. Every one of those is right for a live view and wrong for evidence — by the time
you sit down to work out when a fault started, the rows that would have said so are the ones
that went.

So each finding is also appended to `drift.log` beside the database, once per pass, untrimmed,
in the order it happened. One JSON object per line:

```json
{"at": "2026-09-08T19:44:02Z", "instance": "node-b", "payload_kind": "filters",
 "summary": "20 subscription(s) missing … — the correction could not be pushed: …",
 "corrected": false, "took_ms": 10004, "details": {"missing": ["blocklist:https://…"]}}
```

That format survives being grepped, tailed, piped through `jq` and pasted into an issue, and a
later version can add a field to it without invalidating what is already written.

*Settings → Log → Drift archive* reads it back, newest first, a page at a time. Unlike the
application log beside it, it is not followed live: it is history, and history does not need a
two-second refresh.

**It is on by default**, unlike the application log file above, and that is the point — an
archive nobody switched on is empty exactly when they discover they needed it. It costs a line
only when something drifts, so a healthy hub writes nothing for weeks. It rotates at 2 MB with
three backups, and `ADGUARDHUB_DRIFT_LOG_ENABLED=false` turns it off for a deployment that
would rather not write to its flash at all. A path that cannot be written is reported and
survived, like the application log file.

The archive is **not** redacted — it names your nodes and carries the domains in a finding,
because it is a file on your own disk. The [diagnostic bundle](#reporting-a-problem) is the
one built to be handed to someone else; do not paste the archive into a public issue without
reading it first.

## Reporting a problem

*Settings → Diagnostics → Download diagnostics* produces one JSON file that answers the
questions a report otherwise takes four replies to establish: which version, installed how,
how many nodes and in what state, what is sitting in the retry queue, what reconciliation has
been finding, and the last 200 lines the hub logged.

It is **not** a backup, and the difference decides what is in it. A backup stays with you; this
gets pasted into a public issue. So on top of the rule a backup already follows — no passwords,
in the clear or encrypted — this one also drops everything that says *where* a node is or *who*
signs in to it:

| Left out | Kept |
| --- | --- |
| Node names, addresses, usernames | Scheme, port, and whether the address is a name or an IP |
| The path of a notifier URL — a Discord webhook URL *is* its credential | The notifier's type and host |
| The values inside a settings area — clients, MACs, resolvers | Which areas are replicated, and their key names |
| Client and hub addresses in log lines | Public addresses, such as upstream resolvers |

Every node becomes `node-1`, `node-2` **everywhere in the file**, including inside error strings
and log lines — "connecting to `http://10.10.10.252/control/status`: timed out" is a `last_error`,
a queued job's error and a log line, so redacting the column and leaving the sentence would be
security theatre. Keeping the pseudonym consistent is what lets a drift entry still be matched to
the node's state and to the log lines about it.

What deliberately **stays in** is your filtering content: rule text, subscription addresses (minus
any query string, where a self-hosted list would carry a token), and the domains in a drift entry.
"This allow rule will not stick" is unanswerable without the rule. It is plain JSON, so read it
before you post it.

## Backup and restore

Everything the hub owns lives in one SQLite file, so *Settings → Backup* offers it as a
single JSON document: rules, subscriptions, instance settings, and the list of instances.

```bash
curl -u admin:yourpassword http://adguardhub.lan/api/backup -o adguardhub-backup.json
```

**Instance passwords are never in it.** A backup is downloaded through a browser and then
lives wherever you put it; ciphertext would be no better, since it is one leaked key away
from the plaintext — and by default that key sits in the same directory as the database it
protects. Restored instances therefore come back needing their password typed in again, and
the restore says how many.

That is also why this JSON file is *not* a substitute for backing up `/data`: it deliberately
leaves out the credentials, so restoring from it always costs you a round of retyping. A copy
of the data directory — database and `secret.key` together — restores everything.

Restoring replaces the hub's rules, subscriptions and instance settings and pushes the
result to every node. Two things make that safe to try: the file is validated in full
*before* anything is written, so a wrong file leaves the hub untouched; and the state it
replaces stays in the version history, so a restore is undone by rolling back to it.

A node already connected keeps its credentials — a restore adds what is missing rather
than overwriting what works.

## Notifications

Configure any number of webhook targets under *Settings → Notifications*. Each can subscribe to
specific events or to all of them:

| Event | Fires when |
| --- | --- |
| `reconcile.fixed` | Reconciliation found (and corrected) drift on an instance |
| `instance.unreachable` | An instance stopped responding |
| `instance.recovered` | An instance started responding again |
| `push.failed` | A push failed and went into the retry queue |

The two instance events are **edge-triggered**: one message when a node goes,
one when it comes back, however long the outage lasts and however often the hub
polls in between. A target that subscribes to *all* events picks the new one up
automatically; a target that lists its events explicitly has to add it, since
silently widening a subscription you configured would be the wrong default.

- **Home Assistant** — point it at `http://<ha>:8123/api/webhook/<id>` and trigger an automation
  on that webhook. The JSON body carries `event`, `title` and `message`.
- **Discord** — paste an incoming webhook URL.
- **Gotify** — use `https://<gotify>/message` and put the application token in the token field.

## Security

AdGuardHub is meant to run **inside your network**, alongside the AdGuard instances. It has a
single admin account (bcrypt-hashed password, signed session cookie) and no multi-user model.
Exposing it to the internet is not a supported deployment — put it behind a VPN or keep it on the
LAN.

Sign-ins are rate limited: ten failed attempts from one address within five minutes are answered
with `429` and a `Retry-After` until the window passes. The limit is shared by all three ways in
— the login form, `/control/login` and Basic Auth — and is applied *before* the password is
hashed, so a locked-out source costs a dictionary lookup rather than the ~300 ms bcrypt spends.

Failures are counted **per source address, never per account**. With one admin, locking the
account would hand any device on the network a way to lock you out of your own hub. For the same
reason the source is the connection's peer and not `X-Forwarded-For`: a header the client sets is
a header the client can vary. Behind a reverse proxy that means attempts are counted against the
proxy.

The AdGuard admin passwords the hub stores are encrypted at rest, with the key held outside the
database — see [The encryption key](install.md#the-encryption-key).
