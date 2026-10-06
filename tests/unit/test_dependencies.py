"""Dependency planning cannot install; an approved recipe cannot smuggle commands into installers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from protocore.contracts.llm import ProviderDelta

from daedalus.host.dependencies import KEY, DependencyPlanner
from daedalus.providers.wire import messages_to_wire
from daedalus.tools.shell import shell_argv, shell_environment
from tests.unit.test_components import HEAD, FakeApp
from tests.unit.test_providers import RecordingSink, _chunk, _provider, _sse

SPEC = importlib.util.spec_from_file_location("dependency_runtime_test", Path(__file__).resolve().parents[2] / "launcher" / "dependencies.py")
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.mark.parametrize("package", ["--index-url", "https://example.com/a.whl", "../evil", "a;touch marker", "a\nb", "$(id)", "a[extra]", "a @ file:x", "-rfile"])
def test_recipe_rejects_commands_paths_and_sources(package: str) -> None:
    with pytest.raises(ValueError):
        runtime.recipe({"python": [package], "system": []})
    with pytest.raises(ValueError):
        runtime.recipe({"python": [], "system": [package]})


def test_recipe_canonicalizes_and_refuses_multiple_versions() -> None:
    assert runtime.recipe({"python": ["Pillow==11.0", "Pillow==11.0"], "system": ["gcc", "make"]}) == {"python": ["Pillow==11.0"], "system": ["gcc", "make"]}
    with pytest.raises(ValueError):
        runtime.recipe({"python": ["my-package==1", "my_package==2"], "system": []})
    with pytest.raises(ValueError):
        runtime.recipe({"python": [], "system": [], "command": "echo test"})


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    instance = runtime.Dependencies(tmp_path / "repo", tmp_path / "state", tmp_path / "trigger", native=True)
    monkeypatch.setattr(instance, "capability", lambda: {"mode": "native", "python": True, "system": True, "manager": "apt", "reason": ""})
    return instance


def test_preview_never_writes_and_accept_checks_staleness(service: Any) -> None:
    proposal = service.preview({"python": ["Pillow"], "system": []})
    assert "+" in proposal["patch"] and "Pillow" in proposal["patch"]
    assert not service.manifest.exists()
    runtime.write_json(service.manifest, {"python": ["numpy"], "system": []})
    with pytest.raises(ValueError, match="changed"):
        service.accept(proposal, "a" * 32)


@pytest.mark.asyncio
async def test_preview_checks_real_catalog_shape_without_installing(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(service, "system_manager", lambda: "apt")

    async def fake_command(argv: list[str], **kwargs: Any) -> str:
        calls.append(argv)
        if argv[0] == "apt-get":
            assert argv[-1] == "update" and "install" not in argv
            assert "APT::Update::Error-Mode=any" in argv
            return ""
        assert argv[:3] == ["env", "LC_ALL=C", "apt-cache"]
        return "gcc:\n  Installed: (none)\n  Candidate: 13.2\nmissing:\n  Installed: (none)\n  Candidate: (none)\n"

    monkeypatch.setattr(runtime, "command", fake_command)
    with pytest.raises(ValueError, match="Unavailable.*absent, missing"):
        await service.checked_preview({"python": [], "system": ["gcc", "missing", "absent"]})
    result = await service.checked_preview({"python": [], "system": ["gcc"]})
    assert result["recipe"]["system"] == ["gcc"]
    assert sum(argv[0] == "apt-get" for argv in calls) == 1
    assert not service.manifest.exists()
    assert not (service.root / "job.json").exists()


@pytest.mark.asyncio
async def test_failed_catalog_refresh_cannot_produce_a_ready_patch(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "system_manager", lambda: "apt")

    async def failed(argv: list[str], **kwargs: Any) -> str:
        raise RuntimeError("repository unavailable")

    monkeypatch.setattr(runtime, "command", failed)
    with pytest.raises(RuntimeError, match="repository unavailable"):
        await service.checked_preview({"python": [], "system": ["gcc"]})
    assert not (service.root / "apt-lists" / "checked").exists()
    assert not service.manifest.exists()
    assert not (service.root / "job.json").exists()


def test_accept_revalidates_recipe_and_rejects_removals(service: Any) -> None:
    runtime.write_json(service.manifest, {"python": ["numpy"], "system": []})
    proposal = service.preview({"python": ["Pillow"], "system": []})
    proposal["recipe"]["python"] = ["Pillow"]
    with pytest.raises(ValueError, match="only add"):
        service.accept(proposal, "a" * 32)


def test_unprivileged_native_system_install_refuses_without_sudo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = runtime.Dependencies(tmp_path, tmp_path / "state", tmp_path / "trigger", native=True)
    monkeypatch.setattr(service, "system_manager", lambda: "apt")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/uv")
    assert service.capability()["python"] is True
    assert service.capability()["system"] is False
    with pytest.raises(ValueError, match="never elevates"):
        service.preview({"python": [], "system": ["gcc"]})


@pytest.mark.asyncio
async def test_native_installs_into_versioned_environment_and_never_host_python(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def command(argv: list[str], **kwargs: Any) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(runtime, "command", command)
    proposal = service.preview({"python": ["Pillow"], "system": []})
    job = service.accept(proposal, "a" * 32)
    await service.install(job)
    assert calls[0][:2] == ["uv", "venv"]
    assert str(service.root / "python") in calls[1][4]
    assert calls[1][-2:] == ["--", "Pillow"]
    assert not any("sudo" in call for call in calls)
    assert service.active_bin().startswith(str(service.root / "python"))
    assert runtime.read_json(service.root / "job.json", {})["state"] == "restarting"


@pytest.mark.asyncio
async def test_docker_writes_recipe_and_trigger_but_never_installs_in_running_container(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service.native = False
    service.trigger.mkdir()

    async def forbidden(*args: Any, **kwargs: Any) -> str:
        pytest.fail("Docker dependencies must be installed during image build only")

    monkeypatch.setattr(runtime, "command", forbidden)
    job = service.accept(service.preview({"python": ["Pillow"], "system": ["gcc"]}), "b" * 32)
    await service.install(job)
    assert service.current() == job["recipe"]
    assert (service.trigger / "dependencies-request").read_text().strip() == "b" * 32
    with pytest.raises(ValueError, match="already running"):
        service.accept(service.preview({"python": ["numpy"], "system": []}), "c" * 32)
    (service.trigger / f"dependencies-{job['id']}.result").write_text("build failed")
    assert service.status()["job"]["state"] == "failed"
    assert service.current() == runtime.EMPTY
    assert not (service.root / "maintenance").exists()
    service.preview({"python": ["Pillow"], "system": ["gcc"]})


def test_interrupted_native_install_reports_partial_failure_and_unlocks(service: Any) -> None:
    service.accept(service.preview({"python": ["Pillow"], "system": []}), "a" * 32)
    assert service.status()["job"]["state"] == "failed"
    assert not (service.root / "maintenance").exists()


def test_status_for_old_job_does_not_unlock_a_new_approval(service: Any) -> None:
    runtime.write_json(service.root / "job.json", {"id": "old", "state": "completed"})
    (service.root / "maintenance").write_text("new")
    service.status()
    assert (service.root / "maintenance").read_text() == "new"


def test_tool_shell_uses_agent_environment_without_changing_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent_bin = str(tmp_path / "agent bin")
    monkeypatch.setenv("DAEDALUS_AGENT_BIN", agent_bin)
    argv = shell_argv("python script.py", windows=False)
    assert argv[:2] == ["bash", "-lc"]
    assert argv[2].startswith("export PATH=") and argv[2].endswith("python script.py")
    assert shell_environment("session")["PATH"].startswith(agent_bin)


class MemoryDB:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def kv_get(self, key: str) -> Any:
        return self.values.get(key)

    async def kv_set(self, key: str, value: Any) -> None:
        self.values[key] = value


class Provider:
    def __init__(self, turns: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.turns = iter(turns)
        self.requests: list[Any] = []

    async def stream_with_tools(self, request: Any) -> Any:
        pending: set[str] = set()
        for message in await messages_to_wire(request.messages):
            if message["role"] == "tool":
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
            else:
                assert not pending, "tool results must precede the next conversation message"
                pending.update(call["id"] for call in message.get("tool_calls", []))
        assert not pending, "every tool call must have a result before the next request"
        self.requests.append(request)
        for index, (name, arguments) in enumerate(next(self.turns)):
            yield ProviderDelta(kind="tool_use_start", tool_call_id=str(index), tool_name=name)
            yield ProviderDelta(kind="tool_use_stop", tool_call_id=str(index), tool_input_final=arguments)


def planner_for(tmp_path: Path, provider: Any) -> DependencyPlanner:
    preset = SimpleNamespace(provider="test", display=lambda key: key)
    manager = SimpleNamespace(resolve_model=lambda overrides: ([(provider, "test")], preset), budget_exceeded=lambda: None, provider_costs_nothing=lambda *_: True, dependency_installation_busy=lambda: False)
    return DependencyPlanner(SimpleNamespace(db=MemoryDB(), settings=SimpleNamespace(state_dir=tmp_path), config=SimpleNamespace(presets={"test": preset}), manager=manager))


@pytest.mark.asyncio
async def test_model_only_gets_read_and_propose_tools_and_never_installs(tmp_path: Path, service: Any) -> None:
    provider = Provider([[('InspectEnvironment', {})], [('ProposeDependencies', {"python": ["Pillow"], "system": [], "explanation": "Image processing"})]])
    planner = planner_for(tmp_path, provider)
    calls = []

    async def rpc(op: str, **kwargs: Any) -> Any:
        calls.append(op)
        if op == "dependencies_preview":
            return service.preview(kwargs["additions"])
        return {"capability": service.capability(), "job": None}

    planner.rpc = rpc
    result = await planner.start("Process images", "test")
    await planner.task
    proposal = await planner.proposal()
    assert proposal["state"] == "ready" and proposal["id"] == result["id"]
    assert [event["stage"] for event in proposal["progress"]] == ["model", "inspect", "model", "check", "ready"]
    assert proposal["updated_at"] >= proposal["started_at"]
    assert set(calls) == {"dependencies_status", "dependencies_inventory", "dependencies_preview"}
    assert {tool.name for tool in provider.requests[0].tools} == {"InspectEnvironment", "ProposeDependencies"}
    assert not service.manifest.exists()


def test_docker_status_preserves_stages_deadline_and_live_output(service: Any) -> None:
    service.native = False
    service.trigger.mkdir()
    job = service.accept(service.preview({"python": [], "system": ["gcc"]}), "d" * 32)
    progress = service.trigger / f"dependencies-{job['id']}.progress"
    runtime.write_json(progress, {"stage": "building"})
    (service.trigger / f"dependencies-{job['id']}.log").write_text("DAEDALUS_DEPENDENCIES_STAGE=system\nSetting up gcc\n")
    current = service.status()["job"]
    assert current["stage"] == "building" and current["detail_stage"] == "system"
    assert "Setting up gcc" in current["log"]
    deadline = runtime.time.time() + 30
    runtime.write_json(progress, {"stage": "restart_pending", "restart_at": deadline})
    current = service.status()["job"]
    assert current["restart_at"] == deadline
    assert current["progress"][-1]["stage"] == "restart_pending"
    assert service.status()["job"]["restart_at"] == deadline
    assert current["state"] == "installing"
    assert not (service.trigger / f"dependencies-{job['id']}.result").exists()


def test_restart_notice_is_authenticated_and_not_returned_for_completed_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.extensions.api import build_app

    app = FakeApp(tmp_path, native=True)
    job = {"id": "notice", "state": "installing", "stage": "restart_pending", "restart_at": 100}

    async def rpc(self: Any, op: str, **kwargs: Any) -> Any:
        assert op == "dependencies_status"
        return {"job": job}

    monkeypatch.setattr(DependencyPlanner, "rpc", rpc)
    with TestClient(build_app(app, "tok")) as client:
        assert client.get("/api/maintenance").status_code == 401
        response = client.get("/api/maintenance", headers={"X-Daedalus-Token": "tok"}).json()
        assert response["notice"] == {"id": "notice", "stage": "restart_pending", "restart_at": 100}
        assert response["server_time"] > 0
        job["state"] = "completed"
        assert client.get("/api/maintenance", headers={"X-Daedalus-Token": "tok"}).json()["notice"] is None


@pytest.mark.asyncio
async def test_planner_rejects_unknown_tools_and_stops_after_five_turns(tmp_path: Path) -> None:
    provider = Provider([[('Exec', {"command": "echo forbidden"})]] * 5)
    planner = planner_for(tmp_path, provider)
    with pytest.raises(ValueError, match="five-turn"):
        await planner._run({"id": "a" * 32, "preset": "test", "request": "images"})
    assert len(provider.requests) == 5


@pytest.mark.asyncio
async def test_planner_serializes_every_result_in_a_mixed_tool_batch(tmp_path: Path) -> None:
    provider = Provider([[('InspectEnvironment', {}), ('Unknown', {})], [('ProposeDependencies', {"python": [], "system": ["gcc"], "explanation": "Compiler"})]])
    planner = planner_for(tmp_path, provider)

    async def rpc(op: str, **kwargs: Any) -> Any:
        return {"operation": op}

    planner.rpc = rpc
    result = await planner._run({"id": "a" * 32, "preset": "test", "request": "Compiler"})
    assert result["explanation"] == "Compiler"
    wire = await messages_to_wire(provider.requests[1].messages)
    results = [message for message in wire if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["0", "1"]
    assert "dependencies_inventory" in results[0]["content"]
    assert "only InspectEnvironment" in results[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "tool_calls"])
async def test_planner_drains_usage_but_never_executes_an_incomplete_batch(tmp_path: Path, finish: str) -> None:
    sink = RecordingSink()
    body = _sse([
        _chunk({"tool_calls": [
            {"index": 0, "id": "inspect", "function": {"name": "InspectEnvironment", "arguments": "{}"}},
            {"index": 1, "id": "proposal", "function": {"name": "ProposeDependencies", "arguments": '{"python": [], "system": ["gcc", "g'}},
        ]}),
        _chunk({}, finish=finish),
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 2500}},
    ])
    provider = _provider(body, sink=sink)
    planner = planner_for(tmp_path, provider)

    async def rpc(op: str, **kwargs: Any) -> Any:
        pytest.fail("an incomplete batch must not execute even its complete calls")

    planner.rpc = rpc
    try:
        with pytest.raises(ValueError, match="2500-token" if finish == "length" else "incomplete"):
            await planner._run({"id": "a" * 32, "preset": "test", "request": "Compiler"})
        assert len(sink.records) == 1
        assert sink.records[0].normalized["output_tokens"] == 2500
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_cancel_discards_proposal_and_cannot_then_approve(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})
    await planner.cancel("a")
    assert (await planner.proposal())["state"] == "cancelled"
    with pytest.raises(ValueError, match="no longer"):
        await planner.approve("a")


@pytest.mark.asyncio
async def test_busy_agent_prevents_approval_and_clears_maintenance(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    planner.app.manager.dependency_installation_busy = lambda: True
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})

    async def rpc(op: str, **kwargs: Any) -> Any:
        assert op == "dependencies_status"
        return {"job": None}

    planner.rpc = rpc
    with pytest.raises(ValueError, match="stop running"):
        await planner.approve("a")
    assert not (tmp_path / "dependencies" / "maintenance").exists()
    assert (await planner.proposal())["state"] == "ready"


@pytest.mark.asyncio
async def test_approval_is_exactly_once_and_persists_across_page_reload(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready", "proposal": {"recipe": {"python": ["Pillow"], "system": []}}})
    calls = []

    async def rpc(op: str, **kwargs: Any) -> Any:
        calls.append(op)
        assert (tmp_path / "dependencies" / "maintenance").exists()
        return {"id": "a", "state": "installing"}

    planner.rpc = rpc
    await planner.approve("a")
    other_page = DependencyPlanner(planner.app)
    assert (await other_page.proposal())["state"] == "applying"
    assert await planner.approve("a") == {"id": "a", "state": "applying"}
    assert calls == ["dependencies_apply"]


@pytest.mark.asyncio
async def test_supervisor_receipt_recovers_a_lost_approval_acknowledgement(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "ready"})

    async def rpc(op: str, **kwargs: Any) -> Any:
        assert op in ("dependencies_status", "dependencies_inventory")
        return {"job": {"id": "a", "state": "installing"}}

    planner.rpc = rpc
    assert (await planner.view())["proposal"]["state"] == "applying"
    assert (await planner.proposal())["state"] == "applying"


@pytest.mark.asyncio
async def test_command_timeout_kills_process() -> None:
    with pytest.raises(TimeoutError):
        await runtime.command([runtime.sys.executable, "-c", "import time; time.sleep(30)"], limit=0.05)


@pytest.mark.asyncio
async def test_cancel_interrupts_planning(tmp_path: Path) -> None:
    planner = planner_for(tmp_path, None)
    await planner.app.db.kv_set(KEY, {"id": "a", "state": "planning"})
    planner.task = asyncio.create_task(asyncio.sleep(30))
    await planner.cancel("a")
    assert planner.task.cancelled()
    assert (await planner.proposal())["state"] == "cancelled"


@pytest.mark.asyncio
async def test_real_native_python_install_is_offline_and_isolated(service: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not runtime.shutil.which("uv"):
        pytest.skip("uv is required for the native installer")
    wheel = tmp_path / "dependency_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dependency_fixture/__init__.py", "VALUE = 42\n")
        archive.writestr("dependency_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: dependency-fixture\nVersion: 1.0\n")
        archive.writestr("dependency_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("dependency_fixture-1.0.dist-info/RECORD", "dependency_fixture/__init__.py,,\ndependency_fixture-1.0.dist-info/METADATA,,\ndependency_fixture-1.0.dist-info/WHEEL,,\ndependency_fixture-1.0.dist-info/RECORD,,\n")
    real_command = runtime.command

    async def offline(argv: list[str], **kwargs: Any) -> str:
        if argv[:3] == ["uv", "pip", "install"]:
            argv = [*argv[:3], "--offline", "--no-index", "--find-links", str(tmp_path), *argv[3:]]
        return await real_command(argv, **kwargs)

    monkeypatch.setattr(runtime, "command", offline)
    proposal = service.preview({"python": ["dependency-fixture==1.0"], "system": []})
    await service.install(service.accept(proposal, "a" * 32))
    python = str(Path(service.active_bin()) / ("python.exe" if os.name == "nt" else "python"))
    assert await real_command([python, "-I", "-c", "import dependency_fixture; print(dependency_fixture.VALUE)"]) == "42"
    assert importlib.util.find_spec("dependency_fixture") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_rebuilder_only_replaces_container_after_successful_build(tmp_path: Path, failed: bool) -> None:
    if os.name == "nt":
        pytest.skip("Docker sidecar uses a POSIX shell")
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\n[ "$DAEDALUS_SECRETS_FILE" = /dev/null ] || exit 99\nprintf "%s\\n" "$*" >> "$CALLS"\ncase "$*" in\n *" build "*) exit "$BUILD_RESULT";;\nesac\nexit 0\n')
    fake.chmod(0o755)
    job_id = "b" * 32
    sleeper = tmp_path / "sleep"
    sleeper.write_text('#!/bin/sh\nif [ "$1" = 30 ]; then cp "$PROGRESS" "$NOTICE"; /bin/sleep 0.1; else /bin/sleep "$@"; fi\n')
    sleeper.chmod(0o755)
    (trigger / "dependencies-request").write_text(job_id + "\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""), "DAEDALUS_REBUILD_TRIGGER_DIR": str(trigger), "COMPOSE_FILE": "compose.yaml", "CALLS": str(tmp_path / "calls"), "BUILD_RESULT": "1" if failed else "0"}
    env.update(PROGRESS=str(trigger / f"dependencies-{job_id}.progress"), NOTICE=str(tmp_path / "notice"))
    script = Path(__file__).resolve().parents[2] / "deploy" / "rebuild.sh"
    process = await asyncio.create_subprocess_exec("sh", str(script), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while not (trigger / f"dependencies-{job_id}.result").exists():
                await asyncio.sleep(0.02)
        result = (trigger / f"dependencies-{job_id}.result").read_text()
        calls = (tmp_path / "calls").read_text()
        assert ("up -d --no-build --no-deps daedalus" in calls) is not failed
        assert (result.strip() == "completed") is not failed
        assert (trigger / "dependencies-alive").exists()
        if not failed:
            notice = json.loads((tmp_path / "notice").read_text())
            assert notice["stage"] == "restart_pending"
            assert notice["restart_at"] - notice["at"] == 30
        else:
            assert not (tmp_path / "notice").exists()
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()


@pytest.mark.asyncio
async def test_rebuilder_heartbeat_continues_during_a_blocked_build(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Docker sidecar uses a POSIX shell")
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\ntouch "$STARTED"\nwhile :; do /bin/sleep 1; done\n')
    fake.chmod(0o755)
    sleeper = tmp_path / "sleep"
    sleeper.write_text('#!/bin/sh\n/bin/sleep 0.05\n')
    sleeper.chmod(0o755)
    (trigger / "dependencies-request").write_text("c" * 32)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""), "DAEDALUS_REBUILD_TRIGGER_DIR": str(trigger), "COMPOSE_FILE": "compose.yaml", "STARTED": str(tmp_path / "started")}
    script = Path(__file__).resolve().parents[2] / "deploy" / "rebuild.sh"
    process = await asyncio.create_subprocess_exec("sh", str(script), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.02)
            heartbeat = trigger / "dependencies-alive"
            before = heartbeat.stat().st_mtime_ns
            while heartbeat.stat().st_mtime_ns == before:
                await asyncio.sleep(0.02)
        assert not list(trigger.glob("*.result"))
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        await process.wait()


@pytest.mark.asyncio
async def test_native_supervisor_announces_before_stopping_the_app(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_supervisor_local import _load

    sup = _load()
    supervisor = sup.Supervisor.__new__(sup.Supervisor)
    supervisor.dependencies = service
    service.task = asyncio.current_task()
    supervisor.lock = asyncio.Lock()
    supervisor.restart_requested = asyncio.Event()
    job = service.accept(service.preview({"python": ["Pillow"], "system": []}), "e" * 32)
    events = []

    async def install(current: dict[str, Any]) -> None:
        current["state"] = "restarting"
        service.advance(current, "restart_pending", restart_at=sup.time.time() + 30)

    async def wait(seconds: float) -> None:
        assert 29 <= seconds <= 30
        assert service.status()["job"]["stage"] == "restart_pending"
        assert not supervisor.restart_requested.is_set()
        events.append("warned")

    async def stop() -> None:
        assert events == ["warned", "drained"]
        assert service.status()["job"]["stage"] == "restarting"
        events.append("stopped")

    async def drain(bot_sha: str, core_sha: str, transcript: str) -> dict[str, Any]:
        assert bot_sha and core_sha and transcript == "checked dependency installation"
        assert events == ["warned"]
        events.append("drained")
        return {"state": "committed", "commit_receipt_id": "receipt"}

    monkeypatch.setattr(service, "install", install)
    monkeypatch.setattr(sup.asyncio, "sleep", wait)
    supervisor._drain_before_stop = drain
    supervisor.stop_child = stop
    await supervisor._install_dependencies(job)
    assert events == ["warned", "drained", "stopped"]
    assert supervisor.restart_requested.is_set()
    assert service.status()["job"]["state"] == "completed"


def test_native_restart_during_warning_recovers_without_another_install(service: Any) -> None:
    desired = {"python": ["Pillow"], "system": []}
    job = service.accept(service.preview(desired), "f" * 32)
    runtime.write_json(service.manifest, desired)
    runtime.write_json(service.root / "active.json", {"generation": runtime.fingerprint(desired)})
    job["state"] = "restarting"
    service.advance(job, "restart_pending", restart_at=runtime.time.time() + 30)
    restored = runtime.Dependencies(service.repo, service.root.parent, service.trigger, native=True)
    current = restored.status()["job"]
    assert current["state"] == "completed"
    assert current["restart_at"] == 0
    assert not (service.root / "maintenance").exists()


def test_dependency_routes_require_auth_and_approve_only_stored_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.extensions.api import build_app

    app = FakeApp(tmp_path, native=True)
    app.db = MemoryDB()
    app.manager.dependency_installation_busy = lambda: False
    proposal_id = "c" * 32
    stored = {"base": "original", "recipe": {"python": ["Pillow"], "system": []}}
    app.db.values[KEY] = {"id": proposal_id, "state": "ready", "proposal": stored}
    calls = []

    async def rpc(self: Any, op: str, **kwargs: Any) -> Any:
        calls.append((op, kwargs))
        return {"id": proposal_id, "state": "installing"}

    monkeypatch.setattr(DependencyPlanner, "rpc", rpc)
    with TestClient(build_app(app, "tok")) as client:
        response = client.post(f"/api/dependencies/{proposal_id}/approve")
        assert response.status_code == 401
        assert not calls
        response = client.post(f"/api/dependencies/{proposal_id}/approve", headers=HEAD, json={"proposal": {"recipe": {"python": ["unapproved"], "system": []}}})
        assert response.status_code == 200
        assert calls == [("dependencies_apply", {"proposal": stored, "id": proposal_id})]
        assert client.post(f"/api/dependencies/{proposal_id}/approve", headers=HEAD).status_code == 200
        assert calls == [("dependencies_apply", {"proposal": stored, "id": proposal_id})]
