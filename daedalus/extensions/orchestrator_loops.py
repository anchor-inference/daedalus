"""The register of open results: what the team handed back that no decision of the orchestrator has
followed yet.

A member's report of done, stuck or needs-input opens one; so does a card left in todo with nobody on
it for longer than a turn of the orchestrator. It closes with a decision about that work — the result
accepted or returned (``Accept``), the next step assigned on the card or after it, the member's
question answered, a question put to the operator, the card set aside as waiting for someone or
something named, the card dropped, or ``Decide``: nothing further, and why. The operator acting on
the card closes it too.

The register exists because the transitions were where work was lost, not the work itself: a member
proved a blocker and stopped, the orchestrator marked the card blocked, wrote the journal, told the
operator — and handed the next step to nobody, with two members free; a presentation the operator had
ordered sat unassigned behind a condition the orchestrator had made up. Nothing in either turn was
wrong on its own; what was missing was a decision, and nothing noticed its absence.

It never makes the orchestrator ask the operator anything, and it never assigns work. At the end of a
turn that left a result open, the host says so once, as an event; a second turn that still leaves it
open is not reminded again. Until it closes, the result stays in the state block's "Waiting for your
decision". One reminder per result is the whole of the pressure: an orchestrator that had to act on
every open result every turn would be pushed into assigning work to make the list go away.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.host.events import AppEvent

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

logger = logging.getLogger(__name__)

CAUSES = {"done": "report_done", "stuck": "report_stuck", "needs_input": "report_needs_input"}
"""The reports that open a result, by their kind. A checkpoint is news, not a result."""
REPORT_CAUSES = frozenset(CAUSES.values())
UNOWNED = "task_unowned"
DECIDED = "decided: "
"""How a decision made with Decide is kept: nothing further on the work, for the reason after it."""
SUMMARY_CHARS = 160
LINES_MAX = 10


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class OpenResults:
    """The open results of every project with an orchestrator."""

    def __init__(self, orch: Orchestrators) -> None:
        self.orch = orch

    @property
    def db(self) -> Any:
        return self.orch.manager.db

    # -- opening --------------------------------------------------------------------------------

    async def reported(self, event: AppEvent) -> None:
        """A member's report: a result to decide on when it is done, stuck or needs input. A newer
        report about the same work replaces the older one, which it answers."""
        cause = CAUSES.get(str(event.payload.get("kind") or ""))
        if cause is None or not event.project_id or not event.staff_id:
            return
        if not await self._orchestrated(event.project_id):
            return
        task_id = str(event.payload.get("task_id") or "") or None
        await self.close(event.project_id, task_id=task_id, staff_id=None if task_id else event.staff_id, by="system", decision="a newer report replaced it", causes=REPORT_CAUSES)
        await self.db.execute(
            "INSERT INTO open_loops(project_id, task_id, staff_id, cause, event_seq, summary, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.project_id, task_id, event.staff_id, cause, event.seq, _one_line(str(event.payload.get("text") or ""), SUMMARY_CHARS), _now()),
        )

    async def _orchestrated(self, project_id: str) -> bool:
        project = await self.orch.manager.projects.get(project_id)
        return project is not None and project.settings.orchestrator.enabled

    async def unowned(self, project_id: str, before: str) -> list[str]:
        """Open a result for each card that has sat in todo with nobody on it since before ``before``
        — a turn of the orchestrator's has passed over it — and has none open yet."""
        rows = await self.db.fetchall(
            "SELECT t.id, t.title FROM board_tasks t WHERE t.project_id = ? AND t.status = 'todo' AND t.assignee_staff_id IS NULL AND t.created_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM open_loops l WHERE l.task_id = t.id AND l.cause = ? AND l.closed_at IS NULL)"
            # A card the orchestrator said waits (Decide) is not raised again every turn after.
            " AND NOT EXISTS (SELECT 1 FROM open_loops l WHERE l.task_id = t.id AND l.cause = ? AND l.decision LIKE ?)",
            (project_id, before, UNOWNED, UNOWNED, DECIDED + "%"),
        )
        for row in rows:
            await self.db.execute(
                "INSERT INTO open_loops(project_id, task_id, cause, summary, opened_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, row["id"], UNOWNED, _one_line(row["title"], SUMMARY_CHARS), _now()),
            )
        return [row["id"] for row in rows]

    # -- closing --------------------------------------------------------------------------------

    async def close(
        self,
        project_id: str,
        *,
        task_id: str | None = None,
        staff_id: str | None = None,
        by: str,
        decision: str,
        causes: frozenset[str] | set[str] | None = None,
    ) -> list[int]:
        """Close the open results of a card, or of a member's work without a card; their ids."""
        if not task_id and not staff_id:
            return []
        where = ["project_id = ?", "closed_at IS NULL"]
        params: list[Any] = [project_id]
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        else:
            where.append("task_id IS NULL AND staff_id = ?")
            params.append(staff_id)
        if causes:
            where.append(f"cause IN ({','.join('?' for _ in causes)})")
            params.extend(sorted(causes))
        rows = await self.db.fetchall(f"SELECT id FROM open_loops WHERE {' AND '.join(where)}", tuple(params))  # noqa: S608 — the clauses are this module's own
        ids = [int(r["id"]) for r in rows]
        if ids:
            await self.db.execute(
                f"UPDATE open_loops SET closed_at = ?, closed_by = ?, decision = ? WHERE id IN ({','.join('?' for _ in ids)})",  # noqa: S608
                (_now(), by, _one_line(decision, 500), *ids),
            )
        return ids

    async def on_task(self, event: AppEvent) -> None:
        """The board moved: a card someone took on is owned, a card dropped needs nothing, and a card
        the operator moved or assigned has had their decision."""
        task_id = str(event.payload.get("task_id") or "")
        if not task_id or not event.project_id:
            return
        actor = str(event.payload.get("actor") or "")
        to = str(event.payload.get("to") or "")
        if event.type == "task.assigned" and event.payload.get("assignee_staff_id"):
            await self.close(event.project_id, task_id=task_id, by=actor or "system", decision="assigned", causes={UNOWNED})
        if event.type == "task.moved" and to != "todo":
            await self.close(event.project_id, task_id=task_id, by=actor or "system", decision=f"moved to {to}", causes={UNOWNED})
        if event.type == "task.moved" and to == "dropped":
            await self.close(event.project_id, task_id=task_id, by=actor or "system", decision="dropped")
        if actor == "operator" and event.type in ("task.moved", "task.assigned", "task.accepted"):
            await self.close(event.project_id, task_id=task_id, by="operator", decision="the operator acted on the card")

    # -- what the orchestrator is shown ---------------------------------------------------------

    async def open(self, project_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT l.*, t.title, t.status, m.name AS staff_name FROM open_loops l LEFT JOIN board_tasks t ON t.id = l.task_id LEFT JOIN staff m ON m.id = l.staff_id"
            " WHERE l.project_id = ? AND l.closed_at IS NULL ORDER BY l.id",
            (project_id,),
        )
        out = []
        for row in rows:
            if row["task_id"] and row["title"] is None:
                # The card was deleted: nothing is left to decide on.
                await self.db.execute("UPDATE open_loops SET closed_at = ?, closed_by = 'system', decision = 'the card was deleted' WHERE id = ?", (_now(), row["id"]))
                continue
            out.append(dict(row))
        return out

    def line(self, loop: dict[str, Any], free: list[str]) -> str:
        card = f"\"{_one_line(loop['title'] or '', 60)}\" ({loop['task_id']})" if loop.get("task_id") else "no card"
        who = loop.get("staff_name") or "a member"
        if loop["cause"] == UNOWNED:
            said = f"{card} is in todo with nobody on it"
            nudge = "Assign it, or Decide why it waits"
        else:
            kind = {"report_done": "reported done", "report_stuck": "reported stuck", "report_needs_input": "needs input"}[loop["cause"]]
            said = f"{who} {kind} on {card}: \"{loop['summary']}\""
            nudge = {
                "report_done": "Accept it (accepted, returned or for the operator), or Decide",
                "report_stuck": "give the next step (Assign on the card or after it), ask the operator, set it aside as waiting on someone named, or Decide",
                "report_needs_input": "answer it (Tell), ask the operator, or Decide",
            }[loop["cause"]]
        others = [name for name in free if name != loop.get("staff_name")]
        spare = f"; free: {', '.join(others)}" if others and loop["cause"] != "report_done" else ""
        return f"[L{loop['id']}] {said} — {nudge}{spare}"

    async def free_members(self, project_id: str) -> list[str]:
        """Active members with no session at work on a card: the names that make "nothing to do but
        wait" a choice rather than a fact."""
        members = await self.orch.manager.staff.list(project_id)
        live = await self.orch.manager.staff.live_sessions(project_id)
        free = []
        for member in members:
            if member.one_off:
                continue
            session = live.get(member.id)
            if session is None or session.status in ("idle", "turn_done_unseen", "exited"):
                free.append(member.name)
        return free

    async def section(self, project_id: str) -> list[str]:
        """The open results as the state block lists them, oldest first."""
        loops = await self.open(project_id)
        if not loops:
            return []
        free = await self.free_members(project_id)
        return [self.line(loop, free) for loop in loops]

    # -- the end of a turn ----------------------------------------------------------------------

    async def turn_ended(self, project_id: str, started_at: str, *, delivered: int | None = None) -> list[int]:
        """After a turn of the orchestrator: cards it passed over with nobody on them are opened, and
        every open result it had been shown and not yet been reminded of is said once, as an event.

        ``delivered`` is the last event the orchestrator was given: a result whose report is still on
        its way to it is not its to have decided on yet. The ids reminded of."""
        if not await self._orchestrated(project_id):
            return []
        await self.unowned(project_id, started_at)
        loops = [loop for loop in await self.open(project_id) if loop["reminded_at"] is None and (delivered is None or int(loop["event_seq"] or 0) <= delivered)]
        if not loops:
            return []
        ids = [int(loop["id"]) for loop in loops]
        await self.db.execute(f"UPDATE open_loops SET reminded_at = ? WHERE id IN ({','.join('?' for _ in ids)})", (_now(), *ids))  # noqa: S608
        free = await self.free_members(project_id)
        lines = [self.line(loop, free) for loop in loops[:LINES_MAX]]
        try:
            await self.orch.manager.bus.publish("orchestrator.open_results", {"loops": ids, "lines": lines}, project_id=project_id)
        except Exception:  # noqa: BLE001 — the results stay in the state block either way
            logger.warning("could not remind the orchestrator of %s of its open results", project_id, exc_info=True)
        return ids


__all__ = ["CAUSES", "DECIDED", "OpenResults", "REPORT_CAUSES", "UNOWNED"]
