"""Host tools.

Every module in this package that defines ``TOOLS`` (a list of ``Tool`` classes or
instances) is picked up by :func:`discover_tools`. Adding a tool is adding a file, and every tool
carries a ``search_hint`` (see :func:`search_hint`).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolResult

_ARG_ERROR = re.compile(r"unexpected keyword argument '(?P<extra>\w+)'|missing \d+ required (?:positional|keyword-only) arguments?: (?P<missing>.+)$")


def _explained(exc: TypeError, tool: Tool) -> str | None:
    """A model-readable message for a call with a wrong argument name, or None for any other TypeError."""
    match = _ARG_ERROR.search(str(exc))
    if match is None:
        return None
    params = tool.definition.parameters
    names = sorted((params.properties or {}).keys()) if hasattr(params, "properties") else []
    accepted = ", ".join(names) if names else "see the tool description"
    if match.group("extra"):
        return f"unknown argument {match.group('extra')!r} for {tool.name}; accepted: {accepted}"
    return f"{tool.name} is missing {match.group('missing')}; accepted arguments: {accepted}"


def _guarded(tool: Tool) -> Tool:
    """A call with a misspelled or missing argument answers with the accepted names, not a Python traceback."""
    original = tool.invoke

    async def invoke(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await original(context, arguments)
        except TypeError as exc:
            # Only the call boundary is caught: an error raised deeper inside the tool has a real traceback.
            frames = inspect.trace()
            explained = _explained(exc, tool) if len(frames) <= 2 else None
            if explained is None:
                raise
            return ToolResult(tool_call_id=str(context.metadata.get("tool_call_id") or ""), content=explained, is_error=True)

    tool.invoke = invoke  # type: ignore[method-assign]
    return tool


def search_hint(text: str) -> Callable[[type[Tool]], type[Tool]]:
    """Give a ``@tool``-decorated class the words the tool retriever indexes besides its description.

    The core reads ``search_hint`` off the registered object and never sends it to the model, so it can
    carry what a description written for the model should not: Russian words in their dictionary and
    imperative forms, and the slang the operator actually types. Without one, a Russian message shares
    no word with an English description and the tool is not found. Written as a decorator above
    ``@tool`` because that decorator builds the class and offers no place of its own for the attribute.
    """

    def attach(cls: type[Tool]) -> type[Tool]:
        cls.search_hint = text  # type: ignore[attr-defined]
        return cls

    return attach


GroupLoad = Literal["eager", "auto", "lazy"]
"""How a group reaches a run: ``eager`` is always on the surface (only a provider's cap on the number
of tools can push it off), ``auto`` stays on while the definitions fit and is held back first when
they do not, ``lazy`` is held back whenever ToolSearch is there to load it, however much room is left."""

GROUP_LOADS: tuple[GroupLoad, ...] = ("eager", "auto", "lazy")


@dataclass(frozen=True, slots=True)
class ToolGroupSpec:
    description: str
    """The line the core's catalogue shows while the group is held back: what the group is FOR, in the
    words the operator asks for it with, the tool a model would otherwise replace with a workaround, and
    that workaround named as the thing not to do. A description that only said what the tools do was
    not matched to "pause the loop" or "open a PR against yourself", and the model did the job with git,
    cron or another server's browser instead."""
    load: GroupLoad
    """The default the operator's ``[tools.groups.<name>] load`` and a session's own choice override."""


TOOL_GROUPS: dict[str, ToolGroupSpec] = {
    "agents": ToolGroupSpec(
        "Hand work to other agents: a helper in this workspace for a bounded piece of the task (SubAgent), an "
        "independent agent with its own chat and workspace (SpawnAgent), a question to a named peer session (AskPeer)",
        "auto",
    ),
    "board": ToolGroupSpec(
        "The session's task board, the plan of record for work with more than a few steps: tasks with acceptance "
        "criteria and checklists that survive compaction and restarts",
        "auto",
    ),
    "browser": ToolGroupSpec(
        "A real browser you drive, for sites that need JavaScript or a login: open pages, click and type, look at "
        "how a page looks (BrowserLook), downloads (BrowserDownload), hand-over to the operator for a sign-in or a "
        "payment, and a developer's view of a site you build or debug: its console errors (BrowserLogs), its "
        "requests and API responses (BrowserNetwork), why an element is hidden (BrowserInspect). Not a Puppeteer or "
        "Playwright MCP server, not a headless browser in the shell; WebFetch for plain pages",
        "lazy",
    ),
    "docs": ToolGroupSpec(
        "Search and read the documentation of this installed Daedalus version, for questions about how you "
        "yourself work or are configured",
        "lazy",
    ),
    "learning": ToolGroupSpec(
        "Look back on your own work: a report of recent runs, failures and spend (LearningReport) and a skill "
        "distilled from this session saved as a draft (SkillDraft)",
        "lazy",
    ),
    "loop": ToolGroupSpec(
        "THIS session's loop, whenever the operator says 'the loop' or 'цикл': its status, the next wake-up, pause, "
        "resume, stop (LoopStatus, LoopNext, LoopPause, LoopResume, LoopStop)",
        "lazy",
    ),
    "mcp": ToolGroupSpec(
        "MCP servers for this session: list them, switch one on to reach its tools, switch one off",
        "auto",
    ),
    "mcp_oauth": ToolGroupSpec(
        "Sign in to an MCP server that needs OAuth: start the sign-in, finish it with the code, check or drop the link",
        "lazy",
    ),
    "scheduling": ToolGroupSpec(
        "Anything that must happen later, on a timer, or when something arrives ('remind me', 'every Monday', "
        "'when a webhook comes in'): one-shot and recurring schedules (ScheduleCreate), standing intents that fire "
        "on an inbound webhook or message (IntentCreate). Not cron, sleep or a service",
        "lazy",
    ),
    "calendar": ToolGroupSpec(
        "Read and change the operator's calendar events, including calendars connected to Google, Outlook and Yandex",
        "lazy",
    ),
    "diagrams": ToolGroupSpec(
        "Create and edit native Excalidraw diagrams the operator can open in the app",
        "lazy",
    ),
    "self_development": ToolGroupSpec(
        "Any change to yourself, your code, prompts or skills (repositories 'bot' and 'core'): a worktree "
        "(SelfWorkspace), then a pull request (SelfPropose) or an applied change, a rebuild, a rollback. Never git "
        "clone or push, or gh pr, for your own repositories yourself",
        "lazy",
    ),
    "services": ToolGroupSpec(
        "Processes that outlive the turn, such as a dev server or a demo site, on a port the operator can open "
        "(ServiceStart), with their logs",
        "auto",
    ),
}
"""The families of host tools the core may hold back as a unit, the line its catalogue shows for each,
and how each reaches a run unless the operator says otherwise.

The defaults follow what sessions actually call. A group most sessions never touch — the browser, the
agent's own code, schedules, loops, the documentation, OAuth sign-ins — is ``lazy``: its definitions
cost thousands of tokens on every request of every session for the one session in fifty that uses it,
and its catalogue line and ToolSearch bring it back in one call. A group a working session reaches for
now and then — other agents, the board, services, MCP — is ``auto``: on the surface while it fits. The
file, shell, web, memory and question tools are in no group and never leave."""


def tool_group(name: str) -> Callable[[type[Tool]], type[Tool]]:
    """Put a ``@tool``-decorated class into one of :data:`TOOL_GROUPS`.

    The core reads ``tool_group`` off the registered object, like ``search_hint``; membership never
    reaches the wire. An unknown name fails at import rather than leaving a tool in a group whose
    catalogue line nobody wrote.
    """
    if name not in TOOL_GROUPS:
        raise KeyError(f"unknown tool group {name!r}; declared: {', '.join(sorted(TOOL_GROUPS))}")

    def attach(cls: type[Tool]) -> type[Tool]:
        cls.tool_group = name  # type: ignore[attr-defined]
        return cls

    return attach


def discover_tools() -> list[Tool]:
    """Import every submodule and collect its ``TOOLS``."""
    package = importlib.import_module(__name__)
    found: list[Tool] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for entry in getattr(module, "TOOLS", ()):
            found.append(_guarded(entry() if isinstance(entry, type) else entry))
    return found


def tool_names(tools: Iterable[Tool]) -> list[str]:
    return sorted(t.name for t in tools)


__all__ = ["GROUP_LOADS", "TOOL_GROUPS", "GroupLoad", "ToolGroupSpec", "discover_tools", "search_hint", "tool_group", "tool_names"]
