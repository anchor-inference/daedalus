"""A card can be made in the column it is wanted in, where a move would take it right after.

The board's "+" sat on To do alone because the create route made every card there (or in Blocked,
when it waited on something). A card made somewhere else must still be one the board's own rules
allow: the refusal is the one the same move gets, so the app shows one wording for both.
"""

# ruff: noqa: F811 — the board fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.unit.test_board_extras import HEADERS, app, client, create, edit  # noqa: F401 — fixtures by name


async def made(client: httpx.AsyncClient, title: str, **fields: Any) -> httpx.Response:
    revision = (await client.get("/api/control/revisions", headers=HEADERS)).json()["collection_revision"]
    return await client.post("/api/board", headers=HEADERS, json={
        "title": title, "client_operation_id": f"create-{title}", "expected_collection_revision": revision, **fields})


async def test_without_a_status_the_dependencies_decide_as_before(client: httpx.AsyncClient) -> None:
    first = await create(client, "Book the venue")
    assert first["status"] == "todo"
    assert (await create(client, "Print the tickets", depends_on=[first["id"]]))["status"] == "blocked"


async def test_to_do_and_blocked_are_made_where_asked(client: httpx.AsyncClient) -> None:
    first = await create(client, "Book the venue", status="todo")
    assert first["status"] == "todo"
    # Blocked by hand, for a reason no dependency names, and blocked on a dependency, alike.
    assert (await create(client, "Wait for the quote", status="blocked"))["status"] == "blocked"
    assert (await create(client, "Print the tickets", status="blocked", depends_on=[first["id"]]))["status"] == "blocked"
    # The column it was made in is the first line of its history.
    events = (await client.get(f"/api/board/{first['id']}/events", headers=HEADERS)).json()
    assert [(e["type"], e["to"]) for e in events] == [("task.created", "todo")]


async def test_to_do_is_refused_while_a_dependency_is_open(client: httpx.AsyncClient) -> None:
    first = await create(client, "Book the venue")
    refused = await made(client, "Print the tickets", status="todo", depends_on=[first["id"]])
    assert refused.status_code == 409 and refused.json()["detail"] == "this task still has unfinished dependencies"


async def test_doing_is_the_operators_own_card_only_as_the_move_is(client: httpx.AsyncClient) -> None:
    mine = await create(client, "Renew the domain", status="doing", assignee_operator=True)
    assert mine["status"] == "doing" and mine["assignee_operator"] is True
    # The same refusal a move to doing gets for a card nobody keeps.
    unowned = await made(client, "Somebody's work", status="doing")
    later = await create(client, "Somebody's other work")
    moved = await edit(client, later, status="doing")
    assert unowned.status_code == moved.status_code == 409
    assert unowned.json()["detail"] == moved.json()["detail"] == "work, review and completion require their dedicated result or launch command"
    first = await create(client, "Book the venue")
    waiting = await made(client, "Pay the venue", status="doing", assignee_operator=True, depends_on=[first["id"]])
    assert waiting.status_code == 409 and waiting.json()["detail"] == "this task still has unfinished dependencies"


@pytest.mark.parametrize("status", ["review", "done"])
async def test_review_and_done_need_a_result_and_are_refused_as_the_move_is(client: httpx.AsyncClient, status: str) -> None:
    refused = await made(client, f"Already {status}", status=status, assignee_operator=True)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "work, review and completion require their dedicated result or launch command"


async def test_dropped_and_unknown_columns_are_refused(client: httpx.AsyncClient) -> None:
    dropped = await made(client, "Never mind", status="dropped")
    assert dropped.status_code == 409 and "starts open" in dropped.json()["detail"]
    assert (await made(client, "Sideways", status="sideways")).status_code == 422
    listing = (await client.get("/api/board?include_done=1", headers=HEADERS)).json()
    assert not {"Never mind", "Sideways", "Already review"} & {t["title"] for t in listing}


async def test_a_project_board_takes_the_status_too(app: Any, client: httpx.AsyncClient, tmp_path: Any) -> None:
    root = tmp_path / "bakery"
    root.mkdir()
    project = await app.manager.projects.create("Bakery", [str(root)])
    revision = (await client.get(f"/api/control/revisions?project={project.id}", headers=HEADERS)).json()["collection_revision"]
    made_there = await client.post(f"/api/projects/{project.id}/board", headers=HEADERS, json={
        "title": "Ask the supplier", "client_operation_id": "blocked-there", "expected_collection_revision": revision, "status": "blocked"})
    assert made_there.status_code == 201, made_there.text
    assert made_there.json()["task"]["status"] == "blocked"
