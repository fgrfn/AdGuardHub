# What the hub replicates

```
┌─────────────┐        push         ┌──────────────────┐
│             │ ──────────────────► │  AdGuard Home #1  │
│ AdGuardHub  │                     └──────────────────┘
│  (source of │        push         ┌──────────────────┐
│   truth)    │ ──────────────────► │  AdGuard Home #2  │
│             │                     └──────────────────┘
└─────────────┘
      ▲
      │ reconcile (drift detection)
      └──────────────────────────────────────────────────
```

Every change is pushed to every instance immediately. An unreachable instance never delays the
others: its update goes to a retry queue and is applied as soon as it answers again. A
reconciliation job runs on an interval as the safety net, comparing each node against the
central state, correcting what has drifted, and logging every correction.

Instances are reached through an adapter interface (`push_rules` / `pull_rules` / …), so the
sync core never talks to AdGuard's API directly.

## A push reads before it writes

"Full state" describes what a push *means*, not how many requests it makes. Every push first
asks the node what it currently holds and then writes only what differs — rules, subscriptions
and each configuration section alike. A node that already matches receives no write at all.

That is not an optimisation, it is correctness about cost. **Every accepted write makes AdGuard
reconfigure itself and rewrite `AdGuardHome.yaml`**, so sending a value the node already has
costs exactly what sending a changed one costs and achieves nothing. Until v0.7.3 the
subscription push compared (it always has) while the rule set and nine of the eleven managed
sections were written unconditionally: one edit to one section reconfigured every node nine
times over.

Two consequences worth stating, because they are what the comparison is *for*:

- **The push and the drift log now use the same comparison** (`adapters/compare.py`). They did
  not before, and they disagreed in the one direction that matters: the drift log correctly
  reported a section as unchanged while the push rewrote it anyway. That module is also where
  the rule lives that a node answering `Europe/Berlin` to a requested `Local` has obeyed rather
  than drifted.
- **A subscription's name is not compared.** AdGuard replaces it with the `! Title:` from the
  file it downloads, so the node's answer is the list's own title rather than what the hub sent.
  Comparing them fired a write for every list on every push, for ever, over a difference the
  drift log has never considered one. The hub owns the URL, the kind and whether the list is
  enabled; the title belongs to the list.

A section the node does not implement is skipped rather than written. Writing to an endpoint
that answers 404 fails the whole settings payload, so one AdGuard build missing one area used to
cost that node every other section too.

## Which areas

Under *Instances → Settings*, each area can be replicated or left to the instance:

| Section | Covers |
| --- | --- |
| DNS & upstreams | Upstream/bootstrap/fallback resolvers, upstream mode, DNSSEC, cache, rate limits, blocking mode |
| Clients | Persistent clients and their per-client filtering settings |
| Access control | Allowed/disallowed clients, blocked hostnames |
| Encryption (TLS) | Whether encryption is on — certificates stay per node |
| DNS rewrites | Custom domain-to-answer rewrites |
| Blocked services | Globally blocked services and their schedule |
| Filtering | Filtering on/off and the list refresh interval |
| Safe browsing / Parental / Safe search | The protection modules |
| Query log & Statistics | Retention, anonymisation, ignored domains |

**DHCP is never touched.** Leases and interface bindings belong to the individual host;
copying them between nodes would be actively wrong.

Importing an instance as the master adopts every area it exposes and switches replication on.
An area a given AdGuard version does not implement is skipped rather than failing the sync.

> **On TLS — read before switching it on.** Install and verify a working certificate on **every**
> node first. AdGuard Home does not check that a node can actually serve HTTPS: if one has no
> valid certificate, enabling encryption can make it unreachable — including its own web
> interface, because it redirects to HTTPS. Recovering then needs shell access to that host to
> turn TLS off in `AdGuardHome.yaml` and restart it.
>
> Because of that, encryption is the one area an import adopts but leaves **switched off**; you
> enable it deliberately, and the UI confirms first. Only the on/off state is replicated: each
> node keeps its own certificate and hostname, and the push reads the target's current TLS
> settings and overlays just `enabled` — `/control/tls/configure` replaces the whole object, so a
> partial write would erase the node's certificate.

## Rule order is not replicated

A node holding exactly the right rules in a different order is **not** drift, and is not
corrected. Until v0.7.8 it was, and that was wrong in both directions at once.

The hub has never let anybody *choose* an order. Rules go out in the order they were created, and
nothing in the interface can move one — so the order being enforced on your nodes was an accident
of insertion, not a decision. Enforcing it was not free either: every accepted write makes AdGuard
reconfigure itself and rewrite `AdGuardHome.yaml`, so each of those corrections bought a
reconfiguration of every node in exchange for nothing anyone had asked for.

A node holding the same rule **twice** is still corrected. The comparison counts rules rather than
treating them as a set, because a duplicate is state the hub did not put there — and under the old
comparison it was reported as "present but in a different order", which named the wrong fault.

If it ever turns out that order decides which of two contradicting rules wins on your nodes, the
answer is not to bring this back: it is to let you set an order and replicate *that*. Enforcing one
nobody chose would still be wrong. Worth knowing when weighing that up: allow rules do not
contradict each other, so on a rule set that is all `@@` there is nothing for an order to decide.

## Comments in the rule set

`!` and `#` lines are stored and replicated like any other line, in place. That matters more
than it sounds: a comment is usually the note saying *why* a rule exists — "allowed after the
doorbell app broke" — and since the hub owns the whole rule set, anything it does not store is
something reconciliation later removes from your nodes.

They appear under *Filtering → Rules* in the *Notes* filter, carry a neutral badge because they
filter nothing, and are edited and deleted like any other entry. Two limits worth knowing:

- **Identical comment lines collapse.** Rule text is unique in the hub, so using `!` twice as a
  bare separator keeps one of them. Notes with different wording are all kept.
- **Already-lost comments do not come back.** If an earlier version imported your rules and
  reconciliation stripped the comments from your nodes, that text is gone; re-import from a node
  that still has them, or add them again.

## Rules that clean up after themselves

Most of a rule set is archaeology. Something broke, a domain was allowed from the query log to
find out whether that was the cause, it worked — and the allow stayed, because nobody goes back
to a thing that is working again. An allow rule kept past its purpose is a hole in the filtering
that nobody remembers opening, and what makes them hard to clear out later is that by then
nothing says which ones were meant to be temporary.

So a rule can be given an expiry when it is written. *Allow temporarily…* in the query log offers
15 minutes, an hour, 8 hours or a day; the *Rules* page shows the countdown beside where the rule
came from, and **Keep** drops it for one that turned out to be worth having. Permanent stays the
default and the primary button — most rules are meant to stand, and a countdown nobody asked for
is worse than none.

When a rule falls due the hub deletes it, records a version, and pushes the new rule set to every
instance the way any other deletion is pushed. Nothing about it is special except that nobody had
to remember. Four things are deliberate:

- **No notification.** An expiry firing is the plan working. A message every time a
  thirty-minute allow lapses is the traffic that makes people stop reading the ones that matter.
  It is logged and it appears in *History*.
- **No silent renewal.** A rule still needed is re-added, which takes one press and restates the
  decision. Asking again also *restarts* the countdown — "allow this for 30 minutes" means thirty
  minutes from now, not "you already did that".
- **An expired rule is never pushed back.** The sweeper runs once a minute, and a rule that has
  fallen due is excluded from the desired state immediately — otherwise a reconciliation pass
  landing in that gap would push an expired allow back onto every node, reopening the hole by
  itself.
- **A week is the limit.** Beyond that it is a rule somebody means to keep, and it should be
  written as one.

## Version history

Every change to the hub — a rule, a subscription, a settings section, an import — records a
snapshot. Under *History* you can:

- see what each change actually carried, summarised per entry
- compare any version against the current state or against another version, down to the
  individual settings key
- roll back to any version: the central state is replaced and pushed to every instance, and the
  rollback itself is recorded so it can be undone in turn

## What is capped

Three tables would otherwise grow for as long as the hub runs. None of them grows quickly —
rows appear when something goes wrong, so a healthy hub barely accumulates any — but a node
that flaps for months is a different story:

| Table | Kept | Why that number |
| --- | --- | --- |
| Version history | 200 | Enough to roll back through a bad week |
| Drift log | 500 | What `/api/drift` will serve in one request at most |
| Applied push jobs | 500 | Same, for `/api/jobs` |
| Reconciliation runs | 500 | 500 *streaks*, not passes — see below |

**The retry queue is never trimmed.** A pending or failed job is work still owed to an
instance; dropping one would silently abandon a change that never reached a node, which is the
exact failure the queue exists to prevent. Only jobs that already landed count as history.

## How often the safety net runs

**Nothing you do in the hub waits for this timer.** Whitelisting a domain from the query log,
adding a rule, toggling a subscription — each is pushed to every instance the moment you press
it, and the request does not even wait for the network to answer. A node that was unreachable
is caught by the retry queue on its own, much shorter interval. So the reconciliation interval
governs exactly one thing: how long a change somebody made **outside** the hub — in a node's
native UI, or by a node coming back from downtime with stale state — can stand before it is
found and corrected.

The default is therefore **900 seconds**, not the five minutes it used to be. Fifteen minutes is
ample for that job, while five meant re-reading both nodes' entire configuration 288 times a day
to find nothing 287 of them. Where the timer *is* the mechanism — propagating your own changes —
it was never the mechanism at all.

You can set it under *Settings → Hub*, anywhere from 30 seconds to 24 hours; it takes effect on
the next pass, with no restart. Two things worth knowing:

- **An existing hub keeps the value it has.** A changed default seeds a fresh database and
  nothing else. The stored number is your decision, and moving it underneath you is exactly the
  silent kind of change this hub exists to avoid — so an upgrade leaves a hub set to 300 on 300.
- **`ADGUARDHUB_RECONCILE_INTERVAL`** pre-fills it on first start, for a hub deployed from a
  compose file. After that the UI owns it and the variable is ignored.

## Is the safety net running

A reconciliation pass that finds nothing writes nothing to the drift log, which is right — two
nodes on the timer would otherwise put a couple of hundred rows a day into a table nobody would
then read, and several hundred at the interval this used to default to. The cost is that an **empty drift log means either a healthy fleet or a
reconciler that stopped weeks ago**, and nothing on the dashboard told those two apart.

Every pass is therefore recorded, including the quiet ones, and the *Reconciliation* card above
the drift log reads it back. One row there is a **streak**, not a pass: consecutive passes that
ended the same way share a row and a counter, so a healthy hub holds a single line —

> **Nothing to correct** · 4,032 passes over 2 node(s)
> | Since | Last pass | Passes | Outcome | Duration |
> | --- | --- | --- | --- | --- |
> | 15 Aug, 19:44 | today, 00:38 | 4,032 | nothing to correct | 84 ms · worst 9.2 s |

— rather than three hundred rows a day saying nothing happened. The moment the outcome changes
the streak ends and a new row begins, so the table reads as the history of what changed. That is
why 500 rows is a long history here and about two days elsewhere.

The duration column keeps the **worst** pass of the streak beside the latest, not an average: a
pass that usually takes 84 ms and once took nine seconds is a node that was nearly unreachable,
and a mean is precisely the statistic that hides it.

The headline says **Reconciliation may have stopped** once the last pass is older than three
intervals. Three rather than one: a single late pass is a slow node, a restart, or a pass that
ran long, and a panel that goes red for those is one people stop reading. A reconciler the
operator switched off says so instead — that is a decision, not a fault.

A dry run (*apply_fixes=false*) is deliberately not recorded. It attempted nothing, so folding it
in would let "nothing to correct" mean "nothing was tried", in the one table built to be trusted
about whether the safety net is running.

## Which list blocked this

A query log row names the rule that matched — `||ads.example.com^` — and on its own that does
not narrow anything down. With twenty subscriptions the question is always *which list do I go
and change*, and the rule text is the same whichever list it came from.

So each row also names the list, under *From list* when you open it. AdGuard sends the answer
beside the rule text and the hub resolves it; a rule you wrote yourself reads **Your own rules**,
and a block from a protection module reads as that module.

Two things are worth knowing about how that number is read, because both are places the answer
could be confidently wrong:

- **The id belongs to the node, not to the subscription.** AdGuard assigns it when the list is
  added, so the same URL is a different number on each of your nodes, and the aggregated log
  mixes rows from all of them. Every row is therefore resolved against the node that wrote it.
  One shared table would look perfectly plausible and name the wrong list.
- **An id the hub cannot explain leaves the field blank.** A list added on a node directly, say,
  which reconciliation is about to remove anyway. "Unknown list" in the one field you read to
  decide what to change is worse than nothing, because it reads as an answer.

The node is asked for its list names at most every fifteen minutes, and immediately when a row
cites a list the hub has not seen — so a subscription you have just added is named straight
away, without the query log poll costing an extra request every five seconds.

## A hub with nothing in it replicates nothing

Every push is *full state*: the hub computes what a node should hold and replaces the node's
managed config with it. That is what makes pushes idempotent and removes any need for merges —
and it means an **empty** central state is not a neutral value. Read literally, it says "these
nodes should hold nothing", and reconciliation would carry that out on its timer.

It has: a second AdGuardHub, started to try something out and left standing on step 1 of the
onboarding wizard with a production node already entered, emptied that node of every rule and
every subscription — then again five minutes later, and for days. Nothing was misconfigured.
The hub did exactly what it is built to do, and its full state was nothing.

So **reconciliation does not run until the hub holds something to replicate**: a rule, a
subscription, or a managed section with something imported into it. Until then each pass is
skipped, the reason is written to the hub's log once rather than on every pass, and the
*Reconciliation* card says **Nothing to replicate yet** rather than leaving an unconfigured hub
looking like a timer that stopped. A manual pass is refused with the same sentence instead of
answering "no drift found", which would be reassuring about precisely the wrong thing.

Three things follow from where that gate sits, each of them deliberate:

- **The push is not gated.** Deleting your last rule *produces* an empty desired state, and that
  deletion has to reach the nodes. A hub that refused to push it would abandon the one change
  the operator most certainly meant, and consider the work done.
- **A rule that is switched off still counts.** Turning every rule off is a decision, and it is
  the nodes' job to hold the result. Counting only enabled rules would stand the safety net down
  at the exact moment it was needed.
- **A section switched on but never imported does not count.** The wizard turns sections on
  before the master import fills them, so treating that intermediate step as "configured" would
  put the gate back on the wrong side of the screen this fault was found behind.

*Instances → ⋯ → Push now* **asks first**, and only on an empty hub. Pressing a button is not a
timer acting on its own — but it is not the same as *meaning* this either. Every other thing that
button does is safe and routine and none of them warns, so an operator whose hub is empty for a
reason they have not noticed — a fresh install pointed at a working node, a restored database —
has nothing to tell this press apart from those.

The question counts what would actually go, by asking the node first:

> The hub holds no rule, no subscription and no imported settings, so a full push would delete
> 24 rule(s) and 20 subscription(s) from node-1 and leave it empty. Import this node as the
> master first, or repeat with confirm=true if erasing it is what you meant.

A generality — *this may delete data* — is the kind of warning people click through. Two numbers
are not. It stays the operator's decision; it is now a decision rather than a side effect.

Nothing is asked when there is nothing to lose: a node that is already empty, or one that cannot
be reached to be counted. The second is deliberate — the push is about to fail on its own, with
its own error, and "I could not count what you would lose" says nothing about whether you meant
it.

## When a correction does not hold

Reconciliation corrects a difference by pushing the hub's state and then **reads it back**. A
2xx from AdGuard means it accepted the request, not that it kept what was in it — and the gap
between those two is where this design's worst failure lives: the hub pushes, believes, finds
the same difference five minutes later, pushes again, and repeats for as long as it runs. It has
happened twice, and both times the hub knew its own correction had not taken and said nothing.

An instant push reads back too, so a refused rule is named while you are still
looking at the button you pressed rather than five minutes later under the wrong
word. The read-back happens once, **after every payload has been written**, and
that ordering is the whole point: AdGuard reconfigures itself on every
configuration change, so a settings section can undo the rule set that arrived a
moment earlier. Checking each payload straight after its own write made that
invisible — the rules were read back before the sections had even been sent, the
hub called them landed, and then overwrote them itself. Such a push is **not**
queued for a retry: the queue exists for a node that
was unreachable, and a node that answered and would not keep the write will not
keep it on the second attempt either — queueing it would rebuild the same loop
one layer down. The node stays *online*, because it is; what it would not keep
appears as its last error, and the push does not count as a completed sync.

Sections are the exception, and deliberately: deciding whether one matches needs
the comparison below, which knows that a node answering `Europe/Berlin` to a
requested `Local` has obeyed rather than drifted. A plain equality check in the
push path would report that as refused every time. Reconciliation verifies
sections properly within its interval.

So a correction is only reported as one once the node actually holds it. Where it does not, the
log says *the node did not keep this correction* with the exact items, marks it uncorrected, and
then goes quiet: a refusal repeats on every run by definition, so it is stated once rather than
several hundred times. It is said again when it changes, and normal reporting resumes the moment
the node starts keeping it.

There is a third ending, and it used to be indistinguishable from the first: the push **errored**,
so the node never saw the correction at all. That row now carries the reason — *the correction
could not be pushed: …* — rather than the bare word *detected* repeating every five minutes with
the cause held in memory and thrown away. It is stated once and again when it changes, like a
refusal.

Each payload kind is corrected on its own. A settings section one AdGuard build rejects used to
abort the whole pass, so the rule set went uncorrected too and nothing said which of the two had
gone wrong; now a failure in one area leaves the others to be fixed, which is the same
best-effort-with-no-rollback rule the push path follows.

Pushing continues throughout. A rule set is pushed whole, so holding it back over one refused
line would strand every other line with it.

Going quiet is not the same as forgetting, and it used to be. A held-back repeat is now counted
on the entry that stands, so the row says how many passes have found exactly this and when the
last of them was — while its own timestamp goes on answering the other question, *since when*.
Without both, a fault repeating every five minutes and one that stopped this morning read
identically, which is how one sat unnoticed for four hours.

Each entry also carries how long its correction attempt took, end to end: the push plus the
read-back that proves whether it landed. That number is the shortest route to a class of fault
that otherwise takes weeks — a push reported as timing out is one thing, and a push reported as
timing out *after exactly 10.0 s* names the setting that caused it. The same timings go into the
hub's own log on every pass, including the passes that find nothing:

```
Reconcile node-b: 20 subscription(s) missing … [pull 84 ms, pass 10.2 s, filters 10.0 s]
```

*Clear log* on the dashboard empties the drift log by hand, for when a cause is fixed and the
entries it left behind are noise rather than evidence — an upgrade that reported the same
difference for a day, or a bug in the hub itself. It deletes the record, not the cause: a node
that still disagrees with the hub is found by the next reconciliation run and written again.
The retry queue has no equivalent, for the reason above.

## Maintenance: holding one node back

Sometimes a node is yours for an hour — you are upgrading AdGuard Home on it, moving it to
another host, or testing something in its native UI. The hub's whole purpose works against you
there: a push overwrites what you just did, and reconciliation puts it back on its next pass.

*Instances → ⋯ → Start maintenance* stops both for that one node. It keeps answering DNS the
entire time; nothing about maintenance touches what the node is actually doing for your network.

What makes it a pause rather than a gap is what happens to the work in between. Every change you
make in the hub while a node is held back is written to the same retry queue an unreachable node
uses, so ending maintenance replays it and the node catches up at once — no waiting for the retry
timer, nothing to press. The queue is visible under *Instances* while it waits.

| | Disabled | Maintenance |
| --- | --- | --- |
| Pushes | not sent, not kept | not sent, **queued** |
| Reconciliation | skipped | skipped |
| Outage notifications | none | none |
| On switching back | nothing happens until you push | queued work is applied immediately |

Disabling an instance says "this is not mine any more". Maintenance says "this is mine, hands off
for a moment" — which is why only one of the two remembers.
