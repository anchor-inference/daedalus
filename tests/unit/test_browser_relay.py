"""A server's doors for the operator's Chrome: one-time codes spent for device tokens, and the relay
WebSocket that takes a token in its first frame and carries the frames to the user daemon, split and
joined as the bridge does. Wrong, revoked and long-unused tokens are refused with the code the
extension reads as "pair again", a second Chrome with the one that names the first, and failures at
either door are limited per address."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.browser.user import (
    CLOSE_BUSY,
    CLOSE_UNAUTHORIZED,
    Attempts,
    Joiner,
    Pairing,
    PairingRefused,
    RelayDesk,
    TooMany,
    normal_code,
    split,
)
from daedalus.extensions import api_browser_user
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd

HELLO = {"t": "hello", "proto": 2, "ext_version": "0.1.0", "chrome_version": "141.0", "platform": "linux", "profile_id": "p1", "profile_name": "Work"}


class FakeSocket:
    """The extension's end of the WebSocket, as the relay desk sees it."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: asyncio.Queue[str] = asyncio.Queue()
        self.closed: asyncio.Future[tuple[int, str]] = asyncio.get_running_loop().create_future()

    async def receive(self) -> str | None:
        if self.closed.done():
            return None
        getter = asyncio.ensure_future(self.incoming.get())
        done, _ = await asyncio.wait({getter, self.closed}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            return getter.result()
        getter.cancel()
        return None

    async def send_text(self, text: str) -> None:
        if self.closed.done():
            raise RuntimeError("closed")
        self.sent.put_nowait(text)

    async def close(self, code: int, reason: str = "") -> None:
        if not self.closed.done():
            self.closed.set_result((code, reason))

    async def frame(self) -> dict[str, Any]:
        return json.loads(await asyncio.wait_for(self.sent.get(), 5))

    async def close_code(self) -> int:
        return (await asyncio.wait_for(asyncio.shield(self.closed), 5))[0]


@pytest.fixture
def run_dir() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="br-"))
    yield path / "u"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(run_dir, env="user").start()
    yield fake
    await fake.stop()


@pytest.fixture
def pairing(db: Database) -> Pairing:
    return Pairing(db)


@pytest.fixture
def desk(pairing: Pairing, run_dir: Path) -> RelayDesk:
    return RelayDesk(pairing, lambda: run_dir)


async def paired(pairing: Pairing, profile_id: str = "p1", name: str = "Work") -> str:
    code = (await pairing.mint())["code"]
    return await pairing.redeem(code, address="10.0.0.9", profile_id=profile_id, profile_name=name, ext_version="0.1.0", chrome_version="141.0")


def start(desk: RelayDesk, sock: FakeSocket, hello: dict[str, Any], address: str = "10.0.0.9") -> asyncio.Task[None]:
    sock.incoming.put_nowait(json.dumps(hello))
    return asyncio.create_task(desk.serve(sock, address=address))


# -- pairing -------------------------------------------------------------------------------------


async def test_a_code_is_spent_once_for_a_token_and_only_hashes_are_kept(pairing: Pairing, db: Database) -> None:
    minted = await pairing.mint()
    assert len(normal_code(minted["code"])) == 8 and "-" in minted["code"]
    token = await pairing.redeem(minted["code"].lower().replace("-", " "), address="a", profile_id="p1", profile_name="Work", ext_version="1", chrome_version="141")
    stored = json.dumps([await db.kv_get(Pairing.CODES, []), await db.kv_get(Pairing.DEVICES, [])])
    assert token not in stored and normal_code(minted["code"]) not in stored
    with pytest.raises(PairingRefused):
        await pairing.redeem(minted["code"], address="a", profile_id="p2", profile_name="", ext_version="", chrome_version="")
    (device,) = await pairing.devices()
    assert device.name == "Work" and (await pairing.device_of(token, address="a")) is not None


async def test_a_code_expires_after_ten_minutes(db: Database) -> None:
    now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
    pairing = Pairing(db, now=lambda: now[0])
    code = (await pairing.mint())["code"]
    now[0] += timedelta(minutes=10, seconds=1)
    with pytest.raises(PairingRefused):
        await pairing.redeem(code, address="a", profile_id="p", profile_name="", ext_version="", chrome_version="")


async def test_wrong_codes_are_limited_per_address_and_the_right_one_waits_too(pairing: Pairing) -> None:
    code = (await pairing.mint())["code"]
    for _ in range(5):
        with pytest.raises(PairingRefused):
            await pairing.redeem("AAAA-AAAA", address="1.2.3.4", profile_id="p", profile_name="", ext_version="", chrome_version="")
    with pytest.raises(TooMany):
        await pairing.redeem(code, address="1.2.3.4", profile_id="p", profile_name="", ext_version="", chrome_version="")
    # Another address is not held by this one's guesses.
    assert await pairing.redeem(code, address="5.6.7.8", profile_id="p", profile_name="", ext_version="", chrome_version="")


def test_the_overall_bound_holds_a_guesser_with_many_addresses() -> None:
    clock = [0.0]
    attempts = Attempts(per_address=5, overall=30, window=600, clock=lambda: clock[0])
    for n in range(30):
        attempts.failed(f"10.0.{n}.1")
    assert attempts.blocked("192.0.2.1")
    clock[0] = 601
    assert not attempts.blocked("192.0.2.1")


async def test_the_pair_route_is_public_and_logs_nothing_it_was_given(db: Database, pairing: Pairing, caplog: pytest.LogCaptureFixture) -> None:
    from fastapi import FastAPI  # noqa: PLC0415

    api = FastAPI()
    app = SimpleNamespace(settings=SimpleNamespace(native=False, state_dir=Path("/nonexistent"), miniapp_public_url="https://daedalus.example"), extensions={"browser_pairing": pairing})

    def auth() -> dict[str, Any]:
        raise AssertionError("the pairing door takes no app session")

    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from daedalus.browser.model import BrowserError  # noqa: PLC0415

    @api.exception_handler(BrowserError)
    async def refusal(_: Any, exc: BrowserError) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)

    api_browser_user.register(api, app, auth)  # type: ignore[arg-type]
    code = (await pairing.mint())["code"]
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        bad = await client.post("/api/browser/pair", json={"code": "ZZZZ-ZZZZ", "profile_id": "p1"})
        assert bad.status_code == 403
        good = await client.post("/api/browser/pair", json={"code": code, "profile_id": "p1", "profile_name": "Work", "ext_version": "0.1.0", "chrome_version": "141", "later": "ignored"})
        assert good.status_code == 200
    token = good.json()["token"]
    logged = caplog.text
    assert token not in logged and normal_code(code) not in logged and "ZZZZ" not in logged


# -- the relay ---------------------------------------------------------------------------------------


async def test_a_paired_chrome_is_carried_to_the_daemon_both_ways_without_its_token(daemon: FakeBrowserd, pairing: Pairing, desk: RelayDesk) -> None:
    token = await paired(pairing)
    sock = FakeSocket()
    task = start(desk, sock, {**HELLO, "token": token})
    async with asyncio.timeout(5):
        while not daemon.relays:
            await asyncio.sleep(0.01)
    relay = daemon.relays[-1]
    assert daemon.relay_opens[-1]["transport"] == "server"
    hello = await daemon.relay_frame(relay)
    assert hello["t"] == "hello" and hello["profile_name"] == "Work" and "token" not in hello
    await daemon.relay_send(relay, {"t": "welcome", "proto": 2, "daemon_version": "x", "install_name": "Daedalus", "install_kind": "server"})
    assert (await sock.frame())["t"] == "welcome"
    # A frame larger than one socket frame goes in pieces and arrives whole.
    big = {"t": "result", "id": 7, "ok": True, "value": {"data": "x" * (3 << 20)}}
    sock.incoming.put_nowait(json.dumps(big))
    got = await daemon.relay_frame(relay, timeout=10)
    assert got == big
    assert desk.connected_device() != ""
    sock.incoming.put_nowait(None)
    await asyncio.wait_for(task, 5)
    assert desk.connected_device() == ""


@pytest.mark.parametrize("token", ["", "not-a-token"])
async def test_a_wrong_token_is_refused_with_the_pair_again_code(daemon: FakeBrowserd, pairing: Pairing, desk: RelayDesk, token: str) -> None:
    await paired(pairing)
    sock = FakeSocket()
    task = start(desk, sock, {**HELLO, "token": token})
    bye = await sock.frame()
    assert bye == {"t": "bye", "reason": "unauthorized", "message": bye["message"]}
    assert await sock.close_code() == CLOSE_UNAUTHORIZED
    await task
    assert not daemon.relays, "nothing reached the daemon"


async def test_a_revoked_device_is_refused_and_its_live_relay_ends(daemon: FakeBrowserd, pairing: Pairing, desk: RelayDesk) -> None:
    token = await paired(pairing)
    sock = FakeSocket()
    task = start(desk, sock, {**HELLO, "token": token})
    async with asyncio.timeout(5):
        while not daemon.relays:
            await asyncio.sleep(0.01)
    (device,) = await pairing.devices()
    assert await pairing.revoke(device.id)
    await desk.revoked(device.id)
    assert await sock.close_code() == CLOSE_UNAUTHORIZED
    await asyncio.wait_for(task, 5)
    again = FakeSocket()
    await asyncio.wait_for(start(desk, again, {**HELLO, "token": token}), 5)
    assert await again.close_code() == CLOSE_UNAUTHORIZED


async def test_a_long_unused_pairing_has_expired(db: Database, daemon: FakeBrowserd, run_dir: Path) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    pairing = Pairing(db, now=lambda: now[0])
    token = await paired(pairing)
    now[0] += timedelta(days=91)
    sock = FakeSocket()
    await asyncio.wait_for(start(RelayDesk(pairing, lambda: run_dir), sock, {**HELLO, "token": token}), 5)
    assert await sock.close_code() == CLOSE_UNAUTHORIZED


async def test_a_second_chrome_is_refused_naming_the_one_connected_and_the_same_one_replaces_itself(daemon: FakeBrowserd, pairing: Pairing, desk: RelayDesk) -> None:
    first_token = await paired(pairing, "p1", "Work")
    second_token = await paired(pairing, "p2", "Home")
    first = FakeSocket()
    task = start(desk, first, {**HELLO, "token": first_token})
    async with asyncio.timeout(5):
        while not daemon.relays:
            await asyncio.sleep(0.01)
    second = FakeSocket()
    await asyncio.wait_for(start(desk, second, {**HELLO, "profile_id": "p2", "profile_name": "Home", "token": second_token}), 5)
    bye = await second.frame()
    assert bye["reason"] == "another_relay" and "Work" in bye["message"]
    assert await second.close_code() == CLOSE_BUSY
    assert not first.closed.done()
    # The same profile again (its worker restarted): the new socket is the live one.
    again = FakeSocket()
    replacing = start(desk, again, {**HELLO, "token": first_token})
    assert await first.close_code() == 1000
    async with asyncio.timeout(5):
        while len(daemon.relays) < 2:
            await asyncio.sleep(0.01)
    again.incoming.put_nowait(None)
    await asyncio.wait_for(asyncio.gather(task, replacing), 5)


async def test_the_daemons_own_refusal_of_a_second_relay_closes_with_the_busy_code(daemon: FakeBrowserd, pairing: Pairing, desk: RelayDesk) -> None:
    token = await paired(pairing)
    sock = FakeSocket()
    task = start(desk, sock, {**HELLO, "token": token})
    async with asyncio.timeout(5):
        while not daemon.relays:
            await asyncio.sleep(0.01)
    await daemon.relay_send(daemon.relays[-1], {"t": "bye", "reason": "another_relay", "message": "another Chrome is already connected: Laptop"})
    assert (await sock.frame())["reason"] == "another_relay"
    assert await sock.close_code() == CLOSE_BUSY
    await asyncio.wait_for(task, 5)


async def test_a_first_frame_that_is_not_hello_is_refused(desk: RelayDesk) -> None:
    sock = FakeSocket()
    await asyncio.wait_for(start(desk, sock, {"t": "check", "n": 1}), 5)
    assert await sock.close_code() == 4400


def test_pieces_split_and_join_as_the_daemon_does() -> None:
    frame = b"y" * 2500
    pieces = split(frame, 1000)
    assert [p[0] for p in pieces] == [1, 1, 0] and all(len(p) <= 1000 for p in pieces)
    join = Joiner(10_000)
    assert [join.add(p) for p in pieces] == [None, None, frame]
    with pytest.raises(ValueError):
        Joiner(10).add(b"\x00" + b"z" * 11)
