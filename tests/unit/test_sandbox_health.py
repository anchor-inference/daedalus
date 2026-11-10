"""Exec when the sandbox itself breaks: what the agent reads, what the operator hears, and what the host repairs.

The defect these guard: someone outside the agent's container mounted a proc of the host's PID namespace over
its ``/proc``. From then on bubblewrap died on every command with ``bwrap: open /proc/<pid>/ns/ns failed``
before running anything, its blocked child held the output, and the agent read ``exit_code=1``, the raw line
and a note about "a background process it started", which it had not. The probe's success was kept for the
life of the process, so the host went on calling the sandbox fine, and nobody was told. A model then spent
dozens of steps converting a video in a browser because "the shell is broken".
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import ToolResult

from daedalus.config import Settings
from daedalus.extensions import sandbox_notice
from daedalus.extensions.notifications import ActionRefused, ActionRequest
from daedalus.processes import pid_alive
from daedalus.stores.database import Database
from daedalus.tools import package_cache, sandbox_health, shell
from tests.support.waiting import until
from tests.unit.test_job_wake import context
from tests.unit.test_session_runner import ScriptedProvider, _manager

REFUSED = "bwrap: open /proc/45438/ns/ns failed: No such file or directory"


@pytest.fixture(autouse=True)
def fresh_health(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every test starts with no outage, no listener but a recorder, and the sandbox not switched off."""
    told: list[tuple[str, str]] = []

    async def record(event: str, outage: sandbox_health.Outage) -> None:
        told.append((event, outage.reason))

    monkeypatch.setattr(sandbox_health, "_outage", None)
    monkeypatch.setattr(sandbox_health, "suspended", False)
    monkeypatch.setattr(sandbox_health, "_listeners", {"test": record})
    monkeypatch.setattr(sandbox_health, "diagnostics", lambda bwrap, status: [("probe", status)])
    return told


def refused(content: str = REFUSED) -> ToolResult:
    return ToolResult(tool_call_id="c1", content=f"exit_code=1 elapsed=1.6s cwd=/srv/workspaces/x sandbox=workspace\n{content}", is_error=True, metadata={"exit_code": 1})


def test_a_refusal_is_told_apart_from_a_command_that_failed() -> None:
    assert shell.bwrap_refusal("which ffmpeg", refused()) == REFUSED
    # The command's own failure, unsandboxed, or a command that runs bubblewrap itself: none of them is the sandbox's.
    assert shell.bwrap_refusal("make", refused("make: *** No rule to make target")) is None
    assert shell.bwrap_refusal("bwrap --ro-bind / / true", refused()) is None
    plain = ToolResult(tool_call_id="c1", content=f"exit_code=1 elapsed=0.1s cwd=/w\n{REFUSED}", is_error=True)
    assert shell.bwrap_refusal("ls", plain) is None
    assert shell.bwrap_refusal("ls", refused(REFUSED + "\n" + "\n".join(["more"] * 5))) is None


async def test_a_refused_command_is_run_again_once_the_probe_passes(monkeypatch: pytest.MonkeyPatch, fresh_health: list[tuple[str, str]]) -> None:
    answers = [refused(), ToolResult(tool_call_id="c1", content="exit_code=0 elapsed=0.1s cwd=/w sandbox=workspace\nfine", metadata={"exit_code": 0})]
    probes: list[int] = []

    async def run_once(*args: Any, **kwargs: Any) -> ToolResult:
        return answers.pop(0)

    def status() -> str:
        probes.append(1)
        return "ok"

    monkeypatch.setattr(shell, "_run_once", run_once)
    monkeypatch.setattr(shell, "bwrap_status", status)
    result = await shell._run(context("s1"), "which ffmpeg", None, None, None)
    assert not result.is_error and "fine" in result.content
    assert probes and not answers and fresh_health == [], "the second attempt passed, so nobody was told of anything"


async def test_a_sandbox_that_stays_down_is_one_notice_and_a_plain_answer(monkeypatch: pytest.MonkeyPatch, fresh_health: list[tuple[str, str]]) -> None:
    async def run_once(*args: Any, **kwargs: Any) -> ToolResult:
        return refused()

    monkeypatch.setattr(shell, "_run_once", run_once)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "the /proc this process sees belongs to another PID namespace")
    first = await shell._run(context("s1"), "which ffmpeg", None, None, None)
    second = await shell._run(context("s2"), "ls", None, None, None)
    for result in (first, second):
        assert result.is_error and result.metadata.get("sandbox_unavailable") is True
        assert result.content.startswith("sandbox unavailable: the /proc this process sees belongs to another PID namespace")
        assert "The command did not run" in result.content and "do not invent a workaround" in result.content
        assert "exit_code=" not in result.content and "SIGPIPE" not in result.content and "background process" not in result.content
    assert fresh_health == [("down", "the /proc this process sees belongs to another PID namespace")], "two failed commands, one notice"


async def test_after_two_minutes_the_agent_is_told_to_stop_and_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    outage = await sandbox_health.report_down("no namespaces")
    assert "Exec is checked again on the next call" in sandbox_health.agent_text(outage)
    outage.since -= sandbox_health.ASK_OPERATOR_AFTER_SECONDS + 1
    assert "Stop here: end the turn and tell the operator" in sandbox_health.agent_text(outage)


async def test_a_probe_that_passes_while_commands_fail_does_not_keep_them_failing_twice(monkeypatch: pytest.MonkeyPatch, fresh_health: list[tuple[str, str]]) -> None:
    calls: list[int] = []

    async def run_once(*args: Any, **kwargs: Any) -> ToolResult:
        calls.append(1)
        return refused()

    monkeypatch.setattr(shell, "_run_once", run_once)
    monkeypatch.setattr(shell, "_probe", lambda argv, timeout: (0, ""))
    monkeypatch.setattr(sandbox_health, "proc_view_problem", lambda: "")
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(shell, "_bwrap_state", None)
    result = await shell._run(context("s1"), "ls", None, None, None)
    assert len(calls) == 2 and result.metadata.get("sandbox_unavailable") is True
    assert "although its probe passes" in fresh_health[0][1]
    assert shell.bwrap_status() != "ok", "the next command is refused at once, not after two more attempts"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux only")
def test_success_is_not_kept_for_ever_and_a_foreign_proc_is_never_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [(0, ""), (1, "bwrap: setting up uid map: Permission denied")]
    monkeypatch.setattr(shell, "_probe", lambda argv, timeout: answers.pop(0))
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox_health, "proc_view_problem", lambda: "")
    monkeypatch.setattr(shell, "_bwrap_state", None)
    assert shell.bwrap_status() == "ok"
    monkeypatch.setattr(shell, "_bwrap_probed_at", shell._bwrap_probed_at - shell.PROBE_SECONDS - 1)
    assert shell.bwrap_status().startswith("bwrap cannot create namespaces here: bwrap: setting up uid map")

    shell.invalidate_bwrap()
    repairs: list[int] = []
    monkeypatch.setattr(sandbox_health, "proc_view_problem", lambda: "the /proc this process sees belongs to another PID namespace")
    monkeypatch.setattr(sandbox_health, "repair_proc_view", lambda: repairs.append(1) or "")
    assert shell.bwrap_status().startswith("the /proc this process sees belongs to another PID namespace")
    assert repairs == [1], "a repair is tried before the answer is given"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_this_process_sees_its_own_proc_and_the_stacked_mounts_are_counted(tmp_path: Path) -> None:
    assert sandbox_health.proc_view_problem() == ""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "2815 2808 0:1214 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n"
        "29 2815 0:1214 /bus /proc/bus ro,nosuid - proc proc rw\n"
        "3605 2815 0:1499 / /proc rw,relatime - proc proc rw\n"
        "2816 2808 0:1215 / /dev rw,nosuid - tmpfs tmpfs rw\n"
    )
    assert sandbox_health.proc_mounts(str(mountinfo)) == 2


async def test_a_leftover_in_the_sandbox_is_ended_and_not_blamed_on_the_agent(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandboxed command's leftover holding the output is bubblewrap's, not something the command started."""
    manager = await _manager(settings, db, ScriptedProvider([]))
    try:
        sid = (await manager.create_session("left")).session.id
        marker = settings.state_dir / "leftover.pid"

        async def walled(command: str, *args: Any, **kwargs: Any) -> tuple[list[str], bool]:
            return ["bash", "-c", f"sleep 30 & echo $! > {marker}; echo started"], True

        monkeypatch.setattr(shell, "sandbox_argv", walled)
        result = await shell.exec_command().invoke(context(sid), {"command": "true"})
        assert "started" in result.content and "background process it started" not in result.content
        leftover = int(marker.read_text())
        await until(lambda: not pid_alive(leftover), "the leftover was ended")
    finally:
        await manager.close()


def test_the_shared_caches_link_the_installed_browsers_and_stay_out_of_a_guests_reach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = tmp_path / "pw-browsers"
    (installed / "chromium-1187" / "chrome-linux").mkdir(parents=True)
    (installed / ".links").mkdir()
    (installed / ".links" / "abc").write_text("/srv/venv/lib/python3.12/site-packages/playwright/driver/package")
    base = tmp_path / "cache"
    monkeypatch.setattr(package_cache, "_prepared", None)
    assert package_cache.prepare(base, installed=str(installed)) == base
    browsers = base / "ms-playwright"
    assert (browsers / "chromium-1187").is_symlink() and (browsers / "chromium-1187").resolve() == (installed / "chromium-1187").resolve()
    assert (browsers / ".links").is_dir() and not (browsers / ".links").is_symlink() and (browsers / ".links" / "abc").is_file()
    assert (base / "uv").is_dir() and (base / "npm").is_dir()

    monkeypatch.setenv("DAEDALUS_PACKAGE_CACHE", str(base))
    env = shell.shell_environment("s1")
    assert env["UV_CACHE_DIR"] == str(base / "uv") and env["npm_config_cache"] == str(base / "npm") and env["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    monkeypatch.setattr(shell, "is_guest_turn", lambda session_id: True)
    guest = shell.shell_environment("s1")
    assert not {"UV_CACHE_DIR", "npm_config_cache", "PLAYWRIGHT_BROWSERS_PATH"} & set(guest), "a guest writes no cache the operator reads"


async def test_a_guests_sandbox_binds_no_shared_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "is_guest_turn", lambda session_id: True)
    monkeypatch.setattr(shell, "guest_sandbox_status", lambda: "ok")
    monkeypatch.setattr(shell, "guest_limits_status", lambda: "ok")
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    ws = tmp_path / "ws"
    ws.mkdir()
    argv, sandboxed = await shell.sandbox_argv("uv add requests", SimpleNamespace(sandbox="workspace"), writable=[ws], session_id="guest")
    caches = {str(path) for path in package_cache.writable_dirs()}
    assert sandboxed and not caches & set(argv)


async def test_the_notice_offers_a_check_and_switching_off(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[Any] = []
    resolved: list[tuple[str, str]] = []

    class Notifications:
        def language(self) -> str:
            return "en"

        async def post(self, draft: Any) -> None:
            posted.append(draft)

        async def resolve(self, ref: str, resolution: str, *, via: str) -> int:
            resolved.append((ref, resolution))
            return 1

    app = SimpleNamespace(notifications=Notifications())
    tell = sandbox_notice.listener(app)  # type: ignore[arg-type]
    outage = sandbox_health.Outage(reason="no namespaces", since=0.0, number=3)
    await tell("down", outage)
    assert posted[0].request_ref == "sandbox:host:3" and posted[0].tone == "error" and "no namespaces" in posted[0].body
    assert [action.id for action in posted[0].actions] == ["recheck", "off"]

    answer = sandbox_notice.resolver(app)  # type: ignore[arg-type]
    request = ActionRequest("sandbox:host:3", "sandbox", "host", "3", "recheck", None, "notification", {})  # type: ignore[arg-type]
    monkeypatch.setattr(sandbox_notice, "bwrap_status", lambda: "still no namespaces")
    with pytest.raises(ActionRefused, match="still no namespaces"):
        await answer(request)
    monkeypatch.setattr(sandbox_notice, "bwrap_status", lambda: "ok")
    assert (await answer(request)).resolution == "Exec works again"

    switched = ActionRequest("sandbox:host:3", "sandbox", "host", "3", "off", None, "notification", {})  # type: ignore[arg-type]
    await answer(switched)
    assert sandbox_health.suspended
    argv, sandboxed = await shell.sandbox_argv("ls", SimpleNamespace(sandbox="workspace"), writable=[])
    assert not sandboxed and argv[-3:] == ["bash", "-lc", "ls"]
