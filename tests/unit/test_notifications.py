"""The notifications store: what a post records, how a repeat merges, what the badge counts, what
each write announces on the bus, and the routes and the ``/inbox`` digest that read it."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import commands as slash
from daedalus.extensions import notifications as notifications_module
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import EVENT_BODY_MAX, Action, Draft, NotificationService, format_entries
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

HEAD = {"X-Daedalus-Token": "tok"}
REPO = Path(__file__).resolve().parents[2]


class FakeOutbox:
    def __init__(self, sent: list[tuple[str, str]], session_id: str) -> None:
        self.sent, self.session_id = sent, session_id

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append((self.session_id, text))
        return 1


class FakeFront:
    def __init__(self) -> None:
        self.notified: list[tuple[str, bool]] = []
        self.topics: list[tuple[str, str]] = []
        self.command_hooks: dict[str, Any] = {}

    async def notify(self, text: str, *, markdown: bool = True) -> None:
        self.notified.append((text, markdown))

    async def outbox_for_session(self, session_id: str) -> FakeOutbox:
        return FakeOutbox(self.topics, session_id)


@pytest.fixture
async def manager(settings: Settings, db: Database) -> SessionManager:
    instance = SessionManager(settings, RuntimeConfig(), db=db)
    await instance.start()
    yield instance  # type: ignore[misc]
    await instance.close()


@pytest.fixture
def front() -> FakeFront:
    return FakeFront()


@pytest.fixture
def service(db: Database, manager: SessionManager) -> NotificationService:
    """The store on its own: no bot, nobody present, the default preferences."""
    return NotificationService(db, manager.bus)


async def announced(manager: SessionManager, kind: str = "notify") -> list[Any]:
    return await manager.bus.replay(0, EventFilter(types=(kind,)), limit=100)


async def test_a_post_is_recorded_with_its_category_defaults_and_announced_once(service: NotificationService, manager: SessionManager) -> None:
    view = await service.post(Draft(
        "permission", "Naya is waiting for permission", "npm install", session_id="s1", project_id="p1",
        actions=(Action("allow", "Allow", "primary", quick=True),), request_ref="policy:abc", source="harness",
    ))
    assert view["level"] == "urgent"  # the category's own level when the producer names none
    assert (view["kind"], view["tone"], view["count"], view["seen"]) == ("permission", "info", 1, False)
    assert view["actions"] == [{"id": "allow", "label": "Allow", "style": "primary", "quick": True}]
    assert view["needs_you"] is True and view["resolved"] is None
    [event] = await announced(manager)
    assert event.session_id == "s1" and event.project_id == "p1"
    assert event.payload["notification"]["id"] == view["id"] and event.payload["merged"] is False
    assert event.payload["summary"] == {"unseen": 1, "needs_you": 1}
    assert event.payload["toast"] is True and event.payload["deliver"] == {"push": False, "desktop": False, "telegram": False}
    row = await service.db.fetchone("SELECT event_seq FROM notifications WHERE id = ?", (view["id"],))
    assert row["event_seq"] == event.seq


async def test_an_unknown_category_is_refused_at_the_call_site(service: NotificationService) -> None:
    with pytest.raises(ValueError, match="category"):
        await service.post(Draft("nonsense", "x"))  # type: ignore[arg-type]


async def test_a_repeat_with_the_same_key_merges_into_one_unseen_row(service: NotificationService, manager: SessionManager) -> None:
    first = await service.post(Draft("system", "Service 'web' is down", "first", dedupe_key="service:web", level="quiet"))
    await service.mark_seen([first["id"]])
    again = await service.post(Draft("system", "Service 'web' is still down", "second", dedupe_key="service:web", tone="warning"))
    assert again["id"] == first["id"] and again["count"] == 2
    assert (again["title"], again["body"], again["tone"], again["seen"]) == ("Service 'web' is still down", "second", "warning", False)
    assert again["level"] == "normal"  # a level rises with a repeat and never falls
    quieter = await service.post(Draft("system", "down", dedupe_key="service:web", level="quiet"))
    assert quieter["level"] == "normal" and quieter["count"] == 3
    other = await service.post(Draft("system", "other", dedupe_key="service:api"))
    assert other["id"] != first["id"]
    assert [e.payload["merged"] for e in await announced(manager)] == [False, True, True, False]


async def test_the_summary_counts_what_is_worth_a_badge(service: NotificationService) -> None:
    await service.post(Draft("reminder", "Heartbeat: quiet", level="quiet"))
    loud = await service.post(Draft("run_failed", "Run failed", tone="error"))
    await service.post(Draft("question", "Pick one", request_ref="ask:1"))
    held = await service.post(Draft("permission", "Held for the orchestrator", request_ref="policy:2"))
    later = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    await service.db.execute("UPDATE notifications SET held_until = ? WHERE id = ?", (later, held["id"]))
    assert await service.summary() == {"unseen": 2, "needs_you": 1}
    await service.mark_seen([loud["id"]])
    assert await service.summary() == {"unseen": 1, "needs_you": 1}
    assert [e["title"] for e in (await service.list("all"))["entries"]] == ["Pick one", "Run failed", "Heartbeat: quiet"]


async def test_a_repeat_moves_to_the_top_and_the_pages_still_cover_everything_once(service: NotificationService) -> None:
    # A repeat merges into its earlier row; listing by id used to leave it under older entries.
    await service.post(Draft("run_finished", "a", dedupe_key="run:a"))
    await service.post(Draft("system", "b"))
    await service.post(Draft("system", "c"))
    await service.post(Draft("run_finished", "a", dedupe_key="run:a"))
    assert [e["title"] for e in (await service.list("all"))["entries"]] == ["a", "c", "b"]
    first = await service.list("all", limit=2)
    rest = await service.list("all", before=first["next_before"], limit=2)
    assert [e["title"] for e in first["entries"]] == ["a", "c"] and [e["title"] for e in rest["entries"]] == ["b"]
    assert rest["next_before"] is None


async def test_the_views_and_the_page_cursor(service: NotificationService) -> None:
    for n in range(5):
        await service.post(Draft("system", f"n{n}", tone="warning" if n % 2 else "info", project_id="p1" if n < 2 else None))
    await service.post(Draft("question", "asks", request_ref="ask:9"))
    problems = await service.list("problems")
    assert [e["title"] for e in problems["entries"]] == ["n3", "n1"]
    needs = await service.list("needs_you")
    assert [e["title"] for e in needs["entries"]] == ["asks"]
    first = await service.list("all", limit=4)
    assert [e["title"] for e in first["entries"]] == ["asks", "n4", "n3", "n2"] and first["next_before"] is not None
    rest = await service.list("all", before=first["next_before"], limit=4)
    assert [e["title"] for e in rest["entries"]] == ["n1", "n0"] and rest["next_before"] is None
    assert [e["title"] for e in (await service.list("all", project_id="p1"))["entries"]] == ["n1", "n0"]
    with pytest.raises(ValueError):
        await service.list("everything")  # type: ignore[arg-type]


async def test_mark_seen_by_ids_by_session_and_all_announces_each_change(service: NotificationService, manager: SessionManager) -> None:
    a = await service.post(Draft("system", "a", session_id="s1"))
    await service.post(Draft("system", "b", session_id="s1"))
    c = await service.post(Draft("system", "c", session_id="s2"))
    await service.post(Draft("system", "d"))
    assert await service.mark_seen([a["id"], a["id"], 9999]) == 1
    assert await service.mark_seen([a["id"]]) == 0  # already seen: nothing changes, nothing is announced
    assert await service.mark_seen(session_id="s1") == 1
    assert await service.mark_seen(everything=True) == 2
    assert await service.mark_seen([]) == 0
    seen = await announced(manager, "notify.seen")
    assert [e.payload["ids"] for e in seen] == [[a["id"]], [a["id"] + 1], "all"]
    assert seen[-1].payload["summary"] == {"unseen": 0, "needs_you": 0}
    assert (await service.get(c["id"]))["seen"] is True  # type: ignore[index]


async def test_delete_and_prune_keep_what_is_unseen_or_still_open(service: NotificationService) -> None:
    unseen = await service.post(Draft("system", "unseen"))
    seen = await service.post(Draft("system", "seen"))
    quiet = await service.post(Draft("system", "quiet", level="quiet"))
    open_ask = await service.post(Draft("question", "open", request_ref="ask:1"))
    answered = await service.post(Draft("question", "answered", request_ref="ask:2"))
    recent = await service.post(Draft("system", "recent"))
    await service.mark_seen([seen["id"], open_ask["id"], answered["id"], recent["id"]])
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await service.db.execute("UPDATE notifications SET updated_at = ? WHERE id != ?", (old, recent["id"]))
    await service.db.execute("UPDATE notifications SET resolved_at = ?, resolution = 'answered' WHERE id = ?", (old, answered["id"]))
    assert await service.prune(30) == 3
    left = {e["title"] for e in (await service.list("all"))["entries"]}
    assert left == {"unseen", "open", "recent"}
    assert quiet["id"] not in {e["id"] for e in (await service.list("all"))["entries"]}
    assert await service.delete(unseen["id"]) is True
    assert await service.delete(unseen["id"]) is False


async def test_a_long_body_is_cut_in_the_event_and_kept_whole_in_the_row(service: NotificationService, manager: SessionManager) -> None:
    body = "ж" * 20_000
    view = await service.post(Draft("system", "long", body))
    assert view["body"] == body
    [event] = await announced(manager)
    assert len(event.payload["notification"]["body"]) == EVENT_BODY_MAX and event.payload["notification"]["body_truncated"] is True


async def test_spend_goes_to_general_while_other_installation_notices_stay_off_telegram(db: Database, manager: SessionManager, front: FakeFront) -> None:
    service = NotificationService(db, manager.bus, front=lambda: front)
    await service.post(Draft("spend", "💸 openrouter balance is $1.00", kind="balance", tone="warning"))
    await service.post(Draft("system", "Service 'web' did not come back", "exit 1", kind="service", tone="warning"))
    await service.post(Draft("system", "Change proposal: tidy", kind="change_proposal"))
    await service.post(Draft("reminder", "pills", handled=frozenset({"telegram"})))
    assert front.notified == [("🔔 💸 openrouter balance is $1.00", False)]
    events = await announced(manager)
    assert [e.payload["deliver"]["telegram"] for e in events] == [True, False, False, True]
    delivered = [e["delivered"]["telegram"] for e in reversed((await service.list("all"))["entries"])]
    assert delivered == ["general", "skipped: off", "skipped: off", "handled"]


async def test_a_chat_that_refuses_the_line_leaves_the_record_and_says_so(db: Database, manager: SessionManager) -> None:
    class Broken(FakeFront):
        async def notify(self, text: str, *, markdown: bool = True) -> None:
            raise RuntimeError("telegram is down")

    broken = Broken()
    service = NotificationService(db, manager.bus, front=lambda: broken)
    view = await service.post(Draft("spend", "budget"))
    assert view is not None and view["delivered"]["telegram"] == "failed: RuntimeError"
    assert (await announced(manager))[0].payload["deliver"]["telegram"] is False


async def test_install_wires_the_router_the_resolvers_and_the_run_cap(settings: Settings, db: Database, manager: SessionManager) -> None:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, notifications=None)
    tasks = await notifications_module.install(app)  # type: ignore[arg-type]
    try:
        service = app.notifications
        assert isinstance(service, NotificationService) and app.extensions["notifications"] is service
        assert set(service._resolvers) == {"ask", "policy", "handoff"} and len(manager.delete_hooks) == 1
        await service.on_event("s1", TurnEvent(type=EventType.ERROR, run_id="r3", payload={"kind": "run_cap", "message": "spent $2"}))
        [entry] = (await service.list("all"))["entries"]
        assert (entry["kind"], entry["category"], entry["tone"], entry["run_id"]) == ("run_cap", "run_failed", "warning", "r3")
    finally:
        for task in tasks:
            task.cancel()


async def test_the_inbox_command_lists_unseen_and_marks_them(settings: Settings, db: Database, manager: SessionManager, service: NotificationService) -> None:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, notifications=service)
    state = await manager.create_session("s")
    await service.post(Draft("system", "Quiet record", level="quiet"))
    await service.post(Draft("run_failed", "Run failed", "boom", tone="error"))
    await service.post(Draft("system", "Twice", dedupe_key="k"))
    await service.post(Draft("system", "Twice", dedupe_key="k"))
    text = await slash.run_command(app, state.session.id, "/inbox")  # type: ignore[arg-type]
    assert "❌" in text and "**Run failed** — boom" in text and "**Twice** ×2" in text and "Quiet record" not in text
    assert await service.summary() == {"unseen": 0, "needs_you": 0}
    assert "nothing unread" in await slash.run_command(app, state.session.id, "/inbox")  # type: ignore[arg-type]
    assert "Quiet record" in await slash.run_command(app, state.session.id, "/inbox all")  # type: ignore[arg-type]
    assert format_entries([]) == ""


async def test_the_routes(settings: Settings, db: Database, manager: SessionManager, service: NotificationService) -> None:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, notifications=service, create_session=manager.create_session)
    first = await service.post(Draft("run_failed", "Run failed", tone="error", session_id="s1"))
    await service.post(Draft("question", "Pick one", request_ref="ask:1", session_id="s2"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.get("/api/notifications")).status_code == 401
        page = (await client.get("/api/notifications", headers=HEAD)).json()
        assert [e["title"] for e in page["entries"]] == ["Pick one", "Run failed"]
        assert page["summary"] == {"unseen": 2, "needs_you": 1} and page["next_before"] is None
        assert [e["title"] for e in (await client.get("/api/notifications?view=problems", headers=HEAD)).json()["entries"]] == ["Run failed"]
        assert (await client.get("/api/notifications?view=bogus", headers=HEAD)).status_code == 422
        assert (await client.get("/api/notifications?limit=0", headers=HEAD)).status_code == 422
        assert (await client.get("/api/notifications/summary", headers=HEAD)).json() == {"unseen": 2, "needs_you": 1}
        assert (await client.post("/api/notifications/seen", json={}, headers=HEAD)).status_code == 422
        assert (await client.post("/api/notifications/seen", json={"ids": [1], "all": True}, headers=HEAD)).status_code == 422
        marked = (await client.post("/api/notifications/seen", json={"session_id": "s1"}, headers=HEAD)).json()
        assert marked == {"marked": 1, "summary": {"unseen": 1, "needs_you": 1}}
        marked = (await client.post("/api/notifications/seen", json={"all": True}, headers=HEAD)).json()
        assert marked["marked"] == 1 and marked["summary"]["unseen"] == 0
        gone = (await client.delete(f"/api/notifications/{first['id']}", headers=HEAD)).json()
        assert gone["deleted"] == first["id"]
        assert (await client.delete(f"/api/notifications/{first['id']}", headers=HEAD)).status_code == 404
        status = (await client.get("/api/status", headers=HEAD)).json()
        assert status["notifications"] == {"unseen": 0, "needs_you": 1} and "inbox_unread" not in status
        assert (await client.get("/api/inbox", headers=HEAD)).status_code == 404


def test_nothing_reaches_for_the_old_inbox() -> None:
    """The inbox extension is gone; a producer still asking for it would post into nothing, silently.
    So is the flag that sent a line to General beside the router: the preferences decide that now."""
    pattern = re.compile(r"""extensions(\.get\(|\[)\s*["']inbox["']|extensions\.inbox|\bapp\.notify\(|telegram_general""")
    offenders = [
        f"{path.relative_to(REPO)}:{number}"
        for path in sorted((REPO / "daedalus").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_the_old_retention_setting_moves_to_the_notifications_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[scheduler]\ninbox_keep_days = 12\ncatch_up_missed = false\n", encoding="utf-8")
    config = RuntimeConfig.load(path)
    assert config.notifications.keep_days == 12 and config.scheduler.catch_up_missed is False
    text = path.read_text(encoding="utf-8")
    assert "inbox_keep_days" not in text and "keep_days = 12" in text
