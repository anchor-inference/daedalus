"""The terminals service: every environment's daemon behind one interface, mirrored in the database.

The daemon owns the terminals; this service owns what the host knows about them — who they belong
to, which project, what they were started as, how they ended — and keeps that true across its own
restarts, which the daemons outlive. On every connection it reconciles: a row the daemon no longer
lists ended while nobody was looking (``exited``) or went with a daemon that restarted (``lost``); a
terminal the daemon runs with no row is adopted from the labels it was created with. Between
reconciles it follows the daemon's events, and a periodic sync catches whatever an event missed.

It also holds the machine-wide cap: how many terminals may run at once across both environments.
An agent's launch waits in line for a free place; the operator is asked to confirm and may go past
it, because a person's choice is not blocked by a number meant to keep agents from filling the
machine.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus import load as load_math
from daedalus.terminals import wire
from daedalus.terminals.client import Channel, PtydClient, Unavailable
from daedalus.terminals.endpoint import absolute_on, remember_hook_port, remember_state_dir
from daedalus.terminals.model import (
    ENVS,
    STATUSES,
    Conflict,
    EnvStatus,
    EnvUnavailable,
    InvalidRequest,
    LiveTerminals,
    NoRebuilder,
    NotFound,
    Origin,
    OutputChunk,
    OverCap,
    Owner,
    StaleLaunch,
    TerminalError,
    TerminalEvent,
    TerminalSpec,
    TimedOut,
    Unsupported,
    WriteReceipt,
)
from daedalus.terminals.owners import Owners
from daedalus.terminals.sidechannels import SideChannels
from daedalus.terminals.update import BY_HAND, DaemonUpdate

if TYPE_CHECKING:
    from daedalus.config import TerminalsConfig
    from daedalus.host.events import EventBus
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

TerminalView = dict[str, Any]
"""A terminal as the API and other subsystems see it; the shape is documented in the API docs."""


RECONNECT_FIRST = 0.5
RECONNECT_MAX = 10.0
SYNC_SECONDS = 60.0
PRUNE_SECONDS = 3600.0
FLUSH_SECONDS = 2.0
"""How often the event cursor is written down. Losing the last two seconds of it costs a replay of
events the reconcile handles idempotently; writing it per event would be a database write per bell."""
COSTS_SAVE_SECONDS = 300.0
ADMISSION_RECHECK = 2.0
"""How often a waiting launch looks again without being woken: a cap raised in the settings wakes
nobody, and this is how it is noticed."""
AUDIT_TEXT_BYTES = 4096
PREVIEW_ROWS = 6
LIST_PREVIEW_MAX = 12


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(value: Any) -> str | None:
    """A daemon timestamp (RFC 3339, nanoseconds) in the host's form; ``None`` stays ``None``."""
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits) :]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if stamp.year < 2000:
        return None  # Go's zero time, which a daemon never means as a date
    return stamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rpc_failure(exc: wire.RpcError, what: str) -> TerminalError:
    """A daemon's error as the service's own, keeping the daemon's words for the operator."""
    code = exc.code
    if code == wire.NOT_FOUND:
        return NotFound(f"{what}: no such terminal")
    if code == wire.EXITED:
        return Conflict(f"{what}: the terminal has exited", reason="exited")
    if code == wire.LIMIT:
        return Conflict(f"{what}: {exc.message}", reason="limit")
    if code == wire.FORBIDDEN:
        return Conflict(f"{what}: {exc.message}", reason="forbidden")
    if code == wire.KEYBOARD_HELD:
        return Conflict(f"{what}: a person is typing in this terminal and holds the keyboard", reason="keyboard_held")
    if code == wire.TIMEOUT:
        return TimedOut(f"{what}: {exc.message}")
    if code == wire.UNSUPPORTED:
        # The daemon says why: no sandbox in this build, or a program that reports no commands.
        return Unsupported(f"{what}: {exc.message}")
    if code == wire.METHOD_NOT_FOUND:
        return Unsupported(f"{what}: not available in this terminal service yet ({exc.message})")
    if code in (wire.INVALID_PARAMS, wire.INVALID_SIZE):
        return InvalidRequest(f"{what}: {exc.message}")
    if code == wire.STALE_LAUNCH:
        return StaleLaunch(f"{what}: {exc.message}")
    return TerminalError(f"{what}: {exc.message} ({code})")


@dataclass(slots=True)
class Link:
    """One environment: where its daemon is, and what is known of it now."""

    env: str
    run_dir: Path | None
    client: PtydClient | None = None
    reason: str = "not_configured"
    detail: str = ""
    info: dict[str, Any] = field(default_factory=dict)
    event_seq: int = 0
    event_seq_saved: int = 0
    connected_once: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def available(self) -> bool:
        return self.client is not None and self.client.connected

    @property
    def instance(self) -> str:
        return self.client.instance if self.client is not None else ""


@dataclass(slots=True)
class Waiter:
    """A launch waiting for a place under the cap, as the team view shows it."""

    actor: str
    env: str
    profile: str
    owner: Owner
    since: str
    id: str = field(default_factory=lambda: secrets.token_hex(4))

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "actor": self.actor, "env": self.env, "profile": self.profile, "owner": {"kind": self.owner.kind, "id": self.owner.id}, "since": self.since}


@dataclass(frozen=True, slots=True)
class Attachment:
    """One browser's channel to one terminal, as the daemon opened it."""

    terminal_id: str
    env: str
    client_id: str
    channel: Channel
    client: PtydClient

    @property
    def service_alive(self) -> bool:
        """Whether the connection the channel runs over is still up. A channel that closed while it
        is tells "the terminal let go of this client" apart from "the terminal service went away"."""
        return self.client.connected


Subscriber = Callable[[TerminalEvent], Awaitable[None]]


class Terminals(SideChannels):
    def __init__(
        self,
        db: Database,
        *,
        run_dirs: dict[str, Path | None],
        config: Callable[[], TerminalsConfig],
        owners: Owners,
        bus: EventBus | None = None,
        public_host: str = "",
        port_ranges: dict[str, str] | None = None,
        daemon_update: DaemonUpdate | None = None,
    ) -> None:
        self.db = db
        self.daemon_update = daemon_update
        """The container environment's daemon as the image holds it; None where no image does."""
        self.config = config
        self.owners = owners
        self.bus = bus
        self.public_host = public_host
        self.port_ranges = port_ranges or {}
        self.links = {env: Link(env, run_dirs.get(env)) for env in ENVS}
        for link in self.links.values():
            if link.run_dir is not None:
                link.reason, link.detail = "connecting", "the first connection is being made"
        self.costs = load_math.ProfileCosts()
        self._costs_dirty = False
        self._costs_saved_at = 0.0
        self._queue: list[Waiter] = []
        self._admit_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        """Taken by every change of a row's status, so a reconcile and an exit event cannot both end a
        terminal, and the second cannot overwrite the first's exit code with an unknown one."""
        self._freed = asyncio.Event()
        self._line_moved = asyncio.Event()
        """Set, and replaced, whenever a launch joins or leaves the line for a place under the cap."""
        self._subscribers: list[Subscriber] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._init_side()

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        """Connect to every configured environment in the background, and start the housekeeping.

        Never waits for a daemon: an environment that is not there yet is retried until it is, and
        the rest of the application starts meanwhile.
        """
        saved = await self.db.kv_get("terminals.profile_costs", {})
        if isinstance(saved, dict):
            self.costs.load(saved)
        for link in self.links.values():
            if link.run_dir is not None:
                self._tasks.append(asyncio.create_task(self._connection_loop(link), name=f"terminals-{link.env}"))
        self._tasks.append(asyncio.create_task(self._housekeeping(), name="terminals-housekeeping"))
        return list(self._tasks)

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for link in self.links.values():
            if link.client is not None:
                await link.client.close()
        with contextlib.suppress(Exception):
            await self._flush_cursors()
            await self._save_costs(force=True)

    async def wait_available(self, env: str, timeout: float = 5.0) -> bool:
        """Wait until the environment has been connected and reconciled once; for tests and the doctor."""
        link = self.links[env]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(link.connected_once.wait(), timeout)
        return link.available

    async def _connection_loop(self, link: Link) -> None:
        assert link.run_dir is not None
        delay = RECONNECT_FIRST
        while not self._closing:
            async def notified(method: str, params: dict[str, Any], env: str = link.env) -> None:
                await self._on_notification(env, method, params)

            client = PtydClient(link.env, link.run_dir, on_notification=notified)
            try:
                await client.connect()
                link.info = await client.call("daemon.info")
                link.client = client
                link.reason, link.detail = "", ""
                hooks = str((link.info.get("hooks") or {}).get("listen") or "")
                remember_hook_port(link.run_dir, int(hooks.rpartition(":")[2]) if hooks.rpartition(":")[2].isdigit() else 0)
                remember_state_dir(link.run_dir, str((link.info.get("side_channels") or {}).get("state_dir") or ""))
                await self._resume(link)
                link.connected_once.set()
                delay = RECONNECT_FIRST
                logger.info("terminal service %s connected: %s (instance %s)", link.env, link.info.get("version"), client.instance)
                await client.wait_closed()
                link.reason, link.detail = "unreachable", client.lost_reason or "the connection closed"
                logger.warning("terminal service %s went away: %s", link.env, link.detail)
            except Unavailable as exc:
                if (link.reason, link.detail) != (exc.reason, exc.detail):
                    logger.info("terminal service %s unavailable: %s", link.env, exc.detail)
                link.reason, link.detail = exc.reason, exc.detail
            except wire.RpcError as exc:
                link.reason, link.detail = "unreachable", f"the terminal service refused {exc.message}"
                logger.warning("terminal service %s: %s", link.env, link.detail)
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception:  # noqa: BLE001 — a bug in reconcile must not end the reconnecting for good
                logger.exception("terminal service %s: connecting failed", link.env)
                link.reason, link.detail = "unreachable", "connecting failed; the log has the error"
            link.client = None
            await client.close()
            link.connected_once.set()  # a first attempt that failed is an answer as well
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _resume(self, link: Link) -> None:
        """Pick up the daemon's events where this host left them, then make the rows agree with it."""
        assert link.client is not None
        saved = await self.db.kv_get(f"terminals.{link.env}.events", {})
        after = int(saved.get("seq") or 0) if isinstance(saved, dict) and saved.get("instance") == link.client.instance else 0
        link.event_seq = link.event_seq_saved = after
        # Subscribe first and list second: an event that lands between the two is then seen twice,
        # once in the listing and once as itself, rather than not at all. Both are idempotent.
        await link.client.call("events.subscribe", {"after_seq": after})
        await self._reconcile(link)
        # A daemon starts with no roots, and one that restarted has forgotten them.
        await self._push_roots(link.env, link.client, force=True)

    async def _housekeeping(self) -> None:
        last_sync = last_prune = time.monotonic()
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await self._flush_cursors()
                await self._save_costs()
                self._wake_waiters()
                for link in self.links.values():
                    if link.available and link.client is not None:
                        await self._push_roots(link.env, link.client)
                now = time.monotonic()
                if now - last_sync >= SYNC_SECONDS:
                    last_sync = now
                    for link in self.links.values():
                        if link.available:
                            await self._reconcile(link)
                if now - last_prune >= PRUNE_SECONDS:
                    last_prune = now
                    await self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — housekeeping failing once must not stop it for good
                logger.exception("terminals housekeeping failed")

    async def _flush_cursors(self) -> None:
        for link in self.links.values():
            if link.client is not None and link.event_seq != link.event_seq_saved:
                await self.db.kv_set(f"terminals.{link.env}.events", {"instance": link.client.instance, "seq": link.event_seq})
                link.event_seq_saved = link.event_seq

    async def _save_costs(self, *, force: bool = False) -> None:
        if self._costs_dirty and (force or time.monotonic() - self._costs_saved_at >= COSTS_SAVE_SECONDS):
            await self.db.kv_set("terminals.profile_costs", self.costs.dump())
            self._costs_dirty = False
            self._costs_saved_at = time.monotonic()

    # -- environments ---------------------------------------------------------------------------

    def configured(self, env: str) -> bool:
        """Whether this installation has a daemon for ``env`` at all (a run directory it was told of)."""
        link = self.links.get(env)
        return link is not None and link.run_dir is not None

    def available(self, env: str) -> bool:
        """Whether ``env``'s daemon answers now: connected, not merely installed."""
        link = self.links.get(env)
        return link is not None and link.available

    def environments(self, running: dict[str, int] | None = None) -> list[EnvStatus]:
        cfg = self.config()
        out = []
        for env, link in self.links.items():
            info = link.info if link.available else {}
            capabilities = info.get("capabilities") or {}
            version = str(info.get("version") or "")
            image_version = self.daemon_update.image_version if self.daemon_update is not None and env == "container" else ""
            out.append(
                EnvStatus(
                    env=env,
                    available=link.available,
                    reason="" if link.available else link.reason,
                    detail="" if link.available else link.detail,
                    version=version,
                    sandbox=str(capabilities.get("sandbox") or "") if link.available else "",
                    shell=str(info.get("shell") or ""),
                    home=str(info.get("home") or ""),
                    port_range=self.port_ranges.get(env, ""),
                    public_host=self.public_host,
                    preview_poll_ms=cfg.preview_poll_ms,
                    running=(running or {}).get(env, 0),
                    image_version=image_version,
                    update_available=bool(link.available and version and image_version and version != image_version),
                )
            )
        return out

    async def request_update(self, env: str, *, confirm: bool, actor: str = "operator") -> dict[str, Any]:
        """Ask for the terminals service to be recreated from the image: the daemon is updated, and
        every terminal it runs ends. Refused with the count until the operator confirms, whenever one
        is running — the count is taken here rather than trusted from the page that asked."""
        if env != "container":
            raise InvalidRequest("only the container's terminal service is updated from here; the host's is updated where it is installed")
        if self.daemon_update is None or not self.daemon_update.image_version:
            raise Unsupported("this installation has no terminals service image to update from")
        running = (await self.running_by_env()).get(env, 0)
        if running and not confirm:
            raise LiveTerminals(f"updating the terminal service ends {running} running terminal(s)", running=running)
        if not self.daemon_update.rebuilder_alive():
            raise NoRebuilder(f"no rebuilder is running to recreate the terminals service; on the server run: {BY_HAND}", command=BY_HAND, running=running)
        job = self.daemon_update.request()
        link = self.links[env]
        await self.audit("", env, actor, "daemon_update", {"job": job, "running": running, "from": str(link.info.get("version") or "") if link.available else "", "to": self.daemon_update.image_version})
        return {"job": job, "running": running, "image_version": self.daemon_update.image_version}

    def update_result(self, env: str, job: str) -> dict[str, str]:
        # The job names a file in the trigger directory, so it is exactly what request() makes.
        if env != "container" or self.daemon_update is None or not re.fullmatch(r"[0-9a-f]{32}", job):
            raise NotFound("no update of this environment's terminal service was asked for")
        return self.daemon_update.result(job)

    def _client(self, env: str) -> PtydClient:
        link = self.links.get(env)
        if link is None:
            raise InvalidRequest(f"an environment is one of {', '.join(ENVS)}, not {env!r}")
        if not link.available or link.client is None:
            raise EnvUnavailable(f"the {env} terminal service is not available: {link.detail or link.reason}", env=env, reason=link.reason)
        return link.client

    async def _call(self, env: str, method: str, params: dict[str, Any], *, what: str, timeout: float = 10.0) -> Any:
        client = self._client(env)
        try:
            return await client.call(method, params, timeout=timeout)
        except Unavailable as exc:
            raise EnvUnavailable(f"{what}: the {env} terminal service is not available: {exc.detail}", env=env, reason=exc.reason) from None
        except wire.RpcError as exc:
            raise rpc_failure(exc, what) from None

    # -- rows -------------------------------------------------------------------------------

    async def _row(self, terminal_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM terminals WHERE id = ?", (terminal_id,))
        if row is None:
            raise NotFound(f"no terminal {terminal_id}")
        return dict(row)

    async def count_running(self) -> int:
        row = await self.db.fetchone("SELECT count(*) AS n FROM terminals WHERE status = 'running'")
        return int(row["n"]) if row else 0

    async def running_by_env(self) -> dict[str, int]:
        return {r["env"]: int(r["n"]) for r in await self.db.fetchall("SELECT env, count(*) AS n FROM terminals WHERE status = 'running' GROUP BY env")}

    async def running_by_session(self, ids: Iterable[str] | None = None) -> dict[str, int]:
        """Running terminals per owning session, in one grouped query: the session list carries it."""
        sql = "SELECT owner_id, count(*) AS n FROM terminals WHERE status = 'running' AND owner_kind = 'session'"
        params: list[Any] = []
        if ids is not None:
            wanted = list(ids)
            if not wanted:
                return {}
            sql += f" AND owner_id IN ({','.join('?' * len(wanted))})"
            params = wanted
        return {r["owner_id"]: int(r["n"]) for r in await self.db.fetchall(sql + " GROUP BY owner_id", params)}

    async def audit(self, terminal_id: str, env: str, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
        """Append to the audit. Never with what a person typed: a host terminal's keystrokes include
        every password given to ``sudo``, so attachments record who and how many bytes, never which."""
        await self.db.execute(
            "INSERT INTO terminal_audit(at, terminal_id, env, actor, action, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), terminal_id, env, actor, action, json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))),
        )

    async def audit_log(self, terminal_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM terminal_audit WHERE terminal_id = ? ORDER BY seq DESC LIMIT ?", (terminal_id, max(1, min(limit, 1000))))
        return [{"seq": r["seq"], "at": r["at"], "env": r["env"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail_json"] or "{}")} for r in rows]

    # -- views ------------------------------------------------------------------------------

    async def _views(self, rows: list[dict[str, Any]], *, preview_rows: int = 0) -> list[TerminalView]:
        live: dict[str, dict[str, Any]] = {}
        for env in ENVS:
            ids = [r["id"] for r in rows if r["env"] == env and r["status"] == "running"]
            if not ids or not self.links[env].available:
                continue
            params: dict[str, Any] = {"ids": ids}
            if preview_rows:
                params["preview_rows"] = preview_rows
            try:
                listing = await self._call(env, "terminal.list", params, what="listing terminals")
            except TerminalError as exc:
                logger.info("terminals of %s listed without their live state: %s", env, exc.message)
                continue
            for info in listing.get("terminals") or []:
                live[str(info.get("id"))] = info
        owners = [Owner(r["owner_kind"], r["owner_id"]) for r in rows if r["owner_kind"] in ("session", "staff", "project") and r["owner_id"]]
        labels = await self.owners.labels(owners) if owners else {}
        staffed = [r["id"] for r in rows if r["owner_kind"] == "staff" and r["status"] == "running"]
        activities_of = getattr(self.owners, "activities", None)
        activities = await activities_of(staffed) if staffed and activities_of is not None else {}
        views = [self._view(r, live.get(r["id"]), labels, preview=bool(preview_rows)) for r in rows]
        for view in views:
            view["activity"] = activities.get(view["id"])
        return views

    def _view(self, row: dict[str, Any], info: dict[str, Any] | None, labels: dict[Owner, str], *, preview: bool = False) -> TerminalView:
        owner_id = row["owner_id"]
        label = labels.get(Owner(row["owner_kind"], owner_id)) if owner_id and row["owner_kind"] != "free" else None
        view: TerminalView = {
            "id": row["id"],
            "env": row["env"],
            "title": row["title"] or (str(info.get("title") or "") if info else "") or self._default_title(row),
            "owner": {"kind": row["owner_kind"], "id": owner_id, "label": label},
            "project_id": row["project_id"],
            "profile": row["profile"],
            "sandbox": bool(row["sandbox"]),
            "cwd": (str(info.get("cwd") or "") if info else "") or row["cwd"],
            "status": row["status"],
            "exit_code": row["exit_code"],
            "exit_signal": row["exit_signal"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "exited_at": row["exited_at"],
            "last_output_at": (iso(info.get("last_output_at")) if info else None) or row["last_output_at"],
            "last_input_at": (iso(info.get("last_input_at")) if info else None) or row["last_input_at"],
            "cols": int(info.get("cols") or row["cols"]) if info else row["cols"],
            "rows": int(info.get("rows") or row["rows"]) if info else row["rows"],
            "live": None,
            "last_command": None,
            "shell_integration": bool(row["shell_integration"]),
            "activity": None,
        }
        if info is not None:
            modes = info.get("modes") or {}
            view["live"] = {
                "clients": len(info.get("clients") or []),
                "busy": bool(info.get("busy")),
                "keyboard": info.get("keyboard") or {"owner": "auto", "until": None},
                "size_owner": str(info.get("size_owner") or ""),
                "alt_screen": bool(modes.get("alt_screen")),
                "output_seq": int(info.get("output_seq") or 0),
            }
            command = info.get("last_command")
            if isinstance(command, dict):
                view["last_command"] = {"command": command.get("command") or "", "exit_code": command.get("exit_code"), "at": iso(command.get("at"))}
            if preview and info.get("preview") is not None:
                view["preview"] = info["preview"]
        if view["last_command"] is None and row["last_command_json"]:
            view["last_command"] = json.loads(row["last_command_json"])
        if preview and "preview" not in view and row["final_preview_json"]:
            view["preview"] = json.loads(row["final_preview_json"])
        return view

    def _default_title(self, row: dict[str, Any]) -> str:
        argv = json.loads(row["argv_json"] or "[]")
        shell = str(self.config().shell or self.links[row["env"]].info.get("shell") or "") if row["env"] in self.links else ""
        program = Path(argv[0]).name if argv else Path(shell).name or "shell"
        place = Path(row["cwd"]).name or row["cwd"]
        return f"{program} · {place}" if place else program

    async def get(self, terminal_id: str) -> TerminalView:
        row = await self._row(terminal_id)
        return (await self._views([row]))[0]

    async def list(
        self,
        *,
        env: str | None = None,
        owner: Owner | None = None,
        owner_kind: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        preview_rows: int = 0,
    ) -> list[TerminalView]:
        where: builtins.list[str] = []
        params: builtins.list[Any] = []
        if env is not None:
            where.append("env = ?")
            params.append(env)
        if owner is not None:
            where.append("owner_kind = ? AND owner_id IS ?")
            params += [owner.kind, owner.id]
        elif owner_kind is not None:
            where.append("owner_kind = ?")
            params.append(owner_kind)
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            if status not in STATUSES:
                raise InvalidRequest(f"a status is one of {', '.join(STATUSES)}")
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM terminals" + (f" WHERE {' AND '.join(where)}" if where else "")
        # Running first, then the newest: the order a person scans a wall of terminals in.
        rows = [dict(r) for r in await self.db.fetchall(sql + " ORDER BY status != 'running', created_at DESC", params)]
        return await self._views(rows, preview_rows=max(0, min(preview_rows, LIST_PREVIEW_MAX)))

    # -- the cap ----------------------------------------------------------------------------

    def queue(self) -> builtins.list[dict[str, Any]]:
        """Launches waiting for a place under the cap, oldest first, with who asked."""
        return [w.view() for w in self._queue]

    async def wait_queue(self, predicate: Callable[[builtins.list[dict[str, Any]]], bool], *, timeout: float) -> bool:
        """Wait until the line for a place under the cap satisfies ``predicate``; false on timeout.
        Woken by the line moving, not by polling, so a loaded machine only makes it slower."""
        try:
            async with asyncio.timeout(timeout):
                while not predicate(self.queue()):
                    await self._line_moved.wait()
        except TimeoutError:
            return False
        return True

    def _line_changed(self) -> None:
        self._line_moved.set()
        self._line_moved = asyncio.Event()

    def _wake_waiters(self) -> None:
        if self._queue:
            self._freed.set()
            self._freed = asyncio.Event()

    async def _admit(self, spec: TerminalSpec, row: dict[str, Any], *, confirm_over_cap: bool, wait: float | None) -> bool:
        """Insert the row if the cap allows it, or wait for it to; returns whether the cap was passed.

        The row is written here, inside the admission lock, so it is its own reservation: two
        launches that both saw one free place cannot both take it. An agent's launch that finds no
        place joins the line and is served in order; the operator's is refused until confirmed, then
        admitted past the cap.
        """
        agent = spec.created_by.startswith("agent:")
        deadline = time.monotonic() + (self.config().agent_launch_wait_seconds if wait is None else wait)
        waiter: Waiter | None = None
        try:
            while True:
                async with self._admit_lock:
                    running = await self.count_running()
                    cap = self.config().running_cap
                    first_in_line = not self._queue or self._queue[0] is waiter
                    if running < cap and (not agent or first_in_line):
                        await self._insert(row)
                        return False
                    if not agent:
                        if not confirm_over_cap:
                            raise OverCap(
                                f"{running} terminals already run and the cap is {cap}; confirm to open one more",
                                running=running,
                                cap=cap,
                            )
                        await self._insert(row)
                        return True
                    if waiter is None:
                        waiter = Waiter(actor=spec.created_by, env=spec.env, profile=spec.profile, owner=spec.owner, since=now_iso())
                        self._queue.append(waiter)
                        self._line_changed()
                        logger.info("terminal launch by %s waits for a place: %d running, cap %d", spec.created_by, running, cap)
                    freed = self._freed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OverCap(f"waited for a place under the cap of {cap} and none came free", running=running, cap=cap, waited=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(freed.wait(), min(remaining, ADMISSION_RECHECK))
        finally:
            if waiter is not None and waiter in self._queue:
                self._queue.remove(waiter)
                self._line_changed()
                self._wake_waiters()  # the next in line may already fit

    async def _insert(self, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        await self.db.execute(f"INSERT INTO terminals({columns}) VALUES ({', '.join('?' * len(row))})", list(row.values()))  # noqa: S608 — column names are this module's own

    # -- creating and ending ----------------------------------------------------------------

    async def create(self, spec: TerminalSpec, *, confirm_over_cap: bool = False, wait: float | None = None) -> TerminalView:
        """Start a terminal for an owner and return its view.

        ``confirm_over_cap`` is the operator's answer to "the machine already runs as many as the cap
        allows"; an agent's launch ignores it and waits up to ``wait`` seconds (the configured wait by
        default) for a place instead.
        """
        cfg = self.config()
        link = self.links.get(spec.env)
        if link is None:
            raise InvalidRequest(f"an environment is one of {', '.join(ENVS)}, not {spec.env!r}")
        self._client(spec.env)
        if not await self.owners.exists(spec.owner):
            raise NotFound(f"no {spec.owner.kind} {spec.owner.id}")
        if spec.cwd is not None and not absolute_on(spec.env, spec.cwd):
            raise InvalidRequest("a working directory is an absolute path")
        if not (20 <= spec.cols <= 500 and 4 <= spec.rows <= 300):
            raise InvalidRequest("a terminal is 20×4 to 500×300")
        if not (spec.profile == "shell" or spec.profile.startswith("harness:")):
            raise InvalidRequest("a profile is shell or harness:<name>")
        project_id = spec.project_id or await self.owners.project_of(spec.owner)
        cwd = spec.cwd or await self.owners.default_cwd(spec.env, spec.owner, project_id) or ""
        terminal_id = secrets.token_hex(6)
        row = {
            "id": terminal_id,
            "env": spec.env,
            "project_id": project_id,
            "owner_kind": spec.owner.kind,
            "owner_id": spec.owner.id,
            "title": spec.title.strip()[:200],
            "cwd": cwd or "~",
            "argv_json": json.dumps(spec.argv or []),
            "profile": spec.profile,
            "sandbox": int(spec.sandbox),
            "status": "running",
            "created_at": now_iso(),
            "cols": spec.cols,
            "rows": spec.rows,
            "ptyd_instance": link.instance,
            "created_by": spec.created_by,
        }
        over_cap = await self._admit(spec, row, confirm_over_cap=confirm_over_cap, wait=wait)
        params: dict[str, Any] = {
            "id": terminal_id,
            "env": dict(spec.env_vars),
            "strip_env": list(spec.strip_env),
            "cols": spec.cols,
            "rows": spec.rows,
            "ring_bytes": cfg.ring_bytes,
            "input_idle_ms": cfg.input_idle_ms,
            "labels": {"owner_kind": spec.owner.kind, "owner_id": spec.owner.id or "", "project_id": project_id or "", "profile": spec.profile},
        }
        if cwd:
            params["cwd"] = cwd
        if spec.argv:
            params["argv"] = list(spec.argv)
        elif cfg.shell:
            params["shell"] = {"program": cfg.shell}
        if spec.title:
            params["title"] = spec.title
        if spec.launch_id:
            params["launch_id"] = spec.launch_id
        if spec.log_to_disk:
            params["log_to_disk"] = True
        if spec.sandbox:
            params["sandbox"] = {"writable": await self.owners.sandbox_writable(spec.env, spec.owner, project_id, cwd)}
        try:
            result = await self._call(spec.env, "terminal.create", params, what="starting the terminal")
        except BaseException:
            await self.db.execute("DELETE FROM terminals WHERE id = ?", (terminal_id,))
            self._wake_waiters()
            raise
        actual = str(result.get("cwd") or cwd)
        if spec.launch_id:
            self._launch_terminals.setdefault(spec.launch_id, terminal_id)
        # Whether the shell was started with its integration: only then are its commands recorded.
        integrated = int(bool(result.get("shell_integration")))
        await self.db.execute("UPDATE terminals SET cwd = ?, ptyd_instance = ?, shell_integration = ? WHERE id = ?", (actual, link.instance, integrated, terminal_id))
        detail: dict[str, Any] = {"owner": {"kind": spec.owner.kind, "id": spec.owner.id}, "cwd": actual, "profile": spec.profile, "sandbox": spec.sandbox}
        # What the sandbox made writable is the daemon's answer, not the request: a folder that is a
        # symbolic link, or missing in that environment, is left read-only, and the operator is told.
        boxed = result.get("sandbox") if spec.sandbox else None
        skipped = list((boxed or {}).get("skipped") or [])
        if boxed is not None:
            detail["writable"] = list(boxed.get("writable") or [])
            if skipped:
                detail["skipped"] = skipped
        if spec.argv:
            detail["argv"] = spec.argv
        if over_cap:
            detail["over_cap"] = True
        await self.audit(terminal_id, spec.env, spec.created_by, "create", detail)
        await self._publish(
            "terminal.created",
            {"env": spec.env, "owner_kind": spec.owner.kind, "owner_id": spec.owner.id, "title": row["title"], "cwd": actual, "profile": spec.profile, "sandbox": spec.sandbox},
            row,
        )
        view = await self.get(terminal_id)
        view["cwd_fallback"] = bool(result.get("cwd_fallback"))
        if spec.sandbox:
            view["sandbox_skipped"] = skipped
        return view

    async def kill(self, terminal_id: str, *, actor: str = "operator") -> TerminalView:
        """End a terminal: hang-up to its process group, then the kill after the grace."""
        row = await self._row(terminal_id)
        if row["status"] != "running":
            return await self.get(terminal_id)
        grace = self.config().kill_grace_ms
        observed_instance = self.links[row["env"]].instance
        try:
            result = await self._call(row["env"], "terminal.kill", {"id": terminal_id, "grace_ms": grace}, what="ending the terminal", timeout=grace / 1000 + 15)
        except NotFound:
            result = None
        await self.audit(terminal_id, row["env"], actor, "kill", {"exit_code": result.get("exit_code"), "signal": result.get("signal")} if result else {"gone": True})
        if result is None:
            await self._end(row, status="exited", exit_code=None, signal=None, observed_instance=observed_instance)
        else:
            await self._end(row, status="exited", exit_code=result.get("exit_code"), signal=result.get("signal"), observed_instance=observed_instance)
        return await self.get(terminal_id)

    async def signal(self, terminal_id: str, sig: str, *, actor: str = "operator") -> None:
        row = await self._row(terminal_id)
        if row["status"] != "running":
            raise Conflict("the terminal has exited", reason="exited")
        await self._call(row["env"], "terminal.signal", {"id": terminal_id, "signal": sig}, what=f"sending {sig}")
        await self.audit(terminal_id, row["env"], actor, "signal", {"signal": sig})

    async def restart(self, terminal_id: str, *, sandbox: bool | None = None, actor: str = "operator", confirm_over_cap: bool = False) -> TerminalView:
        """The same program for the same owner in the same place, as a new terminal with a new id."""
        row = await self._row(terminal_id)
        if row["status"] == "running":
            await self.kill(terminal_id, actor=actor)
        owner = Owner(row["owner_kind"], row["owner_id"])
        if not await self.owners.exists(owner):
            owner = Owner("free")
        spec = TerminalSpec(
            env=row["env"],
            owner=owner,
            project_id=row["project_id"],
            cwd=row["cwd"] if absolute_on(row["env"], row["cwd"]) else None,
            argv=json.loads(row["argv_json"] or "[]") or None,
            title=row["title"],
            sandbox=bool(row["sandbox"]) if sandbox is None else sandbox,
            cols=row["cols"],
            rows=row["rows"],
            profile=row["profile"],
            created_by=actor,
        )
        view = await self.create(spec, confirm_over_cap=confirm_over_cap)
        await self.audit(terminal_id, row["env"], actor, "restart", {"new_id": view["id"]})
        return view

    async def remove(self, terminal_id: str, *, actor: str = "operator") -> None:
        """Forget an ended terminal's row. A running one is ended first by whoever asks, never here."""
        row = await self._row(terminal_id)
        if row["status"] == "running":
            raise Conflict("the terminal is still running; end it first", reason="running")
        await self.db.execute("DELETE FROM terminals WHERE id = ?", (terminal_id,))
        if self.links[row["env"]].available:
            with contextlib.suppress(TerminalError):
                await self._call(row["env"], "terminal.forget", {"id": terminal_id}, what="forgetting the terminal")
        await self.audit(terminal_id, row["env"], actor, "remove")

    async def update(self, terminal_id: str, *, title: str | None = None, owner: Owner | None = None, actor: str = "operator") -> TerminalView:
        """Rename, or hand to another owner — usually to ``free``, so it outlives the one it had."""
        row = await self._row(terminal_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title.strip()[:200]
        if owner is not None:
            if not await self.owners.exists(owner):
                raise NotFound(f"no {owner.kind} {owner.id}")
            changes["owner_kind"], changes["owner_id"] = owner.kind, owner.id
            if owner.kind != "free":
                changes["project_id"] = await self.owners.project_of(owner)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            await self.db.execute(f"UPDATE terminals SET {sets} WHERE id = ?", [*changes.values(), terminal_id])  # noqa: S608 — keys are literals above
            await self.audit(terminal_id, row["env"], actor, "update", {k: v for k, v in changes.items()})
        return await self.get(terminal_id)

    async def close_owned(self, owner_kind: str, owner_id: str, *, actor: str = "system") -> int:
        """End every running terminal of an owner; returns how many were running.

        A terminal whose environment cannot be reached now is left running here: the reconcile that
        follows the reconnection ends a terminal whose owner no longer exists.
        """
        rows = await self.db.fetchall("SELECT id, env FROM terminals WHERE status = 'running' AND owner_kind = ? AND owner_id = ?", (owner_kind, owner_id))

        async def end(terminal_id: str) -> None:
            try:
                await self.kill(terminal_id, actor=actor)
            except TerminalError as exc:
                logger.warning("terminal %s of %s %s not ended: %s", terminal_id, owner_kind, owner_id, exc.message)

        await asyncio.gather(*(end(r["id"]) for r in rows))
        return len(rows)

    async def prune(self) -> tuple[int, int]:
        """Drop ended rows past their retention and audit rows past theirs; returns both counts."""
        cfg = self.config()
        ended_before = (datetime.now(UTC) - timedelta(hours=cfg.exited_retention_hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        audit_before = (datetime.now(UTC) - timedelta(days=cfg.audit_retention_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        old = await self.db.fetchall("SELECT id FROM terminals WHERE status != 'running' AND COALESCE(exited_at, created_at) < ?", (ended_before,))
        if old:
            await self.db.execute("DELETE FROM terminals WHERE status != 'running' AND COALESCE(exited_at, created_at) < ?", (ended_before,))
        before = await self.db.fetchone("SELECT count(*) AS n FROM terminal_audit WHERE at < ?", (audit_before,))
        await self.db.execute("DELETE FROM terminal_audit WHERE at < ?", (audit_before,))
        return len(old), int(before["n"]) if before else 0

    async def _end(self, row: dict[str, Any], *, status: str, exit_code: Any, signal: Any, exited_at: str | None = None, observed_instance: str | None = None) -> bool:
        """Publish a newly ended or physically verified terminal; repeated observations return False."""
        async with self._state_lock:
            current = await self.db.fetchone("SELECT * FROM terminals WHERE id = ?", (row["id"],))
            if current is None:
                return False
            row = dict(current)
            physical_exit = (status == "exited" and observed_instance and row["ptyd_instance"] == observed_instance
                             and self.links[row["env"]].instance == observed_instance)
            if current["status"] != "running":
                if not physical_exit:
                    return False
                proof = await self.db.fetchone("SELECT 1 FROM terminal_exit_observations"
                                               " WHERE terminal_id = ? AND runtime_instance = ?",
                                               (row["id"], observed_instance))
                if proof is not None:
                    return False
                # A bookkeeping loss can precede the daemon's actual exit. Preserve the later
                # evidence so cancellation does not remain stuck behind the earlier logical row.
            preview = None
            if status == "exited" and self.links[row["env"]].available:
                with contextlib.suppress(TerminalError):
                    listing = await self._call(row["env"], "terminal.list", {"ids": [row["id"]], "preview_rows": PREVIEW_ROWS}, what="keeping the last screen", timeout=5.0)
                    for info in listing.get("terminals") or []:
                        if info.get("preview") is not None:
                            preview = json.dumps(info["preview"], ensure_ascii=False)
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE terminals SET status = ?, exit_code = ?, exit_signal = ?, exited_at = ?, final_preview_json = COALESCE(?, final_preview_json) WHERE id = ?",
                    (status, exit_code if isinstance(exit_code, int) else None, str(signal) if signal else None,
                     exited_at or now_iso(), preview, row["id"]),
                )
                if physical_exit:
                    await conn.execute("INSERT OR IGNORE INTO terminal_exit_observations(terminal_id,runtime_instance,observed_at)"
                                       " VALUES (?,?,?)", (row["id"], observed_instance, now_iso()))
        self._wake_waiters()
        payload: dict[str, Any] = {"exit_code": exit_code if isinstance(exit_code, int) else None}
        if signal:
            payload["signal"] = str(signal)
        if status == "lost":
            payload["lost"] = True
        await self._publish("terminal.exited", payload, row)
        return True

    async def publish_event(self, terminal_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        """Publish an event about a terminal with the ids its row names: the project, and the session
        or staff member that owns it. False when the host has no row for it — a terminal it neither
        created nor adopted is nobody's, and an event without an owner would reach no filter."""
        row = await self.db.fetchone("SELECT id, project_id, owner_kind, owner_id FROM terminals WHERE id = ?", (terminal_id,))
        if row is None:
            return False
        await self._publish(event_type, payload, dict(row))
        return True

    async def _publish(self, event_type: str, payload: dict[str, Any], row: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(
                event_type,
                payload,
                terminal_id=row["id"],
                project_id=row.get("project_id"),
                session_id=row["owner_id"] if row.get("owner_kind") == "session" else None,
                staff_id=row["owner_id"] if row.get("owner_kind") == "staff" else None,
            )
        except Exception:  # noqa: BLE001 — a terminal's life does not depend on who hears of it
            logger.exception("publishing %s for terminal %s failed", event_type, row["id"])

    # -- reconcile --------------------------------------------------------------------------

    async def _reconcile(self, link: Link) -> None:
        """Make the rows of one environment agree with what its daemon runs."""
        client = link.client
        if client is None:
            return
        listing = await client.call("terminal.list", {})
        listed = {str(info.get("id")): info for info in listing.get("terminals") or []}
        instance = client.instance
        rows = [dict(r) for r in await self.db.fetchall("SELECT * FROM terminals WHERE env = ? AND status = 'running'", (link.env,))]
        for row in rows:
            info = listed.get(row["id"])
            if info is None:
                if row["ptyd_instance"] and row["ptyd_instance"] != instance:
                    await self._end(row, status="lost", exit_code=None, signal=None)
                elif await self._still_creating(row, instance):
                    continue
                else:
                    # Same daemon, and it no longer knows the terminal: it ended and was forgotten
                    # while this host was away (a daemon forgets an ended terminal after an hour).
                    await self._end(row, status="exited", exit_code=None, signal=None, observed_instance=instance)
                continue
            if info.get("status") == "exited":
                await self._end(row, status="exited", exit_code=info.get("exit_code"), signal=info.get("exit_signal"), exited_at=iso(info.get("exited_at")), observed_instance=instance)
                continue
            await self.db.execute(
                "UPDATE terminals SET ptyd_instance = ?, cwd = ?, cols = ?, rows = ?, last_output_at = COALESCE(?, last_output_at), last_input_at = COALESCE(?, last_input_at) WHERE id = ?",
                (instance, str(info.get("cwd") or row["cwd"]), int(info.get("cols") or row["cols"]), int(info.get("rows") or row["rows"]), iso(info.get("last_output_at")), iso(info.get("last_input_at")), row["id"]),
            )
        known = {r["id"] for r in await self.db.fetchall("SELECT id FROM terminals WHERE env = ?", (link.env,))}
        for terminal_id, info in listed.items():
            if terminal_id not in known and info.get("status") == "running":
                await self._adopt(link, info)
        await self._end_orphans(link)

    async def _still_creating(self, row: dict[str, Any], instance: str) -> bool:
        """A row written a moment ago whose ``terminal.create`` is still on its way: not ended."""
        created = iso(row["created_at"])
        if created is None:
            return False
        age = datetime.now(UTC) - datetime.fromisoformat(created.replace("Z", "+00:00"))
        return row["ptyd_instance"] in ("", instance) and age < timedelta(seconds=30)

    async def _adopt(self, link: Link, info: dict[str, Any]) -> None:
        """A terminal the daemon runs and no row describes: this host died between starting it and
        writing it down, or its row was lost. Its labels say whose it was."""
        labels = info.get("labels") or {}
        try:
            owner = Owner(str(labels.get("owner_kind") or "free"), str(labels.get("owner_id") or "") or None)
        except InvalidRequest:
            owner = Owner("free")
        profile = str(labels.get("profile") or "shell")
        row = {
            "id": str(info["id"]),
            "env": link.env,
            "project_id": str(labels.get("project_id") or "") or None,
            "owner_kind": owner.kind,
            "owner_id": owner.id,
            "title": "",
            "cwd": str(info.get("cwd") or "~"),
            "argv_json": json.dumps(info.get("argv") or []),
            "profile": profile if profile == "shell" or profile.startswith("harness:") else "shell",
            "sandbox": int(bool(info.get("sandbox"))),
            "status": "running",
            "created_at": iso(info.get("created_at")) or now_iso(),
            "cols": int(info.get("cols") or 80),
            "rows": int(info.get("rows") or 24),
            "ptyd_instance": link.instance,
            "created_by": "system",
            "shell_integration": int(bool(info.get("shell_integration"))),
        }
        await self.db.execute(
            f"INSERT OR IGNORE INTO terminals({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608 — column names are this module's own
            list(row.values()),
        )
        await self.audit(str(row["id"]), link.env, "system", "create", {"adopted": True, "owner": {"kind": owner.kind, "id": owner.id}})
        logger.warning("terminal %s in %s had no row and was adopted for %s %s", row["id"], link.env, owner.kind, owner.id or "")

    async def _end_orphans(self, link: Link) -> None:
        """End running terminals whose owner is gone: deleted while this environment was unreachable."""
        rows = await self.db.fetchall("SELECT id, owner_kind, owner_id FROM terminals WHERE env = ? AND status = 'running' AND owner_kind != 'free'", (link.env,))
        for row in rows:
            owner = Owner(row["owner_kind"], row["owner_id"])
            if not await self.owners.exists(owner):
                logger.warning("terminal %s outlived its %s %s and is ended", row["id"], owner.kind, owner.id)
                with contextlib.suppress(TerminalError):
                    await self.kill(row["id"], actor="system")

    # -- events -----------------------------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Every daemon event after the host has handled it; returns the unsubscribe."""
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback) if callback in self._subscribers else None

    async def _on_notification(self, env: str, method: str, params: dict[str, Any]) -> None:
        link = self.links[env]
        if method == "events.resync":
            # This host fell further behind than the daemon keeps; what it missed is read from the
            # daemon's listing instead.
            if link.available:
                await self._reconcile(link)
            return
        if method != "event":
            return
        seq = int(params.get("seq") or 0)
        if seq and seq <= link.event_seq:
            return
        link.event_seq = max(link.event_seq, seq)
        kind = str(params.get("type") or "")
        terminal_id = params.get("terminal_id") or None
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        assert isinstance(data, dict)
        if kind == "terminal.stats":
            await self._on_stats(data)
        elif terminal_id:
            await self._on_terminal_event(link, kind, str(terminal_id), data)
        event = TerminalEvent(env=env, seq=seq, type=kind, terminal_id=str(terminal_id) if terminal_id else None, at=iso(params.get("at")) or now_iso(), data=data)
        self._on_side_event(event)
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except Exception:  # noqa: BLE001 — one subscriber's failure is not the others'
                logger.exception("a terminal event subscriber failed on %s", kind)

    async def _on_terminal_event(self, link: Link, kind: str, terminal_id: str, data: dict[str, Any]) -> None:
        row_found = await self.db.fetchone("SELECT * FROM terminals WHERE id = ?", (terminal_id,))
        if kind == "terminal.created":
            if row_found is None and link.client is not None:
                with contextlib.suppress(Unavailable, wire.RpcError):
                    await self._adopt(link, await link.client.call("terminal.get", {"id": terminal_id}))
            return
        if row_found is None:
            return
        row = dict(row_found)
        if kind == "terminal.exited":
            if row["env"] != link.env:
                return
            await self._end(row, status="exited", exit_code=data.get("exit_code"), signal=data.get("signal"),
                            observed_instance=link.instance)
        elif kind == "terminal.cwd" and data.get("cwd"):
            await self.db.execute("UPDATE terminals SET cwd = ? WHERE id = ?", (str(data["cwd"]), terminal_id))
        elif kind == "terminal.command":
            command: dict[str, Any] = {"command": str(data.get("command") or ""), "exit_code": data.get("exit_code"), "at": now_iso()}
            if isinstance(data.get("duration_ms"), int):
                command["duration_ms"] = data["duration_ms"]
            await self.db.execute("UPDATE terminals SET last_command_json = ? WHERE id = ?", (json.dumps(command, ensure_ascii=False), terminal_id))

    async def _on_stats(self, data: dict[str, Any]) -> None:
        samples = [t for t in data.get("terminals") or [] if isinstance(t, dict) and t.get("id")]
        if not samples or not data.get("supported", True):
            return
        ids = [str(t["id"]) for t in samples]
        profiles = {r["id"]: r["profile"] for r in await self.db.fetchall(f"SELECT id, profile FROM terminals WHERE id IN ({','.join('?' * len(ids))})", ids)}  # noqa: S608 — placeholders only
        for sample in samples:
            profile = profiles.get(str(sample["id"]))
            if profile is not None:
                self.costs.add(profile, float(sample.get("rss_bytes") or 0), float(sample.get("cpu_percent") or 0))
                self._costs_dirty = True

    # -- load ---------------------------------------------------------------------------------

    async def load(self, *, cap: int | None = None) -> dict[str, Any]:
        """What the running terminals cost now, and what the machine would carry at ``cap``.

        ``cap`` is the value the operator is considering; without it, the configured one.
        """
        configured = self.config().running_cap
        target = cap if cap is not None else configured
        rows = await self.db.fetchall("SELECT id, env, profile FROM terminals WHERE status = 'running'")
        running_profiles = [r["profile"] for r in rows]
        envs, used_rss, used_cpu, daemons_rss = [], 0, 0.0, 0
        machine: dict[str, Any] = {}
        for env in ENVS:
            link = self.links[env]
            if not link.available:
                continue
            try:
                stats = await self._call(env, "terminal.stats", {}, what="measuring the terminals")
            except TerminalError as exc:
                logger.info("terminal stats of %s unavailable: %s", env, exc.message)
                continue
            terminals = [t for t in stats.get("terminals") or [] if isinstance(t, dict)]
            rss = sum(int(t.get("rss_bytes") or 0) for t in terminals)
            cpu = sum(float(t.get("cpu_percent") or 0) for t in terminals)
            # The daemon itself holds every terminal's emulator and output ring, and none of the
            # terminals' own processes shows that memory; leaving it out made "used now" smaller
            # than what the terminals really cost.
            daemon = stats.get("daemon") if isinstance(stats.get("daemon"), dict) else {}
            daemon_rss = int(daemon.get("rss_bytes") or 0)
            daemon_cpu = float(daemon.get("cpu_percent") or 0)
            used_rss += rss + daemon_rss
            used_cpu += cpu + daemon_cpu
            daemons_rss += daemon_rss
            env_machine = stats.get("machine") or {}
            total, available = load_math.effective_memory(env_machine)
            envs.append({"env": env, "supported": bool(stats.get("supported")), "terminals": len(terminals), "rss_bytes": rss, "cpu_percent": round(cpu, 1), "daemon_rss_bytes": daemon_rss, "mem_total_bytes": total, "mem_available_bytes": available, "cpus": load_math.effective_cpus(env_machine)})
            # The host environment is the machine itself; a container's view is the same machine
            # seen through its limits. Both environments on one server are one machine, so its
            # memory is counted once: from the host daemon when there is one.
            if env == "host" or not machine:
                machine = env_machine
        cost, basis = load_math.likely_cost(self.costs.profiles(), running_profiles)
        total, available = load_math.effective_memory(machine)
        return {
            "cap": configured,
            "running": len(rows),
            "queued": self.queue(),
            "used": {"rss_bytes": used_rss, "daemon_rss_bytes": daemons_rss, "cpu_percent": round(used_cpu, 1), "cpus": load_math.effective_cpus(machine), "mem_total_bytes": total, "mem_available_bytes": available, "machine_cpu_percent": round(float(machine.get("cpu_percent") or 0), 1)},
            "profiles": {name: c.view() for name, c in self.costs.profiles().items()},
            "likely": {**cost.view(), "basis": basis},
            "projection": load_math.project(cap=target, running=len(rows), used_rss=used_rss, used_cpu=used_cpu, machine=machine, cost=cost),
            "envs": envs,
            "thresholds": {"warn": load_math.WARN_PERCENT, "bad": load_math.BAD_PERCENT},
        }

    # -- reading and writing for agents -----------------------------------------------------

    async def write(
        self,
        terminal_id: str,
        *,
        text: str | None = None,
        paste: str | None = None,
        keys: builtins.list[str] | None = None,
        origin: Origin,
        wait_keyboard: bool = True,
        timeout: float = 30.0,
    ) -> WriteReceipt:
        """Write for an agent, after the human's quiet time; every write is in the audit, whole-hashed."""
        given = [(k, v) for k, v in (("text", text), ("paste", paste), ("keys", keys)) if v is not None]
        if len(given) != 1:
            raise InvalidRequest("give exactly one of text, paste and keys")
        kind, payload = given[0]
        row = await self._row(terminal_id)
        if row["status"] != "running":
            raise Conflict("the terminal has exited", reason="exited")
        params: dict[str, Any] = {
            "id": terminal_id,
            kind: payload,
            "origin": {"kind": "agent", "actor": origin.actor, **({"launch_id": origin.launch_id} if origin.launch_id else {}), **({"note": origin.note} if origin.note else {})},
            "wait": "keyboard" if wait_keyboard else "none",
            "timeout_ms": int(timeout * 1000),
        }
        whole = (" ".join(payload) if isinstance(payload, list) else str(payload)).encode()
        detail: dict[str, Any] = {"kind": kind, "text": whole[:AUDIT_TEXT_BYTES].decode("utf-8", "replace"), "sha256": hashlib.sha256(whole).hexdigest(), "length": len(whole)}
        if origin.launch_id:
            detail["launch_id"] = origin.launch_id
        if origin.note:
            detail["note"] = origin.note
        try:
            result = await self._call(row["env"], "terminal.write", params, what="writing to the terminal", timeout=timeout + 10)
        except TerminalError as exc:
            await self.audit(terminal_id, row["env"], origin.actor, "write", {**detail, "error": exc.message})
            raise
        await self.audit(terminal_id, row["env"], origin.actor, "write", {**detail, "bytes": result.get("bytes")})
        return WriteReceipt(bytes=int(result.get("bytes") or 0), seq_before=int(result.get("seq_before") or 0), queued_ms=int(result.get("queued_ms") or 0), delivered_at=iso(result.get("delivered_at")) or now_iso())

    async def read_output(self, terminal_id: str, *, since_seq: int, max_bytes: int = 65536, strip: bool = True) -> OutputChunk:
        row = await self._row(terminal_id)
        result = await self._call(row["env"], "terminal.read_output", {"id": terminal_id, "since_seq": max(0, since_seq), "max_bytes": max(1, min(max_bytes, 1 << 20)), "strip": strip}, what="reading the output")
        data: str | bytes = str(result.get("data") or "") if strip else base64.b64decode(result.get("data_b64") or "")
        return OutputChunk(from_seq=int(result.get("from_seq") or 0), to_seq=int(result.get("to_seq") or 0), head_seq=int(result.get("head_seq") or 0), gap=bool(result.get("gap")), data=data)

    async def read_screen(self, terminal_id: str, *, format: str = "text", scrollback: int = 0) -> dict[str, Any]:
        if format not in ("text", "vt", "runs"):
            raise InvalidRequest("a screen is read as text, vt or runs")
        row = await self._row(terminal_id)
        result: dict[str, Any] = await self._call(row["env"], "terminal.read_screen", {"id": terminal_id, "format": format, "scrollback": max(0, min(scrollback, 10_000))}, what="reading the screen")
        return result

    async def wait_for(self, terminal_id: str, *, regex: str | None = None, scope: str = "screen", idle_ms: int | None = None, command_done: bool = False, since_seq: int | None = None, timeout: float) -> dict[str, Any]:
        row = await self._row(terminal_id)
        params: dict[str, Any] = {"id": terminal_id, "scope": scope, "timeout_ms": int(timeout * 1000)}
        if regex is not None:
            params["regex"] = regex
        if idle_ms is not None:
            params["idle_ms"] = idle_ms
        if command_done:
            params["command_done"] = True
        if since_seq is not None:
            params["since_seq"] = since_seq
        result: dict[str, Any] = await self._call(row["env"], "terminal.wait_for", params, what="waiting on the terminal", timeout=timeout + 10)
        return result

    async def keyboard(self, terminal_id: str, owner: str, ttl_ms: int | None = None, *, actor: str = "operator") -> dict[str, Any]:
        row = await self._row(terminal_id)
        params: dict[str, Any] = {"id": terminal_id, "owner": owner}
        if ttl_ms is not None:
            params["ttl_ms"] = ttl_ms
        result: dict[str, Any] = await self._call(row["env"], "terminal.keyboard", params, what="handing the keyboard")
        await self.audit(terminal_id, row["env"], actor, "keyboard", {"owner": owner, "ttl_ms": ttl_ms})
        return result

    async def resize(self, terminal_id: str, cols: int, rows: int) -> None:
        row = await self._row(terminal_id)
        await self._call(row["env"], "terminal.resize", {"id": terminal_id, "cols": cols, "rows": rows}, what="resizing the terminal")
        await self.db.execute("UPDATE terminals SET cols = ?, rows = ? WHERE id = ?", (cols, rows, terminal_id))

    # -- attachments ------------------------------------------------------------------------

    async def attachable(self, terminal_id: str) -> dict[str, Any]:
        """The row of a terminal a browser may be handed a ticket for; raises what the ticket answers.

        A lost terminal went with its daemon, so there is nothing to show and it is refused as
        unknown. An exited one is still attachable: the daemon keeps its last screen until it is
        forgotten, and that screen is what a person opening it wants to see.
        """
        row = await self._row(terminal_id)
        if row["status"] == "lost":
            raise NotFound(f"terminal {terminal_id} was lost with its terminal service")
        self._client(row["env"])
        return row

    async def attach(self, terminal_id: str, *, read_only: bool, label: str, via: str) -> Attachment:
        """Open an attachment channel for a browser; the caller relays it and closes it.

        ``read_only`` goes to the daemon with the client, which ORs it with what the browser's own
        ATTACH says, so a browser can lower its rights and never raise them.
        """
        row = await self.attachable(terminal_id)
        client = self._client(row["env"])
        params = {"id": terminal_id, "client": {"kind": "viewer" if read_only else "human", "label": label[:256], "via": via[:256], "read_only": read_only}}
        try:
            result = await client.call("terminal.attach", params)
        except Unavailable as exc:
            raise EnvUnavailable(f"attaching: the {row['env']} terminal service is not available: {exc.detail}", env=row["env"], reason=exc.reason) from None
        except wire.RpcError as exc:
            raise rpc_failure(exc, "attaching") from None
        return Attachment(terminal_id=terminal_id, env=row["env"], client_id=str(result.get("client_id") or ""), channel=client.channel(int(result["channel"])), client=client)

    async def commands(self, terminal_id: str, *, last: int = 20, with_output: bool = False) -> builtins.list[dict[str, Any]]:
        """The commands a shell reported running, oldest first and the newest last, each with its
        rows, directory, exit code (``None`` while it runs) and, with ``with_output``, its output as
        the terminal still shows it. ``Unsupported`` when the terminal's program reports none: it is
        not a shell started with its integration."""
        row = await self._row(terminal_id)
        result = await self._call(row["env"], "terminal.commands", {"id": terminal_id, "last": max(1, min(last, 500)), "with_output": with_output}, what="listing the commands")
        return [c for c in result.get("commands") or [] if isinstance(c, dict)]

    async def agent_service(self, op: str, **kwargs: Any) -> Any:
        """What a session's own agent may ask of its terminals: read them, never write them.

        Only terminals the session owns. A host terminal is the operator's own machine, so it is left
        out unless ``terminals.agent_reads_host`` says otherwise; a terminal that is not readable is
        answered exactly like one that does not exist, so its id says nothing either.

        Operations: ``list``; ``resolve`` (``terminal`` = an id, a title, or empty for the only one);
        ``screen``, ``output`` and ``commands`` (``terminal_id``).
        """
        session_id = str(kwargs.pop("session_id"))
        owner = Owner("session", session_id)
        host_too = self.config().agent_reads_host
        if op == "list":
            return [v for v in await self.list(owner=owner) if host_too or v["env"] != "host"]
        if op == "resolve":
            return await self._agent_terminal(owner, str(kwargs.get("terminal") or "").strip(), host_too=host_too)
        terminal_id = str(kwargs.pop("terminal_id", "") or "")
        row = await self.db.fetchone("SELECT owner_kind, owner_id, env FROM terminals WHERE id = ?", (terminal_id,))
        if row is None or (row["owner_kind"], row["owner_id"]) != ("session", session_id) or (row["env"] == "host" and not host_too):
            raise NotFound(f"no terminal {terminal_id} of this session")
        if op == "output":
            return await self.read_output(terminal_id, since_seq=int(kwargs.get("since_seq") or 0), max_bytes=int(kwargs.get("max_bytes") or 65536))
        if op == "screen":
            return await self.read_screen(terminal_id, scrollback=int(kwargs.get("scrollback") or 0))
        if op == "commands":
            return await self.commands(terminal_id, last=int(kwargs.get("last") or 20))
        raise InvalidRequest(f"unknown terminal operation {op!r}")

    async def _agent_terminal(self, owner: Owner, ref: str, *, host_too: bool) -> TerminalView:
        """The session's terminal an agent means: by id, by title (case aside), or the only one."""
        views = [v for v in await self.list(owner=owner) if host_too or v["env"] != "host"]
        if not views:
            raise NotFound("this session has no terminals")
        if ref:
            for view in views:
                if view["id"] == ref:
                    return view
            named = [v for v in views if str(v["title"]).casefold() == ref.casefold()]
            if not named:
                named = [v for v in views if ref.casefold() in str(v["title"]).casefold()]
            # Two terminals may share a title ("bash · app"); the running one is the one meant, and
            # the list is running-first.
            running = [v for v in named if v["status"] == "running"]
            if len(running) == 1 or (not running and len(named) == 1):
                return (running or named)[0]
            if not named:
                raise NotFound(f"no terminal {ref!r} in this session; it has {self._names(views)}")
            raise InvalidRequest(f"{ref!r} names more than one terminal: {self._names(named)}; give the id")
        running = [v for v in views if v["status"] == "running"]
        if len(views) == 1 or len(running) == 1:
            return (running or views)[0]
        raise InvalidRequest(f"this session has {len(views)} terminals; name one: {self._names(views)}")

    @staticmethod
    def _names(views: Iterable[TerminalView]) -> str:
        return ", ".join(f"{v['id']} ({v['title']}, {v['status']})" for v in views)


__all__ = ["Attachment", "Terminals", "TerminalView", "Waiter", "iso", "now_iso"]
