"""Pinning a project or a chat to the top of the sidebar.

The pin is the project's: a chat is pinned by its scratch project, so the pin outlives the chat
becoming a project and goes with the record when the chat is deleted. It is written through its own
address, without the settings write's revision, and the first page of the listing carries a pinned
chat however far down the recency order it is.
"""

from __future__ import annotations

import json
from typing import Any

from daedalus.stores.database import Database
from tests.unit.test_project_commands import running  # noqa: F401 — the fixture is used by name
from tests.unit.test_project_folders_api import HEADERS


async def _pin(client: Any, project_id: str, pinned: bool) -> Any:
    return await client.put(f"/api/projects/{project_id}/pin", headers=HEADERS, json={"pinned": pinned})


async def _listed(client: Any, **query: Any) -> dict[str, Any]:
    answer = await client.get("/api/sessions", headers=HEADERS, params=query)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _pinned_at(listing: dict[str, Any], project_id: str) -> str:
    return next(p for p in listing["projects"] if p["id"] == project_id)["pinned_at"]


async def test_pin_and_unpin_a_project(running: Any, db: Database) -> None:  # noqa: F811
    manager, client = running
    created = await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery"})
    pid = created.json()["id"]
    revision = created.json()["entity_revision"]
    assert _pinned_at(await _listed(client), pid) == ""

    pinned = await _pin(client, pid, True)
    assert pinned.status_code == 200, pinned.text
    stamp = pinned.json()["pinned_at"]
    assert stamp and _pinned_at(await _listed(client), pid) == stamp
    # Pinning again from another screen keeps the first time, which is the item's place in the block.
    assert (await _pin(client, pid, True)).json()["pinned_at"] == stamp
    # Not a revisioned write: an open settings form is not made stale by a pin.
    assert next(p for p in (await client.get("/api/projects", headers=HEADERS)).json() if p["id"] == pid)["entity_revision"] == revision
    events = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'project.changed' AND project_id = ?", (pid,))
    assert "pinned" in [json.loads(e["payload_json"]).get("change") for e in events]

    assert (await _pin(client, pid, False)).json()["pinned_at"] == ""
    assert await manager.projects.pins() == {}
    # A settings write afterwards leaves the pin alone, and a pin leaves the settings alone.
    await _pin(client, pid, True)
    renamed = await client.patch(f"/api/projects/{pid}", headers=HEADERS, json={
        "name": "Bread", "client_operation_id": "rename-1", "expected_entity_revision": revision})
    assert renamed.status_code == 200, renamed.text
    project = await manager.projects.get(pid)
    assert project is not None and project.name == "Bread" and pid in await manager.projects.pins()


async def test_pinning_an_unknown_project_is_refused(running: Any) -> None:  # noqa: F811
    _, client = running
    assert (await _pin(client, "nothing", True)).status_code == 404
    assert (await client.put("/api/projects/nothing/pin", headers=HEADERS, json={"pinned": True, "order": 1})).status_code == 422


async def test_a_pinned_chat_stays_pinned_when_it_becomes_a_project(running: Any) -> None:  # noqa: F811
    manager, client = running
    chat = await manager.create_session("first")
    pid = chat.project.id
    assert chat.project.settings.ephemeral
    stamp = (await _pin(client, pid, True)).json()["pinned_at"]
    await manager.create_session("second", project_id=pid)
    project = await manager.projects.get(pid)
    assert project is not None and not project.settings.ephemeral
    assert (await manager.projects.pins())[pid] == stamp
    assert _pinned_at(await _listed(client), pid) == stamp


async def test_a_deleted_chat_takes_its_pin_with_it(running: Any) -> None:  # noqa: F811
    manager, client = running
    chat = await manager.create_session("first")
    pid = chat.project.id
    await _pin(client, pid, True)
    await manager.delete_session(chat.session.id)
    assert pid not in [p["id"] for p in (await _listed(client))["projects"]]
    assert await manager.projects.pins() == {}


async def test_the_first_page_carries_a_pinned_chat_beyond_the_page(running: Any, db: Database) -> None:  # noqa: F811
    manager, client = running
    made = [await manager.create_session(f"chat {n}") for n in range(3)]
    for n, created in enumerate(made):
        await db.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (f"2026-09-0{n + 1}T10:00:00+00:00", created.session.id))
    oldest = made[0]
    await _pin(client, oldest.project.id, True)
    first = await _listed(client, limit=1)
    assert [s["id"] for s in first["sessions"]] == [made[2].session.id, oldest.session.id]
    assert first["next_cursor"]
    # The cursor is still the last row of the page proper, so the next page is the next by recency.
    second = await _listed(client, limit=1, cursor=first["next_cursor"])
    assert [s["id"] for s in second["sessions"]] == [made[1].session.id]
    # The archive is its own list, where a pinned chat is not lifted out of its order.
    assert (await _listed(client, view="archive"))["sessions"] == []
