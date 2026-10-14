"""SearXNG as a component: the settings it is given, the install, the supervisor's runner, and the wiring.

Everything here runs against fakes and the loopback interface. The archive is built in the test and
served by a mock transport, ``uv`` is replaced by the test's own interpreter, and the "SearXNG" the
runner supervises is a sleeping Python process — what is being checked is what Daedalus does around
SearXNG, which is the part that is ours to get wrong.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from daedalus.config import RuntimeConfig, Settings, WebSearchConfig, WebToolsConfig
from daedalus.host import components, searxng
from daedalus.host.component_install import Installer
from daedalus.tools import websearch

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "deploy" / "searxng" / "settings.yml"
runtime = searxng.runtime


def settings_for(tmp_path: Path, *, native: bool, runtime_dir: Path | None = None) -> Settings:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return Settings(_env_file=None, state_dir=state, native=native, runtime_dir=runtime_dir, bot_repo_dir=REPO)  # type: ignore[call-arg]


def mark_installed(root: Path) -> None:
    """The two things the runner looks for: the marker and the interpreter it names."""
    python = runtime.venv_python(root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")
    runtime.write_json(root / runtime.MARKER, {"version": runtime.VERSION})


# -- the settings SearXNG is given ---------------------------------------------------------------


def test_the_generated_settings_are_the_stacks_with_a_secret_a_loopback_bind_and_no_limiter(tmp_path: Path) -> None:
    path = runtime.write_settings(tmp_path, TEMPLATE)
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    server = settings["server"]
    assert len(server["secret_key"]) == 64 and server["secret_key"] != "ultrasecretkey"
    assert server["bind_address"] == "127.0.0.1", "nothing but this machine may reach it"
    assert server["limiter"] is False, "the limiter caps a JSON client at a few requests an hour"
    assert "json" in settings["search"]["formats"], "without it the tool's format=json answers 403"
    # The rest is the stack's own file, so an engine dropped there is dropped here.
    assert settings["use_default_settings"]["engines"]["keep_only"] == yaml.safe_load(TEMPLATE.read_text())["use_default_settings"]["engines"]["keep_only"]


def test_the_secret_is_minted_once_and_kept_across_starts(tmp_path: Path) -> None:
    first = runtime.write_settings(tmp_path, TEMPLATE).read_text()
    second = runtime.write_settings(tmp_path, TEMPLATE).read_text()
    assert first == second
    assert runtime.secret_key(tmp_path) in first


def test_a_template_without_a_server_section_is_refused_rather_than_run_without_a_secret() -> None:
    with pytest.raises(ValueError):
        runtime.render_settings("search:\n  formats: [json]\n", "s")


def test_the_folder_is_the_runtime_folder_natively_and_the_state_volume_in_a_container(tmp_path: Path) -> None:
    assert runtime.root_for(tmp_path / "state", tmp_path / "runtime") == tmp_path / "runtime" / "searxng"
    assert runtime.root_for(tmp_path / "state", None) == tmp_path / "state" / "searxng"


def test_the_port_of_the_last_start_is_kept_when_it_is_free() -> None:
    port = runtime.pick_port(0)
    assert runtime.pick_port(port) == port
    import socket

    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen()
        assert runtime.pick_port(port) != port, "a taken port is passed over, not fought for"


# -- the supervisor's runner ---------------------------------------------------------------------


class FakeSearxng:
    """Starts a sleeping interpreter in place of SearXNG and answers its health from a flag."""

    def __init__(self) -> None:
        self.healthy = True
        self.spawned: list[asyncio.subprocess.Process] = []

    async def spawn(self, port: int) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        self.spawned.append(process)
        return process

    def probe(self, url: str) -> bool:
        return self.healthy


async def until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("the condition never came true")
        await asyncio.sleep(0.02)


async def test_the_runner_starts_what_is_installed_restarts_it_when_it_dies_and_stops_on_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "BACKOFF_FIRST", 0.05)
    fake = FakeSearxng()
    lines: list[str] = []
    runner = runtime.Runner(tmp_path, TEMPLATE, lines.append, spawn=fake.spawn, probe=fake.probe, poll=0.05)
    runner.backoff = 0.05
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(0.2)
        assert fake.spawned == [], "nothing runs until the install's marker is there"

        mark_installed(tmp_path)
        await until(lambda: runtime.read_endpoint(tmp_path).get("state") == "running")
        first = runtime.read_endpoint(tmp_path)
        assert first["url"].startswith("http://127.0.0.1:") and first["pid"] == fake.spawned[0].pid
        assert searxng.local_url(tmp_path) == first["url"]

        fake.spawned[0].kill()
        await until(lambda: len(fake.spawned) == 2 and runtime.read_endpoint(tmp_path).get("state") == "running")
        assert runtime.read_endpoint(tmp_path)["port"] == first["port"], "the address survives a restart"
        assert any("starting it again" in line for line in lines)

        (tmp_path / runtime.MARKER).unlink()
        await until(lambda: runtime.read_endpoint(tmp_path).get("state") == "stopped")
        await until(lambda: fake.spawned[1].returncode is not None)
        assert searxng.local_url(tmp_path) == ""
    finally:
        await runner.stop()
        await asyncio.wait_for(task, 10)
        for process in fake.spawned:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_an_instance_that_never_answers_is_ended_and_reported_with_a_backoff(tmp_path: Path) -> None:
    fake = FakeSearxng()
    fake.healthy = False
    mark_installed(tmp_path)
    runner = runtime.Runner(tmp_path, TEMPLATE, lambda _line: None, spawn=fake.spawn, probe=fake.probe, poll=0.05, start_timeout=0.3)
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runtime.read_endpoint(tmp_path).get("state") == "failed")
        failed = runtime.read_endpoint(tmp_path)
        assert "did not answer" in failed["error"] and failed["retry_in"] >= 2
        await until(lambda: fake.spawned[0].returncode is not None)
        status = searxng.service(tmp_path)
        assert status is not None and status["state"] == "failed" and status["error"]
    finally:
        await runner.stop()
        await asyncio.wait_for(task, 10)
    assert runtime.read_endpoint(tmp_path)["state"] == "stopped"


async def test_stopping_the_supervisor_stops_the_instance(tmp_path: Path) -> None:
    fake = FakeSearxng()
    mark_installed(tmp_path)
    runner = runtime.Runner(tmp_path, TEMPLATE, lambda _line: None, spawn=fake.spawn, probe=fake.probe, poll=0.05)
    task = asyncio.create_task(runner.run())
    await until(lambda: runtime.read_endpoint(tmp_path).get("state") == "running")
    await runner.stop()
    await asyncio.wait_for(task, 10)
    assert fake.spawned[0].returncode is not None
    assert runtime.read_endpoint(tmp_path)["state"] == "stopped"


def test_the_instance_inherits_no_secret_of_the_supervisors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    env = runtime.child_env(tmp_path, 8768)
    assert "TELEGRAM_BOT_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["SEARXNG_BIND_ADDRESS"] == "127.0.0.1" and env["SEARXNG_PORT"] == "8768"
    assert env["TMPDIR"] == str(tmp_path / "cache")


def test_the_supervisor_runs_searxng_from_the_folder_the_agent_installs_into(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setenv("DAEDALUS_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DAEDALUS_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("DAEDALUS_BOT_REPO", str(REPO))
    spec = importlib.util.spec_from_file_location("supervisor_searxng_under_test", REPO / "launcher" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_searxng_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    supervisor = module.Supervisor()
    agent = settings_for(tmp_path, native=True, runtime_dir=tmp_path / "runtime")
    assert supervisor.searxng.root == searxng.root_for(agent.state_dir, agent.runtime_dir) == tmp_path / "runtime" / "searxng"
    assert supervisor.searxng.template == REPO / "deploy" / "searxng" / "settings.yml"


# -- what the agent reads -----------------------------------------------------------------------


def test_an_instance_recorded_as_running_that_does_not_answer_reads_as_stopped(tmp_path: Path) -> None:
    mark_installed(tmp_path)
    runtime.write_json(tmp_path / runtime.ENDPOINT, {"state": "running", "url": "http://127.0.0.1:9", "port": 9})
    assert searxng.service(tmp_path, connect=lambda *_: False)["state"] == "stopped"  # type: ignore[index]
    assert searxng.service(tmp_path, connect=lambda *_: True)["url"] == "http://127.0.0.1:9"  # type: ignore[index]
    assert searxng.service(tmp_path / "nothing") is None


def test_websearch_goes_to_the_installations_own_instance_only_while_the_address_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websearch, "local_searxng", lambda: "http://127.0.0.1:8768")
    assert websearch.searxng_url("http://searxng:8080") == "http://127.0.0.1:8768"
    assert websearch.searxng_url("http://searxng:8080/") == "http://127.0.0.1:8768"
    assert websearch.searxng_url("http://search.lan:8080") == "http://search.lan:8080", "an address the operator typed is theirs"
    monkeypatch.setattr(websearch, "local_searxng", lambda: "")
    assert websearch.searxng_url("http://searxng:8080") == "http://searxng:8080"


class _FakeSearxngHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the standard library's name
        assert self.path.startswith("/search?") and "format=json" in self.path
        body = json.dumps({"results": [{"title": "SearXNG", "url": "https://docs.searxng.org/", "content": "Search API", "engines": ["wikipedia"]}],
                           "unresponsive_engines": []}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


async def test_the_search_check_reaches_a_local_instance_with_the_configuration_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default configuration, an instance on a loopback port, and a real request between them."""
    server = HTTPServer(("127.0.0.1", 0), _FakeSearxngHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(websearch, "local_searxng", lambda: f"http://127.0.0.1:{server.server_port}")
        outcome = await websearch.search(websearch.SearchQuery(text="searxng json api", limit=5), WebToolsConfig(), chain=["searxng"])
    finally:
        server.shutdown()
    assert outcome.backend == "searxng" and outcome.hits[0].url == "https://docs.searxng.org/"


def test_an_install_makes_searxng_the_backend_and_a_removal_gives_it_back() -> None:
    default = WebSearchConfig()
    taken = searxng.adopted(default, installed=True)
    assert taken is not None and taken.backend == "searxng" and taken.fallback == ["duckduckgo"]
    given_back = searxng.adopted(taken, installed=False)
    assert given_back is not None and given_back.backend == "duckduckgo" and given_back.fallback == ["searxng"]
    assert searxng.adopted(taken, installed=True) is None, "already the backend: nothing to change"


def test_an_install_leaves_a_backend_or_an_address_the_operator_chose_alone() -> None:
    chosen = WebSearchConfig(backend="tavily", fallback=["searxng", "duckduckgo"])
    assert searxng.adopted(chosen, installed=True) is None
    elsewhere = WebSearchConfig()
    elsewhere.searxng.url = "http://search.lan:8080"
    assert searxng.adopted(elsewhere, installed=True) is None


# -- the install ---------------------------------------------------------------------------------


def archive_bytes(*, extra: dict[str, bytes] | None = None) -> bytes:
    files = {
        f"searxng-{runtime.COMMIT}/requirements.txt": b"flask==3.1.3\n",
        f"searxng-{runtime.COMMIT}/searx/__init__.py": b"",
        f"searxng-{runtime.COMMIT}/searx/settings.yml": b"general: {}\n",
        f"searxng-{runtime.COMMIT}/tests/test_x.py": b"",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def serving(payload: bytes, status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == runtime.ARCHIVE_URL
        return httpx.Response(status, content=payload)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def fake_environment(self: searxng.Install) -> None:
    """Stands in for ``uv venv`` and ``uv pip install``: the interpreter appears, nothing is fetched."""
    python = runtime.venv_python(self.root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")


async def test_an_install_unpacks_the_pinned_archive_writes_its_settings_and_its_marker_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = archive_bytes(extra={f"searxng-{runtime.COMMIT}/../escape.txt": b"no"})
    monkeypatch.setattr(runtime, "ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(searxng.Install, "environment", fake_environment)
    root = tmp_path / "searxng"
    steps: list[str] = []
    install = searxng.Install(root, TEMPLATE, steps.append, client=serving(payload))

    async def supervisor() -> None:
        # What the supervisor does once the marker appears: start it and say where.
        await until(lambda: (root / runtime.MARKER).exists())
        runtime.write_json(root / runtime.ENDPOINT, {"state": "running", "at": time.time(), "url": "http://127.0.0.1:8768", "port": 8768})

    helper = asyncio.create_task(supervisor())
    url = await install.run()
    await helper
    assert url == "http://127.0.0.1:8768"
    source = root / runtime.SOURCE
    assert (source / "requirements.txt").is_file() and (source / "searx" / "settings.yml").is_file()
    assert not (source / "tests").exists(), "only what SearXNG runs from is unpacked"
    assert not (tmp_path / "escape.txt").exists() and not (root / "escape.txt").exists()
    assert runtime.VERSION in (source / "searx" / "version_frozen.py").read_text(), "an archive has no git to read a version from"
    assert yaml.safe_load((root / runtime.SETTINGS).read_text())["server"]["bind_address"] == "127.0.0.1"
    assert not (root / "searxng.tar.gz").exists()
    assert any(step.startswith("downloading SearXNG") for step in steps)


async def test_an_archive_that_does_not_match_the_pin_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(searxng.Install, "environment", fake_environment)
    root = tmp_path / "searxng"
    install = searxng.Install(root, TEMPLATE, lambda _step: None, client=serving(archive_bytes()))
    with pytest.raises(searxng.InstallFailed, match="pinned digest"):
        await install.run()
    assert not (root / runtime.MARKER).exists() and not (root / "searxng.tar.gz").exists()


async def test_a_download_that_fails_says_so(tmp_path: Path) -> None:
    install = searxng.Install(tmp_path / "searxng", TEMPLATE, lambda _step: None, client=serving(b"", status=404))
    with pytest.raises(searxng.InstallFailed, match="HTTP 404"):
        await install.run()


async def test_an_install_with_no_supervisor_to_start_it_ends_installed_but_not_running(tmp_path: Path) -> None:
    install = searxng.Install(tmp_path, TEMPLATE, lambda _step: None)
    assert await install.wait_running(silence=0.1, interval=0.02) == ""


async def test_an_instance_that_fails_its_first_start_fails_the_install_with_the_reason(tmp_path: Path) -> None:
    install = searxng.Install(tmp_path, TEMPLATE, lambda _step: None)

    async def failing() -> None:
        await asyncio.sleep(0.05)
        runtime.write_json(tmp_path / runtime.ENDPOINT, {"state": "failed", "at": time.time(), "error": "exited with code 1: ImportError"})

    helper = asyncio.create_task(failing())
    with pytest.raises(searxng.InstallFailed, match="ImportError"):
        await install.wait_running(interval=0.02)
    await helper


def test_the_environment_is_made_with_uv_when_there_is_one_and_with_venv_otherwise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install = searxng.Install(tmp_path, TEMPLATE, lambda _step: None)
    monkeypatch.setattr(searxng.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    with_uv = install.commands()
    assert with_uv[0][:2] == ["/usr/local/bin/uv", "venv"] and with_uv[1][:3] == ["/usr/local/bin/uv", "pip", "install"]
    assert str(tmp_path / "src" / "requirements.txt") in with_uv[1]
    monkeypatch.setattr(searxng.shutil, "which", lambda name: None)
    without = install.commands()
    assert without[0] == [sys.executable, "-m", "venv", str(tmp_path / "venv")]
    assert without[1][1:4] == ["-m", "pip", "install"]


async def test_a_removal_takes_the_marker_first_and_then_the_folder(tmp_path: Path) -> None:
    root = tmp_path / "searxng"
    mark_installed(root)
    runtime.write_json(root / runtime.ENDPOINT, {"state": "running", "url": "http://127.0.0.1:8768"})

    async def supervisor() -> None:
        await until(lambda: not (root / runtime.MARKER).exists())
        runtime.write_json(root / runtime.ENDPOINT, {"state": "stopped"})

    helper = asyncio.create_task(supervisor())
    await searxng.remove(root, interval=0.02)
    await helper
    assert not root.exists()


# -- the Components page ---------------------------------------------------------------------------


def test_a_machine_without_searxng_is_offered_the_install(tmp_path: Path) -> None:
    status = components.Registry(settings_for(tmp_path, native=True, runtime_dir=tmp_path / "runtime"), RuntimeConfig()).status(components.SEARXNG)
    assert status.state == "missing" and status.installable and status.how == "service"
    assert status.detail_key == "comp.detail.searxng.missing" and status.service is None and not status.removable


def test_an_installed_and_running_searxng_reads_as_running_and_can_be_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "runtime" / "searxng"
    mark_installed(root)
    runtime.write_json(root / runtime.ENDPOINT, {"state": "running", "url": "http://127.0.0.1:8768", "port": 8768})
    monkeypatch.setattr(searxng, "_answers", lambda *_: True)
    status = components.Registry(settings_for(tmp_path, native=True, runtime_dir=tmp_path / "runtime"), RuntimeConfig()).status(components.SEARXNG)
    assert status.state == "installed" and status.removable
    assert status.service is not None and status.service["state"] == "running"
    assert status.detail_key == "comp.detail.searxng.running" and status.detail_args["url"] == "http://127.0.0.1:8768"
    assert status.as_dict()["service"]["url"] == "http://127.0.0.1:8768"  # type: ignore[index]


def test_a_container_with_the_stacks_searxng_reads_it_as_installed_and_not_ours_to_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(searxng, "_answers", lambda host, port, timeout: seen.append(f"{host}:{port}") or True)
    status = components.Registry(settings_for(tmp_path, native=False), RuntimeConfig()).status(components.SEARXNG)
    assert seen == ["searxng:8080"]
    assert status.state == "installed" and not status.removable and not status.installable
    assert status.detail_key == "comp.detail.searxng.stack"


def test_a_container_without_the_stacks_searxng_can_install_one_into_its_state_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(searxng, "_answers", lambda *_: False)
    status = components.Registry(settings_for(tmp_path, native=False), RuntimeConfig()).status(components.SEARXNG)
    assert status.state == "missing" and status.installable and status.how == "service"


async def test_the_installer_installs_searxng_itself_and_tells_the_application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changes: list[tuple[str, bool]] = []

    async def after(component_id: str, installed: bool) -> None:
        changes.append((component_id, installed))

    async def run(self: searxng.Install) -> str:
        self.report("downloading SearXNG")
        mark_installed(self.root)
        return "http://127.0.0.1:8768"

    monkeypatch.setattr(searxng.Install, "run", run)
    monkeypatch.setattr(components.shutil, "disk_usage", lambda _path: type("u", (), {"free": 10 * 1024**3})())
    settings = settings_for(tmp_path, native=True, runtime_dir=tmp_path / "runtime")
    installer = Installer(settings, after=after)
    status = components.Registry(settings, RuntimeConfig(), installer=installer).status(components.SEARXNG)
    installer.start(status)
    await installer.wait(components.SEARXNG)
    assert installer.progress_of(components.SEARXNG)["state"] == "installed"  # type: ignore[index]
    assert changes == [(components.SEARXNG, True)]

    async def removed(root: Path, **_: Any) -> None:
        (root / runtime.MARKER).unlink()

    monkeypatch.setattr(searxng, "remove", removed)
    await installer.remove(components.SEARXNG)
    assert changes[-1] == (components.SEARXNG, False)


def test_the_launcher_can_install_searxng_from_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """``daedalus components install searxng`` is how the setup wizard's choice is carried out natively."""
    from daedalus.__main__ import main

    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("DAEDALUS_RUNTIME", str(tmp_path / "runtime"))
    installs: list[Path] = []

    async def run(self: searxng.Install) -> str:
        installs.append(self.root)
        mark_installed(self.root)
        return ""

    monkeypatch.setattr(searxng.Install, "run", run)
    assert main(["components", "install", "searxng"]) == 0
    assert installs == [tmp_path / "runtime" / "searxng"]
    assert "starts with the supervisor" in capsys.readouterr().out
    assert main(["components", "install", "searxng"]) == 0 and len(installs) == 1, "an installed SearXNG is not installed again"
