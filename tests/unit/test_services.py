"""Services: a detached process with a published port, its log, stopping, and what a restart does to it."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api_proxy import unreachable_page
from daedalus.extensions.services import Services, ending_words, local_alive, parse_range, pid_alive, unreachable_hint
from daedalus.host.host_services import exit_code_in
from daedalus.host.session_runner import SessionManager
from daedalus.processes import start_mark
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications
from tests.support.waiting import until, until_await


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
async def test_a_service_binds_loopback_natively_and_the_container_interface_otherwise(app: Any) -> None:
    """Natively the machine is the operator's and its network is their LAN. In a container a published
    port arrives at the container's own address, so loopback there would be a service nobody reaches."""
    state = await app.manager.create_session("host")
    sid = state.session.id
    for native, expected in ((True, "127.0.0.1"), (False, "0.0.0.0")):  # noqa: S104
        app.settings.native = native
        services = Services(app)
        name = f"host-{int(native)}"
        await services.start(sid, name=name, command=f'printf "%s" "$HOST" > {name}.txt; sleep 30')
        assert (state.workspace / f"{name}.txt").read_text() == expected
        await services.stop(sid, name)


@pytest.mark.usefixtures("brief_start_grace")
async def test_sharing_mints_a_slug_once_and_a_key_only_for_key_mode(app: Any) -> None:
    app.settings.miniapp_public_url = "https://daedalus.example.com/app/"
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="site", command="sleep 30")
    assert s["share"] == {"mode": "local", "slug": None, "key": None, "url": None, "public_base": "https://daedalus.example.com", "sandboxed": True}
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


def recording_submits(app: Any) -> list[tuple[str, str, dict[str, Any]]]:
    told: list[tuple[str, str, dict[str, Any]]] = []

    async def submit(session_id: str, text: str, *args: Any, **kwargs: Any) -> str:
        told.append((session_id, text, kwargs))
        return "run"

    app.manager.submit = submit
    return told


def test_the_way_a_service_ended_is_read_from_its_log() -> None:
    log = "\n=== 2026-01-01T00:00:00Z start: a\n\n=== 2026-01-01T00:00:01Z exited with code 2\n\n=== 2026-01-01T00:01:00Z start: b\nserving\n"
    assert exit_code_in(log) is None, "the last run has not ended yet: an older run's code is not this one's"
    assert exit_code_in(log + "boom\n\n=== 2026-01-01T00:02:00Z exited with code 139\n") == 139
    assert ending_words(139) == "it was killed by signal 11 (SIGSEGV)" and ending_words(3) == "it exited with code 3"
    assert "no exit code was recorded" in ending_words(None)


def test_a_process_is_told_from_a_later_one_with_the_same_pid() -> None:
    mark = start_mark(os.getpid())
    assert mark and mark == start_mark(os.getpid()) and ":" in mark
    assert start_mark(999_999_999) is None
    assert local_alive({"pid": os.getpid(), "pid_start": mark}) and local_alive({"pid": os.getpid(), "pid_start": None})
    assert not local_alive({"pid": os.getpid(), "pid_start": mark + "0"}), "the same pid with another start is a stranger"


@pytest.mark.usefixtures("brief_start_grace")
async def test_a_stranger_holding_a_services_pid_is_neither_running_nor_signalled(app: Any) -> None:
    services = Services(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="web", command="sleep 30")
    assert (await services.get(sid, "web") or {})["pid_start"] == start_mark(s["pid"])
    # What a reboot does: the row's pid now names a process that started at another moment.
    await app.db.execute("UPDATE services SET pid_start = 'another-boot:1' WHERE session_id = ? AND name = 'web'", (sid,))
    assert (await services.list(sid))[0]["status"] == "dead"
    stopped = await services.stop(sid, "web")
    assert stopped["status"] == "stopped" and pid_alive(s["pid"]), "the process the pid now names was not signalled"
    await services._terminate(int(s["pid"]), "another-boot:1")
    assert pid_alive(s["pid"])
    await services._terminate(int(s["pid"]), start_mark(s["pid"]))
    assert not pid_alive(s["pid"])
    await services.stop_all(sid)


@pytest.mark.usefixtures("brief_start_grace")
async def test_a_service_that_dies_wakes_its_session_once_and_a_stopped_one_does_not(app: Any) -> None:
    services = Services(app)
    told = recording_submits(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    await services.start(sid, name="api", command="sleep 0.4; echo 'listening failed: address in use'; exit 4", port="none")
    await services.start(sid, name="calm", command="sleep 30", port="none")
    await until(lambda: services._procs[(sid, "api")].poll() is not None, "the service died")
    await services.reconcile_local()
    row = await services.get(sid, "api") or {}
    assert row["status"] == "dead" and "exited with code 4" in row["note"]
    assert len(told) == 1 and told[0][0] == sid and told[0][2]["origin"] == "service" and told[0][2]["steer"] is True
    text = told[0][1]
    assert "service 'api' died" in text and "exited with code 4" in text and "address in use" in text and "ServiceLogs" in text
    await services.reconcile_local()
    assert len(told) == 1, "one death, one wake"
    await services.stop(sid, "calm")
    await services.reconcile_local()
    assert len(told) == 1, "a service the agent stopped wakes nobody"
    await services.stop_all(sid)


@pytest.mark.usefixtures("brief_start_grace")
async def test_a_service_the_restart_policy_brings_back_wakes_nobody(app: Any) -> None:
    services = Services(app)
    told = recording_submits(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    old = await services.start(sid, name="web", command="sleep 30", restart=True)
    young = await services.start(sid, name="flaky", command="sleep 30", restart=True)
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await app.db.execute("UPDATE services SET started_at = ? WHERE session_id = ? AND name = 'web'", (an_hour_ago, sid))
    procs = {n: services._procs[(sid, n)] for n in ("web", "flaky")}
    for s in (old, young):
        os.killpg(int(s["pid"]), signal.SIGKILL)
    await until(lambda: all(p.poll() is not None for p in procs.values()), "both died")
    await services.reconcile_local()
    again = await services.get(sid, "web") or {}
    assert again["status"] == "running" and again["pid"] != old["pid"] and again["port"] == old["port"]
    flaky = await services.get(sid, "flaky") or {}
    assert flaky["status"] == "dead" and "too soon" in flaky["note"] and "signal 9" in flaky["note"]
    assert [t[1].split("]")[0] for t in told] == [f"[service 'flaky' died: {flaky['note']}"], "only the one left dead is told"
    await services.stop_all(sid)


@pytest.mark.usefixtures("brief_start_grace")
async def test_nothing_is_told_while_the_process_shuts_down(app: Any) -> None:
    services = Services(app)
    told = recording_submits(app)
    state = await app.manager.create_session("host")
    sid = state.session.id
    s = await services.start(sid, name="web", command="sleep 30", restart=False)
    proc = services._procs[(sid, "web")]
    os.killpg(int(s["pid"]), signal.SIGKILL)
    await until(lambda: proc.poll() is not None, "it died")
    app.manager.shutting_down = True
    try:
        await services.reconcile_local()
        assert told == [] and (await services.get(sid, "web") or {})["status"] == "running", "left for the next process to judge"
    finally:
        app.manager.shutting_down = False
    await services.stop_all(sid)


def test_a_host_service_the_proxy_cannot_reach_names_the_firewall(settings: Settings) -> None:
    settings.host_services_port_range = "8140-8159"
    assert "ufw" in unreachable_hint(settings, "10.0.70.1", 8141) and "8140:8159" in unreachable_hint(settings, "10.0.70.1", 8141)
    assert unreachable_hint(settings, "127.0.0.1", 8141) == "" and unreachable_hint(settings, "10.0.70.1", 8101) == ""
    page = unreachable_page("http://10.0.70.1:8141/x", timed_out=True, hint="a <firewall>")
    body = bytes(page.body).decode()
    assert page.status_code == 502 and "10.0.70.1:8141" in body and "timed out" in body and "a &lt;firewall&gt;" in body
