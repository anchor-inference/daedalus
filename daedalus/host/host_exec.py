"""Commands of a native session that works in a host folder, run on the host by its terminal daemon.

In Docker the agent's container cannot see a host folder at all, so a session working in one drives
the host the way the benchmark harness drives a task container: every ``Exec`` and every file tool
goes through an :class:`~daedalus.host.filesystem.ExecBackend`, here the daemon's one-shot
``exec.run``. The daemon runs ``bash`` as the operator, in the folder, with the operator's own login
environment; it is the operator's machine, so nothing here adds a sandbox of its own.
"""

from __future__ import annotations

import asyncio
import secrets
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from daedalus.host.filesystem import ExecOutcome

if TYPE_CHECKING:
    from daedalus.terminals.bridge import HostBridge

HOST = "host"

DAEMON_MAX_TIMEOUT = 30 * 60.0
"""The longest one ``exec.run`` may be given (the daemon's own ``MaxExecTimeout``); a longer call is
refused outright, so a longer wait is cut to this."""

HAND_OVER_GRACE = 30.0
"""Seconds the daemon gives the waiting call beyond its own deadline: the script ends itself at the
deadline, and the daemon's timeout is only the backstop for a host that stalled."""

MARK = "@@daedalus-exec@@"
"""The line the hand-over script prints between its own words and the command's output."""

HAND_OVER = (
    "mkdir -p {logdir} && "
    "if command -v setsid >/dev/null 2>&1; then detach=setsid; else detach=; fi; "
    "$detach nohup bash -c 'bash -c \"$0\"; echo $? > \"$1.exit\"' {command} {log} > {log} 2>&1 < /dev/null & "
    "pid=$!; echo $pid > {log}.pid; n={ticks}; "
    "while [ $n -gt 0 ] && [ ! -s {log}.exit ] && kill -0 $pid 2>/dev/null; do sleep 0.1; n=$((n-1)); done; "
    "if [ -s {log}.exit ]; then state=\"exited $(cat {log}.exit)\"; elif kill -0 $pid 2>/dev/null; then state=running; else state=gone; fi; "
    "size=$(wc -c < {log} | tr -d ' '); printf '%s %s %s %s\\n' {mark} \"$state\" $pid $size; "
    "if [ $size -le {whole} ]; then cat {log}; else head -c {head} {log}; "
    "printf '\\n\\n[... %s of %s bytes omitted — all of it is in %s ...]\\n\\n' $((size - {head} - {tail})) $size {log}; tail -c {tail} {log}; fi; "
    "rm -f {log}.pid; if [ \"$state\" != running ]; then rm -f {log}.exit; if [ $size -le {whole} ]; then rm -f {log}; fi; fi; true"
)
"""Runs a command on the host as a detached job from the start and waits for it, up to ``ticks``
tenths of a second, in one call to the daemon. A command that ends in time is answered like any;
one still running is already a job, with the same wrapper and exit file a background job has, so
nothing has to be moved when its foreground time is up — the daemon's one-shot ``exec.run`` ends what
it started at its timeout, and a command it ran directly could not have been kept. The process id
goes to a file at once, so a call cancelled while it waits can still end the command it started. A
command that ended leaves nothing behind but a log too long to have been shown whole, which the
answer names."""

KILL_BY_PIDFILE = (
    "pid=$(cat {log}.pid 2>/dev/null) || exit 0; [ -n \"$pid\" ] || exit 0; "
    "kill -TERM -- -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null; "
    "for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 $pid 2>/dev/null || exit 0; sleep 0.5; done; "
    "kill -KILL -- -$pid 2>/dev/null || kill -KILL $pid 2>/dev/null; true"
)

STDIN_CHUNK = 256 * 1024
"""Bytes sent per call when a file is put on the host: under the daemon's 512 KiB cap on stdin
whatever encoding the transport adds."""

BRIDGE_DOWN = (
    "the host terminal daemon is not answering, so nothing can run on the host now; "
    "start it on the host (systemctl --user start daedalus-ptyd), or install it with bash deploy/setup.sh"
)
OLD_DAEMON = (
    "the host terminal daemon does not run bash for an agent yet; update it on the host "
    "(bash deploy/host-terminal.sh install, which ends the open host terminals)"
)


PRELUDE = (
    'for profile in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do\n'
    '  if [ -r "$profile" ]; then . "$profile" >/dev/null 2>&1 </dev/null; break; fi\n'
    "done\n"
    "exec 2>&1\n"
)
"""What runs before every command. The profile a login shell would read, because the operator's PATH
(nvm, cargo, a virtualenv's tools) lives there, but silenced: ``bash -l`` let a profile that greets
or warns print into the output of every command, and the file tools read a file as that output, so a
greeting became the first line of every file read. ``exec 2>&1`` then makes the command's output one
interleaved stream, as a local Exec returns it; the daemon hands back stdout and stderr apart, and the
order between them would be lost."""


class HostExecBackend:
    """One shell command on the host, through the terminal daemon, as a session's ``exec_backend``.

    ``root`` is the session's working directory on the host and the directory a command without its
    own runs in: the daemon refuses a call without an absolute one, and the file tools of
    :class:`~daedalus.host.filesystem.ShellFS` name none.
    """

    machine = HOST
    foreign_paths = True
    """The paths its tools name are the host's, which this process (in its container) cannot open."""

    def __init__(self, bridge: Callable[[], HostBridge | None], root: str) -> None:
        # The bridge is asked on each call: the terminals service may be installed after the session
        # is loaded, and the daemon comes and goes while this process runs.
        self._bridge = bridge
        self.root = root

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome:
        bridge = self._bridge()
        if bridge is None:
            return ExecOutcome(exit_code=255, output=BRIDGE_DOWN)
        limit = min(max(float(timeout), 1.0), DAEMON_MAX_TIMEOUT)
        argv = ["bash", "-c", PRELUDE + command]
        try:
            result = await bridge.exec_run(HOST, argv, cwd=cwd or self.root, env_vars=env, timeout=limit)
        except ConnectionError:
            return ExecOutcome(exit_code=255, output=BRIDGE_DOWN)
        except OSError as exc:
            text = str(exc)
            if "not among the programs" in text:
                return ExecOutcome(exit_code=126, output=OLD_DAEMON)
            return ExecOutcome(exit_code=255, output=f"the host refused the command: {text}")
        output = result.stdout + (("\n" + result.stderr) if result.stderr.strip() else "")
        if result.truncated:
            output += "\n[... the host daemon cut the output; write it to a file and read that instead ...]"
        code = result.exit_code
        if code < 0 and result.signal:
            output += f"\n[ended by {result.signal}]"
        return ExecOutcome(exit_code=code, output=output, timed_out=bool(result.timed_out))

    def can_hand_over(self) -> bool:
        return True

    async def run_or_hand_over(self, command: str, *, cwd: str | None, env: dict[str, str] | None, after: float, job_id: str, log: str,
                               output_chars: int = 60_000) -> ExecOutcome:
        """Run ``command`` on the host for up to ``after`` seconds and keep it as a job if it is still
        running then (see :data:`HAND_OVER`); ``log`` is the job's log on the host. A call cancelled
        while it waits (the operator's stop) ends the command, as a cancelled foreground command ends."""
        quoted = shlex.quote(log)
        whole = max(1024, output_chars)
        script = HAND_OVER.format(
            logdir=shlex.quote(log.rpartition("/")[0] or "/"), command=shlex.quote(command), log=quoted,
            ticks=max(1, int(round(after * 10))), mark=MARK, whole=whole, head=int(whole * 0.7), tail=int(whole * 0.25),
        )
        try:
            outcome = await self.run(script, cwd=cwd, env=env, timeout=min(after + HAND_OVER_GRACE, DAEMON_MAX_TIMEOUT))
        except asyncio.CancelledError:
            await asyncio.shield(self._end_started(log))
            raise
        before, marked, rest = outcome.output.partition(MARK)
        if not marked:
            return outcome
        line, _, body = rest.lstrip(" ").partition("\n")
        words = line.split()
        output = (before.strip() + "\n" if before.strip() else "") + body
        if words[:1] == ["running"] and len(words) > 1 and words[1].isdigit():
            return ExecOutcome(exit_code=-1, output=output, handed_over=True, pid=int(words[1]))
        if words[:1] == ["exited"] and len(words) > 1 and words[1].lstrip("-").isdigit():
            return ExecOutcome(exit_code=int(words[1]), output=output)
        # The wrapper died without writing the code: ended from outside, together with its group.
        return ExecOutcome(exit_code=-1, output=output + "\n[the command vanished without an exit code: ended from outside]")

    async def _end_started(self, log: str) -> None:
        try:
            await self.run(KILL_BY_PIDFILE.format(log=shlex.quote(log)), cwd=None, env=None, timeout=30.0)
        except Exception:  # noqa: BLE001 — the stop goes on; a command the host could not be reached to end is the host's
            pass

    def prompt_note(self, workdir: object) -> str:
        """What the system prompt says of where this session's tools run."""
        return (
            f"- You work on the operator's own machine (the host), in {workdir}: Exec, Verify, "
            "background jobs and the file tools run there through the host terminal, with no sandbox around them, "
            "so take the care you would take on someone's own computer. ServiceStart is not available there; "
            "start a server with Exec(background=true)."
        )

    async def put_bytes(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` on the host byte for byte: what the operator attached to a message.

        Through stdin rather than the shell's arguments, in pieces under the daemon's cap, into a
        temporary file beside the target that is renamed over it once the last piece is in: written
        straight into the target, a daemon that went away part way left a cut file under the real
        name. An ``OSError`` when the host will not take it, with the temporary file removed.
        """
        bridge = self._bridge()
        if bridge is None:
            raise ConnectionError(BRIDGE_DOWN)
        folder, _, name = path.rpartition("/")
        target = shlex.quote(path)
        temporary = shlex.quote(f"{folder}/.{name}.{secrets.token_hex(4)}.tmp")
        pieces = [data[i : i + STDIN_CHUNK] for i in range(0, len(data), STDIN_CHUNK)] or [b""]
        try:
            for index, piece in enumerate(pieces):
                lead = f"mkdir -p -- {shlex.quote(folder or '/')} && " if index == 0 else ""
                redirect = ">" if index == 0 else ">>"
                result = await bridge.exec_run(HOST, ["bash", "-c", f"{lead}cat {redirect} {temporary}"], cwd=self.root, timeout=120.0, stdin=piece)
                if result.exit_code != 0:
                    raise OSError((result.stderr or result.stdout).strip() or f"could not write {path} on the host")
            result = await bridge.exec_run(HOST, ["bash", "-c", f"mv -f -- {temporary} {target}"], cwd=self.root, timeout=120.0)
            if result.exit_code != 0:
                raise OSError((result.stderr or result.stdout).strip() or f"could not write {path} on the host")
        except BaseException:
            try:
                await bridge.exec_run(HOST, ["bash", "-c", f"rm -f -- {temporary}"], cwd=self.root, timeout=30.0)
            except Exception:  # noqa: BLE001 — the write already failed; that error is the one to report
                pass
            raise


__all__ = ["BRIDGE_DOWN", "DAEMON_MAX_TIMEOUT", "HAND_OVER", "OLD_DAEMON", "PRELUDE", "HostExecBackend"]
