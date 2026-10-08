"""Shared helpers for host tools."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult

from daedalus.config import ExecToolsConfig, ToolsConfig
from daedalus.host.services import SessionServices, locator


def call_id(context: ToolContext) -> str:
    return str(context.metadata.get("tool_call_id") or "")


def services_for(context: ToolContext) -> SessionServices:
    return locator.get(context.session_id)


def refuse_protected(context: ToolContext, services: SessionServices, target: Path, verb: str) -> ToolResult | None:
    """The refusal every tool that opens a path owes, or ``None`` when the path is the session's own.

    It is here rather than in each tool because the tools that forgot it were exactly the ones that
    mattered: the writers checked and ``Read`` did not, so the provider keys were refused to anything
    that would have changed them and handed to the one call that only wanted to see them.
    """
    if services.is_protected(target):
        return error(context, f"{target} is protected and cannot be {verb} by tools")
    return None


FETCH_MAX_BYTES = 200 << 20
"""The largest file brought over from the machine a session works on to be sent, attached or handed
on: every few hundred kilobytes is a round trip there, and a chat delivers nothing larger anyway."""


async def local_copy(services: SessionServices, target: Path, *, limit: int = FETCH_MAX_BYTES) -> Path:
    """``target`` as a file of this process: itself when the session's files are here, else a copy
    fetched from the machine its commands run on (the host, a benchmark container).

    What hands a file on — to the chat, to the media store, to another agent's inbox, to a schedule —
    opens it here, and a session working on the host named a file this process does not have: every
    SendFile of such a session failed with "no such file" while the file sat in its folder. The copy
    is kept in the session's own directory here when it has one (``local_home``), which lives as long
    as the session does, so a schedule that names it later still finds it; it is named by its bytes, so
    fetching the same file twice keeps one copy. ``FileNotFoundError`` when it is not a file there,
    ``ValueError`` when it is larger than ``limit``.
    """
    fs = services.fs
    if not fs.remote:
        return target
    data = await fs.read_bytes(target, limit=limit)
    home = services.extra.get("local_home")
    base = Path(home) / ".from-remote" if home else Path(tempfile.gettempdir()) / "daedalus-from-remote" / re.sub(r"[^A-Za-z0-9_-]", "_", services.session_id)
    folder = base / hashlib.sha256(data).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / (target.name or "file")
    if not copy.exists():
        partial = folder / f".{copy.name}.part"
        partial.write_bytes(data)
        partial.replace(copy)
    return copy


def tool_config(context: ToolContext):  # type: ignore[no-untyped-def]
    """The live ``[tools]`` section (read from the running manager, so edits apply at once)."""
    manager = services_for(context).extra.get("manager")
    config = getattr(manager, "config", None)
    return getattr(config, "tools", None) or ToolsConfig()


#: Room a tool leaves between its own clip and the per-call budget, for the
#: header, the footer and the note it wraps the clipped body in. Without it the
#: tool clips to the budget, adds its frame, and ``ok`` clips a second time —
#: cutting the very lines that said where the rest of the output went.
FRAME_CHARS = 400


def output_limit(context: ToolContext) -> int:
    """How many characters of tool output this session lets a single call return.

    Defensive about the locator: a tool called outside a registered host session
    — a unit test, a one-off script — has no services to ask, and the config
    default is a better answer than an exception raised from a result builder.
    """
    try:
        return services_for(context).max_tool_output_chars
    except (RuntimeError, KeyError):
        return ExecToolsConfig().max_output_chars


def ok(context: ToolContext, content: str, **metadata: object) -> ToolResult:
    """A successful result, never longer than the session's per-call budget.

    The clip lives here rather than in each tool because the tools that forgot
    it are exactly the ones that needed it: a result is capped whether or not
    its author thought about size. A tool that clipped its own output with a
    note about how to get the rest is already under the budget, so this leaves
    it alone.
    """
    return ToolResult(
        tool_call_id=call_id(context),
        content=clip(content, output_limit(context), note="one call returns at most this much"),
        metadata=dict(metadata),
    )


def error(context: ToolContext, content: str, **metadata: object) -> ToolResult:
    """A failed result, under the same budget as a successful one.

    A failure is not automatically small: a build that dies after ten thousand
    lines of compiler output, a Verify miss carrying the whole test log, an Edit
    whose near-miss window is long. The model has to carry it in the transcript
    either way, so it is clipped on the same terms — head and tail, because the
    line that names the failure is as often the last one as the first.
    """
    return ToolResult(
        tool_call_id=call_id(context),
        content=clip(content, output_limit(context), note="one call returns at most this much"),
        is_error=True,
        metadata=dict(metadata),
    )


def clip(text: str, limit: int, *, note: str = "") -> str:
    """Keep the head and the tail of an over-long output."""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25) :]
    dropped = len(text) - len(head) - len(tail)
    marker = f"\n\n[... {dropped} characters omitted{(' — ' + note) if note else ''} ...]\n\n"
    return head + marker + tail


__all__ = ["FRAME_CHARS", "call_id", "clip", "error", "ok", "output_limit", "refuse_protected", "services_for", "tool_config"]
