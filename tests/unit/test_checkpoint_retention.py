"""Retention for the workspace snapshot stores: what goes, what never goes, and what it frees.

The stores are on disk and the point of the exercise is disk, so these tests build real ones —
real commits over real files — and measure the directories afterwards rather than trusting the
bookkeeping.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.__main__ import build_parser, cmd_db
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.checkpoint_retention import RetentionBounds
from daedalus.host.checkpoints import Checkpoints, store_dir
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    made = SessionManager(settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def client(settings: Settings, db: Database, manager: SessionManager) -> Any:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


async def _snapshots(manager: SessionManager, state: Any, count: int, *, payload: int = 0) -> list[str]:
    """``count`` real snapshots of the session's workspace, each over a file that changed."""
    shas: list[str] = []
    for i in range(count):
        (state.workspace / "notes.md").write_text(f"turn {i}\n")
        if payload:
            # The same file every time: a snapshot is only worth bytes on disk once the content it
            # held is in no later tree, which is what a workspace being worked in looks like.
            (state.workspace / "blob.bin").write_bytes(os.urandom(payload))
        sha = await manager.checkpoint(state, kind="before", seq=i)
        assert sha is not None
        shas.append(sha)
    return shas


async def _backdate(db: Database, session_id: str, older_than: int, days: float) -> None:
    """Age the first ``older_than`` snapshots of a session by ``days``."""
    rows = await db.fetchall("SELECT id FROM checkpoints WHERE session_id = ? ORDER BY id", (session_id,))
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    for row in rows[:older_than]:
        await db.execute("UPDATE checkpoints SET at = ? WHERE id = ?", (stamp, row["id"]))


def _store_size(store: Path) -> int:
    return sum(os.lstat(os.path.join(root, name)).st_size for root, _dirs, files in os.walk(store) for name in files)


async def _rows(db: Database, session_id: str) -> list[Any]:
    return await db.fetchall("SELECT id, sha, at FROM checkpoints WHERE session_id = ? ORDER BY id", (session_id,))


async def test_the_last_snapshots_of_a_session_survive_both_bounds(manager: SessionManager, db: Database) -> None:
    """Whatever the age and the size say, a session keeps its newest ``keep_last`` — that is the undo
    the operator actually reaches for, and a store somebody else filled must not take it away."""
    state = await manager.create_session("busy")
    shas = await _snapshots(manager, state, 14)
    await _backdate(db, state.session.id, 14, days=400)
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=1, total_max_gb=0.000000001, keep_last=5))
    kept = await _rows(db, state.session.id)
    assert [row["sha"] for row in kept] == shas[-5:]
    assert report.dropped == 9 and report.cut == 1
    # And the oldest survivor is still a snapshot the workspace can be put back to.
    (state.workspace / "notes.md").write_text("changed since")
    await Checkpoints(state.workspace, manager.settings.checkpoints_dir).restore(kept[0]["sha"])
    assert (state.workspace / "notes.md").read_text() == "turn 9\n"


async def test_snapshots_older_than_the_age_bound_go(manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("aged")
    shas = await _snapshots(manager, state, 10)
    await _backdate(db, state.session.id, 6, days=45)
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert [row["sha"] for row in await _rows(db, state.session.id)] == shas[6:]
    assert report.dropped == 6

    # Run again with nothing old enough: the pass leaves the store alone.
    again = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert again.dropped == 0 and again.cut == 0


async def test_a_store_over_the_size_bound_loses_its_oldest(manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("fat")
    await _snapshots(manager, state, 8, payload=256_000)
    before = _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace))
    assert before > 1_500_000, "the synthetic store is not big enough to say anything about size"
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=3650, total_max_gb=before / 2e9, keep_last=2))
    after = _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace))
    assert report.dropped > 0, "nothing was dropped for a store at twice its bound"
    assert after < before and report.freed == before - after
    assert len(await _rows(db, state.session.id)) >= 2


async def test_pruning_gives_the_disk_back(manager: SessionManager, db: Database) -> None:
    """The measurement the whole unit is for: a cut store is smaller on the filesystem afterwards.

    Deleting rows and moving a ref frees nothing by itself — the snapshots of a workspace are one
    chain of commits, and the objects go only once the chain is cut and ``git gc`` has run.
    """
    state = await manager.create_session("measured")
    await _snapshots(manager, state, 12, payload=500_000)
    before = _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace))
    await _backdate(db, state.session.id, 9, days=90)
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=3))
    after = _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace))
    assert report.dropped == 9
    # Nine of twelve snapshots, each carrying half a megabyte that compresses to nothing.
    assert after < before * 0.6, f"{before} -> {after}: the objects were not collected"
    assert report.freed == before - after


async def test_a_subagent_does_not_lose_the_snapshots_its_leader_shares(manager: SessionManager, db: Database) -> None:
    """Two sessions in one workspace are one chain of commits; the promise is per session, so the
    boundary has to respect the newest of each of them."""
    leader = await manager.create_session("leader")
    follower = await manager.create_session("follower", workspace=leader.workspace, metadata={"workspace": str(leader.workspace)})
    early = await _snapshots(manager, follower, 3)
    await _snapshots(manager, leader, 12)
    await _backdate(db, leader.session.id, 12, days=400)
    await _backdate(db, follower.session.id, 3, days=400)
    await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=3))
    assert [row["sha"] for row in await _rows(db, follower.session.id)] == early, "the follower's three were cut away by the leader's fifteen"


async def test_a_store_a_session_is_working_in_is_left_for_the_next_pass(manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("running")
    await _snapshots(manager, state, 6)
    await _backdate(db, state.session.id, 6, days=400)
    state.task = asyncio.create_task(asyncio.Event().wait())
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert report.dropped == 0 and report.skipped == 1
    state.task.cancel()
    state.task = None
    assert (await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))).dropped == 4


async def test_a_session_with_no_snapshots_yet_still_makes_its_store_busy(manager: SessionManager, db: Database) -> None:
    """A subagent sharing its leader's workspace has no ``checkpoints`` rows until it takes its first
    snapshot — and taking it is a ``git add`` into the chain a pass would otherwise consider idle."""
    leader = await manager.create_session("leader")
    await _snapshots(manager, leader, 6)
    await _backdate(db, leader.session.id, 6, days=400)
    follower = await manager.create_session("follower", workspace=leader.workspace, metadata={"workspace": str(leader.workspace)})
    assert await _rows(db, follower.session.id) == [], "the follower has snapshots; the case under test is that it has none"
    follower.task = asyncio.create_task(asyncio.Event().wait())

    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert report.dropped == 0 and report.skipped == 1, "a store being written into was packed"
    follower.task.cancel()
    follower.task = None
    assert (await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))).dropped == 4


async def test_a_session_that_starts_between_two_cuts_keeps_its_store(manager: SessionManager, db: Database) -> None:
    """``_usable`` decided once per pass, and every cut in between is a ``git gc --prune=now``: the
    two-week grace on loose objects is exactly what the session that just started is writing into."""
    first = await manager.create_session("first")
    second = await manager.create_session("second")
    for state in (first, second):
        await _snapshots(manager, state, 6)
        await _backdate(db, state.session.id, 6, days=400)

    retention = manager.checkpoint_retention
    real_cut = retention._cut
    started: list[str] = []

    async def cut_and_start_a_run(store: Any, count: int, report: Any) -> None:
        if not started:
            started.append("yes")
            second.task = asyncio.create_task(asyncio.Event().wait())  # a run begins while the first store is packed
        await real_cut(store, count, report)

    retention._cut = cut_and_start_a_run  # type: ignore[assignment]
    report = await retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert len(await _rows(db, second.session.id)) == 6, "the store of a session that started mid-pass was cut"
    assert report.dropped == 4
    second.task.cancel()
    second.task = None


async def test_the_store_size_is_measured_again_only_when_the_store_was_written_to(manager: SessionManager) -> None:
    """A gigabyte of stores walked on every maintenance tick to learn that nothing changed is the
    cost the cache exists to avoid; a snapshot taken since is the thing that must invalidate it."""
    state = await manager.create_session("cached")
    await _snapshots(manager, state, 2)
    first = await manager.checkpoint_retention.total_size()
    walks = manager.checkpoint_retention.sizes.walks
    assert first > 0 and walks == 1
    assert await manager.checkpoint_retention.total_size() == first
    assert manager.checkpoint_retention.sizes.walks == walks, "the second read walked the store again"
    await _snapshots(manager, state, 1, payload=200_000)
    grown = await manager.checkpoint_retention.total_size()
    assert manager.checkpoint_retention.sizes.walks == walks + 1 and grown > first


async def test_the_revert_list_shows_what_is_left_and_says_what_went(client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("listed")
    shas = await _snapshots(manager, state, 8)
    sid = state.session.id
    listed = (await client.get(f"/api/sessions/{sid}/checkpoints", headers=H)).json()
    assert [c["sha"] for c in listed["checkpoints"]] == shas and not listed["pruned"] and listed["note"] == ""

    await _backdate(db, sid, 5, days=90)
    await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    after = (await client.get(f"/api/sessions/{sid}/checkpoints", headers=H)).json()
    assert [c["sha"] for c in after["checkpoints"]] == shas[5:], "the list still offers snapshots that are gone"
    assert after["pruned"] and after["removed"] == 5
    assert after["note"] == "older checkpoints were removed by retention"
    assert after["pruned_before"] == after["checkpoints"][0]["at"]
    assert (await client.get("/api/sessions/nope/checkpoints", headers=H)).status_code == 404


async def test_deleting_a_session_takes_its_retention_record_with_it(manager: SessionManager, db: Database) -> None:
    state = await manager.create_session("gone")
    await _snapshots(manager, state, 6)
    await _backdate(db, state.session.id, 6, days=400)
    await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=2))
    assert await db.fetchone("SELECT session_id FROM checkpoint_retention WHERE session_id = ?", (state.session.id,))
    await manager.delete_session(state.session.id)
    assert await db.fetchone("SELECT session_id FROM checkpoint_retention WHERE session_id = ?", (state.session.id,)) is None


async def test_a_store_the_process_cannot_see_keeps_its_rows(manager: SessionManager, db: Database, settings: Settings) -> None:
    """A project's folder is unreachable until it is mounted, and an unreachable folder is not an
    empty one: nothing may be dropped on the strength of a directory that is not there."""
    state = await manager.create_session("unmounted")
    await _snapshots(manager, state, 5)
    await _backdate(db, state.session.id, 5, days=400)
    os.rename(store_dir(settings.checkpoints_dir, state.workspace), settings.checkpoints_dir / "moved-away")
    report = await manager.checkpoint_retention.run(RetentionBounds(keep_days=30, total_max_gb=0, keep_last=1))
    assert report.dropped == 0 and report.skipped == 1
    assert len(await _rows(db, state.session.id)) == 5


async def test_the_cli_runs_the_pass_and_prints_what_it_freed(manager: SessionManager, db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    state = await manager.create_session("by hand")
    await _snapshots(manager, state, 8, payload=300_000)
    await _backdate(db, state.session.id, 6, days=400)
    before = _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace))
    await db.close()

    args = build_parser().parse_args(["--state-dir", str(settings.state_dir), "--workspaces-dir", str(settings.workspaces_dir), "db", "checkpoints-prune"])
    assert await cmd_db(args) == 0
    printed = capsys.readouterr().out
    assert "bounds: 30 days, 2.0 GB in total, the last 50 per session always kept" in printed
    # The defaults keep the last fifty, so this store keeps all eight and the pass frees nothing:
    # that it says so, in bytes, is the point.
    assert "0 checkpoint(s) dropped" in printed and "MB freed" in printed
    assert _store_size(store_dir(manager.settings.checkpoints_dir, state.workspace)) == before

    await db.open()


async def test_the_maintenance_tick_runs_the_pass_and_says_what_it_freed(manager: SessionManager, db: Database, settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """Retention rides the tick the event sweep already runs on, at the same interval."""
    from daedalus.extensions.scheduler import Scheduler

    state = await manager.create_session("ticked")
    await _snapshots(manager, state, 8, payload=200_000)
    await _backdate(db, state.session.id, 6, days=400)
    manager.config.ops.checkpoint_keep_last = 2
    scheduler = Scheduler(SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, extensions={}))  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        await scheduler._maintain_database(datetime.now(UTC))
        # The pass is a task of its own: the tick starts it and goes on dispatching cron and
        # heartbeats while a store is being packed. Nothing waits for it but a shutdown.
        await scheduler.drain_retention()
    assert len(await _rows(db, state.session.id)) == 2
    line = next(r.getMessage() for r in caplog.records if "checkpoint retention" in r.getMessage())
    assert "6 checkpoint(s) dropped from 1 store(s)" in line and "MB freed" in line

    # The interval is the tick's: a second call minutes later does not walk the stores again.
    caplog.clear()
    with caplog.at_level("WARNING"):
        await scheduler._maintain_database(datetime.now(UTC))
        await scheduler.drain_retention()
    assert not [r for r in caplog.records if "checkpoint retention" in r.getMessage()]


async def test_doctor_shows_the_store_against_its_bounds(manager: SessionManager, db: Database, settings: Settings) -> None:
    from daedalus.doctor import DoctorContext, run_checks

    state = await manager.create_session("checked")
    await _snapshots(manager, state, 3, payload=200_000)
    manager.config.ops.checkpoint_total_max_gb = 2.0
    checks = await run_checks(DoctorContext(settings=settings, config=manager.config, db=db, manager=manager))
    line = next(c for c in checks if c.name == "checkpoint store")
    assert line.ok and "GB of workspace snapshots" in line.message and "last 50 per session" in line.message

    manager.config.ops.checkpoint_total_max_gb = 0.0000001
    manager.checkpoint_retention.sizes.forget(store_dir(settings.checkpoints_dir, state.workspace))
    over = next(c for c in await run_checks(DoctorContext(settings=settings, config=manager.config, db=db, manager=manager)) if c.name == "checkpoint store")
    assert not over.ok and over.severity == "warn" and "checkpoints-prune" in over.fix_hint
