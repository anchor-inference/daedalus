"""``SubAgent`` / ``SubAgentList`` — helpers that work in this session's workspace."""

from __future__ import annotations

import time
from datetime import datetime

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.config import REASONING_EFFORTS
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import clip, error, idle_words, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("subagents") if manager is not None else None


@tool_group("agents")
@search_hint(
    "subagent helper here same workspace parallel research one-off background helper "
    "сабагент помощник субагент подагент в этой папке параллельно помощника отправить помощника"
)
@tool(
    name="SubAgent",
    description=(
        "Start a subagent: a helper session that does one task in THIS workspace and reports back. "
        "It runs on your model unless `model` names one of the presets listed in your environment "
        "(use another model only when the operator asks for it or the task clearly suits it). `effort` sets how hard it "
        "thinks — off, low, medium, high or xhigh — on top of whichever model it runs on; omit it to keep the leader's "
        "(or the preset's) effort, raise it for a careful review or a hard problem, lower it for a quick lookup. "
        "wait=false (default) returns at once; the subagent's report arrives later as a message from "
        "`subagent:<name>` — even after you have finished your turn — so you may end your turn and "
        "continue when it comes. wait=true blocks until the report is in and returns it. Write the task "
        "as a complete hand-over: the subagent has none of your context. A subagent is removed once it "
        "has reported (its files stay in the workspace); pass keep=true when you will talk to it again "
        "with SubAgentSend — it then keeps its context until you delete it. Make the contract explicit: "
        "`expects` lists what the report must name, comma-separated (the subagent is told; the host checks each "
        "short item against the report and marks the missing ones), `persona` gives the subagent a stance "
        "(critic, skeptic, simplifier, security, researcher — an independent reviewer that does not share your "
        "context is worth more than a second pass of your own), `deliverable` names a workspace-relative file that must exist when it reports "
        "(checked by the host, not by the subagent's word), `tools_off` lists tools the subagent may NOT call "
        "(e.g. [\"Exec\", \"ServiceStart\"]): the host removes them from its session, so a forbidden "
        "launch is impossible rather than merely discouraged — say so in the task too, since the helper "
        "otherwise assumes your whole toolbox. Omit `task` and give a `name` to raise a "
        "helper without work: it is created idle, costs nothing until you use it, is kept, and takes "
        "its jobs through SubAgentSend(name, text) — use it when you want a standing helper (its own "
        "model, its own persona) before you know what to hand it."
    ),
)
async def sub_agent(
    context: ToolContext,
    task: str | None = None,
    model: str | None = None,
    name: str | None = None,
    wait: bool = False,
    timeout_minutes: int | None = None,
    keep: bool = False,
    expects: str | None = None,
    deliverable: str | None = None,
    persona: str | None = None,
    tools_off: list[str] | None = None,
    effort: str | None = None,
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("spawn", leader_id=context.session_id, task=task, model=model, name=name, wait=wait, timeout_minutes=timeout_minutes, keep=keep, expects=expects, deliverable=deliverable, persona=persona, tools_off=list(tools_off or []), effort=effort)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    return _report(context, result, started=True)


def _report(context: ToolContext, result: dict, *, started: bool) -> ToolResult:  # type: ignore[type-arg]
    fate = f"it stays for follow-ups (SubAgentSend {result['name']!r})" if result.get("kept") else "it has been removed; its files are in the workspace"
    if result.get("answer"):
        limit = services_for(context).max_tool_output_chars
        return ok(context, f"subagent {result['name']!r} reported ({fate}):\n\n" + clip(result["answer"], limit), session_id=result["session_id"])
    if result.get("note"):
        return ok(context, result["note"], session_id=result["session_id"])
    if result.get("delivered") == "steer":
        return ok(context, f"delivered to subagent {result['name']!r} while it works; it will act on it before its next step", session_id=result["session_id"])
    effort = f" with {result['effort']} effort" if result.get("effort") else ""
    what = f"subagent {result['name']!r} started on {result.get('model', 'its model')}{effort}" if started else f"subagent {result['name']!r} took the message and started a run"
    return ok(context, f"{what} (session {result['session_id']}); its report arrives as a message when it finishes", session_id=result["session_id"], run_id=result.get("run_id"))


@tool_group("agents")
@search_hint(
    "send message to subagent steer helper follow up "
    "написать напиши сабагенту субагенту помощнику передать передай направить направь"
)
@tool(
    name="SubAgentSend",
    description=(
        "Send a message to one of your subagents by name. While it is still working the message is a steer "
        "it sees before its next step; once it has finished (and was started with keep=true, so it still "
        "exists with its context) the message starts a new run for it, whose report comes back like the "
        "first one — as a message from subagent:<name>, or inline with wait=true. `effort` (off, low, medium, high, "
        "xhigh) changes how hard it thinks from now on."
    ),
)
async def sub_agent_send(context: ToolContext, name: str, text: str, wait: bool = False, timeout_minutes: int | None = None, effort: str | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("send", leader_id=context.session_id, name=name, text=text, wait=wait, timeout_minutes=timeout_minutes, effort=effort)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    return _report(context, result, started=False)


@tool_group("agents")
@search_hint(
    "subagents list running helpers presets models "
    "сабагенты субагенты список помощники пресеты моделей какие помощники работают"
)
@tool(
    name="SubAgentList",
    description=(
        "List this session's subagents: whether each is running, waiting, idle or failed, whether it is kept, "
        "and how long ago it was last active — plus the model presets and efforts SubAgent accepts."
    ),
)
async def sub_agent_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    views = await hook("views", leader_id=context.session_id)
    models = await hook("models")
    lines = [_list_line(v) for v in views] or ["(no subagents yet)"]
    return ok(context, "\n".join(lines) + "\n\nmodels: " + ", ".join(models) + "\nefforts: " + ", ".join(("off", *REASONING_EFFORTS)))


def _list_line(view: dict) -> str:  # type: ignore[type-arg]
    status = str(view["status"])
    if status == "idle":
        # A helper that never had a task is idle, not finished: it has nothing to have finished.
        status = "idle, never given work" if view.get("idle_start") and view["last_activity_at"] == view["created_at"] else "idle (finished)"
    try:
        ago = idle_words(time.time() - datetime.fromisoformat(str(view["last_activity_at"])).timestamp()) + " ago"
    except (TypeError, ValueError):
        ago = "unknown"
    kept = ", kept" if view.get("kept") else ""
    title = f" — {view['title']}" if view.get("title") else ""
    return f"- {view['name']} → session {view['session_id']} ({status}{kept}, last active {ago}){title}"


@tool_group("agents")
@search_hint(
    "delete remove subagent helper no longer needed clean up kept idle "
    "удалить удали сабагента субагента помощника убрать убери ненужного"
)
@tool(
    name="SubAgentDelete",
    description=(
        "Delete one of your subagents by name (or session id) when you no longer need it — typically a kept or "
        "idle helper whose work is done. Its session and context go; the files it wrote stay in your workspace. "
        "A subagent still running is stopped first."
    ),
)
async def sub_agent_delete(context: ToolContext, name: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("delete", leader_id=context.session_id, name=name)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    stopped = " (it was stopped mid-run)" if result.get("was_running") else ""
    return ok(context, f"subagent {result['name']!r} deleted{stopped}; its files stay in the workspace", session_id=result["session_id"])


# In no tool group, so it is never deferred behind ToolSearch: a helper must not have to search for the
# one way to reach its leader, and a group would put it on the catalogue of every session that has the
# group. Every session without a leader has it blocked (SUBAGENT_ONLY_TOOLS), which keeps it off their
# surface entirely, so its definition costs only the subagents that can use it.
@search_hint(
    "report to leader message leader progress blocked finding tell the session that started me "
    "сообщить лидеру доложить отчитаться написать лидеру ведущему сессии заблокирован находка"
)
@tool(
    name="ReportToLeader",
    description=(
        "Send a message to your leader — the session that started you as a subagent — while you are still "
        "working. Use it when you are blocked, or have a finding the leader should act on before you finish, "
        "or are done with a part it is waiting for. It reaches the leader at once (a running leader sees it "
        "before its next step; an idle one is woken). It is not your report: your final reply still carries "
        "the complete result and is delivered when your run ends. Keep it short and self-contained."
    ),
)
async def report_to_leader(context: ToolContext, text: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "subagents are not available")
    try:
        result = await hook("report", session_id=context.session_id, text=text)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    how = "it sees it before its next step" if result.get("delivered") == "steer" else "it was idle and now starts a turn on it"
    return ok(context, f"delivered to your leader ({how}); carry on with the task — your final reply is still the report")


TOOLS = [sub_agent, sub_agent_list, sub_agent_send, sub_agent_delete, report_to_leader]

__all__ = ["TOOLS"]
