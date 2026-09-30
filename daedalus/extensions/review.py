"""Review and merge: the operator reads a staff branch on its card and merges it with one button.

The orchestrator proposes, the operator merges. A staff member's ``Report(done)`` puts the task in
review with ``merge_state='proposed'``; this module is the only way that work reaches the folder:

- :meth:`Review.review` reads what a merge would bring — commits, the files and their line counts, a
  bounded patch, the dry run of the merge — and what stands in its way, without changing anything;
- :meth:`Review.merge` merges the branch into the folder's current branch as a merge commit when the
  folder is clean, still on the branch the task was cut from, and the dry run is clean; the task is
  done, the worktree goes when the member has nothing else to do in that folder, the merged branch is
  deleted with ``-d`` and ``task.accepted`` tells the orchestrator. A merge that fails is undone, the
  folder is exactly as it was, and ``task.merge_failed`` wakes the orchestrator to sort it out;
- :meth:`Review.reject` sends the task back to its member with the operator's note.

Nothing is pushed: the operator's remote is the operator's business.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions.board import OPERATOR
from daedalus.extensions.staff import SENT_BACK
from daedalus.host.worktrees import BranchComparison, WorktreeError, WorktreeRefused
from daedalus.stores.projects import Project, ProjectFolder
from daedalus.stores.staff import ACTIVE_STATUSES, StaffError

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
        open_items = [str(c.get("text") or "") for c in task.get("checklist") or [] if not c.get("done")]
        if open_items:
            # Checked before the merge, not left to the move to done: a merge that lands and then cannot
            # close its task would leave the work in the folder and the task in review.
            out.append({"code": "checklist", "text": f"the checklist still has {len(open_items)} open item(s): {', '.join(open_items[:3])}"})
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
        if comparison.conflicts and task["status"] == "review" and task.get("merge_state") == "proposed":
            # Merge is disabled on a conflict, so pressing it cannot be what tells the orchestrator: the
            # first review that sees one does, once — the state moves on and a second look is silent.
            await self._failed(task, project, f"the merge would conflict in {', '.join(comparison.conflicts[:10])}", comparison.conflicts)
            task["merge_state"] = "conflict"
        return {
            "task_id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "merge_state": task.get("merge_state") or "",
            "branch": comparison.branch,
            "base": base,
            "current": comparison.current,
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
        """Merge the task's branch and finish the task; the finished task, with ``merge`` saying what landed.

        Only the operator merges (decision: the orchestrator proposes). The preconditions are the ones
        the card shows, checked again here because the folder may have changed since the card was drawn.
        A conflict — seen by the dry run or by the merge itself — sets ``merge_state='conflict'``,
        publishes ``task.merge_failed`` so the orchestrator is woken to have it resolved, and is refused;
        a dirty folder or one on another branch is refused without an event, because it is the
        operator's own folder to put right.
        """
        if by != "operator":
            raise ReviewRefused("only the operator merges a staff branch; the orchestrator proposes it")
        task, project, folder = await self._where(task_id)
        base = await self._base(task)
        try:
            comparison = await self.team.worktrees.compare(folder, str(task["branch"]))
        except Exception as exc:  # noqa: BLE001 — any git failure is a refusal with git's words
            raise ReviewRefused(f"the branch could not be read in {folder.path}: {exc}") from exc
        blockers = self.blockers(task, comparison, base)
        conflict = next((b for b in blockers if b["code"] == "conflicts"), None)
        if conflict is not None:
            await self._failed(task, project, conflict["text"], comparison.conflicts or [])
            raise ReviewRefused(conflict["text"])
        if blockers:
            raise ReviewRefused("; ".join(b["text"] for b in blockers))
        message = f"Merge {task['branch']}: {task['title']}"
        try:
            sha = await self.team.worktrees.merge(folder, str(task["branch"]), message=message)
        except WorktreeRefused as exc:
            await self._failed(task, project, str(exc), [])
            raise ReviewRefused(str(exc)) from exc
        await self.app.db.execute("UPDATE board_tasks SET merge_state = 'merged', acceptance_state = 'operator_approved' WHERE id = ?", (task["id"],))
        # The merge is the operator's acceptance of every check of the card (see ``Board.accept``).
        done = await self.board.update(task["id"], status="done", note=f"merged by the operator as {sha[:10]} into {comparison.current}", check=list(range(len(task.get("checklist") or []))))
        await self.board._publish("task.accepted", done, OPERATOR, merge=sha, branch=str(task["branch"]))
        await self.app.manager.projects.record(  # type: ignore[union-attr]
            project.id, "operator", "merge", f"Merged {task['branch']} into {comparison.current} ({len(comparison.commits)} commits, +{comparison.added} −{comparison.removed}): {task['title']}", {"task_id": task["id"], "commit": sha}
        )
        cleanup = await self._clean_up(task, folder)
        done["merge"] = {"commit": sha, "into": comparison.current, **cleanup}
        return done

    async def _failed(self, task: dict[str, Any], project: Project, error: str, conflicts: list[str]) -> None:
        """Mark the conflict and tell the orchestrator — once per proposal: a task already marked has
        been told, and pressing Merge on it again is not news."""
        if task.get("merge_state") == "conflict":
            return
        await self.app.db.execute("UPDATE board_tasks SET merge_state = 'conflict' WHERE id = ?", (task["id"],))
        extra: dict[str, Any] = {"error": error[:1000]}
        if conflicts:
            extra["conflicts"] = conflicts[:50]
        await self.board._publish("task.merge_failed", await self.board.get(task["id"]), OPERATOR, **extra)
        await self.app.manager.projects.record(project.id, "system", "merge", f"Merging {task['branch']} failed and was undone: {error[:500]}", {"task_id": task["id"]})  # type: ignore[union-attr]

    async def _clean_up(self, task: dict[str, Any], folder: ProjectFolder) -> dict[str, Any]:
        """After a merge: the worktree goes when its member has no next task in this folder, and the
        merged branch goes with ``-d`` when nothing has it checked out.

        The member's session on this task is ended first when it sits idle, because a session whose
        working directory was removed under it would fail on the next thing it is told. A member still
        working (told something new meanwhile) keeps both.
        """
        out = {"worktree_removed": False, "branch_deleted": False}
        member_id = task.get("assignee_staff_id")
        row = await self.app.db.fetchone(
            "SELECT id FROM staff_sessions WHERE task_id = ? AND branch = ? AND worktree_path IS NOT NULL ORDER BY started_at DESC LIMIT 1", (task["id"], task["branch"])
        )
        session = await self.team.manager.staff.session(row["id"]) if row is not None else None
        if member_id and session is not None:
            next_here = await self.app.db.fetchone(
                "SELECT 1 FROM board_tasks WHERE assignee_staff_id = ? AND id != ? AND status IN ('todo', 'blocked', 'doing') AND (folder_id = ? OR folder_id IS NULL) LIMIT 1",
                (member_id, task["id"], folder.id),
            )
            live = await self.team.live(session.id) if session.live else None
            busy = live is not None and live.session.status in ACTIVE_STATUSES
            if next_here is None and not busy:
                if live is not None:
                    await self.team._end(live, "its task was merged", stop=True, by="operator")
                worktree = await self.team.worktree_of(session)
                if worktree is not None:
                    try:
                        out["branch_deleted"] = await self.team.worktrees.remove(worktree, delete_branch_if_merged=True)
                        out["worktree_removed"] = True
                    except WorktreeError as exc:
                        logger.warning("kept the worktree of task %s: %s", task["id"], exc)
        if not out["branch_deleted"]:
            try:
                out["branch_deleted"] = await self.team.worktrees.delete_branch(folder, str(task["branch"]))
            except WorktreeError:
                pass
        return out

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
