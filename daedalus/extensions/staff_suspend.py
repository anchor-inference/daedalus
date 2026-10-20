"""Suspending idle command-line members, and launching them again on their own session when needed.

A command-line member keeps its CLI running between tasks, and each one holds hundreds of megabytes
for a prompt nobody types into. A member that has nothing assigned, nothing asked, nothing on its way
to it and has been idle for ``harness.idle_suspend_minutes`` has its CLI stopped through the usual
stop path, its native session kept: the row says ``suspended``. The provider's prompt cache has
usually expired by then, so taking the session up later costs what a restart of the host costs.

The next message or assignment launches the CLI again with its resume flag on the same session, in
the same folder or worktree, and then delivers. A message goes into the same live session row, as a
message to an idle member always has; an assignment goes through the whole start (the queue, the
route, the attempt with its phase clocks, the writer gate) and takes the suspended conversation up
when the new task is worked where it was.

Only a runtime that can take a native session up again is ever suspended, and only once its native
session is known. Nothing is suspended while anything is in flight; every condition is read again
under the session's lock immediately before the stop; a stop whose outcome cannot be confirmed is
reported as an error and never tried again by itself. A host restarted with a suspended member
leaves it suspended: the launch was ended with the stop, so there is nothing for the reconcile to
take up, and a launch the previous host left open in the middle of a suspend is ended there.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.launch_route import own, refusal
from daedalus.harness.conditions import SUSPENSION_KEY
from daedalus.harness.runtime import SuspendUnknown
from daedalus.staff_runtime import LiveSession, StartRequest
from daedalus.stores.executions import ACTIVE
from daedalus.stores.staff import StaffError
from daedalus.stores.staff_context import staff_context_packet
from daedalus.stores.writer_leases import WriterLeases

if TYPE_CHECKING:
    from daedalus.extensions.staff import Team

logger = logging.getLogger(__name__)

SUSPENDED = "suspended"
CANDIDATES = ("idle", "turn_done_unseen")
"""The statuses an idle member sits in. ``error`` is not one: a failed turn is for someone to look at."""
KEY = SUSPENSION_KEY
"""The kv record of one suspension, by staff session: when, after how long, and the native session."""


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def idle_since(session: Any) -> datetime | None:
    """When the member last did or heard anything: its status changed, or it signalled."""
    stamps = [at for at in (_parse(session.status_at), _parse(session.last_signal_at)) if at is not None]
    return max(stamps) if stamps else None


def words(minutes: int) -> str:
    return f"suspended after {minutes} min idle; the next message or assignment resumes it on the same session"


async def record(team: Team, staff_session_id: str) -> dict[str, Any] | None:
    found = await team.manager.db.kv_get(KEY + staff_session_id)
    return found if isinstance(found, dict) else None


async def blocker(team: Team, live: LiveSession, minutes: int, now: datetime | None = None) -> str:
    """What the team knows that keeps the member from being suspended now; empty when nothing does.

    The runtime adds what only it holds (its delivery queue, the requests of the CLI, the readiness).
    """
    session, member = live.session, live.staff
    db = team.manager.db
    if session.kind != "cli" or session.status not in CANDIDATES:
        return f"it is {session.status}"
    since = idle_since(session)
    now = now or datetime.now(UTC)
    if since is None or (now - since).total_seconds() < minutes * 60:
        return "it has not been idle long enough"
    if session.task_id:
        task = await team.task(session.task_id)
        if task is not None and task.status not in ("review", "done", "dropped"):
            return f"its task {task.id} is {task.status}"
    if await db.fetchone("SELECT 1 FROM board_tasks WHERE assignee_staff_id = ? AND status = 'doing' LIMIT 1", (member.id,)):
        return "a task of it is in doing"
    if team.queue.waiting_for(member.id):
        return "a launch of it waits in the queue"
    if await db.fetchone("SELECT 1 FROM asks WHERE staff_session_id = ? AND resolved_at IS NULL LIMIT 1", (session.id,)):
        return "a request of it is open"
    # A message typed and never acknowledged is not counted forever: one older than the idle time was
    # settled by a turn that has ended since, whatever its receipt says.
    cutoff = datetime.fromtimestamp(now.timestamp() - minutes * 60, UTC).isoformat()
    if await db.fetchone("SELECT 1 FROM staff_messages WHERE staff_session_id = ? AND (state IN ('queued', 'written')"
                         " OR (state = 'submitted' AND updated_at > ?)) LIMIT 1", (session.id, cutoff)):
        return "a message is on its way to it"
    attempts = await db.fetchall("SELECT id, state FROM execution_attempts WHERE staff_session_id = ?", (session.id,))
    if any(row["state"] in (*ACTIVE, "recovering") for row in attempts):
        return "an attempt of it is still open"
    for row in attempts:
        # A contained launch is bound to its attempt; a launch again without one could not be.
        if await db.fetchone("SELECT 1 FROM attempt_resource_bindings WHERE attempt_id = ?"
                             " UNION ALL SELECT 1 FROM writer_attempt_bindings WHERE attempt_id = ?", (row["id"], row["id"])):
            return "it runs contained"
    profile = await db.fetchone("SELECT state FROM resource_profile_versions WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
                                (member.project_id,))
    if profile is not None and profile["state"] == "enabled":
        return "its project launches only contained workers"
    terminals: Any = team.app.extensions.get("terminals")
    if terminals is not None and session.terminal_id:
        try:
            view = await terminals.get(session.terminal_id)
        except Exception:  # noqa: BLE001 — a terminal that cannot be read is not stopped on a guess
            return "its terminal could not be read"
        live_view = view.get("live") or {}
        if int(live_view.get("clients") or 0) > 0:
            return "someone has its terminal open"
        typed = _parse(view.get("last_input_at"))
        if typed is not None and (now - typed).total_seconds() < minutes * 60:
            return "its terminal had input recently"
    return ""


async def suspend_idle(team: Team, now: datetime | None = None) -> int:
    """Suspend every command-line member idle past the configured time; returns how many were."""
    minutes = int(team.manager.config.harness.idle_suspend_minutes)
    if minutes <= 0:
        return 0
    now = now or datetime.now(UTC)
    done = 0
    for session in await team.manager.staff.all_live():
        if session.kind != "cli" or session.status not in CANDIDATES:
            continue
        since = idle_since(session)
        if since is None or (now - since).total_seconds() < minutes * 60:
            continue
        live = await team.live(session.id)
        if live is None:
            continue
        runtime: Any = team.runtimes.get(live.staff.harness)
        if runtime is None or not hasattr(runtime, "suspend") or runtime.suspendable(live):
            continue
        if await blocker(team, live, minutes, now):
            continue
        member_id = live.staff.id
        if team.suspend_lock(member_id).locked() or team.execution_lock(member_id).locked():
            # A wake, a start or a stop of the member is under way: the tick does not wait for it,
            # and the member is not idle anyway.
            continue
        # The suspend lock keeps a message from waking a member half stopped; the execution lock
        # keeps a start or a release of the member from overtaking the stop.
        async with team.suspend_lock(member_id), team.execution_lock(member_id):
            if await suspend(team, live, minutes, now):
                done += 1
    return done


async def suspend(team: Team, live: LiveSession, minutes: int, now: datetime) -> bool:
    """One member's suspend, under its suspend and execution locks. True when its CLI stopped and it
    is suspended."""
    runtime: Any = team.runtimes[live.staff.harness]

    async def recheck(fresh: LiveSession) -> str:
        # Against the sweep's moment: a status or a signal written since then is newer than it, and
        # reads as not idle long enough.
        return await blocker(team, fresh, minutes, now)

    async def mark(fresh: LiveSession) -> None:
        # Written before the stop: a host that goes in the middle leaves a row that says what was meant.
        await team.manager.db.kv_set(KEY + fresh.id, {"since": datetime.now(UTC).isoformat(), "idle_minutes": minutes,
                                                     "cli_session_id": fresh.session.cli_session_id,
                                                     "terminal_id": fresh.session.terminal_id})
        await team.ingress.status(fresh, SUSPENDED, detail=words(minutes))

    try:
        why = await runtime.suspend(live, recheck=recheck, mark=mark)
    except SuspendUnknown as exc:
        logger.warning("the idle suspend of %s has an unknown outcome: %s", live.staff.name, exc)
        fresh = await team.live(live.id)
        if fresh is not None:
            await team.ingress.status(fresh, "error", detail=(
                f"the idle suspend's outcome is unknown: {exc}. It is not tried again; look at its terminal, or release the member"))
        return False
    if why:
        logger.info("%s is not suspended: %s", live.staff.name, why)
        return False
    logger.info("suspended %s after %d min idle; its %s session %s is kept", live.staff.name, minutes, live.staff.harness,
                live.session.cli_session_id)
    return True


async def _pinned(team: Team, staff_session_id: str) -> str:
    """The session's pinned task packet for the standing brief of a relaunch, or nothing.

    The first launch's standing brief carried it, and the first message only points at it, so a
    relaunch that left it out would leave the member with no contract once its history is shortened.
    """
    pinned = await staff_context_packet(team.app.db, staff_session_id)
    return "\n\n" + team.context_brief(staff_session_id, pinned["packet"]) if pinned else ""


async def wake(team: Team, live: LiveSession) -> LiveSession:
    """Launch a suspended member's CLI again on its own session, before a message is delivered to it.

    Called under the member's suspend lock. The member's own runtime takes it, or nothing does: a
    runtime the route would refuse now (a spent window, a lost sign-in) leaves the member suspended
    and the message undelivered, with the reason. Moving a conversation to another runtime is the
    operator's reviewed handoff, or a new assignment the route places, never a silent relaunch.
    """
    member, session = live.staff, live.session
    project = await team.project(member.project_id)
    folder = project.folder(session.folder_id) if session.folder_id else None
    folder = folder or team.folder_for(project, member, None)
    why = await refusal(team, member, own(member), folder)
    if why:
        raise StaffError(f"{member.name} is suspended, and {member.harness} cannot take its session up now: {why}."
                         " It stays suspended; assign the task again for the project's route to place it, or hand the work off")
    runtime: Any = team.runtime(member)
    worktree = await team.worktree_of(session)
    cwd = session.launch_cwd or (str(worktree.cwd) if worktree is not None else str(folder.path))
    task = await team.task(session.task_id) if session.task_id else None
    request = StartRequest(
        staff=member,
        project=project,
        folder=folder,
        cwd=Path(cwd),
        worktree=worktree,
        task=task,
        # Nothing as a first prompt: the message that woke it is delivered once the CLI is ready.
        first_message="",
        brief_text=await team.brief(member, project, folder, worktree) + await _pinned(team, session.id),
        staff_session_id=session.id,
        env=folder.env,
        model=member.model,
        effort=member.effort,
        agent=member.agent,
        permission_level=team.permission_level(project),
        permission_mode=team.launch_permission_mode(member),
        team_url=f"http://127.0.0.1:{team.app.settings.api_port}/api/team/{session.id}",
        # The team tools post to the launch's hook listener; the token is minted for the team route,
        # which a command-line member does not reach (see LaunchSpec.team_token).
        team_token="",
        allow_rules=tuple(r["rule"] for r in await team.manager.staff.allow_rules(member.id)),
    )
    async with team.execution_lock(member.id):
        await team.ingress.status(live, "starting", detail="resuming its session after an idle suspend")
        fresh = await team.live(session.id) or live
        try:
            if member.isolation == "readonly":
                started = await runtime.resume(request, fresh)
            else:
                # The same gate as any uncontained writable launch: none while a contained writer
                # holds the project folders.
                async with WriterLeases(team.app.executions).uncontained_start():
                    started = await runtime.resume(request, fresh)
        except (Exception, asyncio.CancelledError) as exc:
            # The runtime undoes a launch that failed (its terminals killed, its launch ended), so the
            # member is suspended as before and the next message tries again.
            back = await team.live(session.id)
            if back is not None:
                await team.ingress.status(back, SUSPENDED, detail=f"resuming failed: {exc}"[:500])
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StaffError(f"{member.name} could not be resumed: {exc}") from exc
    await team.manager.staff.started(session.id, terminal_id=started.terminal_id, cli_session_id=started.cli_session_id,
                                     transcript_ref=started.transcript_ref)
    await team.manager.db.execute("DELETE FROM kv WHERE key = ?", (KEY + session.id,))
    logger.info("resumed %s on its %s session %s", member.name, member.harness, session.cli_session_id)
    return await team.live(session.id) or fresh


__all__ = ["SUSPENDED", "blocker", "idle_since", "record", "suspend", "suspend_idle", "wake", "words"]
