"""Removing a project: what blocks it, and what goes with it.

One path for the Agents list and for orchestration mode. Only what the operator would lose blocks
it: a chat of theirs, named, or something still at work, named. The project's own machinery goes
with it: the orchestrator's sessions, current and retired, each staff member with every session it
ever had, the board, the journal, the brief, the schedules and wake-ups, the loops and the records
of all that work. Files are never deleted, not a project folder and not a private directory inside
one; a staff member's worktree stays where it is, since an unmerged branch may be the only copy.

Orchestration mode once had no removal of its own that worked: the settings sheet's Remove was
refused for every agent that had ever worked there, and deleting the orchestrator's chat instead
switched the orchestrator off and then failed half way, which moved the project into the Agents
list with a chat nobody could delete.
"""

from __future__ import annotations

import logging
from typing import Any

from daedalus.stores.projects import Project, ProjectError
from daedalus.stores.staff import ACTIVE_STATUSES

logger = logging.getLogger(__name__)


class RemovalRefused(Exception):
    """The project stays; the message names exactly what keeps it."""


def _named(titles: list[str]) -> str:
    return ", ".join(f"“{title}”" for title in titles)


async def blockers(app: Any, project: Project) -> list[str]:
    """Why ``project`` cannot be removed right now, one sentence each; empty when it can."""
    manager = app.manager
    if project.settings.system:
        return [f"{project.name} is the installation's own project and cannot be removed"]
    reasons: list[str] = []
    if project.setup_by:
        reasons.append(f"the main orchestrator is still setting {project.name} up; wait until it is done")
    found = await manager.projects.occupants(project.id)
    visible = found.visible
    if visible:
        one = len(visible) == 1
        reasons.append(f"{len(visible)} chat{'' if one else 's'} {'works' if one else 'work'} in {project.name}: "
                       f"{_named([c['title'] for c in visible])}; move {'it' if one else 'them'} to another project or delete {'it' if one else 'them'} first")
    archived = [c for c in found.chats if c["archived"]]
    if archived:
        one = len(archived) == 1
        reasons.append(f"{len(archived)} archived chat{'' if one else 's'} of yours {'is' if one else 'are'} in {project.name}: "
                       f"{_named([c['title'] for c in archived])}; restore and move {'it' if one else 'them'}, or delete {'it' if one else 'them'} first")
    members = {member.id: member for member in await manager.staff.list(project.id, archived=True)}
    live = await manager.staff.live_sessions(project.id)
    working = sorted(members[staff_id].name for staff_id, session in live.items()
                     if staff_id in members and session.status in ACTIVE_STATUSES)
    active = manager.active_sessions()
    staff_sessions = {session.session_id for session in live.values() if session.session_id}
    if any(m["role"] == "orchestrator" and m["id"] in active for m in found.machinery):
        reasons.append(f"the orchestrator of {project.name} is in a turn; stop it first")
    # A staff member's own session running counts once, under the member's name.
    for m in found.machinery:
        if m["role"] == "staff" and m["id"] in active and m["id"] not in staff_sessions:
            working.append(m["title"])
    if working:
        one = len(working) == 1
        reasons.append(f"{_named(working)} {'is' if one else 'are'} at work in {project.name}; "
                       f"wait until {'it finishes' if one else 'they finish'} or release {'it' if one else 'them'} on the team page first")
    return reasons


async def remove_project(app: Any, project_id: str) -> Project:
    """Remove the project or raise :class:`RemovalRefused`; ``KeyError`` when there is none.

    The order matters: the live staff sessions are released through the team so their runtimes
    stop, the orchestrator is switched off through its own path so its queue stops, every session
    is deleted through the manager so each one's hooks end what it holds (terminals, services,
    browsers, watches), and only then do the rows go, in one transaction.
    """
    manager = app.manager
    project = await manager.projects.get(project_id)
    if project is None:
        raise KeyError(project_id)
    reasons = await blockers(app, project)
    if reasons:
        raise RemovalRefused("; ".join(reasons))
    team = app.extensions.get("staff")
    members = {member.id: member for member in await manager.staff.list(project_id, archived=True)}
    for staff_id, session in (await manager.staff.live_sessions(project_id)).items():
        member = members.get(staff_id)
        if team is not None and member is not None:
            await team.release(member, reason="the project was removed", by="operator")
        else:
            await manager.staff.end_session(session.id, "the project was removed")
    orchestrators = app.extensions.get("orchestrator")
    office = project.settings.orchestrator
    if orchestrators is not None and (office.enabled or office.session_id):
        await orchestrators.disable(project_id)
    for session in (await manager.projects.occupants(project_id)).machinery:
        # A private directory a session was given lives inside a project folder: it stays.
        await manager.delete_session(session["id"], delete_workspace=False)
    for hook in manager.project_delete_hooks:
        try:
            await hook(project_id)
        except Exception:  # noqa: BLE001 — a hook that fails must not keep the project
            logger.exception("project delete hook failed for %s", project_id)
    try:
        await manager.projects.delete(project_id)
    except ProjectError as exc:
        raise RemovalRefused(str(exc)) from exc
    await manager.reload_project(None, project_id)
    await manager.bus.publish("project.changed", {"change": "removed", "actor": "operator"}, project_id=project_id)
    return project
