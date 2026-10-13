"""The companion's own tools: hand work to an agent, watch the agents, stop one, and own goals.

Only the companion session ever sees these. Every other session is blocked from them by the host — it
has SpawnAgent and SubAgent, which do the same job with a full workspace behind them; the companion
has no workspace and no shell, so handing work over is the only thing it can do with a request that
needs doing, and a goal is how it keeps hold of something between conversations.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint
from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("companion") if manager is not None else None


@search_hint(
    "hand off delegate work to an agent background parallel hand-over dispatch project "
    "поручить поручи делегировать делегируй передать передай отдать отдай агенту фоном проекту"
)
@tool(
    name="Delegate",
    description=(
        "Hand a task to an agent and get its session id back at once — the agent works while you keep "
        "talking. Write the task as a full hand-over: what to do, where, and what finished looks like; "
        "the agent cannot ask you what you meant. Give session_id instead to add an instruction to an "
        "agent that is already running (a correction, a change of mind, another detail) rather than "
        "starting a second one for the same job — and it is also how you answer an agent that reported "
        "it is waiting for the operator: ask the operator, then send their words to that session id. "
        "Several things asked at once are several calls, one per "
        "task, so they run in parallel. Say out loud that you are setting it up; do not wait silently. "
        "workspace chooses a directory inside the project: 'shared' (the default) is the project root "
        "and 'own' is a private child for that agent, for an errand that should not share files. "
        "project_id starts the agent inside one of the operator's projects instead — use it only when "
        "the operator named the project ('in the bakery project, ...'); call Projects for the ids. "
        "goal_id links the agent to one of your goals, so the goal's next check sees what it did."
    ),
)
async def delegate(context: ToolContext, title: str, task: str, session_id: str | None = None, workspace: str = "shared", project_id: str | None = None, goal_id: str | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "delegation is not available here")
    try:
        result = await hook("delegate", title=title, task=task, session_id=session_id, workspace=workspace, project_id=project_id, goal_id=goal_id)
    except KeyError:
        return error(context, f"no agent of yours has the id {session_id!r}; call Agents to see them")
    except ValueError as exc:
        return error(context, str(exc))
    if result["answered"]:
        return ok(context, f"your answer reached agent {result['title']!r} ({result['session_id']}), which was waiting for it and is working again", session_id=result["session_id"])
    if result["steered"]:
        return ok(context, f"the instruction reached agent {result['title']!r} ({result['session_id']}), which is already working", session_id=result["session_id"])
    where = f"in {result['project']}" + (", in its own directory" if result["workspace"] == "own" else ", in the shared project folder")
    return ok(context, f"agent {result['title']!r} started as session {result['session_id']} {where}; it reports here when it finishes", session_id=result["session_id"])


@search_hint(
    "running agents status list what are they doing who is stuck overview workers "
    "агенты агентов какие работают что делают кто завис статус агентов список воркеров"
)
@tool(
    name="Agents",
    description=(
        "The agents you started, newest first: id, title, status (idle, running, waiting for the "
        "operator, failed), when each last did something, what it last said on the way, and the beginning "
        "of its last answer. Use it when the operator asks what is going on, and to find the id of an "
        "agent you want to steer or stop."
    ),
)
async def agents(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the agent list is not available here")
    rows = await hook("agents")
    if not rows:
        return ok(context, "no agents are running; nothing has been delegated yet")
    lines = []
    for r in rows:
        waiting = {"operator": ", waiting for the operator to answer it", "approval": ", stopped until the operator approves a call in its session"}.get(r.get("waiting", ""), "")
        line = f"- {r['title']} ({r['session_id']}): {r['status']}{waiting}, last active {r['last_message_at']}"
        if r.get("progress"):
            line += f" — on the way: {r['progress']}"
        if r["answer"]:
            line += f" — last answer: {r['answer']}"
        lines.append(line)
    return ok(context, "\n".join(lines))


@search_hint(
    "agent answer full result detail what the agent said output exact response full text "
    "результат агента ответ агента что ответил подробно итог агента полный ответ дословно"
)
@tool(
    name="AgentResult",
    description=(
        "What one agent last answered, in full, and what it is doing now. Use it when the operator asks "
        "for the detail behind a report you summarised. Summarise what comes back; never read it out whole."
    ),
)
async def agent_result(context: ToolContext, session_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the agent list is not available here")
    try:
        result = await hook("result", session_id=session_id)
    except KeyError:
        return error(context, f"no agent of yours has the id {session_id!r}; call Agents to see them")
    body = result["answer"] or "(it has not answered yet)"
    return ok(context, f"{result['title']} — {result['status']}\n\n{body}")


@search_hint(
    "stop kill an agent halt abort running agent drop it cancel agent "
    "остановить останови убить убей агента прекратить прекрати хватит отменить отмени тормознуть тормозни"
)
@tool(
    name="StopAgent",
    description=(
        "Stop an agent that is running. It finishes the tool call it is in and stops; its session and its "
        "files stay. Use it when the operator says to drop something, not to tidy up after a finished agent."
    ),
)
async def stop_agent(context: ToolContext, session_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "stopping agents is not available here")
    try:
        stopped = await hook("stop", session_id=session_id)
    except KeyError:
        return error(context, f"no agent of yours has the id {session_id!r}; call Agents to see them")
    return ok(context, "it is stopping" if stopped else "it was not running")


@search_hint(
    "projects list which projects agents per project project id identifiers overview "
    "проекты проектов список проектов какие проекты айди проекта сколько агентов обзор проектам"
)
@tool(
    name="Projects",
    description=(
        "The operator's projects — a name, a folder and how many agents work in each — and the id to "
        "pass to Delegate as project_id. Call it when the operator names a project ('in the bakery "
        "project, ...'); without one, an agent you start belongs to your own project."
    ),
)
async def projects(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the project list is not available here")
    rows = await hook("projects")
    if not rows:
        return ok(context, "the operator has no projects")
    lines = [
        f"- {r['name']} ({r['id']}): {r['agents']} agent(s)" + ("" if r["reachable"] else ", folder not reachable — an agent cannot be started in it")
        for r in rows
    ]
    return ok(context, "\n".join(lines))


def _goal_line(goal: dict[str, Any]) -> str:
    line = f"- {goal['title']} ({goal['id']}): {goal['status']}"
    if goal.get("cadence"):
        line += f", checked {goal['cadence']}, next {goal.get('next_check_at') or 'not scheduled'}"
    if goal.get("sessions"):
        line += f", agents {', '.join(goal['sessions'])}"
    if goal.get("last_report"):
        line += f" — last report: {str(goal['last_report'])[:300]}"
    return line


@search_hint(
    "goal own keep an eye on track over time follow up watch check regularly objective "
    "цель следить отслеживать присматривать регулярно проверять задача на будущее"
)
@tool(
    name="GoalCreate",
    description=(
        "Take on a goal: something you own over days rather than one task — 'keep the bakery site's prices "
        "current', 'find me a flat under 80k'. brief says what done looks like and what you may do on your "
        "own. cadence is how often the host gives you a turn to check on it: 'every 6h', '30m', 'daily', or "
        "a five-field cron in UTC; leave it empty for a goal you only check when asked. On each check, look "
        "at its agents, delegate what needs doing and finish with GoalReport."
    ),
)
async def goal_create(context: ToolContext, title: str, brief: str = "", cadence: str = "") -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "goals are not available here")
    try:
        goal = await hook("goal_create", title=title, brief=brief, cadence=cadence)
    except ValueError as exc:
        return error(context, str(exc))
    when = f"; first check {goal['next_check_at']}" if goal.get("next_check_at") else "; no cadence, checked when asked"
    return ok(context, f"goal {goal['title']!r} created as {goal['id']}{when}", goal_id=goal["id"])


@search_hint(
    "goal update change pause resume finish drop cadence rename "
    "цель изменить поставить на паузу возобновить закрыть бросить частоту"
)
@tool(
    name="GoalUpdate",
    description=(
        "Change one of your goals: title, brief, cadence (empty string switches checks off), or status — "
        "active, paused (no checks until active again), done (achieved) or dropped (the operator no longer "
        "wants it). Arguments left out keep their values."
    ),
)
async def goal_update(context: ToolContext, goal_id: str, title: str | None = None, brief: str | None = None, status: str | None = None, cadence: str | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "goals are not available here")
    try:
        goal = await hook("goal_update", goal_id=goal_id, title=title, brief=brief, status=status, cadence=cadence)
    except KeyError:
        return error(context, f"no goal has the id {goal_id!r}; GoalList shows them")
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, _goal_line(goal), goal_id=goal["id"])


@search_hint(
    "goals list my goals what am I tracking objectives status "
    "цели список целей какие цели что отслеживаю"
)
@tool(
    name="GoalList",
    description="Your goals: id, status, cadence and next check, linked agents and the last report. include_closed adds the done and dropped ones.",
)
async def goal_list(context: ToolContext, include_closed: bool = False) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "goals are not available here")
    rows = await hook("goal_list", include_closed=include_closed)
    if not rows:
        return ok(context, "you have no goals")
    return ok(context, "\n".join(_goal_line(g) for g in rows))


@search_hint(
    "goal report progress status update what changed finding result "
    "отчёт по цели прогресс что изменилось результат"
)
@tool(
    name="GoalReport",
    description=(
        "Record what you found about a goal: what changed, what you did, what is next. Call it at the end "
        "of every goal check. When nobody is listening the report reaches the operator's Inbox; "
        "needs_attention=true sends it there in any case — use it only when they have to decide or act."
    ),
)
async def goal_report(context: ToolContext, goal_id: str, report: str, needs_attention: bool = False) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "goals are not available here")
    try:
        goal = await hook("goal_report", goal_id=goal_id, report=report, needs_attention=needs_attention)
    except KeyError:
        return error(context, f"no goal has the id {goal_id!r}; GoalList shows them")
    except ValueError as exc:
        return error(context, str(exc))
    return ok(context, f"report recorded for {goal['title']!r}", goal_id=goal["id"])


TOOLS = [delegate, agents, agent_result, stop_agent, projects, goal_create, goal_update, goal_list, goal_report]

__all__ = ["TOOLS"]
