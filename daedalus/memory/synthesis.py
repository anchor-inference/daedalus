"""The background pass that files what the operator said in their chats into memory.

A chat is read when it has been quiet for a while, in a nightly sweep, or when the operator asks from
the Memory settings. The model sees the operator's own messages verbatim, only the agent's final answer
before each of them (clipped), the run headlines, and the memory as it stands; it answers with a JSON
list of operations. The model proposes and this module disposes: every operation goes through checks
in code before anything is written, and what one pass over one chat changed is one history entry the
operator can undo.

What the checks encode, each found on a benchmark over the operator's real chats before it shipped:

* tool results never reach the model, and neither do the agent's intermediate narration lines — given
  them, every model filed what the agent said it had built (ports, defaults, library names) as the
  operator's facts;
* a bullet in ``you/preferences`` must quote the operator word for word, or the pass could invent
  "prefers short answers" from behaviour — the guard that keeps memory from becoming a response mode;
* a bullet naming an identifier the operator never wrote is held as a suggestion rather than written,
  because it is almost always the agent's own choice reported back;
* sensitive subjects need the operator's leave (the word lists united with the model's own label: the
  lists alone missed whole phrasings), and a few subjects are never stored whatever the switches say;
* what the operator removed is never learned again (tombstones), and what they wrote or edited is never
  changed by the pass, only proposed (locked bullets become suggestions);
* a fact memory already holds gains the chat as a second source instead of a duplicate bullet.

A chat's watermark (``memory_synthesis.through_seq``) moves only after a part of it was applied, so a
failed call reads the same messages again next time rather than losing them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.memory import MemoryRecord
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.host.engine_factory import TENANT
from daedalus.host.prompts import split_headline, without_host_notes
from daedalus.memory.curator import words_of
from daedalus.memory.entries import (
    PREFERENCES,
    PROFILE,
    SUMMARY_MAX_CHARS,
    Entry,
    Target,
    area_slug,
    entry_of,
    fingerprint,
    is_archived,
    is_locked,
    slugify,
    topic_title,
)
from daedalus.memory.taxonomy import SENSITIVE, guess_category, infer_topics, sensitive_subjects
from daedalus.search.chats import PLAIN_PROJECT_SQL, SEARCHABLE_SQL
from daedalus.security.redact import redact as redact_text
from daedalus.security.untrusted import fenced, injection_signs

if TYPE_CHECKING:
    from daedalus.config import RuntimeConfig
    from daedalus.memory.curator import MemoryCurator
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

# -- when ------------------------------------------------------------------------------------------

IDLE_SECONDS = 600
"""A chat is read once it has been quiet this long: the conversation has settled, and a pass never
shares the provider (or, for a local model, the GPU) with a run of the chat it reads."""
RECENT_SECONDS = 48 * 3600
"""The quiet-chat trigger looks only at chats active this recently; older ones wait for the nightly
sweep or the operator's "update now", so a first start does not read the whole backlog by itself."""
TICK_SECONDS = 60
NIGHTLY_AT = (3, 30)
"""Local time of the nightly sweep, which reads whatever the quiet-chat trigger passed over."""
MIN_NEW_OPERATOR_MESSAGES = 2
"""The quiet-chat trigger waits for this many new operator messages, or for a long unread slice: one
"thanks" is not worth a call. The nightly sweep and "update now" read a single message too."""
LONG_SLICE_CHARS = 40_000

NOT_THE_OPERATORS = ("staff_session_id", "spawned_by", "companion_parent")
"""Session metadata, beyond what the chat search already leaves out (subagents, staff, schedules,
heartbeats, coaches, side questions, private chats), that marks a session as an agent's work rather
than the operator's conversation: a team member's session, one another agent started, one the
companion delegated to. Imported sessions are the operator's own chats held elsewhere and are read."""

RECOMMENDED_PRESETS = ("opencode.deepseek-v4.1-flash", "deepseek.deepseek-v4-flash")
"""The model the pass runs on when the operator chose none, in order of preference: on the benchmark
it had every answer valid JSON at the first try, the best precision outside one build chat, no bullet
the agent invented and no one-off request filed as a preference. The first is a prepaid route; the
second, the same model metered, is the fallback when the first fails."""

# -- input -----------------------------------------------------------------------------------------

OPERATOR_MESSAGE_CHARS = 1200
"""The ``user_style`` ceiling: what someone wants shows early in a message."""
ANSWER_CHARS = 600
MAX_INPUT_CHARS = 40_000
"""One call's slice of a chat; a longer chat is read in parts, each seeing what the earlier wrote."""
HEADLINES = 20
HEADLINE_CHARS = 200
FOCUS_CHARS = 12_000
"""The existing bullets shown to the model, so it updates rather than repeats."""
LISTING_ENTRIES = 60

# -- the call --------------------------------------------------------------------------------------

CALL_TIMEOUT_SECONDS = 120.0
MAX_TOKENS = 4096
TEMPERATURE = 0.1
PURPOSE = "memory"
"""What Usage books the pass under; the daily budget sums every ``memory…`` purpose."""

# -- caps ------------------------------------------------------------------------------------------

CAP_YOU_TOPIC = 25
CAP_AREA = 40
CAP_ENTRIES = 60
CAP_OPS_PER_CALL = 40
BULLET_CHARS = 240
DUPLICATE_SURE = 0.85
"""Share of words a new bullet must share with one memory has (in the same scope) to be read as the
same fact said again — the curator's sure-duplicate threshold, so the two never disagree."""


# -- input building --------------------------------------------------------------------------------


@dataclass(slots=True)
class Turn:
    """One message as the pass sees it: already stripped of the host's notes and redacted."""

    seq: int
    role: str
    """``operator`` or ``assistant``."""
    text: str


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    at = cut.rfind(" ")
    return (cut[:at] if at > limit * 0.7 else cut).rstrip() + "…"


def condense(turns: Sequence[Turn]) -> list[Turn]:
    """Operator turns, plus only the agent's last text before each of them (its final answer).

    Every narration line of a tool run used to come along too: on the benchmark the agent's text was
    3.7M of 4.5M input characters, and the model filed what the agent said it had built as the
    operator's facts. The final answer is the context the operator reacted to; the rest is the agent
    talking to itself."""
    out: list[Turn] = []
    pending: Turn | None = None
    for turn in turns:
        if turn.role == "operator":
            if pending is not None:
                out.append(pending)
                pending = None
            out.append(turn)
        else:
            pending = turn
    if pending is not None:
        out.append(pending)
    return out


def slices(turns: Sequence[Turn], budget: int = MAX_INPUT_CHARS) -> list[list[Turn]]:
    """The turns in parts whose rendered size stays under ``budget``. Sizes use the clip the input
    applies, not the raw bodies: a chat of long answers must not become hundreds of near-empty calls."""
    out: list[list[Turn]] = []
    current: list[Turn] = []
    size = 0
    for turn in turns:
        cost = min(len(turn.text), OPERATOR_MESSAGE_CHARS if turn.role == "operator" else ANSWER_CHARS) + 24
        if size + cost > budget and current:
            out.append(current)
            current, size = [], 0
        current.append(turn)
        size += cost
    if current:
        out.append(current)
    return out


def build_input(turns: Sequence[Turn], headlines: Sequence[str], listing: Sequence[str], focus: Sequence[str], *, notes: Sequence[str] = (), imported: str = "") -> tuple[str, dict[int, str]]:
    """The user message of one call, and the operator's full text by seq (for the quote check).

    ``listing`` and ``focus`` are rendered lines (``  - section/slug — title: summary`` and
    ``  - section/slug · handle: text``). ``imported`` names the program an imported chat came from:
    its transcript is fenced as data, since a file another program wrote can carry anything."""
    operator_text: dict[int, str] = {}
    lines: list[str] = []
    budget = MAX_INPUT_CHARS
    for turn in turns:
        if turn.role == "operator":
            operator_text[turn.seq] = turn.text
            block = f"[operator @seq={turn.seq}] {clip(turn.text, OPERATOR_MESSAGE_CHARS)}"
        else:
            block = f"[agent] {clip(turn.text, ANSWER_CHARS)}"
        if budget - len(block) < 0:
            break
        budget -= len(block) + 1
        lines.append(block)
    transcript = "\n".join(lines)
    if imported:
        transcript = fenced(transcript, kind="imported chat", origin=imported, source="a session the operator imported from another program", quoted_by="the memory pass")
    head = ""
    if headlines:
        head = "Run headlines (what the agent did, for context only — never a memory on their own):\n" + "\n".join(f"  - {clip(h, HEADLINE_CHARS)}" for h in headlines[:HEADLINES])
    parts = [
        "CHAT TRANSCRIPT (operator messages verbatim with their seq; agent answers clipped; no tool output):",
        transcript,
        head,
        "Existing entries you may add to or update:\n" + ("\n".join(listing) or "  (none yet)"),
        "Current bullets of the entries this chat most likely touches:\n" + ("\n".join(focus) or "  (none)"),
        *notes,
        "Return the JSON object of operations now.",
    ]
    return "\n\n".join(p for p in parts if p), operator_text


SYSTEM_PROMPT = """\
You maintain a long-term memory about one person (the operator) from their own chats with an AI agent.
You get one chat (or one part of a long chat): the operator's messages verbatim, each tagged
[operator @seq=N], and the agent's answers clipped, tagged [agent]. You return a JSON object of
operations that add to or correct the memory. The memory is a set of titled entries holding short
factual bullets. A later, unrelated chat will read them as background about the operator.

THE TEST FOR EVERY BULLET: would this still be true and useful to know in a different chat a month
from now? If not, leave it out.

RECORD only what the operator stated or decided:
  - who they are and what they work on; people, projects, products, tools and services they named;
  - settled decisions and standing constraints of a project (stack, conventions, deadlines);
  - tastes and habits they stated plainly;
  - standing instructions about how the agent should work in general.
When the operator changes their mind later in the same chat, record only the final position.

NEVER RECORD:
  - anything the agent said, suggested, found, inferred or concluded; the run headlines are context only;
  - implementation details the agent chose — ports, default values, library or model names, file layouts,
    UI specifics — unless the operator themselves named or decided them in an [operator] message;
  - how the operator writes, how long their messages are, or how much effort or length an answer should
    have — that is never a memory;
  - transient state: what is in stock or in the fridge right now, balances, which draft or variant was
    picked during an iteration, version numbers of outputs, file names and paths of generated files,
    dates of a one-off plan, bugs being fixed, the steps the agent took;
  - a request that only concerns the task at hand ("give links", "attach the files", "make a list",
    "try again", "make it less loud") — these are not preferences;
  - immigration or residence status, residence permits, visas, citizenship, criminal records, government
    ID numbers, bank or card numbers, health, intimate life, a home address — even if the operator said them;
  - secrets, tokens, passwords, keys, or anything shown as <redacted>/<token>.

SECTIONS
  - you/preferences: standing instructions for the agent that apply BEYOND this task — phrased by the
    operator as a general rule ("always…", "never…", "from now on…", "in general…", "всегда",
    "никогда", "впредь", "в целом", "по умолчанию"), or repeated across the chat. Each such bullet MUST
    carry "quote": an exact substring (3–200 chars) copied from one [operator] message, containing no
    double-quote character. No quote, no bullet. A requirement for one project goes to that project's
    area, not here.
  - you/profile: role, occupation, company, what they build, languages they work in, city/country only
    as coarse as volunteered.
  - topic/<slug>: a recurring subject of their life or work (food tastes, a hobby, a toolset, people).
  - area/<slug>: one ongoing project or body of work — its purpose, stack, decisions and standing rules.
    An area is not a backlog: individual feature requests, UI tweaks and bug reports are not bullets;
    at most one bullet may summarise what the project's product must do.

OPERATIONS — return exactly {"ops": [ ... ]} where each element is one of:
  {"op":"new_entry","section":"topic|area","slug":"<kebab-case>","title":"<short title>","summary":"<=150 chars"}
  {"op":"add","entry":"<section>/<slug>","text":"<one fact, <=200 chars>","seqs":[N],"subjects":[],"quote":"<only for you/preferences>"}
  {"op":"update","record_id":"<id from the current bullets>","text":"<corrected fact>","seqs":[N],"subjects":[]}
  {"op":"retire","record_id":"<id>","seqs":[N],"reason":"<why it is no longer true>"}
  {"op":"summary","entry":"<section>/<slug>","text":"<=150 chars>"}
"subjects" labels what the fact touches, from exactly this list: "health", "finances", "intimate",
"identity_documents", "precise_location"; use [] when none applies. Label honestly — the label decides
whether the fact may be kept, so a missing label is worse than an extra one.
Rules: put new_entry before the adds that use it; reuse an existing entry when one fits; one fact per
bullet; cite the operator seq(s) the fact rests on; do not repeat a fact already in the current
bullets — update or skip it instead; write each bullet in the language the operator wrote that part
in — Russian stays Russian, never translate; at most 12 add ops per answer — keep the most durable ones.

If nothing in this part is durable, return {"ops": []}. Prefer too few to too many: a wrong memory
costs the operator a correction later. Output only the JSON object, no prose, no code fence.\
"""
"""The benchmarked prompt (the fifth round), model-agnostic and naming no one. Changing it is a
measurement, not an edit: the precision and recall it was chosen on were measured with these words."""


# -- the answer ------------------------------------------------------------------------------------


@dataclass(slots=True)
class ParseResult:
    ok: bool
    ops: list[dict[str, Any]]
    error: str = ""
    recovered: bool = False
    """The JSON had to be dug out of prose or a fence."""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_OBJECT = re.compile(r"\{.*\}", re.S)
_REQUIRED: dict[str, tuple[str, ...]] = {
    "add": ("entry", "text"),
    "update": ("record_id", "text"),
    "retire": ("record_id",),
    "new_entry": ("section", "slug", "title"),
    "summary": ("entry", "text"),
}


def parse_ops(raw: str) -> ParseResult:
    """The model's answer as a validated op list: the whole answer as JSON first, then the first fenced
    block, then the first ``{…}`` span. Strict past that: an op with an unknown name or a missing key
    makes the whole answer invalid (and earns one corrective retry) rather than half-applied."""
    raw = (raw or "").strip()
    recovered = False
    data = _json(raw)
    if data is None and (found := _FENCE.search(raw)):
        data = _json(found.group(1))
        recovered = data is not None
    if data is None and (found := _OBJECT.search(raw)):
        data = _json(found.group(0))
        recovered = data is not None
    if data is None:
        return ParseResult(False, [], "no JSON object found")
    ops = data.get("ops") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if not isinstance(ops, list):
        return ParseResult(False, [], "no 'ops' list", recovered)
    clean: list[dict[str, Any]] = []
    for item in ops:
        if not isinstance(item, dict):
            return ParseResult(False, clean, f"an op is not an object: {type(item).__name__}", recovered)
        name = item.get("op")
        if name not in _REQUIRED:
            return ParseResult(False, clean, f"unknown op {name!r}", recovered)
        for key in _REQUIRED[name]:
            if not item.get(key):
                return ParseResult(False, clean, f"op {name!r} is missing {key!r}", recovered)
        clean.append(item)
    return ParseResult(True, clean, "", recovered)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


# -- what is never kept ----------------------------------------------------------------------------

NEVER_STORE: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Not a switch: no setting allows these. On the benchmark every model filed a residence permit into
    # the profile, and the taxonomy's word list knew only one phrasing of it.
    ("immigration", re.compile(
        r"\b(residence permit|work permit|permanent residen\w*|visas?|immigration|citizenship|asylum|refugee|deport\w*)\b"
        r"|\b(рвп|внж|пмж)\b|разрешени\w* на (временное )?(проживани|работ)|вид\w* на жительств|виз[аыуе]\b"
        r"|гражданств\w*|\bгражданин\w*|\bгражданк\w*|иммиграц\w*|беженц\w*|депортац\w*|миграцион\w*"
        # On the first real pass a refused permanent residence was filed in plain words, with no term above.
        r"|\b(постоянн|временн)\w* (проживани|резидентств)\w*",
        re.IGNORECASE)),
    ("criminal_record", re.compile(
        r"\b(criminal record|conviction|arrest(ed)?|police record)\b|судимост\w*|несудимост\w*|арест\w*|уголовн\w* дел\w*",
        re.IGNORECASE)),
    ("government_id", re.compile(r"\b(иин|бин|инн|снилс|ssn)\b[^\n]{0,12}\d{6,}|(?<!\d)\d{12}(?!\d)", re.IGNORECASE)),
    ("redacted", re.compile(r"<(redacted|token|creds|key)>|\*{4,}", re.IGNORECASE)),
    # A password or PIN with its value. The redactor masks credential shapes and is conservative about
    # prose on purpose, so "пароль поставь 482910" typed by the operator reaches the pass intact; on the
    # benchmark both DeepSeek routes filed such a site password as a project fact.
    ("secret", re.compile(
        r"(password|passcode|passphrase|\bpin\b|парол\w*|пин-?код\w*|код доступа)[^\n]{0,24}?[\"'«:= ]\s*[\"'«]?(?=[^\s\"'»]*\d)[^\s\"'»]{4,}",
        re.IGNORECASE)),
)


def never_store(text: str) -> list[str]:
    """The subjects of ``text`` that are never kept, whatever the switches say; empty when none."""
    return [label for label, pattern in NEVER_STORE if pattern.search(text or "")]


# -- grounding -------------------------------------------------------------------------------------

_GROUND_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@%+-]*[A-Za-z0-9%]|\d+")
_GROUND_COMMON = frozenset({
    "api", "ui", "ux", "ok", "pc", "tts", "url", "fps", "fhd", "ai", "ии", "id", "the", "and", "for", "with", "not", "site",
    "video", "videos", "operator", "agent", "telegram", "tiktok", "youtube", "openrouter", "gpt", "model", "models",
    # Added after the first real pass held these back by the dozen: common nouns of the trade that a
    # Russian speaker writes in Cyrillic or in passing ("гитхаб", "прод"), never a choice the agent made.
    "github", "gitlab", "git", "docker", "kubernetes", "linux", "windows", "android", "ios", "macos", "web", "app", "bot", "cli",
    "gpu", "cpu", "ram", "ssd", "vpn", "dns", "tls", "ssl", "http", "https", "ssh", "json", "html", "css", "sql", "pdf", "email",
    "prod", "test", "dev", "readme", "dashboard", "backend", "frontend", "online", "off", "com", "env", "rtx", "ide", "llm",
})
"""Words that look like identifiers in a Russian bullet but say nothing the agent could have chosen."""


def ident_tokens(text: str) -> list[str]:
    """The tokens of ``text`` that look like identifiers: a Latin word inside a Cyrillic bullet, a
    snake, camel or dotted name, an address, a percentage. Plain numbers and clock times are not,
    because models routinely reformat them ("2к" becomes 2000, "в 9" becomes 9:00)."""
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ") > len(text) * 0.3
    out = []
    for token in _GROUND_TOKEN.findall(text):
        lower = token.lower()
        if lower in _GROUND_COMMON or (len(lower) < 3 and not lower.isdigit()):
            continue
        if re.fullmatch(r"\d+|\d{1,2}:\d{2}", token):
            continue
        if (any(c.isdigit() for c in token) or "_" in token or "." in token.strip(".")
                or (cyrillic and re.match(r"[A-Za-z]", token)) or re.search(r"[a-z][A-Z]", token)):
            out.append(token)
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def ungrounded_tokens(text: str, operator_text: str) -> list[str]:
    """The identifier-like tokens of ``text`` the operator never wrote; empty when it is grounded.

    On the labelled benchmark set this caught 8 of 11 bullets that filed the agent's own choices for 3
    of 39 good ones, which is why what it catches is held as a suggestion rather than thrown away."""
    source = _squash(operator_text)
    return [t for t in ident_tokens(text) if _squash(t) not in source and _squash(t).rstrip("%") not in source]


def quote_backed(quote: str, seqs: Iterable[Any], operator_text: dict[int, str]) -> bool:
    """Whether a preference's quote is a real substring of an operator message: the cited seqs first,
    then any message of the part (a model that cites the wrong seq but quotes a real line is still
    honest; one that invents the line is not). Case and spacing aside."""
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip().lower()

    wanted = norm(quote)
    if len(wanted) < 3:
        return False
    cited = [operator_text[n] for n in _seqs(seqs) if n in operator_text]
    return any(wanted in norm(text) for text in [*cited, *operator_text.values()])


def _seqs(values: Iterable[Any]) -> list[int]:
    out = []
    for value in values if isinstance(values, list | tuple) else []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


# -- the model -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    preset: str
    """The preset the pass runs on; empty when no model is configured at all."""
    fallbacks: tuple[str, ...] = ()
    """Presets tried in turn when a call on the first fails."""
    note: str = ""
    """Why it is not the one the operator chose or the recommended one, in words for the log and the report."""
    reason: str = ""
    """The same as a code the app words in its own language: ``chosen_missing`` (the chosen preset is
    gone), ``fallback`` (the recommended one is not configured; its fallback runs), ``substitute`` (no
    recommended one; a cheap one of the table runs), ``none`` (no model at all); empty when all is well."""
    missing: str = ""
    """The chosen preset that is no longer configured, for ``chosen_missing``."""


def choose_model(config: RuntimeConfig) -> ModelChoice:
    """The model memory's calls run on: the operator's choice; else the recommended cheap model;
    else the classifier's, the summariser's or a middle preset — never silently the strongest one."""
    presets = config.presets
    memory = config.memory
    missing = ""
    gone = ""
    if memory.synthesis_preset:
        if memory.synthesis_preset in presets:
            return ModelChoice(memory.synthesis_preset)
        gone = memory.synthesis_preset
        missing = f"the chosen model {gone} is no longer configured; "
    recommended = [p for p in RECOMMENDED_PRESETS if p in presets]
    if recommended:
        first = recommended[0]
        note = missing + ("" if first == RECOMMENDED_PRESETS[0] else f"{RECOMMENDED_PRESETS[0]} is not configured; using {first}")
        reason = "chosen_missing" if gone else ("" if first == RECOMMENDED_PRESETS[0] else "fallback")
        return ModelChoice(first, tuple(recommended[1:]), note.rstrip("; "), reason, gone)
    for preset, why in ((memory.classify_preset, "the classifier's model"), (config.compaction.preset, "the summariser's model"), (config.middle_preset() or "", "a middle model of the table")):
        if preset and preset in presets:
            return ModelChoice(preset, (), f"{missing}no recommended model is configured; using {why}, {preset}", "chosen_missing" if gone else "substitute", gone)
    return ModelChoice("", (), missing + "no model is configured", "none", gone)


class BudgetReached(Exception):
    """Today's spend on memory's calls reached the daily budget."""


class CallFailed(Exception):
    """Every model the pass may use failed to answer."""


@dataclass(slots=True)
class Reply:
    text: str
    cost: float
    preset: str
    truncated: bool


# -- the pass over one chat ------------------------------------------------------------------------


@dataclass(slots=True)
class Stats:
    """What a pass did and what each check caught; summed over the parts and chats of a sweep."""

    added: int = 0
    merged: int = 0
    """A fact memory already had, said again: the chat became one more source of the bullet."""
    updated: int = 0
    retired: int = 0
    new_entries: int = 0
    summaries: int = 0
    held: int = 0
    """Filed as suggestions: a change to a locked bullet, or a bullet naming what the operator never wrote."""
    dropped: Counter[str] = field(default_factory=Counter)
    subjects: Counter[str] = field(default_factory=Counter)

    def add(self, other: Stats) -> None:
        for name in ("added", "merged", "updated", "retired", "new_entries", "summaries", "held"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.dropped.update(other.dropped)
        self.subjects.update(other.subjects)

    def view(self) -> dict[str, Any]:
        return {
            "added": self.added, "merged": self.merged, "updated": self.updated, "retired": self.retired,
            "new_entries": self.new_entries, "summaries": self.summaries, "held": self.held,
            "dropped": dict(self.dropped), "subjects": dict(self.subjects),
        }

    def changed(self) -> bool:
        return bool(self.added or self.merged or self.updated or self.retired or self.new_entries or self.summaries)


@dataclass(frozen=True)
class Chat:
    id: str
    title: str
    through_seq: int
    last_at: str | None
    project_id: str = ""
    project_name: str = ""
    imported: str = ""
    """The program an imported chat came from; empty for a chat held here from the start."""


class Applier:
    """One pass over one chat: the entries and bullets as the pass sees them, the checks, the writes,
    and what one history entry must hold to undo them all.

    The caller holds the curator's lock around :meth:`apply` and :meth:`finish` (not around the model
    call: an operator's edit should not wait on a slow model)."""

    def __init__(self, curator: MemoryCurator, config: RuntimeConfig, chat: Chat) -> None:
        self.curator = curator
        self.entries = curator.entries
        self.memory = curator.memory
        self.tenant = curator.tenant
        self.config = config
        self.chat = chat
        self.allowed = set(config.memory.sensitive_allowed)
        self.by_key: dict[str, Entry] = {}
        self.handles: dict[str, str] = {}
        self.handle_of: dict[str, str] = {}
        self.before: dict[str, MemoryRecord] = {}
        self.after: dict[str, MemoryRecord] = {}
        self.created: set[str] = set()
        self.removed_sources: list[dict[str, Any]] = []
        self.added_sources: list[dict[str, Any]] = []
        self.made: list[str] = []
        self.stats = Stats()
        self.project_slug = ""
        self.tainted: set[str] = set()
        """Entries this pass made around a subject that is never kept (immigration, a criminal record)
        or an intimate one: on the first real pass a model refused on one bullet went on to file the
        same subject in neutral words ("the appeal goes to the police first"), so the rest of the pass
        adds nothing more to such an entry."""
        self.tainted_now: set[str] = set()
        """The same for entries that existed before the pass, for one answer only: one refused line in
        a project's area must not stop the rest of a long chat teaching the project anything."""
        self.existing: set[str] = set()

    async def load(self) -> None:
        """The You pages exist before the first pass (they are where preferences and the profile go)."""
        for target in (PREFERENCES, PROFILE):
            await self.entries.ensure(target)
        self.by_key = {f"{e.section}/{e.slug}": e for e in await self.entries.list()}
        self.existing = set(self.by_key)
        if self.chat.project_id:
            area = await self.entries.area_of(self.chat.project_id)
            taken = {e.slug for e in self.by_key.values() if e.section == "area"}
            self.project_slug = area.slug if area is not None else area_slug(self.chat.project_name, self.chat.project_id, taken)

    # -- what the model is shown ------------------------------------------------------------------

    def listing(self) -> list[str]:
        return [f"  - {key} — {e.title}: {e.summary or '(no summary yet)'}" for key, e in list(self.by_key.items())[:LISTING_ENTRIES]]

    def notes(self) -> list[str]:
        if not self.chat.project_id:
            return []
        return [f'This chat belongs to the project "{self.chat.project_name}": its own decisions and rules go to area/{self.project_slug}.']

    def focus(self, operator_text: str) -> list[str]:
        """The bullets of the entries this part most likely touches, each under a short handle the
        model cites for ``update``/``retire``: the You pages, the chat's project area, then the entries
        whose bullets share the most words with what the operator wrote, until the room runs out."""
        wanted = words_of(operator_text)
        ranked: list[tuple[int, int, Entry]] = []
        for entry in self.by_key.values():
            records = [r for r in self.entries.records_of(entry.id) if not is_archived(r)]
            if not records:
                continue
            first = 0 if entry.section == "you" else 1 if (entry.section == "area" and entry.slug == self.project_slug) else 2
            overlap = sum(len(words_of(r.text) & wanted) for r in records)
            if first == 2 and not overlap:
                continue
            ranked.append((first, -overlap, entry))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].slug))
        lines: list[str] = []
        room = FOCUS_CHARS
        for _, _, entry in ranked:
            for record in sorted(self.entries.records_of(entry.id), key=lambda r: r.created_at):
                if is_archived(record):
                    continue
                line = f"  - {entry.section}/{entry.slug} · {self._handle(record.id)}: {clip(' '.join(record.text.split()), 300)}{' [locked]' if is_locked(record) else ''}"
                if room - len(line) < 0:
                    return lines
                room -= len(line) + 1
                lines.append(line)
        return lines

    def _handle(self, record_id: str) -> str:
        if record_id not in self.handle_of:
            handle = f"b{len(self.handles) + 1}"
            self.handles[handle] = record_id
            self.handle_of[record_id] = handle
        return self.handle_of[record_id]

    # -- the checks and the writes ----------------------------------------------------------------

    async def apply(self, ops: Sequence[dict[str, Any]], operator_text: dict[int, str]) -> None:
        """Apply one call's operations, cheapest checks first; every refusal is counted."""
        everything = "\n".join(operator_text.values())
        first_seq, last_seq = (min(operator_text), max(operator_text)) if operator_text else (0, 0)
        self._taint(ops[:CAP_OPS_PER_CALL])
        for item in ops[:CAP_OPS_PER_CALL]:
            name = item["op"]
            seqs = [n for n in _seqs(item.get("seqs") or []) if n in operator_text] or ([first_seq, last_seq] if operator_text else [])
            evidence = (min(seqs), max(seqs)) if seqs else (0, 0)
            if name == "summary":
                await self._summary(item)
            elif name == "new_entry":
                await self._new_entry(item)
            elif name == "retire":
                await self._retire(item, evidence)
            else:
                await self._content(item, everything, operator_text, evidence)
        if len(ops) > CAP_OPS_PER_CALL:
            self.stats.dropped["capped"] += len(ops) - CAP_OPS_PER_CALL

    def _taint(self, ops: Sequence[dict[str, Any]]) -> None:
        """Mark the topic and area entries an add of this answer would put an immigration, criminal or
        intimate subject in, before any of its adds is applied — the model's order must not decide what
        slips in ahead of the bullet that gives the subject away. A password or an identity number is a
        value, not a subject, and taints nothing; You pages are never tainted, or one refused line would
        stop the profile learning anything else from the chat. Measured on the real chats: tainting
        every entry for the whole pass on any refusal dropped 166 good project bullets."""
        self.tainted_now = set()
        for item in ops:
            if item.get("op") != "add":
                continue
            key = self._key(item.get("entry"))
            if not key.startswith(("topic/", "area/")):
                continue
            text = " ".join(str(item.get("text") or "").split())
            label = item.get("subjects") if isinstance(item.get("subjects"), list) else []
            subject = {"immigration", "criminal_record"} & set(never_store(text)) or {"intimate"} & ((sensitive_subjects(text) | {s for s in label if isinstance(s, str)}) - self.allowed)
            if subject:
                (self.tainted_now if key in self.existing else self.tainted).add(key)

    def _screen(self, *texts: str, label: Iterable[Any] = ()) -> str:
        """Why these texts may not be kept, or empty: words addressed to an agent, a subject never
        stored, or a sensitive subject the operator did not allow (word lists and the model's label)."""
        joined = "\n".join(t for t in texts if t)
        if injection_signs(joined):
            return "injection"
        banned = never_store(joined)
        if banned:
            self.stats.subjects.update(banned)
            return "never_store"
        subjects = sensitive_subjects(joined) | {s for s in label if isinstance(s, str) and s in SENSITIVE}
        blocked = subjects - self.allowed
        if blocked:
            self.stats.subjects.update(sorted(blocked))
            return "sensitive"
        return ""

    def _key(self, raw: Any) -> str:
        """An entry key as the model wrote it, normalised; a bare slug names the one entry with it."""
        text = str(raw or "").strip().strip("/")
        section, _, slug = text.rpartition("/")
        slug = slugify(slug)
        if section:
            return f"{section.strip().lower()}/{slug}"
        matches = [key for key, e in self.by_key.items() if e.slug == slug]
        return matches[0] if len(matches) == 1 else f"topic/{slug}"

    async def _summary(self, item: dict[str, Any]) -> None:
        entry = self.by_key.get(self._key(item.get("entry")))
        text = clip(" ".join(str(item.get("text") or "").split()), SUMMARY_MAX_CHARS)
        if entry is None:
            self.stats.dropped["unknown_entry"] += 1
            return
        if entry.edited_by_operator:
            # The operator wrote this page's title or summary: theirs stands.
            self.stats.dropped["operator_summary"] += 1
            return
        if reason := self._screen(text):
            self.stats.dropped[reason] += 1
            return
        if text and text != entry.summary:
            fresh = await self.entries.get(entry.id)
            if fresh is None:
                return
            self.by_key[f"{entry.section}/{entry.slug}"] = await self.entries.update(entry.id, if_version=fresh.version, summary=text)
            self.stats.summaries += 1

    async def _new_entry(self, item: dict[str, Any]) -> None:
        section = str(item.get("section") or "").strip().lower()
        slug = slugify(str(item.get("slug") or ""))
        if section not in ("topic", "area") or not slug:
            self.stats.dropped["unknown_entry"] += 1
            return
        if f"{section}/{slug}" in self.by_key:
            return
        title = clip(" ".join(str(item.get("title") or "").split()), 80) or topic_title(slug)
        summary = clip(" ".join(str(item.get("summary") or "").split()), SUMMARY_MAX_CHARS)
        if reason := self._screen(title, summary):
            self.stats.dropped[reason] += 1
            return
        await self._make(section, slug, title, summary)

    async def _make(self, section: str, slug: str, title: str, summary: str = "") -> Entry | None:
        """An entry the model named, made now; ``None`` past the cap on entries. The chat's own project
        area is made bound to the project, so its bullets live in the project's scope and its chats
        recall them."""
        if len(self.by_key) >= CAP_ENTRIES:
            self.stats.dropped["capped"] += 1
            return None
        if section == "area" and self.chat.project_id and slug == self.project_slug:
            entry = await self.entries.ensure(Target("area", project_id=self.chat.project_id))
            if summary and not entry.summary:
                entry = await self.entries.update(entry.id, if_version=entry.version, summary=summary)
        else:
            try:
                entry = await self.entries.create(section, slug, title, summary=summary)
            except ValueError:
                entry = await self.entries.by_slug(section, slug)
                if entry is None:
                    self.stats.dropped["unknown_entry"] += 1
                    return None
        key = f"{entry.section}/{entry.slug}"
        if key not in self.by_key:
            self.made.append(entry.id)
            self.stats.new_entries += 1
        self.by_key[key] = entry
        return entry

    def _record(self, item: dict[str, Any]) -> MemoryRecord | None:
        record_id = self.handles.get(str(item.get("record_id") or "").strip())
        record = self.memory.get_record(self.tenant, record_id) if record_id else None
        if record is None:
            self.stats.dropped["unknown_record"] += 1
        return record

    async def _retire(self, item: dict[str, Any], evidence: tuple[int, int]) -> None:
        record = self._record(item)
        if record is None:
            return
        reason = clip(" ".join(str(item.get("reason") or "").split()), 200)
        if is_locked(record):
            await self._suggest_revision(record, "", action="retire", why="locked", reason=reason, evidence=evidence)
            return
        if record.id not in self.created:
            self.before.setdefault(record.id, record)
            self.removed_sources.extend(await self.entries.source_rows(record_ids=[record.id]))
        self.after.pop(record.id, None)
        self.created.discard(record.id)
        await self.memory.delete(self.tenant, record.id)
        await self.entries.drop_sources(record_ids=[record.id])
        if entry_of(record):
            await self.entries.touched(entry_of(record))
        self.stats.retired += 1

    async def _content(self, item: dict[str, Any], everything: str, operator_text: dict[int, str], evidence: tuple[int, int]) -> None:
        text = clip(" ".join(str(item.get("text") or "").split()), BULLET_CHARS)
        quote = " ".join(str(item.get("quote") or "").split())
        if not text:
            self.stats.dropped["empty"] += 1
            return
        label = item.get("subjects") if isinstance(item.get("subjects"), list) else []
        if reason := self._screen(text, quote, label=label):
            self.stats.dropped[reason] += 1
            return
        if await self.entries.tombstoned(text):
            self.stats.dropped["tombstoned"] += 1
            return
        if item["op"] == "update":
            await self._update(item, text, quote, everything, operator_text, evidence)
            return
        key = self._key(item.get("entry"))
        if key in self.tainted or key in self.tainted_now:
            self.stats.dropped["tainted"] += 1
            return
        section, _, slug = key.partition("/")
        if section == "you" and key not in (f"you/{PREFERENCES.slug}", f"you/{PROFILE.slug}"):
            self.stats.dropped["unknown_entry"] += 1
            return
        if section not in ("you", "topic", "area") or not slug:
            self.stats.dropped["unknown_entry"] += 1
            return
        if key == f"you/{PREFERENCES.slug}":
            if not quote:
                self.stats.dropped["no_quote"] += 1
                return
            if not quote_backed(quote, item.get("seqs") or [], operator_text):
                self.stats.dropped["bad_quote"] += 1
                return
        elif missing := ungrounded_tokens(text, everything):
            await self._hold(key, text, missing, evidence)
            return
        entry = self.by_key.get(key) or await self._make(section, slug, topic_title(slug))
        if entry is None:
            return
        await self._add(entry, text, quote if key == f"you/{PREFERENCES.slug}" else "", evidence)

    async def _update(self, item: dict[str, Any], text: str, quote: str, everything: str, operator_text: dict[int, str], evidence: tuple[int, int]) -> None:
        record = self._record(item)
        if record is None:
            return
        entry = await self.entries.get(entry_of(record)) if entry_of(record) else None
        preference = entry is not None and entry.section == "you" and entry.slug == PREFERENCES.slug
        if preference and not (quote and quote_backed(quote, item.get("seqs") or [], operator_text)):
            # A preference is rewritten on the operator's words only, as it is first written.
            self.stats.dropped["no_quote" if not quote else "bad_quote"] += 1
            return
        if not preference and (missing := ungrounded_tokens(text, everything)):
            await self._suggest_revision(record, text, action="update", why="ungrounded", reason=f"names {', '.join(missing[:5])}, which the operator never wrote", evidence=evidence, tokens=missing)
            return
        if is_locked(record):
            await self._suggest_revision(record, text, action="update", why="locked", reason="the operator wrote or edited this bullet", evidence=evidence)
            return
        if " ".join(text.lower().split()) == " ".join(record.text.lower().split()):
            return
        if record.id not in self.created:
            self.before.setdefault(record.id, record)
        changed = await self.memory.update(self.tenant, record.id, text=text)
        self.after[changed.id] = changed
        await self._source(changed.id, evidence, quote if preference else "")
        if entry is not None:
            await self.entries.touched(entry.id)
        self.stats.updated += 1

    async def _add(self, entry: Entry, text: str, quote: str, evidence: tuple[int, int]) -> None:
        scope, scope_key = self.entries.placement(entry)
        same = self._same_fact(text, scope, scope_key)
        if same is not None:
            # The fact is already kept (often from another chat): this chat becomes one more of its
            # sources, so deleting either chat alone keeps it, and no second bullet says it again.
            await self._source(same.id, evidence, quote)
            self.stats.merged += 1
            return
        live = [r for r in self.entries.records_of(entry.id) if not is_archived(r)]
        if len(live) >= (CAP_AREA if entry.section == "area" else CAP_YOU_TOPIC):
            self.stats.dropped["capped"] += 1
            return
        kind = "preference" if entry.slug == PREFERENCES.slug and entry.section == "you" else \
            "person" if entry.section == "you" or entry.slug == "people" else "project" if entry.section == "area" else guess_category(text)
        topics = infer_topics(text, self.memory.vocabulary(self.tenant), limit=3)
        was = {r.id: r for r in self.memory.records(self.tenant, scope=scope, scope_key=scope_key)}
        result = await self.entries.remember(
            scope, scope_key, text, kind=kind, metadata={"source": "synthesis", "topics": topics, "stated": True},
            origin="synthesis", session_id=self.chat.id, entry=entry, from_seq=evidence[0], to_seq=evidence[1], quote=quote,
            # The pass compared the words itself (above); the store's own merge would otherwise fold the
            # new text into a similar note, a locked one included, and change words the operator wrote.
            similarity_threshold=1.0,
        )
        record = result.record
        if record.id in was:
            # The store found the same words after all: the note memory had is kept, with this chat as
            # one more source (which the undo takes away again).
            if record.id not in self.created:
                if record.version != was[record.id].version:
                    self.before.setdefault(record.id, was[record.id])
                    self.after[record.id] = record
                self.added_sources.append({"record_id": record.id, "origin": "synthesis", "session_id": self.chat.id, "from_seq": evidence[0]})
            self.stats.merged += 1
            return
        self.created.add(record.id)
        self.after[record.id] = record
        self.stats.added += 1

    def _same_fact(self, text: str, scope: Any, scope_key: str) -> MemoryRecord | None:
        """The bullet in the same scope that says this already: the same words (case and punctuation
        aside), or nearly every word in common — the curator's sure duplicate."""
        print_ = fingerprint(text)
        mine = words_of(text)
        best: tuple[float, MemoryRecord] | None = None
        for record in self.memory.records(self.tenant, scope=scope, scope_key=scope_key):
            if fingerprint(record.text) == print_:
                return record
            theirs = words_of(record.text)
            if not mine or not theirs:
                continue
            score = len(mine & theirs) / len(mine | theirs)
            if score >= DUPLICATE_SURE and (best is None or score > best[0]):
                best = (score, record)
        return best[1] if best else None

    async def _source(self, record_id: str, evidence: tuple[int, int], quote: str) -> None:
        row = {"record_id": record_id, "origin": "synthesis", "session_id": self.chat.id, "from_seq": evidence[0]}
        exists = await self.curator.db.fetchone(
            "SELECT 1 FROM memory_sources WHERE tenant_id = ? AND record_id = ? AND origin = 'synthesis' AND session_id = ? AND from_seq = ?",
            (self.tenant, record_id, self.chat.id, evidence[0]),
        )
        if exists is not None:
            return
        await self.entries.add_source(record_id, "synthesis", session_id=self.chat.id, from_seq=evidence[0], to_seq=evidence[1], quote=quote)
        if record_id not in self.created:
            self.added_sources.append(row)

    async def _hold(self, key: str, text: str, missing: Sequence[str], evidence: tuple[int, int]) -> None:
        section, _, slug = key.partition("/")
        entry = self.by_key.get(key)
        detail = {
            "why": "ungrounded", "tokens": list(missing[:8]), "entry_id": entry.id if entry else "", "entry_key": key,
            "entry_title": entry.title if entry else topic_title(slug), "section": section, "slug": slug,
            "project_id": self.chat.project_id if section == "area" and slug == self.project_slug else "",
            "session_id": self.chat.id, "seqs": list(evidence), "action": "add",
        }
        if await self.curator.suggest("addition", [], text, reason=f"names {', '.join(missing[:5])}, which the operator never wrote", detail=detail):
            self.stats.held += 1
        else:
            self.stats.dropped["held_before"] += 1

    async def _suggest_revision(self, record: MemoryRecord, text: str, *, action: str, why: str, reason: str, evidence: tuple[int, int], tokens: Sequence[str] = ()) -> None:
        detail = {"why": why, "action": action, "tokens": list(tokens[:8]), "entry_id": entry_of(record), "session_id": self.chat.id, "seqs": list(evidence)}
        if await self.curator.suggest("revision", [record], text or record.text, reason=reason, detail=detail):
            self.stats.held += 1
        else:
            self.stats.dropped["held_before"] += 1

    async def finish(self, summary: str) -> int | None:
        """One history entry for everything this pass changed, whose undo puts it all back; entries
        it made and left empty are removed first. ``None`` when nothing changed."""
        for entry_id in list(self.made):
            if not self.entries.records_of(entry_id):
                await self.entries.delete(entry_id)
                self.made.remove(entry_id)
                self.stats.new_entries -= 1
        if not (self.before or self.after or self.added_sources or self.made):
            return None
        return await self.curator.journal(
            "synthesis", "learn", summary, list(self.before.values()), list(self.after.values()),
            {"made": self.made, "sources": self.removed_sources, "added_sources": self.added_sources},
        )


# -- the scheduler ---------------------------------------------------------------------------------

REPORT_KEY = "memory_synthesis_report"
NIGHTLY_KEY = "memory_synthesis_nightly"


class Synthesizer:
    """When chats are read, which ones, the model calls and the budget, and what the settings show.

    ``running(session_id)`` says whether a run of that chat is in progress (it is not read then);
    ``costs_nothing(provider_id, model)`` whether a call there is free, so the budget does not stop
    it; ``zone()`` the operator's time zone for the nightly sweep."""

    def __init__(
        self,
        db: Database,
        curator: MemoryCurator,
        config: Callable[[], RuntimeConfig],
        providers: Any,
        *,
        running: Callable[[str], bool] = lambda _: False,
        costs_nothing: Callable[[str | None, str | None], bool] = lambda *_: False,
        zone: Callable[[], tzinfo] = lambda: UTC,
        redact: Callable[[str], str] = redact_text,
        notify: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.curator = curator
        self.config = config
        self.providers = providers
        self.running = running
        self.costs_nothing = costs_nothing
        self.zone = zone
        self.redact = redact
        self.notify = notify
        self.lock = asyncio.Lock()
        """One sweep at a time: two passes over the same chat would both read past its watermark."""
        self.task: asyncio.Task[Any] | None = None
        self._was_paused = False

    # -- which chats ------------------------------------------------------------------------------

    async def candidates(self, *, quiet_since: datetime | None = None, active_after: datetime | None = None) -> list[Chat]:
        """The operator's chats with messages the pass has not read; with ``quiet_since``, only those
        quiet since then and active after ``active_after`` that the last pass has not seen since."""
        extra = " AND ".join(f"coalesce(json_extract(s.metadata, '$.{key}'), '') IN ('', 0)" for key in NOT_THE_OPERATORS)
        clauses = [SEARCHABLE_SQL, f"(CASE WHEN json_valid(s.metadata) THEN {extra} ELSE 1 END)",
                   "(SELECT max(t.seq) FROM transcript t WHERE t.session_id = s.id) > coalesce(m.through_seq, 0)"]
        params: list[Any] = []
        if quiet_since is not None:
            clauses.append("s.last_message_at <= ?")
            params.append(quiet_since.isoformat())
            clauses.append("(m.last_at IS NULL OR s.last_message_at > m.last_at)")
            if active_after is not None:
                clauses.append("s.last_message_at >= ?")
                params.append(active_after.isoformat())
        rows = await self.db.fetchall(
            f"SELECT s.id, s.title, s.project_id, p.name AS project_name, {PLAIN_PROJECT_SQL} AS plain, coalesce(m.through_seq, 0) AS through_seq, m.last_at,"
            " CASE WHEN json_valid(s.metadata) THEN json_extract(s.metadata, '$.imported.harness') END AS imported"
            " FROM sessions s LEFT JOIN projects p ON p.id = s.project_id LEFT JOIN memory_synthesis m ON m.session_id = s.id"
            f" WHERE {' AND '.join(clauses)} ORDER BY s.last_message_at",
            params,
        )
        return [
            Chat(
                str(r["id"]), str(r["title"] or ""), int(r["through_seq"] or 0), r["last_at"],
                project_id="" if r["plain"] or r["project_name"] is None else str(r["project_id"]),
                project_name="" if r["plain"] or r["project_name"] is None else str(r["project_name"]),
                imported=str(r["imported"] or ""),
            )
            for r in rows
        ]

    async def unread(self, chat: Chat) -> tuple[int, int]:
        """How many operator messages past the watermark, and how many characters."""
        row = await self.db.fetchone(
            "SELECT count(*) AS n, coalesce(sum(length(message)), 0) AS chars FROM transcript WHERE session_id = ? AND seq > ?"
            " AND json_extract(message, '$.role') = 'user' AND json_extract(message, '$.metadata.\"daedalus.origin\"') = 'operator'",
            (chat.id, chat.through_seq),
        )
        return (int(row["n"]), int(row["chars"])) if row is not None else (0, 0)

    async def turns(self, chat: Chat) -> tuple[list[Turn], int]:
        """The chat past its watermark as the pass reads it, and the last transcript seq it covers.

        Only the operator's own messages (``daedalus.origin == operator``: not a schedule's, a loop's,
        a report's or a compaction summary's) and the agent's text; tool calls and results never."""
        rows = await self.db.fetchall(
            "SELECT seq, message FROM transcript WHERE session_id = ? AND seq > ? AND json_extract(message, '$.role') IN ('user', 'assistant') ORDER BY seq",
            (chat.id, chat.through_seq),
        )
        last = await self.db.fetchone("SELECT max(seq) AS last FROM transcript WHERE session_id = ?", (chat.id,))
        out: list[Turn] = []
        for row in rows:
            try:
                message = json.loads(row["message"])
            except ValueError:
                continue
            text = "".join(str(b.get("text") or "") for b in message.get("content_blocks") or [] if isinstance(b, dict) and b.get("kind") == "text")
            if message.get("role") == "user":
                if (message.get("metadata") or {}).get("daedalus.origin") != "operator":
                    continue
                text = without_host_notes(text).strip()
                role = "operator"
            else:
                text = split_headline(text)[0].strip()
                role = "assistant"
            if not text:
                continue
            text = self.redact(text)
            if role == "operator" and out and out[-1].role == "operator" and out[-1].text == text:
                continue  # a steer is recorded when it is queued and may be again when the run takes it
            out.append(Turn(int(row["seq"]), role, text))
        return out, int(last["last"] or chat.through_seq) if last is not None else chat.through_seq

    async def headlines(self, chat: Chat) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT headline FROM learning_records WHERE session_id = ? AND coalesce(headline, '') != '' AND at > ? ORDER BY id DESC LIMIT ?",
            (chat.id, chat.last_at or "", HEADLINES),
        )
        return [str(r["headline"]) for r in reversed(rows)]

    # -- the model --------------------------------------------------------------------------------

    async def spent_today(self) -> float:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = await self.db.fetchone("SELECT coalesce(sum(cost_usd), 0) AS spent FROM usage_events WHERE purpose LIKE 'memory%' AND at >= ?", (start,))
        return float(row["spent"]) if row is not None else 0.0

    async def ask(self, choice: ModelChoice, messages: list[Message], run_id: str) -> Reply:
        """One call on the chosen model, then on its fallbacks when it fails. Before each, the budget:
        a call that would be paid for waits for tomorrow once today's memory spend reached it."""
        config = self.config()
        failure = "no model is configured"
        for preset in (choice.preset, *choice.fallbacks):
            if not preset:
                continue
            try:
                provider, model = self.providers.rungs_for(config, preset)[0]
            except (KeyError, RuntimeError) as exc:
                failure = f"{preset}: {exc}"
                continue
            endpoint = getattr(provider, "endpoint", None)
            if not self.costs_nothing(getattr(endpoint, "id", None), model) and await self.spent_today() >= config.memory.synthesis_usd_per_day:
                raise BudgetReached
            request = LLMRequest(
                model=model, messages=messages, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                extra={"enable_thinking": False, "json_object": True},
                observability=LLMObservabilityContext(tenant_id=TENANT, run_id=run_id, call_purpose=PURPOSE, call_category="memory"),
            )
            try:
                response = await asyncio.wait_for(provider.complete_text(request), timeout=CALL_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 — a failed model is tried on the next; the pass reports the last failure
                failure = f"{preset}: {type(exc).__name__}: {str(exc)[:200]}"
                logger.warning("memory pass: %s", failure)
                continue
            text = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock))
            usage = response.usage
            return Reply(text, float(getattr(usage, "response_cost_usd", None) or 0.0), preset, str(getattr(response.stop_reason, "value", response.stop_reason)) == "max_tokens")
        raise CallFailed(failure)

    async def answer(self, choice: ModelChoice, user: str, run_id: str) -> tuple[ParseResult, Reply, int]:
        """The model's operations for one part, with one corrective retry on an invalid answer: the
        start of the bad answer goes back with the error named (replaying a cut-off answer whole only
        teaches the model to repeat the cut). Returns the parse, the last reply and the calls made."""
        system = Message(role=MessageRole.system, content_blocks=[TextBlock(text=SYSTEM_PROMPT)])
        asked = Message(role=MessageRole.user, content_blocks=[TextBlock(text=user)])
        reply = await self.ask(choice, [system, asked], run_id)
        parsed = parse_ops(reply.text)
        if parsed.ok:
            return parsed, reply, 1
        cost = reply.cost
        retry = await self.ask(choice, [
            system, asked,
            Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=reply.text[:400] or "(empty)")]),
            Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"That answer was not a valid operations object ({parsed.error}). Return the complete JSON object again, shorter if needed, and nothing else.")]),
        ], run_id)
        retry.cost += cost
        return parse_ops(retry.text), retry, 2

    # -- reading ----------------------------------------------------------------------------------

    async def read(self, chat: Chat, choice: ModelChoice, report: dict[str, Any], stats: Stats) -> None:
        """One pass over one chat: each part asked, checked and applied in turn, the watermark moved
        after each applied part, and one history entry for the lot. A failure stops the chat where it
        is; what was applied before it stays applied and journalled."""
        turns, last_seq = await self.turns(chat)
        await self._mark(chat.id, status="running")
        run_id = f"memory-{chat.id}-{uuid.uuid4().hex[:8]}"
        applier = Applier(self.curator, self.config(), chat)
        async with self.curator.lock:
            await applier.load()
        headlines = await self.headlines(chat)
        parts = slices(condense(turns))
        through = chat.through_seq
        cost = 0.0
        error = ""
        try:
            if not any(t.role == "operator" for t in turns):
                through = last_seq
            for index, part in enumerate(parts):
                if not any(t.role == "operator" for t in part):
                    through = part[-1].seq if index < len(parts) - 1 else last_seq
                    continue
                operator = "\n".join(t.text for t in part if t.role == "operator")
                user, operator_text = build_input(part, headlines, applier.listing(), applier.focus(operator), notes=applier.notes(), imported=chat.imported)
                parsed, reply, calls = await self.answer(choice, user, run_id)
                cost += reply.cost
                report["calls"] += calls
                report["parts"] += 1
                report["model"] = reply.preset
                if not parsed.ok:
                    error = f"the model's answer was not valid operations twice ({parsed.error})" + (" — cut off at the output limit" if reply.truncated else "")
                    break
                async with self.curator.lock:
                    await applier.apply(parsed.ops, operator_text)
                through = last_seq if index == len(parts) - 1 else part[-1].seq
                await self._mark(chat.id, through_seq=through, status="running")
        except BudgetReached:
            error = "budget"
            raise
        except CallFailed as exc:
            error = str(exc)
        finally:
            async with self.curator.lock:
                history = await applier.finish(self._summary(chat, applier.stats))
            stats.add(applier.stats)
            report["cost_usd"] += cost
            if history is not None:
                report["history_ids"].append(history)
            done = not error
            await self._mark(chat.id, through_seq=through, status="idle" if done else ("budget" if error == "budget" else "error"),
                             error="" if done or error == "budget" else error[:500], cost=cost, last_at=datetime.now(UTC).isoformat())
            if error and error != "budget":
                report["errors"].append({"session_id": chat.id, "title": chat.title[:80], "error": error[:300]})
            if done or applier.stats.changed():
                report["chats"] += 1

    @staticmethod
    def _summary(chat: Chat, stats: Stats) -> str:
        counts = ", ".join(f"{n} {word}" for n, word in ((stats.added, "added"), (stats.updated, "updated"), (stats.merged, "confirmed"), (stats.retired, "retired")) if n)
        return f"Learned from “{chat.title or chat.id}”" + (f": {counts}" if counts else "")

    async def _mark(self, session_id: str, *, through_seq: int | None = None, status: str, error: str = "", cost: float = 0.0, last_at: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO memory_synthesis(session_id, through_seq, last_at, status, error, cost_usd) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET through_seq = max(through_seq, excluded.through_seq),"
            " last_at = coalesce(excluded.last_at, last_at), status = excluded.status, error = excluded.error, cost_usd = cost_usd + excluded.cost_usd",
            (session_id, through_seq or 0, last_at, status, error, cost),
        )

    async def mark_read(self) -> int:
        """While memory is paused, every chat's watermark follows its transcript: what is said during
        a pause is never learned afterwards (claude.ai's rule, and the plan's). Returns how many moved."""
        extra = " AND ".join(f"coalesce(json_extract(s.metadata, '$.{key}'), '') IN ('', 0)" for key in NOT_THE_OPERATORS)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO memory_synthesis(session_id, through_seq, status) SELECT s.id, (SELECT max(t.seq) FROM transcript t WHERE t.session_id = s.id), 'paused'"
                f" FROM sessions s LEFT JOIN memory_synthesis m ON m.session_id = s.id WHERE {SEARCHABLE_SQL} AND (CASE WHEN json_valid(s.metadata) THEN {extra} ELSE 1 END)"
                " AND (SELECT max(t.seq) FROM transcript t WHERE t.session_id = s.id) > coalesce(m.through_seq, 0)"
                " ON CONFLICT(session_id) DO UPDATE SET through_seq = max(through_seq, excluded.through_seq), status = 'paused'",
            )
            return int(cursor.rowcount or 0)

    # -- sweeps -----------------------------------------------------------------------------------

    def blocked(self) -> str:
        """Why no pass runs now, in one word, or empty: paused, off (not learning from chats), or no
        budget at all."""
        memory = self.config().memory
        if memory.paused:
            return "paused"
        if not memory.generate:
            return "off"
        if memory.synthesis_usd_per_day <= 0:
            return "no_budget"
        return ""

    async def sweep(self, chats: Sequence[Chat], trigger: str, *, min_new: int = 1) -> dict[str, Any]:
        """Read ``chats`` in turn (skipping one whose run is in progress or with too little new), and
        keep the report the settings show."""
        started = datetime.now(UTC)
        choice = choose_model(self.config())
        report: dict[str, Any] = {
            "at": started.isoformat(), "trigger": trigger, "model": choice.preset, "note": choice.note, "chats": 0, "parts": 0, "calls": 0,
            "cost_usd": 0.0, "errors": [], "stopped": "", "history_ids": [],
        }
        stats = Stats()
        if not choice.preset:
            report["stopped"] = "no_model"
        else:
            for chat in chats:
                if self.blocked():
                    report["stopped"] = self.blocked()
                    break
                if self.running(chat.id):
                    continue
                n, chars = await self.unread(chat)
                if n < min_new and chars < LONG_SLICE_CHARS:
                    continue
                try:
                    await self.read(chat, choice, report, stats)
                except BudgetReached:
                    report["stopped"] = "budget"
                    break
        report.update(stats.view())
        report["cost_usd"] = round(report["cost_usd"], 6)
        report["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 1)
        if report["chats"] or report["errors"] or report["stopped"] or trigger == "manual":
            await self.db.kv_set(REPORT_KEY, json.dumps(report, ensure_ascii=False))
            if report["chats"] or report["errors"]:
                logger.info("memory pass (%s): %d chat(s), +%d ~%d =%d held %d, $%.4f, %s", trigger, report["chats"], stats.added, stats.updated, stats.merged, stats.held, report["cost_usd"], report["stopped"] or "done")
        if self.notify is not None and stats.held:
            await self.notify(report)
        return report

    async def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """One look: while paused, mark what was said read; otherwise the nightly sweep when it is due,
        else the chats that went quiet."""
        now = now or datetime.now(UTC)
        reason = self.blocked()
        if reason == "paused":
            self._was_paused = True
            await self.mark_read()
            return None
        if self._was_paused:
            # The last minute of the pause, said between the previous look and the resume.
            self._was_paused = False
            await self.mark_read()
            return None
        if reason:
            return None
        async with self.lock:
            if await self._nightly_due(now):
                report = await self.sweep(await self.candidates(), "nightly")
                await self.db.kv_set(NIGHTLY_KEY, now.astimezone(self.zone()).date().isoformat())
                return report
            quiet = await self.candidates(quiet_since=now - timedelta(seconds=IDLE_SECONDS), active_after=now - timedelta(seconds=RECENT_SECONDS))
            if not quiet:
                return None
            return await self.sweep(quiet, "idle", min_new=MIN_NEW_OPERATOR_MESSAGES)

    async def _nightly_due(self, now: datetime) -> bool:
        local = now.astimezone(self.zone())
        last = await self.db.kv_get(NIGHTLY_KEY, "")
        if not last:
            # The first start does not sweep the whole backlog by itself: the first nightly sweep is
            # the next night, and "update now" in the settings reads everything at once on request.
            await self.db.kv_set(NIGHTLY_KEY, local.date().isoformat())
            return False
        return last != local.date().isoformat() and (local.hour, local.minute) >= NIGHTLY_AT

    async def run_now(self) -> bool:
        """The operator's "update now": every chat with something unread, in the background. ``False``
        when a sweep is already running (it is reading them already)."""
        if self.lock.locked() or (self.task is not None and not self.task.done()):
            return False

        async def go() -> None:
            async with self.lock:
                await self.sweep(await self.candidates(), "manual")

        self.task = asyncio.create_task(go(), name="memory-synthesis-now")
        return True

    async def loop(self) -> None:
        await self.db.execute("UPDATE memory_synthesis SET status = 'idle' WHERE status = 'running'")
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — a failed look costs a log line; the next one tries again
                logger.exception("memory pass failed")
            await asyncio.sleep(TICK_SECONDS)

    # -- what the settings show -------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        choice = choose_model(self.config())
        raw = await self.db.kv_get(REPORT_KEY, "")
        try:
            report = json.loads(raw) if raw else None
        except ValueError:
            report = None
        failing = await self.db.fetchone("SELECT count(*) AS n FROM memory_synthesis WHERE status = 'error'")
        return {
            "model": choice.preset, "model_note": choice.note, "model_reason": choice.reason, "model_missing": choice.missing, "blocked": self.blocked(),
            "running": self.lock.locked(), "report": report, "failing_chats": int(failing["n"]) if failing is not None else 0,
        }


__all__ = [
    "PURPOSE",
    "RECOMMENDED_PRESETS",
    "SYSTEM_PROMPT",
    "Applier",
    "BudgetReached",
    "CallFailed",
    "Chat",
    "ModelChoice",
    "ParseResult",
    "Stats",
    "Synthesizer",
    "Turn",
    "build_input",
    "choose_model",
    "clip",
    "condense",
    "ident_tokens",
    "never_store",
    "parse_ops",
    "quote_backed",
    "slices",
    "ungrounded_tokens",
]
