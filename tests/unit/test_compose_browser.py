"""The browser compose service, the image target it runs, updating it, and what the doctor says.

The compose file and the Dockerfile are read as data: the walls they promise — a network of its own
with no route to the keys, no workspace, not root, no widening beyond the one the sandbox needs, a
run directory the agent's commands cannot name — are checked here rather than found out on a server.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from daedalus.config import RuntimeConfig, Settings
from daedalus.doctor import BROWSER_FIXES, DoctorContext, _browser
from daedalus.host.policy import DENY, Policy
from tests.support.fake_ptyd import FakePtyd

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
RUN_DIR = "/run/daedalus-browser"
STATE_DIR = "/var/lib/browserd"


def compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def volumes(service: dict[str, Any]) -> dict[str, str]:
    """Container path → source, for the short-form entries this file uses."""
    out = {}
    for entry in service.get("volumes", []):
        source, target, *_ = entry.split(":")
        out[target] = source
    return out


def dockerfile() -> str:
    return (DEPLOY / "Dockerfile").read_text(encoding="utf-8")


# -- the compose file -------------------------------------------------------------------------


def test_the_browser_service_is_the_images_browser_target_started_as_browserd() -> None:
    services = compose()["services"]
    browser, agent = services["browser"], services["daedalus"]
    assert browser["build"]["context"] == agent["build"]["context"] and browser["build"]["dockerfile"] == agent["build"]["dockerfile"]
    assert browser["build"]["target"] == "browser"
    # An image name of its own: the agent's is the runtime target, and one name for two targets
    # would have each build overwrite the other.
    assert browser["image"] != agent["image"]
    assert browser["entrypoint"] == ["/usr/local/bin/browserd"]
    command = browser["command"]
    assert command[:3] == ["serve", "--env", "container"]
    assert command[command.index("--run-dir") + 1] == RUN_DIR
    assert command[command.index("--state-dir") + 1] == STATE_DIR
    assert browser["environment"]["HOME"] == STATE_DIR
    # Behind a profile: an installation that never browses pulls and runs nothing extra.
    assert browser["profiles"] == ["browser"]
    assert browser["init"] is True and browser["restart"] == "unless-stopped"
    # Neither waits for the other, so neither one's restart drags the other along.
    assert "browser" not in agent.get("depends_on", {}) and "depends_on" not in browser


def test_the_browser_has_a_network_of_its_own_and_no_route_to_anything_else() -> None:
    document = compose()
    services = document["services"]
    assert services["browser"]["networks"] == ["browser"]
    assert "browser" in document["networks"]
    # Nothing else is on it: the key proxy, SearXNG, the Bot API, the agent and the terminals are
    # all somewhere a page cannot reach, whatever the daemon's own proxy says.
    for name, service in services.items():
        if name != "browser":
            assert "browser" not in (service.get("networks") or []), name
    # Nothing is published from it: the host reaches it through its socket, the app through the host.
    assert "ports" not in services["browser"] and "network_mode" not in services["browser"]
    # The Docker host, for the services ranges the wall opens there.
    assert services["browser"]["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_the_browser_is_not_root_and_is_widened_only_as_far_as_its_sandbox_needs() -> None:
    browser = compose()["services"]["browser"]
    uid, _, gid = browser["user"].partition(":")
    assert uid == gid == "1001"
    # seccomp=unconfined is what lets Chromium's sandbox make its user namespaces as an ordinary user.
    # apparmor=unconfined would break that sandbox on a host that restricts unprivileged user
    # namespaces, and SYS_ADMIN is what the terminals need for bubblewrap, not what a browser needs.
    assert sorted(browser["security_opt"]) == ["no-new-privileges:true", "seccomp=unconfined"]
    assert "cap_add" not in browser and browser["cap_drop"] == ["ALL"]
    assert browser["read_only"] is True and any(t.startswith("/tmp") for t in browser["tmpfs"])
    assert re.fullmatch(r"\$\{BROWSER_MEMORY_LIMIT:-\d+[gm]\}", browser["mem_limit"])
    assert browser["pids_limit"] > 0 and browser["shm_size"]


def test_the_browser_mounts_no_workspace_and_its_profiles_are_never_the_agents() -> None:
    services = compose()["services"]
    browser, agent = volumes(services["browser"]), volumes(services["daedalus"])
    # Exactly its run directory and its state: no workspace, no project folder, no checkout. A file
    # reaches a page, or leaves one, only through the host and the session's walls.
    assert browser == {RUN_DIR: "browser-run", STATE_DIR: "browser-state"}
    for name, service in services.items():
        if name != "browser":
            assert "browser-state" not in volumes(service).values(), name
    # The agent reaches the daemon's socket, and is told where it is.
    assert agent[RUN_DIR] == "browser-run"
    assert services["daedalus"]["environment"]["BROWSER_CONTAINER_DIR"] == RUN_DIR
    assert {"browser-run", "browser-state"} <= set(compose()["volumes"])


def test_the_browser_run_directory_is_sealed_from_the_agents_commands() -> None:
    settings = Settings(browser_container_dir=Path(RUN_DIR), browser_host_dir=Path("/data/runtime/browserd/run"))
    assert Path(RUN_DIR) in settings.sealed_everywhere and Path(RUN_DIR) in settings.sealed_paths
    assert Path("/data/runtime/browserd/run") in settings.sealed_everywhere
    # In a container too: the token drives browsers that hold the operator's logins.
    policy = Policy(sealed_paths=settings.sealed_paths, sealed_everywhere=settings.sealed_everywhere)
    for command in (f"cat {RUN_DIR}/token", f"ls {RUN_DIR}", f"python3 -c \"open('{RUN_DIR}/token').read()\""):
        assert policy.evaluate("Exec", {"command": command}).action == DENY, command
    assert policy.evaluate("ServiceStart", {"command": f"socat - UNIX-CONNECT:{RUN_DIR}/browserd.sock"}).action == DENY


def test_the_header_and_the_example_say_how_to_turn_it_on() -> None:
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "--profile browser" in text and "COMPOSE_PROFILES=browser" in text
    example = (DEPLOY / "env.example").read_text(encoding="utf-8")
    assert "#COMPOSE_PROFILES=browser" in example and "#BROWSER_MEMORY_LIMIT=" in example


# -- the image --------------------------------------------------------------------------------


def test_the_image_builds_browserd_without_c_and_ships_it_in_both_targets() -> None:
    text = dockerfile()
    stage = re.search(r"^FROM --platform=\$BUILDPLATFORM golang:(\S+) AS browserd$", text, re.M)
    assert stage, "no browserd stage"
    # The Go of the stage is the one browserd's go.mod names.
    go = re.search(r"^go (\d+\.\d+)", (REPO / "browserd" / "go.mod").read_text(encoding="utf-8"), re.M)
    assert go and stage.group(1).startswith(go.group(1) + "-")
    body = text[stage.start() : text.index("FROM base AS runtime")]
    assert 'CGO_ENABLED=0 GOOS=linux GOARCH="$TARGETARCH"' in body
    assert "internal/version.Version=src-" in body and "./cmd/browserd" in body
    # The replace directive points at ../ptyd, so its shared packages and module files come in, and
    # the version digest covers them: a change to the socket code is a change to the daemon.
    assert "COPY ptyd/proto /src/ptyd/proto" in body
    assert "ptyd/proto ptyd/go.mod ptyd/go.sum" in body
    # ptyd's emulator is not part of it.
    assert "libghostty" not in body
    runtime = text[text.index("FROM base AS runtime") : text.index("FROM runtime AS browser")]
    assert "COPY --from=browserd /out/browserd /usr/local/bin/browserd" in runtime
    assert f"install -d -m 0700 -o 1001 -g 1001 {RUN_DIR}" in runtime


def test_the_browser_target_carries_both_chromium_builds_and_the_browser_user() -> None:
    browser = dockerfile()[dockerfile().index("FROM runtime AS browser") :]
    # One `playwright install chromium` fetches the full build (browserd's) and the headless shell
    # (the skills'); Playwright's headless launch fails with only the full one.
    assert re.search(r"playwright install chromium(\s|\\)", browser) and "chromium-headless-shell" not in browser
    assert "chrome-linux*/chrome" in browser and "chrome-headless-shell" in browser
    assert "useradd --uid 1001" in browser and f"install -d -m 0700 -o browser -g browser {STATE_DIR}" in browser
    packages = (DEPLOY / "apt-packages-browser.txt").read_text(encoding="utf-8").split()
    assert {"libcups2t64", "libcairo2", "libpango-1.0-0", "libgbm1"} <= set(packages)


def test_the_build_context_leaves_the_daemons_build_leftovers_behind() -> None:
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").split()
    assert {"browserd/.cache", "browserd/browserd"} <= set(ignored)


# -- the rebuilder ------------------------------------------------------------------------------


async def run_rebuilder(tmp_path: Path, job: str, *, fail: str = "") -> tuple[Path, str]:
    """Run deploy/rebuild.sh against a fake docker that fails the subcommand named in ``fail``."""
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\ncase " $* " in *" $FAIL "*) exit 1 ;; esac\nexit 0\n')
    fake.chmod(0o755)
    (trigger / "browser-request").write_text(job + "\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""), "DAEDALUS_REBUILD_TRIGGER_DIR": str(trigger), "COMPOSE_FILE": "compose.yaml", "CALLS": str(tmp_path / "calls"), "FAIL": fail or "no-such-word"}
    process = await asyncio.create_subprocess_exec("sh", str(DEPLOY / "rebuild.sh"), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while (trigger / "browser-request").exists() or not (trigger / "alive").exists():
                await asyncio.sleep(0.02)
            if re.fullmatch(r"[0-9a-f]{32}", job):
                while not (trigger / f"browser-{job}.result").exists():
                    await asyncio.sleep(0.02)
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    return trigger, calls


async def test_the_rebuilder_builds_the_browser_target_then_recreates_only_the_browser_service(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the rebuilder is a POSIX shell script")
    job = "b" * 32
    trigger, calls = await run_rebuilder(tmp_path, job)
    lines = calls.strip().splitlines()
    assert lines[-2].endswith("build browser") and lines[-1].endswith("up -d --no-build --no-deps browser")
    assert (trigger / f"browser-{job}.result").read_text().strip() == "completed"


async def test_a_failed_build_leaves_the_running_browser_service_alone(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the rebuilder is a POSIX shell script")
    job = "c" * 32
    trigger, calls = await run_rebuilder(tmp_path, job, fail="build")
    assert "up -d" not in calls
    assert "was left as it was" in (trigger / f"browser-{job}.result").read_text()


async def test_the_rebuilder_drops_a_browser_request_that_is_not_a_job_id(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("the rebuilder is a POSIX shell script")
    trigger, calls = await run_rebuilder(tmp_path, "../../etc/passwd")
    assert calls == "" and not list(trigger.glob("browser-*"))


# -- the doctor ---------------------------------------------------------------------------------


class FakeBrowserd(FakePtyd):
    """ptyd's fake answering daemon.info as browserd does: the framing and the handshake are the same."""

    def __init__(self, run_dir: Path) -> None:
        super().__init__(run_dir)
        self.chromium: dict[str, Any] = {"path": "/opt/pw-browsers/chromium-1234/chrome-linux64/chrome", "version": "151.0.7922.34", "kind": "bundled"}
        self.sandbox = "ok"

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "daemon.info":
            return {"version": "src-0123456789ab", "protocol": 1, "instance": self.instance, "env": self.env, "chromium": self.chromium,
                    "capabilities": {"sandbox": self.sandbox, "headed": False, "screencast": True}, "counts": {"browsers": 0, "groups": 0, "tabs": 0, "viewers": 0}}
        return super()._handle(method, params)


@pytest.fixture
def base() -> Iterable[Path]:
    # Short: a unix socket path is limited to about a hundred bytes.
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "run").start()
    yield fake
    await fake.stop()


async def test_the_doctor_names_the_daemon_its_chromium_and_its_walls(settings: Settings, daemon: FakeBrowserd, base: Path) -> None:
    checks = {c.name: c for c in await _browser(DoctorContext(settings=settings.model_copy(update={"browser_container_dir": base / "run"}), config=RuntimeConfig()))}
    line = checks["browser (container)"]
    assert line.ok and line.message == "browserd src-0123456789ab available, Chromium 151.0.7922.34, bundled"
    assert "browser sandbox (container)" not in checks
    assert "no route to the key proxy" in checks["browser walls (container)"].message


async def test_the_doctor_says_natively_the_proxy_is_the_only_wall_and_when_the_sandbox_is_off(settings: Settings, daemon: FakeBrowserd, base: Path) -> None:
    daemon.sandbox = "no usable sandbox: set CHROME_DEVEL_SANDBOX to a setuid chrome-sandbox"
    daemon.chromium = {"path": "", "kind": "none", "error": "no Chromium found"}
    checks = {c.name: c for c in await _browser(DoctorContext(settings=settings.model_copy(update={"browser_host_dir": base / "run"}), config=RuntimeConfig()))}
    assert not checks["browser (host)"].ok and "no Chromium: no Chromium found" in checks["browser (host)"].message
    assert checks["browser (host)"].fix_hint == "daedalus-desktop install browser"
    assert not checks["browser sandbox (host)"].ok and "CHROME_DEVEL_SANDBOX" in checks["browser sandbox (host)"].message
    assert "the proxy's rules are the whole wall" in checks["browser walls (host)"].message


async def test_the_doctor_names_the_profile_for_a_stack_without_the_service(settings: Settings, base: Path) -> None:
    empty = base / "empty"
    empty.mkdir()
    [line] = await _browser(DoctorContext(settings=settings.model_copy(update={"browser_container_dir": empty}), config=RuntimeConfig()))
    assert not line.ok and line.severity == "info" and line.fix_hint == BROWSER_FIXES["not_installed_container"]
    assert "COMPOSE_PROFILES=browser" in line.fix_hint
    # A daemon that ran and stopped leaves its lock: that is "not running", with the command to start it.
    (empty / "browserd.lock").touch()
    [line] = await _browser(DoctorContext(settings=settings.model_copy(update={"browser_container_dir": empty}), config=RuntimeConfig()))
    assert line.message.startswith("not running") and line.severity == "warn" and "up -d browser" in line.fix_hint
    # Nothing configured, nothing said.
    assert await _browser(DoctorContext(settings=settings, config=RuntimeConfig())) == []


# -- the daemon of the operator's Chrome ---------------------------------------------------------------


USER_RUN_DIR = "/run/daedalus-browser-user"


def test_the_operators_chrome_has_a_daemon_of_its_own_with_no_network_and_its_run_directory_where_the_host_finds_it() -> None:
    services = compose()["services"]
    user, agent = services["browser-user"], services["daedalus"]
    command = user["command"]
    assert user["entrypoint"] == ["/usr/local/bin/browserd"]
    assert command[:5] == ["serve", "--env", "user", "--backend", "attach"]
    assert command[command.index("--run-dir") + 1] == USER_RUN_DIR
    # Nothing in it dials out or listens: Chrome comes through the agent's WebSocket and its socket.
    assert user["network_mode"] == "none" and "ports" not in user and "networks" not in user
    assert user["user"] == "1001:1001" and user["cap_drop"] == ["ALL"] and user["read_only"] is True
    assert user["profiles"] == ["browser"]
    assert volumes(user)[USER_RUN_DIR] == "browser-user-run" and volumes(agent)[USER_RUN_DIR] == "browser-user-run"
    assert agent["environment"]["BROWSER_USER_DIR"] == USER_RUN_DIR
    assert "browser-user-state" not in volumes(agent).values()
    assert f"{USER_RUN_DIR} " in dockerfile() or f"{USER_RUN_DIR}\n" in dockerfile()


def test_the_user_run_directory_is_sealed_from_the_agents_commands() -> None:
    settings = Settings(browser_user_dir=Path(USER_RUN_DIR))
    policy = Policy(sealed_paths=settings.sealed_paths, sealed_everywhere=settings.sealed_everywhere)
    assert policy.evaluate("Exec", {"command": f"cat {USER_RUN_DIR}/token"}).action == DENY
