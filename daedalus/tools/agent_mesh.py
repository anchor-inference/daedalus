"""``ListAgents``, ``ReadAgent`` and ``SendToAgent``: the agents of one project talking to each other.

A project's staff and its orchestrator have them; a command-line member has the same three under the
same names on its team server. Everything is decided by the team's mesh
(:mod:`daedalus.extensions.agent_mesh`): who may be reached, the hop cap, the refusals and the log.
"""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import call_id, error, ok, services_for


async def _call(context: ToolContext, op: str, **fields: object) -> ToolResult:
    manager = services_for(context).extra.get("manager")
    hook = manager.service_hooks.get("agent_mesh") if manager is not None else None
    if hook is None:
        return error(context, "the agents of a project are not reachable in this installation")
    text, failed = await hook(op, session_id=context.session_id, call_id=call_id(context), **fields)
    return error(context, text) if failed else ok(context, text)


@tool_group("team_agents")
@search_hint(
    "list agents colleagues team members who is working other staff peers in the project "
    "список агентов коллеги кто работает другие сотрудники агенты проекта покажи кто есть"
)
@tool(name="ListAgents", description="The agents of your project: who is working, on what, and whether a message can reach each.")
async def list_agents(context: ToolContext) -> ToolResult:
    return await _call(context, "list_agents")


@tool_group("team_agents")
@search_hint(
    "read agent output last reply screen of another agent colleague what did it say "
    "прочитать агента вывод последний ответ экран коллеги что сказал посмотреть"
)
@tool(
    name="ReadAgent",
    description="Read another agent of your project: its last reply (what='last', the default) or its terminal (what='screen'); lines: the last N lines.",
)
async def read_agent(context: ToolContext, agent: str, what: str = "last", lines: int = 40) -> ToolResult:
    return await _call(context, "read_agent", agent=agent, what=what, lines=lines)


@tool_group("team_agents")
@search_hint(
    "send message to agent colleague ask another agent wait for reply hand over to staff member "
    "написать агенту сообщение коллеге спросить другого агента дождаться ответа передать"
)
@tool(
    name="SendToAgent",
    description=(
        "Message another agent of your project by name. wait_for_reply=true returns its answer (up to timeout_seconds, "
        "default 300). Refused into a terminal with no agent, one waiting on a permission or question, and after 5 agent "
        "messages in a row. A reply to an agent waiting on you goes straight to it."
    ),
)
async def send_to_agent(context: ToolContext, agent: str, text: str, wait_for_reply: bool = False, timeout_seconds: int = 300) -> ToolResult:
    return await _call(context, "send_to_agent", agent=agent, text=text, wait_for_reply=wait_for_reply, timeout_s=timeout_seconds)


TOOLS = [list_agents, read_agent, send_to_agent]

__all__ = ["TOOLS"]
