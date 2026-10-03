"""Read-only review projection and explicit rejection for a staff branch.

The merge itself runs through a fenced outbox effect. Reading this projection cannot change the
task, and merging a branch does not itself accept an immutable result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions.staff import SENT_BACK
from daedalus.host.worktrees import BranchComparison, WorktreeError
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import StaffError

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.board import Board
    from daedalus.extensions.staff import Team

logger = logging.getLogger(__name__)

RECEIPTS_MAX = 20
NOTE_MAX = 2000


class ReviewRefused(ValueError):
    """A merge or a rejection that cannot happen now; the message says why and what would unblock it."""


class Review:
    def __init__(self, app: Application, team: Team) -> None:
        self.app = app
        self.team = team

    @property
    def board(self) -> Board:
        return self.app.extensions["board"]  # type: ignore[no-any-return]

    async def _where(self, task_id: str) -> tuple[dict[str, Any], Project, ProjectFolder]:
        """The task with its staff branch, its project and the folder the branch merges into."""
        task = await self.board.get(task_id)
        if not task.get("branch"):
            raise ReviewRefused(f"task {task_id} has no staff branch to review")
        if not task.get("project_id"):
            raise ReviewRefused(f"task {task_id} is not on a project's board")
        project = await self.team.project(task["project_id"])
        folder = project.folder(task["folder_id"]) if task.get("folder_id") else None
        folder = folder or project.primary
        if folder is None:
            raise ReviewRefused(f"{project.name} has no folder to merge into")
        return task, project, folder

    async def _base(self, task: dict[str, Any]) -> str:
        """The branch the task's work was cut from, as its latest staff session recorded it."""
        row = await self.app.db.fetchone(
            "SELECT base_ref FROM staff_sessions WHERE task_id = ? AND branch = ? AND base_ref IS NOT NULL ORDER BY started_at DESC LIMIT 1", (task["id"], task["branch"])
        )
        return str(row["base_ref"]) if row is not None and row["base_ref"] else ""

    async def _receipts(self, task_id: str) -> list[dict[str, Any]]:
        """The verifications the member's sessions on this task ran: what they say they checked."""
        rows = await self.app.db.fetchall(
            "SELECT v.criterion, v.command, v.exit_code, v.passed, v.at FROM verifications v WHERE v.session_id IN "
            "(SELECT session_id FROM staff_sessions WHERE task_id = ? AND session_id IS NOT NULL) ORDER BY v.at DESC LIMIT ?",
            (task_id, RECEIPTS_MAX),
        )
        return [{"criterion": r["criterion"], "command": r["command"], "exit_code": int(r["exit_code"]), "passed": bool(r["passed"]), "at": r["at"]} for r in rows]

    @staticmethod
    def blockers(task: dict[str, Any], comparison: BranchComparison, base: str) -> list[dict[str, str]]:
        """Why Merge cannot be pressed now, each with a code the app words and a sentence for everyone else."""
        out: list[dict[str, str]] = []
        if task["status"] != "review":
            out.append({"code": "status", "text": f"the task is {task['status']}, not in review"})
        if not comparison.exists:
            out.append({"code": "branch", "text": f"the branch {comparison.branch} no longer exists"})
            return out
        if comparison.merged:
            out.append({"code": "merged", "text": f"{comparison.branch} is already in {comparison.current}"})
        if not comparison.clean:
            out.append({"code": "dirty", "text": "the folder has uncommitted changes; commit or stash them first"})
        if base and comparison.current != base:
            out.append({"code": "moved", "text": f"the folder is on {comparison.current}, not on {base} where the work was cut from"})
        if comparison.conflicts is None:
            out.append({"code": "unknown", "text": "git could not tell whether the merge would conflict"})
        elif comparison.conflicts:
            out.append({"code": "conflicts", "text": f"the merge would conflict in {', '.join(comparison.conflicts[:5])}" + (f" and {len(comparison.conflicts) - 5} more" if len(comparison.conflicts) > 5 else "")})
        return out

    async def review(self, task_id: str) -> dict[str, Any]:
        """Everything the review card shows, read-only."""
        task, project, folder = await self._where(task_id)
        base = await self._base(task)
        try:
            comparison = await self.team.worktrees.compare(folder, str(task["branch"]))
        except (WorktreeError, OSError) as exc:
            raise ReviewRefused(f"the branch could not be read in {folder.path}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — git's own failure, worded for the card
            raise ReviewRefused(f"the branch could not be read in {folder.path}: {exc}") from exc
        blockers = self.blockers(task, comparison, base)
        merged = await self.app.db.fetchone("SELECT id,result_id,verdict_id,head_sha,base_sha,merge_sha,state,error"
                                            " FROM task_merge_receipts WHERE task_id = ?"
                                            " ORDER BY created_at DESC,id DESC LIMIT 1", (task_id,))
        head_sha = await self.team.worktrees.commit_identity(folder, str(task["branch"])) if comparison.exists else None
        current_sha = await self.team.worktrees.commit_identity(folder)
        return {
            "task_id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "merge_state": task.get("merge_state") or "",
            "branch": comparison.branch,
            "base": base,
            "current": comparison.current,
            "head_sha": head_sha or (merged["head_sha"] if merged is not None else None),
            "base_sha": current_sha if not merged or merged["state"] != "merged" else merged["base_sha"],
            "current_sha": current_sha,
            "merge_receipt": dict(merged) if merged is not None else None,
            "folder": {"id": folder.id, "path": str(folder.path), "label": folder.label, "env": folder.env},
            "exists": comparison.exists,
            "on_base": not base or comparison.current == base,
            "folder_clean": comparison.clean,
            "merged": comparison.merged,
            "commits": [{"sha": c.sha, "author": c.author, "at": c.at, "subject": c.subject} for c in comparison.commits],
            "more_commits": comparison.more_commits,
            "files": [{"path": f.path, "added": f.added, "removed": f.removed} for f in comparison.files],
            "added": comparison.added,
            "removed": comparison.removed,
            "patch": comparison.patch,
            "patch_complete": comparison.patch_complete,
            "conflicts": comparison.conflicts,
            "receipts": await self._receipts(task["id"]),
            "can_merge": not blockers,
            "blockers": blockers,
            "project": {"id": project.id, "name": project.name},
        }

    async def merge(self, task_id: str, *, by: str = "operator") -> dict[str, Any]:
        """External merges require a queued, exact-result command and a fenced effect claim."""
        raise ReviewRefused("merge requires an exact result, verified verdict, and queued operation receipt")

    async def reject(self, task_id: str, note: str, *, by: str = "operator") -> dict[str, Any]:
        """Send the work back: the task returns to doing with ``merge_state='rejected'`` and the note goes
        to the member — told at once when their session is live, otherwise waiting in the task's notes
        for the session the assignment starts."""
        note = (note or "").strip()[:NOTE_MAX]
        if not note:
            raise ReviewRefused("say what to change: a rejection needs a note")
        task, _project, _folder = await self._where(task_id)
        # One line in the task's notes, which is where the next session reads it from.
        line = " ".join(note.split())
        if task["status"] != "review":
            raise ReviewRefused(f"only a task in review can be sent back; this one is {task['status']}")
        member = await self.team.manager.staff.get(task["assignee_staff_id"]) if task.get("assignee_staff_id") else None
        live = await self.team.live_of(member) if member is not None else None
        told = False
        await self.app.db.execute("UPDATE board_tasks SET merge_state = 'rejected', acceptance_state = 'returned' WHERE id = ?", (task["id"],))
        if live is not None and live.session.task_id == task["id"]:
            updated = await self.board.update(task["id"], status="doing", note=f"{SENT_BACK}{by}: {line}")
            try:
                await self.team.tell(member, f"The operator sent task {task['id']} (\"{task['title']}\") back from review:\n{note}\n\nChange it on {task['branch']}, commit, and report done again.", when="after_turn", by=by)  # type: ignore[arg-type]
                told = True
            except (StaffError, KeyError, RuntimeError) as exc:
                logger.warning("could not tell %s about the rejection of %s: %s", member.name if member else "?", task["id"], exc)
        else:
            # Nobody is on it: back to the queue, where the assignment starts a session whose first
            # message carries the task's notes.
            updated = await self.board.update(task["id"], status="todo", note=f"{SENT_BACK}{by}: {line}")
            if member is not None and member.active:
                try:
                    await self.team.assign(member, task["id"], by=by)
                except (StaffError, KeyError, ValueError) as exc:
                    logger.info("the rejected task %s waits for an assignment: %s", task["id"], exc)
        updated = await self.board.get(task["id"])
        updated["told"] = told
        return updated


__all__ = ["Review", "ReviewRefused"]
