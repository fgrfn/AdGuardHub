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

## Is the safety net running

A reconciliation pass that finds nothing writes nothing to the drift log, which is right — two
nodes on a five-minute timer would otherwise put several hundred rows a day into a table nobody
would then read. The cost is that an **empty drift log means either a healthy fleet or a
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
there: a push overwrites what you just did, and reconciliation puts it back within five minutes.

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
