"""Services: long-running processes a session starts and the operator can reach.

A demo site, a dev server, a worker — anything that must keep running after the
turn that started it. A service is a detached process group in the session's
workspace, its output in a log file there, its port taken from the range the
container publishes, so the operator opens it from their own network. The
processes are children of the bot; a bot restart leaves them running (they are
reparented, the table remembers them), a container rebuild does not — at boot
what the table says was running and is not is started again when it was
marked ``restart``, and reported otherwise. One that dies while the bot runs is found by the watch
(:meth:`Services.watch`): started again when it is marked ``restart`` and had run a while, otherwise
marked dead and its session woken once with how it ended and the end of its log, as a finished job is.
A pid is kept with its process's start mark, so a number the system has since handed to another
process is never taken for the service, nor signalled.

Sharing: a service is reachable on the LAN at ``SERVICES_PUBLIC_HOST:<port>``; the operator can
also open it under ``/s/<slug>/`` — to anyone (``public``) or to whoever presents its key (``key``).
Those requests are proxied to the service's port by a listener of their own, on another origin than
the app (``daedalus.extensions.api_services_door``); ``public_address.services_base`` says where it is.

Natively a service is told to listen on loopback, through ``$HOST``. In a container it has to listen
on every interface of the container, because a published port arrives at the container's own address
and not at its loopback; the container's edge is what is published, and ``compose.yaml`` decides that.

A session that works in a host folder starts its services on the host, through the host terminal
daemon (:mod:`daedalus.host.host_services`): the row says ``machine = 'host'``, its pid is the host's,
its port comes from ``host_services_port_range``, and ``address`` is where on the host it listens and
where the proxy dials it. Whether it still runs is asked of the daemon, so while the daemon is down
the answer is "unknown", never "dead": the process may well be serving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
import subprocess
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import public_address
from daedalus.extensions.notifications import Draft
from daedalus.host.filesystem import machine_of
from daedalus.host.host_exec import BRIDGE_DOWN, HOST
from daedalus.host.host_services import HostDown, HostServices, exit_code_in, noting_exit
from daedalus.processes import end_tree, pid_alive, start_mark
from daedalus.stores import pairing
from daedalus.tools import sandbox_health
from daedalus.tools.shell import SandboxUnavailable, bwrap_status, invalidate_bwrap, sandbox_argv, shell_environment

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

NAME_MAX = 32
LOG_DIR = ".services"
STOP_GRACE_SECONDS = 5.0
START_GRACE_SECONDS = 1.5
"""How long a fresh service has to stay up to count as started rather than as a broken command."""
SHARE_MODES = ("local", "public", "key")
SHARE_COOKIE_PREFIX = "dshare-"
PHONE_COOKIE_PREFIX = "dphone-"
HOST_SEEN_SECONDS = 5.0
"""How long the daemon's word on whether a host service runs is believed. The share proxy asks on every
request, a page asks for dozens of files at once, and each question is a call to the host."""
HOST_WATCH_SECONDS = 30.0
"""How often the host's services are looked at: the daemon can go and come back, and the host reboot,
while this process runs, and nothing else would notice that a service died with them."""
LOCAL_WATCH_SECONDS = 5.0
"""How often this machine's services are looked at: a read of ``/proc`` each, so it can be often."""
RESTART_AFTER_SECONDS = 600.0
"""A service that dies sooner than this after its start is reported, not started again: what dies on
its own that soon is the program failing, and starting it every few seconds would hide that. It also
bounds the wakes: a crash loop is told once per death, and never more often than this."""
WAKE_TAIL_LINES = 12
WAKE_TAIL_CHARS = 1500


def on_host(row: dict[str, Any]) -> bool:
    return row.get("machine") == HOST


def local_alive(row: dict[str, Any]) -> bool:
    """Whether the process a local service's row names still runs and is still that process: a pid
    whose process now has another start mark is a stranger that was handed the number. A row without
    a mark (written before marks were kept, or where ``/proc`` cannot be read) goes by the pid alone."""
    pid = row.get("pid")
    if not pid_alive(pid):
        return False
    recorded = row.get("pid_start")
    if not recorded:
        return True
    now = start_mark(pid)
    return now is None or now == recorded


def ending_words(code: int | None) -> str:
    """How a service ended, from the code its shell recorded (:func:`noting_exit`)."""
    if code is None:
        return "it ended, and no exit code was recorded (killed with its shell, or the machine went down)"
    if code > 128:
        with contextlib.suppress(ValueError):
            return f"it was killed by signal {code - 128} ({signal.Signals(code - 128).name})"
        return f"it was killed by signal {code - 128}"
    if code < 0:
        with contextlib.suppress(ValueError):
            return f"it was killed by signal {-code} ({signal.Signals(-code).name})"
        return f"it was killed by signal {-code}"
    return f"it exited with code {code}"


async def dial_address(services: Any, row: dict[str, Any]) -> str | None:
    """Where the proxy reaches a service's port, or ``None`` when the service is not running.

    Loopback for a process of this machine; for a host service, the address on the host it was told
    to listen on. Any services object will do: one without host services is never asked about them.
    """
    if row.get("status") != "running" or not row.get("port"):
        return None
    if on_host(row):
        return (str(row.get("address") or "") or None) if await services.host_alive(row) else None
    return "127.0.0.1" if local_alive(row) else None


def unreachable_hint(settings: Any, address: str, port: int) -> str:
    """What to look at when the proxy could not connect to a service at ``address:port``: for a host
    service seen from a container, the host's firewall, which is what fails there while the service
    runs fine (ufw drops what arrives from Docker's bridges unless told otherwise). ``""`` otherwise."""
    if settings.native or address in ("127.0.0.1", "localhost", "::1"):
        return ""
    with contextlib.suppress(ValueError):
        lo, hi = parse_range(settings.host_services_port_range)
        if lo <= port <= hi:
            return (
                f"This service runs on the host, and Daedalus reaches it over the Docker bridge at {address}:{port}. If the "
                f"service is running and listening there, a firewall on the host that drops traffic from Docker's networks "
                f"(ufw does by default) is the likely cause: allow TCP port {port}, or the whole range {lo}-{hi}, from the "
                f"Docker networks to the host, for example `sudo ufw allow from <docker subnet> to any port {lo}:{hi} proto tcp`."
            )
    return ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lifetime(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{int(seconds)} s" if minutes < 1 else f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def parse_range(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    a, b = int(lo), int(hi or lo)
    if a < 1 or b < a or b > 65535:
        raise ValueError(f"bad port range {text!r}")
    return a, b


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # On POSIX the option only lets the probe bind over connections in TIME_WAIT. On Windows it
        # lets a socket bind over one that is listening, so every port read as free there and a
        # second service was handed the port a first one was serving on.
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


class Services:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.lo, self.hi = parse_range(app.settings.services_port_range)
        self.public_host = app.settings.services_public_host.strip()
        self.bind_host = "127.0.0.1" if app.settings.native else "0.0.0.0"  # noqa: S104 — the module's docstring says why
        self._procs: dict[tuple[str, str], subprocess.Popen[bytes]] = {}
        self.host_lo, self.host_hi = parse_range(app.settings.host_services_port_range)
        manager = app.manager
        self.host = HostServices(lambda: getattr(manager, "host_bridge", None), native=bool(app.settings.native))
        self._host_seen: dict[int, tuple[float, bool | None]] = {}
        """A host pid → when the daemon was asked about it and whether it ran; ``None`` is "no answer"."""
        self._busy: set[tuple[str, str]] = set()
        """Services being started or stopped right now. Their process may be gone while the row still
        says running, and the watch must not take a stop it is in the middle of for a death."""

    # -- views ----------------------------------------------------------------------

    @property
    def public_base(self) -> str:
        """Read each time, since a tunnel's address arrives while the process runs. The door when
        there is one, and the app's address, where pages are sandboxed, when there is not."""
        settings = self.app.settings
        return public_address.services_base(settings) or public_address.app_base(settings)

    def url_for(self, port: int | None) -> str | None:
        if not port:
            return None
        return f"http://{self.public_host or '<host>'}:{port}"

    def share_url(self, row: dict[str, Any], *, with_key: bool = True) -> str | None:
        """The public address of a shared service: through the bot's site, with the key in the query when it has one."""
        mode = row.get("share_mode") or "local"
        slug = row.get("share_slug")
        if mode == "local" or not slug or not row.get("port"):
            return None
        base = self.public_base or "<public-url>"
        url = f"{base}/s/{slug}/"
        if mode == "key" and with_key and row.get("share_key"):
            url += f"?key={row['share_key']}"
        return url

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        """The row as the app and the tools show it. A host service's liveness is what the daemon last
        said (:meth:`refresh_host` asks); one the daemon could not be asked about is ``unknown``, with a
        note that says why and how to bring the daemon back, because "dead" would be a guess."""
        note = row.get("note")
        if on_host(row):
            seen = self._host_seen.get(int(row.get("pid") or 0), (0.0, None))[1]
            if row["status"] == "running" and seen is None:
                status = "unknown"
                note = f"it runs on the host, and {BRIDGE_DOWN}"
            else:
                status = "running" if row["status"] == "running" and seen else ("stopped" if row["status"] == "stopped" else "dead")
        else:
            alive = row["status"] == "running" and local_alive(row)
            status = "running" if alive else ("stopped" if row["status"] == "stopped" else "dead")
        mode = row.get("share_mode") or "local"
        return {
            "share": {
                "mode": mode,
                "slug": row.get("share_slug"),
                "key": row.get("share_key") if mode == "key" else None,
                "url": self.share_url(row),
                "public_base": self.public_base,
                # On the app's own address a page gets no origin of its own: no storage, no cookies.
                "sandboxed": not public_address.services_base(self.app.settings),
            },
            "name": row["name"],
            "command": row["command"],
            "cwd": row["cwd"],
            "port": row.get("port"),
            # A host service listens on the host's side of the Docker bridge, which the operator's
            # network does not reach (host_services says why): it is opened through its share or a
            # phone's code, and an address that leads nowhere is not offered.
            "url": None if on_host(row) and not self.app.settings.native else self.url_for(row.get("port")),
            "pid": row.get("pid"),
            "status": status,
            "machine": HOST if on_host(row) else None,
            "address": row.get("address") if on_host(row) else None,
            "restart": bool(row.get("restart")),
            "note": note,
            "started_at": row["started_at"],
            "stopped_at": row.get("stopped_at"),
            "log_path": row["log_path"],
        }

    async def rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM services ORDER BY started_at")]
        return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM services WHERE session_id = ? ORDER BY started_at", (session_id,))]

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.rows(session_id)
        await self.refresh_host(rows)
        return [self.view(r) for r in rows]

    async def _view(self, session_id: str, name: str) -> dict[str, Any]:
        row = await self.get(session_id, name) or {}
        await self.refresh_host([row])
        return self.view(row)

    # -- host services --------------------------------------------------------------

    async def refresh_host(self, rows: Iterable[dict[str, Any]], *, fresh: bool = False) -> None:
        """Ask the daemon, in one call, which of the running host services in ``rows`` still run. An
        answer younger than :data:`HOST_SEEN_SECONDS` is kept unless ``fresh``."""
        now = asyncio.get_running_loop().time()
        marks = {int(r["pid"]): r.get("pid_start") for r in rows if on_host(r) and r.get("status") == "running" and r.get("pid")}
        stale = {p for p in marks if fresh or now - self._host_seen.get(p, (-HOST_SEEN_SECONDS * 2, None))[0] > HOST_SEEN_SECONDS}
        if not stale:
            return
        try:
            alive: set[int] | None = await self.host.alive([(pid, marks[pid]) for pid in stale])
        except HostDown:
            alive = None
        except Exception:  # noqa: BLE001 — a question the host could not answer is an unknown, not a death
            logger.warning("could not ask the host whether its services run", exc_info=True)
            alive = None
        for pid in stale:
            self._host_seen[pid] = (now, None if alive is None else pid in alive)

    async def host_alive(self, row: dict[str, Any]) -> bool | None:
        """Whether a host service runs; ``None`` when the daemon cannot be asked."""
        await self.refresh_host([row])
        return self._host_seen.get(int(row.get("pid") or 0), (0.0, None))[1]

    async def _running(self, row: dict[str, Any] | None) -> bool:
        """Whether the row's process runs, asked afresh. A host service the daemon cannot be asked
        about raises :class:`HostDown`, since neither "running" nor "gone" would be true of it."""
        if row is None or row.get("status") != "running":
            return False
        if not on_host(row):
            return local_alive(row)
        await self.refresh_host([row], fresh=True)
        alive = self._host_seen.get(int(row.get("pid") or 0), (0.0, None))[1]
        if alive is None:
            raise HostDown(f"service {row['name']!r} runs on the host, and {BRIDGE_DOWN}")
        return alive

    async def _end(self, row: dict[str, Any]) -> None:
        if on_host(row):
            if not await self.host.stop(int(row["pid"]), grace=STOP_GRACE_SECONDS, mark=row.get("pid_start")):
                logger.warning("service %s/%s: pid %s on the host is another process now; not signalled", row["session_id"], row["name"], row["pid"])
            self._host_seen.pop(int(row["pid"]), None)
        else:
            await self._terminate(int(row["pid"]), row.get("pid_start"))

    @contextlib.contextmanager
    def _hands_on(self, session_id: str, name: str) -> Iterator[None]:
        """While a service is started or stopped here, the watch leaves it alone (see ``_busy``)."""
        key = (session_id, name)
        held = key in self._busy
        self._busy.add(key)
        try:
            yield
        finally:
            if not held:
                self._busy.discard(key)

    async def list_all(self) -> list[dict[str, Any]]:
        """Every session's services, each with the session it belongs to."""
        titles = {row["id"]: row["title"] for row in await self.app.db.fetchall("SELECT id, title FROM sessions")}
        out = []
        rows = await self.rows()
        await self.refresh_host(rows)
        for r in rows:
            out.append({**self.view(r), "session_id": r["session_id"], "session_title": titles.get(r["session_id"], "(deleted session)")})
        out.sort(key=lambda s: (s["status"] != "running", s["session_title"], s["name"]))
        return out

    async def get(self, session_id: str, name: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM services WHERE session_id = ? AND name = ?", (session_id, name))
        return dict(row) if row else None

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM services WHERE share_slug = ?", (slug,))
        return dict(row) if row else None

    # -- sharing --------------------------------------------------------------------

    async def share(self, session_id: str, name: str, mode: str, *, rotate_key: bool = False) -> dict[str, Any]:
        """Switch how the service is reached from outside: LAN only, anyone through the site, or key holders.

        The slug is minted once and kept across mode changes, so a link stays valid when the key is
        dropped; the key is minted when key mode is first chosen and replaced only on ``rotate_key``.
        """
        if mode not in SHARE_MODES:
            raise ValueError(f"share mode must be one of {', '.join(SHARE_MODES)}")
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        if mode != "local" and not row.get("port"):
            raise ValueError("a service without a port cannot be shared")
        slug = row.get("share_slug")
        if not slug:
            # The slug is the only secret of a public share: long enough that it cannot be guessed or crawled into.
            slug = f"{name}-{secrets.token_hex(12)}"
            while await self.by_slug(slug) is not None:
                slug = f"{name}-{secrets.token_hex(12)}"
        key = row.get("share_key")
        if mode == "key" and (not key or rotate_key):
            key = secrets.token_urlsafe(18)
        await self.app.db.execute("UPDATE services SET share_mode = ?, share_slug = ?, share_key = ? WHERE session_id = ? AND name = ?", (mode, slug, key, session_id, name))
        if rotate_key or mode != (row.get("share_mode") or "local"):
            # Whoever was let in was let in on the terms that just changed, phones included.
            await pairing.forget_phones(self.app.db, slug)
        return await self._view(session_id, name)

    async def ensure_slug(self, session_id: str, name: str) -> str:
        """The service's address under ``/s/``, minted if it has none. An address is not a share: a
        private service has a slug too once a phone is sent to it, and it stays closed to anyone without a pass."""
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        slug = row.get("share_slug")
        if not slug:
            slug = f"{name}-{secrets.token_hex(12)}"
            await self.app.db.execute("UPDATE services SET share_slug = ? WHERE session_id = ? AND name = ?", (slug, session_id, name))
        return str(slug)

    def share_allows(self, row: dict[str, Any], presented_key: str | None) -> bool:
        """Whether a request from outside may reach the service: public, or key mode with the right key."""
        mode = row.get("share_mode") or "local"
        if mode == "public":
            return True
        if mode == "key" and row.get("share_key") and presented_key:
            return secrets.compare_digest(str(row["share_key"]), presented_key)
        return False

    # -- ports ----------------------------------------------------------------------

    async def allocate_port(self, wanted: int | str | None, *, host: bool = False, starting: tuple[str, str] | None = None) -> int | None:
        """A port in the published range that no service holds and nothing listens on; None = no port.

        With ``host`` the range is the host's, and "nothing listens" is asked of the host, where the
        service will listen: a port free in this container says nothing about the operator's machine.
        A host service whose row says it runs keeps its port even while the daemon cannot say so,
        except the one ``starting`` names, which is known to be gone and is being started again.
        """
        if wanted in (None, "", "none"):
            return None
        rows = await self.rows()
        free: Callable[[int], bool]
        if host:
            lo, hi, what = self.host_lo, self.host_hi, "the host's service range"
            taken = {int(r["port"]) for r in rows if on_host(r) and r.get("port") and r["status"] == "running" and (r["session_id"], r["name"]) != starting}
            listening = await self.host.listening()
            free = lambda port: port not in listening  # noqa: E731
        else:
            lo, hi, what = self.lo, self.hi, "the published range"
            taken = {int(r["port"]) for r in rows if not on_host(r) and r.get("port") and r["status"] == "running" and local_alive(r)}
            free = port_free
        if wanted != "auto":
            port = int(wanted)
            if not (lo <= port <= hi):
                raise ValueError(f"port {port} is outside {what} {lo}-{hi}; use port='auto' or one inside it")
            if port in taken or not free(port):
                raise ValueError(f"port {port} is in use")
            return port
        for port in range(lo, hi + 1):
            if port not in taken and free(port):
                return port
        raise ValueError(f"no free port in {lo}-{hi}; stop a service first")

    # -- lifecycle ------------------------------------------------------------------

    async def start(self, session_id: str, *, name: str, command: str, cwd: str | None = None, port: int | str | None = "auto", restart: bool = True) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        name = name.strip().lower().replace(" ", "-")
        if not name or len(name) > NAME_MAX or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError("a service name is 1–32 characters: letters, digits, '-' or '_'")
        command = command.strip()
        if not command:
            raise ValueError("the command is empty")
        with self._hands_on(session_id, name):
            return await self._start(session_id, state, name=name, command=command, cwd=cwd, port=port, restart=restart)

    async def _start(self, session_id: str, state: Any, *, name: str, command: str, cwd: str | None, port: int | str | None, restart: bool) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        existing = await self.get(session_id, name)
        try:
            running = await self._running(existing)
        except HostDown as exc:
            raise RuntimeError(str(exc)) from exc
        if running and existing is not None:
            raise ValueError(f"service {name!r} is already running (pid {existing['pid']}); ServiceStop it first")
        # The directory goes through the session's own resolve, as every other path-taking tool's
        # does: a service is the one thing here that outlives the turn, and a cwd taken raw was the
        # single path into the filesystem that the project's containment did not judge.
        session_services = manager.locator_services(session_id)
        if session_services is not None and machine_of(session_services.exec_backend) == HOST:
            return await self._start_on_host(session_id, session_services, name=name, command=command, cwd=cwd, port=port, restart=restart)
        workdir = session_services.resolve(cwd) if session_services is not None else (Path(cwd) if cwd else state.workspace)
        if not workdir.is_absolute():
            workdir = state.workspace / workdir
        if not workdir.is_dir():
            raise ValueError(f"working directory does not exist: {workdir}")
        chosen = await self.allocate_port(port)
        log_dir = session_services.logs_dir(LOG_DIR) if session_services is not None else state.workspace / LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"
        env = shell_environment(session_id, {"PORT": str(chosen)} if chosen else None, cwd=workdir)
        env["HOST"] = self.bind_host
        # The same wall Exec has: a service is a long-lived command, not a way around the sandbox.
        try:
            argv, sandboxed = await sandbox_argv(
                noting_exit(command), self.app.config.tools.exec, writable=session_services.sandbox_writable() if session_services is not None else [state.workspace], session_id=session_id,
            )
        except SandboxUnavailable as exc:
            raise RuntimeError(sandbox_health.agent_text(await sandbox_health.report_down(str(exc)))) from exc
        with open(log_path, "ab") as log:
            log.write(f"\n=== {_now()} start: {command}\n".encode())
            proc = subprocess.Popen(argv, cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)  # noqa: S603
        self._procs[(session_id, name)] = proc
        await self.app.db.execute(
            "INSERT INTO services(session_id, name, command, cwd, port, pid, status, restart, log_path, note, started_at, stopped_at, pid_start) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL, ?)"
            " ON CONFLICT(session_id, name) DO UPDATE SET command = excluded.command, cwd = excluded.cwd, port = excluded.port, pid = excluded.pid, status = 'running', restart = excluded.restart, log_path = excluded.log_path, note = NULL, started_at = excluded.started_at, stopped_at = NULL, pid_start = excluded.pid_start",
            (session_id, name, command, str(workdir), chosen, proc.pid, int(restart), str(log_path), _now(), start_mark(proc.pid)),
        )
        # A command that dies within a moment is a broken command, not a service: say so with the log.
        # Watched rather than slept through, so one that dies at once is reported at once; one that
        # stays up is judged at the same moment as before.
        deadline = asyncio.get_running_loop().time() + START_GRACE_SECONDS
        while proc.poll() is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if proc.poll() is not None:
            await self.app.db.execute("UPDATE services SET status = 'dead', note = ?, stopped_at = ? WHERE session_id = ? AND name = ?", (f"exited with code {proc.returncode} right after start", _now(), session_id, name))
            tail = await self.logs(session_id, name, 40)
            started = tail.rsplit(f" start: {command}\n", 1)[-1].strip()
            if sandboxed and started.startswith("bwrap: ") and len(started.splitlines()) <= 3:
                # bubblewrap refused before the service ran: the machine's fault, said as such, with the probe
                # dropped so the next command finds out afresh. Its child is left blocked in the group: ended here.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                invalidate_bwrap()
                status = await asyncio.to_thread(bwrap_status)
                reason = status if status != "ok" else f"commands fail in bwrap itself although its probe passes: {started.splitlines()[0]}"
                raise RuntimeError(sandbox_health.agent_text(await sandbox_health.report_down(reason)))
            raise RuntimeError(f"service {name!r} exited with code {proc.returncode} right after start. Log:\n{tail}")
        return self.view(await self.get(session_id, name) or {})

    async def _start_on_host(self, session_id: str, session_services: Any, *, name: str, command: str, cwd: str | None, port: int | str | None, restart: bool) -> dict[str, Any]:
        """:meth:`start` for a session whose commands run on the host: the same row, the process on the
        host through the daemon (:class:`HostServices`), and no sandbox, as the session's Exec has none there."""
        if session_services.remote_readonly:
            raise ValueError(f"this session may read {session_services.workspace_dir} but not write it, and a service writes its log there")
        workdir = str(session_services.resolve(cwd))
        try:
            address = self.host.address()
            chosen = await self.allocate_port(port, host=True, starting=(session_id, name))
            log_path = f"{session_services.logs_dir(LOG_DIR)}/{name}.log"
            started = await self.host.start(command, cwd=workdir, log=log_path, port=chosen, host=address, watch=START_GRACE_SECONDS)
        except HostDown as exc:
            raise RuntimeError(f"a service of this session runs on the host, and {exc}") from exc
        await self.app.db.execute(
            "INSERT INTO services(session_id, name, command, cwd, port, pid, status, restart, log_path, note, started_at, stopped_at, machine, address, pid_start)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL, ?, ?, ?)"
            " ON CONFLICT(session_id, name) DO UPDATE SET command = excluded.command, cwd = excluded.cwd, port = excluded.port, pid = excluded.pid,"
            " status = 'running', restart = excluded.restart, log_path = excluded.log_path, note = NULL, started_at = excluded.started_at,"
            " stopped_at = NULL, machine = excluded.machine, address = excluded.address, pid_start = excluded.pid_start",
            (session_id, name, command, workdir, chosen, started.pid, int(restart), log_path, _now(), HOST, address, started.mark or None),
        )
        if started.exit_code is not None:
            await self._mark_dead(session_id, name, f"exited with code {started.exit_code} right after start")
            tail = await self.logs(session_id, name, 40)
            raise RuntimeError(f"service {name!r} exited with code {started.exit_code} right after start. Log:\n{tail}")
        self._host_seen[started.pid] = (asyncio.get_running_loop().time(), True)
        return self.view(await self.get(session_id, name) or {})

    async def stop(self, session_id: str, name: str, *, note: str = "stopped") -> dict[str, Any]:
        with self._hands_on(session_id, name):
            return await self._stop(session_id, name, note=note)

    async def _stop(self, session_id: str, name: str, *, note: str) -> dict[str, Any]:
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        try:
            if await self._running(row):
                await self._end(row)
        except HostDown as exc:
            # Marked stopped, the row would hand its port out again while the server may still hold it.
            raise RuntimeError(f"could not stop {name!r}: {exc}") from exc
        self._procs.pop((session_id, name), None)
        await self.app.db.execute("UPDATE services SET status = 'stopped', note = ?, stopped_at = ?, restart = 0 WHERE session_id = ? AND name = ?", (note, _now(), session_id, name))
        return self.view(await self.get(session_id, name) or {})

    async def _terminate(self, pid: int, mark: str | None = None) -> None:
        """End a local service's group: SIGTERM, then SIGKILL after the grace. Nothing is signalled when
        the pid now belongs to a process whose start mark is not ``mark``: that is a stranger."""
        if mark and (now := start_mark(pid)) is not None and now != mark:
            logger.warning("pid %s is another process now (started %s, the service %s); not signalled", pid, now, mark)
            return
        try:
            end_tree(pid, hard=False)
        except ProcessLookupError:
            return
        except PermissionError:
            logger.warning("could not signal service group %s", pid)
        deadline = asyncio.get_running_loop().time() + STOP_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline and pid_alive(pid):
            await asyncio.sleep(0.2)
        if pid_alive(pid):
            try:
                end_tree(pid, hard=True)
            except (ProcessLookupError, PermissionError):
                pass
        proc = next((p for p in self._procs.values() if p.pid == pid), None)
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    async def remove(self, session_id: str, name: str) -> bool:
        row = await self.get(session_id, name)
        if row is None:
            return False
        with self._hands_on(session_id, name):
            try:
                if await self._running(row):
                    await self._end(row)
            except HostDown as exc:
                raise RuntimeError(f"could not stop {name!r} before removing it: {exc}") from exc
            await self.app.db.execute("DELETE FROM services WHERE session_id = ? AND name = ?", (session_id, name))
            self._procs.pop((session_id, name), None)
        return True

    async def stop_session(self, session_id: str, note: str) -> int:
        """Stop every running service of a session and keep the rows, saying why; how many stopped.

        Unlike :meth:`stop_all`, which is for a deleted session, the rows stay: the session's history
        is still read, and the operator can see the server was stopped with its worker rather than
        wonder where it went. ``restart`` is cleared by :meth:`stop`, so a rebuild does not revive it.
        """
        n = 0
        for row in await self.rows(session_id):
            if row["status"] == "running":
                try:
                    await self.stop(session_id, row["name"], note=note[:700])
                except RuntimeError as exc:
                    # A host service the daemon cannot reach now keeps its row as running, which is
                    # true as far as anyone knows; the session's other servers are still stopped.
                    logger.warning("service %s/%s not stopped: %s", session_id, row["name"], exc)
                    continue
                n += 1
        return n

    async def stop_all(self, session_id: str) -> int:
        n = 0
        for row in await self.rows(session_id):
            self._busy.add((session_id, row["name"]))
            try:
                if await self._running(row):
                    await self._end(row)
                    n += 1
            except HostDown as exc:
                # The session goes either way; a server left behind on the host is at least logged.
                logger.warning("service %s/%s left running on the host: %s", session_id, row["name"], exc)
        await self.app.db.execute("DELETE FROM services WHERE session_id = ?", (session_id,))
        self._busy -= {key for key in self._busy if key[0] == session_id}
        return n

    async def logs(self, session_id: str, name: str, lines: int = 60) -> str:
        row = await self.get(session_id, name)
        if row is None:
            raise ValueError(f"no service named {name!r}")
        if on_host(row):
            try:
                return await self.host.logs(row["log_path"], lines)
            except HostDown as exc:
                return f"(the log is on the host at {row['log_path']}, and {exc})"
        path = Path(row["log_path"])
        manager = self.app.manager
        services = manager.locator_services(session_id) if manager is not None else None
        if services is not None and not services.contains(path):
            # The stored log path is judged like every other path this session names. A row whose
            # service ran before the session's folder became a project's can still carry a path
            # outside it, and reading it here would be the one file read that skipped the wall.
            return f"(the log is at {path}, outside {services.where()}, the folders this session works in: it is not read from here)"
        if not path.exists():
            return "(no log yet)"
        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"(log unreadable: {exc})"
        text = data[-200_000:].decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-max(1, lines):]) or "(empty)"

    # -- containment ----------------------------------------------------------------

    def directory_refusal(self, session_id: str, cwd: str) -> str:
        """Why this session may not run a service in ``cwd`` any more, or ``""`` when it may.

        A service outlives the turn that started it, so its directory is the one stored path here
        that a later change of the session's own folder can leave pointing somewhere the session is
        no longer allowed to go: a row written before the folder was a project's, a session moved
        into a project, a directory that belonged to a sibling agent all along. ``SessionServices``
        answers the containment question; this turns the answer into something an operator can act on.
        """
        manager = self.app.manager
        services = manager.locator_services(session_id) if manager is not None else None
        if services is None or services.walls is None or services.contains(Path(cwd)):
            return ""
        directory, root = Path(cwd), services.workspace_dir
        same_name = root / directory.name
        fix = (
            f"a folder of that name is already at {same_name} — ServiceStart the service there"
            if same_name.is_dir()
            else f"move it into {root}, or open it as a project of its own and start the service from a session there"
        )
        return (
            f"its working directory {directory} is outside {services.where()}, the folders this session works in, so it was left as "
            f"it is rather than started somewhere else: the same command in another folder serves different files under "
            f"the same port and the same link. To bring it back, {fix}."
        )

    async def _mark_dead(self, session_id: str, name: str, note: str) -> None:
        # 700, not the 400 a plain failure gets: a note that names two absolute paths and the way back
        # is the whole point of the row, and a truncated one ends in the middle of the path it is about.
        await self.app.db.execute("UPDATE services SET status = 'dead', note = ?, stopped_at = ? WHERE session_id = ? AND name = ?", (note[:700], _now(), session_id, name))

    # -- boot -----------------------------------------------------------------------

    async def reconcile(self) -> None:
        """After a restart: what the table calls running either still runs, is restarted, or is reported dead."""
        manager = self.app.manager
        assert manager is not None
        notifications = self.app.notifications
        for row in await self.rows():
            if row["status"] != "running" or on_host(row):
                continue  # the host's are looked at by the watch, once the daemon answers
            if local_alive(row):
                continue  # survived the bot restart (reparented); the table is right
            sid, name = row["session_id"], row["name"]
            if row.get("restart") and await manager.get_state(sid) is not None:
                # Asked before the restart rather than caught after it: a directory the session may no
                # longer reach is not a broken service, and the operator needs both paths to fix it.
                if refusal := self.directory_refusal(sid, row["cwd"]):
                    await self._mark_dead(sid, name, f"not restarted after the rebuild: {refusal}")
                    logger.warning("service %s/%s not restarted: %s", sid, name, refusal)
                    if notifications is not None:
                        await notifications.post(Draft("system", f"Service '{name}' was not restarted", refusal, kind="service", tone="warning", session_id=sid, source="services"))
                    continue
                try:
                    await self.start(sid, name=name, command=row["command"], cwd=row["cwd"], port=row.get("port") or None, restart=True)
                    logger.warning("service %s/%s restarted after the rebuild", sid, name)
                    continue
                except Exception as exc:  # noqa: BLE001 - one service that cannot come back must not keep the bot from starting
                    # Anything at all: a command that no longer exists, a port taken, a directory the
                    # session may no longer reach. The row is what the operator reads afterwards, and
                    # the remaining services still get their turn.
                    await self._mark_dead(sid, name, f"could not restart after the rebuild: {exc}")
                    if notifications is not None:
                        await notifications.post(Draft("system", f"Service '{name}' did not come back", str(exc)[:400], kind="service", tone="warning", session_id=sid, source="services"))
                    continue
            await self.app.db.execute("UPDATE services SET status = 'dead', note = 'not running after the restart', stopped_at = ? WHERE session_id = ? AND name = ?", (_now(), sid, name))
            if notifications is not None:
                await notifications.post(Draft("system", f"Service '{name}' is not running", "It did not survive the restart; ServiceStart runs it again.", kind="service", session_id=sid, source="services"))

    def _quiet(self) -> bool:
        """Whether deaths are left for a later look: while this process stops or recovers its sessions,
        a service that went with it is no news, and a wake would land in a session about to stop."""
        manager = self.app.manager
        return manager is None or bool(getattr(manager, "shutting_down", False) or getattr(manager, "recovering", False))

    async def reconcile_local(self) -> None:
        """This machine's services that the table calls running and that are gone: each is started
        again or reported (:meth:`_after_death`). The boot's :meth:`reconcile` covers what died with
        the last process; this is what dies while this one runs."""
        if self._quiet():
            return
        for row in await self.rows():
            key = (row["session_id"], row["name"])
            if row["status"] != "running" or on_host(row) or key in self._busy or local_alive(row):
                continue
            proc = self._procs.pop(key, None)
            # Reaped, so the process is not left a zombie; its code is the shell's, the log's line is the command's.
            returned = proc.poll() if proc is not None and proc.pid == row.get("pid") else None
            current = await self.get(*key)
            if current is None or current["status"] != "running" or current.get("pid") != row.get("pid") or key in self._busy:
                continue  # stopped, removed or started again while it was looked at
            await self._after_death(current, returned)

    async def reconcile_host(self) -> None:
        """What the table calls running on the host either still runs, is started again, or is reported.

        The host's counterpart of :meth:`reconcile`, run whenever the daemon answers rather than once
        at boot: the daemon may connect long after this process started, and a host service ends with
        a reboot of the host while this process runs on unaware. Started again only when it was marked
        ``restart`` and had lived :data:`RESTART_AFTER_SECONDS` (see there).
        """
        rows = [r for r in await self.rows() if on_host(r) and r["status"] == "running"]
        if not rows or not self.host.available() or self._quiet():
            return
        await self.refresh_host(rows, fresh=True)
        for row in rows:
            alive = self._host_seen.get(int(row.get("pid") or 0), (0.0, None))[1]
            if alive is None:
                return  # the daemon went while it was asked; the next look asks again
            key = (row["session_id"], row["name"])
            if alive or key in self._busy:
                continue
            current = await self.get(*key)
            if current is None or current["status"] != "running" or current.get("pid") != row.get("pid") or key in self._busy:
                continue
            await self._after_death(current)

    async def _after_death(self, row: dict[str, Any], returned: int | None = None) -> None:
        """A service that died without being asked to: started again when it is marked ``restart`` and
        had lived :data:`RESTART_AFTER_SECONDS`, which tells nobody; otherwise marked dead, and its
        session woken once with the name, how it ended and the end of its log. The row being dead is
        what makes it once: the watch only looks at rows that say running."""
        sid, name = row["session_id"], row["name"]
        manager = self.app.manager
        notifications = self.app.notifications
        where = " on the host" if on_host(row) else ""
        lived = (datetime.now(UTC) - datetime.fromisoformat(row["started_at"])).total_seconds()
        if row.get("restart") and lived >= RESTART_AFTER_SECONDS and manager is not None:
            try:
                if await manager.get_state(sid) is None:
                    raise KeyError(f"session {sid} is gone")
                if not on_host(row) and (refusal := self.directory_refusal(sid, row["cwd"])):
                    raise ValueError(refusal)
                await self.start(sid, name=name, command=row["command"], cwd=row["cwd"], port=row.get("port") or None, restart=True)
                logger.warning("service %s/%s ended%s and was started again", sid, name, where)
                return
            except Exception as exc:  # noqa: BLE001 - one service that cannot come back must not keep the others from it
                why = f"it ended{where} and could not be started again: {exc}"
                await self._mark_dead(sid, name, why)
                if notifications is not None and on_host(row):
                    await notifications.post(Draft("system", f"Service '{name}' did not come back", str(exc)[:400], kind="service", tone="warning", session_id=sid, source="services"))
                await self._wake(row, why)
                return
        tail = await self._tail(row)
        code = exit_code_in(tail)
        ended = ending_words(code if code is not None else returned)
        why = f"{ended}{where} {_lifetime(lived)} after its start" + (", too soon to be started again unasked" if row.get("restart") else "")
        await self._mark_dead(sid, name, why)
        if notifications is not None and on_host(row):
            await notifications.post(Draft("system", f"Service '{name}' is not running", f"{why[0].upper()}{why[1:]}. ServiceLogs says why; ServiceStart runs it again.", kind="service", session_id=sid, source="services"))
        await self._wake(row, why, tail=tail)

    async def _tail(self, row: dict[str, Any]) -> str:
        try:
            return await self.logs(row["session_id"], row["name"], WAKE_TAIL_LINES)
        except Exception:  # noqa: BLE001 - the wake goes without the lines; the log stays where it is
            logger.debug("could not read the log of service %s/%s", row["session_id"], row["name"], exc_info=True)
            return ""

    async def _wake(self, row: dict[str, Any], why: str, *, tail: str | None = None) -> None:
        """Tell the service's session it died, as a finished job is told: a steer while a turn runs, a
        new turn when the session is idle. The agent started the service and is the one that can fix it."""
        manager = self.app.manager
        sid, name = row["session_id"], row["name"]
        if manager is None or await manager.get_state(sid) is None:
            return
        if tail is None:
            tail = await self._tail(row)
        lines = "\n".join(tail.splitlines()[-WAKE_TAIL_LINES:])[-WAKE_TAIL_CHARS:].strip("\n")
        text = (
            f"[service {name!r} died: {why}]\ncommand: `{' '.join(row['command'].split())[:160]}`\n"
            + (f"the last lines of its log {row['log_path']}:\n{lines}" if lines else f"its log {row['log_path']} is empty")
            + "\nServiceLogs reads more; ServiceStart runs it again."
        )
        try:
            # The command and its log alike: a command line carries a key as readily as its output prints one.
            await manager.submit(sid, manager.redactor.redact(text), steer=True, as_answer=False, origin="service")
        except Exception:  # noqa: BLE001 - as a job's note: logged, and the row says dead for ServiceList
            logger.exception("could not tell session %s that its service %s died", sid, name)

    async def watch(self) -> None:
        """Look at this machine's services every :data:`LOCAL_WATCH_SECONDS` and the host's every
        :data:`HOST_WATCH_SECONDS`, for as long as this process runs."""
        loop = asyncio.get_running_loop()
        host_due = 0.0
        while True:
            try:
                await self.reconcile_local()
            except Exception:  # noqa: BLE001 - the watch outlives one look that failed
                logger.warning("looking at the services failed", exc_info=True)
            if not self.host.native and loop.time() >= host_due:
                host_due = loop.time() + HOST_WATCH_SECONDS
                try:
                    await self.reconcile_host()
                except Exception:  # noqa: BLE001 - as above
                    logger.warning("looking at the host's services failed", exc_info=True)
            await asyncio.sleep(LOCAL_WATCH_SECONDS)

    # -- service hook for the tools ---------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        sid = kwargs.pop("session_id")
        if op == "start":
            return await self.start(sid, **kwargs)
        if op == "stop":
            return await self.stop(sid, kwargs["name"])
        if op == "list":
            return await self.list(sid)
        if op == "logs":
            return await self.logs(sid, kwargs["name"], int(kwargs.get("lines") or 60))
        if op == "share":
            return await self.share(sid, kwargs["name"], kwargs.get("mode") or "local")
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    services = Services(app)
    app.extensions["services"] = services
    assert app.manager is not None
    app.manager.service_hooks["services"] = services.service
    app.manager.delete_hooks.append(services.stop_all)
    await services.reconcile()
    # Natively a host folder is this machine's own and its services are local ones, so the watch asks
    # no daemon there; it still looks at the local services.
    return [asyncio.create_task(services.watch(), name="services-watch")]
