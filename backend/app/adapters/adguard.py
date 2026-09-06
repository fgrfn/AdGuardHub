"""AdGuard Home adapter, speaking the /control REST API."""

from __future__ import annotations

from typing import Any

import httpx

from ..semver import is_newer
from . import session
from .base import AdapterError, DnsAdapter, QueryLogEntry, RemoteFilterList, RemoteUpdate
from .sections import SECTION_NAMES, SPEC_BY_NAME, SectionSpec
from .session import SessionKey, SessionStore

# AdGuard's query log "reason" values that mean the answer was actually filtered.
_ALLOWED_REASONS = {"NotFilteredWhiteList", "NotFilteredNotFound", "NotFilteredError", ""}

# httpx raises its timeout and transport errors with no message at all, so
# f"…failed: {exc}" renders as "…failed:" and tells the operator nothing. These
# say what actually happened instead.
_SILENT_ERRORS = {
    "ConnectTimeout": "no answer while opening the connection (connect timeout)",
    "ReadTimeout": "the connection was accepted but no response arrived (read timeout)",
    "WriteTimeout": "the request could not be sent in time (write timeout)",
    "PoolTimeout": "no connection slot became free in time (pool timeout)",
    "ConnectError": "the connection could not be established",
    "ReadError": "the connection dropped while reading the response",
    "WriteError": "the connection dropped while sending the request",
    "RemoteProtocolError": "the instance sent a malformed HTTP response",
}


def _int(value: Any) -> int:
    """A count from a field that older AdGuard builds may omit or send as a string."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def describe_transport_error(exc: Exception, timeout: float | None = None) -> str:
    """A sentence an operator can act on, even when httpx supplies nothing.

    A timeout and a refused connection mean very different things — one host is
    silent, the other is answering — and an empty message hides which it was.
    """
    message = str(exc).strip()
    if message:
        return message
    name = type(exc).__name__
    described = _SILENT_ERRORS.get(name)
    if described is None:
        return name
    if timeout and name.endswith("Timeout"):
        return f"{described} after {timeout:g}s"
    return described



#: How long to wait for a request that makes AdGuard fetch something itself.
#:
#: `add_url` does not answer until the node has downloaded and parsed the whole
#: subscription, and a threat-intelligence feed is millions of rules — a minute
#: or more of work on a small host, all of it before the first byte of the
#: response. The configured timeout is for a different question, "is this node
#: alive", which wants a short answer; one value cannot be right for both, and
#: the default of ten seconds made adding a large list impossible.
#:
#: Deliberately a constant rather than a multiple of the configured timeout: an
#: operator whose node cannot accept a list should not have to work out that the
#: number to raise is the one labelled "HTTP timeout", nor pay for that guess on
#: every status check.
LIST_FETCH_TIMEOUT = 120.0

#: Enough to name what went wrong without turning one failure into a wall.
MAX_LIST_ERRORS_SHOWN = 3


class AdGuardAdapter(DnsAdapter):
    name = "adguard"

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        verify_tls: bool = True,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        sessions: SessionStore | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=True,
        )
        # Sessions outlive the adapter, which is rebuilt for every operation.
        self._sessions = sessions if sessions is not None else session.store
        self._key: SessionKey = (self.base_url, self._username)
        # Kept for error messages: "no answer after 10s" beats "no answer".
        self._timeout = timeout

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- transport -------------------------------------------------------

    async def _login(self) -> None:
        """Authenticate once and cache the session cookie for later adapters.

        Never call this per request: AdGuard Home counts login traffic against its
        brute-force protection and starts answering 429 (see adapters/session.py).
        """
        if not self._username:
            raise AdapterError("Authentication required but no credentials are configured")

        blocked = self._sessions.blocked_for(self._key)
        if blocked:
            raise AdapterError(
                f"AdGuard Home is rate-limiting logins (HTTP 429); waiting {blocked:.0f}s "
                "before trying again. The credentials themselves may be fine."
            )
        try:
            response = await self._client.post(
                "/control/login",
                json={"name": self._username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"Login failed: {describe_transport_error(exc, self._timeout)}"
            ) from exc

        if response.status_code == 429:
            self._sessions.note_rate_limited(self._key)
            raise AdapterError(
                "AdGuard Home rejected the login with HTTP 429 (too many requests). Its "
                "brute-force protection is active — the credentials may well be correct. "
                "Wait a minute, or restart AdGuard Home to clear the block."
            )
        if response.status_code in (401, 403):
            raise AdapterError(
                f"AdGuard Home rejected the credentials for user {self._username!r} "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise AdapterError(f"Login rejected with HTTP {response.status_code}")

        self._sessions.set(self._key, self._client.cookies)

    async def _ensure_session(self) -> None:
        """Attach a cached cookie, logging in only when there isn't one yet."""
        if not self._username:
            return
        cookies = self._sessions.get(self._key)
        if cookies is not None:
            self._client.cookies = cookies
            return
        async with self._sessions.lock(self._key):
            # Another caller may have logged in while we waited for the lock.
            cookies = self._sessions.get(self._key)
            if cookies is not None:
                self._client.cookies = cookies
                return
            await self._login()

    async def _send(
        self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any
    ) -> httpx.Response:
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # The timeout named in the message has to be the one that actually
            # elapsed, or "no answer after 10s" sends the reader looking at a
            # setting that had nothing to do with it.
            waited = timeout if timeout is not None else self._timeout
            raise AdapterError(
                f"{method} {path} failed: {describe_transport_error(exc, waited)}"
            ) from exc

    async def _request(
        self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any
    ) -> httpx.Response:
        await self._ensure_session()
        response = await self._send(method, path, timeout=timeout, **kwargs)

        # An expired or invalidated session: drop it and authenticate once more.
        if response.status_code in (401, 403) and self._username:
            self._sessions.clear(self._key)
            async with self._sessions.lock(self._key):
                await self._login()
            response = await self._send(method, path, timeout=timeout, **kwargs)

        if response.status_code == 429:
            self._sessions.note_rate_limited(self._key)
            raise AdapterError(
                f"{method} {path} returned HTTP 429: AdGuard Home is rate-limiting requests. "
                "Backing off for a minute."
            )
        if response.status_code >= 400:
            detail = response.text.strip()[:200]
            raise AdapterError(
                f"{method} {path} returned HTTP {response.status_code}: {detail}",
                status=response.status_code,
            )
        return response

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self._request("GET", path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise AdapterError(f"GET {path} returned a non-JSON body") from exc

    # -- DnsAdapter ------------------------------------------------------

    async def check(self) -> str:
        data = await self._get_json("/control/status")
        version = data.get("version") if isinstance(data, dict) else None
        return str(version or "unknown")

    async def check_update(self, current: str = "") -> RemoteUpdate:
        """Ask the node what it knows about its own updates.

        `recheck_now: false` deliberately: AdGuard answers from the result of
        its own periodic check rather than reaching out to its update server
        because the hub asked. The hub polls every node on the reconcile timer,
        and turning that into outbound traffic from every node on the same timer
        would be a rude thing to build.

        The node may answer that it has nothing to say — its update check can be
        switched off in its own configuration, and older builds have no such
        endpoint at all. That is reported as "could not find out", which is not
        the same as "up to date" and must not be shown as if it were.
        """
        try:
            response = await self._request(
                "POST", "/control/version.json", json={"recheck_now": False}
            )
            data = response.json()
        except AdapterError as caught:
            return RemoteUpdate(error=str(caught))
        except ValueError:
            return RemoteUpdate(error="the node's version endpoint returned a non-JSON body")

        if not isinstance(data, dict):
            return RemoteUpdate(error="the node's version endpoint returned an unexpected body")
        if data.get("disabled"):
            return RemoteUpdate(error="this node has its own update check switched off")

        # AdGuard's version.json carries no current_version — the field this used
        # to compare against does not exist in its response. So `current` was
        # always empty, `new_version != ""` was always true, and every node whose
        # answer named a version was reported as having an update to the version
        # it was already running. It reads as absurd in the interface precisely
        # because it is: "v0.107.79 — update to v0.107.79".
        #
        # The version the hub asked the node for moments ago is the honest
        # comparison, and the caller passes it in. A build that does send
        # current_version is believed over it.
        running = str(data.get("current_version") or "").strip() or current.strip()
        latest = str(data.get("new_version") or "").strip()
        url = str(data.get("announcement_url") or "")

        if latest and not running:
            # Nothing to compare against. "Could not find out" is the truth here,
            # and it is not the same as "there is an update" — which is the whole
            # mistake this replaced.
            return RemoteUpdate(
                latest=latest,
                url=url,
                error="the node did not say which version it is running",
            )

        return RemoteUpdate(
            current=running,
            latest=latest,
            # Strictly newer, not merely different: equality is what a node
            # reporting its own version produces, and a difference in the other
            # direction would offer a downgrade as an update.
            available=is_newer(latest, running),
            url=url,
        )

    async def pull_rules(self) -> list[str]:
        data = await self._get_json("/control/filtering/status")
        raw = data.get("user_rules") or []
        return [str(line) for line in raw]

    async def push_rules(self, rules: list[str]) -> None:
        await self._request("POST", "/control/filtering/set_rules", json={"rules": rules})

    async def pull_filter_lists(self) -> list[RemoteFilterList]:
        data = await self._get_json("/control/filtering/status")
        result: list[RemoteFilterList] = []
        for key, kind in (("filters", "blocklist"), ("whitelist_filters", "allowlist")):
            for item in data.get(key) or []:
                result.append(
                    RemoteFilterList(
                        name=str(item.get("name") or item.get("url") or ""),
                        url=str(item.get("url") or ""),
                        enabled=bool(item.get("enabled")),
                        kind=kind,
                        rules_count=_int(item.get("rules_count")),
                    )
                )
        return result

    async def push_filter_lists(self, lists: list[RemoteFilterList]) -> None:
        """Reconcile subscriptions to ``lists``: add missing, update changed, drop extras.

        Every subscription is attempted, and the failures are reported together
        at the end. Stopping at the first one looks tidier and is much worse: a
        node missing twenty lists would fail on the first, abandon the other
        nineteen, and start again from the same first list five minutes later —
        forever, never getting one list further. Nineteen applied and one named
        is the better answer, and it is the one the rest of the hub gives (spec
        §6: best effort, no rollback).
        """
        current = {(item.kind, item.url): item for item in await self.pull_filter_lists()}
        desired = {(item.kind, item.url): item for item in lists}
        failures: list[str] = []

        for key, item in desired.items():
            whitelist = item.kind == "allowlist"
            existing = current.get(key)
            try:
                if existing is None:
                    await self._request(
                        "POST",
                        "/control/filtering/add_url",
                        json={"name": item.name, "url": item.url, "whitelist": whitelist},
                        # The node fetches the list before it answers.
                        timeout=LIST_FETCH_TIMEOUT,
                    )
                    if not item.enabled:
                        await self._set_url(item, whitelist)
                elif existing.enabled != item.enabled or existing.name != item.name:
                    await self._set_url(item, whitelist)
            except AdapterError as exc:
                failures.append(f"{item.url} ({exc})")

        for key, item in current.items():
            if key not in desired:
                try:
                    await self._request(
                        "POST",
                        "/control/filtering/remove_url",
                        json={"url": item.url, "whitelist": item.kind == "allowlist"},
                    )
                except AdapterError as exc:
                    failures.append(f"{item.url} ({exc})")

        if failures:
            shown = "; ".join(failures[:MAX_LIST_ERRORS_SHOWN])
            rest = len(failures) - MAX_LIST_ERRORS_SHOWN
            raise AdapterError(
                f"{len(failures)} of {len(desired)} subscription(s) could not be applied: "
                + shown
                + (f"; and {rest} more" if rest > 0 else "")
            )

    async def _set_url(self, item: RemoteFilterList, whitelist: bool) -> None:
        await self._request(
            "POST",
            "/control/filtering/set_url",
            json={
                "url": item.url,
                "whitelist": whitelist,
                "data": {"name": item.name, "url": item.url, "enabled": item.enabled},
            },
        )


    # -- configuration sections ------------------------------------------

    def supported_sections(self) -> tuple[str, ...]:
        return SECTION_NAMES

    @staticmethod
    def _select(spec: SectionSpec, data: dict[str, Any]) -> dict[str, Any]:
        if not spec.keys:
            return dict(data)
        return {key: data[key] for key in spec.keys if key in data}

    async def _section_json(self, spec: SectionSpec) -> Any | None:
        """Read a section's endpoint, mapping "not implemented" to ``None``.

        AdGuard versions differ in which areas they expose; a missing one must be
        skipped rather than fail the whole sync.
        """
        try:
            return await self._get_json(spec.get_path)
        except AdapterError as exc:
            if exc.status in (404, 405, 501):
                return None
            raise

    async def pull_section(self, name: str) -> dict[str, Any] | None:
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            raise AdapterError(f"Unknown configuration section {name!r}")

        raw = await self._section_json(spec)
        if raw is None:
            return None

        if spec.strategy == "toggle":
            return {"enabled": bool(raw.get("enabled"))}
        if spec.strategy == "clients":
            return {"clients": list(raw.get("clients") or [])}
        if spec.strategy == "rewrites":
            items = raw if isinstance(raw, list) else raw.get("items") or []
            return {"items": [dict(item) for item in items]}
        if not isinstance(raw, dict):
            return None
        return self._select(spec, raw)

    async def push_section(self, name: str, data: dict[str, Any]) -> None:
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            raise AdapterError(f"Unknown configuration section {name!r}")

        if spec.strategy == "toggle":
            path = spec.enable_path if data.get("enabled") else spec.disable_path
            await self._request("POST", path)
            return
        if spec.strategy == "clients":
            await self._push_clients(list(data.get("clients") or []))
            return
        if spec.strategy == "rewrites":
            await self._push_rewrites(list(data.get("items") or []))
            return

        payload = self._select(spec, data)
        if not payload:
            return
        if spec.merge_on_push:
            # This endpoint replaces the whole object, so send the target's current
            # document with only the managed keys overlaid — anything the node owns
            # (a certificate, its hostname) has to survive the push.
            current = await self._section_json(spec)
            if isinstance(current, dict):
                payload = {**current, **payload}
        await self._request(spec.set_method, spec.set_path, json=payload)

    async def _push_clients(self, desired: list[dict[str, Any]]) -> None:
        """Make the persistent client list match ``desired`` exactly."""
        current_raw = await self._section_json(SPEC_BY_NAME["clients"])
        current = {
            str(item.get("name")): item for item in (current_raw or {}).get("clients") or []
        }
        wanted = {str(item.get("name")): item for item in desired if item.get("name")}

        for client_name, client in wanted.items():
            if client_name in current:
                if current[client_name] != client:
                    await self._request(
                        "POST",
                        "/control/clients/update",
                        json={"name": client_name, "data": client},
                    )
            else:
                await self._request("POST", "/control/clients/add", json=client)

        for client_name in current.keys() - wanted.keys():
            await self._request("POST", "/control/clients/delete", json={"name": client_name})

    async def _push_rewrites(self, desired: list[dict[str, Any]]) -> None:
        current_raw = await self._section_json(SPEC_BY_NAME["rewrites"])
        raw_items = current_raw if isinstance(current_raw, list) else []
        current = {(item.get("domain"), item.get("answer")) for item in raw_items}
        wanted = {(item.get("domain"), item.get("answer")) for item in desired}

        for domain, answer in wanted - current:
            await self._request(
                "POST", "/control/rewrite/add", json={"domain": domain, "answer": answer}
            )
        for domain, answer in current - wanted:
            await self._request(
                "POST", "/control/rewrite/delete", json={"domain": domain, "answer": answer}
            )

    async def stats(self) -> dict[str, Any]:
        data = await self._get_json("/control/stats")
        return data if isinstance(data, dict) else {}

    async def query_log(self, limit: int) -> list[QueryLogEntry]:
        data = await self._get_json("/control/querylog", params={"limit": limit})
        rows = data.get("data") if isinstance(data, dict) else None
        entries: list[QueryLogEntry] = []
        for row in rows or []:
            entries.append(self._parse_log_row(row))
        return entries

    @staticmethod
    def _parse_log_row(row: dict[str, Any]) -> QueryLogEntry:
        question = row.get("question") or {}
        reason = str(row.get("reason") or "")
        rules = row.get("rules") or []
        rule_text = ""
        if rules and isinstance(rules[0], dict):
            rule_text = str(rules[0].get("text") or "")
        elif row.get("rule"):
            rule_text = str(row["rule"])
        # AdGuard Home names this field elapsedMs, and sends it as a string. Reading
        # only elapsed_ms meant every entry reported a response time of zero.
        raw_elapsed = row.get("elapsedMs")
        if raw_elapsed is None:
            raw_elapsed = row.get("elapsed_ms")
        try:
            elapsed = float(raw_elapsed or 0)
        except (TypeError, ValueError):
            elapsed = 0.0
        return QueryLogEntry(
            time=str(row.get("time") or ""),
            question=str(question.get("name") or question.get("host") or ""),
            question_type=str(question.get("type") or ""),
            client=str(row.get("client") or ""),
            answer_status=reason,
            blocked=reason not in _ALLOWED_REASONS,
            rule=rule_text,
            elapsed_ms=elapsed,
            upstream=str(row.get("upstream") or ""),
        )
