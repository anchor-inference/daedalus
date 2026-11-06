"""The operator's own Chrome, end to end on one machine: the built extension loaded into a real
Chromium, Chrome starting the real ``browserd bridge`` as its native messaging host, the real user
daemon (``serve --env user --backend attach``) behind it, and the host's browser service driving it.

What a person would check by hand, in order:

- the relay connects, and both sides say so (the daemon's ``relay.status``, the side panel's phase);
- the connection check runs both ways: the panel's check reaches the host as an event and comes back
  confirmed, and the host's check flashes in the panel;
- a conversation switched to the operator's Chrome opens its group there, reads a local page and
  clicks on it, while a tab the operator opened on the same site is never grouped, never debugged
  and never moved;
- cookie values never leave Chrome, and writing or clearing cookies is refused;
- the same again over a server's door: the host's relay WebSocket on loopback, paired with a one-time
  code typed into the side panel's settings;
- two Chromes paired with that door at once, each with the extension as released: both stay
  connected, the agent chooses, each keeps to its own tabs and credential, one reloading its
  extension comes back to its own tabs, and one quitting ends its own work and no other's;
- the side panel against the whole host: the credential handed over the relay, a new conversation
  started from the panel on the operator's current tab, a scripted model reading that very tab, and
  the agent's request for a new site approved inline from the keyboard.

Opt in with ``DAEDALUS_CHROME_E2E=1``, and give it ``DAEDALUS_BROWSERD_BIN`` (a built browserd) and
``CHROMIUM`` (an unbranded Chromium: branded Chrome ignores ``--load-extension`` since 137, so
Playwright's under ``~/.cache/ms-playwright`` is the one to use), and ``DAEDALUS_EXTENSION_DIR``: the
extension unpacked, as ``desktop/stage-extension.sh <dir>`` stages the release the lock pins, or a
``dist/`` built in the daedalus-chrome-extension repository. Everything stays on loopback. Run it
under a memory cap::

    systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \\
      env DAEDALUS_CHROME_E2E=1 DAEDALUS_BROWSERD_BIN=... CHROMIUM=... DAEDALUS_EXTENSION_DIR=... \\
      uv run --frozen pytest tests/integration/test_user_chrome_real.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import os
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.browser.model import USER, BrowserError, Owner
from daedalus.browser.panel import EXTENSION_ID
from daedalus.browser.service import Browsers
from daedalus.browser.user import Pairing, RelayDesk
from daedalus.config import BrowserConfig
from daedalus.stores.database import Database

REPO = Path(__file__).resolve().parents[2]
BINARY = os.environ.get("DAEDALUS_BROWSERD_BIN", "")
CHROMIUM = os.environ.get("CHROMIUM", "")
EXTENSION_DIR = os.environ.get("DAEDALUS_EXTENSION_DIR", "")
EXTENSION = Path(EXTENSION_DIR).resolve()
SITE = REPO / "browserd" / "internal" / "rpc" / "testdata" / "site"
# The extension lives in its own repository, so its src/protocol.ts is not here to read. The id is
# the host's own constant, and the run below still proves the loaded extension has it: the worker's
# address is checked against it. The host name is the bridge's (browserd's bridge.HostName); a
# mismatch shows as a relay that never connects.
HOST_NAME = "com.daedalus.browser"
SECRET = "s3cr3t-cookie-value"

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_CHROME_E2E") != "1", reason="set DAEDALUS_CHROME_E2E=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_BROWSERD_BIN to a built browserd"),
    pytest.mark.skipif(not CHROMIUM or not Path(CHROMIUM).is_file(), reason="set CHROMIUM to an unbranded Chromium"),
    pytest.mark.skipif(not EXTENSION_DIR or not (EXTENSION / "manifest.json").is_file(), reason="set DAEDALUS_EXTENSION_DIR to the unpacked extension: desktop/stage-extension.sh <dir>"),
]


class Everyone:
    async def exists(self, owner: Owner) -> bool:
        return True

    async def label(self, owner: Owner) -> str:
        return "tester"


class Bus:
    """The event bus as the browser service publishes to it: every event kept, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, kind: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((kind, payload))

    def user(self, kind: str) -> list[dict[str, Any]]:
        return [p for k, p in self.events if k == "browser.user" and p.get("kind") == kind]


async def until(predicate: Any, what: str, timeout: float = 20.0) -> Any:
    """Polls a sync or async predicate until it returns something true."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.2)


@pytest.fixture
def site() -> Iterator[str]:
    """The daemon's own test site on loopback. Every answer sets a cookie whose value must never
    reach the host, which is how the run shows cookie values stay inside Chrome."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header("Set-Cookie", f"e2e_secret={SECRET}; Path=/")
            super().end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def scratch() -> Iterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="uc-"))
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def daemon(scratch: Path) -> AsyncIterator[Path]:
    run, state = scratch / "run", scratch / "state"
    log = (scratch / "daemon.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        BINARY, "serve", "--env", "user", "--backend", "attach", "--install-name", "E2E", "--run-dir", str(run), "--state-dir", str(state),
        stdout=log, stderr=log,
    )
    try:
        await until(lambda: (run / "endpoint").is_file(), "the user daemon's endpoint")
        yield run
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 10)
        log.close()
        if os.environ.get("E2E_SHOW_LOGS"):
            print((scratch / "daemon.log").read_text(errors="replace"))


def register_bridge(profile: Path, scratch: Path, daemon: Path) -> None:
    hosts = profile / "NativeMessagingHosts"
    hosts.mkdir(parents=True)
    wrapper = scratch / "daedalus-bridge"
    wrapper.write_text(f"#!/bin/sh\nexec '{BINARY}' bridge --run-dir '{daemon}' \"$@\"\n")
    wrapper.chmod(0o755)
    (hosts / f"{HOST_NAME}.json").write_text(json.dumps({
        "name": HOST_NAME,
        "description": "Daedalus, end-to-end run",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }))


@pytest.fixture
async def chrome(scratch: Path, daemon: Path) -> AsyncIterator[Any]:
    """Chromium with the built extension, and the real bridge registered as its native messaging host
    in the profile's own NativeMessagingHosts folder (where Chromium looks for a user-level host when
    it runs with --user-data-dir), exactly as the launcher's wrapper starts it."""
    from playwright.async_api import async_playwright  # noqa: PLC0415 — only this opt-in run needs it

    profile = scratch / "chrome"
    register_bridge(profile, scratch, daemon)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile), executable_path=CHROMIUM, headless=True,
            args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
        )
        try:
            worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
            assert EXTENSION_ID in worker.url, f"the manifest key did not pin the id: {worker.url}"
            yield SimpleNamespace(context=context, worker=worker)
        finally:
            await context.close()


async def chromes(service: Browsers) -> list[dict[str, Any]]:
    status = await service.user_status()
    return [c for c in (status.get("relay") or {}).get("chromes") or [] if c.get("connected")]


async def connected(service: Browsers, transport: str) -> dict[str, Any] | None:
    return next((c for c in await chromes(service) if c.get("transport") == transport), None)


async def check_both_ways(service: Browsers, bus: Bus, panel: Any) -> None:
    # The panel's check: it reaches the host as an event, and the panel shows it confirmed.
    before = len(bus.user("check"))
    await panel.click("#check")
    await panel.wait_for_selector("#check-result[data-ok='true']", timeout=8000)
    assert "✓" in await panel.inner_text("#check-result")
    await until(lambda: len(bus.user("check")) > before, "the panel's check reaching the host")
    # The host's check: relay.notify answers with a round trip and flashes in the panel.
    checked = await service.check_user((await chromes(service))[0]["chrome"])
    assert checked["ok"] and checked["rtt_ms"] >= 0
    await panel.wait_for_selector("#notice:not([hidden])", timeout=8000)


async def drive(service: Browsers, owner: Owner, site: str, chrome: str = "") -> tuple[str, str]:
    """Open the conversation's group in the operator's Chrome on the shop, read it, click a link."""
    opened = await service.open(owner, url=f"{site}/shop/index.html", actor=f"agent:{owner.id}", chrome=chrome)
    group, tab = opened["group"]["id"], opened["tab"]["id"]
    assert opened["group"]["env"] == USER, opened["group"]
    snapshot = await service.call(group, "page.snapshot", {"tab_id": tab}, what="reading", timeout=30)
    assert 'heading "Find something"' in snapshot["text"], snapshot["text"]
    ref = next(line.split("[ref=")[1].split("]")[0] for line in snapshot["text"].splitlines() if 'link "Shoes"' in line)
    acted = await service.call(group, "page.act", {"tab_id": tab, "action": "click", "ref": ref, "element": "the Shoes link"}, what="clicking", timeout=30)
    assert acted["ok"], acted

    async def landed() -> bool:
        text = await service.call(group, "page.text", {"tab_id": tab}, what="reading", timeout=30)
        return "Running shoes" in text["text"]

    await until(landed, "the click to land on the results page")
    return group, tab


async def untouched(worker: Any, operator_url: str) -> None:
    """The operator's own tab: still where they left it, in no group of the agent's, and no debugger of
    the extension's on it.

    ``chrome.debugger.getTargets().attached`` cannot tell: it is true for every page here, because
    Playwright drives the browser over the same protocol. What can tell is the extension's own attach:
    a second ``chrome.debugger.attach`` by the extension to a tab it already holds fails, and to one
    it does not hold succeeds (and is undone at once). The agent's tab is probed too, so a probe that
    always succeeds cannot pass for an untouched tab.
    """
    seen = await worker.evaluate(
        """async (url) => {
            const probe = async (tabId) => {
                try {
                    await chrome.debugger.attach({ tabId }, "1.3");
                } catch (error) {
                    return String(error && error.message || error);
                }
                await chrome.debugger.detach({ tabId });
                return "";
            };
            const tabs = await chrome.tabs.query({});
            const mine = tabs.filter((t) => t.url === url);
            const agents = tabs.filter((t) => t.groupId !== -1);
            return {
                mine: await Promise.all(mine.map(async (t) => ({ id: t.id, group: t.groupId, held: await probe(t.id) }))),
                agents: await Promise.all(agents.map(async (t) => ({ id: t.id, url: t.url, held: await probe(t.id) }))),
            };
        }""",
        operator_url,
    )
    mine, agents = seen["mine"], seen["agents"]
    assert len(mine) == 1, f"the operator's tab is gone or doubled: {seen}"
    assert mine[0]["group"] == -1, f"the operator's tab was put in a group: {seen}"
    assert mine[0]["held"] == "", f"the extension holds a debugger on the operator's tab: {seen}"
    assert agents and all(a["held"] for a in agents), f"the probe does not see the extension's own debugger on the agent's tab: {seen}"


async def test_the_operators_chrome_end_to_end(db: Database, site: str, daemon: Path, chrome: Any) -> None:
    port = int(site.rsplit(":", 1)[1])
    rules = {"sealed_ports": [], "services_ports": [[port, port]], "local_sites": "allow", "lan_allow": [], "egress_allow": ["127.0.0.1"]}
    bus = Bus()
    service = Browsers(db, run_dirs={"container": None, "host": None, USER: daemon}, config=lambda: BrowserConfig(), owners=Everyone(), bus=bus, wall=lambda env: rules)  # type: ignore[arg-type]
    context, worker = chrome.context, chrome.worker
    # The operator's own tab, opened before the agent has anything, on the very site the agent uses.
    operator = await context.new_page()
    operator_url = f"{site}/steps.html"
    await operator.goto(operator_url)
    panel = context.pages[0]
    await panel.goto(f"chrome-extension://{EXTENSION_ID}/panel.html")
    await service.start()
    server_task: asyncio.Task[None] | None = None
    uvicorn_server: Any = None
    try:
        assert await service.wait_available(USER, timeout=20)

        # Local: through native messaging and the real bridge.
        native = await until(lambda: connected(service, "native"), "the relay through the bridge")
        assert native["profile_name"] == "Chrome" and native["ext_version"], native
        await panel.wait_for_selector("#phase[data-phase='connected']", timeout=10000)
        assert await panel.inner_text("#install") == "E2E"
        await check_both_ways(service, bus, panel)

        owner = Owner("session", "uc1", session_id="uc1")
        # The agent's browser until the operator switches: with none configured, open must not fall
        # back into the operator's Chrome.
        assert await service.env_for(owner) != USER
        await service.choices.switch("uc1", USER, consent=True)
        assert await service.env_for(owner) == USER
        with pytest.raises(BrowserError):
            await service.open(owner, url=f"{site}/shop/index.html", fresh=True, actor="agent:uc1")
        group, _ = await drive(service, owner, site)
        await untouched(worker, operator_url)
        assert operator.url == operator_url

        # Cookie values stay in Chrome: names at most, and no writing or clearing.
        listed = await service.call(group, "cookies.list", {"group_id": group}, what="reading cookies", timeout=30)
        assert "e2e_secret" in json.dumps(listed) and SECRET not in json.dumps(listed), listed
        for method, params in (("cookies.set", {"group_id": group, "cookies": [{"name": "x", "value": "y", "url": site}]}), ("cookies.clear", {"group_id": group})):
            with pytest.raises(BrowserError):
                await service.call(group, method, params, what="changing cookies", timeout=30)

        # A server's door: the host's relay WebSocket on loopback, paired with a one-time code.
        import uvicorn  # noqa: PLC0415
        from fastapi import FastAPI  # noqa: PLC0415

        from daedalus.extensions import api_browser_user  # noqa: PLC0415

        pairing = Pairing(db)
        desk = RelayDesk(pairing, lambda: daemon, install_name=lambda: "E2E server")
        api = FastAPI()
        app = SimpleNamespace(settings=SimpleNamespace(native=False, state_dir=Path("/nonexistent"), miniapp_public_url=""),
                              extensions={"browser": service, "browser_pairing": pairing, "browser_relays": desk})

        def auth() -> dict[str, Any]:
            raise AssertionError("the public doors take no app session")

        api_browser_user.register(api, app, auth)  # type: ignore[arg-type]
        uvicorn_server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        server_task = asyncio.create_task(uvicorn_server.serve())
        await until(lambda: uvicorn_server.started, "the test server")
        server_port = uvicorn_server.servers[0].sockets[0].getsockname()[1]

        code = (await pairing.mint())["code"]
        # Connected, the panel shows the connection; the pairing form is behind this button.
        if await panel.locator("#pair-again").is_visible():
            await panel.click("#pair-again")
        await panel.fill("#server", f"http://127.0.0.1:{server_port}")
        await panel.fill("#code", code)
        await panel.click("#pair")
        served = await until(lambda: connected(service, "server"), "the relay through the host's WebSocket", timeout=30)
        assert served["profile_name"] == "Chrome", served
        assert desk.connected_devices(), "the desk holds no relay"
        await panel.wait_for_selector("#phase[data-phase='connected']", timeout=10000)
        await until(lambda: panel.inner_text("#install"), "the panel to name the installation")
        assert await panel.inner_text("#install") == "E2E server"
        await check_both_ways(service, bus, panel)
        # The relay through the bridge ended when the extension moved to the server, and its group
        # goes once that relay's grace has run out; the conversation then opens again in the same
        # Chrome, which a server knows by another id.
        last: BrowserError | None = None
        for _ in range(120):
            try:
                # Named, as the agent would name it: a conversation never slides to another by itself.
                await drive(service, owner, site, chrome=served["chrome"])
                break
            except BrowserError as exc:
                last = exc
                await asyncio.sleep(0.5)
        else:
            raise AssertionError(f"the group never opened again over the server's relay: {last}")
        await untouched(worker, operator_url)
    finally:
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True
        if server_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server_task, 10)
        await service.close()


async def launched(playwright: Any, profile: Path) -> Any:
    """Another Chromium with the extension, with a profile of its own: another computer, as far as
    the host can tell."""
    context = await playwright.chromium.launch_persistent_context(
        str(profile), executable_path=CHROMIUM, headless=True,
        args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
    )
    worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
    assert EXTENSION_ID in worker.url, worker.url
    panel = context.pages[0]
    await panel.goto(f"chrome-extension://{EXTENSION_ID}/panel.html")
    return SimpleNamespace(context=context, worker=worker, panel=panel)


async def pair(chrome: Any, pairing: Pairing, port: int, name: str) -> None:
    """What the operator does in the panel's settings: the code from the app, and the browser's name."""
    code = (await pairing.mint())["code"]
    await chrome.panel.fill("#server", f"http://127.0.0.1:{port}")
    await chrome.panel.fill("#code", code)
    await chrome.panel.click("#pair")
    await chrome.panel.wait_for_selector("#phase[data-phase='connected']", timeout=20000)
    await chrome.panel.wait_for_selector("#toggle-settings:not([hidden])", timeout=20000)
    if not await chrome.panel.locator("#name").is_visible():
        await chrome.panel.click("#toggle-settings")
    await chrome.panel.fill("#name", name)
    await chrome.panel.locator("#name").blur()


async def tab_ids(chrome: Any) -> list[dict[str, Any]]:
    """The tabs in tab groups of this Chromium, by what its own extension API says."""
    return await chrome.worker.evaluate(  # type: ignore[no-any-return]
        """async () => {
            const groups = Object.fromEntries((await chrome.tabGroups.query({})).map((g) => [g.id, g.title]));
            return (await chrome.tabs.query({})).filter((t) => t.groupId !== -1).map((t) => ({ id: t.id, url: t.url, title: groups[t.groupId] }));
        }"""
    )


async def test_two_chromes_stay_connected_and_keep_apart(db: Database, site: str, daemon: Path, scratch: Path) -> None:
    """The operator's report: Chrome paired on one computer, and the other's panel says "Another Chrome
    is already connected". Here both are the released extension in real Chromiums, through the real
    door, against the real daemon."""
    import uvicorn  # noqa: PLC0415
    from fastapi import FastAPI  # noqa: PLC0415
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from daedalus.browser.panel import PanelGrants, PanelHerald  # noqa: PLC0415
    from daedalus.extensions import api_browser_user  # noqa: PLC0415

    port = int(site.rsplit(":", 1)[1])
    rules = {"sealed_ports": [], "services_ports": [[port, port]], "local_sites": "allow", "lan_allow": [], "egress_allow": ["127.0.0.1"]}
    bus = Bus()
    service = Browsers(db, run_dirs={"container": None, "host": None, USER: daemon}, config=lambda: BrowserConfig(), owners=Everyone(), bus=bus, wall=lambda env: rules)  # type: ignore[arg-type]
    pairing = Pairing(db)
    desk = RelayDesk(pairing, lambda: daemon, install_name=lambda: "E2E server")
    grants = PanelGrants(db)
    herald = PanelHerald(grants, call=service.call_extension, chromes=service.connected_chromes, base=lambda: "http://127.0.0.1")

    async def handed(kind: str, data: dict[str, Any]) -> None:
        if kind == "relay.connected":
            await herald.grant(str(data.get("chrome") or ""))

    service.relay_hooks.append(handed)
    api = FastAPI()
    app = SimpleNamespace(settings=SimpleNamespace(native=False, state_dir=Path("/nonexistent"), miniapp_public_url=""),
                          extensions={"browser": service, "browser_pairing": pairing, "browser_relays": desk, "browser_panel": grants})

    def auth() -> dict[str, Any]:
        raise AssertionError("the public doors take no app session")

    api_browser_user.register(api, app, auth)  # type: ignore[arg-type]
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    server_task = asyncio.create_task(server.serve())
    await until(lambda: server.started, "the test server")
    server_port = server.servers[0].sockets[0].getsockname()[1]
    await service.start()
    try:
        assert await service.wait_available(USER, timeout=20)
        async with async_playwright() as p:
            windows = await launched(p, scratch / "windows")
            mac = await launched(p, scratch / "mac")
            await pair(windows, pairing, server_port, "Windows")
            await pair(mac, pairing, server_port, "MacBook")

            # Both connected at once: this is what used to be refused.
            both = await until(lambda: _two(service), "both Chromes connected", timeout=30)
            assert await windows.panel.locator("#phase").get_attribute("data-phase") == "connected"
            assert await mac.panel.locator("#phase").get_attribute("data-phase") == "connected"
            ids = {c["profile_name"]: c["chrome"] for c in both}
            assert set(ids) == {"Windows", "MacBook"}, both
            assert desk.connected_devices() == set(ids.values())

            # Each extension was handed its own token, down its own relay.
            async def token_of(chrome: Any) -> str:
                stored = await chrome.worker.evaluate("async () => (await chrome.storage.session.get('panelGrant')).panelGrant")
                return str((stored or {}).get("token") or "")

            first = await until(lambda: token_of(windows), "the panel token in the first Chrome")
            second = await until(lambda: token_of(mac), "the panel token in the second Chrome")
            assert first != second
            assert await grants.check(first) == ids["Windows"] and await grants.check(second) == ids["MacBook"]

            # A conversation that says nothing is told them; named, each opens on its own computer.
            at_win, at_mac = Owner("session", "win", session_id="win"), Owner("session", "mac", session_id="mac")
            for owner in (at_win, at_mac):
                await service.choices.switch(owner.id, USER, consent=True)
            with pytest.raises(BrowserError, match="several of the operator's Chromes"):
                await service.open(at_win, actor="agent:win")
            assert not await tab_ids(windows) and not await tab_ids(mac)
            drove = await asyncio.gather(drive(service, at_win, site, chrome="Windows"), drive(service, at_mac, site, chrome="MacBook"))
            (group_win, tab_win), (group_mac, tab_mac) = drove
            assert group_win != group_mac and ids["Windows"] in group_win and ids["MacBook"] in group_mac
            seen_win, seen_mac = await tab_ids(windows), await tab_ids(mac)
            print(f"chrome tab ids: windows {[t['id'] for t in seen_win]}, mac {[t['id'] for t in seen_mac]}; daemon tabs {tab_win} {tab_mac}")
            assert len(seen_win) == 1 and len(seen_mac) == 1, (seen_win, seen_mac)
            # Each group's tab is in its own Chromium and nowhere else, and their pages are their own.
            await service.call(group_win, "page.navigate", {"tab_id": tab_win, "url": f"{site}/steps.html"}, what="going", timeout=30)
            seen_win, seen_mac = await tab_ids(windows), await tab_ids(mac)
            assert seen_win[0]["url"] == f"{site}/steps.html" and "steps" not in seen_mac[0]["url"], (seen_win, seen_mac)

            # Its worker stopping, as Chrome stops one, brings a Chrome back to its own tabs under the
            # same id, with the other's relay and tab untouched.
            current = await chromes(service)
            connected_since = next(c for c in current if c["chrome"] == ids["MacBook"])["since"]
            cdp = await windows.context.new_cdp_session(windows.panel)
            await cdp.send("ServiceWorker.enable")
            await cdp.send("ServiceWorker.stopAllWorkers")
            await windows.panel.reload()
            await until(lambda: _reconnected(service, ids["Windows"], current), "the first Chrome connecting again", timeout=30)
            still = next(c for c in await chromes(service) if c["chrome"] == ids["MacBook"])
            assert still["since"] == connected_since, "the other Chrome's relay was touched"
            await service.call(group_mac, "page.text", {"tab_id": tab_mac}, what="reading", timeout=30)
            reopened = await service.open(at_win, actor="agent:win")
            assert reopened["group"]["id"] == group_win and not reopened["created"], reopened
            await service.call(group_win, "page.text", {"tab_id": tab_win}, what="reading the tab after the restart", timeout=30)

            # One quitting ends its own work; the conversation hears of it and does not slide over.
            await windows.context.close()
            await until(lambda: _lost(service, ids["Windows"]), "the first Chrome's relay to go", timeout=30)
            await service.call(group_mac, "page.text", {"tab_id": tab_mac}, what="reading", timeout=30)

            async def closed() -> bool:
                return bool((await service.get_row(group_win))["status"] != "open")

            await until(closed, "the group of the Chrome that quit to close", timeout=70)
            with pytest.raises(BrowserError, match="«Windows», which is not connected"):
                await service.open(at_win, actor="agent:win")
            assert len(await tab_ids(mac)) == 1, "the conversation of the other computer opened here"
            await service.call(group_mac, "page.text", {"tab_id": tab_mac}, what="reading", timeout=30)
            await mac.context.close()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server_task, 10)
        await service.close()


async def _two(service: Browsers) -> list[dict[str, Any]] | None:
    found = await chromes(service)
    return found if len(found) == 2 and all(c.get("profile_name") in ("Windows", "MacBook") for c in found) else None


async def _two_connected(service: Browsers) -> bool:
    return len(await chromes(service)) == 2


async def _reconnected(service: Browsers, chrome: str, before: list[dict[str, Any]]) -> bool:
    was = next(c for c in before if c["chrome"] == chrome)["since"]
    return any(c["chrome"] == chrome and c["since"] != was for c in await chromes(service))


async def _lost(service: Browsers, chrome: str) -> bool:
    return not any(c["chrome"] == chrome for c in await chromes(service))


class ScriptedModel:
    """A model that answers from a script, one step per request: a tool call or a final text."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        from daedalus.providers.pricing import ModelPricing  # noqa: PLC0415

        self.script = list(script)
        self.requests: list[Any] = []
        self.endpoint = SimpleNamespace(id="scripted", kind="scripted", pricing={"scripted-model": ModelPricing(input_limit=128_000, limit_source="scripted")})

    async def stream_with_tools(self, request: Any) -> AsyncIterator[Any]:
        from protocore.contracts.llm import ProviderDelta, ProviderDeltaKind  # noqa: PLC0415

        self.requests.append(request)
        step = self.script.pop(0) if self.script else {"text": "nothing left to do"}
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0}
        if "tool" in step:
            call = f"call_{len(self.requests)}"
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call, tool_name=step["tool"])
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_input, tool_call_id=call, tool_input_delta=json.dumps(step["args"]))
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_stop, tool_call_id=call, tool_name=step["tool"], tool_input_final=step["args"], is_block_end=True)
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="tool_use")
            return
        yield ProviderDelta(kind=ProviderDeltaKind.text, content=step["text"])
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    async def complete_structured(self, request: Any, response_schema: dict[str, Any]) -> Any:
        return await self.complete_text(request)

    async def complete_text(self, request: Any) -> Any:
        from protocore.contracts.llm import LLMResponse  # noqa: PLC0415
        from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock  # noqa: PLC0415

        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='{"title": "Shop", "summary": "s"}')]), stop_reason=StopReason.end_turn)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def aclose(self) -> None:
        return None

    def results(self) -> str:
        """Every tool result the model was shown, as text."""
        return json.dumps([str(m) for request in self.requests for m in request.messages])


@pytest.mark.skipif(not (REPO / "miniapp" / "dist" / "index.html").is_file(), reason="build the app: cd miniapp && npm run build")
async def test_the_side_panel_end_to_end(settings: Any, db: Database, site: str, daemon: Path, chrome: Any, scratch: Path) -> None:
    """The side panel against the whole host on this computer. The panel page is opened in a tab of
    the operator's window rather than as Chrome's own side panel, which only the operator's gesture
    opens; it is the same page, and the operator's page stays the window's active tab beside it."""
    import socket  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    from daedalus.config import BrowserUserConfig  # noqa: PLC0415
    from daedalus.extensions import browser as browser_extension  # noqa: PLC0415
    from daedalus.extensions.api import build_app  # noqa: PLC0415
    from daedalus.host.session_runner import SessionManager  # noqa: PLC0415
    from tests.support.models import model_config  # noqa: PLC0415

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        api_port = probe.getsockname()[1]
    port = int(site.rsplit(":", 1)[1])
    elsewhere = f"http://localhost:{port}/steps.html"
    model = ScriptedModel([
        # The browser's tools are a group the agent loads when it needs them.
        {"tool": "ToolSearch", "args": {"group": "browser"}},
        {"tool": "BrowserSnapshot", "args": {}},
        {"tool": "BrowserNavigate", "args": {"url": elsewhere}},
        {"text": "I need your yes to open localhost."},
        {"tool": "BrowserNavigate", "args": {"url": elsewhere}},
        {"text": "Done."},
    ])
    # An agent's browser of its own is configured, unstarted: an installation registers the browser's
    # tools only when it has one, and this conversation never uses it.
    configured = settings.model_copy(update={"native": True, "browser_user_dir": daemon, "browser_container_dir": daemon.parent / "no-agent-browser",
                                             "api_port": api_port, "miniapp_public_url": ""})
    manager = SessionManager(configured, model_config(browser=BrowserConfig(user=BrowserUserConfig())), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config, preset_id=None: [(model, "scripted-model")]  # type: ignore[method-assign]
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    server = uvicorn.Server(uvicorn.Config(build_app(app, "tok"), host="127.0.0.1", port=api_port, log_level="warning", lifespan="off"))  # type: ignore[arg-type]
    serving = asyncio.create_task(server.serve())
    context, worker = chrome.context, chrome.worker
    try:
        await until(lambda: server.started, "the host's server")
        service = app.extensions["browser"]
        assert await service.wait_available(USER, timeout=20)
        native = await until(lambda: connected(service, "native"), "the relay through the bridge")
        # The credential, over the relay: what the host does on every connection.
        await browser_extension.relay_changed(app.extensions["browser_panel_herald"], "relay.connected", {"chrome": native["chrome"]})
        grant = await until(lambda: worker.evaluate("async () => (await chrome.storage.session.get('panelGrant')).panelGrant"), "the panel's credential in the extension")
        assert grant["base"] == f"http://127.0.0.1:{api_port}" and grant["token"].startswith("pnl_"), grant
        assert grant["token"] not in json.dumps(await worker.evaluate("async () => chrome.storage.local.get(null)"))

        # The operator's page, the active tab of their window, and the panel beside it.
        operator = context.pages[0]
        await operator.goto(f"{site}/shop/index.html")
        await operator.bring_to_front()
        opening = asyncio.ensure_future(context.wait_for_event("page"))
        await worker.evaluate(
            """async () => {
                const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
                await chrome.tabs.create({ windowId: tab.windowId, url: chrome.runtime.getURL("panel.html"), active: false });
            }"""
        )
        panel = await opening
        frame = panel.frame_locator("#frame")
        await frame.locator(".sp-new .composer textarea").wait_for(timeout=20000)
        assert "Fixture shop" in await frame.locator(".sp-new-tab").inner_text()
        assert "signed in as you" in await frame.locator(".sp-consent").inner_text()

        # A new conversation from the panel: the operator's words, and the tab in front of them.
        await frame.locator(".sp-new .composer textarea").fill("What does this shop sell?")
        await frame.locator(".sp-new .composer textarea").press("Enter")
        # The panel moves to the conversation (the model names it as it likes after the first turn).
        await frame.locator(".sp-body").wait_for(timeout=20000)
        sessions = await service.choices.using_user()
        assert len(sessions) == 1, sessions
        sid = sessions[0]
        # The agent read the operator's own tab: its snapshot is the shop's page.
        await until(lambda: "Find something" in model.results(), "the agent reading the operator's tab", timeout=40)
        in_group = await worker.evaluate("async (url) => (await chrome.tabs.query({})).filter((t) => t.url === url).map((t) => t.groupId)", f"{site}/shop/index.html")
        assert in_group and in_group[0] != -1, f"the operator's tab did not join the conversation's group: {in_group}"

        # Its next step asks for a new site; the card takes the focus and Enter allows it once.
        card = frame.locator(".sp-card")
        await card.wait_for(timeout=40000)
        assert "localhost" in (await card.inner_text())
        await card.press("Enter")
        await until(lambda: "repeat the call now" in model.results(), "the grant resuming the agent", timeout=40)
        await until(lambda: _granted(service, sid, "localhost"), "the site granted for the conversation", timeout=40)
        assert (await service.choices.get(sid))["chrome"] == native["chrome"], "a conversation started from a panel belongs to its Chrome"

        # A second Chrome on the same computer, its own panel and its own token: it stays connected
        # beside the first, and the first one's conversation does not exist for it.
        from playwright.async_api import async_playwright  # noqa: PLC0415

        register_bridge(scratch / "second", scratch, daemon)
        async with async_playwright() as p:
            second = await launched(p, scratch / "second")
            await until(lambda: _two_connected(service), "the second Chrome beside the first", timeout=30)
            theirs = await until(lambda: second.worker.evaluate("async () => (await chrome.storage.session.get('panelGrant')).panelGrant"), "the second Chrome's credential")
            assert theirs["token"] != grant["token"]
            await second.panel.frame_locator("#frame").locator(".sp-new .composer textarea").wait_for(timeout=20000)
            async with httpx.AsyncClient(base_url=theirs["base"]) as client:
                own = {"authorization": f"Panel {grant['token']}"}
                other = {"authorization": f"Panel {theirs['token']}"}
                assert (await client.get(f"/api/panel/sessions/{sid}", headers=own)).status_code == 200
                assert (await client.get(f"/api/panel/sessions/{sid}", headers=other)).status_code == 404
                assert sid in [s["id"] for s in (await client.get("/api/panel/sessions", headers=own)).json()["sessions"]]
                assert sid not in [s["id"] for s in (await client.get("/api/panel/sessions", headers=other)).json()["sessions"]]
            await second.context.close()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serving, 10)
        for task in tasks:
            task.cancel()
        await app.extensions["browser"].close()
        await manager.close()


async def _granted(service: Browsers, session_id: str, site: str) -> bool:
    return site in (await service.choices.get(session_id))["sites"]
