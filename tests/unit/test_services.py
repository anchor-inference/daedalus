"""Services: a detached process with a published port, its log, stopping, and what a restart does to it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.services import Services, parse_range, pid_alive
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications
from tests.support.waiting import until_await


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    settings.services_port_range = "18100-18103"
    settings.services_public_host = "10.0.0.5"
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    app.notifications = RecordingNotifications()
    yield app
    await manager.close()


@pytest.fixture
def brief_start_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests whose services are meant to stay up: the start's grace is not what they test.

    Every start waits a second and a half to tell a broken command from a service, and the tests
    that start two or three long-running services sat through all of it. A command that is meant
    to die is still judged with the full grace, in the test about exactly that.
    """
    from daedalus.extensions import services as services_module

    monkeypatch.setattr(services_module, "START_GRACE_SECONDS", 0.05)


def test_port_range_parsing() -> None:
    assert parse_range("8100-8119") == (8100, 8119) and parse_range("9000") == (9000, 9000)
    with pytest.raises(ValueError):
        parse_range("9-1")


@pytest.mark.usefixtures("brief_start_grace")
async def test_service_runs_detached_with_a_port_and_a_log(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="Web Demo", command='echo "up on $PORT"; python3 -c "import time; print(\'serving\', flush=True); time.sleep(30)"')
    assert s["name"] == "web-demo" and s["status"] == "running" and 18100 <= s["port"] <= 18103
    assert s["url"] == f"http://10.0.0.5:{s['port']}" and pid_alive(s["pid"])
    # The grace no longer gives the command time to print, so the test waits for the log itself.
    await until_await(lambda: _logged(services, sid, "web-demo", "serving"), "the service wrote to its log")
    assert "serving" in await services.logs(sid, "web-demo") and f"up on {s['port']}" in await services.logs(sid, "web-demo")
    with pytest.raises(ValueError, match="already running"):
        await services.start(sid, name="web-demo", command="sleep 5")
    second = await services.start(sid, name="other", command="sleep 30")
    assert second["port"] != s["port"]
    listed = await services.list(sid)
    assert {x["name"] for x in listed} == {"web-demo", "other"} and all(x["status"] == "running" for x in listed)
    stopped = await services.stop(sid, "web-demo")
    assert stopped["status"] == "stopped" and not pid_alive(s["pid"])
    assert (await services.get(sid, "web-demo"))["restart"] == 0
    assert await services.stop_all(sid) == 1 and await services.list(sid) == []


async def _logged(services: Services, session_id: str, name: str, text: str) -> bool:
    return text in await services.logs(session_id, name)


async def test_a_command_that_dies_at_once_is_reported_with_its_log(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    with pytest.raises(RuntimeError, match="exited with code 3"):
        await services.start(state.session.id, name="bad", command="echo boom; exit 3")
    assert (await services.get(state.session.id, "bad"))["status"] == "dead"
    with pytest.raises(ValueError, match="outside the published range"):
        await services.start(state.session.id, name="p", command="sleep 5", port=1)


@pytest.mark.usefixtures("brief_start_grace")
async def test_reconcile_restarts_or_reports_after_a_rebuild(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    kept = await services.start(sid, name="keep", command="sleep 30", restart=True)
    gone = await services.start(sid, name="gone", command="sleep 30", restart=False)
    # the container was rebuilt: both processes are dead, the table still says running
    await services._terminate(int(kept["pid"]))
    await services._terminate(int(gone["pid"]))
    await services.reconcile()
    rows = {r["name"]: r for r in await services.list(sid)}
    assert rows["keep"]["status"] == "running" and rows["keep"]["pid"] != kept["pid"]
    assert rows["gone"]["status"] == "dead" and "not running after the restart" in rows["gone"]["note"]
    assert any("gone" in d.title for d in app.notifications.drafts if d.session_id == sid)
    await services.stop_all(sid)
    await asyncio.sleep(0)


@pytest.mark.usefixtures("brief_start_grace")
async def test_sharing_mints_a_slug_once_and_a_key_only_for_key_mode(app: Any) -> None:
    app.settings.miniapp_public_url = "https://daedalus.example.com/app/"
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="site", command="sleep 30")
    assert s["share"] == {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://daedalus.example.com"}
    public = await services.share(sid, "site", "public")
    slug = public["share"]["slug"]
    assert slug and slug.startswith("site-") and len(slug) >= 5 + 24 and public["share"]["key"] is None
    assert public["share"]["url"] == f"https://daedalus.example.com/s/{slug}/"
    keyed = await services.share(sid, "site", "key")
    key = keyed["share"]["key"]
    assert keyed["share"]["slug"] == slug and key and keyed["share"]["url"] == f"https://daedalus.example.com/s/{slug}/?key={key}"
    assert (await services.share(sid, "site", "key"))["share"]["key"] == key
    assert (await services.share(sid, "site", "key", rotate_key=True))["share"]["key"] != key
    row = await services.by_slug(slug)
    assert row is not None and services.share_allows(row, row["share_key"]) and not services.share_allows(row, "wrong") and not services.share_allows(row, None)
    back = await services.share(sid, "site", "local")
    assert back["share"]["mode"] == "local" and back["share"]["url"] is None and back["share"]["key"] is None
    assert not services.share_allows(await services.by_slug(slug) or {}, row["share_key"])
    with pytest.raises(ValueError, match="one of"):
        await services.share(sid, "site", "everyone")
    await services.stop_all(sid)


@pytest.mark.usefixtures("brief_start_grace")
async def test_a_service_without_a_port_cannot_be_shared(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    await services.start(state.session.id, name="worker", command="sleep 30", port="none")
    with pytest.raises(ValueError, match="without a port"):
        await services.share(state.session.id, "worker", "public")
    await services.stop_all(state.session.id)
