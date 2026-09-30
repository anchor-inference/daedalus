"""Dispatches: work the main orchestrator hands to a project, and the reports that close it.

``app.extensions["dispatches"]`` is the one place a dispatch changes. The main orchestrator opens
one (``dispatch.created``) or adds a follow-up (``dispatch.message``); both wake the project's
orchestrator at once. The project's orchestrator answers with ``ProjectReport(dispatch_id=…)``: a
progress report is a message on the dispatch, a done or blocked report closes it
(``dispatch.closed``). Those two — and a stalled dispatch — are the only things that wake the main
orchestrator, and nothing it does wakes itself, so the two cannot keep each other busy.

A dispatch that goes quiet is watched here too: open for ``dispatcher.stalled_minutes`` with nothing
said on it, while nobody in its project works and its orchestrator is not mid-turn, it raises
``dispatch.stalled`` once. The main orchestrator tells the operator; it does not prod the project.

Questions travel with a dispatch without being copied. A request row names the dispatch it belongs to
(``asks.dispatch_id``), and every window shows that same row. While a project is being set up by the
main orchestrator, every request of the project is linked to its first dispatch. A request of the
orchestrator's own is withdrawn when its dispatch is cancelled, since nobody is waiting on its answer.
A dispatch closed as done or blocked leaves its questions open: an orchestrator that finished the
survey and still asked what only the operator knows is waiting on that answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.host.events import AppEvent, EventFilter
from daedalus.stores.dispatches import CLOSED, Dispatch, DispatchError
from daedalus.stores.files import MAIN, StoredFile, file_ref
from daedalus.stores.projects import Project
from daedalus.stores.staff import ACTIVE_STATUSES, Ask

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager

logger = logging.getLogger(__name__)

WATCH_SECONDS = 60.0
"""How often the watchdog looks for quiet dispatches; a stall is news on the scale of minutes."""
STATE_LINES = 8
TEXT_CHARS = 200
SETUP_BY = "dispatcher"
REPORT_KINDS = ("progress", "done", "blocked", "decision")


def _now() -> datetime:
    return datetime.now(UTC)


def _one_line(text: str, limit: int) -> str:
    flat = " / ".join(line.strip() for line in (text or "").strip().splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _minutes_since(at: str, now: datetime) -> int:
    try:
        then = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    return max(0, int((now - then).total_seconds() // 60))


def _age(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


class Dispatches:
    """Every dispatch of the installation: opening, following up, reporting, cancelling, stalling."""

    def __init__(self, app: Application) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        self.store = self.manager.dispatches
        self.clock = _now
        """The watchdog's idea of now; a test moves it."""

    # -- opening and following up ----------------------------------------------------------------

    async def create(self, project: Project, *, text: str, title: str = "", from_session: str = "", kind: str = "work", files: Sequence[StoredFile] = ()) -> Dispatch:
        """A new dispatch for the project; its orchestrator is woken with it at once. ``files`` are the
        main orchestrator's, made the project's here, so the handles the orchestrator is told open."""
        dispatch = await self.store.create(project.id, text=text, title=title, from_session=from_session, kind=kind)
        refs = await self._share(files, project.id, dispatch.id)
        await self.manager.projects.record(
            project.id, "system", "dispatch",
            # Named, not quoted: the text is the dispatch's own, whole, one Peek away, and a quote cut to
            # fit the journal was taken for the whole request.
            f"The main orchestrator handed over dispatch {dispatch.id} (#{dispatch.seq}){': ' + dispatch.title if dispatch.title else ''}, "
            f"{len(dispatch.text)} characters; Peek(op='dispatch', ref='{dispatch.id}') reads it whole"
            + (f" — files: {', '.join(f.short() for f in files)}" if files else ""),
            {"dispatch_id": dispatch.id, **({"files": [r["id"] for r in refs]} if refs else {})},
        )
        await self._publish(
            "dispatch.created",
            {"dispatch_id": dispatch.id, "seq": dispatch.seq, "title": dispatch.title, "text": dispatch.text, "kind": dispatch.kind, "from_session": from_session, "actor": "dispatcher", **({"files": refs} if refs else {})},
            project.id,
        )
        return dispatch

    async def _share(self, files: Sequence[StoredFile], scope: str, dispatch_id: str, *, actor: str = "dispatcher") -> list[dict[str, Any]]:
        """Grant files to the scope that receives them with a dispatch (the project going down, the main
        orchestrator coming back up), and name them as an event carries them."""
        refs = []
        for stored in files:
            await self.manager.files.grant(stored, scope, actor=actor, target=f"dispatch {dispatch_id}")
            refs.append(file_ref(stored))
        return refs

    async def follow_up(self, dispatch: Dispatch, text: str, *, author: str = "dispatcher", files: Sequence[StoredFile] = ()) -> Dispatch:
        """More for an open dispatch — a correction, a detail, the answer it was blocked on. A blocked
        dispatch opens again with it; a done or cancelled one is over, and new work is a new dispatch."""
        if dispatch.status in CLOSED:
            raise DispatchError(f"dispatch {dispatch.id} is {dispatch.status}; hand the project new work as a new dispatch")
        reopened = await self.store.reopen(dispatch.id) if dispatch.status == "blocked" else False
        refs = await self._share(files, dispatch.project_id, dispatch.id, actor=author)
        body = text + (f"\n\nFiles: {', '.join(f.short() for f in files)}" if files else "")
        await self.store.add_message(dispatch.id, author=author, kind="message", text=body)
        await self._publish(
            "dispatch.message",
            {"dispatch_id": dispatch.id, "author": author, "kind": "message", "text": text, "title": dispatch.title, "actor": author, **({"files": refs} if refs else {})},
            dispatch.project_id,
        )
        if reopened:
            await self._publish("dispatch.updated", {"dispatch_id": dispatch.id, "status": "open", "change": "reopened", "actor": author}, dispatch.project_id)
        current = await self.store.get(dispatch.id)
        assert current is not None
        return current

    async def cancel(self, dispatch: Dispatch, *, reason: str = "", by: str = "dispatcher") -> Dispatch:
        """Tell the project's orchestrator to stop that work, and close the dispatch as cancelled."""
        if not dispatch.open and dispatch.status != "blocked":
            raise DispatchError(f"dispatch {dispatch.id} is already {dispatch.status}")
        why = " ".join((reason or "").split())[:500]
        text = "Cancelled: stop the work of this dispatch and do not report on it again." + (f" Reason: {why}" if why else "")
        await self.store.add_message(dispatch.id, author=by if by in ("dispatcher", "operator") else "dispatcher", kind="cancelled", text=text)
        if dispatch.status == "blocked":
            await self.store.reopen(dispatch.id)
        if not await self.store.close(dispatch.id, "cancelled", why or "cancelled"):
            current = await self.store.get(dispatch.id)
            raise DispatchError(f"dispatch {dispatch.id} is already {current.status if current else 'gone'}")
        # The project hears it as a message on the dispatch, which wakes its orchestrator; the main
        # orchestrator that cancelled it is not woken by its own close.
        await self._publish("dispatch.message", {"dispatch_id": dispatch.id, "author": "dispatcher", "kind": "cancelled", "text": text, "title": dispatch.title, "actor": "dispatcher"}, dispatch.project_id)
        await self._closed(dispatch, "cancelled", why or "cancelled", by=by)
        current = await self.store.get(dispatch.id)
        assert current is not None
        return current

    # -- the project's reports -------------------------------------------------------------------------

    async def report(self, project: Project, dispatch_id: str, *, kind: str, text: str, title: str = "", files: Sequence[StoredFile] = ()) -> Dispatch:
        """A ``ProjectReport`` on a dispatch: progress and decisions are its messages, done and blocked
        close it. Refused for another project's dispatch and for one that is already over."""
        dispatch = await self.store.get(dispatch_id)
        if dispatch is None or dispatch.project_id != project.id:
            raise DispatchError(f"{project.name} has no dispatch {dispatch_id!r}; the state block lists its open dispatches")
        if kind not in REPORT_KINDS:
            raise DispatchError(f"kind is one of {', '.join(REPORT_KINDS)}")
        if not dispatch.open:
            raise DispatchError(f"dispatch {dispatch.id} is already {dispatch.status}; it takes no more reports")
        body = (f"{title.strip()}\n" if title.strip() else "") + text.strip()
        # Made the main orchestrator's before the report is heard of, so the handles it is told open.
        refs = await self._share(files, MAIN, dispatch.id, actor="orchestrator")
        kept = body + (f"\n\nFiles: {', '.join(f.short() for f in files)}" if files else "")
        await self.store.add_message(dispatch.id, author="orchestrator", kind=kind, text=kept)
        if kind in ("done", "blocked"):
            if not await self.store.close(dispatch.id, kind, kept):
                current = await self.store.get(dispatch.id)
                raise DispatchError(f"dispatch {dispatch.id} is already {current.status if current else 'gone'}")
            await self._closed(dispatch, kind, body, by="orchestrator", files=refs)
        else:
            await self._publish(
                "dispatch.message",
                {"dispatch_id": dispatch.id, "author": "orchestrator", "kind": kind, "text": body[:4000], "title": dispatch.title, "actor": "orchestrator", **({"files": refs} if refs else {})},
                project.id,
            )
        current = await self.store.get(dispatch.id)
        assert current is not None
        return current

    async def block_open(self, project: Project, reason: str) -> list[Dispatch]:
        """Mark every open dispatch of the project blocked for a reason of the host's own — its
        orchestrator cannot run — so the main orchestrator is woken and its card stops saying "in
        progress" for work nobody is doing. The dispatches this blocked, oldest first."""
        blocked: list[Dispatch] = []
        for dispatch in await self.store.open_for(project.id):
            await self.store.add_message(dispatch.id, author="system", kind="blocked", text=reason)
            if not await self.store.close(dispatch.id, "blocked", reason):
                continue  # closed by a report or a cancel in the meantime; that one stands
            await self._closed(dispatch, "blocked", reason, by="system")
            blocked.append(dispatch)
        return blocked

    async def unblock(self, project: Project) -> list[Dispatch]:
        """Open again the dispatches :meth:`block_open` blocked and nobody has touched since: the last
        word on each is still the host's. One the main orchestrator followed up or cancelled is its."""
        reopened: list[Dispatch] = []
        candidates = [d for d in await self.store.recent(project_id=project.id, limit=40) if d.status == "blocked"]
        last = await self.store.last_messages([d.id for d in candidates])
        for dispatch in sorted(candidates, key=lambda d: d.seq):
            said = last.get(dispatch.id)
            if said is None or (said.author, said.kind) != ("system", "blocked"):
                continue
            if not await self.store.reopen(dispatch.id):
                continue
            await self.store.add_message(dispatch.id, author="system", kind="reopened", text="The orchestrator can run again; the dispatch is open again.")
            await self._publish("dispatch.updated", {"dispatch_id": dispatch.id, "status": "open", "change": "reopened", "actor": "system"}, project.id)
            reopened.append(dispatch)
        return reopened

    async def _closed(self, dispatch: Dispatch, status: str, result: str, *, by: str, files: list[dict[str, Any]] | None = None) -> None:
        await self._publish("dispatch.closed", {"dispatch_id": dispatch.id, "status": status, "result": result[:4000], "by": by, "title": dispatch.title, "seq": dispatch.seq, "kind": dispatch.kind, "actor": by, **({"files": files} if files else {})}, dispatch.project_id)
        await self._publish("dispatch.updated", {"dispatch_id": dispatch.id, "status": status, "change": "closed", "actor": by}, dispatch.project_id)
        await self.manager.projects.record(dispatch.project_id, "system", "dispatch", f"Dispatch {dispatch.id} (#{dispatch.seq}) closed as {status} by the {by}.", {"dispatch_id": dispatch.id})
        # Only a cancelled dispatch takes its orchestrator's questions with it. Withdrawing them on "done"
        # once closed the question a finished survey had just asked, before the operator saw it.
        if status == "cancelled":
            await self.withdraw_for(dispatch, why=f"dispatch {dispatch.id} was cancelled")
        if dispatch.kind == "setup" and status == "done":
            await self.finish_setup(dispatch.project_id, by="orchestrator")

    # -- the requests shown under a dispatch -------------------------------------------------------------

    async def default_dispatch(self, project_id: str) -> str | None:
        """The asks store's hook: during the setup every request of the project is dispatch #1's."""
        row = await self.manager.db.fetchone(
            "SELECT d.id FROM projects p JOIN dispatches d ON d.project_id = p.id AND d.seq = 1 WHERE p.id = ? AND p.setup_by = ? AND d.status IN ('open', 'blocked')",
            (project_id, SETUP_BY),
        )
        return str(row["id"]) if row is not None else None

    async def withdraw_for(self, dispatch: Dispatch, *, why: str) -> int:
        """Close the orchestrator's own requests still open on a dispatch that is over: nobody is waiting
        on their answers. A staff member's request stays; the member is still waiting."""
        count = 0
        for ask in await self.manager.asks.of_dispatches([dispatch.id]):
            if ask.origin == "orchestrator" and await self.withdraw(ask, why=why):
                count += 1
        return count

    async def withdraw_project(self, project_id: str, *, why: str) -> int:
        """The orchestrator's requests shown under a dispatch, when that orchestrator left its office."""
        rows = await self.manager.db.fetchall(
            "SELECT id FROM asks WHERE project_id = ? AND origin = 'orchestrator' AND dispatch_id IS NOT NULL AND resolved_at IS NULL", (project_id,)
        )
        count = 0
        for row in rows:
            ask = await self.manager.asks.get(row["id"])
            if ask is not None and await self.withdraw(ask, why=why):
                count += 1
        return count

    async def withdraw(self, ask: Ask, *, why: str) -> bool:
        """Close a request nobody will answer: the team's withdrawal, which every window collapses."""
        team = self.app.extensions.get("staff")
        if team is None:
            return False
        withdrawn: bool = await team.withdraw(ask, why=why)
        return withdrawn

    async def finish_setup(self, project_id: str, *, by: str) -> bool:
        """The project's setup is over: its requests are its own again. Whether it was being set up."""
        if not await self.manager.projects.set_setup(project_id, "", expect=SETUP_BY):
            return False
        await self.manager.projects.record(project_id, "system", "setup", f"The setup by the main orchestrator is finished ({by}).", {})
        await self._publish("project.changed", {"change": "setup.finished", "actor": by}, project_id)
        return True

    # -- what the project's orchestrator sees ------------------------------------------------------------

    async def state_section(self, project: Project) -> tuple[list[str], str]:
        """The open dispatches in the project orchestrator's state block."""
        now = self.clock()
        lines: list[str] = []
        if project.setup_by == SETUP_BY:
            lines.append("Setup: the main orchestrator is setting this project up; every question you ask is shown in its chat too.")
        dispatches = [d for d in await self.store.recent(project_id=project.id, limit=40) if d.status in ("open", "blocked")]
        if not dispatches:
            return lines, ""
        lines.append("Dispatches from the main orchestrator (close each with exactly one ProjectReport(dispatch_id=…, kind=done|blocked); Peek(op='dispatch', ref=…) reads one whole):")
        last = await self.store.last_messages([d.id for d in dispatches])
        for dispatch in sorted(dispatches, key=lambda d: d.seq)[:STATE_LINES]:
            said = last.get(dispatch.id)
            tail = f"; last: {said.author} {said.kind} \"{_one_line(said.text, 120)}\"" if said is not None else ""
            heading = f"{dispatch.title}: " if dispatch.title else ""
            lines.append(f"  - [{dispatch.id}] #{dispatch.seq} {dispatch.status}, {_age(_minutes_since(dispatch.created_at, now))}: {heading}\"{_one_line(dispatch.text, TEXT_CHARS)}\"{tail}")
        if len(dispatches) > STATE_LINES:
            lines.append(f"  - … {len(dispatches) - STATE_LINES} more (Journal)")
        return lines, "Journal"

    # -- the watchdog ----------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Raise ``dispatch.stalled`` for every open dispatch that went quiet while nobody works on it."""
        minutes = self.manager.config.dispatcher.stalled_minutes
        now = self.clock()
        before = (now - timedelta(minutes=minutes)).isoformat()
        raised = 0
        for dispatch in await self.store.quiet(before):
            if await self._busy(dispatch.project_id):
                continue
            if not await self.store.mark_stalled(dispatch.id, quiet_since=before):
                continue
            quiet = _minutes_since(dispatch.updated_at, now)
            await self._publish("dispatch.stalled", {"dispatch_id": dispatch.id, "minutes": quiet, "title": dispatch.title, "seq": dispatch.seq, "actor": "system"}, dispatch.project_id)
            raised += 1
        return raised

    async def _busy(self, project_id: str) -> bool:
        """Whether anyone in the project is at work: a staff member mid-task, or its orchestrator mid-turn."""
        live = await self.manager.staff.live_sessions(project_id)
        if any(s.status in ACTIVE_STATUSES for s in live.values()):
            return True
        project = await self.manager.projects.get(project_id)
        session_id = project.settings.orchestrator.session_id if project is not None and project.settings.orchestrator.enabled else ""
        state = self.manager.live_state(session_id) if session_id else None
        return state is not None and state.running

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a failed look is tried again at the next one
                logger.exception("the dispatch watchdog failed")

    # -- plumbing ------------------------------------------------------------------------------------

    async def on_project_changed(self, project_id: str, change: str) -> None:
        """An orchestrator that left its office leaves its linked questions behind: they are withdrawn."""
        if change in ("orchestrator.replaced", "orchestrator.disabled"):
            await self.withdraw_project(project_id, why="the orchestrator that asked was " + ("replaced" if change.endswith("replaced") else "switched off"))

    async def _publish(self, event_type: str, payload: dict[str, Any], project_id: str | None) -> None:
        try:
            await self.manager.bus.publish(event_type, payload, project_id=project_id)
        except Exception:  # noqa: BLE001 — the dispatch is written; the event is how others hear of it
            logger.warning("could not publish %s", event_type, exc_info=True)


async def install(app: Application) -> list[asyncio.Task[None]]:
    dispatches = Dispatches(app)
    app.extensions["dispatches"] = dispatches
    manager = app.manager
    assert manager is not None
    manager.asks.default_dispatch = dispatches.default_dispatch
    orchestrators: Any = app.extensions.get("orchestrator")
    if orchestrators is not None:
        orchestrators.state_sections.append(dispatches.state_section)

    async def on_changed(event: AppEvent) -> None:
        if event.project_id:
            await dispatches.on_project_changed(event.project_id, str(event.payload.get("change") or ""))

    handler = manager.bus.on(EventFilter(types=("project.changed",)), on_changed, name="dispatches")
    watchdog = asyncio.create_task(dispatches.watch(), name="dispatch-watchdog")
    return [handler, watchdog]


__all__ = ["Dispatches", "SETUP_BY", "install"]
