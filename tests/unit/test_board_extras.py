"""A card's due date, the operator as its assignee, and its history.

Each is pinned from the side that could break something else: a date is an instant read the same
everywhere, a card the operator keeps is never launched or handed back by the machinery that works
unassigned cards, and the history is read from the bus's own table without scanning it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import board as board_extension
from daedalus.extensions.api import build_app
from daedalus.extensions.board_commands import normalize_due
from daedalus.extensions.coordinator_authority import install_authority
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from tests.support.notifications import RecordingNotifications

HEADERS = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None,
                          extensions={}, guard=None)
    app.notifications = RecordingNotifications()
    app.executions = ExecutionStore(db)
    app.executions.acquire()
    await app.executions.boot()
    install_authority(app)
    loops = await board_extension.install(app)
    yield app
    for loop in loops:
        loop.cancel()
    await asyncio.gather(*loops, return_exceptions=True)
    app.executions.release()
    await manager.close()


@pytest.fixture
async def client(app: Any) -> Any:
    api = build_app(app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as http:  # type: ignore[arg-type]
        yield http


async def create(client: httpx.AsyncClient, title: str = "A card", **fields: Any) -> dict[str, Any]:
    revision = (await client.get("/api/control/revisions", headers=HEADERS)).json()["collection_revision"]
    made = await client.post("/api/board", headers=HEADERS, json={
        "title": title, "client_operation_id": f"create-{title}-{revision}", "expected_collection_revision": revision, **fields})
    assert made.status_code == 201, made.text
    return made.json()["task"]


async def project_card(app: Any, client: httpx.AsyncClient, tmp_path: Path, **fields: Any) -> tuple[str, dict[str, Any]]:
    root = tmp_path / "bakery"
    root.mkdir(exist_ok=True)
    project = await app.manager.projects.create("Bakery", [str(root)])
    base = f"/api/projects/{project.id}/board"
    revision = (await client.get(f"/api/control/revisions?project={project.id}", headers=HEADERS)).json()["collection_revision"]
    made = await client.post(base, headers=HEADERS, json={
        "title": "The operator's own", "client_operation_id": "mine", "expected_collection_revision": revision, **fields})
    assert made.status_code == 201, made.text
    return project.id, made.json()["task"]


async def edit(client: httpx.AsyncClient, task: dict[str, Any], **fields: Any) -> httpx.Response:
    listing = (await client.get("/api/board?include_done=1", headers=HEADERS)).json()
    revision = next(t["entity_revision"] for t in listing if t["id"] == task["id"])
    return await client.put(f"/api/board/{task['id']}", headers=HEADERS, json={
        "client_operation_id": f"edit-{task['id']}-{revision}", "expected_entity_revision": revision, **fields})


def listed(listing: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    return next(t for t in listing if t["id"] == task_id)


async def board(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return (await client.get("/api/board?include_done=1", headers=HEADERS)).json()


def day(offset: int) -> str:
    return (datetime.now(UTC) + timedelta(days=offset)).strftime("%Y-%m-%d")


# -- the due date --------------------------------------------------------------------------------


def test_a_due_date_is_stored_as_a_utc_instant() -> None:
    assert normalize_due("2026-12-03") == "2026-12-03T23:59:59Z"
    assert normalize_due("2026-12-03T10:00:00+03:00") == "2026-12-03T07:00:00Z"
    assert normalize_due("2026-12-03T10:00:00") == "2026-12-03T10:00:00Z"  # no zone: UTC, never the host's
    assert normalize_due("2026-12-03T10:00:00.250Z") == "2026-12-03T10:00:00Z"
    assert normalize_due("") is None and normalize_due(None) is None and normalize_due("  ") is None
    for bad in ("soon", "2026-13-40", "03.12.2026"):
        with pytest.raises(ValueError, match="ISO"):
            normalize_due(bad)


async def test_a_due_date_is_set_changed_and_cleared_through_the_routes(client: httpx.AsyncClient) -> None:
    task = await create(client, "Pay the invoice", due_at=day(3))
    assert task["due_at"] == f"{day(3)}T23:59:59Z" and task["overdue"] is False
    assert listed(await board(client), task["id"])["due_at"] == task["due_at"]

    # Leaving the field out leaves the date; an empty string clears it.
    kept = (await edit(client, task, priority=1)).json()["task"]
    assert kept["due_at"] == task["due_at"]
    moved = (await edit(client, task, due_at="2026-01-05T08:30:00Z")).json()["task"]
    assert moved["due_at"] == "2026-01-05T08:30:00Z" and moved["overdue"] is True
    cleared = (await edit(client, task, due_at="")).json()["task"]
    assert cleared["due_at"] is None and cleared["overdue"] is False

    refused = await edit(client, task, due_at="next week")
    assert refused.status_code == 422
    assert (await client.post("/api/board", headers=HEADERS, json={
        "title": "x", "client_operation_id": "bad-date", "expected_collection_revision": 1, "due_at": "tomorrow"})).status_code == 422


async def test_a_finished_card_is_never_overdue(client: httpx.AsyncClient) -> None:
    task = await create(client, "Long gone", due_at="2026-01-05")
    assert listed(await board(client), task["id"])["overdue"] is True
    closed = await client.post(f"/api/board/{task['id']}/close", headers=HEADERS, json={
        "client_operation_id": "close-it", "expected_entity_revision": (await edit(client, task, priority=2)).json()["task"]["entity_revision"],
        "status": "dropped", "reason": "not wanted"})
    assert closed.status_code == 200, closed.text
    after = listed(await board(client), task["id"])
    assert after["status"] == "dropped" and after["due_at"] and after["overdue"] is False


async def test_the_agents_tools_carry_the_due_date(app: Any, tmp_path: Path) -> None:
    root = tmp_path / "bakery"
    root.mkdir()
    chat = await app.manager.create_session("a chat", project_id=None)
    hook = app.manager.service_hooks["board_commands"]
    revision = await hook("revision", session_id=chat.session.id)
    task = await hook("add", session_id=chat.session.id, client_operation_id="a-1", expected_collection_revision=revision,
                      title="Call the dentist", due_at=day(2))
    assert task["due_at"] == f"{day(2)}T23:59:59Z"
    task = await hook("update", session_id=chat.session.id, task_id=task["id"], client_operation_id="a-2",
                      expected_entity_revision=task["entity_revision"], due_at="")
    assert task["due_at"] is None
    with pytest.raises(ValueError, match="ISO"):
        await hook("update", session_id=chat.session.id, task_id=task["id"], client_operation_id="a-3",
                   expected_entity_revision=task["entity_revision"], due_at="whenever")


# -- the history ---------------------------------------------------------------------------------


async def test_a_cards_history_is_its_stored_events_oldest_first(client: httpx.AsyncClient) -> None:
    task = await create(client, "Plan the launch")
    other = await create(client, "Somebody else's card")
    await edit(client, task, due_at=day(5))
    await edit(client, task, status="blocked", note="waiting for the venue")
    await edit(client, task, due_at="")
    await edit(client, other, priority=1)

    got = await client.get(f"/api/board/{task['id']}/events", headers=HEADERS)
    assert got.status_code == 200, got.text
    events = got.json()
    assert [e["type"] for e in events] == ["task.created", "task.changed", "task.moved", "task.changed"]
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    created, due, moved, cleared = events
    assert created["text"] == "Created in todo" and created["actor"]["kind"] == "operator" and created["actor"]["id"] == "operator:1"
    assert due["due_at"] == f"{day(5)}T23:59:59Z" and due["text"].startswith("Due ")
    # One edit that moved the card is one line: the move, not "edited" and then the move.
    assert (moved["from"], moved["to"], moved["text"]) == ("todo", "blocked", "todo → blocked")
    assert cleared["due_at"] is None and cleared["text"] == "Due date cleared"
    assert all(e["at"].endswith("Z") for e in events)

    assert [e["type"] for e in (await client.get(f"/api/board/{task['id']}/events?limit=2", headers=HEADERS)).json()] == ["task.moved", "task.changed"]
    assert (await client.get("/api/board/nope/events", headers=HEADERS)).status_code == 404
    assert (await client.get(f"/api/board/{task['id']}/events")).status_code == 401
    assert (await client.get(f"/api/board/{task['id']}/events?limit=0", headers=HEADERS)).status_code == 422


async def test_a_removed_cards_history_stays_readable(client: httpx.AsyncClient) -> None:
    task = await create(client, "Short lived")
    revision = (await edit(client, task, priority=2)).json()["task"]["entity_revision"]
    gone = await client.delete(f"/api/board/{task['id']}", headers=HEADERS,
                               params={"client_operation_id": "remove-it", "expected_entity_revision": revision})
    assert gone.status_code == 200, gone.text
    assert task["id"] not in {t["id"] for t in await board(client)}
    events = (await client.get(f"/api/board/{task['id']}/events", headers=HEADERS)).json()
    assert events[0]["type"] == "task.created" and "task.moved" in {e["type"] for e in events}
    assert events[-1]["to"] == "dropped"


async def test_the_history_is_found_by_an_index_not_a_scan(app: Any) -> None:
    plan = " ".join(str(r["detail"]) for r in await app.db.fetchall(
        "EXPLAIN QUERY PLAN SELECT seq FROM app_events WHERE json_extract(payload_json, '$.task_id') = ? AND type LIKE 'task.%' ORDER BY seq DESC LIMIT 5",
        ("abc",)))
    assert "app_events_by_task" in plan and "SCAN" not in plan
