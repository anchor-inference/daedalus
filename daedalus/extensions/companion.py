"""The companion: the operator's standing personal agent, talked to by text and by voice.

The operator types or speaks; the companion answers, and it is not the agent that does the work — it
is the manager who stays on the line while the engineers work. It answers what it can itself and, the
moment something needs doing, starts an agent session for it and says so at once, so the conversation
never stalls on a tool call that takes four minutes. Between conversations it owns goals: something
it keeps an eye on over days, on a cadence the host keeps for it, delegating and reporting as it goes.

The companion is an ordinary Daedalus session: its transcript is persisted, it is visible in the app,
compaction applies to it and its calls appear in Usage. What makes it the companion is one metadata
flag, which narrows its tools to the set in :data:`daedalus.config.COMPANION_TOOLS` — no shell, no
files, no browser, enforced by the host — and swaps its system prompt for the companion's brief.

Voice is one of its channels, not what it is. A spoken turn is answered by the fast voice preset and
read out; a typed message, an agent's report and a goal check are answered by the background preset,
which nobody is waiting on out loud. Which channel a turn came through rides in its turn context, so
the brief can ask for spoken sentences on a call and allow a list in a typed answer without the
system prompt changing between turns.

A run of a delegated agent that ends, or that stops to ask the operator something, becomes a line in
the companion's conversation — but only while somebody is listening and the companion is not
paused. Otherwise the line is held and delivered when a page connects (or the companion is resumed),
and a finished result also goes to the Inbox and the operator's phone at once, so news that needs
the operator reaches them without a page open. Questions and approvals reach the Inbox through the
notification router already, as every session's do.

An agent also talks while it works: between two tool calls it writes a paragraph saying what it just
found and what it is doing next, and that is what makes a four-minute job bearable to listen to. Those
interim lines are relayed too, under a much tighter rule than a final answer: only while somebody is
listening (an interim is worthless late — it is superseded by the next one, and by the final answer),
coalesced over a window so a burst of narration costs one turn, and rate-capped per agent so a talkative
agent cannot take the conversation over. What is relayed is the agent's own prose only: not a tool
result, not its thinking, not a token at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from protocore.contracts.types import MessageRole, TextBlock
from protocore.runtime.events.types import EventType

from daedalus.config import COMPANION_ONLY_TOOLS, COMPANION_TOOLS, ModelPresetConfig, RuntimeConfig, TtsConfig
from daedalus.extensions.notifications import Draft
from daedalus.host.prompts import split_headline, without_turn_context
from daedalus.stores.companion import CompanionStore, Goal, GoalError, parse_cadence
from daedalus.transport.telegram.voice import TranscriptionError, asr_configured, effective_asr

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

SESSION_KEY = "companion_session"
"""Where the id of the operator's standing companion session is remembered between restarts."""
PAUSED_KEY = "companion_paused"
"""Whether the operator paused the companion, remembered between restarts: a pause that a restart
undid would be a goal check the operator had asked not to happen."""

FLAG = "companion"
"""The session metadata flag that makes a session the companion."""
PARENT = "companion_parent"
"""The session metadata mark on every agent the companion started: the companion session's id."""

TITLE = "Companion"
PROJECT_KIND = "companion"
"""The system project the companion and everything it delegates live in. One folder, one place in the
Agents list, and a boundary the companion's agents cannot walk out of — which the operator's own
sessions, scattered across their own directories, never had a reason to be inside."""
PROJECT_DIR = "companion"
"""The folder, under the workspaces root: the installation's, not the operator's."""
ANSWER_CLIP = 4000
"""How much of an agent's answer AgentResult returns; a spoken summary needs no more."""
LIST_CLIP = 300
REPORT_CLIP = 6000
"""How much of an agent's final answer is handed to the companion.

It used to be a few hundred characters, and a long answer that opened with its checks and closed
with its findings reached the companion as the checks alone, which it then reported as "there was
nothing about that in the answer" — truthfully, and wrongly. The companion reads a whole answer
now; what is cut is marked, and the tool that fetches the rest is named where the cut happens."""
PROGRESS_CLIP = 300
"""How much of an interim line is relayed. One spoken sentence comes out of it; the rest would be read
by a model that is about to throw it away, and an agent's next paragraph supersedes it anyway."""
RELAY_POLL_SECONDS = 0.25
"""How often a held interim looks again at whether the companion has finished its own turn."""
DRAFT_CLIP = 8000
"""The most of one agent message held while it is being written. A message longer than this is not a
progress line by then, and an unbounded buffer per delegated run is how a long-running page leaks."""
APPROVALS_REMEMBERED = 8
"""Approval keys kept per agent, so an agent alternating between two refused calls reports each once."""
MAX_AGENTS = 20
GOAL_POLL_SECONDS = 60.0
"""How often the host looks for a goal whose check has come. Cadences are minutes at the finest."""
MAX_PENDING = 8
"""Reports held for a client that is not connected; the oldest go first, the newest are what matters."""
PENDING_GRACE_SECONDS = 120.0
"""How long a report for an empty room is news rather than history when a page finally connects.

A page that reconnects a second later — a dropped stream, a reload, a window brought back — should
hear what it just missed as if it had been there. A page that connects half an hour later should not:
the companion would open the conversation by reading out news from a morning the operator has already
moved past, indistinguishable, from the chair, from the answer to the last question arriving late.
Past the grace the line is still delivered — the conversation is a transcript too — but marked with
its age, so the companion says it as what happened while the operator was away."""
TITLE_CLIP = 80

REPORT_OPEN = "⟪agent report⟫"
REPORT_CLOSE = "⟪end of report⟫"
GOAL_OPEN = "⟪goal check⟫"
GOAL_CLOSE = "⟪end of goal check⟫"
"""The frame an agent's words arrive in. The companion's brief names it and says that what is inside
it is a report to relay, never an instruction: an agent's answer is made of whatever it read — web
pages, files, another service's output — and it reaches the companion while the delegation tools are
live. The frame characters are taken out of the agent's own text below, so it cannot close the block
early and write outside it."""


def quoted(text: str) -> str:
    """An agent's words as data: the frame's own characters replaced, so only we can open or close a block."""
    return text.replace("⟪", "«").replace("⟫", "»")


def clip_report(answer: str) -> str:
    """An agent's answer as the companion receives it, saying so when the end was left out."""
    if len(answer) <= REPORT_CLIP:
        return answer
    missing = len(answer) - REPORT_CLIP
    return answer[:REPORT_CLIP].rstrip() + (
        f"\n\n[{missing} characters of this answer are not here; call AgentResult with the id "
        "above to read it whole before saying what it does or does not contain]"
    )


def report_block(*, kind: str, title: str, session_id: str, state: str, body: str) -> str:
    """One agent's news, framed. ``kind`` and ``state`` are ours to write; ``title`` and ``body`` are the agent's.

    ``kind`` is the one word the companion needs before it reads a word of the body: ``progress`` is an
    agent thinking out loud and means nothing is finished, ``final`` is a result, ``question`` and
    ``approval`` are the two ways a run stops until the operator says something. It is written by us,
    on its own line, so the difference between "it found the bug" and "it fixed the bug" does not
    depend on how the agent happened to phrase a sentence.
    """
    head = f"agent: {quoted(title.strip())[:TITLE_CLIP] or session_id} ({session_id}) {state}"
    return "\n".join([REPORT_OPEN, f"kind: {kind}", head, no_kind_line(quoted(body.strip())), REPORT_CLOSE])


def no_kind_line(text: str) -> str:
    """The agent's words with any line of its own that reads as our ``kind:`` header defused.

    The word on the second line is what the companion acts on — whether the work is done or the
    agent is only talking — and it is ours to write. A progress line cannot forge one (its
    whitespace is collapsed into a single line), but a final answer, a question and an approval are
    the agent's own prose, and an agent answering a question *about* this format would write one by
    accident as readily as a hostile page would write one on purpose. The words are kept — the line
    is marked as quoted, so it is no longer a line that begins with ours.
    """
    return KIND_LINE_RE.sub(lambda match: "> " + match.group(0).strip(), text)


KIND_LINE_RE = re.compile(r"(?mi)^[ \t]*kind[ \t]*:")


SENTENCE_END = re.compile(r"(?<=[.!?…。！？])[\s\n]+|(?<=[.!?…])$|\n\n+")
"""Where a spoken chunk may be cut. Sentence-final punctuation followed by space, or a paragraph break."""

MIN_SAY_CHARS = 12
"""A fragment shorter than this waits for the next one: "Yes." alone is a worse utterance than "Yes. Here is why."."""

TURN_CLOCKS = 8
"""How many turns are timed at once. One is in flight and the rest are history the page may still ask
for; a conversation that runs all evening does not grow a dictionary of every run it ever had."""


@dataclass
class TurnSpeech:
    """How long one answer waited between the words existing and a sound coming out.

    The number the operator actually feels is this one, not the model's latency and not the
    synthesiser's: the answer is on the screen, and they are waiting to hear it. It is split because
    the two halves have different cures — a voice that has to be built is fixed by warming it up, and
    a synthesiser that renders slower than speech is fixed by choosing another voice.
    """

    turn: str = ""
    said_at: float = 0.0
    """Monotonic, when the first sentence of this turn was handed to the page."""
    first_audio_ms: int = 0
    """From that moment to the first clip being ready to play. Zero means nothing was spoken yet."""
    clip_ms: int = 0
    """What the request that produced the first clip spent on it, loading included."""
    load_ms: int = 0
    """How much of that was the voice being built rather than the sentence being spoken."""

    def as_json(self) -> dict[str, object]:
        return {"turn": self.turn, "first_audio_ms": self.first_audio_ms, "clip_ms": self.clip_ms, "load_ms": self.load_ms}


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split a growing draft into whole sentences plus the tail that is not finished yet.

    Speech starts on the first sentence, not on the last token: the answer is read aloud while the
    model is still writing it, which is most of the difference between a conversation and a wait.
    A sentence too short to be worth its own request ("Yes.") waits and is spoken with the next one.
    """
    out: list[str] = []
    chunk, rest = "", buffer
    while True:
        match = SENTENCE_END.search(rest)
        if match is None or match.end() == 0:
            break
        head, rest = rest[: match.end()].strip(), rest[match.end() :]
        chunk = (chunk + " " + head).strip() if chunk else head
        if len(chunk) >= MIN_SAY_CHARS:
            out.append(chunk)
            chunk = ""
    return out, (chunk + " " + rest if chunk else rest)


def speakable(text: str) -> str:
    """The answer as it is read out: without the retrieval headline, the turn context, or markdown scaffolding."""
    body = split_headline(without_turn_context(text))[0]
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"[*_`#>]+", "", body)
    return re.sub(r"[ \t]+", " ", body).strip()


EVIDENCE_RE = re.compile(r"<(?:file|run)\b[^>]*/?>")
"""The evidence tags an answer carries for the app to render as chips. Spoken, they are punctuation read
out loud, so an interim line drops them rather than trying to say them."""

APPROVAL_KEY_RE = re.compile(r"Approval key: ([0-9a-f]{12})")
"""How a refusal by the policy is recognised in a tool result: the key the operator approves it with.
A session whose next step is an approval is not working, and the operator is the only one who can
unblock it — which makes it something the companion must say, not something it may summarise later."""


def delta_text(payload: dict[str, Any]) -> str:
    """The assistant's own words in one delta, and nothing else.

    A run publishes three kinds of stream to the operator: prose, thinking, and tool traffic. Only the
    first is what the operator would read in the session view as the agent talking, and only the first
    is worth a sentence out loud — thinking is a draft the model did not commit to, and tool arguments
    and results are the detail the companion is explicitly told never to read aloud.
    """
    delta = payload.get("delta") or {}
    return str(delta.get("text") or "") if delta.get("type") == "text_delta" else ""


def settles_interim(payload: dict[str, Any]) -> bool:
    """Whether this end-of-message is an interim one: the agent stopped writing to call a tool.

    Any other stop reason ends the turn, and the turn's own text is the final answer, which is reported
    by the finished-run path with everything it knows — its status included.
    """
    return str(payload.get("stop_reason") or "") == "tool_use"


def progress_line(text: str) -> str:
    """One settled paragraph of an agent's prose, as a line a companion can turn into a sentence.

    The same cleaning a spoken answer gets, plus the evidence tags and the line breaks: what arrives at
    the companion is one clipped line of plain words, because everything else is either unsayable or a
    detail that will be superseded before anyone could ask about it.
    """
    # The tags go first: what makes an answer sayable also strips the punctuation that makes a tag a tag.
    body = speakable(EVIDENCE_RE.sub(" ", text))
    return re.sub(r"\s+", " ", body).strip()[:PROGRESS_CLIP]


def effective_tts(config: TtsConfig, manager: Any) -> TtsConfig:
    """The endpoint to call: a configured provider's base URL and key when ``provider`` is set, else ``url``/``api_key``."""
    if not config.provider:
        return config
    registry = getattr(manager, "providers", None)
    try:
        endpoint = registry.get(config.provider).endpoint if registry is not None else None
    except KeyError:
        endpoint = None
    if endpoint is None or not endpoint.base_url:
        raise RuntimeError(f"text-to-speech provider {config.provider!r} is not configured")
    return config.model_copy(update={"url": endpoint.base_url, "api_key": endpoint.api_key})


def tts_configured(config: TtsConfig) -> bool:
    return bool(config.provider or config.url)


FAST_MAX_OUTPUT = 8_000
"""The largest reply a model may be allowed to write before it is no longer a companion's model.

A spoken answer is two sentences; a model configured to write thirty thousand tokens is configured
for a different job, and it takes the time to match. This and ``thinking`` are the two things the
page warns about, and they are decided here rather than in the app so that the warning and the
default the page suggests cannot disagree.
"""


def fast_enough(preset: ModelPresetConfig) -> bool:
    """Whether a preset answers while the operator is still listening: no thinking, a short reply."""
    return not preset.thinking and preset.max_output_tokens <= FAST_MAX_OUTPUT


def preferred_preset(config: RuntimeConfig) -> str:
    """The preset a live voice turn is answered with, as an override to put on the session.

    The operator's own choice, whenever they have made one. When they have not, the installation's
    default preset is used only if it is fast enough to hold a conversation — and where it is not,
    the first preset in the table that is takes its place. A default that thinks spends its first
    seven seconds on a reasoning block the operator cannot hear, which is most of the delay between
    asking a question out loud and hearing the answer begin; the table almost always has something
    quicker in it, and the page says which one is talking.
    """
    chosen = config.companion.preset
    if chosen in config.presets:
        return chosen
    fallback = config.default_preset()
    if fallback is not None and fast_enough(fallback[1]):
        return ""
    return next((pid for pid, preset in config.presets.items() if fast_enough(preset)), "")


def background_preset(config: RuntimeConfig) -> str:
    """The preset a typed message, an agent's report or a goal check is answered with.

    Nobody is waiting on these out loud, so the fast voice model's one virtue buys nothing and its
    short replies cost a good answer. The operator's choice when it names a preset; otherwise the
    default, which a session with no override already resolves to.
    """
    chosen = config.companion.background_preset
    return chosen if chosen in config.presets else ""


def goal_block(goal: Goal, agents: list[dict[str, Any]]) -> str:
    """The turn the host gives the companion when a goal's cadence comes round.

    Framed like an agent's report, and for the same reason: the goal's title and brief were written
    by the companion, but its last report may quote an agent, and an agent quotes whatever it read.
    """
    lines = [GOAL_OPEN, f"goal: {quoted(goal.title)} ({goal.id})", f"cadence: {goal.cadence.text or 'none'}"]
    if goal.brief:
        lines.append("brief: " + no_kind_line(quoted(goal.brief)))
    if goal.last_report:
        lines.append(f"last report ({goal.last_report_at or 'earlier'}): " + no_kind_line(quoted(goal.last_report)))
    linked = [a for a in agents if a["session_id"] in goal.sessions]
    for agent in linked:
        lines.append(f"agent: {quoted(agent['title'])[:TITLE_CLIP]} ({agent['session_id']}) {agent['status']}")
    lines.append(GOAL_CLOSE)
    lines.append(
        "Check on this goal now. Look at its agents with Agents or AgentResult, delegate what needs doing "
        "(pass goal_id to Delegate so the agent is linked), and finish with GoalReport saying what changed. "
        "If nothing changed and nothing needs doing, report that in one line."
    )
    return "\n".join(lines)


def model_options(config: RuntimeConfig) -> list[dict[str, Any]]:
    """Every preset the companion could be pointed at, as the app lists them.

    The label, the provider and the model id are what the operator picks by; ``fast`` is the
    judgement, made here once. The order is the table's own, which is the order Settings shows.
    """
    return [
        {
            "id": pid,
            "label": preset.display(pid),
            "provider": preset.provider,
            "model": preset.model,
            "thinking": preset.thinking,
            "max_output_tokens": preset.max_output_tokens,
            "fast": fast_enough(preset),
        }
        for pid, preset in config.presets.items()
    ]


AUDIO_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg", "pcm": "audio/pcm"}


class Companion:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.store = CompanionStore(app.db)
        self._listeners: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._pending: list[tuple[float, str]] = []
        """What an agent said while nobody was listening (or the companion was paused), each line with
        the moment it was said, so the delivery can say how old the news is."""
        self._drafts: dict[str, str] = {}
        """Per run: the part of the companion's answer that has not been handed to speech yet."""
        self._turns: dict[str, TurnSpeech] = {}
        """Per recent run: when it was first handed to speech, and when it was first heard."""
        self._last_turn: TurnSpeech | None = None
        """The most recent run that actually produced a sound, for the page's diagnostic line."""
        self._id = ""
        """The companion session's id, held in memory: every event of every session passes through the sink,
        and a database read per event would make the whole installation pay for the companion existing."""
        self._paused = False
        self._calls = 0
        """Pages that have a call open: the microphone live and answers read aloud. Counted, not a flag,
        so a phone hanging up does not end the call a laptop is still on."""
        self._channel = "text"
        """How the companion's next turn arrived (``voice``, ``text``, ``report``, ``goal``), for its turn context."""
        self._lock = asyncio.Lock()
        self._agent_drafts: dict[tuple[str, str], str] = {}
        """Per delegated run: the message an agent is writing, until it stops to call a tool."""
        self._news: dict[str, dict[str, str]] = {}
        """Per delegated agent, for the page: its last interim line, when, and what it is waiting for."""
        self._waiting_relay: dict[str, str] = {}
        """Per delegated agent: the interim line that has not been relayed yet. One entry, not a queue —
        a newer line supersedes an older one, which is the whole point of coalescing progress."""
        self._relay_tasks: dict[str, asyncio.Task[None]] = {}
        self._relayed_at: dict[str, float] = {}
        """Per delegated agent: when its last interim was relayed, for the rate cap."""
        self._relays: list[float] = []
        """When each interim was relayed, across every agent, for the cap that is a total rather than a floor."""
        self._approvals: dict[str, list[str]] = {}
        """Per delegated agent: the approval keys already reported, so a retry loop is reported once. A few
        per agent, because an agent that alternates between two refused calls would otherwise report each
        of them again every time it changed its mind."""

    @property
    def config(self) -> Any:
        return self.app.config.companion

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def call_on(self) -> bool:
        return self._calls > 0 and bool(self._listeners)

    # -- the session ------------------------------------------------------------------

    async def session_id(self, *, create: bool = True) -> str:
        """The operator's standing companion session, remembered across restarts; a new one when it is gone."""
        manager = self.app.manager
        assert manager is not None
        async with self._lock:
            known = self._id or str(await self.app.db.kv_get(SESSION_KEY) or "")
            if known and await manager.get_state(known) is not None:
                self._id = known
                await self.adopt(known)
                return known
            self._id = ""
            if not create:
                return ""
            project = await self.project()
            # The companion is a page, not a Telegram command: its session does not speak in the private chat.
            state = await manager.create_session(self.config.name, metadata={FLAG: True, "telegram_detached": True}, project_id=project.id)
            await self.app.db.kv_set(SESSION_KEY, state.session.id)
            self._id = state.session.id
            await self._point_at(state.session.id, preset=background_preset(self.app.config))
            return state.session.id

    async def project(self) -> Any:
        """The companion's project, made the first time the companion needs one.

        Lazily, and not at install: an installation whose operator never talks to the companion has no
        business growing a folder and a row in the Agents list for a feature it does not use.
        """
        manager = self.app.manager
        assert manager is not None
        return await manager.projects.ensure_system(PROJECT_KIND, name=TITLE, root=manager.settings.workspaces_dir / PROJECT_DIR)

    async def adopt(self, session_id: str) -> None:
        """Put a companion session made before the companion's project existed into it.

        The companion has no file tools, so nothing of its own moves; what changes is that it is
        listed where its agents are instead of among the operator's own sessions.
        """
        manager = self.app.manager
        assert manager is not None
        current = await manager.project_of(session_id)
        if current is not None and current.settings.system == PROJECT_KIND:
            return
        await manager.attach_project(session_id, await self.project())
        if current is not None and not current.settings.system and not await manager.projects.sessions_of(current.id):
            await manager.projects.delete(current.id)

    async def _point_at(self, session_id: str, *, preset: str) -> str:
        """Put this turn's preset on the session, or take the override off when it is the default.

        The session carries the choice, not the configuration: the run reads a live override written
        before it starts. So it is written before every turn — the voice preset for a spoken one, the
        background preset for everything else — and only when it differs from what is there.

        It also decides the argument: Settings → Companion is where this conversation's models are
        chosen, so a model put on the companion session from the session screen does not outlive the
        next turn. One setting, in the place the operator went looking for it.
        """
        manager = self.app.manager
        assert manager is not None
        current = str((await manager.live.load(session_id)).get("preset") or "")
        if preset == current:
            return preset
        if preset:
            await manager.set_model(session_id, preset=preset)
        else:
            # Empty means "the default preset", which is what a session with no override of its own
            # already resolves to — so the way to say it is to stop saying anything.
            await manager.set_model(session_id, clear=True)
        return preset

    def _preset_for(self, channel: str) -> str:
        """A spoken turn, and anything said while a call is on, is answered by the voice preset."""
        if channel == "voice" or (channel == "report" and self.call_on):
            return preferred_preset(self.app.config)
        return background_preset(self.app.config)

    async def apply_model(self) -> str:
        """Point the standing conversation at the configured background preset; the app calls this after a change."""
        if self.app.manager is None:
            return ""
        session_id = await self.session_id(create=False)
        return await self._point_at(session_id, preset=self._preset_for("text")) if session_id else ""

    async def restore(self) -> str:
        """Pick the remembered session up after a restart, so the sink knows it before the page is opened."""
        self._id = str(await self.app.db.kv_get(SESSION_KEY) or "")
        self._paused = bool(await self.app.db.kv_get(PAUSED_KEY, False))
        return self._id

    async def new_session(self) -> str:
        """Start the conversation over: a fresh session, the old one left in place as a transcript.

        Its notes need no carrying: the companion belongs to no project of the operator's, so what it
        remembers lives in the operator's global memory, which every conversation reads.
        """
        await self.app.db.kv_set(SESSION_KEY, "")
        self._id = ""
        self._pending.clear()
        self._forget_agents()
        return await self.session_id()

    def _forget_agents(self) -> None:
        """Drop everything held about the old conversation's agents, scheduled relays included."""
        for task in self._relay_tasks.values():
            task.cancel()
        self._relay_tasks.clear()
        self._agent_drafts.clear()
        self._news.clear()
        self._waiting_relay.clear()
        self._relayed_at.clear()
        self._approvals.clear()

    # -- what the page reads ----------------------------------------------------------

    async def profile(self) -> dict[str, Any]:
        """Who the companion is and what it is doing: the shape every client codes against.

        ``state`` is one word, in the order of what the operator would want to know first: it is
        writing an answer (``thinking``), it is on a call with nothing to say yet (``listening``), its
        agents are working while it waits (``working``), or nothing is happening (``idle``).
        """
        manager = self.app.manager
        session_id = await self.session_id(create=False) if manager is not None else ""
        # Counted from the session list alone: every client polls this, and ``agents()`` reads each
        # agent's transcript for its last answer, which a count has no use for.
        rows = await manager.list_sessions(limit=200) if manager is not None and session_id else []
        running = sum(1 for row in rows if row.get("metadata", {}).get(PARENT) == session_id and row.get("status") == "running")
        live = manager.live_state(session_id) if manager is not None and session_id else None
        if live is not None and live.running:
            state = "thinking"
        elif self.call_on:
            state = "listening"
        elif running:
            state = "working"
        else:
            state = "idle"
        return {
            "session_id": session_id,
            "name": self.config.name,
            "avatar": self.config.avatar.model_dump(mode="json"),
            "paused": self._paused,
            "state": state,
            "running_agents": running,
        }

    async def voice_state(self) -> dict[str, Any]:
        """What the call needs to decide how to listen and how to speak: the models, the endpoints, the agents."""
        config = self.config
        tts = config.tts
        ready, reason = tts_configured(tts), ""
        if ready:
            try:
                effective_tts(tts, self.app.manager)
            except RuntimeError as exc:
                ready, reason = False, str(exc)
        chosen = config.preset if config.preset in self.app.config.presets else ""
        resolved = self.app.config.default_preset(preferred_preset(self.app.config))
        background = background_preset(self.app.config)
        background_resolved = self.app.config.default_preset(background)
        return {
            "enabled": config.enabled,
            "session_id": await self.session_id(create=False),
            # Three answers to one question, because the page asks it three ways: which preset the
            # operator picked ("" is "whatever the default is"), which one that comes out as, and
            # what there is to pick from. A page that had only the first showed "—" for the model
            # an installation was in fact talking to.
            "preset": chosen,
            "using": resolved[0] if resolved else "",
            "model": resolved[1].display(resolved[0]) if resolved else "",
            "background_preset": background,
            "background_model": background_resolved[1].display(background_resolved[0]) if background_resolved else "",
            "presets": model_options(self.app.config),
            "tts": {"configured": ready, "reason": reason, "voice": tts.voice, "model": tts.model, "format": tts.format},
            "stt": await self._asr_state(),
            "agents": await self.agents(),
            "listening": bool(self._listeners),
        }

    async def _asr_state(self) -> dict[str, Any]:
        asr = self.app.config.asr
        ready, reason = asr_configured(asr), ""
        if ready:
            try:
                effective_asr(asr, self.app.manager)
            except TranscriptionError as exc:
                ready, reason = False, str(exc)
        return {"configured": ready, "reason": reason, "model": asr.model, "max_seconds": asr.max_seconds}

    async def turn_notes(self, state: Any) -> str | None:
        """The companion's turn context: the channel this turn came through, its goals, its pause.

        In the turn context and not the system prompt, because all three change between turns and the
        system prompt is what every later call of a conversation is cached on. Answers ``None`` for
        every other session, which leaves their notes to the next hook.
        """
        if not self._id or state.session.id != self._id:
            return None
        channel = {
            "voice": "- Channel: voice. The operator is on a call and hears your answer read aloud: spoken sentences only.",
            "text": "- Channel: text. The operator typed this and reads your answer.",
            "report": "- Channel: an agent report or a goal check, not the operator's words."
            + (" A call is on: what you say is read aloud." if self.call_on else " Nobody is on a call: write it for reading."),
            "goal": "- Channel: a goal check the host started on the goal's cadence; the operator is not waiting on it.",
        }.get(self._channel, "")
        lines = [channel] if channel else []
        goals = await self.store.goals(statuses=("active", "paused"))
        if goals:
            lines.append("- Your goals: " + "; ".join(f"{quoted(g.title)[:TITLE_CLIP]} ({g.id}, {g.status}{', ' + g.cadence.text if g.cadence.text else ''})" for g in goals[:12]))
        if self._paused:
            lines.append("- You are paused: no goal checks and no relays until the operator resumes you. Do not start new work unless asked.")
        return "\n".join(lines)

    # -- what the operator says -------------------------------------------------------

    async def say(self, text: str) -> str:
        """One utterance, already in words: a spoken turn of the companion session.

        What it can wait on, because a page that is silent for half a minute deserves a list rather
        than a guess. ``submit`` takes the session's own ``submit_lock``, which nothing outside this
        conversation holds; it waits for the previous run of *this* session to have settled — the
        snapshot behind the last answer — and for nothing else; and it starts the run rather than
        queueing it, because the companion is never left running between utterances. There is no
        shared queue, no fleet limit and no board in front of it: an agent the companion delegated to
        runs in a session of its own and cannot hold this one up. Everything after that is the model,
        which is why ``preferred_preset`` above cares so much about which one it is.
        """
        return await self._turn(text, channel="voice", via="voice")

    async def message(self, text: str, attachments: Any = (), *, source: str = "app") -> str:
        """A typed message: a text turn of the companion session, answered by the background preset."""
        return await self._turn(text, channel="voice" if source == "voice" else "text", via=source, attachments=attachments)

    async def _turn(self, text: str, *, channel: str, via: str, attachments: Any = ()) -> str:
        body = text.strip()
        if not body and not attachments:
            raise ValueError("nothing was said")
        manager = self.app.manager
        assert manager is not None
        session_id = await self.session_id()
        # Read the configured model at every turn rather than once at the birth of the session: the
        # engine is built per run, so this is the last moment the choice can still take effect
        # without a restart and without a new conversation.
        await self._point_at(session_id, preset=self._preset_for(channel))
        self._channel = channel
        await self.emit("status", {"state": "thinking"})
        return await manager.submit(session_id, body or "File attached.", attachments, via=via)

    async def before_session_turn(self, session_id: str) -> bool:
        """Ready a message that reaches the companion's session by the ordinary session routes.

        The session screen's composer and its file upload — which is how the desktop's quick chat sends
        a picture — submit straight to the session, past :meth:`message`. Without this such a turn ran
        on whichever preset the last turn left on the session (the voice one, after a call) and its
        turn context said the wrong channel. Answers whether the session is the companion's at all.
        """
        if not self._id or session_id != self._id:
            return False
        await self._point_at(session_id, preset=self._preset_for("text"))
        self._channel = "text"
        await self.emit("status", {"state": "thinking"})
        return True

    async def interrupt(self) -> bool:
        """Barge-in: the operator started talking over the answer, so the answer stops."""
        manager = self.app.manager
        assert manager is not None
        session_id = await self.session_id(create=False)
        stopped = await manager.stop(session_id) if session_id else False
        self._drafts.clear()
        await self.emit("status", {"state": "idle"})
        return stopped

    async def call(self, on: bool) -> bool:
        """A page opened or closed a call. Reports said while one is open are read aloud by the voice preset."""
        self._calls = self._calls + 1 if on else max(0, self._calls - 1)
        await self.emit("status", {"state": "listening" if self.call_on else "idle"})
        return self.call_on

    async def pause(self, paused: bool) -> dict[str, Any]:
        """Pause or resume the companion's own initiative: goal checks and relays into the conversation.

        What the operator says to it is still answered — a pause is the companion not acting on its
        own, not the companion refusing to talk. Reports that arrive while paused are held and still
        reach the Inbox; resuming delivers them as one turn.
        """
        if paused == self._paused:
            return await self.profile()
        self._paused = paused
        await self.app.db.kv_set(PAUSED_KEY, paused)
        await self.store.log("resumed" if not paused else "paused", "Resumed" if not paused else "Paused")
        if not paused and self._listeners:
            await self.flush_pending()
        profile = await self.profile()
        await self.emit("profile", profile)
        return profile

    async def save_profile(self, *, name: str | None = None, avatar: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rename the companion or change its face; written to ``[companion]`` like any setting."""
        raw = self.app.config.model_dump(mode="json")
        if name is not None:
            raw["companion"]["name"] = " ".join(name.split())
        if avatar is not None:
            raw["companion"]["avatar"] = {**raw["companion"]["avatar"], **avatar}
        new_config = type(self.app.config).model_validate(raw)
        await self.app.save_config(new_config)
        if getattr(self.app, "front", None) is not None:
            self.app.front.config = new_config
        manager = self.app.manager
        session_id = self._id
        if name is not None and manager is not None and session_id:
            # The session is the companion, so it carries the companion's name in the Agents list.
            with contextlib.suppress(Exception):
                await manager.rename_session(session_id, new_config.companion.name)
        profile = await self.profile()
        await self.emit("profile", profile)
        return profile

    # -- the companion's tools -------------------------------------------------------

    async def own_agent(self, session_id: str, parent: str | None = None) -> Any:
        """The state of an agent the companion started, or ``KeyError``.

        Every tool that names a session id goes through here. The companion has no business with the
        operator's own sessions, with another leader's subagents, or with its own session: a
        hallucinated or borrowed id is refused rather than acted on.
        """
        manager = self.app.manager
        assert manager is not None
        parent = parent if parent is not None else await self.session_id(create=False)
        state = await manager.get_state(session_id)
        if not parent or state is None or state.metadata.get(PARENT) != parent:
            raise KeyError(session_id)
        return state

    async def delegate(self, *, title: str, task: str, session_id: str | None = None, workspace: str = "shared", project_id: str | None = None, goal_id: str | None = None) -> dict[str, Any]:
        """Hand work to an agent: a new session for it, or another instruction to one already working.

        Where the new agent works is the second decision, and the companion makes it out loud:

        * ``workspace="shared"`` — the companion's project's folder, which every agent the companion
          started shares. Several agents on one errand hand each other files by putting them there.
        * ``workspace="own"`` — a directory of its own inside the companion's project: still listed under
          the companion, and its files are nobody else's. An errand with nothing to do with the rest.
        * ``project_id`` — one of the operator's projects, for "in the bakery project, …". The agent
          works in that project's folder, which is what makes it an agent of that project rather
          than one of the companion's that happens to have been pointed at it.

        ``goal_id`` links the agent to one of the companion's goals, so the goal's next check sees it.
        """
        manager = self.app.manager
        assert manager is not None
        body = task.strip()
        if not body:
            raise ValueError("an agent needs a task; write what is to be done")
        if workspace not in ("shared", "own"):
            raise ValueError(f"workspace is 'shared' or 'own', not {workspace!r}")
        companion_id = await self.session_id()
        goal = await self.store.goal(goal_id) if goal_id else None
        if goal_id and goal is None:
            raise ValueError(f"no goal has the id {goal_id!r}; GoalList shows them")
        if session_id and (project_id or workspace != "shared"):
            raise ValueError("an agent that is already running works where it was started; leave workspace and project_id out when you steer one")
        if session_id:
            state = await self.own_agent(session_id, companion_id)
            # An agent stopped on a question is answered, not queued behind it: the operator is on the
            # line and the companion is the only way their answer can reach the agent from this page.
            answering = state.pending is not None
            await manager.submit(session_id, body, as_answer=answering, origin="companion")
            if goal is not None:
                await self.store.update_goal(goal.id, link_session=session_id)
            await self.store.log("answered" if answering else "steered", state.session.title, body, session_id=session_id, goal_id=goal_id)
            await self.emit("status", {"state": "delegating", "title": state.session.title})
            await self.emit("agents", {"agents": await self.agents()})
            return {"session_id": session_id, "title": state.session.title, "steered": True, "answered": answering}
        name = title.strip() or body[:40]
        # The companion hands work to an agent of its own. By default that is the companion's project, so
        # an agent started by talking is listed where the talking happened and not in the middle of
        # the operator's own sessions; a named project is the operator asking for one of theirs.
        own = False
        if project_id:
            project = await manager.projects.get(project_id)
            if project is None:
                raise ValueError(f"no project has the id {project_id!r}; Projects lists the ones there are")
            if workspace == "own":
                raise ValueError(f"an agent in {project.name} works in the project's own folder; leave workspace out when you name a project")
            if not await manager.projects.ensure_reachable(project.primary):
                raise ValueError(f"the folder of {project.name} ({project.primary.path}) is not reachable from here; it has to be mounted first")
        else:
            # Under the same lock the session takes: the companion is told to fan several errands
            # out at once, and each of them wants the companion's project the first time anything does.
            async with self._lock:
                project = await self.project()
            own = workspace == "own"
        state = await manager.create_session(
            name,
            metadata={PARENT: companion_id, "brief": body, "telegram_detached": True},
            project_id=project.id,
            own_directory=own,
        )
        await manager.submit(state.session.id, body, as_answer=False, origin="companion")
        if goal is not None:
            await self.store.update_goal(goal.id, link_session=state.session.id)
        await self.store.log("delegated", name, body, session_id=state.session.id, goal_id=goal_id)
        await self.emit("status", {"state": "delegating", "title": name})
        await self.emit("agents", {"agents": await self.agents()})
        return {"session_id": state.session.id, "title": name, "steered": False, "answered": False, "project": project.name, "workspace": "own" if own else "shared"}

    async def agents(self) -> list[dict[str, Any]]:
        """What the companion has running, newest first: title, status, when it last spoke, its last words."""
        manager = self.app.manager
        if manager is None:
            return []
        companion_id = self._id
        out: list[dict[str, Any]] = []
        for row in await manager.list_sessions(limit=200):
            if row.get("metadata", {}).get(PARENT) != companion_id or not companion_id:
                continue
            answer = await self.last_answer(row["id"])
            news = self._news.get(row["id"], {})
            out.append(
                {
                    "session_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "last_message_at": row["last_message_at"],
                    "answer": answer[:LIST_CLIP],
                    "progress": news.get("line", ""),
                    "progress_at": news.get("at", ""),
                    "waiting": news.get("waiting", ""),
                }
            )
            if len(out) >= MAX_AGENTS:
                break
        return out

    async def project_list(self) -> list[dict[str, Any]]:
        """The operator's projects, for an agent the operator asked to be started in one of them.

        The companion's project is left out on purpose: it is where an agent goes when no project is
        named, so offering it as a choice would only invite the companion to name it back.
        """
        manager = self.app.manager
        if manager is None:
            return []
        counts = await manager.projects.summary()
        return [
            {"id": p.id, "name": p.name, "root": str(p.primary.path), "reachable": p.primary.reachable, "agents": counts.get(p.id, {}).get("total", 0)}
            for p in await manager.projects.list()
            # Nor a chat's project a moved chat left empty: kept only for the move's Undo.
            if not p.settings.system and not (p.settings.ephemeral and p.id not in counts)
        ]

    async def result(self, session_id: str) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await self.own_agent(session_id)
        rows = await manager.list_sessions(limit=200)
        status = next((r["status"] for r in rows if r["id"] == session_id), "idle")
        return {"session_id": session_id, "title": state.session.title, "status": status, "answer": (await self.last_answer(session_id))[:ANSWER_CLIP]}

    async def stop_agent(self, session_id: str) -> bool:
        manager = self.app.manager
        assert manager is not None
        await self.own_agent(session_id)
        return await manager.stop(session_id)

    async def last_answer(self, session_id: str) -> str:
        manager = self.app.manager
        assert manager is not None
        rows = await manager.sessions.list_transcript(session_id)
        for message in reversed(rows):
            if message.role is MessageRole.assistant:
                text = "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return split_headline(text)[0]
        return ""

    # -- the stream to the page -------------------------------------------------------

    async def emit(self, name: str, payload: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            queue.put_nowait((name, payload))

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
        """One connected page. The first one to arrive gets whatever the agents reported while nobody listened."""
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._listeners.add(queue)
        try:
            await self.flush_pending()
            yield queue
        finally:
            self._listeners.discard(queue)

    @property
    def held(self) -> list[str]:
        """Reports waiting for a page to connect (or for the companion to be resumed), oldest first."""
        return [line for _, line in self._pending]

    async def flush_pending(self) -> None:
        """What the agents said while nobody was listening, as one turn.

        A report older than the grace is still delivered — the conversation is a transcript now, not
        only a voice, and a result nobody heard is still a result — but it says how old it is, so the
        companion opens with "while you were away" rather than reading out a morning's news as if it
        had just happened. Anything that needed the operator went to the Inbox when it arrived.
        """
        if self._paused:
            return
        now = time.monotonic()
        held, self._pending = self._pending, []
        if not held:
            return
        parts = []
        for at, line in held:
            age = now - at
            parts.append(line if age < PENDING_GRACE_SECONDS else f"(reported {int(age // 60)} minutes ago, while nobody was listening)\n{line}")
        try:
            await self.say_to_companion("\n\n".join(parts))
        except Exception:  # noqa: BLE001 — a report that cannot be delivered must not close the stream
            logger.exception("could not deliver the held agent reports")

    async def say_to_companion(self, text: str, *, channel: str = "report") -> None:
        manager = self.app.manager
        if manager is None:
            raise RuntimeError("there is no session manager to say anything to")
        session_id = await self.session_id()
        await self._point_at(session_id, preset=self._preset_for(channel))
        self._channel = channel
        await manager.submit(session_id, text, as_answer=False, origin="agent" if channel == "report" else "goal")

    async def report(self, text: str, *, kind: str = "final", title: str = "", session_id: str = "", body: str = "") -> None:
        """A line for the companion to relay — now if a page is listening, later if not.

        Later is the next page to connect, or the resume of a paused companion. A finished result also
        goes to the Inbox and the operator's phone at once when nobody is listening: it is the one kind
        of report nothing else announces for an agent the companion started.
        """
        if self._listeners and not self._paused:
            try:
                await self.say_to_companion(text)
                return
            except Exception:  # noqa: BLE001
                logger.exception("could not deliver an agent report to the companion")
        self._pending.append((time.monotonic(), text))
        del self._pending[:-MAX_PENDING]
        if kind == "final":
            await self.notify_operator(f"{self.config.name}: {title or session_id} finished", body, session_id=session_id or None, key=f"agent:{session_id}")

    async def notify_operator(self, title: str, body: str, *, session_id: str | None = None, key: str = "", level: str = "normal") -> None:
        """Post to the Inbox (and on to the phone, as the notification settings decide)."""
        service = getattr(self.app, "extensions", {}).get("notifications")
        if service is None:
            return
        try:
            await service.post(Draft(
                "agent_notify", title[:120], body.strip()[:1000], kind="companion", level=level,  # type: ignore[arg-type]
                tone="info", session_id=session_id, link="/app/companion",
                dedupe_key=f"companion:{key}" if key else None, source="companion",
            ))
        except Exception:  # noqa: BLE001 — a notification that fails must not lose the report behind it
            logger.exception("could not post the companion's notification")

    # -- events ------------------------------------------------------------------------

    async def on_event(self, session_id: str, event: Any) -> None:
        companion_id = self._id
        if not companion_id:
            return
        if session_id != companion_id:
            # Every event of every session arrives here, so nothing expensive may happen before the
            # cheap check on what the process already holds: is this one of the companion's agents?
            if not self._is_delegated(session_id):
                return
            payload = event.payload
            run_id = str(getattr(event, "run_id", "") or "")
            if event.type is EventType.TOOL_CALL_PENDING and payload.get("kind") == "ask_user":
                await self._agent_asks(session_id, event)
            elif event.type is EventType.CONTENT_BLOCK_DELTA:
                self._collect(session_id, run_id, delta_text(payload))
            elif event.type is EventType.MESSAGE_START:
                self._agent_drafts.pop((session_id, run_id), None)
                if self._listeners:
                    await self.emit("agents", {"agents": await self.agents()})
            elif event.type is EventType.MESSAGE_STOP:
                await self._agent_paused(session_id, run_id, payload)
            elif event.type is EventType.TOOL_RESULT and payload.get("is_error"):
                await self._agent_blocked(session_id, payload)
            elif self._listeners and event.type is EventType.STATE_CHANGED:
                await self.emit("agents", {"agents": await self.agents()})
            return
        if not self._listeners:
            return
        run_id = str(getattr(event, "run_id", "") or "")
        if event.type is EventType.MESSAGE_START:
            self._drafts[run_id] = ""
            await self.emit("status", {"state": "thinking", "turn": run_id})
        elif event.type is EventType.CONTENT_BLOCK_DELTA:
            delta = event.payload.get("delta") or {}
            if delta.get("type") != "text_delta":
                return
            draft = self._drafts.get(run_id, "") + str(delta.get("text") or "")
            await self.emit("partial", {"text": draft})
            sentences, self._drafts[run_id] = split_sentences(draft)
            for sentence in sentences:
                spoken = speakable(sentence)
                if spoken:
                    await self.say_aloud(run_id, spoken)
        elif event.type is EventType.MESSAGE_STOP:
            await self._flush_draft(run_id)
        elif event.type is EventType.TOOL_USE_STOP and str(event.payload.get("tool_name") or "") == "Delegate":
            title = str((event.payload.get("final_input") or {}).get("title") or "")
            await self.emit("status", {"state": "delegating", "title": title})
        elif event.type is EventType.ERROR:
            await self.emit("error", {"message": str(event.payload.get("message") or "the run failed")})

    def _collect(self, session_id: str, run_id: str, text: str) -> None:
        """Hold what an agent is writing, up to the cap; a delta that is not prose adds nothing."""
        if not text or not self.config.progress:
            return
        key = (session_id, run_id)
        self._agent_drafts[key] = (self._agent_drafts.get(key, "") + text)[:DRAFT_CLIP]

    async def _agent_paused(self, session_id: str, run_id: str, payload: dict[str, Any]) -> None:
        """An agent stopped writing. If it stopped to call a tool, what it wrote is progress."""
        draft = self._agent_drafts.pop((session_id, run_id), "")
        if not draft or not settles_interim(payload):
            return
        await self._note_progress(session_id, progress_line(draft))

    async def _note_progress(self, session_id: str, line: str) -> None:
        """Record an interim for the page, and schedule the one the companion will hear.

        The page gets it immediately — it is a line of text in a panel, it costs nothing and it is what
        the operator looks at when they want the detail. The companion gets it through the window below,
        because for the companion an interim is a spoken turn, and spoken turns are expensive in the one
        currency that matters here: the operator's attention.
        """
        if not line:
            return
        self._news.pop(session_id, None)  # re-inserted, so the trim below drops the quietest agent, not this one
        self._news[session_id] = {"line": line, "at": datetime.now(UTC).isoformat(), "waiting": ""}
        for stale in list(self._news)[: -2 * MAX_AGENTS]:
            # A delegated session that never finishes leaves its news behind; the panel shows at most
            # MAX_AGENTS of them anyway, so the oldest are dropped rather than kept for a restart.
            del self._news[stale]
        if self._listeners:
            await self.emit("agents", {"agents": await self.agents()})
        config = self.config
        if not config.progress or not self._listeners:
            # Nobody is listening: an interim is not held for later. By the time a page connects the
            # agent has either moved on or finished, and the final report says more than this would.
            return
        self._waiting_relay[session_id] = line
        task = self._relay_tasks.get(session_id)
        if task is None or task.done():
            self._relay_tasks[session_id] = asyncio.create_task(self._relay_progress(session_id), name=f"voice-progress:{session_id}")

    async def _relay_progress(self, session_id: str) -> None:
        """Wait out the window and the rate cap, then relay whichever interim is the newest by then."""
        config = self.config
        try:
            await asyncio.sleep(config.progress_window_seconds)
            gap = config.progress_min_gap_seconds - (time.monotonic() - self._relayed_at.get(session_id, -config.progress_min_gap_seconds))
            if gap > 0:
                await asyncio.sleep(gap)
            line = self._waiting_relay.pop(session_id, "")
            manager = self.app.manager
            if not line or not self._listeners or manager is None:
                return
            if not self._fleet_has_room():
                return
            if not await self._companion_free(config.progress_window_seconds):
                # The companion is in the middle of a turn of its own. Submitting here would steer that
                # turn, and the operator would hear their own answer interrupted by an agent's aside.
                # A progress line is not worth that, and the agent's next paragraph replaces it anyway.
                return
            state = await manager.get_state(session_id)
            if state is None or state.metadata.get(PARENT) != self._id:
                return
            self._relayed_at[session_id] = time.monotonic()
            self._relays.append(self._relayed_at[session_id])
            await self.say_to_companion(
                report_block(
                    kind="progress",
                    title=state.session.title,
                    session_id=session_id,
                    state="is still working; this is what it said on the way, not a result",
                    body=line,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a progress line is the least important thing here
            logger.exception("could not relay an agent's progress")
        finally:
            self._relay_tasks.pop(session_id, None)
            if self._waiting_relay.get(session_id) and self._listeners:
                # A line that arrived while this task was awaiting: it found the task not done, so
                # nothing scheduled another, and this one has already taken its own line away.
                self._relay_tasks[session_id] = asyncio.create_task(self._relay_progress(session_id), name=f"voice-progress:{session_id}")

    def _fleet_has_room(self) -> bool:
        """Whether another interim may be spoken at all this minute, counting every agent together.

        The per-agent floor bounds one narrator; twenty agents is what ``MAX_AGENTS`` allows, and
        twenty of them each obeying their own floor is a conversation nobody can hold. Over the cap
        the oldest relays are what age out of the window, and the line that arrives is dropped.
        """
        cap = int(self.config.progress_max_per_minute)
        now = time.monotonic()
        self._relays = [at for at in self._relays if now - at < 60.0]
        return len(self._relays) < cap

    async def _companion_free(self, hold_seconds: float) -> bool:
        """Whether the companion can be told something now, after waiting up to ``hold_seconds`` for it.

        The operator's own turn comes first, always: the voice session answers what was said to it, and an
        interim that arrives mid-answer waits its turn or is dropped. Barge-in keeps working as before —
        the operator talking over the answer still goes through ``interrupt``.
        """
        manager = self.app.manager
        if manager is None or not self._id:
            return False
        deadline = time.monotonic() + max(hold_seconds, 0.0)
        while True:
            state = manager.live_state(self._id)
            if state is None or not state.running:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(RELAY_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))

    def _stop_relay(self, session_id: str) -> None:
        """Nothing more is relayed for this agent: what it was about to say is behind the news it just made."""
        self._waiting_relay.pop(session_id, None)
        task = self._relay_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def drain_progress(self) -> None:
        """Wait for the scheduled interims to be relayed or dropped. The tests' way to see what the window decided."""
        while True:
            tasks = [t for t in self._relay_tasks.values() if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _agent_blocked(self, session_id: str, payload: dict[str, Any]) -> None:
        """A tool call the policy refused: the agent is stopped until the operator approves it.

        This is relayed like a question, not like progress — it is held for a page that is not connected
        and it does not wait out the window — because it is the one interim state that goes nowhere on its
        own. An agent narrating gets on with its job; an agent waiting for an approval does not.
        """
        match = APPROVAL_KEY_RE.search(str(payload.get("content") or ""))
        manager = self.app.manager
        if match is None or manager is None or not self.config.progress:
            return
        key = match.group(1)
        reported = self._approvals.setdefault(session_id, [])
        if key in reported:
            return  # the agent retries the refused call; the operator is told about it once
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get(PARENT) != self._id:
            return
        reported.append(key)
        del reported[:-APPROVALS_REMEMBERED]
        pending = (state.metadata.get("policy_pending") or {}).get(key) or {}
        what = f"{pending.get('tool')}: {pending.get('text')}" if pending else "a call it tried to make"
        self._news[session_id] = {"line": f"waiting for approval — {what}"[:PROGRESS_CLIP], "at": datetime.now(UTC).isoformat(), "waiting": "approval"}
        self._stop_relay(session_id)
        await self.report(
            report_block(
                kind="approval",
                title=state.session.title,
                session_id=session_id,
                state="is stopped: the policy will not run one of its calls until the operator approves it in that session",
                body=what[:REPORT_CLIP],
            ),
            kind="approval",
        )
        await self.store.log("approval", state.session.title, what, session_id=session_id)
        await self.emit("agents", {"agents": await self.agents()})

    def _is_delegated(self, session_id: str) -> bool:
        """Whether the companion started this session, read from what the process already holds."""
        manager = self.app.manager
        state = manager.live_state(session_id) if manager is not None else None
        return state is not None and state.metadata.get(PARENT) == self._id

    async def say_aloud(self, run_id: str, text: str) -> None:
        """One sentence of this run, handed to the page to be read out.

        Every spoken sentence carries the run it belongs to, and the page plays only the run it is on.
        Without that, an answer whose synthesiser was still warming up came out behind the answer
        after it: the clips of the first turn were still queued when the second turn's sentences
        arrived, and the operator heard both, in the wrong order, long after asking.

        The first sentence of a run also starts the clock the diagnostic line reads — the operator's
        question is not "how long did the model take" but "how long between the words appearing and a
        sound", and that is a measurement, not a guess.
        """
        if run_id not in self._turns:
            self._turns[run_id] = TurnSpeech(turn=run_id, said_at=time.monotonic())
            for stale in list(self._turns)[:-TURN_CLOCKS]:
                del self._turns[stale]
        await self.emit("say", {"text": text, "turn": run_id})

    def first_audio(self, run_id: str, *, clip_ms: int, load_ms: int) -> None:
        """The first sound of a run exists. Called by the endpoint that made it, once per run.

        ``clip_ms`` is what this request spent producing that first clip and ``load_ms`` is how much
        of it was the voice being built, so the line can say where the wait went rather than only how
        long it was.
        """
        timing = self._turns.get(run_id)
        if timing is None or timing.first_audio_ms:
            return
        timing.first_audio_ms = max(1, int((time.monotonic() - timing.said_at) * 1000))
        timing.clip_ms = clip_ms
        timing.load_ms = load_ms
        self._last_turn = timing

    def last_turn(self) -> dict[str, object]:
        """How long the last answer waited between being written and being heard, for the page."""
        return self._last_turn.as_json() if self._last_turn is not None else TurnSpeech().as_json()

    async def _flush_draft(self, run_id: str) -> None:
        tail = speakable(self._drafts.pop(run_id, ""))
        if tail:
            await self.say_aloud(run_id, tail)

    async def _agent_asks(self, session_id: str, event: Any) -> None:
        manager = self.app.manager
        if manager is None:
            return
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get(PARENT) != self._id:
            return
        questions = (event.payload.get("ask_user_payload") or {}).get("questions") or []
        asked = str(questions[0].get("question") if questions else "").strip()
        self._news[session_id] = {"line": asked[:PROGRESS_CLIP], "at": datetime.now(UTC).isoformat(), "waiting": "operator"}
        self._stop_relay(session_id)
        await self.report(
            report_block(
                kind="question",
                title=state.session.title,
                session_id=session_id,
                state="is waiting for the operator and does nothing until it is answered; the answer goes back with Delegate(title, task, session_id) naming that id",
                body=asked[:REPORT_CLIP] or "it did not say what it is asking; its session has the question",
            ),
            kind="question",
        )
        await self.store.log("question", state.session.title, asked, session_id=session_id)
        await self.emit("agents", {"agents": await self.agents()})

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        companion_id = self._id
        if not companion_id:
            return
        if session_id == companion_id:
            await self._flush_draft(run_id)
            await self.emit("done", {"status": status, "turn": run_id})
            await self.emit("status", {"state": "idle", "turn": run_id})
            await self.emit("agents", {"agents": await self.agents()})
            return
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None or state.metadata.get(PARENT) != companion_id or status == "awaiting":
            return
        answer = await self.last_answer(session_id) if status == "completed" else ""
        body = clip_report(answer) if answer else f"no final answer ({status}); its session has the detail"
        # Whatever it was about to say on the way is behind us now: the answer says more, and a progress
        # line spoken after "it finished" would tell the operator the job is still running.
        self._stop_relay(session_id)
        self._news.pop(session_id, None)
        self._approvals.pop(session_id, None)
        self._agent_drafts = {k: v for k, v in self._agent_drafts.items() if k[0] != session_id}
        await self.store.log("finished" if status == "completed" else "failed", state.session.title, answer or body, session_id=session_id)
        await self.report(
            report_block(kind="final", title=state.session.title, session_id=session_id, state="finished", body=body),
            kind="final", title=state.session.title, session_id=session_id, body=answer or body,
        )
        await self.emit("agents", {"agents": await self.agents()})

    # -- speech ------------------------------------------------------------------------

    async def speech(self, text: str) -> tuple[AsyncIterator[bytes], str]:
        """One sentence as audio from the configured endpoint, streamed on as it arrives."""
        config = effective_tts(self.config.tts, self.app.manager)
        url = config.url.rstrip("/") + "/audio/speech"
        headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        body = {"model": config.model, "voice": config.voice, "input": text, "response_format": config.format}
        client = httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15.0))
        request = client.build_request("POST", url, headers=headers, json=body)
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise RuntimeError(f"the speech endpoint answered HTTP {response.status_code}")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return chunks(), AUDIO_TYPES.get(config.format, "application/octet-stream")

    # -- goals ---------------------------------------------------------------------------

    async def goal_create(self, *, title: str, brief: str = "", cadence: str = "") -> dict[str, Any]:
        goal = await self.store.create_goal(title=title, brief=brief, cadence=parse_cadence(cadence, min_minutes=self.config.goal_min_interval_minutes))
        await self.store.log("goal", f"New goal: {goal.title}", goal.brief, goal_id=goal.id)
        await self.emit("goals", {})
        return goal.as_dict()

    async def goal_update(self, *, goal_id: str, title: str | None = None, brief: str | None = None, status: str | None = None, cadence: str | None = None) -> dict[str, Any]:
        parsed = parse_cadence(cadence, min_minutes=self.config.goal_min_interval_minutes) if cadence is not None else None
        before = await self.store.goal(goal_id)
        goal = await self.store.update_goal(goal_id, title=title, brief=brief, status=status, cadence=parsed)
        if before is not None and status is not None and status != before.status:
            await self.store.log("goal", f"Goal {status}: {goal.title}", goal_id=goal.id)
        await self.emit("goals", {})
        return goal.as_dict()

    async def goal_list(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        statuses = ("active", "paused", "done", "dropped") if include_closed else ("active", "paused")
        return [goal.as_dict() for goal in await self.store.goals(statuses=statuses)]

    async def goal_report(self, *, goal_id: str, report: str, needs_attention: bool = False) -> dict[str, Any]:
        """Record what the companion found about a goal, and tell the operator when nobody is listening.

        A report made while a page is open is already in the conversation the operator is looking at;
        one made by a goal check at three in the morning reaches them through the Inbox instead, and
        one the companion marks as needing them reaches the Inbox either way.
        """
        goal = await self.store.report(goal_id, report)
        await self.store.log("report", goal.title, goal.last_report, goal_id=goal.id)
        if needs_attention or not self._listeners:
            await self.notify_operator(f"{self.config.name}: {goal.title}", goal.last_report, key=f"goal:{goal.id}", level="normal")
        await self.emit("goals", {})
        return goal.as_dict()

    async def check_goals(self) -> int:
        """Give the companion a turn for the goal that is most overdue, if it is free to take one.

        One per pass: two goals due at once are two turns, and the second waits for the first to end
        rather than steering it. A paused companion, or a switched-off one, checks nothing.
        """
        manager = self.app.manager
        if manager is None or self._paused or not self.config.enabled:
            return 0
        due = await self.store.due()
        if not due:
            return 0
        session_id = await self.session_id()
        live = manager.live_state(session_id)
        if live is not None and live.running:
            return 0
        goal = await self.store.checked(due[0].id)
        if goal is None:
            return 0
        await self.store.log("check", goal.title, goal_id=goal.id)
        await self.say_to_companion(goal_block(goal, await self.agents()), channel="goal")
        return 1

    async def goal_loop(self) -> None:
        while True:
            await asyncio.sleep(GOAL_POLL_SECONDS)
            try:
                await self.check_goals()
            except Exception:  # noqa: BLE001 — one failed check must not end the cadence of every goal
                logger.exception("the companion's goal check failed")

    # -- the page's other tabs ---------------------------------------------------------

    async def companion_sessions(self) -> list[str]:
        """Every session that has been the companion: the schedules and notes of an earlier conversation are still its own."""
        rows = await self.app.db.fetchall(
            "SELECT id FROM sessions WHERE json_valid(metadata) AND json_extract(metadata, '$.companion') = 1 ORDER BY created_at DESC"
        )
        return [row["id"] for row in rows]

    async def schedules(self) -> dict[str, Any]:
        """What runs on a clock for the companion: its own schedules and proposals, and its goals' cadences."""
        sessions = await self.companion_sessions()
        marks = ",".join("?" for _ in sessions) or "''"
        rows = await self.app.db.fetchall(
            f"SELECT id, name, kind, cron, run_at, next_run_at, enabled, created_by_session FROM schedules"
            f" WHERE deleted_at IS NULL AND created_by_session IN ({marks}) ORDER BY next_run_at",
            tuple(sessions),
        )
        proposals = await self.app.db.fetchall(
            "SELECT id, json_extract(request_json, '$.name') AS name, json_extract(request_json, '$.kind') AS kind,"
            " json_extract(request_json, '$.cron') AS cron, json_extract(request_json, '$.run_at') AS run_at, status, created_at FROM schedule_proposals"
            f" WHERE source_session_id IN ({marks}) AND status = 'pending' ORDER BY created_at DESC",
            tuple(sessions),
        )
        goals = [g.as_dict() for g in await self.store.goals(statuses=("active", "paused")) if g.cadence.text]
        return {"schedules": [dict(r) for r in rows], "proposals": [dict(r) for r in proposals], "goals": goals}

    async def computer(self) -> dict[str, Any]:
        """The browsers the companion's agents drive, for the Computer tab: the companion itself has none."""
        found = getattr(self.app, "extensions", {}).get("browser")
        if found is None:
            return {"available": False, "reason": "this installation has no browser", "groups": []}
        envs = found.environments()
        up = [e for e in envs if e["available"]]
        groups: list[dict[str, Any]] = []
        for agent in await self.agents():
            groups.extend(await found.list(session_id=agent["session_id"]))
        groups.sort(key=lambda g: (g.get("status") != "open", str(g.get("last_activity_at") or "")), reverse=False)
        reason = "" if up else next((f"{e['env']}: {e['detail'] or e['reason']}" for e in envs if e["configured"]), "no browser is configured")
        return {"available": bool(up), "reason": reason, "groups": groups}

    def tools(self) -> dict[str, Any]:
        """What the companion may call, and what its agents may: the Tools tab, read from the registry itself."""
        manager = self.app.manager
        registry = {t.name: t for t in manager.tools.list_all()} if manager is not None else {}
        own = [
            {"name": name, "description": registry[name].definition.description.split(". ")[0][:200]}
            for name in COMPANION_TOOLS
            if name in registry
        ]
        # Its agents are ordinary sessions: every registered tool but the companion's own, under the
        # same policy as any session, and the MCP servers the operator can switch on in each of them.
        return {"companion": own, "agents": {"tools": len(registry) - len(COMPANION_ONLY_TOOLS), "mcp": sorted(self.app.config.mcp.servers)}}

    async def activity(self, *, limit: int = 100, before: int | None = None) -> list[dict[str, Any]]:
        return await self.store.activity(limit=limit, before=before)

    # -- the tools' way in ---------------------------------------------------------------

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "delegate":
            return await self.delegate(**kwargs)
        if op == "projects":
            return await self.project_list()
        if op == "agents":
            return await self.agents()
        if op == "result":
            return await self.result(kwargs["session_id"])
        if op == "stop":
            return await self.stop_agent(kwargs["session_id"])
        if op == "goal_create":
            return await self.goal_create(**kwargs)
        if op == "goal_update":
            return await self.goal_update(**kwargs)
        if op == "goal_list":
            return await self.goal_list(**kwargs)
        if op == "goal_report":
            return await self.goal_report(**kwargs)
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    companion = Companion(app)
    await companion.restore()
    app.extensions["companion"] = companion
    assert app.manager is not None
    app.manager.service_hooks["companion"] = companion.service
    app.manager.add_sink(companion.on_event)
    app.manager.on_finished(companion.on_run_finished)
    app.manager.turn_notes_hooks.append(companion.turn_notes)
    return [asyncio.create_task(companion.goal_loop(), name="companion-goals")]


__all__ = [
    "PARENT",
    "PENDING_GRACE_SECONDS",
    "REPORT_CLOSE",
    "REPORT_OPEN",
    "SESSION_KEY",
    "Companion",
    "GoalError",
    "background_preset",
    "delta_text",
    "effective_tts",
    "goal_block",
    "install",
    "preferred_preset",
    "progress_line",
    "quoted",
    "report_block",
    "settles_interim",
    "speakable",
    "split_sentences",
    "tts_configured",
]
