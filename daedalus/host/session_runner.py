"""Sessions and runs: one ``QueryEngine`` per session, one asyncio task per run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from protocore.contracts.llm import (
    LLMError,
    LLMObservabilityContext,
    LLMRateLimitError,
    LLMRequest,
    ProviderFailureReason,
)
from protocore.contracts.outcome import RUN_CAUSE_FIELD, RUN_OUTCOME_FIELD, RunCause, RunOutcome
from protocore.contracts.runtime_constants import LoopConstants
from protocore.contracts.tool_registry import ToolVisibilityPolicy, policy_admits, tool_group_of
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    IMAGE_REFS_METADATA_KEY,
    OPERATOR_WORDS_METADATA_KEY,
    ImageRefBlock,
    Message,
    MessageRole,
    Run,
    RunStatus,
    Session,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from protocore.runtime.context.compaction import CompactionState
from protocore.runtime.context.ledger import is_ledger
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType
from protocore.runtime.live_control import new_queued_prompt
from protocore.runtime.loop_state import LoopState
from protocore.runtime.query_engine import QueryEngine
from protocore.runtime.soft_stop import CAUSE_MODEL_NO_PROGRESS, CAUSE_PROVIDER_ERROR
from protocore.runtime.tool_registry import ToolRegistry
from protocore.tools.tool_search import TOOL_SEARCH_TOOL_NAME, ToolSearchTool

from daedalus.browser import cookies as cookie_files
from daedalus.computer import agent as computer_agent
from daedalus.computer import awareness as computer_awareness
from daedalus.computer import wire as computer_wire
from daedalus.config import (
    BTW_TOOLS,
    CHAT_SEARCH_TOOLS,
    COMPANION_ONLY_TOOLS,
    COMPANION_TOOLS,
    DISPATCHER_TOOLS,
    ORCHESTRATOR_ONLY_TOOLS,
    ORCHESTRATOR_TOOLS,
    PROMPT_COACH_ONLY_TOOLS,
    PROMPT_COACH_TOOLS,
    REASONING_EFFORTS,
    STAFF_BLOCKED_TOOLS,
    STAFF_ONLY_TOOLS,
    SUBAGENT_BLOCKED_TOOLS,
    SUBAGENT_ONLY_TOOLS,
    NoModelConfigured,
    RuntimeConfig,
    Settings,
    compaction_ratio,
    max_advertised_tools_for,
    on_demand_tool_groups_for,
)
from daedalus.host import (
    capabilities,
    launcher_bridge,
    prompt_coach,
    prompts,
    standing,
    summaries,
    summary_calls,
    tool_groups,
    user_style,
)
from daedalus.host.btw import BTW_KEY, btw_of, btws_of
from daedalus.host.checkpoint_retention import CheckpointRetention, RetentionBounds, RetentionReport
from daedalus.host.checkpoints import CheckpointError, Checkpoints, scan_workspace, store_dir
from daedalus.host.containment import Walls, walls_for, worktree_writable_paths
from daedalus.host.deliverables import locate as locate_deliverable
from daedalus.host.deliverables import secret_part
from daedalus.host.engine_factory import TENANT, EngineDeps, PolicyAdapter, build_engine
from daedalus.host.events import AppEvent, EventBus, EventFilter
from daedalus.host.foreign_sessions import harness_name
from daedalus.host.handoff import Handoff
from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.host_exec import BRIDGE_DOWN, HostExecBackend
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.policy import ASK, Decision, Policy, Rule, canonical, similar_covers, similar_grant
from daedalus.host.presence import Presence
from daedalus.host.request_manifests import RequestManifestStore
from daedalus.host.rules_gate import RulesGate
from daedalus.host.run_outcome import outcome_message, run_outcome
from daedalus.host.services import SessionServices, locator, session_scratch_dir
from daedalus.host.setting_refs import (
    COMPACTION_MODEL,
    RUN_SPEND_CAP,
    SPEND_CAP,
    SettingProblem,
    SettingRef,
    preset_images,
    provider_spend_cap,
)
from daedalus.host.skills import DirectorySkillStore, ScopedSkillStore
from daedalus.host.transcript_view import TranscriptViewBuilder, message_view
from daedalus.host.worktrees import append_exclude, exclude_lines
from daedalus.mcp.manager import McpManager, McpStillStarting, blocked_for, mcp_tool_prefix
from daedalus.mcp.oauth import mcp_callback_url
from daedalus.memory import privacy
from daedalus.memory import render as memory_render
from daedalus.memory import scope as memory_scope
from daedalus.memory.entries import MemoryEntries
from daedalus.memory.tools import build_memory_tools
from daedalus.processes import end_tree
from daedalus.providers.chain import build_chain
from daedalus.providers.free_catalog import approved_endpoint
from daedalus.providers.free_catalog import catalog as free_catalog
from daedalus.providers.openai_compat import CACHE_ROUTING
from daedalus.providers.registry import ProviderRegistry
from daedalus.security import operator_secrets, redact
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.dispatches import DispatchStore
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.files import MAIN as MAIN_FILES
from daedalus.stores.files import FileRefused, FileStore
from daedalus.stores.inference_budget import InferenceBudget
from daedalus.stores.knowledge import KnowledgeStore, project_facts_note
from daedalus.stores.media import MediaStore
from daedalus.stores.persistent import PersistentMemory, PersistentWorkspace
from daedalus.stores.project_environment import EnvironmentStore
from daedalus.stores.projects import FolderSpec, Project, ProjectFolder, ProjectSettings, ProjectStore
from daedalus.stores.provider_holds import (
    PROVIDER_RESUME_NOTE,
    pin_resumed_run_in,
    pinned_target_in,
    recovery_candidate_in,
    resting_providers_in,
    resume_target_in,
)
from daedalus.stores.purge import Purge
from daedalus.stores.sqlite import (
    LENT_KEY,
    LiveControlStore,
    SqliteEventStream,
    SqliteRunStore,
    SqliteSessionStore,
    SqliteUsageSink,
    message_text,
)
from daedalus.stores.staff import AsksStore, StaffStore
from daedalus.stores.update_drains import UpdateDrainActive, UpdateDrainPaused, assert_admission_open_in
from daedalus.terminals import endpoint as terminal_endpoint
from daedalus.terminals.bridge import HostBridge
from daedalus.tools import TOOL_GROUPS, discover_tools
from daedalus.tools.dispatcher import build as build_dispatcher_tools
from daedalus.tools.shell import RemoteJob, stop_job
from daedalus.tools.shell import outcome as shell_outcome
from daedalus.tools.shell import title as shell_title

logger = logging.getLogger(__name__)

TOOL_RULES_KEY_FILE = "tool-rules-mark.key"
"""Beside the operator's sealing key in the secrets directory, which no command the agent runs can read."""
COMPUTER_LIVE_SECONDS = 3.0
"""How long the answer to "is the desktop daemon up" is reused by the tool policy."""

PROVIDER_OUTAGE_KINDS = frozenset({"llm_provider_error", "llm_timeout", "llm_stream_idle", "llm_rate_limit"})
"""Terminal error kinds that mean the endpoint, not the request, failed: the run is driven again once the wait is over."""
RECOVERY_REASONS = frozenset({"transient_llm_error_retry", "model_fallback_triggered", "soft_stop_notified", "llm_context_window_exceeded", "context_window_recovered", "reasoning_length_cut_retry", "continue_prompt_injected", "max_output_token_recovery"})
"""The state changes worth a log line: each is a round the run had to recover from, and the log is where the reason survives."""
MODEL_METADATA_KEY = "daedalus.model"

BACKGROUND_SHUTDOWN_SECONDS = 5.0
"""How long a shutdown waits for a cancelled background task — the price refresh, the index
backfill — before leaving it. Neither owes anything to disk; what they can owe is a socket."""

MODEL_STAMPS_KEPT = 512
"""How many un-persisted turn stamps a session holds at once. A run writes its rounds as they finish,
so the map is normally one or two entries deep; the cap is only there so a store that is refusing
every write cannot turn it into a leak."""
"""Message metadata naming what produced an assistant turn: provider, model, the configured model, and the fallback if it was one."""
FALLBACK_REASONS = {
    "llm_rate_limit": "rate_limit",
    "llm_timeout": "outage",
    "llm_stream_idle": "stalled",
    "llm_provider_error": "outage",
}
"""The core's error class behind a demotion, as the one word the app and the operator read.

A silent stream is not an outage: the provider answered and then said nothing for longer than the
budget, which a model thinking without streaming its reasoning does. The core retries the first
silence on the same model, so a demotion for it means the silence repeated, and the word says so
rather than claiming the provider was unreachable."""
BRIEF_MAX_CHARS = 12_000
"""A spawned agent's brief lives in its system prompt; longer hand-overs belong in files."""
WORKSPACE_NOTES_CHARS = 6000
"""The least room the workspace's notes files get, on a window too small for their share."""
WORKSPACE_NOTES_WINDOW_SHARE = 0.04
"""The share of the window the workspace's AGENTS.md, CLAUDE.md and memory index may take: 40k tokens on a million."""
MEMORY_CLASSIFY_TIMEOUT_SECONDS = 15
"""A classifier slower than this leaves Remember to the word lists rather than holding the turn."""
MEMORY_INDEX = ".agents/memory/MEMORY.md"
"""The project's memory index, where agents of every program working in the folder keep their notes."""
MCP_EXAMPLE_NAMES = 5
"""How many of a large MCP server's tool names McpList and McpEnable show beside its prefix and count:
enough to show what the server covers and how its names are spelt, not so many that the list is back."""
GRANT_TTL_SECONDS = 2 * 3600

STEER_CARD_CHARS = 200
"""How much of a queued steer travels with a change event. The app draws a card, not the message."""

STEER_CARD_LIMIT = 20
"""Cards one change event carries. Past this the count is the answer; nobody reads the twenty-first card."""
"""How long an approval key stays spendable: long enough for the agent to retry, short enough that a forgotten grant does not wait for a later call."""

EventSink = Callable[[str, Any], Awaitable[None]]
"""A session's listener. It takes the loop's ``TurnEvent`` and the host's own ``HostEvent`` alike: both carry ``type``, ``run_id`` and ``payload``."""
RunFinished = Callable[[str, str, str], Awaitable[None]]  # session_id, run_id, status


class HostEventType(StrEnum):
    """Event kinds the host raises beside the core's turn taxonomy."""

    STEER_CHANGED = "steer_changed"
    HISTORY_CUT = "history_cut"
    SETTING_NOTICE = "setting_notice"
    """An auxiliary model was not set or refused: what was done instead, and the setting that fixes it."""
    COMPACTION_BACKGROUND = "compaction_background"
    """A summary being written ahead of the trigger: its progress, then ``None`` when it is ready or gone.
    Not a state change: the session goes on running or waiting for the operator meanwhile."""


@dataclass(slots=True)
class HostEvent:
    """One of those, shaped like a ``TurnEvent`` so the same sinks carry it.

    It travels to the session's listeners — the app's stream — and no further: it is not a run
    event, so it is never written to the durable event log. Its ``type`` is deliberately outside
    ``EventType``, so a sink that switches on the core's taxonomy falls through it untouched and
    only a sink that reads the name off the wire (the stream does) sees it at all.
    """

    type: HostEventType
    run_id: str
    payload: dict[str, Any]


@dataclass(slots=True)
class PreparedCompaction:
    """A summary of the history as it stood, written without the session lock, to be used when the trigger is reached.

    A compaction between turns made the operator wait as long as the summariser took: eight minutes on
    a 980k-token session. The summary of the history that exists at a share of the trigger is written
    in the background instead, while turns go on, and when the trigger is reached it is adopted with
    every message that came after it kept as it is — only if the history still begins with exactly the
    messages it summarised and nothing rewrote it meanwhile (a revert, a clear, a manual or the core's
    own compaction); otherwise it is dropped. The shape is the core's between-run compaction
    (``compact_session`` prepares without a lock, ``commit_session_compaction`` applies it while the
    history still starts with its source), so the host can move onto that seam when it runs that core.
    """

    keys: list[str]
    """Transcript keys of the summarised prefix, in order."""
    gen: int
    """``persist_gen`` when the prefix was taken; every history rewrite bumps it."""
    tokens: int
    """The measured prompt when it started, for the log."""
    task: asyncio.Task[str]

    def applies_to(self, history: Sequence[Message], gen: int) -> bool:
        if gen != self.gen or len(history) < len(self.keys):
            return False
        return [SqliteSessionStore.transcript_key(m) for m in history[: len(self.keys)]] == self.keys

    def summary(self) -> str | None:
        """The finished summary, or ``None`` while it is written, after it failed, or once it was cancelled."""
        if not self.task.done() or self.task.cancelled() or self.task.exception() is not None:
            return None
        return self.task.result()

    def drop(self) -> None:
        if not self.task.done():
            self.task.cancel()


@dataclass(slots=True)
class Attachment:
    path: Path
    mime_type: str = "application/octet-stream"
    caption: str | None = None
    name: str | None = None
    content_sha256: str | None = None
    stored_name: str | None = None
    image_ref: str | None = None
    """The blob an image was kept as, once ingested: what the model is shown alongside the words."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _set_event() -> asyncio.Event:
    """An event that starts raised: a session with no run behind it has nothing to wait for."""
    event = asyncio.Event()
    event.set()
    return event


@dataclass(slots=True)
class PendingQuestion:
    session_id: str
    run_id: str
    tool_call_id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunBounds:
    """What bounds one turn of an orchestration role (see :meth:`SessionManager.run_bounds`)."""

    max_iterations: int
    usd_per_run: float | None
    """``None`` is ``limits.usd_per_run``."""
    setting: str
    """The setting the spend cap comes from, named in the note that stops a run at it."""


@dataclass(slots=True)
class SessionState:
    session: Session
    workspace: Path
    engine: QueryEngine | None = None
    provider_hold: ExitStack | None = None
    task: asyncio.Task[None] | None = None
    run_id: str | None = None
    pending: PendingQuestion | None = None
    services: SessionServices | None = None
    context_window: int | None = None
    """Per-session override; ``None`` follows the configured model window."""
    extra_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "chat"
    """What the conversation is for (``sessions.kind``): ``chat``, ``computer`` or ``chrome``. Read when
    the state loads and changed only through :meth:`SessionManager.set_session_kind`, which writes both."""
    run_origin: str = "operator"
    """Who started the current run (``operator``, ``schedule``, ``reminder``, ``subagent:…``): StaySilent and the reply routing read it."""
    last_error_kind: str = ""
    """The kind of the error that ended the current run, from the core's ERROR event (``llm_context_window_exceeded`` …)."""
    last_error_message: str = ""
    """The text of that error, masked, as the fronts were shown it: what the inbox entry quotes."""
    last_error_permanent: bool = False
    """The provider's verdict on that error said asking again cannot help (a refused request, key or model)."""
    run_outcome: RunOutcome | None = None
    """How the core says the current run ended, from its terminal ``message_stop``; ``None`` until it has."""
    run_cause: RunCause | None = None
    """Why the current run did not simply complete, from the same event: the bound that wound it down or the failure
    that ended it, with the provider's verdict and its own words (masked). The operator is shown this, not an inference
    from the reply: a run the provider refused ends with the model writing a closing message, and without this the
    message is the only trace of a failure that produced nothing."""
    last_compaction: dict[str, Any] | None = None
    """The current run's latest ``compaction_completed`` payload: when the run dies of size, what says which tier gave out."""
    last_outcome: dict[str, Any] | None = None
    """How the last settled run ended when it produced no answer (see ``run_outcome``); ``None`` when it answered."""
    overflow_streak: int = 0
    """Consecutive runs that overflowed the context window; recovery stops after a few so a hopeless history cannot loop."""
    outage_streak: int = 0
    """Consecutive runs the model provider failed; each one waits longer before the work is driven again."""
    outage_task: asyncio.Task[None] | None = None
    """The wait before the next attempt after a provider failure, so a session sleeps at most once."""
    housekeeping: asyncio.Task[None] | None = None
    """What the last run left to do after its answer was on the screen: the snapshot, the run-finished
    callbacks, the queue it drains, the compaction check. The run itself is over while this is going —
    that is the point of it — so the app draws an idle session and the work goes on behind it."""
    settled: asyncio.Event = field(default_factory=lambda: _set_event())
    """Lowered when a run starts, raised again once its history is persisted and its snapshot taken.
    The next run waits for it: the files a revert would restore must describe the turn that just ended,
    not the one starting. The rest of the housekeeping nobody waits for."""
    environment_scope: tuple[frozenset[str] | None, frozenset[str] | None] = (None, None)
    """The MCP servers and the skills the project's environment lets this session see, ``None`` for
    all of them. Read from the store when a run's engine is assembled and left alone until the next
    one: the tools and the skill catalogue are part of the prompt prefix the provider caches, and an
    operator editing the environment mid-run would otherwise invalidate it for the rest of the run."""
    standing: tuple[str, list[dict[str, Any]]] = ("", [])
    """The carried instructions and memory of an imported session, as read from their blob (the blob's
    reference, then the files); see :mod:`daedalus.host.standing`."""
    compacting: dict[str, Any] | None = None
    """A compaction in flight: reason, stage (summarising/merging/writing), parts done of total, started_at.
    The Mini App and the list read it; ``None`` when none is running."""
    auto_compaction: asyncio.Task[None] | None = None
    """The compaction check a settled run left behind. Deleting the session cancels it: a summary finished
    after the rows are gone writes into a session that no longer exists."""
    prepared_compaction: PreparedCompaction | None = None
    """The summary of a history prefix being written, or written, ahead of the trigger; see
    :class:`PreparedCompaction`. ``None`` when none was started or the last one was used or dropped."""
    background_compacting: dict[str, Any] | None = None
    """That summary's progress while it is written, in the shape of ``compacting`` with ``background`` set."""
    compaction_hold: dict[str, Any] | None = None
    """Why the automatic compaction stands down, and until when: the prompt size it last acted on, how
    many attempts in a row came to nothing, and the monotonic time after which it may try regardless.
    Set by a compaction that failed or freed almost nothing; cleared by one that worked."""
    run_active_since: float = 0.0
    """Monotonic time the current run (re)started driving the model: the time cap counts from here, not from
    the run's creation, so an hour waiting on the operator's answer is not an hour of run time."""
    tool_starts: dict[str, tuple[float, str]] = field(default_factory=dict)
    """Tool calls in flight: ``call_id -> (monotonic start, tool name)``, for the timing rows."""
    tool_group_surface: dict[str, str] = field(default_factory=dict)
    """Each group's state as this process's last ``tool_surface_advertised`` for the session said:
    ``advertised``, ``deferred`` or ``loaded``. Written into the metadata when the run ends, which is
    what the panel reads after a restart."""
    tool_groups_loaded: dict[str, set[str]] = field(default_factory=dict)
    """Groups the current run loaded (``tool_group_loaded``) and which of their tools, until the next
    run starts. The tools, because a search that loaded one browser tool has not loaded the browser."""
    on_demand_groups: bool = True
    """Whether the model this session runs on holds the on-demand groups back: set when a run's engine is
    built and when the panel is read (:meth:`SessionManager.refresh_on_demand_groups`)."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises run starts against history rewrites (compaction)."""
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises the start-or-queue decision: two inputs arriving together (an operator message and a
    loop tick, say) must become one run plus one steer, never two runs driving one history."""
    history_keys: list[str] = field(default_factory=list)
    """Transcript keys of the working history at the last persist, to see what a compaction removed."""
    compacting_keys: list[str] | None = None
    """Transcript keys of the working history as a compaction in the middle of a run found it: the
    turns its summary will stand for are the ones missing afterwards. ``None`` outside a compaction."""
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises history persistence so an older snapshot can never overwrite a newer one."""
    persist_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    persist_chain: asyncio.Task[None] | None = None
    """The persist queued last. Each hand-over waits for it before writing, so the writes of a session
    land in the order the rounds happened however the loop schedules the tasks."""
    persist_rewrite: bool = False
    """Set when a write did not land and nobody has rewritten the history since: there is a hole in the
    stored history, and the next persist writes it whole instead of appending onto rows that are not there."""
    persist_epoch: int = 0
    """Bumped by every failed write. A hand-over queued before the bump describes a history the store no
    longer matches, so it is written whole rather than appended, however late it runs."""
    persist_repair: asyncio.Task[None] | None = None
    """The rewrite scheduled by a failed write, so a store that is down is retried once and not in a loop."""
    persist_gen: int = 0
    """Bumped by every history rewrite (manual compaction); a persist captured before the bump is dropped."""
    configured_model: str = ""
    """``provider:model`` the session is set to answer with: the first rung of the chain this run was built on.
    A live override (a preset chosen for the session, the companion pointing at its own) is part of it —
    what the operator chose is never a fallback, however far it is from the global default."""
    computer_sight: tuple[str, bool] | None = None
    """The model this run was built on and whether it takes images: what decides whether the computer
    tools are offered and how their screenshots are sized and their points read. ``None`` until a run
    was built; the tools are not offered before."""
    effective_model: str = ""
    """``provider:model`` that actually answered last, as the core reported it at the message it started."""
    model_change_reason: str = ""
    """Why the next change of model happened, taken from the core's own account of the demotion; empty means nobody said."""
    model_stamps: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Which model produced each assistant turn, recorded the moment the turn ended and keyed by its
    transcript key. Persists are fire-and-forget, so a turn is very often written after the chain has
    already stepped down; reading the model at write time therefore names whoever is answering *now*.
    An entry is taken out of the map when the turn it describes is stamped."""
    run_history_start: int = 0
    """Length of the working history when the current run began: what this run added starts here."""
    checkpoint_capped: bool = False
    """Set once the size cap has suppressed a snapshot, so the warning is logged once per session."""
    project: Project | None = None
    """The project this session works in: its root is the workspace, and the only place the session's
    tools may reach. ``None`` is a session with a directory of its own."""
    observed_prompt_tokens: int = 0
    """The host's own estimate of the next prompt after it rewrote the history, until a real call
    measures one. A compaction makes every earlier measurement describe a history that is gone."""
    usage_floor_seq: int = 0
    """Usage rows up to here were recorded before the last history rewrite; reading one of them as
    the current prompt size is what made a compaction fire again on the very next turn."""
    steer_seen: list[str] = field(default_factory=list)
    """Ids of the steers the running engine was handed at its last reload, in order. What is written
    back is judged against these: an id that was never handed over was enqueued mid-round and is
    still waiting, and an id that was handed over and not returned is one the model has read."""
    follow_up_seen: list[str] = field(default_factory=list)
    """The same for the follow-up queue."""
    queue_withdrawn: set[str] = field(default_factory=set)
    """Ids taken back since that reload, from either queue. A reload already waiting on the database
    returns the row as it was before the removal, so it is filtered through this on the way into the
    engine; cleared at the next persist, by which time the store and the engine agree."""
    follow_up_steered: set[str] = field(default_factory=set)
    """Follow-ups the operator turned into steers since that reload. The same stale reload would hand
    the item back to the engine's follow-up queue while the store already holds it as a steer, and the
    message would then reach the model twice; and its leaving the follow-up queue is not consumption."""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


def _status_word(state: SessionState) -> str:
    """What the session is doing, in the one word the lists and the ``session.status`` event share."""
    if state.running:
        return "running"
    if state.pending is not None:
        return "waiting"
    if state.compacting is not None:
        return "compacting"
    if state.last_error_kind or (state.engine is not None and state.engine.state is LoopState.FAILED):
        return "failed"
    return "idle"


OPERATOR_ORIGINS = ("operator", "core")
"""Run origins that answer the operator: their own message, or the host continuing their work after
an outage. A schedule, a heartbeat, a loop or a subagent's task answers nobody who is waiting."""

NOT_OPERATOR_FACING = ("subagent_of", "unattended", "heartbeat", "staff_id", prompt_coach.COACH_KEY, BTW_KEY)
"""Session metadata that marks a session nobody reads as a conversation, whatever started the run. A prompt
coach and a side question are read, but only in their window over their chat: an unread dot or a "finished"
notice would point at a session no list shows."""

SUMMARY_CHARS = 200
"""How much of the answer a finished-run event carries: enough for a notification's line, not the answer."""


HOME_KEY = "home"
"""Session metadata naming a directory of its own under the workspaces root, which it runs in
instead of a project folder: a project orchestrator whose folders all lie where this process cannot
reach (a host folder, seen from the agent's container). It reads those folders through the host
bridge; its working directory has to be somewhere a run can start."""
_HOME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def home_of(metadata: dict[str, Any]) -> str:
    """The session's own directory name under the workspaces root, or ``""`` when it works in a folder."""
    value = str(metadata.get(HOME_KEY) or "").strip()
    return value if _HOME_RE.fullmatch(value) and value not in (".", "..") else ""


FORK_IDENTITY_KEY = "fork_identity"
"""What a fork's system prompt is written from, taken from its source when it was made.

A fork made to try the source's work again elsewhere (an exploration's attempt in a worktree of its
own) has a title, a working directory and a brief of its own, each of which the system prompt
names. Written from those, its first request differed from the source's in the system prompt and the
provider served none of the shared history from cache. Its prompt is written from the source's
instead, and what differs is said in the fork's first message, after the shared prefix."""

FORK_COPIED_KEYS = ("discovered_tools", tool_groups.CARRIED_KEY)
"""The source's loaded tools, which decide the tool list the cached prefix starts with."""


def fork_identity(metadata: dict[str, Any]) -> dict[str, Any] | None:
    pinned = metadata.get(FORK_IDENTITY_KEY)
    if not isinstance(pinned, dict) or not all(isinstance(pinned.get(k), str) for k in ("title", "workspace", "notes")):
        return None
    return pinned


def host_home_name(session_id: str) -> str:
    """The directory under the workspaces root that a session working in a host folder runs from.

    Its commands and file tools work on the host, but a run still needs a directory of this process
    to start in, keep its snapshots and receive attachments before they are passed on. Derived from
    the session rather than stored, so a session moved into a host project, or loaded after a restart,
    has it without a migration of its metadata.
    """
    return "host-" + re.sub(r"[^A-Za-z0-9_-]", "_", session_id)


HOST_CHATS = "${XDG_STATE_HOME:-$HOME/.local/state}/daedalus/chats"
"""Where a chat started on the host from the container works: ``<this>/<session id>`` on the host,
as the host's shell expands it.

A chat that names no project gets a scratch folder, and on the host that folder has to be somewhere
the operator did not choose. The state directory rather than a visible folder in the home: the
folder is as disposable as the container's scratch projects, it goes with the chat, and a directory
appearing in the home for every chat would be clutter the operator never asked for. Beside the
terminal daemon's own ``daedalus-ptyd`` but not inside it, since the daemon seals its state
directory against every file read. A chat whose files matter is kept as a project, which keeps its
folder too."""


def host_chat_path(path: Path, session_id: str) -> bool:
    """Whether ``path`` is the host scratch folder :data:`HOST_CHATS` makes for this session: the one
    host path this process ever deletes, so the test is of the exact shape and nothing looser."""
    return path.is_absolute() and path.name == session_id and path.parent.name == "chats" and path.parent.parent.name == "daedalus"


class HostChatRefused(ValueError):
    """A chat cannot be started on the host here: the installation already runs there (natively),
    or the chat named a project, whose folder decides where it runs."""


class HostUnreachable(ValueError):
    """A session cannot be made in a host folder now: the host terminal daemon does not answer.

    A ``ValueError``, so every caller that already turns a refused ``create_session`` into a message
    for the operator says this one too; the text names the fix."""


class CompactionFailed(SettingProblem):
    """Every model a compaction may use refused to summarise; the message names each and why, so the
    operator reads a quota or a refusal instead of "Server unavailable (500)", and the summary model's
    row is offered as the place to fix it."""

    def __init__(self, message: str, setting: SettingRef = COMPACTION_MODEL) -> None:
        super().__init__(message, setting)


class FolderMoving(RuntimeError):
    """A run cannot start: the session's folder is being moved between the container and the host.
    A ``RuntimeError``, so every caller that turns "this run cannot start" into a message says it."""


class WorkspaceUnreachable(RuntimeError):
    """The session's working directory is not there (or not writable) and nothing here can make it.

    A run refused for this will be refused the same way until someone mounts, restores or opens the
    folder, so a caller that retries on its own (a wake-up queue) must say so and slow down rather
    than try again every few seconds. A ``RuntimeError``, so every caller that already turns "this run
    cannot start" into a message needs nothing new."""


def _ensure_inbox(workspace: Path, folder: ProjectFolder | None) -> None:
    """Make the session's inbox — but never make a project folder that is not there.

    ``mkdir(parents=True)`` on an unmounted root creates the whole path, and the mount point is then
    a local empty folder that answers ``reachable`` for ever after: the "not mounted — mount it and
    restart" signal the whole Docker story rests on would be erased by loading the session. On a
    removable or network mount it is worse than that, because the real folder is shadowed by the
    empty one when it comes back. A folder the operator added is theirs to create.
    """
    if folder is not None and (folder.readonly or not folder.reachable):
        # A read-only folder is not ours to write either: no inbox, no note in its git exclude.
        return
    (workspace / "inbox").mkdir(parents=True, exist_ok=True)
    if folder is not None:
        _exclude_artefacts(workspace)


def _waiting(queued: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Both queues as the operator reads them, steers first: they reach the model sooner."""
    return [(kind, item) for kind in ("steer", "follow_up") for item in queued[kind] if str(item.get("text") or "").strip()]


def _steer_cards(items: Sequence[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The waiting messages as the composer draws them: a recognisable amount of each, and not all of them.

    ``kind`` says which queue holds the card, because a follow-up offers to become a steer and a
    steer has nothing sooner left to become."""
    cards: list[dict[str, Any]] = []
    for kind, item in items:
        text = str(item.get("text") or "")
        if len(cards) >= STEER_CARD_LIMIT:
            break
        files = [str(entry.get("name") or "") for entry in item.get("attachments") or [] if isinstance(entry, dict)]
        if files and "caption" in item:
            text = str(item.get("caption") or "")
        card: dict[str, Any] = {"id": str(item.get("id") or ""), "kind": kind, "text": text[:STEER_CARD_CHARS], "queued_at": item.get("queued_at")}
        if len(text) > STEER_CARD_CHARS:
            card["truncated"] = True
        if files:
            # The card says the message carries files, so the operator does not send the screenshot twice.
            card["files"] = files
        cards.append(card)
    return cards


def _exclude_artefacts(root: Path) -> None:
    """Keep the agent's own directories out of the operator's git status.

    ``.git/info/exclude`` rather than ``.gitignore``: the ignore file is the repository's and is
    committed, and a project is somebody else's repository — this is our note to their checkout, not
    a change to their project. A root that is not a git repository has nothing to write and nothing
    to worry about.
    """
    if not (root / ".git").is_dir():
        return
    path = root / ".git" / "info" / "exclude"
    try:
        append_exclude(path, exclude_lines())
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)


TITLE_PROMPT = (
    "Write a short title for a chat that opens with the message below. "
    "Use the same language as the message. Six words at most. "
    "No quotes and no trailing punctuation.\n\nMessage:\n"
)


def clip_title(text: str) -> str:
    """A title from the message itself, used until a model has named the chat.

    A trailing period is not part of a title. A message that is only punctuation keeps those
    characters: stripping them would leave the chat with nothing to be called.
    """
    line = " ".join(text.strip().split())
    if not line:
        return ""
    short = line if len(line) <= 48 else (line[:48].rsplit(" ", 1)[0] or line[:48])
    return short.rstrip(".!…") or line[:48]


def _clean_title(raw: str) -> str:
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = line.strip(" \"'`“”«»").rstrip(".!…")
    return " ".join(line.split())[:80]


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        *,
        db: Database,
        governance_path: Path | None = None,
        execution_store: ExecutionStore | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.db = db
        self.execution_store = execution_store
        self.blobs = FileBlobStore(settings.blobs_dir)
        self.media = MediaStore(db, self.blobs)
        self.sessions = SqliteSessionStore(db, view=TranscriptViewBuilder(), media=self.media)
        self.runs = SqliteRunStore(db)
        self.events = SqliteEventStream(db)
        self.usage = SqliteUsageSink(db)
        self.live = LiveControlStore(db)
        # The installation's own folders are not projects, and neither is the whole home folder:
        # a project folder is reachable to every agent in it and is an open root to the policy.
        self.projects = ProjectStore(
            db,
            managed_root=settings.workspaces_dir,
            reserved=[Path(p) for p in settings.sandbox_never_writable] + [settings.secrets_dir, settings.workspaces_dir],
            home=Path.home(),
            local_env="host" if settings.native else "container",
        )
        personas = settings.bot_repo_dir / "personas"
        # Read through the live configuration, not a copy: a preset added in Settings is a model a
        # staff member may be given at once, without a restart.
        self.staff = StaffStore(
            db,
            local_env=self.projects.local_env,
            config=lambda: self.config.staff,
            presets=lambda: self.config.presets.keys(),
            personas=lambda: [p.stem for p in personas.glob("*.md")] if personas.is_dir() else [],
        )
        self.asks = AsksStore(db)
        self.files = FileStore(db, self.blobs)
        """Files handed between the operator, the orchestrators and staff, by handle (``att:<id>``)."""
        self.dispatches = DispatchStore(db)
        """The main orchestrator's hand-overs to projects: what a project orchestrator's state lists
        and what its reports close."""
        self.checkpoint_retention = CheckpointRetention(db, workspaces_dir=settings.workspaces_dir, checkpoints_dir=settings.checkpoints_dir, busy=self.busy_sessions, occupants=self.store_occupants)
        self.memory = PersistentMemory(db)
        self.memory_entries = MemoryEntries(db, self.memory, TENANT)
        """The titled pages memory is read in; every new record comes through its ``remember``."""
        self.workspace_units = PersistentWorkspace(db)
        self.skills = DirectorySkillStore(settings.skills_dir)
        self.redactor = redact.shared()
        self.capabilities = capabilities.resolve(settings, config)
        """What this installation can do, decided once: the tools, the routes, the prompt and the
        app all read the same answer, and a configuration change reaches them on the next start."""
        capabilities.publish(self.capabilities, settings.state_dir)
        """And the supervisor reads it from there rather than resolving it a second time."""
        self._configure_redactor(settings, config)
        self.secrets = operator_secrets.OperatorSecrets(
            db, settings.secrets_dir / operator_secrets.KEY_FILE, self.redactor, files_dir=settings.state_dir.parent / "operator-secrets",
        )
        """The values the operator handed the agent to use without reading. Opened at start; the redactor
        learns them there, and the files commands read are written beside the state directory, not in it,
        because the policy refuses every command that names the sealed state directory."""
        self.secrets.project_of = self._project_of
        self.secrets.staff_of = self._staff_of
        self._tool_rules_key: bytes | None = None
        self._computer_live: tuple[float, bool] = (0.0, False)
        """When the desktop daemon's endpoint was last looked at, and whether it was there."""
        self.hooks = DaedalusHookManager(self.redactor, hooks_config=lambda: self.config.hooks)
        self.action_rules = RulesGate(self)
        """The operator's action rules over this host's sessions: levels in the policy gate, the review in the hook."""
        self.hooks.reviewer = self.action_rules.pre_tool
        self.bus = EventBus(db, redactor=self.redactor, queue_size=config.ops.event_subscriber_queue, replay_max=config.ops.event_replay_max)
        """What happens to sessions, terminals and staff, for whoever subscribes: the app's stream,
        the notifications, an orchestrator. Persisted before it is delivered, so a subscriber resumes
        from its cursor after a restart."""
        self.presence = Presence(self.bus, db, ttl_seconds=config.ops.presence_ttl_seconds, grace_seconds=config.ops.presence_grace_seconds)
        """What the operator's windows show: whether a finished run was watched, and when an unread
        result has been seen."""
        self._background: set[asyncio.Task[Any]] = set()
        self.jobs: dict[str, dict[str, Any]] = {}
        """Each session's background jobs by id, shared with its tools; :mod:`daedalus.extensions.jobs`
        watches them and wakes the session when one ends."""
        self.moving_folders: dict[str, Path] = {}
        """The project folders being moved now (:mod:`daedalus.host.folder_move`), by folder id, with
        where they are being copied to. No run starts in one, and the copy's directory is not an
        orphan for the sweep while it is being filled."""
        self.tools = ToolRegistry()
        """Every tool but the main orchestrator's, MCP proxies included; a session sees the part its policy admits."""
        self.dispatcher_tools = ToolRegistry()
        """The main orchestrator's tools and the few shared ones it may call, and nothing else."""
        self.providers = ProviderRegistry(
            settings, config, usage_sink=self.usage, image_loader=self._load_image,
            admission=HostInferenceAdmission(self),
        )
        self.mcp = McpManager(config.mcp.servers, self.tools, token_dir=settings.state_dir / "mcp",
                              redirect_default=lambda: mcp_callback_url(settings.miniapp_public_url))
        # A server's catalogue can change under running sessions — a re-listing, a reconnect, another
        # session enabling it. Each session refuses the proxies of servers it did not enable by name, so
        # every session's policy is recomputed at once, not at the next thing that happens to refresh it.
        self.mcp.catalog_listeners.append(lambda server: self._apply_tool_visibility_all())
        self.request_manifests = RequestManifestStore(db, self.blobs, tenant_id=TENANT)
        self._group_usage = tool_groups.UsageCache()
        self.governance_path = governance_path or (settings.bot_repo_dir / "GOVERNANCE.md")
        self._states: dict[str, SessionState] = {}
        # One load per session at a time: see ``get_state``.
        self._loading: dict[str, asyncio.Task[SessionState | None]] = {}
        self._sinks: list[EventSink] = []
        self._finished: list[RunFinished] = []
        self.compaction_hooks: list[Callable[[str, dict[str, Any]], Awaitable[None]]] = []
        """Called after an automatic compaction with what changed, so the chat can say so in one line."""
        self._pending_restored: list[Callable[[str, PendingQuestion], Awaitable[None]]] = []
        self.service_hooks: dict[str, Any] = {}
        self.host_bridge: HostBridge | None = None
        """The host through its terminal daemon, set when the terminals are installed: what a session
        working in a host folder runs its commands and file tools through."""
        self.delete_hooks: list[Callable[[str], Awaitable[None]]] = []
        """Called with the session id before a session is removed (extensions release what they hold for it)."""
        self.project_delete_hooks: list[Callable[[str], Awaitable[None]]] = []
        """Called with the project id before a project is forgotten, for what an extension holds for the
        project itself rather than for one of its sessions (its terminals)."""
        """Callbacks the transport layer installs: send_file, spawn_agent, schedule, self_*."""
        self.prompt_hooks: list[Callable[[str, str], Awaitable[str]]] = []
        """``(session_id, text) -> text`` applied to a message that starts a new run (fired reminders ride along)."""
        self.idle_work = asyncio.Lock()
        self.answer_claims: list[Callable[[str, str, str], Awaitable[str | None]]] = []
        """``(session_id, tool_call_id, via) -> refusal`` asked before a pending question is answered;
        a non-empty refusal (someone else answered first) stops the answer with that text."""
        self.turn_notes_hooks: list[Callable[[SessionState], Awaitable[str | None]]] = []
        self.turn_line_hooks: list[Callable[[SessionState], Awaitable[str]]] = []
        """Each ``state -> line`` adds one line of volatile fact to a run's turn context (``""`` for
        none): what an extension knows changes between runs and must stay out of the cached prompt."""
        self.answer_hooks: dict[str, Callable[[str, dict[str, Any], bool], Awaitable[None]]] = {}
        """``on_answer → (session_id, request, allowed)``: what the host itself carries out when the
        operator answers a request made with :meth:`ask_operator`. Such a request lets no call through:
        the yes is spent by the host's own action, so nothing the agent sends next can reach it."""
        self.steer_hooks: list[Callable[[str, str], Awaitable[str]]] = []
        """Each ``(session_id, text) -> text`` rewords an operator's message sent into a running turn,
        for the model only; the transcript keeps the words as sent."""
        """``state -> notes`` asked at every run start before the workspace's own notes; the first that
        answers (not ``None``) replaces them. A project's orchestrator reads its project's state here."""
        self.preset_hooks: list[Callable[[SessionState], str | None]] = []
        """``state -> preset id`` that a session runs whatever its live override says: an orchestrator
        runs the model its project names, which the operator changes on the project, not per chat."""
        self.run_started_hooks: list[Callable[[str, str], Awaitable[None]]] = []
        """``(session_id, run_id)`` after a run was actually created — the point where a prompt hook's side effects may be committed."""
        self.shutting_down = False
        self._update_parked_run_ids: set[str] = set()
        self.recovering = True
        """True from construction until boot recovery has decided the fate of every run the previous process left behind."""
        self.unloadable_sessions: dict[str, str] = {}
        """Sessions whose stored directory no longer resolves inside their project, by id, with the
        reason. ``get_state`` puts one here instead of raising through whoever asked for it; the entry
        is dropped the moment the session opens again, so what is here is what is broken now."""
        self.stale_runs: list[str] = []
        """Runs the previous process was driving that this one could not pick up — no snapshot to resume
        from, so the row is closed as cancelled. Empty on a clean stop; a line in the operator's inbox
        when it is not, because a run that simply vanishes is the thing they would otherwise go looking
        for in the logs."""
        self.index_rebuild: dict[str, int] | None = None
        """How far the search index still has to be rebuilt, while it is being rebuilt. A search that
        answers "nothing" from an index that has not reached those rows yet reads as "that was never
        said" — to the operator and to the agent asking it of its own history."""
        self.budget_flag = settings.state_dir / "BUDGET_EXCEEDED"
        self._capped_runs: set[str] = set()
        """Runs already stopped at the per-run cap (the stop is cooperative; the notice fires once)."""

    # -- lifecycle ------------------------------------------------------------------

    def _project_of(self, session_id: str) -> str | None:
        state = self._states.get(session_id)
        return state.project.id if state is not None and state.project is not None else None

    def _staff_of(self, session_id: str) -> str | None:
        state = self._states.get(session_id)
        if state is None:
            return None
        return str(state.metadata.get("staff_id") or "") or None

    async def start(self, *, recovering: bool | None = None) -> None:
        """Open the stores; ``recovering`` (default: whether a previous process left runs behind) gates new runs until resume_unfinished()."""
        await self.memory.load()
        await self.memory_entries.file_unfiled()
        await self.secrets.load()
        operator_secrets.install(self.secrets)
        await self.workspace_units.load()
        await self.bus.start()
        await self.presence.load()
        self.bus.on(EventFilter(types=("presence",)), self._on_presence, name="unread-result")
        # The policy is built inside a tool call and cannot wait on a query; this is where the project
        # roots it compares against are read — and where a folder of our own that is not on disk is
        # put back, so the first run after a start is not the thing that discovers it missing.
        await self.projects.ensure_roots()
        if interrupted := await InferenceBudget(self.db).recover_interrupted():
            logger.warning("%d provider call(s) were open when the last process stopped; their spend is unknown", interrupted)
        # New runs wait until resume_unfinished() has continued what the previous process left behind;
        # a process that finds nothing to resume (tests, a first start) opens the gate at once.
        self.recovering = recovering if recovering is not None else bool(await self.events.unfinished_snapshots())
        self.stale_runs = []
        # A tool this installation cannot honour is not registered at all: an unusable name in the
        # list is an invitation the model accepts and a failure it cannot understand.
        disabled = self.capabilities.selfdev.disabled_tools
        browser = self.capabilities.browser.configured
        computer = self.capabilities.computer.configured
        for tool in discover_tools():
            if tool.name in disabled or (not browser and tool.name.startswith(capabilities.BROWSER_TOOL_PREFIX)):
                continue
            if not computer and tool.name in capabilities.COMPUTER_TOOLS:
                continue
            self.tools.register(tool)
        memory_tools = build_memory_tools(self.memory, entries=self.memory_entries, tenant_allowed=lambda: self.config.memory.sensitive_allowed, classifier=self.memory_classifier)
        for tool in privacy.guard_memory_tools(list(memory_tools), self.is_private):
            self.tools.register(tool)
        for group, spec in TOOL_GROUPS.items():
            instructions = prompts.group_instructions(group, selfdev_mode=self.capabilities.selfdev.mode)
            self.tools.declare_group(group, spec.description, load=spec.load, instructions=instructions)
        # The core advertises it only while it holds a group back, so on an ordinary window it costs
        # nothing; without it registered the core holds nothing back at all, and a large MCP server
        # would put every one of its tools on the surface. The main orchestrator's registry has no
        # groups and a dozen tools, so it has nothing to find there.
        self.tools.register(ToolSearchTool(self.tools))
        for tool in build_dispatcher_tools():
            self.dispatcher_tools.register(tool)
        for tool in self.tools.list_all():
            if tool.name in DISPATCHER_TOOLS and tool.name not in {t.name for t in self.dispatcher_tools.list_all()}:
                self.dispatcher_tools.register(tool)
        self.service_hooks.setdefault("mcp", self.mcp_service)
        locator.default = None
        self.index_rebuild = None
        self._backfill_task = asyncio.create_task(self._backfill_index(), name="transcript-index")
        self._backfill_task.add_done_callback(_log_task_failure)
        # Its own task, not a background write: the flush that waits for those would wait for a day.
        self._price_task = asyncio.create_task(self._refresh_prices_daily(), name="price-refresh")
        self._price_task.add_done_callback(_log_task_failure)
        logger.warning("tools registered: %s", ", ".join(sorted(t.name for t in self.tools.list_all())))

    async def _backfill_index(self) -> None:
        def progress(done: int, total: int) -> None:
            self.index_rebuild = None if done >= total else {"done": done, "total": total}

        try:
            indexed = await self.sessions.backfill_transcript_index(progress)
        finally:
            self.index_rebuild = None
        if indexed:
            logger.warning("transcript search index: %d older turns indexed", indexed)

    async def close(self) -> None:
        """Shut down keeping every active run resumable (snapshots stay in place)."""
        self.shutting_down = True
        await self.secrets.settle()
        if operator_secrets.shared() is self.secrets:
            operator_secrets.install(None)
        # What a finished run still owes is a task of its own now, and a task is something a shutdown
        # can kill. It used to run inside the run, which had already ended by the time this looked at
        # it, so nothing was ever lost here. So it is given its moment before anything is cancelled:
        # the snapshot it is taking is the one an undo of that turn depends on, and the callbacks
        # behind it are an answer somebody is waiting for on another front.
        grace = self.config.ops.shutdown_grace_seconds
        housekeeping = [s.housekeeping for s in self._states.values() if s.housekeeping is not None and not s.housekeeping.done()]
        if housekeeping and grace > 0:
            _done, unfinished = await asyncio.wait(housekeeping, timeout=grace)
            if unfinished:
                logger.warning("%d run(s) were still being written down after %.0f s; the shutdown cancels them", len(unfinished), grace)
        tasks = [t for s in self._states.values() for t in (s.task, s.outage_task, s.housekeeping) if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending = [t for s in self._states.values() for t in s.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # the last history write must land
        # A summary written ahead owes nothing to disk and may have minutes left to write: the flush
        # below waits for every background task, so it is dropped first rather than waited for.
        for state in self._states.values():
            if state.prepared_compaction is not None:
                state.prepared_compaction.drop()
        await self.flush_background()
        await self.hooks.aclose()
        for task in (getattr(self, "_backfill_task", None), getattr(self, "_price_task", None)):
            if task is not None and not task.done():
                task.cancel()
                # Bounded, because a cancelled task is not a finished one: the price refresh is
                # inside an HTTP client when it is cancelled, and closing a connection whose peer has
                # gone waits for a FIN that never arrives. Neither of these owes anything to disk, so
                # a shutdown that gives up on one loses nothing and a shutdown that waits for one
                # stops the process from ever exiting.
                with suppress(TimeoutError):
                    async with asyncio.timeout(BACKGROUND_SHUTDOWN_SECONDS):
                        await asyncio.gather(task, return_exceptions=True)
        await self.mcp.close()
        await self.providers.aclose()
        await self.presence.close()
        # Last, once the runs are drained, so their final events are written; and it ends every open
        # event stream, which a shutdown would otherwise wait on.
        await self.bus.close()

    # -- what the bus is told --------------------------------------------------------

    def _operator_facing(self, state: SessionState) -> bool:
        if self._tells_news(state):
            return True
        return state.run_origin in OPERATOR_ORIGINS and not any(state.metadata.get(key) for key in NOT_OPERATOR_FACING)

    def _tells_news(self, state: SessionState) -> bool:
        """A run of the main orchestrator woken by a project's report: it speaks to the operator, who is
        usually not looking, so its answer is an unattended result like one they asked for."""
        return state.run_origin == "events" and bool(state.metadata.get("dispatcher"))

    def _telegram_delivers(self, state: SessionState) -> bool:
        """Whether the Telegram front shows this session's answers, questions and approvals itself.

        A subagent never speaks in Telegram (the front refuses it an outbox), so counting it as
        delivered there would hide its result from push and the unread mark as well.
        """
        metadata = state.metadata
        return bool(self.settings.telegram_bot_token) and not metadata.get("telegram_detached") and not metadata.get("subagent_of")

    @staticmethod
    def _event_ids(state: SessionState) -> dict[str, str | None]:
        # A staff member's session names the member too: whoever follows a project's team (the
        # orchestrator, the team page) filters on the member, not on sessions it would have to look up.
        staff_id = str(state.metadata.get("staff_id") or "") or None
        return {"session_id": state.session.id, "project_id": state.project.id if state.project is not None else None, "staff_id": staff_id}

    async def _publish(self, state: SessionState, event_type: str, payload: dict[str, Any]) -> AppEvent | None:
        """Tell the bus about this session. A bus that cannot write must not end a run, so a failure is logged."""
        try:
            return await self.bus.publish(event_type, payload, **self._event_ids(state))
        except Exception:  # noqa: BLE001 — the run is the operator's work; the event is a courtesy to subscribers
            logger.warning("could not publish %s for session %s", event_type, state.session.id, exc_info=True)
            return None

    def _publish_soon(self, state: SessionState, event_type: str, payload: dict[str, Any]) -> None:
        """The same from synchronous code (the policy gate), which cannot wait for the write."""
        try:
            self.bus.publish_soon(event_type, payload, **self._event_ids(state))
        except Exception:  # noqa: BLE001 — as above; a tool call is never refused because an event was not
            logger.warning("could not queue %s for session %s", event_type, state.session.id, exc_info=True)

    async def _publish_status(self, state: SessionState, status: str | None = None) -> None:
        payload: dict[str, Any] = {"status": status or _status_word(state)}
        if state.run_id:
            payload["run_id"] = state.run_id
        await self._publish(state, "session.status", payload)

    def _finished_payload(self, state: SessionState, run_id: str, status: str, duration_s: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "duration_s": round(duration_s, 1),
            "watched": self.presence.attending(session_id=state.session.id),
            "origin": state.run_origin,
            "operator_facing": self._operator_facing(state),
            "telegram": self._telegram_delivers(state),
            "title": state.session.title,
        }
        if self._tells_news(state):
            payload["news"] = True
        if state.metadata.get("dispatcher"):
            payload["link"] = "/app/main"
        summary = _last_answer(state.engine.history) if state.engine is not None else ""
        if summary:
            payload["summary"] = summary[:SUMMARY_CHARS]
        if state.last_error_message:
            payload["error"] = state.last_error_message[:300]
        return payload

    def _ask_payload(self, state: SessionState, pending: PendingQuestion) -> dict[str, Any]:
        questions = []
        for question in pending.payload.get("questions") or []:
            if not isinstance(question, dict):
                continue
            questions.append(
                {
                    "question": str(question.get("question") or ""),
                    "options": [
                        {"label": str(option.get("label") or ""), "description": str(option.get("description") or "")}
                        for option in question.get("options") or []
                        if isinstance(option, dict)
                    ],
                    "multi": bool(question.get("multiSelect")),
                    "custom": bool(question.get("allow_custom")),
                }
            )
        return {
            "request_id": pending.tool_call_id,
            "request_ref": f"ask:{state.session.id}:{pending.tool_call_id}",
            "run_id": pending.run_id,
            "title": state.session.title,
            "questions": questions,
            "operator_facing": self._operator_facing(state),
            "telegram": self._telegram_delivers(state),
        }

    async def _mark_unread(self, state: SessionState, run_id: str) -> None:
        """A result nobody saw anywhere: the session's row carries a dot until the operator looks."""
        mark = {"run_id": run_id, "at": datetime.now(UTC).isoformat()}
        state.metadata["unread_result"] = mark
        state.session.metadata["unread_result"] = dict(mark)
        try:
            await self.sessions.update_metadata(state.session.id, state.session.metadata)
        except Exception:  # noqa: BLE001 — the mark is a courtesy; the answer itself is already written
            logger.warning("could not mark session %s unread", state.session.id, exc_info=True)
            return
        await self._publish(state, "session.unread_result", {"unread": True, "run_id": run_id})

    async def clear_unread(self, session_id: str) -> bool:
        """The operator has seen the session's last result; ``True`` when there was a mark to clear."""
        state = self._states.get(session_id)
        if state is None:
            # Not loaded: the mark is read from the stored row, and loading the whole session to
            # clear a flag would open its workspace for nothing.
            try:
                session = await self.sessions.get(session_id, TENANT)
            except Exception:  # noqa: BLE001 — no such session, nothing to clear
                return False
            if not session.metadata.get("unread_result"):
                return False
            session.metadata.pop("unread_result", None)
            await self.sessions.update_metadata(session_id, session.metadata)
            project = await self.projects.for_session(session_id)
            try:
                await self.bus.publish("session.unread_result", {"unread": False}, session_id=session_id, project_id=project.id if project is not None else None)
            except Exception:  # noqa: BLE001 — the mark is gone either way; a list re-read shows it
                logger.warning("could not publish the cleared mark of session %s", session_id, exc_info=True)
            return True
        if not (state.metadata.get("unread_result") or state.session.metadata.get("unread_result")):
            return False
        state.metadata.pop("unread_result", None)
        state.session.metadata.pop("unread_result", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "session.unread_result", {"unread": False})
        return True

    async def _on_presence(self, event: AppEvent) -> None:
        """Opening a session is seeing its result."""
        newly = event.payload.get("newly_attended") or {}
        for session_id in newly.get("sessions") or ():
            await self.clear_unread(str(session_id))

    def budget_exceeded(self) -> str | None:
        if self.budget_flag.exists():
            return self.budget_flag.read_text(encoding="utf-8").strip()
        return None

    def provider_costs_nothing(self, provider_id: str | None, model: str | None = None) -> bool:
        """Whether no dollar cap can count a call to this endpoint (and model, when given).

        A cap limits spend it can measure. A local model, a free one, and a subscription or
        self-hosted model with no price add nothing any cap counts, so a closed cap must not stop
        them: refusing every non-local call once the day's priced spend passed its cap stopped
        chats on the operator's subscription logins too.
        """
        if not provider_id:
            return False
        try:
            endpoint = self.providers.get(provider_id).endpoint
        except KeyError:
            return False
        return endpoint.kind == "llamacpp" or (model is not None and not endpoint.measures_spend(model))

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def on_finished(self, callback: RunFinished) -> None:
        self._finished.append(callback)

    def on_pending_restored(self, callback: Callable[[str, PendingQuestion], Awaitable[None]]) -> None:
        """Called after a restart for every session still waiting on a question."""
        self._pending_restored.append(callback)

    def memory_classifier(self) -> Callable[[str], Awaitable[str]] | None:
        """The call Remember asks for a category and topics, on the preset Settings → Memory names;
        ``None`` without one, and the word lists answer alone."""
        preset = self.config.memory.classify_preset
        if not preset or preset not in self.config.presets:
            return None

        async def ask(prompt: str) -> str:
            provider, model = self.providers.rungs_for(self.config, preset)[0]
            request = LLMRequest(
                model=model,
                messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt)])],
                max_tokens=200,
                temperature=0.0,
                extra={"enable_thinking": False},
                observability=LLMObservabilityContext(tenant_id=TENANT, call_purpose="memory_classify", call_category="memory"),
            )
            response = await asyncio.wait_for(provider.complete_text(request), timeout=MEMORY_CLASSIFY_TIMEOUT_SECONDS)
            return "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock))

        return ask

    def _configure_redactor(self, settings: Settings, config: RuntimeConfig) -> None:
        """Every credential this process holds is a value the redactor masks wherever it shows up."""
        values: list[str] = [
            settings.telegram_bot_token,
            settings.deepseek_api_key,
            settings.openrouter_api_key,
            settings.vllm_api_key,
            settings.github_token,
            settings.telegram_api_hash,
        ]
        values.extend(p.api_key for p in config.providers.values())
        for server in config.mcp.servers.values():
            values.extend(server.headers.values())
            values.extend(server.env.values())
        self.redactor.replace_values(values)

    def reload_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self._configure_redactor(self.settings, config)
        self.bus.queue_size = config.ops.event_subscriber_queue
        self.bus.replay_max = config.ops.event_replay_max
        self.presence.ttl_seconds = config.ops.presence_ttl_seconds
        self.presence.grace_seconds = config.ops.presence_grace_seconds
        self.providers.reload(config)
        self.mcp.reload(config.mcp.servers)
        for state in self._states.values():
            if state.services is not None:
                state.services.tool_timeout_seconds = config.limits.tool_timeout_seconds
                state.services.max_tool_output_chars = config.tools.exec.max_output_chars
        self._apply_tool_visibility_all()

    def vision_model(self) -> tuple[Any, str, FileBlobStore, str] | None:
        """The vision model as a tool that looks at an image needs it, or ``None`` without one: for a
        caller with no session of its own (a command-line member's browser)."""
        return self._vision()

    def _vision(self) -> tuple[Any, str, FileBlobStore, str] | None:
        found = self.config.vision_preset()
        if found is None:
            return None
        preset_id, preset = found
        try:
            provider = self.providers.get(preset.provider)
        except KeyError:
            # The tool then reports no vision model at all; the log keeps the chosen one and why it is out.
            logger.warning("vision preset %s names provider %s, which has no live endpoint", preset_id, preset.provider)
            return None
        return provider, preset.model, self.blobs, TENANT

    def resolve_model(self, overrides: dict[str, Any]) -> tuple[list[tuple[Any, str]], Any]:
        """Rungs and the effective preset for a session, from its live overrides.

        A chosen preset wins; a manual provider/model pair runs with the default preset's
        thinking and window settings; otherwise the global default preset applies.
        """
        if overrides.get("preset") and overrides["preset"] in self.config.presets:
            pid = overrides["preset"]
            return self.providers.rungs_for(self.config, pid), self.config.presets[pid]
        _, default = self.config.preset()  # NoModelConfigured when the table is empty
        if overrides.get("provider") and overrides.get("model_name"):
            # A manual pair is an explicit choice outside the default preset. Carry its window
            # settings, but do not claim the default free model's price evidence for that pair.
            selected = default.model_copy(update={"free_only": False}) if default.free_only else default
            return self.providers.rungs_for_pair(self.config, overrides["provider"], overrides["model_name"]), selected
        return self.providers.rungs_for(self.config), default

    # -- MCP per session --------------------------------------------------------------

    def mcp_enabled(self, state: SessionState) -> list[str]:
        return [s for s in state.metadata.get("mcp_enabled", []) if s in self.mcp_visible(state)]

    def mcp_visible(self, state: SessionState) -> list[str]:
        """The configured MCP servers this session may switch on: all of them, or the subset its
        project's environment names (as of the current run's start)."""
        allowed = state.environment_scope[0]
        return [s for s in self.mcp.available() if allowed is None or s in allowed]

    async def _environment_scope(self, state: SessionState) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        """The visibility subsets of the session's project environment; ``None`` where it names none."""
        if state.project is None:
            return (None, None)
        try:
            record = await EnvironmentStore(self.db).get(state.project.id)
        except Exception:  # noqa: BLE001 — a store that cannot be read narrows nothing; the run is not refused for it
            logger.warning("the environment of project %s could not be read; its sessions see every server and skill", state.project.id, exc_info=True)
            return (None, None)
        spec = record.spec
        return (frozenset(spec.mcp) if spec.mcp else None, frozenset(spec.skills) if spec.skills else None)

    def skills_for(self, state: SessionState) -> ScopedSkillStore:
        return ScopedSkillStore(self.skills, lambda: state.environment_scope[1])

    async def set_mcp(self, session_id: str, server: str, enabled: bool, *, announce: bool = True) -> list[str]:
        """Switch ``server`` on or off for a session; returns the servers enabled now.

        A server switched off with ``announce`` (the operator's switch, not the model's own McpDisable,
        whose result says so) leaves a note for the next turn: the history still holds results that
        called its tools loaded and callable, and a model reading them lists them as available.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        current = self.mcp_enabled(state)
        was_enabled = server in current
        if enabled:
            if server in self.mcp.available() and server not in self.mcp_visible(state):
                raise KeyError(f"MCP server {server!r} is not part of this project's environment; it offers: {', '.join(self.mcp_visible(state)) or 'none'}")
            try:
                await self.mcp.ensure(server)
            except McpStillStarting:
                # Switched on all the same: the server goes on starting, and its tools join the
                # session when it is ready; the caller reads the "connecting" state from the status.
                logger.info("MCP server %s is still starting; enabled for %s anyway", server, session_id)
            if server not in current:
                current.append(server)
        else:
            current = [s for s in current if s != server]
        switched_off = [str(s) for s in (state.metadata.get("mcp_switched_off") or ()) if str(s) != server]
        if not enabled and was_enabled and announce:
            switched_off.append(server)
        for meta in (state.metadata, state.session.metadata):
            meta["mcp_enabled"] = current
            if switched_off:
                meta["mcp_switched_off"] = switched_off
            else:
                meta.pop("mcp_switched_off", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        self._apply_tool_visibility_all()
        return current

    def mcp_tools_line(self, state: SessionState, server: str) -> str:
        """``server``'s tools as this session should read them: every exact name, or the prefix and a few.

        With ToolSearch the core holds every server back and its catalogue names a large one by prefix
        and count, so listing all three hundred names here only put ten kilobytes into the history that
        every later request re-reads — a third of what holding the server back saved. The examples are
        spread across the sorted names, so they show more of the server than its first few names.
        """
        names = sorted(self.mcp.tool_names(server))
        prefix = mcp_tool_prefix(server)
        compact = self._searches_for_tools(state) and len(names) > self._catalogue_listed_names(state)
        if not compact or not all(name.startswith(prefix) for name in names):
            return ", ".join(names)
        step = len(names) / MCP_EXAMPLE_NAMES
        examples = ", ".join(names[int(i * step)] for i in range(MCP_EXAMPLE_NAMES))
        # "Spelt like", not "for example": offered five names after McpEnable, a model picked the one
        # that looked closest to its task, called it three times with the wrong arguments and only
        # then searched. The names show the spelling; the search finds the tool.
        return f"{prefix}* ({len(names)} tools, spelt like {examples})"

    @staticmethod
    def _catalogue_listed_names(state: SessionState) -> int:
        """How many names the core's catalogue lists before it falls back to a prefix and a count."""
        rc = state.engine.config.rc if state.engine is not None else None
        return int(getattr(rc, "tool_catalogue_max_listed_names", 0) or LoopConstants.model_fields["tool_catalogue_max_listed_names"].default)

    async def mcp_service(
        self, op: str, *, session_id: str, server: str | None = None, redirect_url: str = ""
    ) -> str:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if op == "list":
            enabled = set(self.mcp_enabled(state))
            visible = set(self.mcp_visible(state))
            lines = []
            for item in self.mcp.status():
                if item["name"] not in visible:
                    continue
                mark = "on " if item["name"] in enabled else "off"
                tools = self.mcp_tools_line(state, item["name"]) if item["tools"] else ("connected, no tools" if item["connected"] else "not connected yet")
                err = f" (error: {item['error']})" if item["error"] else ""
                lines.append(f"[{mark}] {item['name']} — {item['description'] or 'no description'}: {tools}{err}")
            return "\n".join(lines) or "no MCP servers are configured"
        if server is None:
            raise KeyError("server is required")
        if op == "oauth_status":
            return "\n".join(f"{key}: {value}" for key, value in self.mcp.oauth_status(server).items())
        if op == "oauth_begin":
            url = await self.mcp.oauth_begin(server)
            client = self.mcp.oauth_client(server)
            if client is not None and client.redirect_uri.endswith("/api/mcp/oauth/callback"):
                return (
                    "Give this authorization URL to the owner to open in a browser (it creates or links the account on "
                    "the server's own page):\n\n"
                    f"{url}\n\n"
                    "After approving, the browser comes back to the app and the link completes by itself; then run "
                    f"McpEnable(server={server!r}). McpOAuthStatus says whether it is linked."
                )
            return (
                "Give this authorization URL to the owner to open in a browser (it creates or links the account on "
                "the server's own page):\n\n"
                f"{url}\n\n"
                "After approving, the browser redirects to a local address that cannot load — copy the FULL address "
                "from the URL bar and paste it back, then run McpOAuthFinish."
            )
        if op == "oauth_finish":
            if not redirect_url:
                raise KeyError("redirect_url is required for oauth_finish")
            result = await self.mcp.oauth_finish(server, redirect_url)
            return f"linked {server}: scopes {result.get('scope')}, access token valid for {result.get('expires_in')}s. Now run McpEnable(server={server!r})."
        if op == "oauth_disconnect":
            removed = await self.mcp.oauth_disconnect(server)
            return f"removed stored OAuth tokens for {server}" if removed else f"{server} is not OAuth-configured"
        # The model's own switch: its result tells it, so no note is left for the next turn.
        current = await self.set_mcp(session_id, server, enabled=(op == "enable"), announce=False)
        if op == "enable":
            if not self.mcp.tool_names(server) and any(row["name"] == server and row["state"] == "connecting" for row in self.mcp.status()):
                return f"enabled {server}, but it is still starting (a server may install itself on first start); its tools appear once it connects: call McpEnable(server={server!r}) again in a minute to see them"
            names = self.mcp_tools_line(state, server) or "(none)"
            if self._searches_for_tools(state):
                # A server's tools are the first thing the core holds back whenever ToolSearch is on, so
                # they are named in the catalogue rather than put in the tool list. Saying so, with the
                # exact names or the exact prefix, is what keeps a model from composing a name out of
                # the server's.
                if names.startswith(mcp_tool_prefix(server) + "*"):
                    return f"enabled {server}; its tools are not loaded yet: find the ones you need with ToolSearch by describing the task; the names below only show how they are spelt. Tools: {names}"
                return f"enabled {server}; its tools are not loaded yet: load the ones you need with ToolSearch, by these exact names: {names}"
            return f"enabled {server}; tools available from your next step: {names}"
        return f"disabled {server}; its tools ({mcp_tool_prefix(server)}*) are no longer available, including any loaded earlier; enabled now: {', '.join(current) or 'none'}"

    async def _load_image(self, ref: str) -> tuple[bytes, str]:
        data = await self.blobs.get(TENANT, ref)
        meta = await self.blobs.head(TENANT, ref)
        return data, meta.content_type

    # -- sessions -------------------------------------------------------------------

    def workspace_for(self, session_id: str) -> Path:
        return self.settings.workspaces_dir / session_id

    def folder_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> ProjectFolder:
        """The project folder a session works in: the one its ``folder_id`` names, or the primary."""
        if project is None:
            raise RuntimeError(f"session {session_id} has no project")
        if not project.folders:
            raise RuntimeError(f"the project {project.name} of session {session_id} has no folder")
        wanted = str(metadata.get("folder_id") or "")
        if not wanted:
            return project.primary
        folder = project.folder(wanted)
        if folder is None:
            raise RuntimeError(f"session {session_id} works in a folder {project.name} no longer has")
        return folder

    def host_folder_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> ProjectFolder | None:
        """The host folder the session works in when this process runs in the container; ``None`` otherwise.

        Such a session runs from a directory of its own here (:func:`host_home_name`) and does its
        work on the host through the terminal daemon (:class:`HostExecBackend`). Natively a host folder
        is this process's own and none of this applies.
        """
        if project is None or not project.folders or self.projects.local_env == "host":
            return None
        try:
            folder = self.folder_of(session_id, metadata, project)
        except RuntimeError:
            return None
        return folder if folder.env == "host" else None

    def workspace_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> Path:
        """Return the session's project folder, its private directory inside that folder, or its own
        directory under the workspaces root (``home``) when its folders are out of this process's reach."""
        home = home_of(metadata)
        if home:
            return self.settings.workspaces_dir / home
        if self.host_folder_of(session_id, metadata, project) is not None:
            return self.settings.workspaces_dir / host_home_name(session_id)
        return self.folder_workspace(session_id, metadata, project)

    def folder_workspace(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> Path:
        """Where in its project folder the session works — the folder, its worktree or its own directory —
        whether or not this process can reach it: for a host folder, the path on the host."""
        base = self.folder_of(session_id, metadata, project).path
        worktree = str(metadata.get("worktree_cwd") or "").strip()
        if worktree:
            # A staff member in its own worktree works there: ``<folder>/.agents/worktrees/<name>``,
            # or the same sub-folder of it when the project folder is a sub-folder of its repository.
            target = Path(os.path.normpath(worktree))
            if not target.is_absolute() or (base != target and base not in target.parents):
                raise RuntimeError(f"session {session_id} has a worktree outside its project folder")
            return target
        relative = str(metadata.get("directory") or "").strip()
        if not relative:
            return base
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(f"session {session_id} has an invalid project directory")
        target = Path(os.path.normpath(base / candidate))
        if base != target and base not in target.parents:
            raise RuntimeError(f"session {session_id} has a directory outside its project")
        return target

    def inbox_folder(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> ProjectFolder | None:
        """The project folder whose rules decide whether the session's inbox may be made: none for a
        session with a home of its own, which is ours to create."""
        if home_of(metadata) or self.host_folder_of(session_id, metadata, project) is not None:
            return None
        return self.folder_of(session_id, metadata, project)

    def work_dir(self, state: SessionState) -> Path:
        """Where the session's tools work: its workspace, or the path on the host for a session whose
        commands run there. What the prompt names as the working directory and what a reviewer's
        diff is taken in."""
        services = state.services
        if services is not None and isinstance(services.exec_backend, HostExecBackend):
            return services.workspace_dir
        return state.workspace

    def env_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> str:
        """Where the session's commands and file tools run: ``host`` for one working in a host folder
        through the daemon, else this process's own environment. A project's coordinator with a home
        of its own only reads the host, and runs here."""
        if not home_of(metadata) and self.host_folder_of(session_id, metadata, project) is not None:
            return "host"
        return self.projects.local_env

    def host_unreachable(self) -> str:
        """Why a session cannot be started in a host folder now, or ``""`` when it can."""
        bridge = self.host_bridge
        if bridge is None or not bridge.available():
            return BRIDGE_DOWN
        return ""

    def locator_services(self, session_id: str) -> SessionServices | None:
        state = self._states.get(session_id)
        return state.services if state is not None else None

    async def create_session(
        self,
        title: str,
        *,
        session_id: str | None = None,
        workspace: Path | None = None,
        metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
        own_directory: bool = False,
        folder_id: str | None = None,
        on_host: bool = False,
        directory: str | None = None,
    ) -> SessionState:
        """Make a session. ``on_host`` starts a chat that names no project on the host from the
        container: its scratch project's one folder is on the host, so every command and file tool
        of it runs there through the terminal daemon, as in any host folder. ``directory`` is another
        session's own directory in the folder, shared rather than made: a prompt coach reads where its
        chat works."""
        sid = session_id or uuid.uuid4().hex[:12]
        project = await self.projects.get(project_id) if project_id else None
        if project_id and project is None:
            raise KeyError(project_id)
        if on_host:
            if project is not None or workspace is not None or folder_id:
                raise HostChatRefused("a chat on the host names no project; a project's folder decides where its chats run")
            if self.projects.local_env == "host":
                raise HostChatRefused("this installation runs on the host already, so every chat works there")
            problem = self.host_unreachable()
            if problem:
                raise HostUnreachable(f"a chat cannot be started on the host now: {problem}")
            folder = await self._make_host_chat_folder(sid)
            project = await self.projects.create(title, [FolderSpec(folder, env="host")], settings=ProjectSettings(ephemeral=True), project_id=sid)
        meta = dict(metadata or {})
        if not privacy.is_private(meta) and any(privacy.is_private(self._states.get(parent)) for parent in privacy.parent_ids(meta)):
            # A fork, a subagent, a side question or a coach of a private chat carries that chat's
            # conversation, so it is private as well; otherwise the search would index the copy.
            meta[privacy.PRIVATE_KEY] = True
        if folder_id:
            folder = project.folder(folder_id) if project is not None else None
            if folder is None:
                raise ValueError(f"{folder_id} is not a folder of {project.name if project is not None else 'a named project'}")
            if not folder.local(self.projects.local_env) and folder.env != "host":
                # A Daedalus session works with this process's own tools, and natively nothing can
                # reach into the container; only a terminal there can. A host folder seen from the
                # container is worked in through the host terminal daemon (``host_folder_of``).
                raise ValueError(f"{folder.path} is a {folder.env} folder, which an agent of this process cannot work in")
            meta["folder_id"] = folder_id
        else:
            meta.pop("folder_id", None)
        if project is None:
            # A chat that names no project gets a scratch project of its own, removed with its last session.
            project = (
                await self.projects.adopt_directory(title, workspace)
                if workspace is not None
                else await self.projects.create(title, settings=ProjectSettings(snapshots=True, ephemeral=True), project_id=sid)
            )
        if own_directory:
            meta["directory"] = f".agents/{sid}"
        elif directory:
            meta["directory"] = directory
        else:
            meta.pop("directory", None)
        host_folder = self.host_folder_of(sid, meta, project)
        if host_folder is not None and not home_of(meta):
            # Refused before anything is stored: a session whose every command would fail is a chat
            # the operator then has to find and delete. A session with a home of its own (the
            # project's coordinator) reads the host through Peek and starts without the daemon.
            problem = self.host_unreachable()
            if problem:
                raise HostUnreachable(f"{host_folder.path} is on the host, and {problem}")
            if own_directory:
                await self._make_host_directory(self.folder_workspace(sid, meta, project), host_folder)
        workspace = self.workspace_of(sid, meta, project)
        _ensure_inbox(workspace, self.inbox_folder(sid, meta, project))
        session = Session(id=sid, tenant_id=TENANT, title=title, metadata=dict(meta))
        await self.sessions.create(session, project_id=project.id)
        project = await self._settle_project(project)
        state = SessionState(session=session, workspace=workspace, metadata=dict(meta), project=project)
        self._states[sid] = state
        self.register_services(state)
        created: dict[str, Any] = {"title": title}
        if meta.get("subagent_of"):
            created["parent"] = str(meta["subagent_of"])
        await self._publish(state, "session.created", created)
        return state

    async def _settle_project(self, project: Project) -> Project:
        """The project as it is after a session was added to it: a chat's scratch project with a
        second top-level session is a project from now on (:meth:`ProjectStore.settle`). The loaded
        sessions of it are given the fresh value, since the browser, the secrets and the dispatcher
        read ``settings.ephemeral`` from the project a session holds."""
        if not await self.projects.settle(project.id):
            return project
        fresh = await self.projects.get(project.id) or project
        await self.reload_project(fresh, project.id)
        return fresh

    async def _make_host_chat_folder(self, session_id: str) -> str:
        """Make the host scratch folder of a chat (:data:`HOST_CHATS`) and return its absolute path
        as the host spells it. Run from ``/``, because the daemon wants an existing absolute working
        directory and this process does not know the operator's home there; the shell does."""
        backend = HostExecBackend(lambda: self.host_bridge, "/")
        script = f'd="{HOST_CHATS}/{session_id}" && mkdir -p -- "$d" && cd -- "$d" && pwd -P'
        outcome = await backend.run(script, cwd="/", env=None, timeout=30.0)
        lines = [line.strip() for line in outcome.output.splitlines() if line.strip()]
        path = lines[-1] if lines else ""
        if outcome.exit_code != 0 or not host_chat_path(Path(path), session_id):
            raise HostUnreachable(f"could not make the chat's folder on the host: {outcome.output.strip() or 'no answer'}")
        return path

    async def _remove_host_chat_folder(self, project: Project, session_id: str) -> None:
        """Remove a deleted host chat's scratch folder through the daemon. Only a folder of exactly
        the shape :data:`HOST_CHATS` makes, for this session, of an ephemeral project of its own: an
        operator's folder never goes with a chat. A daemon that is down leaves the folder behind,
        which costs a directory and nothing else."""
        if not (project.id == session_id and project.settings.ephemeral and len(project.folders) == 1):
            return
        folder = project.primary
        if folder.env != "host" or self.projects.local_env == "host" or not host_chat_path(folder.path, session_id):
            return
        backend = HostExecBackend(lambda: self.host_bridge, "/")
        outcome = await backend.run(f"rm -rf -- {shlex.quote(str(folder.path))}", cwd="/", env=None, timeout=60.0)
        if outcome.exit_code != 0:
            logger.warning("session %s deleted; its folder on the host stays: %s", session_id, outcome.output.strip())

    async def _make_host_directory(self, path: Path, folder: ProjectFolder) -> None:
        """Make a session's own directory inside a host folder, through the daemon, as ``_ensure_inbox``
        makes it locally; the daemon refuses a command whose working directory is not there."""
        backend = HostExecBackend(lambda: self.host_bridge, str(folder.path))
        outcome = await backend.run(f"mkdir -p -- {shlex.quote(str(path / 'inbox'))}", cwd=str(folder.path), env=None, timeout=30.0)
        if outcome.exit_code != 0:
            raise HostUnreachable(f"could not make {path} on the host: {outcome.output.strip()}")

    def live_state(self, session_id: str) -> SessionState | None:
        """The state of a session this process already holds; ``None`` for one it would have to load."""
        return self._states.get(session_id)

    async def project_of(self, session_id: str) -> Project | None:
        """The project a session works in, for anything that makes a child of it.

        Every path that creates a session from another one — a subagent, a spawned agent, a fork, a
        scheduled task, the companion's delegate — asks this and passes the answer on. A child
        that did not would carry the parent's directory without the parent's wall, which is the whole
        of the boundary gone through the commonest way of making a new worker.
        """
        state = self._states.get(session_id)
        if state is not None:
            return state.project
        return await self.projects.for_session(session_id)

    async def get_state(self, session_id: str) -> SessionState | None:
        """The live state of a session, loading it once however many callers ask at the same moment.

        The load awaits three reads between "not held" and "installed". After a restart the app's
        parallel requests and a Telegram message each built their own state, the last one won the
        map, and Stop and steer then reached an object with no run on it while the run went on in
        another. Every caller now shares one load task, shielded so a caller that gives up does not
        cancel it for the others.
        """
        state = self._states.get(session_id)
        if state is not None:
            return state
        load = self._loading.get(session_id)
        if load is None:
            load = asyncio.create_task(self._load_state(session_id), name=f"load-session-{session_id}")
            self._loading[session_id] = load
            load.add_done_callback(lambda done: self._loading.pop(session_id, None) if self._loading.get(session_id) is done else None)
        return await asyncio.shield(load)

    async def _load_state(self, session_id: str) -> SessionState | None:
        try:
            session = await self.sessions.get(session_id, TENANT)
        except Exception:
            return None
        project = await self.projects.for_session(session_id)
        try:
            workspace = self.workspace_of(session_id, dict(session.metadata), project)
            _ensure_inbox(workspace, self.inbox_folder(session_id, dict(session.metadata), project))
        except (RuntimeError, OSError) as exc:
            # The session's stored directory no longer resolves inside the project that holds it, or
            # the folder cannot be made. Loading a session happens on every path into this process —
            # boot recovery, the scheduler restoring its in-flight runs, a service being reconciled —
            # and most of those callers already treat "no such session" as an outcome. Raising here
            # made one bad row in one session fatal to all of them; it is reported and skipped instead,
            # and ``unloadable_sessions`` is what the doctor and the operator read afterwards.
            self.unloadable_sessions[session_id] = str(exc)
            logger.warning("session %s cannot be opened: %s", session_id, exc)
            return None
        self.unloadable_sessions.pop(session_id, None)
        state = SessionState(session=session, workspace=workspace, metadata=dict(session.metadata), project=project)
        state.kind = await self.sessions.kind(session_id)
        measurement = await self.db.kv_get(f"context_measurement:{session_id}", {})
        state.usage_floor_seq = int(measurement.get("floor", 0))
        state.observed_prompt_tokens = int(measurement.get("tokens", 0))
        # A state installed meanwhile by another path (a session just created) is the live one.
        if (held := self._states.get(session_id)) is not None:
            return held
        self._states[session_id] = state
        self.register_services(state)
        return state

    async def transcript(self, session_id: str, *, tail: int = 0, before: int = 0) -> list[Message]:
        """Display history: the durable transcript plus whatever the live engine has not persisted yet.

        ``tail`` and ``before`` are pushed into SQL: a page costs the page. Only a caller that
        passes neither reads the whole session, and the live engine's unpersisted messages are
        merged in only for the newest page, which is the only page they can belong to.
        """
        rows = await self.sessions.list_transcript(session_id, limit=tail, before_seq=before)
        if not rows and not before:
            # Sessions from before the transcript existed: seed it from the working history.
            lo, _ = await self.sessions.transcript_bounds(session_id)
            if not lo:
                history = list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
                if history:
                    await self.sessions.append_transcript(session_id, history)
                    rows = list(history)[-tail:] if tail > 0 else history
        if before:
            return rows
        live = await self._unpersisted(session_id, {self.sessions.transcript_key(m) for m in rows})
        if not live:
            return rows
        rows = rows + live
        return rows[-tail:] if tail > 0 else rows

    async def transcript_page(self, session_id: str, *, tail: int = 600, before: int = 0) -> list[dict[str, Any]]:
        """The page as the app draws it: stored views for what is persisted, built on the spot for what is not.

        Nothing here parses a message or runs a redaction pattern for a row that was already
        written — that was done once, when the row was appended.
        """
        views = await self.sessions.list_transcript_views(session_id, limit=tail, before_seq=before)
        if not views and not before:
            seeded = await self.transcript(session_id, tail=tail)  # legacy session: seeds the transcript from the history
            if seeded:
                views = await self.sessions.list_transcript_views(session_id, limit=tail)
        if before:
            return views
        live = await self._unpersisted(session_id, set())  # a view carries no key: the transcript is asked instead
        if not live:
            return views
        # The transcript is written by a task of its own, so during a run the newest message or two
        # are not in it yet and have no row number. A view without one reads to the app as a hole in
        # the history and costs it a re-read of the whole session on every event. So each gets the
        # number its row will have when it is written, and is marked as live: the app matches the
        # settled rows by number and takes the live tail as it comes.
        base = int(views[-1].get("seq") or 0) if views else 0
        views = views + [{**message_view(m), "seq": base + n, "live": True} for n, m in enumerate(live, start=1)]
        return views[-tail:] if tail > 0 else views

    async def _unpersisted(self, session_id: str, known_keys: set[str]) -> list[Message]:
        """What the live engine holds and the transcript does not yet, in transcript order.

        Against one page alone the dedup would re-show every live message older than it, so
        what the page does not settle is asked of the transcript by key: one indexed query,
        and the answer does not depend on how much of the session was read.
        """
        state = self._states.get(session_id)
        if state is None or state.engine is None or not state.running:
            return []
        # Context lent to a side question is never in its transcript and must not look like a round still being written.
        candidates = [m for m in state.engine.history if self.sessions.transcript_key(m) not in known_keys and not m.metadata.get(LENT_KEY)]
        if candidates:
            persisted = await self.sessions.transcript_keys_present(session_id, [self.sessions.transcript_key(m) for m in candidates])
            candidates = [m for m in candidates if self.sessions.transcript_key(m) not in persisted]
        for message in candidates:
            message.metadata.setdefault("daedalus.run_id", state.run_id)
        # Received queue messages are tagged at queue_update. Untagged user messages are core
        # recovery notices, not operator input; the live view and persisted sync must hide both.
        return [
            m.model_copy(update={"metadata": {**m.metadata, "daedalus.origin": "core", OPERATOR_WORDS_METADATA_KEY: False}})
            if m.role is MessageRole.user and "daedalus.origin" not in m.metadata and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
            else m
            for m in candidates
        ]

    async def list_sessions(self, limit: int = 100, *, ids: list[str] | None = None) -> list[dict[str, Any]]:
        rows = await self.sessions.list_sessions(TENANT, limit=limit, ids=ids)
        projects = await self.projects.by_session()
        kinds = await self.sessions.kinds([session.id for session in rows] if ids is not None else None)
        out: list[dict[str, Any]] = []
        for session in rows:
            if prompt_coach.hidden(session.metadata):
                # A prompt coach lives in a window over its chat. Left out here, it is out of the sidebar,
                # the inbox, search results, Telegram's list and every agent that lists sessions at once.
                continue
            state = self._states.get(session.id)
            status = _status_word(state) if state is not None else "idle"
            out.append(
                {
                    "id": session.id,
                    "title": session.title,
                    "status": status,
                    "created_at": session.created_at.isoformat(),
                    "last_message_at": session.last_message_at.isoformat(),
                    "run_id": state.run_id if state else None,
                    "metadata": session.metadata,
                    "project_id": projects.get(session.id),
                    "kind": kinds.get(session.id, "chat"),
                }
            )
        return out

    async def set_session_kind(self, session_id: str, kind: str, *, only_from: str | None = None) -> bool:
        """Change what a conversation is for; with ``only_from``, only from that kind (a mark that sticks
        leaves the operator's own choice alone). ``True`` when it changed, and then the fronts hear of it.

        A conversation that becomes a Chrome conversation starts its runs with the browser's tools
        loaded, as one the side panel started does: the agent is told to work in the operator's Chrome
        and should not have to search for the tools first."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        changed = await self.sessions.set_kind(session_id, kind, only_from=only_from)
        if not changed:
            return False
        state.kind = kind
        if kind == "chrome":
            carried = [str(g) for g in (state.metadata.get(tool_groups.CARRIED_KEY) or ()) if isinstance(g, str)]
            if "browser" not in carried:
                for meta in (state.metadata, state.session.metadata):
                    meta[tool_groups.CARRIED_KEY] = list(dict.fromkeys([*carried, "browser"]))
                await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "session.kind", {"kind": kind})
        return True

    async def session_catalog(self) -> list[dict[str, Any]]:
        """Return the cheap session read model without opening any transcript.

        The catalogue is deliberately assembled from the indexed session rows and live manager
        state.  A large installation must not turn one list request into one history read per row.
        """
        rows = await self.list_sessions(limit=10_000)
        children: dict[str, int] = {}
        for row in rows:
            parent = str(row["metadata"].get("subagent_of") or "")
            if parent and row["status"] in ("running", "waiting", "compacting"):
                children[parent] = children.get(parent, 0) + 1
        for row in rows:
            archived = bool(row["metadata"].get("archived"))
            unread = bool(row["metadata"].get("unread_result"))
            # Running jobs only: every job the session ever started was counted, so a session whose jobs had
            # all ended long ago still looked as if it waited for something.
            background = sum(1 for job in self.jobs.get(row["id"], {}).values() if job.running) + children.get(row["id"], 0)
            row.update(
                {
                    "archived": archived,
                    "background_count": background,
                    "unread_result": unread,
                    "needs_attention": row["status"] in ("failed", "waiting") or unread,
                }
            )
        return rows

    async def task_views(self, session_id: str) -> list[dict[str, Any]]:
        """Every background job of a session, as one operator-facing list.

        Subagents are not listed here: they are sessions with a panel of their own
        (``Subagents.views``), and listing them twice made a standing helper look like a job that never
        ends. A job's state comes from what the watcher last saw (it asks the host every few seconds), and its
        records outlive a restart, so a job that ended or was lost while the bot was down is listed with
        that outcome rather than dropped. This is the one list the app shows: it used to show these beside
        a second list read out of the transcript, which named the same jobs again without their state.
        """
        out: list[dict[str, Any]] = []
        states = {"succeeded": "done", "failed": "failed", "killed": "cancelled", "timed_out": "failed", "lost": "lost"}
        quiet_limit = self.config.tools.jobs.quiet_minutes * 60.0
        overdue_limit = self.config.tools.jobs.max_hours * 3600.0
        mono = time.monotonic()
        for job in self.jobs.get(session_id, {}).values():
            word = shell_outcome(job)
            state = "running" if word is None else states[word]
            started = job.started_at or time.time() - max(0.0, mono - job.started)
            try:
                active = datetime.fromtimestamp(job.log.stat().st_mtime, UTC)
            except OSError:
                active = datetime.fromtimestamp(job.ended or started, UTC)
            flag = None
            if word is None and job.kind == "job":
                if "quiet" in job.flags or (quiet_limit > 0 and job.grown_at and mono - job.grown_at >= quiet_limit):
                    flag = "quiet"
                elif "overdue" in job.flags or (overdue_limit > 0 and mono - job.started >= overdue_limit):
                    flag = "overdue"
            out.append(
                {
                    "id": job.id,
                    "owner_session_id": session_id,
                    "parent_run_id": None,
                    "kind": job.kind,
                    "state": state,
                    "outcome": word,
                    "exit_code": job.exit_code,
                    "title": shell_title(job)[:200],
                    "command": job.command,
                    "where": job.where,
                    "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
                    "last_activity_at": active.isoformat(),
                    "ended_at": datetime.fromtimestamp(job.ended, UTC).isoformat() if job.ended and word is not None else None,
                    "flag": flag,
                    "reported": job.reported,
                    "progress": None,
                    "child_session_id": None,
                    "result_ref": f".jobs/{job.log.name}",
                    "stop_supported": word is None,
                }
            )
        out.sort(key=lambda item: (item["state"] not in ("running", "waiting"), item["last_activity_at"], item["id"]))
        return out

    async def stop_task(self, session_id: str, task_id: str) -> bool:
        """Stop a task only when it belongs to the session named by the route."""
        job = self.jobs.get(session_id, {}).get(task_id)
        if job is None:
            return False
        await stop_job(job, by="operator")
        await self.publish_jobs(session_id, job.id, "stopped")
        return True

    async def publish_jobs(self, session_id: str, job_id: str, change: str) -> None:
        """Tell the open Jobs lists that one of the session's background jobs changed."""
        state = self._states.get(session_id)
        if state is None:
            return
        running = sum(1 for job in self.jobs.get(session_id, {}).values() if job.running)
        await self._publish(state, "session.jobs", {"job_id": job_id, "change": change, "running": running})

    async def delete_session(self, session_id: str, *, delete_workspace: bool = True) -> bool:
        """Remove a session entirely: its run, records, events and (optionally) its workspace."""
        state = await self.get_state(session_id)
        if state is None:
            return False
        for hook in self.delete_hooks:
            try:
                await hook(session_id)
            except Exception:  # noqa: BLE001
                logger.exception("delete hook failed for %s", session_id)
        if state is None:
            return False
        if state.running and state.engine is not None:
            state.engine.stop()
            if state.task is not None:
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
        # Everything the last run still owes is about to be deleted along with the session; a
        # snapshot row written after the rows are gone would belong to a session that does not exist.
        if state.housekeeping is not None and not state.housekeeping.done():
            state.housekeeping.cancel()
            await asyncio.gather(state.housekeeping, return_exceptions=True)
        # The same holds for a compaction the last run left running: its progress events and its
        # rewritten history reference the session row, and a summary that finished after the delete
        # failed on the foreign key instead of quietly having nothing to do.
        if state.auto_compaction is not None and not state.auto_compaction.done():
            state.auto_compaction.cancel()
            await asyncio.gather(state.auto_compaction, return_exceptions=True)
        if state.prepared_compaction is not None:
            state.prepared_compaction.drop()  # a summary written ahead for a session that is going away
            await asyncio.gather(state.prepared_compaction.task, return_exceptions=True)
            state.prepared_compaction = None
        if state.provider_hold is not None:
            state.provider_hold.close()
            state.provider_hold = None
            await self.providers.close_retired()
        self._states.pop(session_id, None)
        locator.unregister(session_id)
        for job in self.jobs.pop(session_id, {}).values():
            if isinstance(job, RemoteJob):
                # Its host goes on running it otherwise, with nobody left who knows it is there.
                with suppress(Exception):
                    await job.kill()
                continue
            process = getattr(job, "process", None)
            if process is not None and process.returncode is None:
                try:
                    end_tree(process.pid, hard=True)
                except (ProcessLookupError, PermissionError):
                    pass
        runs = await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,))
        async with self.db.transaction() as conn:
            for row in runs:
                await conn.execute("DELETE FROM events WHERE run_id = ?", (row["id"],))
                await conn.execute("DELETE FROM snapshots WHERE run_id = ?", (row["id"],))
            # The runs and the session row go through the purge, with every record that references
            # them: a provider refusal's record refuses a plain DELETE and named its run without a
            # cascade, so a chat that had once met one could never be deleted, and its deletion
            # failed only after the hooks above had already acted on it.
            purge = Purge(conn)
            await purge.rows("runs", "session_id = ?", (session_id,))
            await conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            # The index points at transcript rows by number; it goes with them, or it answers for rows that are gone.
            await conn.execute("DELETE FROM transcript_fts WHERE rowid IN (SELECT seq FROM transcript WHERE session_id = ?)", (session_id,))
            await conn.execute("DELETE FROM transcript WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM live_control WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM kv WHERE key = ?", (f"context_measurement:{session_id}",))
            await conn.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM topics WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM checkpoint_retention WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM verifications WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM learning_records WHERE session_id = ?", (session_id,))
            await purge.rows("sessions", "id = ?", (session_id,))
            await purge.finish()
            project = state.project
            # A chat's scratch project goes with its last chat while it is still ephemeral, whatever
            # its folder is: the installation's own scratch, a host chat's, or a folder the chat was
            # started in. Only the record goes; a folder on disk is removed by nobody here but the
            # host chat's below, which tests its exact shape. The flag is read from the row, not
            # from the copy this state holds, because a second chat may have made it a project since
            # (and then it stays, with no chat at all). Nothing re-marks a project here any more: an
            # older chat's project was once marked only when its chat was deleted, which would have
            # turned a project that a second chat made back into a scratch one; the migration that
            # introduced the rule marked those once instead.
            if project is not None:
                await conn.execute(
                    "DELETE FROM projects WHERE id = ? AND system = '' AND json_extract(settings, '$.ephemeral') = 1 "
                    "AND NOT EXISTS (SELECT 1 FROM sessions WHERE project_id = projects.id)",
                    (project.id,),
                )
        await self.projects.list()
        if state.project is not None and await self.projects.get(state.project.id) is None:
            await self._remove_host_chat_folder(state.project, session_id)
        # After the rows are gone, so a list read on hearing it no longer carries the session.
        await self._publish(state, "session.deleted", {"title": state.session.title})
        # The logs a read-only folder's session kept in the state volume are the session's alone.
        # Removed whether or not the folder is read-only today: it may have been when they were written.
        shutil.rmtree(session_scratch_dir(self.settings.state_dir, session_id), ignore_errors=True)
        # So is the directory a session in a host folder ran from: nothing of the operator's is in it.
        shutil.rmtree(self.settings.workspaces_dir / host_home_name(session_id), ignore_errors=True)
        # A private child belongs to this session. A project folder never goes with a session.
        if (
            delete_workspace
            and state.metadata.get("directory")
            # A coach's or a side question's directory is its chat's, which it only read.
            and not self.lives_in_window(state)
            and state.workspace.exists()
            and state.project is not None
            and any(state.workspace.is_relative_to(folder.path) for folder in state.project.folders)
        ):
            if await self.workspace_users(state.workspace):
                logger.warning("session %s deleted; its workspace stays, other sessions work in it", session_id)
            else:
                shutil.rmtree(state.workspace, ignore_errors=True)
        return True

    async def workspace_users(self, workspace: Path) -> list[dict[str, str]]:
        """The sessions that work in ``workspace``: a project's, their own directory, or one they were attached to."""
        target = workspace.resolve()
        projects = {p.id: p for p in await self.projects.list()}
        out: list[dict[str, str]] = []
        for row in await self.db.fetchall("SELECT id, title, metadata, project_id FROM sessions"):
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (ValueError, AttributeError):
                metadata = {}
            project = projects.get(row["project_id"])
            if project is None or prompt_coach.hidden(metadata):
                # A coach only reads its chat's workspace: listed, it would show a hidden session under
                # the chat and keep a deleted chat's directory alive for a reader that goes with it.
                continue
            path = self.workspace_of(row["id"], metadata, project)
            if path.resolve() == target:
                out.append({"id": row["id"], "title": row["title"]})
        return out

    def _schedule_entitle(self, session_id: str, text: str, provisional: str) -> None:
        """Name an auto-created chat from its first message, after the run has already started."""
        self._spawn_background(self.entitle(session_id, text, provisional), f"entitle:{session_id}")

    async def entitle(self, session_id: str, text: str, provisional: str) -> None:
        """Replace the placeholder title when the operator has not named the chat themselves.

        The model is asked once, in the language of the message. A failure leaves the placeholder,
        which is already a clip of what they wrote, so the chat is never stuck untitled.
        """
        try:
            invented = await self._invent_title(session_id, text)
        except Exception:  # noqa: BLE001 — naming is a courtesy, not part of the run
            logger.warning("could not name session %s", session_id, exc_info=True)
            return
        state = await self.get_state(session_id)
        if state is None or state.session.title != provisional or not invented or invented == provisional:
            return
        await self.rename_session(session_id, invented)
        project = state.project
        if project is not None and project.name == provisional:
            row = await self.db.fetchone("SELECT json_extract(settings, '$.ephemeral') AS ephemeral FROM projects WHERE id = ?", (project.id,))
            if row is not None and row["ephemeral"] in (1, True):
                try:
                    await self.projects.update(project.id, name=invented)
                except Exception:  # noqa: BLE001 — the session title is the one the list shows
                    logger.warning("could not rename the folder of session %s", session_id, exc_info=True)

    async def _invent_title(self, session_id: str, text: str) -> str:
        source = " ".join(text.split())
        if not source:
            return ""
        try:
            rungs, _ = self.resolve_model(await self.live.load(session_id))
        except Exception:  # noqa: BLE001 — no model means the clip of the message stays
            return ""
        if not rungs:
            return ""
        provider, model = rungs[0]
        request = LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=TITLE_PROMPT + source[:2000])])],
            max_tokens=40,
            temperature=0.2,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, call_purpose="session_title", call_category="session"),
        )
        response = await asyncio.wait_for(provider.complete_text(request), timeout=20)
        raw = "".join(block.text for block in response.message.content_blocks if isinstance(block, TextBlock))
        return _clean_title(raw)

    async def rename_session(self, session_id: str, title: str) -> SessionState:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        title = title.strip()[:128]
        if not title:
            raise ValueError("empty title")
        await self.sessions.update_title(session_id, title)
        await self.db.execute("UPDATE topics SET title = ? WHERE session_id = ?", (title, session_id))
        state.session = state.session.model_copy(update={"title": title})
        return state

    async def compact(self, session_id: str, instructions: str = "", *, keep_recent: int = 0) -> str:
        """Replace the history (all of it, or all but the last ``keep_recent`` messages) with one summary.

        Holds the session lock for the whole operation (including the summarising call) so
        no run can start against the history while it is being rewritten. The replaced
        transcript is kept in the workspace as ``.history-<timestamp>.jsonl``.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            # A summary replaces the history of the turn whose snapshot may still be being written.
            await self._wait_until_quiet(state)
            summary = await self._compact_locked(state, instructions, keep_recent=keep_recent)
            state.compaction_hold = None  # the operator's own compaction starts the automatic one afresh
            return summary

    async def _maybe_auto_compact(self, state: SessionState, *, required_only: bool = False) -> None:
        """When the last prompt filled ``compaction.auto_ratio`` of the window, compact the history.

        This runs after a run settles, between turns, because a compaction is a summariser call
        that takes a minute or two and nobody should be waiting on it. ``required_only`` is the
        exception on the way into a run: a history that no longer fits the window at all cannot
        be sent, so that one is compacted before the run rather than refused by the provider.
        """
        cfg = self.config.compaction
        if cfg.auto_ratio <= 0 or state.pending is not None or self.shutting_down:
            return

        def ratio_of(status: dict[str, Any]) -> float:
            return 1.0 if required_only else compaction_ratio(cfg, int(status["window"] or 0), int(status.get("output_cap") or 0))

        def below(status: dict[str, Any]) -> bool:
            return not status["window"] or status["tokens"] < ratio_of(status) * status["window"] or status["messages"] < cfg.min_messages

        def held(status: dict[str, Any]) -> bool:
            # A compaction that failed, or one that left the prompt about as large as it found it,
            # tried again after every turn: in an orchestrator whose summariser timed out each time
            # it held the session for three minutes per message and never shrank anything. It waits
            # now until the history has really grown, or (after a failure) until some time has passed.
            hold = state.compaction_hold
            if hold is None or required_only:
                return False
            grown = status["tokens"] >= hold["tokens"] + COMPACTION_RETRY_GROWTH * status["window"]
            return not grown and time.monotonic() < hold["until"]

        status = await self.context_status(state)
        if held(status):
            return
        if below(status):
            if not required_only:
                await self._prepare_compaction(state, status, ratio_of(status))
            return
        # A summary written ahead is waited for here, outside the lock, so the session stays the
        # operator's while it finishes; what it says is checked again behind the lock before use.
        prepared = await self._prepared_compaction(state)
        if self.shutting_down:
            return  # the wait outlived the process's will to rewrite anything
        async with state.lock:
            # Asked again behind the lock: two callers that both saw a full window — a manual compaction
            # racing this one, or the check on the way into a run racing the one after a run — serialise
            # here, and the second would otherwise summarise a history the first has already replaced.
            if self._states.get(state.session.id) is not state:
                return  # deleted while this waited for the lock: there is no history left to rewrite
            status = await self.context_status(state)
            if below(status) or held(status) or (state.running and state.task is not asyncio.current_task()):
                return  # a run started while this waited: it compacts after that one instead
            started = time.monotonic()
            failures = int((state.compaction_hold or {}).get("failures", 0))
            try:
                await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True, prepared=prepared)  # own_task_ok: the required_only call runs inside the run's own task
            except Exception:  # noqa: BLE001 — the next run must start even when the summary could not be made
                failures += 1
                wait = min(COMPACTION_RETRY_SECONDS * 2 ** (failures - 1), COMPACTION_RETRY_MAX_SECONDS)
                state.compaction_hold = {"tokens": status["tokens"], "failures": failures, "until": time.monotonic() + wait, "reason": "failed"}
                logger.exception(
                    "auto-compaction failed for session %s after %.0fs (attempt %d in a row; prompt %d of %d tokens); the next try waits for %d more tokens or %.0f min",
                    state.session.id, time.monotonic() - started, failures, status["tokens"], status["window"], int(COMPACTION_RETRY_GROWTH * status["window"]), wait / 60,
                )
                return
            after = await self.context_status(state)
            logger.warning("session %s: auto-compacted %d → %d messages in %.0fs (prompt was %d of %d tokens)", state.session.id, status["messages"], after["messages"], time.monotonic() - started, status["tokens"], status["window"])
            freed = status["tokens"] - after["tokens"]
            ratio = ratio_of(status)
            if after["tokens"] >= ratio * status["window"] or freed < COMPACTION_MIN_FREED * status["tokens"]:
                # What is left is still over the trigger — the kept tail, or whatever the host puts in
                # front of every prompt, is itself that large — so the next turn would compact again and
                # free the same nothing. Only growth of the history can make another pass worth paying for.
                state.compaction_hold = {"tokens": after["tokens"], "failures": failures + 1, "until": float("inf"), "reason": "freed_little"}
                logger.warning(
                    "session %s: auto-compaction freed only %d of %d prompt tokens (%d left, trigger %d); not compacting again until the prompt grows by %d",
                    state.session.id, freed, status["tokens"], after["tokens"], int(ratio * status["window"]), int(COMPACTION_RETRY_GROWTH * status["window"]),
                )
            else:
                state.compaction_hold = None
            for hook in self.compaction_hooks:
                try:
                    await hook(state.session.id, {"before_messages": status["messages"], "after_messages": after["messages"], "before_tokens": status["tokens"], "window": status["window"], "seconds": round(time.monotonic() - started)})
                except Exception:  # noqa: BLE001
                    logger.exception("compaction hook failed")

    async def _prepare_compaction(self, state: SessionState, status: dict[str, Any], ratio: float) -> None:
        """Start writing the summary of the history as it stands, in the background, once the prompt passes
        ``compaction.background_share`` of the trigger; see :class:`PreparedCompaction`.

        Nothing here takes the session lock, and the summary is written on a task of its own: turns go on
        while it is made. One that no longer applies is dropped and, still in the band, started afresh;
        one that failed stays, so a failing summariser is not asked again after every turn — the
        trigger then compacts the ordinary way, with its own back-off."""
        cfg = self.config.compaction
        prepared = state.prepared_compaction
        if prepared is not None:
            if prepared.applies_to(await self._working_history(state), state.persist_gen):
                return
            self._drop_prepared(state, "the history changed under it")
        window = int(status["window"] or 0)
        if cfg.background_share <= 0 or not window or status["messages"] < cfg.min_messages or state.compacting is not None:
            return
        if status["tokens"] < cfg.background_share * ratio * window:
            return
        full = await self._working_history(state)
        prefix = full[: compaction_cut(full, cfg.keep_recent_messages)]
        if len(prefix) < 2:
            return
        instructions = self._compaction_instructions(state, "")
        task = self._spawn_background(self._write_prepared(state, prefix, instructions), f"prepare-compaction:{state.session.id}")
        state.prepared_compaction = PreparedCompaction(keys=[self.sessions.transcript_key(m) for m in prefix], gen=state.persist_gen, tokens=int(status["tokens"]), task=task)
        logger.info("session %s: writing the summary of %d messages ahead of the trigger (prompt %d of %d tokens)", state.session.id, len(prefix), status["tokens"], window)

    async def _write_prepared(self, state: SessionState, prefix: list[Message], instructions: str) -> str:
        started = time.monotonic()
        state.background_compacting = {"reason": "auto", "stage": "summarising", "messages": len(prefix), "parts_done": 0, "parts_total": 0, "started_at": datetime.now(UTC).isoformat(), "background": True}
        await self._background_progress(state)
        try:
            summary = await self.summarise_with_fallback(state, prefix, instructions, progress=lambda **f: self._background_progress(state, **f))
            logger.info("session %s: the summary of %d messages is ready ahead of the trigger after %.0fs", state.session.id, len(prefix), time.monotonic() - started)
            return summary
        finally:  # a failure is logged by the task's owner; the trigger then compacts the ordinary way
            state.background_compacting = None
            await self._background_progress(state)

    async def _background_progress(self, state: SessionState, **fields: Any) -> None:
        if state.background_compacting is not None:
            state.background_compacting.update(fields)
        if self._states.get(state.session.id) is state:  # a deleted session has no stream to tell
            await self._notify_sinks(state.session.id, HostEvent(HostEventType.COMPACTION_BACKGROUND, state.run_id or "", {"compaction": state.background_compacting}))

    async def _prepared_compaction(self, state: SessionState) -> PreparedCompaction | None:
        """The summary written ahead, once finished, when it still applies to the history; ``None`` otherwise."""
        prepared = state.prepared_compaction
        if prepared is None:
            return None
        if prepared.applies_to(await self._working_history(state), state.persist_gen):
            await asyncio.gather(asyncio.shield(prepared.task), return_exceptions=True)
            if prepared.summary() is not None and state.prepared_compaction is prepared:
                return prepared
        if state.prepared_compaction is prepared:
            self._drop_prepared(state, "it failed" if prepared.task.done() and not prepared.task.cancelled() else "the history changed under it")
        return None

    def _drop_prepared(self, state: SessionState, why: str) -> None:
        prepared = state.prepared_compaction
        if prepared is None:
            return
        state.prepared_compaction = None
        prepared.drop()
        logger.info("session %s: dropped the summary written ahead: %s", state.session.id, why)

    async def _working_history(self, state: SessionState) -> list[Message]:
        if state.engine is not None:
            return list(state.engine.history)
        return list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))

    def _compaction_instructions(self, state: SessionState, instructions: str) -> str:
        if not instructions and self.is_orchestrator(state):
            # Its brief, team and board come back in full every turn; a summary that spends its words
            # on them loses the promises and open questions that exist nowhere else.
            return prompts.ORCHESTRATOR_COMPACTION
        if not instructions and self.is_dispatcher(state):
            return prompts.DISPATCHER_COMPACTION
        return instructions

    async def _compact_locked(self, state: SessionState, instructions: str, *, keep_recent: int = 0, reason: str = "manual", own_task_ok: bool = False, prepared: PreparedCompaction | None = None) -> str:
        """Rewrite the history as one summary; with ``prepared``, the summary written ahead when it still applies.

        Adopting it keeps every message after the prefix it summarised — more than ``keep_recent`` — and
        makes no summariser call. Any other summary written ahead is dropped: this rewrite outdates it."""
        instructions = self._compaction_instructions(state, instructions)
        if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None and not own_task_ok:
            # The loop has settled; only bookkeeping remains.
            await asyncio.gather(asyncio.shield(state.task), return_exceptions=True)
        busy = state.running and not (own_task_ok and state.task is asyncio.current_task())
        if busy or state.pending is not None:
            raise RuntimeError("the session is busy; stop the run (or answer the question) first")
        pending = [t for t in state.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # no straggler may write the old history later
        full = await self._working_history(state)
        written = prepared.summary() if prepared is not None and prepared.applies_to(full, state.persist_gen) else None
        # Whatever was written ahead is spent now: used here, or outdated by the rewrite this makes.
        self._drop_prepared(state, "used" if written is not None else "outdated by this compaction")
        if written is not None:
            assert prepared is not None
            cut = len(prepared.keys)
            logger.warning("session %s: adopting the summary written ahead of %d messages; the %d after it stay as they are", state.session.id, cut, len(full) - cut)
        else:
            exceeded = self.budget_exceeded()
            compact_provider, compact_model = await self._compaction_rung(state)
            if exceeded and not self.provider_costs_nothing(compact_provider.endpoint.id, compact_model):
                raise RuntimeError(f"daily budget exceeded ({exceeded}); compaction is a paid call")
            if not full:
                raise RuntimeError("nothing to compact")
            cut = compaction_cut(full, keep_recent)
        history, tail = full[:cut], full[cut:]
        if not history:
            raise RuntimeError("nothing to compact: the whole history is inside the kept tail")
        async with self.idle_work:
            state.compacting = {"reason": reason, "stage": "writing" if written is not None else "summarising", "messages": len(history), "parts_done": 0, "parts_total": 0, "started_at": datetime.now(UTC).isoformat()}
        await self._compaction_progress(state)
        await self._publish_status(state, "compacting")
        try:
            return await self._compact_progressing(state, history, tail, instructions, reason, own_task_ok=own_task_ok, summary=written)
        finally:
            state.compacting = None
            await self._compaction_progress(state)
            await self._publish_status(state)

    async def _compaction_progress(self, state: SessionState, **fields: Any) -> None:
        """Advance the compaction's public state and tell the session's listeners (the Mini App stream)."""
        if state.compacting is not None:
            state.compacting.update(fields)
        event = TurnEvent(
            type=EventType.STATE_CHANGED,
            run_id=state.run_id or "",
            payload={"from": "idle", "to": "compacting" if state.compacting else "idle", "reason": "compaction_progress", "compacting": state.compacting},
        )
        await self._notify_sinks(state.session.id, event)

    async def _notify_sinks(self, session_id: str, event: TurnEvent | HostEvent) -> None:
        """Hand an event to the session's listeners without booking it as a run event.

        ``_dispatch_event`` is the path for everything the loop produces: it redacts, times, and
        writes to the durable log. What the host itself raises about a session — a compaction's
        progress, a change to the steer queue — has no place in that log and takes this door.
        """
        envelope = await self.events.publish_session(
            session_id,
            event,
            history_rewrite=getattr(event, "type", None) is HostEventType.HISTORY_CUT,
        )
        event.payload["_session_envelope"] = envelope
        for sink in self._sinks:
            try:
                await sink(session_id, event)
            except Exception:  # noqa: BLE001
                logger.exception("event sink failed")

    def _compaction_preset_rung(self) -> tuple[tuple[Any, str] | None, str]:
        """The summary model's rung and, when it cannot be used, why; ``(None, "")`` when none is chosen.

        An empty choice is the operator's "the session's own model" and is no fault; a chosen preset
        that was deleted, or whose endpoint has no key, is one, and says so."""
        chosen = self.config.compaction.preset
        if not chosen:
            return None, ""
        if chosen not in self.config.presets:
            return None, f"the summary model {chosen!r} is no longer among the models"
        try:
            return self.providers.rungs_for(self.config, chosen)[0], ""
        except Exception as exc:  # noqa: BLE001 — an endpoint without a key, a preset whose provider is gone
            return None, f"the summary model {chosen!r} cannot be used: {exc}"

    async def _compaction_rungs(self, state: SessionState) -> list[tuple[Any, str]]:
        """The models a compaction may summarise with, in order: the preset configured for it, then the
        session's own rungs. A preset whose provider is out of quota used to end a manual /compact in a
        bare 500 while the session's own model stood ready; each rung after the first is that fallback.

        With ``model.fallback_to_session`` off, a chosen preset is the only model asked, and one that
        cannot be used refuses the compaction here with the reason and the row to fix it."""
        rungs, _ = self.resolve_model(await self.live.load(state.session.id))
        chosen: list[tuple[Any, str]] = []
        preset_rung, problem = self._compaction_preset_rung()
        if preset_rung is not None:
            chosen.append(preset_rung)  # a cheaper one configured for it
        elif problem:
            if not self.config.model.fallback_to_session:
                await self.setting_notice(state.session.id, kind="compaction", outcome="failed", detail=problem, setting=COMPACTION_MODEL)
                raise CompactionFailed(problem)
            logger.warning("%s; summarising with the session's model", problem)
        if self.config.compaction.preset and not self.config.model.fallback_to_session:
            return chosen
        for rung in rungs:  # the session's own model summarises its own history
            if all((rung[0].endpoint.id, rung[1]) != (p.endpoint.id, m) for p, m in chosen):
                chosen.append(rung)
        return chosen

    async def _compaction_rung(self, state: SessionState) -> tuple[Any, str]:
        return (await self._compaction_rungs(state))[0]

    async def session_window(self, state: SessionState) -> int:
        """The window of the model this session runs on, in tokens; 0 when no model is usable."""
        try:
            _, preset = self.resolve_model(await self.live.load(state.session.id))
            return int(state.context_window or preset.context_window or 0)
        except Exception:  # noqa: BLE001 — no usable model is reported elsewhere
            return int(state.context_window or 0)

    async def summarise_with_fallback(self, state: SessionState, history: Sequence[Message], instructions: str = "", *, progress: Callable[..., Awaitable[None]] | None = None) -> str:
        """One summary of ``history`` from the first model of the compaction's ladder that answers.

        The ladder is the configured summary model, then the session's own rungs; a model that is out
        of quota, refuses or times out hands over to the next, and the operator hears once when the
        chosen summary model was not the one that answered. Shared by a compaction and by an imported
        session's summary of the turns it does not keep verbatim, so both fall back the same way.
        Raises :class:`CompactionFailed` when no model could summarise.
        """
        session_id = state.session.id
        language = self.config.answer_language if self.config.answer_language != "auto" else operator_language(history)
        observability = LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, run_id=state.run_id, call_purpose="compaction", call_category="compaction")
        exceeded = self.budget_exceeded()
        rungs = [(p, m) for p, m in await self._compaction_rungs(state) if not exceeded or self.provider_costs_nothing(p.endpoint.id, m)]
        if not rungs:
            raise CompactionFailed(f"no model may summarise the history now: the daily budget is exceeded ({exceeded}) and every model that could is a paid one" if exceeded else "no model may summarise the history")
        preset_rung, problem = self._compaction_preset_rung()
        window = await self.session_window(state)
        failures: list[str] = []
        summary = ""
        for index, (provider, model) in enumerate(rungs):
            try:
                with self.providers.hold([provider]):
                    summary = await self._summarise_history(provider, model, history, language=language, instructions=instructions, observability=observability, progress=progress, window=window)
                break
            except LLMError as exc:
                # Out of quota, refused, timed out or too small a window: the next model may well answer.
                failures.append(f"{provider.endpoint.id}/{model}: {exc}"[:400])
                if index == len(rungs) - 1:
                    await self.setting_notice(session_id, kind="compaction", outcome="failed", detail="; ".join(failures), setting=COMPACTION_MODEL)
                    raise CompactionFailed("no model could summarise the history: " + "; ".join(failures)) from exc
                logger.warning("compaction of %s with %s/%s failed (%s); trying %s/%s", session_id, provider.endpoint.id, model, exc, rungs[index + 1][0].endpoint.id, rungs[index + 1][1])
        used = (provider.endpoint.id, model)
        if self.config.compaction.preset and (preset_rung is None or used != (preset_rung[0].endpoint.id, preset_rung[1])):
            # The chosen summary model did not make this summary: the operator hears it once, as a
            # warning with the way to the row, instead of not at all (a silent fallback hides a quota
            # running out) or as a failure (the summary was made).
            await self.setting_notice(
                session_id, kind="compaction", outcome="fallback", model=f"{used[0]}/{used[1]}",
                detail=problem or (failures[0] if failures else "the summary model was skipped"), setting=COMPACTION_MODEL,
            )
        return summary

    async def _compact_progressing(self, state: SessionState, history: list[Message], tail: list[Message], instructions: str, reason: str, *, own_task_ok: bool, summary: str | None = None) -> str:
        session_id = state.session.id
        if summary is None:
            summary = await self.summarise_with_fallback(state, history, instructions, progress=lambda **f: self._compaction_progress(state, **f))
            await self._compaction_progress(state, stage="writing")
        # What the operator said is written by code, never by the summariser: rules do not decay.
        # Their room is a share of the window as well as a fixed bound: a summary that quoted every
        # long message whole could itself fill the window past the trigger, and compact again each turn.
        window = int((await self.context_status(state)).get("window") or 0)
        share = min(OPERATOR_QUOTES_WINDOW_SHARE, compaction_ratio(self.config.compaction, window) / 4 or OPERATOR_QUOTES_WINDOW_SHARE)
        room = min(OPERATOR_QUOTES_MAX_CHARS, int(window * CHARS_PER_TOKEN * share)) if window else OPERATOR_QUOTES_MAX_CHARS
        summary = self.redactor.redact(summary + operator_quotes(history, room=room) + identifier_index(history) + verbatim_tail(history, room=room))
        busy = state.running and not (own_task_ok and state.task is asyncio.current_task())
        if busy or state.pending is not None:  # a run resumed from a snapshot meanwhile
            raise RuntimeError("the session became busy during compaction; nothing was changed")
        backup = state.workspace / f".history-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        try:
            backup.write_text("\n".join(m.model_dump_json() for m in history) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("could not write the history backup %s", backup, exc_info=True)
        await self.sessions.append_transcript(session_id, history, from_history=True)
        seqs = await self.sessions.transcript_seqs(session_id, [self.sessions.transcript_key(m) for m in history])
        archive_note = f"\n\n[archived turns seq {seqs[0]}–{seqs[-1]}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]" if seqs else ""
        message = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=f"<compacted-turn id='{reason}'>{summary}{archive_note}</compacted-turn>")],
            metadata={
                COMPACTION_SUMMARY_METADATA_KEY: True,
                # The operator's passages the summary quotes, named for the core's ledger and for the
                # next compaction here, which would otherwise read this summary as a model's words.
                OPERATOR_WORDS_METADATA_KEY: [text for _, text in operator_passages(history)][-OPERATOR_PASSAGES_KEPT:] or False,
                "daedalus.compaction": {"reason": reason, "messages": len(history), "kept": len(tail), "at": datetime.now(UTC).isoformat()},
                "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": seqs} if seqs else {"seqs": []},
            },
        )
        # The core's compaction ledger — exact values, files and operator words, recorded by code as
        # the run compacted — is carried over as it is, never through the summariser: a record a model
        # rewrites on every compaction compounds its errors. The core rebuilds it on its next pass.
        ledgers = [m for m in history if is_ledger(m)]
        rebuilt = [message, *ledgers[-1:], *tail]
        # The capture must be durable before any working-history rewrite. If it cannot be recorded,
        # this compaction fails with the old context intact instead of leaving an untraceable summary.
        await KnowledgeStore(self.db).capture_compaction(
            session_id, state.run_id or "", state.project.id if state.project is not None else None,
            summary, seqs,
        )
        state.persist_gen += 1  # any persist captured before this point describes a history that is gone
        await self._reset_observed_prompt(state, before=[*history, *tail], after=rebuilt)
        if state.engine is not None:
            engine = state.engine
            engine.history = rebuilt
            engine.compact_checkpoint = None  # type: ignore[attr-defined]
            # The core gates automatic compaction on the last measured prompt size; that
            # measurement described the history that no longer exists.
            engine.last_observed_prompt_tokens = 0
            engine.compaction_state = CompactionState()
        state.history_keys = [self.sessions.transcript_key(m) for m in rebuilt]
        await self.sessions.replace_messages(session_id, TENANT, rebuilt)
        _forget_persisted(state)
        await self.sessions.append_transcript(session_id, [message])
        return summary

    async def _reset_observed_prompt(self, state: SessionState, *, before: Sequence[Message], after: Sequence[Message]) -> None:
        """Re-estimate the prompt for the history that now exists, and retire the measurements of the one that does not.

        The trigger compares the last prompt the provider counted against the window. A
        compaction does not make a call, so that count still described the history it just
        replaced — and the next turn compacted again, summarising the kept tail for forty to
        eighty seconds and changing nothing. Nearly half of the compactions in the log were
        that. The new size is the measured one scaled by how much of the history survived.
        Scaling rather than re-counting is deliberate: the estimator's calibration cancels out
        of the ratio, so the answer errs only in the direction that costs nothing — a little
        low, which delays the next compaction by a turn at most, where a little high would pay
        for one that compacts nothing all over again.
        """
        row = await self.db.fetchone("SELECT coalesce(max(seq), 0) seq FROM usage_events WHERE session_id = ?", (state.session.id,))
        measured = int((await self.context_status(state))["tokens"])
        state.usage_floor_seq = int(row["seq"]) if row else 0
        was = history_tokens(before)
        scaled = round(measured * history_tokens(after) / was) if was else 0
        # A rewrite that did not shrink the history says nothing about the prompt getting smaller.
        state.observed_prompt_tokens = min(measured, scaled)
        await self.db.kv_set(f"context_measurement:{state.session.id}", {"floor": state.usage_floor_seq, "tokens": state.observed_prompt_tokens})
        # Every rewrite of the history between runs comes through here: a compaction, a revert, a clear.
        await self._memory_history_rewritten(state, after)

    async def _summarise_history(self, provider: Any, model: str, history: Sequence[Message], *, language: str, instructions: str, observability: LLMObservabilityContext, progress: Callable[..., Awaitable[None]] | None = None, window: int = 0) -> str:
        """One structured summary of ``history``, built on the summary it starts with.

        The previous summary goes to the summariser whole, as the base to update (see
        :mod:`daedalus.host.summaries`). The transcript after it goes whole as well: in one call
        when it fits ``chunk_tokens``, otherwise as parts summarised in parallel into notes, the
        notes merged in rounds while they are still too long for one call, and the last merge
        made onto the base. ``progress`` hears how many parts there are, each one finishing, then
        the merge; a compaction of a long history takes minutes and the operator watches it."""

        async def report(**fields: Any) -> None:
            if progress is not None:
                await progress(**fields)

        cfg = self.config.compaction
        focus = f"\n\nThe operator asks to focus on: {instructions.strip()}" if instructions.strip() else ""
        words = summaries.summary_words(cfg.max_words, window)
        rendered = summaries.summary_input(history)
        base = rendered.previous
        parts = summaries.split_transcript(rendered.transcript, cfg.chunk_tokens)
        await report(parts_total=len(parts), parts_done=0, stage="summarising")
        if len(parts) == 1:
            prompt = summaries.compact_prompt(language=language, max_words=words, has_base=bool(base)) + focus
            return await summary_calls.SummaryGate(1).call(lambda: self._summary_call(provider, model, prompt, summaries.with_base(base, rendered.transcript), observability, max_tokens=summaries.summary_max_tokens(words)))
        part_words = max(400, words // 2)
        # Wide enough that thirty parts are three waves rather than five, never wider than the parts
        # themselves; the gate narrows itself when the provider answers with a rate limit.
        gate = summary_calls.SummaryGate(min(cfg.parallel_calls, len(parts)))
        done = 0

        async def note(prompt: str, body: str) -> str:
            return await gate.call(lambda: self._summary_call(provider, model, prompt + focus, body, observability, strict=False, max_tokens=summaries.summary_max_tokens(part_words)))

        async def part_note(index: int, part: str) -> str:
            nonlocal done
            text = await note(summaries.CHUNK_PROMPT.format(index=index + 1, total=len(parts), language=language, max_words=part_words), part)
            done += 1
            await report(parts_done=done)
            return text

        notes = [text for text in await summary_calls.all_or_nothing(part_note(i, part) for i, part in enumerate(parts)) if text]
        limit = cfg.chunk_tokens * summaries.CHARS_PER_TOKEN
        for _ in range(summaries.MERGE_ROUNDS_MAX):
            # A history of a million tokens leaves thirty parts whose notes alone exceed one call; they
            # are merged in groups first, so the last merge reads all of them rather than the newest.
            if len(summaries.joined_notes(notes)) <= limit or len(notes) < 2:
                break
            groups = summaries.pack(notes, limit)
            await report(stage="merging")
            prompt = summaries.MERGE_NOTES_PROMPT.format(language=language, max_words=part_words)
            notes = await summary_calls.all_or_nothing(note(prompt, summaries.joined_notes(group)) if len(group) > 1 else asyncio.sleep(0, group[0]) for group in groups)
        await report(stage="merging")
        prompt = summaries.merge_prompt(language=language, max_words=words, has_base=bool(base)) + focus
        return await gate.call(lambda: self._summary_call(provider, model, prompt, summaries.with_base(base, summaries.joined_notes(notes)), observability, max_tokens=summaries.summary_max_tokens(words)))

    async def _summary_call(self, provider: Any, model: str, prompt: str, body: str, observability: LLMObservabilityContext, *, strict: bool = True, max_tokens: int = 6000) -> str:
        """One summariser call; ``strict`` is the final summary, checked for its sections and asked again once.

        A part's note or a merge of notes (``strict`` false) is never checked: it has none of the final
        summary's sections by design, and checking it logged a rejection and an acceptance for every part."""
        candidate = ""
        problem = ""
        for attempt in range(2 if strict else 1):
            reminder = f"\n\nYour previous attempt was rejected: {problem}. Produce every section, each exactly once, in the given order." if problem else ""
            request = LLMRequest(
                model=model,
                messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt + reminder + "\n\n" + body)])],
                max_tokens=max_tokens,
                temperature=0.2,
                extra={"enable_thinking": False},
                observability=observability,
            )
            candidate = (await self._summary_request(provider, request)).strip()
            if not strict:
                break
            problem = summaries.validate_summary_sections(candidate)
            if not problem:
                return candidate
            logger.warning("compaction summary rejected (attempt %d): %s", attempt + 1, problem)
        if not candidate:
            raise RuntimeError("the model returned an empty summary")
        if problem:
            logger.warning("compaction summary accepted without the fixed sections: %s", problem)
        return candidate  # a usable summary beats a session the operator cannot compact

    async def _summary_request(self, provider: Any, request: LLMRequest) -> str:
        """One streamed summariser call, asked again when it stalled or failed in a way that passes.

        A compaction once died on a single HTTP 500 from the provider and the session went on
        growing to nine tenths of its window. A transient failure — a 5xx, a dropped stream — is
        asked again after a pause, twice; a call that went silent is asked again once at once (see
        :mod:`daedalus.host.summary_calls` for why silence, not length, is what ends one). What
        still fails goes back to the caller, which tries the next model of the ladder or reports
        the compaction as failed with the history untouched."""
        silence = self.config.compaction.call_timeout_seconds
        ceiling = summary_calls.ceiling_for(request.max_tokens or 6000, silence)
        stalled = False
        pauses = list(SUMMARY_RETRY_PAUSES)
        while True:
            try:
                return await summary_calls.stream_text(provider, request, silence=silence, ceiling=ceiling)
            except summary_calls.SummaryStalled as exc:
                if stalled or not exc.retryable:
                    raise
                stalled = True
                logger.warning("compaction summariser call %s; asking once more", exc)
            except LLMError as exc:
                # A rate limit is the gate's to pace (see SummaryGate); it is not a hiccup to retry here.
                if not pauses or not exc.retryable or isinstance(exc, LLMRateLimitError):
                    raise
                pause = pauses.pop(0)
                logger.warning("compaction summariser call failed (%s); asking again in %.0fs", exc, pause)
                await asyncio.sleep(pause)

    # -- checkpoints, revert, fork ------------------------------------------------------

    async def checkpoint(self, state: SessionState, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        """Snapshot the workspace (bounded by ``ops.checkpoint_max_gb``); returns the commit id or None.

        A project is not snapshotted unless the operator asked for it. A session workspace holds what
        one agent made; a project root is the operator's own repository, and committing it into
        its snapshot store before every turn and after every run is a cost the undo does not repay on a
        tree that already has a history of its own. Switched on in the project's settings, it behaves
        exactly as a workspace does, size cap included.
        """
        if state.project is not None and not state.project.settings.snapshots:
            return None
        if state.services is not None and not state.services.workspace_writable:
            # Nothing the session does can change a folder it may not write, and the snapshot store
            # would be the one thing written into it.
            return None
        limit = self.config.ops.checkpoint_max_gb
        try:
            # One walk answers both the size cap and the excludes; it used to be three.
            scan = await asyncio.to_thread(scan_workspace, state.workspace)
            if limit and scan.size > limit * 1e9:
                if not state.checkpoint_capped:
                    state.checkpoint_capped = True
                    logger.warning("session %s: workspace exceeds ops.checkpoint_max_gb=%s; no snapshots, revert restores the history only", state.session.id, limit)
                return None
            state.checkpoint_capped = False
            sha = await Checkpoints(state.workspace, self.settings.checkpoints_dir).snapshot(f"{kind} seq={seq} run={run_id}", scan=scan)
        except (CheckpointError, OSError) as exc:
            logger.warning("checkpoint failed for %s: %s", state.session.id, exc)
            return None
        await self.db.execute(
            "INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, ?, ?, ?, ?)",
            (state.session.id, seq, run_id, kind, sha, datetime.now(UTC).isoformat()),
        )
        return sha

    async def checkpoint_before(self, session_id: str, seq: int) -> str | None:
        """The snapshot taken right before the operator turn at transcript ``seq``."""
        row = await self.db.fetchone("SELECT sha FROM checkpoints WHERE session_id = ? AND kind = 'before' AND seq = ? ORDER BY id DESC LIMIT 1", (session_id, seq))
        return row["sha"] if row else None

    async def list_checkpoints(self, session_id: str) -> dict[str, Any]:
        """The snapshots this session can still be put back to, and what retention took away.

        The app asks before it offers an undo: a turn whose snapshot has been dropped would revert
        the history and quietly leave the files where they are, which is not what "revert" reads as.
        """
        rows = await self.db.fetchall("SELECT seq, run_id, kind, sha, at FROM checkpoints WHERE session_id = ? ORDER BY id", (session_id,))
        cut = await self.db.fetchone("SELECT removed_before, removed, at FROM checkpoint_retention WHERE session_id = ?", (session_id,))
        ops = self.config.ops
        return {
            "checkpoints": [{"seq": r["seq"], "run_id": r["run_id"], "kind": r["kind"], "sha": r["sha"], "at": r["at"]} for r in rows],
            "total": len(rows),
            "pruned": cut is not None,
            "pruned_before": cut["removed_before"] if cut else None,
            "removed": int(cut["removed"]) if cut else 0,
            "note": "older checkpoints were removed by retention" if cut is not None else "",
            "keep_days": ops.checkpoint_keep_days,
            "keep_last": ops.checkpoint_keep_last,
        }

    async def prune_checkpoints(self) -> RetentionReport:
        """Bring the snapshot stores inside the configured bounds; returns what the pass freed."""
        return await self.checkpoint_retention.run(RetentionBounds.from_ops(self.config.ops))

    async def revert(self, session_id: str, seq: int) -> dict[str, Any]:
        """Permanently remove this operator turn and its tail; restore existing file checkpoints."""
        return await self._cut_history(session_id, seq, retry=False)

    async def retry(self, session_id: str, seq: int) -> dict[str, Any]:
        """Replace an assistant message and its entire tail in the same session, without a backup."""
        return await self._cut_history(session_id, seq, retry=True)

    async def _cut_history(self, session_id: str, seq: int, *, retry: bool) -> dict[str, Any]:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.submit_lock, state.lock:
            await self._wait_until_quiet(state)
            pending = [t for t in state.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            row = await self.sessions.transcript_row(session_id, seq)
            if row is None:
                raise ValueError(f"no transcript turn {seq}")
            key, target = row
            expected = MessageRole.assistant if retry else MessageRole.user
            if target.role is not expected or target.metadata.get("daedalus.origin") in ("core", "revert", "clear") or target.metadata.get(COMPACTION_SUMMARY_METADATA_KEY):
                raise ValueError("retry requires an assistant message; revert requires an operator turn")
            if retry:
                if not self.config.has_model:
                    raise NoModelConfigured
                if self.shutting_down or self.recovering or (self.settings.state_dir / "dependencies" / "maintenance").exists():
                    raise RuntimeError("the bot is restarting; try again when it is ready")
                writes = state.services is None or state.services.workspace_writable
                if not state.workspace.is_dir() or (writes and not os.access(state.workspace, os.W_OK)):
                    raise RuntimeError("the working directory is not writable")
                rungs, _ = self.resolve_model(await self.live.load(session_id))
                provider_id = rungs[0][0].endpoint.id if rungs else None
                model = rungs[0][1] if rungs else None
                exceeded = self.budget_exceeded()
                if exceeded and not self.provider_costs_nothing(provider_id, model):
                    raise RuntimeError(f"daily budget exceeded ({exceeded})")
                breach = await self.cap_breach(state, provider_id, model)
                if breach is not None:
                    raise RuntimeError(breach[1])
            history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
            keys = [self.sessions.transcript_key(m) for m in history]
            if key in keys:
                kept = history[:keys.index(key)]
            else:
                # A newer summary may contain the deleted answer. Rebuild only from the prefix,
                # including summaries that already existed there, never from the current summary.
                prefix = await self.sessions.list_transcript(session_id, before_seq=seq)
                cleared = max((int(m.metadata["daedalus.seq"]) for m in prefix if m.metadata.get("daedalus.origin") == "clear"), default=0)
                excluded: set[int] = set()
                for message in prefix:
                    excluded.update(int(n) for n in (message.metadata.get("daedalus.archived") or {}).get("seqs", []))
                    if message.metadata.get("daedalus.origin") == "revert":
                        start = int((message.metadata.get("daedalus.revert") or {}).get("seq", 0))
                        excluded.update(range(start, int(message.metadata["daedalus.seq"])))
                kept = [m for m in prefix if int(m.metadata["daedalus.seq"]) > cleared and int(m.metadata["daedalus.seq"]) not in excluded and m.metadata.get("daedalus.origin") not in ("revert", "clear") and not m.metadata.get("daedalus.queued")]
            if retry and not kept:
                raise ValueError("there is no input before this assistant message")
            sha = await self.checkpoint_before(session_id, seq) if not retry else None
            restored = False
            untouched: list[str] = []
            if sha:
                try:
                    untouched = await Checkpoints(state.workspace, self.settings.checkpoints_dir).restore(sha, record=False)
                    restored = True
                except CheckpointError as exc:
                    logger.warning("workspace restore failed: %s", exc)
            state.persist_gen += 1
            state.persist_epoch += 1
            dropped, through = await self.sessions.truncate_history(session_id, TENANT, seq, kept)
            await self._reset_observed_prompt(state, before=history, after=kept)
            if state.engine is not None:
                state.engine.history = kept
                state.engine.last_observed_prompt_tokens = 0
                state.engine.compaction_state = CompactionState()
            state.history_keys = [self.sessions.transcript_key(m) for m in kept]
            _forget_persisted(state)
            # Input queued during the undone turns and any run snapshot that could resume them are pre-revert by definition.
            await self.live.save_queues(session_id, [], [])
            await self.steer_changed(session_id, reason="cleared")
            state.last_error_kind = ""
            state.last_error_message = ""
            state.last_error_permanent = False
            state.run_outcome = None
            state.run_cause = None
            result = {"seq": seq, "through": through, "dropped": dropped, "workspace_restored": restored, "untouched": untouched, "kept": len(kept)}
            await self._notify_sinks(session_id, HostEvent(HostEventType.HISTORY_CUT, state.run_id or "", result))
            if retry:
                result["run_id"] = await self._start_run_locked(state, None)
            return result

    async def clear_history(self, session_id: str) -> dict[str, Any]:
        """Start the session over with an empty working history: the workspace, the brief, the model, the
        loop and every other setting stay; the transcript keeps the old turns and a marker says they are gone.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        async with state.lock:
            await self._wait_until_quiet(state)
            pending = [t for t in state.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(session_id, TENANT, limit=10_000))
            if history:
                backup = state.workspace / f".history-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
                try:
                    backup.write_text("\n".join(m.model_dump_json() for m in history) + "\n", encoding="utf-8")
                except OSError:
                    logger.warning("could not write the history backup %s", backup, exc_info=True)
                await self.sessions.append_transcript(session_id, history, from_history=True)
            state.persist_gen += 1
            await self._reset_observed_prompt(state, before=history, after=[])
            if state.engine is not None:
                state.engine.history = []
                state.engine.last_observed_prompt_tokens = 0
                state.engine.compaction_state = CompactionState()
            state.history_keys = []
            await self.sessions.replace_messages(session_id, TENANT, [])
            _forget_persisted(state)
            await self.live.save_queues(session_id, [], [])
            await self.steer_changed(session_id, reason="cleared")
            for run in await self.db.fetchall("SELECT id FROM runs WHERE session_id = ?", (session_id,)):
                await self.events.delete_snapshot(run["id"])
            marker = Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text=f"[history cleared: {len(history)} message(s) left the working history; the project files and the session's settings stay]")],
                metadata={"daedalus.origin": "clear", "daedalus.clear": {"dropped": len(history)}},
            )
            await self.sessions.append_transcript(session_id, [marker])
            return {"dropped": len(history)}

    async def fork(self, source_id: str, seq: int, *, title: str | None = None) -> dict[str, Any]:
        """Make a new session from the source's history before transcript ``seq``, or nothing at all.

        The session is created first because the copy needs somewhere to go, and a failure after
        that used to leave an empty session in the project: a fork pressed during a run was refused
        by ``fork_into`` and the operator found a blank chat next to the one they wanted to copy.
        Whatever goes wrong now — a refusal, a full disk, a cancelled request — the new session is
        deleted again before the error is passed on.
        """
        source = await self.get_state(source_id)
        if source is None:
            raise KeyError(source_id)
        name = (title or f"{re.sub(r'\s*\(fork @\d+\)$', '', source.session.title)} (fork @{seq})")[:128]
        # A fork of a project session stays in the project: the two sessions share the root, which
        # is what "several agents work in one project" already means, and the fork keeps the wall.
        # It is opened on the site, so it does not grow a topic or speak in the private chat.
        target = await self.create_session(
            name,
            metadata={"forked_from": {"session_id": source_id, "seq": seq}, "telegram_detached": True},
            project_id=source.project.id if source.project is not None else None,
            own_directory=bool(source.metadata.get("directory")),
            folder_id=source.metadata.get("folder_id") or None,
        )
        try:
            result = await self.fork_into(source_id, seq, target)
        except BaseException:
            # Shielded: a cancelled request must still take its half-made session with it.
            await asyncio.shield(self.delete_session(target.session.id, delete_workspace=True))
            raise
        return {"id": target.session.id, "title": name, **result}

    async def pin_fork_identity(self, target_id: str, source_id: str) -> dict[str, Any]:
        """Have ``target``'s requests start with the bytes ``source``'s next request would start with.

        Call after :meth:`fork_into`. The system prompt is written from the source's title, working
        directory and notes, the source's loaded tools and stale-trimmed results are carried, and the
        run's requests are routed by the source's conversation, so a provider's prefix cache serves the
        fork the history it shares with the source. A fork of a fork keeps the first source's identity.
        """
        source = await self.get_state(source_id)
        target = await self.get_state(target_id)
        if source is None or target is None:
            raise KeyError(source_id if source is None else target_id)
        pinned = fork_identity(source.metadata)
        if pinned is None:
            trimmed = sorted(source.engine.trimmed_tool_result_ids) if source.engine is not None else []
            pinned = {"title": source.session.title, "workspace": str(self.work_dir(source)),
                      "project": source.project.name if source.project is not None else "",
                      "notes": self.notes_for(source, window=int(source.context_window or 0)),
                      "trimmed": trimmed, "cache_key": source_id}
        copied = {key: source.metadata[key] for key in FORK_COPIED_KEYS if key in source.metadata}
        for meta in (target.metadata, target.session.metadata):
            meta[FORK_IDENTITY_KEY] = pinned
            meta.update(copied)
        await self.sessions.update_metadata(target_id, target.session.metadata)
        return pinned

    async def fork_into(self, source_id: str, seq: int, target: SessionState, *, copy_workspace: bool = True) -> dict[str, Any]:
        """Give ``target`` the source's history before transcript ``seq`` and the right project files.

        Turns a revert undid and turns a compaction summary already stands for are left out of
        the working history; the archived originals still go into the fork's transcript, and
        the summary is re-pointed at the fork's own seqs so HistoryExpand keeps working there.

        A source in the middle of a run is forked from its last settled point: nothing of the run
        in flight (its prompt included) is copied, and the run is neither waited for nor touched.
        A fork only reads the source, so refusing it the way an undo is refused protected nothing.

        Two sessions in the same project share one directory and nothing is copied at all. Where a
        copy does happen it is a whole directory tree, so it is measured first and refused above
        ``ops.checkpoint_max_gb`` rather than written into the state directory unbounded.
        ``copy_workspace=False`` copies the history alone: a staff member's fork works in a git
        worktree of its own, and the source's tree copied over it would replace the file that links
        the worktree to its repository.
        """
        source = await self.get_state(source_id)
        if source is None:
            raise KeyError(source_id)
        async with source.lock:
            live_run = self._live_run_of(source)
            if not live_run:
                try:
                    await self._wait_until_quiet(source, what="the source session")
                except RuntimeError:
                    # A run that started while the last one was being saved is forked around like
                    # any other; a save that never finished is still refused.
                    live_run = self._live_run_of(source)
                    if not live_run:
                        raise
            pending = [t for t in source.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            rows = await self.sessions.list_transcript(source_id)
            if live_run:
                started = [int(m.metadata.get("daedalus.seq", 0)) for m in rows if m.metadata.get("daedalus.run_id") == live_run]
                if started:
                    seq = min(seq, min(started))
            reverted: set[int] = set()
            archived: set[int] = set()
            for m in rows:
                own = int(m.metadata.get("daedalus.seq", 0))
                if own >= seq:
                    continue
                if m.metadata.get("daedalus.origin") == "revert":
                    cut = int((m.metadata.get("daedalus.revert") or {}).get("seq") or 0)
                    reverted.update(range(cut, own))
                if m.metadata.get("daedalus.archived"):
                    archived.update(int(x) for x in (m.metadata["daedalus.archived"].get("seqs") or []))
            before = [m for m in rows if int(m.metadata.get("daedalus.seq", 0)) < seq and int(m.metadata.get("daedalus.seq", 0)) not in reverted and m.metadata.get("daedalus.origin") != "revert"]
            transcript = []
            for original in before:
                copied = original.model_copy(update={"metadata": {k: v for k, v in original.metadata.items() if k != "daedalus.seq"}})
                transcript.append(await self.media.clone_references(source_id, target.session.id, copied))
            await self.sessions.append_transcript(target.session.id, transcript)
            keys = [self.sessions.transcript_key(m) for m in transcript]
            new_seqs = await self.sessions.transcript_seqs(target.session.id, keys)
            mapping = {int(m.metadata.get("daedalus.seq", 0)): new for m, new in zip(before, new_seqs, strict=False)}
            history: list[Message] = []
            for original, message in zip(before, transcript, strict=True):
                own = int(original.metadata.get("daedalus.seq", 0))
                if own in archived:
                    continue
                if message.metadata.get("daedalus.archived"):
                    message = repoint_summary(message, mapping)
                    await self.sessions.replace_transcript_message(target.session.id, self.sessions.transcript_key(original), message)
                history.append(message)
            # A fork taken mid-turn must not start on a tool call nobody answered.
            while history and history[-1].role is MessageRole.assistant and any(isinstance(b, ToolUseBlock) for b in history[-1].content_blocks):
                history.pop()
            await self.sessions.replace_messages(target.session.id, TENANT, history)
            target.history_keys = [self.sessions.transcript_key(m) for m in history]
            _forget_persisted(target)
            copied = False
            if not copy_workspace:
                pass
            elif target.workspace == source.workspace:
                # Both sessions are in the same project: the folder is the operator's and there is one
                # of it. Copying it would take the repository out of the folder they chose, unbounded,
                # and leave the fork working on a stale duplicate of it.
                pass
            elif source.workspace.exists():
                scan = await asyncio.to_thread(scan_workspace, source.workspace)
                limit = self.config.ops.checkpoint_max_gb
                if limit and scan.size > limit * 1e9:
                    raise RuntimeError(
                        f"{source.workspace} is {scan.size / 1e9:.1f} GB, over ops.checkpoint_max_gb={limit}; "
                        "a fork copies the whole directory, so this one is refused rather than written twice"
                    )
                await asyncio.to_thread(shutil.copytree, source.workspace, target.workspace, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".agents"))
                copied = True
                # The store is not in the folder any more, so the copy above did not carry it.
                checkpoints = await Checkpoints(source.workspace, self.settings.checkpoints_dir).copy_to(target.workspace, self.settings.checkpoints_dir)
                sha = await self.checkpoint_before(source_id, seq)
                if sha:
                    try:
                        await checkpoints.restore(sha)
                    except CheckpointError as exc:
                        logger.warning("fork workspace restore failed: %s", exc)
        await self.sessions.update_metadata(target.session.id, {**target.session.metadata, "forked_from": {"session_id": source_id, "seq": seq}})
        return {"messages": len(history), "workspace_copied": copied, "workspace_shared": target.workspace == source.workspace}

    async def closed_topic_sessions(self) -> list[dict[str, Any]]:
        """Sessions whose topic is closed but whose data is still on disk."""
        rows = await self.db.fetchall(
            "SELECT t.session_id, t.title, t.chat_id, t.thread_id FROM topics t WHERE t.closed_at IS NOT NULL"
        )
        out = []
        for row in rows:
            state = await self.get_state(row["session_id"])
            size = 0
            if state is not None and state.workspace.exists():
                size = sum(f.stat().st_size for f in state.workspace.rglob("*") if f.is_file())
            out.append({"session_id": row["session_id"], "title": row["title"], "chat_id": row["chat_id"], "thread_id": row["thread_id"], "bytes": size})
        return out

    async def orphan_workspaces(self) -> list[Path]:
        """Workspace directories that no session, schedule or standing task refers to.

        The one definition every caller shares: a session's own folder, any folder named in a
        session's metadata (scheduled and heartbeat runs), and every schedule workspace are kept.
        """
        rows = await self.db.fetchall("SELECT id, metadata FROM sessions")
        known = {r["id"] for r in rows}
        known_paths: set[Path] = set()
        for r in rows:
            try:
                metadata = json.loads(r["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            ws = metadata.get("workspace") if isinstance(metadata, dict) else None
            if ws:
                known_paths.add(Path(ws).resolve())
            home = home_of(metadata) if isinstance(metadata, dict) else ""
            if home:
                known_paths.add((self.settings.workspaces_dir / home).resolve())
            known_paths.add((self.settings.workspaces_dir / host_home_name(r["id"])).resolve())
        sched = await self.db.fetchall("SELECT workspace FROM schedules")
        known_paths.update(Path(r["workspace"]).resolve() for r in sched)
        known_paths.add((self.settings.workspaces_dir / "heartbeat").resolve())
        known_paths.update(target.resolve() for target in self.moving_folders.values())
        # A system project's folder lives under the workspaces root and is not a workspace nobody
        # refers to: the companion's agents work in it, and a sweep would take their files with it.
        for project in await self.projects.list():
            for folder in project.folders:
                with suppress(OSError):
                    known_paths.add(folder.path.resolve())
        out: list[Path] = []
        if not self.settings.workspaces_dir.exists():
            return out
        for entry in sorted(self.settings.workspaces_dir.iterdir()):
            if not entry.is_dir() or entry.name in known or any(entry.resolve() == path or entry.resolve() in path.parents for path in known_paths):
                continue
            out.append(entry)
        return out

    async def sweep_orphan_workspaces(self) -> list[str]:
        """Delete the directories :meth:`orphan_workspaces` reports."""
        removed: list[str] = []
        for entry in await self.orphan_workspaces():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return removed

    async def reload_project(self, project: Project | None, project_id: str) -> None:
        """Refresh the editable project value held by loaded sessions."""
        for state in self._states.values():
            if state.project is None or state.project.id != project_id:
                continue
            state.project = project
            state.workspace = self.workspace_of(state.session.id, dict(state.session.metadata), project)
            with suppress(OSError):
                _ensure_inbox(state.workspace, self.inbox_folder(state.session.id, dict(state.session.metadata), project))
            self.register_services(state)

    async def set_home(self, session_id: str, home: str) -> None:
        """Run a session in its own directory under the workspaces root (``home``) from now on.

        For a session stored with a working directory this process cannot reach; its project and its
        folders are unchanged. A loaded session is moved at once, its walls with it."""
        if not _HOME_RE.fullmatch(home) or home in (".", ".."):
            raise ValueError(f"{home!r} is not a directory name under the workspaces root")
        session = await self.sessions.get(session_id, TENANT)
        metadata = {**dict(session.metadata), HOME_KEY: home}
        await self.sessions.update_metadata(session_id, metadata)
        self.unloadable_sessions.pop(session_id, None)
        state = self._states.get(session_id)
        if state is None:
            return
        state.session.metadata.clear()  # the Session model is frozen; its dict is the thing that is kept
        state.session.metadata.update(metadata)
        state.metadata[HOME_KEY] = home
        state.workspace = self.workspace_of(session_id, metadata, state.project)
        with suppress(OSError):
            _ensure_inbox(state.workspace, None)
        self.register_services(state)

    async def attach_project(self, session_id: str, project: Project, *, own_directory: bool = False) -> SessionState:
        """Move a session to a project root or to its private child in that project."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        for coach_id in [*await prompt_coach.coaches_of(self, session_id), *await btws_of(self, session_id)]:
            # A prompt coach or a side question reads where its chat works; left behind it would read the
            # old place, and keep a chat's scratch project alive that the move has emptied.
            await self.delete_session(coach_id, delete_workspace=False)
        metadata = dict(state.session.metadata)
        if own_directory:
            metadata["directory"] = f".agents/{session_id}"
        else:
            metadata.pop("directory", None)
        # The folder it worked in belongs to the project it leaves; in the new one it starts in the primary.
        metadata.pop("folder_id", None)
        metadata.pop(HOME_KEY, None)
        await self.sessions.update_metadata(session_id, metadata)
        await self.projects.attach(session_id, project.id)
        project = await self._settle_project(project)
        state.session.metadata.clear()  # the Session model is frozen; its dict is the thing that is kept
        state.session.metadata.update(metadata)
        state.metadata = dict(metadata)
        state.project = project
        state.workspace = self.workspace_of(session_id, metadata, project)
        with suppress(OSError):
            _ensure_inbox(state.workspace, self.inbox_folder(session_id, metadata, project))
        self.register_services(state)
        return state

    @staticmethod
    def _live_run_of(state: SessionState) -> str:
        """The run in flight in a session — one working, or one waiting on a question — or ""."""
        return (state.run_id or "") if (state.running or state.pending is not None) else ""

    @staticmethod
    def _settling(state: SessionState) -> bool:
        """The run is over but its snapshot is not on disk yet.

        Not a run — the app draws the session as idle and the next message may be typed into it —
        but a restart or a retention pass landing here would still take the snapshot away from the
        turn that just ended, so everything that asks "is anything going on in this session" is told
        yes until the files are safe.
        """
        return not state.settled.is_set()

    async def _wait_until_quiet(self, state: SessionState, *, what: str = "the session") -> None:
        """Hold until nothing else is writing this session's history or its files.

        One test for every way a session can be rewritten from underneath — undo, clear, fork,
        compaction — because they all depend on the same two things being finished: the history of
        the last turn, and the snapshot of the workspace as that turn left it. A run in flight and a
        question outstanding are refused, as they always were. The third state is the one this
        exists for: the run is over, the app draws the session as idle, and the snapshot of the very
        turn an undo would undo is still being written. That one is waited for rather than refused —
        it is a second or two, and the operator pressed the button on a session that looked idle
        because it *is* idle; what is behind it is ours to finish, not theirs to retry around.

        The wait is bounded (``ops.settle_wait_seconds``): a snapshot on a mount that never answers
        would otherwise wedge the session with nothing on the screen to say why.
        """
        if state.running or state.pending is not None:
            raise RuntimeError(f"{what} is busy; stop the run (or answer the question) first")
        if self._settling(state):
            try:
                async with asyncio.timeout(self.config.ops.settle_wait_seconds):
                    await state.settled.wait()
            except TimeoutError:
                raise RuntimeError(f"{what} is still saving the last turn; nothing may rewrite it until that finishes") from None
            # Nothing here holds ``submit``: a run may have started in the moment we waited.
            if state.running or state.pending is not None:
                raise RuntimeError(f"{what} is busy; stop the run (or answer the question) first")

    def running_run_ids(self) -> set[str]:
        return {s.run_id for s in self._states.values() if s.run_id and (s.running or s.pending is not None or self._settling(s))}

    def busy_sessions(self) -> set[str]:
        """Sessions nothing may be rewritten under: a turn in flight, a question outstanding, or a
        finished turn still being written down — the same test ``revert``, ``clear_history``,
        ``fork`` and ``compact`` wait on."""
        return {sid for sid, state in self._states.items() if state.running or state.pending is not None or self._settling(state)}

    def dependency_installation_busy(self) -> bool:
        """An input still preparing a run must finish before installation closes the run gate."""
        return bool(self.busy_sessions()) or self.idle_work.locked() or any(state.submit_lock.locked() for state in self._states.values())

    def active_sessions(self) -> set[str]:
        """Sessions the operator would call working: a turn in flight or a question outstanding.

        Narrower than :meth:`busy_sessions` on purpose, and the difference is the point. A session
        whose run has ended and whose snapshot is still being written may not be rewritten — so the
        refusals ask the wider question — but it is not *working*, and a list that draws it as
        running contradicts its own screen, which says idle.
        """
        return {sid for sid, state in self._states.items() if state.running or state.pending is not None}

    def sessions_at_work(self) -> set[str]:
        """Sessions nothing may move the files of: :meth:`busy_sessions`, one whose next run is being
        prepared, and one with a background job still running. Wider than a rewrite's test, because a
        job or a run about to start writes the files whatever the history says."""
        preparing = {sid for sid, state in self._states.items() if state.submit_lock.locked()}
        jobs = {sid for sid, jobs in self.jobs.items() if any(getattr(job, "running", False) for job in jobs.values())}
        return self.busy_sessions() | preparing | jobs

    async def refresh_metadata(self, session_id: str) -> None:
        """Read a loaded session's metadata again from its row, after something rewrote the row
        directly (a folder move rewrites the worktree paths in it)."""
        state = self._states.get(session_id)
        if state is None:
            return
        session = await self.sessions.get(session_id, TENANT)
        state.session.metadata.clear()  # the Session model is frozen; its dict is the thing that is kept
        state.session.metadata.update(session.metadata)
        state.metadata = dict(session.metadata)

    def store_occupants(self) -> dict[Path, set[str]]:
        """Which sessions this process holds open in each snapshot store, by the store's own directory.

        Retention reads the ``checkpoints`` rows to find the stores, which is every session that has
        taken a snapshot — and not the one that has not taken its first yet. A subagent sharing its
        leader's workspace is exactly that session, and its before-turn snapshot is a ``git add`` into
        the chain a pass would otherwise consider idle. This is read from the sessions the process
        actually holds, so a store is busy from the moment one of them is loaded in it.
        """
        out: dict[Path, set[str]] = {}
        for session_id, state in self._states.items():
            out.setdefault(store_dir(self.settings.checkpoints_dir, state.workspace), set()).add(session_id)
        return out

    def register_services(self, state: SessionState) -> None:
        hooks = self.service_hooks
        services = SessionServices(
            session_id=state.session.id,
            workspace_dir=state.workspace,
            protected_paths=self.protected_paths(),
            tool_timeout_seconds=self.config.limits.tool_timeout_seconds,
            max_tool_output_chars=self.config.tools.exec.max_output_chars,
            send_file=_bind(hooks.get("send_file"), state.session.id),
            attach_media=lambda items, layout: self.media.stage(state.session.id, state.run_id or "", items, layout=layout),
            spawn_agent=_bind(hooks.get("spawn_agent"), state.session.id),
            schedule=_own_schedules(hooks.get("schedule"), state.session.id) if self.is_companion(state) else hooks.get("schedule"),
            self_propose=hooks.get("self_propose"),
            self_apply=hooks.get("self_apply"),
            self_rebuild=hooks.get("self_rebuild"),
            self_rollback=hooks.get("self_rollback"),
            progress=_bind(hooks.get("progress"), state.session.id),
            writable=[q for p in (state.session.metadata.get("worktrees") or []) if Path(str(p)).is_absolute() for q in worktree_writable_paths(Path(str(p)))],
            # The walls are what the sandbox binds writable and what ``resolve`` holds every path to:
            # every local folder of the project to read, the ones not marked read-only to write, or
            # the session's own directory alone when it has one.
            walls=self.walls_of(state),
            extra={"skill_store": self.skills_for(state), "manager": self, "vision": _LiveVision(self), "jobs": self.jobs.setdefault(state.session.id, {})},
        )
        host_folder = self.host_folder_of(state.session.id, state.session.metadata, state.project)
        if host_folder is not None:
            self._drive_host(services, state, host_folder)
        elif services.walls is not None and not services.workspace_writable:
            # The host writes a read-only folder's command logs to the state volume instead, and the
            # session reads them there: a readable wall, never a writable one, so the agent sees
            # its jobs' output without being able to write into the installation.
            services.log_root = session_scratch_dir(self.settings.state_dir, state.session.id)
            services.walls = Walls(readable=(*services.walls.readable, services.log_root), writable=services.walls.writable)
        if state.project is not None and self.is_orchestrator(state):
            # What an orchestrator delivers is read where the project's files are, not through its
            # walls: an orchestrator of a host project runs from a directory of its own, and the
            # member's document it was asked to attach was on the host, out of every wall it had.
            services.extra["deliver"] = _bind(self.deliverable, state.session.id)
        state.services = services
        locator.register(services)

    async def deliverable(self, session_id: str, raw: str) -> Path:
        """The file an orchestrator delivers, as a file of this process.

        ``raw`` must lie inside one of the project's folders (a staff worktree in one included) and
        outside git's metadata and credential files, judged on the text by
        :func:`daedalus.host.deliverables.locate` and again on the real path where the file lives. A
        local file is handed back as it is; a host file is fetched through the bridge, bounded, into
        a copy named by its bytes. ``FileRefused`` says why a path is not delivered and
        ``FileNotFoundError`` that nothing is there.
        """
        state = await self.get_state(session_id)
        project = state.project if state is not None else None
        if project is None:
            raise FileRefused("this session has no project to deliver a file from")
        folder, path = locate_deliverable(project, raw)
        allowed = [str(f.path) for f in project.folders if f.env == folder.env]
        if folder.local(self.projects.local_env):
            real = Path(os.path.realpath(path))
            root = next((Path(os.path.realpath(r)) for r in allowed if real == Path(os.path.realpath(r)) or Path(os.path.realpath(r)) in real.parents), None)
            if root is None:
                raise FileRefused(f"{path} leads outside the project's folders")
            # A link inside a folder that points into its .git is judged by where it lands.
            if (part := secret_part(str(real.relative_to(root)))) is not None:
                raise FileRefused(f"{path} leads into {part}, which holds the repository's metadata or credentials and is never delivered")
            if not real.is_file():
                raise FileNotFoundError(path)
            return real
        data = await Handoff(self.files, local_env=self.projects.local_env, host=lambda: self.host_bridge).read(folder.env, path, allowed=allowed)
        keep = Path(tempfile.gettempdir()) / "daedalus-deliveries" / re.sub(r"[^A-Za-z0-9_-]", "_", session_id) / hashlib.sha256(data).hexdigest()[:16]
        keep.mkdir(parents=True, exist_ok=True)
        copy = keep / (PurePosixPath(path).name or "file")
        if not copy.exists():
            partial = keep / f".{copy.name}.part"
            partial.write_bytes(data)
            partial.replace(copy)
        return copy

    def _drive_host(self, services: SessionServices, state: SessionState, folder: ProjectFolder) -> None:
        """Point a session's tools at the host folder it works in, as the benchmark harness points them
        at a task container: commands and file tools through the terminal daemon, relative paths under
        the folder (its worktree or its own directory), and no walls of this process, whose realpath
        checks would judge a host path by whatever the container has under the same name.

        It is the operator's own machine and nothing new fences it in; what is kept is the read-only
        promise, because a reviewer told it may not write would otherwise write through every tool.
        """
        metadata = state.session.metadata
        target = self.folder_workspace(state.session.id, metadata, state.project)
        services.exec_backend = HostExecBackend(lambda: self.host_bridge, str(target))
        services.workspace_dir = target
        # Its directory here, where a file fetched from the host to be sent or handed on is kept.
        services.extra["local_home"] = state.workspace
        services.walls = None
        services.writable = []
        services.log_root = None
        services.remote_readonly = folder.readonly or str(metadata.get("staff_isolation") or "") == "readonly" or self.lives_in_window(state)

    def walls_of(self, state: SessionState) -> Walls | None:
        """The walls of a loaded session, from its project, its folder and its own directory."""
        if state.project is None:
            return None
        metadata = state.session.metadata
        # A staff member is walled by its isolation: its own worktree (and what a commit there
        # writes) instead of the checkout it was made from, or nothing writable at all.
        isolation = str(metadata.get("staff_isolation") or "shared")
        if self.lives_in_window(state):
            # It reads its chat's files to tailor the prompt or answer the question and writes nothing: a
            # coach's saved prompt is written by the host on the chat's terms (``prompt_coach.save_prompt``),
            # not through these walls.
            isolation = "readonly"
        worktree = str(metadata.get("worktree") or "").strip()
        home = home_of(metadata)
        return walls_for(
            state.project,
            folder_id=str(metadata.get("folder_id") or "") or None,
            directory=str(metadata.get("directory") or "").strip() or None,
            local_env=self.projects.local_env,
            isolation=isolation,
            worktree=Path(worktree) if isolation == "worktree" else None,
            own_home=state.workspace if home else None,
        )

    async def open_writable(self, session_id: str, path: Path) -> None:
        """Let this session write to ``path`` under the sandbox from now on — a worktree it opened for its own changes."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        paths = [str(p) for p in (state.session.metadata.get("worktrees") or [])]
        if str(path) not in paths:
            paths.append(str(path))
            state.session.metadata["worktrees"] = paths
            state.metadata["worktrees"] = paths
            await self.sessions.update_metadata(session_id, state.session.metadata)
        if state.services is not None:
            for q in worktree_writable_paths(path):
                if q not in state.services.writable:
                    state.services.writable.append(q)

    # -- the steer queue, as the app sees it ----------------------------------------

    async def queued_input(self, session_id: str) -> list[dict[str, Any]]:
        """What this session has taken in and not yet given to the model, steers first, oldest first.

        The store is the record: a run reloads the queues from it before every model call and writes
        back what it did not place. Between those two moments a running engine holds the only copy,
        so a listing taken exactly then can name an item the model is already reading; the change
        event that follows the write corrects it within the round.

        Each item comes back as a card: enough text to recognise it, with ``truncated`` set when
        there is more. Three long pasted messages otherwise travel whole to every open stream on
        every later change of the queue.
        """
        return _steer_cards(_waiting(await self.live.load(session_id)))

    async def drop_queued_input(self, session_id: str, item_id: str) -> bool:
        """Take one waiting message back before the run reads it; ``False`` when it is already gone.

        The engine is asked first and the store second. A run between its reload and its next model
        call holds the queues in memory and would write that copy back over any store-only removal —
        emptying its list first is what actually stops the message, and the store write behind it
        stops a reload from bringing the item round again.
        """
        state = self._states.get(session_id)
        removed = False
        engine = state.engine if state is not None else None
        if engine is not None:
            for attribute in ("_steer_queue", "_follow_up_queue"):
                queue = list(getattr(engine, attribute, []) or [])
                kept = [item for item in queue if str(item.get("id") or "") != item_id]
                if len(kept) != len(queue):
                    setattr(engine, attribute, kept)
                    removed = True
        receipt = await self.live.receipt(session_id, item_id)
        if receipt is not None:
            withdrawn = await self.live.withdraw(session_id, item_id)
            if withdrawn is not None and withdrawn["status"] == "withdrawn":
                removed = True
        elif await self.live.remove(session_id, "steer", item_id) or await self.live.remove(session_id, "follow_up", item_id):
            removed = True
        if removed and state is not None:
            # A reload that was already waiting on the database when this ran will be handed the row
            # as it stood before the removal. Remembering the id here is what stops that answer from
            # putting the withdrawn message back into the engine's queue, where it is authoritative.
            state.queue_withdrawn.add(item_id)
        if removed:
            await self.steer_changed(session_id, reason="withdrawn")
        return removed

    async def steer_queued(self, session_id: str, item_id: str) -> bool:
        """Turn a waiting follow-up into a steer of the run under way; ``False`` when it cannot be.

        A message sent during a run waits for the turn to end; this is the operator deciding it should
        not wait. The engine's copy is taken out first, for the reason ``drop_queued_input`` gives: once
        it is out of the engine's list the core can no longer start placing it, so the store move that
        follows cannot race a placement into a message the model reads twice. An item the run was
        handed and no longer holds is being placed right now, and that answers ``False``.
        """
        state = self._states.get(session_id)
        if state is None or not state.running:
            # Nothing to steer: a follow-up left behind by a finished run opens the next one anyway.
            return False
        engine = state.engine
        taken: tuple[int, dict[str, Any]] | None = None
        if engine is not None:
            queue = list(getattr(engine, "_follow_up_queue", []) or [])
            index = next((i for i, item in enumerate(queue) if str(item.get("id") or "") == item_id), None)
            if index is not None:
                taken = (index, queue.pop(index))
                engine._follow_up_queue = queue  # type: ignore[attr-defined]
            elif item_id in state.follow_up_seen:
                return False
        if not await self.live.promote(session_id, item_id):
            if taken is not None and engine is not None:
                # The store says it is gone (withdrawn, or consumed from another path): the engine's
                # list goes back as it was, because the store is the record and the engine only a copy.
                queue = list(getattr(engine, "_follow_up_queue", []) or [])
                queue.insert(taken[0], taken[1])
                engine._follow_up_queue = queue  # type: ignore[attr-defined]
            return False
        state.follow_up_steered.add(item_id)
        await self.steer_changed(session_id, reason="steered")
        return True

    async def steer_changed(self, session_id: str, *, reason: str) -> None:
        """Tell the session's listeners that what waits for the model is not what they last drew."""
        state = self._states.get(session_id)
        waiting = _waiting(await self.live.load(session_id))
        await self._notify_sinks(
            session_id,
            HostEvent(
                type=HostEventType.STEER_CHANGED,
                run_id=(state.run_id if state is not None else "") or "",
                # ``count`` is the whole queue; ``queued`` is the first cards' worth of it.
                payload={"session_id": session_id, "reason": reason, "count": len(waiting), "queued": _steer_cards(waiting)},
            ),
        )

    async def setting_notice(
        self, session_id: str | None, *, kind: str, outcome: str, detail: str, setting: SettingRef, model: str = "",
    ) -> None:
        """Tell the operator that an auxiliary model was not set or refused, and where that is fixed.

        ``outcome`` is ``fallback`` (the session's model did the work: a warning) or ``failed`` (an
        error). The conversation hears it as a ``setting_notice`` session event, drawn inline; the
        notifications hear it through the ``setting_notice`` service hook and raise a toast with the
        button. Neither may fail the work it reports on, so both are best effort."""
        payload = {"kind": kind, "outcome": outcome, "detail": detail[:600], "model": model, "setting": setting.as_dict()}
        if session_id:
            state = self._states.get(session_id)
            try:
                await self._notify_sinks(session_id, HostEvent(HostEventType.SETTING_NOTICE, (state.run_id if state is not None else "") or "", {"session_id": session_id, **payload}))
            except Exception:  # noqa: BLE001
                logger.warning("could not tell session %s about its %s model", session_id, kind, exc_info=True)
        hook = self.service_hooks.get("setting_notice")
        if hook is not None:
            try:
                await hook(session_id, payload)
            except Exception:  # noqa: BLE001
                logger.warning("could not post the %s model notice", kind, exc_info=True)

    async def session_vision(self, session_id: str | None) -> tuple[tuple[Any, str, FileBlobStore, str] | None, str, SettingRef | None]:
        """The session's own model as a vision route, or ``None`` with why it cannot look and where to fix it.

        What the vision model falls back to: the session's first rung (the default model for a caller
        with no session) when it takes images. A model that does not is not asked to pretend. Taking
        images is a switch on the preset, not a fact about the model, so the refusal says the switch is
        off and names that preset's row; it used to say the model "does not take images" about Claude
        models that read pictures and were only saved with the switch off."""
        try:
            overrides = await self.live.load(session_id) if session_id else {}
            rungs, _ = self.resolve_model(overrides)
        except Exception as exc:  # noqa: BLE001 — no model at all is a reason, not a crash
            return None, f"the session has no model to look with ({exc})", None
        if not rungs:
            return None, "the session has no model to look with", None
        provider, model = rungs[0]
        accepts = getattr(provider, "accepts_images", None)
        if accepts is None or not accepts(model):
            preset_id = self._preset_of_rung(overrides, provider, model)
            if preset_id is None:
                return None, f"the session's model {model} is not marked as taking images", None
            return None, f"the session's model {model} is not marked as taking images (its Images switch in Settings → Models is off)", preset_images(preset_id)
        return (provider, model, self.blobs, TENANT), "", None

    def _preset_of_rung(self, overrides: dict[str, Any], provider: Any, model: str) -> str | None:
        """The preset a session's first rung came from: the chosen or default one when it is that
        model, otherwise any preset naming the model on that provider; ``None`` for a manual pair."""
        presets = self.config.presets
        chosen = overrides.get("preset") if overrides.get("preset") in presets else None
        if chosen is None and not (overrides.get("provider") and overrides.get("model_name")):
            chosen = self.config.model.preset
        if chosen in presets and presets[chosen].model == model:
            return chosen
        for pid, preset in presets.items():
            try:
                if preset.model == model and self.providers.get(preset.provider) is provider:
                    return pid
            except KeyError:
                continue
        return None

    # -- input --------------------------------------------------------------------

    async def resume_provider_hold(
        self, session_id: str, failed_run_id: str, provider_id: str, model: str,
        *, client_message_id: str,
    ) -> str:
        """Continue one approved provider hold; its source is checked under the input lock."""
        return await self.submit(
            session_id, PROVIDER_RESUME_NOTE, as_answer=False, origin="core",
            client_message_id=client_message_id,
            _provider_resume=(failed_run_id, provider_id, model),
        )

    async def submit(
        self,
        session_id: str,
        text: str,
        attachments: Sequence[Attachment] = (),
        *,
        steer: bool = False,
        follow_up: bool = False,
        expected_running: bool | None = None,
        as_answer: bool = True,
        origin: str = "operator",
        client_message_id: str | None = None,
        via: str = "app",
        operator_words: Sequence[str] | None = None,
        reply_to: dict[str, Any] | None = None,
        secrets: Sequence[str] = (),
        autopilot: dict[str, Any] | None = None,
        _provider_resume: tuple[str, str, str] | None = None,
    ) -> str:
        """Deliver input. Starts a run, or queues a follow-up when one is active.

        ``origin`` names who wrote the text (``operator``, or a system source such as
        ``reminder``); the transcript and the Mini App show it accordingly. ``via`` is the front it
        came through, which a free-text answer to a pending question reports as where it was answered.
        While a run is active the text is a steer unless ``follow_up`` asks for it to wait for the end
        of the turn (or the session's ``queue_mode`` says so); ``steer`` wins over both.
        ``operator_words`` are the operator's passages a runtime note relays verbatim (an answer
        among a wake-up's events): compaction quotes them, and nothing else of the note.
        ``secrets`` names operator's secrets attached to the message: each becomes a line saying what the
        model may use and how, and the message carries the names for the app to draw.
        ``autopilot`` marks a message autopilot sent (its kind and decision), which the app labels and
        the continuation count reads (:mod:`daedalus.extensions.autopilot`).
        """
        # Whatever reaches a session — the operator's words, a staff member's report relayed, a wake-up — goes
        # in with each value the operator handed over put back as its placeholder. A password pasted into a
        # message by habit is then still not in the transcript, the model's input or the provider's logs.
        text = self.redactor.conceal_named(text)
        words = operator_words_metadata(origin, text, operator_words)
        if reply_to:
            # Rides with the operator's words on every copy of the message, so the app draws what it answers.
            words = {**words, "daedalus.reply_to": dict(reply_to)}
        if autopilot:
            words = {**words, "daedalus.autopilot": dict(autopilot)}
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if secrets and (handed := self.secrets.named(secrets, session_id)):
            lines = "\n".join(operator_secrets.attachment_text(secret) for secret in handed)
            text = f"{text.rstrip()}\n\n{lines}" if text.strip() else lines
            words = {**words, "daedalus.secrets": [{"name": secret.name, "scope": secret.scope_kind} for secret in handed]}
        if origin == "operator":
            # Writing to a session is having read it: the unread mark of its last result goes.
            try:
                await self.clear_unread(session_id)
            except Exception:  # noqa: BLE001 — a mark that stays is a dot too many, not a lost message
                logger.warning("could not clear the unread mark of session %s", session_id, exc_info=True)
        async with state.submit_lock:
            if not self.config.has_model:
                # The one place every way in converges: chat, the API, a schedule, a subagent, the
                # companion. None of them can start a run without a model, and all of them
                # already turn a RuntimeError here into a message the operator reads.
                raise NoModelConfigured
            if self.shutting_down:
                raise RuntimeError("the bot is stopping; the run starts after the restart")
            if (self.settings.state_dir / "dependencies" / "maintenance").exists():
                raise RuntimeError("dependencies are being installed; wait for the application to restart")
            if self.recovering and not state.running:
                raise RuntimeError("the bot is starting up and first continues the runs it left behind; try again in a moment")
            if self.moving_folders and state.project is not None:
                # A run started under a move would write into a copy that is about to be thrown away,
                # or into a folder half-way to its new place.
                try:
                    moving = self.folder_of(state.session.id, state.metadata, state.project).id in self.moving_folders
                except RuntimeError:
                    moving = False
                if moving:
                    raise FolderMoving(f"the folder of {state.session.title} is being moved; the run can start once the move is over")
            if not state.workspace.is_dir():
                # One of ours is remade here: a folder under the managed tree is the installation's
                # to create, and a run refused because our own bookkeeping lost a directory is an
                # outage with nothing for the operator to do about it. A folder they pointed at is
                # theirs, so that one still refuses, with the path in the message.
                if home_of(state.metadata) or self.host_folder_of(state.session.id, state.metadata, state.project) is not None:
                    with suppress(OSError):
                        _ensure_inbox(state.workspace, None)
                elif state.project is not None:
                    folder = self.folder_of(state.session.id, state.metadata, state.project)
                    await self.projects.ensure_reachable(folder)
                    _ensure_inbox(state.workspace, folder)
            if not state.workspace.is_dir():
                name = state.project.name if state.project is not None else state.session.title
                raise WorkspaceUnreachable(f"the working directory for {name} ({state.workspace}) is not reachable; restore or mount it before starting a run")
            # A session in a folder its walls make read-only reads and runs there without writing, so
            # only a folder it may write has to be writable on disk; attachments, which land in the
            # folder's inbox, have nowhere to go in one it may not.
            writes = state.services is None or state.services.workspace_writable
            if writes and not os.access(state.workspace, os.W_OK):
                raise WorkspaceUnreachable(f"the working directory for {state.session.title} is not writable")
            if attachments and not writes:
                raise RuntimeError(f"{state.workspace} is read-only for {state.session.title}, so attachments have nowhere to go; send them in a session that works in a writable folder")
            attachment_receipts: list[dict[str, str]] = []
            for item in attachments:
                name = Path(item.name or item.path.name).name
                if client_message_id and not item.content_sha256:
                    item.content_sha256 = await asyncio.to_thread(_file_sha256, item.path)
                attachment_receipts.append(
                    {
                        "name": name,
                        "mime_type": item.mime_type,
                        "caption": item.caption or "",
                        "sha256": item.content_sha256 or "",
                    }
                )
            receipt_payload = {
                "text": text,
                "steer": steer,
                "follow_up": follow_up,
                "as_answer": as_answer,
                "origin": origin,
                "attachments": attachment_receipts,
            }
            if client_message_id:
                existing = await self.live.check_receipt(session_id, client_message_id, "input", receipt_payload)
                if existing is not None and existing["status"] != "accepted":
                    if _provider_resume is not None:
                        async with self.db.transaction() as conn:
                            pinned = await pinned_target_in(conn, session_id, str(existing["run_id"] or ""))
                        if (existing["status"] != "consumed" or pinned is None
                                or pinned["action_id"] != client_message_id.removeprefix("provider-resume:")
                                or (pinned["provider_id"], pinned["model"]) != _provider_resume[1:]):
                            raise RuntimeError("the provider continuation has no matching durable run pin")
                    return str(existing["run_id"] or state.run_id or "")
            if expected_running is not None and bool(state.running) != expected_running:
                raise RuntimeError("the run changed since the message was composed; review its destination and send again")
            if _provider_resume is not None:
                failed_run_id, provider_id, model = _provider_resume
                if (not client_message_id or text != PROVIDER_RESUME_NOTE or origin != "core"
                        or as_answer or steer or follow_up or attachments
                        or state.running or state.pending is not None
                        or state.run_id not in (None, failed_run_id)):
                    raise RuntimeError("the provider continuation no longer matches the failed idle session")
                async with self.db.transaction() as conn:
                    target = await resume_target_in(conn, session_id, failed_run_id, provider_id, model, client_message_id)
                if target["provider_source_digest"] != self.providers.get(provider_id).endpoint.source_digest():
                    raise RuntimeError("the held provider connection changed; review the new configuration")
            body, image_refs = await self._ingest_attachments(state, text, attachments)
            if state.pending is not None:
                if as_answer:
                    # Free-text reply to a pending question counts as a custom answer.
                    if client_message_id:
                        receipt, created = await self.live.accept(session_id, client_message_id, "input", receipt_payload)
                        if not created and receipt["status"] != "accepted":
                            return str(receipt["run_id"] or state.run_id or "")
                    run_id = await self.answer(session_id, [{"custom": body}], via=via)
                    if client_message_id:
                        await self.live.consume(session_id, client_message_id, run_id=run_id, step_id="answer", message_seq=None)
                    return run_id
                queued = {**new_queued_prompt("follow_up", body, attachments=_queued_attachments(attachments)).to_dict(), "origin": origin, "operator_words": None if operator_words is None else list(operator_words), "queued_at": datetime.now(UTC).isoformat(), **_queued_caption(text, attachments), **_queued_display(words)}
                if client_message_id:
                    queued["id"] = client_message_id
                    receipt, created = await self.live.accept(
                        session_id,
                        client_message_id,
                        "input",
                        receipt_payload,
                        queue_item=queued,
                        queue_kind="follow_up",
                    )
                    if not created:
                        return str(receipt["run_id"] or state.run_id or "")
                else:
                    await self.live.enqueue(session_id, "follow_up", queued)
                await self.sessions.append_transcript(
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": "follow_up", "daedalus.origin": origin, **words, "daedalus.queued": True, **({"daedalus.client_message_id": client_message_id} if client_message_id else {}), **_image_refs_metadata(image_refs)})]
                )
                await self.steer_changed(session_id, reason="queued")
                return state.run_id or ""
            provider_id: str | None = None
            cap_model: str | None = None
            if not state.running:
                if _provider_resume is not None:
                    provider_id, cap_model = _provider_resume[1], _provider_resume[2]
                    self.providers.get(provider_id)
                else:
                    try:
                        rungs, _ = self.resolve_model(await self.live.load(session_id))
                        provider_id = rungs[0][0].endpoint.id if rungs else None
                        cap_model = rungs[0][1] if rungs else None
                    except Exception:  # noqa: BLE001 — a model problem surfaces when the run starts, not here
                        provider_id = None
                exceeded = self.budget_exceeded()
                if exceeded and not self.provider_costs_nothing(provider_id, cap_model):
                    raise RuntimeError(f"daily budget exceeded ({exceeded}); runs resume tomorrow or after /budget reset")
                breach = await self.cap_breach(state, provider_id, cap_model)
                if breach is not None:
                    raise RuntimeError(breach[1])
            if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None:
                # The loop has settled and the task is only doing bookkeeping: let it finish and start a new turn.
                try:
                    await asyncio.shield(state.task)
                except Exception:  # noqa: BLE001
                    pass
            if state.running:
                # A message sent while the agent works is a steer: the core places it before the
                # next model call (after the current tool batch). follow_up would wait for the end.
                kind = "follow_up" if not steer and (follow_up or state.metadata.get("queue_mode") == "follow_up") else "steer"
                placed = body
                if origin == "operator":
                    for steer_hook in self.steer_hooks:
                        try:
                            placed = await steer_hook(session_id, placed)
                        except Exception:  # noqa: BLE001 — the message goes as it was written
                            logger.exception("steer hook failed for session %s", session_id)
                # The operator's words are the message as written, never the host's note placed before it:
                # compaction quotes them verbatim, and the note would have been quoted as the operator's.
                said = list(operator_words) if operator_words is not None else ([body] if placed != body else None)
                # The files travel in the item itself, as the core's own ``attachments`` field, so they
                # survive every move the queue makes: a follow-up turned into a steer, a round that hands
                # back what it did not place, the drain that opens the next run. Without them only the
                # words reached the model, and a pasted screenshot arrived as a path it never looked at.
                queued = {**new_queued_prompt(kind, placed, attachments=_queued_attachments(attachments)).to_dict(), "origin": origin, "operator_words": said, "queued_at": datetime.now(UTC).isoformat(), **_queued_caption(text, attachments), **_queued_display(words)}
                if client_message_id:
                    queued["id"] = client_message_id
                    receipt, created = await self.live.accept(
                        session_id,
                        client_message_id,
                        "input",
                        receipt_payload,
                        queue_item=queued,
                        queue_kind=kind,
                    )
                    if not created:
                        return str(receipt["run_id"] or state.run_id or "")
                else:
                    await self.live.enqueue(session_id, kind, queued)  # type: ignore[arg-type]
                # The core folds queued prompts into the model's history later (and compaction may
                # rewrite them); the transcript keeps the operator's words as sent.
                await self.sessions.append_transcript(
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": kind, "daedalus.origin": origin, **words, "daedalus.queued": True, **({"daedalus.client_message_id": client_message_id} if client_message_id else {}), **_image_refs_metadata(image_refs)})]
                )
                await self.steer_changed(session_id, reason="queued")
                return state.run_id or ""
            # A new run starts. The one before it may still be tidying up behind the answer, and most
            # of that is none of this run's business — but the files are: a revert of the turn that
            # just ended restores the snapshot taken after it, so that snapshot has to exist before
            # anything is allowed to change the workspace again.
            try:
                async with asyncio.timeout(self.config.ops.settle_wait_seconds):
                    await state.settled.wait()
            except TimeoutError:
                # Bounded because this is held under ``state.submit_lock``: a snapshot that never
                # returns would otherwise wedge every later message behind a session the app draws
                # as idle, with nothing anywhere saying why. The turn starts; the cost is that an
                # undo of the previous one may restore a tree a turn older, and the log says so.
                logger.warning("session %s: the previous turn was still being written down after %.0f s; starting the next run anyway", session_id, self.config.ops.settle_wait_seconds)
            recorded_message: Message | None = None
            if state.metadata.pop("autotitle", None):
                # The first message of a chat the operator started by typing, rather than by naming
                # it first. The clip is already the title; the model may replace it once it answers.
                # The session row and the live state each hold a copy, and a later write of either
                # one would bring the flag back and name the chat a second time.
                state.session.metadata.pop("autotitle", None)
                provisional = state.session.title
                await self.sessions.update_metadata(session_id, dict(state.metadata))
                source = text
                if attachments and text in {"File attached.", "Files attached."}:
                    # Those two sentences are what an upload says when the operator typed nothing.
                    # They are the same for every file, so a title taken from them would be too.
                    source = ", ".join(Path(item.name or item.path.name).name for item in attachments)
                self._schedule_entitle(session_id, source, provisional)
            if client_message_id:
                receipt, created = await self.live.accept(session_id, client_message_id, "input", receipt_payload)
                if not created and receipt["status"] != "accepted":
                    return str(receipt["run_id"] or state.run_id or "")
                if not created:
                    # The acknowledgement may have been lost after the transcript append but before
                    # the run acquired its id. Reuse that exact row: rerunning prompt hooks or adding
                    # another user row would turn one browser submission into two instructions.
                    recorded_message = next(
                        (
                            item
                            for item in reversed(await self.sessions.list_transcript(session_id, limit=10_000))
                            if item.metadata.get("daedalus.client_message_id") == client_message_id
                        ),
                        None,
                    )
            if recorded_message is None:
                # Hooks may decorate the message (a fired reminder rides along). They run only for
                # the first placement of a receipt, so a transport retry cannot repeat a side effect.
                for hook in self.prompt_hooks:
                    try:
                        body = await hook(session_id, body)
                    except Exception:  # noqa: BLE001
                        logger.exception("prompt hook failed")
                message = Message(
                    role=MessageRole.user,
                    content_blocks=[TextBlock(text=body)],
                    metadata={"daedalus.origin": origin, **words, **({"daedalus.client_message_id": client_message_id} if client_message_id else {}), **_image_refs_metadata(image_refs)},
                )
                if _provider_resume is not None:
                    message.metadata["daedalus.provider_resume"] = {
                        "failed_run_id": _provider_resume[0], "provider_id": _provider_resume[1],
                        "model": _provider_resume[2],
                    }
                # Only what cannot be sent at all is compacted here — a model whose window is smaller
                # than the history was built for. The ratio-based compaction happens between runs
                # (see _drive), because it is a summariser call of a minute or two and the operator's
                # message used to queue behind it.
                # A held-provider decision must not spend on the previous engine's fallback
                # while preparing its input. The pinned engine handles its own context bound.
                if _provider_resume is None:
                    await self._maybe_auto_compact(state, required_only=True)
                await self.sessions.append_transcript(session_id, [message])
            else:
                message = recorded_message
            seqs = await self.sessions.transcript_seqs(session_id, [self.sessions.transcript_key(message)])
            await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
            run_id = await self._start_run(state, message)
            if client_message_id:
                await self.live.consume(
                    session_id,
                    client_message_id,
                    run_id=run_id,
                    step_id="start",
                    message_seq=seqs[0] if seqs else None,
                )
            for started in self.run_started_hooks:
                try:
                    await started(session_id, run_id)
                except Exception:  # noqa: BLE001
                    logger.exception("run-started hook failed")
            return run_id

    def file_scope(self, state: SessionState) -> str | None:
        """Where an attachment of this session is kept by handle: the main orchestrator's ``main``, a
        project orchestrator's project; ``None`` for a session that works in its own folder."""
        if self.is_dispatcher(state):
            return MAIN_FILES
        if self.is_orchestrator(state):
            return str(state.metadata.get("orchestrator_of") or state.metadata.get("orchestrator_retired_of") or "") or None
        return None

    async def _keep_attachments(self, state: SessionState, text: str, attachments: Sequence[Attachment], scope: str) -> tuple[str, list[tuple[str, str]]]:
        """An orchestrator's attachments go to the store, not its inbox, and it is told their handles.

        An orchestrator never opens a file where it lies: it reads it through Peek or Files and hands
        it to staff, who work in another folder or on another machine. A path in its inbox named
        nothing they could open — a host project's orchestrator once passed its container inbox path to
        a member on the host — so the message names the handle, which means the same file everywhere.
        """
        lines: list[str] = []
        image_refs: list[tuple[str, str]] = []
        actor = "operator"
        for attachment in attachments:
            name = Path(attachment.name or attachment.path.name).name
            try:
                stored = await self.files.add_path(
                    attachment.path, name=name, mime=attachment.mime_type, origin="operator", origin_ref=state.session.id, scope=scope, actor=actor
                )
            except FileRefused as exc:
                lines.append(f"- {name}: not kept — {exc}")
                continue
            attachment.stored_name = stored.name
            attachment.content_sha256 = stored.sha256
            lines.append(f"- {stored.line()}")
            if stored.mime.startswith("image/"):
                meta = await self.blobs.put(TENANT, await self.files.read(stored), content_type=stored.mime)
                attachment.image_ref = meta.ref
                image_refs.append((meta.ref, stored.mime))
        how = (
            "Read one with Files(op='read', file=…); pass them to a project with Delegate(files=[…])."
            if scope == MAIN_FILES
            else "Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])."
        )
        body = (text.strip() + "\n\n" if text.strip() else "") + "Attached files (kept by handle; " + how + "):\n" + "\n".join(lines)
        return body, image_refs

    async def _cookie_hint(self, path: Path, mime: str, size: int) -> str:
        """What the attachment line adds for a cookie file: the sites, and the tool that loads them.

        Said, never done: the agent decides whether the task wants the cookies in its browser or the
        file passed to curl. Only where there is a browser to load them into, and only for a file that
        is plainly a cookie export (``cookies.sniff``), so an ordinary text file is never called one.
        """
        if self.service_hooks.get("browser") is None or size > cookie_files.MAX_BYTES:
            return ""
        if not (mime.startswith("text/") or "json" in mime or path.suffix.lower() in (".txt", ".json")):
            return ""
        try:
            sites = await asyncio.to_thread(lambda: cookie_files.sniff(path.read_bytes()))
        except OSError:
            return ""
        return f" — cookies for {', '.join(sites)}; BrowserCookies can load them into the browser" if sites else ""

    async def _ingest_attachments(
        self, state: SessionState, text: str, attachments: Sequence[Attachment]
    ) -> tuple[str, list[tuple[str, str]]]:
        if not attachments:
            return text, []
        scope = self.file_scope(state)
        if scope:
            return await self._keep_attachments(state, text, attachments, scope)
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        image_refs: list[tuple[str, str]] = []
        for attachment in attachments:
            name = Path(attachment.name or attachment.path.name).name
            digest = attachment.content_sha256 or await asyncio.to_thread(_file_sha256, attachment.path)
            attachment.content_sha256 = digest
            target = inbox / name
            if attachment.path.resolve() != target.resolve():
                if target.exists() and await asyncio.to_thread(_file_sha256, target) != digest:
                    source_name = Path(name)
                    target = inbox / f"{source_name.stem}-{digest[:8]}{source_name.suffix}"
                    counter = 1
                    while target.exists() and await asyncio.to_thread(_file_sha256, target) != digest:
                        target = inbox / f"{source_name.stem}-{digest[:8]}-{counter}{source_name.suffix}"
                        counter += 1
                if not target.exists():
                    # A crash during a copy must not leave a plausible-looking partial attachment.
                    temporary = inbox / f".{target.name}.{uuid.uuid4().hex}.part"
                    try:
                        await asyncio.to_thread(shutil.copy2, attachment.path, temporary)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            attachment.stored_name = target.name
            shown = target
            backend = state.services.exec_backend if state.services is not None else None
            if isinstance(backend, HostExecBackend) and state.services is not None:
                # The session's tools work on the host, where the copy here cannot be opened; the
                # file is passed on into the folder's inbox there and named by that path.
                shown = state.services.workspace_dir / "inbox" / target.name
                await backend.put_bytes(str(shown), await asyncio.to_thread(target.read_bytes))
            size = target.stat().st_size
            lines.append(f"- {shown} ({attachment.mime_type}, {size} bytes){await self._cookie_hint(target, attachment.mime_type, size)}")
            if attachment.mime_type.startswith("image/"):
                meta = await self.blobs.put(TENANT, target.read_bytes(), content_type=attachment.mime_type)
                attachment.image_ref = meta.ref
                image_refs.append((meta.ref, attachment.mime_type))
        body = (text.strip() + "\n\n" if text.strip() else "") + "Attached files:\n" + "\n".join(lines)
        return body, image_refs

    async def answer(self, session_id: str, answers: list[dict[str, Any]], *, via: str, source: str = "user", claimed: bool = False) -> str:
        """Resume a run paused on AskUser with the operator's answers.

        ``via`` says where it was answered (app, telegram, timeout, cli, …): the other fronts retire
        their copy of the question when they hear it was answered somewhere else. ``source`` is who
        answered, as the model reads it in the tool result (``user``, or ``orchestrator`` for a staff
        member's question). Before anything changes, every answer claim is asked whether this answer
        is the first (``claimed`` says the caller already won that race itself); a question that has a
        request row elsewhere is answered once, by whoever updates that row first.
        """
        async with self.db.transaction() as conn:
            await assert_admission_open_in(conn)
        state = await self.get_state(session_id)
        if state is None or state.pending is None or state.engine is None:
            raise RuntimeError("no pending question for this session")
        if state.running:
            raise RuntimeError("the run is still finishing; try again in a moment")
        pending = state.pending
        if not claimed:
            for claim in self.answer_claims:
                refusal = await claim(session_id, pending.tool_call_id, via)
                if refusal:
                    raise RuntimeError(refusal)
            if state.pending is not pending:
                raise RuntimeError("the question was answered meanwhile")
        questions = [q.get("question", "") for q in pending.payload.get("questions", [])]
        shaped = []
        for index, answer in enumerate(answers):
            shaped.append(
                {
                    "question": answer.get("question") or (questions[index] if index < len(questions) else ""),
                    "selected": list(answer.get("selected") or []),
                    "custom": answer.get("custom"),
                }
            )
        result = json.dumps({"answers": shaped, "source": source}, ensure_ascii=False)
        state.engine.history.append(
            Message(
                role=MessageRole.tool,
                # The operator's reply to the model's question: the core's ledger quotes it whole.
                content_blocks=[ToolResultBlock(tool_call_id=pending.tool_call_id, content=result, metadata={OPERATOR_WORDS_METADATA_KEY: source in ("user", "operator")})],
            )
        )
        state.engine.clear_pending_approval(pending.tool_call_id)
        state.engine.transition_to(LoopState.RUNNING)
        state.pending = None
        await self.db.execute("DELETE FROM pending_questions WHERE session_id = ?", (session_id,))
        await self._publish(state, "ask.answered", {"request_id": pending.tool_call_id, "request_ref": f"ask:{session_id}:{pending.tool_call_id}", "via": via})
        return await self._start_run(state, None, continue_turn=True)

    async def small_model(self, prompt: str, preset: str, *, purpose: str, session_id: str | None = None, max_tokens: int = 200, temperature: float = 0.0, instructions: str = "") -> str:
        """One short request to a preset for a helper around the operator's messages.

        ``purpose`` is the label the call's cost is booked under in Usage (``call_category="assist"``
        is what tells the usage record to keep it), so what the helpers spend is visible apart from
        the runs. Refused when the day's budget is spent and the model is a paid one.

        ``instructions``, when given, go first as a system message of their own and ``prompt`` follows
        as the user's: the helper's fixed wording then stays one cacheable prefix, and whatever moves
        (the conversation's tail, the operator's style profile) rides in the message after it.
        """
        provider, model = self.providers.rungs_for(self.config, preset or None)[0]
        if self.budget_exceeded() and not self.provider_costs_nothing(provider.endpoint.id, model):
            raise RuntimeError("daily inference budget exceeded")
        lead = [Message(role=MessageRole.system, content_blocks=[TextBlock(text=instructions)])] if instructions.strip() else []
        request = LLMRequest(
            model=model,
            messages=[*lead, Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt)])],
            max_tokens=max_tokens,
            temperature=temperature,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, call_purpose=purpose, call_category="assist"),
        )
        response = await provider.complete_text(request)
        return "".join(block.text for block in response.message.content_blocks if isinstance(block, TextBlock))

    async def set_yagni(self, session_id: str, on: bool) -> bool:
        """Switch YAGNI mode for a session; the next run's opening turn tells the model.

        Only the flag is stored. What the model was last told is read off its working history when the
        next turn starts (see ``_yagni_note``), so a compaction, a clear or a revert that took the
        note away is noticed without each of them having to remember to say so.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        for meta in (state.metadata, state.session.metadata):
            if on:
                meta["yagni"] = True
            else:
                meta.pop("yagni", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return on

    async def _yagni_note(self, state: SessionState, history: Sequence[Message] | None = None) -> str:
        """The YAGNI note this turn has to carry, or ``""`` when the model already knows.

        The last note in the working history is what the model was told; none counts as off. A host
        compaction's summary leaves the turn context out, so after one the rules are restated while the
        mode is on, and a switch on and back off before any turn says nothing at all.
        """
        wanted = bool(state.metadata.get("yagni"))
        if history is None:
            history = await self._working_history(state)
        told = False
        for message in reversed(history):
            if message.role is not MessageRole.user:
                continue
            text = "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))
            context = text[text.rfind(prompts.TURN_CONTEXT_OPEN):] if prompts.TURN_CONTEXT_OPEN in text else ""
            if prompts.YAGNI_ON in context or prompts.YAGNI_OFF in context:
                told = prompts.YAGNI_ON in context
                break
        return "" if told == wanted else prompts.yagni_note(wanted)

    def run_bounds(self, state: SessionState) -> RunBounds | None:
        """An orchestration role's own bounds on one turn, or ``None`` for every other session.

        An orchestrator's turn hands work over and ends, and it is woken by events with nobody
        watching; its bounds stop one that does not end from spending unattended. They are not a
        mode: no operator's chat has any, and nothing here tells the agent how far to go.
        """
        if self.is_orchestrator(state):
            limits = self.config.orchestrator
            return RunBounds(limits.max_iterations, limits.usd_per_run, "orchestrator.usd_per_run")
        if self.is_dispatcher(state):
            bounds = self.config.dispatcher
            return RunBounds(bounds.max_iterations, bounds.usd_per_run, "dispatcher.usd_per_run")
        return None

    async def stop(self, session_id: str) -> bool:
        state = self._states.get(session_id)
        if state is None or not state.running or state.engine is None:
            return False
        state.engine.stop()
        return True

    async def stop_run(self, session_id: str, run_id: str) -> bool:
        """Stop only the observed run; checking before an awaited authorization leaves a race."""
        state = self._states.get(session_id)
        if state is None or state.run_id != run_id or not state.running or state.engine is None:
            return False
        state.engine.stop()
        return True

    async def set_model(
        self,
        session_id: str,
        *,
        model_name: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        clear: bool = False,
    ) -> None:
        state = await self.get_state(session_id)
        arguments = dict(model_name=model_name, provider=provider, preset=preset, thinking=thinking,
                         reasoning_effort=reasoning_effort, clear=clear)
        if state is None:
            await self._set_model_locked(session_id, **arguments)
            return
        was = await self.session_window(state)
        # A continuation pins its model while holding this lock. Checking before acquiring it
        # let a concurrent override change the engine after the durable pin was written.
        async with state.submit_lock:
            await self._set_model_locked(session_id, **arguments)
        now = await self.session_window(state)
        if 0 < now < was and not state.running and state.pending is None:
            # A history built for a larger window is compacted as soon as the operator moves it onto a
            # smaller one, with the summary it deserves, rather than at the next message, or by the
            # core masking half of it in the middle of a run. A hold left by an earlier compaction
            # measured the old window and does not apply to this one.
            state.compaction_hold = None
            state.auto_compaction = self._spawn_background(self._maybe_auto_compact(state), f"auto-compact:{session_id}")

    async def _set_model_locked(
        self,
        session_id: str,
        *,
        model_name: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        clear: bool = False,
    ) -> None:
        if clear:
            await self.live.clear_overrides(session_id)
            return
        if preset is not None and preset not in self.config.presets:
            raise ValueError(f"no such model preset {preset!r}")
        if provider is not None and provider not in self.providers.available():
            raise ValueError(f"unknown or unusable provider {provider!r}")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}")
        state = self._states.get(session_id)
        if state is not None and state.running and state.run_id and (model_name or provider or preset):
            async with self.db.transaction() as conn:
                if await pinned_target_in(conn, session_id, state.run_id) is not None:
                    raise RuntimeError("this provider continuation is pinned; stop it before changing its model")
        await self.live.set_model(
            session_id, model_name=model_name, provider=provider, preset=preset, thinking_enabled=thinking, reasoning_effort=reasoning_effort
        )
        state = self._states.get(session_id)
        if state is not None and state.engine is not None and state.running:
            state.engine.apply_live_controls(
                model_name=model_name, thinking_enabled=thinking, reasoning_effort=reasoning_effort
            )
            if model_name:
                # The operator moved the running session onto another model. That is a change the
                # fronts should say out loud, and it is not a fallback: what the session is set to
                # moves with it, so the header notes the switch once and then reads as normal again.
                state.configured_model = self._rung_label(getattr(state.engine, "llm", None), model_name)
                state.model_change_reason = "live_override"

    # -- runs -----------------------------------------------------------------------

    async def _build_engine(
        self, state: SessionState, run_id: str, *, message: Message | None = None,
    ) -> QueryEngine:
        overrides = await self.live.load(state.session.id)
        for hook in self.preset_hooks:
            chosen = hook(state)
            if chosen:
                overrides = {**overrides, "preset": chosen}
                break
        target = None
        async with self.db.transaction() as conn:
            if message is not None and message.metadata.get("daedalus.provider_resume"):
                requested = message.metadata["daedalus.provider_resume"]
                target = await resume_target_in(
                    conn, state.session.id, requested["failed_run_id"], requested["provider_id"],
                    requested["model"], str(message.metadata.get("daedalus.client_message_id") or ""),
                )
            else:
                target = await pinned_target_in(conn, state.session.id, run_id)
        if target is not None:
            provider_id, model = str(target["provider_id"]), str(target["model"])
            rungs = [(self.providers.get(provider_id), model)]
            if target["provider_source_digest"] != rungs[0][0].endpoint.source_digest():
                raise RuntimeError("the held provider connection changed; review the new configuration")
            _, preset = self.config.preset()
            preset = next((item for item in self.config.presets.values()
                           if item.provider == provider_id and item.model == model), preset)
        else:
            rungs, preset = self.resolve_model(overrides)
        if preset.free_only:
            if len(rungs) != 1 or rungs[0][0].endpoint.id != preset.provider or rungs[0][1] != preset.model:
                raise RuntimeError("free preset has a different model route")
            snapshot = await free_catalog.get()
            source = next((row for row in snapshot["providers"] if row["id"] == preset.provider), None)
            listed = next((row for row in source["models"] if row["id"] == preset.model), None) if source else None
            # The last listing the catalog read stands while a refresh fails: refusing every run of a
            # free preset whenever the public model list was briefly unreachable stopped free work
            # for no charge it could prevent. A model the provider no longer lists as free still stops.
            if source is None or listed is None or listed["mechanism"] != "zero_price":
                raise RuntimeError("the free model is no longer listed as free by its provider; choose another model before running")
            configured = self.config.providers.get(preset.provider)
            if configured is None or not approved_endpoint(preset.provider, configured.base_url, source["base_url"]):
                raise RuntimeError("free model provider no longer points to its verified endpoint")
        hold = ExitStack()
        hold.enter_context(self.providers.hold([provider for provider, _ in rungs]))
        try:
            engine = await self._assemble_engine(state, run_id, overrides, rungs, preset)
        except BaseException:
            hold.close()
            await self.providers.close_retired()
            raise
        if state.provider_hold is not None:
            state.provider_hold.close()
        state.provider_hold = hold
        return engine

    async def _assemble_engine(self, state: SessionState, run_id: str, overrides: dict[str, Any], rungs: list[Any], preset: Any) -> QueryEngine:
        await self._load_capability_ancestors(state)
        scope = await self._environment_scope(state)
        if scope != state.environment_scope:
            state.environment_scope = scope
            self._apply_tool_visibility_all()
        await standing.load(self, state)
        if rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id, rungs[0][1]):
            # A local, free or subscription primary remains usable after a priced budget is
            # exhausted. Paid fallbacks do not inherit that exemption: when any applicable dollar
            # guard is already closed, keep only uncounted rungs so a failed server cannot turn a
            # free run into a charge.
            paid_blocked = self.budget_exceeded() is not None
            if not paid_blocked:
                for provider, fallback in rungs[1:]:
                    if (not self.provider_costs_nothing(provider.endpoint.id, fallback)
                            and await self.cap_breach(state, provider.endpoint.id, fallback) is not None):
                        paid_blocked = True
                        break
            if paid_blocked:
                rungs = [(provider, model) for provider, model in rungs if self.provider_costs_nothing(provider.endpoint.id, model)]
        deps = EngineDeps(
            tool_registry=self.registry_for(state),
            event_stream=self.events,
            blob_store=self.blobs,
            skill_store=self.skills_for(state),
            hook_manager=self.hooks,
            bot_repo=self.settings.bot_repo_dir,
            core_repo=self.settings.core_repo_dir,
            governance_path=self.governance_path,
            github_org=self.settings.daedalus_github_org,
            ssh_config=Path.home() / ".ssh" / "config",
            policy_gate=self.policy_gate,
            selfdev_mode=self.capabilities.selfdev.mode,
            request_manifest_sink=self.request_manifests,
            tool_rules_key=self.tool_rules_key(),
        )
        bounds = self.run_bounds(state)
        enabled = self.mcp_enabled(state)
        if enabled:
            # Re-establish connections for servers this session left enabled (e.g. after a
            # host restart or a dropped transport) so their tools are visible again instead
            # of silently missing until the agent re-runs McpEnable. Bounded as a whole: a dead
            # or slow server must never block the run, and one still starting is left to finish.
            await self.mcp.warm(enabled)
        async with self.db.transaction() as conn:
            resting = await resting_providers_in(conn, [provider.endpoint.id for provider, _ in rungs[1:]])
        chain = build_chain(rungs, room=self.providers.room_for(self.config), resting=resting)
        # What this run was *asked* for. Everything the fallback notice says is measured against it,
        # so it is read once here, from the same rungs the chain is built on, and not re-derived later
        # from a configuration the operator may have changed while the run was going.
        state.configured_model = self._rung_label(rungs[0][0], rungs[0][1])
        # Read before the tool policy below, which offers the computer tools only to a model that sees.
        accepts = getattr(rungs[0][0], "accepts_images", None) if rungs else None
        state.computer_sight = (rungs[0][1], bool(accepts(rungs[0][1]))) if rungs and accepts is not None else None
        # The primary's switch: a fallback runs on the surface the primary was given, as the tool limit
        # is not, because a surface too big is refused and one decided for another model is only worse.
        state.on_demand_groups = on_demand_tool_groups_for(preset, rungs[0][1]) if rungs else True
        state.effective_model = ""
        state.model_change_reason = ""
        state.model_stamps.clear()
        pinned = fork_identity(state.metadata)
        engine = build_engine(
            deps=deps,
            config=self.config,
            max_iterations=bounds.max_iterations if bounds is not None else 0,
            run_id=run_id,
            session_id=state.session.id,
            session_title=pinned["title"] if pinned else state.session.title,
            workspace=Path(pinned["workspace"]) if pinned else self.work_dir(state),
            project=str(pinned.get("project") or "") if pinned else state.project.name if state.project is not None else "",
            rungs=rungs,
            provider_chain=chain,
            model_name=rungs[0][1],
            thinking=preset.thinking if overrides.get("thinking_enabled") is None else bool(overrides["thinking_enabled"]),
            reasoning_effort=overrides.get("reasoning_effort") or preset.reasoning_effort,
            context_window=state.context_window or preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            extra_notes=pinned["notes"] if pinned else self.notes_for(state, window=int(state.context_window or preset.context_window or 0)),
            tool_visibility_policy=self.tool_policy_for(state),
            role="companion" if self.is_companion(state) else "coach" if self.is_prompt_coach(state) else "btw" if self.is_btw(state) else "orchestrator" if self.is_orchestrator(state) else "dispatcher" if self.is_dispatcher(state) else "agent",
            max_advertised_tools=self.advertised_tools_limit(rungs),
            discovered_tools=self.discovered_tools_for(state),
            loaded_tool_groups=self.loaded_groups_for(state),
            # The previous run's engine is still the state's here: what it cut stays cut, or the new run
            # decided again from nothing and cut, at its first request, what the last one left whole.
            trimmed_tool_result_ids=(state.engine.trimmed_tool_result_ids if state.engine is not None
                                     else tuple(pinned["trimmed"]) if pinned else ()),
            tool_group_loads=self.tool_group_loads_for(state) if not self.is_dispatcher(state) else None,
            tool_rules_session=(str(pinned.get("cache_key") or "") or None) if pinned else None,
        )
        self._attach_hooks(engine, state)
        self._stamp_memory(state, engine)
        if chain is not None:
            # The fit check reads the prompt size the engine last saw; before the first call it is zero and every rung fits.
            chain.bind_prompt_size(lambda: int(getattr(engine, "last_observed_prompt_tokens", 0) or 0))
        return engine

    def _attach_hooks(self, engine: QueryEngine, state: SessionState) -> None:
        session_id = state.session.id

        async def reload_live_control(eng: QueryEngine) -> None:
            data = await self.live.load(session_id)
            steer = [item for item in data["steer"] if str(item.get("id") or "") not in state.queue_withdrawn]
            follow_up = [item for item in data["follow_up"] if str(item.get("id") or "") not in state.queue_withdrawn | state.follow_up_steered]
            state.steer_seen = [str(item.get("id") or "") for item in steer]
            state.follow_up_seen = [str(item.get("id") or "") for item in follow_up]
            eng._steer_queue = steer  # type: ignore[attr-defined]
            eng._follow_up_queue = follow_up  # type: ignore[attr-defined]

        async def persist_live_control(eng: QueryEngine) -> None:
            steer = list(getattr(eng, "_steer_queue", []) or [])
            follow_up = list(getattr(eng, "_follow_up_queue", []) or [])
            # A merge, not a write: a steer that arrived after this round's reload is in the store
            # and not in the engine's list, and writing that list over the column would destroy it
            # while the app was being told the model had read it.
            gone = await self.live.replace_seen(
                session_id, steer, follow_up, seen_steer=state.steer_seen, seen_follow_up=state.follow_up_seen,
            )
            # A follow-up turned into a steer leaves the follow-up queue without being read, so it
            # is not consumed, unless this round was handed it as a steer and placed it. Excluding
            # every turned item once left the card of a delivered steer standing above the composer.
            handed_steers = set(state.steer_seen)
            consumed = [
                item_id for item_id in gone
                if item_id not in state.queue_withdrawn
                and (item_id not in state.follow_up_steered or item_id in handed_steers)
            ]
            state.steer_seen = [str(item.get("id") or "") for item in steer]
            state.follow_up_seen = [str(item.get("id") or "") for item in follow_up]
            state.queue_withdrawn.clear()
            state.follow_up_steered.clear()
            if consumed:
                # The round placed queued text into the history, so the cards the app draws above its
                # composer are stale. Only what this round was handed and did not hand back counts,
                # and a withdrawal announced itself when it happened and is not this.
                await self.steer_changed(session_id, reason="consumed")

        def start_persist(history: list[Message], fresh: list[Message] | None) -> None:
            """Queue one round's write behind the writes of the rounds before it.

            The hook returns as soon as the write is a task, so the rounds of a session are only in
            order if the tasks are made to be: each waits for the one queued before it. Without that,
            a write that fails could have its repair overtaken by a hand-over queued behind it, and
            the round's messages were simply never written.
            """
            previous = state.history_keys
            known = set(previous)
            for message in fresh if fresh is not None else history:
                if self.sessions.transcript_key(message) not in known:
                    message.metadata.setdefault("daedalus.run_id", engine.config.run_id)
            state.history_keys = [self.sessions.transcript_key(m) for m in history]
            prior, gen, epoch = state.persist_chain, state.persist_gen, state.persist_epoch

            async def ordered() -> None:
                if prior is not None and not prior.done():
                    await asyncio.wait([prior])  # its outcome is its own; this waits only for its turn
                await self._persist_history(state, history, previous, gen, fresh=fresh, epoch=epoch)

            task = asyncio.get_running_loop().create_task(ordered(), name=f"persist:{session_id}")
            state.persist_chain = task
            state.persist_tasks.add(task)
            task.add_done_callback(_log_task_failure)
            task.add_done_callback(_forget_if_dropped(state))
            task.add_done_callback(state.persist_tasks.discard)

        def persist_session_history(eng: QueryEngine) -> None:
            """The whole working history, for a core that cannot say what changed."""
            start_persist(list(eng.history), None)

        def persist_history_delta(eng: QueryEngine, delta: Any) -> None:
            """What the round added — or the whole history, when the sequence itself changed.

            The core remembers what it last handed over, so the store is told "these two are
            new" instead of being handed eight hundred messages to write again. A rewrite (a
            compaction, a revert, rows the host dropped) says so, and then the history becomes
            a new generation exactly as it did before.
            """
            start_persist(list(delta.history), None if delta.rewritten else list(delta.appended))

        engine.reload_live_control = reload_live_control  # type: ignore[attr-defined]
        engine.persist_live_control = persist_live_control  # type: ignore[attr-defined]
        # Both are attached: a core that knows about the delta calls the second and never the
        # first, and one that does not calls the first and never looks for the second.
        engine.persist_session_history = persist_session_history  # type: ignore[attr-defined]
        engine.persist_history_delta = persist_history_delta  # type: ignore[attr-defined]

    async def _persist_history(
        self,
        state: SessionState,
        history: list[Message],
        previous_keys: list[str],
        gen: int | None = None,
        *,
        fresh: list[Message] | None = None,
        epoch: int | None = None,
    ) -> None:
        """Persist the working history and the transcript, then label fresh summaries with what they replaced.

        A compaction summary the core just produced is tagged with the transcript seqs of the
        turns it stands for, in its metadata (for the Mini App) and in its text (so the model
        knows what HistoryExpand would return). Persists are serialised per session and a
        snapshot taken before a history rewrite is dropped, so an older picture never lands last.

        A write that did not land leaves the stored history short of a round, and the rounds after
        it would append onto messages that are not there — the working history the next load reads
        comes from ``session_messages``, so the hole is what the session would resume from, with a
        tool result missing under a tool call that is not. So a failure is recorded on the session
        before this call returns, and the next persist of that session writes the history whole
        rather than adding to the end of it, whichever hand-over gets there first. A hand-over made
        before that failure and still queued behind it is written whole too: what it holds is what
        the round added, and the rows it would be added to are the ones that went missing.
        """
        async with state.persist_lock:
            if gen is not None and gen != state.persist_gen:
                return
            if state.persist_rewrite or (epoch is not None and epoch != state.persist_epoch):
                # The stored history is missing a round. Appending is not an option, and the
                # snapshot this call was given may be older than what the session holds now — a
                # rewrite from that would drop the rounds taken since — so the engine's own history
                # is written where there is one.
                fresh = None
                live = state.engine.history if state.engine is not None else None
                if live is not None and len(live) >= len(history):
                    history = list(live)
            try:
                await self._write_history(state, history, previous_keys, fresh=fresh)
            except (Exception, asyncio.CancelledError):
                self._persist_failed(state, history)
                raise
            if fresh is None:
                state.persist_rewrite = False  # the history was written whole; there is no hole left

    def _persist_failed(self, state: SessionState, history: list[Message]) -> None:
        """A write did not land: take the promise back, and make sure somebody rewrites the history.

        The core is told at once — not from a done-callback, which fires a loop iteration later,
        by which time the hand-overs made in between have already been queued as appends. The
        rewrite is scheduled as well as marked, because the failing round may have been the last of
        the run: with no traffic after it, a mark nobody reads repairs nothing.
        """
        state.persist_rewrite = True
        state.persist_epoch += 1
        _forget_persisted(state)
        if state.persist_repair is not None and not state.persist_repair.done():
            # The failure happened inside the repair itself, or one is already on its way: a store
            # that is refusing every write is retried by the next round, not by this one forever.
            return
        task = asyncio.get_running_loop().create_task(self._repair_history(state, history), name=f"persist-repair:{state.session.id}")
        state.persist_repair = task
        state.persist_tasks.add(task)
        task.add_done_callback(_log_task_failure)
        task.add_done_callback(state.persist_tasks.discard)

    async def _repair_history(self, state: SessionState, history: list[Message]) -> None:
        """Write the history whole after a failed write, unless a hand-over has already done it."""
        await asyncio.sleep(0)  # let a hand-over queued behind the failure take it first
        if not state.persist_rewrite:
            return
        await self._persist_history(state, history, state.history_keys)

    def _stamp_model(self, state: SessionState, messages: Sequence[Message], previous_keys: Sequence[str]) -> None:
        """Name the model on every assistant turn this round added, before its row is written.

        The transcript row is written once and ignored ever after, so the stamp has to be on the
        message the first time it is offered — a turn stamped later is a turn the app never sees
        stamped. Only turns this round added are touched: a rewrite hands over the whole history,
        and stamping that with the model answering now would put today's fallback on every answer
        the session ever gave.

        The stamp itself is not read here: it was taken at ``message_stop``, while the model that
        wrote the turn was still the one answering. A persist is a fire-and-forget task, so round *n*
        is very often written after round *n+1* has already stepped the chain down — reading
        ``effective_model`` at write time is what made a turn the configured model produced claim the
        fallback wrote it, permanently, since transcript rows are written once.

        The metadata dict is annotated in place. It is the same dict the core's own history holds,
        and that is the point: the working copy and the transcript copy say the same thing, and
        neither is a message the model is ever shown.
        """
        if not state.model_stamps:
            return
        known = set(previous_keys)
        for message in messages:
            if message.role is not MessageRole.assistant:
                continue
            key = self.sessions.transcript_key(message)
            if MODEL_METADATA_KEY in message.metadata or key in known:
                continue
            record = state.model_stamps.pop(key, None)
            if record is None:
                # Nobody watched this turn end — a history the host did not stream, a turn the core
                # wrote itself. An unstamped turn says nothing; a guessed one says something false.
                continue
            message.metadata[MODEL_METADATA_KEY] = record

    async def _write_history(
        self,
        state: SessionState,
        history: list[Message],
        previous_keys: list[str],
        *,
        fresh: list[Message] | None,
    ) -> None:
        """The writes themselves, under the caller's lock: the transcript, then the working history."""
        session_id = state.session.id
        known = set(previous_keys)
        for message in fresh if fresh is not None else history:
            if self.sessions.transcript_key(message) not in known:
                message.metadata.setdefault("daedalus.run_id", state.run_id)
        self._stamp_model(state, fresh if fresh is not None else history, previous_keys)
        await self._label_summaries(state, history, previous_keys)
        await self.sessions.append_transcript(session_id, fresh if fresh is not None else history, from_history=True)
        # A round adds to the end of the history it was given; only a rewrite of the
        # sequence starts a generation, and that is not what a round does. ``fresh`` is
        # what the round added, when the caller knows; ``None`` means it does not, and
        # the stored history is brought up to this one the long way.
        if fresh is None:
            await self.sessions.sync_messages(session_id, TENANT, history)
        elif fresh:
            await self.sessions.append_messages(session_id, TENANT, fresh)

    async def _label_summaries(self, state: SessionState, history: list[Message], previous_keys: Sequence[str]) -> None:
        """Tag each compaction summary not yet labelled with the transcript seqs of the turns it replaced.

        The seqs go into its metadata (for the Mini App) and its text (so the model knows what
        HistoryExpand would return). ``previous_keys`` is the history as the store last had it: the
        turns missing from ``history`` now are the ones the summary stands for. The live engine's
        copy is labelled too, found by identity and never by position, since the core may have
        reshaped its history since ``history`` was taken.
        """
        unlabelled = [i for i, m in enumerate(history) if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in m.metadata]
        if not unlabelled:
            return
        session_id = state.session.id
        current = {self.sessions.transcript_key(m) for m in history}
        removed = [k for k in previous_keys if k not in current]
        seqs = await self.sessions.transcript_seqs(session_id, removed) if removed else []
        if removed and len(seqs) < len(removed):
            logger.warning("session %s: %d of %d archived turns were never in the transcript", session_id, len(removed) - len(seqs), len(removed))
        for index in unlabelled:
            original = history[index]
            if seqs:
                contiguous = seqs[-1] - seqs[0] + 1 == len(seqs)
                span = f"seq {seqs[0]}–{seqs[-1]}" if contiguous else f"within seq {seqs[0]}–{seqs[-1]} ({len(seqs)} turns)"
                note = f"[archived turns {span}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]"
                annotated = annotate_summary(original, note, seqs)
            else:
                annotated = original.model_copy(update={"metadata": {**original.metadata, "daedalus.archived": {"seqs": []}}})
            history[index] = annotated
            key = self.sessions.transcript_key(original)
            if state.engine is not None:
                live = state.engine.history
                for li, lm in enumerate(live):
                    if lm.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in lm.metadata and self.sessions.transcript_key(lm) == key:
                        live[li] = annotated
                        break

    async def _transcribe_before_compaction(self, state: SessionState, engine: QueryEngine) -> None:
        """Write every turn of the working history into the transcript before a compaction in a run replaces them.

        The core hands its history over at the start of a run and at a background wake, not after
        each round, so the turns a run produced before it compacted were never written anywhere:
        HistoryExpand could not return them and the summary's archived range had holes in it. They
        are written here, with the model stamp and run id a hand-over would have given them, and
        their keys are kept to tell, once the compaction is done, which turns the summary replaced.
        """
        chain = state.persist_chain
        if chain is not None and not chain.done():
            await asyncio.wait([chain])  # its outcome is its own; this waits only for its turn
        history = list(engine.history)
        known = set(state.history_keys)
        for message in history:
            if self.sessions.transcript_key(message) not in known:
                message.metadata.setdefault("daedalus.run_id", state.run_id)
        self._stamp_model(state, history, state.history_keys)
        async with state.persist_lock:
            await self.sessions.append_transcript(state.session.id, history, from_history=True)
        state.compacting_keys = [self.sessions.transcript_key(m) for m in history]

    async def _label_compacted_history(self, state: SessionState, engine: QueryEngine) -> None:
        """Label the summary a compaction in the middle of a run just wrote, before the next request.

        The label used to be added by the next hand-over of the history, which comes at a
        background wake or at the end of the run, so requests went out without it and a later one
        carried it: a second rewrite of the head of the conversation, and a second miss on all of
        it. Measured on 2026-10-14, two compactions of a 980k session were each followed two calls
        later by a request of 465k tokens served 23k from the cache. The core waits for this while
        it handles the event, so the request it builds next already carries the label.
        """
        previous = state.compacting_keys if state.compacting_keys is not None else state.history_keys
        state.compacting_keys = None
        await self._label_summaries(state, engine.history, previous)
        await self._memory_history_rewritten(state, engine.history)

    async def _start_run(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        async with state.lock:
            return await self._start_run_locked(state, message, continue_turn=continue_turn)

    async def _start_run_locked(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        # A bounded indexing chunk yields before an agent starts; the index checks busy again
        # under this same lock, so background inference cannot overlap an active run.
        async with self.idle_work:
            if (self.settings.state_dir / "dependencies" / "maintenance").exists():
                raise RuntimeError("dependencies are being installed; wait for the application to restart")
            return await self._start_run_ready(state, message, continue_turn=continue_turn)

    async def _start_run_ready(
        self, state: SessionState, message: Message | None, *, continue_turn: bool = False
    ) -> str:
        async with self.db.transaction() as conn:
            await assert_admission_open_in(conn)
        if state.running and not continue_turn:
            raise RuntimeError("a run is already active in this session")
        state.run_active_since = time.monotonic()
        state.last_error_kind = ""
        state.last_error_message = ""
        state.last_error_permanent = False
        state.run_outcome = None
        state.run_cause = None
        state.last_compaction = None
        if message is not None:
            state.run_origin = str(message.metadata.get("daedalus.origin") or "operator")
        if message is not None and not continue_turn:
            message = await self._with_turn_context(state, message)
        if state.engine is None or not continue_turn:
            run_id = uuid.uuid4().hex[:12]
            engine = await self._build_engine(state, run_id, message=message)
            state.tool_groups_loaded = {}
            await self._clear_queued_group_loads(state)
            if state.engine is not None:
                engine.history = list(state.engine.history)
            else:
                engine.history = list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))
            state.engine = engine
            state.run_history_start = len(engine.history)
        else:
            run_id = state.run_id or uuid.uuid4().hex[:12]
            engine = state.engine
        self._stamp_memory(state, engine)
        admission = self.service_hooks.get("execution_run_admission")
        staff_session_id = state.metadata.get("staff_session_id")
        if staff_session_id and admission is not None:
            await admission(str(staff_session_id), state.session.id, run_id)
        if message is not None:
            message.metadata["daedalus.run_id"] = run_id
            await self.sessions.replace_transcript_message(state.session.id, self.sessions.transcript_key(message), message)
        if not continue_turn:
            run = Run(id=run_id, tenant_id=TENANT, session_id=state.session.id, status=RunStatus.running)
            requested = message.metadata.get("daedalus.provider_resume") if message is not None else None
            if requested is not None:
                client_message_id = str(message.metadata.get("daedalus.client_message_id") or "")
                seqs = await self.sessions.transcript_seqs(state.session.id, [self.sessions.transcript_key(message)])
                async with self.db.transaction() as conn:
                    await resume_target_in(
                        conn, state.session.id, requested["failed_run_id"], requested["provider_id"],
                        requested["model"], client_message_id,
                    )
                    await self.runs.create_in(conn, run)
                    await pin_resumed_run_in(
                        conn, action_id=client_message_id.removeprefix("provider-resume:"),
                        session_id=state.session.id, run_id=run_id,
                    )
                    # Run ownership, the provider pin and input placement survive the same
                    # crash. A restart must never build the default chain for this decision.
                    await conn.execute(
                        "UPDATE input_receipts SET status = 'consumed',run_id = ?,step_id = 'start',"
                        "message_seq = ?,updated_at = ? WHERE session_id = ? AND client_message_id = ?"
                        " AND status = 'accepted'",
                        (run_id, seqs[0] if seqs else None, datetime.now(UTC).isoformat(),
                         state.session.id, client_message_id),
                    )
                    snapshot = {**engine.snapshot(), "state": "running",
                                "provider_resume_input": message.model_dump(mode="json")}
                    await conn.execute(
                        "INSERT INTO snapshots(run_id,tenant_id,session_id,state,snapshot,updated_at)"
                        " VALUES (?,?,?,'running',?,?)",
                        (run_id, TENANT, state.session.id, json.dumps(snapshot), datetime.now(UTC).isoformat()),
                    )
            else:
                await self.runs.create(run)
            state.run_id = run_id
            await self._publish(state, "run.started", {"run_id": run_id, "origin": state.run_origin, "title": state.session.title})
        else:
            await self.runs.update_status(run_id, TENANT, RunStatus.running)
            state.run_id = run_id
        state.task = asyncio.create_task(self._drive(state, engine, message, continue_turn), name=f"run:{run_id}")
        state.task.add_done_callback(_log_task_failure)
        await self._publish_status(state, "running")
        return run_id

    def _running_jobs_note(self, session_id: str) -> str:
        """The turn-context line naming the session's jobs that still run, and the promise about them.

        An agent that started a job in an earlier turn did not know, at the start of the next one, that
        it was still out there — and either started another waiter for it or claimed it was finished."""
        running = [job for job in self.jobs.get(session_id, {}).values() if job.running]
        if not running:
            return ""
        mono = time.monotonic()
        named = "; ".join(
            f"{job.id}{' (service)' if job.kind == 'service' else ''}, {int((mono - job.started) // 60)} min: `{' '.join(shell_title(job).split())[:80]}`"
            for job in running[:6]
        )
        more = f" and {len(running) - 6} more" if len(running) > 6 else ""
        return f"- Background jobs still running: {named}{more}. Each wakes you with its outcome when it ends; there is nothing to poll."

    async def _side_questions_note(self, session_id: str) -> str:
        """The side questions this session still has waiting, as a turn-context line.

        In the turn context because the model forgets what it asked: past a compaction it would ask
        again, or never take back what it has since settled. ``""`` when none waits."""
        try:
            open_now = await self.asks.of_session(session_id)
        except Exception:  # noqa: BLE001 — a turn starts without the line rather than not at all
            logger.warning("could not read the side questions of %s", session_id, exc_info=True)
            return ""
        if not open_now:
            return ""
        listed = "; ".join(f"[{ask.short_id}] {ask.heading}" for ask in open_now)
        return f"- Your side questions still waiting for the operator: {listed}. Drop any you have settled yourself with DropSideQuestion."

    async def _with_turn_context(self, state: SessionState, message: Message) -> Message:
        """The run's opening message with the turn context as its last block.

        Same message identity (role and creation time), so the transcript keeps the copy that was
        recorded when the operator sent it and the working history carries the one the model reads;
        the app and the summariser strip the block from what they show.
        """
        # The core allows a user message one content block, so the context joins the text rather
        # than following it; a message with no text (an image alone) goes as it is.
        at = next((i for i, block in enumerate(message.content_blocks) if isinstance(block, TextBlock)), None)
        if at is None:
            return message
        notes: str | None = None
        for hook in self.turn_notes_hooks:
            try:
                notes = await hook(state)
            except Exception:  # noqa: BLE001 — a turn starts without its notes rather than not at all
                logger.exception("turn notes hook failed for session %s", state.session.id)
            if notes is not None:
                break
        volatile = [notes if notes is not None else await self.workspace_notes(state)]
        facts = await self._project_facts(state)
        if facts:
            volatile.append("\n" + facts)
        history: Sequence[Message] | None = None

        async def working() -> Sequence[Message]:
            # Read once for the notes that judge what the model was already told (YAGNI, memory):
            # with no engine yet it is the whole stored history.
            nonlocal history
            if history is None:
                history = await self._working_history(state)
            return history

        memory = await self._memory_turn(state, working)
        if memory.text:
            volatile.append("\n" + memory.text)
        if privacy.is_private(state):
            volatile.append("\n- " + privacy.TURN_NOTE)
        switched_off = await self._take_mcp_switched_off(state)
        if switched_off:
            volatile.append("\n" + switched_off)
        loop_state = str(state.metadata.get("loop_state") or "").strip()
        if loop_state:
            volatile.append("\n" + loop_state)  # the loop's counter and next wake-up change every run
        yagni = await self._yagni_note(state, await working())
        if yagni:
            volatile.append("\n- " + yagni)
        if waiting := self._running_jobs_note(state.session.id):
            volatile.append("\n" + waiting)
        if asked := await self._side_questions_note(state.session.id):
            volatile.append("\n" + asked)
        if computer := await self._computer_note(state):
            volatile.append("\n" + computer)
        for line_hook in self.turn_line_hooks:
            try:
                line = await line_hook(state)
            except Exception:  # noqa: BLE001 — a turn starts without the line rather than not at all
                logger.exception("turn line hook failed for session %s", state.session.id)
                continue
            if line:
                volatile.append("\n" + line)
        if self.is_btw(state):
            volatile.append("\n- " + prompts.BTW_TURN_NOTE)
        if self.is_prompt_coach(state) and not privacy.is_private(state) and (style := user_style.context_block((await user_style.load(self.db)).summary)):
            # A coach writes a prompt in the operator's voice, so it reads how they write. Here and not in
            # its role prompt: the profile changes as it learns, and the role prompt is the cached prefix.
            volatile.append("\n\n" + style)
        if handed := operator_secrets.prompt_section(self.secrets.available(state.session.id)):
            # In the turn context rather than the system prompt: the set changes whenever the operator adds
            # one, and the system prompt is the part every later call is cached on.
            volatile.append("\n\n" + handed)
        # A report, a board line or a note can quote a value the operator handed over; it goes in as its placeholder.
        context = prompts.turn_context(notes=self.redactor.conceal_named("".join(volatile)))
        blocks = list(message.content_blocks)
        said = blocks[at].text  # type: ignore[union-attr]
        blocks[at] = TextBlock(text=f"{said.rstrip()}\n\n{context}")
        update: dict[str, Any] = {"content_blocks": blocks}
        if message.metadata.get(OPERATOR_WORDS_METADATA_KEY) in (None, True):
            # The context is the host's, not the operator's: without a mark the core would quote the
            # board, the clock and the YAGNI note as the operator's words when it compacts.
            update["metadata"] = {**message.metadata, **operator_words_metadata(str(message.metadata.get("daedalus.origin") or "operator"), prompts.without_turn_context(said))}
        if yagni:
            # The app draws the switch as a mark on the message instead of the note, which it never shows.
            update["metadata"] = {**update.get("metadata", message.metadata), "daedalus.yagni": "on" if yagni.startswith(prompts.YAGNI_ON) else "off"}
        opened = message.model_copy(update=update)
        if memory.snapshot is not None:
            await self._keep_memory_snapshot(state, memory, self.sessions.transcript_key(opened))
        return opened

    async def _working_history(self, state: SessionState) -> Sequence[Message]:
        """The history the next run starts from: the engine's while one is kept, else the stored one."""
        if state.engine is not None:
            return state.engine.history
        return await self.sessions.list_messages(state.session.id, TENANT, limit=10_000)

    def memory_block_wanted(self, state: SessionState) -> bool:
        """Whether this run's opening context may carry the memory block (:mod:`daedalus.memory.render`).

        Only the operator's own turn in a conversation they read. A private chat is handed nothing
        from other chats. A staff member, a subagent, a scheduled or unattended session, a heartbeat,
        a prompt coach and a side question are not talking to the operator, and what their task needs
        is in their brief. A harness session (a CLI agent a staff member runs in a terminal) never
        comes through here at all. A run some other origin started in an operator's chat — a
        schedule's turn, a loop's tick, a report waking it — waits: the block or its delta rides on
        the operator's next message, whose words the core keeps verbatim through a compaction, so the
        block is never handed to a summariser to paraphrase."""
        if privacy.is_private(state) or self.is_staff(state) or any(state.metadata.get(key) for key in NOT_OPERATOR_FACING):
            return False
        return state.run_origin == "operator"

    async def _memory_turn(self, state: SessionState, working: Callable[[], Awaitable[Sequence[Message]]]) -> memory_render.Turn:
        """What this run's opening context carries of memory: the whole block the first time, a delta
        when memory changed since, nothing while it has not — the earlier block is still in the history."""
        if not self.memory_block_wanted(state):
            return memory_render.Turn("", None)
        try:
            snapshot = memory_render.Snapshot.read(state.metadata)
            block = await memory_render.render_for(
                self.memory_entries, project_id=self.memory_placement(state).project_id,
                budget=memory_render.budget_for(await self.session_window(state)),
            )
            history = await working()
            keys = {self.sessions.transcript_key(m) for m in history}
            quotes: dict[str, str] = {}
            if snapshot is not None and snapshot.carriers and snapshot.digest != block.digest:
                carriers = set(snapshot.carriers)
                quotes = memory_render.told_lines(
                    text[text.rfind(prompts.TURN_CONTEXT_OPEN):]
                    for m in history if self.sessions.transcript_key(m) in carriers
                    for text in ("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)),)
                )
            return memory_render.plan(snapshot, block, history_keys=keys, quotes=quotes)
        except Exception:  # noqa: BLE001 — a turn starts without memory rather than not at all
            logger.exception("the memory block for session %s could not be rendered", state.session.id)
            return memory_render.Turn("", None)

    async def _keep_memory_snapshot(self, state: SessionState, turn: memory_render.Turn, key: str) -> None:
        """Record what the opening message ``key`` told the model of memory, so the next run knows."""
        snapshot = turn.snapshot
        assert snapshot is not None
        if turn.text:
            snapshot.carriers = [key] if turn.full else [*snapshot.carriers, key]
        await self._write_memory_snapshot(state, snapshot)

    async def _write_memory_snapshot(self, state: SessionState, snapshot: memory_render.Snapshot) -> None:
        dumped = snapshot.dump()
        for meta in (state.metadata, state.session.metadata):
            if dumped == memory_render.Snapshot().dump():
                meta.pop(memory_render.SNAPSHOT_KEY, None)
            else:
                meta[memory_render.SNAPSHOT_KEY] = dumped
        try:
            await self.sessions.update_metadata(state.session.id, state.session.metadata)
        except Exception:  # noqa: BLE001 — at worst the next run carries the block or the delta again
            logger.warning("could not keep the memory snapshot of session %s", state.session.id, exc_info=True)

    async def _memory_history_rewritten(self, state: SessionState, history: Sequence[Message]) -> None:
        """After a rewrite of the working history: when a message that carried the memory block or a
        delta of it is gone (summarised, cut by a revert, cleared), the next run carries the whole
        block again. The rewrite already changed the head of the conversation, so this costs no cache."""
        snapshot = memory_render.Snapshot.read(state.metadata)
        if snapshot is None or not snapshot.carriers or snapshot.reinject:
            return
        if set(snapshot.carriers) <= {self.sessions.transcript_key(m) for m in history}:
            return
        snapshot.reinject = True
        await self._write_memory_snapshot(state, snapshot)

    async def _project_facts(self, state: SessionState) -> str:
        """The project's promoted facts for its coordinator and ordinary chats; ``""`` elsewhere.

        A staff member's session is left out: its launch packet already carries the facts chosen
        for its task, and a second, unselected copy would only crowd its context.
        """
        if state.project is None or state.metadata.get("staff_id") or privacy.is_private(state):
            # A private chat is handed nothing learned from other chats, the project's facts included.
            return ""
        try:
            return self.redactor.redact(await project_facts_note(self.db, state.project.id))
        except Exception:  # noqa: BLE001 — a turn starts without the facts rather than not at all
            logger.exception("project facts for session %s could not be read", state.session.id)
            return ""

    async def _take_mcp_switched_off(self, state: SessionState) -> str:
        """The note for the MCP servers the operator switched off since the last turn, once; ``""`` for none.

        Without it nothing told the model: the tools left the surface, but the history still said
        "Loaded, and callable" of them, and a model asked what it could use listed them.
        """
        if "mcp_switched_off" not in state.metadata:
            return ""
        # A server switched back on since is not gone; its tools are named again where they are.
        servers = [str(s) for s in (state.metadata.get("mcp_switched_off") or ()) if str(s) not in self.mcp_enabled(state)]
        for meta in (state.metadata, state.session.metadata):
            meta.pop("mcp_switched_off", None)
        try:
            await self.sessions.update_metadata(state.session.id, state.session.metadata)
        except Exception:  # noqa: BLE001 — at worst the note is given twice
            logger.warning("could not clear the switched-off MCP servers of session %s", state.session.id, exc_info=True)
        return "\n".join(
            f"- MCP server {server} was switched off: its tools ({mcp_tool_prefix(server)}*) are no longer available, whatever an earlier result said."
            for server in servers
        )

    async def _drive(
        self, state: SessionState, engine: QueryEngine, message: Message | None, continue_turn: bool
    ) -> None:
        run_id = engine.config.run_id
        status = "completed"
        pinned = fork_identity(state.metadata)
        if pinned and pinned.get("cache_key"):
            # This task's own context: the routing reaches every request of the run and nothing else.
            CACHE_ROUTING.set(str(pinned["cache_key"]))
        try:
            # A continuation drives the model against the history as it stands; nothing is appended.
            closed = None
            async with self.db.transaction() as conn:
                try:
                    await assert_admission_open_in(conn)
                except UpdateDrainActive as exc:
                    closed = exc
            if closed is not None:
                # Snapshot persistence takes its own database lock. Keep it outside the
                # admission transaction, including when a run has not entered the core yet.
                if message is not None and not continue_turn:
                    engine.history.append(message)
                if engine.state is not LoopState.RUNNING:
                    engine.transition_to(LoopState.RUNNING)
                await engine.persist_snapshot()
                raise UpdateDrainPaused(str(closed)) from closed
            iterator = engine.run(None) if continue_turn else engine.run(message)
            async for event in iterator:
                await self._dispatch_event(state, event)
                if event.type is EventType.COMPACTION_STARTED:
                    await self._transcribe_before_compaction(state, engine)
                elif event.type is EventType.COMPACTION_COMPLETED:
                    await self._label_compacted_history(state, engine)
            if engine.state is LoopState.AWAITING and state.pending is not None:
                status = "awaiting"
            elif engine.state is LoopState.FAILED:
                status = "failed"
            elif engine.state is LoopState.CANCELLED:
                status = "cancelled"
            elif await self._report_provider_refusal(state, run_id):
                status = "failed"
        except UpdateDrainPaused:
            status = "interrupted"
            await self.runs.update_status(run_id, TENANT, RunStatus.paused)
            raise
        except asyncio.CancelledError:
            parked = run_id in self._update_parked_run_ids
            status = "interrupted" if self.shutting_down or parked else "cancelled"
            if parked:
                await self.runs.update_status(run_id, TENANT, RunStatus.paused)
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator, never swallowed
            logger.exception("run %s crashed", run_id)
            status = "failed"
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.ERROR, run_id=run_id, payload={"message": f"{type(exc).__name__}: {exc}"}),
            )
        finally:
            if status != "awaiting" and state.provider_hold is not None:
                state.provider_hold.close()
                state.provider_hold = None
                await self.providers.close_retired()
            # Everything between the last token and the end of this function is time the app spends
            # drawing a run that is over: the chip says "running" and the cursor blinks under the
            # finished answer. So only what the answer itself depends on — writing it down and saying
            # the run ended — happens here; the rest is handed to a task nobody watches.
            await self._persist_history(state, list(engine.history), state.history_keys)
            state.history_keys = [self.sessions.transcript_key(m) for m in engine.history]
            # Here and not in the settling task: the next message may start a run before that task
            # has done anything, and it reads what this run loaded. The store is written there.
            carried = self._keep_loaded_tools(state, engine)
            try:
                if status == "interrupted":
                    pass  # snapshot stays; resume_unfinished() continues the run after restart
                elif status == "awaiting":
                    await self.runs.update_status(run_id, TENANT, RunStatus.paused)
                else:
                    run_status = {
                        "completed": RunStatus.completed,
                        "failed": RunStatus.error,
                        "cancelled": RunStatus.cancelled,
                    }.get(status, RunStatus.completed)
                    await self.runs.update_status(run_id, TENANT, run_status)
            except Exception:  # noqa: BLE001
                logger.exception("run %s bookkeeping failed", run_id)
            # The announcement goes out before the task is made, and the task before this function
            # returns: a created task does not run until the loop gets the turn back, which is after
            # ``_drive`` has ended and the session reads as idle. A run-finished callback that starts
            # the next run — the companion answering, a leader collecting a subagent — would
            # otherwise be told the run is over while this one is still technically going.
            # An interrupted run is not a settled one: it is parked. Its snapshot stays where it is,
            # ``resume_unfinished()`` drives it on after the restart, and nothing is announced —
            # saying "the run ended" would put every front back to idle on a turn that is about to
            # continue. The run-finished callbacks do not run for it either, and that is the change
            # this made: they used to run for every status. They hand a finished answer on (a leader
            # collecting a subagent, a schedule learning its run ended), and this answer is not
            # finished; the resumed run announces itself when it really is.
            if status != "interrupted":
                await self._announce_settled(state, run_id, status, housekeeping=True)
                if status == "awaiting":
                    # Said here, before the housekeeping task exists, so an answer that arrives at once
                    # cannot publish its "running" ahead of this "waiting".
                    await self._publish_status(state, "waiting")
                state.settled.clear()
                state.housekeeping = asyncio.create_task(self._settle_run(state, run_id, status, carried=carried), name=f"settle:{run_id}")
                state.housekeeping.add_done_callback(_log_task_failure)

    PROVIDER_REFUSED_NOTE = "⚠️ the model provider refused this run's requests, so it was closed early — {detail}"
    """What the operator is shown when a run ends on the provider rather than on its work. It quotes the provider."""

    SPENDING_CAP_NOTE = "💸 a spending cap stopped this run before its next model call — {detail}"
    """What the operator is shown when the host's own cap, not the provider, ended the run."""

    async def _report_provider_refusal(self, state: SessionState, run_id: str) -> bool:
        """A run the provider refused ends as an error, whatever the model wrote on the way out.

        The core winds such a run down: the tools go, the model is told the endpoint failed, and it
        writes a closing message. That message is an answer to nobody's question — the work did not
        happen — and if it is the only thing the operator sees, a provider outage reads as a polite
        non-answer and the session looks like it finished. So the failure is put on the wire as an
        error and the run is recorded as one: the app draws the error state, the Telegram front adds
        the line, the inbox gets the entry, and the outage recovery drives the turn again once the
        endpoint is back — the same path a run that failed outright already takes.

        Nothing is claimed without the provider's own words: a wind-down with no detail behind it is
        some other kind of stop and is left alone. What ended the run is the core's word on its
        terminal event (``outcome`` and ``cause``), not a reading of the wind-down's text.
        """
        cause = state.run_cause
        if state.run_outcome is not RunOutcome.failed or cause is None:
            return False
        if cause.kind == CAUSE_MODEL_NO_PROGRESS:
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.ERROR, run_id=run_id, payload={
                    "kind": "model_no_progress",
                    "message": "The model repeatedly returned reasoning without an answer or a tool call; recovery attempts were exhausted.",
                }),
            )
            return True
        if cause.kind != CAUSE_PROVIDER_ERROR or not cause.detail:
            return False
        # The host's own spending cap is not the provider: saying the provider refused sent the
        # operator to check a key or an outage when the fix was a limit in the settings. The cap
        # raises with a ``budget`` verdict, which the core carries to here.
        capped = cause.failure is not None and cause.failure.reason is ProviderFailureReason.budget
        note = self.SPENDING_CAP_NOTE if capped else self.PROVIDER_REFUSED_NOTE
        await self._dispatch_event(
            state,
            TurnEvent(
                type=EventType.ERROR,
                run_id=run_id,
                payload={"kind": "spending_cap" if capped else "llm_provider_error",
                         "message": note.format(detail=cause.detail)},
            ),
        )
        return True

    async def _announce_settled(self, state: SessionState, run_id: str, status: str, *, housekeeping: bool) -> None:
        """Say on the wire that the run is over — once when the answer is written down, once when the rest is.

        The app ends the streaming turn on the model's own ``message_stop``; this is what puts the
        session back to idle beside it. ``housekeeping`` says whether anything is still being written
        behind the answer, so a front that wants to show it has something to show and something to
        take away again.
        """
        try:
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.RUN_SETTLED, run_id=run_id, payload={"status": status, "housekeeping": housekeeping, "session_id": state.session.id}),
            )
        except Exception:  # noqa: BLE001 — the run is over either way; a front that missed the event polls
            logger.warning("could not announce the end of run %s", run_id, exc_info=True)

    async def _settle_run(self, state: SessionState, run_id: str, status: str, *, carried: bool = False) -> None:
        """What a finished run still owes, off the path the operator is watching.

        Order is the contract. The snapshot comes first and raises ``state.settled``, because that is
        the one thing the next run may not start without: a revert of the turn that just ended has to
        find the files as that turn left them. Everything after it — the event log's own tidying, the
        run-finished callbacks (delivery to the other fronts, the learning record, a memory extraction
        that is a model call of its own), the queue that filled up while the run was settling, the
        compaction check — nobody waits for, and each is guarded so one failure does not eat the rest.
        """
        session_id = state.session.id
        # Measured before the snapshot: the operator waited for the answer, not for the commit.
        duration_s = time.monotonic() - state.run_active_since if state.run_active_since else 0.0
        try:
            if status in ("completed", "failed", "cancelled"):
                await self.checkpoint(state, kind="after", run_id=run_id)
        except Exception:  # noqa: BLE001
            logger.exception("run %s snapshot failed", run_id)
        finally:
            state.settled.set()  # the next run may start: the history is written and the files are snapshotted
        if carried:
            try:
                await self.sessions.update_metadata(session_id, state.session.metadata)
            except Exception:  # noqa: BLE001 — the tools are loaded again by a search if this is lost
                logger.warning("could not store the loaded tools of session %s", session_id, exc_info=True)
        if status in ("completed", "failed", "cancelled"):
            # Before anything announces the end: the closing line is part of the turn the app redraws.
            await self._close_without_answer(state, run_id, status)
        if status in ("completed", "failed", "cancelled"):
            # A run parked on a question is not finished; an interrupted one never reaches here.
            finished = self._finished_payload(state, run_id, status, duration_s)
            await self._publish(state, "run.finished", finished)
            await self._publish_status(state, "failed" if status == "failed" else "idle")
            if status != "cancelled" and finished["operator_facing"] and not finished["watched"] and not finished["telegram"]:
                await self._mark_unread(state, run_id)
        if status in ("completed", "failed", "cancelled"):
            try:
                await self.events.delete_snapshot(run_id)
                self.events.close_run(run_id)
                await self.events.trim(run_id, TENANT, max_len=self.config.ops.events_keep_per_run)
            except Exception:  # noqa: BLE001
                logger.exception("run %s event bookkeeping failed", run_id)
        for callback in self._finished:
            try:
                await callback(session_id, run_id, status)
            except Exception:  # noqa: BLE001
                logger.exception("run-finished callback failed")
        await self._announce_settled(state, run_id, status, housekeeping=False)
        if status in ("completed", "failed"):
            # A run that ended in an error still owes an answer to what arrived meanwhile.
            try:
                await self._drain_leftover_follow_ups(state)
            except Exception:  # noqa: BLE001
                logger.exception("draining the queue of session %s failed", session_id)
        if status in ("completed", "failed", "cancelled"):
            # On a task of its own, and not awaited here: a summariser call is a minute or two,
            # and while this task is unfinished the session reads as running — which blocks the
            # next message in submit() and tells the app a run is in progress that is not one.
            state.auto_compaction = self._spawn_background(self._maybe_auto_compact(state), f"auto-compact:{session_id}")
        if status == "completed":
            state.overflow_streak = 0
            state.outage_streak = 0
        elif status == "failed" and state.last_error_kind == "llm_context_window_exceeded" and not state.running:
            await self._recover_from_overflow(state)
        elif status == "failed" and state.last_error_kind in PROVIDER_OUTAGE_KINDS and state.last_error_permanent:
            # A refusal is not an outage: the provider said what is wrong with the request, key or model, and
            # driving the turn again after a wait sends it the same request to refuse the same way. The failed
            # run with the provider's words is the answer until someone changes what it refused.
            logger.warning("session %s: the provider refused the run for good; not driving it again", state.session.id)
        elif status == "failed" and state.last_error_kind in PROVIDER_OUTAGE_KINDS and not state.running:
            self._schedule_outage_recovery(state)

    async def _close_without_answer(self, state: SessionState, run_id: str, status: str) -> None:
        """A run that produced no answer says so, as the closing line of its turn in the transcript.

        Only the transcript: the model's working history does not carry it, so it costs no context and
        cannot read as an answer. ``state.last_outcome`` is what a loop's next iteration is told.
        """
        steps = 0
        last_tool = ""
        if state.engine is not None:
            for message in state.engine.history:
                if message.metadata.get("daedalus.run_id") not in (run_id, None) or message.role is not MessageRole.assistant:
                    continue
                for block in message.content_blocks:
                    if isinstance(block, ToolUseBlock):
                        steps += 1
                        last_tool = block.name
        outcome = run_outcome(
            status,
            error_kind=state.last_error_kind,
            error_message=state.last_error_message or (state.run_cause.detail if state.run_cause is not None else ""),
            compaction=state.last_compaction,
            steps=steps,
            last_tool=last_tool,
        )
        state.last_outcome = outcome
        if outcome is None:
            return
        try:
            await self.sessions.append_transcript(state.session.id, [outcome_message(outcome, run_id)])
        except Exception:  # noqa: BLE001 — the line is a courtesy; the run's own record is already written
            logger.warning("could not write the closing line of run %s", run_id, exc_info=True)

    OUTAGE_NOTE = (
        "[The previous turn stopped because the model provider was unreachable for a while. "
        "Nothing was lost; continue the work from where it stopped.]"
    )

    def _schedule_outage_recovery(self, state: SessionState) -> None:
        """A run the provider dropped is not the end of the task either: wait, then drive the turn again.

        The core already retried in place and wound the run down; that covers a blip of seconds. An outage
        of minutes ends in a failed run, and a failed run used to stay failed until the operator wrote
        something — the turn had to be resumed by hand. The wait grows with every consecutive failure and
        the attempts are bounded, so a provider that is down for the day does not keep a session busy.
        """
        if self.shutting_down:
            return
        ops = self.config.ops
        if state.outage_streak >= ops.provider_retry_max_attempts:
            logger.warning("session %s: the provider failed %d runs in a row; not retrying", state.session.id, state.outage_streak)
            return
        state.outage_streak += 1
        delay = min(ops.provider_retry_base_seconds * 2 ** (state.outage_streak - 1), ops.provider_retry_max_seconds)
        logger.warning("session %s: provider failure %d; driving the turn again in %.0f s", state.session.id, state.outage_streak, delay)
        if state.outage_task is not None and not state.outage_task.done():
            state.outage_task.cancel()
        state.outage_task = asyncio.create_task(self._recover_from_outage(state, state.run_id, delay), name=f"outage:{state.session.id}")
        state.outage_task.add_done_callback(_log_task_failure)

    async def _recover_from_outage(self, state: SessionState, failed_run_id: str | None, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.shutting_down or self._states.get(state.session.id) is not state:
            return
        if state.running or state.pending is not None or state.run_id != failed_run_id:
            return  # something else moved the session on meanwhile (an operator message, a scheduled turn)
        if failed_run_id:
            async with self.db.transaction() as conn:
                if await recovery_candidate_in(conn, state.session.id, failed_run_id):
                    return
        try:
            await self.submit(state.session.id, self.OUTAGE_NOTE, as_answer=False, origin="core")
        except RuntimeError as exc:
            logger.warning("session %s: outage recovery did not start: %s", state.session.id, exc)

    OVERFLOW_NOTE = (
        "[The previous turn stopped because the conversation no longer fitted the model's context window. "
        "The history has been compacted into the summary above; continue the work from where it stopped.]"
    )

    async def _recover_from_overflow(self, state: SessionState) -> None:
        """A run the provider refused for size is not the end of the task: shrink the history and drive the turn again."""
        if self.shutting_down:
            return  # the interrupted run resumes after the restart; recovery would start a run into a closing process
        if state.overflow_streak >= 2:
            logger.warning("session %s: overflowed %d times in a row; not retrying", state.session.id, state.overflow_streak)
            return
        state.overflow_streak += 1
        cfg = self.config.compaction
        status = await self.context_status(state)
        if status["messages"] >= cfg.min_messages:
            async with state.lock:
                try:
                    await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True)  # own_task_ok: the required_only call runs inside the run's own task
                except RuntimeError as exc:
                    logger.warning("session %s: overflow recovery could not compact: %s", state.session.id, exc)
                    return
        logger.warning("session %s: context overflow; history compacted, driving the turn again", state.session.id)
        try:
            await self.submit(state.session.id, self.OVERFLOW_NOTE, as_answer=False, origin="core")
        except RuntimeError as exc:
            logger.warning("session %s: overflow recovery did not start: %s", state.session.id, exc)

    async def _drain_leftover_follow_ups(self, state: SessionState) -> None:
        """Input that arrived while the run was settling starts the next turn instead of rotting in the queue.

        Under ``submit_lock`` because this is the second place a run is started: the settling run's
        queue and an operator message arriving at the same moment must become one run, not two driving
        one history. A message that got there first is already running — it took the queue with it.
        """
        if self.shutting_down:
            return  # the queue is in the store; the next start reads it rather than opening a run into a shutdown
        async with state.submit_lock:
            if state.running:
                return
            await self._drain_locked(state)

    async def _drain_locked(self, state: SessionState) -> None:
        queued = await self.live.load(state.session.id)
        items = [item for item in queued["follow_up"] + queued["steer"] if str(item.get("text") or "").strip()]
        if not items:
            return
        await self.live.save_queues(state.session.id, [], [])
        await self.steer_changed(state.session.id, reason="consumed")
        texts = [str(item["text"]).strip() for item in items]
        origins = {str(item.get("origin") or "operator") for item in items}
        # The queued rows stay hidden; this copy, the one that opens the run, is where the chat shows the words.
        origin = "operator" if "operator" in origins else next(iter(origins))
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="\n\n".join(texts))], metadata={"daedalus.origin": origin, "daedalus.delivery": "drained", **_merged_words(items), **_image_refs_metadata(_queued_image_refs(items))})
        # The app keeps a temporary bubble for each message it sent until a shown row names that message's
        # receipt id. This copy used to carry none: the queued row that does is hidden, so a message that
        # arrived as a turn ended stayed drawn twice — the delivered row and, under it, the bubble with the
        # words as typed — until the page was reloaded. Same as a prompt the core places mid-run.
        client_message_ids = [str(item["id"]) for item in items if item.get("id") and await self.live.receipt(state.session.id, str(item["id"])) is not None]
        if client_message_ids:
            message.metadata["daedalus.client_message_ids"] = client_message_ids
        await self.sessions.append_transcript(state.session.id, [message])
        seqs = await self.sessions.transcript_seqs(state.session.id, [self.sessions.transcript_key(message)])
        await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
        run_id = await self._start_run(state, message)
        for client_message_id in client_message_ids:
            await self.live.consume(state.session.id, client_message_id, run_id=run_id, step_id="start", message_seq=seqs[0] if seqs else None)

    @staticmethod
    def _rung_label(provider: Any, model: str) -> str:
        """``provider:model`` — one rung of the chain, named the way the chain names it."""
        return f"{getattr(getattr(provider, 'endpoint', None), 'id', '') or '?'}:{model}"

    def model_status(self, state: SessionState) -> dict[str, Any]:
        """What is really answering this session, and whether that is what it was set to.

        ``fallback`` is present only while another model holds the run: the configured one
        answering again takes it away, which is what makes the header's note self-clearing.
        """
        configured = state.configured_model
        effective = state.effective_model or configured
        out: dict[str, Any] = {
            "configured_model": configured.split(":", 1)[-1] if configured else "",
            "effective_model": effective.split(":", 1)[-1] if effective else "",
            "effective_provider": effective.split(":", 1)[0] if ":" in effective else "",
            "fallback": None,
        }
        if configured and effective and effective != configured:
            out["fallback"] = {
                "from": configured.split(":", 1)[-1],
                "to": effective.split(":", 1)[-1],
                "reason": state.model_change_reason or "chain_step",
            }
        return out

    def _model_record(self, state: SessionState) -> dict[str, Any]:
        """The stamp an assistant turn carries: who answered, and what the session had asked for."""
        status = self.model_status(state)
        record: dict[str, Any] = {
            "provider": status["effective_provider"],
            "model": status["effective_model"],
            "configured": status["configured_model"],
        }
        if status["fallback"] is not None:
            record["fallback"] = status["fallback"]
        return record

    def _model_change(self, state: SessionState, event: TurnEvent) -> TurnEvent | None:
        """The event that says the model answering has changed — or ``None`` when it has not.

        Two kinds of event feed this, and both are needed. The core's ``model_changed`` is a demotion
        down the chain or a restore that re-seated the run on a later rung: it names the rung and
        why, and for a demotion it is the only notice there is, because the retry re-opens the
        stream inside the assistant message that was already started — no second ``message_start``
        announces it. And ``message_start`` is what catches the one way the model can differ from
        the configured one that the core does not announce: a model rebound by a live override.
        """
        payload = event.payload
        engine = state.engine
        if engine is None:
            return None
        if event.type is EventType.MODEL_CHANGED and "configured" not in payload:
            return self._observe_model(
                state,
                event.run_id,
                str(payload.get("to") or ""),
                FALLBACK_REASONS.get(str(payload.get("error_class") or ""), "chain_step"),
            )
        if event.type is EventType.MESSAGE_START:
            return self._observe_model(
                state,
                event.run_id,
                str(payload.get("model") or getattr(engine.config, "model_name", "") or ""),
                state.model_change_reason or "chain_step",
            )
        return None

    def _observe_model(self, state: SessionState, run_id: str, model: str, reason: str) -> TurnEvent | None:
        """Record which model is answering now; say so on the wire when it is not the one before."""
        engine = state.engine
        if engine is None or not model:
            return None
        observed = self._rung_label(getattr(engine, "llm", None), model)
        previous = state.effective_model
        if observed == previous:
            return None
        state.effective_model = observed
        if not previous and observed == state.configured_model:
            # The run's first message on the model it was configured with: nothing changed, it began.
            return None
        if not previous:
            reason = "live_override" if reason == "chain_step" else reason
        state.model_change_reason = "" if observed == state.configured_model else reason
        return TurnEvent(
            type=EventType.MODEL_CHANGED,
            run_id=run_id,
            payload={
                "from": (previous or state.configured_model).split(":", 1)[-1],
                "to": model,
                "model_name": model,
                "provider": observed.split(":", 1)[0],
                "configured": state.configured_model.split(":", 1)[-1],
                "reason": reason,
                "fallback": observed != state.configured_model,
                "session_id": state.session.id,
            },
        )

    def _record_model_stamp(self, state: SessionState) -> None:
        """Name the model on the assistant turn that has just ended, while it is still the one answering.

        The turn is already in the working history by ``message_stop`` — the tool result of a tool
        round lands before it — so the last assistant message there is the one that stopped. The
        record is kept against that message's transcript key until the persist of its round picks it
        up; the map is capped so a session whose turns are never written cannot grow it without end.
        """
        engine = state.engine
        if engine is None or not state.effective_model:
            return
        message = next((m for m in reversed(list(engine.history)) if m.role is MessageRole.assistant), None)
        if message is None:
            return
        key = self.sessions.transcript_key(message)
        if key in state.model_stamps:
            return
        state.model_stamps[key] = self._model_record(state)
        while len(state.model_stamps) > MODEL_STAMPS_KEPT:
            state.model_stamps.pop(next(iter(state.model_stamps)))

    async def _dispatch_event(self, state: SessionState, event: TurnEvent) -> None:
        if event.type is EventType.MODEL_CHANGED and "configured" not in event.payload:
            # The core's own notice of a chain advance or a restore on a later rung is read, not
            # passed on: the app is sent the host's event in its place (see ``_model_change``),
            # which also names the provider, the configured model and whether it is a fallback.
            # Passing both put two model changes in front of the app for one demotion.
            change = self._model_change(state, event)
            if change is not None:
                await self._dispatch_event(state, change)
            return
        if event.type is EventType.QUEUE_UPDATE and event.payload.get("placed") and state.engine is not None:
            # The core yields immediately after appending the received prompt. Submission time
            # belongs to the queue; this message's place in history is when the model received it.
            message = state.engine.history[-1]
            if message.role is MessageRole.user:
                queued = await self.live.load(state.session.id)
                placed = set(event.payload["placed"])
                items = [item for kind in ("steer", "follow_up") for item in queued[kind] if item.get("id") in placed]
                origin = str(items[0].get("origin") or "operator") if items else "operator"
                message.metadata.update({"daedalus.origin": origin, "daedalus.delivery": event.payload.get("kind"), "daedalus.run_id": event.run_id})
                if items:
                    # The images came with the items: the core places each one's ``blob_ref`` on the
                    # message under its own key, so only the words need saying here.
                    message.metadata.update(_merged_words(items))
                else:
                    # A placed prompt the queue no longer lists: its words are the operator's only
                    # when the operator sent it, and the text as placed is the best copy there is.
                    message.metadata.update(operator_words_metadata(origin, prompts.without_turn_context(message.text)))
                # The core may combine queued prompts into one visible row; carry each receipt id
                # so the app keeps its temporary bubble until that row is actually shown.
                client_message_ids = []
                for client_message_id in sorted(placed):
                    if await self.live.receipt(state.session.id, str(client_message_id)) is not None:
                        client_message_ids.append(str(client_message_id))
                        await self.live.consume(
                            state.session.id,
                            str(client_message_id),
                            run_id=event.run_id,
                            step_id=str(event.payload.get("step_id") or "queue"),
                            message_seq=None,
                        )
                if client_message_ids:
                    message.metadata["daedalus.client_message_ids"] = client_message_ids
        change = self._model_change(state, event)
        if change is not None:
            # Before the message it explains, not after it: the header says which model is speaking
            # while the first token of that model is still on its way.
            await self._dispatch_event(state, change)
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user":
            pending = PendingQuestion(
                session_id=state.session.id,
                run_id=event.run_id,
                tool_call_id=str(event.payload.get("tool_call_id")),
                kind="ask_user",
                payload=dict(event.payload.get("ask_user_payload") or {}),
            )
            state.pending = pending
            await self.db.execute(
                "INSERT OR REPLACE INTO pending_questions(session_id, run_id, tool_call_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (pending.session_id, pending.run_id, pending.tool_call_id, pending.kind, json.dumps(pending.payload), datetime.now(UTC).isoformat()),
            )
            await self._publish(state, "ask.pending", self._ask_payload(state, pending))
        self._time_tool(state, event)
        self._track_tool_groups(state, event)
        self._redact_event(state, event)
        durable = event.to_event()
        durable.payload.setdefault("tenant_id", TENANT)
        durable.payload.setdefault("event_type", event.type.value)
        await self.events.emit(durable)
        envelope = await self.events.publish_session(state.session.id, event)
        event.payload["_session_envelope"] = envelope
        for sink in self._sinks:
            try:
                await sink(state.session.id, event)
            except Exception:  # noqa: BLE001
                logger.exception("event sink failed")
        if event.type is EventType.MESSAGE_STOP:
            self._record_model_stamp(state)
            await self._enforce_caps(state, event.run_id)

    @staticmethod
    def _track_tool_groups(state: SessionState, event: TurnEvent) -> None:
        """Keep what the run says of its tool groups, for the session's panel: the core's word on an
        ``auto`` group is the only one there is, and a group loaded mid-run is loaded now."""
        if event.type.value == "tool_surface_advertised":
            rows = event.payload.get("tool_groups") or ()
            state.tool_group_surface = {str(row.get("name")): str(row.get("state")) for row in rows if isinstance(row, dict) and row.get("name")}
        elif event.type.value == "tool_group_loaded" and event.payload.get("group"):
            tools = state.tool_groups_loaded.setdefault(str(event.payload["group"]), set())
            tools.update(str(name) for name in event.payload.get("tools") or ())

    def _time_tool(self, state: SessionState, event: TurnEvent) -> None:
        """One row per tool call: name, when, how long, whether it failed. The waterfall of a run is these rows."""
        p = event.payload
        call_id = str(p.get("tool_call_id") or "")
        if not call_id:
            return
        if event.type is EventType.TOOL_USE_START:
            state.tool_starts[call_id] = (time.monotonic(), str(p.get("tool_name") or ""))
        elif event.type is EventType.TOOL_UNADVERTISED_CALL and p.get("executed") is False:
            # A blind call held back to give its group's rules first: the core says so before the
            # result. It never ran, so it is no call to time and no use of the tool; counted, the
            # retry that does run made every such call two in the month of use a group is judged by.
            state.tool_starts.pop(call_id, None)
        elif event.type is EventType.TOOL_RESULT:
            started = state.tool_starts.pop(call_id, None)
            if started is None:
                return
            duration_ms = int((time.monotonic() - started[0]) * 1000)
            row = (state.session.id, event.run_id, call_id, started[1], datetime.now(UTC).isoformat(), duration_ms, 0 if p.get("is_error") else 1)
            self._spawn_background(self.db.execute("INSERT INTO tool_calls(session_id, run_id, tool_call_id, name, at, duration_ms, ok) VALUES (?, ?, ?, ?, ?, ?, ?)", row), f"tool-timing:{call_id}")
            p["duration_ms"] = duration_ms

    def _redact_event(self, state: SessionState, event: TurnEvent) -> None:
        """Mask what the core's hook did not see before any store or front gets the event: a result's
        envelope, error text, tool arguments.

        Every result, a failed call's included, went through the ``post_tool_use`` hook before it
        entered the history, so the history needs nothing here. The event carries more than the
        text the hook was shown (a tool's UI payload and metadata), and that is masked here.
        """
        p = event.payload
        if event.type is EventType.TOOL_RESULT:
            cleaned = self._redact_tool_result_payload(p)
            p.clear()
            p.update(cleaned)
        elif event.type is EventType.TOOL_USE_INPUT_DELTA:
            # A credential or home path can be split across provider chunks, so a fragment cannot
            # be redacted reliably. The completed input is retained by TOOL_USE_STOP below.
            p["partial_input_json"] = ""
        elif event.type is EventType.TOOL_USE_STOP and isinstance(p.get("final_input"), dict):
            p["final_input"] = self.redactor.redact_tool_data(p["final_input"])
        elif event.type is EventType.ERROR and isinstance(p.get("message"), str):
            p["message"] = self.redactor.redact(p["message"])
            state.last_error_kind = str(p.get("kind") or state.last_error_kind)
            state.last_error_message = p["message"]
            state.last_error_permanent = p.get("retryable") is False
            logger.warning("run error in session %s (%s): %s", getattr(getattr(state, "session", None), "id", "?"), p.get("kind") or "-", p["message"][:500])
        elif event.type is EventType.COMPACTION_COMPLETED:
            state.last_compaction = dict(p)
        elif event.type is EventType.MESSAGE_STOP and RUN_OUTCOME_FIELD in p:
            # The terminal stop: the cause's detail is usually the provider's own error text, which
            # can echo a key, so it is masked before any store or front sees the event.
            cause = p.get(RUN_CAUSE_FIELD)
            if isinstance(cause, dict) and isinstance(cause.get("detail"), str):
                cause["detail"] = self.redactor.redact(cause["detail"])
            state.run_outcome = RunOutcome(p[RUN_OUTCOME_FIELD])
            state.run_cause = RunCause.model_validate(cause) if isinstance(cause, dict) else None
        elif event.type is EventType.STATE_CHANGED and p.get("reason") in RECOVERY_REASONS:
            detail = {k: v for k, v in p.items() if k not in ("from", "to", "reason")}
            logger.warning("run recovery in session %s: %s %s", getattr(getattr(state, "session", None), "id", "?"), p.get("reason"), self.redactor.redact(json.dumps(detail, ensure_ascii=False, default=str)[:400]))

    def _redact_tool_result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mask credentials and the operator's home prefix before tool output is stored.

        A home-relative path remains useful for locating an artifact without recording the
        machine's account name in durable events or their exports.
        """
        return self.redactor.redact_tool_data(payload)

    async def spend_with_subagents(self, session_id: str) -> float:
        """A session's priced spend plus that of every subagent it started: the cap is the leader's."""
        rows = await self.db.fetchall("SELECT id FROM sessions WHERE metadata LIKE ?", (f'%"subagent_of": "{session_id}"%',))
        ids = [session_id, *(str(r["id"]) for r in rows)]
        placeholders = ",".join("?" for _ in ids)
        row = await self.db.fetchone(f"SELECT sum(cost_usd) usd FROM usage_events WHERE session_id IN ({placeholders})", tuple(ids))
        return float(row["usd"] or 0.0) if row else 0.0

    async def spend(self, *, run_id: str | None = None, session_id: str | None = None, provider_id: str | None = None, since: str | None = None) -> tuple[float, int]:
        """Priced spend in USD (and the number of unpriced calls) over the given slice of usage events."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("run_id", run_id), ("session_id", session_id), ("provider_id", provider_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = await self.db.fetchone(f"SELECT sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events{where}", tuple(params))
        return (float(row["usd"] or 0.0), int(row["unmetered"] or 0)) if row else (0.0, 0)

    async def context_status(self, state: SessionState) -> dict[str, Any]:
        """What the model actually sees right now: the last prompt size against the window, and what the history is made of.

        The prompt size is the provider's own count from the latest call (the engine keeps it
        while a run is live; the usage log has it between runs), so it includes the system
        prompt and tool schemas, not only the history.
        """
        history = list(state.engine.history) if state.engine is not None else list(await self.sessions.list_messages(state.session.id, TENANT, limit=10_000))
        rewritten_at = max((str(m.metadata.get("daedalus.compaction", {}).get("at", "")) for m in history if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)), default="")
        tokens = int(state.engine.last_observed_prompt_tokens) if state.engine is not None else 0
        estimated = False
        if not tokens:
            # Only a call made against the history as it stands now says anything about it: a row
            # from before the last rewrite measured a history that no longer exists.
            row = await self.db.fetchone(
                "SELECT input_tokens FROM usage_events WHERE session_id = ? AND purpose = 'stream' AND seq > ? AND at > ? ORDER BY seq DESC LIMIT 1",
                (state.session.id, state.usage_floor_seq, rewritten_at),
            )
            tokens = int(row["input_tokens"] or 0) if row else state.observed_prompt_tokens
            estimated = row is None
            if not tokens and rewritten_at:
                tokens = history_tokens(history)
        reserved_response = 0
        try:
            _, preset = self.resolve_model(await self.live.load(state.session.id))
            window = int(state.context_window or preset.context_window or 0)
            reserved_response = int(preset.max_output_tokens or 0)
        except Exception:  # noqa: BLE001 — no usable model is reported elsewhere
            window = int(state.context_window or 0)
        summary_count = sum(1 for m in history if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
        # The newest summary the host wrote says when and why the history was last compacted; the
        # core's own summaries carry no such record, and then there is none to show rather than a guess.
        stamps = [m.metadata.get("daedalus.compaction") for m in history if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)]
        last_compaction = next(({"at": str(c.get("at") or ""), "reason": str(c.get("reason") or "")} for c in reversed(stamps) if isinstance(c, dict)), None)
        operator = sum(1 for m in history if m.role is MessageRole.user and m.metadata.get("daedalus.origin") not in (None, "core") and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
        manifests = await self.request_manifests.recent_for_session(state.session.id)
        breakdown = None
        prefix_changed: list[str] = []
        if manifests:
            latest = manifests[0]
            system_bytes = int(latest["system_prompt"]["byte_length"])
            message_bytes = int(latest["messages"]["byte_length"])
            tool_bytes = int(latest["tools"]["byte_length"])
            attachment_bytes = sum(
                len(block.model_dump_json().encode("utf-8"))
                for message in history
                for block in message.content_blocks
                if str(getattr(block, "type", "")).lower() in ("image", "file", "document", "audio", "video")
            )
            breakdown = {
                "instructions": (system_bytes + 3) // 4,
                "tools": (tool_bytes + 3) // 4,
                "conversation": (max(0, message_bytes - system_bytes - attachment_bytes) + 3) // 4,
                "attachments": (attachment_bytes + 3) // 4,
                "reserved_response": reserved_response,
                "source": "request_bytes_estimate",
            }
            if len(manifests) > 1:
                previous = manifests[1]
                for name, key in (("rules", "system_prompt"), ("toolset", "tools")):
                    if latest[key]["sha256"] != previous[key]["sha256"]:
                        prefix_changed.append(name)
                if latest["model"] != previous["model"]:
                    prefix_changed.append("model")
        recent_usage = await self.db.fetchone(
            "SELECT input_tokens, cache_read_tokens FROM usage_events WHERE session_id = ? AND purpose = 'stream' ORDER BY seq DESC LIMIT 1",
            (state.session.id,),
        )
        cache = None
        if recent_usage:
            prompt_tokens = int(recent_usage["input_tokens"] or 0)
            cache_tokens = int(recent_usage["cache_read_tokens"] or 0)
            cache = {
                "read_tokens": cache_tokens,
                "prompt_tokens": prompt_tokens,
                "hit_percent": round(100 * cache_tokens / prompt_tokens) if prompt_tokens else 0,
            }
        return {
            "tokens": tokens,
            "estimated": estimated,
            "window": window,
            "output_cap": reserved_response,
            "messages": len(history),
            "summaries": summary_count,
            "last_compaction": last_compaction,
            "operator_turns": operator,
            "breakdown": breakdown,
            "recent_cache": cache,
            "prefix_changed": prefix_changed,
        }

    def tools_off(self, state: SessionState) -> set[str]:
        """Tools the operator switched off for this session (``metadata["tools_off"]``); unknown names are ignored."""
        known = {t.name for t in self.tools.list_all()}
        return {str(n) for n in (state.metadata.get("tools_off") or ()) if str(n) in known}

    @staticmethod
    def is_companion(state: SessionState) -> bool:
        """Whether this is the companion: the operator's standing personal agent, by text and by voice."""
        return bool(state.metadata.get("companion"))

    def is_private(self, session_id: str) -> bool:
        """Whether a loaded session is a private chat (:mod:`daedalus.memory.privacy`). A tool call always
        comes from a loaded session, so the memory tools ask this without reading the row."""
        return privacy.is_private(self._states.get(session_id))

    @staticmethod
    def is_prompt_coach(state: SessionState) -> bool:
        """Whether this session is a prompt coach, beside the chat whose id it carries."""
        return bool(prompt_coach.coach_of(state.metadata))

    @staticmethod
    def is_btw(state: SessionState) -> bool:
        """Whether this session answers a side question about the chat whose id it carries."""
        return bool(btw_of(state.metadata))

    @classmethod
    def lives_in_window(cls, state: SessionState) -> bool:
        """Whether this session lives in a window over its chat — a prompt coach or a side question — and
        so reads where the chat works, writes nowhere and leaves the chat's directory to the chat."""
        return cls.is_prompt_coach(state) or cls.is_btw(state)

    @staticmethod
    def is_staff(state: SessionState) -> bool:
        """Whether this session is a staff member's, working on a task of its project's team."""
        return bool(state.metadata.get("staff_session_id"))

    def memory_placement(self, state: SessionState) -> memory_scope.MemoryPlacement:
        """Where this run's memory lives (:mod:`daedalus.memory.scope`): the chat's project when it is
        one of the operator's — not a lone chat's scratch project, not a system one — and whether
        the run is the operator's own conversation, by the same test the fronts use to decide who an
        answer is for. Read per run, since one session's runs change hands (the operator's turn, then
        a schedule's)."""
        project = state.project
        project_id = project.id if project is not None and not project.settings.ephemeral and not project.settings.system else ""
        operator = state.run_origin in OPERATOR_ORIGINS and not self.is_staff(state) and not any(state.metadata.get(key) for key in NOT_OPERATOR_FACING)
        return memory_scope.MemoryPlacement(project_id=project_id, operator=operator)

    def _stamp_memory(self, state: SessionState, engine: QueryEngine) -> None:
        """Tell the run's memory tools where its memory lives. Without it Remember fell to the core's
        default, a scope keyed by the session id, and no other chat ever recalled the note."""
        memory_scope.stamp(engine.run_state.run_metadata, self.memory_placement(state))

    @staticmethod
    def is_orchestrator(state: SessionState) -> bool:
        """Whether this session is a project's orchestrator (current or retired; a retired one's tools refuse)."""
        return bool(state.metadata.get("orchestrator_of") or state.metadata.get("orchestrator_retired_of"))

    @staticmethod
    def is_dispatcher(state: SessionState) -> bool:
        """Whether this session is the main orchestrator (current or retired; a retired one's tools refuse).

        Its tools come from a registry of their own (:attr:`dispatcher_tools`): they share names with
        other roles' tools, and a session is shown one registry or the other, never a mix."""
        return bool(state.metadata.get("dispatcher") or state.metadata.get("dispatcher_retired"))

    def registry_for(self, state: SessionState) -> ToolRegistry:
        return self.dispatcher_tools if self.is_dispatcher(state) else self.tools

    def _searches_for_tools(self, state: SessionState) -> bool:
        """Whether this session has ToolSearch, which is what makes the core hold MCP servers back."""
        registry = self.registry_for(state)
        return registry.get(TOOL_SEARCH_TOOL_NAME) is not None and policy_admits(self.tool_policy_for(state), TOOL_SEARCH_TOOL_NAME)

    def _local_blocked_tools_for(self, state: SessionState) -> set[str]:
        """Restrictions selected directly for this session, before its parent narrows them."""
        known = {t.name for t in self.tools.list_all()}
        blocked = blocked_for(self.mcp, self.mcp_enabled(state)) | self.tools_off(state)
        # The companion talks, owns goals and hands work over; it may not read, write or run anything
        # itself, and the tools that hand work over are its alone. Neither rule goes through a mode, so
        # editing one cannot give the companion a shell, nor give a working agent a second way to spawn one.
        blocked |= (known - set(COMPANION_TOOLS)) if self.is_companion(state) else (known & set(COMPANION_ONLY_TOOLS))
        # A prompt coach asks, reads its chat's project and saves the prompt; its saving tool, which writes
        # into another session's workspace, is nobody else's. For the companion's reason: no mode edit
        # can give the coach a shell or an ordinary agent that second way into a folder.
        blocked |= (known - set(PROMPT_COACH_TOOLS)) if self.is_prompt_coach(state) else (known & set(PROMPT_COACH_ONLY_TOOLS))
        # A side question reads and nothing else, whatever a mode says: it is answered beside the work and
        # must leave no mark on it.
        if self.is_btw(state):
            blocked |= known - set(BTW_TOOLS)
        # A staff member asks the orchestrator rather than the operator and works on its task rather
        # than starting agents, schedules or loops; its two reporting tools are its alone. Beside the
        # companion rule and for the same reason: no mode edit can hand either side the other's tools.
        blocked |= (known & set(STAFF_BLOCKED_TOOLS)) if self.is_staff(state) else (known & set(STAFF_ONLY_TOOLS))
        # A subagent reaches the operator through its leader, never past it. Its descendants inherit
        # this through the parent chain below, and a staff member's subagents through both rules.
        if state.metadata.get("subagent_of"):
            blocked |= known & set(SUBAGENT_BLOCKED_TOOLS)
        # An orchestrator runs a team and does none of the work: everything outside its allowlist is
        # off, and its control tools are nobody else's. Beside the two rules above, for their reason.
        blocked |= (known - set(ORCHESTRATOR_TOOLS)) if self.is_orchestrator(state) else (known & set(ORCHESTRATOR_ONLY_TOOLS))
        if not self.computer_offered(state):
            blocked |= known & capabilities.COMPUTER_TOOLS
        if not self.config.memory.search_chats:
            blocked |= known & set(CHAT_SEARCH_TOOLS)
        return blocked

    def _computer_agent(self) -> Any:
        """The process's agent for the desktop daemon, the one the computer tools use, or ``None``."""
        hooked = self.service_hooks.get("computer")
        if hooked is not None:
            return hooked
        run_dir = self.settings.computer_host_dir
        return computer_agent.agent_for(run_dir) if run_dir is not None else None

    async def _computer_lease(self, state: SessionState) -> tuple[Any, dict[str, Any] | None]:
        """The desktop agent and the lease this session holds now (``None`` when it holds none)."""
        if not self.capabilities.computer.configured or not computer_wire.endpoint_live(self.settings.computer_host_dir):
            return None, None
        agent = self._computer_agent()
        if agent is None:
            return None, None
        return agent, await computer_awareness.held_lease(agent.client, state.session.id)

    async def _computer_note(self, state: SessionState) -> str:
        """The turn context's line on the operator's computer, and the tools loaded to match it.

        While this session holds the lease, the line says what it may drive and until when, and the
        ``computer`` group is queued to load with the run about to start, so the agent never has to
        search for the tools the line names. A conversation made for the computer that holds no lease
        is told control is not granted now. Any other conversation without a lease gets no line."""
        if not self.capabilities.computer.configured:
            return ""
        try:
            agent, lease = await self._computer_lease(state)
            if lease is None:
                return computer_awareness.NOT_GRANTED if state.kind == "computer" else ""
            scope = await computer_awareness.describe(agent.client, state.session.id, lease)
        except Exception:  # noqa: BLE001 — a turn starts without the line rather than not at all
            logger.exception("could not read the computer lease for session %s", state.session.id)
            return ""
        sight = state.computer_sight
        if "computer" not in (state.metadata.get(tool_groups.CARRIED_KEY) or ()):
            await self.load_tool_group_now(state.session.id, "computer")
        return computer_awareness.granted_line(scope, lease, loaded=True, sees=sight is None or bool(sight[1]))

    async def computer_granted(self, session_id: str) -> None:
        """The operator granted this conversation control of their computer through the app.

        The mark sticks: a plain chat becomes a computer conversation. The tools are queued for the
        next run, and a run already going is steered with the news, the way an allowed call is: its
        turn context was written before the grant and says nothing of it."""
        state = await self.get_state(session_id)
        if state is None:
            return
        await self.set_session_kind(session_id, "computer", only_from="chat")
        await self.load_tool_group_now(session_id, "computer")
        if not state.running:
            return
        try:
            agent, lease = await self._computer_lease(state)
            if lease is None:
                return
            scope = await computer_awareness.describe(agent.client, session_id, lease)
            await self.submit(session_id, computer_awareness.grant_steer(scope, lease), steer=True, expected_running=True, as_answer=False, origin="events")
        except Exception:  # noqa: BLE001 — the grant stands; only the note about it failed
            logger.exception("could not tell session %s about a computer grant", session_id)

    def computer_offered(self, state: SessionState) -> bool:
        """Whether this session is offered the computer tools now: a native installation with a desktop
        daemon that is up, and a run built on a model that takes images.

        A model without images is not offered them at all rather than offered tools that answer with
        pictures it cannot see; describing the screen through the vision model and pointing through a
        grounding model is the later route for such a model. The daemon's endpoint is looked at once
        every few seconds: the policy is asked many times a run, and a daemon coming or going within
        that moment is caught by the tool's own first call."""
        if not self.capabilities.computer.configured:
            return False
        sight = state.computer_sight
        if not sight or not sight[1]:
            return False
        now = time.monotonic()
        checked, live = self._computer_live
        if now - checked > COMPUTER_LIVE_SECONDS or checked == 0.0:
            live = computer_wire.endpoint_live(self.settings.computer_host_dir)
            self._computer_live = (now, live)
        return live

    def blocked_tools_for(self, state: SessionState) -> set[str]:
        """Everything this session may not call under its own and every loaded parent's current rules.

        A child stores the restrictions present when it was created so a missing parent cannot widen it.
        Following the live parent chain additionally makes later revocations effective. A corrupt cycle is
        denied completely: no ancestry that cannot be established is allowed to grant capabilities.
        """
        known = {t.name for t in self.tools.list_all()}
        blocked: set[str] = set()
        current: SessionState | None = state
        seen: set[str] = set()
        while current is not None:
            current_id = current.session.id
            if current_id in seen:
                return known
            seen.add(current_id)
            blocked |= self._local_blocked_tools_for(current)
            parent_id = str(current.metadata.get("subagent_of") or "")
            current = self._states.get(parent_id) if parent_id else None
        # The subagent-only tools are judged on this session alone, after the chain and not inside
        # ``_local_blocked_tools_for``: the leader at the top of every chain has no leader, so a block
        # there would be inherited down the chain and take the tool from the very subagents it is for.
        if not state.metadata.get("subagent_of"):
            blocked |= known & set(SUBAGENT_ONLY_TOOLS)
        return blocked

    async def _load_capability_ancestors(self, state: SessionState) -> None:
        """Load a persisted parent chain before a child run resolves its effective policy."""
        current = state
        seen = {state.session.id}
        while parent_id := str(current.metadata.get("subagent_of") or ""):
            if parent_id in seen:
                return
            seen.add(parent_id)
            parent = await self.get_state(parent_id)
            if parent is None:
                return
            current = parent

    def tool_policy_for(self, state: SessionState) -> ToolVisibilityPolicy:
        """The one effective tool policy used for catalogue advertisement and dispatch admission.

        Only refusals: what a session may call is its registry minus ``blocked``. Nothing is pinned,
        because a pin is what the core never holds back — every tool used to be pinned here, which kept
        the whole of a connected MCP server on the surface however large it was. The role allowlists
        (voice, orchestrator) arrive as refusals of everything outside them.
        """
        if self.is_dispatcher(state):
            # Its own registry is its whole allowlist; the operator may still switch a tool off.
            own = {t.name for t in self.dispatcher_tools.list_all()}
            off = {str(n) for n in (state.metadata.get("tools_off") or ()) if str(n) in own}
            return ToolVisibilityPolicy(blocked=off)
        return ToolVisibilityPolicy(blocked=self.blocked_tools_for(state))

    def advertised_tools_limit(self, rungs: Sequence[tuple[Any, str]]) -> int:
        """The lowest cap on tools per request among the run's rungs, 0 when none has one.

        The lowest and not the primary's: a fallback to a model that refuses more than 128 tools
        would fail on its first request with a surface sized for the primary.
        """
        limits = [max_advertised_tools_for(self.config.providers.get(provider.endpoint.id), model) for provider, model in rungs]
        return min((limit for limit in limits if limit > 0), default=0)

    def tool_rules_key(self) -> bytes:
        """The key every session's rules mark is derived from, kept beside the operator's sealing key.

        The mark sits in the tool catalogue at the head of the cached prompt. Drawn anew in every process,
        it changed at each restart and rewrote the system prompt of every session whose catalogue carries
        a group's rules — a full cache miss per session per deploy — and left the rules already in the
        history under a mark the catalogue no longer named. A key that cannot be read or made falls back to
        the process key: a cost, not a failure.
        """
        if self._tool_rules_key is None:
            try:
                self._tool_rules_key = operator_secrets.load_key(self.settings.secrets_dir / TOOL_RULES_KEY_FILE)
            except (OSError, operator_secrets.SecretError):
                logger.warning("tool rules key unavailable; marks change at every restart", exc_info=True)
                self._tool_rules_key = b""
        return self._tool_rules_key

    def discovered_tools_for(self, state: SessionState) -> tuple[str | dict[str, str], ...]:
        """The tools loaded in the session that it may still call, as the next run is to load them.

        Handed to the next run so it starts with them loaded rather than searching again, in the order
        they were loaded, so the tail of the tool list a prefix cache keys on comes back the same. A tool
        of a group carried whole goes as ``{"name", "group"}``: the core then counts the group as the one
        entry it was, and puts it back where it was loaded — as a separate seed the groups came after
        every name, and a run that loaded a group and then one more tool started the next one with the
        tool list reordered. The session's current policy decides, not the one they were loaded under: a
        tool switched off or refused to the role since then is not carried back.
        """
        registry = self.registry_for(state)
        declared = registry.tool_groups()
        whole = set(self.loaded_groups_for(state))
        out: list[str | dict[str, str]] = []
        for name in self._carried_names(state):
            tool = registry.get(name)
            group = tool_group_of(tool, declared) if tool is not None else ""
            out.append({"name": name, "group": group} if group in whole else name)
        return tuple(out)

    def _carried_names(self, state: SessionState) -> tuple[str, ...]:
        registry = self.registry_for(state)
        policy = self.tool_policy_for(state)
        names = [str(name) for name in (state.metadata.get("discovered_tools") or ()) if isinstance(name, str)]
        return tuple(name for name in names if registry.get(name) is not None and policy_admits(policy, name))

    def _admitted_groups(self, state: SessionState) -> dict[str, list[str]]:
        """Each group, the host's and a connected server's alike, with the tools of it this session may call."""
        registry = self.registry_for(state)
        policy = self.tool_policy_for(state)
        declared = registry.tool_groups()
        out: dict[str, list[str]] = {}
        for tool in registry.list_all():
            group = tool_group_of(tool, declared)
            if group and policy_admits(policy, tool.name):
                out.setdefault(group, []).append(tool.name)
        return out

    def loaded_groups_for(self, state: SessionState) -> tuple[str, ...]:
        """The groups the next run starts with loaded whole: what the session loaded so far, then what the
        operator asked for. Whole, because the core counts a group as one of the loaded tools it keeps: passed as
        names, the browser's twelve and the schedules' six pushed each other out. A group the session may
        no longer call any tool of is dropped here, as a tool is; so is one now always on the surface,
        which loading would only spend one of those entries on."""
        admitted = self._admitted_groups(state)
        loads = self.tool_group_loads_for(state)
        carried = [str(g) for g in (state.metadata.get(tool_groups.CARRIED_KEY) or ()) if isinstance(g, str)]
        queued = [str(g) for g in (state.metadata.get(tool_groups.LOAD_NOW_KEY) or ()) if str(g) in TOOL_GROUPS]
        return tuple(g for g in dict.fromkeys(carried + queued) if admitted.get(g) and loads.get(g) != "eager")

    async def _clear_queued_group_loads(self, state: SessionState) -> None:
        if tool_groups.LOAD_NOW_KEY not in state.metadata:
            return
        for meta in (state.metadata, state.session.metadata):
            meta.pop(tool_groups.LOAD_NOW_KEY, None)
        await self.sessions.update_metadata(state.session.id, state.session.metadata)

    def _keep_loaded_tools(self, state: SessionState, engine: QueryEngine) -> bool:
        """Remember what the finished run had loaded, and what it last said of its groups; ``True`` when
        that changed what is stored.

        Everything loaded is carried, in the order it was loaded, and the groups loaded whole are named
        as such. Only the tools a run called used to be carried, so an unused search match or a group
        loaded and then left alone for one run dropped out of the next one's tool list — and the tool
        list is the head of the cached prompt, so the session wrote its whole context into the cache
        again (a long one, four hundred thousand tokens) to save one definition that a cache read costs
        a tenth of the input price. What bounds the carried set now is the core's cap on loaded tools,
        applied at the start of a run.
        """
        manager = engine.context_manager
        names = list(manager.discovered_tool_names())
        groups = list(manager.loaded_tool_group_names())
        wanted: dict[str, Any] = {"discovered_tools": names, tool_groups.CARRIED_KEY: groups}
        if state.tool_group_surface:
            wanted[tool_groups.SURFACE_KEY] = dict(state.tool_group_surface)
        changed = False
        for key, value in wanted.items():
            if value == (state.metadata.get(key) or type(value)()):
                continue
            changed = True
            for meta in (state.metadata, state.session.metadata):
                if value:
                    meta[key] = value
                else:
                    meta.pop(key, None)
        return changed

    def _apply_tool_visibility(self, state: SessionState) -> None:
        if state.engine is None:
            return
        state.engine.config = replace(state.engine.config, tool_visibility_policy=self.tool_policy_for(state))

    def _apply_tool_visibility_all(self) -> None:
        """Refresh every loaded engine because registry and parent changes can narrow other sessions too."""
        for state in self._states.values():
            self._apply_tool_visibility(state)

    async def set_tools_off(self, session_id: str, names: list[str]) -> list[str]:
        """Switch tools off (or back on, by omission) for a session; applies from the next model call."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        known = {t.name for t in self.tools.list_all()}
        chosen = sorted({str(n) for n in names if str(n) in known})
        for meta in (state.metadata, state.session.metadata):
            if chosen:
                meta["tools_off"] = chosen
            else:
                meta.pop("tools_off", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        self._apply_tool_visibility_all()
        return chosen

    def tool_group_loads_for(self, state: SessionState) -> dict[str, str]:
        """How each host group reaches this session's next run: the settings, its model, then its own choice.

        The companion has every group eager: it has no ToolSearch to load one with, and what it is
        allowed of each group (its schedules, the calendar's reading half) is a handful of tools the
        policy has already cut every group down to. A prompt coach and a side question likewise.
        """
        if self.is_companion(state) or self.lives_in_window(state):
            return {name: "eager" for name in tool_groups.configured_loads(self.config)}
        return tool_groups.resolved_loads(self.config, state.metadata, on_demand=state.on_demand_groups)

    async def refresh_on_demand_groups(self, state: SessionState) -> None:
        """Read the session's model's switch for on-demand groups, as its next run would.

        The panel is read between runs, after a restart or a change of model, when the flag the last
        engine build left behind describes another model or none.
        """
        try:
            rungs, preset = self.resolve_model(await self.live.load(state.session.id))
        except RuntimeError:
            # No model configured (NoModelConfigured is a RuntimeError, and a test that reloads the
            # config module makes a second class of it), so no switch to read; the panel still answers.
            return
        if rungs:
            state.on_demand_groups = on_demand_tool_groups_for(preset, rungs[0][1])

    @staticmethod
    def _stored_surface(state: SessionState) -> dict[str, str]:
        raw = state.metadata.get(tool_groups.SURFACE_KEY)
        if not isinstance(raw, dict):
            return {}
        return {str(name): str(value) for name, value in raw.items() if value in ("advertised", "deferred", "loaded")}

    def _group_members(self) -> dict[str, list[str]]:
        return tool_groups.members(self.tools.list_all())

    async def tool_group_catalogue(self) -> dict[str, Any]:
        """Every host group for the settings page: what it is for, its tools and their cost, its default
        and current mode, and how many sessions and runs of the last month called any of its tools."""
        grouped = self._group_members()
        usage = await self._group_usage.read(self.db, tool_groups.group_of(grouped))
        loads = tool_groups.configured_loads(self.config)
        groups = []
        for name, spec in TOOL_GROUPS.items():
            names = grouped[name]
            if not names:
                continue  # not on this installation (no browser, self-development off): nothing to choose
            used = usage.groups[name]
            groups.append({
                "name": name,
                "description": spec.description,
                "tools": names,
                "tokens": tool_groups.schema_tokens(t for t in self.tools.list_all() if t.name in names),
                "default": spec.load,
                "load": loads[name],
                "usage": {"sessions": used.sessions, "runs": used.runs, "calls": used.calls},
            })
        return {"days": usage.days, "sessions": usage.sessions, "runs": usage.runs, "groups": groups}

    def tool_group_states(self, state: SessionState) -> list[dict[str, Any]]:
        """Each host group as this session has it: its mode and where that came from, and its state.

        ``off`` — the session may call none of its tools (switched off, or the role or mode refuses them);
        ``loaded`` — loaded in this session (by a search, a call, the operator, or carried from an earlier
        run), ``pending`` when that happens at the next run, with ``loaded`` counting the tools of it
        that are (a search may load one tool of twelve); ``deferred`` — only its catalogue line is
        in the prompt; ``advertised`` — its tools are in every request. Without ToolSearch the core
        holds nothing back, so a lazy group is advertised then.
        """
        registry = self.registry_for(state)
        policy = self.tool_policy_for(state)
        loads = self.tool_group_loads_for(state)
        searches = self._searches_for_tools(state)
        carried = set(self._carried_names(state))
        carried_groups = {str(g) for g in (state.metadata.get(tool_groups.CARRIED_KEY) or ())}
        queued = {str(g) for g in (state.metadata.get(tool_groups.LOAD_NOW_KEY) or ())}
        surface = state.tool_group_surface or self._stored_surface(state)
        out = []
        for name, names in self._group_members().items():
            if not names:
                continue
            admitted = [n for n in names if registry.get(n) is not None and policy_admits(policy, n)]
            load = loads[name]
            pending = False
            running_tools = state.tool_groups_loaded.get(name, set()) if state.running else set()
            loaded_tools = set(admitted) & (carried | running_tools)
            if not admitted:
                current = "off"
            elif (state.running and name in state.tool_groups_loaded) or name in carried_groups or loaded_tools:
                current = "loaded"
                if name in carried_groups:
                    loaded_tools = set(admitted)
            elif name in queued:
                # "Load now" loads the group whole.
                current, pending, loaded_tools = "loaded", True, set(admitted)
            elif not searches or load == "eager":
                current = "advertised"
            elif load == "lazy":
                current = "deferred"
            else:
                # "auto" is the core's call on the window, and only a run makes it: the last run's word,
                # kept across a restart, or — before any run — the mode itself rather than a guess.
                current = surface.get(name) or "undecided"
                current = "advertised" if current == "loaded" else current
            out.append({
                "name": name,
                "description": TOOL_GROUPS[name].description,
                "tools": len(admitted),
                "load": load,
                "source": tool_groups.load_source(name, self.config, state.metadata, on_demand=state.on_demand_groups),
                "state": current,
                "pending": pending,
                "loaded": len(loaded_tools) if current == "loaded" else 0,
            })
        return out

    async def set_session_tool_group(self, session_id: str, group: str, load: str | None) -> list[dict[str, Any]]:
        """This session's own mode for a group, over the settings; ``None`` follows the settings again."""
        if group not in TOOL_GROUPS:
            raise KeyError(group)
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        chosen = tool_groups.session_loads(state.metadata)
        if load is None:
            chosen.pop(group, None)
        else:
            chosen[group] = load
        for meta in (state.metadata, state.session.metadata):
            if chosen:
                meta[tool_groups.SESSION_LOADS_KEY] = dict(chosen)
            else:
                meta.pop(tool_groups.SESSION_LOADS_KEY, None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self.refresh_on_demand_groups(state)
        return self.tool_group_states(state)

    async def load_tool_group_now(self, session_id: str, group: str) -> list[dict[str, Any]]:
        """Have the session's next run start with a group loaded, as if a search had loaded it.

        Queued in the metadata rather than written into ``discovered_tools``: the run in progress
        rewrites that list when it ends, and would drop what was put there while it ran. The next
        run's engine takes the queue and empties it, and from then on the group is carried whole for the
        rest of the session.
        """
        if group not in TOOL_GROUPS:
            raise KeyError(group)
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        queued = [str(g) for g in (state.metadata.get(tool_groups.LOAD_NOW_KEY) or ()) if str(g) in TOOL_GROUPS]
        if group not in queued:
            queued.append(group)
            for meta in (state.metadata, state.session.metadata):
                meta[tool_groups.LOAD_NOW_KEY] = list(queued)
            await self.sessions.update_metadata(session_id, state.session.metadata)
        await self.refresh_on_demand_groups(state)
        return self.tool_group_states(state)

    def notes_for(self, state: SessionState, *, window: int = 0) -> str:
        """What the system prompt says about this session beyond the environment: the brief it was created
        with, and what an imported session carries from the program it came from (loaded beforehand into
        ``state.standing`` by :func:`standing.load`)."""
        parts = [state.extra_notes.strip()] if state.extra_notes.strip() else []
        if state.services is not None and isinstance(state.services.exec_backend, HostExecBackend):
            # The sandbox line of the environment section describes this process's commands, not these.
            parts.append(
                f"- You work on the operator's own machine (the host), in {state.services.workspace_dir}: Exec, Verify, "
                "background jobs and the file tools run there through the host terminal, with no sandbox around them, "
                "so take the care you would take on someone's own computer. ServiceStart is not available there; "
                "start a server with Exec(background=true)."
            )
        available = set(self.providers.available())
        models = [pid for pid, preset in self.config.presets.items() if preset.provider in available and preset.model]
        if models:
            parts.append("- Models SubAgent accepts (preset ids): " + ", ".join(models))
        loop_note = str(state.metadata.get("loop_note") or "").strip()
        if loop_note:
            parts.append(loop_note)
        brief = str(state.metadata.get("brief") or "").strip()
        if brief:
            origin = state.metadata.get("spawned_by") or state.metadata.get("subagent_of")
            parts.append("- Your brief" + (f" (from session {origin})" if origin else "") + ", the standing instructions for this session:\n" + brief)
        record = standing.record_of(state.session.metadata)
        if record is not None and state.standing[1]:
            imported = state.session.metadata.get("imported") or {}
            carried = standing.render(state.standing[1], source=harness_name(str(record.get("source") or "")), window=window, workspace=str(imported.get("cwd") or ""))
            if carried:
                parts.append(carried)
        return "\n".join(parts)

    async def workspace_notes(self, state: SessionState) -> str:
        """What the workspace and the board say about the work in progress: read at every run start, so a run
        begins from the plan of record rather than from memory of it.

        ``AGENTS.md`` and ``CLAUDE.md`` in the workspace root, and the index of ``.agents/memory``, are the
        agent's own project memory; the open board tasks of this session are its plan. Both are volatile
        text: they change between runs, so they travel in the turn context at the end of the run's opening
        message, never in the system prompt, whose bytes must not change between runs if the provider is
        to serve them from cache.
        """
        parts: list[str] = []
        # Its room is a share of the window, not a fixed few thousand characters: a 20 KB CLAUDE.md
        # cut at 6,000 lost the rules an imported session had been working under. Both files are read
        # when both exist and differ — a project kept for Claude Code and for Codex has one of each.
        room = max(WORKSPACE_NOTES_CHARS, int(await self.session_window(state) * CHARS_PER_TOKEN * WORKSPACE_NOTES_WINDOW_SHARE))
        seen: list[str] = []
        for name in ("AGENTS.md", "CLAUDE.md"):
            text = await self._workspace_note(state, name)
            if not text or text in seen:
                continue
            seen.append(text)
            if len(text) > room:
                text = text[:room] + f"\n[… {name} continues; Read it for the rest]"
            room = max(0, room - len(text))
            parts.append(f"- {name} in the workspace (your project memory; keep it current):\n{self.redactor.redact(text)}")
        # The index of the project's memory notes, where the operator keeps what every agent working in
        # the folder should know; the notes themselves are read when the work touches them. Without it
        # an agent asked "you remember the stand?" had nothing to look in.
        index = await self._workspace_note(state, MEMORY_INDEX)
        if index and room:
            if len(index) > room:
                index = index[:room] + f"\n[… {MEMORY_INDEX} continues; Read it for the rest]"
            parts.append(f"- {MEMORY_INDEX} in the workspace (the project's memory notes; Read the ones the work touches before you plan, and keep them current):\n{self.redactor.redact(index)}")
        try:
            rows = await self.db.fetchall("SELECT id, title, status, priority FROM board_tasks WHERE session_id = ? AND status NOT IN ('done', 'cancelled') ORDER BY priority, updated_at DESC LIMIT 8", (state.session.id,))
        except Exception:  # noqa: BLE001 — the board is optional
            rows = []
        if rows:
            parts.append("- Your open board tasks (BoardGet for details; update them as you go):\n" + "\n".join(f"  - [{r['status']}] {r['id']}: {r['title']}" for r in rows))
        return ("\n" + "\n".join(parts)) if parts else ""

    async def _workspace_note(self, state: SessionState, name: str) -> str | None:
        """The text of a notes file in the workspace root, or ``None`` when it is not there to read.

        A session working in a host folder keeps its notes there, not in the directory it runs from,
        so its file is read through its tools' own filesystem."""
        services = state.services
        try:
            if services is not None and isinstance(services.exec_backend, HostExecBackend):
                return (await services.fs.read_text(services.workspace_dir / name)).strip()
            path = state.workspace / name
            if not path.is_file():
                return None
            return (await asyncio.to_thread(path.read_text, "utf-8", "replace")).strip()
        except (OSError, UnicodeDecodeError):
            return None

    async def set_brief(self, session_id: str, brief: str) -> str:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        brief = brief.strip()[:BRIEF_MAX_CHARS]
        for meta in (state.metadata, state.session.metadata):
            if brief:
                meta["brief"] = brief
            else:
                meta.pop("brief", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return brief

    @staticmethod
    def session_cap(state: SessionState) -> float | None:
        """This session's own spend cap (all of its runs), or None when it follows the global limits only."""
        raw = state.metadata.get("usd_cap")
        return float(raw) if raw is not None else None

    # -- tool policy ---------------------------------------------------------------

    def policy(self, *, base_dir: Path | str = "") -> Policy:
        cfg = self.config.policy
        rules = [Rule(id=r.id or f"config.{i}", tool=r.tool or "*", action=r.action, note=r.note, pattern=r.pattern, source="config") for i, r in enumerate(cfg.rules, 1)]
        return Policy(
            protected_paths=self.protected_paths(),
            egress_allow=cfg.egress_allow, rules=rules, workspace_roots=(self.settings.workspaces_dir,),
            operator_checkouts=(self.settings.bot_repo_dir, self.settings.core_repo_dir),
            selfdev_mode=self.capabilities.selfdev.mode,
            native=self.settings.native,
            # The home folder is the operator's, and only a native installation is inside it.
            home_dir=Path.home() if self.settings.native else "",
            project_roots=self.projects.roots,
            worktrees_root=self.settings.worktrees_dir,
            sealed_paths=(*self.settings.sealed_paths, *terminal_endpoint.sealed_state_dirs(self.settings)),
            sealed_everywhere=self.settings.sealed_everywhere,
            sealed_ports=self._sealed_ports(),
            # Where a relative path is resolved from, so that `../../daedalus-secrets/keyproxy.env`
            # is read as the file it names rather than as a word with no slash at the front.
            base_dir=base_dir,
            mcp_approvals=self.mcp.approvals(),
        )

    def sealed_ports(self) -> tuple[int, ...]:
        """The installation's own loopback doors, for the browser daemon's network wall natively."""
        return self._sealed_ports()

    def _sealed_ports(self) -> tuple[int, ...]:
        """The installation's own doors on the loopback interface: the app's API, and the launcher's
        action page where a launcher is holding this installation.

        The file that carries the launcher's token is sealed, but sealing a path is matched against
        the words of a command and a path a command builds for itself is not matched by it. The port
        is not built by anybody: whatever reaches the launcher reaches it there. The agent asks the
        app for a restart or an install the way the operator does, and the app is what holds the keys
        to both — so a request made to either door directly is a way round that, and is refused.
        """
        ports = [self.settings.api_port]
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is not None:
            ports.append(launcher.port)
        # The terminal daemons' doors, where one listens on this loopback interface: a daemon on a
        # TCP endpoint (Windows) and its hook listener (natively). Whatever reaches either holds a
        # shell or speaks for a running CLI, so it is asked through the app as the rest is.
        ports.extend(terminal_endpoint.sealed_ports(self.settings))
        return tuple(ports)

    def protected_paths(self) -> tuple[Path, ...]:
        """What no session may read or write: the installation itself.

        It is the sealed set — the secrets, the whole state directory, the runtime, the launcher and
        the environment file the launcher owns — plus the two paths that are the installation's
        without being part of it: the governance file the operator writes the rules in, and the
        launcher's directory in the image.

        The sealed set is not copied here, it is asked for, because the two layers that judge a path
        must not be able to disagree about it: the shell rules ask ``Settings.sealed_paths`` and the
        file tools, the browser and ``SessionServices.is_protected`` ask this list. Enumerating them
        twice is how the environment file came to be refused to a command and handed to ``Read``.
        """
        return (self.governance_path, Path("/opt/launcher"), *self.settings.sealed_paths)

    def policy_gate(self, session_id: str, run_id: str) -> Any:
        """The policy bound to one session: grants are the session's, the egress log names the run."""

        def decide(tool: str, arguments: dict[str, Any]) -> Decision:
            state = self._states.get(session_id)
            if state is not None and tool in self.tool_policy_for(state).blocked:
                return Decision("deny", "the tool is outside this session's effective capability set", "session.capabilities")
            if state is not None and tool == "AskUser" and (asked := self._asks_about_pending(state, arguments)):
                return asked
            policy = self.policy(base_dir=state.workspace if state is not None else self.workspace_for(session_id))
            now = time.time()
            grants = {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now} if state is not None else set()
            similar = list(state.metadata.get("policy_similar") or []) if state is not None else []
            decision = policy.evaluate(tool, arguments, grants=grants, similar=similar)
            # The operator's action rules, on top: they turn an allowed call into a question, a
            # handoff or a call a review stands before, and never loosen what the policy said.
            decision = self.action_rules.apply(state, tool, arguments, decision, grants, similar)
            if decision.key and decision.action == "allow" and state is not None:
                self._consume_grant(state, decision.key)
            elif decision.key and decision.action == "ask" and state is not None:
                self.record_question(state, decision, tool, arguments)
            if decision.hosts:
                self._record_egress(session_id, run_id, tool, decision.hosts, decision.action)
            if decision.action != "allow":
                logger.warning("policy %s %s for %s in session %s: %s", decision.action, decision.rule, tool, session_id, decision.reason)
            return decision

        def ask_hint() -> str | None:
            # A staff member's request goes to its orchestrator, not to the operator, and is answered
            # by a message; retrying before then is the same request again.
            state = self._states.get(session_id)
            return prompts.STAFF_POLICY_HINT if state is not None and self.is_staff(state) else None

        def review_due(run: str, tool: str, arguments: dict[str, Any], ruled: Any) -> None:
            self.action_rules.due(session_id, run, tool, arguments, ruled)

        return PolicyAdapter(decide, ask_hint=ask_hint, review_due=review_due)

    @staticmethod
    def _asks_about_pending(state: SessionState, arguments: dict[str, Any]) -> Decision | None:
        """A refusal of an AskUser that asks the operator about a request already waiting on its card.

        The refusals used to tell the agent to ask with AskUser, and an agent may still do it by habit.
        The operator then had two questions about one thing, and the answer to the agent's ("Allow")
        granted nothing: only the card's Allow does. So the question is refused with a pointer to the
        card, rather than linking its free-text answer to a grant, which would let the agent word a
        question that an unrelated answer approves. A question that quotes an open key, or tells the
        operator to type /allow, is about one."""
        pending = list((state.metadata.get("policy_pending") or {}).keys())
        if not pending:
            return None
        said = json.dumps(arguments, ensure_ascii=False).lower()
        quoted = [key for key in pending if key in said]
        if not quoted and "/allow" not in said:
            return None
        key = quoted[0] if quoted else pending[-1]
        return Decision("deny", "the request is already on the operator's card", "approval.duplicate", told=(
            f"AskUser was not sent: the request with approval key {key} is already in front of the operator as a card "
            "with Allow and Deny, in the app, the Chrome side panel and Telegram, and only that card grants it. "
            "Do not ask about it yourself: end your turn, or continue with other work. A message tells you when they allow it."
        ))

    def record_question(self, state: SessionState, decision: Decision, tool: str, arguments: dict[str, Any]) -> None:
        """An ``ask`` the operator is to answer: kept open on the session and announced once.

        The policy's questions and the operator's rules' — a level of ``ask``, a review that could
        not decide — are one kind of request: the same key, grant, card and buttons."""
        # The refusal's preimage, so an operator granting the key from chat sees what they approve.
        pending = dict(state.metadata.get("policy_pending") or {})
        fresh = decision.key not in pending
        pending[decision.key] = {"tool": tool, "text": self.redactor.redact(canonical(tool, arguments))[:300], "at": datetime.now(UTC).isoformat()}
        # The family "Allow similar" would grant, worked out now: the arguments are gone by the
        # time the operator answers, and the pending entry keeps only a redacted preview.
        family = similar_grant(tool, arguments, decision.rule)
        if family is not None:
            pending[decision.key]["similar"] = {**family, "label": self.redactor.redact(family["label"])[:200]}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = dict(list(pending.items())[-20:])
        if fresh:
            # Once per request: the agent retrying the same refused call is the same request.
            self._publish_soon(state, "permission.pending", self._permission_payload(state, decision, pending[decision.key]))

    def browser_gate(self, session_id: str, ask: Any) -> tuple[bool, str]:
        """A sensitive browser action of this session, asked about the way the policy asks.

        The policy judges a call from its arguments before it runs; whether a click buys something is
        known only once the browser has looked at the element, inside the tool. So the tool asks here,
        and the answer follows the policy's own rules: a grant for the key lets that one action through
        once, otherwise the request is recorded and announced once and the agent is refused with the
        key. An action on a page goes to the operator — for a staff member too, whose other requests go
        to its orchestrator: money, messages and a person's accounts are the operator's to approve. The
        network wall's question about an address (a router's page, a device on the LAN) is a staff
        member's request like any other, routed by the project's autonomy: the orchestrator allows it
        when the operator already said so for the work, or hands it to them.
        """
        state = self._states.get(session_id)
        decision = ask.decision
        if state is None:
            return False, "this session is not loaded; the action was not taken"
        now = time.time()
        grants = {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now}
        if decision.key in grants:
            self._consume_grant(state, decision.key)
            return True, ""
        pending = dict(state.metadata.get("policy_pending") or {})
        fresh = decision.key not in pending
        staff = self.is_staff(state)
        to_orchestrator = staff and ask.network
        pending[decision.key] = {"tool": ask.tool, "text": self.redactor.redact(ask.said())[:500], "at": datetime.now(UTC).isoformat()}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = dict(list(pending.items())[-20:])
        if fresh:
            payload = self._permission_payload(state, decision, pending[decision.key])
            # Answered in the app, never from a lock screen: whoever holds the phone should not be
            # the whole check on a purchase. The picture of the element rides along for the card.
            payload.update({"risk": "elevated", "quick": False, "browser": {"group_id": ask.group, "kinds": list(ask.kinds), "origin": ask.origin, "element": ask.element, "name": ask.name, "thumbnail": ask.thumbnail}})
            if not to_orchestrator:
                # Without it the team routes the request by the project's autonomy, to the orchestrator.
                payload["routed_to"] = "operator"
            self._publish_soon(state, "permission.pending", payload)
        if to_orchestrator:
            return False, f"needs approval: {decision.reason}. Approval key: {decision.key}. " + prompts.STAFF_BROWSER_NETWORK_HINT
        head = f"needs the operator's approval: {decision.reason} (rule {decision.rule}). Approval key: {decision.key}. "
        if staff:
            return False, head + prompts.STAFF_BROWSER_HINT
        return False, head + prompts.OPERATOR_APPROVAL_HINT

    def ask_operator(self, session_id: str, *, key: str, tool: str, text: str, on_answer: str, card: dict[str, Any] | None = None) -> bool:
        """Put a request on the session that only the operator answers and the host carries out.

        It is announced like a policy question — the same card, the same Allow and Deny, from the app,
        Telegram or the staff request list — but always to the operator, a staff member's too, and
        their answer runs ``answer_hooks[on_answer]`` instead of granting a call. ``False`` when the
        session is not loaded and nothing was asked."""
        state = self._states.get(session_id)
        if state is None:
            return False
        pending = dict(state.metadata.get("policy_pending") or {})
        fresh = key not in pending
        pending[key] = {"tool": tool, "text": self.redactor.redact(text)[:800], "at": datetime.now(UTC).isoformat(), "on_answer": on_answer}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = dict(list(pending.items())[-20:])
        self._spawn_background(self.sessions.update_metadata(session_id, state.session.metadata), f"operator-ask:{session_id}")
        if fresh:
            payload = self._permission_payload(state, Decision(ASK, text, on_answer, key=key), pending[key])
            payload.update({"risk": "elevated", "quick": False, "routed_to": "operator", **(card or {})})
            self._publish_soon(state, "permission.pending", payload)
        return True

    def _carried_out(self, state: SessionState, key: str, via: str) -> Callable[[str, dict[str, Any], bool], Awaitable[None]] | None:
        """The host's own action for an open :meth:`ask_operator` request, or ``None`` for an ordinary
        policy question. Refused to an orchestrator: what the operator alone may decide, a team's
        autonomy does not stretch to, whatever list the request was seen on."""
        refused = (state.metadata.get("policy_pending") or {}).get(key) or {}
        hook = self.answer_hooks.get(str(refused.get("on_answer") or ""))
        if hook is not None and via == "orchestrator":
            raise ValueError("only the operator answers this request")
        return hook

    def computer_gate(self, session_id: str, ask: Any) -> tuple[bool, str]:
        """A sensitive action on the operator's desktop, asked about as :meth:`browser_gate` asks.

        The same rules: a grant for the key lets that one action through once, otherwise the request is
        recorded and announced once and the agent is refused with the key. Always the operator's to
        answer, a staff member's included — it is their screen, their mail and their files — and always in
        the app, never from a lock screen or a notification's button."""
        state = self._states.get(session_id)
        decision = ask.decision
        if state is None:
            return False, "this session is not loaded; the action was not taken"
        now = time.time()
        grants = {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now}
        if decision.key in grants:
            self._consume_grant(state, decision.key)
            return True, ""
        pending = dict(state.metadata.get("policy_pending") or {})
        fresh = decision.key not in pending
        pending[decision.key] = {"tool": ask.tool, "text": self.redactor.redact(ask.said())[:500], "at": datetime.now(UTC).isoformat()}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = dict(list(pending.items())[-20:])
        if fresh:
            payload = self._permission_payload(state, decision, pending[decision.key])
            # No thumbnail yet: the browser's is a route the app fetches the element's picture from, and
            # the desktop has no such route; the card names the app and the window instead.
            payload.update({
                "risk": "elevated", "quick": False, "routed_to": "operator",
                "computer": {"kinds": list(ask.kinds), "action": ask.action, "app": ask.app, "window": ask.window, "element": ask.element, "thumbnail": ask.thumbnail},
            })
            self._publish_soon(state, "permission.pending", payload)
        head = f"needs the operator's approval: {decision.reason} (rule {decision.rule}). Approval key: {decision.key}. "
        return False, head + "Ask the operator with AskUser, quoting the key and what the action does; once they grant it (/allow <key>, or the Mini App), the same action passes once."

    async def flush_background(self) -> None:
        """Wait for the fire-and-forget writes (timing rows, egress rows, grant updates) to land."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def _refresh_prices_daily(self) -> None:
        """Gateways that publish no prices get them from models.dev: once at start, then once a day."""
        from daedalus.providers.modelsdev import REFRESH_SECONDS  # Lazy: the provider package imports the host's config

        while True:
            try:
                priced = await self.providers.refresh_prices(self.db)
                if priced:
                    logger.warning("model prices refreshed from models.dev: %s models", priced)
            except Exception:  # noqa: BLE001
                logger.warning("model price refresh failed", exc_info=True)
            await asyncio.sleep(REFRESH_SECONDS)

    def _spawn_background(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("%s failed: %s", name, t.exception())

        task.add_done_callback(_done)
        return task

    def _consume_grant(self, state: SessionState, key: str) -> None:
        for meta in (state.metadata, state.session.metadata):
            grants = {k: v for k, v in (meta.get("policy_grants") or {}).items() if k != key}
            meta["policy_grants"] = grants
        self._spawn_background(self.sessions.update_metadata(state.session.id, state.session.metadata), f"grant-consume:{state.session.id}")

    def _record_egress(self, session_id: str, run_id: str, tool: str, hosts: list[str], action: str) -> None:
        async def _write() -> None:
            now = datetime.now(UTC).isoformat()
            await self.db.executemany("INSERT INTO egress_log(at, session_id, run_id, tool, host, action) VALUES (?, ?, ?, ?, ?, ?)", [(now, session_id, run_id, tool, h, action) for h in hosts])

        self._spawn_background(_write(), f"egress:{run_id}")

    def _permission_payload(self, state: SessionState, decision: Decision, refused: dict[str, Any]) -> dict[str, Any]:
        # A rule about the machine itself (the operator's home, the installation) is answered in the
        # app, never from a lock screen: whoever holds the phone should not be the whole check.
        elevated = decision.rule.startswith("host.")
        return {
            "request_id": decision.key,
            "request_ref": f"policy:{state.session.id}:{decision.key}",
            "kind": "policy",
            "title": state.session.title,
            "tool": str(refused.get("tool") or ""),
            "text": str(refused.get("text") or ""),
            "risk": "elevated" if elevated else "routine",
            "quick": not elevated,
            "telegram": self._telegram_delivers(state),
        }

    @staticmethod
    def _approval_key(key: str) -> str:
        key = key.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", key):
            raise ValueError("an approval key is 12 hex characters, as shown in the refusal")
        return key

    def _drop_pending(self, state: SessionState, key: str) -> dict[str, Any] | None:
        """Take the key off the open refusals, returning what it was; the request is answered."""
        refused = (state.metadata.get("policy_pending") or {}).get(key)
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = {k: v for k, v in (meta.get("policy_pending") or {}).items() if k != key}
        return refused

    async def grant(self, session_id: str, key: str, *, via: str) -> dict[str, Any]:
        """The operator lets one refused call through: the key from the refusal, valid once, for a limited time.

        Returns what was approved (the refusal's tool and text when the host saw it) and the open grants.
        ``via`` is where it was approved, for the ``permission.resolved`` event.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        key = self._approval_key(key)
        carry_out = self._carried_out(state, key, via)
        # Answered, so off the open list: the same call refused again after the grant is spent is a
        # new request, and is announced as one.
        approves = self._drop_pending(state, key)
        if carry_out is not None:
            # The host does what was asked, and no grant is written: a grant would let the agent's
            # next call through, and the request was never the agent's to carry out.
            await self.sessions.update_metadata(session_id, state.session.metadata)
            await self._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"policy:{session_id}:{key}", "decision": "allow", "via": via})
            await carry_out(session_id, approves or {}, True)
            open_grants = [k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > time.time()]
            return {"key": key, "approves": approves, "grants": open_grants, "expires_in_minutes": 0}
        until = time.time() + GRANT_TTL_SECONDS
        for meta in (state.metadata, state.session.metadata):
            grants = {k: v for k, v in (meta.get("policy_grants") or {}).items() if float(v) > time.time()}
            grants[key] = until
            meta["policy_grants"] = dict(list(grants.items())[-20:])
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"policy:{session_id}:{key}", "decision": "allow", "via": via})
        if via != "orchestrator":  # the orchestrator's grant tells the member itself, with its basis
            await self._resume_after_grant(session_id, [approves] if approves else [])
        return {"key": key, "approves": approves, "grants": list(state.metadata["policy_grants"]), "expires_in_minutes": GRANT_TTL_SECONDS // 60}

    async def _resume_after_grant(self, session_id: str, approved: Sequence[dict[str, Any]]) -> None:
        """Tell the agent the calls it was refused may go through now, which starts its turn if it ended.

        A refusal ends the agent's turn with "ask the operator", and the operator's grant only opened
        the way: the agent sat idle until the operator also wrote "go on", after pressing Allow in the
        app or typing /allow — they took the button for the whole answer, which it should be.
        A grant for a call this host never saw refused (a key taken on trust) has nothing to resume."""
        lines = [f"- {item.get('tool')}: {item.get('text')}" for item in approved if item and item.get("tool")]
        if not lines:
            return
        text = ("The operator allowed what was refused; repeat the call now — it passes once:\n" + "\n".join(lines))
        try:
            await self.submit(session_id, text, steer=True, as_answer=False, origin="events")
        except Exception:  # noqa: BLE001 — the grant stands; only the nudge to use it failed
            logger.exception("could not tell session %s about a grant", session_id)

    def similar_of(self, session_id: str, key: str) -> dict[str, Any] | None:
        """What "Allow similar" would grant for an open request, for the app to say before it asks."""
        state = self._states.get(session_id)
        if state is None:
            raise KeyError(session_id)
        refused = (state.metadata.get("policy_pending") or {}).get(self._approval_key(key)) or {}
        family = refused.get("similar")
        return dict(family) if isinstance(family, dict) else None

    async def grant_similar(self, session_id: str, key: str, *, via: str) -> dict[str, Any]:
        """The operator allows calls like a refused one for the rest of the session.

        The family was fixed when the call was refused (:func:`similar_grant`): the same tool, the same
        rule that asked, and the command prefix, folder or host. It stands with the session, unspent, the
        way a command-line member keeps an "Always"; and every request still open that it covers is
        answered with it, so twenty asks of the same kind take one answer, not twenty.
        """
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        key = self._approval_key(key)
        family = self.similar_of(session_id, key)
        if family is None:
            raise ValueError("this request has no narrower family of calls to allow; allow it once instead")
        standing = [g for g in (state.metadata.get("policy_similar") or []) if g != family]
        standing.append(family)
        resolved = [k for k, refused in (state.metadata.get("policy_pending") or {}).items()
                    if isinstance(refused.get("similar"), dict)
                    and similar_covers(family, str(refused.get("tool") or ""), str(refused["similar"].get("rule") or ""), refused["similar"])]
        approves = [self._drop_pending(state, k) for k in resolved]
        for meta in (state.metadata, state.session.metadata):
            meta["policy_similar"] = standing[-20:]
        await self.sessions.update_metadata(session_id, state.session.metadata)
        for k in resolved:
            await self._publish(state, "permission.resolved", {"request_id": k, "request_ref": f"policy:{session_id}:{k}", "decision": "allow", "via": via})
        if via != "orchestrator":
            await self._resume_after_grant(session_id, [item for item in approves if item])
        return {"key": key, "similar": family, "resolved": resolved, "approves": approves,
                "standing": list(state.metadata["policy_similar"])}

    async def refuse(self, session_id: str, key: str, *, via: str) -> dict[str, Any]:
        """The operator leaves a refused call refused. Nothing changes for the agent, which was already
        told no; the request stops being open, and everyone showing it hears that it was answered."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        key = self._approval_key(key)
        carry_out = self._carried_out(state, key, via)
        refused = self._drop_pending(state, key)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"policy:{session_id}:{key}", "decision": "deny", "via": via})
        if carry_out is not None:
            # Unlike a refused call, whose agent was told no already, this agent was told to wait.
            await carry_out(session_id, refused or {}, False)
        return {"key": key, "refuses": refused}

    async def egress(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT at, run_id, tool, host, action FROM egress_log WHERE session_id = ? ORDER BY seq DESC LIMIT ?", (session_id, limit))
        return [dict(r) for r in rows]

    async def tool_timing(self, session_id: str) -> list[dict[str, Any]]:
        """Per tool: calls, errors, total and mean time — where a session's wall-clock goes."""
        rows = await self.db.fetchall(
            "SELECT name, count(*) calls, sum(ok = 0) errors, sum(duration_ms) total_ms, avg(duration_ms) mean_ms, max(duration_ms) max_ms FROM tool_calls WHERE session_id = ? GROUP BY name ORDER BY total_ms DESC", (session_id,)
        )
        return [dict(r) for r in rows]

    async def set_session_cap(self, session_id: str, cap: float | None) -> float | None:
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if cap is not None and cap < 0:
            raise ValueError("a session cap cannot be negative")
        for meta in (state.metadata, state.session.metadata):
            if cap is None:
                meta.pop("usd_cap", None)
            else:
                meta["usd_cap"] = float(cap)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        return cap

    async def cap_breach(self, state: SessionState, provider_id: str | None,
                         model: str | None = None) -> tuple[str, str] | None:
        """``(kind, note)`` when the session, its provider or everything together has spent its cap.

        Three caps stack above the per-run one: the session's own (session settings), the
        provider's total across every session (``limits.usd_total_per_provider``) and the grand
        total (``limits.usd_total``); the last two count from ``limits.total_since``.
        """
        if self.provider_costs_nothing(provider_id, model):
            # Dollar caps prevent another charge. A llama.cpp call is recorded at exactly zero and a
            # subscription or free call adds nothing a cap counts, so refusing either would turn a
            # priced provider's balance into an unrelated outage.
            return None
        limits = self.config.limits
        since = limits.total_since or None
        session_cap = self.session_cap(state)
        if session_cap is not None:
            spent = await self.spend_with_subagents(state.session.id)
            if spent >= session_cap:
                return "session_cap", f"session cap reached: ${spent:.2f} spent of ${session_cap:.2f} (subagents included); raise it in the session settings to continue"
        provider_cap = float(limits.usd_total_per_provider.get(provider_id or "", 0) or 0)
        if provider_id and provider_cap > 0:
            spent, _ = await self.spend(provider_id=provider_id, since=since)
            if spent >= provider_cap:
                return "provider_cap", f"total cap for provider {provider_id!r} reached: ${spent:.2f} spent of ${provider_cap:.2f} (limits.usd_total_per_provider); raise it or reset the counter in Settings → Limits"
        if limits.usd_total > 0:
            spent, _ = await self.spend(since=since)
            if spent >= limits.usd_total:
                return "total_cap", f"total spend cap reached: ${spent:.2f} spent of ${limits.usd_total:.2f} (limits.usd_total); raise it or reset the counter in Settings → Limits"
        return None

    async def _enforce_caps(self, state: SessionState, run_id: str) -> None:
        """Stop a run that crossed a spend cap: its own, the session's, its provider's or the total."""
        if state.engine is None or not state.running or run_id in self._capped_runs:
            return
        bounds = self.run_bounds(state)
        role_cap = bounds.usd_per_run if bounds is not None else None
        run_cap = role_cap if role_cap is not None else self.config.limits.usd_per_run
        spent, unmetered = await self.spend(run_id=run_id)
        note: str | None = None
        kind = "run_cap"
        setting: SettingRef | None = None
        limits = self.config.limits
        if limits.max_run_tokens > 0:
            row = await self.db.fetchone("SELECT sum(input_tokens + output_tokens) t FROM usage_events WHERE run_id = ?", (run_id,))
            used = int(row["t"] or 0) if row else 0
            if used >= limits.max_run_tokens:
                kind, note = "run_tokens", f"⏱ token cap reached: {used:,} tokens in this run of {limits.max_run_tokens:,} (limits.max_run_tokens); stopping this run. Send a message to continue in a new run."
        if note is None and limits.max_run_minutes > 0 and state.run_active_since:
            minutes = (time.monotonic() - state.run_active_since) / 60
            if minutes >= limits.max_run_minutes:
                kind, note = "run_minutes", f"⏱ time cap reached: {minutes:.0f} min of active work in this run of {limits.max_run_minutes} (limits.max_run_minutes); stopping this run. Send a message to continue in a new run."
        if note is not None:
            pass
        elif (run_cap > 0 or role_cap is not None) and spent >= run_cap:
            source = bounds.setting if bounds is not None and role_cap is not None else "limits.usd_per_run"
            note = f"💸 per-run cap reached: ${spent:.2f} spent of ${run_cap:.2f} ({source}); stopping this run. Send a message to continue in a new run."
            setting = RUN_SPEND_CAP if role_cap is None else None
            if unmetered:
                note += f" {unmetered} call(s) had no known price and are not counted."
        else:
            last = await self.db.fetchone("SELECT provider_id,model FROM usage_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,))
            breach = await self.cap_breach(state, last["provider_id"] if last else None, last["model"] if last else None)
            if breach is not None:
                kind, note = breach[0], f"💸 {breach[1]}; stopping this run."
                setting = {"total_cap": SPEND_CAP, "provider_cap": provider_spend_cap(str(last["provider_id"] if last else ""))}.get(kind)
        if note is None:
            return
        self._capped_runs.add(run_id)
        logger.warning("run %s stopped (%s): %s", run_id, kind, note)
        state.engine.stop()
        # The cap's row travels with the message, so the chat's error line can offer the way to it.
        payload: dict[str, Any] = {"message": note, "kind": kind, **({"setting": setting.as_dict()} if setting is not None else {})}
        await self._dispatch_event(state, TurnEvent(type=EventType.ERROR, run_id=run_id, payload=payload))

    # -- recovery -------------------------------------------------------------------

    async def stop_for_update(self, targets: list[dict[str, str]]) -> dict[str, list[str]]:
        """Stop exact native runs only when no external tool outcome is still unconfirmed.

        The engine's task exiting is insufficient for a detached command or an interrupted
        side effect. Those remain visible for their existing runtime controls to reconcile.
        """
        async with self.db.transaction() as conn:
            try:
                await assert_admission_open_in(conn)
            except UpdateDrainActive:
                pass
            else:
                raise RuntimeError("close update admission before stopping native runs")
        deadline = asyncio.get_running_loop().time() + self.config.ops.shutdown_grace_seconds
        stopping = []
        unconfirmed = []
        tasks = []
        for target in targets:
            run_id = target['id']
            state = self._states.get(target['session_id'])
            row = await self.db.fetchone('SELECT session_id,status FROM runs WHERE id = ?', (run_id,))
            if row is None or row['session_id'] != target['session_id']:
                unconfirmed.append(run_id)
                continue
            if row['status'] in ('completed', 'error', 'cancelled'):
                stopping.append(run_id)
                continue
            if state is None or state.run_id != run_id or state.engine is None:
                unconfirmed.append(run_id)
                continue
            snapshot = state.engine.snapshot()
            if snapshot.get('background_task_ids') or any(
                    intent.get('state') == 'DISPATCHED' or intent.get('outcome') == 'unknown'
                    for intent in snapshot.get('open_intents', [])):
                unconfirmed.append(run_id)
                continue
            if state.task is not None and not state.task.done():
                state.engine.stop()
                state.task.cancel()
                tasks.append(state.task)
                stopping.append(run_id)
            else:
                # A saved question has no live task to cancel. Ending it must still publish
                # the normal settled event and callbacks so ownership observes a real exit.
                state.engine.stop()
                state.pending = None
                await self.db.execute('DELETE FROM pending_questions WHERE session_id = ? AND run_id = ?',
                                      (target['session_id'], run_id))
                await self.runs.update_status(run_id, TENANT, RunStatus.cancelled)
                await self._announce_settled(state, run_id, 'cancelled', housekeeping=True)
                state.settled.clear()
                state.housekeeping = asyncio.create_task(self._settle_run(state, run_id, 'cancelled'),
                                                        name=f"settle:{run_id}")
                state.housekeeping.add_done_callback(_log_task_failure)
                tasks.append(state.housekeeping)
                stopping.append(run_id)
        if tasks:
            await asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        writes = [task for state in self._states.values() if state.run_id in stopping
                  for task in (state.housekeeping, *state.persist_tasks) if task is not None and not task.done()]
        if writes:
            await asyncio.wait(writes, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        confirmed = []
        for run_id in stopping:
            row = await self.db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,))
            state = next((s for s in self._states.values() if s.run_id == run_id), None)
            if (row is not None and row['status'] in ('completed', 'error', 'cancelled')
                    and not (state and (state.running or self._settling(state)))):
                confirmed.append(run_id)
            else:
                unconfirmed.append(run_id)
        return {'stopped': sorted(confirmed), 'unconfirmed': sorted(set(unconfirmed))}

    async def park_for_update(self, run_ids: set[str]) -> dict[str, list[str]]:
        """Park exact native runs without closing providers or calling them completed.

        A cancellation that has not finished saving remains unconfirmed; a timeout cannot
        authorize replacement of the process or deletion of its checkpoint.
        """
        async with self.db.transaction() as conn:
            try:
                await assert_admission_open_in(conn)
            except UpdateDrainActive:
                pass
            else:
                raise RuntimeError("close update admission before parking native runs")
        tasks = []
        for state in tuple(self._states.values()):
            if state.run_id not in run_ids:
                continue
            if state.task is not None and not state.task.done():
                self._update_parked_run_ids.add(state.run_id)
                state.task.cancel()
                tasks.append(state.task)
        if tasks:
            await asyncio.wait(tasks, timeout=self.config.ops.shutdown_grace_seconds)
        checkpoints = {entry["run_id"] for entry in await self.events.unfinished_snapshots()}
        parked = []
        unconfirmed = []
        for run_id in sorted(run_ids):
            state = next((s for s in self._states.values() if s.run_id == run_id), None)
            pending_writes = bool(state and any(not task.done() for task in state.persist_tasks))
            active = bool(state and (state.running or self._settling(state)))
            if not active and not pending_writes and run_id in checkpoints:
                parked.append(run_id)
                self._update_parked_run_ids.discard(run_id)
            else:
                unconfirmed.append(run_id)
        return {"parked": parked, "unconfirmed": unconfirmed}

    async def resume_unfinished(self, allowed_run_ids: set[str] | None = None) -> list[str]:
        """Continue runs that were mid-flight when the process last stopped."""
        # Recovery must not turn a closed update fence into a failed resume that deletes
        # the only saved engine state. The update decision owns reopening admission.
        async with self.db.transaction() as conn:
            try:
                await assert_admission_open_in(conn)
            except UpdateDrainActive:
                self.recovering = False
                return []
        resumed: list[str] = []
        parked: list[str] = []
        exceeded = self.budget_exceeded()
        try:
            seen: set[str] = set()
            for entry in await self.events.unfinished_snapshots():  # newest first
                if allowed_run_ids is not None and entry['run_id'] not in allowed_run_ids:
                    continue
                session_id = str(entry["session_id"] or entry["snapshot"].get("session_id") or "")
                if session_id in seen:
                    # An older snapshot of a session whose newer run is resumed: a leftover, not a second run.
                    await self.events.delete_snapshot(entry["run_id"])
                    continue
                seen.add(session_id)
                if exceeded:
                    state = await self.get_state(session_id)
                    try:
                        rungs, _preset = self.resolve_model(await self.live.load(session_id))
                        free = bool(rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id, rungs[0][1]))
                    except Exception:  # noqa: BLE001 — the normal resume path records the unusable model
                        free = False
                    if state is not None and not free:
                        parked.append(entry["run_id"])
                        continue
                await self._resume_one(entry, resumed)
                if entry["run_id"] not in resumed:
                    # A restore can legitimately wait for an answer, or fail because the
                    # selected model is unavailable. Either way its saved state is still
                    # the only copy of the work and cannot be treated as a stale run.
                    parked.append(entry["run_id"])
            if parked:
                logger.warning("budget exceeded; %d paid-provider unfinished run(s) stay parked until the cap is lifted", len(parked))
            await self._settle_stale_runs(resumed, parked)
        finally:
            self.recovering = False
        return resumed

    async def _settle_stale_runs(self, resumed: list[str], parked: list[str] | None = None) -> None:
        """A run row still 'running' that nobody drives is a leftover of the previous process: closed as cancelled."""
        active = set(resumed) | set(parked or ()) | self.running_run_ids()
        rows = await self.db.fetchall("SELECT id FROM runs WHERE status = ?", (RunStatus.running.value,))
        self.stale_runs = []
        for row in rows:
            if row["id"] in active:
                continue
            await self.runs.update_status(row["id"], TENANT, RunStatus.cancelled)
            await self.events.delete_snapshot(row["id"])
            self.stale_runs.append(row["id"])
            logger.warning("run %s was left running by the previous process and could not be resumed; closed as cancelled", row["id"])

    async def _resume_one(self, entry: dict[str, Any], resumed: list[str]) -> None:
        session_id = entry["session_id"] or entry["snapshot"].get("session_id")
        state = await self.get_state(session_id)
        if state is None:
            logger.warning("run %s could not restore its session; checkpoint retained", entry["run_id"])
            return
        if state.running:
            return  # something in this process already drives the session; its own run owns the snapshot
        try:
            engine = await self._build_engine(state, entry["run_id"])
            await engine.resume_from_snapshot(entry["snapshot"])
            prepared_input = entry["snapshot"].get("provider_resume_input")
            message = Message.model_validate(prepared_input) if prepared_input is not None else None
            if message is not None:
                receipt = await self.live.receipt(
                    session_id, str(message.metadata.get("daedalus.client_message_id") or ""),
                )
                if (receipt is None or receipt["status"] != "consumed" or receipt["run_id"] != entry["run_id"]
                        or not message.metadata.get("daedalus.provider_resume")):
                    raise RuntimeError("the prepared provider input has no matching durable placement")
        except Exception:  # noqa: BLE001
            logger.exception("could not resume run %s; checkpoint retained", entry["run_id"])
            return
        state.engine = engine
        state.run_id = entry["run_id"]
        state.run_active_since = time.monotonic()  # the time cap counts from the resume, not from the run's creation
        state.history_keys = [self.sessions.transcript_key(m) for m in engine.history]
        if engine.state is LoopState.AWAITING:
            row = await self.db.fetchone("SELECT * FROM pending_questions WHERE session_id = ?", (session_id,))
            if row is None:
                logger.warning("run %s has no saved question; checkpoint retained", entry["run_id"])
                return
            state.pending = PendingQuestion(
                session_id=session_id,
                run_id=row["run_id"],
                tool_call_id=row["tool_call_id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
            )
            # Again after a restart: a subscriber that keeps its own list of open requests dedupes by
            # the reference, and one that started with this process has never heard of it.
            await self._publish(state, "ask.pending", self._ask_payload(state, state.pending))
            for callback in self._pending_restored:
                try:
                    await callback(session_id, state.pending)
                except Exception:  # noqa: BLE001
                    logger.exception("pending-restored callback failed")
            return
        if not engine.history and message is None:
            logger.warning("run %s has no replayable history; checkpoint retained", entry["run_id"])
            return
        if engine.state is not LoopState.RUNNING:
            engine.transition_to(LoopState.RUNNING)
        async with self.idle_work:
            state.task = asyncio.create_task(
                self._drive(state, engine, message, message is None), name=f"resume:{entry['run_id']}",
            )
        state.task.add_done_callback(_log_task_failure)
        resumed.append(entry["run_id"])


VERBATIM_TAIL_MESSAGES = 3
SUMMARY_RETRY_PAUSES = (5.0, 20.0)
"""The pauses before a failed summariser call is asked again, one per retry."""
COMPACTION_RETRY_GROWTH = 0.1
"""After an automatic compaction that failed or freed almost nothing, the prompt must grow by this share of
the window before the next one is tried: about twenty turns of an orchestrator on a 490k window."""
COMPACTION_RETRY_SECONDS = 1800.0
"""A failed compaction is also retried after this long without that growth, doubling with each failure in a
row: a provider that stalled for a while should not leave a long session uncompacted for good."""
COMPACTION_RETRY_MAX_SECONDS = 6 * 3600.0
COMPACTION_MIN_FREED = 0.1
"""A compaction that took less than this share off the prompt counts as having freed nothing."""
OPERATOR_QUOTES_MAX_CHARS = 60_000
"""Room for the operator's words a compaction quotes by code. Each passage is quoted whole; past this
room the oldest are named as left out, and a single passage longer than all of it is cut with a
marker, both pointing at the archived turns. They were once clipped silently at 400 characters."""
OPERATOR_QUOTES_WINDOW_SHARE = 0.1
"""The share of the model's window each of the two quote sections may take, and never more than a
quarter of the trigger's ratio: together they stay under half of what starts a compaction."""
OPERATOR_PASSAGES_KEPT = 400
"""Passages a summary carries forward in its metadata for the next compaction to quote again."""
ARCHIVE_POINTER = "HistoryExpand over the archived turns named at the end of this summary returns them verbatim"


def operator_words_metadata(origin: str, text: str, passages: Sequence[str] | None = None) -> dict[str, Any]:
    """The core's mark of whose words a user turn carries, for a message this host delivers.

    The core takes any plain user turn to be the operator's, and would quote a wake-up's event
    list or a reminder as the operator's instruction when it compacts. So a message from the
    operator names their text, a runtime note names the operator's passages it relays (an answer
    inside a batch of events), and one that relays none says so. ``passages`` given overrides the
    origin: a note the host writes on the operator's behalf names only the words they gave."""
    if passages is None:
        words = [text.strip()] if origin == "operator" and text.strip() else []
    else:
        words = [passage.strip() for passage in passages if passage and passage.strip()]
    return {OPERATOR_WORDS_METADATA_KEY: words or False}


def _queued_attachments(attachments: Sequence[Attachment]) -> list[dict[str, Any]]:
    """The files of a message that waits in a queue, as the item carries them: each kept file's name
    and type, and the blob an image was kept as, under the core's ``blob_ref`` so the core places the image
    with the words. A file the store refused was never kept and is not listed."""
    out: list[dict[str, Any]] = []
    for item in attachments:
        if not item.stored_name:
            continue
        entry: dict[str, Any] = {"name": item.stored_name, "mime_type": item.mime_type}
        if item.image_ref:
            entry["blob_ref"] = item.image_ref
        out.append(entry)
    return out


def _queued_caption(text: str, attachments: Sequence[Attachment]) -> dict[str, Any]:
    """What a queued card shows for a message with files: the words as typed, not the list of paths
    the model is given. The upload's stand-in sentence for "no words" is shown as nothing."""
    if not attachments:
        return {}
    return {"caption": "" if text in {"File attached.", "Files attached."} else text.strip()}


def _queued_image_refs(items: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for item in items:
        for entry in item.get("attachments") or []:
            if isinstance(entry, dict) and entry.get("blob_ref"):
                refs.append((str(entry["blob_ref"]), str(entry.get("mime_type") or "image/png")))
    return refs


def _image_refs_metadata(image_refs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The mark that makes the wire send a user message's images with it, in the core's own key and
    shape, so the core prices them and anything built on the core reads them (see ``providers.wire``)."""
    if not image_refs:
        return {}
    return {IMAGE_REFS_METADATA_KEY: [ImageRefBlock(blob_ref=ref, mime_type=mime).model_dump(mode="json") for ref, mime in image_refs]}


# What the app draws a message from besides its text. The queued row has it from ``submit``; the copy the
# core places, or the one that opens the next turn, is written later from the queue item alone, so the item
# has to carry it. It used to be dropped there: a message with a secret attached showed the host's note
# about the secret as the operator's words and no chip.
_DISPLAY_KEYS = ("daedalus.secrets", "daedalus.reply_to", "daedalus.autopilot")


def _queued_display(words: dict[str, Any]) -> dict[str, Any]:
    display = {key: words[key] for key in _DISPLAY_KEYS if words.get(key)}
    return {"display": display} if display else {}


def _merged_words(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The mark for one message made of several queued items, each with its own origin, and what the
    app draws it with: every item's secrets, and the reply of the first item, the one whose quote
    opens the joined text."""
    words: list[str] = []
    secrets: list[dict[str, Any]] = []
    for item in items:
        marked = operator_words_metadata(str(item.get("origin") or "operator"), str(item.get("text") or ""), item.get("operator_words"))
        words += marked[OPERATOR_WORDS_METADATA_KEY] or []
        secrets += [handed for handed in (item.get("display") or {}).get("daedalus.secrets") or [] if handed not in secrets]
    merged: dict[str, Any] = {OPERATOR_WORDS_METADATA_KEY: words or False}
    if secrets:
        merged["daedalus.secrets"] = secrets
    reply = (items[0].get("display") or {}).get("daedalus.reply_to") if items else None
    if reply:
        merged["daedalus.reply_to"] = reply
    # Autopilot's mark only when every item is autopilot's: a message joined with the operator's own
    # words is theirs, and labelling it would claim the operator's text was generated.
    marks = [(item.get("display") or {}).get("daedalus.autopilot") for item in items]
    if items and all(marks):
        merged["daedalus.autopilot"] = marks[-1]
    return merged


def operator_passages(history: Sequence[Message]) -> list[tuple[Message, str]]:
    """Every passage of the operator's in ``history``, oldest first, each whole.

    What a message marks decides (see :func:`operator_words_metadata`); an unmarked user turn
    counts when the operator sent it. A tool result marked as the operator's reply — the answer to
    a question the model asked — counts as well, and so do the passages an earlier summary carried
    forward, so a second compaction quotes what the first one did. An answer that arrived inside a
    wake-up's events was once left to the summariser, which paraphrased it."""
    found: list[tuple[Message, str]] = []
    for m in history:
        if is_ledger(m):
            continue
        marked = m.metadata.get(OPERATOR_WORDS_METADATA_KEY)
        if isinstance(marked, list):
            found += [(m, passage.strip()) for passage in marked if isinstance(passage, str) and passage.strip()]
        elif m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY):
            continue
        elif marked is True or (marked is None and m.role is MessageRole.user and m.metadata.get("daedalus.origin") == "operator"):
            text = prompts.without_turn_context("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))).strip()
            if text:
                found.append((m, text))
        for block in m.content_blocks:
            if isinstance(block, ToolResultBlock) and (block.metadata or {}).get(OPERATOR_WORDS_METADATA_KEY) is True and (block.content or "").strip():
                found.append((m, block.content.strip()))
    unique: dict[str, tuple[Message, str]] = {}
    for m, text in found:
        unique.pop(text, None)  # a passage said again counts where it was said last
        unique[text] = (m, text)
    return list(unique.values())


def _quoted(text: str, room: int) -> str:
    """A passage as a list item, its later lines indented; cut only past ``room``, and then with a marker."""
    if len(text) > room:
        text = f"{text[:room].rstrip()} [truncated {len(text) - room} chars — full text: {ARCHIVE_POINTER}]"
    return "- " + "\n  ".join(text.splitlines())


def operator_quotes(history: Sequence[Message], *, skip_last: int = VERBATIM_TAIL_MESSAGES, room: int = OPERATOR_QUOTES_MAX_CHARS) -> str:
    """Every older passage of the operator's, quoted whole by code: constraints survive compaction unchanged.

    The summariser is asked to quote rules too, but a model paraphrases under pressure; this
    section is written without it. The most recent ``skip_last`` passages are left to
    :func:`verbatim_tail`. Past ``room`` the oldest are named as left out, never clipped.
    """
    passages = [text for _, text in operator_passages(history)]
    older = passages[:-skip_last] if skip_last else passages
    lines: list[str] = []
    left = room
    omitted = 0
    for text in reversed(older):
        if len(text) > left and lines:
            omitted += 1
            continue
        lines.append(_quoted(text, max(left, 0)))
        left -= len(text)
    if not lines:
        return ""
    head = [f"- [{omitted} older passages are left out here for room — {ARCHIVE_POINTER}]"] if omitted else []
    return "\n\n## Operator said (verbatim, oldest first)\n" + "\n".join(head + list(reversed(lines)))


IDENTIFIER_RE = re.compile(
    r"(?<![\w/])(?:/[\w.@~-]+(?:/[\w.@~-]+)+|https?://[^\s'\"<>)]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{12,40}\b|\b[A-Za-z][\w-]*\.(?:py|md|toml|json|txt|yaml|yml|ts|tsx|sh|sql)\b|\bPR\s?#?\d{1,6}\b|#\d{1,6}\b|\b\d{4,5}\b)"
)
IDENTIFIER_INDEX_MAX = 150
"""Identifiers pinned by code below the summary: paths, URLs, ids, file names, PR numbers, ports."""


def identifier_index(history: Sequence[Message], *, limit: int = IDENTIFIER_INDEX_MAX) -> str:
    """Every identifier the operator or the agent's tool calls named, in first-seen order, written by code.

    The summariser keeps some of these; this list keeps all of them, because a path or an
    id that is gone from context is gone from the work. Tool results are not scanned: they
    carry directory listings and logs, which would bury the ones that matter.
    """
    seen: dict[str, None] = {}
    for m in history:
        if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY):
            continue
        for b in m.content_blocks:
            if isinstance(b, TextBlock) and (m.role is MessageRole.assistant or m.metadata.get("daedalus.origin") == "operator"):
                source = prompts.without_turn_context(b.text)
            elif isinstance(b, ToolUseBlock):
                source = b.arguments_json or ""
            else:
                continue
            for found in IDENTIFIER_RE.findall(source):
                token = found.strip().rstrip(".,;:")
                if token.isdigit() and (len(token) < 4 or 1900 <= int(token) <= 2100):
                    continue  # bare years and short numbers are noise
                if len(token) > 200 or token in seen:
                    continue
                seen[token] = None
    if not seen:
        return ""
    items = list(seen)[-limit:]
    return "\n\n## Identifiers seen (extracted)\n" + "\n".join(f"- {t}" for t in items)


CHARS_PER_TOKEN = 4
"""The usual approximation, used only to say how big a history is after the host rewrote it —
until the next call comes back with the provider's own count, which is what everything else reads."""


def history_tokens(messages: Sequence[Message]) -> int:
    """Rough text-token estimate, including reasoning that the adapter sends back to the model."""
    # Search intentionally omits reasoning; using only its text undercounted reasoning-heavy tails.
    return sum(len(message_text(m)) + len(m.reasoning_content or "".join(b.text for b in m.content_blocks if isinstance(b, ThinkingBlock))) for m in messages) // CHARS_PER_TOKEN


def compaction_cut(history: Sequence[Message], keep_recent: int) -> int:
    """Index where the kept tail starts: at most ``keep_recent`` messages, never inside a tool exchange.

    The tail begins at a user-role message that is not a tool result (a turn boundary), so a
    tool call is never separated from its result and a summary never ends on an unanswered call.
    """
    if keep_recent <= 0 or len(history) <= keep_recent:
        return len(history) if keep_recent <= 0 else 0
    cut = len(history) - keep_recent
    while cut > 0:
        m = history[cut]
        if m.role is MessageRole.user and not any(isinstance(b, ToolResultBlock) for b in m.content_blocks):
            break
        cut -= 1
    return cut


def verbatim_tail(history: Sequence[Message], count: int = VERBATIM_TAIL_MESSAGES, *, room: int = OPERATOR_QUOTES_MAX_CHARS) -> str:
    """The operator's last passages, appended by code so the summary can never lose their wording.
    Each has an equal share of ``room``; past it, it is cut with a marker, never silently."""
    tail = operator_passages(history)[-count:]
    if not tail:
        return ""
    lines = ["", "", "## Recent operator messages (verbatim)"]
    for m, text in tail:
        lines.append(_quoted(f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {text}", max(1, room // len(tail))))
    return "\n".join(lines)


def annotate_summary(message: Message, note: str, seqs: Sequence[int]) -> Message:
    """Append the archived-range note inside a summary's wrapper and record the exact seqs in its metadata."""
    blocks = list(message.content_blocks)
    for i, block in enumerate(blocks):
        if isinstance(block, TextBlock):
            text = block.text
            if text.rstrip().endswith("</compacted-turn>"):
                cut = text.rstrip()[: -len("</compacted-turn>")]
                text = f"{cut.rstrip()}\n\n{note}</compacted-turn>"
            else:
                text = f"{text.rstrip()}\n\n{note}"
            blocks[i] = block.model_copy(update={"text": text})
            break
    return message.model_copy(update={"content_blocks": blocks, "metadata": {**message.metadata, "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": list(seqs)}}})


def repoint_summary(message: Message, mapping: dict[int, int]) -> Message:
    """A compaction summary copied into another session: its archived seqs and inline note now name that session's rows."""
    archived = message.metadata.get("daedalus.archived") or {}
    seqs = sorted(mapping[int(x)] for x in archived.get("seqs") or [] if int(x) in mapping)
    blocks = list(message.content_blocks)
    for i, block in enumerate(blocks):
        if isinstance(block, TextBlock) and "[archived turns " in block.text:
            text = re.sub(r"\[archived turns [^\]]*\]", "", block.text).rstrip()
            if seqs:
                contiguous = seqs[-1] - seqs[0] + 1 == len(seqs)
                span = f"seq {seqs[0]}–{seqs[-1]}" if contiguous else f"within seq {seqs[0]}–{seqs[-1]} ({len(seqs)} turns)"
                note = f"[archived turns {span}: HistoryExpand({seqs[0]}, {seqs[-1]}) returns them verbatim]"
                text = f"{text[: -len('</compacted-turn>')].rstrip()}\n\n{note}</compacted-turn>" if text.endswith("</compacted-turn>") else f"{text}\n\n{note}"
            blocks[i] = block.model_copy(update={"text": text})
            break
    meta = {"from_seq": seqs[0], "to_seq": seqs[-1], "seqs": seqs} if seqs else {"seqs": []}
    return message.model_copy(update={"content_blocks": blocks, "metadata": {**message.metadata, "daedalus.archived": meta}})


def operator_language(history: Sequence[Message]) -> str:
    """A coarse guess at the operator's language from their messages (Cyrillic → Russian, else English)."""
    text = " ".join(
        prompts.without_turn_context(b.text) for m in history if m.role is MessageRole.user for b in m.content_blocks if isinstance(b, TextBlock)
    )
    letters = [c for c in text if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3:
        return "Russian"
    return "English"


class _LiveVision:
    """Resolves the ImageView provider/model at call time, so settings edits apply immediately."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager

    def __iter__(self):  # type: ignore[no-untyped-def]
        resolved = self._manager._vision()
        if resolved is None:
            raise ValueError("no vision model is configured")
        return iter(resolved)

    def __bool__(self) -> bool:
        return self._manager._vision() is not None


def _forget_persisted(state: SessionState) -> None:
    """Tell the core that what it believes the store holds is no longer there.

    The core remembers the history it last handed over and appends onto it. Rows this host
    dropped or rebuilt — a compaction, a revert, a cleared history, a fork's fresh copy — are
    not there to be appended onto, and the next round would add its messages to a generation
    that no longer contains what came before them. Forgetting makes the next hand-over a
    rewrite, which is what it is. A core that rewrote the history every round has nothing to
    forget and does not carry this.
    """
    engine = state.engine
    forget = getattr(engine, "forget_persisted_history", None) if engine is not None else None
    if callable(forget):
        forget()


def _forget_if_dropped(state: SessionState) -> Callable[[asyncio.Task[None]], None]:
    """Take back the promise if the write the hook returned on never landed.

    The hook returns as soon as the write is a task, which tells the core the round is
    recorded. When that task fails or is cancelled, it is not, and the core would go on
    appending after messages that were never written. Forgetting makes the next hand-over a
    rewrite, which writes the missing ones with everything else.
    """

    def done(task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            _forget_persisted(state)

    return done


def _last_answer(history: Sequence[Message]) -> str:
    """The text of the last assistant turn that said anything, on one line, without its retrieval headline.

    It becomes the "finished" notification's summary, which the operator reads; the headline in it
    leaked into every push and toast until it was split off here.
    """
    for message in reversed(history):
        if message.role is not MessageRole.assistant:
            continue
        text = prompts.split_headline(" ".join(block.text for block in message.content_blocks if isinstance(block, TextBlock)).strip())[0]
        if text:
            return " ".join(text.split())
    return ""


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("run task %s failed: %r", task.get_name(), exc, exc_info=exc)


def _bind(fn: Callable[..., Awaitable[Any]] | None, session_id: str) -> Callable[..., Awaitable[Any]] | None:
    if fn is None:
        return None

    async def bound(*args: Any, **kwargs: Any) -> Any:
        return await fn(session_id, *args, **kwargs)

    return bound


def _own_schedules(fn: Callable[..., Awaitable[Any]] | None, session_id: str) -> Callable[..., Awaitable[Any]] | None:
    """The schedule service as the companion sees it: the list holds only what it proposed itself.

    Every other session's ScheduleList shows every schedule, which is how an agent avoids proposing
    a second copy of one. The companion is the operator's personal agent and its schedules are its
    own; reading the operator's whole timetable back to them is not what "your schedules" means.
    """
    if fn is None:
        return None

    async def scoped(op: str, **kwargs: Any) -> Any:
        result = await fn(op, **kwargs)
        if op != "list":
            return result
        return [row for row in result if session_id in (row.get("created_by_session"), row.get("source_session_id"))]

    return scoped


__all__ = ["HOME_KEY", "Attachment", "HostUnreachable", "PendingQuestion", "SessionManager", "SessionState", "WorkspaceUnreachable", "annotate_summary", "home_of", "host_home_name", "verbatim_tail"]
