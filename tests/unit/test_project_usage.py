"""A project's spend, per staff member, its orchestrator and its other sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.project_usage import ProjectUsage, tokens_words
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import project_with, repository

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)


async def spend(manager: SessionManager, session_id: str, usd: float | None, tokens: int, *, at: datetime = NOW) -> None:
    await manager.db.execute(
        "INSERT INTO usage_events(at, provider_id, model, purpose, session_id, input_tokens, output_tokens, cost_usd, raw) VALUES (?, 'p', 'm', 'stream', ?, ?, ?, ?, '{}')",
        (at.isoformat(), session_id, tokens // 2, tokens - tokens // 2, usd),
    )


async def staff_session(manager: SessionManager, staff_id: str, *, kind: str, session_id: str | None = None, usage: dict | None = None, at: datetime = NOW) -> None:
    await manager.db.execute(
        "INSERT INTO staff_sessions(id, staff_id, kind, session_id, status, status_at, started_at, ended_at, usage_json) VALUES (?, ?, ?, ?, 'idle', ?, ?, ?, ?)",
        (f"ss-{staff_id}-{kind}-{session_id}-{at.isoformat()}", staff_id, kind, session_id, at.isoformat(), at.isoformat(), at.isoformat(), json.dumps(usage or {})),
    )


async def test_the_spend_is_split_by_who_spent_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        project = await project_with(manager, repository(tmp_path), orchestrator=False)
        ada = await manager.staff.hire(project.id, name="Ada", isolation="shared")
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="shared")
        orch = await manager.create_session("Orchestrator", metadata={"orchestrator_of": project.id}, project_id=project.id)
        retired = await manager.create_session("Old orchestrator", metadata={"orchestrator_retired_of": project.id}, project_id=project.id)
        ada_s = await manager.create_session("Ada · Menu", metadata={"staff_id": ada.id}, project_id=project.id)
        helper = await manager.create_session("[sub] helper", metadata={"subagent_of": ada_s.session.id}, project_id=project.id)
        grandchild = await manager.create_session("[sub] deeper", metadata={"subagent_of": helper.session.id}, project_id=project.id)
        plain = await manager.create_session("scratch", project_id=project.id)
        elsewhere = await manager.create_session("not this project")
        await staff_session(manager, ada.id, kind="daedalus", session_id=ada_s.session.id, usage={"input_tokens": 999, "output_tokens": 999, "cost_usd": 9.0, "source": "metered"})

        await spend(manager, orch.session.id, 0.50, 1000)
        await spend(manager, retired.session.id, 0.25, 500, at=NOW - timedelta(days=3))
        await spend(manager, ada_s.session.id, 1.00, 4000)
        await spend(manager, helper.session.id, 0.20, 600)
        await spend(manager, grandchild.session.id, None, 400)
        await spend(manager, ada_s.session.id, 2.00, 8000, at=NOW - timedelta(days=30))
        await spend(manager, plain.session.id, 0.10, 100, at=NOW - timedelta(days=2))
        await spend(manager, elsewhere.session.id, 5.00, 5000)
        # A command-line member: the runtime's latest snapshot, one on the subscription, one metered and old.
        await staff_session(manager, cleo.id, kind="cli", usage={"input_tokens": 300_000, "output_tokens": 112_000, "cost_usd": None, "window_used_pct": 23.0, "source": "subscription"})
        await staff_session(manager, cleo.id, kind="cli", usage={"input_tokens": 1000, "output_tokens": 0, "cost_usd": 0.4, "window_used_pct": None, "source": "metered"}, at=NOW - timedelta(days=20))

        usage = await ProjectUsage(manager).summary(project.id, now=NOW)
        rows = {row["name"]: row for row in usage["staff"]}
        # Ada: her session and both generations of subagents, once each; her snapshot is not added on top.
        assert rows["Ada"]["today"] == {"usd": 1.2, "tokens": 5000, "unpriced": 1, "subscriptions": 0}
        assert rows["Ada"]["all"] == {"usd": 3.2, "tokens": 13000, "unpriced": 1, "subscriptions": 0}
        assert rows["Cleo"]["today"] == {"usd": 0.0, "tokens": 412_000, "unpriced": 0, "subscriptions": 1}
        assert rows["Cleo"]["all"] == {"usd": 0.4, "tokens": 413_000, "unpriced": 0, "subscriptions": 1}
        assert rows["Cleo"]["subscription"]["window_used_pct"] == 23.0 and rows["Cleo"]["harness"] == "claude"
        # Every generation of the orchestrator is the orchestrator's.
        assert usage["orchestrator"]["today"]["usd"] == 0.5 and usage["orchestrator"]["week"]["usd"] == 0.75
        assert usage["other"]["today"]["usd"] == 0.0 and usage["other"]["week"]["usd"] == 0.1
        assert usage["total"]["all"] == {"usd": 4.45, "tokens": 427_600, "unpriced": 1,
                                         "subscriptions": 1}, "another project's spend is not here"
        assert usage["since"]["today"] == NOW.replace(hour=0).isoformat()
    finally:
        await manager.close()


async def test_today_is_the_operators_day(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        manager.presence._locale = ("en", "Asia/Tokyo")  # UTC+9: at 15:00 UTC it is already the 26th there
        starts = ProjectUsage(manager).starts(NOW)
        assert starts["today"] == "2026-09-25T15:00:00+00:00" and starts["week"] == "2026-09-19T15:00:00+00:00"
        assert tokens_words(412_000) == "412k" and tokens_words(1_500_000) == "1.5M" and tokens_words(12) == "12"
    finally:
        await manager.close()


async def test_the_usage_route(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        project = await project_with(manager, repository(tmp_path), orchestrator=False)
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            assert (await client.get(f"/api/projects/{project.id}/usage")).status_code == 401
            got = await client.get(f"/api/projects/{project.id}/usage", headers={"X-Daedalus-Token": "tok"})
            assert got.status_code == 200 and got.json()["total"]["all"] == {"usd": 0.0, "tokens": 0,
                                                                              "unpriced": 0, "subscriptions": 0}
            assert (await client.get("/api/projects/nope/usage", headers={"X-Daedalus-Token": "tok"})).status_code == 404
    finally:
        await manager.close()
