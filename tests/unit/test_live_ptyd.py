"""The terminal daemon's test double with real processes: the protocol an adapter relies on, proven
against programs in real pseudo-terminals and a real hook listener, all on this machine."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from daedalus.harness.env import staff_inherited_env
from daedalus.terminals import wire
from tests.support.harness_ports import Rig

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RAW_READER = """
import os, sys, tty
tty.setraw(0)
sys.stdout.write({prefix!r} + "READY\\r\\n"); sys.stdout.flush()
data = b""
while not data.endswith({end!r}):
    data += os.read(0, 4096)
open({out!r}, "wb").write(data)
"""


def post(url: str, body: bytes, token: str | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def test_a_program_runs_draws_and_exits_with_its_code() -> None:
    async with Rig() as rig:
        term = await rig.spawn(["/bin/sh", "-c", "printf 'hello\\033[1mbold\\033[0m\\n'; exit 4"])
        exited = await rig.event("terminal.exited")
        assert exited["data"]["exit_code"] == 4
        assert exited["terminal_id"] == term.id
        screen = await rig.client.call("terminal.read_screen", {"id": term.id})
        assert screen["lines"][0] == "hellobold"
        output = await rig.client.call("terminal.read_output", {"id": term.id, "since_seq": 0, "strip": True})
        assert output["data"].startswith("hellobold\n") and output["to_seq"] == output["head_seq"]
        raw = await rig.client.call("terminal.read_output", {"id": term.id, "since_seq": 0})
        assert b"\x1b[1m" in base64.b64decode(raw["data_b64"])
        info = await rig.client.call("terminal.get", {"id": term.id})
        assert info["status"] == "exited" and info["exit_code"] == 4


@pytest.mark.parametrize("bracketed", [True, False])
async def test_paste_is_bracketed_only_when_the_program_asked(bracketed: bool) -> None:
    async with Rig() as rig:
        out = rig.root / "got"
        end = b"\x1b[201~" if bracketed else b"b"
        script = RAW_READER.format(prefix="\x1b[?2004h" if bracketed else "", end=end, out=str(out))
        term = await rig.spawn([sys.executable, "-c", script])
        await rig.screen_until(term, "READY")
        assert (await term.modes())["bracketed_paste"] is bracketed
        await term.write(paste="a\nb")
        await rig.event("terminal.exited")
        expected = b"\x1b[200~a\nb\x1b[201~" if bracketed else b"a\rb"
        assert out.read_bytes() == expected
        written = rig.ptyd.terminals[term.id].writes[-1]
        assert written["kind"] == "paste" and written["origin"]["actor"] == "adapter"


async def test_named_keys_follow_the_cursor_key_mode() -> None:
    async with Rig() as rig:
        out = rig.root / "keys"
        script = RAW_READER.format(prefix="\x1b[?1h", end=b"\r", out=str(out))
        term = await rig.spawn([sys.executable, "-c", script])
        await rig.screen_until(term, "READY")
        await term.write(keys=["Up", "Esc", "C-c", "Enter"])
        await rig.event("terminal.exited")
        assert out.read_bytes() == b"\x1bOA\x1b\x03\r"


async def test_an_agent_write_waits_for_the_human() -> None:
    async with Rig(input_idle_ms=400) as rig:
        term = await rig.spawn(["/bin/cat"])
        await rig.ptyd.type_as_human(term.id, "typed by a person")
        with pytest.raises(wire.RpcError) as refused:
            await rig.client.call("terminal.write", {"id": term.id, "text": "x", "origin": {"kind": "agent", "actor": "t"}, "timeout_ms": 100})
        assert refused.value.code == 1006
        # Once the person has been quiet for the window, the write goes through without a grant.
        receipt = await rig.client.call("terminal.write", {"id": term.id, "text": "later", "origin": {"kind": "agent", "actor": "t"}, "timeout_ms": 5000})
        assert receipt["queued_ms"] > 0
        # A human grant holds agents off; an agent grant lets them straight through.
        await rig.client.call("terminal.keyboard", {"id": term.id, "owner": "human", "ttl_ms": 60_000})
        with pytest.raises(wire.RpcError):
            await rig.client.call("terminal.write", {"id": term.id, "text": "x", "origin": {"kind": "agent", "actor": "t"}, "timeout_ms": 50})
        await rig.client.call("terminal.keyboard", {"id": term.id, "owner": "agent", "ttl_ms": 60_000})
        await rig.ptyd.type_as_human(term.id, "the person types again")
        info = await rig.client.call("terminal.get", {"id": term.id})
        assert info["keyboard"]["owner"] == "auto"  # human typing ended the agent's grant


async def test_wait_for_a_pattern_quiet_and_exit() -> None:
    async with Rig() as rig:
        term = await rig.spawn(["/bin/sh", "-c", "sleep 0.2; echo ready-now; sleep 30"])
        assert await term.wait_for(regex=r"ready-\w+", timeout=10)
        assert await term.wait_for(idle_ms=100, timeout=10)
        result = await rig.client.call("terminal.wait_for", {"id": term.id, "regex": "never", "timeout_ms": 100})
        assert result["matched"] == "timeout"
        await rig.client.call("terminal.kill", {"id": term.id, "grace_ms": 500})
        exited = await rig.event("terminal.exited")
        assert exited["data"]["signal"] == "SIGHUP"
        result = await rig.client.call("terminal.wait_for", {"id": term.id, "regex": "never", "timeout_ms": 5000})
        assert result["matched"] == "exited"


async def test_the_program_gets_the_contracts_environment() -> None:
    async with Rig(extra_env={"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/opt/claude-config", "TMUX": "/tmp/tmux", "KEEP_ME": "yes"}) as rig:
        launch = await rig.register()
        out = rig.root / "env.json"
        term = await rig.spawn([sys.executable, "-c", f"import json, os; json.dump(dict(os.environ), open({str(out)!r}, 'w'))"], env={"EXTRA": "1"}, launch_id=launch["launch_id"])
        await rig.event("terminal.exited")
        env = json.loads(out.read_text())
        assert "CLAUDECODE" not in env and "TMUX" not in env
        assert env["CLAUDE_CONFIG_DIR"] == "/opt/claude-config" and env["KEEP_ME"] == "yes" and env["EXTRA"] == "1"
        assert env["TERM"] == "xterm-256color" and env["DAEDALUS_TERMINAL_ID"] == term.id and env["HOME"] == str(rig.home)
        assert env["DAEDALUS_HOOK_URL"] == launch["hook_url"] and env["DAEDALUS_HOOK_TOKEN"] == launch["hook_token"]
        assert env["DAEDALUS_LAUNCH_DIR"] == launch["dir"] and env["DAEDALUS_DIAL_DIR"] == launch["dial_dir"]


async def test_staff_terminal_does_not_inherit_other_provider_environment() -> None:
    async with Rig(extra_env={"CLAUDE_CONFIG_DIR": "/config/claude", "OPENAI_API_KEY": "other-provider-token",
                              "GH_TOKEN": "repository-token"}) as rig:
        out = rig.root / "staff-env.json"
        await rig.client.call("terminal.create", {
            "id": "staff-env", "argv": [sys.executable, "-c", f"import json, os; json.dump(dict(os.environ), open({str(out)!r}, 'w'))"],
            "cwd": str(rig.root), "cols": 80, "rows": 24,
            "inherited_env": staff_inherited_env("claude"),
        })
        await rig.event("terminal.exited")
        env = json.loads(out.read_text())
        assert env["CLAUDE_CONFIG_DIR"] == "/config/claude"
        assert "OPENAI_API_KEY" not in env and "GH_TOKEN" not in env


async def test_exec_runs_listed_programs_only_and_kills_on_timeout() -> None:
    async with Rig() as rig:
        version = await rig.env_port.run(["claude", "--version"])
        assert version.exit_code == 0 and version.stdout.strip().endswith("(Claude Code)")
        with pytest.raises(wire.RpcError) as refused:
            await rig.env_port.run(["sh", "-c", "true"])
        assert refused.value.code == 1004
        rig.ptyd.exec_allow.add("sleep")
        slow = await rig.env_port.run(["sleep", "30"], timeout=0.3)
        assert slow.timed_out and slow.exit_code == -1


async def test_files_under_the_roots_and_never_on_the_deny_list() -> None:
    async with Rig() as rig:
        project = rig.work / "project"
        (project / ".claude").mkdir(parents=True)
        (project / "notes.txt").write_text("one\n")
        (project / ".claude" / ".credentials.json").write_text("{}")
        await rig.client.call("fs.set_roots", {"roots": [str(project)]})
        assert await rig.env_port.read(str(project / "notes.txt")) == b"one\n"
        assert await rig.env_port.list(str(project)) == [".claude", "notes.txt"]
        assert await rig.env_port.list(str(project / ".claude")) == []
        assert await rig.env_port.stat(str(project / "missing")) is None
        for path in (project / ".claude" / ".credentials.json", rig.home):
            with pytest.raises(wire.RpcError) as refused:
                await rig.env_port.read(str(path))
            assert refused.value.code == 1004
        first = await rig.client.call("fs.tail", {"path": str(project / "notes.txt"), "from_offset": 0})
        assert base64.b64decode(first["data_b64"]) == b"one\n"

        async def append_later() -> None:
            await asyncio.sleep(0.2)
            with (project / "notes.txt").open("a") as handle:
                handle.write("two\n")

        writer = asyncio.create_task(append_later())
        followed = await rig.client.call("fs.tail", {"path": str(project / "notes.txt"), "from_offset": first["next_offset"], "follow_ms": 10_000, "file_id": first["file_id"]}, timeout=20)
        await writer
        assert base64.b64decode(followed["data_b64"]) == b"two\n" and not followed["rotated"]
        # Replaced (a new inode, written before the old one goes so the number cannot be reused) and
        # longer than the old one: only the file id tells.
        (project / "notes.new").write_text("a new, longer file\n")
        os.replace(project / "notes.new", project / "notes.txt")
        rotated = await rig.client.call("fs.tail", {"path": str(project / "notes.txt"), "from_offset": followed["next_offset"], "file_id": followed["file_id"]})
        assert rotated["rotated"] and base64.b64decode(rotated["data_b64"]) == b"a new, longer file\n"


async def test_hooks_tokens_holds_and_replies() -> None:
    async with Rig() as rig:
        launch = await rig.register(files={"settings.json": b'{"hooks": {}}'}, hold_max_ms=5000)
        overlay = Path(launch["dir"]) / "settings.json"
        assert overlay.read_bytes() == b'{"hooks": {}}'
        assert stat.S_IMODE(overlay.stat().st_mode) == 0o600 and stat.S_IMODE(Path(launch["dir"]).stat().st_mode) == 0o700
        url = launch["hook_url"]
        assert (await asyncio.to_thread(post, f"{url}/Stop", b"{}", "wrong"))[0] == 401
        assert (await asyncio.to_thread(post, url.rsplit("/", 1)[0] + "/unknown/Stop", b"{}", launch["hook_token"]))[0] == 410
        assert (await asyncio.to_thread(post, f"{url}/Stop", b'{"a": 1}', launch["hook_token"]))[0] == 204
        stop = await rig.event("hook", where={"name": "Stop"})
        assert stop["data"]["body"] == {"a": 1} and "reply_id" not in stop["data"]

        held = asyncio.create_task(asyncio.to_thread(post, f"{url}/PermissionRequest?wait_ms=5000", b'{"tool_name": "Bash"}', launch["hook_token"]))
        event = await rig.event("hook", where={"name": "PermissionRequest"})
        assert event["data"]["hold_ms"] == 5000
        await rig.client.call("hooks.reply", {"reply_id": event["data"]["reply_id"], "status": 200, "body": {"decision": "allow"}})
        status, body = await held
        assert status == 200 and json.loads(body) == {"decision": "allow"}
        with pytest.raises(wire.RpcError):  # answered once; nothing waits any more
            await rig.client.call("hooks.reply", {"reply_id": event["data"]["reply_id"], "status": 200, "body": {}})

        # The body can ask for the hold itself, and an unanswered hold ends with an empty success.
        started = asyncio.get_running_loop().time()
        status, body = await asyncio.to_thread(post, f"{url}/team", b'{"tool": "ask", "daedalus_hold_ms": 200}', launch["hook_token"])
        assert status == 204 and body == b"" and asyncio.get_running_loop().time() - started >= 0.2

        pending = asyncio.create_task(asyncio.to_thread(post, f"{url}/team?wait_ms=5000", b"{}", launch["hook_token"]))
        await rig.event("hook", where={"name": "team"}, after=len(rig.events) - 1)
        assert (await rig.client.call("hooks.unregister_launch", {"launch_id": launch["launch_id"]}))["removed"]
        assert (await pending)[0] == 410
        assert not Path(launch["dir"]).exists()
        await rig.event("launch.ended", where={"launch_id": launch["launch_id"]})
        assert (await asyncio.to_thread(post, f"{url}/Stop", b"{}", launch["hook_token"]))[0] == 410


async def test_a_launch_ends_after_its_terminal_exits() -> None:
    async with Rig(launch_grace_s=0.1) as rig:
        launch = await rig.register()
        await rig.spawn(["/bin/true"], launch_id=launch["launch_id"])
        ended = await rig.event("launch.ended")
        assert ended["data"] == {"launch_id": launch["launch_id"], "reason": "terminal_exited"}
        with pytest.raises(wire.RpcError) as stale:
            await rig.spawn(["/bin/true"], launch_id=launch["launch_id"])
        assert stale.value.code == 1008


async def test_dial_a_launchs_socket_and_only_its_registered_ports() -> None:
    async with Rig() as rig:
        launch = await rig.register()

        async def shout(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while line := await reader.readline():
                writer.write(line.upper())
                await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(shout, path=os.path.join(launch["dial_dir"], "app.sock"))
        async with server:
            dialled = await rig.client.call("net.dial", {"target": "unix:app.sock", "launch_id": launch["launch_id"]})
            channel = rig.client.channel(dialled["channel"])
            await channel.send(b"hello\n")
            assert await channel.recv() == b"HELLO\n"
            await channel.close()
        with pytest.raises(wire.RpcError) as refused:
            await rig.client.call("net.dial", {"target": "tcp:127.0.0.1:9", "launch_id": launch["launch_id"]})
        assert refused.value.code == 1004
        with pytest.raises(wire.RpcError):
            await rig.client.call("net.dial", {"target": "unix:../../token", "launch_id": launch["launch_id"]})
