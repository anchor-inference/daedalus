"""The operator's list of questions: an orchestrator asks several at once and takes back what no longer
matters, the operator answers some of them together, and the orchestrator hears the answers once."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions import questions
from daedalus.extensions.api import build_app
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.staff import AsksStore
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, events, events_messages, rig

THREE = [
    {"title": "Database", "text": "Orders need concurrent writes. Which database?", "options": ["Postgres", "SQLite"]},
    {"title": "Payment providers", "text": "Which providers at launch?", "options": ["Stripe", "PayPal", "Cash"], "multi": True},
    {"title": "Launch day", "text": "When do we open the shop?", "urgent": True},
]


async def ask_three(r: Rig, sid: str) -> list[Any]:
    said = await r.call(sid, "ask_operator", questions=THREE)
    asks = await r.manager.asks.open_for(r.project.id, routed_to="operator")
    assert said.startswith("asked the operator 3 questions: ") and all(f"[{a.short_id}] {a.title}" in said for a in asks), said
    return asks


async def test_a_batch_is_asked_at_once_with_titles_and_a_bad_question_asks_nothing(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        database, providers, launch = await ask_three(r, sid)
        assert [a.title for a in (database, providers, launch)] == ["Database", "Payment providers", "Launch day"]
        assert database.detail["options"] == ["Postgres", "SQLite"] and database.detail["multi"] is False and "allow_free" not in database.detail
        assert providers.detail["multi"] is True and launch.detail["urgent"] is True and launch.detail["options"] == []
        pending = await events(r.manager, "ask.pending")
        assert len(pending) == 3 and pending[1].payload["questions"][0]["multi"] is True
        assert pending[0].payload["questions"][0]["question"].startswith("Database\n\nOrders need"), "a notification names the decision first"
        # The state block the orchestrator reads names each question by its title.
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f'[{database.short_id}] you (question): "Database"' in state

        # One question without a title refuses the whole batch, naming it, and nothing is asked.
        with pytest.raises(Refused, match="question 2 has no title"):
            await r.call(sid, "ask_operator", questions=[{"title": "Colour", "text": "Which colour?"}, {"text": "Which font?"}])
        with pytest.raises(Refused, match="has no title"):
            await r.call(sid, "ask_operator", text="Which font?")
        with pytest.raises(Refused, match="at most 12"):
            await r.call(sid, "ask_operator", questions=[{"title": f"Q{i}", "text": "?"} for i in range(13)])
        assert len(await r.manager.asks.open_for(r.project.id, routed_to="operator")) == 3

        # The single form still asks one, and says so the way it always did.
        said = await r.call(sid, "ask_operator", title="Logo", text="Keep the old logo?", options=["Keep", "Redraw"])
        assert said.startswith("asked the operator as [q")
        logo = (await r.manager.asks.open_for(r.project.id, routed_to="operator"))[-1]
        assert logo.detail["options"] == ["Keep", "Redraw"] and "allow_free" not in logo.detail
    finally:
        await r.manager.close()


async def test_the_orchestrator_withdraws_its_own_open_questions_and_nothing_else(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        database, providers, launch = await ask_three(r, sid)
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        staff_ask = await r.manager.asks.open(r.project.id, origin="staff", kind="question", text="Which font?", routed_to="operator", staff_id=ada.id)
        await r.team.answer(launch.id, text="Next Monday", by="operator", via="app")

        said = await r.call(sid, "withdraw_questions", ids=[database.short_id, staff_ask.short_id, launch.short_id], reason="the owner chose Postgres in the brief")
        assert said.startswith(f"withdrew [{database.short_id}]"), said
        assert f"[{staff_ask.short_id}]: not yours to withdraw" in said
        assert f"[{launch.short_id}]: already answered by the operator: Next Monday" in said
        closed = await r.manager.asks.get(database.id)
        assert closed is not None and closed.resolved_by == "system"
        assert closed.resolution == {"closed": "the owner chose Postgres in the brief", "via": "withdrawn", "by": "orchestrator"}
        assert (await r.manager.asks.get(staff_ask.id)).open  # type: ignore[union-attr]
        assert (await r.manager.asks.get(providers.id)).open  # type: ignore[union-attr]

        # Every window hears it go, with the reason; the orchestrator is not woken by its own doing.
        withdrawn = [e for e in await events(r.manager, "ask.answered") if e.payload.get("via") == "withdrawn"]
        assert len(withdrawn) == 1 and withdrawn[0].payload["request_id"] == database.id and withdrawn[0].payload["reason"] == "the owner chose Postgres in the brief"
        assert await r.orch.classify(r.project.id, withdrawn[0]) is None
        journal = await r.manager.projects.journal(r.project.id, limit=5)
        assert any(e.kind == "withdrawal" and database.short_id in e.text for e in journal)

        # Nothing to withdraw is a refusal, and so is a missing reason.
        with pytest.raises(Refused, match="nothing withdrawn"):
            await r.call(sid, "withdraw_questions", ids=[database.short_id], reason="again")
        with pytest.raises(Refused, match="say why"):
            await r.call(sid, "withdraw_questions", ids=[providers.short_id], reason=" ")
    finally:
        await r.manager.close()


async def test_a_batch_answer_gives_each_item_its_own_outcome(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        database, providers, launch = await ask_three(r, sid)
        logo_said = await r.call(sid, "ask_operator", title="Logo", text="Keep the old logo?", options=["Keep", "Redraw"])
        logo = next(a for a in await r.manager.asks.open_for(r.project.id, routed_to="operator") if a.short_id in logo_said)
        # Answered on the phone a moment before Send, and one withdrawn by the orchestrator meanwhile.
        await r.team.answer(launch.id, text="Friday", by="operator", via="telegram")
        await r.call(sid, "withdraw_questions", ids=[logo.short_id], reason="not needed any more")

        app = r.team.app
        result = await questions.answer(app, [  # type: ignore[arg-type]
            {"ask_id": database.id, "selected": ["Postgres"], "note": "but keep SQLite for the tests"},
            {"ask_id": providers.id, "selected": ["Stripe", "Cash"]},
            {"ask_id": launch.id, "text": "Monday"},
            {"ask_id": logo.id, "selected": ["Keep"]},
            {"ask_id": "ask-nothing"},
        ], project_id=r.project.id, via="project")
        states = {item["ask_id"]: item for item in result["results"]}
        assert states[database.id]["state"] == "answered" and states[providers.id]["state"] == "answered"
        assert states[launch.id]["state"] == "conflict" and states[launch.id]["answered_by"] == "operator" and states[launch.id]["withdrawn"] is False
        assert states[logo.id]["state"] == "conflict" and states[logo.id]["withdrawn"] is True
        assert states["ask-nothing"]["state"] == "missing"

        answered = await r.manager.asks.get(database.id)
        assert answered is not None and answered.resolution["selected"] == ["Postgres"] and answered.resolution["note"] == "but keep SQLite for the tests"
        assert answered.resolution["batch"] == result["batch_id"] and answered.resolution["via"] == "project"
        assert (await r.manager.asks.get(launch.id)).resolution["text"] == "Friday", "the answer that came first stands"  # type: ignore[union-attr]

        # The wrong shape is refused per item and answers nothing.
        again = await r.call(sid, "ask_operator", questions=[{"title": "Colour", "text": "Which colour?", "options": ["Red", "Blue"], "allow_free": False}, {"title": "Font", "text": "Which font?", "options": ["Serif", "Sans"]}])
        colour, font = [a for a in await r.manager.asks.open_for(r.project.id, routed_to="operator") if a.short_id in again]
        shapes = await questions.answer(app, [  # type: ignore[arg-type]
            {"ask_id": colour.id, "text": "Green"},
            {"ask_id": font.id, "selected": ["Serif", "Sans"]},
            {"ask_id": database.id, "selected": ["SQLite"]},
        ], project_id="some-other-project", via="project")
        reasons = [item.get("error", "") for item in shapes["results"]]
        assert [item["state"] for item in shapes["results"]] == ["refused", "refused", "refused"]
        assert all("not this project's" in reason for reason in reasons)
        shapes = await questions.answer(app, [{"ask_id": font.id, "selected": ["Serif", "Sans"]}, {"ask_id": font.id, "note": "any"}], project_id=r.project.id, via="project")
        assert [item["error"] for item in shapes["results"]] == [
            "choose one option",
            "a note goes beside a chosen option; without one, write the answer itself",
        ]
        # "Only the options" is gone: the operator may always answer in words, and a stored row that
        # still says allow_free false (as the orchestrator's "Colour" asked for) is answered so too.
        assert "allow_free" not in colour.detail
        await db.execute("UPDATE asks SET detail_json = json_set(detail_json, '$.allow_free', json('false')) WHERE id = ?", (colour.id,))
        listed = {q["id"]: q for q in await questions.waiting(app, r.project.id)}  # type: ignore[arg-type]
        assert "allow_free" not in listed[colour.id]
        words = await questions.answer(app, [{"ask_id": colour.id, "text": "Green, none of those"}], project_id=r.project.id, via="project")
        assert [item["state"] for item in words["results"]] == ["answered"], words
        assert (await r.manager.asks.get(colour.id)).resolution["text"] == "Green, none of those"  # type: ignore[union-attr]
    finally:
        await r.manager.close()


async def test_a_batch_of_answers_wakes_the_orchestrator_once(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Postgres, Stripe and cash, Monday — noted."}, {"text": "A second wake-up that must not happen."}])
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        database, providers, launch = await ask_three(r, sid)
        result = await questions.answer(r.team.app, [  # type: ignore[arg-type]
            {"ask_id": database.id, "selected": ["Postgres"], "note": "keep SQLite for tests"},
            {"ask_id": providers.id, "selected": ["Stripe", "Cash"]},
            {"ask_id": launch.id, "text": "Monday"},
        ], project_id=r.project.id, via="project")
        assert [item["state"] for item in result["results"]] == ["answered"] * 3

        # Each answer is still announced on its own — the router closes each notification, the app
        # drops each card — but only the batch's event wakes the orchestrator.
        answered = await events(r.manager, "ask.answered")
        assert len(answered) == 3
        assert [await r.orch.classify(r.project.id, e) for e in answered] == [None, None, None]
        [batch] = await events(r.manager, "ask.batch")
        assert batch.payload["ask_ids"] == [database.id, providers.id, launch.id] and batch.project_id == r.project.id
        wake = await r.orch.classify(r.project.id, batch)
        assert wake is not None and wake.urgent

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid))

        await until_await(woken, "the batch woke the orchestrator")
        # Answers wake at urgent speed (about a second); a second wake-up would have come by now.
        await asyncio.sleep(2.5)
        messages = await events_messages(r.manager, sid)
        assert len(messages) == 1, messages
        [text] = messages
        assert " · 3 since " in text.splitlines()[0]
        assert f'the operator answered your request [{database.short_id}] "Database": Postgres — note: keep SQLite for tests' in text
        assert f'[{providers.short_id}] "Payment providers": Stripe, Cash' in text
        assert f'[{launch.short_id}] "Launch day": Monday' in text
    finally:
        await r.manager.close()


async def test_permissions_and_escalations_sit_above_the_questions_and_the_main_list_gathers_projects(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await ask_three(r, sid)
        ira = await r.manager.staff.hire(r.project.id, name="Ira", harness="claude")
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        perm = await r.manager.asks.open(r.project.id, origin="staff", kind="permission", text="Exec: npm publish", routed_to="operator", staff_id=ira.id)
        daedalus_perm = await r.manager.asks.open(r.project.id, origin="staff", kind="permission", text="Exec: rm -rf dist", routed_to="operator", staff_id=ada.id)
        await r.manager.asks.open(r.project.id, origin="staff", kind="question", text="Which font?", routed_to="orchestrator", staff_id=ada.id)
        other = await r.manager.projects.create("Garden", [])
        await r.manager.asks.open(other.id, origin="staff", kind="question", text="Water daily?", routed_to="operator")

        listed = await questions.waiting(r.team.app, r.project.id)  # type: ignore[arg-type]
        by_id = {q["id"]: q for q in listed}
        assert len(listed) == 5, "the question routed to the orchestrator is not the operator's"
        assert by_id[perm.id]["section"] == "requests" and by_id[perm.id]["asker"] == "Ira" and by_id[perm.id]["always"] is True
        assert by_id[daedalus_perm.id]["always"] is False, "a Daedalus member's gate has no standing grant"
        assert by_id[perm.id]["heading"] == "Exec: npm publish" and by_id[perm.id]["title"] == ""
        database = next(q for q in listed if q["title"] == "Database")
        assert database["section"] == "questions" and database["asker"] == "orchestrator" and database["project_name"] == "Bakery"
        assert database["options"] == ["Postgres", "SQLite"] and "allow_free" not in database

        # The main chat's list: every orchestrated project's, and not Garden's, which has no orchestrator.
        everything = await questions.waiting(r.team.app)  # type: ignore[arg-type]
        assert {q["project_name"] for q in everything} == {"Bakery"} and len(everything) == 5

        result = await questions.answer(r.team.app, [  # type: ignore[arg-type]
            {"ask_id": perm.id, "allow": False, "note": "not before the review"},
            {"ask_id": daedalus_perm.id, "allow": True, "text": "sure"},
        ], project_id=None, via="main")
        first, second = result["results"]
        assert first["state"] == "answered"
        refused = await r.manager.asks.get(perm.id)
        assert refused is not None and refused.resolution["allow"] is False and refused.resolution["note"] == "not before the review" and refused.resolution["via"] == "main"
        assert second["state"] == "refused" and "allow or deny" in second["error"]
    finally:
        await r.manager.close()


async def test_the_routes_list_and_answer_in_a_batch(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        database, providers, launch = await ask_three(r, sid)
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions=r.team.app.extensions, guard=None, notifications=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            listed = await client.get(f"/api/questions?project={r.project.id}", headers=headers)
            assert listed.status_code == 200 and [q["title"] for q in listed.json()["questions"]] == ["Database", "Payment providers", "Launch day"]
            assert (await client.get("/api/questions", headers=headers)).json()["questions"][0]["id"] == database.id
            assert (await client.get("/api/questions?project=nope", headers=headers)).status_code == 404
            sent = await client.post(f"/api/projects/{r.project.id}/asks/answer", json=[{"ask_id": database.id, "selected": ["SQLite"]}, {"ask_id": launch.id, "text": "Monday"}], headers=headers)
            assert sent.status_code == 200, sent.text
            assert [i["state"] for i in sent.json()["results"]] == ["answered", "answered"]
            again = await client.post("/api/asks/answer", json=[{"ask_id": database.id, "selected": ["Postgres"]}, {"ask_id": providers.id, "selected": ["PayPal"]}], headers=headers)
            assert [i["state"] for i in again.json()["results"]] == ["conflict", "answered"]
            assert (await r.manager.asks.get(providers.id)).resolution["via"] == "main"  # type: ignore[union-attr]
            assert (await client.post("/api/asks/answer", json=[{"ask_id": providers.id, "colour": "red"}], headers=headers)).status_code == 422
            # The server-wide "always" is a field of the batch too: the list sent it and was refused whole.
            server_wide = await client.post("/api/asks/answer", json=[{"ask_id": database.id, "allow": True, "always": True, "server": True}], headers=headers)
            assert server_wide.status_code == 200, server_wide.text
            assert (await client.get(f"/api/questions?project={r.project.id}", headers=headers)).json()["questions"] == []
    finally:
        await r.manager.close()


async def test_the_title_migration_keeps_every_request_and_old_ones_read_by_their_first_line(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    titled = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, str) and "ADD COLUMN title" in migration)
    raw = sqlite3.connect(path)
    raw.executescript(f"CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES ({titled});")
    opening = SimpleNamespace(workspaces_dir=tmp_path / "workspaces", local_env="container")
    for script in MIGRATIONS[:titled]:
        raw.executescript(script(opening) if callable(script) else script)
    raw.execute("INSERT INTO projects(id, name, created_at) VALUES ('p1', 'Bakery', '2026-09-01T00:00:00+00:00')")
    raw.execute(
        "INSERT INTO asks(id, short_id, project_id, origin, kind, text, detail_json, routed_to, created_at, routed_at) "
        "VALUES ('ask-1', 'qabcde', 'p1', 'orchestrator', 'question', ?, '{}', 'operator', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
        ("Postgres or SQLite?\n\nContext: orders need concurrency",),
    )
    raw.commit()
    raw.close()
    db = Database(path, local_env="container")
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        ask = await AsksStore(db).get("ask-1")
        assert ask is not None and ask.title == "" and ask.heading == "Postgres or SQLite?"
        assert [r[0] for r in await db.fetchall("PRAGMA integrity_check")] == ["ok"]
    finally:
        await db.close()
    # A second open finds nothing to do.
    db = Database(path, local_env="container")
    await db.open()
    try:
        assert (await AsksStore(db).get("ask-1")).heading == "Postgres or SQLite?"  # type: ignore[union-attr]
    finally:
        await db.close()


def test_a_permissions_answer_carries_the_server_wide_always_only_with_always_and_allow() -> None:
    permission: Any = SimpleNamespace(kind="permission", detail={"tool": "mcp__daedalus_browser__BrowserNavigate"})
    assert questions._shape(permission, {"allow": True, "always": True, "server": True}) == {"allow": True, "always": True, "server": True, "note": ""}
    # "server" means nothing on its own: an allow once, or a refusal, stays that.
    assert questions._shape(permission, {"allow": True, "server": True})["server"] is False
    assert questions._shape(permission, {"allow": False, "always": True, "server": True}) == {"allow": False, "always": False, "server": False, "note": ""}
