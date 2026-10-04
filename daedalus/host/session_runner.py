"""Sessions and runs: one ``QueryEngine`` per session, one asyncio task per run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.runtime_constants import LoopConstants
from protocore.contracts.tool_registry import ToolVisibilityPolicy, policy_admits, tool_group_of
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    OPERATOR_WORDS_METADATA_KEY,
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
from protocore.tools.memory import build_memory_tools
from protocore.tools.tool_search import TOOL_SEARCH_TOOL_NAME, ToolSearchTool

from daedalus.config import (
    DISPATCHER_TOOLS,
    ORCHESTRATOR_ONLY_TOOLS,
    ORCHESTRATOR_TOOLS,
    REASONING_EFFORTS,
    STAFF_BLOCKED_TOOLS,
    STAFF_ONLY_TOOLS,
    SUBAGENT_BLOCKED_TOOLS,
    VOICE_ONLY_TOOLS,
    VOICE_TOOLS,
    ModeConfig,
    NoModelConfigured,
    RuntimeConfig,
    Settings,
    max_advertised_tools_for,
    on_demand_tool_groups_for,
)
from daedalus.host import capabilities, launcher_bridge, prompts, tool_groups
from daedalus.host.checkpoint_retention import CheckpointRetention, RetentionBounds, RetentionReport
from daedalus.host.checkpoints import DIR_NAME as CHECKPOINT_DIR_NAME
from daedalus.host.checkpoints import CheckpointError, Checkpoints, scan_workspace
from daedalus.host.containment import Walls, walls_for, worktree_writable_paths
from daedalus.host.engine_factory import TENANT, EngineDeps, PolicyAdapter, build_engine
from daedalus.host.events import AppEvent, EventBus, EventFilter
from daedalus.host.hooks import DaedalusHookManager
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.host.policy import Decision, Policy, Rule, canonical
from daedalus.host.presence import Presence
from daedalus.host.request_manifests import RequestManifestStore
from daedalus.host.run_outcome import outcome_message, run_outcome
from daedalus.host.services import SessionServices, locator, session_scratch_dir
from daedalus.host.skills import DirectorySkillStore
from daedalus.host.transcript_view import TranscriptViewBuilder, message_view
from daedalus.host.worktrees import append_exclude, exclude_lines
from daedalus.mcp.manager import McpManager, blocked_for, mcp_tool_prefix
from daedalus.processes import end_tree
from daedalus.providers.chain import build_chain
from daedalus.providers.registry import ProviderRegistry
from daedalus.security import redact
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.dispatches import DispatchStore
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.files import MAIN as MAIN_FILES
from daedalus.stores.files import FileRefused, FileStore
from daedalus.stores.knowledge import KnowledgeStore
from daedalus.stores.media import MediaStore
from daedalus.stores.persistent import PersistentMemory, PersistentWorkspace
from daedalus.stores.projects import Project, ProjectFolder, ProjectSettings, ProjectStore
from daedalus.stores.provider_holds import (
    PROVIDER_RESUME_NOTE,
    pin_resumed_run_in,
    pinned_target_in,
    recovery_candidate_in,
    resume_target_in,
)
from daedalus.stores.sqlite import (
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
from daedalus.tools import TOOL_GROUPS, discover_tools
from daedalus.tools.dispatcher import build as build_dispatcher_tools

logger = logging.getLogger(__name__)

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
    "llm_stream_idle": "outage",
    "llm_provider_error": "outage",
}
"""The core's error class behind a demotion, as the one word the app and the operator read."""
BRIEF_MAX_CHARS = 12_000
"""A spawned agent's brief lives in its system prompt; longer hand-overs belong in files."""
WORKSPACE_NOTES_CHARS = 6000
MCP_EXAMPLE_NAMES = 5
"""How many of a large MCP server's tool names McpList and McpEnable show beside its prefix and count:
enough to show what the server covers and how its names are spelt, not so many that the list is back."""
GRANT_TTL_SECONDS = 2 * 3600

STEER_CARD_CHARS = 200
"""How much of a queued steer travels with a change event. The app draws a card, not the message."""

STEER_CARD_LIMIT = 20
"""Cards one change event carries. Past this the count is the answer; nobody reads the twenty-first card."""
"""How long an approval key stays spendable: long enough for the agent to retry, short enough that a forgotten grant does not wait for a later call."""
"""How much of the workspace AGENTS.md rides along in the prompt; the rest is one Read away."""

EventSink = Callable[[str, Any], Awaitable[None]]
"""A session's listener. It takes the loop's ``TurnEvent`` and the host's own ``HostEvent`` alike: both carry ``type``, ``run_id`` and ``payload``."""
RunFinished = Callable[[str, str, str], Awaitable[None]]  # session_id, run_id, status


class HostEventType(StrEnum):
    """Event kinds the host raises beside the core's turn taxonomy."""

    STEER_CHANGED = "steer_changed"
    HISTORY_CUT = "history_cut"


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
class Attachment:
    path: Path
    mime_type: str = "application/octet-stream"
    caption: str | None = None
    name: str | None = None
    content_sha256: str | None = None
    stored_name: str | None = None


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
    run_origin: str = "operator"
    """Who started the current run (``operator``, ``schedule``, ``reminder``, ``subagent:…``): StaySilent and the reply routing read it."""
    last_error_kind: str = ""
    """The kind of the error that ended the current run, from the core's ERROR event (``llm_context_window_exceeded`` …)."""
    last_error_message: str = ""
    """The text of that error, masked, as the fronts were shown it: what the inbox entry quotes."""
    last_error_permanent: bool = False
    """The provider's verdict on that error said asking again cannot help (a refused request, key or model)."""
    soft_stop_cause: str = ""
    """Which bound wound the current run down, from the core's ``soft_stop_notified`` state change; empty when none did."""
    soft_stop_detail: str = ""
    """What that bound said in its own words — the upstream's error text for a provider failure. The operator is shown this,
    not an inference from the reply: a run the provider refused ends with the model writing a closing message, and without
    this the message is the only trace of a failure that produced nothing."""
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
    compacting: dict[str, Any] | None = None
    """A compaction in flight: reason, stage (summarising/merging/writing), parts done of total, started_at.
    The Mini App and the list read it; ``None`` when none is running."""
    auto_compaction: asyncio.Task[None] | None = None
    """The compaction check a settled run left behind. Deleting the session cancels it: a summary finished
    after the rows are gone writes into a session that no longer exists."""
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
    A live override (a preset chosen for the session, the voice concierge pointing at its own) is part of it —
    what the operator chose is never a fallback, however far it is from the global default."""
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
    steer_withdrawn: set[str] = field(default_factory=set)
    """Ids taken back since that reload. A reload already waiting on the database returns the row as
    it was before the removal, so it is filtered through this on the way into the engine; cleared at
    the next persist, by which time the store and the engine agree."""

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

NOT_OPERATOR_FACING = ("subagent_of", "unattended", "heartbeat", "staff_id")
"""Session metadata that marks a session nobody reads as a conversation, whatever started the run."""

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


def _steer_cards(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The waiting steers as the composer draws them: a recognisable amount of each, and not all of them."""
    cards: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        if len(cards) >= STEER_CARD_LIMIT:
            break
        card: dict[str, Any] = {"id": str(item.get("id") or ""), "text": text[:STEER_CARD_CHARS], "queued_at": item.get("queued_at")}
        if len(text) > STEER_CARD_CHARS:
            card["truncated"] = True
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
        self.checkpoint_retention = CheckpointRetention(db, workspaces_dir=settings.workspaces_dir, busy=self.busy_sessions, occupants=self.store_occupants)
        self.memory = PersistentMemory(db)
        self.workspace_units = PersistentWorkspace(db)
        self.skills = DirectorySkillStore(settings.skills_dir)
        self.redactor = redact.shared()
        self.capabilities = capabilities.resolve(settings, config)
        """What this installation can do, decided once: the tools, the routes, the prompt and the
        app all read the same answer, and a configuration change reaches them on the next start."""
        capabilities.publish(self.capabilities, settings.state_dir)
        """And the supervisor reads it from there rather than resolving it a second time."""
        self._configure_redactor(settings, config)
        self.hooks = DaedalusHookManager(self.redactor, hooks_config=lambda: self.config.hooks)
        self.bus = EventBus(db, redactor=self.redactor, queue_size=config.ops.event_subscriber_queue, replay_max=config.ops.event_replay_max)
        """What happens to sessions, terminals and staff, for whoever subscribes: the app's stream,
        the notifications, an orchestrator. Persisted before it is delivered, so a subscriber resumes
        from its cursor after a restart."""
        self.presence = Presence(self.bus, db, ttl_seconds=config.ops.presence_ttl_seconds, grace_seconds=config.ops.presence_grace_seconds)
        """What the operator's windows show: whether a finished run was watched, and when an unread
        result has been seen."""
        self._background: set[asyncio.Task[Any]] = set()
        self._jobs: dict[str, dict[str, Any]] = {}
        self.tools = ToolRegistry()
        """Every tool but the main orchestrator's, MCP proxies included; a session sees the part its policy admits."""
        self.dispatcher_tools = ToolRegistry()
        """The main orchestrator's tools and the few shared ones it may call, and nothing else."""
        self.providers = ProviderRegistry(
            settings, config, usage_sink=self.usage, image_loader=self._load_image,
            admission=HostInferenceAdmission(self),
        )
        self.mcp = McpManager(config.mcp.servers, self.tools, token_dir=settings.state_dir / "mcp")
        # A server's catalogue can change under running sessions — a re-listing, a reconnect, another
        # session enabling it. Each session refuses the proxies of servers it did not enable by name, so
        # every session's policy is recomputed at once, not at the next thing that happens to refresh it.
        self.mcp.catalog_listeners.append(lambda server: self._apply_tool_visibility_all())
        self.request_manifests = RequestManifestStore(db, self.blobs, tenant_id=TENANT)
        self._group_usage = tool_groups.UsageCache()
        self.governance_path = governance_path or (settings.bot_repo_dir / "GOVERNANCE.md")
        self._states: dict[str, SessionState] = {}
        self._sinks: list[EventSink] = []
        self._finished: list[RunFinished] = []
        self.compaction_hooks: list[Callable[[str, dict[str, Any]], Awaitable[None]]] = []
        """Called after an automatic compaction with what changed, so the chat can say so in one line."""
        self._pending_restored: list[Callable[[str, PendingQuestion], Awaitable[None]]] = []
        self.service_hooks: dict[str, Any] = {}
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

    async def start(self, *, recovering: bool | None = None) -> None:
        """Open the stores; ``recovering`` (default: whether a previous process left runs behind) gates new runs until resume_unfinished()."""
        await self.memory.load()
        await self.workspace_units.load()
        await self.bus.start()
        await self.presence.load()
        self.bus.on(EventFilter(types=("presence",)), self._on_presence, name="unread-result")
        # The policy is built inside a tool call and cannot wait on a query; this is where the project
        # roots it compares against are read — and where a folder of our own that is not on disk is
        # put back, so the first run after a start is not the thing that discovers it missing.
        await self.projects.ensure_roots()
        # New runs wait until resume_unfinished() has continued what the previous process left behind;
        # a process that finds nothing to resume (tests, a first start) opens the gate at once.
        self.recovering = recovering if recovering is not None else bool(await self.events.unfinished_snapshots())
        self.stale_runs = []
        # A tool this installation cannot honour is not registered at all: an unusable name in the
        # list is an invitation the model accepts and a failure it cannot understand.
        disabled = self.capabilities.selfdev.disabled_tools
        browser = self.capabilities.browser.configured
        for tool in discover_tools():
            if tool.name in disabled or (not browser and tool.name.startswith(capabilities.BROWSER_TOOL_PREFIX)):
                continue
            self.tools.register(tool)
        for tool in build_memory_tools(self.memory):
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

    def provider_costs_nothing(self, provider_id: str | None) -> bool:
        """Whether dollar caps are irrelevant to this endpoint by definition."""
        if not provider_id:
            return False
        try:
            return self.providers.get(provider_id).endpoint.kind == "llamacpp"
        except KeyError:
            return False

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def on_finished(self, callback: RunFinished) -> None:
        self._finished.append(callback)

    def on_pending_restored(self, callback: Callable[[str, PendingQuestion], Awaitable[None]]) -> None:
        """Called after a restart for every session still waiting on a question."""
        self._pending_restored.append(callback)

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
        _, preset = found
        try:
            provider = self.providers.get(preset.provider)
        except KeyError:
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
            return self.providers.rungs_for_pair(self.config, overrides["provider"], overrides["model_name"]), default
        return self.providers.rungs_for(self.config), default

    # -- MCP per session --------------------------------------------------------------

    def mcp_enabled(self, state: SessionState) -> list[str]:
        return [s for s in state.metadata.get("mcp_enabled", []) if s in self.mcp.available()]

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
            await self.mcp.ensure(server)
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
            lines = []
            for item in self.mcp.status():
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

    def workspace_of(self, session_id: str, metadata: dict[str, Any], project: Project | None) -> Path:
        """Return the session's project folder, its private directory inside that folder, or its own
        directory under the workspaces root (``home``) when its folders are out of this process's reach."""
        home = home_of(metadata)
        if home:
            return self.settings.workspaces_dir / home
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
        return None if home_of(metadata) else self.folder_of(session_id, metadata, project)

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
    ) -> SessionState:
        sid = session_id or uuid.uuid4().hex[:12]
        project = await self.projects.get(project_id) if project_id else None
        if project_id and project is None:
            raise KeyError(project_id)
        meta = dict(metadata or {})
        if folder_id:
            folder = project.folder(folder_id) if project is not None else None
            if folder is None:
                raise ValueError(f"{folder_id} is not a folder of {project.name if project is not None else 'a named project'}")
            if not folder.local(self.projects.local_env):
                # A Daedalus session works with this process's own tools, which cannot reach a folder
                # of the other environment; only a terminal bridged there can.
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
        else:
            meta.pop("directory", None)
        workspace = self.workspace_of(sid, meta, project)
        _ensure_inbox(workspace, self.inbox_folder(sid, meta, project))
        session = Session(id=sid, tenant_id=TENANT, title=title, metadata=dict(meta))
        await self.sessions.create(session, project_id=project.id)
        state = SessionState(session=session, workspace=workspace, metadata=dict(meta), project=project)
        self._states[sid] = state
        self.register_services(state)
        return state

    def live_state(self, session_id: str) -> SessionState | None:
        """The state of a session this process already holds; ``None`` for one it would have to load."""
        return self._states.get(session_id)

    async def project_of(self, session_id: str) -> Project | None:
        """The project a session works in, for anything that makes a child of it.

        Every path that creates a session from another one — a subagent, a spawned agent, a fork, a
        scheduled task, the voice concierge's delegate — asks this and passes the answer on. A child
        that did not would carry the parent's directory without the parent's wall, which is the whole
        of the boundary gone through the commonest way of making a new worker.
        """
        state = self._states.get(session_id)
        if state is not None:
            return state.project
        return await self.projects.for_session(session_id)

    async def get_state(self, session_id: str) -> SessionState | None:
        state = self._states.get(session_id)
        if state is not None:
            return state
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
        measurement = await self.db.kv_get(f"context_measurement:{session_id}", {})
        state.usage_floor_seq = int(measurement.get("floor", 0))
        state.observed_prompt_tokens = int(measurement.get("tokens", 0))
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
        candidates = [m for m in state.engine.history if self.sessions.transcript_key(m) not in known_keys]
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
        out: list[dict[str, Any]] = []
        for session in rows:
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
                }
            )
        return out

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
            background = len(self._jobs.get(row["id"], {})) + children.get(row["id"], 0)
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
        """Project existing jobs and child sessions into one operator-facing task list."""
        now = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for job in self._jobs.get(session_id, {}).values():
            code = job.process.returncode
            state = "running" if code is None else "cancelled" if code < 0 else "done" if code == 0 else "failed"
            started = now.timestamp() - max(0.0, time.monotonic() - job.started)
            try:
                active = datetime.fromtimestamp(job.log.stat().st_mtime, UTC)
            except OSError:
                active = datetime.fromtimestamp(started, UTC)
            out.append(
                {
                    "id": job.id,
                    "owner_session_id": session_id,
                    "parent_run_id": None,
                    "kind": "job",
                    "state": state,
                    "title": job.command[:200],
                    "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
                    "last_activity_at": active.isoformat(),
                    "progress": None,
                    "child_session_id": None,
                    "result_ref": f".jobs/{job.log.name}",
                    "stop_supported": code is None,
                }
            )
        for row in await self.list_sessions(limit=10_000):
            if row["metadata"].get("subagent_of") != session_id:
                continue
            state = "running" if row["status"] in ("running", "waiting", "compacting") else "failed" if row["status"] == "failed" else "done"
            out.append(
                {
                    "id": f"agent:{row['id']}",
                    "owner_session_id": session_id,
                    "parent_run_id": None,
                    "kind": "agent",
                    "state": state,
                    "title": str(row["metadata"].get("subagent_name") or row["title"]),
                    "started_at": row["created_at"],
                    "last_activity_at": row["last_message_at"],
                    "progress": None,
                    "child_session_id": row["id"],
                    "result_ref": None,
                    "stop_supported": state == "running",
                }
            )
        out.sort(key=lambda item: (item["state"] not in ("running", "waiting"), item["last_activity_at"], item["id"]))
        return out

    async def stop_task(self, session_id: str, task_id: str) -> bool:
        """Stop a task only when it belongs to the session named by the route."""
        if task_id.startswith("agent:"):
            child_id = task_id.removeprefix("agent:")
            child = await self.get_state(child_id)
            if child is None or child.metadata.get("subagent_of") != session_id:
                return False
            await self.stop(child_id)
            return True
        job = self._jobs.get(session_id, {}).get(task_id)
        if job is None:
            return False
        if job.process.returncode is None:
            try:
                end_tree(job.process.pid, hard=False)
            except ProcessLookupError:
                pass
            await job.process.wait()
        return True

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
        if state.provider_hold is not None:
            state.provider_hold.close()
            state.provider_hold = None
            await self.providers.close_retired()
        self._states.pop(session_id, None)
        locator.unregister(session_id)
        for job in self._jobs.pop(session_id, {}).values():
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
            await conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
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
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            project = state.project
            if project is not None and project.folders and project.primary.managed:
                # Remember ownership for older projects too, even when their creator goes first.
                if project.id == session_id:
                    await conn.execute("UPDATE projects SET settings = json_set(settings, '$.ephemeral', json('true')) WHERE id = ?", (project.id,))
                await conn.execute(
                    "DELETE FROM projects WHERE id = ? AND system = '' AND json_extract(settings, '$.ephemeral') = 1 "
                    "AND NOT EXISTS (SELECT 1 FROM sessions WHERE project_id = projects.id)",
                    (project.id,),
                )
        await self.projects.list()
        # The logs a read-only folder's session kept in the state volume are the session's alone.
        # Removed whether or not the folder is read-only today: it may have been when they were written.
        shutil.rmtree(session_scratch_dir(self.settings.state_dir, session_id), ignore_errors=True)
        # A private child belongs to this session. A project folder never goes with a session.
        if (
            delete_workspace
            and state.metadata.get("directory")
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
            if project is None:
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
        ratio = 1.0 if required_only else cfg.auto_ratio

        def below(status: dict[str, Any]) -> bool:
            return not status["window"] or status["tokens"] < ratio * status["window"] or status["messages"] < cfg.min_messages

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
        if below(status) or held(status):
            return
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
                await self._compact_locked(state, "", keep_recent=cfg.keep_recent_messages, reason="auto", own_task_ok=True)  # own_task_ok: the required_only call runs inside the run's own task
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

    async def _compact_locked(self, state: SessionState, instructions: str, *, keep_recent: int = 0, reason: str = "manual", own_task_ok: bool = False) -> str:
        session_id = state.session.id
        if not instructions and self.is_orchestrator(state):
            # Its brief, team and board come back in full every turn; a summary that spends its words
            # on them loses the promises and open questions that exist nowhere else.
            instructions = prompts.ORCHESTRATOR_COMPACTION
        elif not instructions and self.is_dispatcher(state):
            instructions = prompts.DISPATCHER_COMPACTION
        if state.running and state.engine is not None and state.engine.is_terminal and state.task is not None and not own_task_ok:
            # The loop has settled; only bookkeeping remains.
            await asyncio.gather(asyncio.shield(state.task), return_exceptions=True)
        busy = state.running and not (own_task_ok and state.task is asyncio.current_task())
        if busy or state.pending is not None:
            raise RuntimeError("the session is busy; stop the run (or answer the question) first")
        pending = [t for t in state.persist_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # no straggler may write the old history later
        exceeded = self.budget_exceeded()
        compact_provider, _compact_model = await self._compaction_rung(state)
        if exceeded and not self.provider_costs_nothing(compact_provider.endpoint.id):
            raise RuntimeError(f"daily budget exceeded ({exceeded}); compaction is a paid call")
        full = list(state.engine.history) if state.engine is not None else list(
            await self.sessions.list_messages(session_id, TENANT, limit=10_000)
        )
        if not full:
            raise RuntimeError("nothing to compact")
        cut = compaction_cut(full, keep_recent)
        history, tail = full[:cut], full[cut:]
        if not history:
            raise RuntimeError("nothing to compact: the whole history is inside the kept tail")
        async with self.idle_work:
            state.compacting = {"reason": reason, "stage": "summarising", "messages": len(history), "parts_done": 0, "parts_total": 0, "started_at": datetime.now(UTC).isoformat()}
        await self._compaction_progress(state)
        await self._publish_status(state, "compacting")
        try:
            return await self._compact_progressing(state, history, tail, instructions, reason, own_task_ok=own_task_ok)
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

    async def _compaction_rung(self, state: SessionState) -> tuple[Any, str]:
        session_id = state.session.id
        rungs, _ = self.resolve_model(await self.live.load(session_id))
        provider, model = rungs[0]  # the session's own model summarises its own history …
        if self.config.compaction.preset and self.config.compaction.preset in self.config.presets:
            try:
                provider, model = self.providers.rungs_for(self.config, self.config.compaction.preset)[0]  # … unless a cheaper one is configured for it
            except Exception:  # noqa: BLE001 — an unusable compaction preset falls back to the session's model
                logger.warning("compaction preset %r is not usable; summarising with the session's model", self.config.compaction.preset)
        return provider, model

    async def _compact_progressing(self, state: SessionState, history: list[Message], tail: list[Message], instructions: str, reason: str, *, own_task_ok: bool) -> str:
        session_id = state.session.id
        provider, model = await self._compaction_rung(state)
        language = self.config.answer_language if self.config.answer_language != "auto" else operator_language(history)
        observability = LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, run_id=state.run_id, call_purpose="compaction", call_category="compaction")
        with self.providers.hold([provider]):
            summary = await self._summarise_history(provider, model, history, language=language, instructions=instructions, observability=observability, progress=lambda **f: self._compaction_progress(state, **f))
        await self._compaction_progress(state, stage="writing")
        # What the operator said is written by code, never by the summariser: rules do not decay.
        # Their room is a share of the window as well as a fixed bound: a summary that quoted every
        # long message whole could itself fill the window past the trigger, and compact again each turn.
        window = int((await self.context_status(state)).get("window") or 0)
        share = min(OPERATOR_QUOTES_WINDOW_SHARE, self.config.compaction.auto_ratio / 4 or OPERATOR_QUOTES_WINDOW_SHARE)
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

    async def _summarise_history(self, provider: Any, model: str, history: Sequence[Message], *, language: str, instructions: str, observability: LLMObservabilityContext, progress: Callable[..., Awaitable[None]] | None = None) -> str:
        """One structured summary of ``history``: a single call, or parallel part summaries merged when the transcript is long.

        ``progress`` hears how many parts there are and each one finishing, then the merge; a
        compaction of a long history takes minutes and the operator watches it."""

        async def report(**fields: Any) -> None:
            if progress is not None:
                await progress(**fields)

        cfg = self.config.compaction
        focus = f"\n\nThe operator asks to focus on: {instructions.strip()}" if instructions.strip() else ""
        transcript_text = transcript_for_summary(history)
        parts = split_transcript(transcript_text, cfg.chunk_tokens)
        await report(parts_total=len(parts), parts_done=0, stage="summarising")
        if len(parts) == 1:
            return await self._summary_call(provider, model, COMPACT_PROMPT.format(language=language, max_words=cfg.max_words) + focus, transcript_text, observability)
        part_words = max(300, cfg.max_words // 2)
        done = 0

        async def part_summary(index: int, part: str) -> str:
            nonlocal done
            text = await self._summary_call(provider, model, CHUNK_PROMPT.format(index=index + 1, total=len(parts), language=language, max_words=part_words) + focus, part, observability, strict=False)
            done += 1
            await report(parts_done=done)
            return text

        partials = await asyncio.gather(*(part_summary(i, part) for i, part in enumerate(parts)))
        joined = "\n\n".join(f"<part {i + 1}>\n{text}\n</part {i + 1}>" for i, text in enumerate(partials) if text)
        await report(stage="merging")
        return await self._summary_call(provider, model, MERGE_PROMPT.format(language=language, max_words=cfg.max_words) + focus, joined, observability)

    async def _summary_call(self, provider: Any, model: str, prompt: str, body: str, observability: LLMObservabilityContext, *, strict: bool = True) -> str:
        summary = ""
        candidate = ""
        problem = ""
        timeout = self.config.compaction.call_timeout_seconds
        for attempt in range(2 if strict else 1):
            reminder = f"\n\nYour previous attempt was rejected: {problem}. Produce every section, each exactly once, in the given order." if problem else ""
            request = LLMRequest(
                model=model,
                messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt + reminder + "\n\n" + body)])],
                max_tokens=6000,
                temperature=0.2,
                extra={"enable_thinking": False},
                observability=observability,
            )
            try:
                response = await asyncio.wait_for(provider.complete_text(request), timeout=timeout)
            except TimeoutError:
                # A provider stall must not hold the session: one more try, then the compaction gives up.
                # The retry gets twice the time. A model that reasons whether or not it is asked to (Grok
                # spends four to six thousand reasoning tokens on a part) takes 70 to 90 s over a 30k-token
                # part, and the same request under the same limit failed the same way every time.
                logger.warning("compaction summariser call exceeded %.0fs; retrying once with %.0fs", timeout, 2 * timeout)
                response = await asyncio.wait_for(provider.complete_text(request), timeout=2 * timeout)
            candidate = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
            problem = validate_summary_sections(candidate)
            if not problem:
                summary = candidate
                break
            logger.warning("compaction summary rejected (attempt %d): %s", attempt + 1, problem)
        if not summary:
            if not candidate:
                raise RuntimeError("the model returned an empty summary")
            logger.warning("compaction summary accepted without the fixed sections: %s", problem)
            summary = candidate  # a usable summary beats a session the operator cannot compact
        return summary

    # -- checkpoints, revert, fork ------------------------------------------------------

    async def checkpoint(self, state: SessionState, *, kind: str, seq: int | None = None, run_id: str | None = None) -> str | None:
        """Snapshot the workspace (bounded by ``ops.checkpoint_max_gb``); returns the commit id or None.

        A project is not snapshotted unless the operator asked for it. A session workspace holds what
        one agent made; a project root is the operator's own repository, and committing it into
        ``.checkpoints`` before every turn and after every run is a cost the undo does not repay on a
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
            sha = await Checkpoints(state.workspace).snapshot(f"{kind} seq={seq} run={run_id}", scan=scan)
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
                exceeded = self.budget_exceeded()
                if exceeded and not self.provider_costs_nothing(provider_id):
                    raise RuntimeError(f"daily budget exceeded ({exceeded})")
                breach = await self.cap_breach(state, provider_id)
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
                    untouched = await Checkpoints(state.workspace).restore(sha, record=False)
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
            state.soft_stop_cause = ""
            state.soft_stop_detail = ""
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

    async def fork_into(self, source_id: str, seq: int, target: SessionState) -> dict[str, Any]:
        """Give ``target`` the source's history before transcript ``seq`` and the right project files.

        Turns a revert undid and turns a compaction summary already stands for are left out of
        the working history; the archived originals still go into the fork's transcript, and
        the summary is re-pointed at the fork's own seqs so HistoryExpand keeps working there.

        Two sessions in the same project share one directory and nothing is copied at all. Where a
        copy does happen it is a whole directory tree, so it is measured first and refused above
        ``ops.checkpoint_max_gb`` rather than written into the state directory unbounded.
        """
        source = await self.get_state(source_id)
        if source is None:
            raise KeyError(source_id)
        async with source.lock:
            await self._wait_until_quiet(source, what="the source session")
            pending = [t for t in source.persist_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            rows = await self.sessions.list_transcript(source_id)
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
            if target.workspace == source.workspace:
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
                checkpoints = Checkpoints(target.workspace)
                await checkpoints.relocate()
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
        sched = await self.db.fetchall("SELECT workspace FROM schedules")
        known_paths.update(Path(r["workspace"]).resolve() for r in sched)
        known_paths.add((self.settings.workspaces_dir / "heartbeat").resolve())
        # A system project's folder lives under the workspaces root and is not a workspace nobody
        # refers to: the concierge's agents work in it, and a sweep would take their files with it.
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
            out.setdefault(state.workspace / CHECKPOINT_DIR_NAME, set()).add(session_id)
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
            schedule=hooks.get("schedule"),
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
            extra={"skill_store": self.skills, "manager": self, "vision": _LiveVision(self), "jobs": self._jobs.setdefault(state.session.id, {})},
        )
        if services.walls is not None and not services.workspace_writable:
            # The host writes a read-only folder's command logs to the state volume instead, and the
            # session reads them there: a readable wall, never a writable one, so the agent sees
            # its jobs' output without being able to write into the installation.
            services.log_root = session_scratch_dir(self.settings.state_dir, state.session.id)
            services.walls = Walls(readable=(*services.walls.readable, services.log_root), writable=services.walls.writable)
        state.services = services
        locator.register(services)

    def walls_of(self, state: SessionState) -> Walls | None:
        """The walls of a loaded session, from its project, its folder and its own directory."""
        if state.project is None:
            return None
        metadata = state.session.metadata
        # A staff member is walled by its isolation: its own worktree (and what a commit there
        # writes) instead of the checkout it was made from, or nothing writable at all.
        isolation = str(metadata.get("staff_isolation") or "shared")
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

    async def queued_steers(self, session_id: str) -> list[dict[str, Any]]:
        """The steers this session has taken in and not yet given to the model, oldest first.

        The store is the record: a run reloads the queue from it before every model call and writes
        back what it did not place. Between those two moments a running engine holds the only copy,
        so a listing taken exactly then can name an item the model is already reading; the change
        event that follows the write corrects it within the round.

        Each item comes back as a card: enough text to recognise it, with ``truncated`` set when
        there is more. Three long pasted messages otherwise travel whole to every open stream on
        every later change of the queue.
        """
        queued = await self.live.load(session_id)
        return _steer_cards(queued["steer"])

    async def drop_queued_steer(self, session_id: str, item_id: str) -> bool:
        """Take one steer back before the run reads it; ``False`` when it is already gone.

        The engine is asked first and the store second. A run between its reload and its next model
        call holds the queue in memory and would write that copy back over any store-only removal —
        emptying its list first is what actually stops the message, and the store write behind it
        stops a reload from bringing the item round again.
        """
        state = self._states.get(session_id)
        removed = False
        engine = state.engine if state is not None else None
        if engine is not None:
            queue = list(getattr(engine, "_steer_queue", []) or [])
            kept = [item for item in queue if str(item.get("id") or "") != item_id]
            if len(kept) != len(queue):
                engine._steer_queue = kept  # type: ignore[attr-defined]
                removed = True
        receipt = await self.live.receipt(session_id, item_id)
        if receipt is not None:
            withdrawn = await self.live.withdraw(session_id, item_id)
            if withdrawn is not None and withdrawn["status"] == "withdrawn":
                removed = True
        elif await self.live.remove(session_id, "steer", item_id):
            removed = True
        if removed and state is not None:
            # A reload that was already waiting on the database when this ran will be handed the row
            # as it stood before the removal. Remembering the id here is what stops that answer from
            # putting the withdrawn message back into the engine's queue, where it is authoritative.
            state.steer_withdrawn.add(item_id)
        if removed:
            await self.steer_changed(session_id, reason="withdrawn")
        return removed

    async def steer_changed(self, session_id: str, *, reason: str) -> None:
        """Tell the session's listeners that its steer queue is not what they last drew."""
        state = self._states.get(session_id)
        waiting = [item for item in (await self.live.load(session_id))["steer"] if str(item.get("text") or "").strip()]
        await self._notify_sinks(
            session_id,
            HostEvent(
                type=HostEventType.STEER_CHANGED,
                run_id=(state.run_id if state is not None else "") or "",
                # ``count`` is the whole queue; ``queued`` is the first cards' worth of it.
                payload={"session_id": session_id, "reason": reason, "count": len(waiting), "queued": _steer_cards(waiting)},
            ),
        )

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
        """
        words = operator_words_metadata(origin, text, operator_words)
        if reply_to:
            # Rides with the operator's words on every copy of the message, so the app draws what it answers.
            words = {**words, "daedalus.reply_to": dict(reply_to)}
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if origin == "operator":
            # Writing to a session is having read it: the unread mark of its last result goes.
            try:
                await self.clear_unread(session_id)
            except Exception:  # noqa: BLE001 — a mark that stays is a dot too many, not a lost message
                logger.warning("could not clear the unread mark of session %s", session_id, exc_info=True)
        async with state.submit_lock:
            if not self.config.has_model:
                # The one place every way in converges: chat, the API, a schedule, a subagent, the
                # voice concierge. None of them can start a run without a model, and all of them
                # already turn a RuntimeError here into a message the operator reads.
                raise NoModelConfigured
            if self.shutting_down:
                raise RuntimeError("the bot is stopping; the run starts after the restart")
            if (self.settings.state_dir / "dependencies" / "maintenance").exists():
                raise RuntimeError("dependencies are being installed; wait for the application to restart")
            if self.recovering and not state.running:
                raise RuntimeError("the bot is starting up and first continues the runs it left behind; try again in a moment")
            if not state.workspace.is_dir():
                # One of ours is remade here: a folder under the managed tree is the installation's
                # to create, and a run refused because our own bookkeeping lost a directory is an
                # outage with nothing for the operator to do about it. A folder they pointed at is
                # theirs, so that one still refuses, with the path in the message.
                if home_of(state.metadata):
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
                queued = {**new_queued_prompt("follow_up", body).to_dict(), "origin": origin, "operator_words": None if operator_words is None else list(operator_words), "queued_at": datetime.now(UTC).isoformat()}
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
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": "follow_up", "daedalus.origin": origin, **words, "daedalus.queued": True, **({"daedalus.client_message_id": client_message_id} if client_message_id else {})})]
                )
                return state.run_id or ""
            provider_id: str | None = None
            if not state.running:
                if _provider_resume is not None:
                    provider_id = _provider_resume[1]
                    self.providers.get(provider_id)
                else:
                    try:
                        rungs, _ = self.resolve_model(await self.live.load(session_id))
                        provider_id = rungs[0][0].endpoint.id if rungs else None
                    except Exception:  # noqa: BLE001 — a model problem surfaces when the run starts, not here
                        provider_id = None
                exceeded = self.budget_exceeded()
                if exceeded and not self.provider_costs_nothing(provider_id):
                    raise RuntimeError(f"daily budget exceeded ({exceeded}); runs resume tomorrow or after /budget reset")
                breach = await self.cap_breach(state, provider_id)
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
                queued = {**new_queued_prompt(kind, placed).to_dict(), "origin": origin, "operator_words": said, "queued_at": datetime.now(UTC).isoformat()}
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
                    session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=body)], metadata={"daedalus.delivery": kind, "daedalus.origin": origin, **words, "daedalus.queued": True, **({"daedalus.client_message_id": client_message_id} if client_message_id else {})})]
                )
                if kind == "steer":
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
                    metadata={"daedalus.origin": origin, **words, **({"daedalus.client_message_id": client_message_id} if client_message_id else {}), **({"image_refs": [{"ref": ref, "mime": mime} for ref, mime in image_refs]} if image_refs else {})},
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
                image_refs.append((meta.ref, stored.mime))
        how = (
            "Read one with Files(op='read', file=…); pass them to a project with Delegate(files=[…])."
            if scope == MAIN_FILES
            else "Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])."
        )
        body = (text.strip() + "\n\n" if text.strip() else "") + "Attached files (kept by handle; " + how + "):\n" + "\n".join(lines)
        return body, image_refs

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
            lines.append(f"- {target} ({attachment.mime_type}, {target.stat().st_size} bytes)")
            if attachment.mime_type.startswith("image/"):
                meta = await self.blobs.put(TENANT, target.read_bytes(), content_type=attachment.mime_type)
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

    async def set_mode(self, session_id: str, mode: str | None) -> str:
        """Switch a session to a configured mode (limits, prompt rules, verbosity); empty = default."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        name = (mode or "").strip()
        if name and name not in self.config.modes:
            raise ValueError(f"no such mode {name!r}; configured: {', '.join(self.config.modes) or 'none'}")
        if name:
            state.metadata["mode"] = name
            state.session.metadata["mode"] = name
        else:
            state.metadata.pop("mode", None)
            state.session.metadata.pop("mode", None)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        self._apply_tool_visibility_all()  # a running child also inherits a parent's narrower mode from the next call on
        return name

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

    async def _yagni_note(self, state: SessionState) -> str:
        """The YAGNI note this turn has to carry, or ``""`` when the model already knows.

        The last note in the working history is what the model was told; none counts as off. A host
        compaction's summary leaves the turn context out, so after one the rules are restated while the
        mode is on, and a switch on and back off before any turn says nothing at all.
        """
        wanted = bool(state.metadata.get("yagni"))
        history = state.engine.history if state.engine is not None else await self.sessions.list_messages(state.session.id, TENANT, limit=10_000)
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

    def mode_for(self, state: SessionState) -> Any:
        name = str(state.metadata.get("mode") or "")
        if name:
            return self.config.modes.get(name)
        if self.is_orchestrator(state):
            # An orchestrator's turn hands work over and ends; its own limits bound one that does not,
            # without a mode the operator would have to remember to set.
            limits = self.config.orchestrator
            return ModeConfig(max_iterations=limits.max_iterations, usd_per_run=limits.usd_per_run, description="orchestrator")
        if self.is_dispatcher(state):
            bounds = self.config.dispatcher
            return ModeConfig(max_iterations=bounds.max_iterations, usd_per_run=bounds.usd_per_run, description="main orchestrator")
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
        # A continuation pins its model while holding this lock. Checking before acquiring it
        # let a concurrent override change the engine after the durable pin was written.
        async with state.submit_lock:
            await self._set_model_locked(session_id, **arguments)

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
        if rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id):
            # A local primary remains usable after a hosted-provider budget is exhausted. Paid
            # fallbacks do not inherit that exemption: when any applicable dollar guard is already
            # closed, keep only local rungs so a failed server cannot turn a free run into a charge.
            paid_blocked = self.budget_exceeded() is not None
            if not paid_blocked:
                for provider, _model in rungs[1:]:
                    if not self.provider_costs_nothing(provider.endpoint.id) and await self.cap_breach(state, provider.endpoint.id) is not None:
                        paid_blocked = True
                        break
            if paid_blocked:
                rungs = [(provider, model) for provider, model in rungs if self.provider_costs_nothing(provider.endpoint.id)]
        deps = EngineDeps(
            tool_registry=self.registry_for(state),
            event_stream=self.events,
            blob_store=self.blobs,
            skill_store=self.skills,
            hook_manager=self.hooks,
            bot_repo=self.settings.bot_repo_dir,
            core_repo=self.settings.core_repo_dir,
            governance_path=self.governance_path,
            github_org=self.settings.daedalus_github_org,
            ssh_config=Path.home() / ".ssh" / "config",
            policy_gate=self.policy_gate,
            selfdev_mode=self.capabilities.selfdev.mode,
            request_manifest_sink=self.request_manifests,
        )
        mode = self.mode_for(state)
        enabled = self.mcp_enabled(state)
        if enabled:
            # Re-establish connections for servers this session left enabled (e.g. after a
            # host restart or a dropped transport) so their tools are visible again instead
            # of silently missing until the agent re-runs McpEnable. Bounded: a dead or
            # slow server must never block the run.
            for server in enabled:
                try:
                    await asyncio.wait_for(self.mcp.ensure(server), timeout=5)
                except TimeoutError:
                    logger.warning("MCP warm-up for %s timed out; tools may be unavailable this run", server)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("MCP warm-up for %s failed; tools may be unavailable this run", server, exc_info=True)
        chain = build_chain(rungs, room=self.providers.room_for(self.config))
        # What this run was *asked* for. Everything the fallback notice says is measured against it,
        # so it is read once here, from the same rungs the chain is built on, and not re-derived later
        # from a configuration the operator may have changed while the run was going.
        state.configured_model = self._rung_label(rungs[0][0], rungs[0][1])
        # The primary's switch: a fallback runs on the surface the primary was given, as the tool limit
        # is not, because a surface too big is refused and one decided for another model is only worse.
        state.on_demand_groups = on_demand_tool_groups_for(preset, rungs[0][1]) if rungs else True
        state.effective_model = ""
        state.model_change_reason = ""
        state.model_stamps.clear()
        engine = build_engine(
            deps=deps,
            config=self.config,
            mode=mode,
            run_id=run_id,
            session_id=state.session.id,
            session_title=state.session.title,
            workspace=state.workspace,
            project=state.project.name if state.project is not None else "",
            rungs=rungs,
            provider_chain=chain,
            model_name=rungs[0][1],
            thinking=preset.thinking if overrides.get("thinking_enabled") is None else bool(overrides["thinking_enabled"]),
            reasoning_effort=overrides.get("reasoning_effort") or preset.reasoning_effort,
            context_window=state.context_window or preset.context_window,
            max_output_tokens=preset.max_output_tokens,
            extra_notes=self.notes_for(state),
            tool_visibility_policy=self.tool_policy_for(state),
            role="voice" if self.is_voice(state) else "orchestrator" if self.is_orchestrator(state) else "dispatcher" if self.is_dispatcher(state) else "agent",
            max_advertised_tools=self.advertised_tools_limit(rungs),
            discovered_tools=self.discovered_tools_for(state),
            loaded_tool_groups=self.loaded_groups_for(state),
            tool_group_loads=self.tool_group_loads_for(state) if not self.is_dispatcher(state) else None,
        )
        self._attach_hooks(engine, state)
        if chain is not None:
            # The fit check reads the prompt size the engine last saw; before the first call it is zero and every rung fits.
            chain.bind_prompt_size(lambda: int(getattr(engine, "last_observed_prompt_tokens", 0) or 0))
        return engine

    def _attach_hooks(self, engine: QueryEngine, state: SessionState) -> None:
        session_id = state.session.id

        async def reload_live_control(eng: QueryEngine) -> None:
            data = await self.live.load(session_id)
            steer = [item for item in data["steer"] if str(item.get("id") or "") not in state.steer_withdrawn]
            follow_up = list(data["follow_up"])
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
            consumed = [item_id for item_id in gone if item_id not in state.steer_withdrawn]
            state.steer_seen = [str(item.get("id") or "") for item in steer]
            state.follow_up_seen = [str(item.get("id") or "") for item in follow_up]
            state.steer_withdrawn.clear()
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
        current = {self.sessions.transcript_key(m) for m in history}
        known = set(previous_keys)
        for message in fresh if fresh is not None else history:
            if self.sessions.transcript_key(message) not in known:
                message.metadata.setdefault("daedalus.run_id", state.run_id)
        removed = [k for k in previous_keys if k not in current]
        self._stamp_model(state, fresh if fresh is not None else history, previous_keys)
        unlabelled = [i for i, m in enumerate(history) if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in m.metadata]
        if unlabelled:
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
                # Locate the live message by identity, never by position: the core may have
                # reshaped its history since this snapshot was taken.
                key = self.sessions.transcript_key(original)
                if state.engine is not None:
                    live = state.engine.history
                    for li, lm in enumerate(live):
                        if lm.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) and "daedalus.archived" not in lm.metadata and self.sessions.transcript_key(lm) == key:
                            live[li] = annotated
                            break
        await self.sessions.append_transcript(session_id, fresh if fresh is not None else history, from_history=True)
        # A round adds to the end of the history it was given; only a rewrite of the
        # sequence starts a generation, and that is not what a round does. ``fresh`` is
        # what the round added, when the caller knows; ``None`` means it does not, and
        # the stored history is brought up to this one the long way.
        if fresh is None:
            await self.sessions.sync_messages(session_id, TENANT, history)
        elif fresh:
            await self.sessions.append_messages(session_id, TENANT, fresh)

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
        state.soft_stop_cause = ""
        state.soft_stop_detail = ""
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
        switched_off = await self._take_mcp_switched_off(state)
        if switched_off:
            volatile.append("\n" + switched_off)
        loop_state = str(state.metadata.get("loop_state") or "").strip()
        if loop_state:
            volatile.append("\n" + loop_state)  # the loop's counter and next wake-up change every run
        yagni = await self._yagni_note(state)
        if yagni:
            volatile.append("\n- " + yagni)
        context = prompts.turn_context(notes="".join(volatile))
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
        return message.model_copy(update=update)

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
            # the next run — the voice concierge answering, a leader collecting a subagent — would
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
        some other kind of stop and is left alone.
        """
        if state.soft_stop_cause == CAUSE_MODEL_NO_PROGRESS:
            await self._dispatch_event(
                state,
                TurnEvent(type=EventType.ERROR, run_id=run_id, payload={
                    "kind": "model_no_progress",
                    "message": "The model repeatedly returned reasoning without an answer or a tool call; recovery attempts were exhausted.",
                }),
            )
            return True
        if state.soft_stop_cause != CAUSE_PROVIDER_ERROR or not state.soft_stop_detail:
            return False
        await self._dispatch_event(
            state,
            TurnEvent(
                type=EventType.ERROR,
                run_id=run_id,
                payload={"kind": "llm_provider_error", "message": self.PROVIDER_REFUSED_NOTE.format(detail=state.soft_stop_detail)},
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
            error_message=state.last_error_message or state.soft_stop_detail,
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
        # The transcript already holds each item as it was sent; this copy only opens the run and stays hidden.
        origin = "operator" if "operator" in origins else next(iter(origins))
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="\n\n".join(texts))], metadata={"daedalus.origin": origin, "daedalus.delivery": "drained", **_merged_words(items)})
        await self.sessions.append_transcript(state.session.id, [message])
        seqs = await self.sessions.transcript_seqs(state.session.id, [self.sessions.transcript_key(message)])
        await self.checkpoint(state, kind="before", seq=seqs[0] if seqs else None)
        await self._start_run(state, message)

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

        Two kinds of event feed this, and both are needed. The core's ``model_fallback_triggered``
        is the demotion itself: it names the rung the run moved to and why, and it is the only
        notice there is, because the retry re-opens the stream inside the assistant message that
        was already started — no second ``message_start`` announces it. And ``message_start`` is
        what catches every other way the model can differ from the configured one without a
        demotion ever being announced: a chain position restored from a snapshot, a model rebound
        by a live override.
        """
        payload = event.payload
        engine = state.engine
        if engine is None:
            return None
        if event.type is EventType.STATE_CHANGED and payload.get("reason") == "model_fallback_triggered":
            # ``from``/``to`` here are the loop's own state, not models; the rung is the other field.
            return self._observe_model(
                state,
                event.run_id,
                str(payload.get("fallback_model_id") or ""),
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
                    message.metadata.update(_merged_words(items))
                else:
                    # A placed prompt the queue no longer lists: its words are the operator's only
                    # when the operator sent it, and the text as placed is the best copy there is.
                    message.metadata.update(operator_words_metadata(origin, prompts.without_turn_context(message.text)))
                for client_message_id in placed:
                    if await self.live.receipt(state.session.id, str(client_message_id)) is not None:
                        await self.live.consume(
                            state.session.id,
                            str(client_message_id),
                            run_id=event.run_id,
                            step_id=str(event.payload.get("step_id") or "queue"),
                            message_seq=None,
                        )
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
        """Mask secrets the core's hook did not see: failure results, error text, tool arguments.

        A tool that returned normally was masked by the PostToolUse hook before its result
        entered the history. A tool that raised takes the dispatcher's failure path, which
        skips the hook, so its message is masked here — in the event every consumer sees and
        in the history message the core already appended.
        """
        p = event.payload
        if event.type is EventType.TOOL_RESULT:
            original = p.get("content")
            if not isinstance(original, str):
                original = "".join(
                    str(block.get("text") or "") for block in p.get("content_blocks") or ()
                    if isinstance(block, dict)
                )
            # The core sends normal results in content_blocks, and tools can attach UI payloads
            # and metadata. Mask the whole envelope before either durable event store sees it.
            cleaned = self._redact_tool_result_payload(p)
            p.clear()
            p.update(cleaned)
            content = p.get("content")
            if not isinstance(content, str):
                content = "".join(
                    str(block.get("text") or "") for block in p.get("content_blocks") or ()
                    if isinstance(block, dict)
                )
            if content != original:
                self._redact_history_result(state, str(p.get("tool_call_id")), content)
        elif event.type is EventType.TOOL_USE_STOP and isinstance(p.get("final_input"), dict):
            p["final_input"] = self.redactor.redact_any(p["final_input"])
        elif event.type is EventType.ERROR and isinstance(p.get("message"), str):
            p["message"] = self.redactor.redact(p["message"])
            state.last_error_kind = str(p.get("kind") or state.last_error_kind)
            state.last_error_message = p["message"]
            state.last_error_permanent = p.get("retryable") is False
            logger.warning("run error in session %s (%s): %s", getattr(getattr(state, "session", None), "id", "?"), p.get("kind") or "-", p["message"][:500])
        elif event.type is EventType.COMPACTION_COMPLETED:
            state.last_compaction = dict(p)
        elif event.type is EventType.STATE_CHANGED and p.get("reason") in RECOVERY_REASONS:
            if p.get("reason") == "soft_stop_notified":
                state.soft_stop_cause = str(p.get("soft_stop_cause") or "")
                state.soft_stop_detail = self.redactor.redact(str(p.get("soft_stop_detail") or ""))
            detail = {k: v for k, v in p.items() if k not in ("from", "to", "reason")}
            logger.warning("run recovery in session %s: %s %s", getattr(getattr(state, "session", None), "id", "?"), p.get("reason"), self.redactor.redact(json.dumps(detail, ensure_ascii=False, default=str)[:400]))

    def _redact_tool_result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mask credentials and the operator's home prefix before tool output is stored.

        A home-relative path remains useful for locating an artifact without recording the
        machine's account name in durable events or their exports.
        """
        home = str(Path.home())
        if home == "/":
            return self.redactor.redact_any(payload)
        home_path = re.compile(rf"(?<![A-Za-z0-9]){re.escape(home)}(?=[/\\]|\b)")

        def clean(value: Any) -> Any:
            if isinstance(value, str):
                return home_path.sub("~", value)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        return clean(self.redactor.redact_any(payload))

    @staticmethod
    def _redact_history_result(state: SessionState, tool_call_id: str, cleaned: str) -> None:
        engine = state.engine
        if engine is None:
            return
        for index in range(len(engine.history) - 1, -1, -1):
            message = engine.history[index]
            if message.role is not MessageRole.tool:
                continue
            blocks = list(message.content_blocks)
            changed = False
            for bi, block in enumerate(blocks):
                if isinstance(block, ToolResultBlock) and block.tool_call_id == tool_call_id:
                    blocks[bi] = block.model_copy(update={"content": cleaned})
                    changed = True
            if changed:
                engine.history[index] = message.model_copy(update={"content_blocks": blocks})
                return

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
        summaries = sum(1 for m in history if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY))
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
            "messages": len(history),
            "summaries": summaries,
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
    def is_voice(state: SessionState) -> bool:
        """Whether this is the voice session: the operator's spoken conversation with the concierge."""
        return bool(state.metadata.get("voice"))

    @staticmethod
    def is_staff(state: SessionState) -> bool:
        """Whether this session is a staff member's, working on a task of its project's team."""
        return bool(state.metadata.get("staff_session_id"))

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
        # The concierge talks and hands work over; it may not read, write or run anything itself, and the
        # tools that hand work over are its alone. Neither rule goes through a mode, so editing one cannot
        # give a session being spoken to a shell, nor give a working agent a second way to spawn one.
        blocked |= (known - set(VOICE_TOOLS)) if self.is_voice(state) else (known & set(VOICE_ONLY_TOOLS))
        # A staff member asks the orchestrator rather than the operator and works on its task rather
        # than starting agents, schedules or loops; its two reporting tools are its alone. Beside the
        # voice rule and for the same reason: no mode edit can hand either side the other's tools.
        blocked |= (known & set(STAFF_BLOCKED_TOOLS)) if self.is_staff(state) else (known & set(STAFF_ONLY_TOOLS))
        # A subagent reaches the operator through its leader, never past it. Its descendants inherit
        # this through the parent chain below, and a staff member's subagents through both rules.
        if state.metadata.get("subagent_of"):
            blocked |= known & set(SUBAGENT_BLOCKED_TOOLS)
        # An orchestrator runs a team and does none of the work: everything outside its allowlist is
        # off, and its control tools are nobody else's. Beside the two rules above, for their reason.
        blocked |= (known - set(ORCHESTRATOR_TOOLS)) if self.is_orchestrator(state) else (known & set(ORCHESTRATOR_ONLY_TOOLS))
        mode = self.mode_for(state)
        if mode is not None:
            if mode.tools_only:
                blocked |= known - {str(n) for n in mode.tools_only}
            for entry in mode.tools_off:
                name = str(entry)
                blocked |= {t for t in known if t.startswith(name[:-1])} if name.endswith("*") else {name}
        return blocked

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
        (voice, orchestrator, plan mode) arrive as refusals of everything outside them.
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

    def discovered_tools_for(self, state: SessionState) -> tuple[str, ...]:
        """The tools the session's last run loaded through ToolSearch and called that it may still call.

        Handed to the next run so it starts with them loaded rather than searching again, in the order
        they were loaded, so the tail of the tool list a prefix cache keys on comes back the same. The
        session's current policy decides, not the one they were loaded under: a tool switched off or
        refused to the role since then is not carried back.
        """
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
        """The groups the next run starts with loaded whole: what the last run carried, then what the operator
        asked for. Whole, because the core counts a group as one of the loaded tools it keeps: passed as
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
        """Remember what the finished run had loaded and used, and what it last said of its groups;
        ``True`` when that changed what is stored.

        A group it loaded whole and called a tool of is carried whole; otherwise only the tools it called
        are carried: a search loads its best three matches whether or not the model wanted them, and every
        unused near-miss carried along cost a definition on every request of every later run. A group
        seeded whole and then never used goes the same way. A tool seeded by name counts as called — an
        earlier run called it.
        """
        manager = engine.context_manager
        called = manager.called_discovered_tool_names()
        group_of = manager.discovered_tool_groups()
        used = {group_of[name] for name in called if name in group_of}
        groups = [g for g in manager.loaded_tool_group_names() if g in used]
        names = [name for name in called if group_of.get(name) not in used]
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
        """How each host group reaches this session's next run: the settings, its model, then its own choice."""
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
        carried = set(self.discovered_tools_for(state))
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
        run's engine takes the queue and empties it, and from then on the group is carried whole for as
        long as the runs call a tool of it.
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

    def notes_for(self, state: SessionState) -> str:
        """What the system prompt says about this session beyond the environment: the brief it was created with."""
        parts = [state.extra_notes.strip()] if state.extra_notes.strip() else []
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
        return "\n".join(parts)

    async def workspace_notes(self, state: SessionState) -> str:
        """What the workspace and the board say about the work in progress: read at every run start, so a run
        begins from the plan of record rather than from memory of it.

        ``AGENTS.md`` (or ``CLAUDE.md``) in the workspace root is the agent's own project memory; the open
        board tasks of this session are its plan. Both are volatile text: they change between runs, so
        they travel in the turn context at the end of the run's opening message, never in the system
        prompt, whose bytes must not change between runs if the provider is to serve them from cache.
        """
        parts: list[str] = []
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = state.workspace / name
            try:
                if path.is_file():
                    text = (await asyncio.to_thread(path.read_text, "utf-8", "replace")).strip()
                    if text:
                        if len(text) > WORKSPACE_NOTES_CHARS:
                            text = text[:WORKSPACE_NOTES_CHARS] + f"\n[… {name} continues; Read it for the rest]"
                        parts.append(f"- {name} in the workspace (your project memory; keep it current):\n{self.redactor.redact(text)}")
                    break
            except OSError:
                continue
        try:
            rows = await self.db.fetchall("SELECT id, title, status, priority FROM board_tasks WHERE session_id = ? AND status NOT IN ('done', 'cancelled') ORDER BY priority, updated_at DESC LIMIT 8", (state.session.id,))
        except Exception:  # noqa: BLE001 — the board is optional
            rows = []
        if rows:
            parts.append("- Your open board tasks (BoardGet for details; update them as you go):\n" + "\n".join(f"  - [{r['status']}] {r['id']}: {r['title']}" for r in rows))
        return ("\n" + "\n".join(parts)) if parts else ""

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
            policy = self.policy(base_dir=state.workspace if state is not None else self.workspace_for(session_id))
            now = time.time()
            grants = {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now} if state is not None else set()
            decision = policy.evaluate(tool, arguments, grants=grants)
            if decision.key and decision.action == "allow" and state is not None:
                self._consume_grant(state, decision.key)
            elif decision.key and state is not None:
                # The refusal's preimage, so an operator granting the key from chat sees what they approve.
                pending = dict(state.metadata.get("policy_pending") or {})
                fresh = decision.key not in pending
                pending[decision.key] = {"tool": tool, "text": self.redactor.redact(canonical(tool, arguments))[:300], "at": datetime.now(UTC).isoformat()}
                for meta in (state.metadata, state.session.metadata):
                    meta["policy_pending"] = dict(list(pending.items())[-20:])
                if fresh:
                    # Once per request: the agent retrying the same refused call is the same request.
                    self._publish_soon(state, "permission.pending", self._permission_payload(state, decision, pending[decision.key]))
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

        return PolicyAdapter(decide, ask_hint=ask_hint)

    def browser_gate(self, session_id: str, ask: Any) -> tuple[bool, str]:
        """A sensitive browser action of this session, asked about the way the policy asks.

        The policy judges a call from its arguments before it runs; whether a click buys something is
        known only once the browser has looked at the element, inside the tool. So the tool asks here,
        and the answer follows the policy's own rules: a grant for the key lets that one action through
        once, otherwise the request is recorded and announced once and the agent is refused with the
        key. It always goes to the operator — for a staff member too, whose other requests go to its
        orchestrator: money, messages and a person's accounts are the operator's to approve.
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
        said = f"{ask.action} “{ask.element}”" + (f" (the page calls it “{ask.name}”)" if ask.name and ask.name.casefold() not in ask.element.casefold() else "") + f" on {ask.origin}"
        pending[decision.key] = {"tool": ask.tool, "text": self.redactor.redact(said)[:300], "at": datetime.now(UTC).isoformat()}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_pending"] = dict(list(pending.items())[-20:])
        if fresh:
            payload = self._permission_payload(state, decision, pending[decision.key])
            # Answered in the app, never from a lock screen: whoever holds the phone should not be
            # the whole check on a purchase. The picture of the element rides along for the card.
            payload.update({"risk": "elevated", "quick": False, "routed_to": "operator", "browser": {"group_id": ask.group, "kinds": list(ask.kinds), "origin": ask.origin, "element": ask.element, "name": ask.name, "thumbnail": ask.thumbnail}})
            self._publish_soon(state, "permission.pending", payload)
        head = f"needs the operator's approval: {decision.reason} (rule {decision.rule}). Approval key: {decision.key}. "
        if self.is_staff(state):
            return False, head + prompts.STAFF_BROWSER_HINT
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
        # Answered, so off the open list: the same call refused again after the grant is spent is a
        # new request, and is announced as one.
        approves = self._drop_pending(state, key)
        until = time.time() + GRANT_TTL_SECONDS
        for meta in (state.metadata, state.session.metadata):
            grants = {k: v for k, v in (meta.get("policy_grants") or {}).items() if float(v) > time.time()}
            grants[key] = until
            meta["policy_grants"] = dict(list(grants.items())[-20:])
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"policy:{session_id}:{key}", "decision": "allow", "via": via})
        return {"key": key, "approves": approves, "grants": list(state.metadata["policy_grants"]), "expires_in_minutes": GRANT_TTL_SECONDS // 60}

    async def refuse(self, session_id: str, key: str, *, via: str) -> dict[str, Any]:
        """The operator leaves a refused call refused. Nothing changes for the agent, which was already
        told no; the request stops being open, and everyone showing it hears that it was answered."""
        state = await self.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        key = self._approval_key(key)
        refused = self._drop_pending(state, key)
        await self.sessions.update_metadata(session_id, state.session.metadata)
        await self._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"policy:{session_id}:{key}", "decision": "deny", "via": via})
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

    async def cap_breach(self, state: SessionState, provider_id: str | None) -> tuple[str, str] | None:
        """``(kind, note)`` when the session, its provider or everything together has spent its cap.

        Three caps stack above the per-run one: the session's own (session settings), the
        provider's total across every session (``limits.usd_total_per_provider``) and the grand
        total (``limits.usd_total``); the last two count from ``limits.total_since``.
        """
        if self.provider_costs_nothing(provider_id):
            # Dollar caps prevent another charge. A llama.cpp call is recorded at exactly zero, so
            # refusing it would turn a hosted-provider balance into an unrelated local outage.
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
        mode = self.mode_for(state)
        mode_cap = mode.usd_per_run if mode is not None else None
        run_cap = mode_cap if mode_cap is not None else self.config.limits.usd_per_run
        spent, unmetered = await self.spend(run_id=run_id)
        note: str | None = None
        kind = "run_cap"
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
        elif (run_cap > 0 or mode_cap is not None) and spent >= run_cap:
            source = (f"mode {state.metadata.get('mode')}" if state.metadata.get("mode") else "orchestrator.usd_per_run") if mode_cap is not None else "limits.usd_per_run"
            note = f"💸 per-run cap reached: ${spent:.2f} spent of ${run_cap:.2f} ({source}); stopping this run. Send a message to continue in a new run."
            if unmetered:
                note += f" {unmetered} call(s) had no known price and are not counted."
        else:
            last = await self.db.fetchone("SELECT provider_id FROM usage_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,))
            breach = await self.cap_breach(state, last["provider_id"] if last else None)
            if breach is not None:
                kind, note = breach[0], f"💸 {breach[1]}; stopping this run."
        if note is None:
            return
        self._capped_runs.add(run_id)
        logger.warning("run %s stopped (%s): %s", run_id, kind, note)
        state.engine.stop()
        await self._dispatch_event(state, TurnEvent(type=EventType.ERROR, run_id=run_id, payload={"message": note, "kind": kind}))

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
                        free = bool(rungs and self.provider_costs_nothing(rungs[0][0].endpoint.id))
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


SUMMARY_SECTIONS = ("Goal", "Constraints", "State", "Discoveries", "Open", "Next steps", "Unknowns", "Identifiers")

COMPACT_PROMPT = """Summarise the conversation transcript below so that an agent can continue the work \
in a fresh context. Write the summary in {language}, as Markdown with exactly these eight sections, \
each once, in this order, as level-2 headings whose titles stay in English verbatim whatever the \
language of the body: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers.
Goal: what was asked and why, with the success criteria. Constraints: rules, preferences and decisions \
the operator stated — quote them, do not paraphrase. State: what is done, with concrete results (paths, \
commands, numbers, decisions) — every identifier verbatim, never rounded or guessed. Discoveries: \
technical facts learned, errors and how they were resolved, approaches that failed and why. Open: what \
is in progress or untouched. Next steps: the exact actions to take next, in order. Unknowns: what the \
transcript does not show. The absence of a tool result or confirmation means the outcome is UNKNOWN, \
not that it did not happen or that it succeeded; put such items under Unknowns rather than asserting \
them. Identifiers: one line per path, id, URL, port, command, name or number that later work may need, \
each exactly as it appeared. Never include credentials or tokens. At most ~{max_words} words. No \
commentary outside the sections."""

CHUNK_PROMPT = """The text below is ONE PART of a longer conversation transcript (part {index} of {total}). \
Summarise this part in {language} with exactly these eight level-2 headings, each once, in this order, \
titles in English: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers. Keep every identifier (paths, ids, URLs, ports, commands, numbers) \
verbatim; quote operator rules rather than paraphrasing; an outcome the part does not show is UNKNOWN. \
Never include credentials or tokens. At most ~{max_words} words. No commentary outside the sections."""

MERGE_PROMPT = """Below are summaries of consecutive parts of one conversation, oldest first. Merge them \
into ONE summary in {language} with exactly these eight level-2 headings, each once, in this order, \
titles in English: ## Goal · ## Constraints · ## State · ## Discoveries · ## Open · ## Next steps · \
## Unknowns · ## Identifiers. Keep the latest known state of each thing and every identifier verbatim; \
keep every quoted operator rule; an item still unknown stays under Unknowns. At most ~{max_words} \
words. No commentary outside the sections."""

VERBATIM_TAIL_MESSAGES = 3
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


def _merged_words(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The mark for one message made of several queued items, each with its own origin."""
    words: list[str] = []
    for item in items:
        marked = operator_words_metadata(str(item.get("origin") or "operator"), str(item.get("text") or ""), item.get("operator_words"))
        words += marked[OPERATOR_WORDS_METADATA_KEY] or []
    return {OPERATOR_WORDS_METADATA_KEY: words or False}


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


def split_transcript(text: str, chunk_tokens: int) -> list[str]:
    """Consecutive parts of a rendered transcript, each about ``chunk_tokens`` (4 chars per token), split on line ends."""
    limit = max(1, chunk_tokens) * 4
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def transcript_for_summary(history: Sequence[Message], *, result_chars: int = 600) -> str:
    """A compact textual rendering of the history for the summariser."""
    lines: list[str] = []
    for message in history:
        if message.role is MessageRole.system or is_ledger(message):
            continue  # the ledger is carried beside the summary, not through it
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                text = prompts.without_turn_context(block.text).strip()  # the clock and the board of a past turn are not history
                if text:
                    lines.append(f"[{message.role.value}] {text[:4000]}")
            elif isinstance(block, ToolUseBlock):
                lines.append(f"[tool call] {block.name} {(block.arguments_json or '')[:300]}")
            elif isinstance(block, ToolResultBlock):
                body = (block.content or "").strip()
                if len(body) > result_chars:
                    body = body[: result_chars // 2] + " … " + body[-result_chars // 2 :]
                lines.append(f"[tool result{' ERROR' if block.is_error else ''}] {body}")
            elif isinstance(block, ThinkingBlock):
                continue
    return "\n".join(lines)[-200_000:]


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
    """The text of the last assistant turn that said anything, on one line."""
    for message in reversed(history):
        if message.role is not MessageRole.assistant:
            continue
        text = " ".join(block.text for block in message.content_blocks if isinstance(block, TextBlock)).strip()
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


__all__ = ["HOME_KEY", "Attachment", "PendingQuestion", "SessionManager", "SessionState", "WorkspaceUnreachable", "annotate_summary", "home_of", "transcript_for_summary", "validate_summary_sections", "verbatim_tail"]
