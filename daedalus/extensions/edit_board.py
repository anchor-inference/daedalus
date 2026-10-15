"""The who-edits-what board, wired to the agents that edit: native tools, CLI members, the team.

The store (``stores/edit_board.py``) knows leases and notes; this knows who an agent is. A native
Write, Edit or MultiEdit asks through the session manager's ``edit_board`` service hook, a CLI
member's runtime through its ``edit_board`` attribute, and both get back the line to append to the
tool's result. Nothing here ever reaches a system prompt or an earlier message: the note travels
only in the result of the edit that found the overlap, so the cached prefix is never touched.

A lease is dropped the moment its holder stops: a member's status leaving work (its turn ended, it
was released or dismissed) and a session's run finishing. The project orchestrator's state carries
the live board as one section, and the team page shows it from the staff listing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.host.events import AppEvent, EventFilter
from daedalus.stores.edit_board import EditBoard, Editor, Holder, canonical_path, repo_root, shown_path

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.stores.projects import Project

logger = logging.getLogger(__name__)

WORKING_STATUSES = frozenset({"starting", "working", "question", "permission", "no_signal"})
"""A member in one of these is still inside its turn; any other status means the turn is over and
its leases go. A question or a permission holds the turn open, and the files are still being changed."""
PRUNE_SECONDS = 300
STATE_HOLDERS = 8
STATE_FILES = 4


def _clock(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%H:%M")


class EditBoards:
    def __init__(self, app: Application) -> None:
        self.app = app
        assert app.manager is not None
        self.manager = app.manager

    @property
    def board(self) -> EditBoard:
        # Built per use from the live configuration, so a changed lease length applies at once.
        config = self.manager.config.edit_board
        return EditBoard(self.manager.db, lease_seconds=config.lease_seconds, reshow_seconds=config.reshow_minutes * 60)

    @property
    def enabled(self) -> bool:
        return bool(self.manager.config.edit_board.enabled)

    async def session_editor(self, session_id: str) -> Editor | None:
        """A native session as an editor: the member whose live session it is, else the session itself."""
        row = await self.manager.db.fetchone(
            "SELECT st.id, st.name, st.project_id FROM staff_sessions ss JOIN staff st ON st.id = ss.staff_id"
            " WHERE ss.session_id = ? AND ss.ended_at IS NULL", (session_id,))
        if row is not None:
            return Editor.member(row["id"], row["name"], row["project_id"])
        row = await self.manager.db.fetchone("SELECT title, project_id FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return Editor.session(session_id, str(row["title"] or "untitled"), row["project_id"])

    async def member_editor(self, staff_session_id: str) -> Editor | None:
        row = await self.manager.db.fetchone(
            "SELECT st.id, st.name, st.project_id FROM staff_sessions ss JOIN staff st ON st.id = ss.staff_id WHERE ss.id = ?",
            (staff_session_id,))
        return Editor.member(row["id"], row["name"], row["project_id"]) if row is not None else None

    async def native_edit(self, session_id: str, paths: Sequence[str]) -> str:
        """The service hook a native file tool calls after a successful write."""
        if not self.enabled:
            return ""
        editor = await self.session_editor(session_id)
        if editor is None:
            return ""
        return await self.board.record(editor, [canonical_path(p) for p in paths])

    async def cli_edit(self, staff_session_id: str, paths: Sequence[str], cwd: str) -> str:
        """What a CLI member's runtime calls for each edit its hooks or events report."""
        if not self.enabled:
            return ""
        editor = await self.member_editor(staff_session_id)
        if editor is None:
            return ""
        canonical = [canonical_path(p, cwd) for p in paths]
        root = repo_root(canonical[0], cwd) if canonical else cwd
        return await self.board.record(editor, canonical, root=root)

    async def on_status(self, event: AppEvent) -> None:
        status = str(event.payload.get("status") or "")
        # Release and Dismiss end the live session first, which reports it ``exited``: one rule
        # covers a turn's end and a member let go.
        if event.staff_id and status not in WORKING_STATUSES:
            await self.board.purge(f"staff:{event.staff_id}")

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if status != "awaiting":
            # A run waiting on the operator's answer has not ended; its files are still in hand.
            await self.board.purge(f"session:{session_id}")

    async def holders(self, project: Project) -> list[Holder]:
        return await self.board.holders(project_id=project.id, folders=[str(f.path) for f in project.folders])

    async def project_view(self, project: Project) -> list[dict[str, Any]]:
        """The board of one project for the app: who, which files, when last edited."""
        now = time.time()
        return [
            {"holder": h.key, "kind": h.kind, "name": h.name, "edited_at": datetime.fromtimestamp(h.edited_at, UTC).isoformat(),
             "age_s": max(0, int(now - h.edited_at)),
             "files": [{"path": f.path, "shown": shown_path(f.path, f.root)} for f in h.files]}
            for h in await self.holders(project)
        ]

    async def state_section(self, project: Project) -> tuple[list[str], str]:
        """The board in the orchestrator's state: one line per agent holding files now.

        Clock times rather than ages, so a board that did not change reads the same on the next turn
        and the state elision can leave it out."""
        holders = await self.holders(project)
        if not holders:
            return [], ""
        lines = ["Editing now (who → files; agents sharing a folder see a note when they overlap):"]
        for holder in holders[:STATE_HOLDERS]:
            files = ", ".join(shown_path(f.path, f.root) for f in holder.files[:STATE_FILES])
            if len(holder.files) > STATE_FILES:
                files += f" and {len(holder.files) - STATE_FILES} more"
            who = holder.name if holder.kind == "member" else f"session \"{holder.name}\""
            lines.append(f"  - {who} → {files} (last {_clock(holder.edited_at)} UTC)")
        if len(holders) > STATE_HOLDERS:
            lines.append(f"  - … {len(holders) - STATE_HOLDERS} more")
        return lines, ""

    async def prune_loop(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_SECONDS)
            try:
                await self.board.prune()
            except Exception:  # noqa: BLE001 — a missed prune leaves rows every read already filters
                logger.exception("pruning the edit board failed")


async def install(app: Application) -> list[asyncio.Task[None]]:
    boards = EditBoards(app)
    app.extensions["edit_board"] = boards
    manager = app.manager
    assert manager is not None
    manager.service_hooks["edit_board"] = boards.native_edit
    manager.on_finished(boards.on_run_finished)
    orchestrators: Any = app.extensions.get("orchestrator")
    if orchestrators is not None:
        orchestrators.state_sections.append(boards.state_section)
    team = app.extensions.get("staff")
    for runtime in dict(getattr(team, "runtimes", None) or {}).values():
        if hasattr(runtime, "edit_board"):
            runtime.edit_board = boards.cli_edit
    handler = manager.bus.on(EventFilter(types=("staff.status",)), boards.on_status, name="edit-board")
    pruner = asyncio.create_task(boards.prune_loop(), name="edit-board-prune")
    return [handler, pruner]


__all__ = ["EditBoards", "install"]
