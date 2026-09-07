# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's
[Report a vulnerability](https://github.com/fgrfn/AdGuardHub/security/advisories/new)
form. It is not a public issue: only you and the maintainer can see it until
there is a fix.

That matters more here than in most projects. AdGuardHub holds the admin
credentials of the DNS infrastructure of everyone running it, and every one of
those hubs updates on its own schedule. A working exploit posted publicly is
usable against installations that have not upgraded yet — including, quite
possibly, yours.

If you cannot use the form, open an issue saying only that you have found
something and asking for a private channel. No details in the issue.

**What to include, once you have a private channel:** what an attacker can do,
how to reproduce it, and the version you found it on. A rough note beats waiting
until you have a polished write-up.

## What to expect

This is a hobby project with one maintainer, so no response-time promises are
made that could not be kept. What is promised: reports are read, you are told
what will happen, and fixes are credited to you unless you would rather they
were not.

## Scope

**In scope** — anything that lets someone reach the hub or the AdGuard instances
behind it without the credentials for them:

- authentication and session handling, including the AdGuard-compatible
  `/control` façade
- the stored instance credentials and their encryption at rest
- injection, path traversal, or SSRF through the instance URLs
- anything that leaks credentials into a log, a backup, or the diagnostic bundle

**Out of scope** — deliberate design decisions, documented as such:

- **Exposing the hub to the internet.** It is built to run inside your network,
  with one admin account and no multi-user model. Network-level protection is
  the operator's job — see [Security](docs/operations.md#security).
- **The admin can change everything.** That is the whole point of the
  application; it is not privilege escalation.
- **The encryption key next to the database.** With no `ADGUARDHUB_SECRET_KEY`
  set, the hub generates one and stores it in the data directory. That is a
  documented trade-off — see
  [The encryption key](docs/install.md#the-encryption-key) — and it still does
  the one thing it is for: the database file alone is not enough.

A finding in the "out of scope" list is still worth sending if you think the
reasoning behind it is wrong. Being told the trade-off was misjudged is useful;
being told it exists is not.

## Supported versions

The newest release, and only that one. Pre-1.0 this is a single moving line:
fixes go into the next release rather than being backported.
