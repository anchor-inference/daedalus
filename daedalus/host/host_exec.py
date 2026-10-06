"""Commands of a native session that works in a host folder, run on the host by its terminal daemon.

In Docker the agent's container cannot see a host folder at all, so a session working in one drives
the host the way the benchmark harness drives a task container: every ``Exec`` and every file tool
goes through an :class:`~daedalus.host.filesystem.ExecBackend`, here the daemon's one-shot
``exec.run``. The daemon runs ``bash`` as the operator, in the folder, with the operator's own login
environment; it is the operator's machine, so nothing here adds a sandbox of its own.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from daedalus.host.filesystem import ExecOutcome

if TYPE_CHECKING:
    from daedalus.terminals.bridge import HostBridge

HOST = "host"

DAEMON_MAX_TIMEOUT = 30 * 60.0
"""The longest one ``exec.run`` may be given (the daemon's own ``MaxExecTimeout``); a longer call is
refused outright, so a longer wait is cut to this and the command is told to go to the background."""

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

    async def put_bytes(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` on the host byte for byte: what the operator attached to a message.

        Through stdin rather than the shell's arguments, in pieces under the daemon's cap, the first
        replacing the file and the rest appending; an ``OSError`` when the host will not take it.
        """
        bridge = self._bridge()
        if bridge is None:
            raise ConnectionError(BRIDGE_DOWN)
        target = shlex.quote(path)
        pieces = [data[i : i + STDIN_CHUNK] for i in range(0, len(data), STDIN_CHUNK)] or [b""]
        for index, piece in enumerate(pieces):
            lead = f"mkdir -p -- {shlex.quote(path.rsplit('/', 1)[0] or '/')} && " if index == 0 else ""
            redirect = ">" if index == 0 else ">>"
            result = await bridge.exec_run(HOST, ["bash", "-c", f"{lead}cat {redirect} {target}"], cwd=self.root, timeout=120.0, stdin=piece)
            if result.exit_code != 0:
                raise OSError((result.stderr or result.stdout).strip() or f"could not write {path} on the host")


__all__ = ["BRIDGE_DOWN", "DAEMON_MAX_TIMEOUT", "OLD_DAEMON", "PRELUDE", "HostExecBackend"]
