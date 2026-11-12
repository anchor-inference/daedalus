"""Services of a session that works in a host folder: started, watched, read and ended on the host
through its terminal daemon, registered like any other service, and honest while the daemon is down.

The daemon is :class:`~tests.unit.test_host_exec.ShellHost`, which runs the scripts on this machine
under a directory standing in for the operator's, so the processes, the log and the port are real."""

from __future__ import annotations

import os
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings
from daedalus.extensions import services as services_module
from daedalus.extensions.services import Services, dial_address
from daedalus.host import host_services
from daedalus.host.host_services import HostDown, HostServices, default_gateway, exit_code_in
from daedalus.host.policy import DENY, Policy
from daedalus.processes import start_mark
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications
from tests.support.waiting import until_await
from tests.unit.test_host_exec import HOST_ROOT, ShellHost, container_only, context, host_disk, host_project
from tests.unit.test_session_runner import ScriptedProvider, _manager


@pytest.fixture(autouse=True)
def no_user_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real ``systemd-run --user --scope`` would move the test's servers out of reach of its cleanup."""
    monkeypatch.setattr(host_services, "SCOPE_PROBE", "false")


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start's watch and the daemon's remembered answers are not what these tests measure."""
    monkeypatch.setattr(services_module, "START_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(services_module, "HOST_SEEN_SECONDS", 0.0)


def free_range(size: int = 4) -> str:
    """A run of ports nothing on this machine listens on, so the host's range is really free here."""
    for start in range(18240, 18900, size):
        ok = True
        for port in range(start, start + size):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    ok = False
                    break
        if ok:
            return f"{start}-{start + size - 1}"
    raise RuntimeError("no free ports for the test")


def backend(disk: Path, **kwargs: Any) -> tuple[ShellHost, HostServices]:
    host = ShellHost(disk, **kwargs)
    return host, HostServices(lambda: host, resolve=lambda: "127.0.0.1")  # type: ignore[arg-type, return-value]


def test_the_default_gateway_is_read_from_the_route_table() -> None:
    routes = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t0046000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
        "eth0\t00000000\t0146000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    assert default_gateway(routes) == "10.0.70.1"
    assert default_gateway("Iface\tDestination\tGateway\n") == ""


async def test_the_host_half_starts_reads_and_ends_a_process_group(tmp_path: Path) -> None:
    disk = host_disk(tmp_path)
    host, half = backend(disk)
    log = f"{HOST_ROOT}/.services/web.log"
    started = await half.start("echo \"on $HOST:$PORT\"; exec sleep 60", cwd=HOST_ROOT, log=log, port=18999, host="127.0.0.1", watch=0.3)
    try:
        assert started.exit_code is None and started.pid > 0
        assert os.getpgid(started.pid) == started.pid, "it leads a process group of its own, so the group is what is ended"
        assert await half.alive([started.pid, 999_999_999]) == {started.pid}
        assert (disk / ".services" / "web.log").is_file(), "the log is in the folder on the host"
        assert "on 127.0.0.1:18999" in await half.logs(log, 20)
    finally:
        await half.stop(started.pid, grace=2.0)
    assert await half.alive([started.pid]) == set()

    died = await half.start("echo broken; exit 3", cwd=HOST_ROOT, log=log, port=None, host="127.0.0.1", watch=1.0)
    assert died.exit_code == 3, "a command that dies at once is reported with its code, not kept as a service"
    with pytest.raises(ValueError, match="does not exist on the host"):
        await half.start("true", cwd=f"{HOST_ROOT}/missing", log=log, port=None, host="127.0.0.1", watch=0.1)

    host.up = False
    for question in (half.alive([1]), half.listening(), half.logs(log, 5)):
        with pytest.raises(HostDown, match="systemctl --user start daedalus-ptyd"):
            await question


async def test_a_pid_the_host_handed_to_another_process_is_not_the_service(tmp_path: Path) -> None:
    disk = host_disk(tmp_path)
    _, half = backend(disk)
    log = f"{HOST_ROOT}/.services/web.log"
    started = await half.start("exec sleep 60", cwd=HOST_ROOT, log=log, port=None, host="127.0.0.1", watch=0.3)
    try:
        assert started.mark == start_mark(started.pid), "the host's mark is the one this machine's /proc gives"
        assert await half.alive([(started.pid, started.mark)]) == {started.pid}
        assert await half.alive([(started.pid, "another-boot:1")]) == set(), "the same pid with another start is a stranger"
        assert not await half.stop(started.pid, grace=1.0, mark="another-boot:1")
        assert await half.alive([started.pid]) == {started.pid}, "and the stranger was not signalled"
        assert await half.stop(started.pid, grace=2.0, mark=started.mark)
        assert await half.alive([started.pid]) == set()
    finally:
        await half.stop(started.pid, grace=1.0)
    died = await half.start("echo bye; exit 5", cwd=HOST_ROOT, log=log, port=None, host="127.0.0.1", watch=1.0)
    assert died.exit_code == 5 and exit_code_in(await half.logs(log, 20)) == 5, "the way it ended is in the log"


async def test_the_ports_the_host_listens_on_are_read_on_the_host(tmp_path: Path) -> None:
    _, half = backend(host_disk(tmp_path))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert port in await half.listening()


@pytest.mark.usefixtures("quick")
async def test_a_host_session_starts_a_service_that_is_registered_reached_and_ended(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.services import service_list, service_logs, service_start, service_stop

    settings.host_services_port_range = free_range()
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        host = ShellHost(disk, allow=("bash", "git"))
        manager.host_bridge = host  # type: ignore[assignment]
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, notifications=RecordingNotifications())
        services = Services(app)  # type: ignore[arg-type]
        services.host = HostServices(lambda: manager.host_bridge, resolve=lambda: "127.0.0.1")
        manager.service_hooks["services"] = services.service
        state = await manager.create_session("Labs chat", project_id=(await host_project(manager)).id)
        assert state.services is not None
        state.services.extra["manager"] = manager
        ctx = context(state.session.id)

        started = await service_start().invoke(ctx, {"name": "web", "command": "exec python3 -m http.server $PORT --bind $HOST"})
        assert not started.is_error, started.content
        assert "running on the host" in started.content and "Services panel" in started.content and "BrowserOpen" in started.content
        row = await services.get(state.session.id, "web")
        assert row is not None and row["machine"] == "host" and row["address"] == "127.0.0.1"
        lo, hi = services.host_lo, services.host_hi
        assert lo <= row["port"] <= hi and row["cwd"] == HOST_ROOT and row["log_path"] == f"{HOST_ROOT}/.services/web.log"

        async def answers() -> bool:
            try:
                async with httpx.AsyncClient() as client:
                    return (await client.get(f"http://127.0.0.1:{row['port']}/README.md")).status_code == 200
            except httpx.HTTPError:
                return False

        await until_await(answers, "the server listens on $HOST:$PORT")
        assert await dial_address(services, row) == "127.0.0.1", "the proxy dials the address the service was told to listen on"

        view = (await services.list_all())[0]
        assert view["status"] == "running" and view["machine"] == "host" and view["url"] is None, "the LAN cannot reach it, so no LAN address is offered"
        assert "on the host → http://127.0.0.1:" in (await service_list().invoke(ctx, {})).content
        assert "GET /README.md" in (await service_logs().invoke(ctx, {"name": "web"})).content

        second = await services.start(state.session.id, name="api", command="exec sleep 60")
        assert second["port"] != row["port"], "a port a host service holds is not handed out again"
        shared = await services.share(state.session.id, "web", "public")
        assert shared["share"]["slug"] and shared["status"] == "running"

        ended = await service_stop().invoke(ctx, {"name": "web"})
        assert not ended.is_error and "stopped" in ended.content
        assert not await answers()
        assert await dial_address(services, await services.get(state.session.id, "web") or {}) is None
        assert await services.stop_all(state.session.id) == 1
    finally:
        await manager.close()


@pytest.mark.usefixtures("quick")
async def test_while_the_daemon_is_down_a_host_service_is_unknown_and_not_called_dead(settings: Settings, db: Database, tmp_path: Path) -> None:
    settings.host_services_port_range = free_range()
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        host = ShellHost(host_disk(tmp_path))
        manager.host_bridge = host  # type: ignore[assignment]
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, notifications=RecordingNotifications())
        services = Services(app)  # type: ignore[arg-type]
        services.host = HostServices(lambda: manager.host_bridge, resolve=lambda: "127.0.0.1")
        state = await manager.create_session("Labs chat", project_id=(await host_project(manager)).id)
        sid = state.session.id
        s = await services.start(sid, name="worker", command="exec sleep 60", port="none")
        try:
            host.up = False
            view = (await services.list(sid))[0]
            assert view["status"] == "unknown" and "systemctl --user start daedalus-ptyd" in view["note"]
            with pytest.raises(RuntimeError, match="could not stop 'worker'.*not answering"):
                await services.stop(sid, "worker")
            assert "not answering" in await services.logs(sid, "worker")
            await services.reconcile_host()
            assert (await services.get(sid, "worker") or {})["status"] == "running", "a look the daemon could not answer changes nothing"
            await services.reconcile()
            assert (await services.get(sid, "worker") or {})["status"] == "running", "the boot's reconcile leaves the host's services to the daemon"
            host.up = True
            assert (await services.list(sid))[0]["status"] == "running"
        finally:
            host.up = True
            await services.stop(sid, "worker")
        assert not await services.host.alive([s["pid"]])
    finally:
        await manager.close()


@pytest.mark.usefixtures("quick")
async def test_a_host_service_that_died_is_started_again_or_reported(settings: Settings, db: Database, tmp_path: Path) -> None:
    settings.host_services_port_range = free_range()
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        manager.host_bridge = ShellHost(host_disk(tmp_path))  # type: ignore[assignment]
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, notifications=RecordingNotifications())
        services = Services(app)  # type: ignore[arg-type]
        services.host = HostServices(lambda: manager.host_bridge, resolve=lambda: "127.0.0.1")
        state = await manager.create_session("Labs chat", project_id=(await host_project(manager)).id)
        sid = state.session.id
        told: list[tuple[str, str]] = []

        async def submit(session_id: str, text: str, *args: Any, **kwargs: Any) -> str:
            told.append((session_id, text))
            return "run"

        manager.submit = submit  # type: ignore[method-assign]
        old = await services.start(sid, name="web", command="exec sleep 60")
        young = await services.start(sid, name="flaky", command="exec sleep 60")
        an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await db.execute("UPDATE services SET started_at = ? WHERE session_id = ? AND name = 'web'", (an_hour_ago, sid))
        for pid in (old["pid"], young["pid"]):
            os.killpg(pid, signal.SIGKILL)

        async def gone() -> bool:
            return not await services.host.alive([old["pid"], young["pid"]])

        await until_await(gone, "the host's processes ended")
        await services.reconcile_host()
        again = await services.get(sid, "web") or {}
        assert again["status"] == "running" and again["pid"] != old["pid"] and again["port"] == old["port"], "brought back on its own port"
        flaky = await services.get(sid, "flaky") or {}
        assert flaky["status"] == "dead" and "too soon" in flaky["note"], "one that died soon after its start is reported, not looped"
        assert len(told) == 1 and told[0][0] == sid and "service 'flaky' died" in told[0][1] and "on the host" in told[0][1], "the session hears of the one left dead, once"
        await services.reconcile_host()
        assert len(told) == 1
        await services.stop_all(sid)
    finally:
        await manager.close()


async def test_a_session_on_an_operators_terminal_is_still_sent_to_a_background_job(settings: Settings, db: Database, tmp_path: Path) -> None:
    from daedalus.tools.services import service_start

    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        container_only(manager)
        manager.host_bridge = ShellHost(host_disk(tmp_path))  # type: ignore[assignment]
        state = await manager.create_session("Labs chat", project_id=(await host_project(manager)).id)
        assert state.services is not None
        state.services.extra["manager"] = manager
        manager.service_hooks["services"] = lambda *a, **k: None
        state.services.exec_backend = SimpleNamespace(machine="device:laptop")  # type: ignore[assignment]
        refused = await service_start().invoke(context(state.session.id), {"name": "web", "command": "python -m http.server"})
        assert refused.is_error and "Exec(background=true)" in refused.content
    finally:
        await manager.close()


def test_the_rules_on_commands_hold_for_a_service_started_on_the_host() -> None:
    policy = Policy(drives_host=True)
    tunnel = policy.evaluate("ServiceStart", {"name": "web", "command": "ngrok http 8000"})
    assert tunnel.action == DENY and tunnel.rule == "egress.tunnel"
    entered = policy.evaluate("ServiceStart", {"name": "web", "command": "docker exec deploy-daedalus-1 sh"})
    assert entered.action == DENY and entered.rule == "host.own_containers"
