"""A member's launch over the project's ordered route of runtimes.

The route moves on only when a runtime is refused before admission for a reason that is certain
now: its subscription window is spent, it is not signed in, it is not installed or its environment
cannot run it, or every provider of a Daedalus preset is resting on a quota or auth refusal. Each of
those is read from what the host already knows, before anything is launched, so no runtime ever
saw the task when the route passes it by.

Everything else stops the chain. A launch the queue accepted belongs to that runtime from then on,
whatever its model does later; a refusal for another reason (the card's state, the member busy or
waiting on a question) is the command's answer; and a launch whose outcome was lost is reported and
never tried again elsewhere, since a second runtime on the same task is two executors. A task that
already has a live session on another runtime needs the operator's reviewed runtime handoff, which
carries what that session did; a silent second launch would not.

A member hired on a runtime keeps it as its own: the route puts it first, and a fallback that runs
the member elsewhere remembers it (``route_home``) so the next launch tries it again first.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.harness.capabilities import RESTRICTIVE_MODES
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.launch_routes import LaunchRoutes, RouteRecord
from daedalus.stores.projects import ProjectFolder, RouteEntry
from daedalus.stores.provider_holds import resting_providers_in
from daedalus.stores.staff import (
    ACTIVE_STATUSES,
    HARNESS_NAMES,
    Staff,
    StaffError,
    daedalus_cannot_reach,
    daedalus_reaches,
)

if TYPE_CHECKING:
    from daedalus.extensions.staff import Team

logger = logging.getLogger(__name__)

CONFIG_FIELDS = ("harness", "model", "agent", "permission_mode", "effort", "env")
"""What a member runs as. Agent, mode and effort mean something only to their own runtime, so a
fallback starts them empty and the member's own come back with its own runtime."""


class RouteRefused(StaffError):
    """The route launched nothing; ``route`` says why, attempt by attempt."""

    def __init__(self, message: str, route: dict[str, Any]) -> None:
        super().__init__(message)
        self.route = route


def _label(harness: str, model: str = "") -> str:
    name = HARNESS_NAMES.get(harness, harness)
    return f"{name} ({model})" if model else name


def _now() -> str:
    return datetime.now(UTC).isoformat()


def own(member: Staff) -> dict[str, str]:
    return {name: str(getattr(member, name) or "") for name in CONFIG_FIELDS}


def candidates(member: Staff, route: tuple[RouteEntry, ...], folder: ProjectFolder) -> list[dict[str, str]]:
    """The member's own runtime first, as it was hired, then the route's other runtimes in order."""
    home = {name: str((member.route_home or {}).get(name) or "") for name in CONFIG_FIELDS} if member.route_home else own(member)
    found = [home]
    for entry in route:
        if entry.runtime == home["harness"]:
            continue
        found.append({"harness": entry.runtime, "model": entry.model, "agent": "", "permission_mode": "",
                      "effort": "", "env": "" if entry.runtime == "daedalus" else folder.env})
    return found


async def window_refusal(db: Any, harness: str, now: datetime | None = None) -> str:
    """A subscription window the CLI itself last reported as fully used, and that cannot have reset yet.

    The newest snapshot of any session on that CLI speaks for the account. A window ``m`` minutes
    long that was at 100 % at time ``t`` is still spent before ``t + m`` at the latest; past that it
    proves nothing, and the CLI is tried."""
    moment = now or datetime.now(UTC)
    rows = await db.fetchall(
        "SELECT s.usage_json FROM staff_sessions s JOIN staff m ON m.id = s.staff_id"
        " WHERE m.harness = ? AND s.kind = 'cli' AND s.usage_json IS NOT NULL AND s.usage_json != ''"
        " ORDER BY COALESCE(s.status_at, s.started_at) DESC LIMIT 8",
        (harness,),
    )
    newest: tuple[datetime, list[Any]] | None = None
    for row in rows:
        try:
            snapshot = json.loads(row["usage_json"])
            at = datetime.fromisoformat(str(snapshot.get("at") or ""))
        except (TypeError, ValueError, AttributeError):
            continue
        if at.tzinfo is None:
            continue
        windows = snapshot.get("windows") if isinstance(snapshot.get("windows"), list) else []
        if newest is None or at > newest[0]:
            newest = (at, windows)
    if newest is None:
        return ""
    at, windows = newest
    for window in windows:
        if not isinstance(window, dict):
            continue
        try:
            minutes, used = int(window.get("minutes") or 0), float(window.get("used_pct") or 0)
        except (TypeError, ValueError):
            continue
        reset_by = at + timedelta(minutes=minutes)
        if minutes > 0 and used >= 100 and reset_by > moment:
            return (f"usage window exhausted ({minutes // 60 or minutes}{'h' if minutes >= 60 else 'min'} window at "
                    f"{used:.0f}% as of {at.astimezone(UTC):%H:%M} UTC) until {reset_by.astimezone(UTC):%H:%M} UTC at the latest")
    return ""


async def refusal(team: Team, member: Staff, config: dict[str, str], folder: ProjectFolder) -> str:
    """Why ``config`` certainly cannot take the launch now, or ``""`` when it may."""
    harness, model = config["harness"], config["model"]
    runtime = team.runtimes.get(harness)
    if runtime is None:
        return "not installed on this installation"
    manager = team.manager
    if harness == "daedalus":
        local = manager.projects.local_env
        if not daedalus_reaches(folder.env, local):
            return daedalus_cannot_reach(str(folder.path), folder.env, local)
        config_now = manager.config
        if not getattr(config_now, "has_model", True):
            return "no model is configured"
        if model and model not in getattr(config_now, "presets", {}):
            return f"no preset {model!r}"
        providers = getattr(manager, "providers", None)
        if providers is None:
            return ""
        try:
            rungs = providers.rungs_for(config_now, model or None)
        except (RuntimeError, KeyError) as exc:
            return str(exc)
        async with team.app.db.transaction() as conn:
            resting = await resting_providers_in(conn, [provider.endpoint.id for provider, _ in rungs])
        if rungs and all(provider.endpoint.id in resting for provider, _ in rungs):
            # Only every rung: the run's own chain falls back past a resting provider by itself.
            return resting[rungs[0][0].endpoint.id]
        return ""
    available = await runtime.available(folder.env)
    if not available.ok:
        return available.reason
    if member.isolation == "readonly" and not RESTRICTIVE_MODES.get(harness):
        return "it has no mode that keeps it from writing, and the member is read-only"
    return await window_refusal(team.app.db, harness)


async def _write(team: Team, member_id: str, config: dict[str, str], home: dict[str, str] | None) -> Staff:
    await team.manager.db.execute(
        "UPDATE staff SET harness = ?,model = ?,agent = ?,permission_mode = ?,effort = ?,env = ?,route_home = ?"
        " WHERE id = ? AND archived_at IS NULL",
        (*(config[name] for name in CONFIG_FIELDS), json.dumps(home) if home is not None else None, member_id),
    )
    member = await team.member(member_id)
    try:
        await team.manager.bus.publish("project.changed", {"change": "staff.route", "actor": "system"},
                                       project_id=member.project_id, staff_id=member.id)
    except Exception:  # noqa: BLE001 — the change is written; the event is a courtesy
        logger.warning("could not publish the route change of %s", member_id, exc_info=True)
    return member


def story(attempts: list[dict[str, Any]], outcome: str, reason: str = "") -> str:
    """The route as one line the orchestrator and the operator read: who refused and why, then what happened."""
    parts = [f"{_label(a['harness'], a.get('model', ''))}: {a['reason']}" for a in attempts if a["outcome"] == "pre_admission_rejected"]
    last = attempts[-1] if attempts else None
    who = _label(last["harness"], last.get("model", "")) if last is not None else ""
    if outcome == "accepted":
        parts.append(f"{who} accepted")
    elif outcome == "no_runtime":
        parts.append("no runtime of the route can take it now; nothing was launched")
    elif outcome == "admission_unknown":
        parts.append(f"{who}: the launch outcome is unknown; it is not tried again elsewhere")
    else:
        parts.append(reason)
    return " → ".join(p for p in parts if p)


async def _tell(team: Team, member: Staff, task_id: str, record: RouteRecord) -> None:
    """Into the journal and onto the card, where the operator looks: only when the route did more than launch the first runtime."""
    rejected = any(a["outcome"] == "pre_admission_rejected" for a in record.attempts)
    if record.state == "accepted" and not rejected:
        return
    text = f"{member.name}, task {task_id}: {record.reason}"
    try:
        await team.manager.projects.record(member.project_id, "system", "launch_route", text,
                                           {"task_id": task_id, "staff_id": member.id, "operation_id": record.operation_id,
                                            "outcome": record.state})
        await team.note_on_card(task_id, f"Launch route: {record.reason}")
        await team.manager.bus.publish("project.changed", {"change": "journal", "actor": "system"}, project_id=member.project_id)
    except Exception:  # noqa: BLE001 — the record is the authority; these are its copies for reading
        logger.warning("could not write the launch route of %s into the journal", task_id, exc_info=True)


def _replayed(record: RouteRecord) -> dict[str, Any]:
    view = record.view()
    if record.state == "accepted":
        return {**record.result, "route": view}
    if record.state == "claimed":
        raise RouteRefused("a launch with this operation id is in flight, or the host lost its outcome; it is not started again", view)
    raise RouteRefused(record.reason or f"this launch ended as {record.state}", view)


async def route_assign(team: Team, member: Staff, task_id: str, *, principal: Principal, client_operation_id: str,
                       effort: str | None,
                       launch: Callable[[Staff, str | None], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Launch ``member`` on ``task_id`` over the project's route; ``launch`` queues one runtime's launch.

    Answers ``launch``'s receipt with a ``route`` view, or raises :class:`RouteRefused`. The operation
    id is claimed first and every attempt is saved before its launch is queued."""
    project = await team.project(member.project_id)
    task = await team.task(task_id)
    if task is None:
        raise KeyError(task_id)
    store = LaunchRoutes(team.app.db)
    claimed, record = await store.claim(client_operation_id, project_id=project.id, task_id=task_id,
                                        staff_id=member.id, actor_id=principal.actor_id)
    if not claimed:
        return _replayed(record)
    folder = team.folder_for(project, member, task)
    route = candidates(member, project.settings.member_route, folder)
    home = route[0]
    pending: dict[str, Any] | None = None
    try:
        for config in route:
            attempt = {"harness": config["harness"], "model": config["model"], "at": _now()}
            why = await refusal(team, member, config, folder)
            if why:
                record.attempts.append({**attempt, "outcome": "pre_admission_rejected", "reason": why})
                await store.save(record)
                continue
            stop = await _live_blocker(team, member, task_id, config)
            if stop is not None:
                state, reason = stop
                record.attempts.append({**attempt, "outcome": state, "reason": reason})
                await _settle(team, store, member, task_id, record, state, reason)
                raise RouteRefused(record.reason, record.view())
            pending = {**attempt, "outcome": "claimed"}
            record.attempts.append(pending)
            record.selected_harness, record.selected_model = config["harness"], config["model"]
            await store.save(record)
            before, before_home = own(member), member.route_home
            switched = member
            if config["harness"] != member.harness and (idle := await team.live_of(member)) is not None:
                # An idle session of the member's on its present runtime, on another task (the
                # blocker refused every other case): it would have ended at the start anyway, and
                # only its own runtime can stop it.
                await team._end(idle, "next task on another runtime", stop=True)
            if config != before:
                switched = await _write(team, member.id, config, None if config == home else home)
            try:
                result = await launch(switched, effort if config["harness"] == "daedalus" else None)
            except (StaffError, ControlConflict, ControlDenied, KeyError, PermissionError, ValueError) as exc:
                # Refused inside the launch command's own transaction: nothing was queued, so the
                # member goes back to what it was, and the caller gets the command's own refusal.
                if config != before:
                    await _write(team, member.id, before, before_home)
                pending["outcome"], pending["reason"] = "refused", str(exc)
                pending = None
                await _settle(team, store, member, task_id, record, "refused",
                              f"{_label(config['harness'], config['model'])} refused: {exc}")
                raise
            pending["outcome"] = "accepted"
            pending = None
            record.result = {key: value for key, value in result.items() if key != "route"}
            await _settle(team, store, member, task_id, record, "accepted")
            if config != home:
                # On the member's card: why it runs elsewhere, which also says when it may be back.
                await team.manager.db.execute("UPDATE staff SET route_home = json_set(route_home,'$.reason',?)"
                                              " WHERE id = ? AND route_home IS NOT NULL", (record.reason[:500], member.id))
            return {**result, "route": record.view()}
        await _settle(team, store, member, task_id, record, "no_runtime")
        raise RouteRefused(record.reason, record.view())
    except BaseException:
        if record.state == "claimed":
            # Lost between saving the attempt and knowing what the launch did: kept as unknown and
            # never retried, by this caller or by a repeat of the same command.
            if pending is not None:
                pending["outcome"] = "admission_unknown"
            await _settle(team, store, member, task_id, record, "admission_unknown" if pending is not None else "refused",
                          "" if pending is not None else "the route stopped before any launch")
        raise


async def _settle(team: Team, store: LaunchRoutes, member: Staff, task_id: str, record: RouteRecord,
                  state: str, reason: str = "") -> None:
    record.state = state
    record.reason = story(record.attempts, state, reason)
    await store.save(record)
    await _tell(team, member, task_id, record)


async def _live_blocker(team: Team, member: Staff, task_id: str, config: dict[str, str]) -> tuple[str, str] | None:
    """Whether a live session stands in the way of launching ``config``: ``(outcome, reason)`` or ``None``."""
    target = _label(config["harness"])
    rows = await team.manager.db.fetchall(
        "SELECT s.staff_id,s.status,s.task_id,m.harness,m.name FROM staff_sessions s JOIN staff m ON m.id = s.staff_id"
        " WHERE s.ended_at IS NULL AND (s.task_id = ? OR s.staff_id = ?)", (task_id, member.id))
    for row in rows:
        if row["harness"] == config["harness"]:
            continue
        where = _label(row["harness"])
        if row["task_id"] == task_id:
            return ("handoff_required",
                    f"handoff required: {row['name']} has a live session on this task on {where}; continuing it on {target} "
                    "goes through the operator's runtime handoff, never a second launch")
        if row["staff_id"] == member.id and row["status"] in ACTIVE_STATUSES:
            return ("refused", f"{member.name} is still in a session on {where} ({row['status']}); "
                               f"release or answer it before a launch on {target}")
    return None


__all__ = ["RouteRefused", "candidates", "refusal", "route_assign", "story", "window_refusal"]
