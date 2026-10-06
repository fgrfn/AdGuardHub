from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import func, select


async def test_setup_required_before_first_admin(client: httpx.AsyncClient) -> None:
    state = (await client.get("/api/auth/state")).json()
    assert state["setup_required"] is True
    assert state["authenticated"] is False


async def test_protected_routes_require_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/rules")).status_code == 401
    assert (await client.get("/api/instances")).status_code == 401


async def test_setup_then_login_logout(client: httpx.AsyncClient) -> None:
    setup = await client.post(
        "/api/auth/setup", json={"username": "admin", "password": "supersecret"}
    )
    assert setup.status_code == 200
    assert (await client.get("/api/rules")).status_code == 200

    # Setup is a one-shot: a second call must not silently replace the admin.
    again = await client.post(
        "/api/auth/setup", json={"username": "mallory", "password": "supersecret"}
    )
    assert again.status_code == 409

    await client.post("/api/auth/logout")
    assert (await client.get("/api/rules")).status_code == 401

    bad = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401

    good = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "supersecret"}
    )
    assert good.status_code == 200
    assert (await client.get("/api/rules")).status_code == 200


async def test_only_one_concurrent_setup_can_create_the_admin(
    client: httpx.AsyncClient,
) -> None:
    """The single-admin promise must also hold for overlapping first requests."""
    from app.db import session_scope
    from app.models import User

    async with await second_browser() as other:
        first, second = await asyncio.gather(
            client.post(
                "/api/auth/setup", json={"username": "admin", "password": "supersecret"}
            ),
            other.post(
                "/api/auth/setup", json={"username": "mallory", "password": "anothersecret"}
            ),
        )

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_password_change(auth_client: httpx.AsyncClient) -> None:
    wrong = await auth_client.post(
        "/api/auth/password",
        json={"current_password": "nope", "new_password": "anotherlongpassword"},
    )
    assert wrong.status_code == 403

    ok = await auth_client.post(
        "/api/auth/password",
        json={"current_password": "supersecret", "new_password": "anotherlongpassword"},
    )
    assert ok.status_code == 200

    await auth_client.post("/api/auth/logout")
    login = await auth_client.post(
        "/api/auth/login", json={"username": "admin", "password": "anotherlongpassword"}
    )
    assert login.status_code == 200


async def test_a_fresh_hub_has_not_finished_onboarding(client: httpx.AsyncClient) -> None:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"})

    state = (await client.get("/api/auth/state")).json()
    assert state["authenticated"] is True
    assert state["onboarding_done"] is False


async def test_finishing_onboarding_sticks(auth_client: httpx.AsyncClient) -> None:
    """Recorded on the hub, not in a browser: a second browser must not be walked
    through the setup of an already configured hub."""
    assert (await auth_client.post("/api/settings/onboarding-complete")).status_code == 204

    assert (await auth_client.get("/api/auth/state")).json()["onboarding_done"] is True
    # Idempotent: the walkthrough may be finished and then re-opened from the nav.
    assert (await auth_client.post("/api/settings/onboarding-complete")).status_code == 204
    assert (await auth_client.get("/api/auth/state")).json()["onboarding_done"] is True


async def test_onboarding_cannot_be_completed_anonymously(client: httpx.AsyncClient) -> None:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"})
    await client.post("/api/auth/logout")

    assert (await client.post("/api/settings/onboarding-complete")).status_code == 401


async def second_browser() -> httpx.AsyncClient:
    """Another client against the same hub, holding its own cookie jar."""
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_changing_the_password_ends_every_other_session(
    auth_client: httpx.AsyncClient,
) -> None:
    """A cookie is tied to the password it was issued under.

    Until it was, a session stayed valid for its full fourteen days however
    many times the password changed — and changing the password is exactly
    what an operator does when they suspect a session has been taken.
    """
    async with await second_browser() as other:
        login = await other.post(
            "/api/auth/login", json={"username": "admin", "password": "supersecret"}
        )
        assert login.status_code == 200
        assert (await other.get("/api/rules")).status_code == 200

        changed = await auth_client.post(
            "/api/auth/password",
            json={"current_password": "supersecret", "new_password": "anotherlongpassword"},
        )
        assert changed.status_code == 200

        # The other browser is out, and /api/auth/state agrees with the routes.
        assert (await other.get("/api/rules")).status_code == 401
        assert (await other.get("/api/auth/state")).json()["authenticated"] is False

    # The browser that changed the password is the one that must not be signed
    # out by it: it was handed a fresh cookie in the same response.
    assert (await auth_client.get("/api/rules")).status_code == 200
    assert (await auth_client.get("/api/auth/state")).json()["authenticated"] is True


async def test_a_session_from_the_adguard_login_is_tied_to_the_password_too(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"})
    await client.post("/api/auth/logout")

    async with await second_browser() as phone:
        assert (
            await phone.post("/control/login", json={"name": "admin", "password": "supersecret"})
        ).status_code == 200
        assert (await phone.get("/control/status")).status_code == 200

        await client.post("/api/auth/login", json={"username": "admin", "password": "supersecret"})
        await client.post(
            "/api/auth/password",
            json={"current_password": "supersecret", "new_password": "anotherlongpassword"},
        )

        assert (await phone.get("/control/status")).status_code == 401


async def test_a_restart_with_the_same_env_password_keeps_sessions(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    """bcrypt salts every hash, so rehashing on every start would change the
    hash — and with it, sign everyone out — for a password that never moved."""
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import session_scope
    from app.main import bootstrap_admin
    from app.models import User

    monkeypatch.setenv("ADGUARDHUB_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADGUARDHUB_ADMIN_PASSWORD", "supersecret")
    get_settings.cache_clear()

    await bootstrap_admin()
    async with session_scope() as session:
        before = (await session.execute(select(User.password_hash))).scalar_one()
    login = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "supersecret"}
    )
    assert login.status_code == 200

    # The hub restarts with the same environment.
    await bootstrap_admin()
    async with session_scope() as session:
        after = (await session.execute(select(User.password_hash))).scalar_one()
    assert after == before
    assert (await client.get("/api/rules")).status_code == 200

    # A password that did move in the environment still takes effect, and ends
    # the sessions issued under the old one.
    monkeypatch.setenv("ADGUARDHUB_ADMIN_PASSWORD", "rotated-elsewhere")
    get_settings.cache_clear()
    await bootstrap_admin()
    assert (await client.get("/api/rules")).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "rotated-elsewhere"}
        )
    ).status_code == 200


async def _over(scheme: str) -> httpx.AsyncClient:
    from app.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://testserver"
    )


async def test_the_cookie_is_marked_secure_only_when_served_over_https(
    auth_client: httpx.AsyncClient,
) -> None:
    """Behind a proxy that terminates TLS the cookie must never travel over
    plain http; over plain http itself a Secure cookie would never come back."""
    async with await _over("https") as tls:
        login = await tls.post(
            "/api/auth/login", json={"username": "admin", "password": "supersecret"}
        )
        assert login.status_code == 200
        assert "secure" in login.headers["set-cookie"].lower()
        assert "httponly" in login.headers["set-cookie"].lower()
        assert (await tls.get("/api/rules")).status_code == 200

        logout = await tls.post("/api/auth/logout")
        assert "secure" in logout.headers["set-cookie"].lower()

    async with await _over("http") as plain:
        login = await plain.post(
            "/api/auth/login", json={"username": "admin", "password": "supersecret"}
        )
        assert login.status_code == 200
        assert "secure" not in login.headers["set-cookie"].lower()

        # The AdGuard-compatible login follows the same rule.
        await plain.post("/api/auth/logout")
        app_login = await plain.post(
            "/control/login", json={"name": "admin", "password": "supersecret"}
        )
        assert app_login.status_code == 200
        assert "secure" not in app_login.headers["set-cookie"].lower()

    async with await _over("https") as tls:
        app_login = await tls.post(
            "/control/login", json={"name": "admin", "password": "supersecret"}
        )
        assert "secure" in app_login.headers["set-cookie"].lower()
