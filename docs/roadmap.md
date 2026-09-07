# Releases and roadmap

## What each release brought

**v0.1.0** was the MVP: instance management, the central rule model with instant push,
reconciliation with a visible drift log, the aggregated query log, subscription management,
single-user login, and the three notifier types.

**v0.2.0** is what the daily use of it asked for, in roughly that order — full configuration
replication rather than rules alone, version history with diff and rollback, the
AdGuard-compatible `/control` API so phone remotes and Home Assistant can point at the hub,
German alongside English, backup and restore, rate-limited sign-ins, and caps on the tables
that used to grow without end.

**v0.3.0** made the hub say what it knows about itself: update checks against this repository,
a one-click self-update for native installs, and a native installer for Debian and Ubuntu.

**v0.4.0** is mostly about finding things. The top bar dropped from nine entries to seven and
the pages that cover more than one thing grew tabs, so Settings is five linkable pages rather
than six cards stacked down one. Each node now says whether a newer AdGuard Home is waiting for
it — and says so distinctly when it could not be asked, which is not the same as being current.
The hub's own log is readable in the interface, so diagnosing it no longer starts with finding
a shell. The README became a page again, with everything past the quick start moved into these
documentation pages.

> **Do not install v0.4.0 natively.** On Python 3.13 — the default on Debian 13 — it comes up
> with an async database engine and no greenlet to drive it, dies on its first query, and is
> restarted by systemd every five seconds while the installer prints that the hub is running.
> The Docker image is unaffected: it is built on Python 3.12, where the dependency is installed.
> v0.4.1 fixes it.

**v0.4.1** is that fix, and the pipeline changes that should have caught it. greenlet is now a
dependency this project states itself rather than one it hoped to inherit — SQLAlchemy declares
it only for Python below 3.13 — and the installer waits for the hub to answer `/api/health`
before it claims anything, so a crash loop can no longer look like a successful install. CI
tests both Python versions and starts the container rather than only building it. The Updates
card also stopped offering one Docker command to people who never had a compose file.

**v0.4.2** hardens the command this project invites people to pipe into a root shell. `curl`
without `-f` prints the server's error body and exits zero, so a URL that answers "Not Found"
sends those words to `sh`, which tries to run them. Every documented form now passes `-f`, and
the install one-liner points back at `raw.githubusercontent.com` — the shorter domain in front
of it turned out to serve nothing, which is how the missing flag was noticed.

**v0.4.3** ends a loop that had been running since the first two-node install. Reconciliation
compared the hub's `time_zone: "Local"` against a node's `Europe/Berlin` and called it drift,
corrected it by sending `Local` again, and found the same difference on the next run — every few
minutes, forever, writing a drift event and firing a notification each time. `Local` is not a
zone but an instruction to use the node's own, so the comparison was holding a request next to
its answer. Only nodes whose clock knows where they are were affected, which is why it showed on
one node of two.

Alongside that: `docker run` is no longer a supported way in — everything the compose file
carries lives in shell history when you start a container that way — and the installation docs
now say which side of the volume mount is yours to choose, and to open the hub at the host's
address rather than at localhost.

**v0.4.4** is about the release pipeline rather than the hub. The published image name is
written down instead of derived from the repository — renaming this repository would otherwise
have started publishing to a different GHCR package while every existing `docker compose pull`
kept pointing at the old one, which stops receiving updates without ever failing. And the
release now carries a `SHA256SUMS` that the installer checks before unpacking a tarball into
`/opt` as root. That check catches a truncated download or the wrong mirror, not a compromised
GitHub — the sums travel from the same place as the archive, and defending against that needs a
signature checked against a key held in advance, which this project does not publish.

**v0.4.5** brought maintenance mode, which had been written down as a v1 non-goal twice.
Working on a node used to mean fighting the hub: a push overwrote what you had just done, and
reconciliation put it back within five minutes. The only lever was disabling the instance, which
also makes the hub forget what it still owes that node. Maintenance stops pushes and
reconciliation for one instance while queueing what it misses, so releasing it replays the queue
at once rather than waiting for the retry timer. A feature in a patch release is not what
SemVer intends; it went out that way because it was the fix to a problem in front of us, and the
number is left as it was rather than rewritten after the fact.

**v0.5.0** is the first minor since the 0.4 line settled, and both halves are about a number the
interface was not showing.

The drift log can now be cleared. A drift entry had no way out — once written it stayed until
the 500-row cap pushed it off the end — which is right while a finding is live and wrong once
its cause is gone, as with the time-zone loop above that filled the log with a fault that never
existed. The confirmation says what the button does not do: deleting the record does not resolve
anything, and a node that still disagrees is found again on the next run. The retry queue
deliberately gets no such button, because a pending job is work still owed to an instance.

Filter lists now show how many rules each subscription holds, and the active total. The hub
never stores the contents of a list, so it cannot answer that from its own database — the
numbers come back from the nodes, and the interface says so where that matters: a list no node
has fetched yet shows a dash rather than a zero, an unreachable fleet means the sizes are
unknown rather than empty, and two nodes reporting different sizes (they refresh on their own
schedules) is marked and broken down per node rather than averaged into one number. None of it
reaches reconciliation: a rule count is an observation about a file, not configuration the hub
owns, so a difference in one is never drift.

**v0.6.0** fixes the update button on any hub that had already updated once. Pressing it
appeared to do nothing: the confirmation closed and an ordinary idle button came back, with no
progress and no log. The upgrade was running the whole time — a browser reload a few minutes
later showed the new version — but nothing said so, and pressing again hit a hub that was
already restarting, which is where the `failed to fetch` came from.

The updater truncates its log when it starts, so between the press and the systemd path unit
firing, the only log on disk is the previous upgrade's, still ending in its own `[exit 0]`. The
hub read that marker as this run's outcome, reported the request as neither running nor
finished, and the interface never began watching. A fresh install has no such log, which is why
the fault only ever appeared from the second upgrade onward. A log is now attributed to the
previous run when it is older than the request, not only when it is older than fifteen minutes.

Alongside it, the release notice grew the counterpart it was missing. The banner announcing a
new version is dismissible on purpose — a bar you cannot get rid of teaches you to stop reading
bars — but dismissing it used to make the release invisible, leaving only a settings page you
had to already suspect. A dot now sits beside *Settings* in the top bar for as long as a newer
release exists: not dismissible, never in the way, gone when the hub is current. The
interruption and the standing fact are two different things rather than one thing asked to be
both.

**v0.6.1** is the other half of that upgrade, found by performing it. Both faults are about the
window in which a hub is mid-upgrade rather than about the hub itself.

The interface was unreachable for far longer than the upgrade took, and the journal disagreed:
two installs of ninety-three seconds, both clean, the health check passing before the installer
printed anything. The hub was up the whole time — the browser was not looking at it.
`index.html` was served with no `Cache-Control` at all, which leaves a browser free to invent
its own freshness and reuse the page without asking. The bundles that page names carry a content
hash and an upgrade deletes the old ones, so the stale page asked for scripts that were gone and
rendered nothing, until the invented freshness expired. That is why pressing F5 had always
"fixed" it. The unhashed files now revalidate on every load; the hashed ones take the opposite
rule and are immutable for a year, since a name there never outlives its contents.

The other minute was pip, run against `requirements.txt` on every upgrade and spending most of
it proving that what is installed is installed — and that is not idle time, because the hub's
files have already been replaced by then. A stamp beside the venv now records what the last
successful install was for: the hash of `requirements.txt` and the interpreter's version, both,
since a distribution upgrade that moves `python3` leaves a venv nothing can import from. It is
written only after pip succeeds, so a failed upgrade cannot tell the next run there is nothing
to do, and it is trusted only if the venv still imports what the hub needs to start — v0.4.0
shipped without greenlet on Python 3.13 and died on its first query, which is the shape of fault
a skipped pip run could otherwise bring back in silence.

**v0.6.2, v0.6.3 and v0.6.4** are one line on the Instances card, in three attempts:

```
VERSION
v0.107.79   [update to v0.107.79]
```

**v0.6.2** is the arithmetic. AdGuard's `/control/version.json` does not carry a
`current_version` field at all, so the comparison guarding that button read an empty string and
every node was reported as being behind itself. The version the hub asked the node for moments
earlier is the honest thing to compare against, so it is passed in; a build that does send its
own is still believed over the hub's copy. The leading `v` stopped counting as a difference —
`/control/status` answers `v0.107.79` where other endpoints in the same API answer `0.107.79`,
which would have reintroduced the same false positive by another route — and no version to
compare against now answers "could not find out" rather than "there is an update".

**v0.6.3** is the admission that fixing the arithmetic changed nothing anyone could see. These
fields are written by reconciliation and read back until the next run, so every existing hub
already held a row saying its nodes were behind, and nothing between two runs re-examined it: a
corrected comparison changed what would be written in five minutes' time and left every card
exactly as it was. An update that is not newer than the version beside it is now simply not
shown, which takes effect on the hub that already has the row, immediately, with no migration.
`check_instance` re-examines the row as well, so pressing *Test* — which is what anyone would
try — clears it rather than only hiding it. The comparison moved to `app/semver.py`, shared with
the hub's own update check.

**v0.6.4** is what was left once the state was right and still read as a fault. "v0.107.79 update
unknown" is two bare words that say nothing, and the explanation lived in a `title` attribute
while the screenshots came from a phone, where there is no hover — so the reason was unreachable
exactly where the card is hardest to read. The reason is the label now. A container's AdGuard
Home ships with its own updater switched off, which is normal and permanent, and the card says
so instead of implying something went wrong. The adapters' reasons go through
`dynamic-keys.json` like every other backend-provided string, so the German interface gets
German; an unfamiliar transport error falls through as itself, which is still better than
silence.

**v0.6.5** goes after the class of fault behind the v0.4.3 time-zone loop rather than another
instance of it. Reported from a real fleet: the same allow rule pushed to both nodes every five
minutes and logged as "1 rule(s) missing, corrected" each time, for hours. AdGuard answered 2xx
and did not keep the rule. The hub called that a correction, rediscovered it on the next run,
corrected it again, and wrote a drift event and fired a notification every time — while knowing,
on each of those runs, that its own correction had not taken. v0.4.3 fixed one comparison and
left the assumption underneath it: that a push which did not error had landed.

A correction is now read back, and only what the node actually holds counts as fixed. What
survives is reported as what it is — "the node did not keep this correction", with the items
that remain — and stated once rather than on every run, since a refusal repeats by definition;
it is said again when it changes, and ordinary reporting resumes the moment the node keeps it.
Pushing continues throughout, because a rule set is pushed whole and holding it back over one
refused line would strand every other line with it. The instant-push path learned the same
thing: press *Push now* on a node that will not keep a rule and the rule is named in the answer,
not in a drift entry five minutes later.

The important half is what a refusal is *not*. Not queued for a retry — that queue exists for a
node that was unreachable, and a node that answered and refused will refuse again, so feeding it
there rebuilds the same loop one layer down. Not marked unreachable, because it answered, and
saying otherwise would send an outage notification about a node serving DNS perfectly well. Not
recorded as a completed sync, because it was not one. None of this makes the refused rule
arrive; whether AdGuard keeps what it is given is AdGuard's decision. What changes is that the
hub says so, once, instead of hiding it inside a correction that reads as success.

**v0.6.6** is about being able to find out what happened, after a week in which three faults
were diagnosed by asking for screenshots and a paste of `journalctl`.

The sync engine wrote three log statements in total, all of them only when a whole pass crashed.
A push that failed for one instance, a node going unreachable, a correction that did not hold —
none of it reached the log; it went to the database and to notifications, which hold state per
object and per event, and neither of those is the order things happened in. Each push and where
it went is now logged, along with a queued failure, a refusal, a node's transitions and every
reconciliation pass that found something. What stays quiet matters as much: a pass that found
nothing says nothing, and a node down for an hour is reported once, on the transition, rather
than on every pass — two nodes on a five-minute timer would otherwise write several hundred
lines a day to report that nothing happened, which is how a log stops being read.

The hub keeps five hundred lines and most of them are uvicorn reporting requests, so the log
page gained filters by text, by minimum level and by source. They run in the browser on purpose:
the view already holds the same five hundred lines the hub does, so filtering server-side could
not reach one line further back. The rule that matters is tested on its own — a level this build
does not recognise is shown, never hidden.

And *Settings → Diagnostics* now produces the whole picture as one file: version and install
method, each node's state, the retry queue, recent drift, and the last two hundred log lines.
It is not a backup, and that difference decides what may be in it, because a backup stays with
the person who downloaded it and this gets pasted into a public issue. So on top of the rule a
backup already follows — no passwords, encrypted or otherwise — this one drops everything that
says where a node is or who signs in to it, and the substitution runs over free text as well as
over columns: a node's address turns up inside `last_error` and in every log line about the push
that failed, so redacting the column and leaving the sentence would be theatre. Each node reads
as `node-1`, `node-2` consistently across every section, since naming a node is what leaks and
correlating one is the entire point of the file. The filtering content stays — rules,
subscription addresses and the domains in a drift entry are what most reports are about — and
the page says so before the download rather than leaving it to be discovered afterwards.

**v0.6.7** finishes what v0.6.5 started. A reconciliation pass can end three ways for one
payload kind: the node kept the correction, the node took it and did not keep it, or the push
itself errored and the node never saw it. Only the middle one said so. The third reached the
operator as the bare word "detected", five minutes apart for as long as the fault lasted, with
the reason attached to a field nothing persists. The drift row now carries it. Underneath that
was a second fault: one `try` block wrapped the whole loop over the correctable differences, so
a settings section an AdGuard build rejects meant the rule set was never corrected either. Each
kind is attempted on its own now, which is the best-effort-with-no-rollback rule the push path
already follows (spec §6).

**v0.6.8** is the same lesson one level up. A push sends three payloads — rules, subscriptions,
settings sections — and each was verified straight after its own write, which quietly assumed
the three are independent. They are not: AdGuard reconfigures itself on every configuration
change, and a later reconfigure can undo what an earlier one put there. The rules were therefore
read back before the subscriptions and the sections had even left the hub, so a section write
that dropped the rule set found the hub had already recorded the rules as landed. The operator
saw "corrected", and the rule was gone by the next reconciliation run — which is exactly the
loop the read-back was built to stop. Everything is now read back at the end of the push, so a
payload undone by a later write in the same push is caught and named like any other refusal.

**v0.6.9** is two things the interface was not showing. Seven keys in the DNS section were read
from the node, stored, replicated and rolled back, and had no form field — reachable only
through *Edit raw document*. The upstream timeout is the one that hurts: it decides how long a
client waits before the fallback servers are tried at all, which is the whole point of running
two nodes. The rate limit's help text was wrong in the direction that matters, too, saying "per
client" where AdGuard counts per address block; at the default `/24` every device on a subnet
shares one limit of a hundred queries a second, and hitting it looks to a client exactly like a
DNS outage. A test now asserts that every replicated key has a field or a written-down reason
for not having one, and that no field edits a key the push does not send.

The subscriptions page also split into *All / Blocklists / Allowlists*, each tab carrying its
own count whether or not it is open. A "(0)" beside *Allowlists* is the answer to "why is my
exception list doing nothing", and an allowlist filed as a blocklist is easy to do and invisible
until something looks wrong.

**v0.7.0** is a security pass over the code that had accumulated since v0.2.0, plus the licence
file the README had been promising since the first commit.

The findings, in the order they matter. *Test connection* decrypts a saved node's password when
the form's password field is left blank — but logged in to the address in the form rather than
the saved one, so changing the URL and pressing *Test* delivered that node's password to
whatever host was typed. The stored password is now used only when the address still matches.
A session cookie said which user it was for and nothing about which password it was issued
under, so changing the password revoked nothing and there was no way to end a session at all;
the cookie now carries a digest of the hash it was issued under, and cookies from before the
change are refused once, on upgrade. The sign-in forms ran bcrypt on the event loop — three
hundred milliseconds during which no push reached a node and every other request waited, on the
one route anyone on the network can reach without a session — and answered an unknown username
in a millisecond, which reads the admin's username out of the timing. Both now go through one
function, in a worker thread, against a decoy hash when there is no account. `/control`'s
handlers checked the AdGuard-compatible API switch *after* FastAPI had resolved the user, so
turning the surface off left the password door open behind the 404; the check is a router
dependency now. A node's `announcement_url` was rendered as a link href unfiltered, so a node
answering with a `javascript:` URL put a script on the operator's page running under the hub's
own session; only `http` and `https` survive now. The session cookie is marked `Secure` when the
request arrived over https — per request, not by configuration, since over plain http a `Secure`
cookie is one the browser never sends back. Backup validation checked the shape of the four
lists but not what was in them, so a value the hub never produces was written and broke whatever
read it back; each entry is now checked as the API would check it. The diagnostic bundle masked
private IPv4 and let IPv6 and MAC addresses through, which name the same hosts just as readily.
`pip-audit` reported eighteen advisories across `cryptography` 44.0.0 and the `starlette` that
`fastapi` 0.115.6 pulls in, both pinned since December 2024; both moved, and `react-router` went
to 7 for two moderate advisories that were not reachable here but are only fixed there.

Two faults found alongside them are not security bugs but were worth the same pass.
Reconciliation wrote the node's status, then ran its diffs — and the first diff flushed those
writes, opening a write transaction that SQLite backs with a lock on the whole file, held until
the pass committed at the very end. For as long as a slow node took to accept a correction, no
edit could be saved anywhere in the hub: "database is locked". And every edit schedules its own
full-state push, so two edits a moment apart raced, and whichever push the node finished last
was the state it kept — both pushes read back what they had sent and were content, and nothing
noticed until reconciliation looked, up to five minutes later. One lock per node serialises
them now, with the desired state read inside the lock so the last push carries the newest state.

**v0.7.1** is why restarting the service took ninety seconds whenever a browser tab was open on
the hub — which is precisely when it happens, since an upgrade is started from that very page.
The installer sat visibly on its service step for a minute and a half, every time, and nothing
was failing: uvicorn shuts down gracefully and waits for the requests still in flight, an SSE
response is a request that never finishes on its own, and the stream's only exit was "has the
client gone away", which on a server shutdown it has not. So uvicorn waited, the stream sent a
keep-alive every twenty seconds, and systemd ended the standoff with SIGKILL after its
ninety-second default. The event bus now closes every open stream when the hub shuts down, with
a sentinel through each queue rather than a flag alone, because a flag would end each stream on
its next keep-alive up to twenty seconds later. Two guards sit behind that so an unanticipated
request cannot bring the wait back: `--timeout-graceful-shutdown 5` and `TimeoutStopSec=30`.

The same release ends a fault that had a fleet losing all twenty of its subscriptions for four
hours. `add_url` does not answer until AdGuard has downloaded and parsed the entire list — a
threat feed is millions of rules, a minute or more of work before the first byte of the response
— and the adapter held that to the same ten seconds it uses to ask a node whether it is alive.
Those are different questions wanting very different answers, so `add_url` has its own generous
timeout now, a constant rather than a multiple of the configured one: an operator whose node
will not take a list should not have to work out that the number to raise is the one labelled
"HTTP timeout", nor pay for that guess on every status check. Worse, the loop stopped at the
first failure, so twenty missing lists meant failing on the first, abandoning the other nineteen
and starting again at the same first list five minutes later — never getting one list further,
for as long as the hub ran. Every subscription is attempted now and the failures reported
together. Lastly, the installer stopped telling the operator of a six-month-old hub to create
the admin account: it asks `/api/auth/state` and words its final line accordingly.

**v0.7.2** adds the marker that would have made the fault above visible from the dashboard. A
node that answers, takes a correction and does not keep it was reachable, online, and had a
green dot — the refusal was written into the drift log and the node's last error, both of which
you have to already suspect something to go and read. An instance that ends a reconciliation
pass with a correction still outstanding is now marked out of sync on the dashboard, in amber
rather than red, with the time it has been that way. It clears itself the moment a pass comes
back clean, so it is a live fact rather than another entry to dismiss.

Alongside it: hovering *GitHub* or *Report an issue* in the footer underlined the separator dot
in front of the link as well, because the dot was a `::before` on the anchor and
`text-decoration` is drawn by the ancestor — nothing on a descendant can opt out of it. The
project also gained a `SECURITY.md`, so the findings in v0.7.0 have a private channel to arrive
through next time, and the backend test suite gained a per-test timeout: two tests written for
v0.7.1 hung the whole suite instead of failing, which for a test about something that never
returns is the one unacceptable outcome.

## Next

Translating the drift log's summaries: they are generated in the backend and stored as English
text, so they stay English in the German interface.

Deliberately **not** planned before v1.0: per-client rule scoping and multi-user accounts.
v1.0 is when the feature set has settled for daily use, not a particular feature landing.

Support for other DNS filters is **not** on the roadmap. AdGuardHub is an AdGuard Home tool.
The seam for one exists anyway and is worth keeping on its own merits — push, reconcile and
import all reach a node through `DnsAdapter` rather than calling AdGuard's API themselves,
which is what keeps the sync core testable — but nothing is planned behind it, and the rule
syntax is deliberately not abstracted: the hub stores AdGuard-native rules.
