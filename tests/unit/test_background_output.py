"""A command that leaves a background child behind returns when its shell does, not at the timeout."""

from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path
from types import SimpleNamespace

from protocore.contracts.tools import ToolContext

from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.services import SessionServices, locator
from daedalus.security.redact import Redactor
from daedalus.tools.shell import exec_command


def _end(output: str) -> None:
    """End the background child the command printed the pid of, so the test leaves nothing running."""
    for pid in re.findall(r"child=(\d+)", output):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_exec_returns_soon_after_the_shell_exits_and_says_what_holds_the_output(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="bg", workspace_dir=tmp_path, tool_timeout_seconds=12))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="bg", metadata={"tool_call_id": "call-bg"})
    started = time.monotonic()
    try:
        result = await exec_command().invoke(ctx, {"command": "echo before; sleep 30 & echo child=$!"})
        elapsed = time.monotonic() - started
        _end(result.content)
        assert elapsed < 10, result.content
        assert not result.is_error, result.content
        assert "before" in result.content and "child=" in result.content
        assert "still holds" in result.content and "background=true" in result.content
    finally:
        locator.unregister("bg")


async def test_output_a_background_child_writes_just_after_the_exit_is_still_read(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="late", workspace_dir=tmp_path, tool_timeout_seconds=12))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="late", metadata={"tool_call_id": "call-late"})
    try:
        result = await exec_command().invoke(ctx, {"command": "(sleep 0.3; echo late) & echo now"})
        assert "now" in result.content and "late" in result.content
        assert "still holds" not in result.content
    finally:
        locator.unregister("late")


async def test_a_hook_that_leaves_a_child_holding_its_output_answers_without_timing_out() -> None:
    hooks = DaedalusHookManager(Redactor(), hooks_config=lambda: SimpleNamespace(timeout_seconds=12.0))
    started = time.monotonic()
    answer = await hooks._run_script("echo '{\"ok\": true}'; sleep 30 & echo child=$!", {"session_id": "s"}, 12.0)
    elapsed = time.monotonic() - started
    assert answer is not None
    _end(answer[1])
    assert elapsed < 10
    code, out = answer
    assert code == 0 and '{"ok": true}' in out
