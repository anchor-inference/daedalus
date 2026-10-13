"""Signing in on the site: with Telegram's Login Widget or a pairing link, then the two-factor code when that is on."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import (
    SESSION_COOKIE,
    build_app,
    session_cookie_value,
    validate_login_widget,
    verify_session_cookie,
)
from daedalus.stores import pairing
from daedalus.stores.database import Database

TOKEN = "123456:ABC-DEF"


def _signed(fields: dict[str, object]) -> dict[str, object]:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    return {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


def test_login_widget_data_is_verified_against_the_bot_token() -> None:
    fields = {"id": 42, "first_name": "A", "username": "a", "auth_date": int(time.time())}
    assert validate_login_widget(_signed(fields), TOKEN)["id"] == 42
    with pytest.raises(ValueError, match="bad signature"):
        validate_login_widget({**_signed(fields), "id": 43}, TOKEN)
    with pytest.raises(ValueError, match="bad signature"):
        validate_login_widget(_signed(fields), "other-token")
    with pytest.raises(ValueError, match="expired"):
        validate_login_widget(_signed({**fields, "auth_date": int(time.time()) - 2 * 86400}), TOKEN)
    with pytest.raises(ValueError, match="incomplete"):
        validate_login_widget({"id": 42}, TOKEN)


def test_session_cookie_round_trip_and_forgery() -> None:
    secret = b"s" * 32
    value = session_cookie_value(secret, 42)
    assert verify_session_cookie(secret, value) == 42
    assert verify_session_cookie(b"t" * 32, value) is None
    assert verify_session_cookie(secret, value[:-1] + ("0" if value[-1] != "0" else "1")) is None
    assert verify_session_cookie(secret, "garbage") is None
    assert verify_session_cookie(secret, session_cookie_value(secret, 42, ttl=-1)) is None
    assert verify_session_cookie(secret, session_cookie_value(secret, 0)) == 0  # the owner of an installation with no Telegram


# -- pairing links: signing in with no Telegram at all -----------------------------


class _Bot:
    def __init__(self, username: str) -> None:
        self.username = username

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(username=self.username)


def _app(settings: Settings, db: Database, *, bot: str | None = None) -> SimpleNamespace:
    """Stand-in for daedalus.app.Application covering what the auth routes read."""
    front = SimpleNamespace(bot=_Bot(bot)) if bot else None
    return SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(), front=front, extensions={}, guard=None)


@pytest.fixture
async def client(settings: Settings, db: Database) -> Any:
    settings.telegram_bot_token = ""
    settings.owner_user_id = 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


H = {"X-Daedalus-Token": "tok"}


async def test_auth_config_names_the_ways_in_and_never_needs_a_bot(settings: Settings, db: Database) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        body = (await c.get("/api/auth/config")).json()
    assert body == {"telegram": None, "pairing": False, "second_factor": False}
    await pairing.mint(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db, bot="daedalus_bot"), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        body = (await c.get("/api/auth/config")).json()
    assert body == {"telegram": {"bot_username": "daedalus_bot"}, "pairing": True, "second_factor": False}


async def test_a_pairing_link_opens_the_app_once(client: httpx.AsyncClient, db: Database) -> None:
    code = await pairing.mint(db)
    response = await client.get("/api/auth/pair", params={"code": code})
    assert response.status_code == 303 and response.headers["location"] == "/app/"
    assert (await client.get("/api/auth/me")).json() == {"user_id": 1, "via": "cookie"}
    # Spent: the same link a second time lands on the app with the reason, and so does one that was never minted;
    # the browser that paired is still signed in, and one that is not sees the login page say why.
    for spent in (code, "not-a-code"):
        again = await client.get("/api/auth/pair", params={"code": spent})
        assert again.status_code == 303 and again.headers["location"] == "/app/?pairing=spent"
        assert "set-cookie" not in again.headers


async def test_an_installation_with_no_telegram_account_still_holds_a_session(settings: Settings, db: Database) -> None:
    """Owner id zero is what an installation without Telegram has; a cookie must still mean the owner."""
    settings.telegram_bot_token = ""
    settings.owner_user_id = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        assert (await c.get("/api/auth/pair", params={"code": await pairing.mint(db)})).status_code == 303
        assert (await c.get("/api/auth/me")).json() == {"user_id": 0, "via": "cookie"}
        assert (await c.post("/api/auth/totp/setup")).status_code == 200


async def test_using_a_link_revokes_the_ones_still_outstanding(db: Database) -> None:
    stale = await pairing.mint(db)
    fresh = await pairing.mint(db)
    assert await pairing.outstanding(db) == 2
    assert await pairing.redeem(db, fresh)
    assert not await pairing.redeem(db, stale)
    assert await pairing.outstanding(db) == 0


async def test_a_code_expires(db: Database) -> None:
    code = await pairing.mint(db, ttl_minutes=-1)
    assert await pairing.outstanding(db) == 0
    assert not await pairing.redeem(db, code)


async def test_the_printed_link_is_readable_by_its_owner_only(db: Database, tmp_path: Path) -> None:
    url = await pairing.announce(db, tmp_path / "state", "https://example.org/")
    path = tmp_path / "state" / pairing.URL_FILE
    assert path.read_text(encoding="utf-8").strip() == url
    assert path.stat().st_mode & 0o777 == 0o600
    assert url.startswith("https://example.org/api/auth/pair?code=")


async def test_the_session_cookie_is_secure_only_when_the_installation_is_reached_over_tls(client: httpx.AsyncClient, db: Database, settings: Settings) -> None:
    plain = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    assert "secure" not in plain.headers["set-cookie"].lower()  # http on the machine itself would never get it back
    # A header any client can send does not make the connection TLS; the public address does.
    claimed = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)}, headers={"x-forwarded-proto": "https"})
    assert "secure" not in claimed.headers["set-cookie"].lower()
    settings.miniapp_public_url = "https://agent.example.test"
    secure = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    assert "secure" in secure.headers["set-cookie"].lower()
    settings.miniapp_public_url = ""


async def test_the_cookie_secret_survives_the_bot_token_and_changes_with_the_api_token(settings: Settings, db: Database) -> None:
    settings.telegram_bot_token = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        await c.get("/api/auth/pair", params={"code": await pairing.mint(db)})
        cookie = c.cookies[SESSION_COOKIE]
        assert (await c.get("/api/auth/me")).status_code == 200
    # A rotated API token invalidates what the old one signed, and the per-installation secret is kept.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "other")), base_url="http://test") as c:  # type: ignore[arg-type]
        c.cookies.set(SESSION_COOKIE, cookie)
        assert (await c.get("/api/auth/me")).status_code == 401
    assert await db.kv_get("session_secret")


async def test_signing_out_everywhere_ends_every_session_but_reissues_this_one(client: httpx.AsyncClient, db: Database) -> None:
    first = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    old_cookie = first.headers["set-cookie"].split(";")[0]
    assert (await client.get("/api/auth/me")).json()["via"] == "cookie"
    revoked = await client.post("/api/auth/sessions/revoke")
    assert revoked.status_code == 200 and "set-cookie" in revoked.headers
    # The old cookie is dead; the one the revoke answered with is alive.
    client.cookies.clear()
    name, _, value = old_cookie.partition("=")
    assert (await client.get("/api/auth/me", cookies={name: value})).status_code == 401
    client.cookies.set(name, revoked.headers["set-cookie"].split(";")[0].partition("=")[2])
    assert (await client.get("/api/auth/me")).json()["via"] == "cookie"
