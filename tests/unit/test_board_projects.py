"""A project's board: who sees which tasks, what staff may do with theirs, and what waits on the operator.

Two scopes live side by side — an ordinary agent's family board and a project's board — so each test
pins both: the day an ordinary agent in a project finds the team's tasks on its own board, it starts
working on them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.board import Board
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications

HEADERS = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={}, guard=None)
    app.notifications = RecordingNotifications()
    yield app
    await manager.close()


async def test_direct_board_completion_requires_review_receipt(app: Any) -> None:
    board = Board(app)
    task = await board.add(title="A checked task", operator=True)
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(task["id"], status="done")
    assert (await board.get(task["id"]))["status"] == "todo"


async def test_project_task_count_and_depth_are_reserved_before_insert(app: Any, tmp_path: Path) -> None:
    team = await _team(app, tmp_path)
    board = Board(app)
    await app.db.execute("UPDATE planning_budgets SET max_tasks = 1,max_depth = 1 WHERE project_id = ?",
                         (team.project,))
    first = await board.add(title="First", project_id=team.project, operator=True)
    with pytest.raises(ValueError, match="1 open tasks"):
        await board.add(title="Second", project_id=team.project, operator=True)
    await app.db.execute("UPDATE planning_budgets SET max_tasks = 2 WHERE project_id = ?", (team.project,))
    with pytest.raises(ValueError, match="depth"):
        await board.add(title="Dependent", project_id=team.project, depends_on=[first["id"]], operator=True)
    assert (await app.db.fetchone("SELECT count(*) AS n FROM board_tasks WHERE project_id = ?", (team.project,)))["n"] == 1


async def test_finished_tasks_free_the_planning_budget(app: Any, tmp_path: Path) -> None:
    team = await _team(app, tmp_path)
    board = Board(app)
    await app.db.execute("UPDATE planning_budgets SET max_tasks = 1 WHERE project_id = ?", (team.project,))
    first = await board.add(title="First", project_id=team.project, operator=True)
    await app.db.execute("UPDATE board_tasks SET status = 'dropped' WHERE id = ?", (first["id"],))
    second = await board.add(title="Second", project_id=team.project, operator=True)
    await app.db.execute("UPDATE board_tasks SET status = 'done' WHERE id = ?", (second["id"],))
    assert (await board.add(title="Third", project_id=team.project, operator=True))["title"] == "Third"


async def _team(app: Any, tmp_path: Path) -> SimpleNamespace:
    """A project with an orchestrator, one staff member with a session, an ordinary agent in it and one outside."""
    manager: SessionManager = app.manager
    root = tmp_path / "bakery"
    (root / ".git").mkdir(parents=True)
    project = await manager.projects.create("Bakery", [str(root)])
    elsewhere = tmp_path / "garden"
    elsewhere.mkdir()
    other = await manager.projects.create("Garden", [str(elsewhere)])
    orchestrator = await manager.create_session("orchestrator", project_id=project.id)
    assert await manager.projects.set_orchestrator(project.id, expect="", value=orchestrator.session.id)
    ada = await manager.staff.hire(project.id, name="Ada", role="Menu page", isolation="shared")
    bo = await manager.staff.hire(project.id, name="Bo", role="Tests", isolation="shared")
    ada_session = await manager.create_session("Ada · menu", project_id=project.id)
    live = await manager.staff.claim_session(ada.id, kind="daedalus")
    await manager.staff.started(live.id, session_id=ada_session.session.id)
    agent = await manager.create_session("a chat in the project", project_id=project.id)
    outsider = await manager.create_session("a chat elsewhere", project_id=other.id)
    return SimpleNamespace(
        project=project.id, other=other.id, orchestrator=orchestrator.session.id, ada=ada, bo=bo, ada_session=ada_session.session.id,
        ada_live=live.id, agent=agent.session.id, outsider=outsider.session.id,
    )


async def test_each_session_sees_its_own_board(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    teams = await board.add(title="Checkout page", project_id=team.project, operator=True)
    own = await board.add(title="Look at the logs", session_id=team.agent)
    unaddressed = await board.add(title="Anyone: rotate the keys")
    outside = await board.add(title="Water the beds", session_id=team.outsider)

    # A task an agent adds is drawn on the board of the project it works in; the operator's
    # unaddressed one belongs to no project.
    assert own["project_id"] == team.project and outside["project_id"] == team.other and unaddressed["project_id"] is None

    ids = lambda tasks: {t["id"] for t in tasks}  # noqa: E731
    # The ordinary agent in the project keeps its family board: the team's task is not on it.
    assert ids(await board.list(None, actor=team.agent)) == {own["id"], unaddressed["id"]}
    with pytest.raises(KeyError):
        await board.get(teams["id"], actor=team.agent)
    # Staff and the orchestrator see the project's board, and nothing outside it.
    assert ids(await board.list(None, actor=team.ada_session)) == {teams["id"], own["id"]}
    assert ids(await board.list(None, actor=team.orchestrator)) == {teams["id"], own["id"]}
    # An agent of another project sees neither.
    assert ids(await board.list(None, actor=team.outsider)) == {outside["id"], unaddressed["id"]}
    # The operator sees everything, and one project's board on asking.
    assert ids(await board.list(None)) == {teams["id"], own["id"], unaddressed["id"], outside["id"]}
    assert ids(await board.list(None, project_id=team.project)) == {teams["id"], own["id"]}

    assert (await board.actor_of(team.ada_session)).kind == "staff"
    assert (await board.actor_of(team.orchestrator)).kind == "orchestrator"
    assert (await board.actor_of(team.agent)).kind == "agent"
    assert (await board.actor_of(None)).kind == "operator"


async def test_staff_move_only_their_own_tasks_and_never_to_done(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    mine = await board.add(title="Menu page", project_id=team.project, assignee_staff_id=team.ada.id, brief={"objective": "a menu", "done_when": "it renders"}, operator=True)
    theirs = await board.add(title="Test suite", project_id=team.project, assignee_staff_id=team.bo.id, operator=True)
    assert mine["assignee_staff_id"] == team.ada.id and mine["brief"] == {"objective": "a menu", "deliverable": "", "boundaries": "", "done_when": "it renders"}

    doing = await board.update(mine["id"], status="doing", actor=team.ada_session)
    assert doing["status"] == "doing" and doing["session_id"] == team.ada_session
    assert (await board.update(mine["id"], status="review", note="ready", actor=team.ada_session))["status"] == "review"
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(mine["id"], status="done", actor=team.ada_session)
    for status in ("dropped", "todo"):
        with pytest.raises(ValueError, match="takes a task to doing, review, blocked"):
            await board.update(mine["id"], status=status, actor=team.ada_session)
    with pytest.raises(ValueError, match="only the tasks assigned to them"):
        await board.update(theirs["id"], status="doing", actor=team.ada_session)
    with pytest.raises(ValueError, match="does not reassign"):
        await board.update(mine["id"], assignee_staff_id=team.bo.id, actor=team.ada_session)
    # A note on a colleague's task is not a move, and stays allowed.
    assert "seen it" in (await board.update(theirs["id"], note="seen it", actor=team.ada_session))["notes"]
    # An ordinary agent in the project cannot hand even its own task to the team; the orchestrator can.
    own = await board.add(title="my own", session_id=team.agent)
    with pytest.raises(ValueError, match="assigns tasks"):
        await board.update(own["id"], assignee_staff_id=team.ada.id, actor=team.agent)
    with pytest.raises(ValueError, match="assigns tasks"):
        await board.add(title="for Ada", session_id=team.agent, assignee_staff_id=team.ada.id)
    assert (await board.update(theirs["id"], assignee_staff_id=team.ada.id, actor=team.orchestrator))["assignee_staff_id"] == team.ada.id
    # The assignee must be on this project's team, and active.
    other = await app.manager.staff.hire(team.other, name="Cy", isolation="shared")
    with pytest.raises(ValueError, match="not on this project's team"):
        await board.update(theirs["id"], assignee_staff_id=other.id)
    await app.manager.staff.archive(team.bo.id)
    with pytest.raises(ValueError, match="dismissed"):
        await board.update(theirs["id"], assignee_staff_id=team.bo.id)
    assert (await board.update(theirs["id"], assignee_staff_id=""))["assignee_staff_id"] is None
    # A task outside every project has no team to be assigned to.
    loose = await board.add(title="loose")
    with pytest.raises(ValueError, match="project's board"):
        await board.update(loose["id"], assignee_staff_id=team.ada.id)


async def test_review_cannot_close_without_an_exact_result(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    task = await board.add(title="Checkout", project_id=team.project, assignee_staff_id=team.ada.id, operator=True)
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(task["id"], status="done")
    await board.update(task["id"], status="doing", actor=team.ada_session)
    await board.update(task["id"], status="review", actor=team.ada_session)
    await app.db.execute("UPDATE board_tasks SET branch = 'agent/ada/checkout', merge_state = 'proposed' WHERE id = ?", (task["id"],))
    # Neither an unmerged branch nor a manually marked merge proves an accepted result.
    for actor in (None, team.orchestrator):
        with pytest.raises(ValueError, match="exact-result acceptance"):
            await board.update(task["id"], status="done", actor=actor)
    await app.db.execute("UPDATE board_tasks SET merge_state = 'merged' WHERE id = ?", (task["id"],))
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(task["id"], status="done")

    plain = await board.add(title="Photos", project_id=team.project, operator=True)
    await board.update(plain["id"], status="review")
    with pytest.raises(ValueError, match="exact-result acceptance"):
        await board.update(plain["id"], status="done")


async def test_every_change_is_an_event_that_names_its_actor(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    seen: list[tuple[str, dict[str, Any], str | None, str | None]] = []
    async with app.manager.bus.subscribe(EventFilter(types=("task.",), project_id=team.project), name="test") as events:
        first = await board.add(title="Menu", project_id=team.project, assignee_staff_id=team.ada.id, operator=True)
        second = await board.add(title="Photos", project_id=team.project, depends_on=[first["id"]], operator=True)
        await board.update(first["id"], status="doing", actor=team.ada_session)
        await board.update(first["id"], status="review", actor=team.ada_session)
        await board.update(second["id"], assignee_staff_id=team.bo.id, actor=team.orchestrator)
        for _ in range(6):
            event = await anext(events)
            seen.append((event.type, event.payload, event.staff_id, event.project_id))
    kinds = [(kind, payload["task_id"], payload.get("actor"), payload.get("from"), payload.get("to")) for kind, payload, _, _ in seen]
    assert kinds == [
        ("task.created", first["id"], "operator", None, "todo"),
        ("task.assigned", first["id"], "operator", None, None),
        ("task.created", second["id"], "operator", None, "blocked"),
        ("task.moved", first["id"], "staff", "todo", "doing"),
        ("task.moved", first["id"], "staff", "doing", "review"),
        ("task.assigned", second["id"], "orchestrator", None, None),
    ]
    assert all(project == team.project for *_, project in seen)
    assert seen[1][2] == team.ada.id and seen[1][1]["assignee_staff_id"] == team.ada.id
    assert seen[-1][2] == team.bo.id


async def test_needs_you_is_the_open_requests_routed_to_the_operator(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    task = await board.add(title="Discount", project_id=team.project, assignee_staff_id=team.ada.id, operator=True)
    asks = app.manager.asks
    question = await asks.open(team.project, origin="staff", kind="question", text="SPRING10 before or after delivery?", routed_to="operator", staff_id=team.ada.id, staff_session_id=team.ada_live, task_id=task["id"])
    await asks.open(team.project, origin="staff", kind="permission", text="npm install", routed_to="orchestrator", staff_id=team.ada.id, staff_session_id=team.ada_live)
    mine = await asks.open(team.project, origin="orchestrator", kind="question", text="Ship on Friday?", routed_to="operator")

    needs = await board.needs_you(team.project)
    assert [n["id"] for n in needs] == [question.id, mine.id]
    first = needs[0]
    assert (first["staff"]["name"], first["task_title"], first["session_id"], first["short_id"]) == ("Ada", "Discount", team.ada_session, question.short_id)
    # The orchestrator's own question is answered in the orchestrator's session.
    assert needs[1]["staff"] is None and needs[1]["session_id"] == team.orchestrator

    listing = await board.project_board(team.project)
    assert listing["counts"]["needs_you"] == 2 and listing["counts"]["todo"] == 1
    card = listing["tasks"][0]
    assert card["assignee"]["name"] == "Ada" and card["assignee"]["session_id"] == team.ada_session
    assert [m["name"] for m in listing["staff"]] == ["Ada", "Bo"]

    # Nothing is stored on the task: answering the request is what ends it, wherever it was answered.
    assert await asks.resolve(question.id, "operator", {"text": "after"})
    assert [n["id"] for n in await board.needs_you(team.project)] == [mine.id]
    assert await board.needs_you(team.other) == []


async def test_stale_recovery_leaves_assigned_tasks_to_the_staff_runtime(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    assigned = await board.add(title="Long refactor", project_id=team.project, assignee_staff_id=team.ada.id, operator=True)
    loose = await board.add(title="Quick fix", session_id=team.agent)
    await board.update(assigned["id"], status="doing", actor=team.ada_session)
    await board.update(loose["id"], status="doing", actor=team.agent)
    await app.db.execute("UPDATE board_tasks SET heartbeat_at = '2000-01-01T00:00:00+00:00', updated_at = '2000-01-01T00:00:00+00:00'")
    assert await board.recover_stale() == [loose["id"]]
    assert (await board.get(assigned["id"]))["status"] == "doing"


async def test_dependencies_stay_on_one_board_and_never_loop(app: Any, tmp_path: Path) -> None:
    board = Board(app)
    team = await _team(app, tmp_path)
    a = await board.add(title="a", project_id=team.project, operator=True)
    b = await board.add(title="b", project_id=team.project, depends_on=[a["id"]], operator=True)
    elsewhere = await board.add(title="elsewhere", session_id=team.outsider)
    with pytest.raises(ValueError, match="another project's board"):
        await board.add(title="c", project_id=team.project, depends_on=[elsewhere["id"]], operator=True)
    with pytest.raises(ValueError, match="cannot depend on itself"):
        await board.update(a["id"], depends_on=[b["id"]])
    cleared = await board.update(b["id"], depends_on=[])
    assert cleared["status"] == "todo" and cleared["depends_on"] == []
    again = await board.update(b["id"], depends_on=[a["id"]])
    assert again["status"] == "blocked"


class FakeRuntime:
    def __init__(self) -> None:
        self.assigned: list[tuple[str, str, str]] = []

    async def assign(self, staff: Any, task: dict[str, Any], *, by: str) -> dict[str, Any]:
        self.assigned.append((staff.name, task["id"], by))
        return {"state": "queued", "position": 1}


async def test_the_board_over_http(app: Any, tmp_path: Path) -> None:
    team = await _team(app, tmp_path)
    board = Board(app)
    runtime = FakeRuntime()
    app.extensions.update(board=board, staff=runtime)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        base = f"/api/projects/{team.project}/board"
        assert (await client.get(base)).status_code == 401
        assert (await client.get("/api/projects/nope/board", headers=HEADERS)).status_code == 404

        collection = (await client.get(f"/api/control/revisions?project={team.project}", headers=HEADERS)).json()["collection_revision"]
        create = {"title": "Checkout", "brief": {"objective": "pay by card", "done_when": "a test order goes through"},
                  "priority": 2, "client_operation_id": "checkout-create", "expected_collection_revision": collection}
        made = await client.post(base, headers=HEADERS, json=create)
        assert made.status_code == 201, made.text
        task = made.json()["task"]
        assert task["project_id"] == team.project and task["brief"]["objective"] == "pay by card"
        assert runtime.assigned == []
        assert (await client.post(base, headers=HEADERS, json=create)).json()["command"] == made.json()["command"]
        invalid = {**create, "client_operation_id": "invalid", "brief": {"surprise": "1"}}
        assert (await client.post(base, headers=HEADERS, json=invalid)).status_code == 422
        unknown = {**create, "client_operation_id": "unknown-worker", "expected_collection_revision": collection + 1,
                   "assignee_staff_id": "nobody"}
        assert (await client.post(base, headers=HEADERS, json=unknown)).status_code == 409

        # Saving an assignee edits metadata; only the separate launch command contacts its runtime.
        edit = {"assignee_staff_id": team.ada.id, "brief": {"boundaries": "only the checkout folder"},
                "client_operation_id": "checkout-edit", "expected_entity_revision": task["entity_revision"]}
        put = await client.put(f"/api/board/{task['id']}", headers=HEADERS, json=edit)
        assert put.status_code == 200, put.text
        task = put.json()["task"]
        assert runtime.assigned == []
        assert task["brief"] == {"objective": "pay by card", "deliverable": "", "boundaries": "only the checkout folder", "done_when": "a test order goes through"}
        assert (await client.put(f"/api/board/{task['id']}", headers=HEADERS, json=edit)).json()["command"] == put.json()["command"]
        listing = (await client.get(base, headers=HEADERS)).json()
        assert [t["id"] for t in listing["tasks"]] == [task["id"]] and listing["tasks"][0]["assignee"]["name"] == "Ada"
        assert listing["needs_you"] == [] and listing["counts"]["todo"] == 1
        for status in ("done", "review"):
            refused = await client.put(f"/api/board/{task['id']}", headers=HEADERS,
                                      json={"status": status, "expected_entity_revision": task["entity_revision"],
                                            "client_operation_id": f"direct-{status}"})
            assert refused.status_code == 409

        await board.add(title="elsewhere", session_id=team.outsider)
        lens = (await client.get(f"/api/board?include_done=1&project={team.project}", headers=HEADERS)).json()
        assert [t["title"] for t in lens] == ["Checkout"]
        assert len((await client.get("/api/board?include_done=1", headers=HEADERS)).json()) == 2
        archived = await client.delete(f"/api/board/{task['id']}", headers=HEADERS,
                                       params={"client_operation_id": "checkout-archive",
                                               "expected_entity_revision": task["entity_revision"]})
        assert archived.status_code == 200
        assert await app.db.fetchone("SELECT id FROM board_tasks WHERE id = ?", (task["id"],))
        assert await app.db.fetchone("SELECT task_id FROM task_contract_versions WHERE task_id = ?", (task["id"],))
