"""Read-only review projection for a staff branch.

The merge itself runs through a fenced outbox effect. Reading this projection cannot change the
task, and merging a branch does not itself accept an immutable result.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from daedalus.extensions.ci_observations import ci_readiness
from daedalus.extensions.orchestrator_domain import unresolved_review_comments
from daedalus.host.worktrees import BranchComparison, WorktreeError
from daedalus.stores.projects import Project, ProjectFolder

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.board import Board
    from daedalus.extensions.staff import Team

RECEIPTS_MAX = 20


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

    async def _comparison_where(self, task_id: str, attempt_id: str) -> tuple[dict[str, Any], Project,
                                                                               ProjectFolder, dict[str, Any]]:
        """Resolve a contender's observed worktree without projecting it onto the shared task."""
        task = await self.board.get(task_id)
        row = await self.app.db.fetchone(
            "SELECT g.id AS group_id,g.state AS group_state,g.contract_revision,m.worktree_identity,"
            " s.folder_id,s.branch,s.base_ref,s.worktree_path,s.staff_id,staff.project_id"
            " FROM comparison_group_attempts m JOIN comparison_groups g ON g.id = m.group_id"
            " JOIN execution_attempts a ON a.id = m.attempt_id"
            " JOIN staff_sessions s ON s.id = a.staff_session_id"
            " JOIN staff ON staff.id = s.staff_id"
            " WHERE m.attempt_id = ? AND g.task_id = ?", (attempt_id, task_id))
        if (row is None or row["group_state"] not in ("planned", "active", "ready") or
                row["contract_revision"] != task["contract_revision"] or
                row["project_id"] != task["project_id"] or not row["worktree_path"] or
                not row["branch"] or not row["folder_id"] or
                hashlib.sha256(row["worktree_path"].encode()).hexdigest() != row["worktree_identity"]):
            raise ReviewRefused("the comparison attempt has no current isolated worktree")
        project = await self.team.project(task["project_id"])
        folder = project.folder(row["folder_id"])
        if folder is None:
            raise ReviewRefused("the comparison attempt's project folder is missing")
        return task, project, folder, dict(row)

    async def _base(self, task: dict[str, Any]) -> str:
        """The branch the task's work was cut from, as its latest staff session recorded it."""
        row = await self.app.db.fetchone(
            "SELECT base_ref FROM staff_sessions WHERE task_id = ? AND branch = ? AND base_ref IS NOT NULL ORDER BY started_at DESC LIMIT 1", (task["id"], task["branch"])
        )
        return str(row["base_ref"]) if row is not None and row["base_ref"] else ""

    async def _receipts(self, task_id: str) -> list[dict[str, Any]]:
        """The verifications the member's sessions on this task ran: what they say they checked."""
        rows = await self.app.db.fetchall(
            "SELECT v.id, v.criterion, v.command, v.exit_code, v.passed, v.at, v.tree FROM verifications v WHERE v.session_id IN "
            "(SELECT session_id FROM staff_sessions WHERE task_id = ? AND session_id IS NOT NULL) ORDER BY v.at DESC LIMIT ?",
            (task_id, RECEIPTS_MAX),
        )
        # The id and the commit let the card offer a passing receipt as evidence for the exact head.
        return [{"id": int(r["id"]), "criterion": r["criterion"], "command": r["command"], "exit_code": int(r["exit_code"]), "passed": bool(r["passed"]), "at": r["at"], "tree": r["tree"] or ""} for r in rows]

    @staticmethod
    def blockers(task: dict[str, Any], comparison: BranchComparison, base: str,
                 *, comparison_member: bool = False) -> list[dict[str, str]]:
        """Why Merge cannot be pressed now, each with a code the app words and a sentence for everyone else."""
        out: list[dict[str, str]] = []
        if not comparison_member and task["status"] != "review":
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

    async def review(self, task_id: str, *, comparison_attempt_id: str | None = None) -> dict[str, Any]:
        """Everything the review card shows, read-only."""
        member = None
        if comparison_attempt_id is None:
            task, project, folder = await self._where(task_id)
            base = await self._base(task)
            branch = str(task["branch"])
        else:
            task, project, folder, member = await self._comparison_where(task_id, comparison_attempt_id)
            base = str(member["base_ref"] or "")
            branch = str(member["branch"])
        try:
            comparison = await self.team.worktrees.compare(folder, branch)
        except (WorktreeError, OSError) as exc:
            raise ReviewRefused(f"the branch could not be read in {folder.path}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — git's own failure, worded for the card
            raise ReviewRefused(f"the branch could not be read in {folder.path}: {exc}") from exc
        blockers = self.blockers(task, comparison, base, comparison_member=member is not None)
        merged = None if member is not None else await self.app.db.fetchone(
            "SELECT id,result_id,verdict_id,head_sha,base_sha,merge_sha,state,error"
            " FROM task_merge_receipts WHERE task_id = ? ORDER BY created_at DESC,id DESC LIMIT 1", (task_id,))
        head_sha = await self.team.worktrees.commit_identity(folder, branch) if comparison.exists else None
        current_sha = await self.team.worktrees.commit_identity(folder)
        binding = await self.app.db.fetchone("SELECT contract_revision,current_attempt_id FROM board_tasks"
                                            " WHERE id = ?", (task_id,))
        candidate_attempt = comparison_attempt_id if member is not None else binding["current_attempt_id"] if binding else None
        async with self.app.db.transaction() as conn:
            ci = await ci_readiness(conn, task_id, binding["contract_revision"], head_sha) \
                if binding is not None else {"state": "not_required", "checks": []}
        if ci["state"] == "blocked":
            blockers.append({"code": "ci", "text": "required CI has not passed for the current branch HEAD"})
        result = await self.app.db.fetchone(
            "SELECT id,outcome FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
            " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (task_id, binding["contract_revision"], candidate_attempt),
        ) if binding is not None else None
        verdict = await self.app.db.fetchone(
            "SELECT id,verification,accepted,head,base FROM review_verdicts WHERE result_id = ?"
            " ORDER BY created_at DESC,rowid DESC LIMIT 1", (result["id"],),
        ) if result is not None else None
        if result is None:
            blockers.append({"code": "result_missing", "text": "no current immutable result was submitted"})
        elif result["outcome"] != "complete":
            blockers.append({"code": "result_incomplete", "text": "the current result is not complete"})
        if result is not None:
            if verdict is None or verdict["verification"] != "verified" or not verdict["accepted"]:
                blockers.append({"code": "verdict_missing", "text": "no independent verified approval binds this result"})
            elif verdict["head"] != head_sha or verdict["base"] != current_sha:
                blockers.append({"code": "verdict_stale", "text": "the reviewed branch or base changed"})
            async with self.app.db.transaction() as conn:
                if await unresolved_review_comments(conn, result["id"]):
                    blockers.append({"code": "comments", "text": "blocking review comments remain unresolved"})
        return {
            "task_id": task["id"],
            "comparison_group_id": member["group_id"] if member is not None else None,
            "comparison_attempt_id": comparison_attempt_id,
            "contract_revision": binding["contract_revision"] if binding is not None else None,
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
            "result_id": result["id"] if result is not None else None,
            "verdict_id": verdict["id"] if verdict is not None else None,
            "verification": verdict["verification"] if verdict is not None else "unverified",
            "verdict_accepted": bool(verdict["accepted"]) if verdict is not None else False,
            "ci_status": ci["state"],
            "ci_checks": ci["checks"],
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
            "can_merge": member is None and not blockers,
            "can_choose": member is not None and not blockers,
            "blockers": blockers,
            "project": {"id": project.id, "name": project.name},
        }



__all__ = ["Review", "ReviewRefused"]
