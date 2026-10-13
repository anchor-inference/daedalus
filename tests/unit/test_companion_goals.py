"""The companion's goals, its pause, its profile and the ways in that are not its voice."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import COMPANION_TOOLS, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.companion import GOAL_OPEN, PAUSED_KEY, Companion, report_block
from daedalus.host.session_runner import SessionManager, _own_schedules
from daedalus.speech.service import LocalSpeech
from daedalus.speech.tts_service import LocalTts
from daedalus.stores.companion import Cadence, CompanionStore, GoalError, parse_cadence
from daedalus.stores.database import MIGRATIONS, Database
from tests.support.models import FALLBACK_PRESET, model_config

HEADERS = {"X-Daedalus-Token": "tok"}


class Inbox:
    """Stands in for the notification service: what was posted, in order."""

    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, draft: Any, *, force: bool = False) -> None:
        self.posted.append(draft)


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    inbox = Inbox()
    application = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"notifications": inbox})
    application.inbox = inbox
    yield application
    await manager.close()


def _capture(manager: SessionManager) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator", via="app") -> str:  # type: ignore[no-untyped-def]
        submitted.append({"session_id": session_id, "text": text, "origin": origin, "via": via, "attachments": list(attachments)})
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    return submitted


# -- cadences ---------------------------------------------------------------------------------


def test_a_cadence_is_read_the_way_it_is_said() -> None:
    assert parse_cadence("", min_minutes=15) == Cadence()
    assert parse_cadence("every 2h", min_minutes=15) == Cadence(interval_minutes=120)
    assert parse_cadence("30m", min_minutes=15) == Cadence(interval_minutes=30)
    assert parse_cadence("daily", min_minutes=15) == Cadence(interval_minutes=24 * 60)
    assert parse_cadence("Every 3 Days", min_minutes=15) == Cadence(interval_minutes=3 * 24 * 60)
    assert parse_cadence("0 9 * * 1-5", min_minutes=15) == Cadence(cron="0 9 * * 1-5")
    assert parse_cadence("every 2h", min_minutes=15).text == "every 2h"


def test_a_cadence_faster_than_the_floor_is_refused_however_it_is_written() -> None:
    with pytest.raises(GoalError):
        parse_cadence("5m", min_minutes=15)
    with pytest.raises(GoalError):
        parse_cadence("*/5 * * * *", min_minutes=15)
    with pytest.raises(GoalError):
        parse_cadence("whenever you like", min_minutes=15)


# -- the store --------------------------------------------------------------------------------


async def test_a_goal_is_first_due_one_cadence_after_it_was_made_and_moves_on_when_checked(db: Database) -> None:
    store = CompanionStore(db)
    goal = await store.create_goal(title="  Watch   the flat prices ", brief="under 80k", cadence=Cadence(interval_minutes=60))
    assert goal.title == "Watch the flat prices" and goal.status == "active"
    assert goal.next_check_at is not None
    assert await store.due() == [], "a goal is not due the moment it is made"
    later = datetime.now(UTC) + timedelta(minutes=61)
    assert [g.id for g in await store.due(later)] == [goal.id]
    checked = await store.checked(goal.id, later)
    assert checked is not None and checked.last_check_at == later.isoformat()
    assert datetime.fromisoformat(checked.next_check_at or "") == later + timedelta(minutes=60)
    assert await store.due(later) == []

    # Paused goals are never due; resuming starts the cadence again from now.
    paused = await store.update_goal(goal.id, status="paused")
    assert await store.due(later + timedelta(days=1)) == []
    assert paused.status == "paused"
    linked = await store.update_goal(goal.id, status="active", link_session="s1")
    assert linked.sessions == ["s1"]
    assert (await store.update_goal(goal.id, link_session="s1")).sessions == ["s1"]

    reported = await store.report(goal.id, "two new listings, both too far out")
    assert reported.last_report.startswith("two new") and reported.last_report_at
    with pytest.raises(GoalError):
        await store.report(goal.id, "   ")
    with pytest.raises(KeyError):
        await store.update_goal("g-none", status="done")


async def test_the_activity_log_keeps_the_newest_lines(db: Database, monkeypatch: Any) -> None:
    from daedalus.stores import companion as store_module

    monkeypatch.setattr(store_module, "ACTIVITY_KEPT", 3)
    store = CompanionStore(db)
    for n in range(5):
        await store.log("delegated", f"agent {n}")
    assert [row["title"] for row in await store.activity()] == ["agent 4", "agent 3", "agent 2"]


# -- the companion's goals ----------------------------------------------------------------------


async def test_a_due_goal_becomes_a_turn_answered_by_the_background_model(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    submitted = _capture(manager)
    app.config.companion.background_preset = FALLBACK_PRESET
    goal = await companion.goal_create(title="Bakery prices", brief="keep them current", cadence="every 6h")
    assert goal["cadence"] == "every 6h"
    assert await companion.check_goals() == 0, "nothing is due yet"

    await app.db.execute("UPDATE companion_goals SET next_check_at = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), goal["id"]))
    assert await companion.check_goals() == 1
    turn = submitted[-1]
    assert turn["origin"] == "goal" and GOAL_OPEN in turn["text"] and "Bakery prices" in turn["text"] and "GoalReport" in turn["text"]
    session_id = await companion.session_id()
    assert (await manager.live.load(session_id))["preset"] == FALLBACK_PRESET
    # Checked once: the next one is a cadence away, so a slow turn does not check it again a minute later.
    assert await companion.check_goals() == 0
    kinds = [row["kind"] for row in await companion.activity()]
    assert "check" in kinds and "goal" in kinds


async def test_a_paused_companion_checks_no_goal_and_relays_nothing_until_resumed(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    submitted = _capture(manager)
    goal = await companion.goal_create(title="Inbox zero", cadence="daily")
    await app.db.execute("UPDATE companion_goals SET next_check_at = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), goal["id"]))
    await companion.pause(True)
    assert await app.db.kv_get(PAUSED_KEY) is True
    assert await companion.check_goals() == 0

    async with companion.listen():
        await companion.report(report_block(kind="final", title="Digest", session_id="s-d", state="finished", body="it went out"), kind="final", title="Digest", session_id="s-d", body="it went out")
        assert not submitted, "a paused companion relayed a report into the conversation"
        assert companion.held
        profile = await companion.pause(False)
        assert profile["paused"] is False
        assert submitted and "it went out" in submitted[-1]["text"]

    # A restart remembers a pause.
    await companion.pause(True)
    again = Companion(app)
    await again.restore()
    assert again.paused is True


async def test_news_nobody_hears_reaches_the_inbox(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    _capture(manager)
    await companion.session_id()
    await companion.report(report_block(kind="final", title="Invoices", session_id="s-i", state="finished", body="all sent"), kind="final", title="Invoices", session_id="s-i", body="all sent")
    assert [d.title for d in app.inbox.posted] == ["Daedalus: Invoices finished"]
    assert app.inbox.posted[0].link == "/app/companion" and app.inbox.posted[0].body == "all sent"
    # A question is the router's to announce, as every session's is: no second entry for it here.
    await companion.report(report_block(kind="question", title="Invoices", session_id="s-i", state="waiting", body="which month?"), kind="question")
    assert len(app.inbox.posted) == 1

    goal = await companion.goal_create(title="Flat")
    await companion.goal_report(goal_id=goal["id"], report="one new listing worth a look")
    assert app.inbox.posted[-1].title == "Daedalus: Flat"
    # With a page open, a routine report stays in the conversation; one that needs the operator does not.
    async with companion.listen():
        await companion.goal_report(goal_id=goal["id"], report="nothing new")
        assert app.inbox.posted[-1].body == "one new listing worth a look"
        await companion.goal_report(goal_id=goal["id"], report="the landlord wants an answer today", needs_attention=True)
        assert app.inbox.posted[-1].body == "the landlord wants an answer today"


async def test_a_delegated_agent_can_be_linked_to_a_goal(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    _capture(manager)
    goal = await companion.goal_create(title="Flat")
    started = await companion.delegate(title="Listings", task="scan the listings", goal_id=goal["id"])
    linked = (await companion.goal_list())[0]
    assert linked["sessions"] == [started["session_id"]]
    with pytest.raises(ValueError):
        await companion.delegate(title="x", task="y", goal_id="g-missing")


async def test_the_turn_context_says_the_channel_and_the_goals(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    _capture(manager)
    await companion.goal_create(title="Flat", cadence="daily")
    await companion.message("what is up")
    state = manager.live_state(await companion.session_id())
    notes = await companion.turn_notes(state)
    assert notes is not None and "Channel: text" in notes and "Flat" in notes
    await companion.say("and now?")
    assert "Channel: voice" in (await companion.turn_notes(state) or "")
    other = await manager.create_session("other")
    assert await companion.turn_notes(other) is None


async def test_a_new_conversation_keeps_the_companions_notes(app: Any) -> None:
    from protocore.contracts.memory import MemoryScope

    from daedalus.host.engine_factory import TENANT

    manager: SessionManager = app.manager
    companion = Companion(app)
    _capture(manager)
    old = await companion.session_id()
    await manager.memory.write(TENANT, MemoryScope.session, old, "the operator takes their coffee black", kind="fact")
    new = await companion.new_session()
    assert new != old
    assert [r.text for r in manager.memory.records(TENANT, scope=MemoryScope.session, scope_key=new)] == ["the operator takes their coffee black"]
    assert not manager.memory.records(TENANT, scope=MemoryScope.session, scope_key=old)


# -- what the host lets it do ---------------------------------------------------------------------


async def test_the_companion_has_its_tools_and_nothing_that_touches_a_file_a_shell_or_a_browser(app: Any) -> None:
    manager: SessionManager = app.manager
    companion = Companion(app)
    state = manager.live_state(await companion.session_id())
    assert state is not None
    known = {t.name for t in manager.tools.list_all()}
    allowed = known - manager.blocked_tools_for(state)
    assert allowed == set(COMPANION_TOOLS) & known
    for name in ("Exec", "Read", "Write", "Edit", "WebFetch", "SpawnAgent", "SubAgent", "CalendarCreate", "CalendarDelete"):
        assert name not in allowed, name
    assert {"GoalCreate", "GoalReport", "Remember", "Recall", "ScheduleCreate", "Notify", "CalendarEvents"} <= allowed
    # Every group eager: the companion has no ToolSearch to load one with.
    assert set(manager.tool_group_loads_for(state).values()) == {"eager"}
    # An ordinary session cannot reach the companion's own tools.
    other = await manager.create_session("other")
    assert {"GoalCreate", "Delegate"} <= manager.blocked_tools_for(other)


async def test_the_companions_schedule_list_is_its_own() -> None:
    rows = [{"id": "a", "created_by_session": "c1"}, {"id": "b", "created_by_session": "s9"}, {"id": "p1", "source_session_id": "c1"}]

    async def service(op: str, **kwargs: Any) -> Any:
        return rows if op == "list" else {"op": op}

    scoped = _own_schedules(service, "c1")
    assert scoped is not None
    assert [r["id"] for r in await scoped("list", source_session_id="c1")] == ["a", "p1"]
    assert await scoped("create", name="x") == {"op": "create"}


# -- the contract and the API ---------------------------------------------------------------------


@pytest.fixture
async def client(settings: Settings, db: Database) -> Any:
    settings.telegram_bot_token = ""
    settings.owner_user_id = 1
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    application = SimpleNamespace(
        settings=settings, config=manager.config, db=db, manager=manager, front=None, guard=None,
        extensions={}, speech=LocalSpeech(settings.state_dir, manager.config), tts=LocalTts(settings.state_dir, manager.config),
    )

    async def save_config(config: RuntimeConfig) -> None:
        application.config = config
        config.save(settings.config_path)
        manager.reload_config(config)

    application.save_config = save_config
    companion = Companion(application)
    application.extensions["companion"] = companion
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(application, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        c.app = application  # type: ignore[attr-defined]
        c.companion = companion  # type: ignore[attr-defined]
        yield c
    await manager.close()


async def test_the_profile_is_the_shape_every_client_codes_against(client: Any) -> None:
    body = (await client.get("/api/companion", headers=HEADERS)).json()
    assert {"session_id", "name", "avatar", "paused", "state", "running_agents"} <= set(body)
    assert body["name"] == "Daedalus" and body["paused"] is False and body["state"] == "idle" and body["running_agents"] == 0
    assert set(body["avatar"]) == {"shape", "color", "eyes", "accessory"}

    saved = (await client.put("/api/companion", json={"name": "  Iris ", "avatar": {"shape": "star", "color": "#FF8800"}}, headers=HEADERS)).json()
    assert saved["name"] == "Iris" and saved["avatar"]["shape"] == "star" and saved["avatar"]["color"] == "#ff8800"
    assert RuntimeConfig.load(client.app.settings.config_path).companion.name == "Iris"
    assert (await client.put("/api/companion", json={"avatar": {"shape": "hexagon"}}, headers=HEADERS)).status_code == 400
    assert (await client.put("/api/companion", json={"avatar": {"color": "red"}}, headers=HEADERS)).status_code == 400

    paused = (await client.post("/api/companion/pause", json={"paused": True}, headers=HEADERS)).json()
    assert paused["paused"] is True
    assert (await client.post("/api/companion/call", json={"on": True}, headers=HEADERS)).json() == {"call": False}, "no page is listening"


async def test_a_message_with_an_attachment_is_a_text_turn_in_the_companions_session(client: Any) -> None:
    submitted = _capture(client.app.manager)
    staged = (await client.post("/api/companion/attachments", files={"file": ("shot.png", b"\x89PNG....", "image/png")}, headers=HEADERS)).json()
    assert staged["name"] == "shot.png" and staged["mime_type"] == "image/png" and len(staged["id"]) == 24
    answer = await client.post("/api/companion/message", json={"text": "what is on this screen?", "attachments": [staged["id"]], "source": "quick_chat"}, headers=HEADERS)
    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is True and body["session_id"] == await client.companion.session_id()
    turn = submitted[-1]
    assert turn["text"] == "what is on this screen?" and turn["via"] == "quick_chat"
    assert [a.name for a in turn["attachments"]] == ["shot.png"]
    # Used once: the staged file is gone, and naming it again is a 404 rather than a resend.
    again = await client.post("/api/companion/message", json={"text": "again", "attachments": [staged["id"]]}, headers=HEADERS)
    assert again.status_code == 404
    assert (await client.post("/api/companion/message", json={"text": "x", "attachments": ["../../etc"]}, headers=HEADERS)).status_code == 400
    assert (await client.post("/api/companion/message", json={"text": "  "}, headers=HEADERS)).status_code == 400
    assert (await client.post("/api/companion/message", json={"text": "hi", "source": "carrier pigeon"}, headers=HEADERS)).status_code == 422


async def test_goals_are_edited_from_the_page_too(client: Any) -> None:
    made = (await client.post("/api/companion/goals", json={"title": "Flat", "cadence": "daily"}, headers=HEADERS)).json()
    assert made["cadence"] == "every 1d" and made["status"] == "active"
    assert (await client.post("/api/companion/goals", json={"title": "Spam", "cadence": "1m"}, headers=HEADERS)).status_code == 400
    done = (await client.patch(f"/api/companion/goals/{made['id']}", json={"status": "done"}, headers=HEADERS)).json()
    assert done["status"] == "done"
    assert (await client.get("/api/companion/goals", headers=HEADERS)).json()["goals"] == []
    assert len((await client.get("/api/companion/goals?closed=true", headers=HEADERS)).json()["goals"]) == 1
    assert (await client.patch("/api/companion/goals/g-none", json={"status": "done"}, headers=HEADERS)).status_code == 404
    assert (await client.delete(f"/api/companion/goals/{made['id']}", headers=HEADERS)).json() == {"deleted": True}
    items = (await client.get("/api/companion/activity", headers=HEADERS)).json()["items"]
    assert items and items[0]["kind"] == "goal"
    tools = (await client.get("/api/companion/tools", headers=HEADERS)).json()
    assert "GoalCreate" in {t["name"] for t in tools["companion"]} and "Exec" not in {t["name"] for t in tools["companion"]}
    schedules = (await client.get("/api/companion/schedules", headers=HEADERS)).json()
    assert set(schedules) == {"schedules", "proposals", "goals"}
    computer = (await client.get("/api/companion/computer", headers=HEADERS)).json()
    assert computer["available"] is False and computer["groups"] == []


# -- carrying the voice concierge over ----------------------------------------------------------------


def test_the_voice_section_becomes_the_companion_section_with_its_place_on_the_rail(tmp_path: Any) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[voice]\npreset = "opencode.fast"\nprogress_max_per_minute = 2\n\n[voice.tts]\nlocal_voice = "multi-supertonic"\nlocal_speed = 1.2\n\n'
        '[navigation]\nrail = ["terminals", "voice", "inbox"]\nhidden = []\n\n[navigation.icons]\nvoice = "bolt"\n',
        encoding="utf-8",
    )
    config = RuntimeConfig.load(path)
    assert config.companion.preset == "opencode.fast" and config.companion.progress_max_per_minute == 2
    assert config.companion.tts.local_voice == "multi-supertonic" and config.companion.tts.local_speed == 1.2
    assert config.companion.name == "Daedalus"
    assert config.navigation.rail == ["terminals", "companion", "inbox"] and config.navigation.icons == {"companion": "bolt"}
    written = path.read_text(encoding="utf-8")
    assert "[voice]" not in written and "[companion]" in written
    # Loading the rewritten file changes nothing more.
    assert RuntimeConfig.load(path) == config


async def test_the_migration_carries_the_concierges_rows_to_the_companion(settings: Settings) -> None:
    db = Database(settings.state_dir / "carry.sqlite")
    await db.open()
    # Rows as the concierge left them, then the schema put back one version so 111 runs over them.
    try:
        await db.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES ('pv', 'Voice', '', '{\"system\":\"voice\"}', 'voice')")
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('sv', 't', 'Voice', '', '', '{\"voice\":true,\"telegram_detached\":true}', 'pv')")
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('sa', 't', 'Agent', '', '', '{\"voice_parent\":\"sv\",\"brief\":\"x\"}', 'pv')")
        await db.execute("INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('so', 't', 'Other', '', '', '{\"workspace\":\"/w\"}', 'pv')")
        await db.kv_set("voice_session", "sv")
        await db.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS) - 1,))
    finally:
        await db.close()
    for _ in range(2):
        await db.open()
        try:
            meta = {r["id"]: json.loads(r["metadata"]) for r in await db.fetchall("SELECT id, metadata FROM sessions")}
            assert meta["sv"] == {"companion": True, "telegram_detached": True}
            assert meta["sa"] == {"companion_parent": "sv", "brief": "x"}
            assert meta["so"] == {"workspace": "/w"}
            project = await db.fetchone("SELECT name, system, settings FROM projects WHERE id = 'pv'")
            assert project["name"] == "Companion" and project["system"] == "companion" and json.loads(project["settings"])["system"] == "companion"
            assert await db.kv_get("companion_session") == "sv" and await db.kv_get("voice_session") is None
        finally:
            await db.close()
