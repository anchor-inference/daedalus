"""Two-factor sign-in: the RFC 6238 arithmetic, the sealed store, and the second step of a login."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import SESSION_COOKIE, build_app
from daedalus.extensions.api_two_factor import PENDING_COOKIE, pending_cookie_value, verify_pending_cookie
from daedalus.security import operator_secrets
from daedalus.security.two_factor import (
    Attempts,
    TwoFactor,
    TwoFactorError,
    code_at,
    enrolment_uri,
    matching_step,
    new_recovery_codes,
    new_secret,
    normalise_recovery,
    qr_rows,
    step_of,
)
from daedalus.stores import pairing
from daedalus.stores.database import Database
from tests.unit.test_site_auth import TOKEN, _signed

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
"""The ASCII "12345678901234567890" of RFC 6238's SHA-1 vectors, in base32."""


@pytest.mark.parametrize(("when", "code"), [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"), (2000000000, "279037")])
def test_codes_match_the_rfc_vectors(when: int, code: str) -> None:
    # The RFC prints eight digits; six are the same number modulo a million, its last six digits.
    assert code_at(RFC_SECRET, step_of(when)) == code


def test_one_step_of_skew_either_way_and_no_more() -> None:
    now = 1_700_000_000.0
    step = step_of(now)
    for offset in (-1, 0, 1):
        assert matching_step(RFC_SECRET, code_at(RFC_SECRET, step + offset), now=now) == step + offset
    for offset in (-2, 2):
        assert matching_step(RFC_SECRET, code_at(RFC_SECRET, step + offset), now=now) is None
    # A step already spent, or an earlier one, does not count again.
    assert matching_step(RFC_SECRET, code_at(RFC_SECRET, step), now=now, after=step) is None
    assert matching_step(RFC_SECRET, code_at(RFC_SECRET, step + 1), now=now, after=step) == step + 1


def test_the_enrolment_uri_is_what_every_authenticator_reads() -> None:
    uri = enrolment_uri("JBSWY3DPEHPK3PXP", "agent.example.org")
    assert uri == "otpauth://totp/Daedalus:agent.example.org?secret=JBSWY3DPEHPK3PXP&issuer=Daedalus&algorithm=SHA1&digits=6&period=30"
    assert len(new_secret()) == 32


def test_the_qr_code_is_a_square_with_its_three_finders() -> None:
    rows = qr_rows(enrolment_uri(new_secret(), "operator"))
    size = len(rows)
    assert all(len(r) == size for r in rows) and (size - 17) % 4 == 0
    # Each finder's top edge is seven dark modules followed by a light one.
    assert rows[0].startswith("11111110") and rows[0].endswith("01111111") and rows[-7].startswith("11111110")


def test_recovery_codes_are_typed_loosely() -> None:
    codes = new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert all(len(normalise_recovery(c)) == 12 for c in codes)
    assert normalise_recovery(" ABCD-efgh ijkm ") == "abcdefghijkm"


def test_the_throttle_closes_after_five_failures_and_reopens() -> None:
    now = [0.0]
    attempts = Attempts(clock=lambda: now[0])
    for _ in range(4):
        attempts.failed()
    assert attempts.wait() == 0
    attempts.failed()
    assert attempts.wait() > 0
    now[0] = 301.0
    assert attempts.wait() == 0


# -- the store ---------------------------------------------------------------------------------


def _store(settings: Settings, db: Database, now: list[float]) -> TwoFactor:
    return TwoFactor(db, settings.secrets_dir / operator_secrets.KEY_FILE, clock=lambda: now[0])


async def test_the_secret_is_sealed_and_a_code_is_good_once(settings: Settings, db: Database) -> None:
    now = [1_700_000_000.0]
    store = _store(settings, db, now)
    secret = await store.begin()
    row = await db.fetchone("SELECT sealed FROM two_factor")
    assert secret.encode() not in bytes(row["sealed"])  # the database alone holds nothing usable
    assert not (await store.status()).enabled and (await store.status()).enrolling
    assert await store.confirm("000000" if code_at(secret, step_of(now[0])) != "000000" else "111111") is None
    codes = await store.confirm(code_at(secret, step_of(now[0])))
    assert codes is not None and (await store.status()).as_dict() | {"enabled_at": None} == {"enabled": True, "enrolling": False, "recovery_left": 10, "enabled_at": None}
    # The code that switched it on is spent; the next step's is not.
    assert await store.check(code_at(secret, step_of(now[0]))) is None
    now[0] += 30
    assert await store.check(code_at(secret, step_of(now[0]))) == "totp"
    assert await store.check(code_at(secret, step_of(now[0]))) is None
    # A recovery code works once, however it is typed.
    assert await store.check(codes[0].upper().replace("-", " ")) == "recovery"
    assert await store.check(codes[0]) is None
    assert (await store.status()).recovery_left == 9
    # A new set retires the old.
    fresh = await store.regenerate()
    assert await store.check(codes[1]) is None and await store.check(fresh[1]) == "recovery"
    with pytest.raises(TwoFactorError, match="already on"):
        await store.begin()
    await store.disable()
    left = await db.fetchone("SELECT count(*) c FROM two_factor_recovery")
    assert not await store.enabled() and left is not None and left["c"] == 0


async def test_a_replaced_key_file_says_so_rather_than_crashing(settings: Settings, db: Database) -> None:
    now = [1_700_000_000.0]
    store = _store(settings, db, now)
    secret = await store.begin()
    await store.confirm(code_at(secret, step_of(now[0])))
    key = settings.secrets_dir / operator_secrets.KEY_FILE
    key.unlink()
    with pytest.raises(TwoFactorError, match="recovery code"):
        await _store(settings, db, now).check(code_at(secret, step_of(now[0]) + 1))


async def test_the_migration_drops_passkeys_and_marks_window_codes(db: Database) -> None:
    tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "passkeys" not in tables and {"two_factor", "two_factor_recovery"} <= tables
    columns = {r["name"] for r in await db.fetchall("PRAGMA table_info(pairing_codes)")}
    assert "for_window" in columns
    code = await pairing.mint(db, for_window=True)
    redeemed = await pairing.redeem(db, code)
    assert redeemed is not None and redeemed.for_window
    plain = await pairing.redeem(db, await pairing.mint(db))
    assert plain is not None and not plain.for_window


# -- over HTTP ---------------------------------------------------------------------------------


def test_a_pending_cookie_is_signed_expires_and_is_no_session() -> None:
    key = b"k" * 32
    value = pending_cookie_value(key, "nonce")
    assert verify_pending_cookie(key, value) == "nonce"
    assert verify_pending_cookie(b"j" * 32, value) is None
    assert verify_pending_cookie(key, pending_cookie_value(key, "nonce", ttl=-1)) is None


def _app(settings: Settings, db: Database) -> SimpleNamespace:
    return SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(), front=None, extensions={}, guard=None)


@pytest.fixture
async def client(settings: Settings, db: Database) -> Any:
    settings.telegram_bot_token = TOKEN
    settings.owner_user_id = 42
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(_app(settings, db), "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


H = {"X-Daedalus-Token": "tok"}


def _now_code(secret: str, ahead: int = 0) -> str:
    return code_at(secret, step_of(time.time()) + ahead)


def _wrong(secret: str) -> str:
    good = {code_at(secret, step_of(time.time()) + k) for k in (-1, 0, 1, 2)}
    return next(c for c in ("000000", "111111", "222222", "333333", "444444") if c not in good)


async def _enrol(client: httpx.AsyncClient) -> tuple[str, list[str]]:
    setup = (await client.post("/api/auth/totp/setup", headers=H)).json()
    assert setup["uri"].startswith("otpauth://totp/Daedalus:operator?secret=" + setup["secret"])
    assert setup["qr"] and len(setup["qr"]) == len(setup["qr"][0])
    wrong = await client.post("/api/auth/totp/enable", json={"code": _wrong(setup["secret"])}, headers=H)
    assert wrong.status_code == 400
    assert not (await client.get("/api/auth/totp", headers=H)).json()["enabled"]
    done = await client.post("/api/auth/totp/enable", json={"code": _now_code(setup["secret"])}, headers=H)
    assert done.status_code == 200, done.text
    assert done.json()["enabled"] and len(done.json()["recovery_codes"]) == 10
    return setup["secret"], done.json()["recovery_codes"]


async def test_management_needs_a_signed_in_browser(client: httpx.AsyncClient) -> None:
    for path in ("/api/auth/totp/setup", "/api/auth/totp/enable", "/api/auth/totp/disable", "/api/auth/totp/recovery"):
        assert (await client.post(path, json={"code": "123456"})).status_code == 401
    assert (await client.get("/api/auth/totp")).status_code == 401
    assert (await client.post("/api/auth/totp/verify", json={"code": "123456"})).status_code == 401  # no first factor yet


async def test_a_pairing_link_then_the_code_signs_in(client: httpx.AsyncClient, db: Database) -> None:
    secret, _ = await _enrol(client)
    paired = await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    assert paired.status_code == 303 and paired.headers["location"] == "/app/"
    assert SESSION_COOKIE not in client.cookies and PENDING_COOKIE in client.cookies
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/auth/config")).json()["second_factor"] is True
    assert (await client.post("/api/auth/totp/verify", json={"code": _wrong(secret)})).status_code == 400
    done = await client.post("/api/auth/totp/verify", json={"code": _now_code(secret, 1)})
    assert done.status_code == 200 and done.json()["with"] == "totp"
    assert (await client.get("/api/auth/me")).json() == {"user_id": 42, "via": "cookie"}
    assert PENDING_COOKIE not in client.cookies
    # The same code once more, on a fresh first factor, is spent.
    client.cookies.clear()
    await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    assert (await client.post("/api/auth/totp/verify", json={"code": _now_code(secret, 1)})).status_code == 400


async def test_the_login_widget_owes_the_code_too(client: httpx.AsyncClient) -> None:
    secret, codes = await _enrol(client)
    widget = await client.post("/api/auth/telegram", json=_signed({"id": 42, "first_name": "A", "auth_date": int(time.time())}))
    assert widget.status_code == 200 and widget.json() == {"ok": True, "second_factor": True}
    assert SESSION_COOKIE not in client.cookies
    done = await client.post("/api/auth/totp/verify", json={"code": codes[3]})
    assert done.status_code == 200 and done.json()["with"] == "recovery" and done.json()["recovery_left"] == 9


async def test_the_launchers_window_link_skips_the_code(client: httpx.AsyncClient, db: Database) -> None:
    await _enrol(client)
    code = await pairing.mint(db, for_window=True, ttl_minutes=pairing.WINDOW_TTL_MINUTES)
    assert (await client.get("/api/auth/pair", params={"code": code})).status_code == 303
    assert (await client.get("/api/auth/me")).json()["via"] == "cookie"


async def test_a_first_factor_buys_five_guesses(client: httpx.AsyncClient, db: Database) -> None:
    secret, _ = await _enrol(client)
    await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    answers = [(await client.post("/api/auth/totp/verify", json={"code": _wrong(secret)})).status_code for _ in range(5)]
    assert answers == [400, 400, 400, 400, 401]
    # Burnt: even the right code is refused, and the throttle closes the door for everyone else too.
    assert (await client.post("/api/auth/totp/verify", json={"code": _now_code(secret, 1)})).status_code == 401
    client.cookies.clear()
    await client.get("/api/auth/pair", params={"code": await pairing.mint(db)})
    throttled = await client.post("/api/auth/totp/verify", json={"code": _now_code(secret, 1)})
    assert throttled.status_code == 429 and int(throttled.headers["retry-after"]) > 0


async def test_turning_it_off_and_new_recovery_codes_need_a_code(client: httpx.AsyncClient) -> None:
    secret, codes = await _enrol(client)
    assert (await client.post("/api/auth/totp/setup", headers=H)).status_code == 409
    assert (await client.post("/api/auth/totp/recovery", json={"code": _wrong(secret)}, headers=H)).status_code == 400
    fresh = await client.post("/api/auth/totp/recovery", json={"code": _now_code(secret, 1)}, headers=H)
    assert fresh.status_code == 200 and set(fresh.json()["recovery_codes"]).isdisjoint(codes)
    assert (await client.post("/api/auth/totp/disable", json={"code": codes[0]}, headers=H)).status_code == 400  # retired
    off = await client.post("/api/auth/totp/disable", json={"code": fresh.json()["recovery_codes"][0]}, headers=H)
    assert off.status_code == 200 and off.json()["enabled"] is False
    # Off again: a pairing link signs in on its own.
    assert (await client.get("/api/auth/totp", headers=H)).json() == {"enabled": False, "enrolling": False, "recovery_left": 0, "enabled_at": None}
