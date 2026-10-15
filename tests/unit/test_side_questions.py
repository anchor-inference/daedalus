"""Side questions: an ordinary session leaves the operator a question that does not stop it, takes back
its own when it settles one itself, and gets the operator's answers — with any files — as one message."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions import questions, side_questions
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.staff import AsksStore, StaffError
from tests.support.waiting import SETTLE, until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "thanks"}] * 4))
    application = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, notifications=None)
    await side_questions.install(application)  # type: ignore[arg-type]
    yield application
    await manager.close()


async def ask(app: Any, session_id: str, question: str, **kwargs: Any) -> str:
    return await app.manager.service_hooks["side_questions"]("ask", session_id=session_id, question=question, **kwargs)


async def side_events(manager: SessionManager, session_id: str) -> list[dict[str, Any]]:
    from daedalus.host.events import EventFilter

    return [e.payload for e in await manager.bus.replay(0, EventFilter(types=("ask.side",), session_id=session_id), limit=500)]


async def settled(manager: SessionManager) -> None:
    """Every run the answers started has ended, so the manager closes over nothing still writing."""
    async def idle() -> bool:
        return not any(state.running for state in manager._states.values())

    await until_await(idle)


# -- who has them ------------------------------------------------------------------------------


async def test_every_working_session_has_them_and_no_other_role_does(app: Any) -> None:
    manager: SessionManager = app.manager
    work = await manager.create_session("work")
    assert {"SideQuestion", "DropSideQuestion"}.isdisjoint(manager.blocked_tools_for(work))
    assert side_questions.may_ask(manager, work)
    roles = {
        "companion": {"companion": True},
        "staff": {"staff_session_id": "ss-1"},
        "orchestrator": {"orchestrator_of": "p-1"},
        "subagent": {"subagent_of": work.session.id},
    }
    for role, metadata in roles.items():
        state = await manager.create_session(role, metadata=metadata)
        assert {"SideQuestion", "DropSideQuestion"} <= manager.blocked_tools_for(state), role
        assert not side_questions.may_ask(manager, state), role
        with pytest.raises(side_questions.Refused, match="does not leave side questions"):
            await ask(app, state.session.id, "Which colour?")
    dispatcher = await manager.create_session("main", metadata={"dispatcher": True})
    assert not side_questions.may_ask(manager, dispatcher)
    # Plan mode reads and asks; leaving a question is asking.
    await manager.set_mode(work.session.id, "plan")
    assert "SideQuestion" not in manager.blocked_tools_for(work)


async def test_the_prompt_teaches_the_two_ways_of_asking_only_where_the_tool_is_admitted() -> None:
    assert prompts.SIDE_QUESTIONS in "".join(prompts.tool_sections({"SideQuestion"}))
    assert prompts.SIDE_QUESTIONS not in "".join(prompts.tool_sections(set(), admitted=set()))
    assert "AskUser stops the run" in prompts.SIDE_QUESTIONS and "DropSideQuestion" in prompts.SIDE_QUESTIONS


# -- asking and taking back ----------------------------------------------------------------------


async def test_a_question_is_left_at_once_listed_bounded_and_never_asked_twice(app: Any) -> None:
    manager: SessionManager = app.manager
    work = await manager.create_session("work")
    sid = work.session.id
    said = await ask(app, sid, "Which port should the dev server use?", why="I will use 8080 until told otherwise", options=["8080", "3000", "8080"])
    [first] = await manager.asks.of_session(sid)
    assert said.startswith(f"left for the operator as [{first.short_id}]") and "Keep working" in said
    assert first.origin == "session" and first.session_id == sid and first.project_id is None and first.routed_to == "operator"
    assert first.heading == "Which port should the dev server use?" and first.detail["options"] == ["8080", "3000"]
    assert (await side_events(manager, sid))[0] == {"change": "opened", "ask_ids": [first.id]}
    # No project's list, count or orchestrator reads it.
    assert await manager.asks.open_counts("operator") in ({}, {None: 1})
    assert first.id not in [q["id"] for q in await questions.waiting(app)]

    with pytest.raises(side_questions.Refused, match=f"already waiting as \\[{first.short_id}\\]"):
        await ask(app, sid, "  which port should the dev server   use? ")
    for i in range(side_questions.OPEN_MAX - 1):
        await ask(app, sid, f"Question number {i}?")
    with pytest.raises(side_questions.Refused, match="already wait for the operator .*DropSideQuestion"):
        await ask(app, sid, "One too many?")
    with pytest.raises(side_questions.Refused, match="needs the question"):
        await ask(app, sid, "  ")

    # The turn context names what is open, so a session past a compaction knows what it asked.
    note = await manager._side_questions_note(sid)
    assert note.startswith("- Your side questions still waiting for the operator: [") and first.short_id in note
    assert await manager._side_questions_note((await manager.create_session("other")).session.id) == ""


async def test_a_session_drops_only_its_own_and_says_what_became_of_the_rest(app: Any) -> None:
    manager: SessionManager = app.manager
    mine = (await manager.create_session("mine")).session.id
    theirs = (await manager.create_session("theirs")).session.id
    await ask(app, mine, "Keep the old logo?")
    await ask(app, mine, "Tabs or spaces?")
    await ask(app, theirs, "Rename the repo?")
    logo, tabs = await manager.asks.of_session(mine)
    [other] = await manager.asks.of_session(theirs)
    await questions.answer(app, [{"ask_id": tabs.id, "text": "Spaces"}], project_id=None, via="app", session_id=mine)

    drop = manager.service_hooks["side_questions"]
    said = await drop("withdraw", session_id=mine, ids=[logo.short_id, other.short_id, tabs.short_id], reason="found it in the README")
    assert said.startswith(f"dropped [{logo.short_id}]")
    assert f"[{other.short_id}]: not one of your side questions" in said
    assert f"[{tabs.short_id}]: already answered by the operator: Spaces" in said
    closed = await manager.asks.get(logo.id)
    assert closed is not None and closed.resolved_by == "system" and closed.resolution == {"closed": "found it in the README", "via": "withdrawn", "by": "agent"}
    assert (await manager.asks.get(other.id)).open  # type: ignore[union-attr]
    assert {"change": "withdrawn", "ask_ids": [logo.id], "reason": "found it in the README"} in await side_events(manager, mine)
    assert [q["id"] for q in await questions.waiting(app, session_id=mine)] == []

    with pytest.raises(side_questions.Refused, match="nothing dropped: .*already dropped"):
        await drop("withdraw", session_id=mine, ids=[logo.short_id], reason="again")
    with pytest.raises(side_questions.Refused, match="say why"):
        await drop("withdraw", session_id=theirs, ids=[other.short_id], reason=" ")


async def test_the_store_keeps_side_questions_apart_from_projects(db: Database) -> None:
    asks = AsksStore(db)
    with pytest.raises(StaffError, match="names the session"):
        await asks.open(None, origin="session", kind="question", text="?", routed_to="operator")
    with pytest.raises(StaffError, match="names the session"):
        await asks.open("p", origin="orchestrator", kind="question", text="?", routed_to="operator", session_id="s")
    with pytest.raises(StaffError, match="only the confirmation of a new project and a side question"):
        await asks.open(None, origin="orchestrator", kind="question", text="?", routed_to="operator")


# -- answers reach the session -------------------------------------------------------------------


def user_texts(messages: list[Any], origin: str) -> list[str]:
    return [
        prompts.without_turn_context("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)))
        for m in messages if m.role is MessageRole.user and m.metadata.get("daedalus.origin") == origin
    ]


async def test_answers_start_a_turn_of_an_idle_session_with_each_question_quoted_and_files_ingested(app: Any, tmp_path: Path) -> None:
    from daedalus.host.session_runner import Attachment

    manager: SessionManager = app.manager
    state = await manager.create_session("idle")
    sid = state.session.id
    await ask(app, sid, "Which port should the dev server use?", options=["8080", "3000"])
    await ask(app, sid, "What should the screenshot look like?")
    port, look = await manager.asks.of_session(sid)
    shot = tmp_path / "staged.part"
    shot.write_bytes(PNG)
    result = await questions.answer(app, [
        {"ask_id": port.id, "selected": ["3000"], "note": "8080 is taken"},
        {"ask_id": look.id, "files": [0]},
    ], project_id=None, via="app", session_id=sid, uploads=[Attachment(path=shot, name="screenshot.png", mime_type="image/png")])
    assert [(r["state"], r["delivered"], r["error"]) for r in result["results"]] == [("answered", True, ""), ("answered", True, "")]
    assert (await manager.asks.get(look.id)).resolution["files"] == ["screenshot.png"]  # type: ignore[union-attr]

    async def recorded() -> list[str]:
        return user_texts(await manager.sessions.list_transcript(sid), side_questions.ORIGIN)

    await until_await(recorded)
    [text] = await recorded()
    assert text.startswith("Answers to your side questions")
    assert f"[{port.short_id}] Which port should the dev server use?\nAnswer: 3000 — 8080 is taken" in text
    assert f"[{look.short_id}] What should the screenshot look like?\nAnswer: {questions.FILES_ONLY}\nAttached with this answer: screenshot.png" in text
    assert "Attached files:" in text and "inbox/screenshot.png" in text
    assert (state.workspace / "inbox" / "screenshot.png").read_bytes() == PNG
    [message] = [m for m in await manager.sessions.list_transcript(sid) if m.metadata.get("daedalus.origin") == side_questions.ORIGIN]
    assert message.metadata.get("image_refs"), "the model is shown the image, not only its path"
    assert {"change": "answered", "ask_ids": [port.id, look.id]} in await side_events(manager, sid)
    await settled(manager)


async def test_answers_steer_a_running_session_and_queue_behind_a_blocking_question(app: Any) -> None:
    manager: SessionManager = app.manager
    state = await manager.create_session("busy")
    sid = state.session.id
    await ask(app, sid, "Keep the old logo?")
    [logo] = await manager.asks.of_session(sid)
    state.task = asyncio.create_task(asyncio.sleep(SETTLE))
    try:
        await questions.answer(app, [{"ask_id": logo.id, "text": "Keep it"}], project_id=None, via="app", session_id=sid)
        queued = await manager.live.load(sid)
        assert [item["origin"] for item in queued["steer"]] == [side_questions.ORIGIN]
        assert "Answer: Keep it" in queued["steer"][0]["text"]
    finally:
        state.task.cancel()


async def test_an_answer_that_cannot_reach_the_session_leaves_its_question_waiting(app: Any) -> None:
    manager: SessionManager = app.manager
    sid = (await manager.create_session("over budget")).session.id
    await ask(app, sid, "Keep the old logo?")
    [logo] = await manager.asks.of_session(sid)

    async def refuse(*_: Any, **__: Any) -> str:
        raise RuntimeError("daily budget exceeded")

    manager.submit = refuse  # type: ignore[method-assign]
    [result] = (await questions.answer(app, [{"ask_id": logo.id, "text": "Keep it"}], project_id=None, via="app", session_id=sid))["results"]
    assert result["state"] == "answered" and result["delivered"] is False
    assert result["error"] == "daily budget exceeded; the question waits again"
    again = await manager.asks.get(logo.id)
    assert again is not None and again.open and again.resolution == {}
    assert [q["id"] for q in await questions.waiting(app, session_id=sid)] == [logo.id]
    assert (await side_events(manager, sid))[-1] == {"change": "opened", "ask_ids": [logo.id]}


async def test_a_list_answers_only_its_own_session_and_the_operator_can_put_one_aside(app: Any) -> None:
    manager: SessionManager = app.manager
    mine = (await manager.create_session("mine")).session.id
    theirs = (await manager.create_session("theirs")).session.id
    await ask(app, theirs, "Rename the repo?")
    await ask(app, mine, "Delete the old script?")
    [other] = await manager.asks.of_session(theirs)
    [script] = await manager.asks.of_session(mine)
    refused = await questions.answer(app, [{"ask_id": other.id, "text": "No"}], project_id=None, via="app", session_id=mine)
    assert refused["results"][0]["state"] == "refused" and (await manager.asks.get(other.id)).open  # type: ignore[union-attr]

    closed = await side_questions.dismiss(app, session_id=mine, ask_id=script.id)
    assert closed.resolved_by == "operator" and closed.resolution["dismissed"] is True
    assert {"change": "dismissed", "ask_ids": [script.id]} in await side_events(manager, mine)
    assert user_texts(await manager.sessions.list_transcript(mine), side_questions.ORIGIN) == [], "a question put aside wakes nobody"
    with pytest.raises(side_questions.Refused, match="put it aside"):
        await side_questions.dismiss(app, session_id=mine, ask_id=script.id)
    with pytest.raises(KeyError):
        await side_questions.dismiss(app, session_id=mine, ask_id=other.id)


async def test_the_routes_list_answer_with_files_and_dismiss(app: Any) -> None:
    manager: SessionManager = app.manager
    sid = (await manager.create_session("work")).session.id
    await ask(app, sid, "Which logo?", options=["Old", "New"])
    await ask(app, sid, "Which font?")
    await ask(app, sid, "Which colour?")
    logo, font, colour = await manager.asks.of_session(sid)
    api = build_app(app, "tok")
    headers = {"X-Daedalus-Token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        listed = (await client.get(f"/api/questions?session={sid}", headers=headers)).json()["questions"]
        assert [q["heading"] for q in listed] == ["Which logo?", "Which font?", "Which colour?"]
        assert listed[0]["options"] == ["Old", "New"] and listed[0]["files"] is True and listed[0]["asker"] == "agent" and listed[0]["session_id"] == sid
        assert (await client.get("/api/questions?session=nope", headers=headers)).status_code == 404

        items = [{"ask_id": logo.id, "selected": ["New"], "files": [0, 1]}, {"ask_id": font.id, "text": "Inter"}]
        sent = await client.post(
            f"/api/sessions/{sid}/asks/answer", headers=headers,
            data={"items": json.dumps(items)},
            files=[("files", ("shot.png", PNG, "image/png")), ("files", ("C:\\Users\\someone\\notes.txt", b"hello", "text/plain"))],
        )
        assert sent.status_code == 200, sent.text
        assert [r["state"] for r in sent.json()["results"]] == ["answered", "answered"]
        assert (await manager.asks.get(logo.id)).resolution["files"] == ["shot.png", "notes.txt"]  # type: ignore[union-attr]
        assert list((app.settings.state_dir / "upload-staging").glob("*.part")) == [], "the staged files go once handed on"
        # A file the batch does not have, or one claimed twice, refuses the send.
        bad = await client.post(f"/api/sessions/{sid}/asks/answer", headers=headers, data={"items": json.dumps([{"ask_id": colour.id, "files": [3]}])},
                                files=[("files", ("a.txt", b"x", "text/plain"))])
        assert bad.status_code == 400
        assert (await client.post(f"/api/sessions/{sid}/asks/answer", headers=headers, json=[{"ask_id": colour.id, "colour": "red"}])).status_code == 422

        gone = await client.post(f"/api/sessions/{sid}/asks/{colour.id}/dismiss", headers=headers)
        assert gone.status_code == 200 and gone.json()["ask"]["resolution"]["dismissed"] is True
        assert (await client.post(f"/api/sessions/{sid}/asks/{colour.id}/dismiss", headers=headers)).status_code == 409
        assert (await client.get(f"/api/questions?session={sid}", headers=headers)).json()["questions"] == []
    await settled(manager)


async def test_a_deleted_session_takes_its_side_questions_with_it(app: Any) -> None:
    manager: SessionManager = app.manager
    sid = (await manager.create_session("short-lived")).session.id
    await ask(app, sid, "Is this needed?")
    await manager.delete_session(sid, delete_workspace=False)
    assert await manager.db.fetchall("SELECT id FROM asks WHERE session_id = ?", (sid,)) == []


# -- the migration -------------------------------------------------------------------------------


OLD_ASKS = """
DROP TABLE asks;
CREATE TABLE asks (
    id TEXT PRIMARY KEY,
    short_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ('staff', 'orchestrator', 'dispatcher')),
    kind TEXT NOT NULL CHECK (kind IN ('question', 'permission', 'folder', 'project')),
    staff_id TEXT REFERENCES staff(id) ON DELETE CASCADE,
    staff_session_id TEXT REFERENCES staff_sessions(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES board_tasks(id) ON DELETE SET NULL,
    request_ref TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    routed_to TEXT NOT NULL CHECK (routed_to IN ('orchestrator', 'operator')),
    suggestion TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    routed_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT CHECK (resolved_by IN ('orchestrator', 'operator', 'staff', 'system')),
    resolution_json TEXT NOT NULL DEFAULT '{}',
    dispatch_id TEXT REFERENCES dispatches(id) ON DELETE SET NULL
, title TEXT NOT NULL DEFAULT '', origin_contract_revision INTEGER, answered_contract_revision INTEGER);
CREATE INDEX asks_open ON asks(project_id, resolved_at, routed_to);
CREATE UNIQUE INDEX asks_short_id_open ON asks(short_id) WHERE resolved_at IS NULL;
CREATE INDEX asks_by_dispatch ON asks(dispatch_id) WHERE dispatch_id IS NOT NULL;
"""
"""The requests table as the schema before side questions left it."""


async def test_the_migration_keeps_every_request_and_lets_a_session_ask(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db = Database(path, local_env="container")
    await db.open()
    await db.close()
    raw = sqlite3.connect(path)
    raw.executescript(OLD_ASKS)
    # The version just before the rebuild of the asks, found by its script: later migrations are
    # appended after it, and "the last one" stopped naming it the day the first of them was.
    rebuild = next(i for i, m in enumerate(MIGRATIONS) if isinstance(m, tuple) and "asks_rebuilt" in m[0])
    raw.execute("UPDATE schema_version SET version = ?", (rebuild,))
    raw.execute("INSERT INTO projects(id, name, created_at) VALUES ('p1', 'Bakery', '2026-09-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO asks(id, short_id, project_id, origin, kind, text, detail_json, routed_to, created_at, routed_at, resolved_at, resolved_by, title) "
        "VALUES ('ask-1', 'qabcde', 'p1', 'orchestrator', 'question', 'Postgres?', '{}', 'operator', '2026-09-01T00:00:00+00:00', "
        "'2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00', 'operator', 'Database')"
    )
    raw.commit()
    raw.close()
    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        old = await AsksStore(db).get("ask-1")
        assert old is not None and old.title == "Database" and old.session_id is None and old.resolved_by == "operator"
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, project_id) VALUES ('s1', 't', 'x', '2026-09-01', '2026-09-01', 'p1')")
        made = await AsksStore(db).open(None, origin="session", kind="question", text="Which port?", routed_to="operator", session_id="s1")
        assert [a.id for a in await AsksStore(db).of_session("s1")] == [made.id]
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
    db = Database(path, local_env="container")
    await db.open()
    try:
        assert len(await AsksStore(db).of_session("s1")) == 1, "a second open finds nothing to do"
    finally:
        await db.close()
