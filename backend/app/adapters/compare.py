"""Whether a node's answer already satisfies what the hub asked for.

This lives beside the adapter rather than in the sync engine because the rules
in it are AdGuard's, not the hub's: ``Local`` is a time zone only AdGuard would
answer for itself, and a future adapter would have its own list. It is here
rather than inside ``adguard.py`` because two callers need the *same* answer,
and until now they did not have one:

* **reconciliation** compares a pulled section against the hub's document to
  decide whether it is drift, and
* **the push path** should compare the same two things to decide whether the
  write is needed at all.

Those two disagreeing is not hypothetical: the push wrote every managed section
unconditionally, so a node that already matched was reconfigured nine times per
settings change — while the drift log, using the comparison below, correctly
said nothing had changed. One of the two was wasting the node's config file.

Every write to AdGuard is a `reconfiguring server` and a rewrite of
AdGuardHome.yaml. A write that changes nothing is not free; it is the same cost
as a real one, for no result.
"""

from __future__ import annotations

from typing import Any

# Settings each node is entitled to answer for itself, by the path they sit at.
#
# "Local" is not a time zone; it is an instruction to use whichever zone the node
# is in. A node reading back "Europe/Berlin" has obeyed that instruction rather
# than drifted from it — but comparing the request against the answer made every
# reconciliation run report a difference, correct it by pushing "Local" again, and
# find the same difference on the next run. Forever, on any node whose clock knows
# where it is, filling the drift log and firing a notification each time (v0.4.3).
#
# Only the placeholder is forgiven. A hub that says Europe/Berlin and a node that
# says something else is still drift, and is still corrected.
SELF_RESOLVED: dict[tuple[str, ...], frozenset[str]] = {
    ("blocked_services", "schedule", "time_zone"): frozenset({"Local", ""}),
}


def normalise(value: Any) -> Any:
    """Order-insensitive for mappings, so key order is never a difference."""
    if isinstance(value, list):
        return [normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: normalise(item) for key, item in sorted(value.items())}
    return value


def equivalent(expected: Any, actual: Any, path: tuple[str, ...]) -> bool:
    """Whether the node's answer satisfies what the hub asked for, at this path."""
    allowed = SELF_RESOLVED.get(path)
    if allowed is not None and expected in allowed:
        return True
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return False
        return all(equivalent(expected[key], actual[key], path + (key,)) for key in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(
            equivalent(item, other, path) for item, other in zip(expected, actual, strict=True)
        )
    return normalise(expected) == normalise(actual)


def section_differences(
    name: str, expected: dict[str, Any], actual: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Per-key differences for one section, or ``None`` when it matches.

    ``actual`` is ``None`` when the instance does not implement the section; that
    is not drift, just a capability difference, so it is reported without a
    correction.

    Only the keys the hub manages are compared. Everything else in the node's
    document — a certificate, a hostname, whatever a future AdGuard adds — is the
    node's own and is neither read nor judged here.
    """
    if actual is None:
        return {"unsupported": True}
    changed = {
        key: {"expected": normalise(value), "actual": normalise(actual.get(key))}
        for key, value in expected.items()
        if not equivalent(value, actual.get(key), (name, key))
    }
    return changed or None


def section_matches(name: str, expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    """Whether the node already holds every managed key of this section.

    ``False`` for a section the node does not implement: there is nothing to
    match against, and the caller decides what that means. The push skips it —
    writing to an endpoint that answered 404 fails the whole settings push —
    while reconciliation reports it as a capability gap rather than drift.
    """
    return actual is not None and section_differences(name, expected, actual) is None
