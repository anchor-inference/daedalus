"""Services of a session that works in a host folder, run on the host by its terminal daemon.

Such a session's commands already run on the operator's machine (:mod:`daedalus.host.host_exec`), and
a server it starts has to run there too: its files are there, and the container cannot see them. This
is the host's half of :class:`~daedalus.extensions.services.Services` — starting a detached process
group, asking whether it still runs, ending it, reading its log — each one call to the daemon's
``exec.run``, so the table of services stays the one place that knows about every server, wherever
it runs.

Where it listens. The share proxy and the app's previews run in the container and dial the service,
so it must listen on an address of the host that the container reaches. Loopback is not one (from in
here it is the container itself), and every interface would put a development server on the operator's
network that nobody asked for. What is left is the host's own address on the Docker bridge, the one
``host.docker.internal`` names: reachable from the containers on this machine and not from the LAN.
Natively nothing of this is needed, because Daedalus and the service share a machine and its loopback.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import socket
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from daedalus.host.filesystem import ExecOutcome
from daedalus.host.host_exec import BRIDGE_DOWN, OLD_DAEMON, HostExecBackend

if TYPE_CHECKING:
    from daedalus.terminals.bridge import HostBridge

MARK = "@@daedalus-service@@"
"""The line a script prints before its answer, so an answer is told from a profile's chatter."""

GATEWAY_NAME = "host.docker.internal"

SCOPE = "systemd-run --user --scope --quiet --collect --"
"""Moves the service out of the daemon's own unit. The daemon is a systemd user unit whose processes
all stop with it, so a service started as its child died whenever the daemon was restarted or
updated; in a scope of its own it outlives the daemon as a local service outlives the bot."""

SCOPE_PROBE = "systemd-run --user --scope --quiet --collect -- true"
"""Asked before the scope is used: a host without a user manager (no systemd, no session bus) runs
the service as the daemon's child, which still works until the daemon goes."""

MARK_OF = (
    "mark_of() {{ if [ -r /proc/$1/stat ]; then s=$(cat /proc/$1/stat 2>/dev/null) || return 0; s=${{s##*) }}; set -- $s; "
    "[ -n \"${{20}}\" ] && printf '%s:%s' \"$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)\" \"${{20}}\"; "
    "else t=$(ps -o lstart= -p $1 2>/dev/null | tr -s ' ' '_' | sed 's/^_//;s/_$//'); [ -n \"$t\" ] && printf 'ps:%s' \"$t\"; fi; return 0; }}; "
)
"""A shell function that prints what tells a process from a later one with the same pid: the boot and
its start time where the host has ``/proc`` (the same mark :func:`daedalus.processes.start_mark`
takes), the start time ``ps`` reports elsewhere, nothing when the process is gone. Spaces become
underscores, so a mark is one word in the answers below."""

START = (
    MARK_OF +
    "cd -- {cwd} 2>/dev/null || {{ printf '%s nodir\\n' {mark}; exit 0; }}; "
    "mkdir -p -- {logdir} && touch -- {log} || {{ printf '%s nolog\\n' {mark}; exit 0; }}; "
    "export PORT={port} HOST={host}; "
    "printf '\\n=== %s start: %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" {shown} >> {log}; "
    "scope=; if command -v systemd-run >/dev/null 2>&1 && {probe} >/dev/null 2>&1; then scope='{scope}'; fi; "
    "if command -v setsid >/dev/null 2>&1; then detach=setsid; else detach=; fi; "
    "$detach $scope nohup bash -c {command} >> {log} 2>&1 < /dev/null & "
    "pid=$!; echo $pid > {log}.pid; m=$(mark_of $pid); n={ticks}; "
    "while [ $n -gt 0 ] && kill -0 $pid 2>/dev/null; do sleep 0.1; n=$((n-1)); done; "
    "if kill -0 $pid 2>/dev/null; then printf '%s running %s %s\\n' {mark} $pid \"$m\"; "
    "else wait $pid; printf '%s exited %s %s\\n' {mark} $? $pid; fi"
)
"""Starts the command detached on the host, in its own process group (``setsid``) and, where systemd
can give it one, its own scope (:data:`SCOPE`), with its output appended to the log; then watches it
for ``ticks`` tenths of a second, so a command that dies at once is reported as broken rather than
recorded as a service. ``$PORT`` and ``$HOST`` are exported after the profile was read, so a profile
that sets either cannot change where the service listens. The pid goes to a file beside the log at
once, so a start cancelled while it watches can still end what it started, and the process's mark
(:data:`MARK_OF`) is taken at once too, before the pid could be anyone else's."""

ALIVE = (
    MARK_OF +
    "for e in {entries}; do p=${{e%%=*}}; m=${{e#*=}}; "
    "if kill -0 -- -$p 2>/dev/null || kill -0 $p 2>/dev/null; then now=$(mark_of $p); "
    "if [ -z \"$m\" ] || [ -z \"$now\" ] || [ \"$now\" = \"$m\" ]; then echo $p; fi; fi; done; "
    "printf '%s\\n' {mark}"
)
"""Which of the services still run, as a group or as a process, each given as ``pid=mark``. A pid
whose process now has another mark is a stranger that was handed the number, and is not named. One
with no mark now (its leader gone, the group still there) is the service: the kernel does not hand
out a pid that is still a group's id. The marker line proves the answer is whole."""

STOP = (
    MARK_OF +
    "now=$(mark_of {pid}); if [ -n {expected} ] && [ -n \"$now\" ] && [ \"$now\" != {expected} ]; then printf 'stranger\\n%s\\n' {mark}; exit 0; fi; "
    "kill -TERM -- -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null; "
    "for _ in {ticks}; do kill -0 -- -{pid} 2>/dev/null || kill -0 {pid} 2>/dev/null || {{ printf '%s\\n' {mark}; exit 0; }}; sleep 0.5; done; "
    "kill -KILL -- -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null; printf '%s\\n' {mark}"
)
"""SIGTERM to the service's group, SIGKILL to what is left of it after the grace; nothing at all when the
pid now belongs to a process with another mark."""

KILL_BY_PIDFILE = "pid=$(cat {log}.pid 2>/dev/null) || exit 0; [ -n \"$pid\" ] || exit 0; kill -KILL -- -$pid 2>/dev/null || kill -KILL $pid 2>/dev/null; true"

LISTENING = (
    "if [ -r /proc/net/tcp ]; then "
    "for h in $(awk '$4 == \"0A\" {{ n = split($2, a, \":\"); print a[n] }}' /proc/net/tcp /proc/net/tcp6 2>/dev/null | sort -u); do echo $((16#$h)); done; "
    "else netstat -an 2>/dev/null | awk 'toupper($0) ~ /LISTEN/ {{ print $4 }}' | sed -E 's/.*[.:]([0-9]+)$/\\1/'; fi; "
    "printf '%s\\n' {mark}"
)
"""Every TCP port something listens on, on any address of the host: Linux's own table, or ``netstat``
where there is none. Read rather than bound, because a probe that binds would have to bind the very
address the service will, and a port held on any address of the host is one a server may fail on."""

LOGS = (
    "if [ -e {log} ]; then tail -c 200000 -- {log} | tail -n {lines}; else printf '(no log yet)\\n'; fi"
)


def noting_exit(command: str) -> str:
    """``command`` run in a subshell, with the way it ended appended to its output.

    The process that started a service is long gone when the service dies (this one was restarted,
    or the service runs on the host and was never its child), so nothing is left to collect the exit
    code from: the line in the log is the one record of it. Stopped on purpose, the whole group is
    signalled, this shell with it, and no line is written, which is right: nothing went wrong."""
    return (
        f"(\n{command}\n); c=$?; "
        "printf '\\n=== %s exited with code %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" \"$c\"; exit $c"
    )


EXIT_LINE = re.compile(r"^=== \S+ exited with code (-?\d+)$", re.MULTILINE)
START_LINE = re.compile(r"^=== \S+ start: ", re.MULTILINE)


def exit_code_in(log_tail: str) -> int | None:
    """The code the last run of a service ended with, as :func:`noting_exit` wrote it into the log;
    ``None`` when the run since the last start line has written none."""
    starts = list(START_LINE.finditer(log_tail))
    run = log_tail[starts[-1].end():] if starts else log_tail
    found = EXIT_LINE.findall(run)
    return int(found[-1]) if found else None


class HostDown(ConnectionError):
    """The host terminal daemon does not answer, so what a host service is doing cannot be known."""


@dataclass(slots=True)
class Started:
    pid: int
    exit_code: int | None = None
    """None while it runs; the code it exited with when it died within the watch."""
    mark: str = ""
    """The process's mark on the host (:data:`MARK_OF`); empty where the host could not give one."""


def default_gateway(routes: str) -> str:
    """The default route's gateway in the text of ``/proc/net/route``, or ``""``."""
    for line in routes.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
            with contextlib.suppress(ValueError, struct.error):
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return ""


def gateway_address() -> str:
    """The host's address as this container reaches it: the name Docker gives it when the compose
    file asks for one, else the container's default gateway, which is an address of the host too."""
    with contextlib.suppress(OSError):
        address = socket.gethostbyname(GATEWAY_NAME)
        if address and not address.startswith("127."):
            return address
    with contextlib.suppress(OSError):
        return default_gateway(Path("/proc/net/route").read_text())
    return ""


class HostServices:
    """The daemon calls behind a host service. Every method raises :class:`HostDown` when the daemon
    does not answer, so a caller can tell "the host could not be asked" from "the service is gone"."""

    def __init__(self, bridge: Callable[[], HostBridge | None], *, native: bool = False, resolve: Callable[[], str] = gateway_address) -> None:
        self._bridge = bridge
        self.native = native
        self._resolve = resolve

    def available(self) -> bool:
        bridge = self._bridge()
        return bridge is not None and bool(getattr(bridge, "available", lambda: True)())

    def address(self) -> str:
        """Where a host service is told to listen, which is also where it is dialled."""
        if self.native:
            return "127.0.0.1"
        address = self._resolve()
        if not address:
            raise RuntimeError(
                f"this container cannot tell its host's address: give the daedalus service `extra_hosts: [\"{GATEWAY_NAME}:host-gateway\"]` in compose.yaml"
            )
        return address

    async def _run(self, script: str, *, cwd: str = "/", timeout: float = 30.0) -> ExecOutcome:
        if not self.available():
            raise HostDown(BRIDGE_DOWN)
        outcome = await HostExecBackend(self._bridge, cwd).run(script, cwd=cwd, env=None, timeout=timeout)
        if outcome.output.strip() in (BRIDGE_DOWN, OLD_DAEMON):
            raise HostDown(outcome.output.strip())
        return outcome

    async def _answer(self, script: str, *, cwd: str = "/", timeout: float = 30.0) -> list[str]:
        """The lines a marked script printed before its mark; :class:`HostDown` when it never got there."""
        outcome = await self._run(script, cwd=cwd, timeout=timeout)
        before, marked, _ = outcome.output.rpartition(MARK)
        if not marked:
            raise HostDown(f"the host did not answer the question whole: {outcome.output.strip()[-300:] or f'exit code {outcome.exit_code}'}")
        return [line.strip() for line in before.splitlines() if line.strip()]

    async def start(self, command: str, *, cwd: str, log: str, port: int | None, host: str, watch: float) -> Started:
        """Start ``command`` in ``cwd`` on the host and watch it for ``watch`` seconds. ``ValueError``
        when the directory is not there or the log cannot be written."""
        quoted_log = shlex.quote(log)
        script = START.format(
            cwd=shlex.quote(cwd), logdir=shlex.quote(log.rpartition("/")[0] or "/"), log=quoted_log, command=shlex.quote(noting_exit(command)), shown=shlex.quote(command),
            port=shlex.quote(str(port or "")), host=shlex.quote(host), probe=SCOPE_PROBE, scope=SCOPE, mark=MARK,
            ticks=max(1, int(round(watch * 10))),
        )
        try:
            outcome = await self._run(script, cwd="/", timeout=watch + 30.0)
        except BaseException:
            # A start cancelled or cut off while it watched may have started the command: end it,
            # because no row will ever name it and nothing else would.
            with contextlib.suppress(Exception):
                await self._run(KILL_BY_PIDFILE.format(log=quoted_log), timeout=15.0)
            raise
        _, marked, rest = outcome.output.rpartition(MARK)
        words = rest.split() if marked else []
        if words[:1] == ["nodir"]:
            raise ValueError(f"working directory does not exist on the host: {cwd}")
        if words[:1] == ["nolog"]:
            raise ValueError(f"the log {log} cannot be written on the host")
        if words[:1] == ["running"] and len(words) > 1 and words[1].isdigit():
            return Started(pid=int(words[1]), mark=words[2] if len(words) > 2 else "")
        if words[:1] == ["exited"] and len(words) > 2 and words[1].lstrip("-").isdigit() and words[2].isdigit():
            return Started(pid=int(words[2]), exit_code=int(words[1]))
        raise RuntimeError(f"the host did not say whether the service started: {outcome.output.strip()[-400:] or f'exit code {outcome.exit_code}'}")

    async def alive(self, processes: Iterable[int | tuple[int, str | None]]) -> set[int]:
        """Which of the processes, each a pid or a pid with its mark, still run on the host. A pid given
        without a mark is judged by the number alone."""
        wanted: dict[int, str] = {}
        for entry in processes:
            pid, mark = entry if isinstance(entry, tuple) else (entry, None)
            if pid:
                wanted[int(pid)] = str(mark or "")
        if not wanted:
            return set()
        entries = " ".join(shlex.quote(f"{pid}={mark}") for pid, mark in sorted(wanted.items()))
        lines = await self._answer(ALIVE.format(entries=entries, mark=MARK))
        return {int(line) for line in lines if line.isdigit()} & set(wanted)

    async def stop(self, pid: int, *, grace: float, mark: str | None = None) -> bool:
        """End the service's group; ``False``, with nothing signalled, when ``pid`` now belongs to a
        process whose mark is not ``mark``."""
        ticks = " ".join(str(n) for n in range(max(1, int(grace / 0.5))))
        lines = await self._answer(STOP.format(pid=int(pid), ticks=ticks, mark=MARK, expected=shlex.quote(mark or "")), timeout=grace + 30.0)
        return "stranger" not in lines

    async def listening(self) -> set[int]:
        """The TCP ports something listens on, on the host."""
        return {int(line) for line in await self._answer(LISTENING.format(mark=MARK)) if line.isdigit()}

    async def logs(self, log: str, lines: int) -> str:
        outcome = await self._run(LOGS.format(log=shlex.quote(log), lines=max(1, int(lines))))
        text = outcome.output.rstrip("\n")
        return text or "(empty)"


__all__ = ["GATEWAY_NAME", "MARK", "HostDown", "HostServices", "Started", "default_gateway", "exit_code_in", "gateway_address", "noting_exit"]
