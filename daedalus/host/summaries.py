"""What a compaction's summariser is asked, and the text of the history it is given.

A compaction replaces the history with a summary, and the next compaction summarises that summary
together with everything after it. The chain only holds when every summary is built from the one
before it. It used to break every time: the summariser saw the last 200,000 characters of the
rendered history, the previous summary sat at the very start, and each message was cut at 4,000
characters, so an imported session lost what the other program had established at its first
compaction and kept only the last topic it worked on. Here the previous summary is handed over
whole and apart from the transcript, as the base to update, and the transcript is never cut from
the front: a history longer than one call is summarised in parts whose notes are merged into that
base, in rounds when the notes themselves are too long for one call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    OPERATOR_WORDS_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from protocore.runtime.context.ledger import is_ledger

from daedalus.host import prompts

SUMMARY_SECTIONS = ("Standing instructions", "Stable facts", "Threads", "Closed threads", "Unknowns", "Identifiers")

_HEADINGS = " · ".join(f"## {name}" for name in SUMMARY_SECTIONS)

OMISSION_RULE = """A marker `… [N chars left out] …` is where the host shortened a long message to fit \
this text; it is not part of the tool output and not a failure — never record it as truncation or as an error."""
"""Without it a summariser reads the host's own cut as the tool's and records a command as truncated
or failed when it was neither."""

SECTION_RULES = f"""Markdown with exactly these six level-2 sections, each once, in this order, titles in \
English verbatim whatever the language of the body: {_HEADINGS}.
Standing instructions: every rule, preference, boundary and way of working the operator stated that still \
applies — who owns what, what not to touch, how and where to deliver, which environment or stand to use, \
how to report — one bullet each, quoted in the operator's words, never paraphrased.
Stable facts: what stays true across tasks — where credentials and tokens are kept (the file and the line, \
never the value), environments and stands, hosts, URLs, repositories and branches, databases and tables, \
accounts, ids, commands known to work — one bullet each, verbatim.
Threads: one level-3 heading `### <short name>` per open task or topic — every one, not only the latest: \
the operator often runs several at once. Under each: Status (done so far, in progress, blocked or waiting, \
with concrete results); Decisions (with their reasons); Facts (paths, numbers, findings, approaches that \
failed and why); Operator (what the operator explained, asked or corrected about it, in their words); Next \
step (the exact next action, and where a report or result lies).
Closed threads: one line per finished or dropped thread — its name, the outcome, where the result is.
Unknowns: what the transcript does not show. The absence of a tool result or confirmation means the \
outcome is UNKNOWN, not that it happened or failed; such items go here rather than being asserted.
Identifiers: one line per path, id, URL, port, command, name or number later work may need, exactly as it \
appeared.
Never write a secret value (password, token, key); name where it is kept instead. Every identifier \
verbatim, never rounded or guessed. No commentary outside the sections.
""" + OMISSION_RULE

BASE_RULE = """The text starts with <previous-summary>: the memory of this conversation as it stood \
before the transcript that follows it. It is the base you update, not material to condense again. Carry \
every standing instruction and stable fact in it forward verbatim unless the transcript shows the operator \
withdrew it or it proved wrong; keep every thread in it under Threads, updated, until the transcript shows \
it finished or dropped, and only then move it to Closed threads; add what is new. Losing a thread, an \
instruction or a fact the base held is the worst mistake you can make here."""

COMPACT_PROMPT = """You keep the working memory of a long conversation between the operator and an agent, \
so that the agent continues in a fresh context and the operator never has to explain anything again. \
Write it in {language} as """ + SECTION_RULES + """
{base_rule}At most ~{max_words} words; when that is too few, shorten Closed threads and Identifiers first, \
never Standing instructions or Stable facts."""

CHUNK_PROMPT = """The text below is ONE PART (part {index} of {total}) of a longer conversation between the \
operator and an agent. Write notes on this part only, in {language}, as """ + SECTION_RULES + """
At most ~{max_words} words."""

MERGE_NOTES_PROMPT = """Below are notes on consecutive parts of one conversation, oldest first. Merge them \
into one set of notes in {language}, keeping the latest known state of each thread, every operator \
instruction quoted, every fact and identifier verbatim, as """ + SECTION_RULES + """
At most ~{max_words} words."""

MERGE_PROMPT = """You keep the working memory of a long conversation between the operator and an agent, \
so that the agent continues in a fresh context and the operator never has to explain anything again. \
Below are notes on consecutive parts of the conversation, oldest first. Write the memory from them in \
{language} as """ + SECTION_RULES + """
{base_rule}At most ~{max_words} words; when that is too few, shorten Closed threads and Identifiers first, \
never Standing instructions or Stable facts."""

CHARS_PER_TOKEN = 4

SUMMARY_WORDS_PER_WINDOW = 100
"""One word of summary per this many tokens of the model's window: 4,000 words on a 400k window. The
summary is the only memory of everything it replaces; 1,200 words for a session of five parallel tasks
was the budget that kept only the latest one."""
SUMMARY_WORDS_CAP = 8000
SUMMARY_TOKENS_PER_WORD = 3
"""Output tokens one summary word may take: Russian runs at about two and a half."""
SUMMARY_MAX_OUTPUT_TOKENS = 32_000
MERGE_ROUNDS_MAX = 4

ASSISTANT_TEXT_CHARS = 8000
"""An agent's message is cut in the middle past this; its conclusions are at the start and the end."""
NOTE_TEXT_CHARS = 12_000
"""A user-role message that is not the operator's — a subagent's report, an event batch — is cut in
the middle past this. Reports run to thirteen thousand characters and carry the result of a thread."""
TOOL_ARGUMENTS_CHARS = 1000
RESULT_CHARS = 8000
"""A tool result is cut in the middle past this. At 1,500 an id or a verdict a third of the way into
a result never reached the summariser, which then recorded the outcome as unknown; the history is
already summarised in parts of ``chunk_tokens``, so a longer result costs summariser tokens, not a
call that does not fit."""
MEMORY_RESULT_CHARS = 12_000
"""A read of an instruction or memory file is the agent learning the operator's standing knowledge;
it is shown nearly whole rather than as its first and last lines."""

MEMORY_FILE_RE = re.compile(
    r"(?:^|[\s/\\'\"=:])(?:AGENTS|CLAUDE|CLAUDE\.local|MEMORY|GEMINI|GROK)\.md\b"
    r"|[/\\]memor(?:y|ies)[/\\][^\s'\"]*\.md\b",
)
"""A path or command that reads an instruction file or a note of a memory directory."""


@dataclass(frozen=True)
class SummaryInput:
    """What the summariser is given: the memory as it stood, and what happened since, both whole."""

    previous: str
    transcript: str


def clip_middle(text: str, limit: int) -> str:
    """``text`` cut in its middle to about ``limit`` characters: a result's verdict sits at its start or its end."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]} … [{len(text) - limit} chars left out] … {text[-half:]}"


def _is_operator(message: Message) -> bool:
    if message.role is not MessageRole.user:
        return False
    marked = message.metadata.get(OPERATOR_WORDS_METADATA_KEY)
    if isinstance(marked, list):
        return bool(marked)
    return marked is True or (marked is None and message.metadata.get("daedalus.origin") == "operator")


def _summary_text(message: Message) -> str:
    text = "\n".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))
    return re.sub(r"</?compacted-turn[^>]*>", "", text).strip()


def leading_summaries(history: Sequence[Message]) -> int:
    """How many messages at the start of ``history`` are earlier summaries (a ledger among them counts).

    Only the leading ones are the memory to update. A summary further in is the core's summary of
    one span, written mid-run, and is read in its place in the transcript."""
    index = 0
    while index < len(history) and (history[index].metadata.get(COMPACTION_SUMMARY_METADATA_KEY) or is_ledger(history[index])):
        index += 1
    return index


def summary_input(history: Sequence[Message], *, result_chars: int = RESULT_CHARS) -> SummaryInput:
    """The previous summary and the rest of the history, rendered for the summariser.

    Nothing is cut from the front or the back. Per message: what the operator wrote is never cut;
    another summary is never cut; an agent's text, a note and a tool exchange are cut in the middle
    past their bounds, except a read of an instruction or memory file, which keeps much more."""
    start = leading_summaries(history)
    previous = "\n\n".join(text for m in history[:start] if not is_ledger(m) and (text := _summary_text(m)))
    calls: dict[str, str] = {}
    lines: list[str] = []
    for message in history[start:]:
        if message.role is MessageRole.system or is_ledger(message):
            continue  # the ledger is carried beside the summary, not through it
        summary = bool(message.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
        operator = _is_operator(message)
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                text = prompts.without_turn_context(block.text).strip()  # the clock and the board of a past turn are not history
                if not text:
                    continue
                if summary:
                    lines.append(f"[summary of earlier turns] {_summary_text(message)}")
                elif operator:
                    lines.append(f"[operator] {text}")
                elif message.role is MessageRole.assistant:
                    lines.append(f"[assistant] {clip_middle(text, ASSISTANT_TEXT_CHARS)}")
                else:
                    lines.append(f"[{message.role.value}] {clip_middle(text, NOTE_TEXT_CHARS)}")
            elif isinstance(block, ToolUseBlock):
                arguments = block.arguments_json or ""
                calls[block.tool_call_id] = arguments
                lines.append(f"[tool call] {block.name} {clip_middle(arguments, TOOL_ARGUMENTS_CHARS)}")
            elif isinstance(block, ToolResultBlock):
                body = (block.content or "").strip()
                memory = bool(MEMORY_FILE_RE.search(calls.get(block.tool_call_id, "") + " " + str(block.path or "")))
                lines.append(f"[tool result{' ERROR' if block.is_error else ''}] {clip_middle(body, MEMORY_RESULT_CHARS if memory else result_chars)}")
            elif isinstance(block, ThinkingBlock):
                continue
    return SummaryInput(previous=previous, transcript="\n".join(lines))


def transcript_for_summary(history: Sequence[Message], *, result_chars: int = RESULT_CHARS) -> str:
    """The whole history as one text: the previous summary first, then the transcript."""
    rendered = summary_input(history, result_chars=result_chars)
    if not rendered.previous:
        return rendered.transcript
    return f"[summary of earlier turns] {rendered.previous}\n{rendered.transcript}"


def with_base(previous: str, body: str) -> str:
    """The summariser's body: the previous summary, marked as the base, then what follows it."""
    if not previous:
        return body
    return f"<previous-summary>\n{previous}\n</previous-summary>\n\n{body}"


def split_transcript(text: str, chunk_tokens: int) -> list[str]:
    """Consecutive parts of a rendered transcript, each about ``chunk_tokens`` (4 chars per token), split on line ends.

    A single line longer than a part (an operator's long message, a summary) is split inside
    rather than sent as a part the summariser cannot take."""
    limit = max(1, chunk_tokens) * 4
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def pack(notes: Sequence[str], limit: int) -> list[list[str]]:
    """Consecutive notes grouped to stay within ``limit`` characters, at least two to a group.

    Two at least, so every round of merging halves the notes even when each is long: a merge's
    output is bounded by its word budget, so the rounds converge where groups of one never would."""
    groups: list[list[str]] = []
    size = 0
    for note in notes:
        if groups and (size + len(note) <= limit or len(groups[-1]) < 2):
            groups[-1].append(note)
            size += len(note)
        else:
            groups.append([note])
            size = len(note)
    return groups


def joined_notes(notes: Sequence[str]) -> str:
    return "\n\n".join(f"<part {i + 1}>\n{text}\n</part {i + 1}>" for i, text in enumerate(notes) if text)


def summary_words(floor: int, window: int) -> int:
    """The summary's length budget: the configured floor, or more on a larger window."""
    return max(floor, min(SUMMARY_WORDS_CAP, window // SUMMARY_WORDS_PER_WINDOW)) if window > 0 else floor


def summary_max_tokens(words: int) -> int:
    return min(SUMMARY_MAX_OUTPUT_TOKENS, max(6000, words * SUMMARY_TOKENS_PER_WORD + 1000))


def compact_prompt(*, language: str, max_words: int, has_base: bool) -> str:
    return COMPACT_PROMPT.format(language=language, max_words=max_words, base_rule=BASE_RULE + "\n" if has_base else "")


def merge_prompt(*, language: str, max_words: int, has_base: bool) -> str:
    return MERGE_PROMPT.format(language=language, max_words=max_words, base_rule=BASE_RULE + "\n" if has_base else "")


def validate_summary_sections(summary: str) -> str:
    """The reason a summary is rejected, or an empty string when every section appears once, in order."""
    positions: list[int] = []
    for name in SUMMARY_SECTIONS:
        pattern = re.compile(rf"^##\s+{re.escape(name)}\s*$", re.MULTILINE | re.IGNORECASE)
        found = pattern.findall(summary)
        if len(found) != 1:
            return f"section '## {name}' appears {len(found)} times (expected once)"
        positions.append(pattern.search(summary).start())  # type: ignore[union-attr]
    if positions != sorted(positions):
        return "sections are out of order"
    return ""


__all__ = [
    "COMPACT_PROMPT", "CHUNK_PROMPT", "MEMORY_FILE_RE", "MERGE_NOTES_PROMPT", "MERGE_PROMPT", "SUMMARY_SECTIONS", "SummaryInput",
    "compact_prompt", "joined_notes", "leading_summaries", "merge_prompt", "pack", "split_transcript", "summary_input",
    "summary_max_tokens", "summary_words", "transcript_for_summary", "validate_summary_sections", "with_base",
]
