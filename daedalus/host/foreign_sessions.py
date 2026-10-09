"""Sessions of other agent programs as Daedalus messages.

The host terminal daemon reads another program's session files on the operator's machine and hands
them over already normalised: a header, then turns of parts (text, thinking, image, tool call, tool
result, compaction, meta). Everything here works on that one shape, whatever program wrote it, and
turns it into the two layers a Daedalus session has: the transcript, which keeps everything that was
said and done, word for word, and the model's working history, which keeps what a model of ours can
continue from.

The two differ in three ways, each for a reason:

* Thinking is kept in the transcript and never in the history. A signed thinking block belongs to
  the model and the provider that wrote it; another provider refuses it, and no other one needs it.
* A tool call with an exact equivalent among ours (a shell command, a file read, an edit) becomes our
  tool with our arguments, so the model reads its past as work it did itself. Anything else becomes
  text inside the assistant's message, ``[Claude Code · TodoWrite] …``: a call to a tool the model
  does not have, left in its history, is a call it tries to make again, and some providers refuse a
  history that names an undeclared tool at all.
* Long tool outputs are cut for the history and kept whole (to a generous bound) in the transcript,
  where HistoryExpand and the search find them.

A transcript row is identified by its role and creation time (``SqliteSessionStore.transcript_key``),
and a row with a key already present is silently skipped. Programs write several records with the
same millisecond, so every message made here gets a time strictly later than the one before it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from protocore.constants import MAX_TOOL_CALL_ARGUMENT_BYTES
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.security.redact import Redactor

HARNESS_NAMES = {
    "claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "opencode": "opencode", "pi": "pi",
    "grok": "Grok CLI", "cursor": "Cursor", "aider": "aider", "qwen": "Qwen Code", "goose": "goose", "crush": "Crush",
}
HARNESS_ORDER = ("claude", "codex")
"""The programs listed first, in this order: the ones the operator has the most sessions in."""

IMPORTED_KEY = "daedalus.imported"
"""Every imported message carries ``{harness, ext_id, seq}`` here, and ``tools`` (call id to the
program's own tool name) where a call was renamed to ours, so the chat can label the step with the
name the operator knew it by."""

MODEL_OUTPUT_CHARS = 60_000
"""A tool result in the working history, as ``tools.exec.max_output_chars`` cuts a result of ours."""
TAIL_OUTPUT_CHARS = 4_000
"""A tool result in the verbatim tail of a summarised import: the tail is there for the conversation,
not for megabytes of logs, which stay in the transcript."""
TRANSCRIPT_OUTPUT_CHARS = 200_000
TEXT_ARGS_CHARS = 400
TEXT_RESULT_CHARS = 1_500
TRANSCRIPT_ARGS_CHARS = 20_000
IMAGE_MAX_BYTES = 5 * 1024 * 1024
CHARS_PER_TOKEN = 4
TICK = timedelta(microseconds=1)

TAIL_WINDOW_SHARE = 0.25
LARGE_WINDOW_SHARE = 0.6
LARGE_MESSAGES = 2_000
TAIL_MIN_USER_TURNS = 3
TRANSCRIPT_MAX_MESSAGES = 20_000
"""The most messages an import writes into the transcript. A session longer than that keeps its
latest ones; the earlier part stays in the stored original."""

_NOISE_RE = re.compile(
    r"\A\s*<(environment_context|user_instructions|system-reminder|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr|task-notification|user-prompt-submit-hook)\b[^>]*>.*</\1>\s*\Z",
    re.DOTALL,
)
"""A user record that is only a program's own wrapping. The daemon files these as ``meta``; this is
the second line, for a reader older than the list it keeps."""

_PLAN_TOOLS = {"todowrite", "todo_write", "update_plan"}
_AGENT_TOOLS = {"task", "agent", "spawn_agent"}
_SHELLS = {"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/bin/zsh", "pwsh", "powershell"}


def harness_name(harness: str) -> str:
    return HARNESS_NAMES.get(harness, harness or "another program")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Go writes up to nine fractional digits; Python reads six.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clip(text: str, limit: int, *, where: str = "the transcript") -> str:
    """``text`` within ``limit`` characters: its head and its tail, and a line saying how much is gone."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = max(0, limit - head - 120)
    gone = len(text) - head - tail
    return f"{text[:head]}\n[… {gone} characters cut at import; {where} has the whole output …]\n{text[-tail:] if tail else ''}"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _arguments(raw: Any) -> dict[str, Any]:
    """A call's input as a dictionary: programs store it as an object, a JSON string or a bare string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"input": raw}
        return parsed if isinstance(parsed, dict) else {"input": raw}
    return {} if raw is None else {"input": raw}


def _first(args: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = args.get(name)
        if value not in (None, ""):
            return value
    return None


def shell_command(raw: Any) -> str:
    """A command as one line of bash: an argv joined, and a ``bash -lc "<cmd>"`` wrapper taken off."""
    if isinstance(raw, list) and all(isinstance(part, str) for part in raw):
        if len(raw) == 3 and raw[0] in _SHELLS and raw[1] in ("-lc", "-c", "-Command"):
            return raw[2]
        return shlex.join(raw)
    return raw if isinstance(raw, str) else ""


# -- the native tools -----------------------------------------------------------------------------


def _drop_empty(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if value not in (None, "", [], {})}


def _exec(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    command = shell_command(_first(args, "command", "cmd", "argv"))
    if not command.strip():
        return None
    # Every program that writes a timeout writes milliseconds (Claude Code's ``timeout``, Codex's
    # ``timeout_ms``, opencode's ``timeout``); ours is in seconds.
    timeout = _first(args, "timeout_ms", "timeout")
    seconds = max(1, int(timeout / 1000)) if isinstance(timeout, (int, float)) and timeout > 0 else None
    return "Exec", _drop_empty({"command": command, "cwd": _first(args, "workdir", "cwd", "directory"), "timeout_seconds": seconds})


def _path(args: dict[str, Any]) -> Any:
    return _first(args, "file_path", "filePath", "absolute_path", "path", "file")


def _read(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path = _path(args)
    if not path:
        return None
    return "Read", _drop_empty({"path": path, "offset": args.get("offset"), "limit": args.get("limit")})


def _write(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path, content = _path(args), args.get("content")
    if not path or not isinstance(content, str):
        return None
    return "Write", {"path": path, "content": content}


def _edit(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path = _path(args)
    old, new = _first(args, "old_string", "oldString"), args.get("new_string", args.get("newString"))
    if not path or not isinstance(old, str) or not isinstance(new, str):
        return None
    out: dict[str, Any] = {"path": path, "old_string": old, "new_string": new}
    if _first(args, "replace_all", "replaceAll"):
        out["replace_all"] = True
    return "Edit", out


def _multi_edit(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path, edits = _path(args), args.get("edits")
    if not path or not isinstance(edits, list) or not edits:
        return None
    return "MultiEdit", {"path": path, "edits": edits}


def _find(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    return "Find", _drop_empty({"pattern": pattern, "path": args.get("path")})


def _search(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    out = _drop_empty({"pattern": pattern, "path": args.get("path"), "glob": _first(args, "glob", "include")})
    if args.get("-i") or args.get("case_insensitive"):
        out["case_insensitive"] = True
    return "Search", out


def _list(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path = _first(args, "path", "dir_path", "directory") or "."
    return "Exec", {"command": f"ls -la {shlex.quote(str(path))}"}


def _fetch(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    url = args.get("url")
    return ("WebFetch", {"url": url}) if isinstance(url, str) and url else None


def _web_search(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    query = _first(args, "query", "q")
    if not isinstance(query, str):
        action = args.get("action")
        query = action.get("query") if isinstance(action, dict) else None
    return ("WebSearch", {"query": query}) if isinstance(query, str) and query else None


def _patch(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    text = _first(args, "input", "patch")
    if not isinstance(text, str):
        return None
    files = parse_patch(text)
    if len(files) != 1:
        return None
    only = files[0]
    if only["op"] == "add":
        return "Write", {"path": only["path"], "content": only["new"]}
    if only["op"] == "update" and not only.get("move") and only["hunks"] == 1:
        return "Edit", {"path": only["path"], "old_string": only["old"], "new_string": only["new"]}
    return None


_NATIVE = {
    "bash": _exec, "shell": _exec, "local_shell_call": _exec, "exec_command": _exec, "run_shell_command": _exec,
    "commandexecution": _exec, "container.exec": _exec,
    "read": _read, "read_file": _read,
    "write": _write, "write_file": _write,
    "edit": _edit, "replace": _edit,
    "multiedit": _multi_edit,
    "glob": _find,
    "grep": _search, "search_file_content": _search,
    "ls": _list, "list_directory": _list,
    "webfetch": _fetch, "web_fetch": _fetch,
    "websearch": _web_search, "web_search": _web_search, "google_web_search": _web_search, "web_search_call": _web_search,
    "apply_patch": _patch,
}


def native_call(name: str, raw_input: Any, kind: str = "native") -> tuple[str, dict[str, Any]] | None:
    """Our tool and its arguments for one of the program's calls, or ``None`` when ours would not do
    exactly the same thing with nothing lost. An MCP server's tool is never ours, whatever its name."""
    if kind == "mcp":
        return None
    mapper = _NATIVE.get(name.lower())
    if mapper is None:
        return None
    mapped = mapper(_arguments(raw_input))
    if mapped is None:
        return None
    if len(_json(mapped[1]).encode("utf-8")) > MAX_TOOL_CALL_ARGUMENT_BYTES:
        return None
    return mapped


def parse_patch(text: str) -> list[dict[str, Any]]:
    """The files of a ``*** Begin Patch`` block, each ``{op, path, move?, hunks, old, new, added, removed}``.

    ``old`` and ``new`` are the hunk's context and changed lines on either side, which is what an
    Edit of a one-hunk update needs; for an added file ``new`` is its content."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old: list[str] = []
    new: list[str] = []
    for line in text.splitlines():
        header = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)", line)
        if header:
            if current is not None:
                current.update(old="\n".join(old), new="\n".join(new))
            current = {"op": header.group(1).lower(), "path": header.group(2).strip(), "hunks": 0, "added": 0, "removed": 0}
            files.append(current)
            old, new = [], []
            continue
        if current is None or line.startswith(("*** Begin Patch", "*** End Patch", "*** End of File")):
            continue
        if line.startswith("*** Move to: "):
            current["move"] = line[len("*** Move to: "):].strip()
        elif line.startswith("@@"):
            current["hunks"] += 1
        elif current["op"] == "add":
            new.append(line[1:] if line.startswith("+") else line)
            current["added"] += 1
        elif line.startswith("+"):
            new.append(line[1:])
            current["added"] += 1
        elif line.startswith("-"):
            old.append(line[1:])
            current["removed"] += 1
        elif line.startswith(" ") or line == "":
            old.append(line[1:])
            new.append(line[1:])
    if current is not None:
        current.update(old="\n".join(old), new="\n".join(new))
    for entry in files:
        if entry["op"] == "update" and entry["hunks"] == 0 and (entry["added"] or entry["removed"]):
            entry["hunks"] = 1
        if entry["op"] == "add" and entry["new"] and not entry["new"].endswith("\n"):
            entry["new"] += "\n"
    return files


def plan_text(raw_input: Any) -> str:
    """A to-do list or plan as one line: ``Plan: [x] done  [~] doing  [ ] next``."""
    args = _arguments(raw_input)
    items = args.get("todos") or args.get("plan") or []
    marks = {"completed": "x", "done": "x", "in_progress": "~", "in-progress": "~"}
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        what = str(item.get("content") or item.get("step") or item.get("title") or "").strip()
        if what:
            out.append(f"[{marks.get(str(item.get('status') or ''), ' ')}] {what}")
    head = "Plan: " + "  ".join(out) if out else "Plan: (empty)"
    explanation = args.get("explanation")
    return f"{head}\n{explanation}" if isinstance(explanation, str) and explanation.strip() else head


# -- the conversion -------------------------------------------------------------------------------


@dataclass
class Entry:
    """One message of the import: its transcript form and its form in the working history, which
    share the creation time and so the transcript key. ``history`` is ``None`` for what only the
    transcript keeps: a turn that was nothing but thinking, a sub-agent's own turns, a plan an
    imported later plan replaced."""

    transcript: Message
    history: Message | None
    turn: int
    sidechain: str = ""
    summary: bool = False
    operator: bool = False
    """A message of the operator's own: a tail never starts with fewer than a few of them."""
    tail_history: Message | None = None
    """``history`` with its tool output cut to the tail's bound, when that is shorter."""


@dataclass
class Conversion:
    entries: list[Entry] = field(default_factory=list)
    blobs: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    """Image bytes by the reference the blob store will give them (their SHA-256)."""
    counts: dict[str, int] = field(default_factory=dict)
    masked: int = 0
    last_at: datetime | None = None
    last_seq: int = -1
    last_ext: str = ""
    dropped: int = 0
    """Messages left out of the transcript because the session is longer than it keeps."""

    def tokens(self, entries: Iterable[Entry] | None = None, *, tail: bool = False) -> int:
        return sum(estimate_tokens((e.tail_history or e.history) if tail else e.history) for e in (self.entries if entries is None else entries) if e.history is not None)


def estimate_tokens(message: Message | None) -> int:
    if message is None:
        return 0
    chars = 0
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            chars += len(block.text)
        elif isinstance(block, ToolUseBlock):
            chars += len(block.name) + len(block.arguments_json)
        elif isinstance(block, ToolResultBlock):
            chars += len(block.content)
    return chars // CHARS_PER_TOKEN + 4


class Converter:
    """Turns of one foreign session into :class:`Entry` rows, in order.

    ``after`` is the time the last message of an earlier import of the same session got: a refresh
    continues strictly after it. ``redactor`` masks secrets in every string of every turn before
    anything is built from it, and the number of strings it changed is the conversion's ``masked``.
    """

    def __init__(self, harness: str, *, redactor: Redactor | None = None, after: datetime | None = None, transcript_max: int = TRANSCRIPT_MAX_MESSAGES) -> None:
        self.harness = harness
        self.label = harness_name(harness)
        self.redactor = redactor
        self.clock = after
        self.transcript_max = transcript_max

    # -- secrets ------------------------------------------------------------------------------

    def _mask(self, value: Any, counter: list[int], *, key: str = "") -> Any:
        if self.redactor is None:
            return value
        if isinstance(value, str):
            if key in ("data_b64", "signature"):
                return value
            out = self.redactor.redact_any({key: value})[key] if key else self.redactor.redact_any(value)
            if out != value:
                counter[0] += 1
            return out
        if isinstance(value, dict):
            return {k: self._mask(v, counter, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._mask(v, counter) for v in value]
        return value

    # -- time ---------------------------------------------------------------------------------

    def _tick(self, at: datetime | None) -> datetime:
        when = at or self.clock or datetime.now(UTC)
        if self.clock is not None and when <= self.clock:
            when = self.clock + TICK
        self.clock = when
        return when

    # -- the whole session --------------------------------------------------------------------

    def convert(self, turns: Sequence[dict[str, Any]]) -> Conversion:
        counter = [0]
        turns = [self._mask(turn, counter) for turn in turns if isinstance(turn, dict)]
        out = Conversion(masked=counter[0])
        counts = {"turns": len(turns), "user": 0, "assistant": 0, "tool_calls": 0, "native_calls": 0, "text_calls": 0,
                  "compactions": 0, "sidechains": 0, "images": 0, "thinking": 0, "meta": 0, "orphan_results": 0}
        self.results: dict[str, dict[str, Any]] = {}
        for turn in turns:
            for part in turn.get("parts") or []:
                if isinstance(part, dict) and part.get("kind") == "tool_result" and part.get("call_id"):
                    self.results.setdefault(str(part["call_id"]), part)
        self.consumed: set[str] = set()
        main = [t for t in turns if not t.get("sidechain")]
        side: dict[str, list[dict[str, Any]]] = {}
        for turn in turns:
            if turn.get("sidechain"):
                side.setdefault(str(turn["sidechain"]), []).append(turn)
        counts["sidechains"] = len(side)
        self.last_plan = self._last_plan(main)
        attached = self._attach(main, side)
        entries: list[Entry] = []
        for index, turn in enumerate(main):
            entries.extend(self._turn(turn, counts, out))
            for chain in attached.get(index, []):
                for sub in side[chain]:
                    entries.extend(self._turn(sub, counts, out, sidechain=chain))
        counts["orphan_results"] = sum(1 for call_id in self.results if call_id not in self.consumed)
        if len(entries) > self.transcript_max:
            out.dropped = len(entries) - self.transcript_max
            entries = entries[-self.transcript_max:]
        out.entries = entries
        out.counts = counts
        if turns:
            last = turns[-1]
            out.last_seq = max(int(t.get("seq") or 0) for t in turns)
            out.last_ext = str(last.get("ext_id") or "")
        out.last_at = self.clock
        return out

    def _last_plan(self, main: Sequence[dict[str, Any]]) -> str:
        last = ""
        for turn in main:
            for part in turn.get("parts") or []:
                if isinstance(part, dict) and part.get("kind") == "tool_call" and str(part.get("name") or "").lower() in _PLAN_TOOLS:
                    last = str(part.get("call_id") or "")
        return last

    def _attach(self, main: Sequence[dict[str, Any]], side: dict[str, list[dict[str, Any]]]) -> dict[int, list[str]]:
        """Which main turn each sub-agent's turns follow: the one holding the call whose id is the
        sidechain's, else the last main turn that began before the sub-agent did."""
        where: dict[str, int] = {}
        for index, turn in enumerate(main):
            for part in turn.get("parts") or []:
                if isinstance(part, dict) and part.get("kind") in ("tool_call", "tool_result") and part.get("call_id"):
                    where.setdefault(str(part["call_id"]), index)
        attached: dict[int, list[str]] = {}
        for chain, chain_turns in side.items():
            index = where.get(chain)
            if index is None:
                started = parse_time(chain_turns[0].get("at"))
                index = max(0, len(main) - 1)
                if started is not None:
                    before = [i for i, t in enumerate(main) if (parse_time(t.get("at")) or started) <= started]
                    index = before[-1] if before else 0
            attached.setdefault(index, []).append(chain)
        return attached

    # -- one turn -----------------------------------------------------------------------------

    def _meta(self, turn: dict[str, Any], sidechain: str, **extra: Any) -> dict[str, Any]:
        mark: dict[str, Any] = {"harness": self.harness, "ext_id": str(turn.get("ext_id") or ""), "seq": int(turn.get("seq") or 0)}
        if sidechain:
            mark["sidechain"] = sidechain
        if turn.get("model"):
            mark["model"] = str(turn["model"])
        mark.update({k: v for k, v in extra.items() if v})
        return {IMPORTED_KEY: mark}

    def _turn(self, turn: dict[str, Any], counts: dict[str, int], out: Conversion, *, sidechain: str = "") -> list[Entry]:
        role = str(turn.get("role") or "")
        at = parse_time(turn.get("at"))
        seq = int(turn.get("seq") or 0)
        parts = [p for p in turn.get("parts") or [] if isinstance(p, dict)]
        entries: list[Entry] = []
        if role == "assistant":
            counts["assistant"] += 1
            self._assistant(turn, parts, at, seq, sidechain, counts, out, entries)
            return entries
        texts: list[str] = []
        refs: list[dict[str, str]] = []

        def flush() -> None:
            if not texts and not refs:
                return
            body = "\n\n".join(texts) if texts else "(image)"
            metadata = {"daedalus.origin": "operator", **self._meta(turn, sidechain)}
            if refs:
                metadata["image_refs"] = list(refs)
            message = Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], created_at=self._tick(at), metadata=metadata)
            entries.append(Entry(message, None if sidechain else message, seq, sidechain, operator=not sidechain))
            counts["user"] += 1
            texts.clear()
            refs.clear()

        for part in parts:
            kind = part.get("kind")
            if kind == "text" and role == "user":
                text = str(part.get("text") or "")
                if text.strip() and not _NOISE_RE.match(text):
                    texts.append(text)
                elif text.strip():
                    counts["meta"] += 1
            elif kind == "image":
                note = self._image(part, out, refs)
                counts["images"] += 1
                if note:
                    texts.append(note)
            elif kind == "compaction":
                flush()
                entries.append(self._summary(turn, part, at, seq, sidechain))
                counts["compactions"] += 1
            elif kind == "tool_result":
                continue  # consumed with its call, or counted as an orphan at the end
            else:
                counts["meta"] += 1
        flush()
        return entries

    def _image(self, part: dict[str, Any], out: Conversion, refs: list[dict[str, str]]) -> str:
        """Store an image's bytes under the reference the blob store will give them, or return the line
        that says it could not be carried over."""
        mime = str(part.get("mime") or "image/png")
        name = str(part.get("path") or mime)
        data_b64 = part.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            try:
                data = base64.b64decode(data_b64, validate=False)
            except (binascii.Error, ValueError):
                data = b""
            if data and len(data) <= IMAGE_MAX_BYTES:
                ref = hashlib.sha256(data).hexdigest()
                out.blobs[ref] = (data, mime)
                refs.append({"ref": ref, "mime": mime})
                return ""
        return f"[image: {name}, unavailable]"

    def _summary(self, turn: dict[str, Any], part: dict[str, Any], at: datetime | None, seq: int, sidechain: str) -> Entry:
        summary = str(part.get("summary") or "").strip() or "(the program recorded a compaction without its summary)"
        metadata = {
            COMPACTION_SUMMARY_METADATA_KEY: True,
            "daedalus.compaction": {"reason": "import", "source": self.harness, "auto": bool(part.get("auto")), "at": (at or datetime.now(UTC)).isoformat()},
            **self._meta(turn, sidechain),
        }
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"<compacted-turn id='foreign:{self.harness}'>{summary}</compacted-turn>")],
                          created_at=self._tick(at), metadata=metadata)
        return Entry(message, None if sidechain else message, seq, sidechain, summary=not sidechain)

    def _assistant(self, turn: dict[str, Any], parts: list[dict[str, Any]], at: datetime | None, seq: int, sidechain: str,
                   counts: dict[str, int], out: Conversion, entries: list[Entry]) -> None:
        shown: list[Any] = []
        kept: list[Any] = []
        natives: list[tuple[str, str]] = []
        renamed: dict[str, str] = {}
        texted: list[str] = []

        def flush() -> None:
            if not shown:
                return
            created = self._tick(at)
            metadata = self._meta(turn, sidechain, tools=dict(renamed), text_tools=list(texted))
            transcript = Message(role=MessageRole.assistant, content_blocks=_joined(shown), created_at=created, metadata=metadata)
            history = Message(role=MessageRole.assistant, content_blocks=_joined(kept), created_at=created, metadata=metadata) if kept and not sidechain else None
            entries.append(Entry(transcript, history, seq, sidechain))
            for call_id, original in natives:
                result = self.results[call_id]
                self.consumed.add(call_id)
                output = self._output(result)
                error = bool(result.get("is_error"))
                result_meta = self._meta(turn, sidechain, tools={call_id: original})
                result_at = self._tick(parse_time(result.get("at")) or at)
                full = Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=call_id, content=clip(output, TRANSCRIPT_OUTPUT_CHARS, where="the original session"), is_error=error)],
                               created_at=result_at, metadata=result_meta)
                model = full.model_copy(update={"content_blocks": (ToolResultBlock(tool_call_id=call_id, content=clip(output, MODEL_OUTPUT_CHARS), is_error=error),)})
                tail = full.model_copy(update={"content_blocks": (ToolResultBlock(tool_call_id=call_id, content=clip(output, TAIL_OUTPUT_CHARS), is_error=error),)})
                entries.append(Entry(full, None if sidechain else model, seq, sidechain, tail_history=None if sidechain else tail))
            shown.clear()
            kept.clear()
            natives.clear()
            renamed.clear()
            texted.clear()

        for part in parts:
            kind = part.get("kind")
            if kind == "text":
                text = str(part.get("text") or "")
                if text.strip():
                    shown.append(TextBlock(text=text))
                    kept.append(TextBlock(text=text))
            elif kind == "thinking":
                counts["thinking"] += 1
                text = str(part.get("text") or "").strip()
                if part.get("encrypted") and not text:
                    text = f"[reasoning hidden by {self.label}]"
                if text:
                    shown.append(ThinkingBlock(text=text))
            elif kind == "tool_call":
                counts["tool_calls"] += 1
                self._call(part, shown, kept, natives, renamed, texted, counts)
            elif kind == "compaction":
                flush()
                entries.append(self._summary(turn, part, at, seq, sidechain))
                counts["compactions"] += 1
            elif kind == "image":
                counts["images"] += 1
                note = f"[image: {part.get('path') or part.get('mime') or 'image'}, not carried over]"
                shown.append(TextBlock(text=note))
                kept.append(TextBlock(text=note))
            elif kind == "tool_result":
                continue
            else:
                counts["meta"] += 1
        flush()

    def _output(self, result: dict[str, Any] | None) -> str:
        if result is None:
            return ""
        output = result.get("output")
        text = output if isinstance(output, str) else ("" if output is None else _json(output))
        images = result.get("images") or []
        if isinstance(images, list) and images:
            text = f"{text}\n[{len(images)} image(s) the program returned were not carried over]".strip()
        if result.get("truncated"):
            text = f"{text}\n[the program itself kept only part of this output]"
        return text

    def _call(self, part: dict[str, Any], shown: list[Any], kept: list[Any], natives: list[tuple[str, str]],
              renamed: dict[str, str], texted: list[str], counts: dict[str, int]) -> None:
        call_id = str(part.get("call_id") or "")
        name = str(part.get("name") or "tool")
        raw = part.get("input")
        lowered = name.lower()
        result = self.results.get(call_id) if call_id else None
        # ``kind`` is the part's own discriminator, so the call's kind travels as ``tool_kind``.
        call_kind = str(part.get("tool_kind") or ("mcp" if part.get("server") else "native"))
        mapped = native_call(name, raw, call_kind)
        if mapped is not None and result is not None and call_id not in self.consumed:
            # A renamed call keeps its id, so its result still pairs with it.
            tool, args = mapped
            block = ToolUseBlock(tool_call_id=call_id, name=tool, arguments_json=_json(args))
            shown.append(block)
            kept.append(block)
            natives.append((call_id, name))
            renamed[call_id] = name
            counts["native_calls"] += 1
            return
        if call_id:
            self.consumed.add(call_id)
        counts["text_calls"] += 1
        texted.append(name)
        full, short = self._render(name, lowered, part, raw, result)
        shown.append(TextBlock(text=full))
        if lowered in _PLAN_TOOLS and call_id != self.last_plan:
            return  # only the latest plan is worth the model's context; the transcript keeps every one
        kept.append(TextBlock(text=short))

    def _render(self, name: str, lowered: str, part: dict[str, Any], raw: Any, result: dict[str, Any] | None) -> tuple[str, str]:
        """A call the model has no tool for, as text: the whole of it for the transcript, and a short form
        for the history."""
        server = str(part.get("server") or "")
        # An MCP tool is named by its server unless its name says it already (Claude Code's ``mcp__notes__append``).
        label = f"[{self.label} · {server + '.' if server and server not in name else ''}{name}]"
        output = self._output(result)
        if result is None:
            ending_full = ending_short = "no result was recorded (the call was interrupted)"
        elif result.get("is_error"):
            ending_full, ending_short = f"error: {clip(output, TRANSCRIPT_OUTPUT_CHARS)}", f"error: {_short(output, TEXT_RESULT_CHARS)}"
        else:
            ending_full, ending_short = clip(output, TRANSCRIPT_OUTPUT_CHARS), _short(output, TEXT_RESULT_CHARS)
        args = _arguments(raw)
        if lowered in _PLAN_TOOLS:
            plan = plan_text(raw)
            return f"{label} {plan}", f"{label} {plan}"
        if lowered in _AGENT_TOOLS:
            what = str(args.get("description") or args.get("subagent_type") or args.get("agent_type") or "sub-agent")
            task = str(args.get("prompt") or args.get("message") or args.get("task") or "")
            full = f"[{self.label} · sub-agent «{what}»] task: {clip(task, TRANSCRIPT_ARGS_CHARS)}\nresult: {ending_full}"
            short = f"[{self.label} · sub-agent «{what}»] task: {_short(task, TEXT_ARGS_CHARS)}; result: {ending_short}"
            return full, short
        if lowered == "apply_patch":
            patch = _first(args, "input", "patch")
            files = parse_patch(patch) if isinstance(patch, str) else []
            if files:
                changed = ", ".join(_patch_line(f) for f in files)
                return f"{label} changed: {changed}\n{clip(str(patch), TRANSCRIPT_ARGS_CHARS)}\n→ {ending_full}", f"{label} changed: {changed} → {ending_short}"
        shown_args = raw if isinstance(raw, str) else _json(args)
        return f"{label} {clip(shown_args, TRANSCRIPT_ARGS_CHARS)}\n→ {ending_full}", f"{label} {_short(shown_args, TEXT_ARGS_CHARS)} → {ending_short}"


def _joined(blocks: Sequence[Any]) -> list[Any]:
    """Neighbouring text blocks as one, a blank line between them. A reader joins a message's text
    blocks with nothing in between, and a rendered call followed by the answer read as one line."""
    out: list[Any] = []
    for block in blocks:
        if isinstance(block, TextBlock) and out and isinstance(out[-1], TextBlock):
            out[-1] = TextBlock(text=f"{out[-1].text}\n\n{block.text}")
        else:
            out.append(block)
    return out


def _patch_line(entry: dict[str, Any]) -> str:
    if entry["op"] == "add":
        return f"{entry['path']} (new)"
    if entry["op"] == "delete":
        return f"{entry['path']} (deleted)"
    moved = f" → {entry['move']}" if entry.get("move") else ""
    return f"{entry['path']}{moved} (+{entry['added']} −{entry['removed']})"


# -- what the working history keeps ---------------------------------------------------------------


@dataclass
class Plan:
    """Which entries the working history is made of.

    ``mode`` is ``full`` (everything since the program's own last compaction) or ``tail`` (a summary
    and the last turns verbatim). ``summary`` is the index of the program's last compaction summary,
    or -1. ``gap`` are the entries a summary of ours has to stand for (tail mode), ``kept`` the
    entries whose history form is the history after the summary, and ``archived`` every entry the
    summary stands for, whose transcript rows HistoryExpand returns."""

    mode: str
    summary: int
    kept: list[int]
    gap: list[int]
    archived: list[int]


def large(conversion: Conversion, window: int) -> bool:
    """Whether a full import would fill too much of the model's window to start from."""
    history = [e for e in conversion.entries if e.history is not None]
    return conversion.tokens() > LARGE_WINDOW_SHARE * window or len(history) > LARGE_MESSAGES


def plan_history(conversion: Conversion, *, mode: str, window: int) -> Plan:
    entries = conversion.entries
    main = [i for i, e in enumerate(entries) if not e.sidechain]
    summaries = [i for i in main if entries[i].summary]
    last_summary = summaries[-1] if summaries else -1
    after = [i for i in main if i > last_summary and entries[i].history is not None]
    if mode != "tail":
        kept = ([last_summary] if last_summary >= 0 else []) + after
        archived = list(range(last_summary)) if last_summary > 0 else []
        return Plan("full", last_summary, kept, [], archived)
    budget = int(TAIL_WINDOW_SHARE * window)
    used = 0
    users = 0
    start = len(after)
    for position in range(len(after) - 1, -1, -1):
        entry = entries[after[position]]
        cost = estimate_tokens(entry.tail_history or entry.history)
        if used + cost > budget and users >= TAIL_MIN_USER_TURNS:
            break
        used += cost
        start = position
        if entry.operator:
            users += 1
    # The tail starts at a message of the operator's, never between a call and its result.
    while start > 0 and not entries[after[start]].operator:
        start -= 1
    if start <= 0 and last_summary < 0:
        return Plan("full", -1, after, [], [])
    gap = after[:start]
    kept = after[start:]
    first_kept = kept[0] if kept else len(entries)
    return Plan("tail", last_summary, kept, gap, list(range(first_kept)))


# -- the model ------------------------------------------------------------------------------------


def _model_key(name: str) -> str:
    text = name.lower().strip().rsplit("/", 1)[-1].split(":", 1)[0]
    text = text.replace(".", "-").replace("_", "-")
    return re.sub(r"-(\d{8}|latest)$", "", text)


def match_preset(presets: dict[str, Any], source_model: str) -> str | None:
    """The preset that runs the model the session ran on, or ``None``: the same model id, ignoring the
    provider's prefix, a date suffix and how the version is punctuated (``claude-opus-4-1-20250805``
    is ``anthropic/claude-opus-4.1``)."""
    wanted = _model_key(source_model or "")
    if not wanted:
        return None
    exact = [pid for pid, preset in presets.items() if _model_key(str(getattr(preset, "model", "") or "")) == wanted]
    if exact:
        return exact[0]
    return None


__all__ = [
    "HARNESS_NAMES", "HARNESS_ORDER", "IMPORTED_KEY", "Conversion", "Converter", "Entry", "Plan", "clip", "estimate_tokens",
    "harness_name", "large", "match_preset", "native_call", "parse_patch", "parse_time", "plan_history", "plan_text", "shell_command",
]
