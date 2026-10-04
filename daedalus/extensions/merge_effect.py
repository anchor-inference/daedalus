"""Fenced, exact-result merge effect with durable provenance and read-only reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.ci_observations import ci_readiness
from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.merge_source import file_identity, source_identity, stage_report
from daedalus.extensions.orchestrator_domain import OrchestratorDomain, unresolved_review_comments
from daedalus.host.worktrees import WorktreeError, WorktreeRefused
from daedalus.stores.control import ControlDenied, now
from daedalus.stores.files import FILES_TENANT
from daedalus.stores.outbox import Claim

if TYPE_CHECKING:
    from daedalus.app import Application


class MergeEffect:
    """Merge a reviewed branch once; a crash leaves an unknown effect for explicit reconciliation."""

    def __init__(self, app: Application) -> None:
        self.app = app

    async def _review(self, task_id: str) -> tuple[Any, Any, dict[str, Any]]:
        team = self.app.extensions.get("staff")
        review = getattr(team, "review", None)
        if review is None:
            raise ValueError("the review service is unavailable")
        task, _project, folder = await review._where(task_id)
        return team, folder, task

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        if claim.kind != "review.merge" or claim.task_id != claim.payload.get("task_id"):
            return EffectOutcome("failed", "merge claim task does not match")
        row = await self.app.db.fetchone("SELECT m.*,v.verification,v.accepted AS verdict_accepted,"
                                         " r.contract_revision,r.attempt_id,t.current_attempt_id,"
                                         " t.contract_revision AS current_revision,t.status"
                                         " FROM task_merge_receipts m JOIN review_verdicts v ON v.id=m.verdict_id"
                                         " JOIN result_receipts r ON r.id=m.result_id JOIN board_tasks t ON t.id=m.task_id"
                                         " WHERE m.id = ?", (claim.id,))
        if row is None or row["operation_receipt_id"] != claim.receipt_id:
            return EffectOutcome("failed", "merge provenance is missing")
        if (row["result_id"] != claim.payload.get("result_id") or row["verdict_id"] != claim.payload.get("verdict_id") or
                row["head_sha"] != claim.payload.get("head_sha") or row["base_sha"] != claim.payload.get("base_sha")):
            return EffectOutcome("failed", "merge payload no longer matches its provenance")
        pinned_source = claim.payload.get("source")
        if not isinstance(pinned_source, dict) or not pinned_source.get("digest"):
            return EffectOutcome("failed", "merge source snapshot is missing")
        if row["state"] == "merged":
            return EffectOutcome("completed")
        if (row["state"] != "queued" or row["verification"] != "verified" or not row["verdict_accepted"] or
                row["contract_revision"] != row["current_revision"] or row["status"] != "review"):
            return EffectOutcome("failed", "result, verdict, or task review state changed")
        async with self.app.db.transaction() as conn:
            ci = await ci_readiness(conn, claim.task_id, row["contract_revision"], row["head_sha"],
                                    require_policy=True)
            if ci["state"] != "passed":
                return EffectOutcome("failed", "required CI changed or is missing for the reviewed head")
            latest = await conn.execute("SELECT id FROM result_receipts WHERE task_id = ? AND contract_revision = ?"
                                        " AND attempt_id IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                        (claim.task_id, row["contract_revision"], row["current_attempt_id"]))
            latest_result = await latest.fetchone()
            await latest.close()
            if (row["attempt_id"] != row["current_attempt_id"] or latest_result is None or
                    latest_result["id"] != row["result_id"] or await unresolved_review_comments(conn, row["result_id"])):
                return EffectOutcome("failed", "a newer result or blocking review comment prevents merge")
        try:
            team, folder, task = await self._review(claim.task_id)
            review = await team.review.review(claim.task_id)
            if review["blockers"]:
                return EffectOutcome("failed", "; ".join(blocker["code"] for blocker in review["blockers"]))
            if review["head_sha"] != row["head_sha"] or review["base_sha"] != row["base_sha"]:
                return EffectOutcome("failed", "the reviewed branch or folder changed")
            await check(claim)
        except (ControlDenied, WorktreeError, ValueError) as exc:
            return EffectOutcome("failed", str(exc))
        # The effect lease check can await while another delivery records a newer failing CI run.
        # Re-read the durable result at the last host-controlled point before touching Git.
        async with self.app.db.transaction() as conn:
            ci = await ci_readiness(conn, claim.task_id, row["contract_revision"], row["head_sha"],
                                    require_policy=True)
            if ci["state"] != "passed":
                return EffectOutcome("failed", "required CI changed or is missing for the reviewed head")
            try:
                current_source = await source_identity(conn, row["result_id"], row["verdict_id"])
            except ValueError as exc:
                return EffectOutcome("failed", str(exc))
            if current_source != pinned_source:
                return EffectOutcome("failed", "reviewed report, verdict, evidence, or artifacts changed")
        try:
            # The report can be damaged after approval without changing its receipt or the Git head.
            original = await OrchestratorDomain(self.app.db).original(claim.task_id, row["result_id"])
            for artifact in pinned_source["snapshot"]["artifacts"]:
                if not artifact["file_id"]:
                    continue
                path = self.app.manager.files.blobs.path_of(FILES_TENANT, artifact["digest"])
                digest, size = await asyncio.to_thread(file_identity, path)
                if size != artifact["size_bytes"] or digest != artifact["digest"]:
                    raise ValueError("reviewed artifact bytes changed")
            stage_report(self.app.db.path.parent / "result-originals", claim.id, original,
                         pinned_source["report_digest"], pinned_source["report_size"])
        except (OSError, ValueError) as exc:
            return EffectOutcome("failed", f"merge source is unavailable or changed: {exc}")
        try:
            merge_sha = await team.worktrees.merge(folder, str(task["branch"]),
                                                  message=f"Merge {task['branch']}: {task['title']}",
                                                  expected_head=row["base_sha"],
                                                  expected_branch_tip=row["head_sha"])
        except WorktreeRefused as exc:
            return EffectOutcome("unknown", f"merge refused; inspect Git before retry: {exc}")
        # The external merge happened. If this write fails, the outcome is unknown and reconciliation
        # must inspect the Git parentage before anyone changes the receipt or retries.
        async with self.app.db.transaction() as conn:
            await conn.execute("UPDATE task_merge_receipts SET state = 'merged',merge_sha = ?,updated_at = ?"
                               " WHERE id = ? AND state = 'queued'", (merge_sha, now(), claim.id))
            await conn.execute("UPDATE board_tasks SET merge_state = 'merged',entity_revision = entity_revision + 1"
                               " WHERE id = ? AND status = 'review'", (claim.task_id,))
        return EffectOutcome("completed")

    async def inspect_unknown(self, action_id: str) -> dict[str, Any]:
        """Read Git parentage; ancestry alone is not proof of this operation's merge commit."""
        row = await self.app.db.fetchone("SELECT task_id,head_sha,base_sha,merge_sha,state FROM task_merge_receipts"
                                         " WHERE id = ?", (action_id,))
        if row is None:
            raise KeyError(action_id)
        if row["merge_sha"]:
            return {"state": "completed", "merge_sha": row["merge_sha"], "proof": "stored_merge_receipt"}
        team, folder, _task = await self._review(row["task_id"])
        git = team.worktrees._git(folder.env)
        current = await team.worktrees.commit_identity(folder)
        history = await git.run(["rev-list", "--first-parent", "--parents", "--max-count=1000", "HEAD"], cwd=folder.path)
        for line in history.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == row["base_sha"] and row["head_sha"] in parts[2:]:
                return {"state": "completed", "merge_sha": parts[0], "current_sha": current,
                        "proof": "merge_commit_parents"}
        return {"state": "unknown", "current_sha": current,
                "reason": "no exact merge commit found in bounded first-parent history"}

    async def record_reconciled_merge(self, action_id: str, merge_sha: str) -> None:
        """Store a proven completion after OutboxStore has accepted the reconciliation evidence."""
        async with self.app.db.transaction() as conn:
            row = await conn.execute("SELECT task_id FROM task_merge_receipts WHERE id = ?", (action_id,))
            receipt = await row.fetchone()
            await row.close()
            if receipt is None:
                raise KeyError(action_id)
            await conn.execute("UPDATE task_merge_receipts SET state = 'merged',merge_sha = ?,updated_at = ?"
                               " WHERE id = ? AND state IN ('queued','claimed','unknown')", (merge_sha, now(), action_id))
            await conn.execute("UPDATE board_tasks SET merge_state = 'merged' WHERE id = ?", (receipt["task_id"],))

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        """Resolve an unknown external effect only from exact stored or Git-parent proof."""
        observed = await self.inspect_unknown(claim.id)
        if observed["state"] != "completed":
            return None
        await self.record_reconciled_merge(claim.id, observed["merge_sha"])
        return EffectResolution("completed", observed)


__all__ = ["MergeEffect"]
