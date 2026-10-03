"""A project orchestrator's wake-ups: the alarms it sets itself with ``WakeMe``, and the ones the
operator leaves it from the app.

A wake-up is a schedule of kind ``wake`` (:mod:`daedalus.extensions.scheduler`) aimed at the
orchestrator's session. When it fires the scheduler publishes ``schedule.fired`` on the project and
the orchestrator's wake queue delivers the note at once. A wake-up belongs to the project rather
than to one session: the listing finds it through any session that is or was the project's
orchestrator, and taking the office points every wake-up at the new holder.

What bounds them, and why: every wake-up is a turn of the strongest model the operator has, and a
fired wake-up is urgent, so the hourly cap on routine wake-ups does not hold it back. A cron that
fires every minute would therefore buy sixty turns an hour. So a recurring wake-up may not come
round more often than ``orchestrator.wake_cron_min_minutes``, and a project holds at most
``orchestrator.wakeups_max`` of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from daedalus.stores.control import ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.stores.projects import Project

NOTE_MAX = 500
NAME_MAX = 80
"""The note is the wake-up's name in the list of schedules, shortened to this many characters."""
IN_MINUTES_MAX = 60 * 24 * 60
"""Sixty days: past that an alarm is a plan, and the brief or the board is the place for it."""
AT_MAX_DAYS = 366
CRON_CHECKS = 24
"""How many consecutive occurrences of a cron are measured to find its shortest gap."""
TICK_NOTE = "The scheduler looks every 30 seconds, so a wake-up can come up to half a minute late."

_OF_PROJECT = (
    "coalesce(json_extract(s.metadata, '$.orchestrator_of'), json_extract(s.metadata, '$.orchestrator_retired_of')) = ?"
)


class WakeupRefused(ValueError):
    """A wake-up that will not be set, said so that whoever asked can set a different one."""


def _zone(app: Application) -> tzinfo:
    """The operator's zone as their app last reported it: an ``at`` without an offset is their time."""
    manager = app.manager
    tz = manager.presence.locale()[1] if manager is not None else ""
    try:
        return ZoneInfo(tz) if tz else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def shortest_gap(cron: str, start: datetime) -> timedelta:
    """The shortest time between two of the next :data:`CRON_CHECKS` occurrences of ``cron``."""
    it = croniter(cron, start)
    previous = it.get_next(datetime)
    gap = timedelta(days=366)
    for _ in range(CRON_CHECKS):
        following = it.get_next(datetime)
        gap = min(gap, following - previous)
        previous = following
    return gap


def resolve_when(app: Application, *, at: str | None, in_minutes: int | None, cron: str | None, now: datetime | None = None) -> tuple[str | None, str | None]:
    """``(run_at, cron)`` for the scheduler from the three ways of saying when; exactly one is given."""
    given = [name for name, value in (("at", at), ("in_minutes", in_minutes), ("cron", cron)) if value not in (None, "")]
    if len(given) != 1:
        raise WakeupRefused("give exactly one of at, in_minutes or cron")
    moment = now or datetime.now(UTC)
    config = app.config.orchestrator
    if cron:
        cron = " ".join(cron.split())
        if not croniter.is_valid(cron):
            raise WakeupRefused(f"{cron!r} is not a cron expression (minute hour day month weekday, in UTC)")
        if shortest_gap(cron, moment) < timedelta(minutes=config.wake_cron_min_minutes):
            raise WakeupRefused(f"a recurring wake-up comes round at most every {config.wake_cron_min_minutes} minutes; every wake-up is a turn")
        return None, cron
    if in_minutes is not None:
        try:
            minutes = int(in_minutes)
        except (TypeError, ValueError) as exc:
            raise WakeupRefused("in_minutes is a whole number of minutes") from exc
        if not 1 <= minutes <= IN_MINUTES_MAX:
            raise WakeupRefused(f"in_minutes is between 1 and {IN_MINUTES_MAX}")
        return (moment + timedelta(minutes=minutes)).isoformat(), None
    try:
        when = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WakeupRefused(f"{at!r} is not a moment; use ISO 8601, e.g. 2026-10-01T09:30 (the operator's time) or with an offset") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=_zone(app))
    when = when.astimezone(UTC)
    if when <= moment:
        raise WakeupRefused(f"{at} has already passed; give a moment in the future")
    if when - moment > timedelta(days=AT_MAX_DAYS):
        raise WakeupRefused(f"a wake-up is at most {AT_MAX_DAYS} days ahead")
    return when.isoformat(), None


async def count(app: Application, project_id: str) -> int:
    row = await app.db.fetchone(
        f"SELECT count(*) AS n FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.kind = 'wake' AND sc.enabled = 1 AND {_OF_PROJECT}",
        (project_id,),
    )
    return int(row["n"]) if row is not None else 0


def short_name(text: str) -> str:
    """The note as a schedule's name: whole if it fits, otherwise cut at a word and marked as cut.

    It was sliced at 80 characters, so the list of schedules ended a title on half a word with nothing
    to say more followed, and "против ст" read as a word of its own."""
    if len(text) <= NAME_MAX:
        return text
    cut = text[: NAME_MAX - 1]
    space = cut.rfind(" ")
    if space > NAME_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:") + "…"


async def set_wakeup(
    app: Application,
    project: Project,
    *,
    note: str,
    at: str | None = None,
    in_minutes: int | None = None,
    cron: str | None = None,
    by_session: str | None = None,
    principal: Principal | None = None,
    client_operation_id: str | None = None,
    expected_collection_revision: int | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Set a wake-up for the project's orchestrator. ``by_session`` is the orchestrator that set it
    itself; without it the operator did. Returns the wake-up as :func:`view` shows it."""
    scheduler: Any = app.extensions.get("scheduler")
    if scheduler is None:
        raise WakeupRefused("the scheduler is not running on this installation")
    orchestrator = project.settings.orchestrator
    if not orchestrator.enabled or not orchestrator.session_id:
        raise WakeupRefused(f"{project.name} has no orchestrator to wake; switch it on first")
    text = " ".join((note or "").split())
    if not text:
        raise WakeupRefused("a wake-up needs a note: what to look at when it fires")
    if len(text) > NOTE_MAX:
        raise WakeupRefused(f"a note is at most {NOTE_MAX} characters")
    limit = app.config.orchestrator.wakeups_max
    previous = None
    if principal is not None and client_operation_id:
        previous = await app.db.fetchone(
            "SELECT id FROM operation_receipts WHERE scope_kind='project' AND scope_id=? AND actor_id=?"
            " AND operation_kind IN ('schedule.create','schedule.propose') AND client_operation_id=?",
            (project.id, principal.actor_id, client_operation_id),
        )
    if previous is None and await count(app, project.id) >= limit:
        raise WakeupRefused(f"{project.name} already has {limit} wake-ups; cancel one first")
    run_at, recurring = resolve_when(app, at=at, in_minutes=in_minutes, cron=cron)
    if principal is not None and by_session is not None and run_at is not None:
        if not client_operation_id:
            raise WakeupRefused("the host must identify the wake-up command")
        service: Any = app.extensions.get("recurring")
        if service is None:
            raise WakeupRefused("durable scheduling is unavailable")
        revision = await app.db.fetchone(
            "SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?",
            (project.id,),
        )
        if revision is None:
            raise WakeupRefused("the project no longer exists")
        try:
            created = await service.create_internal_wake(
                principal, project_id=project.id, session_id=by_session,
                name=short_name(text), prompt=text, run_at=run_at,
                requested_time={"at": at, "in_minutes": in_minutes},
                expected_collection_revision=int(revision["revision"]),
                client_operation_id=client_operation_id,
            )
        except ControlDenied:
            # The grant may end before this wake. Save a proposal, which stays
            # inert until an operator explicitly approves its exact output.
            pass
        else:
            row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
            return view(dict(row)) if row is not None else created
    if by_session is not None:
        if not client_operation_id:
            raise WakeupRefused("the host must identify the wake-up command")
        proposals: Any = app.extensions.get("schedule_proposals")
        if proposals is None:
            raise WakeupRefused("schedule proposals are unavailable")
        created = await proposals.create(
            source_session_id=by_session, source_run_id=None,
            source_command_id=client_operation_id, name=short_name(text), prompt=text,
            cron=recurring, run_at=run_at, files=[], model=None, kind="wake",
            run_in="new", target_session=orchestrator.session_id,
            requested_time={"at": at, "in_minutes": in_minutes, "cron": cron},
        )
        return {"id": created["id"], "note": text, "cron": recurring, "at": run_at,
                "next_run_at": created["next_run_at"], "enabled": False,
                "authority_state": "needs_approval", "set_by": "orchestrator",
                "proposal_revision": created["proposal_revision"]}
    if (principal is None or principal.origin_class != "operator" or not client_operation_id
            or expected_collection_revision is None or not expires_at):
        raise WakeupRefused("operator wake-ups require a reviewed expiry and command identity")
    service: Any = app.extensions.get("recurring")
    if service is None:
        raise WakeupRefused("durable scheduling is unavailable")
    created = await service.create(
        principal, name=short_name(text), prompt=text, cron=recurring, run_at=run_at,
        kind="wake", target_session=orchestrator.session_id, project_id=project.id,
        expires_at=expires_at, expected_collection_revision=expected_collection_revision,
        client_operation_id=client_operation_id,
        requested_time={"at": at, "in_minutes": in_minutes, "cron": cron},
    )
    row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (created["id"],))
    return view(dict(row)) if row is not None else created


def view(row: dict[str, Any]) -> dict[str, Any]:
    """A wake-up as the app and the tools show it."""
    return {
        "id": row["id"],
        "note": row.get("prompt") or row.get("name") or "",
        "cron": row.get("cron"),
        "at": row.get("run_at"),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "enabled": bool(row.get("enabled")),
        "authority_state": row.get("authority_state", "needs_approval"),
        "schedule_revision": row.get("schedule_revision"),
        "set_by": "orchestrator" if row.get("created_by_session") else "operator",
        "created_at": row.get("created_at"),
    }


async def wakeups(app: Application, project_id: str, *, include_done: bool = False) -> list[dict[str, Any]]:
    """The project's wake-ups, soonest first. A one-off that has fired is done and left out unless asked."""
    rows = await app.db.fetchall(
        f"SELECT sc.* FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.kind = 'wake' AND {_OF_PROJECT}"
        + ("" if include_done else " AND sc.enabled = 1")
        + " ORDER BY sc.next_run_at IS NULL, sc.next_run_at, sc.created_at",
        (project_id,),
    )
    return [view(dict(r)) for r in rows]


async def cancel(app: Application, project_id: str, wakeup_id: str, *,
                 principal: Principal | None = None, by_session: str | None = None,
                 expected_collection_revision: int | None = None,
                 expected_schedule_revision: int | None = None,
                 client_operation_id: str | None = None) -> dict[str, Any] | None:
    """Remove a wake through the operator's durable schedule command."""
    row = await app.db.fetchone(
        f"SELECT sc.id,sc.schedule_revision,sc.deleted_at FROM schedules sc JOIN sessions s ON s.id = sc.target_session WHERE sc.id = ? AND sc.kind = 'wake' AND {_OF_PROJECT}",
        (wakeup_id, project_id),
    )
    if row is None:
        return None
    if by_session is not None or principal is None or principal.origin_class != "operator":
        raise WakeupRefused("removing a standing wake-up needs operator approval in the app")
    recurring: Any = app.extensions.get("recurring")
    if recurring is None:
        raise WakeupRefused("durable scheduling is unavailable")
    if (expected_collection_revision is None or expected_schedule_revision is None
            or not client_operation_id):
        raise WakeupRefused("removing a wake-up requires its current revision and command identity")
    return await recurring.remove(
        principal, wakeup_id,
        expected_collection_revision=expected_collection_revision,
        expected_schedule_revision=expected_schedule_revision,
        client_operation_id=client_operation_id,
    )


async def repoint(app: Application, project_id: str, session_id: str) -> None:
    """Aim every wake-up of the project at the session that now holds the office. Called when an
    orchestrator is switched on again after being off, and on a replacement, so the state block —
    which lists the wake-ups aimed at the session it is written for — shows them all."""
    await app.db.execute(
        f"UPDATE schedules SET target_session = ? WHERE kind = 'wake' AND target_session IN (SELECT s.id FROM sessions s WHERE {_OF_PROJECT})",
        (session_id, project_id),
    )


def describe(wakeup: dict[str, Any]) -> str:
    """One line for the orchestrator: id, when, note."""
    when = f"cron {wakeup['cron']} (UTC)" if wakeup.get("cron") else f"at {str(wakeup.get('next_run_at') or wakeup.get('at') or '')[:16].replace('T', ' ')} UTC"
    approval = "ready" if wakeup.get("authority_state") == "current" else "needs operator approval"
    return f"[{wakeup['id']}] {when} — {wakeup['note']} (set by the {wakeup['set_by']}; {approval})"


__all__ = ["NOTE_MAX", "TICK_NOTE", "WakeupRefused", "cancel", "count", "describe", "repoint", "resolve_when", "set_wakeup", "view", "wakeups"]
