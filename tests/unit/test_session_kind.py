"""What a conversation is for: chat, computer or chrome, in a column of its own.

Listed, changed, and marked by what the operator does — nothing makes a chosen kind plain again — and
carried across a restart. The migration marks the conversations already in the operator's Chrome.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.host import tool_groups
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import MIGRATIONS, Database
from tests.support.models import model_config

# -- the column -------------------------------------------------------------------------------------


async def test_the_migration_marks_panel_and_operator_chrome_conversations(tmp_path: Path) -> None:
    path = tmp_path / "kinds.sqlite"
    db = Database(path, workspaces_dir=tmp_path / "ws")
    await db.open()
    await db.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE sessions DROP COLUMN kind")
    raw.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p', 'P', 'now', '{}', '')")
    for sid, metadata in (("panel", '{"browser.panel": true}'), ("switched", "{}"), ("plain", "{}"), ("broken", "not json")):
        raw.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', '', 'now', 'now', ?, 'p')", (sid, metadata))
    raw.execute("""INSERT INTO kv(key, value) VALUES ('browser.user.sessions', '{"switched": {"env": "user"}, "plain": {"env": "agent"}, "gone": {"env": "user"}}')""")
    raw.execute(f"UPDATE schema_version SET version = {len(MIGRATIONS) - 1}")
    raw.commit()
    raw.close()
    db = Database(path, workspaces_dir=tmp_path / "ws")
    await db.open()
    rows = {r["id"]: r["kind"] for r in await db.fetchall("SELECT id, kind FROM sessions")}
    await db.close()
    assert rows == {"panel": "chrome", "switched": "chrome", "plain": "chat", "broken": "chat"}
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE sessions SET kind = 'robot' WHERE id = 'plain'")
    raw.close()


@pytest.fixture
async def manager(settings: Settings, db: Database) -> AsyncIterator[SessionManager]:
    found = SessionManager(settings, model_config(), db=db)
    await found.start()
    yield found
    await found.close()


async def test_a_kind_is_listed_changed_and_a_sticky_mark_leaves_a_chosen_kind_alone(manager: SessionManager) -> None:
    sid = (await manager.create_session("chat")).session.id
    assert (await manager.get_state(sid)).kind == "chat"  # type: ignore[union-attr]
    subscription = manager.bus.subscribe(EventFilter(types=("session.kind",)), name="test")
    assert await manager.set_session_kind(sid, "chrome", only_from="chat") is True
    event = await asyncio.wait_for(anext(subscription), 2)
    assert event.payload == {"kind": "chrome"} and event.session_id == sid
    # A Chrome conversation starts its runs with the browser's tools, as a side panel one does.
    assert "browser" in (await manager.get_state(sid)).metadata[tool_groups.CARRIED_KEY]  # type: ignore[union-attr]
    assert await manager.set_session_kind(sid, "computer", only_from="chat") is False
    assert [row["kind"] for row in await manager.list_sessions() if row["id"] == sid] == ["chrome"]
    assert [row["kind"] for row in await manager.list_sessions(ids=[sid])] == ["chrome"]
    with pytest.raises(ValueError):
        await manager.set_session_kind(sid, "robot")
    manager._states.pop(sid)
    assert (await manager.get_state(sid)).kind == "chrome"  # type: ignore[union-attr]
