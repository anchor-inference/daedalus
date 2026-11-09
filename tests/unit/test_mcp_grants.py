"""Adding an MCP server in one step, and "Allow all from <server>" for a span of one session."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.runtime.tool_registry import ToolRegistry

from daedalus.config import McpServerConfig, RuntimeConfig, Settings
from daedalus.extensions import mcp_admin
from daedalus.host.guest_turns import GuestTurn
from daedalus.host.policy import Policy, server_grant, similar_grant
from daedalus.host.session_runner import SessionManager
from daedalus.mcp import manager as mcp_manager
from daedalus.mcp.manager import McpConnection, McpManager, mcp_tool_name, resolve_command
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import server_grant_rows

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"


def remote(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=name, input_schema={"type": "object", "properties": {}})


def catalogue(manager: McpManager, server: str, *tools: str, config: McpServerConfig | None = None) -> None:
    """A server's tools as if it had connected: no process, the same proxies and approvals."""
    connection = McpConnection(server, config or McpServerConfig(transport="stdio", command="x"))
    manager._replace_catalog(server, [connection._proxy(remote(tool)) for tool in tools])


# -- the policy -------------------------------------------------------------------------------------------


def test_a_server_or_a_tool_set_to_deny_is_refused_without_a_question() -> None:
    registry = ToolRegistry()
    manager = McpManager({}, registry)
    catalogue(manager, "phone", "tap", "look", config=McpServerConfig(transport="stdio", command="x", approval="deny", tool_approval={"look": "allow"}))
    policy = Policy(mcp_approvals=manager.approvals(), mcp_servers=manager.servers_by_tool())
    refused = policy.evaluate(mcp_tool_name("phone", "tap"), {})
    assert refused.action == "deny" and refused.rule == "mcp.denied"
    # A tool allowed by name stays usable on a server that is otherwise refused.
    assert policy.evaluate(mcp_tool_name("phone", "look"), {}).action == "allow"
    # An operator's allow rule never lowers a refusal.
    from daedalus.host.policy import Rule  # noqa: PLC0415

    loose = Policy(mcp_approvals=manager.approvals(), rules=[Rule(id="mine", tool="*", action="allow", note="", pattern=".")])
    assert loose.evaluate(mcp_tool_name("phone", "tap"), {}).action == "deny"


def test_a_server_grant_covers_that_servers_tools_and_no_others_and_ends_on_time() -> None:
    registry = ToolRegistry()
    manager = McpManager({}, registry)
    catalogue(manager, "mobile-mcp", "tap", "swipe")
    catalogue(manager, "notes", "append")
    policy = Policy(mcp_approvals=manager.approvals(), mcp_servers=manager.servers_by_tool())
    tap, swipe, append = (mcp_tool_name("mobile-mcp", "tap"), mcp_tool_name("mobile-mcp", "swipe"), mcp_tool_name("notes", "append"))
    assert policy.evaluate(tap, {}).action == "ask"
    grant = server_grant("mobile-mcp", seconds=3600)
    assert policy.evaluate(tap, {}, similar=[grant]).action == "allow"
    assert policy.evaluate(swipe, {}, similar=[grant]).action == "allow"
    assert policy.evaluate(append, {}, similar=[grant]).action == "ask", "another server keeps asking"
    ended = server_grant("mobile-mcp", seconds=3600, now=0)
    assert policy.evaluate(tap, {}, similar=[ended]).action == "ask", "a grant whose span is over answers nothing"
    forever = server_grant("mobile-mcp", seconds=0, now=0)
    assert forever["until"] == 0 and policy.evaluate(tap, {}, similar=[forever]).action == "allow"


def test_a_server_grant_does_not_answer_a_question_about_a_path() -> None:
    """The operator allowed the server's tools, not whatever their arguments reach."""
    registry = ToolRegistry()
    manager = McpManager({}, registry)
    catalogue(manager, "files", "write")
    policy = Policy(mcp_approvals=manager.approvals(), mcp_servers=manager.servers_by_tool(), home_dir="/home/someone", native=True)
    tool = mcp_tool_name("files", "write")
    asked = policy.evaluate(tool, {"path": "/home/someone/.ssh/config"})
    assert asked.action in ("ask", "deny") and asked.rule != "mcp.annotations"
    again = policy.evaluate(tool, {"path": "/home/someone/.ssh/config"}, similar=[server_grant("files", seconds=3600)])
    assert again.action == asked.action


def test_the_mcp_family_names_its_server() -> None:
    family = similar_grant("Mcp_Notes_append", {}, "mcp.annotations", server="notes")
    assert family is not None and family["server"] == "notes" and family["kind"] == "tool"


# -- the session --------------------------------------------------------------------------------------------


@pytest.fixture
async def session(settings: Settings, db: Database) -> Any:
    config = RuntimeConfig()
    config.mcp.servers = {name: McpServerConfig(transport="stdio", command="x") for name in ("mobile-mcp", "notes")}
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    catalogue(manager.mcp, "mobile-mcp", "tap", "swipe")
    catalogue(manager.mcp, "notes", "append")
    state = await manager.create_session("t")
    state.metadata["mcp_enabled"] = ["mobile-mcp", "notes"]
    yield manager, state
    await manager.close()


async def test_allow_all_from_a_server_runs_its_tools_unasked_until_the_span_ends(session: Any) -> None:
    manager, state = session
    sid = state.session.id
    gate = manager.policy_gate(sid, "r").decide
    tap, swipe, append = (mcp_tool_name("mobile-mcp", "tap"), mcp_tool_name("mobile-mcp", "swipe"), mcp_tool_name("notes", "append"))
    asked = gate(tap, {"x": 1})
    assert asked.action == "ask" and state.metadata["policy_pending"][asked.key]["server"] == "mobile-mcp"
    assert manager.server_of(sid, asked.key) == "mobile-mcp"
    other = gate(append, {})
    # The acceptance's six hours, shortened to most of a second.
    result = await manager.grant_server(sid, asked.key, seconds=0.6, via="app")
    assert result["server"] == "mobile-mcp" and result["resolved"] == [asked.key]
    assert other.key in state.metadata["policy_pending"], "the other server's request stays open"
    assert gate(tap, {"x": 2}).action == "allow" and gate(swipe, {}).action == "allow"
    assert gate(tap, {"x": 2}).action == "allow", "not spent by a call"
    assert gate(append, {}).action == "ask", "other servers keep asking"
    [standing] = manager.standing(state)
    assert standing["server"] == "mobile-mcp" and 0 <= standing["left_seconds"] <= 1
    stored = await manager.sessions.get(sid, "daedalus")
    assert stored.metadata["policy_similar"][-1]["kind"] == "server", "it outlives a restart of the host"
    await asyncio.sleep(0.7)
    assert gate(tap, {"x": 3}).action == "ask", "asked again once the span is over"
    assert manager.standing(state) == []


async def test_a_grant_is_taken_back_from_the_sessions_settings(session: Any) -> None:
    manager, state = session
    sid = state.session.id
    gate = manager.policy_gate(sid, "r").decide
    tap = mcp_tool_name("mobile-mcp", "tap")
    await manager.grant_server(sid, gate(tap, {}).key, hours=0, via="app")
    [standing] = manager.standing(state)
    assert standing["until"] is None and standing["left_seconds"] is None, "forever has no end to count down"
    assert gate(tap, {}).action == "allow"
    assert await manager.revoke_standing(sid, standing["id"], via="app") == []
    assert gate(tap, {}).action == "ask"
    with pytest.raises(KeyError):
        await manager.revoke_standing(sid, standing["id"], via="app")


async def test_only_the_offered_spans_are_granted_and_only_for_an_mcp_question(session: Any) -> None:
    manager, state = session
    sid = state.session.id
    gate = manager.policy_gate(sid, "r").decide
    key = gate(mcp_tool_name("mobile-mcp", "tap"), {}).key
    with pytest.raises(ValueError, match="span"):
        await manager.grant_server(sid, key, hours=48, via="app")
    shell = gate("Exec", {"command": "curl https://example.com/x"})
    if shell.action == "ask":
        assert manager.server_of(sid, shell.key) is None
        with pytest.raises(ValueError, match="not an MCP server"):
            await manager.grant_server(sid, shell.key, via="app")


async def test_a_guests_turn_uses_no_grant_and_cannot_make_one(session: Any) -> None:
    manager, state = session
    sid = state.session.id
    gate = manager.policy_gate(sid, "r").decide
    tap = mcp_tool_name("mobile-mcp", "tap")
    key = gate(tap, {}).key
    await manager.grant_server(sid, key, hours=6, via="app")
    assert gate(tap, {}).action == "allow"
    state.guest_turn = GuestTurn("link1", "a friend's agent", "all", None, "tap it")
    try:
        assert gate(tap, {}).action == "deny", "a guest neither spends nor inherits what the operator allowed"
        with pytest.raises(ValueError, match="guest"):
            await manager.grant_server(sid, key, via="app")
    finally:
        state.guest_turn = None
    assert gate(tap, {}).action == "allow"


# -- the one-step add -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("pasted", "name", "spec"), [
    ("npx -y @mobilenext/mobile-mcp@latest", "mobile-mcp", {"command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"], "env": {}}),
    ("API_KEY=k1 uvx mcp-server-git==1.2 --repository .", "mcp-server-git", {"command": "uvx", "args": ["mcp-server-git==1.2", "--repository", "."], "env": {"API_KEY": "k1"}}),
    ("python3 -m notes_server", "notes_server", {"command": "python3", "args": ["-m", "notes_server"], "env": {}}),
])
def test_a_pasted_command_becomes_a_named_stdio_server(pasted: str, name: str, spec: dict[str, Any]) -> None:
    got_name, got = mcp_admin.parse_pasted(pasted)
    assert got_name == name and got == {"transport": "stdio", **spec}


def test_an_address_and_a_readme_block_are_read_too() -> None:
    assert mcp_admin.parse_pasted("https://mcp.linear.app/mcp") == ("linear", {"transport": "http", "url": "https://mcp.linear.app/mcp"})
    block = json.dumps({"mcpServers": {"mobile-mcp": {"command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"]}}})
    assert mcp_admin.parse_pasted(block) == ("mobile-mcp", {"transport": "stdio", "command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"], "env": {}})
    assert mcp_admin.parse_pasted("npx -y @x/notes", taken={"notes"})[0] == "notes-2"
    with pytest.raises(mcp_admin.McpAdminError):
        mcp_admin.parse_pasted("  ")


def test_uvx_runs_as_uv_tool_run_where_only_uv_is_installed(tmp_path: Path) -> None:
    (tmp_path / "uv").write_text("#!/bin/sh\n")
    (tmp_path / "uv").chmod(0o755)
    assert resolve_command("uvx", ["mcp-server-git"], {"PATH": str(tmp_path)}) == ("uv", ["tool", "run", "mcp-server-git"])
    with pytest.raises(FileNotFoundError, match="not installed where MCP servers start"):
        resolve_command("adb-mcp", [], {"PATH": str(tmp_path)})


async def test_a_server_that_fails_to_start_says_why_in_its_own_words() -> None:
    config = McpServerConfig(transport="stdio", command=sys.executable,
                             args=["-c", "import sys; sys.stderr.write('adb: no devices/emulators found\\n'); sys.exit(3)"])
    manager = McpManager({"phone": config}, ToolRegistry())
    with pytest.raises(RuntimeError):
        await manager.ensure("phone", deadline=20)
    [row] = manager.status()
    assert row["state"] == "unavailable" and "adb: no devices/emulators found" in row["error"]
    assert "adb: no devices/emulators found" in row["stderr"]
    await manager.close()


async def test_a_trial_lists_the_tools_and_keeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = McpManager({}, ToolRegistry())
    monkeypatch.setattr(mcp_manager, "WHERE", ["container"])
    tried = await manager.trial("echo", McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)]), deadline=30)
    assert tried["state"] == "ready" and sorted(tool["name"] for tool in tried["tools"]) == ["add", "echo"]
    assert tried["runs_in"] == "container"
    assert manager.all_tool_names() == set() and manager.available() == []
    broken = await manager.trial("gone", McpServerConfig(transport="stdio", command="no-such-mcp-command"), deadline=10)
    assert broken["state"] == "unavailable" and "not installed where MCP servers start (the Daedalus container" in broken["error"]


# -- Telegram ---------------------------------------------------------------------------------------------------


def test_telegram_offers_the_same_spans_with_six_hours_first() -> None:
    rows = server_grant_rows("abc123def456", "0123456789ab", "mobile-mcp")
    assert rows[0] == [("🔓 All from mobile-mcp · 6 h", "ps:abc123def456:0123456789ab:6")]
    assert [data for _, data in rows[1]] == [f"ps:abc123def456:0123456789ab:{h}" for h in (1, 2, 12, 0)]
    assert all(len(data.encode()) <= 64 for row in rows for _, data in row), "Telegram's limit on a button's data"
