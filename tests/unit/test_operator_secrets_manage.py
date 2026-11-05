"""Managing the operator's secrets from the app: list without values, reveal on its own audited request,
change a value, rename, remove one that conversations still name — and none of it without signing in."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions import api_panel
from daedalus.remote import door as remote_door
from daedalus.security import operator_secrets
from daedalus.security.operator_secrets import OperatorSecrets, SecretError
from daedalus.security.redact import Redactor
from daedalus.stores.database import Database
from tests.unit.test_operator_secrets import _scopes
from tests.unit.test_operator_secrets_paths import _everything_stored
from tests.unit.test_session_runner import ScriptedProvider, _manager

VALUE = "Tr0ub4dor-and-3"
NEW_VALUE = "correct-horse-battery-9"


@pytest.fixture
async def store(db: Database, tmp_path: Path) -> OperatorSecrets:
    await _scopes(db)
    secrets = OperatorSecrets(db, tmp_path / "secrets" / operator_secrets.KEY_FILE, Redactor(), files_dir=tmp_path / "runtime" / "secrets")
    secrets.project_of = {"chat": "proj", "staff-chat": "proj", "other": "elsewhere"}.get
    await secrets.load()
    return secrets


async def test_a_new_value_replaces_the_old_and_the_old_stays_masked(db: Database, store: OperatorSecrets, tmp_path: Path) -> None:
    secret = await store.put("session", "chat", "router_admin", VALUE, "admin")
    outcome = await store.update(secret.id, value=NEW_VALUE)
    assert outcome is not None and outcome[1] == ["value"]
    assert store.get(secret.id).value == NEW_VALUE  # type: ignore[union-attr]
    # Nothing of the old value is kept at rest: one sealed row, which opens to the new value only.
    assert (await db.fetchone("SELECT count(*) AS n FROM operator_secrets"))["n"] == 1
    again = OperatorSecrets(db, tmp_path / "secrets" / operator_secrets.KEY_FILE, Redactor())
    await again.load()
    assert again.get(secret.id).value == NEW_VALUE  # type: ignore[union-attr]
    # A command that still prints the old one reads the placeholder.
    assert VALUE not in store.redactor.redact(f"old {VALUE}")  # type: ignore[attr-defined]


async def test_a_rename_reseals_under_the_new_name_and_moves_the_placeholder(db: Database, store: OperatorSecrets, tmp_path: Path) -> None:
    secret = await store.put("session", "chat", "router_admin", VALUE)
    await store.put("session", "chat", "wifi", "wifi-password-1")
    with pytest.raises(SecretError, match="already a secret named wifi"):
        await store.update(secret.id, name="WiFi")
    outcome = await store.update(secret.id, name="Router root", note="the root account")
    assert outcome is not None and outcome[1] == ["name", "note"]
    assert store.get(secret.id).placeholder == "«secret:router_root»"  # type: ignore[union-attr]
    assert store.substitute({"p": "«secret:router_root»"}, "chat", used_by="test")[0] == {"p": VALUE}
    # The sealing carries the name: a fresh store, as after a restart, opens it under the new one.
    again = OperatorSecrets(db, tmp_path / "secrets" / operator_secrets.KEY_FILE, Redactor())
    await again.load()
    assert again.get(secret.id).value == VALUE and again.get(secret.id).name == "router_root"  # type: ignore[union-attr]
    assert await store.update("missing", name="x") is None
    assert (await store.update(secret.id, note="the root account"))[1] == []  # type: ignore[index]


async def test_a_reveal_is_audited_by_name_and_never_by_value(db: Database, store: OperatorSecrets) -> None:
    secret = await store.put("session", "chat", "router_admin", VALUE)
    shown = await store.reveal(secret.id, purpose="copy", via="cookie")
    assert shown is not None and shown[1] == VALUE
    trail = await store.audit_trail(secret.id)
    assert [(row["name"], row["action"], row["via"]) for row in trail] == [("router_admin", "copy", "cookie")]
    rows = await db.fetchall("SELECT * FROM operator_secret_audit")
    assert VALUE not in json.dumps([dict(row) for row in rows])
    assert await store.reveal("missing", purpose="reveal", via="cookie") is None


async def test_references_are_the_conversations_whose_history_names_the_placeholder(db: Database, store: OperatorSecrets) -> None:
    mine = await store.put("session", "chat", "router_admin", VALUE)
    shared = await store.put("project", "proj", "nas", "nas-password-77")
    assert await store.references(mine) == (0, [])
    for session_id, text in (("chat", "log in with «secret:router_admin»"), ("staff-chat", "use «secret:nas»"), ("other", "«secret:nas» elsewhere")):
        await db.execute(
            "INSERT INTO session_messages(session_id, tenant_id, message) VALUES (?, 'operator', ?)",
            (session_id, json.dumps({"role": "user", "content": text}, ensure_ascii=False)),
        )
    assert await store.references(mine) == (1, [{"id": "chat", "title": "chat"}])
    # A project's secret is named by the project's conversations, not by another project's.
    count, titles = await store.references(shared)
    assert count == 1 and titles == [{"id": "staff-chat", "title": "staff-chat"}]


def _client(manager: Any, settings: Settings, db: Database, *, via_panel: bool = False) -> httpx.AsyncClient:
    from daedalus.extensions.api import build_app

    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    inner = build_app(app, "tok")  # type: ignore[arg-type]
    if not via_panel:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=inner), base_url="http://test")  # type: ignore[arg-type]

    async def as_if_the_panel_door_let_it_through(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope = {**scope, api_panel.SCOPE_KEY: {"panel": "p1"}}
        await inner(scope, receive, send)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=as_if_the_panel_door_let_it_through), base_url="http://test")  # type: ignore[arg-type]


async def test_the_routes_list_without_values_reveal_change_and_remove(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    headers = {"X-Daedalus-Token": "tok"}
    try:
        async with _client(manager, settings, db) as client:
            chat = (await manager.create_session("router")).session.id
            other = (await manager.create_session("elsewhere")).session.id
            kept = (await client.post("/api/secrets", headers=headers, json={"session_id": chat, "scope": "session", "name": "router_admin", "value": VALUE})).json()
            await client.post("/api/secrets", headers=headers, json={"session_id": other, "scope": "session", "name": "nas", "value": "nas-password-77"})

            listed = await client.get(f"/api/secrets?session_id={chat}", headers=headers)
            assert [s["name"] for s in listed.json()["secrets"]] == ["router_admin"]
            assert [s["name"] for s in listed.json()["others"]] == ["nas"]
            everything = await client.get("/api/secrets", headers=headers)
            assert VALUE not in listed.text and VALUE not in everything.text and "nas-password-77" not in everything.text
            assert "projects" in everything.json()

            revealed = await client.post(f"/api/secrets/{kept['id']}/reveal", headers=headers, json={"purpose": "reveal"})
            assert revealed.status_code == 200 and revealed.json() == {"value": VALUE}
            assert revealed.headers["cache-control"] == "no-store"
            assert (await client.post("/api/secrets/nope/reveal", headers=headers, json={})).status_code == 404

            renamed = await client.patch(f"/api/secrets/{kept['id']}", headers=headers, json={"name": "Router root", "value": NEW_VALUE, "note": "root"})
            assert renamed.status_code == 200 and renamed.json()["name"] == "router_root" and renamed.json()["changed"] == ["name", "value", "note"]
            assert VALUE not in renamed.text and NEW_VALUE not in renamed.text
            clash = await client.patch(f"/api/secrets/{kept['id']}", headers=headers, json={"name": "x"})
            assert clash.status_code == 200
            await client.post("/api/secrets", headers=headers, json={"session_id": chat, "scope": "session", "name": "y", "value": "y-value-12"})
            assert (await client.patch(f"/api/secrets/{kept['id']}", headers=headers, json={"name": "y"})).status_code == 400
            assert (await client.patch(f"/api/secrets/{kept['id']}", headers=headers, json={"value": ""})).status_code == 422
            assert (await client.post(f"/api/secrets/{kept['id']}/reveal", headers=headers, json={"purpose": "copy"})).json() == {"value": NEW_VALUE}

            usage = (await client.get(f"/api/secrets/{kept['id']}/usage", headers=headers)).json()
            assert usage["conversations"] == 0
            assert [row["action"] for row in usage["audit"]] == ["copy", "rename", "note", "change", "rename", "reveal", "create"][: len(usage["audit"])]
            assert {"reveal", "copy", "change", "rename", "create"} <= {row["action"] for row in usage["audit"]}

            assert (await client.delete(f"/api/secrets/{kept['id']}", headers=headers)).json() == {"ok": True}
            trail = await db.fetchall("SELECT action FROM operator_secret_audit WHERE secret_id = ? ORDER BY seq", (kept["id"],))
            assert trail[-1]["action"] == "delete"
        stored = _everything_stored(Path(db.path))
        assert VALUE not in stored and NEW_VALUE not in stored
    finally:
        await manager.close()


async def test_removing_a_secret_a_conversation_names_is_warned_about(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    headers = {"X-Daedalus-Token": "tok"}
    try:
        async with _client(manager, settings, db) as client:
            chat = (await manager.create_session("router")).session.id
            kept = (await client.post("/api/secrets", headers=headers, json={"session_id": chat, "scope": "session", "name": "router_admin", "value": VALUE})).json()
            await db.execute(
                "INSERT INTO session_messages(session_id, tenant_id, message) VALUES (?, 'operator', ?)",
                (chat, json.dumps({"content": operator_secrets.attachment_text(manager.secrets.get(kept["id"]))}, ensure_ascii=False)),  # type: ignore[arg-type]
            )
            usage = (await client.get(f"/api/secrets/{kept['id']}/usage", headers=headers)).json()
            assert usage["conversations"] == 1 and usage["titles"] == [{"id": chat, "title": "router"}]
            assert (await client.delete(f"/api/secrets/{kept['id']}", headers=headers)).status_code == 200
            assert (await client.get(f"/api/secrets/{kept['id']}/usage", headers=headers)).status_code == 404
    finally:
        await manager.close()


async def test_a_project_secret_can_be_filed_from_settings(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    headers = {"X-Daedalus-Token": "tok"}
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('p1','Home network','2026-01-01','{}','')")
        await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('p2','One chat','2026-01-01','{\"ephemeral\": true}','')")
        async with _client(manager, settings, db) as client:
            projects = (await client.get("/api/secrets", headers=headers)).json()["projects"]
            assert {"id": "p1", "name": "Home network"} in projects and all(p["id"] != "p2" for p in projects)
            filed = await client.post("/api/secrets", headers=headers, json={"project_id": "p1", "scope": "project", "name": "nas", "value": VALUE})
            assert filed.status_code == 200 and filed.json()["scope_id"] == "p1" and VALUE not in filed.text
            refused = await client.post("/api/secrets", headers=headers, json={"project_id": "p2", "scope": "project", "name": "nas", "value": VALUE})
            assert refused.status_code == 400
            assert (await client.post("/api/secrets", headers=headers, json={"scope": "session", "name": "nas", "value": VALUE})).status_code == 400
            assert (await client.post("/api/secrets", headers=headers, json={"project_id": "zz", "scope": "project", "name": "nas", "value": VALUE})).status_code == 404
    finally:
        await manager.close()


async def test_every_secrets_route_wants_a_signed_in_operator(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        chat = (await manager.create_session("router")).session.id
        secret = await manager.secrets.put("session", chat, "router_admin", VALUE)
        async with _client(manager, settings, db) as client:
            for method, path, body in (
                ("GET", "/api/secrets", None), ("POST", f"/api/secrets/{secret.id}/reveal", {"purpose": "reveal"}),
                ("PATCH", f"/api/secrets/{secret.id}", {"note": "x"}), ("GET", f"/api/secrets/{secret.id}/usage", None),
                ("DELETE", f"/api/secrets/{secret.id}", None), ("POST", f"/api/secrets/{secret.id}/reveal", {}),
            ):
                answer = await client.request(method, path, json=body, headers={"X-Daedalus-Token": "wrong"} if body == {} else {})
                assert answer.status_code == 401, (method, path)
                assert VALUE not in answer.text
        assert manager.secrets.get(secret.id) is not None
        assert await db.fetchall("SELECT * FROM operator_secret_audit") == []
    finally:
        await manager.close()


async def test_the_side_panel_and_a_terminal_never_reach_a_value(settings: Settings, db: Database) -> None:
    # Neither door forwards a secrets route at all…
    for method in ("GET", "POST", "PATCH", "DELETE"):
        for path in ("/api/panel/secrets", "/api/panel/secrets/abc/reveal", "/api/secrets/abc/reveal"):
            assert not any(wanted == method and pattern.fullmatch(path) for wanted, pattern in (*api_panel.FORWARDED, *api_panel.OWN))
        assert remote_door.route_of(method, "/api/secrets/abc/reveal") is None
        assert remote_door.route_of(method, "/api/secrets") is None
    # …and a request that claims to have come through the panel's door without passing it is not let in:
    # the door takes the mark off every request outside its own routes. (Were a door ever to forward a
    # secrets route, the reveal also refuses its ``via`` by name, ``api_secrets.CLOSED_DOORS``.)
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        chat = (await manager.create_session("router")).session.id
        secret = await manager.secrets.put("session", chat, "router_admin", VALUE)
        async with _client(manager, settings, db, via_panel=True) as client:
            answer = await client.post(f"/api/secrets/{secret.id}/reveal", json={"purpose": "reveal"})
            assert answer.status_code in (401, 403) and VALUE not in answer.text
        assert await db.fetchall("SELECT * FROM operator_secret_audit WHERE action = 'reveal'") == []
    finally:
        await manager.close()


async def test_the_reveal_refuses_a_request_that_came_through_either_door(settings: Settings, db: Database) -> None:
    from fastapi import FastAPI

    from daedalus.extensions import api_secrets

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        chat = (await manager.create_session("router")).session.id
        secret = await manager.secrets.put("session", chat, "router_admin", VALUE)
        for via in ("panel", "device"):
            api = FastAPI()
            api_secrets.register(api, SimpleNamespace(manager=manager, db=db), lambda via=via: {"user_id": 1, "via": via})  # type: ignore[arg-type,misc]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
                answer = await client.post(f"/api/secrets/{secret.id}/reveal", json={"purpose": "reveal"})
            assert answer.status_code == 403 and VALUE not in answer.text, via
        assert await db.fetchall("SELECT * FROM operator_secret_audit") == []
    finally:
        await manager.close()
