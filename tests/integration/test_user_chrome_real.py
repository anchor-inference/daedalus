"""The operator's own Chrome, end to end on one machine: the built extension loaded into a real
Chromium, Chrome starting the real ``browserd bridge`` as its native messaging host, the real user
daemon (``serve --env user --backend attach``) behind it, and the host's browser service driving it.

What a person would check by hand, in order:

- the relay connects, and both sides say so (the daemon's ``relay.status``, the popup's phase);
- the connection check runs both ways: the popup's check reaches the host as an event and comes back
  confirmed, and the host's check flashes in the popup;
- a conversation switched to the operator's Chrome opens its group there, reads a local page and
  clicks on it, while a tab the operator opened on the same site is never grouped, never debugged
  and never moved;
- cookie values never leave Chrome, and writing or clearing cookies is refused;
- the same again over a server's door: the host's relay WebSocket on loopback, paired with a one-time
  code typed into the popup.

Opt in with ``DAEDALUS_CHROME_E2E=1``, and give it ``DAEDALUS_BROWSERD_BIN`` (a built browserd) and
``CHROMIUM`` (an unbranded Chromium: branded Chrome ignores ``--load-extension`` since 137, so
Playwright's under ``~/.cache/ms-playwright`` is the one to use). Build the extension first
(``cd extension && npm ci && npm run build``). Everything stays on loopback. Run it under a memory cap::

    systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \\
      env DAEDALUS_CHROME_E2E=1 DAEDALUS_BROWSERD_BIN=... CHROMIUM=... \\
      uv run --frozen pytest tests/integration/test_user_chrome_real.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.browser.model import USER, BrowserError, Owner
from daedalus.browser.service import Browsers
from daedalus.browser.user import Pairing, RelayDesk
from daedalus.config import BrowserConfig
from daedalus.stores.database import Database

REPO = Path(__file__).resolve().parents[2]
BINARY = os.environ.get("DAEDALUS_BROWSERD_BIN", "")
CHROMIUM = os.environ.get("CHROMIUM", "")
EXTENSION = REPO / "extension" / "dist"
SITE = REPO / "browserd" / "internal" / "rpc" / "testdata" / "site"
# The id and the host name as the extension declares them, so this run cannot drift from it.
PROTOCOL = (REPO / "extension" / "src" / "protocol.ts").read_text()
EXTENSION_ID = re.search(r'EXTENSION_ID = "([a-p]{32})"', PROTOCOL).group(1)  # type: ignore[union-attr]
HOST_NAME = re.search(r'NATIVE_HOST = "([a-z0-9_.]+)"', PROTOCOL).group(1)  # type: ignore[union-attr]
SECRET = "s3cr3t-cookie-value"

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_CHROME_E2E") != "1", reason="set DAEDALUS_CHROME_E2E=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_BROWSERD_BIN to a built browserd"),
    pytest.mark.skipif(not CHROMIUM or not Path(CHROMIUM).is_file(), reason="set CHROMIUM to an unbranded Chromium"),
    pytest.mark.skipif(not (EXTENSION / "manifest.json").is_file(), reason="build the extension: cd extension && npm run build"),
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


@pytest.fixture
async def chrome(scratch: Path, daemon: Path) -> AsyncIterator[Any]:
    """Chromium with the built extension, and the real bridge registered as its native messaging host
    in the profile's own NativeMessagingHosts folder (where Chromium looks for a user-level host when
    it runs with --user-data-dir), exactly as the launcher's wrapper starts it."""
    from playwright.async_api import async_playwright  # noqa: PLC0415 — only this opt-in run needs it

    profile = scratch / "chrome"
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


async def relay(service: Browsers) -> dict[str, Any]:
    status = await service.user_status()
    return status.get("relay") or {}


async def connected(service: Browsers, transport: str) -> dict[str, Any] | None:
    found = await relay(service)
    return found if found.get("connected") and found.get("transport") == transport else None


async def check_both_ways(service: Browsers, bus: Bus, popup: Any) -> None:
    # The popup's check: it reaches the host as an event, and the popup shows it confirmed.
    before = len(bus.user("check"))
    await popup.click("#check")
    await popup.wait_for_selector("#check-result[data-ok='true']", timeout=8000)
    assert "✓" in await popup.inner_text("#check-result")
    await until(lambda: len(bus.user("check")) > before, "the popup's check reaching the host")
    # The host's check: relay.notify answers with a round trip and flashes in the popup.
    checked = await service.check_user()
    assert checked["ok"] and checked["rtt_ms"] >= 0
    await popup.wait_for_selector("#notice:not([hidden])", timeout=8000)


async def drive(service: Browsers, owner: Owner, site: str) -> tuple[str, str]:
    """Open the conversation's group in the operator's Chrome on the shop, read it, click a link."""
    opened = await service.open(owner, url=f"{site}/shop/index.html", actor=f"agent:{owner.id}")
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
    popup = context.pages[0]
    await popup.goto(f"chrome-extension://{EXTENSION_ID}/popup.html")
    await service.start()
    server_task: asyncio.Task[None] | None = None
    uvicorn_server: Any = None
    try:
        assert await service.wait_available(USER, timeout=20)

        # Local: through native messaging and the real bridge.
        native = await until(lambda: connected(service, "native"), "the relay through the bridge")
        assert native["profile_name"] == "Chrome" and native["ext_version"], native
        await popup.wait_for_selector("#phase[data-phase='connected']", timeout=10000)
        assert await popup.inner_text("#install") == "E2E"
        await check_both_ways(service, bus, popup)

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
        await popup.fill("#server", f"http://127.0.0.1:{server_port}")
        await popup.fill("#code", code)
        await popup.click("#pair")
        served = await until(lambda: connected(service, "server"), "the relay through the host's WebSocket", timeout=30)
        assert served["profile_name"] == "Chrome", served
        assert desk.connected_device(), "the desk holds no relay"
        await popup.wait_for_selector("#phase[data-phase='connected']", timeout=10000)
        await until(lambda: popup.inner_text("#install"), "the popup to name the installation")
        assert await popup.inner_text("#install") == "E2E server"
        await check_both_ways(service, bus, popup)
        # The relay through the bridge ended when the extension moved to the server, and the group
        # with it; the conversation opens again in the same Chrome.
        last: BrowserError | None = None
        for _ in range(60):
            try:
                await drive(service, owner, site)
                break
            except BrowserError as exc:
                # The group of the bridge's relay may still be closing as the server's relay arrives.
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
