"""The quota forecast: readings kept per window, a robust burn rate, and the time to run out with a band."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.quota import (
    Sample,
    current_window,
    forecast,
    quota_forecast,
    record,
    record_cli,
    record_subscriptions,
    reset_time,
    window_name,
)

T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
RESET = T0 + timedelta(hours=5)


def series(points: list[tuple[float, float]], *, resets: datetime | None = RESET) -> list[Sample]:
    return [Sample(T0 + timedelta(minutes=m), pct, resets) for m, pct in points]


def test_fewer_than_three_readings_are_not_a_trend() -> None:
    found = forecast("codex", "5h", series([(0, 10), (10, 12)]))
    assert (found.status, found.samples, found.eta_at, found.burn_per_hour) == ("insufficient", 2, None, None)
    assert found.used_percent == 12 and not found.runs_out_within(10**9)


def test_a_steady_burn_gives_its_rate_and_when_it_runs_out() -> None:
    # 6 points an hour from 10 %: 90 points left is 15 hours away, after the reset in 5.
    found = forecast("codex", "5h", series([(m, 10 + m / 10) for m in range(0, 61, 10)]))
    assert found.status == "ok" and found.burn_per_hour == 6.0
    eta = datetime.fromisoformat(found.eta_at or "")
    assert eta == T0 + timedelta(minutes=60) + timedelta(hours=84 / 6)
    assert found.eta_early_at == found.eta_late_at == found.eta_at, "a perfectly steady pace has no spread"
    assert found.exhausts_before_reset is False
    assert not found.runs_out_within(3600 * 48, now=T0), "a window that resets first does not run out"


def test_one_wild_reading_barely_moves_the_rate_and_widens_the_band() -> None:
    points = [(m, 50 + m / 2) for m in range(0, 61, 10)]
    points[3] = (30, 90)  # a burst, or a share the provider rounded badly
    found = forecast("claude", "five_hour", series(points, resets=T0 + timedelta(days=1)))
    assert found.status == "ok" and 25 <= (found.burn_per_hour or 0) <= 40, found.burn_per_hour
    early, median = datetime.fromisoformat(found.eta_early_at or ""), datetime.fromisoformat(found.eta_at or "")
    assert early < median and found.exhausts_before_reset is True
    assert found.runs_out_within(3600 * 3, now=T0 + timedelta(hours=1))


def test_a_window_spending_nothing_is_idle_and_a_full_one_is_exhausted() -> None:
    assert forecast("grok", "weekly", series([(0, 40), (10, 40), (20, 40)])).status == "idle"
    full = forecast("grok", "weekly", series([(0, 99), (10, 100)]))
    assert full.status == "exhausted" and full.runs_out_within(0)


def test_only_the_open_window_counts() -> None:
    old = series([(0, 80), (10, 90)], resets=T0)
    new = series([(20, 1), (30, 2), (40, 3)])
    assert [s.used_percent for s in current_window([*new, *old])] == [1, 2, 3]
    # Without a reset time a fall in the share is the reset, and the window's length bounds it.
    cli = [Sample(T0 + timedelta(minutes=m), p, None, 300 * 60) for m, p in ((0, 70), (10, 75), (20, 2), (30, 4))]
    assert [s.used_percent for s in current_window(cli)] == [2, 4]
    stale = [Sample(T0 + timedelta(hours=h), p, None, 300 * 60) for h, p in ((0, 1), (6, 2), (7, 3))]
    assert [s.used_percent for s in current_window(stale)] == [2, 3]


def test_reset_times_and_window_names() -> None:
    assert reset_time(1760515200) == datetime(2025, 10, 15, 8, 0, tzinfo=UTC)
    assert reset_time("2026-10-15T13:00:00Z") == RESET and reset_time(None) is None and reset_time(0) is None
    assert [window_name(m) for m in (300, 10080, 90)] == ["5h", "7d", "90m"]


async def test_readings_are_kept_spaced_bounded_and_forecast(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        answer = {"codex": {"logged_in": True, "windows": [{"name": "5h", "used_percent": 10, "resets_at": RESET.timestamp()},
                                                           {"name": "weekly", "used_percent": 3, "resets_at": None}]},
                  "grok": {"logged_in": False}, "claude": {"logged_in": True, "error": "expired"}}
        assert await record_subscriptions(db, answer, at=T0) == 2
        assert await record_subscriptions(db, answer, at=T0 + timedelta(seconds=30)) == 0, "a reading a minute apart is the same reading"
        for step in (1, 2, 3):
            answer["codex"]["windows"][0]["used_percent"] = 10 + step * 2  # type: ignore[index]
            await record_subscriptions(db, answer, at=T0 + timedelta(minutes=10 * step))
        await record_cli(db, "codex", "container", [(300, 20.0)], at=T0)
        await record(db, "codex", [{"name": "ancient", "used_percent": 1}], source="keyproxy", at=T0 - timedelta(days=9))
        await record(db, "codex", [{"name": "5h", "used_percent": True}, {"name": "", "used_percent": 4}], source="keyproxy", at=T0 + timedelta(hours=1))
        found = {(f.account, f.window): f for f in await quota_forecast(db)}
        assert set(found) == {("codex", "5h"), ("codex", "weekly"), ("cli:codex@container", "5h")}, "the old reading went with the next write"
        five = found[("codex", "5h")]
        assert five.status == "ok" and five.samples == 4 and five.burn_per_hour == 12.0
        assert found[("cli:codex@container", "5h")].status == "insufficient"
        assert [f.window for f in await quota_forecast(db, account="codex", window="weekly")] == ["weekly"]
    finally:
        await db.close()


async def test_the_usage_answer_carries_the_forecasts(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        now = datetime.now(UTC)
        for step in range(4):
            await record(db, "cli:codex@container", [{"name": "5h", "used_percent": 10 + step, "window_seconds": 18000}],
                         source="cli", at=now - timedelta(minutes=40 - 10 * step))
        app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            answer = (await client.get("/api/usage", headers={"X-Daedalus-Token": "tok"})).json()
        [found] = answer["forecasts"]
        assert (found["account"], found["window"], found["status"], found["burn_per_hour"]) == ("cli:codex@container", "5h", "ok", 6.0)
    finally:
        await manager.close()
