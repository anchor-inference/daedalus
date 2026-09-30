"""History tools: search the full transcript and read archived turns back verbatim.

Compaction replaces old turns in the model's working history with summaries; the
transcript keeps every turn. These tools let the agent find and re-read what a summary
only alludes to, by sequence number.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.security.redact import redact
from daedalus.stores.sqlite import message_text
from daedalus.tools import search_hint
from daedalus.tools._common import clip, error, ok, services_for

MAX_EXPAND_ROWS = 60
DEFAULT_EXPAND_CHARS = 12_000


def _store(context: ToolContext) -> Any:
    manager = services_for(context).extra.get("manager")
    return getattr(manager, "sessions", None)


@search_hint(
    "search conversation history transcript what did I say earlier past chat messages discussed mentions "
    "история переписка переписке разговор что я писал говорил раньше вчера чате поискать поищи упоминания обсуждали"
)
@tool(
    name="HistorySearch",
    description=(
        "Full-text search over this session's complete transcript, including turns that were "
        "compacted into summaries. Returns matching turns with their seq numbers and a snippet; "
        "read the full text with HistoryExpand. Words are matched as prefixes; quote a phrase "
        "for an exact match. Set all_sessions=true to search every session."
    ),
)
async def history_search(context: ToolContext, query: str, limit: int = 10, all_sessions: bool = False) -> ToolResult:
    store = _store(context)
    if store is None:
        return error(context, "history is not available in this session")
    hits = [h for h in await store.search_transcript(query, session_id=None if all_sessions else context.session_id, limit=max(1, min(int(limit), 50))) if not h.get("queued")]
    if not hits:
        rebuilding = getattr(services_for(context).extra.get("manager"), "index_rebuild", None)
        if rebuilding:
            # Not the same answer as "nothing matches": the index is derived from the transcript and
            # is rebuilt in the background after an upgrade, and what it has not reached is invisible.
            return ok(context, f"no turns match {query!r} yet — the search index is still being rebuilt ({rebuilding['done']} of {rebuilding['total']} turns). Ask again in a moment, or read the turns directly with HistoryExpand.")
        return ok(context, f"no turns match {query!r}")
    lines = []
    for h in hits:
        where = f" [session {h['session_id']} — {h.get('title') or '?'}; read with HistoryExpand({h['seq']}, session_id={h['session_id']!r})]" if all_sessions else ""
        lines.append(f"seq {h['seq']} ({h['role']}){where}: {redact(str(h['snippet'])).replace(chr(10), ' ')}")
    return ok(context, "\n".join(lines) + "\n\nHistoryExpand(from_seq=N) reads a turn in full; give to_seq for a range.")


@search_hint(
    "full transcript turns verbatim range of messages by number expand old messages "
    "дословно реплики переписки полностью диапазон сообщений развернуть разверни старые сообщения вербатим"
)
@tool(
    name="HistoryExpand",
    description=(
        "Read transcript turns verbatim by seq number (from HistorySearch or an archived-range "
        "note in a summary). from_seq alone returns that turn; to_seq returns the range. Pass "
        "session_id for a hit HistorySearch found in another session. Long results are clipped; "
        "narrow the range to see more."
    ),
)
async def history_expand(
    context: ToolContext, from_seq: int, to_seq: int | None = None, session_id: str | None = None, max_chars: int = DEFAULT_EXPAND_CHARS
) -> ToolResult:
    store = _store(context)
    if store is None:
        return error(context, "history is not available in this session")
    start = int(from_seq)
    end = int(to_seq) if to_seq is not None else start
    if end < start:
        start, end = end, start
    if end - start + 1 > MAX_EXPAND_ROWS:
        end = start + MAX_EXPAND_ROWS - 1
    rows = await store.expand_transcript(session_id or context.session_id, start, end)
    if not rows:
        return error(context, f"no transcript turns in seq {start}–{end}")
    parts = []
    for seq, message in rows:
        if message.metadata.get("daedalus.queued"):
            # A message as it was queued while a turn ran; the copy where the model read it follows
            # (placed in that turn, or opening the next). Shown both, the operator's words read as
            # sent twice, the first time out of order.
            continue
        text = message_text(message)
        if text:
            parts.append(f"— seq {seq} · {message.role.value} · {message.created_at.strftime('%Y-%m-%d %H:%M')} —\n{text}")
    body = redact("\n\n".join(parts))
    limit = max(1_000, min(int(max_chars), services_for(context).max_tool_output_chars))
    return ok(context, clip(body, limit, note="narrow the seq range for the rest"), first_seq=start, last_seq=end)


TOOLS = [history_search, history_expand]

__all__ = ["TOOLS"]
