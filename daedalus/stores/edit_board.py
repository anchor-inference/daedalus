"""Who is editing which file right now, for agents that share one folder.

Worktree isolation keeps most members apart, but a member hired with ``shared`` isolation, a native
session and the operator's own session can all write the same checkout. Each completed edit takes a
short lease on the file it touched, keyed by the file's canonical path, so two agents are on one
row only when they really wrote the same file; a worktree's copy is another path and never meets
the original. The lease is extended by every edit and lapses on its own, and the end of the
holder's turn or run drops it at once: an agent that stopped must not haunt the roster for a minute.

When an agent edits a file another agent holds, the edit's tool result gets one line saying who,
when and how to reach them. It never blocks: the edit has already happened by the time the line is
written, and refusing a write over a lease nobody asked for would stall work for a courtesy. The
line is deduplicated by signature per agent, the set of other holders and the files they share,
so it is shown again only when that set changes or after a staleness window long enough to outlive
a compaction that folded the earlier line away. Leases and signatures live in the database, so a
restart neither forgets a lease nor repeats a note the agent was already given.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from daedalus.stores.database import Database

HolderKind = Literal["member", "session"]

FILES_PER_HOLDER = 3
"""Files named for one other holder in a note; past it the note says how many more. A note longer
than a line stops being read, and the first few files are what tells the agent where the overlap is."""
ROOT_SEARCH_DEPTH = 64


@dataclass(frozen=True, slots=True)
class Editor:
    """The agent behind an edit: a project member (by its staff id) or a session (by its id)."""

    key: str
    """``staff:<staff id>`` or ``session:<session id>``. A member is keyed by the member, not by its
    live session, because Release and Dismiss name the member and a member has one live session."""
    kind: HolderKind
    name: str
    project_id: str | None = None

    @staticmethod
    def member(staff_id: str, name: str, project_id: str | None) -> Editor:
        return Editor(f"staff:{staff_id}", "member", name, project_id)

    @staticmethod
    def session(session_id: str, title: str, project_id: str | None) -> Editor:
        return Editor(f"session:{session_id}", "session", title, project_id)


@dataclass(frozen=True, slots=True)
class Lease:
    path: str
    holder: str
    kind: HolderKind
    name: str
    project_id: str | None
    root: str
    edited_at: float
    expires_at: float


@dataclass(slots=True)
class Holder:
    """One agent's live leases, as the orchestrator's state and the team page show them."""

    key: str
    kind: HolderKind
    name: str
    project_id: str | None
    edited_at: float
    files: list[Lease] = field(default_factory=list)


def canonical_path(path: str, cwd: str = "") -> str:
    """The one spelling of a file both a native tool and a CLI's hook arrive at.

    Relative paths are taken against the agent's folder, then symlinks resolved as far as this
    filesystem has them: a CLI on the operator's machine names paths this container may not hold,
    and those keep their normalised spelling rather than failing."""
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.realpath(os.path.normpath(path))


def repo_root(path: str, fallback: str = "") -> str:
    """The checkout the file belongs to: the nearest folder upwards holding ``.git``, else ``fallback``."""
    current = os.path.dirname(path)
    for _ in range(ROOT_SEARCH_DEPTH):
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return fallback


def shown_path(path: str, root: str) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1:]
    return path


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def contact(kind: HolderKind) -> str:
    """How the agent reading the note reaches the other holder."""
    if kind == "member":
        return "reach them through the project orchestrator (it can Tell them; a member asks with AskOrchestrator)"
    return "the operator's session: coordinate through the operator"


def note_for(conflicts: Sequence[Lease], root: str, now: float) -> str:
    """The one line appended to an edit's tool result."""
    holders: dict[str, list[Lease]] = {}
    for lease in sorted(conflicts, key=lambda c: -c.edited_at):
        holders.setdefault(lease.holder, []).append(lease)
    parts = []
    for files in holders.values():
        first = files[0]
        label = f"member {first.name}" if first.kind == "member" else f"session \"{first.name}\""
        names = ", ".join(shown_path(f.path, root) for f in files[:FILES_PER_HOLDER])
        if len(files) > FILES_PER_HOLDER:
            names += f" and {len(files) - FILES_PER_HOLDER} more"
        parts.append(f"{label} — {names} (edited {_age(now - first.edited_at)}), {contact(first.kind)}")
    return ("[edit board] Others are editing the same files: " + "; ".join(parts)
            + ". Your edit went through; agree on who changes what before you overwrite each other.")


def _signature(conflicts: Iterable[Lease]) -> str:
    # Times are left out on purpose: an extended lease is the same roster, and a note repeated on
    # every edit of a busy pair is noise the agent learns to skip.
    pairs = sorted({(c.holder, c.path) for c in conflicts})
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def _lease(row: Any) -> Lease:
    return Lease(row["path"], row["holder"], row["holder_kind"], row["holder_name"], row["project_id"],
                 row["root"], float(row["edited_at"]), float(row["expires_at"]))


class EditBoard:
    """The leases and the per-agent note signatures, over the shared database."""

    def __init__(self, db: Database, *, lease_seconds: float = 60.0, reshow_seconds: float = 45 * 60.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.lease_seconds = lease_seconds
        self.reshow_seconds = reshow_seconds
        self.clock = clock

    async def record(self, editor: Editor, paths: Sequence[str], *, root: str = "") -> str:
        """Take or extend the editor's lease on each path; the note to append, or empty.

        ``paths`` are canonical already (:func:`canonical_path`). The note is empty when nobody else
        holds any file this editor holds, and when the agent was already shown this same roster
        within the staleness window."""
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return ""
        now = self.clock()
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM edit_leases WHERE expires_at <= ?", (now,))
            for path in paths:
                await conn.execute(
                    "INSERT INTO edit_leases(path, holder, holder_kind, holder_name, project_id, root, first_at, edited_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path, holder) DO UPDATE SET holder_name = excluded.holder_name,"
                    " project_id = excluded.project_id, root = excluded.root, edited_at = excluded.edited_at, expires_at = excluded.expires_at",
                    (path, editor.key, editor.kind, editor.name[:120], editor.project_id, root or repo_root(path), now, now, now + self.lease_seconds),
                )
            async with conn.execute(
                "SELECT other.* FROM edit_leases mine JOIN edit_leases other ON other.path = mine.path AND other.holder != mine.holder"
                " WHERE mine.holder = ?", (editor.key,),
            ) as cursor:
                conflicts = [_lease(row) for row in await cursor.fetchall()]
            async with conn.execute("SELECT signature, shown_at FROM edit_notices WHERE holder = ?", (editor.key,)) as cursor:
                shown = await cursor.fetchone()
            if not conflicts:
                # The overlap is over: the same roster coming back later is news again.
                if shown is not None:
                    await conn.execute("DELETE FROM edit_notices WHERE holder = ?", (editor.key,))
                return ""
            signature = _signature(conflicts)
            if shown is not None and shown["signature"] == signature and now - float(shown["shown_at"]) < self.reshow_seconds:
                return ""
            await conn.execute("INSERT INTO edit_notices(holder, signature, shown_at) VALUES (?,?,?)"
                               " ON CONFLICT(holder) DO UPDATE SET signature = excluded.signature, shown_at = excluded.shown_at",
                               (editor.key, signature, now))
        return note_for(conflicts, root or repo_root(paths[0]), now)

    async def purge(self, holder: str) -> int:
        """Drop every lease of one holder: its turn or run ended, or it was released."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM edit_leases WHERE holder = ?", (holder,))
            return cursor.rowcount or 0

    async def prune(self) -> int:
        """Drop lapsed leases, and signatures older than the staleness window that nothing renews."""
        now = self.clock()
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM edit_leases WHERE expires_at <= ?", (now,))
            await conn.execute("DELETE FROM edit_notices WHERE shown_at <= ? AND holder NOT IN (SELECT holder FROM edit_leases)",
                               (now - self.reshow_seconds,))
            return cursor.rowcount or 0

    async def holders(self, *, project_id: str | None = None, folders: Sequence[str] = ()) -> list[Holder]:
        """The live leases of one project's folder set, by holder, newest edit first.

        A lease counts when its holder belongs to the project or its file lies under one of
        ``folders``: the operator's own session in the project's checkout belongs to no project
        of its own, and it is exactly the editor a member needs to know about."""
        now = self.clock()
        rows = await self.db.fetchall("SELECT * FROM edit_leases WHERE expires_at > ? ORDER BY edited_at DESC", (now,))
        prefixes = [os.path.normpath(f).rstrip("/") + "/" for f in folders if f]
        out: dict[str, Holder] = {}
        for row in rows:
            lease = _lease(row)
            if project_id is not None and lease.project_id != project_id and not any(lease.path.startswith(p) for p in prefixes):
                continue
            holder = out.get(lease.holder)
            if holder is None:
                holder = out[lease.holder] = Holder(lease.holder, lease.kind, lease.name, lease.project_id, lease.edited_at)
            holder.files.append(lease)
        return list(out.values())


SCHEMA = """
CREATE TABLE IF NOT EXISTS edit_leases (
    path TEXT NOT NULL,
    holder TEXT NOT NULL,
    holder_kind TEXT NOT NULL CHECK (holder_kind IN ('member', 'session')),
    holder_name TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    root TEXT NOT NULL DEFAULT '',
    first_at REAL NOT NULL,
    edited_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (path, holder)
);
CREATE INDEX IF NOT EXISTS edit_leases_by_holder ON edit_leases(holder);
CREATE INDEX IF NOT EXISTS edit_leases_by_expiry ON edit_leases(expires_at);
CREATE TABLE IF NOT EXISTS edit_notices (
    holder TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    shown_at REAL NOT NULL
);
"""
"""Unix seconds for the times: a lease is compared with the clock on every edit, and a number
compares without the formatting questions an ISO string carries. No foreign keys: a holder is a
member or a session that may be deleted while its lease runs out on its own a minute later."""


__all__ = ["SCHEMA", "EditBoard", "Editor", "Holder", "Lease", "canonical_path", "contact", "note_for", "repo_root", "shown_path"]
