"""Turn core events into what the operator sees in a topic.

One live status message per run plus the final answer as ordinary messages. The
status message is edited at most once per interval, and the interval grows with the
age of the run (a run in its second hour does not need a fresh edit every second).
Everything the transport knows about a run lives in :class:`RunView`; the Telegram
side is behind the small :class:`Outbox` protocol so the renderer can be exercised
without a bot.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.host.prompts import split_headline
from daedalus.security.redact import redact
from daedalus.transport.telegram.markdown import DOCUMENT_THRESHOLD, split_message

_TOOL_ICONS = {
    "Exec": "⚙️",
    "Read": "📖",
    "Write": "✏️",
    "Edit": "✏️",
    "Find": "🔎",
    "Search": "🔎",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
    "SendFile": "📎",
    "AttachMedia": "🖼",
    "ImageView": "🖼",
    "AskUser": "❓",
    "Remember": "🧠",
    "Recall": "🧠",
    "SelfPropose": "🛠",
    "SelfRebuild": "🔄",
    "McpEnable": "🔌",
    "McpDisable": "🔌",
    "McpAdd": "🔌",
    "McpRemove": "🔌",
}

_EVIDENCE_RE = re.compile(r"<\s*(file|run)\s+([^<>]*?)/?\s*>")
_ATTR_RE = re.compile(r"([a-z_]+)\s*=\s*\"([^\"]*)\"")
_MEDIA_RE = re.compile(r"!\[[^\]\n]{0,500}\]\(daedalus-media:[0-9a-f-]{36}\)")


def flatten_evidence(text: str) -> str:
    """The evidence tags of an answer, as text a chat without click targets can read.

    The Mini App turns ``<file path="x.py" lines="3-9"/>`` and ``<run id="v12" label="14 passed"/>``
    into chips that open the file and the receipt. Telegram has nothing to open, and an unrendered
    tag is noise in the operator's chat — so a file becomes ``x.py:3-9`` and a run becomes its label
    with the id behind it. A tag that carries neither a path nor an id is dropped: it points nowhere.
    """

    def one(match: re.Match[str]) -> str:
        kind = match.group(1)
        attrs = dict(_ATTR_RE.findall(match.group(2)))
        if kind == "file":
            path = attrs.get("path", "").strip()
            lines = attrs.get("lines", "").strip()
            return f"{path}:{lines}" if path and lines else path
        run_id = attrs.get("id", "").strip()
        label = attrs.get("label", "").strip()
        if not run_id:
            return label
        return f"{label} (run {run_id})" if label else f"run {run_id}"

    return re.sub(r"\n{3,}", "\n\n", _MEDIA_RE.sub("", _EVIDENCE_RE.sub(one, text))).strip()


DEFAULT_EDIT_TIERS: tuple[tuple[float, float], ...] = ((60, 1), (300, 2), (900, 5), (0, 10))
TOOL_TICK_SECONDS = 10.0
"""How often a running tool's elapsed time is re-rendered (the display is bucketed, so most ticks edit nothing)."""
TOOL_DURATION_SHOWN_FROM_SECONDS = 2.0
MAX_TICKER_SECONDS = 12 * 3600
"""A safety bound for the elapsed-time ticker of a run whose end event never arrives."""


class Outbox(Protocol):
    async def send_text(self, text: str, *, markdown: bool = True) -> int: ...
    async def send_html(self, html: str) -> int: ...
    """Send rich HTML (headings, lists, details, pre); degrades to plain text where unsupported."""
    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None: ...
    """Edit a message; ``html`` marks rich HTML rather than plain text."""
    async def send_document(self, path: Path, caption: str | None = None) -> int: ...
    async def send_photo(self, path: Path | str, caption: str | None = None) -> int: ...
    async def send_video(self, path: Path | str, caption: str | None = None) -> int: ...
    async def send_audio(self, path: Path | str, caption: str | None = None) -> int: ...
    async def send_animation(self, path: Path | str, caption: str | None = None) -> int: ...
    async def send_album(self, paths: list[Path | str], caption: str | None = None) -> list[int]: ...
    async def delete(self, message_id: int) -> None: ...
    async def send_draft(self, draft_id: int, text: str) -> bool: ...
    """Show ``text`` as a live draft; returns False when drafts are not possible here."""


@dataclass(slots=True)
class RunView:
    run_id: str
    model: str
    verbosity: int = 1
    status_message_id: int | None = None
    state: str = "running"
    text_buffer: str = ""
    narration: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    tool_started: dict[str, float] = field(default_factory=dict)
    tool_lines: dict[str, int] = field(default_factory=dict)
    """Index in ``tools`` of the log line for a call id, so its duration can be appended."""
    current_tool: str | None = None
    current_tool_since: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    changed_files: set[str] = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)
    dirty: bool = False
    last_edit: float = 0.0
    last_rendered: str = ""
    progress_line: str = ""
    final_sent: bool = False
    delivery_failed: bool = False
    draft_id: int = 0
    draft_sent: str = ""
    session_id: str = ""
    widget_calls: dict[str, str] = field(default_factory=dict)
    """Widget calls whose result has not come back yet, by call id, with their titles."""
    widgets: list[str] = field(default_factory=list)
    """The titles of the widgets the run drew: Telegram cannot run one, so each is said under the
    answer with a link to the conversation in the app, where it is drawn."""
    fallback_from: str = ""
    """The model the run was set to answer with, while another one is answering instead.

    Telegram has no header to change, so the only place the operator can be told is under the
    answer itself — and only when it is true, which is why this is cleared the moment the
    configured model takes the run back."""


class RunRenderer:
    """Consumes events for one run and drives an :class:`Outbox`."""

    def __init__(
        self,
        outbox: Outbox,
        view: RunView,
        *,
        edit_interval: float = 1.0,
        edit_tiers: Sequence[Sequence[float]] = DEFAULT_EDIT_TIERS,
        slow_tool_seconds: float = 60.0,
        cost_lookup: Callable[[str], Awaitable[float | None]] | None = None,
        streaming: bool = False,
        draft_interval: float = 0.35,
    ) -> None:
        self.outbox = outbox
        self.view = view
        self.edit_interval = edit_interval
        self.edit_tiers = tuple((float(a), float(m)) for a, m in edit_tiers) or DEFAULT_EDIT_TIERS
        self.slow_tool_seconds = slow_tool_seconds
        self.cost_lookup = cost_lookup
        self.streaming = streaming
        self.draft_interval = draft_interval
        self._flush_task: asyncio.Task[None] | None = None
        self._draft_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # -- events ---------------------------------------------------------------------

    async def handle(self, event: TurnEvent) -> None:
        v = self.view
        p = event.payload
        t = event.type
        if t is EventType.MESSAGE_START:
            v.text_buffer = ""
            v.draft_id = random.randint(1, 2**31 - 1)
            v.draft_sent = ""
        elif t is EventType.CONTENT_BLOCK_DELTA:
            delta = p.get("delta") or {}
            if delta.get("type") == "text_delta":
                v.text_buffer += delta.get("text") or ""
                self._mark_draft()
        elif t is EventType.TOOL_USE_START:
            name = str(p.get("tool_name") or "")
            call_id = str(p.get("tool_call_id"))
            v.tool_names[call_id] = name
            v.tool_started[call_id] = time.monotonic()
            v.current_tool = name
            v.current_tool_since = v.tool_started[call_id]
            v.progress_line = ""
            self._start_ticker()
            self._mark()
        elif t is EventType.TOOL_USE_STOP:
            call_id = str(p.get("tool_call_id"))
            name = v.tool_names.get(call_id, "?")
            args = p.get("final_input") or {}
            v.tool_lines[call_id] = len(v.tools)
            v.tools.append(f"{_TOOL_ICONS.get(name, '•')} {name} {_args_summary(name, args)}")
            if name in ("Write", "Edit") and args.get("path"):
                v.changed_files.add(str(args["path"]))
            if name == "Widget" and isinstance(args, dict):
                v.widget_calls[call_id] = " ".join(str(args.get("title") or "").split())[:120]
            self._mark()
        elif t is EventType.TOOL_RESULT:
            call_id = str(p.get("tool_call_id"))
            drawn = v.widget_calls.pop(call_id, None)
            if drawn is not None and not p.get("is_error"):
                v.widgets.append(drawn)
            started = v.tool_started.pop(call_id, None)
            line = v.tool_lines.pop(call_id, None)
            if started is not None and line is not None and line < len(v.tools):
                elapsed = time.monotonic() - started
                if elapsed >= TOOL_DURATION_SHOWN_FROM_SECONDS:
                    v.tools[line] += f" · {_duration(int(elapsed))}"
            v.current_tool = None
            if v.verbosity >= 2:
                content = str(p.get("content") or "")
                if content:
                    v.narration.append(f"↳ {redact(content[:300])}")
            self._mark()
        elif t is EventType.MESSAGE_STOP:
            used = p.get("tokens_used") or {}
            if used:
                v.tokens = {k: int(val) for k, val in used.items() if isinstance(val, int | float)}
            if p.get("stop_reason") == "tool_use":
                narration = flatten_evidence(split_headline(v.text_buffer.strip())[0])
                if narration and v.verbosity >= 1:
                    v.narration.append(narration[:400])
                v.text_buffer = ""
            self._mark()
        elif t is EventType.ERROR:
            v.narration.append(f"⚠️ {redact(str(p.get('message') or json.dumps(p)[:300]))}")
            self._mark()
        elif t is EventType.STATE_CHANGED:
            v.state = str(p.get("to") or v.state)
            self._mark()
        elif t is EventType.COMPACTION_STARTED:
            v.narration.append("🗜 compacting context")
            self._mark()
        elif t is EventType.MODEL_CHANGED:
            v.model = str(p.get("model_name") or p.get("to") or v.model)
            v.fallback_from = str(p.get("configured") or p.get("from") or "") if p.get("fallback") else ""
            self._mark()

    def _start_ticker(self) -> None:
        """While a tool runs, re-render its elapsed time even though no event arrives."""
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        v = self.view
        last_bucket = -1
        while v.current_tool and v.state in ("running", "compacting") and time.monotonic() - v.started < MAX_TICKER_SECONDS:
            await asyncio.sleep(TOOL_TICK_SECONDS)
            if not v.current_tool:
                return
            bucket = _bucket(int(time.monotonic() - v.current_tool_since))
            if bucket != last_bucket:
                last_bucket = bucket
                self._mark()

    def close(self) -> None:
        """Stop every background task; call when the renderer is discarded without :meth:`finish`."""
        for task in (self._tick_task, self._flush_task, self._draft_task):
            if task is not None and not task.done():
                task.cancel()

    def _mark_draft(self) -> None:
        if not self.streaming:
            return
        if self._draft_task is None or self._draft_task.done():
            self._draft_task = asyncio.create_task(self._draft_soon())

    async def _draft_soon(self) -> None:
        await asyncio.sleep(self.draft_interval)
        await self.push_draft()

    async def push_draft(self) -> None:
        """Send the accumulated answer text as a live draft (plain text, capped at the message limit)."""
        v = self.view
        text = flatten_evidence(redact(split_headline(v.text_buffer.strip())[0]))[:3800]
        if not self.streaming or not text or text == v.draft_sent or not v.draft_id:
            return
        try:
            ok = await self.outbox.send_draft(v.draft_id, text)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            self.streaming = False  # this chat cannot show drafts; fall back to the final message only
            return
        v.draft_sent = text

    async def progress(self, line: str) -> None:
        self.view.progress_line = redact(line)
        self._mark()

    def current_edit_interval(self) -> float:
        """The base interval scaled by the tier the run's age falls in."""
        age = time.monotonic() - self.view.started
        multiplier = self.edit_tiers[-1][1]
        for limit, factor in self.edit_tiers:
            if limit > 0 and age < limit:
                multiplier = factor
                break
        return self.edit_interval * multiplier

    def _mark(self) -> None:
        self.view.dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        wait = self.current_edit_interval() - (time.monotonic() - self.view.last_edit)
        if wait > 0:
            await asyncio.sleep(wait)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            v = self.view
            if not v.dirty:
                return
            text = self.render_status_html()
            v.dirty = False
            if text == v.last_rendered:
                return
            try:
                await self._show_status(text)
                v.last_rendered = text
            except Exception:  # noqa: BLE001 — a failed edit must never break the run
                pass
            v.last_edit = time.monotonic()

    async def _show_status(self, html_text: str) -> None:
        """Create the status message on first use, edit it afterwards (rich HTML)."""
        v = self.view
        if v.status_message_id is None:
            v.status_message_id = await self.outbox.send_html(html_text)
        else:
            await self.outbox.edit_text(v.status_message_id, html_text, html=True)

    # -- rendering ------------------------------------------------------------------

    def _head(self, cost: float | None = None) -> str:
        v = self.view
        elapsed = int(time.monotonic() - v.started)
        icon = {"running": "⏳", "awaiting": "❓", "completed": "✅", "failed": "❌", "cancelled": "⏹", "compacting": "🗜", "interrupted": "⏸"}.get(v.state, "⏳")
        head = f"{icon} {v.state} · {v.model} · {_duration(elapsed)}"
        if v.tokens:
            head += f" · {v.tokens.get('total', 0):,} tok"
            if v.tokens.get("cache_read"):
                head += f" ({v.tokens['cache_read']:,} cached)"
        if cost is not None:
            head += f" · ${cost:.4f}"
        return head

    def _current_tool_line(self) -> str:
        v = self.view
        if not v.current_tool:
            return ""
        elapsed = int(time.monotonic() - v.current_tool_since)
        slow = elapsed >= self.slow_tool_seconds
        line = f"{'⏱' if slow else '▶'} {v.current_tool}"
        if elapsed >= TOOL_TICK_SECONDS:
            line += f" · {_duration(_bucket(elapsed))}"
        if slow:
            line += " (still running)"
        if v.progress_line:
            line += f": {v.progress_line}"
        return line

    def render_status_html(self, cost: float | None = None) -> str:
        """The status message as rich HTML: one line of vitals, the tool log folded away, the latest note."""
        v = self.view
        parts = [f"<p><b>{_esc(self._head(cost))}</b></p>"]
        current = self._current_tool_line()
        if current:
            parts.append(f"<p>{_esc(current)}</p>")
        if v.tools:
            shown = v.tools[-40:]
            while len(shown) > 3 and sum(len(t) for t in shown) > 7000:
                shown = shown[1:]
            items = "".join(f"<li>{_esc(t[:200])}</li>" for t in shown)
            if len(v.tools) > len(shown):
                items = f"<li>… {len(v.tools) - len(shown)} earlier</li>" + items
            open_attr = " open" if v.state in ("running", "awaiting", "compacting") and len(v.tools) <= 6 else ""
            parts.append(f"<details{open_attr}><summary>{len(v.tools)} tool call{'s' if len(v.tools) != 1 else ''}</summary><ul>{items}</ul></details>")
        if v.narration and v.verbosity >= 1:
            notes = v.narration[-3:] if v.state in ("running", "awaiting") else v.narration[-1:]
            parts.append("<blockquote>" + "<br/>".join(_esc(n[:400]) for n in notes) + "</blockquote>")
        if v.changed_files:
            parts.append("<p>files: " + ", ".join(f"<code>{_esc(f[:120])}</code>" for f in sorted(v.changed_files)[:20]) + "</p>")
        return "".join(parts)

    def render_status(self) -> str:
        """Plain-text status (the fallback when rich editing is unavailable, and for tests)."""
        v = self.view
        lines = [self._head()]
        current = self._current_tool_line()
        if current:
            lines.append(current)
        if v.tools:
            lines.append("")
            lines.extend(v.tools[-8:])
            if len(v.tools) > 8:
                lines.append(f"… {len(v.tools) - 8} earlier tool calls")
        if v.narration and v.verbosity >= 1:
            lines.append("")
            lines.extend(n.replace("\n", " ") for n in v.narration[-3:])
        if v.changed_files:
            lines.append("")
            lines.append("files: " + ", ".join(sorted(v.changed_files))[:300])
        return "\n".join(lines)[:4000]

    async def finish(self, status: str, *, workspace: Path, quiet: bool = False) -> None:
        """Send the final answer and freeze the status message.

        Delivery degrades in steps and never ends in silence: rich Markdown, then plain
        text per chunk, then the whole answer as a file, and as a last resort an explicit
        note that Telegram refused it (the text is still in the workspace and the Mini App).
        ``quiet`` (an unattended run that called StaySilent) keeps the final reply out of
        the chat; the status message records that the run chose silence.
        """
        v = self.view
        v.state = status
        final, _headline = split_headline(v.text_buffer.strip())
        final = flatten_evidence(redact(final))  # the model may quote a secret the operator pasted; Telegram must not receive it
        v.text_buffer = ""
        if quiet:
            v.final_sent = True
            v.narration.append("🤫 quiet run: nothing needed the operator's attention")
            final = ""
        if self._tick_task is not None:
            self._tick_task.cancel()
        cost: float | None = None
        if self.cost_lookup is not None:
            try:
                cost = await self.cost_lookup(v.run_id)
            except Exception:  # noqa: BLE001
                cost = None
        if final and v.fallback_from and v.fallback_from != v.model:
            # One line, under the answer, because the operator's next question depends on knowing it:
            # the reply he is reading was not written by the model this session is set to.
            final = f"{final}\n\n_answered by {v.model} — {v.fallback_from} was unavailable_"
        if final and not v.final_sent:
            v.final_sent = True
            await self._deliver_final(final, workspace)
            if v.delivery_failed:
                v.narration.append("⚠️ the answer did not reach Telegram as a message (see the note above)")
            if self.streaming and v.draft_id and v.draft_sent:
                # The real message is in the chat now; drop the live draft so the client stops the dots.
                try:
                    await self.outbox.send_draft(v.draft_id, "")
                except Exception:  # noqa: BLE001
                    pass
                v.draft_sent = ""
        elif not final and status == "completed" and not v.tools:
            await self.outbox.send_text("(the agent finished without a reply)", markdown=False)
        if v.widgets and not quiet:
            await self._say_widgets()
        summary = self.render_status_html(cost)
        v.dirty = False
        if v.status_message_id is not None:
            try:
                await self.outbox.edit_text(v.status_message_id, summary, html=True)
            except Exception:  # noqa: BLE001
                pass
        elif status != "completed" or v.tools:
            try:
                await self._show_status(summary)
            except Exception:  # noqa: BLE001
                pass

    async def _deliver_final(self, final: str, workspace: Path) -> None:
        v = self.view
        failures: list[str] = []
        if len(final) <= DOCUMENT_THRESHOLD:
            delivered_chunks = 0
            for chunk in split_message(final):
                try:
                    await self.outbox.send_text(chunk)
                    delivered_chunks += 1
                except Exception as exc:  # noqa: BLE001 — one bad chunk must not truncate the answer
                    failures.append(f"{type(exc).__name__}: {exc}")
            if delivered_chunks and not failures:
                return
            if delivered_chunks:
                await self._say(
                    f"⚠️ {len(failures)} part(s) of the answer could not be delivered ({redact(failures[-1][:200])}); "
                    "the complete answer follows as a file (the parts above are repeated in it)."
                )
        try:
            out = workspace / "answer.md"
            out.write_text(final, encoding="utf-8")
            await self.outbox.send_document(out, caption=final[:900])
            return
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {exc}")
        v.delivery_failed = True
        await self._say(
            "⚠️ The answer could not be delivered to Telegram ("
            + redact(failures[-1][:300] if failures else "unknown error")
            + "). It is saved as answer.md in the workspace and visible in the Mini App."
        )

    async def _say_widgets(self) -> None:
        """One message naming the run's widgets, each a link into the app, which turns the relative
        address into a full one when the installation has a public address and into plain words when
        it has none."""
        v = self.view
        link = f"/app/agents/{v.session_id}" if v.session_id else "/app"
        titles = [redact(title) or "Widget" for title in v.widgets]
        v.widgets = []
        try:
            await self.outbox.send_text("\n".join(f"🧩 **{title}** — interactive; [open it in the app]({link})" for title in titles))
        except Exception:  # noqa: BLE001 — the answer itself is out; a lost line about a widget is no reason to fail the run
            await self._say("\n".join(f"🧩 {title} — interactive; open it in the app" for title in titles))

    async def _say(self, text: str) -> None:
        try:
            await self.outbox.send_text(text, markdown=False)
        except Exception:  # noqa: BLE001 — nothing further to try
            pass


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _bucket(seconds: int) -> int:
    """Round elapsed time down so the displayed value changes rarely (10 s steps, then whole minutes)."""
    if seconds < 60:
        return seconds - seconds % 10
    return seconds - seconds % 60


def _args_summary(name: str, args: dict[str, Any]) -> str:
    """One line of arguments for the tool log, with secrets masked before they reach the chat."""
    if name == "Exec":
        return redact(str(args.get("command", "")))[:120]
    for key in ("path", "pattern", "url", "query", "skill", "title", "name"):
        if key in args:
            return redact(str(args[key]))[:120]
    return redact(json.dumps(args, ensure_ascii=False))[:120] if args else ""


__all__ = ["DEFAULT_EDIT_TIERS", "Outbox", "RunRenderer", "RunView"]
