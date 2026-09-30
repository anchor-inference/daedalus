"""The browser service against the real browser daemon and a real Chromium: a group opened on a local
page, its outline read, a button clicked by ref, and a live view through the host's WebSocket relay
that draws frames and shows the agent's action.

Needs ``DAEDALUS_INTEGRATION=1 DAEDALUS_BROWSERD_BIN=<path to browserd>`` and a Chromium the daemon
finds (``BROWSERD_CHROMIUM``, or Playwright's under ``PLAYWRIGHT_BROWSERS_PATH``). Run it under a
memory cap: ``systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0``. The page is served on
this machine's loopback interface, so a daemon whose network wall is configured to refuse loopback
must be given the fixture's port in its services range.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import shutil
import struct
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from daedalus.browser import wire
from daedalus.browser.agent import BrowserAgent, Caller, SensitiveAsk
from daedalus.browser.model import Owner
from daedalus.browser.service import Browsers
from daedalus.config import BrowserConfig
from daedalus.stores.database import Database

BINARY = os.environ.get("DAEDALUS_BROWSERD_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_BROWSERD_BIN to a built browserd"),
]

PAGE = b"""<!doctype html><html><head><title>Fixture</title></head><body>
<h1>Fixture shop</h1>
<button id="add" onclick="document.getElementById('out').textContent='added'">Add to cart</button>
<p id="out">empty</p>
</body></html>"""


class Everyone:
    async def exists(self, owner: Owner) -> bool:
        return True

    async def label(self, owner: Owner) -> str:
        return "tester"


@pytest.fixture
def site() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the standard library's name
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture
async def daemon() -> AsyncIterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="bd-"))
    run, state = base / "run", base / "state"
    proc = await asyncio.create_subprocess_exec(BINARY, "serve", "--env", "container", "--run-dir", str(run), "--state-dir", str(state), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(20):
            while not (run / "endpoint").is_file():
                await asyncio.sleep(0.05)
        yield run
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 10)
        shutil.rmtree(base, ignore_errors=True)


async def test_open_read_click_and_watch_through_the_host(db: Database, daemon: Path, site: str) -> None:
    port = int(site.rsplit(":", 1)[1].strip("/"))
    # The fixture is on this machine's loopback: the wall lets it through as one of the services,
    # and asks about any other port of this machine, as it does natively.
    rules = {"sealed_ports": [], "services_ports": [[port, port]], "local_sites": "ask", "lan_allow": []}
    service = Browsers(db, run_dirs={"container": daemon, "host": None}, config=lambda: BrowserConfig(), owners=Everyone(), wall=lambda env: rules)  # type: ignore[arg-type]
    await service.start()
    try:
        assert await service.wait_available("container", timeout=20)
        owner = Owner("session", "real1", session_id="real1")
        opened = await service.open(owner, url=site, actor="agent:real1")
        group = opened["group"]["id"]
        tab = opened["tab"]["id"]
        snapshot = await service.call(group, "page.snapshot", {"tab_id": tab}, what="reading", timeout=30)
        assert 'heading "Fixture shop"' in snapshot["text"]
        ref = next(line.split("[ref=")[1].split("]")[0] for line in snapshot["text"].splitlines() if 'button "Add to cart"' in line)

        attachment = await service.attach(group, read_only=False, label="test", via="token")
        await attachment.channel.send(bytes([wire.ATTACH]) + json.dumps({"tier": "live", "tab": tab, "max_w": 1280, "max_h": 800}).encode())
        acted = await service.call(group, "page.act", {"tab_id": tab, "action": "click", "ref": ref, "element": "the Add to cart button"}, what="clicking", timeout=30)
        assert acted["ok"]
        frames, action = 0, None
        async with asyncio.timeout(20):
            while frames == 0 or action is None:
                payload = await attachment.channel.recv()
                assert payload is not None, "the view closed"
                if payload[0] == wire.FRAME:
                    frame_no = struct.unpack(">I", payload[1:5])[0]
                    frames += 1
                    await attachment.channel.send(bytes([wire.ACK]) + struct.pack(">I", frame_no))
                elif payload[0] == wire.EVENT:
                    event = json.loads(payload[1:])
                    if event.get("type") == "action":
                        action = event
        assert action is not None and action.get("kind") == "click"
        text = await service.call(group, "page.text", {"tab_id": tab}, what="reading", timeout=30)
        assert "added" in text["text"]
        await attachment.channel.close()

        # The agent's own path over the same browser: the outline fenced, a click by ref.
        agent = BrowserAgent(service)

        async def gate(ask: SensitiveAsk) -> tuple[bool, str]:
            return False, "asked"

        caller = Caller(owner=owner, actor="agent:real1", gate=gate, files=None)  # type: ignore[arg-type]
        text, failed = await agent.run("BrowserSnapshot", {}, caller)
        assert not failed and "[page content from http://127.0.0.1:" in text and 'heading "Fixture shop"' in text
        text, failed = await agent.run("BrowserAct", {"action": "click", "ref": ref, "element": "the Add to cart button"}, caller)
        assert not failed and "Done: click" in text, text
        # Another port of this machine: the wall asks; the gate here says no, so nothing is loaded.
        text, failed = await agent.run("BrowserNavigate", {"url": f"http://127.0.0.1:{port + 1}/"}, caller)
        assert failed and text == "asked", text
        await service.close_group(group, actor="agent:real1")
    finally:
        await service.close()
