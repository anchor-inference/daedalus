"""Command-line staff and the browser: every adapter puts the browser's tool set into its launch, and
the runtime answers the calls ``ptyd tools-mcp`` posts with the same tools a Daedalus session runs —
the policy, the credential wall and a sensitive action held as the operator's question until it is
answered, never the orchestrator's; the network wall's question about a LAN address goes to the
orchestrator, as the member's other permissions do."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from daedalus.browser.agent import BrowserAgent
from daedalus.browser.cli import SERVER, TOOL_SET, StaffBrowser, tools_file
from daedalus.browser.owners import DatabaseOwners
from daedalus.browser.service import Browsers
from daedalus.config import BrowserConfig, Settings
from daedalus.extensions.staff import StaffError
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.contract import LaunchSpec
from daedalus.harness.grok import AGENT_FILE, GrokAdapter
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.pi import PiAdapter
from daedalus.stores.database import Database
from daedalus.tools.browser import BROWSER_TOOLS, READ_ONLY_TOOLS
from tests.support.fake_browserd import FakeBrowserd
from tests.support.fake_cli.fake_codex import parse_overrides
from tests.support.fake_cli.fake_grok import front_matter
from tests.unit.test_browser_tools import shop
from tests.unit.test_cli_staff_runtime import Stand, stand, trust


def browser_set(hold_ms: int = 330_000) -> Any:
    return StaffBrowser.spec(StaffBrowser.__new__(StaffBrowser), hold_ms)


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "claude", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\n---\n", "tool_sets": (browser_set(),),
    }
    values.update(changes)
    return LaunchSpec(**values)


def test_the_launch_file_is_the_native_tools_word_for_word() -> None:
    file = json.loads(tools_file(330_000))
    assert file["server"] == SERVER and file["hold_ms"] == 330_000 and "not instructions" in file["instructions"]
    assert [t["name"] for t in file["tools"]] == list(BROWSER_TOOLS)
    act = next(t for t in file["tools"] if t["name"] == "BrowserAct")
    # One action or steps of several: neither is required by the schema, and the agent says which is missing.
    assert act["inputSchema"]["required"] == [] and "element is required" in act["description"]
    steps = act["inputSchema"]["properties"]["steps"]["anyOf"][0]
    assert steps["type"] == "array" and steps["items"]["type"] == "object" and "steps=[" in act["description"]
    text = next(t for t in file["tools"] if t["name"] == "BrowserText")
    assert {"find", "regex", "query", "schema"} <= set(text["inputSchema"]["properties"])
    assert {"x", "y"} <= set(act["inputSchema"]["properties"])
    assert set(READ_ONLY_TOOLS) < set(BROWSER_TOOLS) and "BrowserAct" not in READ_ONLY_TOOLS


def test_every_adapter_offers_the_set_and_lets_only_its_reads_through_unasked(tmp_path: Path) -> None:
    command = 'exec "$DAEDALUS_PTYD_BIN" tools-mcp --set browser'
    reads = [f"mcp__daedalus_browser__{name}" for name in READ_ONLY_TOOLS]

    claude = ClaudeCodeAdapter().launch_plan(spec())
    mcp = json.loads(claude.files["mcp.json"])["mcpServers"]
    assert mcp["daedalus_browser"] == {"command": "sh", "args": ["-c", command], "env": {"DAEDALUS_TOOLS_HOLD_MS": "330000"}}
    allow = json.loads(claude.files["settings.json"])["permissions"]["allow"]
    assert set(reads) <= set(allow) and "mcp__daedalus_browser__BrowserAct" not in allow
    assert json.loads(claude.files["tools/browser.json"])["server"] == SERVER
    # Claude's timeout on a tool call is above the hold, or a purchase awaiting the operator is cut off.
    assert int(claude.env["MCP_TOOL_TIMEOUT"]) > 330_000

    codex = CodexAdapter().launch_plan(spec(harness="codex"))
    [server] = codex.companions
    config = parse_overrides([server.argv[i + 1] for i, word in enumerate(server.argv) if word == "-c"])
    entry = config["mcp_servers"]["daedalus_browser"]
    assert entry["args"] == ["-c", command] and "DAEDALUS_LAUNCH_DIR" in entry["env_vars"] and entry["tool_timeout_sec"] > 330
    assert codex.files["tools/browser.json"]

    opencode = OpenCodeAdapter().launch_plan(spec(harness="opencode"))
    local = json.loads(opencode.env["OPENCODE_CONFIG_CONTENT"])["mcp"]["daedalus_browser"]
    assert local["command"] == ["sh", "-c", command] and local["timeout"] > 330_000 and opencode.files["tools/browser.json"]

    grok = GrokAdapter().launch_plan(spec(harness="grok"))
    path = tmp_path / AGENT_FILE
    path.write_bytes(grok.files[AGENT_FILE])
    meta, _ = front_matter(path)
    assert [s["name"] for s in meta["mcpServers"]] == ["daedalus_team", "daedalus_browser"]
    argv = list(grok.argv)
    allowed = [argv[i + 1] for i, word in enumerate(argv) if word == "--allow"]
    assert allowed == ["MCPTool(daedalus_team__*)", *(f"MCPTool(daedalus_browser__{n})" for n in READ_ONLY_TOOLS)]

    pi = PiAdapter().launch_plan(spec(harness="pi"))
    assert pi.env["DAEDALUS_TOOL_SETS"] == "browser" and pi.env["DAEDALUS_TOOLS_HOLD_MS"] == "330000" and pi.files["tools/browser.json"]

    # A launch with no browser offers none.
    bare = ClaudeCodeAdapter().launch_plan(spec(tool_sets=()))
    assert "daedalus_browser" not in json.loads(bare.files["mcp.json"])["mcpServers"] and "tools/browser.json" not in bare.files


@pytest.fixture
def browser_dir() -> Any:
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class Browsing:
    def __init__(self, s: Stand, daemon: FakeBrowserd, service: Browsers) -> None:
        self.s = s
        self.daemon = daemon
        self.service = service

    def launch(self) -> Any:
        [launch] = [launch for launch in self.s.ptyd.launches.values()]
        return launch

    async def call(self, tool: str, arguments: dict[str, Any], *, wait_ms: int = 20_000) -> tuple[int, dict[str, Any] | None]:
        """What ``ptyd tools-mcp`` posts for one call, and the host's answer to it."""
        launch = self.launch()
        url = f"http://127.0.0.1:{self.s.ptyd.hook_port}/hook/{launch.launch_id}/tools?wait_ms={wait_ms}"
        body = {"set": TOOL_SET, "tool": tool, "arguments": arguments, "call_id": f"{launch.launch_id}:0000abcd:{tool}{len(launch.posts)}"}
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(url, json=body, headers={"Authorization": f"Bearer {launch.token}"})
        return response.status_code, (response.json() if response.content else None)


@pytest.fixture
async def browsing(settings: Settings, db: Database, browser_dir: Path) -> AsyncIterator[Browsing]:
    daemon = await FakeBrowserd(browser_dir / "b").start()
    shop(daemon)
    async with stand(settings, db) as s:
        service = Browsers(db, run_dirs={"container": browser_dir / "b", "host": None}, config=lambda: BrowserConfig(control_wait_seconds=0.2), owners=DatabaseOwners(db), bus=s.manager.bus)
        await service.start()
        assert await service.wait_available("container")
        s.runtime.tool_sets[TOOL_SET] = StaffBrowser(BrowserAgent(service), team=lambda: s.team, manager=s.manager)
        trust(s)
        try:
            yield Browsing(s, daemon, service)
        finally:
            await service.close()
            await daemon.stop()


async def _started(b: Browsing) -> Any:
    ada = await b.s.hire()
    await b.s.assign(ada, await b.s.task())
    for _ in range(600):
        if b.s.ptyd.launches:
            break
        await asyncio.sleep(0.05)
    return ada


async def test_a_cli_member_browses_through_the_host_and_its_purchase_waits_for_the_operator(browsing: Browsing) -> None:
    b = browsing
    ada = await _started(b)
    # The launch may hold a question to the operator past the set's own hold.
    assert b.launch().hold_max_ms >= 330_000

    status, reply = await b.call("BrowserOpen", {"url": "https://shop.test/cart"})
    assert status == 200 and reply is not None and not reply["error"] and "Browser opened" in reply["text"]
    group = f"m-{ada.id}"
    assert b.daemon.groups[group].labels["owner_kind"] == "staff" and b.daemon.groups[group].profile == f"project-{b.s.project.id}"
    status, reply = await b.call("BrowserSnapshot", {})
    assert reply is not None and "[page content from https://shop.test;" in reply["text"] and 'button "Buy now" [ref=e20]' in reply["text"]

    # Where the browser may go is the host's policy, as for a session.
    _, reply = await b.call("BrowserNavigate", {"url": "file:///etc/passwd"})
    assert reply is not None and reply["error"] and "rule browser.scheme" in reply["text"]

    # The purchase is held as a question for the operator; the orchestrator cannot answer it.
    buying = asyncio.create_task(b.call("BrowserAct", {"action": "click", "ref": "e20", "element": "the Buy now button"}))
    ask = None
    for _ in range(400):
        asks = [a for a in await b.s.manager.asks.open_for(b.s.project.id) if a.kind == "permission"]
        if asks:
            ask = asks[0]
            break
        await asyncio.sleep(0.05)
    assert ask is not None and ask.routed_to == "operator" and ask.request_ref.startswith("tools:")
    assert "Buy now" in ask.text and not buying.done()
    pending = [e for e in await b.s.events("permission.pending", staff_id=ada.id)]
    assert pending[-1].payload["risk"] == "elevated" and pending[-1].payload["quick"] is False
    with pytest.raises(StaffError, match="operator's to answer"):
        await b.s.team.answer(ask.short_id, allow=True, by="orchestrator", basis="it is only a test purchase")
    await b.s.team.answer(ask.short_id, allow=True, by="operator")
    status, reply = await asyncio.wait_for(buying, 30)
    assert reply is not None and not reply["error"] and "Done: click" in reply["text"]

    # Refused, it is refused in words the member acts on.
    refusing = asyncio.create_task(b.call("BrowserAct", {"action": "click", "ref": "e20", "element": "the Buy now button"}))
    for _ in range(400):
        asks = [a for a in await b.s.manager.asks.open_for(b.s.project.id) if a.kind == "permission"]
        if asks:
            break
        await asyncio.sleep(0.05)
    await b.s.team.answer(asks[0].short_id, allow=False, by="operator")
    _, reply = await asyncio.wait_for(refusing, 30)
    assert reply is not None and reply["error"] and "operator refused" in reply["text"]

    # A replayed call (the same call id) is answered as the first was, and run once.
    before = len([e for e in b.daemon.events if e["type"] == "action"])
    launch = b.launch()
    body = {"set": TOOL_SET, "tool": "BrowserAct", "arguments": {"action": "click", "ref": "e22", "element": "remove"}, "call_id": "same-call"}
    url = f"http://127.0.0.1:{b.s.ptyd.hook_port}/hook/{launch.launch_id}/tools?wait_ms=500"
    async with httpx.AsyncClient(timeout=30) as http:
        first = await http.post(url, json=body, headers={"Authorization": f"Bearer {launch.token}"})
        second = await http.post(url, json=body, headers={"Authorization": f"Bearer {launch.token}"})
    assert first.status_code in (200, 204) and second.status_code in (200, 204)
    await asyncio.sleep(0.2)
    assert len([e for e in b.daemon.events if e["type"] == "action"]) == before


async def test_a_secret_field_refuses_a_cli_member_too(browsing: Browsing) -> None:
    b = browsing
    await _started(b)
    await b.call("BrowserOpen", {"url": "https://login.test/"})
    _, reply = await b.call("BrowserAct", {"action": "type", "ref": "e31", "element": "the password", "text": "hunter2"})
    assert reply is not None and reply["error"] and "BrowserHandoff" in reply["text"]
    _, reply = await b.call("BrowserEvaluate", {"script": "document.cookie"})
    assert reply is not None and reply["error"] and "no browser tool" in reply["text"]


async def _open_ask(b: Browsing) -> Any:
    for _ in range(400):
        asks = [a for a in await b.s.manager.asks.open_for(b.s.project.id) if a.kind == "permission"]
        if asks:
            return asks[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no permission request was opened")


async def test_a_cli_members_request_for_a_lan_address_goes_to_its_orchestrator(browsing: Browsing) -> None:
    """The router's page a Codex member could not open: the wall asks, the request goes to the
    orchestrator with the address, the member and its reason; a grant the operator's words back reaches
    the daemon, and the call goes through."""
    b = browsing
    ada = await _started(b)
    await b.call("BrowserOpen", {})
    group = f"m-{ada.id}"
    b.daemon.walled["10.20.0.1"] = ("ask", "private")
    opening = asyncio.create_task(b.call("BrowserNavigate", {"url": "http://10.20.0.1/", "why": "read the router's DHCP leases"}))
    ask = await _open_ask(b)
    assert ask.routed_to == "orchestrator" and ask.request_ref.startswith("tools:") and ask.staff_id == ada.id
    assert "http://10.20.0.1:80" in ask.text and "read the router's DHCP leases" in ask.text and "local network" in ask.text
    assert not opening.done()
    # A reason of its own is not enough where the autonomy asks for the operator's words.
    with pytest.raises(StaffError, match="basis"):
        await b.s.team.answer(ask.short_id, allow=True, by="orchestrator", basis="the member needs it")
    await b.s.manager.projects.set_brief(b.s.project.id, "allowed_without_operator", "open the router's admin page at 10.20.0.1", "operator")
    await b.s.team.answer(ask.short_id, allow=True, by="orchestrator", basis="open the router's admin page at 10.20.0.1")
    _, reply = await asyncio.wait_for(opening, 30)
    assert reply is not None and not reply["error"] and "10.20.0.1" in reply["text"]
    assert (b.daemon.groups[group].browser_id, "10.20.0.1", 80) in b.daemon.grants

    # Escalated, it is the operator's; refused, the member is told not to reach it another way.
    b.daemon.walled["10.0.0.7"] = ("ask", "private")
    refusing = asyncio.create_task(b.call("BrowserNavigate", {"url": "http://10.0.0.7:8080/"}))
    ask = await _open_ask(b)
    assert ask.routed_to == "orchestrator"
    assert await b.s.team.escalate(ask, why="the operator has not said this device may be opened")
    ask = await b.s.manager.asks.get(ask.id)
    assert ask is not None and ask.routed_to == "operator"
    await b.s.team.answer(ask.short_id, allow=False, by="operator")
    _, reply = await asyncio.wait_for(refusing, 30)
    assert reply is not None and reply["error"] and "was refused" in reply["text"] and "another way" in reply["text"]
    assert not any(host == "10.0.0.7" for _, host, _ in b.daemon.grants)

    # What the wall denies is never a question: the metadata service is refused outright.
    b.daemon.walled["169.254.169.254"] = ("deny", "metadata")
    _, reply = await b.call("BrowserNavigate", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert reply is not None and reply["error"] and "network wall refused 169.254.169.254" in reply["text"]
    assert not [a for a in await b.s.manager.asks.open_for(b.s.project.id) if a.kind == "permission"]


async def test_a_cli_members_lan_request_left_open_says_where_it_waits(browsing: Browsing) -> None:
    """The hold gives up before the orchestrator answers: the member reads that the request is with its
    orchestrator, and a later grant is kept for the same call made again."""
    b = browsing
    ada = await _started(b)
    await b.call("BrowserOpen", {})
    held = b.s.runtime.config().model_copy(update={"permission_hold_s": 0})
    b.s.runtime.config = lambda: held
    b.daemon.walled["10.20.0.1"] = ("ask", "private")
    _, reply = await b.call("BrowserNavigate", {"url": "http://10.20.0.1/"})
    assert reply is not None and reply["error"] and "your orchestrator" in reply["text"] and "make the same call again" in reply["text"]
    ask = await _open_ask(b)
    await b.s.manager.projects.update_orchestrator(b.s.project.id, enabled=True, autonomy="full")
    await b.s.team.answer(ask.short_id, allow=True, by="orchestrator", basis="the operator asked for the router's page")
    _, reply = await b.call("BrowserNavigate", {"url": "http://10.20.0.1/"})
    assert reply is not None and not reply["error"]
    assert (b.daemon.groups[f"m-{ada.id}"].browser_id, "10.20.0.1", 80) in b.daemon.grants
