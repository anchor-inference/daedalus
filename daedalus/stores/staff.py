"""Staff: the named members of a project's team, the sessions they work in, what is said to them,
and the requests they leave waiting.

A staff member is an identity that outlives its sessions: a name, what it is responsible for, which
executor runs it (Daedalus itself or one of the command-line agents), and notes it carries from one
task to the next. A session is one run of work for one task. The database, not a lock, keeps the
rule that a staff member has at most one live session — a partial unique index — so two launches
racing for the same member lose in the one place every process sees.

Every pending decision of a project is a row in ``asks``. Answering is a compare-and-set on that
row: whoever updates it delivers the answer, and every other answer — the app, Telegram, a push
action, the orchestrator — learns who was first.
"""

from __future__ import annotations

import builtins
import json
import re
import secrets
import sqlite3
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.config import REASONING_EFFORTS, StaffConfig
from daedalus.host.worktrees import staff_slug
from daedalus.stores.database import Database

HARNESSES = ("daedalus", "claude", "codex", "grok", "opencode", "pi", "cursor")
"""Who runs a staff member: Daedalus itself, or one of the command-line agents, by the name of its CLI."""
CLI_HARNESSES = HARNESSES[1:]
HARNESS_NAMES = {"daedalus": "Daedalus", "claude": "Claude Code", "codex": "Codex", "grok": "Grok Build", "opencode": "OpenCode", "pi": "pi", "cursor": "Cursor Agent"}
ISOLATIONS = ("shared", "worktree", "readonly")
ENVIRONMENTS = ("container", "host")
CREATORS = ("operator", "orchestrator")

STATUSES = ("starting", "working", "turn_done_unseen", "idle", "question", "permission", "error", "exited", "no_signal")
ACTIVE_STATUSES = frozenset({"starting", "working", "question", "permission", "no_signal"})
"""The statuses that hold one of the project's concurrency slots. A session that finished its turn,
is idle or has failed occupies nothing until it is given work again."""
WAITING_STATUSES = frozenset({"question", "permission"})
SESSION_KINDS = ("daedalus", "cli")

MESSAGE_ORIGINS = ("orchestrator", "operator")
MESSAGE_MODES = ("now", "after_turn", "interrupt")
MESSAGE_STATES = ("queued", "written", "submitted", "acknowledged", "failed")
_MESSAGE_RANK = {"queued": 0, "written": 1, "submitted": 2, "acknowledged": 3}

ASK_ORIGINS = ("staff", "orchestrator", "dispatcher")
ASK_KINDS = ("question", "permission", "folder", "project")
"""``project`` is the main orchestrator's confirmation before it creates a project: the one request
that belongs to no project yet, because nothing exists until it is answered."""
ASK_ROUTES = ("orchestrator", "operator")
ASK_RESOLVERS = ("orchestrator", "operator", "staff", "system")

PALETTE = ("blue", "green", "amber", "violet", "rose", "teal", "orange", "slate")
"""The colours a staff member can wear, by the name of the app's token for each. A colour the app
does not know would draw as nothing, so the store keeps to this list."""

NAME_MAX = 32
ROLE_MAX = 200
INSTRUCTIONS_MAX = 16_000
TEXT_MAX = 8_000
"""A message to a staff member or the text of a request. Longer text belongs in a file the message points at."""
ASK_DETAIL_MAX = 16_000
"""Characters of a request's detail or answer, as JSON written with its letters as they are. Written
escaped, every Cyrillic letter cost six, and an operator's answer of 2,700 Russian characters was
refused although the answer routes accept 4,000."""
TITLE_MAX = 120
"""A request's title: a line in a list, not a second text."""

SHORT_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
"""Crockford's base32: no i, l, o or u, so what a person reads off a lock screen is what they type."""
SHORT_ID_PREFIX = "q"
SHORT_ID_LENGTH = 5
SHORT_ID_ATTEMPTS = 8

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"[A-Za-z0-9._:/@+-]{0,128}")
DAEDALUS_EFFORTS = ("", "off", *REASONING_EFFORTS)


class StaffError(ValueError):
    """A staff member, session or request the store will not keep, with the reason in words."""


class StaffBusy(StaffError):
    """The staff member already has a live session, or is asked to leave while it works."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _short_id() -> str:
    return SHORT_ID_PREFIX + "".join(secrets.choice(SHORT_ID_ALPHABET) for _ in range(SHORT_ID_LENGTH))


def normalise_short_id(text: str) -> str:
    """What a person typed, read the way it was meant: case and the letters base32 leaves out folded away."""
    return (text or "").strip().lower().replace("o", "0").replace("i", "1").replace("l", "1")


def colour_for(name: str) -> str:
    """A stable colour for a name, so the same member looks the same after a restart and on every screen."""
    return PALETTE[zlib.crc32(name.strip().lower().encode("utf-8")) % len(PALETTE)]


def _json(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def daedalus_reaches(env: str, local_env: str) -> bool:
    """Whether a Daedalus session can work in a folder of ``env``: its own environment, or the host
    seen from the container, whose commands and files the host terminal daemon runs for it. Natively
    nothing reaches into the container but a terminal there."""
    return env == local_env or (env == "host" and local_env == "container")


def daedalus_cannot_reach(path: Any, env: str, local_env: str) -> str:
    """Why a Daedalus staff member cannot work in a folder of the other environment, and who can."""
    where = "on the host" if env == "host" else "in the container"
    runs = "inside the agent's container" if local_env == "container" else "on this machine, outside the container"
    terminal = "a host terminal" if env == "host" else "a container terminal"
    return (
        f"{path} is {where}, and a Daedalus staff member runs {runs}, where that folder cannot be reached; "
        f"hire a command-line member ({', '.join(n for k, n in HARNESS_NAMES.items() if k != 'daedalus')}) instead, which works in {terminal}"
    )


def cap_notes(text: str, limit: int) -> str:
    """Notes within ``limit`` characters, dropping the oldest lines first.

    Notes grow at the end — every ``Report(remember=…)`` appends — so the oldest line is the one least
    likely to still be true. A single line longer than the whole budget keeps its end, for the same reason.
    """
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    while lines and len("\n".join(lines)) > limit:
        lines.pop(0)
    kept = "\n".join(lines)
    return kept if lines else text[-limit:]


@dataclass(frozen=True, slots=True)
class Staff:
    id: str
    project_id: str
    name: str
    color: str
    role: str
    harness: str
    agent: str
    model: str
    effort: str
    permission_mode: str
    env: str
    """Empty is the project's default environment."""
    default_folder_id: str | None
    isolation: str
    instructions: str
    notes: str
    one_off: bool
    created_by: str
    created_at: str
    archived_at: str | None

    @property
    def active(self) -> bool:
        return self.archived_at is None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "color": self.color,
            "role": self.role,
            "harness": self.harness,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "permission_mode": self.permission_mode,
            "env": self.env,
            "default_folder_id": self.default_folder_id,
            "isolation": self.isolation,
            "instructions": self.instructions,
            "notes": self.notes,
            "one_off": self.one_off,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "archived_at": self.archived_at,
        }


@dataclass(frozen=True, slots=True)
class StaffSession:
    id: str
    staff_id: str
    kind: str
    session_id: str | None
    terminal_id: str | None
    cli_session_id: str | None
    transcript_ref: str | None
    task_id: str | None
    status: str
    waiting_for: str
    status_at: str
    last_signal_at: str | None
    started_at: str
    ended_at: str | None
    end_reason: str
    predecessor_id: str | None
    folder_id: str | None
    launch_cwd: str | None
    worktree_path: str | None
    branch: str | None
    base_ref: str | None
    pause_requested: bool
    usage: dict[str, Any]

    @property
    def live(self) -> bool:
        return self.ended_at is None

    def view(self) -> dict[str, Any]:
        # The team token's hash is not here on purpose: nothing outside the ingress has a use for it.
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "terminal_id": self.terminal_id,
            "cli_session_id": self.cli_session_id,
            "task_id": self.task_id,
            "status": self.status,
            "waiting_for": self.waiting_for,
            "status_at": self.status_at,
            "last_signal_at": self.last_signal_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "predecessor_id": self.predecessor_id,
            "folder_id": self.folder_id,
            "launch_cwd": self.launch_cwd,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "pause_requested": self.pause_requested,
            "usage": self.usage,
        }


@dataclass(frozen=True, slots=True)
class StaffMessage:
    id: str
    staff_id: str
    staff_session_id: str | None
    origin: str
    text: str
    mode: str
    state: str
    attempts: int
    created_at: str
    updated_at: str
    error: str

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "staff_session_id": self.staff_session_id,
            "origin": self.origin,
            "text": self.text,
            "mode": self.mode,
            "state": self.state,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Ask:
    id: str
    short_id: str
    project_id: str | None
    """``None`` only for the confirmation of a project that does not exist yet."""
    origin: str
    kind: str
    staff_id: str | None
    staff_session_id: str | None
    task_id: str | None
    request_ref: str
    text: str
    detail: dict[str, Any]
    routed_to: str
    suggestion: str
    created_at: str
    routed_at: str
    resolved_at: str | None
    resolved_by: str | None
    resolution: dict[str, Any]
    dispatch_id: str | None = None
    """The main orchestrator's dispatch this request is shown under, in its chat as well as the project's."""
    title: str = ""
    """A few words naming the decision; empty on requests made before titles existed and on staff
    requests, which are shown by :attr:`heading`."""

    @property
    def open(self) -> bool:
        return self.resolved_at is None

    @property
    def heading(self) -> str:
        """The title, or the first line of the text: what a list of requests shows as each one's name."""
        if self.title:
            return self.title
        first = next((line.strip() for line in self.text.splitlines() if line.strip()), "")
        return first if len(first) <= TITLE_MAX else first[: TITLE_MAX - 1].rstrip() + "…"

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_id": self.short_id,
            "project_id": self.project_id,
            "origin": self.origin,
            "kind": self.kind,
            "staff_id": self.staff_id,
            "staff_session_id": self.staff_session_id,
            "task_id": self.task_id,
            "request_ref": self.request_ref,
            "text": self.text,
            "detail": self.detail,
            "routed_to": self.routed_to,
            "suggestion": self.suggestion,
            "created_at": self.created_at,
            "routed_at": self.routed_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "resolution": self.resolution,
            "dispatch_id": self.dispatch_id,
            "title": self.title,
            "heading": self.heading,
        }


def _staff(row: Any) -> Staff:
    return Staff(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        color=row["color"],
        role=row["role"],
        harness=row["harness"],
        agent=row["agent"],
        model=row["model"],
        effort=row["effort"],
        permission_mode=row["permission_mode"],
        env=row["env"],
        default_folder_id=row["default_folder_id"],
        isolation=row["isolation"],
        instructions=row["instructions"],
        notes=row["notes"],
        one_off=bool(row["one_off"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _session(row: Any) -> StaffSession:
    return StaffSession(
        id=row["id"],
        staff_id=row["staff_id"],
        kind=row["kind"],
        session_id=row["session_id"],
        terminal_id=row["terminal_id"],
        cli_session_id=row["cli_session_id"],
        transcript_ref=row["transcript_ref"],
        task_id=row["task_id"],
        status=row["status"],
        waiting_for=row["waiting_for"],
        status_at=row["status_at"],
        last_signal_at=row["last_signal_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        end_reason=row["end_reason"],
        predecessor_id=row["predecessor_id"],
        folder_id=row["folder_id"],
        launch_cwd=row["launch_cwd"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        base_ref=row["base_ref"],
        pause_requested=bool(row["pause_requested"]),
        usage=_json(row["usage_json"]),
    )


def _message(row: Any) -> StaffMessage:
    return StaffMessage(
        id=row["id"],
        staff_id=row["staff_id"],
        staff_session_id=row["staff_session_id"],
        origin=row["origin"],
        text=row["text"],
        mode=row["mode"],
        state=row["state"],
        attempts=int(row["attempts"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row["error"],
    )


def _ask(row: Any) -> Ask:
    return Ask(
        id=row["id"],
        short_id=row["short_id"],
        project_id=row["project_id"],
        origin=row["origin"],
        kind=row["kind"],
        staff_id=row["staff_id"],
        staff_session_id=row["staff_session_id"],
        task_id=row["task_id"],
        request_ref=row["request_ref"],
        text=row["text"],
        detail=_json(row["detail_json"]),
        routed_to=row["routed_to"],
        suggestion=row["suggestion"],
        created_at=row["created_at"],
        routed_at=row["routed_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        resolution=_json(row["resolution_json"]),
        dispatch_id=row["dispatch_id"],
        title=row["title"],
    )


def _plain(value: str, field: str, limit: int, *, multiline: bool = False) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise StaffError(f"{field} is at most {limit} characters")
    if multiline:
        text = text.replace("\r\n", "\n")
        if _CONTROL.search(text.replace("\n", "").replace("\t", "")):
            raise StaffError(f"{field} has control characters in it")
    elif _CONTROL.search(text):
        raise StaffError(f"{field} is one line of text")
    return text


EDITABLE = ("role", "agent", "model", "effort", "permission_mode", "env", "default_folder_id", "isolation", "instructions", "notes", "color")
"""What changes on a staff member after hiring, from its next session on. The name is not here: it is
the member's worktree folder and branch prefix, and a rename would orphan both. Neither is the
executor: a Claude Code member and a Daedalus one keep different transcripts, so changing it is hiring."""


class StaffStore:
    """The team of each project, their sessions and what is said to them."""

    def __init__(
        self,
        db: Database,
        *,
        local_env: str | None = None,
        config: Callable[[], StaffConfig] = StaffConfig,
        presets: Callable[[], Iterable[str]] = tuple,
        personas: Callable[[], Iterable[str]] = tuple,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self.local_env = local_env or db.local_env
        """The environment this process runs in. A Daedalus staff member works only here."""
        self._config = config
        self._presets = presets
        self._personas = personas
        self._clock = clock
        self._signalled: dict[str, float] = {}
        """When each live session's last signal was last written, by this process's clock."""

    def personas(self) -> list[str]:
        """The personas a Daedalus staff member may take, by name."""
        return sorted(self._personas())

    # -- reading -------------------------------------------------------------------

    async def get(self, staff_id: str) -> Staff | None:
        row = await self._db.fetchone("SELECT * FROM staff WHERE id = ?", (staff_id,))
        return _staff(row) if row is not None else None

    async def by_name(self, project_id: str, name: str) -> Staff | None:
        """The active member of this project with this name, ignoring case."""
        row = await self._db.fetchone("SELECT * FROM staff WHERE project_id = ? AND name = ? COLLATE NOCASE AND archived_at IS NULL", (project_id, (name or "").strip()))
        return _staff(row) if row is not None else None

    async def find(self, project_id: str, ref: str, *, dismissed: bool = False) -> Staff | None:
        """A member of this project named by id or by name — the two ways a person or a model refers to one.

        With ``dismissed``, a name no active member has is the latest dismissed member of that name: a
        one-off helper goes from the team on its own, and "nobody called mediafix" was the answer to
        an orchestrator that still had to free the card the helper had left behind.
        """
        found = await self.get(ref)
        if found is not None and found.project_id == project_id:
            return found
        active = await self.by_name(project_id, ref)
        if active is not None or not dismissed:
            return active
        row = await self._db.fetchone(
            "SELECT * FROM staff WHERE project_id = ? AND name = ? COLLATE NOCASE AND archived_at IS NOT NULL ORDER BY archived_at DESC LIMIT 1",
            (project_id, (ref or "").strip()),
        )
        return _staff(row) if row is not None else None

    async def list(self, project_id: str, archived: bool = False) -> list[Staff]:
        """The project's team in the order it was hired; with ``archived``, the dismissed as well."""
        where = "" if archived else " AND archived_at IS NULL"
        rows = await self._db.fetchall(f"SELECT * FROM staff WHERE project_id = ?{where} ORDER BY created_at, rowid", (project_id,))
        return [_staff(r) for r in rows]

    async def live_sessions(self, project_id: str) -> dict[str, StaffSession]:
        """``{staff_id: live session}`` for every member of the project who has one, in one query."""
        rows = await self._db.fetchall(
            "SELECT s.* FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE m.project_id = ? AND s.ended_at IS NULL",
            (project_id,),
        )
        return {r["staff_id"]: _session(r) for r in rows}

    async def team_counts(self) -> dict[str, dict[str, int]]:
        """``{project_id: {staff, working}}`` for every project with a team, in two queries.

        The agents list draws an orchestrated project as one entry that says how many work in it, and
        it lists every project on every poll: asking each project in turn would be a query per project.
        """
        out: dict[str, dict[str, int]] = {}
        for row in await self._db.fetchall("SELECT project_id, COUNT(*) AS n FROM staff WHERE archived_at IS NULL GROUP BY project_id"):
            out.setdefault(row["project_id"], {"staff": 0, "working": 0})["staff"] = int(row["n"])
        active = ",".join("?" * len(ACTIVE_STATUSES))
        rows = await self._db.fetchall(
            f"SELECT m.project_id, COUNT(*) AS n FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE s.ended_at IS NULL AND s.status IN ({active}) GROUP BY m.project_id",
            tuple(sorted(ACTIVE_STATUSES)),
        )
        for row in rows:
            out.setdefault(row["project_id"], {"staff": 0, "working": 0})["working"] = int(row["n"])
        return out

    async def live_by_harness(self, harness: str) -> builtins.list[dict[str, Any]]:
        """Every member running on ``harness`` who has a live session, across all projects, with the
        environment the CLI runs in: the member's own, else the project's default, else this
        process's. The harness manager refuses to update a CLI under anyone listed here."""
        rows = await self._db.fetchall(
            "SELECT m.id, m.name, m.env, m.project_id, p.name AS project_name, p.settings, s.id AS staff_session_id, s.status "
            "FROM staff_sessions s JOIN staff m ON m.id = s.staff_id JOIN projects p ON p.id = m.project_id "
            "WHERE m.harness = ? AND s.ended_at IS NULL ORDER BY p.name, m.name",
            (harness,),
        )
        out = []
        for row in rows:
            default = str(_json(row["settings"]).get("default_env") or "")
            env = row["env"] or (default if default in ENVIRONMENTS else "") or self.local_env
            out.append({
                "staff_id": row["id"], "name": row["name"], "project_id": row["project_id"], "project": row["project_name"],
                "env": env, "staff_session_id": row["staff_session_id"], "status": row["status"],
            })
        return out

    async def session_counts(self, project_id: str) -> dict[str, int]:
        rows = await self._db.fetchall(
            "SELECT s.staff_id, COUNT(*) AS n FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE m.project_id = ? GROUP BY s.staff_id",
            (project_id,),
        )
        return {r["staff_id"]: int(r["n"]) for r in rows}

    # -- hiring, editing, dismissing ----------------------------------------------------

    async def _project(self, project_id: str) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
        """The project's settings, its name, and its folders by id — what every check below reads."""
        row = await self._db.fetchone("SELECT name, settings, system FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise KeyError(project_id)
        settings = _json(row["settings"])
        if row["system"] or settings.get("system"):
            raise StaffError(f"{row['name']} is the installation's own project and has no team")
        if settings.get("ephemeral"):
            raise StaffError(f"{row['name']} is a chat's own project; keep it as a project before hiring a team for it")
        folders = await self._db.fetchall("SELECT id, path, env, is_git, readonly, position FROM project_folders WHERE project_id = ? ORDER BY position", (project_id,))
        return settings, row["name"], {f["id"]: dict(f) for f in folders}

    def _check(self, fields: dict[str, Any], settings: dict[str, Any], folders: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """The fields as they will be stored, or the first reason they cannot be."""
        out = dict(fields)
        harness = out["harness"]
        if harness not in HARNESSES:
            raise StaffError(f"a staff member runs on {', '.join(HARNESSES)}, not {harness!r}")
        if out["isolation"] not in ISOLATIONS:
            raise StaffError(f"isolation is shared, worktree or readonly, not {out['isolation']!r}")
        if out["env"] not in ("", *ENVIRONMENTS):
            raise StaffError(f"a staff member runs in the container or on the host, not {out['env']!r}")
        out["role"] = _plain(out["role"], "the responsibility", ROLE_MAX)
        out["instructions"] = _plain(out["instructions"], "the instructions", INSTRUCTIONS_MAX, multiline=True)
        out["notes"] = cap_notes(_plain(out["notes"], "the notes", 10 * self._config().notes_max_chars, multiline=True), self._config().notes_max_chars)
        for name in ("agent", "model", "effort", "permission_mode"):
            value = (out[name] or "").strip()
            if not _TOKEN.fullmatch(value):
                raise StaffError(f"{name.replace('_', ' ')} {value!r} is not a name this store keeps (letters, digits and ._:/@+- only)")
            out[name] = value
        colour = (out["color"] or "").strip()
        if colour and colour not in PALETTE:
            raise StaffError(f"a staff member's colour is one of {', '.join(PALETTE)}, not {colour!r}")
        out["color"] = colour or colour_for(out["name"])
        if harness == "daedalus":
            if out["model"] and out["model"] not in set(self._presets()):
                # The project's orchestrator model is not a fallback here: empty means the installation's default.
                raise StaffError(f"{out['model']!r} is not a model preset of this installation; leave it empty for the default")
            out["agent"] = out["agent"].lower()
            if out["agent"] and out["agent"] not in set(self._personas()):
                raise StaffError(f"there is no persona called {out['agent']!r}")
            if out["effort"] not in DAEDALUS_EFFORTS:
                raise StaffError(f"a Daedalus staff member's effort is one of {', '.join(e or 'default' for e in DAEDALUS_EFFORTS)}")
            if out["permission_mode"]:
                raise StaffError("a permission mode is a command-line agent's setting; a Daedalus member follows the project's autonomy")
            if out["env"] and not daedalus_reaches(out["env"], self.local_env):
                raise StaffError(f"a Daedalus staff member works where Daedalus runs ({self.local_env}), not on the {out['env']}")
        env = out["env"] or str(settings.get("default_env") or self.local_env)
        folder_id = out["default_folder_id"] or None
        out["default_folder_id"] = folder_id
        if folder_id is not None and folder_id not in folders:
            raise StaffError("that folder is not one of this project's")
        folder = folders[folder_id] if folder_id is not None else next(iter(folders.values()), None)
        if harness == "daedalus":
            # A Daedalus member works where its folder is, when it can reach it at all: in the
            # container's own folders, and on the host through the host terminal daemon.
            if out["env"] and folder is not None and out["env"] != folder["env"]:
                raise StaffError(f"a Daedalus staff member works where its folder is ({folder['env']}), not on the {out['env']}")
            env = folder["env"] if folder is not None and daedalus_reaches(folder["env"], self.local_env) else self.local_env
        if folder is not None:
            where = "on the host" if folder["env"] == "host" else "in the container"
            if folder["env"] != env and harness == "daedalus":
                # A Daedalus member is a session of this process and cannot be given a working
                # directory it cannot reach: its first turn would be refused, and a workspace of its
                # own would hold none of the project's files. The member who can work there is a
                # command-line one in a terminal of that environment.
                raise StaffError(daedalus_cannot_reach(folder["path"], folder["env"], self.local_env))
            if folder["env"] != env:
                raise StaffError(f"{folder['path']} is {where}, and this staff member would run {'on the host' if env == 'host' else 'in the container'}")
            if out["isolation"] == "worktree" and folder["readonly"]:
                raise StaffError(f"{folder['path']} is read-only, so no worktree can be made in it; choose shared or read-only")
            if out["isolation"] == "worktree" and folder["env"] == self.local_env and not folder["is_git"]:
                raise StaffError(f"{folder['path']} is not a git repository, so there is no worktree to give; choose shared or read-only")
        return out

    async def hire(
        self,
        project_id: str,
        *,
        name: str,
        role: str = "",
        harness: str = "daedalus",
        agent: str = "",
        model: str = "",
        effort: str = "",
        permission_mode: str = "",
        env: str = "",
        folder_id: str | None = None,
        isolation: str = "worktree",
        instructions: str = "",
        notes: str = "",
        one_off: bool = False,
        color: str = "",
        created_by: str = "operator",
    ) -> Staff:
        """A new member of the project's team; the hire is written to the project's journal with it."""
        label = _plain(name, "a name", NAME_MAX)
        if not label:
            raise StaffError("a staff member needs a name")
        if created_by not in CREATORS:
            raise StaffError(f"staff are hired by the operator or the orchestrator, not {created_by!r}")
        settings, _, folders = await self._project(project_id)
        fields = self._check(
            {
                "name": label, "role": role, "harness": harness, "agent": agent, "model": model, "effort": effort,
                "permission_mode": permission_mode, "env": env, "default_folder_id": folder_id, "isolation": isolation,
                "instructions": instructions, "notes": notes, "color": color,
            },
            settings,
            folders,
        )
        staff = Staff(id=f"st-{uuid.uuid4().hex[:12]}", project_id=project_id, one_off=bool(one_off), created_by=created_by, created_at=_now(), archived_at=None, **fields)
        who = "The operator" if created_by == "operator" else "The orchestrator"
        text = f"{who} hired {staff.name} ({HARNESS_NAMES[staff.harness]}{', one-off' if staff.one_off else ''}){': ' + staff.role if staff.role else ''}"
        slug = staff_slug(staff.name)
        try:
            async with self._db.transaction() as conn:
                # The slug names the member's worktree and branch, and two names can share one ("Anna"
                # and "Анна"). Checked inside the write transaction, so a second hire cannot slip in
                # between the check and the insert.
                cursor = await conn.execute("SELECT name FROM staff WHERE project_id = ? AND archived_at IS NULL", (project_id,))
                taken = [r["name"] for r in await cursor.fetchall()]
                await cursor.close()
                clash = next((n for n in taken if staff_slug(n) == slug and n.lower() != label.lower()), None)
                if clash is not None:
                    raise StaffError(f"{label} would share the worktree and branch name {slug!r} with {clash}; choose another name")
                await conn.execute(
                    "INSERT INTO staff(id, project_id, name, color, role, harness, agent, model, effort, permission_mode, env, default_folder_id, isolation, instructions, notes, one_off, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        staff.id, project_id, staff.name, staff.color, staff.role, staff.harness, staff.agent, staff.model, staff.effort,
                        staff.permission_mode, staff.env, staff.default_folder_id, staff.isolation, staff.instructions, staff.notes,
                        int(staff.one_off), staff.created_by, staff.created_at,
                    ),
                )
                await conn.execute("INSERT INTO staff_role_versions(staff_id,role_revision,purpose,authority_json,"
                                   " output_contract_json,created_at) VALUES (?,1,?,'{}','{}',?)",
                                   (staff.id, staff.role, staff.created_at))
                await self._journal(conn, project_id, "hire", text, {"staff_id": staff.id})
        except sqlite3.IntegrityError as exc:
            # The partial unique index over active names: the check is the database's, so two hires
            # of one name racing each other cannot both land.
            if await self.by_name(project_id, label) is not None:
                raise StaffError(f"the team already has someone called {label}") from exc
            raise
        return staff

    async def update(self, staff_id: str, **changes: Any) -> Staff:
        """Change what may change after hiring (``EDITABLE``); it takes effect from the next session."""
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise StaffError(f"{', '.join(unknown)} cannot be changed after hiring")
        current = await self.get(staff_id)
        if current is None:
            raise KeyError(staff_id)
        if not current.active:
            raise StaffError(f"{current.name} has been dismissed")
        settings, _, folders = await self._project(current.project_id)
        merged = {k: getattr(current, k) for k in ("name", "harness", *EDITABLE)}
        merged.update({k: v for k, v in changes.items() if v is not None})
        if "default_folder_id" in changes and changes["default_folder_id"] == "":
            merged["default_folder_id"] = None
        fields = self._check(merged, settings, folders)
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE staff SET role = ?, agent = ?, model = ?, effort = ?, permission_mode = ?, env = ?, default_folder_id = ?, isolation = ?, instructions = ?, notes = ?, color = ? WHERE id = ?",
                tuple(fields[k] for k in EDITABLE) + (staff_id,),
            )
            if fields["role"] != current.role:
                row = await conn.execute("SELECT role_revision,authority_json,output_contract_json FROM staff WHERE id = ?",
                                         (staff_id,))
                old = await row.fetchone()
                await row.close()
                revision = int(old["role_revision"]) + 1
                await conn.execute("UPDATE staff SET role_revision = ?,purpose = ? WHERE id = ?",
                                   (revision, fields["role"], staff_id))
                await conn.execute("INSERT INTO staff_role_versions(staff_id,role_revision,purpose,authority_json,"
                                   " output_contract_json,created_at) VALUES (?,?,?,?,?,?)",
                                   (staff_id, revision, fields["role"], old["authority_json"],
                                    old["output_contract_json"], _now()))
        updated = await self.get(staff_id)
        assert updated is not None
        return updated

    # -- standing grants -------------------------------------------------------------

    async def allow_rules(self, staff_id: str) -> builtins.list[dict[str, str]]:
        """The member's standing grants, oldest first: ``{rule, created_at, created_by}``."""
        rows = await self._db.fetchall("SELECT rule, created_at, created_by FROM staff_allow_rules WHERE staff_id = ? ORDER BY created_at, rule", (staff_id,))
        return [{"rule": r["rule"], "created_at": r["created_at"], "created_by": r["created_by"]} for r in rows]

    async def grant_rule(self, staff_id: str, rule: str, *, by: str = "operator") -> bool:
        """Keep an allow rule for the member; ``False`` when it was already kept."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute("INSERT OR IGNORE INTO staff_allow_rules(staff_id, rule, created_at, created_by) VALUES (?, ?, ?, ?)", (staff_id, rule, _now(), by))
            changed = cursor.rowcount
            await cursor.close()
        return bool(changed)

    async def revoke_rule(self, staff_id: str, rule: str) -> bool:
        """Drop an allow rule; ``False`` when the member had none such. A live session keeps what it
        was granted until it ends: the CLI takes no rule back from outside."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM staff_allow_rules WHERE staff_id = ? AND rule = ?", (staff_id, rule))
            changed = cursor.rowcount
            await cursor.close()
        return bool(changed)

    async def archive(self, staff_id: str, *, by: str = "operator") -> Staff:
        """Dismiss a member: the row stays (sessions and spend point at it) and its name is free again.

        Refused while a session of theirs is live — ending one is the runtime's to do, since only it
        knows how to stop what is running there.
        """
        current = await self.get(staff_id)
        if current is None:
            raise KeyError(staff_id)
        if not current.active:
            return current
        at = _now()
        who = "The operator" if by == "operator" else "The orchestrator" if by == "orchestrator" else "The system"
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE staff SET archived_at = ? WHERE id = ? AND archived_at IS NULL AND NOT EXISTS (SELECT 1 FROM staff_sessions WHERE staff_id = ? AND ended_at IS NULL)",
                (at, staff_id, staff_id),
            )
            changed = cursor.rowcount
            await cursor.close()
            if changed:
                await self._journal(conn, current.project_id, "dismiss", f"{who} dismissed {current.name}", {"staff_id": staff_id})
        if not changed:
            raise StaffBusy(f"{current.name} is working; release the session first")
        archived = await self.get(staff_id)
        assert archived is not None
        return archived

    async def append_notes(self, staff_id: str, text: str) -> str:
        """Add a line to what the member carries between sessions; the oldest lines go past the cap."""
        line = _plain(text, "a note", TEXT_MAX, multiline=True)
        if not line:
            current = await self.get(staff_id)
            if current is None:
                raise KeyError(staff_id)
            return current.notes
        limit = self._config().notes_max_chars
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT notes FROM staff WHERE id = ?", (staff_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(staff_id)
            notes = cap_notes(f"{row['notes']}\n{line}" if row["notes"] else line, limit)
            await conn.execute("UPDATE staff SET notes = ? WHERE id = ?", (notes, staff_id))
        return notes

    @staticmethod
    async def _journal(conn: Any, project_id: str, kind: str, text: str, refs: dict[str, Any]) -> None:
        """A system entry in the same transaction as the change it records, so neither lands without the other."""
        await conn.execute(
            "INSERT INTO project_journal(project_id, at, author, kind, text, refs_json) VALUES (?, ?, 'system', ?, ?, ?)",
            (project_id, _now(), kind, text, json.dumps(refs)),
        )

    # -- sessions ------------------------------------------------------------------

    async def claim_session(
        self,
        staff_id: str,
        *,
        kind: str,
        task_id: str | None = None,
        folder_id: str | None = None,
        launch_cwd: str | None = None,
        worktree_path: str | None = None,
        branch: str | None = None,
        base_ref: str | None = None,
        predecessor_id: str | None = None,
        team_token_hash: str = "",
    ) -> StaffSession:
        """Open the member's one live session, or raise :class:`StaffBusy` when there already is one.

        The refusal is the database's partial unique index, not a check made first: a check and an
        insert are two statements, and two launches can both pass the check.
        """
        if kind not in SESSION_KINDS:
            raise StaffError(f"a staff session is daedalus or cli, not {kind!r}")
        staff = await self.get(staff_id)
        if staff is None:
            raise KeyError(staff_id)
        if not staff.active:
            raise StaffError(f"{staff.name} has been dismissed")
        at = _now()
        session_id = f"ss-{uuid.uuid4().hex[:12]}"
        try:
            await self._db.execute(
                "INSERT INTO staff_sessions(id, staff_id, kind, task_id, status, status_at, last_signal_at, started_at, predecessor_id, folder_id, launch_cwd, worktree_path, branch, base_ref, team_token_hash) "
                "VALUES (?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, staff_id, kind, task_id, at, at, at, predecessor_id, folder_id, launch_cwd, worktree_path, branch, base_ref, team_token_hash),
            )
        except sqlite3.IntegrityError as exc:
            if await self.live(staff_id) is not None:
                raise StaffBusy(f"{staff.name} already has a live session") from exc
            raise
        self._signalled[session_id] = self._clock()
        claimed = await self.session(session_id)
        assert claimed is not None
        return claimed

    async def session(self, staff_session_id: str) -> StaffSession | None:
        row = await self._db.fetchone("SELECT * FROM staff_sessions WHERE id = ?", (staff_session_id,))
        return _session(row) if row is not None else None

    async def live(self, staff_id: str) -> StaffSession | None:
        row = await self._db.fetchone("SELECT * FROM staff_sessions WHERE staff_id = ? AND ended_at IS NULL", (staff_id,))
        return _session(row) if row is not None else None

    async def sessions(self, staff_id: str, *, limit: int = 50) -> list[StaffSession]:
        """The member's sessions, newest first."""
        rows = await self._db.fetchall("SELECT * FROM staff_sessions WHERE staff_id = ? ORDER BY started_at DESC, rowid DESC LIMIT ?", (staff_id, max(1, min(int(limit), 500))))
        return [_session(r) for r in rows]

    async def resumable_sessions(self, project_id: str, harness: str, folder_id: str, launch_cwd: str, *, worktree_path: str | None = None, before: str | None = None, limit: int = 50) -> list[tuple[StaffSession, str, str]]:
        """CLI conversations launched from this folder, including dismissed members' sessions."""
        folder_match = "(s.launch_cwd = ? OR (s.launch_cwd IS NULL AND s.folder_id = ?)"
        folder_args: tuple[Any, ...] = (launch_cwd, folder_id)
        if worktree_path:
            folder_match += " OR s.worktree_path = ?"
            folder_args += (worktree_path,)
        folder_match += ")"
        cursor = await self._db.fetchone(
            "SELECT s.started_at, s.rowid FROM staff_sessions s JOIN staff m ON m.id = s.staff_id "
            "WHERE s.id = ? AND m.project_id = ? AND m.harness = ? AND " + folder_match,
            (before, project_id, harness, *folder_args),
        ) if before else None
        if before and cursor is None:
            raise StaffError("the session cursor is not in this project, harness and launch folder")
        where = " AND (s.started_at < ? OR (s.started_at = ? AND s.rowid < ?))" if cursor else ""
        args: tuple[Any, ...] = (project_id, harness, *folder_args)
        if cursor:
            args += (cursor["started_at"], cursor["started_at"], cursor["rowid"])
        rows = await self._db.fetchall(
            "SELECT s.*, m.name AS owner_name, COALESCE(t.title, '') AS task_title "
            "FROM staff_sessions s JOIN staff m ON m.id = s.staff_id "
            "LEFT JOIN board_tasks t ON t.id = s.task_id "
            "WHERE m.project_id = ? AND m.harness = ? AND " + folder_match + " "
            "AND s.kind = 'cli' AND s.cli_session_id IS NOT NULL" + where +
            " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?",
            (*args, max(1, min(int(limit), 100))),
        )
        return [(_session(row), row["owner_name"], row["task_title"]) for row in rows]

    async def started(self, staff_session_id: str, *, session_id: str | None = None, terminal_id: str | None = None, cli_session_id: str | None = None, transcript_ref: str | None = None) -> StaffSession | None:
        """Record where the runtime put the work: the Daedalus session, or the terminal and the CLI's own session."""
        await self._db.execute(
            "UPDATE staff_sessions SET session_id = COALESCE(?, session_id), terminal_id = COALESCE(?, terminal_id), cli_session_id = COALESCE(?, cli_session_id), transcript_ref = COALESCE(?, transcript_ref) WHERE id = ?",
            (session_id, terminal_id, cli_session_id, transcript_ref, staff_session_id),
        )
        return await self.session(staff_session_id)

    async def retask(self, staff_session_id: str, task_id: str) -> StaffSession | None:
        """Give a live session its next task; ``None`` when the session is no longer live. A
        command-line member keeps its terminal and its conversation from one task to the next, and
        everything that reads the session's task — its requests, its reports, the silence watch —
        follows the row, so the row is what moves."""
        await self._db.execute("UPDATE staff_sessions SET task_id = ? WHERE id = ? AND ended_at IS NULL", (task_id, staff_session_id))
        session = await self.session(staff_session_id)
        return session if session is not None and session.live else None

    async def end_session(self, staff_session_id: str, reason: str) -> StaffSession | None:
        """End once, including an observed CLI exit whose final screen reason has not arrived yet."""
        at = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE staff_sessions SET ended_at = COALESCE(ended_at,?), end_reason = ?, status = 'exited',"
                " status_at = ?, waiting_for = '' WHERE id = ? AND (ended_at IS NULL OR"
                " (kind = 'cli' AND end_reason = '' AND EXISTS (SELECT 1 FROM runtime_exit_observations x"
                " WHERE x.staff_session_id = staff_sessions.id AND x.runtime_kind = 'cli'))) ",
                (at, _plain(reason, "the reason", 500), at, staff_session_id),
            )
            changed = cursor.rowcount
            await cursor.close()
        self._signalled.pop(staff_session_id, None)
        return await self.session(staff_session_id) if changed else None

    async def set_status(self, staff_session_id: str, status: str, waiting_for: str = "") -> tuple[str, StaffSession] | None:
        """Move a live session to ``status``; returns the status it had and the session as it is now.

        ``waiting_for`` is kept only for the two statuses that wait on someone; any other status
        clears it, so a stale "waiting for an answer" cannot outlive the question.
        """
        if status not in STATUSES:
            raise StaffError(f"a staff session's status is one of {', '.join(STATUSES)}, not {status!r}")
        if status == "exited":
            raise StaffError("a session exits through end_session, which records why")
        waiting = _plain(waiting_for, "what it waits for", 500) if status in WAITING_STATUSES else ""
        at = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT status FROM staff_sessions WHERE id = ? AND ended_at IS NULL", (staff_session_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            await conn.execute(
                "UPDATE staff_sessions SET status = ?, waiting_for = ?, status_at = ?, last_signal_at = ? WHERE id = ?",
                (status, waiting, at, at, staff_session_id),
            )
        self._signalled[staff_session_id] = self._clock()
        session = await self.session(staff_session_id)
        assert session is not None
        return row["status"], session

    async def touch(self, staff_session_id: str) -> bool:
        """Note a sign of life; written at most once per ``signal_write_seconds``. True when it was written."""
        now = self._clock()
        last = self._signalled.get(staff_session_id)
        if last is not None and now - last < self._config().signal_write_seconds:
            return False
        self._signalled[staff_session_id] = now
        await self._db.execute("UPDATE staff_sessions SET last_signal_at = ? WHERE id = ? AND ended_at IS NULL", (_now(), staff_session_id))
        return True

    async def by_session(self, session_id: str) -> StaffSession | None:
        """The staff session a Daedalus session belongs to, live or ended; ``None`` for any other session."""
        row = await self._db.fetchone("SELECT * FROM staff_sessions WHERE session_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (session_id,))
        return _session(row) if row is not None else None

    async def all_live(self) -> list[StaffSession]:
        """Every live staff session of every project: what a host rebuilds its live map from at start."""
        return [_session(r) for r in await self._db.fetchall("SELECT * FROM staff_sessions WHERE ended_at IS NULL ORDER BY started_at")]

    async def team_token_hash(self, staff_session_id: str) -> str:
        row = await self._db.fetchone("SELECT team_token_hash FROM staff_sessions WHERE id = ?", (staff_session_id,))
        return str(row["team_token_hash"]) if row is not None else ""

    async def request_pause(self, staff_session_id: str, requested: bool = True) -> bool:
        """Ask a live session to stop after its turn; True when the session was live."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute("UPDATE staff_sessions SET pause_requested = ? WHERE id = ? AND ended_at IS NULL", (int(requested), staff_session_id))
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def record_usage(self, staff_session_id: str, usage: dict[str, Any]) -> None:
        """What the session has spent, as its runtime last reported it."""
        await self._db.execute("UPDATE staff_sessions SET usage_json = ? WHERE id = ?", (json.dumps(usage), staff_session_id))

    async def chain(self, staff_session_id: str, *, limit: int = 50) -> list[StaffSession]:
        """The session and the ones it replaced, newest first, following ``predecessor_id``.

        Bounded, and a loop — a row edited by hand to point at its own successor — stops the walk
        instead of spinning it.
        """
        out: list[StaffSession] = []
        seen: set[str] = set()
        current: str | None = staff_session_id
        while current and current not in seen and len(out) < limit:
            seen.add(current)
            found = await self.session(current)
            if found is None:
                break
            out.append(found)
            current = found.predecessor_id
        return out

    # -- messages ------------------------------------------------------------------

    async def add_message(self, staff_id: str, text: str, *, origin: str, mode: str,
                          staff_session_id: str | None = None, message_id: str | None = None) -> StaffMessage:
        if origin not in MESSAGE_ORIGINS:
            raise StaffError(f"a message to staff comes from the orchestrator or the operator, not {origin!r}")
        if mode not in MESSAGE_MODES:
            raise StaffError(f"a message goes in now, after the turn or with an interrupt ({', '.join(MESSAGE_MODES)}), not {mode!r}")
        body = _plain(text, "a message", TEXT_MAX, multiline=True)
        if not body:
            raise StaffError("a message needs text")
        at = _now()
        if message_id is not None and not re.fullmatch(r"sm-[0-9a-f]{32}", message_id):
            raise StaffError("the host message identity is invalid")
        message = StaffMessage(message_id or f"sm-{uuid.uuid4().hex[:12]}", staff_id, staff_session_id, origin, body, mode, "queued", 0, at, at, "")
        await self._db.execute(
            "INSERT INTO staff_messages(id, staff_id, staff_session_id, origin, text, mode, state, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
            (message.id, staff_id, staff_session_id, origin, body, mode, at, at),
        )
        return message

    async def message(self, message_id: str) -> StaffMessage | None:
        row = await self._db.fetchone("SELECT * FROM staff_messages WHERE id = ?", (message_id,))
        return _message(row) if row is not None else None

    async def messages(self, staff_id: str, *, limit: int = 50) -> list[StaffMessage]:
        rows = await self._db.fetchall("SELECT * FROM staff_messages WHERE staff_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (staff_id, max(1, min(int(limit), 500))))
        return [_message(r) for r in rows]

    async def set_message_state(self, message_id: str, state: str, error: str = "") -> StaffMessage | None:
        """Advance a message's delivery receipt. Returns the message as stored afterwards.

        Receipts only move forward — queued, written, submitted, acknowledged — because they arrive
        from different channels and out of order: a late "written" must not undo an "acknowledged".
        ``failed`` can end anything not yet acknowledged, and a failed message may be queued again.
        Every move to written or submitted counts as a delivery attempt.
        """
        if state not in MESSAGE_STATES:
            raise StaffError(f"a message's state is one of {', '.join(MESSAGE_STATES)}, not {state!r}")
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT state FROM staff_messages WHERE id = ?", (message_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            old = row["state"]
            if state == "failed":
                allowed = old != "acknowledged"
            elif old == "failed":
                allowed = state == "queued"
            else:
                allowed = _MESSAGE_RANK[state] > _MESSAGE_RANK[old]
            if allowed:
                attempt = 1 if state in ("written", "submitted") and old in ("queued", "failed") else 0
                await conn.execute(
                    "UPDATE staff_messages SET state = ?, attempts = attempts + ?, updated_at = ?, error = ? WHERE id = ?",
                    (state, attempt, _now(), _plain(error, "the error", 2000, multiline=True) if state == "failed" else "", message_id),
                )
        return await self.message(message_id)


class AsksStore:
    """Every pending decision in every project: one row each, answered once."""

    def __init__(self, db: Database, *, short_id: Callable[[], str] = _short_id) -> None:
        self._db = db
        self._short_id = short_id
        self.default_dispatch: Callable[[str], Awaitable[str | None]] | None = None
        """``project id -> dispatch id`` asked for a request that names no dispatch: while the main
        orchestrator sets a project up, every request of the project belongs to that first dispatch.
        Asked here, where every request is made, so no way of asking can slip past it."""

    async def open(
        self,
        project_id: str | None,
        *,
        origin: str,
        kind: str,
        text: str,
        routed_to: str,
        staff_id: str | None = None,
        staff_session_id: str | None = None,
        task_id: str | None = None,
        request_ref: str = "",
        detail: dict[str, Any] | None = None,
        suggestion: str = "",
        dispatch_id: str | None = None,
        title: str = "",
    ) -> Ask:
        """A new request, with a short id nobody else waiting holds.

        The short id is random, so two open requests can draw the same one; the partial unique
        index refuses the second, and the insert is tried again with a fresh draw. The dispatch link
        is written in the same insert, before anyone announces the request, so every window that
        shows it learns where it belongs from the row itself.
        """
        if project_id is None and kind != "project":
            raise StaffError("only the confirmation of a new project belongs to no project")
        if dispatch_id is None and project_id is not None and self.default_dispatch is not None:
            dispatch_id = await self.default_dispatch(project_id)
        if origin not in ASK_ORIGINS:
            raise StaffError(f"a request comes from staff or the orchestrator, not {origin!r}")
        if kind not in ASK_KINDS:
            raise StaffError(f"a request is a question, a permission or a folder, not {kind!r}")
        if routed_to not in ASK_ROUTES:
            raise StaffError(f"a request goes to the orchestrator or the operator, not {routed_to!r}")
        body = _plain(text, "the request", TEXT_MAX, multiline=True)
        if not body:
            raise StaffError("a request needs text")
        heading = _plain(title, "the title", TITLE_MAX)
        details = json.dumps(detail or {}, ensure_ascii=False)
        if len(details) > ASK_DETAIL_MAX:
            raise StaffError(f"a request's detail is at most {ASK_DETAIL_MAX} characters of JSON")
        at = _now()
        ask_id = f"ask-{uuid.uuid4().hex[:12]}"
        for _ in range(SHORT_ID_ATTEMPTS):
            short = self._short_id()
            try:
                await self._db.execute(
                    "INSERT INTO asks(id, short_id, project_id, origin, kind, staff_id, staff_session_id, task_id, request_ref, text, detail_json, routed_to, suggestion, created_at, routed_at, dispatch_id, title, origin_contract_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " (SELECT contract_revision FROM board_tasks WHERE id = ?))",
                    (ask_id, short, project_id, origin, kind, staff_id, staff_session_id, task_id, _plain(request_ref, "the request reference", 500), body, details, routed_to, _plain(suggestion, "the suggestion", TEXT_MAX, multiline=True), at, at, dispatch_id or None, heading, task_id),
                )
            except sqlite3.IntegrityError:
                if await self._db.fetchone("SELECT 1 FROM asks WHERE short_id = ? AND resolved_at IS NULL", (short,)) is None:
                    raise
                continue
            made = await self.get(ask_id)
            assert made is not None
            return made
        raise StaffError(f"no free short id after {SHORT_ID_ATTEMPTS} draws")

    async def get(self, ref: str) -> Ask | None:
        """A request by its id, or an open one by the short id a person typed."""
        row = await self._db.fetchone("SELECT * FROM asks WHERE id = ?", (ref,))
        if row is None:
            row = await self._db.fetchone("SELECT * FROM asks WHERE short_id = ? AND resolved_at IS NULL", (normalise_short_id(ref),))
        return _ask(row) if row is not None else None

    async def resolve(self, ask_id: str, by: str, resolution: dict[str, Any] | None = None) -> bool:
        """Answer a request. True for the one answer that got there first; False for every later one."""
        if by not in ASK_RESOLVERS:
            raise StaffError(f"a request is answered by the orchestrator, the operator, staff or the system, not {by!r}")
        async with self._db.transaction() as conn:
            row = await conn.execute("SELECT a.task_id,a.origin_contract_revision,t.contract_revision"
                                     " FROM asks a LEFT JOIN board_tasks t ON t.id = a.task_id WHERE a.id = ?", (ask_id,))
            ask = await row.fetchone()
            await row.close()
            applies = ask is not None and (ask["task_id"] is None or
                                           (ask["origin_contract_revision"] is not None and
                                            ask["origin_contract_revision"] == ask["contract_revision"]))
            # A stale answer still closes the request. Returning false after writing it made the
            # operator see "already answered" and hid the fact that the old contract cannot use it.
            body = json.dumps({**(resolution or {}), **({"applies": False} if not applies else {})}, ensure_ascii=False)
            if len(body) > ASK_DETAIL_MAX:
                raise StaffError(f"an answer is at most {ASK_DETAIL_MAX} characters of JSON")
            cursor = await conn.execute(
                "UPDATE asks SET resolved_at = ?, resolved_by = ?, resolution_json = ?,"
                " answered_contract_revision = ? WHERE id = ? AND resolved_at IS NULL",
                (_now(), by, body, ask["contract_revision"] if applies and ask["task_id"] else None, ask_id),
            )
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def revise(self, ask_id: str, *, title: str, text: str, detail: dict[str, Any]) -> bool:
        """Change an open request's words in place: the same row, the same id, so an answer the operator
        has started to write stays with it. False when it was answered or withdrawn meanwhile."""
        body = json.dumps(detail, ensure_ascii=False)
        if len(body) > ASK_DETAIL_MAX:
            raise StaffError(f"a request's detail is at most {ASK_DETAIL_MAX} characters of JSON")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE asks SET title = ?, text = ?, detail_json = ? WHERE id = ? AND resolved_at IS NULL",
                (_plain(title, "the title", TITLE_MAX), _plain(text, "the text", TEXT_MAX, multiline=True), body, ask_id),
            )
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def route(self, ask_id: str, to: str, suggestion: str = "") -> bool:
        """Send an open request to the orchestrator or the operator; the orchestrator's suggestion travels with it."""
        if to not in ASK_ROUTES:
            raise StaffError(f"a request goes to the orchestrator or the operator, not {to!r}")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE asks SET routed_to = ?, routed_at = ?, suggestion = CASE WHEN ? = '' THEN suggestion ELSE ? END WHERE id = ? AND resolved_at IS NULL",
                (to, _now(), suggestion, _plain(suggestion, "the suggestion", TEXT_MAX, multiline=True), ask_id),
            )
            changed = cursor.rowcount
            await cursor.close()
        return changed == 1

    async def open_for(self, project_id: str, routed_to: str | None = None) -> list[Ask]:
        """The project's open requests, oldest first; with ``routed_to='operator'`` this is "Needs you"."""
        if routed_to is None:
            rows = await self._db.fetchall("SELECT * FROM asks WHERE project_id = ? AND resolved_at IS NULL ORDER BY created_at, rowid", (project_id,))
        else:
            rows = await self._db.fetchall("SELECT * FROM asks WHERE project_id = ? AND resolved_at IS NULL AND routed_to = ? ORDER BY created_at, rowid", (project_id, routed_to))
        return [_ask(r) for r in rows]

    async def link(self, ask_id: str, dispatch_id: str | None) -> None:
        """Show a request under a dispatch, or under none."""
        await self._db.execute("UPDATE asks SET dispatch_id = ? WHERE id = ?", (dispatch_id or None, ask_id))

    async def of_dispatches(self, dispatch_ids: Sequence[str], *, open_only: bool = True) -> list[Ask]:
        """The requests shown under these dispatches, oldest first."""
        if not dispatch_ids:
            return []
        marks = ", ".join("?" for _ in dispatch_ids)
        clause = " AND resolved_at IS NULL" if open_only else ""
        rows = await self._db.fetchall(f"SELECT * FROM asks WHERE dispatch_id IN ({marks}){clause} ORDER BY created_at, rowid", tuple(dispatch_ids))  # noqa: S608 — only placeholders are formatted in
        return [_ask(r) for r in rows]

    async def of_origin(self, origin: str, *, open_only: bool = True, limit: int = 50) -> list[Ask]:
        """Requests of one origin, newest first: the main orchestrator's own confirmations."""
        clause = " AND resolved_at IS NULL" if open_only else ""
        rows = await self._db.fetchall(f"SELECT * FROM asks WHERE origin = ?{clause} ORDER BY created_at DESC, rowid DESC LIMIT ?", (origin, limit))  # noqa: S608 — a fixed clause
        return [_ask(r) for r in rows]

    async def open_counts(self, routed_to: str) -> dict[str, int]:
        """How many open requests each project has for ``routed_to``, in one query."""
        rows = await self._db.fetchall("SELECT project_id, COUNT(*) AS n FROM asks WHERE resolved_at IS NULL AND routed_to = ? GROUP BY project_id", (routed_to,))
        return {r["project_id"]: int(r["n"]) for r in rows}


__all__ = [
    "ACTIVE_STATUSES",
    "CLI_HARNESSES",
    "EDITABLE",
    "HARNESSES",
    "HARNESS_NAMES",
    "ISOLATIONS",
    "PALETTE",
    "STATUSES",
    "Ask",
    "AsksStore",
    "Staff",
    "StaffBusy",
    "StaffError",
    "StaffMessage",
    "StaffSession",
    "StaffStore",
    "cap_notes",
    "colour_for",
    "daedalus_cannot_reach",
    "daedalus_reaches",
    "normalise_short_id",
]
