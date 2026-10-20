"""The computer tools against the real desktop daemon serving its fake desktop: opt in with ``DESKD_BIN``.

``DESKD_BIN`` names a built ``deskd`` (``deskd/toolchain.Dockerfile``, ``CGO_ENABLED=0``). The daemon
runs ``serve --backend fake`` in a temporary directory: no display, no input reaching the machine,
everything recorded. The test plays the launcher with the operator's token to grant the lease, then
drives the host's agent as a session would, and reads back from ``fake.simulate`` where each event
landed. Skipped when the variable is unset, so the ordinary suite never needs the binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.computer import refusals
from daedalus.computer.agent import Caller, ComputerAgent
from daedalus.computer.wire import DeskClient
from daedalus.model_capabilities import CLAUDE_PROFILE, RELATIVE_PROFILE
from daedalus.terminals.client import PtydClient

DESKD_BIN = os.environ.get("DESKD_BIN", "")
SESSION = "session-real"

pytestmark = pytest.mark.skipif(not DESKD_BIN or not Path(DESKD_BIN).is_file(), reason="DESKD_BIN names no deskd binary")


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[tuple[Path, PtydClient]]:
    run_dir, state_dir = tmp_path / "run", tmp_path / "state"
    run_dir.mkdir()
    state_dir.mkdir()
    process = subprocess.Popen([DESKD_BIN, "serve", "--backend", "fake", "--run-dir", str(run_dir), "--state-dir", str(state_dir), "--audit-file", "off"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if (run_dir / "endpoint").exists() and (state_dir / "operator.token").exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("deskd did not start: " + (process.stderr.read().decode() if process.poll() is not None and process.stderr else "no endpoint"))
        # The launcher's side: the operator's token presented on the same socket. A directory of its own
        # holding that token and the socket's absolute path lets the terminals' client do the handshake.
        operator_dir = tmp_path / "operator"
        operator_dir.mkdir()
        (operator_dir / "endpoint").write_text(f"unix:{run_dir / 'deskd.sock'}")
        (operator_dir / "token").write_text((state_dir / "operator.token").read_text())
        operator = PtydClient("host", operator_dir, label="desktop service", lock="deskd.lock")
        hello = await operator.connect()
        assert hello["role"] == "operator"
        yield run_dir, operator
        await operator.close()
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(5)
        if process.poll() is None:
            process.kill()


async def _inputs(operator: PtydClient) -> list[dict[str, Any]]:
    answer = await operator.call("fake.simulate", {})
    return list(answer.get("inputs") or [])


async def test_a_session_with_the_lease_sees_clicks_and_types_on_the_real_daemon(daemon: tuple[Path, PtydClient]) -> None:
    run_dir, operator = daemon
    client = DeskClient(run_dir)
    agent = ComputerAgent(client, settle=0)
    me = Caller(session_id=SESSION, profile=RELATIVE_PROFILE)
    try:
        refused = await agent.computer(me, {"action": "screenshot"})
        assert refused.failed and "Only the operator can grant control" in refused.text

        await operator.call("lease.grant", {"session": SESSION, "scope": {"kind": "desktop"}, "ttl_ms": 60_000})
        shot = await agent.computer(me, {"action": "screenshot"})
        assert not shot.failed, shot.text
        assert shot.image is not None and shot.image.width > 0 and shot.image.data[:2] == b"\xff\xd8"
        await _inputs(operator)  # drain anything recorded so far

        # The editor of the default scene is at (100, 100, 800, 600) on the 1440×900 primary display:
        # (500/1440, 400/900) of it is well inside, in relative points (347, 444).
        clicked = await agent.computer(me, {"action": "left_click", "coordinate": [347, 444]})
        assert not clicked.failed, clicked.text
        assert "the action landed on window 10" in clicked.text
        typed = await agent.computer(me, {"action": "type", "text": "hello"})
        assert not typed.failed, typed.text
        assert "hello" not in typed.text
        events = await _inputs(operator)
        buttons = [e for e in events if e["kind"] == "button"]
        assert buttons and all(e.get("window") == "10" for e in buttons)
        press = next(e for e in buttons if e.get("down"))
        assert abs(press["x"] - 500) <= 2 and abs(press["y"] - 400) <= 2
        assert any(e["kind"] == "type" and e.get("window") == "10" for e in events)

        listed = await agent.apps(me, {"action": "list"})
        assert not listed.failed and "[screen content from Editor;" in listed.text
    finally:
        await client.close()


async def test_a_point_outside_the_granted_window_is_explained(daemon: tuple[Path, PtydClient]) -> None:
    run_dir, operator = daemon
    client = DeskClient(run_dir)
    agent = ComputerAgent(client, settle=0)
    me = Caller(session_id=SESSION, profile=CLAUDE_PROFILE)
    try:
        await operator.call("lease.grant", {"session": SESSION, "scope": {"kind": "window", "window": "10"}, "ttl_ms": 60_000})
        shot = await agent.computer(me, {"action": "screenshot"})
        assert not shot.failed, shot.text
        assert shot.image is not None and shot.image.window == "10"
        # A screenshot of the window itself: its far corner is still inside, a point past it is off the picture.
        outside = await agent.computer(me, {"action": "left_click", "coordinate": [shot.image.shown_width + 50, 10]})
        assert outside.failed and "not on the screenshot" in outside.text
        await operator.call("lease.revoke", {"session": SESSION})
        gone = await agent.computer(me, {"action": "left_click", "coordinate": [10, 10]})
        assert gone.failed and gone.text == refusals.explain(1200, "", {"reason": "revoked"})
    finally:
        await client.close()
