"""Rule model semantics (spec §5) and notifier configuration (spec §10)."""

from __future__ import annotations

import httpx
import pytest

from app.models import RuleKind
from app.security import Crypto, Sessions
from app.services.notify import build_payload
from app.services.rules import allow_rule_for_domain, block_rule_for_domain, classify, is_comment


def test_rule_classification() -> None:
    assert classify("@@||example.com^") is RuleKind.allow
    assert classify("||example.com^") is RuleKind.block
    assert is_comment("! comment") and is_comment("# comment")
    assert not is_comment("||example.com^")
    assert allow_rule_for_domain(" Example.COM. ") == "@@||example.com^"
    assert block_rule_for_domain("Example.COM") == "||example.com^"


def test_crypto_round_trip_and_key_isolation() -> None:
    token = Crypto("key-one").encrypt("hunter2")
    assert token != "hunter2"
    assert Crypto("key-one").decrypt(token) == "hunter2"
    with pytest.raises(ValueError):
        Crypto("key-two").decrypt(token)


def test_session_tokens_are_rejected_when_expired_or_forged() -> None:
    sessions = Sessions("key-one")
    token = sessions.issue("admin", "$2b$12$not-a-real-hash")
    assert sessions.verify(token, 3600).subject == "admin"
    assert sessions.verify(token, -1) is None
    assert Sessions("key-two").verify(token, 3600) is None


def test_notifier_payload_shapes() -> None:
    class Target:
        type = "discord"

    assert "Title" in build_payload(Target(), "e", "Title", "Body")["content"]
    Target.type = "gotify"
    assert build_payload(Target(), "e", "Title", "Body")["title"] == "Title"
    Target.type = "homeassistant"
    assert build_payload(Target(), "e", "Title", "Body")["event"] == "e"


async def test_duplicate_rules_are_rejected(auth_client: httpx.AsyncClient) -> None:
    assert (await auth_client.post("/api/rules", json={"text": "||a.com^"})).status_code == 201
    assert (await auth_client.post("/api/rules", json={"text": "||a.com^"})).status_code == 409


async def test_comments_are_stored_as_rules(auth_client: httpx.AsyncClient) -> None:
    """They used to be refused, which quietly deleted the note explaining a rule."""
    response = await auth_client.post("/api/rules", json={"text": "! just a note"})
    assert response.status_code == 201
    assert response.json()["kind"] == "comment"


async def test_a_commented_out_allow_rule_is_a_comment(
    auth_client: httpx.AsyncClient,
) -> None:
    """Reading the `@@` first would file a disabled line under the rules it is not."""
    response = await auth_client.post("/api/rules", json={"text": "! @@||example.com^"})
    assert response.json()["kind"] == "comment"


async def test_allow_is_idempotent_across_entry_points(auth_client: httpx.AsyncClient) -> None:
    """The Allowlist tab and the query log's action must converge on one rule."""
    first = await auth_client.post(
        "/api/rules/allow?origin=allowlist", json={"domain": "shop.example.com"}
    )
    second = await auth_client.post(
        "/api/rules/allow?origin=querylog", json={"domain": "shop.example.com"}
    )
    assert first.json()["id"] == second.json()["id"]
    assert len((await auth_client.get("/api/rules")).json()) == 1


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        "example.com^$important",
        "example..com",
        "-example.com",
        "example.com/path",
        "example.com\\other",
        "example.com\n||injected.example^",
    ],
)
async def test_domain_actions_reject_invalid_rule_input(
    auth_client: httpx.AsyncClient, domain: str
) -> None:
    response = await auth_client.post("/api/rules/allow", json={"domain": domain})
    assert response.status_code == 422
    assert (await auth_client.get("/api/rules")).json() == []


async def test_domain_actions_normalise_international_names(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.post("/api/rules/allow", json={"domain": "Bücher.Example."})
    assert response.status_code == 200
    assert response.json()["text"] == "@@||xn--bcher-kva.example^"


async def test_bulk_import_keeps_comments_and_skips_blanks(
    auth_client: httpx.AsyncClient,
) -> None:
    """Order is the point: a comment is the line above the rule it explains."""
    response = await auth_client.post(
        "/api/rules/bulk",
        json={"text": "! ads\n||a.com^\n\n# shop\n@@||b.com^\n||a.com^"},
    )
    assert [rule["text"] for rule in response.json()] == [
        "! ads",
        "||a.com^",
        "# shop",
        "@@||b.com^",
    ]


async def test_rule_filters(auth_client: httpx.AsyncClient) -> None:
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await auth_client.post("/api/rules/allow", json={"domain": "shop.example.com"})

    assert len((await auth_client.get("/api/rules?kind=allow")).json()) == 1
    assert len((await auth_client.get("/api/rules?origin=allowlist")).json()) == 1
    assert len((await auth_client.get("/api/rules?search=ads")).json()) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "adguard.local",
        "http://",
        "http://admin:secret@adguard.local",
        "http://adguard.local?target=other",
        "http://adguard.local#fragment",
        "http://adguard.local:99999",
        "http://adguard.local:\\control",
    ],
)
async def test_invalid_instance_url_is_rejected(
    auth_client: httpx.AsyncClient, base_url: str
) -> None:
    response = await auth_client.post(
        "/api/instances", json={"name": "bad", "base_url": base_url}
    )
    assert response.status_code == 422


async def test_notifier_crud_and_unknown_event_validation(
    auth_client: httpx.AsyncClient,
) -> None:
    bad = await auth_client.post(
        "/api/settings/notifiers",
        json={
            "name": "hass",
            "type": "homeassistant",
            "url": "http://hass.local/api/webhook/x",
            "events": ["not.an.event"],
        },
    )
    assert bad.status_code == 422

    created = await auth_client.post(
        "/api/settings/notifiers",
        json={
            "name": "hass",
            "type": "homeassistant",
            "url": "http://hass.local/api/webhook/x",
            "token": "secret-token",
            "events": ["push.failed"],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["has_token"] is True
    assert "token" not in body
    assert body["events"] == ["push.failed"]

    updated = await auth_client.patch(
        f"/api/settings/notifiers/{body['id']}", json={"enabled": False, "events": []}
    )
    assert updated.json()["enabled"] is False
    assert updated.json()["events"] == []

    assert (
        await auth_client.delete(f"/api/settings/notifiers/{body['id']}")
    ).status_code == 204
    assert (await auth_client.get("/api/settings/notifiers")).json() == []


async def test_dashboard_counts(auth_client: httpx.AsyncClient) -> None:
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await auth_client.post("/api/rules/allow", json={"domain": "shop.example.com"})
    await auth_client.post(
        "/api/filter-lists", json={"name": "L", "url": "https://example.com/l.txt"}
    )

    stats = (await auth_client.get("/api/dashboard")).json()
    assert stats["rules_total"] == 2
    assert stats["rules_allow"] == 1
    assert stats["rules_block"] == 1
    assert stats["filter_lists_enabled"] == 1
