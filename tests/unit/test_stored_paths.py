"""Stored absolute paths that a later change made unreachable, and the start that has to survive them.

A row written by one build is replayed by the next. Between the two the session's folder can become
a project's folder, and the containment that follows says the path the row holds is one this session
may not touch any more. Every such replay here reports and carries on; none of them is allowed to be
the reason the bot does not start.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from daedalus import doctor
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import install_all
from daedalus.extensions.notifications import NotificationService
from daedalus.extensions.recurring import Recurring
from daedalus.extensions.scheduler import Scheduler
from daedalus.extensions.services import Services
from daedalus.host.session_runner import SessionManager
from daedalus.stores.control import Principal
from daedalus.stores.database import Database


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    settings.services_port_range = "18140-18149"
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, extension_failures={})
    app.notifications = NotificationService(db, manager.bus)
    yield app
    await manager.close()


async def _stranded_row(app: Any, services: Services, *, owner: str, name: str, cwd: str, log_path: str) -> None:
    """The shape a migration leaves behind: a running service whose directory belongs to someone else."""
    await app.db.execute(
        "INSERT INTO services(session_id, name, command, cwd, port, pid, status, restart, log_path, note, started_at, stopped_at)"
        " VALUES (?, ?, 'python3 -m http.server $PORT', ?, NULL, 999999, 'running', 1, ?, NULL, '2026-01-01T00:00:00+00:00', NULL)",
        (owner, name, cwd, log_path),
    )


async def test_a_service_pointing_into_another_sessions_folder_is_reported_not_fatal(app: Any) -> None:
    """Today's shape exactly: the row is left dead with both paths, the other services still start."""
    neighbour = await app.manager.create_session("the site")
    owner = await app.manager.create_session("serve it on the network")
    (neighbour.workspace / "test-site").mkdir(parents=True, exist_ok=True)
    services = Services(app)
    healthy = await services.start(owner.session.id, name="healthy", command="sleep 30", restart=True)
    await services._terminate(int(healthy["pid"]))  # the container was rebuilt: nothing is running any more
    await _stranded_row(
        app,
        services,
        owner=owner.session.id,
        name="test-site-lan",
        cwd=str(neighbour.workspace / "test-site"),
        log_path=str(owner.workspace / ".services" / "test-site-lan.log"),
    )

    await services.reconcile()  # must not raise: this is what killed the process

    rows = {r["name"]: r for r in await services.list(owner.session.id)}
    assert rows["healthy"]["status"] == "running" and rows["healthy"]["pid"] != healthy["pid"]
    stranded = rows["test-site-lan"]
    assert stranded["status"] == "dead"
    assert str(neighbour.workspace / "test-site") in stranded["note"] and str(owner.workspace) in stranded["note"]
    assert "left as it is" in stranded["note"]
    titles = [e["title"] for e in await app.db.fetchall("SELECT title FROM notifications WHERE session_id = ?", (owner.session.id,))]
    assert "Service 'test-site-lan' was not restarted" in titles
    checks = await doctor.run_checks(doctor.DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=app.manager))
    assert any(c.name == "services" and "may not reach" in c.message for c in checks)
    await services.stop_all(owner.session.id)
    await asyncio.sleep(0)


async def test_a_folder_of_the_same_name_in_the_session_is_offered_but_never_taken(app: Any) -> None:
    """Re-pointing is the operator's decision: the same command in another folder serves other files."""
    neighbour = await app.manager.create_session("the site")
    owner = await app.manager.create_session("serve it")
    (neighbour.workspace / "test-site").mkdir(parents=True, exist_ok=True)
    (owner.workspace / "test-site").mkdir(parents=True, exist_ok=True)
    services = Services(app)
    await _stranded_row(app, services, owner=owner.session.id, name="site", cwd=str(neighbour.workspace / "test-site"), log_path=str(owner.workspace / "site.log"))
    await services.reconcile()
    row = await services.get(owner.session.id, "site")
    assert row["status"] == "dead" and row["cwd"] == str(neighbour.workspace / "test-site")  # not re-pointed
    assert str(owner.workspace / "test-site") in row["note"] and "ServiceStart the service there" in row["note"]


async def test_a_service_log_outside_the_session_folder_is_not_read(app: Any) -> None:
    neighbour = await app.manager.create_session("the site")
    owner = await app.manager.create_session("serve it")
    secret = neighbour.workspace / "private.log"
    secret.write_text("the neighbour's log\n")
    services = Services(app)
    await _stranded_row(app, services, owner=owner.session.id, name="site", cwd=str(owner.workspace), log_path=str(secret))
    text = await services.logs(owner.session.id, "site")
    assert "the neighbour's log" not in text and str(secret) in text and str(owner.workspace) in text


async def test_a_session_whose_directory_left_its_project_is_reported_not_fatal(app: Any) -> None:
    """``get_state`` is on every path into the process; one bad row must not raise through all of them."""
    state = await app.manager.create_session("moved", own_directory=True)
    sid = state.session.id
    await app.db.execute("UPDATE sessions SET metadata = ? WHERE id = ?", ('{"directory": "../elsewhere"}', sid))
    app.manager._states.pop(sid, None)

    assert await app.manager.get_state(sid) is None
    assert sid in app.manager.unloadable_sessions and "project directory" in app.manager.unloadable_sessions[sid]
    checks = await doctor.run_checks(doctor.DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=app.manager))
    assert any(c.name == "sessions" and sid in c.message for c in checks)
    # And the subsystems that replay stored rows over it survive the same way.
    await Scheduler(app).restore()
    await Services(app).reconcile()


async def test_a_schedule_whose_folder_is_gone_records_a_failure_instead_of_making_it(app: Any) -> None:
    scheduler = Scheduler(app)
    app.extensions["recurring"] = Recurring(app)
    revision = await app.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
    created = await app.extensions["recurring"].create(
        Principal("operator:1", "operator"), name="nightly", prompt="check the feed",
        cron="0 3 * * *", run_at=None, kind="agent", target_session=None, project_id=None,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=revision["revision"], client_operation_id="folder-approval",
    )
    gone = app.settings.workspaces_dir / "removed-by-the-operator"
    await app.db.execute("UPDATE schedules SET workspace = ?, next_run_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (str(gone), created["id"]))

    await scheduler.tick()  # must not raise, and must not re-create the folder

    assert not gone.exists()
    row = await app.db.fetchone("SELECT failure_count, last_error FROM schedules WHERE id = ?", (created["id"],))
    assert row["failure_count"] == 1 and "is not there" in row["last_error"]
    titles = [e["title"] for e in (await app.notifications.list())["entries"]]
    assert any("could not start" in t for t in titles)


def _extension(name: str, *, fails: bool) -> ModuleType:
    module = ModuleType(name)

    async def install(app: Any) -> list[asyncio.Task[None]]:
        if fails:
            raise RuntimeError("the port it wanted is taken")
        app.extensions[name.rsplit(".", 1)[-1]] = object()
        return []

    module.install = install  # type: ignore[attr-defined]
    return module


async def test_a_failing_extension_is_isolated_reported_and_the_rest_still_install(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    extensions = importlib.import_module("daedalus.extensions")
    for name, fails in (("daedalus.extensions.broken", True), ("daedalus.extensions.working", False)):
        monkeypatch.setitem(sys.modules, name, _extension(name, fails=fails))
    monkeypatch.setattr(extensions, "EXTENSIONS", ("daedalus.extensions.notifications", "daedalus.extensions.broken", "daedalus.extensions.working"))
    app.extensions.clear()

    await install_all(app)

    assert "working" in app.extensions and "broken" not in app.extensions
    assert app.extension_failures == {"broken": "RuntimeError: the port it wanted is taken"}
    entries = (await app.notifications.list())["entries"]
    assert "notifications" in app.extensions
    assert any(e["title"] == "The broken subsystem did not start" and "port it wanted is taken" in e["body"] for e in entries)
    checks = await doctor.run_checks(doctor.DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=app.manager, extension_failures=app.extension_failures))
    assert any(c.name == "extensions" and "broken" in c.message for c in checks)


async def test_the_channel_the_failures_are_reported_through_is_deliberately_fatal(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    extensions = importlib.import_module("daedalus.extensions")
    monkeypatch.setitem(sys.modules, "daedalus.extensions.notifications", _extension("daedalus.extensions.notifications", fails=True))
    monkeypatch.setattr(extensions, "EXTENSIONS", ("daedalus.extensions.notifications", "daedalus.extensions.working"))
    with pytest.raises(RuntimeError, match="the port it wanted is taken"):
        await install_all(app)
