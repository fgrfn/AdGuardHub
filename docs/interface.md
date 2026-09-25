# The interface

A flat top bar carries seven areas, the way AdGuard Home's own UI does, and a status
element that is never off screen: green while every node carries the current configuration,
amber while a push waits in the retry queue, red when a node is unreachable or reconciliation
found drift. The whole point of the hub is that the nodes agree; when they stop agreeing, that
has to find you rather than wait to be looked up.

The two areas that cover more than one thing carry tabs of their own — *Filtering* over the
rule set and the filter lists, *Instances* over the nodes and what the hub replicates to them —
and Settings is a page per subject rather than a stack of cards. Every tab is a real route, so
any of them can be linked to or bookmarked.

The dashboard leads with what the network actually did — queries over time, block rate, top
domains and clients, summed across every node — and says plainly how many nodes answered. A
total that is short by one node otherwise reads as a quiet day. Below that sits the hub's own
state: nodes, last push, queued pushes, drift.

Light and dark both ship, following the operating system by default with a manual override in
the top bar. There are no web fonts and no charting library: the hub runs on a local network
that may have no internet at all, so everything it renders is in the image.

English and German both ship as well. The language follows the browser on first load and is
switched from the top bar (or from the login card, before there is a top bar); the choice is
remembered per browser. Nothing about it is server-side, so two people can use the same hub in
different languages.

## Screenshots

From a local demo against two test instances, so the numbers are made up — the interface is
not.

The dashboard: traffic summed across every node, top domains and clients, and the hub's own
sync state.

<img src="./screenshots/dashboard-light.png" width="900" alt="The AdGuardHub dashboard in the light theme" />

Same dashboard, dark theme. Both ship; the default follows the operating system.

<img src="./screenshots/dashboard-dark.png" width="900" alt="The dashboard in the dark theme" />

The aggregated query log. Every node's queries in one stream, newest first, with the node that
answered in its own column; a row opens onto the rule that matched, and allowing or blocking
from here writes one rule that reaches every node at once.

<img src="./screenshots/querylog.png" width="900" alt="The query log with one row expanded, showing the matched rule and the allow action" />

The central rule set, in native AdGuard syntax. Three ways in — a custom rule, a domain to
allow, or a pasted block — all writing to the same model.

<img src="./screenshots/rules.png" width="900" alt="The filtering rules page: entry forms above, the rule table below with block and allow badges" />

Filter lists. The hub tracks the URL and whether it is on; AdGuard Home still downloads and
applies the list itself, so the 700k-domain lists never touch this database. A *Rules* column
says how big each list turned out to be, with the active total above the table — those numbers
come back from the nodes, because the hub never sees the file. A node that has not fetched a
list yet leaves a dash rather than a zero, and where two nodes report different sizes (they
refresh on their own schedules) the row is marked and the tooltip breaks it down per node.

An *Updated* column says when each list last arrived — the most recent any node reports, since
they refresh on their own schedules. **never** is the reading that matters: AdGuard keeps a
subscription it cannot download rather than dropping it, so a URL that has rotted does not
disappear from a node, it just stops changing. *Check for updates* asks every node to download now
instead of waiting out its own interval; the hub cannot do it itself, and a node that cannot be
reached is named rather than folded into the count.

<img src="./screenshots/subscriptions.png" width="900" alt="The filter lists page listing four blocklist URLs with their enabled state" />

Instances. Each AdGuard Home is added once, with credentials encrypted before they are stored.

<img src="./screenshots/instances.png" width="900" alt="The instances page showing two connected nodes, both online, with their version and last sync time" />

Instance settings. The left column answers the page's real question — what the hub owns, and
what is left to each node.

<img src="./screenshots/instance-settings.png" width="900" alt="Instance settings, with the encryption section selected and its certificate warning shown" />

Version history. Every change is a snapshot you can compare or roll back to.

<img src="./screenshots/history.png" width="900" alt="The history page listing five versions, each with compare and roll back actions" />

The whole interface in German. The language follows the browser on first load and is switched
from the top bar; dates and numbers follow the language, not the browser.

<img src="./screenshots/dashboard-de.png" width="900" alt="The dashboard in German" />
