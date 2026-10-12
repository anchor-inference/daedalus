"""``AskPeer`` / ``PeerList`` — talk to another named session."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import clip, error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("peers") if manager is not None else None


@tool_group("agents")
@search_hint(
    "ask peer session named peer another session collaborate hand over "
    "спросить соседнюю сессию пир другая сессия коллега пиру спросить пира"
)
@tool(
    name="AskPeer",
    description=(
        "Send a question or a task to a named peer session and get its final reply back (wait=true, "
        "the default) or just hand it over (wait=false). Peers are sessions of this project the operator "
        "named with /peer here <name>; PeerList shows them. Use it to split work across sessions (research, "
        "implement, review) or to consult a session that holds the context you lack."
    ),
)
async def ask_peer(context: ToolContext, name: str, prompt: str, wait: bool = True, timeout_minutes: int | None = None) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "peers are not available")
    try:
        result = await hook("ask", from_session=context.session_id, name=name, prompt=prompt, wait=wait, timeout_minutes=timeout_minutes)
    except (ValueError, RuntimeError) as exc:
        return error(context, str(exc))
    if result.get("answer"):
        limit = services_for(context).max_tool_output_chars
        return ok(context, f"{name} replied:\n\n" + clip(result["answer"], limit), session_id=result["session_id"])
    return ok(context, result.get("note") or f"handed to {name} (session {result['session_id']}); the reply will appear in its topic", session_id=result["session_id"])


@tool_group("agents")
@search_hint(
    "list peers named sessions reachable other chats neighbours "
    "список пиров сессии пиры кто доступен соседние сессии другие чаты показать покажи"
)
@tool(name="PeerList", description="List the named peer sessions of this project that AskPeer can reach.")
async def peer_list(context: ToolContext) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "peers are not available")
    peers = await hook("list", from_session=context.session_id)
    return ok(context, "\n".join(f"- {n} → session {s}" for n, s in sorted(peers.items())) or "(no peers in this project; the operator names them with /peer here <name>)")


TOOLS = [ask_peer, peer_list]

__all__ = ["TOOLS"]
