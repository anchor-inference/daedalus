"""What a terminal's owner means to the terminals service: whether it exists, its name, its project,
where its terminals start and what a sandboxed one of them may write.

Kept behind a small interface so the service does not hold the session manager: the service is
tested against a fake of this, and the manager is the one implementation the application uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from daedalus.terminals.model import Owner

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager


class Owners(Protocol):
    async def exists(self, owner: Owner) -> bool: ...

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]: ...

    async def project_of(self, owner: Owner) -> str | None: ...

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        """An absolute path in ``env``'s filesystem, or ``None`` for the environment's home."""
        ...

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]: ...

    # Optional: ``activities(terminal_ids) -> {terminal_id: activity}``, what another part of the
    # application says a terminal is doing. The service asks it when the owners have it.


ACTIVITY_LEVELS = {"permission": "warn", "question": "warn", "error": "bad", "no_signal": "idle", "turn_done_unseen": "ok", "idle": "idle"}
"""How a staff member's status colours its terminal's card: waiting on someone is amber, a failure
red, silence and rest grey (silence is never a failure), work and a fresh result green."""


class ManagerOwners:
    """The owners as the session manager and the database know them."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.db = manager.db
        self.local_env = manager.projects.local_env

    async def exists(self, owner: Owner) -> bool:
        if owner.kind == "free":
            return True
        table = {"session": "sessions", "project": "projects", "staff": "staff"}[owner.kind]
        return await self.db.fetchone(f"SELECT 1 FROM {table} WHERE id = ?", (owner.id,)) is not None  # noqa: S608 — the table is one of three literals

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        wanted: dict[str, set[str]] = {}
        for owner in owners:
            if owner.id:
                wanted.setdefault(owner.kind, set()).add(owner.id)
        out: dict[Owner, str] = {}
        for kind, column, table in (("session", "title", "sessions"), ("project", "name", "projects"), ("staff", "name", "staff")):
            ids = sorted(wanted.get(kind, ()))
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            for row in await self.db.fetchall(f"SELECT id, {column} AS label FROM {table} WHERE id IN ({marks})", ids):  # noqa: S608 — literals and placeholders only
                out[Owner(kind, row["id"])] = str(row["label"] or "")
        return out

    async def project_of(self, owner: Owner) -> str | None:
        if owner.kind == "project":
            return owner.id
        if owner.kind == "session":
            row = await self.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (owner.id,))
            return row["project_id"] if row else None
        if owner.kind == "staff":
            row = await self.db.fetchone("SELECT project_id FROM staff WHERE id = ?", (owner.id,))
            return row["project_id"] if row else None
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        """Where the owner's work is, in the environment the terminal runs in.

        A session in this process's own environment starts in its workspace — the same directory its
        agent's tools work in. Anywhere else the paths of this process mean nothing, so a terminal
        starts in the owner's project's first folder of that environment, or at home.
        """
        if owner.kind == "session" and env == self.local_env:
            state = await self.manager.get_state(str(owner.id))
            if state is not None:
                return str(state.workspace)
        project = await self.manager.projects.get(project_id) if project_id else None
        if project is None:
            return None
        for folder in project.folders:
            if folder.env == env and Path(folder.path).is_absolute():
                return str(folder.path)
        return None

    async def activities(self, terminal_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """What the staff members working in these terminals are doing, as the Terminals screen's
        cards show it: the status (the app has words for it), what the member waits for in the host's
        words, and — when it waits on someone — where it is answered.

        Only the terminal a live session runs in gets one; a companion process (Codex's app-server)
        is plumbing, and a second "Answer" on it would be the same request twice.
        """
        ids = sorted(set(terminal_ids))
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(
            "SELECT ss.terminal_id, ss.status, ss.waiting_for, ss.staff_id, s.project_id FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id "  # noqa: S608 — placeholders only
            f"WHERE ss.ended_at IS NULL AND ss.terminal_id IN ({marks})",
            ids,
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            status = str(row["status"])
            activity: dict[str, Any] = {"status": status, "label": str(row["waiting_for"] or ""), "level": ACTIVITY_LEVELS.get(status, "ok")}
            if status in ("permission", "question"):
                activity["action"] = {"kind": "answer", "label": "Answer", "path": f"/app/project/{row['project_id']}/staff/{row['staff_id']}"}
            out[str(row["terminal_id"])] = activity
        return out

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        """What a sandboxed terminal may write, scoped to a staff attempt when one owns it.

        Other terminals retain their session walls or project folders.
        """
        if owner.kind == "session" and env == self.local_env:
            services = self.manager.locator_services(str(owner.id))
            if services is None and await self.manager.get_state(str(owner.id)) is not None:
                services = self.manager.locator_services(str(owner.id))
            if services is not None:
                return [str(p) for p in services.sandbox_writable()]
        if owner.kind == "staff":
            # Project folders were once all bound writable for a staff terminal. A strict attempt
            # assigned to one folder could therefore alter a sibling without crossing any mount.
            rows = await self.db.fetchall(
                "SELECT s.project_id,s.isolation,ss.launch_cwd,ss.worktree_path,f.env,f.path AS folder_path,"
                " f.readonly FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id"
                " JOIN project_folders f ON f.id = ss.folder_id"
                " WHERE ss.staff_id = ? AND ss.kind = 'cli' AND ss.ended_at IS NULL",
                (owner.id,),
            )
            if (len(rows) != 1 or rows[0]["project_id"] != project_id or rows[0]["env"] != env
                    or rows[0]["launch_cwd"] != cwd):
                raise ValueError("a staff terminal needs its current task workspace")
            row = rows[0]
            if row["isolation"] == "readonly" or row["readonly"]:
                return []
            if row["isolation"] == "worktree":
                if not row["worktree_path"]:
                    raise ValueError("a worktree member needs its current worktree")
                return [row["worktree_path"]]
            if row["isolation"] == "shared" and cwd == row["folder_path"]:
                return [cwd]
            raise ValueError("a staff terminal has no writable task workspace")
        project = await self.manager.projects.get(project_id) if project_id else None
        if project is not None:
            folders = [str(f.path) for f in project.folders if f.env == env and not f.readonly]
            if folders:
                return folders
        return [cwd] if cwd else []


__all__ = ["ManagerOwners", "Owners"]
