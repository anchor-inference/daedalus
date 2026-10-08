"""The ways an operator's secret is used without the model reading it, end to end: attached to a message,
read by a command, typed into a page, put into an MCP call, handed to a staff member — and masked back to
its placeholder in everything that returns."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolDefinition

from daedalus.config import Settings
from daedalus.host.transcript_view import message_view
from daedalus.mcp.manager import McpToolProxy, SecretVault
from daedalus.security import operator_secrets
from daedalus.stores.database import Database
from daedalus.tools import shell
from tests.unit.test_browser_tools import Rig, base, daemon, rig  # noqa: F401 — the browser rig and its fixtures
from tests.unit.test_front import front  # noqa: F401 — the Telegram front and its recording bot
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

VALUE = "Tr0ub4dor-and-3"
PLACEHOLDER = "«secret:router_admin»"


def _everything_stored(path: Path) -> str:
    """Every text value in every table of the database, as one string: where a leak would be found."""
    connection = sqlite3.connect(path)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        parts: list[str] = []
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):  # noqa: S608 — the table names are the database's own
                parts.extend(value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value) for value in row if value is not None)
        return "\n".join(parts)
    finally:
        connection.close()


async def test_a_secret_on_a_message_is_used_by_a_command_and_never_read(settings: Settings, db: Database) -> None:
    command = f'echo "user=admin pass=$DAEDALUS_SECRET_ROUTER_ADMIN"; cat "$DAEDALUS_SECRET_ROUTER_ADMIN_FILE"; echo; echo "via {PLACEHOLDER}"'
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": command}}, {"text": "logged in"}])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session("router")
        sid = state.session.id
        await manager.secrets.put("session", sid, "router_admin", VALUE, "ISP router web admin, user admin")
        waiter = asyncio.create_task(_wait_finished(manager))
        # The operator also pastes the value out of habit; it goes in as the placeholder all the same.
        await manager.submit(sid, f"Log into the router. (it is {VALUE})", secrets=["router_admin"])
        assert (await waiter)[0][2] == "completed"

        sent = json.dumps([m.model_dump(mode="json") for request in provider.requests for m in request.messages], ensure_ascii=False)
        assert VALUE not in sent
        assert f"(it is {PLACEHOLDER})" in sent and "ISP router web admin" in sent and "$DAEDALUS_SECRET_ROUTER_ADMIN" in sent
        assert "Secrets the operator handed you" in sent  # the turn context names it on every run
        tool_result = next(m for m in provider.requests[-1].messages if m.role.value == "tool")
        result_text = json.dumps(tool_result.model_dump(mode="json"), ensure_ascii=False)
        # The command had the value — it printed it twice — and the model read the placeholder both times.
        assert result_text.count(PLACEHOLDER) >= 3 and VALUE not in result_text

        transcript = await manager.transcript(sid)
        opening = next(m for m in transcript if m.role.value == "user")
        view = message_view(opening)
        assert view["secrets"] == [{"name": "router_admin", "scope": "session"}]
        assert operator_secrets.ATTACHMENT_PREFIX not in view["text"] and view["text"].startswith("Log into the router.")
        await manager.secrets.settle()
        secret = manager.secrets.listed()[0]
        assert secret.uses >= 1 and secret.last_used_by.startswith("Exec in")
    finally:
        await manager.close()
    assert VALUE not in _everything_stored(settings.db_path)


async def test_the_environment_and_the_sandbox_show_a_session_only_its_own(settings: Settings, db: Database, monkeypatch: Any) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        mine = (await manager.create_session("mine")).session.id
        other = (await manager.create_session("other")).session.id
        await manager.get_state(mine)
        await manager.get_state(other)
        await manager.secrets.put("session", mine, "router_admin", VALUE)
        await manager.secrets.put("session", other, "elsewhere", "another-value-1")
        env = shell.shell_environment(mine)
        assert env["DAEDALUS_SECRET_ROUTER_ADMIN"] == VALUE and "DAEDALUS_SECRET_ELSEWHERE" not in env
        assert Path(env["DAEDALUS_SECRET_ROUTER_ADMIN_FILE"]).read_text() == VALUE
        # A placeholder in a command becomes the variable; the value is never written into the command line.
        rewritten, named = manager.secrets.rewrite_command(f"curl -u admin:{PLACEHOLDER} http://router", mine)
        assert rewritten == "curl -u admin:${DAEDALUS_SECRET_ROUTER_ADMIN} http://router" and [s.name for s in named] == ["router_admin"]
        assert manager.secrets.rewrite_command(f"echo {PLACEHOLDER}", other)[0] == f"echo {PLACEHOLDER}"
        # Inside the sandbox the secrets' folder is an empty one with only this session's scope bound back.
        monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
        argv, sandboxed = await shell.sandbox_argv("true", SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[]), writable=[], sealed=[], session_id=mine)
        assert sandboxed
        root = manager.secrets.files_dir
        assert root is not None and argv[argv.index("--tmpfs", argv.index("/tmp") + 1) + 1] == str(root)
        bound = [argv[i + 1] for i, part in enumerate(argv) if part == "--ro-bind" and argv[i + 1].startswith(str(root))]
        assert bound == [str(root / f"session-{mine}")]
    finally:
        await manager.close()


class _Connection:
    """An MCP server that echoes what it was given."""

    name = "router-api"

    def __init__(self) -> None:
        self.vault = SecretVault()
        self.calls: list[dict[str, Any]] = []

    async def call(self, remote: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(arguments)
        return SimpleNamespace(content=[SimpleNamespace(text=f"signed in with {arguments['password']}", type="text")], isError=False)


async def test_an_mcp_call_gets_the_value_and_its_answer_comes_back_masked(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("api")).session.id
        await manager.get_state(sid)
        await manager.secrets.put("session", sid, "router_admin", VALUE)
        connection = _Connection()
        proxy = McpToolProxy(connection, "login", ToolDefinition(name="router_api_login", description="log in", parameters={"type": "object", "properties": {}}))  # type: ignore[arg-type]
        result = await proxy.invoke(ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"}), {"user": "admin", "password": PLACEHOLDER})
        assert connection.calls == [{"user": "admin", "password": VALUE}]
        assert VALUE not in str(result.content) and PLACEHOLDER in str(result.content)
        # Another session's call names the placeholder and sends it as it is.
        other = (await manager.create_session("other")).session.id
        await manager.get_state(other)
        await proxy.invoke(ToolContext(tenant_id="t", run_id="r", session_id=other, metadata={"tool_call_id": "c"}), {"user": "admin", "password": PLACEHOLDER})
        assert connection.calls[-1]["password"] == PLACEHOLDER
    finally:
        await manager.close()


async def test_the_browser_types_a_handed_over_secret_into_a_password_field(rig: Rig) -> None:  # noqa: F811 — the fixture
    sid = await rig.session()
    await rig.manager.secrets.put("session", sid, "router_admin", VALUE)
    await rig.call(sid, "BrowserOpen", url="https://login.test/")
    text, failed = await rig.call(sid, "BrowserAct", action="type", ref="e31", element="the password field", text=PLACEHOLDER)
    assert not failed, text
    group = rig.daemon.groups[f"s-{sid}"]
    typed = [p for m, p in rig.daemon.calls if m == "page.act" and not p.get("dry_run")][-1]
    assert typed["text"] == VALUE and typed["operator_secret"] is True
    assert VALUE not in text
    # Into an ordinary field the value goes too, but not as a handed-over secret, and the page's echo of it
    # comes back as the placeholder.
    text, failed = await rig.call(sid, "BrowserAct", action="type", ref="e30", element="the email field", text=f"admin+{PLACEHOLDER}")
    assert not failed, text
    typed = [p for m, p in rig.daemon.calls if m == "page.act" and not p.get("dry_run")][-1]
    assert typed["text"] == f"admin+{VALUE}" and "operator_secret" not in typed
    snapshot, _ = await rig.call(sid, "BrowserSnapshot")
    assert VALUE not in snapshot
    audit = json.dumps(await rig.app.extensions["browser"].audit_log(f"s-{sid}"), ensure_ascii=False)
    assert VALUE not in audit and '"secrets": ["router_admin"]' in audit
    assert group is not None
    # A session the secret is not for types the placeholder itself, and the password field stays the operator's.
    other = await rig.session("elsewhere")
    await rig.call(other, "BrowserOpen", url="https://login.test/")
    text, failed = await rig.call(other, "BrowserAct", action="type", ref="e31", element="the password field", text=PLACEHOLDER)
    assert failed and "BrowserHandoff(reason='login'" in text


async def test_deleting_a_secret_takes_it_away_from_every_path(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("router")).session.id
        await manager.get_state(sid)
        secret = await manager.secrets.put("session", sid, "router_admin", VALUE)
        assert "DAEDALUS_SECRET_ROUTER_ADMIN" in shell.shell_environment(sid)
        assert await manager.secrets.delete(secret.id)
        assert "DAEDALUS_SECRET_ROUTER_ADMIN" not in shell.shell_environment(sid)
        assert manager.secrets.substitute({"password": PLACEHOLDER}, sid, used_by="test")[0] == {"password": PLACEHOLDER}
        assert operator_secrets.prompt_section(manager.secrets.available(sid)) == ""
        # What a command prints of it afterwards is still masked.
        assert VALUE not in manager.redactor.redact(f"it was {VALUE}")
    finally:
        await manager.close()


async def test_the_app_hands_one_over_lists_it_without_its_value_and_takes_it_back(settings: Settings, db: Database) -> None:
    import httpx

    from daedalus.extensions.api import build_app

    manager = await _manager(settings, db, ScriptedProvider([{"text": "noted"}]))
    try:
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
        headers = {"X-Daedalus-Token": "tok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            chat = (await manager.create_session("router")).session.id
            refused = await client.post("/api/secrets", headers=headers, json={"session_id": chat, "scope": "project", "name": "x1", "value": VALUE})
            assert refused.status_code == 400 and "not in a project" in refused.text
            kept = await client.post("/api/secrets", headers=headers, json={"session_id": chat, "scope": "session", "name": "Router admin", "value": VALUE, "note": "user admin"})
            assert kept.status_code == 200 and kept.json()["placeholder"] == PLACEHOLDER and VALUE not in kept.text
            listed = await client.get(f"/api/secrets?session_id={chat}", headers=headers)
            assert [s["name"] for s in listed.json()["secrets"]] == ["router_admin"] and listed.json()["project_id"] is None and VALUE not in listed.text
            everything = await client.get("/api/secrets", headers=headers)
            assert everything.json()["secrets"][0]["scope_title"] == "router"
            waiter = asyncio.create_task(_wait_finished(manager))
            sent = await client.post(f"/api/sessions/{chat}/messages", headers=headers, json={"text": "", "secrets": ["router_admin"], "client_message_id": "m1"})
            assert sent.status_code == 200, sent.text
            await waiter
            view = (await client.get(f"/api/sessions/{chat}", headers=headers)).json()
            users = [m for m in view["messages"] if m["role"] == "user"]
            assert users[0]["secrets"] == [{"name": "router_admin", "scope": "session"}] and users[0]["text"] == ""
            assert (await client.delete(f"/api/secrets/{kept.json()['id']}", headers=headers)).json() == {"ok": True}
            assert (await client.get("/api/secrets", headers=headers)).json()["secrets"] == []
            assert (await client.delete(f"/api/secrets/{kept.json()['id']}", headers=headers)).status_code == 404
    finally:
        await manager.close()


async def test_telegram_keeps_a_secret_and_deletes_the_message_that_carried_it(front: Any) -> None:  # noqa: F811 — the fixture
    from aiogram.filters import CommandObject

    from tests.unit.test_private_chat import _message, _said

    deleted: list[tuple[int, int]] = []

    async def delete_message(chat_id: int, message_id: int) -> bool:
        deleted.append((chat_id, message_id))
        return True

    front.bot.delete_message = delete_message
    replies: list[str] = []
    await front.cmd_new(_said(_message("/new router"), replies), CommandObject(prefix="/", command="new", args="router"))
    sid = await front.current_session_id()
    await front.cmd_secret(_said(_message(f"/secret router_admin {VALUE}"), replies), CommandObject(prefix="/", command="secret", args=f"router_admin {VALUE}"))
    assert deleted == [(_message().chat.id, 1)]
    assert "Kept «secret:router_admin»" in replies[-1] and VALUE not in replies[-1]
    assert [s.name for s in front.manager.secrets.available(sid)] == ["router_admin"]
    await front.cmd_secret(_said(_message("/secret"), replies), CommandObject(prefix="/", command="secret", args=None))
    assert replies[-1].startswith("usage: /secret")
    assert front.submitted == []  # nothing of it reached the session as a message
