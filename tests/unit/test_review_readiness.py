"""A branch preview never advertises merge without a current bound result and verdict."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.extensions.review import Review
from daedalus.host.worktrees import BranchComparison
from daedalus.stores.database import Database


@pytest.mark.asyncio
async def test_review_projection_blocks_missing_and_stale_result_binding(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('project1','Project','2026-01-01','{}')")
        await db.execute("INSERT INTO board_tasks(id,title,status,priority,acceptance,checklist,depends_on,"
                         " created_at,updated_at,project_id,brief_json,branch,merge_state) VALUES"
                         " ('task1','Work','review',3,'','[]','[]','2026-01-01','2026-01-01','project1','{}',"
                         " 'agent/worker/work','proposed')")
        await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                         " snapshot_json,created_at) VALUES"
                         " ('task1',1,'operator','','{\"requirements\":[],\"checklist\":[]}',"
                         " '2026-01-01')")

        class Worktrees:
            async def compare(self, folder, branch: str) -> BranchComparison:  # type: ignore[no-untyped-def]
                return BranchComparison(branch, True, "main", False, True, [], False, [], "", True, [])

            async def commit_identity(self, folder, branch: str | None = None) -> str:  # type: ignore[no-untyped-def]
                return "head1" if branch else "base1"

        class Team:
            worktrees = Worktrees()

            async def project(self, project_id: str):  # type: ignore[no-untyped-def]
                folder = SimpleNamespace(id="folder1", path=tmp_path, label="Folder", env="local")
                return SimpleNamespace(id=project_id, name="Project", primary=folder, folder=lambda _: folder)

        class Board:
            async def get(self, task_id: str):  # type: ignore[no-untyped-def]
                return {"id": task_id, "title": "Work", "status": "review", "branch": "agent/worker/work",
                        "project_id": "project1", "folder_id": None, "merge_state": "proposed"}

        app = SimpleNamespace(db=db, extensions={"board": Board()})
        review = Review(app, Team())  # type: ignore[arg-type]
        missing = await review.review("task1")
        assert missing["can_merge"] is False
        assert {item["code"] for item in missing["blockers"]} == {"result_missing", "ci"}
        assert missing["ci_status"] == "blocked" and missing["ci_checks"] == []
        await db.execute("INSERT INTO result_receipts(id,task_id,contract_revision,outcome,original_text,"
                         " original_digest,original_size_bytes,checks_json,limitations_json,actor_id,created_at)"
                         " VALUES ('result1','task1',1,'complete','Report',?,6,'[]','[]',"
                         " 'staff:worker','2026-01-01')", (hashlib.sha256(b"Report").hexdigest(),))
        await db.execute("INSERT INTO review_verdicts(id,result_id,contract_revision,reviewer_actor_id,"
                         " verification,accepted,head,base,evidence_json,reason,created_at)"
                         " VALUES ('verdict1','result1',1,'operator:1','verified',1,'oldhead','base1','[]',"
                         " 'Seen','2026-01-01')")
        stale = await review.review("task1")
        assert stale["can_merge"] is False
        assert {item["code"] for item in stale["blockers"]} == {"verdict_stale", "ci"}
        assert stale["result_id"] == "result1" and stale["verdict_id"] == "verdict1"
    finally:
        await db.close()
