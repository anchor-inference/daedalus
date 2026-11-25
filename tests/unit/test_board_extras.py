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
from daedalus.extensions.orchestrator_loops import OpenResults
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import ControlDenied, Principal
from daedalus.stores.database import MIGRATIONS, Database
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


# -- the operator as an assignee -----------------------------------------------------------------


async def test_a_card_the_operator_keeps_is_theirs_alone(client: httpx.AsyncClient) -> None:
    task = await create(client, "Renew the domain", assignee_operator=True)
    assert task["assignee_operator"] is True
    mine = listed(await board(client), task["id"])
    assert mine["assignee"] == {"kind": "operator"} and mine["needs_you"] == "assigned"

    # One owner or none.
    both = await client.post("/api/board", headers=HEADERS, json={
        "title": "x", "client_operation_id": "both", "expected_collection_revision": 1,
        "assignee_operator": True, "assignee_staff_id": "someone"})
    assert both.status_code in (409, 422)

    # The operator starts it themselves: no launch, no worker, so the plain move is theirs to make.
    started = await edit(client, task, status="doing")
    assert started.status_code == 200, started.text
    assert started.json()["task"]["status"] == "doing"
    assert listed(await board(client), task["id"])["needs_you"] == "assigned"

    # Finishing is their Close; afterwards it waits on nobody.
    revision = started.json()["task"]["entity_revision"]
    closed = await client.post(f"/api/board/{task['id']}/close", headers=HEADERS, json={
        "client_operation_id": "done-it", "expected_entity_revision": revision, "status": "done", "reason": "renewed"})
    assert closed.status_code == 200, closed.text
    finished = listed(await board(client), task["id"])
    assert finished["status"] == "done" and finished["needs_you"] is None and finished["assignee"] == {"kind": "operator"}


async def test_the_operator_hands_a_card_back_and_the_flag_goes(client: httpx.AsyncClient) -> None:
    task = await create(client, "Sort the post")
    assert task["assignee_operator"] is False and listed(await board(client), task["id"])["assignee"] is None
    kept = (await edit(client, task, assignee_operator=True)).json()["task"]
    assert kept["assignee_operator"] is True
    freed = (await edit(client, task, assignee_operator=False)).json()["task"]
    assert freed["assignee_operator"] is False and listed(await board(client), task["id"])["needs_you"] is None
    again = (await edit(client, task, assignee_operator=True)).json()["task"]
    cleared = (await edit(client, task, assignee_staff_id="")).json()["task"]  # "take it off whoever had it"
    assert again["assignee_operator"] is True and cleared["assignee_operator"] is False


async def test_no_agent_takes_a_card_from_the_operator_or_keeps_one_for_them(app: Any, client: httpx.AsyncClient, tmp_path: Path) -> None:
    from daedalus.extensions.orchestrator_domain import DomainConflict

    project, task = await project_card(app, client, tmp_path, assignee_operator=True)
    chat = await app.manager.create_session("a chat in the project", project_id=project)
    hook = app.manager.service_hooks["board_commands"]
    revision = await hook("revision", session_id=chat.session.id)
    with pytest.raises(ControlDenied, match="only the operator"):
        await hook("add", session_id=chat.session.id, client_operation_id="agent-keeps",
                   expected_collection_revision=revision, title="Mine now", assignee_operator=True)
    # Not by editing the card either: handing it on, or claiming it into work as the chat's own.
    await app.db.execute("UPDATE board_tasks SET origin_session_id = ? WHERE id = ?", (chat.session.id, task["id"]))
    current = await app.db.fetchone("SELECT entity_revision FROM board_tasks WHERE id = ?", (task["id"],))
    with pytest.raises(DomainConflict, match="the operator's"):
        await hook("update", session_id=chat.session.id, task_id=task["id"], client_operation_id="agent-frees",
                   expected_entity_revision=current["entity_revision"], assignee_operator=False)
    with pytest.raises(DomainConflict, match="the operator's"):
        await hook("update", session_id=chat.session.id, task_id=task["id"], client_operation_id="agent-claims",
                   expected_entity_revision=current["entity_revision"], status="doing")
    assert (await app.db.fetchone("SELECT assignee_operator FROM board_tasks WHERE id = ?", (task["id"],)))["assignee_operator"] == 1


async def test_the_machinery_that_works_unassigned_cards_leaves_the_operators_alone(app: Any, client: httpx.AsyncClient, tmp_path: Path) -> None:
    from daedalus.extensions.task_launch import queue_launch

    project, task = await project_card(app, client, tmp_path, assignee_operator=True)
    # A launch is refused outright, before it can write a worker over the operator's name.
    with pytest.raises(Exception, match="operator's own"):
        await queue_launch(app, task["id"], Principal("operator:1", "operator"), staff_id="nobody",
                           client_operation_id="launch", expected_entity_revision=task["entity_revision"])
    # The staleness sweep hands back a doing card nobody holds; the operator's is not nobody's.
    started = (await edit(client, task, status="doing")).json()["task"]
    await app.db.execute("UPDATE board_tasks SET heartbeat_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (task["id"],))
    assert await app.extensions["board"].recover_stale() == []
    assert (await app.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (started["id"],)))["status"] == "doing"
    # The coordinator's "nobody has this card" nudge does not count it as unowned.
    await edit(client, task, status="todo")
    loops = OpenResults(SimpleNamespace(manager=app.manager))  # type: ignore[arg-type]
    assert await loops.unowned(project, "9999-12-31") == []
    await edit(client, task, assignee_operator=False)
    assert await loops.unowned(project, "9999-12-31") == [task["id"]]
    # The database itself refuses a staff member beside the operator.
    with pytest.raises(Exception, match="CHECK"):
        await app.db.execute("UPDATE board_tasks SET assignee_operator = 1, assignee_staff_id = 'x' WHERE id = ?", (task["id"],))


async def test_the_history_says_when_a_card_was_kept_for_the_operator(client: httpx.AsyncClient) -> None:
    task = await create(client, "Sort the post")
    await edit(client, task, assignee_operator=True)
    events = (await client.get(f"/api/board/{task['id']}/events", headers=HEADERS)).json()
    assert [e["type"] for e in events] == ["task.created", "task.assigned"]
    assert events[1]["assignee"] == {"kind": "operator"} and events[1]["text"] == "Kept by the operator"


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


# -- the migration -------------------------------------------------------------------------------


async def test_the_migration_adds_the_columns_and_the_index_and_keeps_every_row(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    for n in range(3):
        await db.execute(
            "INSERT INTO board_tasks(id, title, status, priority, created_at, updated_at) VALUES (?, ?, 'todo', 3, 't', 't')",
            (f"t{n}", f"task {n}"))
        await db.execute("INSERT INTO app_events(at, type, payload_json) VALUES ('2026-01-01T00:00:00.000Z', 'task.created', ?)",
                         (f'{{"task_id": "t{n}", "title": "task {n}"}}',))
    await db.execute("DROP INDEX app_events_by_task")
    await db.execute("ALTER TABLE board_tasks DROP COLUMN assignee_operator")
    await db.execute("ALTER TABLE board_tasks DROP COLUMN due_at")
    await db.execute(f"UPDATE schema_version SET version = {len(MIGRATIONS) - 1}")
    await db.close()

    for _ in range(2):  # the second start runs nothing and breaks nothing
        again = Database(path, local_env="container")
        await again.open()
        try:
            assert int((await again.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
            rows = await again.fetchall("SELECT id, due_at, assignee_operator FROM board_tasks ORDER BY id")
            assert [(r["id"], r["due_at"], r["assignee_operator"]) for r in rows] == [("t0", None, 0), ("t1", None, 0), ("t2", None, 0)]
            assert (await again.fetchone("SELECT count(*) AS n FROM app_events"))["n"] == 3
            assert [r[0] for r in await again.fetchall("PRAGMA integrity_check")] == ["ok"]
            assert await again.fetchall("PRAGMA foreign_key_check") == []
            assert len(await again.fetchall("SELECT 1 FROM sqlite_master WHERE name = 'app_events_by_task'")) == 1
        finally:
            await again.close()
