"""The terminals compose service, the image that carries its daemon, and updating that daemon.

The compose file and the Dockerfile are read as data: what they promise — the same paths on both
sides, a home the agent never sees, a daemon that outlives the agent's container — is checked here
rather than found out on the server.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import terminals as terminals_extension
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.terminals.model import Owner, TerminalSpec
from daedalus.terminals.service import Terminals
from daedalus.terminals.update import BY_HAND, REQUEST_FILE, DaemonUpdate, daemon_version
from tests.support.fake_ptyd import FakePtyd

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
RUN_DIR = "/run/daedalus-terminals"
H = {"X-Daedalus-Token": "tok"}


def compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def volumes(service: dict[str, Any]) -> dict[str, str]:
    """Container path → source, for the short-form entries this file uses."""
    out = {}
    for entry in service.get("volumes", []):
        source, target, *_ = entry.split(":")
        out[target] = source
    return out


def port_range(text: str) -> range:
    low, high = (int(n) for n in text.split("-"))
    return range(low, high + 1)


def default(value: str) -> str:
    """``${NAME:-default}`` → ``default``."""
    found = re.fullmatch(r"\$\{\w+:-([^}]*)\}", value)
    assert found, value
    return found.group(1)


# -- the compose file -------------------------------------------------------------------------


def test_the_terminals_service_is_the_agents_image_started_as_ptyd() -> None:
    services = compose()["services"]
    terminals, agent = services["terminals"], services["daedalus"]
    assert terminals["image"] == agent["image"] and terminals["build"] == agent["build"]
    assert terminals["entrypoint"] == ["/usr/local/bin/ptyd"]
    command = terminals["command"]
    assert command[:3] == ["serve", "--env", "container"]
    assert command[command.index("--run-dir") + 1] == RUN_DIR
    assert command[command.index("--home") + 1] == "/root" and terminals["environment"]["HOME"] == "/root"
    # Always on: a profile would leave the app's container terminals missing on a plain `up -d`.
    assert "profiles" not in terminals
    assert terminals["init"] is True and terminals["restart"] == "unless-stopped"
    # The daedalus service never waits for it, so neither one's restart drags the other along.
    assert "terminals" not in agent.get("depends_on", {})


def test_the_terminals_may_create_the_namespaces_their_sandbox_needs() -> None:
    # The sandbox toggle runs bubblewrap inside the service; Docker's default seccomp and AppArmor
    # profiles refuse it the namespaces, exactly as they would the agent's Exec sandbox.
    services = compose()["services"]
    for name in ("terminals", "daedalus"):
        assert services[name]["cap_add"] == ["SYS_ADMIN"]
        assert sorted(services[name]["security_opt"]) == ["apparmor=unconfined", "seccomp=unconfined"]


def test_the_terminals_see_the_workspaces_at_the_agents_paths_and_nothing_of_the_agent_beyond() -> None:
    services = compose()["services"]
    terminals, agent = volumes(services["terminals"]), volumes(services["daedalus"])
    # Every workspace path the agent has, the terminals have at the same path from the same volume.
    shared = {target: source for target, source in agent.items() if target.startswith("/srv/workspaces")}
    assert shared and all(terminals.get(target) == source for target, source in shared.items())
    # Staff worktrees live inside the project folders, which are mounted at their own paths; the
    # self-development worktrees, the state, the repositories and the virtualenv are the agent's.
    for target in ("/srv/worktrees", "/srv/state", "/srv/daedalus", "/srv/protocore-exp", "/srv/venv", "/srv/ssh", "/run/daedalus-rebuild"):
        assert target not in terminals, target
    assert "daedalus-worktrees" not in terminals.values()
    # Both reach the daemon's run directory; the agent is told where it is.
    assert terminals[RUN_DIR] == agent[RUN_DIR] == "terminal-run"
    assert services["daedalus"]["environment"]["TERMINALS_CONTAINER_DIR"] == RUN_DIR


def test_the_terminals_home_is_never_the_agents() -> None:
    services = compose()["services"]
    assert volumes(services["terminals"])["/root"] == "terminals-home"
    for name, service in services.items():
        if name != "terminals":
            assert "terminals-home" not in volumes(service).values(), name
    declared = compose()["volumes"]
    assert {"terminal-run", "terminals-home", "terminals-state"} <= set(declared)


def test_what_the_harnesses_screen_installs_is_found_first_and_kept_in_the_home() -> None:
    environment = compose()["services"]["terminals"]["environment"]
    path = environment["PATH"].split(":")
    # The native installers' directory and the npm prefix, both on the home volume, ahead of anything
    # the image carries; otherwise an installed CLI or the pinned Node is there and never found.
    assert path[:2] == ["/root/.local/bin", "/root/.npm-global/bin"]
    assert {"/usr/local/bin", "/usr/bin", "/bin"} <= set(path)
    assert environment["NPM_CONFIG_PREFIX"] == "/root/.npm-global"


def test_the_terminals_have_their_own_way_out_and_no_route_to_the_keys() -> None:
    document = compose()
    assert document["services"]["terminals"]["networks"] == ["terminals"]
    assert "terminals" in document["networks"]
    for private in ("keys", "search", "telegram"):
        assert private not in document["services"]["terminals"]["networks"]


def test_the_terminal_ports_are_their_own_range_and_the_agent_is_told_it() -> None:
    services = compose()["services"]
    [published] = services["terminals"]["ports"]
    host_side, container_side = re.findall(r"\$\{[^}]*\}", published)
    assert host_side == container_side == "${TERMINALS_PORT_RANGE:-8120-8139}"
    agent_ports = [p for p in services["daedalus"]["ports"] if "SERVICES_PORT_RANGE" in p]
    assert agent_ports
    terminals_range = port_range(default(host_side))
    services_range = port_range(default(services["daedalus"]["environment"]["SERVICES_PORT_RANGE"]))
    assert not set(terminals_range) & set(services_range)
    assert default(services["daedalus"]["environment"]["TERMINALS_PORT_RANGE"]) == default(host_side)
    assert Settings.model_fields["terminals_port_range"].default == default(host_side)
    # The example the operator copies agrees with the defaults.
    example = dict(line.split("=", 1) for line in (DEPLOY / "env.example").read_text(encoding="utf-8").splitlines() if re.match(r"^[A-Z_]+=", line))
    assert example["TERMINALS_PORT_RANGE"] == default(host_side)
    assert not set(port_range(example["TERMINALS_PORT_RANGE"])) & set(port_range(example["SERVICES_PORT_RANGE"]))


def test_the_header_says_what_recreating_the_service_costs_and_how_the_first_deploy_goes() -> None:
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "ends every container terminal" in text
    assert "docker compose ... up -d" in text and "never touch it" in text


# -- the image --------------------------------------------------------------------------------


def test_the_image_builds_the_daemon_with_its_emulator_and_ships_only_the_binary() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    # Built on the builder's own platform and cross-compiled for the image's, not under emulation.
    stage = re.search(r"^FROM --platform=\$BUILDPLATFORM golang:(\S+) AS ptyd$", dockerfile, re.M)
    assert stage, "no ptyd stage"
    # The Go of the stage is the one go.mod names; the two drift apart only by someone's mistake.
    go = re.search(r"^go (\d+\.\d+)", (REPO / "ptyd" / "go.mod").read_text(encoding="utf-8"), re.M)
    assert go and stage.group(1).startswith(go.group(1) + "-")
    assert "libghostty/build.sh" in dockerfile and 'LIBGHOSTTY_TARGET="$(cat /opt/target-triple)"' in dockerfile
    assert 'CGO_ENABLED=1 GOOS=linux GOARCH="$TARGETARCH"' in dockerfile
    assert "internal/version.Version=src-" in dockerfile
    assert "COPY --from=ptyd /out/ptyd /usr/local/bin/ptyd" in dockerfile
    runtime = dockerfile[dockerfile.index("FROM base AS runtime") :]
    assert "COPY --from=ptyd" in runtime.split("FROM runtime AS browser")[0]


def test_the_build_context_leaves_the_emulator_cache_behind() -> None:
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").split()
    assert "ptyd/.cache" in ignored


# -- the rebuilder ------------------------------------------------------------------------------


async def run_rebuilder(tmp_path: Path, job: str, *, fail: bool = False) -> tuple[Path, str]:
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\nexit "$UP_RESULT"\n')
    fake.chmod(0o755)
    (trigger / REQUEST_FILE).write_text(job + "\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""), "DAEDALUS_REBUILD_TRIGGER_DIR": str(trigger), "COMPOSE_FILE": "compose.yaml", "CALLS": str(tmp_path / "calls"), "UP_RESULT": "1" if fail else "0"}
    process = await asyncio.create_subprocess_exec("sh", str(DEPLOY / "rebuild.sh"), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while (trigger / REQUEST_FILE).exists() or not (trigger / "alive").exists():
                await asyncio.sleep(0.02)
            if re.fullmatch(r"[0-9a-f]{32}", job):
                while not (trigger / f"terminals-{job}.result").exists():
                    await asyncio.sleep(0.02)
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    return trigger, calls


@pytest.mark.parametrize("fail", [False, True])
async def test_the_rebuilder_recreates_only_the_terminals_service_from_the_built_image(tmp_path: Path, fail: bool) -> None:
    if os.name == "nt":
        pytest.skip("the rebuilder is a POSIX shell script")
    job = "d" * 32
    trigger, calls = await run_rebuilder(tmp_path, job, fail=fail)
    assert calls.strip().endswith("up -d --no-build --no-deps terminals")
    assert " build " not in f" {calls} "
    update = DaemonUpdate(trigger)
    assert update.result(job) == ({"state": "failed", "detail": "the terminals service was not recreated; inspect rebuilder logs"} if fail else {"state": "completed", "detail": ""})


async def test_the_rebuilder_drops_a_request_that_is_not_a_job_id(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the rebuilder is a POSIX shell script")
    trigger, calls = await run_rebuilder(tmp_path, "../../etc/passwd")
    assert calls == "" and not list(trigger.glob("terminals-*"))


# -- the image's daemon and the request ---------------------------------------------------------


def fake_daemon(path: Path, line: str, code: int = 0) -> Path:
    path.write_text(f"#!/bin/sh\n[ \"$1\" = version ] || exit 9\necho '{line}'\nexit {code}\n")
    path.chmod(0o755)
    return path


async def test_the_image_version_is_read_from_the_binary_and_nothing_else_counts(tmp_path: Path) -> None:
    assert await daemon_version(fake_daemon(tmp_path / "a", "ptyd src-0123456789ab (protocol 1)")) == "src-0123456789ab"
    assert await daemon_version(fake_daemon(tmp_path / "b", "ptyd src-0123 (protocol 1)", code=1)) == ""
    assert await daemon_version(fake_daemon(tmp_path / "c", "something else")) == ""
    assert await daemon_version(tmp_path / "missing") == ""


def test_a_request_is_only_offered_to_a_rebuilder_that_is_there(tmp_path: Path) -> None:
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    now = [time.time()]
    update = DaemonUpdate(trigger, clock=lambda: now[0])
    assert not update.rebuilder_alive()
    (trigger / "alive").touch()
    assert update.rebuilder_alive()
    now[0] += 3600
    assert not update.rebuilder_alive()
    assert not DaemonUpdate(None).rebuilder_alive()
    job = update.request()
    assert (trigger / REQUEST_FILE).read_text().strip() == job and re.fullmatch(r"[0-9a-f]{32}", job)
    assert not (trigger / f"{REQUEST_FILE}.pending").exists()
    assert update.result(job) == {"state": "pending", "detail": ""}


# -- over the API, with a daemon older than the image -------------------------------------------


@pytest.fixture
def run_dir() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
async def app(settings: Settings, run_dir: Path, db: Database, daemon: FakePtyd, tmp_path: Path) -> AsyncIterator[Any]:
    terminal_settings = settings.model_copy(update={"terminals_container_dir": run_dir})
    manager = SessionManager(terminal_settings, RuntimeConfig(), db=db)
    await manager.start()
    application = SimpleNamespace(settings=terminal_settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await terminals_extension.install(application)  # type: ignore[arg-type]
    service: Terminals = application.extensions["terminals"]
    assert await service.wait_available("container")
    # The image's daemon, as the agent's container would carry it at /usr/local/bin/ptyd.
    service.daemon_update = DaemonUpdate(terminal_settings.rebuild_trigger_dir, binary=fake_daemon(tmp_path / "ptyd", "ptyd src-feedfacecafe (protocol 1)"))
    await service.daemon_update.probe()
    yield application
    for task in tasks:
        task.cancel()
    await service.close()
    await manager.close()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


async def test_a_daemon_older_than_the_image_is_offered_for_update(client: httpx.AsyncClient) -> None:
    container, host = (await client.get("/api/terminals", headers=H)).json()["envs"]
    assert container["version"] == "fake" and container["image_version"] == "src-feedfacecafe" and container["update_available"] is True
    assert host["image_version"] == "" and host["update_available"] is False


async def test_hiring_can_check_environment_containment_without_listing_terminals(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/terminals/envs", headers=H)
    assert response.status_code == 200
    assert [row["env"] for row in response.json()["envs"]] == ["container", "host"]
    assert "containment" in response.json()["envs"][0]
    assert "terminals" not in response.json()
    assert (await client.get("/api/terminals/envs")).status_code == 401


async def test_the_update_names_the_terminals_it_ends_until_the_operator_confirms(client: httpx.AsyncClient, app: Any, db: Database) -> None:
    service: Terminals = app.extensions["terminals"]
    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    trigger: Path = app.settings.rebuild_trigger_dir
    refused = await client.post("/api/terminals/envs/container/update", json={}, headers=H)
    assert refused.status_code == 409 and refused.json()["code"] == "live_terminals" and refused.json()["running"] == 2
    assert not (trigger / REQUEST_FILE).exists()
    accepted = await client.post("/api/terminals/envs/container/update", json={"confirm": True}, headers=H)
    assert accepted.status_code == 202, accepted.text
    job = accepted.json()["job"]
    assert accepted.json()["running"] == 2 and (trigger / REQUEST_FILE).read_text().strip() == job
    audit = await db.fetchone("SELECT actor, detail_json FROM terminal_audit WHERE action = 'daemon_update'")
    assert audit["actor"] == "operator" and '"to":"src-feedfacecafe"' in audit["detail_json"] and '"from":"fake"' in audit["detail_json"]
    assert (await client.get(f"/api/terminals/envs/container/update/{job}", headers=H)).json() == {"state": "pending", "detail": ""}
    (trigger / f"terminals-{job}.result").write_text("completed\n")
    assert (await client.get(f"/api/terminals/envs/container/update/{job}", headers=H)).json() == {"state": "completed", "detail": ""}
    # A job id is a file name in the trigger directory, so only the shape request() makes is read.
    assert (await client.get("/api/terminals/envs/container/update/..%2F..%2Fetc", headers=H)).status_code == 404


async def test_without_a_rebuilder_the_operator_is_given_the_command(client: httpx.AsyncClient, app: Any) -> None:
    (app.settings.rebuild_trigger_dir / "alive").unlink()
    refused = await client.post("/api/terminals/envs/container/update", json={"confirm": True}, headers=H)
    assert refused.status_code == 503 and refused.json()["code"] == "no_rebuilder" and refused.json()["command"] == BY_HAND
    assert (await client.post("/api/terminals/envs/host/update", json={"confirm": True}, headers=H)).status_code == 400
    assert (await client.post("/api/terminals/envs/container/update", json={"confirm": True})).status_code == 401


async def test_the_doctor_names_the_update_and_the_first_deploy(app: Any, settings: Settings, tmp_path: Path) -> None:
    from daedalus.doctor import DoctorContext, _terminals  # noqa: PLC0415 — the probe alone, not the whole doctor

    checks = {c.name: c for c in await _terminals(DoctorContext(settings=app.settings, config=app.config, extensions=app.extensions))}
    update = checks["terminals update (container)"]
    assert "src-feedfacecafe" in update.message and BY_HAND in update.fix_hint
    # A stack started before the service existed: the run directory is an empty volume.
    empty = tmp_path / "empty-run"
    empty.mkdir()
    alone = await _terminals(DoctorContext(settings=settings.model_copy(update={"terminals_container_dir": empty}), config=app.config))
    assert alone[0].name == "terminals (container)" and not alone[0].ok and "up -d --build" in alone[0].fix_hint
