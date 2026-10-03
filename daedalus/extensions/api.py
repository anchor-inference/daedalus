"""HTTP API for the Mini App (FastAPI, served in-process by uvicorn)."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import fnmatch
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, quote, urlencode

import httpx
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from protocore.contracts.memory import MemoryScope
from protocore.contracts.types import ToolResultBlock, ToolUseBlock
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.websockets import WebSocket, WebSocketDisconnect

from daedalus.config import (
    NO_MODEL_MESSAGE,
    NOTIFICATION_CATEGORIES,
    PROVIDER_KINDS,
    HeartbeatConfig,
    ModelPresetConfig,
    NotificationsConfig,
    ProviderConfig,
    is_keyproxy_url,
    keyproxy_base,
    keyproxy_unresolved,
    keyproxy_upstream,
    on_demand_tool_groups_for,
)
from daedalus.doctor import DoctorContext, render_text, run_checks, summarize
from daedalus.extensions import (
    api_board,
    api_browsers,
    api_control,
    api_coordinator_authority,
    api_files,
    api_harnesses,
    api_integrations,
    api_knowledge,
    api_lifecycle,
    api_plugins,
    api_projects,
    api_runtime,
    api_skill_quality,
    api_staff,
    api_staff_reports,
    api_workflows,
    api_workspace_archive,
    launcher_updates,
)
from daedalus.extensions import commands as slash
from daedalus.extensions.heartbeat import TEMPLATE as HEARTBEAT_TEMPLATE
from daedalus.extensions.inbound import PAYLOAD_MAX_CHARS, flatten_payload, verify_signature, webhook_facts
from daedalus.extensions.notifications import ActionConflict, ActionRefused, Draft, NotificationService
from daedalus.extensions.push import PushRefused, PushService
from daedalus.extensions.review import ReviewRefused
from daedalus.extensions.services import SHARE_COOKIE_PREFIX, SHARE_MODES, pid_alive
from daedalus.extensions.voice import model_options, tts_configured
from daedalus.harness.capabilities import CAPABILITIES
from daedalus.host import capabilities, component_install, launcher_bridge
from daedalus.host import components as component_list
from daedalus.host.config_validation import UNKNOWN_REFERENCE_PATHS, ConfigConflict, config_revision, validate_candidate
from daedalus.host.dependencies import DependencyPlanner
from daedalus.host.events import EventFilter, event_stream, streamed_types
from daedalus.host.policy import sealed_root
from daedalus.host.presence import MAX_ID_LENGTH, MAX_PROJECTS, MAX_SESSIONS, MAX_TERMINALS, PresenceReport
from daedalus.host.prompt_changes import PromptChangePlanner
from daedalus.host.prompts import DEFAULT_RULES, without_turn_context
from daedalus.host.services import SCRATCH_DIR_NAME
from daedalus.host.session_runner import TENANT, Attachment, clip_title
from daedalus.host.transcript_view import full_tool_result, message_view
from daedalus.processes import end_tree
from daedalus.providers.llamacpp import discover_llamacpp
from daedalus.providers.openai_compat import UsageRecord
from daedalus.search.service import ConversationSearch, SearchBusy
from daedalus.security import redact
from daedalus.speech import catalog as speech_catalog
from daedalus.speech import models as speech_models
from daedalus.speech import service as speech_service
from daedalus.speech import tts_catalog
from daedalus.speech.engine import CACHE as ENGINE_CACHE
from daedalus.speech.engine import SAMPLE_RATE, SpeechError, clamp_rate
from daedalus.speech.service import note_transcribers, recogniser_available, transcribe_recording
from daedalus.speech.tts_engine import CACHE as VOICE_CACHE
from daedalus.speech.tts_engine import MAX_SPEED, MIN_SPEED, TtsError
from daedalus.speech.tts_service import MEDIA_TYPE_HEADER, SEQUENCE_TYPE
from daedalus.speech.tts_service import frame as speech_frame
from daedalus.staff_runtime import LiveSession
from daedalus.stores import pairing, passkeys
from daedalus.stores.control import Principal
from daedalus.stores.harness import HarnessStore
from daedalus.stores.media import MEDIA_TENANT
from daedalus.stores.projects import Project
from daedalus.stores.sqlite import ReceiptConflict, message_text
from daedalus.stores.staff import ACTIVE_STATUSES, HARNESSES, Staff, StaffBusy, StaffError
from daedalus.terminals.gateway import TERMINAL_WS_MAX_BYTES, Gateway, SocketGone, ticket_who
from daedalus.terminals.model import EnvUnavailable, TerminalError, TerminalSpec
from daedalus.terminals.model import Owner as TerminalOwner
from daedalus.tools import TOOL_GROUPS, websearch
from daedalus.transport.telegram.front import TelegramBusy, TelegramOutbox, TelegramRefused
from daedalus.transport.telegram.markdown import split_message
from daedalus.transport.telegram.voice import (
    TranscriptionError,
    asr_configured,
    effective_asr,
    voice_note_text,
)

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.config import RuntimeConfig
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

INIT_DATA_MAX_AGE = 24 * 3600
KEYPROXY_CACHE_SECONDS = 5.0
"""How long the key proxy's answer about its upstreams is reused. It is read once per app load and a
proxy that does not answer costs the whole timeout; a few seconds is well inside a first screen."""


def no_credential(kind: str, name: str) -> str:
    """Why an endpoint could not be listed, in the terms of what is actually missing.

    Written as a function rather than a table keyed by credential kind: a constant whose keys are
    ``api_key`` and the like is a dictionary of secret-named keys, and the redactor the agent reads
    its own source through masks the values under those.
    """
    if kind == "cli_login":
        return f"{name} is not signed in on this machine, so it cannot be asked what it serves. Sign in with its command-line tool and try again."
    if kind == "endpoint":
        return f"{name} did not answer, and it holds no credential to retry with."
    return f"{name} has no key in the key proxy, so it cannot be asked what it serves. Add one and restart, or add the endpoint and its own key below."


LOGIN_WIDGET_MAX_AGE = 24 * 3600
SESSION_COOKIE = "daedalus_session"
SESSION_TTL = 30 * 24 * 3600
VOICE_AUDIO_MAX = 25 << 20
"""One spoken utterance, not a recording session: anything larger is a mistake, not speech."""
VOICE_NOTE_MAX = 50 << 20
"""A composer voice note. The site sends 16 kHz WAV, so this is over twenty minutes of talking."""
KEPT_RECORDINGS = ".voice-notes"
"""Where, under a session's inbox, a voice note waits until its words are in the composer."""
KEPT_NAME = re.compile(r"[0-9a-f]{12}\.[a-z0-9]{1,5}")
"""What a kept recording is called; a name from the client that is not this is not looked up."""
KEPT_RECORDING_DAYS = 7
"""A kept recording nobody retried or discarded is let go after this long."""


def forget_old_recordings(kept: Path) -> None:
    """Delete kept voice notes older than :data:`KEPT_RECORDING_DAYS`, so the folder does not grow forever."""
    if not kept.is_dir():
        return
    horizon = time.time() - KEPT_RECORDING_DAYS * 86400
    for path in kept.iterdir():
        with suppress(OSError):
            if path.is_file() and path.stat().st_mtime < horizon:
                path.unlink()


VOICE_SAY_MAX_CHARS = 4000
"""One utterance in words. Dictation runs long, a pasted document is not speech: past this it is refused."""

LISTEN_CHUNK_MAX = 2 << 20
"""Largest PCM chunk one POST may carry — about a minute of 16 kHz mono. A page sends a fifth of a
second at a time; anything near this is a client that stopped streaming and started uploading."""

LISTEN_IDLE_SECONDS = 120.0
"""A listening stream nobody has fed for this long is abandoned and its engine handle released."""

LISTEN_MAX_STREAMS = 4
"""More open streams than this means a page that never closes them; the oldest idle one goes."""

STT_ENGINE_WAIT_SECONDS = 600.0
"""How long ``POST /api/stt/engine`` waits for the engine before it answers that it is still going.
The installer's own bound is longer, so without this the picker's request would be held past any
sensible answer and the caller could not tell a slow install from a stuck one."""

VOICE_TTS_MAX_CHARS = 2000
"""One sentence to read aloud. The page only ever sends what ``split_sentences`` cut, and the speech
endpoint is usually metered by the character, so an unbounded body is somebody else's bill."""


def validate_login_widget(data: dict[str, Any], bot_token: str, *, max_age: int = LOGIN_WIDGET_MAX_AGE) -> dict[str, Any]:
    """Verify what Telegram's Login Widget handed the page (id, first_name, …, auth_date, hash) and return it."""
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    received = str(data.get("hash") or "")
    if not received or not fields.get("id") or not fields.get("auth_date"):
        raise ValueError("incomplete login data")
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("bad signature")
    if time.time() - int(fields["auth_date"]) > max_age:
        raise ValueError("login data expired")
    return fields


def session_cookie_value(secret: bytes, user_id: int, *, ttl: int = SESSION_TTL) -> str:
    """A signed, expiring statement that the browser holding it is the owner."""
    payload = base64.urlsafe_b64encode(json.dumps({"uid": int(user_id), "exp": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session_cookie(secret: bytes, value: str) -> int | None:
    """The user id a session cookie vouches for, or None when it is forged or expired."""
    payload, _, signature = (value or "").partition(".")
    if not payload or not signature:
        return None
    if not hmac.compare_digest(hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest(), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    if "uid" not in data:
        return None
    # Zero is the owner of an installation with no Telegram account behind it, so it is an answer
    # like any other: only a forged or expired cookie has none.
    return int(data["uid"])


def validate_init_data(init_data: str, bot_token: str, *, max_age: int = INIT_DATA_MAX_AGE) -> dict[str, Any]:
    """Verify Telegram Mini App ``initData`` and return its fields."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("missing hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("bad signature")
    auth_date = int(pairs.get("auth_date", "0"))
    if time.time() - auth_date > max_age:
        raise ValueError("initData expired")
    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


class PasskeyRegisterBody(BaseModel):
    credential: dict[str, Any]
    """What ``navigator.credentials.create`` returned, serialised as the browser gives it."""
    name: str = Field(default="", max_length=80)
    ceremony: str = Field(default="", max_length=64)
    """The id the matching ``begin`` call answered with: it names the challenge this response signs."""


class PasskeyLoginBody(BaseModel):
    credential: dict[str, Any]
    """What ``navigator.credentials.get`` returned, serialised as the browser gives it."""
    ceremony: str = Field(default="", max_length=64)


class ReplyTo(BaseModel):
    seq: int
    excerpt: str = Field(default="", max_length=600)


class SendMessageBody(BaseModel):
    text: str
    steer: bool = False
    client_message_id: str = Field(default="", max_length=64)
    reply_to: ReplyTo | None = None
    """What in the chat the message answers: a report, an event line, a reply of the agent's."""


class VoiceSayBody(BaseModel):
    text: str


class VoiceSpeakBody(BaseModel):
    text: str
    turn: str = ""
    """Which answer this sentence belongs to, as the ``say`` event named it. The page sends it back so
    the wait between the words and the sound can be attributed to the turn that waited; a request
    without one is still spoken, it is simply not timed."""


class VoiceModelBody(BaseModel):
    """Which model preset the concierge answers with; empty means the default one."""

    preset: str = ""


class AnswerBody(BaseModel):
    answers: list[dict[str, Any]]


class PresenceBody(BaseModel):
    """One window's account of itself: whether it is seen, and what it shows."""

    client: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["browser", "pwa", "telegram", "window"] = "browser"
    visible: bool
    focused: bool
    sessions: list[str] = []
    terminals: list[str] = []
    projects: list[str] = []
    screen: str = Field("", max_length=64)
    lang: str = Field("", max_length=16)
    tz: str = Field("", max_length=64)


class SpaFiles(StaticFiles):
    """The built app with its screens in the URL: a path that is not a file is the app itself."""

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise


class HireBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str = ""
    harness: str = "daedalus"
    agent: str = ""
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    env: str = ""
    folder_id: str | None = None
    isolation: str | None = None
    """Omitted: an own worktree where the folder is a git repository, the shared folder otherwise."""
    instructions: str = ""
    one_off: bool = False
    color: str = ""


class StaffPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    env: str | None = None
    folder_id: str | None = None
    """An empty string clears it: the member then works in the project's primary folder."""
    isolation: str | None = None
    instructions: str | None = None
    notes: str | None = None
    color: str | None = None


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    resume_from: str | None = None
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1, strict=True)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_worktree: bool = True


class AskAnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: bool | None = None
    always: bool = False
    server: bool = False
    """With ``always``: every tool of the asked tool's MCP server, not the one tool."""
    text: str | None = None
    selected: list[str] | None = None
    window: Literal["main", "project"] | None = None
    """Which chat of the app the card was answered in, so every other window can say where."""


class TeamReportBody(BaseModel):
    kind: str
    note: str
    artifacts: list[str] | None = None
    remember: str | None = None
    evidence: list[dict[str, str]] | None = None
    acknowledged: list[str] | None = None
    operator_steps: dict[str, Any] | None = None


class TeamAskBody(BaseModel):
    question: str
    options: list[str] | None = None
    context: str = ""


class NewSessionBody(BaseModel):
    title: str = ""
    """Empty when the chat is started by its first message; a clip of that message stands in."""
    autotitle: bool = False
    """Name the chat from its first message. A chat given a title here is left as the operator named it."""
    prompt: str | None = None
    project_id: str | None = None
    """The project to work in: its folder becomes the session's workspace and the limit of its reach."""
    folder_id: str | None = None
    """One of the project's folders to work in; empty is its primary folder."""
    own_directory: bool = False
    """Work in a private child of the project rather than its shared root."""
    tools_off: list[str] = Field(default_factory=list)
    """Tools this session does not get (by name); everything else stays on."""
    preset: str | None = None
    """The model preset the session starts on; empty = the global default."""
    loop: LoopBody | None = None
    """Make it a loop agent: woken up for this instruction on an interval or when it says so."""


class MoveSessionBody(BaseModel):
    project_id: str
    """The project to move the session into."""
    own_directory: bool = False
    """Use a private child inside the destination project rather than its shared root."""


class ToolsOffBody(BaseModel):
    tools_off: list[str] = Field(default_factory=list)


class ToolGroupLoadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    load: Literal["eager", "auto", "lazy"] | None = None
    """``None`` goes back to the layer below: the default for the settings, the settings for a session."""
    base_revision: str | None = None
    """The configuration revision the Settings screen was showing, for the installation's mode: a
    change made in another window since is refused rather than overwritten. A session's mode is not
    configuration and ignores it."""


class MemoryBody(BaseModel):
    text: str = Field(min_length=1)
    kind: str = "fact"
    scope: str = "global"
    scope_key: str = ""


class MemoryPatch(BaseModel):
    text: str | None = None
    kind: str | None = None


class MemoryDeleteBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class ShareBody(BaseModel):
    mode: str = Field(pattern="^(local|public|key)$")
    rotate_key: bool = False


class LoopBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    mode: str = "interval"
    interval_minutes: int | None = Field(default=None, ge=1)
    max_runs: int | None = Field(default=None, ge=1)
    start_now: bool = True


class LoopActionBody(BaseModel):
    action: str
    reason: str = ""


class DecisionBody(BaseModel):
    decision: str
    reason: str = ""


class ScheduleBody(BaseModel):
    name: str
    prompt: str
    cron: str | None = None
    run_at: str | None = None
    model: str | None = None
    kind: str = "agent"
    target_session: str | None = None
    run_in: str = "new"


class SchedulePatchBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    run_at: str | None = None
    enabled: bool | None = None
    model_config = {"extra": "forbid"}


class NotificationsSeenBody(BaseModel):
    """Which notifications the operator has seen: the listed ids, every one, or every one of a session."""

    ids: list[int] | None = Field(default=None, max_length=1000)
    all: bool = False
    session_id: str | None = Field(default=None, max_length=64)
    model_config = {"extra": "forbid"}


class NotificationActBody(BaseModel):
    """One of a notification's actions: ``allow``, ``deny``, ``answer:<i>``, ``open``, or ``answer`` with the words.

    From a lock screen the service worker has no sign-in of its own; it sends the ``token`` that came
    inside the pushed message instead, and names its own subscription ``endpoint`` so the
    withdrawal that follows the answer skips the device that gave it.
    """

    action: str = Field(min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=4000)
    via: Literal["notification", "push"] = "notification"
    token: str | None = Field(default=None, max_length=200)
    endpoint: str | None = Field(default=None, max_length=1024)
    model_config = {"extra": "forbid"}


class PushKeysBody(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)
    model_config = {"extra": "ignore"}


class PushSubscriptionBody(BaseModel):
    """What ``PushSubscription.toJSON()`` gives, plus a name for the device. Unknown keys (``expirationTime``) are ignored."""

    endpoint: str = Field(min_length=1, max_length=1024)
    keys: PushKeysBody
    device: str = Field(default="", max_length=80)
    model_config = {"extra": "ignore"}


class PushRenewBody(PushSubscriptionBody):
    """A subscription the browser replaced by itself; the old one's secret proves who is asking."""

    old_endpoint: str = Field(min_length=1, max_length=1024)
    old_auth: str = Field(min_length=1, max_length=100)


class NotificationPreferencesBody(BaseModel):
    """The whole ``[notifications]`` section, and the revision of the configuration it was read from."""

    preferences: dict[str, Any]
    base_revision: str
    model_config = {"extra": "forbid"}


class HeartbeatBody(BaseModel):
    text: str | None = Field(default=None, max_length=20_000)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5)
    active_hours: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    preset: str | None = None
    max_runs_per_day: int | None = Field(default=None, ge=1)


class RenameBody(BaseModel):
    title: str | None = None
    archived: bool | None = None


class RevertBody(BaseModel):
    seq: int


class InboundBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    sender: str = Field(default="local", max_length=100)
    session: str | None = None
    """Session id or exact title; empty = the standing '[inbound]' session."""
    prompt: str = ""


class PeerBody(BaseModel):
    name: str
    session_id: str


class CommandBody(BaseModel):
    line: str = Field(min_length=2, max_length=4000)


class BriefBody(BaseModel):
    brief: str = Field(default="", max_length=12_000)


class SessionCapBody(BaseModel):
    usd_cap: float | None = None
    """None removes the session's own cap; the global limits still apply."""


class ModeBody(BaseModel):
    mode: str | None = None
    """A configured mode name; null or "" = default behaviour."""


class YagniBody(BaseModel):
    on: bool


class ForkBody(BaseModel):
    seq: int
    title: str | None = None


class CompactBody(BaseModel):
    instructions: str = ""


class SettingsBody(BaseModel):
    base_revision: str | None = None
    model: dict[str, Any] | None = None
    prompt: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    mcp: dict[str, Any] | None = None
    self_change: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    """max_iterations, tool_timeout_seconds and usd_per_run; the daily cap is the supervisor's."""
    balance: dict[str, Any] | None = None
    scheduler: dict[str, Any] | None = None
    telegram: dict[str, Any] | None = None
    asr: dict[str, Any] | None = None
    modes: dict[str, Any] | None = None
    webhooks: dict[str, Any] | None = None
    ops: dict[str, Any] | None = None
    compaction: dict[str, Any] | None = None
    orchestrator: dict[str, Any] | None = None
    """The project orchestrator's defaults: its model preset, its wake-up batching and its limits."""
    dispatcher: dict[str, Any] | None = None
    """The main orchestrator: its model preset, its wake-up batching, when a dispatch counts as stalled."""
    terminals: dict[str, Any] | None = None
    """``running_cap`` from Settings; the terminals service reads the configuration on every admission."""
    browser: dict[str, Any] | None = None
    """Settings → Browser: the cap, the idle close, recording, watch mode, the local network the
    browser may be let into, the injection monitor. The browser service hands the daemon its part at
    once (``limits.set``, ``net.configure``)."""
    answer_language: str | None = None


class SettingsValidationBody(BaseModel):
    base_revision: str
    candidate: SettingsBody


class ConversationSearchBody(BaseModel):
    mode: Literal["off", "local"] = "off"
    paused: bool = False


class SttSelectBody(BaseModel):
    """Choosing a local speech model. Every field is optional; what is sent is what changes."""

    model: str | None = None
    """A catalog id, or "" to stop using a local model."""
    language: str | None = None
    threads: int | None = Field(default=None, ge=1, le=16)


class TtsSelectBody(BaseModel):
    """Choosing a local voice. Every field is optional; what is sent is what changes."""

    voice: str | None = None
    """A catalog id, or "" to stop speaking here and go back to the endpoint and the browser."""
    speaker: str | None = None
    """A named speaker inside a multi-speaker voice; ignored by the single-voice ones."""
    speed: float | None = Field(default=None, ge=MIN_SPEED, le=MAX_SPEED)
    threads: int | None = Field(default=None, ge=1, le=16)


class SearchCheckBody(BaseModel):
    """The Mini App's "check" button for the WebSearch backends."""

    backend: str = ""
    """One backend to try on its own; empty = the configured backend and its fallbacks."""
    query: str = "searxng json api"


class ProviderPatch(BaseModel):
    """Partial edit of one configured provider endpoint (see ``apply_provider_patch``)."""

    kind: str | None = None
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    """Omitted = keep the stored key; "" or null = clear it; any other value = store it."""
    timeout_seconds: float | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    """Sampling temperature for this endpoint. ``null`` clears a pin and returns to the host default."""
    pricing: dict[str, dict[str, Any]] | None = None
    """Per-model USD per 1M tokens; a subscription-backed endpoint sets zeros so its runs are metered, not unknown."""


class ModelsLookupBody(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    provider: str | None = None
    """A configured client id: probe its stored base_url with its stored key (keys stay server-side)."""


class PresetPatch(BaseModel):
    label: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None
    reasoning_effort: str | None = None
    images: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    on_demand_tool_groups: bool | None = None
    """Sent as null, it goes back to what the model is known for; the one field where null is a value."""


PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NO_MODEL_LABEL = "no model"
"""Where a model name would go in a list or a chip and there is none yet."""


def resolve_model_patch(current: dict[str, Any], patch: dict[str, Any]) -> None:
    """Merge a ``model`` settings patch (default preset id and/or fallback chain) in place."""
    presets = current.get("presets") or {}
    merged = dict(current.get("model", {}))
    if "preset" in patch:
        preset_id = str(patch.get("preset") or "").strip()
        if preset_id not in presets:
            raise HTTPException(400, f"no such model preset {preset_id!r}")
        merged["preset"] = preset_id
    if "chain" in patch:
        chain = [str(c) for c in (patch.get("chain") or []) if str(c) in presets and str(c) != merged.get("preset")]
        merged["chain"] = chain
    current["model"] = merged


def per_million(pricing: Any) -> dict[str, float]:
    """A price per token (what OpenRouter publishes) as a price per million (what everything here uses)."""
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, float] = {}
    for name, key in (("input", "prompt"), ("output", "completion"), ("cache_hit", "input_cache_read")):
        try:
            value = float(pricing[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0:
            out[name] = round(value * 1_000_000, 6)
    return out


def model_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """One model as the Add-a-model list shows it: whatever the endpoint chose to say about it.

    Only ``id`` is ever there — a plain OpenAI-compatible ``/models`` says nothing else, and the
    operator fills the rest in by hand. OpenRouter carries the context window, the modalities,
    whether the model reasons and what a million tokens cost; those prefill the form instead.
    """
    entry: dict[str, Any] = {"id": str(raw.get("id") or "").strip()}
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        entry["name"] = name.strip()
    top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    context = raw.get("context_length") or top.get("context_length")
    if isinstance(context, int | float) and context > 0:
        entry["context_length"] = int(context)
    max_output = top.get("max_completion_tokens")
    if isinstance(max_output, int | float) and max_output > 0:
        entry["max_output_tokens"] = int(max_output)
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    modalities = architecture.get("input_modalities")
    if isinstance(modalities, list):
        entry["input_modalities"] = [str(m) for m in modalities]
        entry["images"] = "image" in entry["input_modalities"]
    parameters = raw.get("supported_parameters")
    if isinstance(parameters, list):
        entry["reasoning"] = "reasoning" in parameters or "reasoning_effort" in parameters
    pricing = per_million(raw.get("pricing"))
    if pricing:
        entry["pricing"] = pricing
    return entry


def _said(response: httpx.Response) -> str:
    """The endpoint's own error sentence, in parentheses, or nothing when it did not write one."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    text = str(message or "").strip()
    return f" ({text[:160]})" if text else ""


async def lookup_openai_models(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """List the model ids served at an OpenAI-compatible ``/models`` endpoint.

    Tries ``{base}/models`` first, then ``{base}/v1/models``, so a base_url typed
    without the ``/v1`` prefix (``http://host:9000``) still resolves; the returned
    ``base_url`` is the exact root the list was found at. ``models`` is the bare list of
    ids; ``entries`` is the same list with whatever else the endpoint said about each one
    (see :func:`model_entry`). Tests inject an
    ``httpx.AsyncClient`` with a mock transport; production uses its own short-timeout
    client. Raises :class:`ValueError` when nothing answers with a model list.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base or not re.match(r"^https?://", base, re.IGNORECASE):
        raise ValueError("base_url must be an http(s) URL")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    errors: list[str] = []
    owns_client = client is None
    if client is None:
        # No redirects: the base URL is the operator's and the request carries their key. httpx
        # drops the header across origins, so this is defence in depth — and a models endpoint that
        # answers with a redirect is a misconfiguration worth seeing rather than following.
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    try:
        for url in candidates:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc.__class__.__name__}")
                continue
            if response.status_code != 200:
                # The status alone made a missing key read as a vendor whose /models path had moved;
                # whatever the endpoint said about it is the sentence that names the real fix.
                errors.append(f"{url}: HTTP {response.status_code}{_said(response)}")
                continue
            try:
                data = response.json()
            except ValueError:
                errors.append(f"{url}: not JSON")
                continue
            listed = data.get("data") if isinstance(data, dict) else None
            rows = [m for m in listed if isinstance(m, dict) and str(m.get("id") or "").strip()] if isinstance(listed, list) else []
            if not rows:
                errors.append(f"{url}: no model list in the response")
                continue
            entries = [model_entry(m) for m in rows]
            return {"base_url": url[: -len("/models")], "models": [e["id"] for e in entries], "entries": entries}
    finally:
        if owns_client:
            await client.aclose()
    raise ValueError("; ".join(errors[:3]) or "no response")


def extend_model_list(result: dict[str, Any], extra: list[str]) -> dict[str, Any]:
    """Append ids the endpoint did not name, after its own and without repeating one.

    The Codex proxy's ``/models`` is a short fallback plus whatever its usage table has already
    metered, so a model the CLI lists and nobody has called yet never arrives. The harness check
    already stored that list; this is where Add a model hears about it.
    """
    have = set(result.get("models") or [])
    models = list(result.get("models") or [])
    entries = list(result.get("entries") or [])
    for model in extra:
        name = str(model or "").strip()
        if not name or name in have:
            continue
        have.add(name)
        models.append(name)
        entries.append({"id": name})
    result["models"] = models
    result["entries"] = entries
    return result


def apply_provider_patch(providers: dict[str, Any], provider_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial patch into a config-style ``providers`` dict (in place; returns it).

    ``patch`` is the ``exclude_unset`` dump of :class:`ProviderPatch` — only the keys the
    client actually sent. Unknown or null fields are skipped; ``api_key`` is special-cased
    so an absent key never overwrites a stored one. Unknown ids create a new endpoint
    (defaulting to the generic OpenAI-compatible kind).
    """
    entry = dict(providers.get(provider_id) or {"kind": "openai_compat"})
    for key, value in patch.items():
        if key == "api_key":
            entry[key] = value or ""
        elif key == "temperature":
            # ``null`` is how the Mini App clears a pin; skipping None would make the field sticky.
            entry[key] = value
        elif value is not None and key in ProviderConfig.model_fields:
            entry[key] = value
    providers[provider_id] = entry
    return providers


def mask_provider_keys(settings_view: dict[str, Any]) -> dict[str, Any]:
    """Never echo stored credentials back to the Mini App.

    Provider keys become an ``api_key_set`` flag; MCP header and env values (the same
    values the redactor masks everywhere else) are shown as the mask and, when sent back
    unchanged, keep their stored value (see :func:`restore_masked_mcp`).
    """
    for entry in (settings_view.get("providers") or {}).values():
        entry["api_key_set"] = bool(entry.get("api_key"))
        entry["api_key"] = ""
    for server in ((settings_view.get("mcp") or {}).get("servers") or {}).values():
        for section in ("headers", "env"):
            values = server.get(section)
            if isinstance(values, dict):
                server[section] = {k: (redact.MASK if v else v) for k, v in values.items()}
    asr = settings_view.get("asr") or {}
    if isinstance(asr, dict):
        asr["api_key_set"] = bool(asr.get("api_key"))
        asr["api_key"] = ""
    for hook in (settings_view.get("webhooks") or {}).values():
        if isinstance(hook, dict):
            hook["secret_set"] = bool(hook.get("secret"))
            hook["secret"] = ""
    settings_view["provider_kinds"] = list(PROVIDER_KINDS)
    return settings_view


def restore_masked_secrets(current: dict[str, Any], dumped: dict[str, Any]) -> None:
    """An empty or masked secret sent back by the Mini App means "keep what is stored"."""
    asr = dumped.get("asr")
    if isinstance(asr, dict) and not asr.get("api_key") and "api_key" in asr:
        asr["api_key"] = (current.get("asr") or {}).get("api_key", "")
    hooks = dumped.get("webhooks")
    if isinstance(hooks, dict):
        for name, hook in hooks.items():
            if isinstance(hook, dict) and not hook.get("secret") and "secret" in hook:
                hook["secret"] = ((current.get("webhooks") or {}).get(name) or {}).get("secret", "")


def restore_masked_mcp(current: dict[str, Any], patch: dict[str, Any]) -> None:
    """A masked MCP header/env value sent back by the Mini App means "keep what is stored"."""
    stored = ((current.get("mcp") or {}).get("servers") or {})
    for name, server in ((patch.get("servers") or {}).items()):
        if not isinstance(server, dict):
            continue
        for section in ("headers", "env"):
            values = server.get(section)
            if not isinstance(values, dict):
                continue
            kept = (stored.get(name) or {}).get(section) or {}
            for key, value in list(values.items()):
                if value == redact.MASK:
                    if key in kept:
                        values[key] = kept[key]
                    else:
                        del values[key]


def _deep_merge(base: Any, patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        out[key] = _deep_merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


WEBHOOK_MAX_BYTES = 2 * 1024 * 1024

GZIP_MIN_BYTES = 1024
"""Responses smaller than this go out as they are: compressing them costs more than it saves."""

EVENT_TYPES_MAX = 32
"""Types or prefixes one ``/api/events`` request may name; a real client names a handful."""
EVENT_TYPE_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)*\.?$")

MAX_TRANSCRIPT_PAGE = 2000
"""Turns one request may ask for. Beyond this a client is asking for a session, not a page."""

FILE_SEARCH_SKIP = frozenset({".git", "__pycache__", "node_modules", ".venv", ".checkpoints", ".mypy_cache", ".ruff_cache", ".pytest_cache"})
"""Folders the explorer never shows and neither search walks into; the tree hides the same names."""

FILE_SEARCH_MAX_ENTRIES = 20_000
"""Paths one name search may look at. A tree larger than this answers from its first part of it, and says so."""

FILE_SEARCH_MAX_RESULTS = 200
"""Hits one search answers with. The box that asks is a filter, not a report."""

FILE_SEARCH_BUDGET_SECONDS = 0.2
"""Wall-clock one name search may spend. It answers a key press, so a slow disk truncates rather than waits."""

FILE_GREP_BUDGET_SECONDS = 1.0
"""Wall-clock one content search may spend: reading files is the slower of the two, and still bounded."""

FILE_GREP_MAX_FILESIZE = "1M"
"""Files larger than this are not read for a snippet — as ripgrep spells a size."""

FILE_GREP_MAX_COLUMNS = 300
"""How much of a matching line comes back. A minified bundle is one line and nobody wants all of it."""

FILE_SEARCH_WORKERS = 4
"""Threads the two searches share. Their own pool, because a filter box typed into over a large tree
would otherwise park one of the process's general-purpose threads per key press."""

FILE_SEARCH_WATCH_TICK_SECONDS = 0.02
"""How often the watchdog over a search subprocess looks at its budget and at the caller's flag."""


_search_threads: ThreadPoolExecutor | None = None


def _search_pool() -> ThreadPoolExecutor:
    """The searches' own small pool, made on first use.

    Nothing else runs on it: a search that overruns can only ever queue behind another search, never
    behind — or in front of — the rest of the process's blocking work.
    """
    global _search_threads
    if _search_threads is None:
        _search_threads = ThreadPoolExecutor(max_workers=FILE_SEARCH_WORKERS, thread_name_prefix="file-search")
    return _search_threads


def _end_search(process: subprocess.Popen[str]) -> None:
    """End a search's ripgrep, and anything it started, at once.

    The pipe the reader waits on is held open by every process that inherited it, so killing only
    the one that was started can leave the read blocked on a child of it. The searches give their
    subprocess a session of its own precisely so that the whole of it can be ended here.
    """
    try:
        end_tree(process.pid, hard=True)
    except (OSError, ValueError):
        pass
    process.kill()


def _kill_after(process: subprocess.Popen[str], budget: float, cancel: threading.Event | None) -> Callable[[], None]:
    """Kill ``process`` once ``budget`` is spent or ``cancel`` is raised; the returned call ends the watch.

    A search that matches nothing writes no line at all, so the reader blocks in the pipe and no
    deadline inside the loop around it is ever reached. The bound has to sit on the process rather
    than on the loop: killing it ends the read by EOF, and the loop's own truncation logic then runs
    exactly as it does when a bound on results or entries is the one that was hit.
    """
    done = threading.Event()

    def watch() -> None:
        end = time.monotonic() + budget
        while not done.wait(FILE_SEARCH_WATCH_TICK_SECONDS):
            if time.monotonic() >= end or (cancel is not None and cancel.is_set()):
                _end_search(process)
                return

    threading.Thread(target=watch, name="file-search-watchdog", daemon=True).start()
    return done.set


async def _run_search(work: Callable[..., Any], *args: Any) -> Any:
    """Run one blocking search on the searches' own pool and stop it when the caller gives up.

    A thread cannot be cancelled, but it can be told: the flag handed to the search ends its walk at
    the next entry and kills its ripgrep within a tick, so a browser that moved on does not leave a
    tree walk running behind it.
    """
    cancel = threading.Event()
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_search_pool(), work, *args, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise


_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Self-development", ("Self", "LearningReport")),
    ("Agents & peers", ("SubAgent", "SpawnAgent", "AskPeer", "PeerList")),
    ("Scheduling & board", ("Schedule", "Board", "Intent", "StaySilent", "Loop")),
    ("Services", ("Service",)),
    ("MCP", ("Mcp",)),
    ("Memory & history", ("Remember", "Recall", "Forget", "History", "Skill")),
    ("Web", ("Web",)),
    ("Files & shell", ("Exec", "Job", "Read", "Write", "Edit", "MultiEdit", "Find", "Search", "SendFile", "AttachMedia", "ImageView", "Verify")),
)


def _tool_group(name: str) -> str:
    for group, prefixes in _TOOL_GROUPS:
        if any(name.startswith(p) for p in prefixes):
            return group
    return "Other"


class TerminalCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: Literal["container", "host"]
    owner_kind: Literal["session", "staff", "project", "free"]
    owner_id: str | None = Field(default=None, max_length=128)
    project_id: str | None = Field(default=None, max_length=128)
    cwd: str | None = Field(default=None, max_length=4096)
    title: str = Field(default="", max_length=200)
    sandbox: bool = False
    cols: int = Field(default=80, ge=20, le=500)
    rows: int = Field(default=24, ge=4, le=300)
    confirm: bool = False
    """The operator's yes to "the machine already runs as many terminals as the cap allows"."""


class TerminalPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    owner_kind: Literal["session", "staff", "project", "free"] | None = None
    owner_id: str | None = Field(default=None, max_length=128)


class TerminalSignalBody(BaseModel):
    signal: Literal["INT", "TERM", "HUP", "KILL", "QUIT", "TSTP", "CONT", "WINCH", "USR1", "USR2"]


class TerminalKeyboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Literal["auto", "human"]
    """``auto`` gives the keyboard back, so the messages that wait for a person to stop typing go in
    now; ``human`` holds it. Handing it to the agent is the agent's own business, not a button's."""


class TerminalTicketBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_only: bool = False


class TerminalDaemonUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class TerminalSocket:
    """The framework's WebSocket as the terminal relay's ``FrameSocket``."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive(self) -> bytes | str | None:
        try:
            message = await self.websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("bytes") is not None:
            return bytes(message["bytes"])
        return str(message.get("text") or "")

    async def send(self, frame: bytes) -> None:
        try:
            await self.websocket.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise SocketGone(str(exc)) from None

    async def close(self, code: int, reason: str = "") -> None:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.websocket.close(code, reason)


class TerminalRestartBody(BaseModel):
    sandbox: bool | None = None
    confirm: bool = False


def build_app(app: Application, api_token: str) -> FastAPI:
    dependency_planner = DependencyPlanner(app)
    prompt_change_planner = PromptChangePlanner(app)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await dependency_planner.close()
            await prompt_change_planner.close()

    api = FastAPI(title="Daedalus", docs_url=None, redoc_url=None, lifespan=lifespan)
    # A session page is JSON and compresses about fivefold; over a phone connection that is the
    # difference the operator feels. The event stream is excluded by content type, so a token
    # still leaves the process the moment it arrives.
    api.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)

    # The desktop launcher gives each start of the supervisor an id, and the bot inherits it. Echoing
    # it on the answers to /app and /app/ — what the launcher's health check asks for — is how the
    # launcher tells this process's answer from one by a stack a crashed launcher left running on the
    # same port. It is a random value per start, not a secret.
    boot_id = app.settings.boot_id

    @api.middleware("http")
    async def shared_dialog_pages_stay_out_of_indexes(request: Request, call_next: Any) -> Any:
        # The address bar ends on /app/c/<slug>, which is the app shell. The shell is a static file,
        # so the noindex has to be added on the way out; the dialog itself is fetched separately.
        response = await call_next(request)
        # Only on the two answers the launcher's health check reads (/app, and /app/ it redirects to).
        # Nothing public carries it — the shared pages under /c/, /s/ and /app/c/ are read by people
        # outside, who have no use for when the stack restarted.
        if boot_id and request.url.path in ("/app", "/app/"):
            response.headers["X-Daedalus-Boot"] = boot_id
        if request.url.path.startswith("/app/c/"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["Cache-Control"] = "no-store"
        return response

    @api.exception_handler(TerminalError)
    async def terminal_refusal(_: Request, exc: TerminalError) -> JSONResponse:
        # ``detail`` stays the sentence the app already shows for any refusal; ``code`` and the
        # details beside it are what a screen that acts on the refusal reads (the cap's confirmation).
        return JSONResponse({"detail": exc.message, "code": exc.code, **exc.details}, status_code=exc.status)

    manager = app.manager
    assert manager is not None
    settings = app.settings
    # The manager resolved this on the way up; resolving it again here costs nothing and keeps the
    # routes buildable around a stand-in manager (the auth tests build the app without one).
    caps = getattr(manager, "capabilities", None) or capabilities.resolve(settings, app.config)
    # The installer is the application's, so that an install outlives the request that started it.
    # A stand-in application built by a test that cares about something else gets one of its own
    # rather than a missing attribute in the middle of an unrelated route.
    installer = getattr(app, "components", None) or component_install.Installer(settings)
    secret_cache: dict[str, bytes] = {}
    secret_lock = asyncio.Lock()

    async def session_secret() -> bytes:
        """What signs the browser's session cookie.

        The API token, so that rotating it invalidates every cookie, plus a secret minted once for
        this installation, so that the token alone is not enough to forge one. Not the bot token:
        an installation without Telegram has none, and its cookies must still mean something.
        """
        async with secret_lock:  # two first requests at once must not mint two secrets
            if "value" not in secret_cache:
                stored = await app.db.kv_get("session_secret")
                if not stored:
                    stored = secrets.token_urlsafe(32)
                    await app.db.kv_set("session_secret", stored)
                secret_cache["value"] = hashlib.sha256(f"session:{api_token}:{stored}".encode()).digest()
            return secret_cache["value"]

    async def revoke_sessions() -> None:
        """Every browser session ends at once: a new secret signs the cookies from here on."""
        async with secret_lock:
            await app.db.kv_set("session_secret", secrets.token_urlsafe(32))
            secret_cache.clear()

    def over_https(request: Request) -> bool:
        """Whether the browser reached us over TLS.

        A cookie marked secure is never sent back over plain http, which is exactly how the app is
        opened on the machine itself; marking it unconditionally locks that case out. TLS is either
        on this connection or on the proxy the public address names; a forwarded-proto header is not
        consulted, because any client can send one.
        """
        return request.url.scheme == "https" or settings.miniapp_public_url.lower().startswith("https://")

    async def sign_in(response: Response, request: Request) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_cookie_value(await session_secret(), settings.owner_user_id),
            max_age=SESSION_TTL,
            httponly=True,
            secure=over_https(request),
            samesite="lax",
            path="/",
        )

    async def auth(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if header.startswith("tma "):
            if not settings.telegram_bot_token:
                raise HTTPException(401, "this installation has no Telegram bot")
            try:
                data = validate_init_data(header[4:], settings.telegram_bot_token)
            except ValueError as exc:
                raise HTTPException(401, f"invalid initData: {exc}") from exc
            user = data.get("user") or {}
            if int(user.get("id", 0)) != settings.owner_user_id:
                raise HTTPException(403, "not the owner")
            return {"user_id": settings.owner_user_id, "via": "telegram"}
        token = request.headers.get("x-daedalus-token")
        if not token and request.url.path.endswith("/download"):
            token = request.query_params.get("token")  # browser navigation cannot set headers
        if token and secrets.compare_digest(token, api_token):
            return {"user_id": settings.owner_user_id, "via": "token"}
        # A browser that paired, signed in with a passkey or used Telegram's widget holds a signed cookie.
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and verify_session_cookie(await session_secret(), cookie) == settings.owner_user_id:
            return {"user_id": settings.owner_user_id, "via": "cookie"}
        raise HTTPException(401, "authentication required")

    # -- auth: the site outside Telegram ---------------------------------------------

    bot_username: dict[str, str] = {}

    async def _bot_username() -> str | None:
        """The bot whose Login Widget vouches for the operator; None when there is none to ask."""
        if bot_username.get("name"):
            return bot_username["name"]
        front = app.front
        if front is None or getattr(front, "bot", None) is None:
            return None
        try:
            me = await front.bot.get_me()
        except Exception:  # noqa: BLE001 — an unreachable bot is one way in missing, not a broken login page
            logger.warning("could not read the bot's username", exc_info=True)
            return None
        bot_username["name"] = str(me.username or "")
        return bot_username["name"] or None

    def _relying_party() -> passkeys.RelyingParty:
        return passkeys.relying_party(settings.miniapp_public_url, settings.api_port)

    # Both ceremonies are a pair of calls, and the challenge of the first has to survive until the
    # second. Each begin names its ceremony, so two browsers (or a stranger polling the public login
    # endpoint) cannot spend each other's challenge; the table stays small by dropping what expired.
    CEREMONY_TTL, CEREMONY_MAX = 300.0, 64
    challenges: dict[str, tuple[str, bytes, float]] = {}

    def _hold_challenge(kind: str, options: dict[str, Any]) -> str:
        now = time.time()
        for key in [k for k, (_, _, until) in challenges.items() if until < now]:
            challenges.pop(key, None)
        while len(challenges) >= CEREMONY_MAX:
            challenges.pop(next(iter(challenges)))  # the oldest goes; a flood cannot grow the table
        ceremony = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64decode(options["challenge"] + "=" * (-len(options["challenge"]) % 4))
        challenges[ceremony] = (kind, challenge, now + CEREMONY_TTL)
        return ceremony

    def _take_challenge(kind: str, ceremony: str) -> bytes:
        held = challenges.pop(ceremony, None) if ceremony else None
        if held is None or held[0] != kind or held[2] < time.time():
            raise HTTPException(400, "the request expired; start again")
        return held[1]

    @api.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        """What the login page needs: which ways in this installation actually has."""
        username = await _bot_username()
        return {
            "telegram": {"bot_username": username} if username else None,
            "passkeys": await passkeys.count(app.db),
            "pairing": await pairing.outstanding(app.db) > 0,
        }

    @api.post("/api/auth/telegram")
    async def auth_telegram(body: dict[str, Any], request: Request, response: JSONResponse) -> dict[str, Any]:
        """Telegram's Login Widget result: verified with the bot token, accepted only for the owner, answered with a session cookie."""
        if not settings.telegram_bot_token:
            raise HTTPException(503, "this installation has no Telegram bot")
        try:
            fields = validate_login_widget(body, settings.telegram_bot_token)
        except ValueError as exc:
            raise HTTPException(401, f"login refused: {exc}") from exc
        if int(fields.get("id", 0)) != settings.owner_user_id:
            raise HTTPException(403, "not the owner")
        await sign_in(response, request)
        return {"ok": True, "user_id": settings.owner_user_id, "name": fields.get("first_name")}

    @api.get("/api/auth/pair")
    async def auth_pair(code: str, request: Request) -> RedirectResponse:
        """A pairing link: spend the code, hand the browser a session cookie and open the app."""
        if not await pairing.redeem(app.db, code, state_dir=settings.state_dir):
            # A spent link still lands on the app: a browser that paired with it earlier is signed in
            # already, and one that is not sees the login page say why.
            return RedirectResponse("/app/?pairing=spent", status_code=303)
        response = RedirectResponse("/app/", status_code=303)
        await sign_in(response, request)
        return response

    @api.post("/api/auth/passkeys/register/begin")
    async def passkey_register_begin(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        options = await passkeys.registration_options(app.db, _relying_party())
        return {**options, "ceremony": _hold_challenge("register", options)}

    @api.post("/api/auth/passkeys/register/finish")
    async def passkey_register_finish(body: PasskeyRegisterBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            verified = passkeys.verify_registration(body.credential, challenge=_take_challenge("register", body.ceremony), rp=_relying_party())
        except Exception as exc:  # noqa: BLE001 — every failure here is the same answer: this key is not accepted
            raise HTTPException(400, f"the passkey was not accepted: {exc}") from exc
        if passkeys.declined_resident_key(body.credential):
            # Login offers no list of credentials (no username to type), so a key the authenticator
            # did not make discoverable could be enrolled and then never offered at sign-in.
            raise HTTPException(400, "this authenticator did not make the passkey discoverable, so it could not sign you in later; use a device or key that stores passkeys")
        transports = list(body.credential.get("response", {}).get("transports") or [])
        await passkeys.store(
            app.db,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=int(verified.sign_count),
            transports=[str(x) for x in transports],
            name=body.name.strip() or "This device",
        )
        return {"ok": True, "passkeys": await passkeys.listing(app.db)}

    @api.post("/api/auth/passkeys/login/begin")
    async def passkey_login_begin() -> dict[str, Any]:
        if await passkeys.count(app.db) == 0:
            raise HTTPException(404, "no passkey is enrolled")
        options = await passkeys.authentication_options(app.db, _relying_party())
        return {**options, "ceremony": _hold_challenge("login", options)}

    @api.post("/api/auth/passkeys/login/finish")
    async def passkey_login_finish(body: PasskeyLoginBody, request: Request, response: JSONResponse) -> dict[str, Any]:
        stored = await passkeys.find(app.db, str(body.credential.get("id") or ""))
        if stored is None:
            raise HTTPException(403, "this passkey is not enrolled here")
        try:
            verified = passkeys.verify_authentication(
                body.credential,
                challenge=_take_challenge("login", body.ceremony),
                rp=_relying_party(),
                public_key=base64.urlsafe_b64decode(stored["public_key"] + "=" * (-len(stored["public_key"]) % 4)),
                sign_count=int(stored["sign_count"]),
            )
        except Exception as exc:  # noqa: BLE001 — a refused signature is a refused login, whatever went wrong
            raise HTTPException(403, f"the passkey was refused: {exc}") from exc
        await passkeys.used(app.db, stored["credential_id"], int(verified.new_sign_count))
        await sign_in(response, request)
        return {"ok": True, "user_id": settings.owner_user_id}

    @api.get("/api/auth/passkeys")
    async def passkey_list(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await passkeys.listing(app.db)

    @api.delete("/api/auth/passkeys/{passkey_id}")
    async def passkey_delete(passkey_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await passkeys.remove(app.db, passkey_id):
            raise HTTPException(404, "no such passkey")
        return {"ok": True, "passkeys": await passkeys.listing(app.db)}

    @api.post("/api/auth/sessions/revoke")
    async def auth_revoke(request: Request, response: JSONResponse, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Sign out everywhere: every cookie issued so far stops working, this browser gets a fresh one."""
        await revoke_sessions()
        if who.get("via") == "cookie":
            await sign_in(response, request)
        return {"ok": True}

    @api.get("/api/auth/me")
    async def auth_me(who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"user_id": who["user_id"], "via": who.get("via", "token")}

    @api.post("/api/auth/logout")
    async def auth_logout(response: JSONResponse) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # The projects, their folders, brief and journal: their own module, which the features built
    # on projects extend rather than this file.
    from daedalus.extensions.api_orchestrator_domain import (
        install_routes,  # Lazy: route composition follows the authentication dependency.
    )

    install_routes(api, app, auth)
    api_board.register(api, app, auth)
    api_control.register(api, app, auth)
    api_coordinator_authority.register(api, app, auth)
    api_projects.register(api, app, auth)
    api_knowledge.register(api, app, auth)
    api_runtime.register(api, app, auth)
    api_lifecycle.register(api, app, auth)
    api_staff_reports.register(api, app, auth)
    api_workflows.register(api, app, auth)
    api_workspace_archive.register(api, app, auth)
    api_plugins.register(api, app, auth)
    api_skill_quality.register(api, app, auth)
    api_integrations.register(api, app, auth)
    # The command-line agents: the Harnesses screen and the hiring form's catalog.
    api_harnesses.register(api, app, auth)
    # One staff member's session as its runtime sees it: the staff view's reads.
    api_staff.register(api, app, auth)
    # The files orchestration keeps by handle: the cards a chat draws, their bytes, the audit.
    api_files.register(api, app, auth)
    # The agent's browser: its groups, the live view's ticket and socket, control, the audit.
    api_browsers.register(api, app, auth)

    # -- staff: the named members of a project's team ------------------------------------------

    def staff_row(member: Staff, live: Any, sessions: int, health: dict[str, Any] | None = None) -> dict[str, Any]:
        queue = getattr(app.extensions.get("staff"), "queue", None)
        # What the member waits to start, each with why: "queued" alone is the question the
        # operator would then have to ask.
        queued = queue.waiting_for(member.id) if queue is not None else []
        return {**member.view(), "live": live.view() if live is not None else None, "status": live.status if live is not None else "off", "sessions": sessions, "queued": queued, "health": health}

    async def staff_health(member: Staff, session: Any) -> dict[str, Any] | None:
        """Whether the host still hears a working member, for its card; nothing without a live session."""
        team = app.extensions.get("staff")
        if session is None or team is None or not hasattr(team, "health"):
            return None
        return (await team.health(LiveSession(member, session))).view()  # type: ignore[no-any-return]

    def team_or_503() -> Any:
        team = app.extensions.get("staff")
        if team is None:
            raise HTTPException(503, "the staff runtime is not running")
        return team

    async def team_call(call: Awaitable[Any]) -> Any:
        """A team operation with its refusals as the HTTP answers the app shows as they are."""
        try:
            return await call
        except KeyError as exc:
            raise HTTPException(404, f"no such {exc.args[0] if exc.args else 'thing'}") from exc
        except StaffBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except StaffError as exc:
            raise HTTPException(409 if type(exc).__name__ == "AlreadyAnswered" else 400, str(exc)) from exc

    async def staff_project(project_id: str) -> Project:
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        return project

    async def staff_member(staff_id: str) -> Staff:
        member = await manager.staff.get(staff_id)
        if member is None:
            raise HTTPException(404, "no such staff member")
        return member

    async def staff_changed(member: Staff, change: str) -> None:
        # The team page of another window, and later the orchestrator, learn of it from the stream.
        await manager.bus.publish("project.changed", {"change": change, "actor": "operator"}, project_id=member.project_id, staff_id=member.id)

    @api.get("/api/projects/{project_id}/staff")
    async def list_staff(project_id: str, archived: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The team, with each member's live session, and what the hiring form offers for Daedalus staff.

        The personas and presets travel with the team because nothing else serves the personas, and a
        form that has to wait on three requests before it can draw a choice is a form drawn twice.
        What the command-line agents offer is the harness catalog's, asked for separately.
        """
        project = await staff_project(project_id)
        members = await manager.staff.list(project_id, archived=archived)
        live = await manager.staff.live_sessions(project_id)
        counts = await manager.staff.session_counts(project_id)
        presets = manager.config.presets
        default = manager.config.default_preset()
        orchestrator = project.settings.orchestrator
        return {
            "project": {
                "id": project.id,
                "name": project.name,
                "ephemeral": project.settings.ephemeral,
                "system": project.settings.system,
                "default_env": project.settings.default_env or manager.projects.local_env,
                "local_env": manager.projects.local_env,
                "concurrency": orchestrator.concurrency,
                "concurrency_cap": orchestrator.concurrency_cap,
                "orchestrator": orchestrator.enabled,
                "folders": [{"id": f.id, "path": str(f.path), "label": f.label, "env": f.env, "is_git": f.is_git, "readonly": f.readonly} for f in project.folders],
            },
            "staff": [staff_row(m, live.get(m.id), counts.get(m.id, 0), await staff_health(m, live.get(m.id))) for m in members],
            "queue": queue.queue(project_id) if (queue := getattr(app.extensions.get("staff"), "queue", None)) is not None else [],
            "counts": {"staff": sum(1 for m in members if m.active), "working": sum(1 for s in live.values() if s.status in ACTIVE_STATUSES)},
            "choices": {
                "harnesses": list(HARNESSES),
                "personas": manager.staff.personas(),
                "presets": [{"id": pid, "label": preset.display(pid)} for pid, preset in presets.items()],
                "default_preset": default[0] if default else "",
            },
        }

    @api.post("/api/projects/{project_id}/staff", status_code=201)
    async def hire_staff(project_id: str, body: HireBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        project = await staff_project(project_id)
        harness_manager = app.extensions.get("harness")
        if body.harness != "daedalus" and harness_manager is not None and body.harness in CAPABILITIES:
            # A CLI the last check found missing (or of a major Daedalus cannot run) is refused here
            # rather than at the first launch, where the orchestrator would be the one to find out.
            problem = await harness_manager.hire_problem(body.env or project.settings.default_env or manager.projects.local_env, body.harness)
            if problem:
                raise HTTPException(400, problem)
        isolation = body.isolation
        if isolation is None:
            folder = project.folder(body.folder_id) if body.folder_id else (project.folders[0] if project.folders else None)
            isolation = "worktree" if folder is not None and folder.is_git and not folder.readonly else "shared"
        try:
            member = await manager.staff.hire(
                project_id,
                name=body.name, role=body.role, harness=body.harness, agent=body.agent, model=body.model, effort=body.effort,
                permission_mode=body.permission_mode, env=body.env, folder_id=body.folder_id or None, isolation=isolation,
                instructions=body.instructions, one_off=body.one_off, color=body.color, created_by="operator",
            )
        except StaffError as exc:
            raise HTTPException(400, str(exc)) from exc
        await staff_changed(member, "staff.hired")
        row = staff_row(member, None, 0)
        warn_of = getattr(harness_manager, "hire_warning", None) if body.harness != "daedalus" else None
        row["warning"] = str(await warn_of(body.env or project.settings.default_env or manager.projects.local_env, body.harness) or "") if warn_of is not None and body.harness in CAPABILITIES else ""
        return row

    @api.get("/api/staff/{staff_id}")
    async def get_staff(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        member = await staff_member(staff_id)
        live = await manager.staff.live(staff_id)
        return staff_row(member, live, len(await manager.staff.sessions(staff_id, limit=500)), await staff_health(member, live))

    @api.patch("/api/staff/{staff_id}")
    async def patch_staff(staff_id: str, body: StaffPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await staff_member(staff_id)
        changes = body.model_dump(exclude_unset=True)
        if "folder_id" in changes:
            changes["default_folder_id"] = changes.pop("folder_id")
        try:
            member = await manager.staff.update(staff_id, **changes)
        except StaffError as exc:
            raise HTTPException(400, str(exc)) from exc
        await staff_changed(member, "staff.updated")
        live = await manager.staff.live(staff_id)
        return staff_row(member, live, len(await manager.staff.sessions(staff_id, limit=500)))

    @api.delete("/api/staff/{staff_id}")
    async def dismiss_staff(staff_id: str, release: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Dismiss a member. Refused while a session of theirs is live, unless ``release`` asks the
        staff runtime to end it first — which only the runtime can, since only it knows what is running."""
        member = await staff_member(staff_id)
        live = await manager.staff.live(staff_id)
        if live is not None:
            runtime = app.extensions.get("staff")
            if not release:
                raise HTTPException(409, f"{member.name} is working; release the session first")
            if runtime is None or not hasattr(runtime, "release"):
                raise HTTPException(409, f"{member.name} has a live session and nothing here can end it yet")
            await runtime.release(member, keep_worktree=True)
        try:
            member = await manager.staff.archive(staff_id, by="operator")
        except StaffBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        await staff_changed(member, "staff.dismissed")
        return {"ok": True, "staff": member.view()}

    @api.get("/api/staff/{staff_id}/sessions")
    async def staff_sessions(staff_id: str, limit: int = 50, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        await staff_member(staff_id)
        return [s.view() for s in await manager.staff.sessions(staff_id, limit=limit)]

    @api.get("/api/staff/{staff_id}/resume-sessions")
    async def staff_resume_sessions(staff_id: str, task_id: str | None = None, before: str | None = None, limit: int = Query(default=50, ge=1, le=100), _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        team = team_or_503()
        member = await staff_member(staff_id)
        return await team_call(team.resume_sessions(member, task_id=task_id, before=before, limit=limit))  # type: ignore[no-any-return]

    @api.get("/api/staff/{staff_id}/messages")
    async def staff_messages(staff_id: str, limit: int = Query(default=20, ge=1, le=200), before: str | None = None, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """What was sent to a member, newest first, with each message's delivery state and, for a
        command-line member, how it was delivered (``delivery``: paste or pointer, Enters sent, when
        each state was reached). ``before`` is a message id: the page continues below it."""
        await staff_member(staff_id)
        messages = list(await manager.staff.messages(staff_id, limit=500 if before else limit))
        if before:
            ids = [m.id for m in messages]
            messages = messages[ids.index(before) + 1 :] if before in ids else []
        page = messages[:limit]
        deliveries = await HarnessStore(manager.db).deliveries([m.id for m in page])
        rows = []
        for message in page:
            row = message.view()
            delivery = deliveries.get(message.id)
            if delivery is not None:
                row["delivery"] = dataclasses.asdict(delivery)
            rows.append(row)
        return rows

    @api.post("/api/staff/{staff_id}/assign")
    async def assign_staff(staff_id: str, body: AssignBody, authenticated: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Commit a scoped launch command before the runtime attempts any work."""
        team = team_or_503()
        member = await staff_member(staff_id)
        return await team_call(team.assign(member, body.task_id, principal=Principal.operator(authenticated),
                                          client_operation_id=body.client_operation_id,
                                          expected_entity_revision=body.expected_entity_revision,
                                          resume_from=body.resume_from))  # type: ignore[no-any-return]

    @api.post("/api/staff/{staff_id}/interrupt")
    async def interrupt_staff(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        team = team_or_503()
        member = await staff_member(staff_id)
        await team_call(team.interrupt(member))
        return {"ok": True}

    @api.post("/api/staff/{staff_id}/pause")
    async def pause_staff(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        team = team_or_503()
        member = await staff_member(staff_id)
        return await team_call(team.pause(member))  # type: ignore[no-any-return]

    @api.post("/api/staff/{staff_id}/release")
    async def release_staff(staff_id: str, body: ReleaseBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        team = team_or_503()
        member = await staff_member(staff_id)
        released = await team_call(team.release(member, keep_worktree=body.keep_worktree if body is not None else True))
        return {"released": bool(released)}

    @api.get("/api/asks")
    async def list_asks(project: str, open: bool = True, routed_to: Literal["orchestrator", "operator"] | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The project's requests; ``routed_to=operator`` is "Needs you"."""
        await staff_project(project)
        if open:
            asks = await manager.asks.open_for(project, routed_to)
        else:
            rows = await manager.db.fetchall("SELECT id FROM asks WHERE project_id = ? ORDER BY created_at DESC LIMIT 200", (project,))
            asks = [a for a in [await manager.asks.get(r["id"]) for r in rows] if a is not None]
        return {"asks": [a.view() for a in asks]}

    @api.post("/api/asks/{ask_id}/answer")
    async def answer_ask(ask_id: str, body: AskAnswerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's answer. The first answer to reach a request wins; a later one is a 409 naming who was first."""
        team = team_or_503()
        return await team_call(team.answer(ask_id, allow=body.allow, always=body.always, server=body.server, text=body.text, selected=body.selected, by="operator", via=body.window or "app"))  # type: ignore[no-any-return]

    async def team_live(staff_session_id: str, request: Request) -> Any:
        """A command-line member's team server, authenticated by the token minted for its launch —
        not by the operator's credentials, which the member never holds."""
        team = team_or_503()
        try:
            return team, await team.authenticate(staff_session_id, request.headers.get("x-daedalus-team-token", ""))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc

    @api.post("/api/team/{staff_session_id}/report")
    async def team_report(staff_session_id: str, body: TeamReportBody, request: Request) -> dict[str, Any]:
        team, live = await team_live(staff_session_id, request)
        try:
            told = await team.ingress.report(live, body.kind, body.note, body.artifacts, body.remember, evidence=body.evidence, acknowledged=body.acknowledged, operator_steps=body.operator_steps)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "text": told}

    @api.post("/api/team/{staff_session_id}/ask")
    async def team_ask(staff_session_id: str, body: TeamAskBody, request: Request) -> dict[str, Any]:
        team, live = await team_live(staff_session_id, request)
        try:
            ask_id = await team.ingress.ask(live, body.question, body.options, body.context)
        except StaffError as exc:
            raise HTTPException(400, str(exc)) from exc
        ask = await manager.asks.get(ask_id)
        return {"ask_id": ask_id, "short_id": ask.short_id if ask else "", "text": "Asked. End your turn: the answer arrives as a message."}

    # -- sessions -------------------------------------------------------------------

    async def conversation_search() -> ConversationSearch:
        service = getattr(app, "search", None)
        if service is None:
            service = ConversationSearch(app.db, settings.state_dir, manager=manager)
            app.search = service
        await service.load()
        return service

    @api.get("/api/conversation-search/settings")
    async def search_settings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await (await conversation_search()).status()

    @api.put("/api/conversation-search/settings")
    async def search_configure(body: ConversationSearchBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await (await conversation_search()).configure(body.mode, body.paused)

    @api.post("/api/conversation-search/model")
    async def search_download(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = await conversation_search()
        service.downloads.start("multilingual-e5-small")
        return await service.status()

    @api.post("/api/conversation-search/model/cancel")
    async def search_cancel(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = await conversation_search()
        service.downloads.cancel("multilingual-e5-small")
        return await service.status()

    @api.delete("/api/conversation-search/model")
    async def search_delete(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = await conversation_search()
        await service.configure("off", True)
        await service.downloads.delete("multilingual-e5-small")
        return await service.status()

    @api.get("/api/sessions/search")
    async def search_sessions(q: str = Query(min_length=1, max_length=500), project: str = Query(default="", max_length=128),
                              limit: int = Query(default=30, ge=1, le=50), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = await conversation_search()
        try:
            found = await service.query(q, project=project, limit=limit)
        except SearchBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        hits = {h["session_id"]: h for h in found.pop("hits")}
        listing = await session_listing(list(hits))
        for row in listing["sessions"]:
            row["match"] = hits[row["id"]]
        listing["sessions"].sort(key=lambda row: -row["match"]["score"])
        listing["projects"] = [p for p in listing["projects"] if any(s["project_id"] == p["id"] for s in listing["sessions"])]
        return {**listing, **found, "indexing": service.settings["mode"] == "local" and bool((await service.status())["pending"])}

    @api.get("/api/sessions/{session_id}/search")
    async def search_session(session_id: str, q: str = Query(min_length=1, max_length=500), limit: int = Query(default=30, ge=1, le=50),
                             _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Search inside one conversation: each matching message by its seq, which the chat opens at
        (``#m<seq>``). A message queued while a turn ran is found at the copy the chat shows, once."""
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        service = await conversation_search()
        try:
            found = await service.query(q, limit=limit, session=session_id)
        except SearchBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        hits = []
        for hit in found.pop("hits"):
            rows = await manager.sessions.expand_transcript(session_id, int(hit["seq"]), int(hit["seq"]))
            if not rows or rows[0][1].metadata.get("daedalus.queued"):
                continue  # the copy where the model read it is the one the chat shows
            message = rows[0][1]
            hits.append({"seq": hit["seq"], "snippet": hit.get("snippet", ""), "role": message.role.value, "at": message.created_at.isoformat(), "origin": message.metadata.get("daedalus.origin")})
        hits.sort(key=lambda h: -int(h["seq"]))
        return {"hits": hits, **found}

    @api.get("/api/sessions")
    async def list_sessions(
        cursor: str = Query(default="", max_length=512),
        limit: int = Query(default=30, ge=1, le=100),
        view: Literal["all", "attention", "working", "archive"] = "all",
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        return await session_listing(cursor=cursor, limit=limit, view=view)

    async def session_listing(
        ids: list[str] | None = None,
        *,
        cursor: str = "",
        limit: int = 30,
        view: Literal["all", "attention", "working", "archive"] = "all",
    ) -> dict[str, Any]:
        if ids is not None:
            rows = await manager.list_sessions(limit=200, ids=ids)
            next_cursor = None
        else:
            catalog = await manager.session_catalog()
            visible = [row for row in catalog if (
                row["archived"] if view == "archive" else
                row["needs_attention"] and not row["archived"] if view == "attention" else
                row["status"] in ("running", "waiting", "compacting") and not row["archived"] if view == "working" else
                not row["archived"]
            )]
            visible.sort(key=lambda row: (0 if row["needs_attention"] else 1, -datetime.fromisoformat(row["last_message_at"]).timestamp(), row["id"]))
            start = 0
            if cursor:
                try:
                    marker = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                    start = next((i + 1 for i, row in enumerate(visible) if row["id"] == marker["id"] and row["last_message_at"] == marker["at"]), 0)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    raise HTTPException(400, "invalid session cursor") from None
            rows = visible[start : start + limit]
            next_cursor = None
            if start + limit < len(visible) and rows:
                tail = rows[-1]
                next_cursor = base64.urlsafe_b64encode(json.dumps({"id": tail["id"], "at": tail["last_message_at"]}, separators=(",", ":")).encode()).decode().rstrip("=")
        projects = await manager.projects.list()
        names = {p.id: p.name for p in projects}
        default = app.config.default_preset()
        default_label = default[1].display(default[0]) if default else NO_MODEL_LABEL
        overrides_by_id = await manager.live.load_models([row["id"] for row in rows])
        terminals = app.extensions.get("terminals")
        running_terminals = await terminals.running_by_session([row["id"] for row in rows]) if terminals is not None else {}  # type: ignore[attr-defined]
        for row in rows:
            # The directory the session works in, for the tooltip on its row and the chip that says
            # it has one of its own. The list groups by project now, not by workspace.
            project = next((p for p in projects if p.id == row["project_id"]), None)
            workspace = manager.workspace_of(row["id"], row["metadata"], project)
            row["workspace"] = workspace.name
            row["workspace_path"] = str(workspace)
            """The whole path, for the tooltip on a row: a folder named by its last segment alone says
            nothing about which folder it is, and the list no longer groups by it."""
            row["workspace_own"] = bool(row["metadata"].get("directory"))
            row["project"] = names[row["project_id"]]
            row["terminals"] = running_terminals.get(row["id"], 0)
            overrides = overrides_by_id.get(row["id"], {})
            if overrides.get("preset") and overrides["preset"] in app.config.presets:
                row["model"] = app.config.presets[overrides["preset"]].display(overrides["preset"])
            elif overrides.get("provider") and overrides.get("model_name"):
                row["model"] = f"{overrides['provider']}/{overrides['model_name']}"
            else:
                row["model"] = default_label
        active = manager.active_sessions()
        counts = await manager.projects.summary(active)
        empty = {"total": 0, "active": 0, "loops": 0, "last_message_at": ""}
        # A project whose orchestrator is on is drawn as one entry that opens its focus mode, and that
        # entry says what is going on inside: how many work for it, how many are at it now, and how
        # many requests wait for the operator. Counted for every project in three queries.
        orchestrated = [p for p in projects if p.settings.orchestrator.enabled]
        teams = await manager.staff.team_counts() if orchestrated else {}
        waiting = await manager.asks.open_counts("operator") if orchestrated else {}
        folders = [{**p.view(), **counts.get(p.id, empty), "orchestrator": orchestration(p, teams, waiting)} for p in projects]
        folders.sort(key=lambda p: (p["last_message_at"] or p["created_at"], p["id"]), reverse=True)
        return {"sessions": rows, "projects": folders, "next_cursor": next_cursor}

    def orchestration(project: Project, teams: dict[str, dict[str, int]], waiting: dict[str, int]) -> dict[str, Any] | None:
        orchestrator = project.settings.orchestrator
        if not orchestrator.enabled:
            return None
        team = teams.get(project.id, {})
        return {"enabled": True, "session_id": orchestrator.session_id, "staff": team.get("staff", 0), "working": team.get("working", 0), "needs_you": waiting.get(project.id, 0)}

    @api.post("/api/sessions")
    async def new_session(body: NewSessionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        metadata: dict[str, Any] = {"tools_off": sorted(set(body.tools_off))} if body.tools_off else {}
        # A session opened here is not a Telegram session. Without this, private-chat mode
        # delivers the agent's replies into the operator's direct messages.
        metadata["telegram_detached"] = True
        title = body.title.strip()
        if body.autotitle:
            # The operator typed the message instead of a name. A clip stands in until the model
            # names the chat; a filename does when the first turn is only an attachment.
            title = clip_title(title or body.prompt or "")
            if not title:
                raise HTTPException(400, "a chat started this way needs a first message")
            metadata["autotitle"] = True
        elif not title:
            raise HTTPException(400, "a session needs a title")
        if body.project_id:
            project = await manager.projects.get(body.project_id)
            if project is None:
                raise HTTPException(404, "no such project")
            folder = project.folder(body.folder_id) if body.folder_id else project.primary
            if folder is None:
                raise HTTPException(404, f"{project.name} has no such folder")
            if not folder.local(manager.projects.local_env):
                # An agent of this process works with this process's tools; a folder of the other
                # environment is reached only by what runs in a terminal there.
                raise HTTPException(409, f"{folder.path} is a {folder.env} folder; an agent started here cannot work in it, only one started in a {folder.env} terminal can")
            if not await manager.projects.ensure_reachable(folder):
                raise HTTPException(409, f"the folder of {project.name} ({folder.path}) is not reachable from here yet; mount it and restart before starting an agent in it")
        elif body.folder_id:
            raise HTTPException(400, "a folder is named within a project")
        create_args: dict[str, Any] = {"metadata": metadata or None, "project_id": body.project_id or None}
        if body.folder_id:
            create_args["folder_id"] = body.folder_id
        if body.own_directory:
            create_args["own_directory"] = True
        state = await manager.create_session(title, **create_args)
        if body.preset:
            # Before the first run, so the session's opening task already goes to the chosen model.
            try:
                await manager.set_model(state.session.id, preset=body.preset)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.prompt:
            await manager.submit(state.session.id, body.prompt)
        if body.loop is not None:
            loops = app.extensions.get("loops")
            if loops is None:
                raise HTTPException(503, "loops are not installed")
            try:
                await loops.create(state.session.id, instruction=body.loop.instruction, mode=body.loop.mode, interval_seconds=(body.loop.interval_minutes or 0) * 60 or None, max_runs=body.loop.max_runs, start_now=body.loop.start_now)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return {"id": state.session.id, "title": title, "model": await session_model_label(state)}

    async def session_model_label(state: Any) -> str:
        overrides = await manager.live.load(state.session.id)
        if overrides.get("preset") and overrides["preset"] in app.config.presets:
            return app.config.presets[overrides["preset"]].display(overrides["preset"])
        if overrides.get("provider") and overrides.get("model_name"):
            return f"{overrides['provider']}/{overrides['model_name']}"
        default = app.config.default_preset()
        return default[1].display(default[0]) if default else NO_MODEL_LABEL

    async def session_thinking(state: Any) -> dict[str, Any]:
        """The thinking switch and effort this session will use on the next call.

        A live override wins; otherwise the preset the session is on (or the global default).
        The composer draws these so a pick stays on this session and nowhere else.
        """
        overrides = await manager.live.load(state.session.id)
        if overrides.get("preset") and overrides["preset"] in app.config.presets:
            preset = app.config.presets[overrides["preset"]]
        else:
            found = app.config.default_preset()
            preset = found[1] if found else None
        if preset is None:
            return {"thinking": True, "reasoning_effort": "medium"}
        thinking = preset.thinking if overrides.get("thinking_enabled") is None else bool(overrides["thinking_enabled"])
        effort = overrides.get("reasoning_effort") or preset.reasoning_effort
        return {"thinking": thinking, "reasoning_effort": effort}

    async def session_provider(state: Any) -> str:
        """The provider id the session's next call goes to (for the usage card beside the chat)."""
        overrides = await manager.live.load(state.session.id)
        if overrides.get("preset") and overrides["preset"] in app.config.presets:
            return app.config.presets[overrides["preset"]].provider
        if overrides.get("provider"):
            return str(overrides["provider"])
        default = app.config.default_preset()
        return default[1].provider if default else ""

    def _session_folders(state: Any) -> list[dict[str, Any]]:
        """The folders of its project the session's file pane may open: the ones its walls let it read.

        Empty for a session with a directory of its own, whose pane is that directory and nothing
        else; ``writable`` is the walls' answer, so a folder marked read-only offers no upload.
        """
        services = state.services
        if state.project is None or services is None or services.walls is None or state.metadata.get("directory"):
            return []
        out = []
        for folder in state.project.folders:
            if not services.contains(folder.path):
                continue
            out.append({
                "id": folder.id,
                "path": str(folder.path),
                "label": folder.label,
                "env": folder.env,
                "readonly": folder.readonly,
                "writable": services.contains(folder.path, write=True),
            })
        return out

    @api.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, tail: int = 600, before: int = 0, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One page of a session. ``tail`` is the newest N turns; ``before=<seq>`` the page older than one already shown.

        A page is all that is ever read: a session whose transcript is twenty thousand turns
        costs the same to open as one with fifty.
        """
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if state.project is None:
            raise HTTPException(500, "session has no project")
        tail = max(1, min(tail, MAX_TRANSCRIPT_PAGE))
        messages = await manager.transcript_page(session_id, tail=tail, before=before)
        oldest, _newest = await manager.sessions.transcript_bounds(session_id)
        first_seq = next((v["seq"] for v in messages if isinstance(v.get("seq"), int)), 0)
        # Compacting first: a compaction between runs happens on a task of its own, and a session
        # reported as running while it summarises is a run the app draws that nobody started.
        # A run that ended on an error is not an idle session: the reply the model managed to write
        # on the way out — a provider that refused every request still gets one — reads like an
        # answer, and without this the failure has no other trace on the screen. The kind is cleared
        # when the next run starts, so this describes the last run and only until there is another.
        status = "compacting" if state.compacting else "running" if state.running else "waiting" if state.pending else "failed" if state.last_error_kind else "idle"
        # The day's share beside the total, from the same rows: an orchestrator lives for weeks, and
        # its total alone does not say what it is costing now. The day is UTC's, as on Usage.
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        usage = await app.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd,"
            " sum(at >= ?) c_today, sum(CASE WHEN at >= ? THEN input_tokens END) i_today, sum(CASE WHEN at >= ? THEN output_tokens END) o_today,"
            " sum(CASE WHEN at >= ? THEN cost_usd END) usd_today FROM usage_events WHERE session_id = ?",
            (today, today, today, today, session_id),
        )
        context = await manager.context_status(state)
        leader = await manager.get_state(str(state.metadata["subagent_of"])) if state.metadata.get("subagent_of") else None
        subagents = []
        for child in await app.extensions["subagents"].children(session_id) if "subagents" in app.extensions else []:
            child_state = await manager.get_state(child["session_id"])
            subagents.append({**child, "model": await session_model_label(child_state) if child_state else ""})
        return {
            "context": context,
            "id": session_id,
            "title": state.session.title,
            "status": status,
            # The run is over and its answer is on the screen; what is still being written behind it
            # (the snapshot, the delivery to the other fronts, the learning record) is not the run,
            # and a front that draws it draws it as "saving", not as "running".
            "housekeeping": state.housekeeping is not None and not state.housekeeping.done(),
            "error": state.last_error_message if state.last_error_kind else "",
            "compacting": state.compacting,
            "run_id": state.run_id,
            "workspace": str(state.workspace),
            "workspace_name": state.workspace.name,
            "workspace_own": bool(state.metadata.get("directory")),
            "project": state.project.view(),
            "folder_id": state.metadata.get("folder_id") or (state.project.primary.id if state.project.folders else ""),
            "folders": _session_folders(state),
            # Subagents share their leader's workspace by design; they are listed under Subagents (and the leader under
            # "leader:"), so the workspace list shows only the sessions that were attached to it.
            "workspace_sessions": [u for u in await manager.workspace_users(state.workspace) if u["id"] != session_id and u["id"] not in {c["session_id"] for c in subagents} and u["id"] != state.metadata.get("subagent_of")],
            "pending": state.pending.payload if state.pending else None,
            "model": await session_model_label(state),
            "provider": await session_provider(state),
            **(await session_thinking(state)),
            # What is really answering, when that is not what the session was set to. The header
            # reads this on every poll, so a fallback that ends while the screen is open goes away
            # on its own instead of waiting for the session to be reopened.
            **manager.model_status(state),
            "mode": state.metadata.get("mode") or "",
            "yagni": bool(state.metadata.get("yagni")),
            "usd_cap": state.metadata.get("usd_cap"),
            "brief": state.metadata.get("brief") or "",
            "spawned_by": state.metadata.get("spawned_by"),
            "tools_off": sorted(manager.tools_off(state)),
            "loop": state.metadata.get("loop"),
            "services": await app.extensions["services"].list(session_id) if "services" in app.extensions else [],
            "subagent_of": state.metadata.get("subagent_of"),
            "subagent_name": state.metadata.get("subagent_name"),
            # What the session is to its project: its orchestrator (current or retired), or the session
            # of a staff member. The app draws the orchestrator's chat and a staff member's header from these.
            "orchestrator_of": state.metadata.get("orchestrator_of") or state.metadata.get("orchestrator_retired_of"),
            "staff": {"id": state.metadata["staff_id"], "session_id": state.metadata.get("staff_session_id")} if state.metadata.get("staff_id") else None,
            "leader_title": leader.session.title if leader is not None else None,
            "subagents": subagents,
            "telegram_linked": bool(app.front is not None and await app.front.binding_for_session(session_id)),
            "share": await app.extensions["dialogs"].for_session(session_id) if "dialogs" in app.extensions else {"mode": "local", "slug": None, "key": None, "url": None, "public_base": ""},
            "verifications": dict(await app.db.fetchone("SELECT count(*) total, sum(passed) passed FROM verifications WHERE session_id = ?", (session_id,)) or {}),
            "messages": messages,
            "first_seq": first_seq,
            "has_older": bool(first_seq and oldest and first_seq > oldest),
            "usage": dict(usage) if usage else {},
        }

    @api.get("/api/sessions/{session_id}/tasks")
    async def session_tasks(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        tasks = await manager.task_views(session_id)
        return {"tasks": tasks, "background_count": sum(task["state"] == "running" for task in tasks)}

    @api.post("/api/sessions/{session_id}/tasks/{task_id}/stop")
    async def stop_session_task(session_id: str, task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await manager.stop_task(session_id, task_id):
            raise HTTPException(404, "no such task")
        return {"id": task_id, "stopped": True}

    @api.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, after: int = 0, limit: int = 500, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        replay = await manager.events.session_replay(session_id, after=after, limit=limit)
        events = [
            {**item, "seq": item["event_seq"], "type": item["kind"]}
            for item in replay["events"]
        ]
        return {
            **replay,
            "run_id": state.run_id,
            "events": events,
            "last_seq": events[-1]["event_seq"] if events else after,
        }

    @api.get("/api/sessions/{session_id}/stream")
    async def session_stream(session_id: str, request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")

        async def gen():  # type: ignore[no-untyped-def]
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
            overflowed = False
            raw_after = request.query_params.get("after") or request.headers.get("last-event-id")
            fresh = raw_after is None
            try:
                after = max(0, int(raw_after or "0"))
            except ValueError:
                after = 0

            async def sink(sid: str, event: Any) -> None:
                nonlocal overflowed
                if sid == session_id:
                    if overflowed:
                        return
                    try:
                        envelope = dict(event.payload.get("_session_envelope") or {})
                        if envelope:
                            queue.put_nowait(envelope)
                    except asyncio.QueueFull:
                        overflowed = True
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait({"kind": "resync_required", "payload": {"reason": "slow_client"}})

            manager.add_sink(sink)
            try:
                replay = await manager.events.session_replay(session_id, after=after, limit=1000)
                watermark = int(replay["watermark"])
                hello = {
                    "session_id": session_id,
                    "event_seq": watermark,
                    "history_revision": replay["history_revision"],
                    "runtime_epoch": replay["runtime_epoch"],
                }
                yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
                # A new page already loaded the materialized transcript, so it joins at the
                # current boundary. Only reconnects ask for replay; treating a missing cursor as
                # zero would replay the whole lifetime of every session whenever its page opens.
                if fresh:
                    replay = {**replay, "events": [], "resync_required": False}
                    after = watermark
                if replay["resync_required"] or after > watermark:
                    payload = {k: v for k, v in replay.items() if k != "events"}
                    if after > watermark:
                        payload.update({"resync_required": True, "reason": "cursor_ahead"})
                    yield f"event: resync_required\ndata: {json.dumps(payload)}\n\n"
                    return
                last_sent = after
                while True:
                    for envelope in replay["events"]:
                        last_sent = int(envelope["event_seq"])
                        yield f"id: {last_sent}\nevent: {envelope['kind']}\ndata: {json.dumps(envelope, default=str)}\n\n"
                    if last_sent >= watermark:
                        break
                    replay = await manager.events.session_replay(
                        session_id,
                        after=last_sent,
                        through=watermark,
                        limit=1000,
                    )
                    if replay["resync_required"] or not replay["events"]:
                        payload = {k: v for k, v in replay.items() if k != "events"}
                        payload.update({"resync_required": True, "reason": "replay_gap"})
                        yield f"event: resync_required\ndata: {json.dumps(payload)}\n\n"
                        return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        envelope = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    name = str(envelope.get("kind") or "")
                    sequence = int(envelope.get("event_seq") or 0)
                    ephemeral = "ephemeral" in envelope
                    if not ephemeral and sequence <= last_sent:
                        continue
                    if not ephemeral:
                        last_sent = sequence
                    event_id = f"id: {sequence}\n" if not ephemeral else ""
                    yield f"{event_id}event: {name}\ndata: {json.dumps(envelope, default=str)}\n\n"
                    if name == "resync_required":
                        return
            finally:
                manager._sinks.remove(sink)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.get("/api/events")
    async def events_stream(
        request: Request,
        types: str = "",
        client: str = Query("", max_length=64, pattern=r"^[A-Za-z0-9_-]*$"),
        kind: Literal["browser", "pwa", "telegram", "window", "launcher"] = "browser",
        after: int | None = Query(None, ge=0),
        _: dict[str, Any] = Depends(auth),
    ) -> StreamingResponse:
        """Everything that happens to sessions, terminals, staff and notifications, as one stream.

        ``after`` (or ``Last-Event-ID`` on a browser's own reconnect) resumes past a cursor; without
        either the stream is live from now. ``kind`` and ``client`` say who is listening, which the
        presence of the operator will be read from.
        """
        wanted = tuple(t for t in (part.strip() for part in types.split(",")) if t)
        if len(wanted) > EVENT_TYPES_MAX or not all(EVENT_TYPE_RE.fullmatch(t) for t in wanted):
            raise HTTPException(400, f"types: at most {EVENT_TYPES_MAX} dotted lower-case names or prefixes ending in '.'")
        cursor = after
        if cursor is None:
            # A browser's own EventSource reconnect sends this; a malformed one is treated as no cursor
            # (live from now), because the client cannot correct a header it did not write.
            last = request.headers.get("last-event-id", "").strip()
            cursor = int(last) if last.isdigit() else None
        flt = EventFilter(types=wanted or streamed_types())
        presence = manager.presence

        async def opened() -> None:
            await presence.stream_opened(client, kind)

        async def closed() -> None:
            await presence.stream_closed(client, kind)

        frames = event_stream(
            manager.bus, flt, after=cursor, is_disconnected=request.is_disconnected, client=client, kind=kind,
            on_open=opened, on_close=closed,
        )
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(frames, media_type="text/event-stream", headers=headers)

    @api.post("/api/presence", status_code=204)
    async def report_presence(body: PresenceBody, _: dict[str, Any] = Depends(auth)) -> Response:
        """What one window shows, re-sent every 20 s while it is visible and whenever that changes.

        Nothing is written to the database here except the language and time zone when they change:
        every visible tab calls this three times a minute.
        """
        limits = (("sessions", body.sessions, MAX_SESSIONS), ("terminals", body.terminals, MAX_TERMINALS), ("projects", body.projects, MAX_PROJECTS))
        for name, ids, most in limits:
            if len(ids) > most or any(not item or len(item) > MAX_ID_LENGTH for item in ids):
                raise HTTPException(400, f"{name}: at most {most} ids of at most {MAX_ID_LENGTH} characters")
        await manager.presence.report(
            PresenceReport(
                client=body.client, kind=body.kind, visible=body.visible, focused=body.focused,
                sessions=tuple(body.sessions), terminals=tuple(body.terminals), projects=tuple(body.projects),
                screen=body.screen, lang=body.lang, tz=body.tz,
            )
        )
        return Response(status_code=204)

    @api.get("/api/sessions/{session_id}/tool-results/{call_id}")
    async def tool_result(session_id: str, call_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The whole text of one tool result, for the listing's preview to expand.

        ``complete`` is false only when the result was masked and its stored original is gone; the
        text is then the placeholder's own account of it, and the app says so.

        The transcript is written when a run ends, so a result of the run still going is not in it
        yet; asked for one, this answered 404 and the app showed an error under a result it had just
        drawn from the live stream. The run's own history holds it in memory, and the stream's stored
        envelope holds what the app was shown, so those are read next, in that order. When none has
        it and the session is running, ``pending`` says to ask again once the step is saved.
        """
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        messages = await manager.sessions.messages_for_call(session_id, call_id)
        if state.engine is not None:
            messages = [*messages, *reversed(state.engine.history)]
        for message in messages:
            for block in message.content_blocks:
                if isinstance(block, ToolResultBlock) and block.tool_call_id == call_id:
                    text, complete = await full_tool_result(block, manager.blobs, TENANT)
                    return {"id": call_id, "content": redact.redact(text), "is_error": block.is_error, "length": len(text), "complete": complete}
        streamed = await manager.events.session_tool_result(session_id, call_id)
        if streamed is not None:
            text, is_error = streamed
            return {"id": call_id, "content": redact.redact(text), "is_error": is_error, "length": len(text), "complete": True}
        if state.running:
            return {"id": call_id, "content": None, "is_error": False, "length": None, "complete": False, "pending": True}
        raise HTTPException(404, "no such tool result")

    @api.get("/api/sessions/{session_id}/sent/{call_id}/download")
    async def sent_file(session_id: str, call_id: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        """The file a SendFile call handed over, by that call: the app attaches it under the answer, wherever the file lives."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        for message in await manager.sessions.messages_for_call(session_id, call_id):
            for block in message.content_blocks:
                if isinstance(block, ToolUseBlock) and block.tool_call_id == call_id:
                    if block.name != "SendFile":
                        raise HTTPException(404, "that call did not send a file")
                    try:
                        raw = json.loads(block.arguments_json or "{}").get("path")
                    except ValueError:
                        raw = None
                    if not isinstance(raw, str) or not raw.strip():
                        raise HTTPException(404, "the call named no file")
                    candidate = Path(raw).expanduser()
                    target = candidate if candidate.is_absolute() else state.workspace / candidate
                    # The path is read back out of the transcript, so it is checked again rather than
                    # trusted: a project whose folder has moved since the call must not serve the old one.
                    if state.services is not None and not state.services.contains(target):
                        raise HTTPException(403, "that file is outside this project")
                    if not target.is_file():
                        raise HTTPException(404, "the file is gone")
                    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", filename=target.name, headers={"Access-Control-Allow-Origin": "https://web.telegram.org"})
        raise HTTPException(404, "no such call")

    @api.post("/api/media/access")
    async def media_access(request: Request, _: dict[str, Any] = Depends(auth)) -> JSONResponse:
        """Give native audio/video elements the same authenticated browser session as API fetches."""
        response = JSONResponse({"ok": True})
        await sign_in(response, request)
        return response

    @api.get("/api/sessions/{session_id}/media/{presentation_id}/{item_id}/content")
    async def media_content(
        session_id: str,
        presentation_id: str,
        item_id: str,
        _: dict[str, Any] = Depends(auth),
    ) -> Response:
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        item = await manager.media.item(session_id, presentation_id, item_id)
        if item is None:
            raise HTTPException(404, "no such media")
        remote = str(item.get("source_url") or "")
        if remote:
            # The browser loads the link. This redirect is only for a download that still hits the content route.
            if not remote.startswith(("https://", "http://")):
                raise HTTPException(410, "media link is not usable")
            return RedirectResponse(remote, status_code=302)
        path = manager.blobs.path_of(MEDIA_TENANT, str(item["blob_ref"]))
        if not path.is_file():
            raise HTTPException(410, "media bytes are gone")
        return FileResponse(
            path,
            media_type=str(item["mime_type"]),
            filename=str(item["filename"]),
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
        )

    @api.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: SendMessageBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        text, reply = body.text, None
        if body.reply_to is not None:
            # The message names what it answers, in words the model reads and a reference the app
            # draws: a complaint about a member's missing instruction was once taken for one about
            # another member's work, reported in the same minutes.
            found = await manager.sessions.expand_transcript(session_id, body.reply_to.seq, body.reply_to.seq)
            if not found:
                raise HTTPException(404, "that message is not in this conversation")
            excerpt = " ".join(body.reply_to.excerpt.split())[:300] or " ".join(without_turn_context(message_text(found[0][1])).split())[:300]
            reply = {"seq": body.reply_to.seq, "excerpt": excerpt}
            text = f"[In reply to: «{excerpt}»]\n{body.text}"
        try:
            run_id = await manager.submit(
                session_id,
                text,
                steer=body.steer,
                client_message_id=body.client_message_id or None,
                reply_to=reply,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ReceiptConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        receipt = await manager.live.receipt(session_id, body.client_message_id) if body.client_message_id else None
        return {"run_id": run_id, **({"receipt": receipt} if receipt is not None else {})}

    @api.post("/api/sessions/{session_id}/upload")
    async def upload(
        session_id: str,
        text: str = Form(""),
        client_message_id: str = Form("", max_length=64),
        files: list[UploadFile] = File(default=[]),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """Send a message with attachments (or attachments alone) from the Mini App."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        staging = manager.settings.state_dir / "upload-staging"
        staging.mkdir(parents=True, exist_ok=True)
        attachments: list[Attachment] = []
        try:
            for upload_file in files:
                name = Path(upload_file.filename or "file").name
                target = staging / f"{secrets.token_hex(16)}.part"
                digest = hashlib.sha256()
                with target.open("wb") as fh:
                    while chunk := await upload_file.read(1 << 20):
                        digest.update(chunk)
                        fh.write(chunk)
                attachments.append(
                    Attachment(
                        path=target,
                        name=name,
                        content_sha256=digest.hexdigest(),
                        mime_type=upload_file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                )
            if not text.strip() and not attachments:
                raise HTTPException(400, "nothing to send")
            body = text.strip() or ("Files attached." if len(attachments) > 1 else "File attached.")
            run_id = await manager.submit(
                session_id,
                body,
                attachments,
                client_message_id=client_message_id or None,
            )
        except ReceiptConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            for attachment in attachments:
                attachment.path.unlink(missing_ok=True)
        receipt = await manager.live.receipt(session_id, client_message_id) if client_message_id else None
        return {
            "run_id": run_id,
            "files": [item.stored_name or item.name or item.path.name for item in attachments],
            **({"receipt": receipt} if receipt is not None else {}),
        }

    @api.get("/api/asr")
    async def asr_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Whether the site may offer a microphone: something here can turn a recording into words."""
        asr = app.config.asr
        chain = note_transcribers(app.speech, app.config)
        reason = ""
        if asr_configured(asr):
            try:
                effective_asr(asr, manager)
            except TranscriptionError as exc:
                reason = str(exc)
                chain = [who for who in chain if who.kind != "cloud"]
        elif not chain:
            reason = "no speech-to-text endpoint or local model is chosen"
        return {
            "configured": bool(chain),
            "reason": "" if chain else reason,
            "provider": asr.provider,
            "model": asr.model,
            "max_seconds": asr.max_seconds,
            "autosend": asr.autosend,
            "transcriber": asr.transcriber,
            "fallback": asr.fallback,
            # What will actually be tried, in order, after the settings met what is installed.
            "chain": [{"kind": who.kind, "model": who.model} for who in chain],
            "local": app.speech.state(),
        }

    async def transcribe_into(kept: Path, audio: UploadFile | None, recording: str) -> dict[str, Any]:
        """A recording from the site's microphone → its words, marked as a transcript, for a composer.

        A recording that could not be transcribed is kept in ``kept``, not deleted: the error names
        it, and a retry sends that name instead of the audio again. A long voice note used to be lost
        whole to one refusal from the endpoint; now nothing the operator said leaves this host's disk
        until its words have.
        """
        if not recogniser_available(app.speech, app.config):
            raise HTTPException(409, "speech-to-text is not set up (Settings → Voice → Speech recognition)")
        forget_old_recordings(kept)
        if recording:
            if not KEPT_NAME.fullmatch(recording) or not (kept / recording).is_file():
                raise HTTPException(404, "that recording is no longer kept; send the audio again")
            target = kept / recording
        elif audio is not None:
            suffix = Path(audio.filename or "").suffix or mimetypes.guess_extension((audio.content_type or "").split(";")[0]) or ".webm"
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix):
                suffix = ".webm"
            kept.mkdir(parents=True, exist_ok=True)
            target = kept / f"{secrets.token_hex(6)}{suffix.lower()}"
            size = 0
            try:
                with target.open("wb") as fh:
                    while chunk := await audio.read(1 << 20):
                        size += len(chunk)
                        if size > VOICE_NOTE_MAX:
                            raise HTTPException(413, f"a recording may be up to {VOICE_NOTE_MAX >> 20} MB")
                        fh.write(chunk)
            except BaseException:
                # A half-written upload is not a recording worth keeping: the browser still has it.
                target.unlink(missing_ok=True)
                raise
        else:
            raise HTTPException(422, "send the audio, or the name of a kept recording")
        try:
            transcript = await transcribe_recording(app.speech, app.config, manager, target)
        except TranscriptionError as exc:
            logger.warning("voice note %s kept after a failed transcription: %s", target.name, exc)
            raise HTTPException(502, {"message": str(exc), "recording": target.name}) from exc
        target.unlink(missing_ok=True)
        return {"transcript": transcript, "text": voice_note_text(transcript), "autosend": app.config.asr.autosend}

    def forget_kept(kept: Path, recording: str) -> dict[str, Any]:
        if not KEPT_NAME.fullmatch(recording):
            raise HTTPException(404, "no such recording")
        path = kept / recording
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return {"deleted": existed}

    async def session_recordings(session_id: str) -> Path:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return state.workspace / "inbox" / KEPT_RECORDINGS

    def new_chat_recordings() -> Path:
        # A note spoken on the start page belongs to no session yet: it waits in the host's state,
        # not in a workspace that does not exist until the message is sent.
        return settings.state_dir / KEPT_RECORDINGS

    @api.post("/api/sessions/{session_id}/transcribe")
    async def transcribe_audio(
        session_id: str,
        audio: UploadFile | None = File(None),
        recording: str = Form(""),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """A voice note in a session's composer; see ``transcribe_into``."""
        return await transcribe_into(await session_recordings(session_id), audio, recording)

    @api.delete("/api/sessions/{session_id}/transcribe/{recording}")
    async def forget_recording(session_id: str, recording: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Let go of a kept recording: the operator attached it as a file, or discarded it."""
        return forget_kept(await session_recordings(session_id), recording)

    @api.post("/api/transcribe")
    async def transcribe_new_chat(
        audio: UploadFile | None = File(None),
        recording: str = Form(""),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """A voice note in the start page's composer, before there is a session to keep it in."""
        return await transcribe_into(new_chat_recordings(), audio, recording)

    @api.delete("/api/transcribe/{recording}")
    async def forget_new_chat_recording(recording: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Let go of a recording kept for the start page's composer."""
        return forget_kept(new_chat_recordings(), recording)

    # -- voice: the concierge page ---------------------------------------------------

    def voice() -> Any:
        """The voice extension, or a plain refusal: the page is optional and can be switched off."""
        extension = app.extensions.get("voice")
        if extension is None:
            raise HTTPException(503, "the voice page is not installed")
        if not app.config.voice.enabled:
            raise HTTPException(503, "the voice page is switched off in the configuration")
        return extension

    async def voice_view() -> dict[str, Any]:
        """What the page needs to decide how to listen and how to speak: the model, the endpoints, the agents.

        Both the page's poll and the model change answer with this, so a change is shown by the same
        reading that a reload would produce rather than by what the app hoped it had just done.
        """
        extension = app.extensions.get("voice")
        if extension is None or not app.config.voice.enabled:
            # The model is still named and still choosable with the page switched off: which model a
            # concierge would answer with is a decision the operator can make before switching it on.
            # Validated here as well as on the enabled path: a preset id that names nothing would
            # reach the picker as a value no option carries, and a select given one silently shows
            # its first entry instead — so the page would draw a choice the file does not hold.
            preset = app.config.voice.preset if app.config.voice.preset in app.config.presets else ""
            return {
                "enabled": False,
                "session_id": "",
                "preset": preset,
                "using": "",
                "model": "",
                "presets": model_options(app.config),
                "tts": {"configured": False},
                "stt": {"configured": False},
                "agents": [],
                "listening": False,
            }
        state = await extension.state()
        # Which recogniser the page should use is decided here rather than in the extension: the
        # extension knows about endpoints, and a local model is not one.
        spoken = app.tts.state()
        tts = dict(state.get("tts") or {})
        tts["local"] = spoken
        # Which of the three actually speaks, said once here so the page does not have to work it out
        # from three flags. The order is the same one `voice_tts` enforces below.
        if spoken["active"]:
            # Opening the page is the third free moment to build the synthesiser — the operator is
            # looking at the orb and has not said anything yet. What comes back is where that load
            # is, and until it says ready the page speaks the first answer in the browser's own voice
            # rather than holding it back.
            load = app.tts.warm()
            tts.update(
                configured=True, reason="", kind="local", voice=spoken["label"],
                state=load["state"] if load["state"] != "idle" else spoken["state"],
                loaded_in_ms=load["loaded_in_ms"], error=load["error"],
            )
        else:
            # Nothing is built anywhere else: an endpoint and the browser's own synthesiser both
            # speak the moment they are asked, so the page never draws a wait for them.
            tts["kind"] = "endpoint" if tts.get("configured") else "browser"
            tts["state"] = "error" if spoken["voice"] and not spoken["active"] else "ready"
            tts["loaded_in_ms"] = 0
            tts["error"] = spoken["error"] if spoken["voice"] and not spoken["active"] else ""
        tts["last_turn"] = extension.last_turn()
        state["tts"] = tts
        local = app.speech.state()
        stt = dict(state.get("stt") or {})
        stt["local"] = local
        if local["active"]:
            stt["configured"] = True
            stt["reason"] = ""
            stt["model"] = local["label"]
            # Opening the page is the second free moment to load the weights — the operator is looking
            # at the microphone and has not tapped it yet. What comes back is where that load is, and
            # the page keeps the microphone closed until it says ready.
            load = app.speech.warm()
            stt["kind"] = "local"
            stt["state"] = load["state"]
            stt["loaded_in_ms"] = load["loaded_in_ms"]
            stt["error"] = load["error"]
        else:
            # Nothing is loaded anywhere else: an endpoint and the browser's own recogniser are both
            # ready the moment they are asked, so the page never waits on them.
            stt["kind"] = "endpoint" if stt.get("configured") else "browser"
            stt["state"] = "ready"
            stt["loaded_in_ms"] = 0
            stt["error"] = ""
        state["stt"] = stt
        return state

    @api.get("/api/voice")
    async def voice_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await voice_view()

    @api.put("/api/voice/model")
    async def voice_model(body: VoiceModelBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Choose the model the concierge answers with, from the app rather than from the file.

        An unknown preset is refused rather than written: an id that names nothing would be read back
        as "the default", and the operator would be looking at a choice the installation had quietly
        declined to make. Empty is the one id that means that on purpose.
        """
        preset = body.preset.strip()
        if preset and preset not in app.config.presets:
            raise HTTPException(400, f"no such model preset {preset!r}")
        raw = app.config.model_dump(mode="json")
        raw["voice"]["preset"] = preset
        try:
            new_config = type(app.config).model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        await app.save_config(new_config)
        if app.front is not None:
            app.front.config = new_config
        extension = app.extensions.get("voice")
        if extension is not None:
            # The standing conversation is pointed at the new model now, so the next thing said into
            # the microphone is answered by it — no restart, no new conversation.
            await extension.apply_model()  # type: ignore[attr-defined]
        return await voice_view()

    @api.get("/api/voice/stream")
    async def voice_stream(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """The concierge's half of the conversation: a status chip, the answer as it is written, a sentence at a time to speak."""
        extension = voice()

        async def gen():  # type: ignore[no-untyped-def]
            async with extension.listen() as queue:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        name, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.post("/api/voice/say")
    async def voice_say(body: VoiceSayBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One utterance the browser already turned into words."""
        if len(body.text) > VOICE_SAY_MAX_CHARS:
            raise HTTPException(413, f"an utterance may be up to {VOICE_SAY_MAX_CHARS} characters")
        try:
            run_id = await voice().say(body.text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.post("/api/voice/audio")
    async def voice_audio(audio: UploadFile = File(...), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One utterance as a recording, for a browser with no speech recognition of its own."""
        extension = voice()
        if not recogniser_available(app.speech, app.config):
            raise HTTPException(409, "speech-to-text is not set up (Settings → Voice → Speech recognition)")
        suffix = Path(audio.filename or "").suffix or mimetypes.guess_extension((audio.content_type or "").split(";")[0]) or ".webm"
        target = settings.state_dir / "tmp" / f"utterance-{secrets.token_hex(4)}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = 0
            with target.open("wb") as fh:
                while chunk := await audio.read(1 << 20):
                    size += len(chunk)
                    if size > VOICE_AUDIO_MAX:
                        raise HTTPException(413, f"an utterance may be up to {VOICE_AUDIO_MAX >> 20} MB")
                    fh.write(chunk)
            transcript = await transcribe_recording(app.speech, app.config, manager, target)
        except TranscriptionError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            # A client that aborts mid-upload leaves a part file behind unless the write is in here too.
            target.unlink(missing_ok=True)
        try:
            run_id = await extension.say(transcript)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"transcript": transcript, "run_id": run_id}

    @api.post("/api/voice/interrupt")
    async def voice_interrupt(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Barge-in: the operator talked over the answer, so the answer stops.

        Both halves of it. The run stops producing sentences, and anything already being read aloud
        stops at the end of the sentence it is in — otherwise the operator is talked over by a
        synthesiser working through a paragraph that has already been abandoned.
        """
        app.tts.interrupt()
        return {"stopped": await voice().interrupt()}

    @api.post("/api/voice/new")
    async def voice_new(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start the conversation over; the previous one stays as a session of its own."""
        return {"session_id": await voice().new_session()}

    @api.post("/api/voice/tts")
    async def voice_tts(request: Request, body: VoiceSpeakBody, _: dict[str, Any] = Depends(auth)) -> Response:
        """One answer read aloud: a downloaded voice, then the configured endpoint, then 404.

        The 404 is the page's signal to use the browser's own synthesiser, so it is an answer rather
        than a failure and stays a 404 whatever the reason nothing here speaks.

        A local voice answers as a sequence of per-sentence clips rather than as one file, so the
        page starts playing the first sentence while the rest is still being made. The first one is
        synthesised before the response begins: a voice that cannot speak has to be able to say 503,
        and after the first byte of a body there is no status code left to send.
        """
        extension = voice()
        if len(body.text) > VOICE_TTS_MAX_CHARS:
            raise HTTPException(413, f"a sentence may be up to {VOICE_TTS_MAX_CHARS} characters")
        if not body.text.strip():
            raise HTTPException(400, "nothing to say")
        # A voice that was downloaded here comes first: the operator chose it, it costs nothing per
        # sentence and it works with the network down. A local voice that fails is *not* silently
        # replaced by the endpoint — an endpoint is metered and may not be configured at all, and a
        # failure hidden behind a fallback is a failure nobody fixes.
        if app.tts.available():
            clips = app.tts.clips(body.text.strip())
            # Where the wait goes, measured rather than guessed: how long this request took to have
            # something playable, and how much of that was the voice being built. A warm voice makes
            # the second number zero, which is the whole point of warming it.
            began = time.monotonic()
            cold = app.tts.load_state()["state"] != "ready"
            try:
                first, media_type = await anext(clips)
            except StopAsyncIteration as exc:
                await clips.aclose()
                raise HTTPException(503, "the local voice could not speak this: there was nothing to say") from exc
            except Exception as exc:
                # Anything at all, not only TtsError: a wheel that is the wrong build for this
                # machine raises out of the engine itself, and a 500 with no words in it is the one
                # outcome this endpoint promised never to have.
                await clips.aclose()
                raise HTTPException(503, f"the local voice could not speak this: {exc}") from exc

            clip_ms = int((time.monotonic() - began) * 1000)
            extension.first_audio(body.turn, clip_ms=clip_ms, load_ms=int(app.tts.load_state()["loaded_in_ms"]) if cold else 0)

            async def spoken() -> AsyncIterator[bytes]:
                try:
                    yield speech_frame(first)
                    async for clip, _ in clips:
                        if await request.is_disconnected():
                            return
                        yield speech_frame(clip)
                except TtsError as exc:
                    # Halfway through is too late for a status code; the page has the sentences it
                    # already has, and the reason belongs in the log.
                    logger.warning("the local voice stopped mid-answer: %s", exc)
                finally:
                    await clips.aclose()

            return StreamingResponse(spoken(), media_type=SEQUENCE_TYPE, headers={MEDIA_TYPE_HEADER: media_type})
        if not tts_configured(app.config.voice.tts):
            raise HTTPException(404, "nothing here speaks; the browser says this one itself")
        began = time.monotonic()
        try:
            chunks, media_type = await extension.speech(body.text.strip())
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"the speech endpoint could not be reached: {type(exc).__name__}") from exc
        # An endpoint has nothing to load, so the whole of its wait is the request itself.
        extension.first_audio(body.turn, clip_ms=int((time.monotonic() - began) * 1000), load_ms=0)
        return StreamingResponse(chunks, media_type=media_type)

    # -- local speech synthesis --------------------------------------------------------------
    #
    # The same shape as the recognition endpoints below, and deliberately so: a catalog, a download
    # that reports itself over SSE, a choice that is one line of configuration, and a delete. The one
    # thing recognition has no use for is `sample` — nobody picks a voice from a table of numbers, so
    # a card can be made to say a sentence in its own language before it is chosen.

    def _tts_view() -> dict[str, Any]:
        """The picker's whole state. Three endpoints answer with it, so it is built in one place."""
        view = speech_models.view(
            app.tts.downloads,
            selected=app.config.voice.tts.local_voice,
            entries=tts_catalog.VOICES,
            to_json=tts_catalog.as_json,
            all_languages=tts_catalog.languages,
        )
        view["engine_installed"] = speech_service.engine_present()
        view["recommended"] = {code: found.id for code in tts_catalog.languages() if (found := tts_catalog.recommended(code))}
        view["state"] = app.tts.state()
        view["load"] = app.tts.load_state()
        return view

    @api.get("/api/tts")
    async def tts_voices(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The picker: every voice, what is installed, what is downloading, and what it all costs."""
        return _tts_view()

    @api.post("/api/tts/engine/warm")
    async def tts_warm(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Build the chosen voice now, so the first answer does not pay for it.

        Answers at once with where the load is — ``idle``, ``loading``, ``ready`` or ``error`` — and
        the rest arrives on ``/api/tts/progress`` as it happens. Calling it twice is one load.
        """
        return app.tts.warm()

    @api.post("/api/tts/voices/{voice_id}/download")
    async def tts_download(voice_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Fetch a voice. Returns at once; the bar is fed by /api/tts/progress."""
        try:
            progress = app.tts.downloads.start(voice_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": progress.id, "state": progress.state, "fraction": progress.fraction}

    @api.post("/api/tts/voices/{voice_id}/cancel")
    async def tts_cancel(voice_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Stop a download. What has arrived is kept, so starting again continues from there."""
        try:
            return {"cancelled": app.tts.downloads.cancel(voice_id)}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.delete("/api/tts/voices/{voice_id}")
    async def tts_delete(voice_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Remove an installed voice, and stop using it if it was the one in use."""
        try:
            removed = await app.tts.downloads.delete(voice_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if app.config.voice.tts.local_voice == voice_id:
            await app.save_config(app.config.model_copy(update={"voice": app.config.voice.model_copy(
                update={"tts": app.config.voice.tts.model_copy(update={"local_voice": "", "local_speaker": ""})})}))
        app.tts.forget()
        return {"deleted": removed, **_tts_view()}

    @api.post("/api/tts/select")
    async def tts_select(body: TtsSelectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Use this voice — or, with an empty id, go back to the endpoint and the browser.

        Choosing one loads it straight away rather than at the first sentence. Loading takes a second
        or two; paying for it here, while the operator is looking at the page they asked on, is much
        better than paying for it in the middle of the first answer they wanted to hear.
        """
        patch: dict[str, Any] = {}
        if body.voice is not None:
            if body.voice and not app.tts.downloads.is_installed(body.voice):
                raise HTTPException(409, f"{body.voice} is not downloaded yet")
            patch["local_voice"] = body.voice
            # A speaker belongs to the voice that has it; carrying one across is a name the new model
            # does not know, which silently becomes speaker zero.
            patch["local_speaker"] = ""
        if body.speaker is not None:
            patch["local_speaker"] = body.speaker.strip()
        if body.speed is not None:
            patch["local_speed"] = body.speed
        if body.threads is not None:
            patch["local_threads"] = body.threads
        if patch:
            voice_config = app.config.voice
            await app.save_config(app.config.model_copy(update={"voice": voice_config.model_copy(
                update={"tts": voice_config.tts.model_copy(update=patch)})}))
        # The answer carries the load this call started rather than a reading taken before it: the
        # page draws "loading the voice" off the same response that chose the voice, and a view built
        # a moment earlier would tell it the voice is idle.
        return {**_tts_view(), "load": app.tts.warm()}

    @api.post("/api/tts/voices/{voice_id}/sample")
    async def tts_sample(voice_id: str, language: str = "", _: dict[str, Any] = Depends(auth)) -> Response:
        """A short phrase in this voice, so it can be heard before it is chosen.

        ``language`` is what the picker is filtered by: a voice that speaks thirty-one languages is
        filed under one of them, and reading its sample in that one answered a question the operator
        filtering for another language had not asked.
        """
        try:
            clip, media_type = await app.tts.sample(voice_id, language)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TtsError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(clip, media_type=media_type)

    @api.get("/api/tts/progress")
    async def tts_progress(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """Download progress as it happens, so the bar moves rather than being polled at."""

        async def gen():  # type: ignore[no-untyped-def]
            # Two things move on this stream and ``kind`` tells them apart, exactly as on the
            # recognition side: a download, which the picker draws as a bar on one card, and the
            # voice being built, which the voice page draws as the reason it is not speaking yet.
            async with app.tts.downloads.watch() as queue, VOICE_CACHE.watch() as loads:
                for current in app.tts.downloads.progress().values():
                    yield f"data: {json.dumps({'kind': 'download', 'id': current.id, 'state': current.state, 'fraction': current.fraction, 'error': current.error})}\n\n"
                yield f"data: {json.dumps({'kind': 'engine', **app.tts.load_state()})}\n\n"
                downloading = asyncio.ensure_future(queue.get())
                loading = asyncio.ensure_future(loads.get())
                try:
                    while True:
                        if await request.is_disconnected():
                            return
                        # Both waits stay alive across the loop and only the one that finished is
                        # started again: cancelling a queue.get() that has already taken an item off
                        # the queue is how an update disappears.
                        done, _pending = await asyncio.wait({downloading, loading}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            yield ": keepalive\n\n"
                            continue
                        if downloading in done:
                            update = downloading.result()
                            downloading = asyncio.ensure_future(queue.get())
                            body = {"kind": "download", "id": update.id, "state": update.state, "fraction": update.fraction, "error": update.error}
                            yield f"data: {json.dumps(body)}\n\n"
                        if loading in done:
                            load = loading.result()
                            loading = asyncio.ensure_future(loads.get())
                            yield f"data: {json.dumps({'kind': 'engine', **load})}\n\n"
                finally:
                    downloading.cancel()
                    loading.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    # -- local speech recognition ------------------------------------------------------------
    #
    # A model is chosen from the catalog, downloaded into the state directory and used in front of
    # everything else. The endpoints below are the whole of that: the list with what is installed,
    # a download that reports itself over SSE, a selection that is one line of configuration, and a
    # stream the voice page talks into so that a browser without its own recognition still sees words
    # appear while the sentence is being said.

    listening: dict[str, dict[str, Any]] = {}
    """Open streams by id. Small, short-lived, and pruned on every call that touches one."""

    def _prune_streams() -> None:
        """Let go of what nobody is feeding. Called on every listen request, not only on a new one.

        Reaping only when a new stream arrives is not a timeout: a page that opens four streams and
        is then closed leaves four decoders resident until somebody else starts listening, which on a
        single-operator installation may be never.
        """
        now = time.monotonic()
        stale = [sid for sid, entry in listening.items() if now - entry["seen"] > LISTEN_IDLE_SECONDS]
        while len(listening) - len(stale) > LISTEN_MAX_STREAMS:
            oldest = min((sid for sid in listening if sid not in stale), key=lambda sid: listening[sid]["seen"])
            stale.append(oldest)
        for sid in stale:
            listening.pop(sid, None)

    def stt_view() -> dict[str, Any]:
        """The whole picker, from one place.

        Every endpoint that changes something about local recognition answers with this, complete —
        the models, the settings, the engine, the decoders and the recommendations. It is written once
        because it was not: ``select`` used to answer with the models alone, the picker replaced its
        whole view with what came back, and the next render read ``decoders.opus`` off an object that
        no longer had it and took the screen down. A partial view is a crash waiting for a render.
        """
        view = speech_models.view(app.speech.downloads, selected=app.config.stt.local_model)
        view["language"] = app.config.stt.local_language
        view["threads"] = app.config.stt.local_threads
        view["engine_installed"] = speech_service.engine_present()
        view["decoders"] = speech_service.decoders()
        view["recommended"] = {code: model.id for code in ("en", "ru") if (model := speech_catalog.recommended(code))}
        view["load"] = app.speech.load_state()
        return view

    @api.get("/api/stt")
    async def stt_models(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The picker: every model, what is installed, what is downloading, and what it all costs."""
        return stt_view()

    @api.post("/api/stt/engine")
    async def stt_install_engine(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Install the engine that runs a downloaded model, and wait for it.

        The picker calls this by itself before the first download, so this endpoint waits rather than
        answering "started": the download that follows it needs the engine to be there. Everything
        underneath is the components installer — the same one the Components page drives — because two
        installers running ``uv sync`` into one environment is how a site-packages directory ends up
        half written, and because "the speech runtime" ought to mean the same thing on both pages.
        """
        if component_list.engine_installed():
            return {"installed": True, "message": "the speech engine is already installed"}
        status = component_registry().status(component_list.SPEECH)
        try:
            installer.start(status)
        except component_install.Busy as exc:
            if exc.component_id != component_list.SPEECH:
                raise HTTPException(409, f"{exc.component_id} is installing; one at a time") from None
        except component_install.NotInstallable as exc:
            raise HTTPException(501 if settings.native else 409, exc.reason) from None
        try:
            async with asyncio.timeout(STT_ENGINE_WAIT_SECONDS):
                await installer.wait(component_list.SPEECH)
        except TimeoutError:
            # Still going, and the install is not abandoned: it is the same installer the Components
            # page drives, and the frame is on its stream. What is given up on is this request.
            raise HTTPException(504, "the speech engine is still installing; Settings → Components shows where it has got to") from None
        frame = installer.progress_of(component_list.SPEECH) or {}
        if frame.get("state") != "installed":
            raise HTTPException(502, str(frame.get("error") or "the speech engine could not be installed"))
        speech_service.forget_engine()
        logger.warning("the local speech engine was installed on demand")
        return {"installed": True, "message": "the speech engine is installed; the model can be downloaded now"}

    @api.post("/api/stt/engine/warm")
    async def stt_warm(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Load the chosen model into memory now, so the first utterance does not pay for it.

        Answers at once with where the load is — ``idle``, ``loading``, ``ready`` or ``error`` — and
        the rest arrives on ``/api/stt/progress`` as it happens. Calling it twice is one load.
        """
        return app.speech.warm()

    @api.post("/api/stt/models/{model_id}/download")
    async def stt_download(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Fetch a model. Returns at once; the bar is fed by /api/stt/progress."""
        try:
            progress = app.speech.downloads.start(model_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": progress.id, "state": progress.state, "fraction": progress.fraction}

    @api.post("/api/stt/models/{model_id}/cancel")
    async def stt_cancel(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Stop a download. What has arrived is kept, so starting again continues from there."""
        try:
            return {"cancelled": app.speech.downloads.cancel(model_id)}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.delete("/api/stt/models/{model_id}")
    async def stt_delete(model_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Remove an installed model, and unselect it if it was the one in use."""
        try:
            removed = await app.speech.downloads.delete(model_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if app.config.stt.local_model == model_id:
            await app.save_config(app.config.model_copy(update={"stt": app.config.stt.model_copy(update={"local_model": ""})}))
        app.speech.forget()
        return {"deleted": removed, **stt_view()}

    @api.post("/api/stt/select")
    async def stt_select(body: SttSelectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Use this model — or, with an empty id, go back to the endpoint and the browser."""
        patch: dict[str, Any] = {}
        if body.model is not None:
            if body.model and not app.speech.downloads.is_installed(body.model):
                raise HTTPException(409, f"{body.model} is not installed yet")
            patch["local_model"] = body.model
        if body.language is not None:
            patch["local_language"] = body.language.strip() or "auto"
        if body.threads is not None:
            patch["local_threads"] = body.threads
        if patch:
            await app.save_config(app.config.model_copy(update={"stt": app.config.stt.model_copy(update=patch)}))
            # Choosing a model is the moment its weights are about to be wanted and the moment nobody
            # is waiting on them, so the load starts here rather than under the first utterance.
            app.speech.warm()
        return stt_view()

    @api.get("/api/stt/progress")
    async def stt_progress(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """Download progress as it happens, so the bar moves rather than being polled at."""

        async def gen():  # type: ignore[no-untyped-def]
            # Two things move on this stream and they are told apart by ``kind``: a download, which
            # the picker draws as a bar on one card, and the engine loading a model into memory,
            # which the voice page draws as the reason its microphone is not open yet. One stream
            # rather than two because a page that wants either usually wants both, and because a
            # second SSE connection costs a second proxied, kept-alive socket for four small frames.
            async with app.speech.downloads.watch() as queue, ENGINE_CACHE.watch() as loads:
                for current in app.speech.downloads.progress().values():
                    yield f"data: {json.dumps({'kind': 'download', 'id': current.id, 'state': current.state, 'fraction': current.fraction, 'error': current.error})}\n\n"
                yield f"data: {json.dumps({'kind': 'engine', **app.speech.load_state()})}\n\n"
                downloading = asyncio.ensure_future(queue.get())
                loading = asyncio.ensure_future(loads.get())
                try:
                    while True:
                        if await request.is_disconnected():
                            return
                        # Both waits stay alive across the loop and only the one that finished is
                        # started again: cancelling a queue.get() that has already taken an item off
                        # the queue is how an update disappears.
                        done, _pending = await asyncio.wait({downloading, loading}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            yield ": keepalive\n\n"
                            continue
                        if downloading in done:
                            update = downloading.result()
                            downloading = asyncio.ensure_future(queue.get())
                            body = {"kind": "download", "id": update.id, "state": update.state, "fraction": update.fraction, "error": update.error}
                            yield f"data: {json.dumps(body)}\n\n"
                        if loading in done:
                            load = loading.result()
                            loading = asyncio.ensure_future(loads.get())
                            yield f"data: {json.dumps({'kind': 'engine', **load})}\n\n"
                finally:
                    downloading.cancel()
                    loading.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    # -- components ---------------------------------------------------------------------

    @api.get("/api/dependencies")
    async def dependencies_view(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await dependency_planner.view()
        except (RuntimeError, OSError) as exc:
            raise HTTPException(503, str(exc)) from None

    @api.get("/api/maintenance")
    async def maintenance_view(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            status = await dependency_planner.rpc("dependencies_status")
        except (RuntimeError, OSError):
            raise HTTPException(503, "maintenance status unavailable") from None
        job = status.get("job") or {}
        notice = None
        if job.get("state") in ("installing", "restarting") and job.get("stage") in ("restart_pending", "restarting"):
            notice = {"id": job["id"], "stage": job["stage"], "restart_at": job.get("restart_at", 0)}
        return {"notice": notice, "server_time": time.time()}

    @api.post("/api/dependencies/request")
    async def dependencies_request(body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await dependency_planner.start(str(body.get("request", "")), str(body.get("preset", "")))
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/dependencies/{proposal_id}/cancel")
    async def dependencies_cancel(proposal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await dependency_planner.cancel(proposal_id)
            return {"cancelled": True}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/dependencies/{proposal_id}/approve")
    async def dependencies_approve(proposal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if installer.running_id():
            raise HTTPException(409, "a component is being installed; wait for it to finish")
        try:
            return await dependency_planner.approve(proposal_id)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None

    @api.get("/api/prompt-change")
    async def prompt_change_view(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await prompt_change_planner.view()

    @api.post("/api/prompt-change/request")
    async def prompt_change_request(body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await prompt_change_planner.start(str(body.get("instruction", "")), str(body.get("preset", "")))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/prompt-change/{proposal_id}/cancel")
    async def prompt_change_cancel(proposal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            await prompt_change_planner.cancel(proposal_id)
            return {"cancelled": True}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.post("/api/prompt-change/{proposal_id}/approve")
    async def prompt_change_approve(proposal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await prompt_change_planner.approve(proposal_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    def component_registry() -> component_list.Registry:
        """A registry over the configuration as it is now.

        Built per call rather than held: every answer on this page is a measurement, and the one thing
        an operator does here is change what the measurement would say.
        """
        return component_list.Registry(settings, app.config, installer=installer)

    @api.get("/api/components")
    async def components_view(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every optional piece: what it unlocks, whether it is here, and what it would take."""
        return await asyncio.to_thread(lambda: component_registry().view())

    @api.post("/api/components/{component_id}/install")
    async def components_install(component_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Install one component. Answers at once; the bar is fed by /api/components/stream.

        Three refusals, and each is a different thing the operator can do about it: 404 is not a
        component, 409 is one already going in, and 501 is a component this installation cannot put in
        place from here — which carries the command or the image tag that can.
        """
        if component_id not in component_list.CATALOGUE:
            raise HTTPException(404, f"no such component: {component_id}")
        if (settings.state_dir / "dependencies" / "maintenance").exists():
            raise HTTPException(409, "dependencies are being installed; wait for the application to restart")
        status = component_registry().status(component_id)
        if status.state == "installed":
            return {"id": component_id, "state": "installed", "step": "", "error": "", "restart_required": False}
        try:
            return installer.start(status)
        except component_install.Busy as exc:
            raise HTTPException(409, f"{exc.component_id} is installing; one at a time") from None
        except component_install.NotInstallable as exc:
            raise HTTPException(501, f"{exc.reason}{chr(10) + chr(10) + exc.fix if exc.fix else ''}") from None

    @api.post("/api/components/{component_id}/cancel")
    async def components_cancel(component_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Stop an install where the step underneath can be stopped — which is not all of them."""
        if component_id not in component_list.CATALOGUE:
            raise HTTPException(404, f"no such component: {component_id}")
        return {"cancelled": installer.cancel(component_id)}

    async def _refuse_restart_while_busy(force: bool) -> None:
        """A restart is refused while any session is running or still writing its last turn down.

        Both are losses nothing recovers. During a run it is the run. In the moment after one — the
        window the app draws as idle — it is the snapshot of the turn that just ended and the
        handover to the other fronts, which is a finished answer somebody is waiting for. So the
        button says what is in the way, by name, instead of taking it with it.
        """
        if force:
            return
        busy = manager.busy_sessions()
        if not busy:
            return
        names = []
        for session_id in sorted(busy):
            state = await manager.get_state(session_id)
            names.append((state.session.title if state is not None else "") or session_id)
        one = len(busy) == 1
        raise HTTPException(
            409,
            f"{len(busy)} agent{'' if one else 's'} {'is' if one else 'are'} working or still saving the last turn ({', '.join(names)}); "
            f"a restart now loses what {'it is' if one else 'they are'} writing — stop {'it' if one else 'them'} first, or pass force=1",
        )

    @api.post("/api/components/restart")
    async def components_restart(force: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Restart the agent, so a component that arrived in the environment is in force.

        The answer comes back before the restart does — this process is what goes away — so it says
        what was asked for rather than what happened.
        """
        await _refuse_restart_while_busy(force)
        try:
            return {"result": await installer.restart()}
        except component_install.NotInstallable as exc:
            raise HTTPException(501, f"{exc.reason}{chr(10) + chr(10) + exc.fix if exc.fix else ''}") from None
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.get("/api/components/stream")
    async def components_stream(request: Request, _: dict[str, Any] = Depends(auth)) -> StreamingResponse:
        """Install progress as it happens, so a three-hundred-megabyte download looks like one."""

        async def gen():  # type: ignore[no-untyped-def]
            async with installer.watch() as queue:
                for frame in installer.all_progress():
                    yield f"data: {json.dumps(frame)}\n\n"
                waiting = asyncio.ensure_future(queue.get())
                try:
                    while True:
                        if await request.is_disconnected():
                            return
                        done, _pending = await asyncio.wait({waiting}, timeout=15)
                        if not done:
                            yield ": keepalive\n\n"
                            continue
                        frame = waiting.result()
                        waiting = asyncio.ensure_future(queue.get())
                        yield f"data: {json.dumps(frame)}\n\n"
                finally:
                    waiting.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.post("/api/voice/listen/open")
    async def voice_listen_open(rate: int = SAMPLE_RATE, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Open a stream and get back the id every chunk of it must carry.

        The id is minted here rather than invented by the page. It is the only thing separating two
        listening sessions, and a page that picks its own with ``Math.random()`` can collide with
        another and interleave two people's audio into one transcript.

        The rate is the one the browser's own graph settled on, which is not always the one it was
        asked for: Safari and a number of Android webviews hand back the hardware's 44 100 or 48 000
        instead. It is declared once, here, because a stream that changed rate mid-sentence would have
        to be re-opened anyway — and because feeding 48 kHz samples to a model told they are 16 kHz
        makes it hear the sentence at three times its speed and answer with noise.
        """
        if not app.speech.available():
            raise HTTPException(409, "no local speech model is installed and selected (Settings → Voice)")
        _prune_streams()
        try:
            clamp_rate(rate)
            session = await app.speech.session()
        except SpeechError as exc:
            raise HTTPException(400 if "capture rate" in str(exc) else 503, str(exc)) from exc
        stream = secrets.token_urlsafe(16)
        listening[stream] = {"session": session, "seen": time.monotonic(), "rate": rate, "seq": 0}
        return {"stream": stream, "rate": rate}

    @api.post("/api/voice/listen")
    async def voice_listen(request: Request, stream: str, seq: int, final: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One chunk of mono PCM from the page, answered with the words heard so far.

        POSTed chunks rather than a socket: the app authenticates with a header that a browser cannot
        put on a WebSocket, and inside Telegram there is no cookie to fall back on either. A fifth of
        a second per request is well within what the loop and the model can keep up with — decoding
        runs tens of times faster than real time — and it makes the transport the same one every other
        call already uses.

        ``seq`` counts from one and must not skip. HTTP promises nothing about the order separate
        POSTs arrive in; the page serialises them, but a retry or a proxy that reorders would otherwise
        corrupt a transcript with nothing anywhere able to notice.

        A ``final`` answer is an utterance: the page hands it to the concierge exactly as it hands one
        the browser recognised itself.
        """
        if not app.speech.available():
            raise HTTPException(409, "no local speech model is installed and selected (Settings → Voice)")
        chunk = await request.body()
        if len(chunk) > LISTEN_CHUNK_MAX:
            raise HTTPException(413, f"a chunk may be up to {LISTEN_CHUNK_MAX >> 20} MB of samples")
        _prune_streams()
        entry = listening.get(stream)
        if entry is None:
            raise HTTPException(404, "this listening stream is not open; open one at /api/voice/listen/open")
        if seq != entry["seq"] + 1:
            listening.pop(stream, None)
            raise HTTPException(409, f"chunk {seq} arrived where {entry['seq'] + 1} was expected; the stream is closed")
        entry["seq"] = seq
        entry["seen"] = time.monotonic()
        try:
            partial = await entry["session"].feed(chunk, entry["rate"]) if chunk else None
            if final:
                partial = await entry["session"].finish()
                listening.pop(stream, None)
        except SpeechError as exc:
            listening.pop(stream, None)
            raise HTTPException(502, str(exc)) from exc
        if partial is None:
            return {"text": "", "final": False}
        return {"text": partial.text, "final": partial.final}

    @api.post("/api/voice/listen/close")
    async def voice_listen_close(stream: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The page stopped listening; let go of the stream without decoding what is left.

        For abandoning a stream, not for ending one: a page that wants the words it has not been given
        yet sends its last chunk with ``final=true``, which decodes the tail and pops the entry itself.
        """
        return {"closed": listening.pop(stream, None) is not None}

    @api.post("/api/sessions/{session_id}/answer")
    async def answer(session_id: str, body: AnswerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            run_id = await manager.answer(session_id, body.answers, via="app")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @api.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, keep_workspace: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if app.front is not None:
            # Before the deletion: the topic to close and the private chat's pointer are both
            # read from rows that go with the session.
            await app.front.forget_session(session_id)
        # Ended here rather than only by the delete hook, so the answer can say how many — the
        # dialog that asked has already told the operator the number.
        terminals = app.extensions.get("terminals")
        ended = await terminals.close_owned("session", session_id, actor="operator") if terminals is not None else 0  # type: ignore[attr-defined]
        return {"deleted": await manager.delete_session(session_id, delete_workspace=not keep_workspace), "terminals_ended": ended}

    @api.delete("/api/sessions/{session_id}/telegram")
    async def detach_session_telegram(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        if app.front is None:
            raise HTTPException(409, "Telegram is not configured")
        try:
            detached = await app.front.detach_session(session_id)
        except TelegramRefused as exc:
            raise HTTPException(502, f"Telegram did not close the topic: {exc}") from exc
        if not detached:
            raise HTTPException(409, "this session is not linked to an open Telegram topic")
        return {"detached": True}

    @api.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, body: RenameBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            state = await manager.get_state(session_id)
            if state is None:
                raise KeyError(session_id)
            if body.title is not None:
                if app.front is not None:
                    await app.front.rename_session(session_id, body.title)
                else:
                    await manager.rename_session(session_id, body.title)
            if body.archived is not None:
                state.session.metadata["archived"] = body.archived
                await manager.sessions.update_metadata(session_id, state.session.metadata)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": session_id, "title": state.session.title, "archived": bool(state.session.metadata.get("archived"))}

    @api.post("/api/sessions/{session_id}/project")
    async def move_session(session_id: str, body: MoveSessionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Put a session in a project, take it out of one, or move it between two.

        Nothing on disk moves. Into a project the session either starts working in the project's
        folder — where it sees the rest of the project's files, and they see what it writes — or
        keeps the directory it already has: the same one, listed under the new project and sharing
        none of it. Out of a project it keeps that directory too. Nothing a session has written is
        ever left behind by a move.
        """
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if session_id in manager.busy_sessions():
            raise HTTPException(409, f"{state.session.title or session_id} is working; moving it now would change the folder under it mid-turn — stop it first")
        project = await manager.projects.get(body.project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        if not await manager.projects.ensure_reachable(project.primary):
            raise HTTPException(409, f"the folder of {project.name} ({project.primary.path}) is not reachable from here yet; mount it and restart")
        moved = await manager.attach_project(session_id, project, own_directory=body.own_directory)
        return {"id": session_id, "project_id": project.id, "project": project.name, "workspace": str(moved.workspace)}

    @api.get("/api/sessions/{session_id}/checkpoints")
    async def session_checkpoints(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The workspace snapshots this session still has, and whether retention cut the list."""
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        return await manager.list_checkpoints(session_id)

    @api.post("/api/sessions/{session_id}/revert")
    async def revert_session(session_id: str, body: RevertBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await manager.revert(session_id, body.seq)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/retry")
    async def retry_session(session_id: str, body: RevertBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await manager.retry(session_id, body.seq)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/fork")
    async def fork_session(session_id: str, body: ForkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        source = await manager.get_state(session_id)
        if source is None:
            raise HTTPException(404, "no such session")
        title = (body.title or f"{re.sub(r'\s*\(fork @\d+\)$', '', source.session.title)} (fork @{body.seq})")[:128]
        # A fork of a project session stays in the project: the two sessions share the root, which
        # is what "several agents work in one project" already means, and the fork keeps the wall.
        # It is opened on the site, so it does not grow a topic or speak in the private chat.
        target = await manager.create_session(
            title,
            metadata={"forked_from": {"session_id": session_id, "seq": body.seq}, "telegram_detached": True},
            project_id=source.project.id if source.project is not None else None,
            own_directory=bool(source.metadata.get("directory")),
            folder_id=source.metadata.get("folder_id") or None,
        )
        try:
            result = await manager.fork_into(session_id, body.seq, target)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": target.session.id, "title": title, **result}

    @api.post("/api/sessions/{session_id}/mode")
    async def set_mode(session_id: str, body: ModeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"mode": await manager.set_mode(session_id, body.mode)}
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/yagni")
    async def set_yagni(session_id: str, body: YagniBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"yagni": await manager.set_yagni(session_id, body.on)}
        except KeyError:
            raise HTTPException(404, "no such session") from None

    @api.post("/api/sessions/{session_id}/cap")
    async def set_session_cap(session_id: str, body: SessionCapBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            cap = await manager.set_session_cap(session_id, body.usd_cap)
        except KeyError:
            raise HTTPException(404, "no such session") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        spent, _ = await manager.spend(session_id=session_id)
        return {"usd_cap": cap, "spent_usd": round(spent, 4)}

    @api.get("/api/tools")
    async def list_tools(_: dict[str, Any] = Depends(auth)) -> list[dict[str, str]]:
        """Every host tool with a one-line description and a group, for the tool checkboxes."""
        out = []
        for t in sorted(manager.tools.list_all(), key=lambda t: t.name):
            if t.name.startswith("Mcp_"):
                continue  # MCP tools come and go with their servers; they are switched per server
            desc = " ".join((t.definition.description or "").split())
            out.append({"name": t.name, "description": desc[:160], "group": _tool_group(t.name)})
        return out

    @api.get("/api/tool-groups")
    async def tool_group_catalogue(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The host's tool groups for the settings page: purpose, tools, cost, mode, a month of use."""
        return await manager.tool_group_catalogue()

    @api.put("/api/tool-groups/{group}")
    async def set_tool_group_load(group: str, body: ToolGroupLoadBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The installation's mode for a group; every session that did not choose its own follows it
        from its next run."""
        if group not in TOOL_GROUPS:
            raise HTTPException(404, "no such tool group")
        raw = app.config.model_dump(mode="json")
        chosen = raw.setdefault("tools", {}).setdefault("groups", {})
        if body.load is None or body.load == TOOL_GROUPS[group].load:
            # Writing the default down would pin it: a better default shipped later would not reach an
            # installation whose operator merely clicked the one it already had.
            chosen.pop(group, None)
        else:
            chosen[group] = {"load": body.load}
        try:
            await app.save_config(type(app.config).model_validate(raw), expected_revision=body.base_revision)
        except ConfigConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
        # The whole settings view comes back with the catalogue: the save moved the configuration's
        # revision, and a Settings screen still holding the old one refused its next save as "changed in
        # another window" when the other window was this one.
        return {**await manager.tool_group_catalogue(), "settings": _settings_view()}

    @api.get("/api/sessions/{session_id}/tool-groups")
    async def session_tool_groups(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        await manager.refresh_on_demand_groups(state)
        return {"groups": manager.tool_group_states(state)}

    @api.put("/api/sessions/{session_id}/tool-groups/{group}")
    async def set_session_tool_group(session_id: str, group: str, body: ToolGroupLoadBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if group not in TOOL_GROUPS:
            raise HTTPException(404, "no such tool group")
        try:
            return {"groups": await manager.set_session_tool_group(session_id, group, body.load)}
        except KeyError:
            raise HTTPException(404, "no such session") from None

    @api.post("/api/sessions/{session_id}/tool-groups/{group}/load")
    async def load_session_tool_group(session_id: str, group: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start the session's next run with the group loaded, as if a search had loaded it."""
        if group not in TOOL_GROUPS:
            raise HTTPException(404, "no such tool group")
        try:
            return {"groups": await manager.load_tool_group_now(session_id, group)}
        except KeyError:
            raise HTTPException(404, "no such session") from None

    # -- terminals: the terminal daemons' terminals, as the service mirrors them --------------

    terminal_gateway = Gateway(
        service=lambda: app.extensions.get("terminals"),  # type: ignore[arg-type, return-value]
        public_url=lambda: settings.miniapp_public_url,
        ticket_ttl=lambda: app.config.terminals.ticket_ttl_seconds,
    )
    api.state.terminal_gateway = terminal_gateway  # the tests reach its ticket book's clock through this

    def terminal_service() -> Terminals:
        service = app.extensions.get("terminals")
        if service is None:
            raise HTTPException(503, "the terminals subsystem is not running")
        return service  # type: ignore[return-value]

    @api.get("/api/terminals")
    async def terminals_list(
        env: Literal["container", "host"] | None = None,
        owner_kind: Literal["session", "staff", "project", "free"] | None = None,
        owner_id: str | None = Query(default=None, max_length=128),
        project_id: str | None = Query(default=None, max_length=128),
        status: Literal["running", "exited", "lost"] | None = None,
        preview: int = Query(default=0, ge=0, le=12),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        service = terminal_service()
        owner = TerminalOwner(owner_kind, owner_id) if owner_kind and (owner_id or owner_kind == "free") else None
        views = await service.list(env=env, owner=owner, owner_kind=owner_kind if owner is None else None, project_id=project_id, status=status, preview_rows=preview)
        by_env = await service.running_by_env()
        return {
            "envs": [e.view() for e in service.environments(by_env)],
            "terminals": views,
            "capacity": {"running": sum(by_env.values()), "cap": app.config.terminals.running_cap, "queued": len(service.queue())},
        }

    @api.get("/api/terminals/load")
    async def terminals_load(cap: int | None = Query(default=None, ge=1, le=100_000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the running terminals cost, and what the machine would carry at ``cap`` of them."""
        return await terminal_service().load(cap=cap)

    @api.post("/api/terminals/envs/{env}/update", status_code=202)
    async def terminals_daemon_update(env: str, body: TerminalDaemonUpdateBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Recreate the terminals service from the image, which updates its daemon and ends its
        terminals: 409 ``live_terminals`` with the count until repeated with ``confirm``."""
        return await terminal_service().request_update(env, confirm=body.confirm if body is not None else False)

    @api.get("/api/terminals/envs/{env}/update/{job}")
    async def terminals_daemon_update_result(env: str, job: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        return terminal_service().update_result(env, job)

    @api.post("/api/terminals", status_code=201)
    async def terminals_create(body: TerminalCreateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        spec = TerminalSpec(
            env=body.env,
            owner=TerminalOwner(body.owner_kind, body.owner_id or None),
            project_id=body.project_id or None,
            cwd=body.cwd or None,
            title=body.title,
            sandbox=body.sandbox,
            cols=body.cols,
            rows=body.rows,
            created_by="operator",
        )
        return await terminal_service().create(spec, confirm_over_cap=body.confirm)

    @api.get("/api/terminals/{terminal_id}")
    async def terminals_get(terminal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await terminal_service().get(terminal_id)

    @api.patch("/api/terminals/{terminal_id}")
    async def terminals_patch(terminal_id: str, body: TerminalPatchBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        owner = TerminalOwner(body.owner_kind, body.owner_id or None) if body.owner_kind else None
        return await terminal_service().update(terminal_id, title=body.title, owner=owner)

    @api.post("/api/terminals/{terminal_id}/kill")
    async def terminals_kill(terminal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await terminal_service().kill(terminal_id)

    @api.post("/api/terminals/{terminal_id}/signal")
    async def terminals_signal(terminal_id: str, body: TerminalSignalBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await terminal_service().signal(terminal_id, body.signal)
        return {"ok": True}

    @api.post("/api/terminals/{terminal_id}/keyboard")
    async def terminals_keyboard(terminal_id: str, body: TerminalKeyboardBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Who holds the terminal's keyboard: the staff view's "Release" after the operator typed."""
        return await terminal_service().keyboard(terminal_id, body.owner)

    @api.post("/api/terminals/{terminal_id}/restart")
    async def terminals_restart(terminal_id: str, body: TerminalRestartBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        body = body or TerminalRestartBody()
        return await terminal_service().restart(terminal_id, sandbox=body.sandbox, confirm_over_cap=body.confirm)

    @api.delete("/api/terminals/{terminal_id}")
    async def terminals_remove(terminal_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await terminal_service().remove(terminal_id)
        return {"ok": True}

    @api.get("/api/terminals/{terminal_id}/screen")
    async def terminals_screen(terminal_id: str, format: Literal["text", "vt", "runs"] = "text", scrollback: int = Query(default=0, ge=0, le=10_000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await terminal_service().read_screen(terminal_id, format=format, scrollback=scrollback)

    @api.get("/api/terminals/{terminal_id}/commands")
    async def terminals_commands(terminal_id: str, last: int = Query(default=20, ge=1, le=500), output: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The commands the terminal's shell reported, for "copy last output" and the marks."""
        return {"commands": await terminal_service().commands(terminal_id, last=last, with_output=output)}

    @api.get("/api/terminals/{terminal_id}/audit")
    async def terminals_audit(terminal_id: str, limit: int = Query(default=200, ge=1, le=1000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        # The audit outlives the row on purpose, so a removed terminal's history is still answered.
        entries = await terminal_service().audit_log(terminal_id, limit=limit)
        if not entries and await app.db.fetchone("SELECT 1 FROM terminals WHERE id = ?", (terminal_id,)) is None:
            raise HTTPException(404, "no such terminal")
        return {"entries": entries}

    @api.post("/api/terminals/{terminal_id}/ticket")
    async def terminals_ticket(terminal_id: str, request: Request, body: TerminalTicketBody | None = None, who: dict[str, Any] = Depends(auth)) -> Any:
        """A single-use ticket for the terminal's WebSocket, which cannot carry the auth headers."""
        read_only = body.read_only if body is not None else False
        caller = ticket_who(str(who.get("via") or "token"), request.headers.get("user-agent", ""), request.client.host if request.client else "")
        try:
            return await terminal_gateway.issue(terminal_id, read_only=read_only, who=caller)
        except EnvUnavailable as exc:
            # 409, not the 503 of the other routes: the app reads this answer as "the environment is
            # down, keep trying" rather than as the whole host failing.
            return JSONResponse({"detail": exc.message, "code": exc.code, **exc.details}, status_code=409)

    @api.websocket("/ws/terminals/{terminal_id}")
    async def terminals_socket(websocket: WebSocket, terminal_id: str) -> None:
        await websocket.accept()
        await terminal_gateway.serve(
            TerminalSocket(websocket),
            terminal_id,
            ticket=websocket.query_params.get("ticket", ""),
            origin=websocket.headers.get("origin"),
            host=websocket.headers.get("host"),
            address=websocket.client.host if websocket.client else "",
        )

    @api.get("/api/services")
    async def all_services(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every hosted service with the session it belongs to."""
        services = app.extensions.get("services")
        return await services.list_all() if services is not None else []

    @api.get("/api/sessions/{session_id}/services")
    async def session_services(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        services = app.extensions.get("services")
        return await services.list(session_id) if services is not None else []

    @api.post("/api/sessions/{session_id}/services/{name}/stop")
    async def session_service_stop(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return await services.stop(session_id, name, note="stopped by the operator")
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.delete("/api/sessions/{session_id}/services/{name}")
    async def session_service_remove(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a service: a running one is stopped first; its log file stays in the workspace."""
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        if not await services.remove(session_id, name):
            raise HTTPException(404, f"no service {name!r} in this session")
        return {"removed": name}

    @api.get("/api/sessions/{session_id}/services/{name}/logs")
    async def session_service_logs(session_id: str, name: str, lines: int = 120, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return {"text": await services.logs(session_id, name, lines)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/services/{name}/share")
    async def session_service_share(session_id: str, name: str, body: ShareBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """local: LAN only · public: anyone through the site · key: whoever opens the link that carries the key."""
        services = app.extensions.get("services")
        if services is None:
            raise HTTPException(503, "services are not installed")
        try:
            return await services.share(session_id, name, body.mode, rotate_key=body.rotate_key)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/share")
    async def session_dialog_share(session_id: str, body: ShareBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """local: only in the app · public: anyone with the link · key: whoever opens the link that carries the key."""
        dialogs = app.extensions.get("dialogs")
        if dialogs is None:
            raise HTTPException(503, "sharing is not installed")
        try:
            return await dialogs.share(session_id, body.mode, rotate_key=body.rotate_key)  # type: ignore[attr-defined]
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- a shared dialog: /c/<slug> is the page, and the messages are read with no login ---------
    # The same address the services use. No auth dependency: public mode is open to anyone, key mode
    # to whoever presents the key (once in the query, then in a cookie scoped to the slug).

    noindex = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store"}

    def _share_refusal(request: Request, status: int) -> HTMLResponse:
        russian = (request.headers.get("accept-language") or "").lower().startswith("ru")
        if status == 403:
            text = "Для этой ссылки нужен ключ." if russian else "This link needs its key."
        else:
            text = "Этот диалог не открыт." if russian else "This conversation is not shared."
        title = "Общий диалог" if russian else "Shared conversation"
        lang = "ru" if russian else "en"
        body = (
            f"<!DOCTYPE html><html lang=\"{lang}\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta name=\"robots\" content=\"noindex\"><title>{title}</title></head>"
            "<body style=\"margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0d;color:#ececee;"
            f"font:15px/1.5 sans-serif;padding:24px;text-align:center\"><p>{text}</p></body></html>"
        )
        return HTMLResponse(body, status_code=status, headers=noindex)

    async def _shared_dialog(slug: str, request: Request) -> tuple[dict[str, Any], str | None]:
        """The share row, and a key from the query that still has to be moved into a cookie."""
        dialogs = app.extensions.get("dialogs")
        row = await dialogs.by_slug(slug) if dialogs is not None else None  # type: ignore[attr-defined]
        if row is None or (row.get("share_mode") or "local") not in ("public", "key"):
            raise HTTPException(404, "nothing is shared here")
        query_key = request.query_params.get("key")
        if row["share_mode"] == "key" and query_key is not None:
            if not dialogs.allows(row, query_key):  # type: ignore[attr-defined]
                raise HTTPException(403, "wrong key")
            return row, query_key
        presented = request.cookies.get(SHARE_COOKIE_PREFIX + slug) or request.headers.get("x-share-key")
        if not dialogs.allows(row, presented):  # type: ignore[attr-defined]
            raise HTTPException(403, "this dialog needs its key: open the link that carries ?key=…")
        return row, None

    @api.get("/c/{slug}/robots.txt")
    async def dialog_robots(slug: str) -> PlainTextResponse:
        """Crawlers that reach a shared dialog anyway are told to leave."""
        return PlainTextResponse("User-agent: *\nDisallow: /\n", headers={"X-Robots-Tag": "noindex, nofollow"})

    @api.get("/c/{slug}/transcript")
    async def dialog_transcript(slug: str, request: Request) -> JSONResponse:
        dialogs = app.extensions.get("dialogs")
        row, _key = await _shared_dialog(slug, request)
        if dialogs is None:
            raise HTTPException(404, "nothing is shared here")
        try:
            page = await dialogs.page(row["id"], slug)  # type: ignore[attr-defined]
        except KeyError as exc:
            raise HTTPException(404, "nothing is shared here") from exc
        except RuntimeError as exc:
            raise HTTPException(503, "sharing is not available") from exc
        return JSONResponse(page, headers=noindex)

    @api.get("/c/{slug}/media/{presentation_id}/{item_id}")
    async def dialog_media(slug: str, presentation_id: str, item_id: str, request: Request) -> Response:
        dialogs = app.extensions.get("dialogs")
        row, _key = await _shared_dialog(slug, request)
        found = await dialogs.media(row["id"], presentation_id, item_id) if dialogs is not None else None  # type: ignore[attr-defined]
        if found is None:
            raise HTTPException(404, "no such media")
        mime, location = found
        if location.startswith(("https://", "http://")):
            return RedirectResponse(location, status_code=302, headers=noindex)
        return FileResponse(location, media_type=mime, content_disposition_type="inline", headers={**noindex, "X-Content-Type-Options": "nosniff"})

    @api.get("/c/{slug}")
    async def dialog_page(slug: str, request: Request) -> Response:
        try:
            _row, key = await _shared_dialog(slug, request)
        except HTTPException as exc:
            return _share_refusal(request, exc.status_code if exc.status_code in (403, 404) else 404)
        response = RedirectResponse("/app/c/" + quote(slug, safe=""), status_code=303, headers=noindex)
        if key:
            # The key leaves the address after the first open, so a link copied from the page does not carry it.
            response.set_cookie(
                SHARE_COOKIE_PREFIX + slug,
                key,
                httponly=True,
                samesite="lax",
                path=f"/c/{slug}",
                max_age=30 * 86400,
                secure=request.url.scheme == "https",
            )
        return response

    # -- shared services: the site proxies /s/<slug>/… to the service's port --------------
    # The reverse proxy in front of the API already makes the site public; a shared service rides on the
    # same address instead of a port of its own. No auth dependency here: public mode is open to anyone,
    # key mode to whoever presents the key (once in the query, then in a cookie scoped to the slug).

    hop_headers = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"}

    @api.get("/s/{slug}/robots.txt")
    async def shared_robots(slug: str) -> Any:
        """Crawlers that reach a shared service anyway are told to leave (the proxied pages say the same in a header)."""
        return PlainTextResponse("User-agent: *\nDisallow: /\n", headers={"X-Robots-Tag": "noindex, nofollow"})

    @api.get("/s/{slug}")
    async def shared_root(slug: str, request: Request) -> RedirectResponse:
        return RedirectResponse(f"/s/{slug}/" + (f"?{request.url.query}" if request.url.query else ""))

    @api.api_route("/s/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def shared_proxy(slug: str, path: str, request: Request) -> Any:
        services = app.extensions.get("services")
        row = await services.by_slug(slug) if services is not None else None  # type: ignore[attr-defined]
        if row is None or (row.get("share_mode") or "local") not in SHARE_MODES[1:]:
            raise HTTPException(404, "nothing is shared here")
        cookie_name = SHARE_COOKIE_PREFIX + slug
        presented = request.query_params.get("key")
        if row["share_mode"] == "key" and presented is not None:
            if not services.share_allows(row, presented):  # type: ignore[attr-defined]
                raise HTTPException(403, "wrong key")
            # The key moves from the address into a cookie, so the link people copy afterwards does not carry it.
            rest = [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
            response: Any = RedirectResponse(f"/s/{slug}/{path}" + (f"?{urlencode(rest)}" if rest else ""), status_code=303)
            response.set_cookie(cookie_name, presented, httponly=True, samesite="lax", path=f"/s/{slug}", max_age=30 * 86400, secure=request.url.scheme == "https")
            return response
        if not services.share_allows(row, request.cookies.get(cookie_name) or request.headers.get("x-share-key")):  # type: ignore[attr-defined]
            raise HTTPException(403, "this service needs its key: open the link that carries ?key=…")
        if row["status"] != "running" or not pid_alive(row.get("pid")) or not row.get("port"):
            raise HTTPException(503, "the service is not running")
        upstream = f"http://127.0.0.1:{row['port']}/{path}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in hop_headers}
        headers["x-forwarded-prefix"] = f"/s/{slug}"
        headers["x-forwarded-host"] = request.headers.get("host", "")
        headers["x-forwarded-proto"] = request.url.scheme
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        try:
            up = await client.send(client.build_request(request.method, upstream, headers=headers, content=await request.body()), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"the service did not answer: {exc}") from exc
        # Nothing under /s/ is for search engines: the address is the only secret of a public share.
        out: dict[str, str] = {"X-Robots-Tag": "noindex, nofollow, noarchive"}
        for k, v in up.headers.multi_items():
            lk = k.lower()
            if lk in hop_headers or lk == "x-robots-tag":
                continue
            if lk == "location" and v.startswith("/") and not v.startswith(f"/s/{slug}/"):
                v = f"/s/{slug}{v}"  # a redirect to the service's root stays under the slug
            out[k] = v
        # A page must not outlive the share in a browser cache: switching a service back to LAN-only takes
        # effect on the next reload, not when the cached copy expires.
        if up.headers.get("content-type", "").startswith("text/html"):
            out["Cache-Control"] = "no-store"

        async def body() -> Any:
            try:
                async for chunk in up.aiter_raw():
                    yield chunk
            finally:
                await up.aclose()
                await client.aclose()

        return StreamingResponse(body(), status_code=up.status_code, headers=out)

    @api.post("/api/sessions/{session_id}/loop")
    async def set_session_loop(session_id: str, body: LoopBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Attach (or replace) the session's loop."""
        loops = app.extensions.get("loops")
        if loops is None:
            raise HTTPException(503, "loops are not installed")
        try:
            return await loops.create(session_id, instruction=body.instruction, mode=body.mode, interval_seconds=(body.interval_minutes or 0) * 60 or None, max_runs=body.max_runs, start_now=body.start_now)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/sessions/{session_id}/loop/action")
    async def session_loop_action(session_id: str, body: LoopActionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """pause | resume | stop | remove | run (an iteration now)."""
        loops = app.extensions.get("loops")
        if loops is None:
            raise HTTPException(503, "loops are not installed")
        try:
            if body.action == "pause":
                return await loops.pause(session_id, body.reason or "paused by the operator")
            if body.action == "resume":
                return await loops.resume(session_id)
            if body.action == "stop":
                return await loops.stop(session_id, body.reason or "stopped by the operator")
            if body.action == "remove":
                return {"removed": await loops.remove(session_id)}
            if body.action == "run":
                await loops.schedule_next(session_id, 0, "run now")
                return {"fired": await loops._fire_if_due(session_id)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(422, "action is pause, resume, stop, remove or run")

    @api.post("/api/sessions/{session_id}/tools")
    async def set_session_tools(session_id: str, body: ToolsOffBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"tools_off": await manager.set_tools_off(session_id, body.tools_off)}
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc

    @api.post("/api/sessions/{session_id}/brief")
    async def set_brief(session_id: str, body: BriefBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"brief": await manager.set_brief(session_id, body.brief)}
        except KeyError:
            raise HTTPException(404, "no such session") from None

    @api.get("/api/commands")
    async def list_commands(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Only what this installation can run: a palette entry that answers with a refusal is a lie."""
        return [{"name": c.name, "args": c.args, "description": c.description, "scope": c.scope, "confirm": c.confirm} for c in slash.available(app)]

    @api.post("/api/sessions/{session_id}/command")
    async def run_slash_command(session_id: str, body: CommandBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Run a slash command for this session and return the text the chat would have shown."""
        try:
            text = await slash.run_command(app, session_id, body.line)
        except KeyError as exc:
            raise HTTPException(404, f"unknown command or session: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except TelegramBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"text": redact.redact(text)}

    @api.get("/api/limits/spend")
    async def limits_spend(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Priced spend per provider and in total since ``limits.total_since``, next to the caps."""
        limits = app.config.limits
        since = limits.total_since or None
        rows = await app.db.fetchall(
            "SELECT provider_id, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered FROM usage_events" + (" WHERE at >= ?" if since else "") + " GROUP BY provider_id",
            (since,) if since else (),
        )
        per_provider = {r["provider_id"]: {"spent_usd": round(float(r["usd"] or 0.0), 4), "unmetered": int(r["unmetered"] or 0), "cap_usd": float(limits.usd_total_per_provider.get(r["provider_id"], 0) or 0)} for r in rows}
        for pid, cap in limits.usd_total_per_provider.items():
            per_provider.setdefault(pid, {"spent_usd": 0.0, "unmetered": 0, "cap_usd": float(cap or 0)})
        total, unmetered = await manager.spend(since=since)
        return {"since": limits.total_since, "total": {"spent_usd": round(total, 4), "unmetered": unmetered, "cap_usd": limits.usd_total}, "per_provider": per_provider}

    @api.post("/api/limits/reset-total")
    async def limits_reset_total(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start the total counters afresh from now; past usage stays recorded, it just no longer counts."""
        raw = app.config.model_dump(mode="json")
        raw.setdefault("limits", {})["total_since"] = datetime.now(UTC).isoformat()
        await app.save_config(type(app.config).model_validate(raw))
        return {"total_since": app.config.limits.total_since}

    @api.get("/api/modes")
    async def modes(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {name: m.model_dump() for name, m in app.config.modes.items()}

    # -- board ---------------------------------------------------------------------------------

    def _board():  # type: ignore[no-untyped-def]
        board = app.extensions.get("board")
        if board is None:
            raise HTTPException(503, "the board is not installed")
        return board

    @api.get("/api/board")
    async def board_list(include_done: int = 0, project: str | None = None, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every task, or with ``project`` one project's board: the shell's project lens over the Board screen.

        A project's task carries its project's and its assignee's names: the screen's card is the only
        place it is read there, and a card that names neither the work's project nor who did it read as
        an empty title."""
        tasks = await _board().list(None, include_done=bool(include_done), project_id=project or None)
        projects = {r["id"]: r["name"] for r in await manager.db.fetchall("SELECT id, name FROM projects")}
        staff = {r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]} for r in await manager.db.fetchall("SELECT id, name, color FROM staff")}
        for task in tasks:
            task["project_name"] = projects.get(task.get("project_id") or "")
            task["assignee"] = staff.get(task.get("assignee_staff_id") or "")
        return tasks

    @api.get("/api/projects/{project_id}/board")
    async def project_board(project_id: str, include_done: int = 0, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A project's board: its tasks with their assignees, what waits on the operator, and the counts."""
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        board = await _board().project_board(project_id, include_done=bool(include_done))
        return {"project": {"id": project.id, "name": project.name, "ephemeral": project.settings.ephemeral, "system": project.settings.system}, **board}

    def _review():  # type: ignore[no-untyped-def]
        review = getattr(app.extensions.get("staff"), "review", None)
        if review is None:
            raise HTTPException(503, "review and merge need the staff runtime")
        return review

    @api.get("/api/board/{task_id}/review")
    async def board_review(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What merging a task's staff branch would bring and what stands in its way; read-only."""
        try:
            return await _review().review(task_id)  # type: ignore[no-any-return]
        except KeyError:
            raise HTTPException(404, "no such task") from None
        except ReviewRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/peers")
    async def peers_list(_: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        peers = app.extensions.get("peers")
        return await peers.registry() if peers is not None else {}  # type: ignore[attr-defined]

    @api.post("/api/peers")
    async def peers_register(body: PeerBody, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        peers = app.extensions.get("peers")
        if peers is None:
            raise HTTPException(503, "peers are not installed")
        if await manager.get_state(body.session_id) is None:
            raise HTTPException(404, "no such session")
        try:
            await peers.register(body.name, body.session_id)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return await peers.registry()  # type: ignore[attr-defined]

    @api.delete("/api/peers/{name}")
    async def peers_forget(name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        peers = app.extensions.get("peers")
        if peers is None:
            raise HTTPException(503, "peers are not installed")
        return {"forgotten": await peers.forget(name.lower())}  # type: ignore[attr-defined]

    # -- inbound events ------------------------------------------------------------------

    @api.post("/api/inbound")
    async def inbound_post(body: InboundBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        inbound = app.extensions.get("inbound")
        if inbound is None:
            raise HTTPException(503, "inbound events are not installed")
        try:
            return await inbound.deliver(source=body.sender, text=body.text, session_ref=body.session, default_title="[inbound]", prompt=body.prompt)  # type: ignore[attr-defined]
        except TelegramBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/webhooks/{provider}")
    async def webhook(provider: str, request: Request) -> dict[str, Any]:
        # Every pre-verification refusal looks the same from outside: an anonymous caller learns nothing about what is configured.
        inbound = app.extensions.get("inbound")
        conf = app.config.webhooks.get(provider)
        if inbound is None or conf is None or not conf.enabled or not conf.secret:
            logger.warning("webhook %s refused: %s", provider, "not installed" if inbound is None else "unknown or disabled" if conf is None or not conf.enabled else "no secret configured")
            raise HTTPException(401, "refused")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "payload too large")
        raw = await request.body()
        if len(raw) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "payload too large")
        if not verify_signature(conf.scheme, conf.secret, raw, {k.lower(): v for k, v in request.headers.items()}):
            raise HTTPException(401, "refused")
        stamped = request.headers.get("x-github-delivery") or request.headers.get("x-delivery-id")
        # Without a delivery id, identical bodies within the same minute are one event; later repeats are new ones.
        delivery_id = stamped or hashlib.sha256(raw).hexdigest() + ":" + datetime.now(UTC).strftime("%Y%m%d%H%M")
        if not await inbound.record_delivery(provider, delivery_id):  # type: ignore[attr-defined]
            return {"status": "duplicate", "delivery_id": delivery_id}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")[:PAYLOAD_MAX_CHARS]}
        event = request.headers.get("x-github-event") or request.headers.get("x-event") or ""
        if not event and isinstance(payload, dict) and isinstance(payload.get("event") or payload.get("type"), str):
            event = str(payload.get("event") or payload.get("type"))[:100]
        summary = flatten_payload(payload)
        text = (f"event: {event}\n" if event else "") + summary
        # Every accepted delivery is an event, so a project's watch on a pull request or a CI result
        # hears it whether or not a session also runs it.
        try:
            await manager.bus.publish(
                "webhook.received",
                {"provider": provider, "event": event, "delivery_id": delivery_id, "summary": summary, **webhook_facts(event, payload)},
            )
        except Exception:  # noqa: BLE001 — the delivery is accepted; the event is for the watches
            logger.warning("could not publish webhook.received for %s", provider, exc_info=True)
        if conf.deliver == "events":
            return {"status": "accepted", "delivery_id": delivery_id, "delivered": "events"}
        try:
            result = await inbound.deliver(source=f"webhook:{provider}", text=text, session_ref=conf.session or None, default_title=f"[webhook {provider}]", prompt=conf.prompt)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — the sender must get a status, and the failure goes to the inbox
            logger.warning("webhook %s could not run", provider, exc_info=True)
            await inbound.forget_delivery(provider, delivery_id)  # type: ignore[attr-defined]
            if app.notifications is not None:
                await app.notifications.post(Draft("system", f"Webhook {provider} could not start a run", f"{type(exc).__name__}: {exc}", kind="webhook_failed", tone="warning", source=f"webhook:{provider}"))
            raise HTTPException(503, "accepted but could not start a run; see the inbox") from exc
        return {"status": "accepted", "delivery_id": delivery_id, **result}

    @api.get("/api/sessions/{session_id}/verifications")
    async def session_verifications(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        rows = await app.db.fetchall("SELECT id, run_id, criterion, command, exit_code, passed, output_digest, output_head, duration_ms, at, sandboxed, dependencies, tree, tests_run, tests_skipped, file_digests FROM verifications WHERE session_id = ? ORDER BY id DESC LIMIT 100", (session_id,))
        r = redact.shared()
        return [{**dict(row), "criterion": r.redact(row["criterion"]), "command": r.redact(row["command"]), "output_head": r.redact(row["output_head"] or "")} for row in rows]

    @api.post("/api/sessions/{session_id}/compact")
    async def compact_session(session_id: str, body: CompactBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            summary = await manager.compact(session_id, body.instructions)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"summary": summary}

    @api.post("/api/sessions/{session_id}/clear")
    async def clear_session(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start over with an empty working history; the workspace, the brief and the settings stay."""
        try:
            return await manager.clear_history(session_id)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/sessions/{session_id}/export/download")
    async def session_export(session_id: str, _: dict[str, Any] = Depends(auth)) -> Response:
        """The whole session as one Markdown file: every turn, tool call and result, with the spend at the end."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        lines = [f"# {state.session.title}", "", f"Session {session_id}, exported {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}.", ""]
        for m in await manager.transcript(session_id):
            view = message_view(m)
            if view.get("internal"):
                continue
            who = {"user": "Operator", "assistant": "Agent", "tool": "Tool", "system": "System"}.get(m.role.value, m.role.value)
            if view.get("text"):
                lines += [f"## {who}", "", redact.redact(view["text"]), ""]
            for call in view.get("tool_calls", []):
                lines += [f"### → {call['name']}", "", "```json", json.dumps(call["arguments"], ensure_ascii=False, indent=1)[:4000], "```", ""]
            for result in view.get("tool_results", []):
                lines += [f"### ← result{' (error)' if result.get('is_error') else ''}", "", "```", result["content"][:4000], "```", ""]
        usage = await app.db.fetchone("SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd FROM usage_events WHERE session_id = ?", (session_id,))
        if usage and usage["c"]:
            lines += ["## Spend", "", f"{usage['c']} model calls · {int(usage['i'] or 0):,} in ({int(usage['ch'] or 0):,} cached) · {int(usage['o'] or 0):,} out · ${float(usage['usd'] or 0):.4f}", ""]
        body = "\n".join(lines)
        return Response(content=body, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="session-{session_id}.md"'})

    @api.post("/api/sessions/{session_id}/policy/grant")
    async def policy_grant(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Let one refused call through: ``{"key": "<approval key from the refusal>"}``."""
        try:
            grants = await manager.grant(session_id, str(body.get("key") or ""), via="app")
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"grants": grants}

    @api.post("/api/sessions/{session_id}/policy/refuse")
    async def policy_refuse(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Leave one refused call refused: ``{"key": "<approval key>"}``. The request stops being open."""
        try:
            return await manager.refuse(session_id, str(body.get("key") or ""), via="app")
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/sessions/{session_id}/egress")
    async def session_egress(session_id: str, limit: int = 200, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"items": await manager.egress(session_id, limit=limit)}

    @api.get("/api/sessions/{session_id}/tools/timing")
    async def session_tool_timing(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"items": await manager.tool_timing(session_id)}

    @api.get("/api/policy")
    async def policy_rules(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        policy = manager.policy()
        return {"rules": policy.describe(), "egress_allow": policy.egress_allow}

    @api.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"stopped": await manager.stop(session_id)}

    @api.post("/api/sessions/{session_id}/model")
    async def set_model(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """``{"clear": true}`` returns the session to the global default; otherwise any of
        ``provider`` (a configured client id), ``model``, ``thinking``, ``reasoning_effort``."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if body.get("context_window"):
            # Per-session window (in memory): smaller than the model's for cheap runs or tests.
            state.context_window = max(8_000, int(body["context_window"]))
        try:
            await manager.set_model(
                session_id,
                model_name=body.get("model"),
                provider=body.get("provider"),
                preset=body.get("preset") or None,
                thinking=body.get("thinking"),
                reasoning_effort=body.get("reasoning_effort"),
                clear=bool(body.get("clear")),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        orchestrators = app.extensions.get("orchestrator")
        if orchestrators is not None and (body.get("preset") or body.get("clear")):
            # The chip on an orchestrator's chat chooses the project's orchestrator model: the next
            # orchestrator of the project, after a replacement, runs the same one.
            await orchestrators.model_chosen(session_id, body.get("preset") or None, clear=bool(body.get("clear")))
        dispatcher = app.extensions.get("dispatcher")
        if dispatcher is not None and (body.get("preset") or body.get("clear")):
            # The chip on the main chat chooses the main orchestrator's model in Settings.
            await dispatcher.model_chosen(session_id, body.get("preset") or None, clear=bool(body.get("clear")))
        return {"ok": True, "model": await session_model_label(state), **(await session_thinking(state))}

    # -- MCP per session --------------------------------------------------------------

    @api.get("/api/sessions/{session_id}/mcp")
    async def session_mcp(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return {"enabled": manager.mcp_enabled(state), "servers": manager.mcp.status()}

    @api.put("/api/sessions/{session_id}/mcp")
    async def set_session_mcp(session_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            enabled = await manager.set_mcp(session_id, str(body["server"]), bool(body.get("enabled", True)))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"enabled": enabled, "servers": manager.mcp.status()}

    # -- workspace files ------------------------------------------------------------

    def _safe_path(root: Path, rel: str) -> Path:
        target = (root / rel).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            raise HTTPException(400, "path escapes the workspace")
        # A session's log scratch sits in the sealed state directory on purpose, and the pane is rooted
        # at it only by ``_pane_target`` for that session's own logs; the containment above keeps the
        # answer inside that one directory, so the rest of the state volume stays sealed.
        own_logs = (manager.settings.state_dir / SCRATCH_DIR_NAME).resolve() in root.resolve().parents
        if not own_logs and sealed_root(str(target), [str(p) for p in manager.protected_paths()]) is not None:
            # The containment above is what normally keeps this pane inside the operator's own work.
            # This is the same answer the tools get, asked again here: a root that ever comes to sit
            # over part of the installation must not open it through a browser either.
            raise HTTPException(403, "that path is part of the installation, not of its work")
        return target

    def _read_path(root: Path, path: str) -> dict[str, Any]:
        """A directory listing or a file's text at ``path`` under ``root`` (the sessions' and the workspaces' file panes share it)."""
        target = _safe_path(root, path)
        if target.is_file():
            if target.stat().st_size > 512_000:
                return {"path": path, "kind": "file", "truncated": True, "content": target.read_text(errors="replace")[:512_000]}
            try:
                return {"path": path, "kind": "file", "content": target.read_text(encoding="utf-8")}
            except UnicodeDecodeError:
                return {"path": path, "kind": "binary", "size": target.stat().st_size}
        if not target.is_dir():
            raise HTTPException(404, "no such path")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in {".git", "__pycache__", "node_modules", ".venv"}:
                continue
            if _contained(root, str(child.relative_to(root.resolve()))) is None:
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append({"name": child.name, "dir": child.is_dir(), "size": stat.st_size, "mtime": stat.st_mtime})
        return {"path": path, "kind": "dir", "entries": entries}

    def _contained(root: Path, rel: str) -> Path | None:
        """``_safe_path`` as a filter: the real path when it is inside the pane, ``None`` when it is not.

        A search reaches paths nobody typed — whatever the walk or ripgrep turned up — so every one
        of them is put through the browser's own check rather than trusted for having come from
        under the root. A link out of the tree and a name that lands in the installation are both
        simply absent from the answer.
        """
        try:
            return _safe_path(root, rel)
        except HTTPException:
            return None

    def _walk_files(root: Path) -> Iterator[str]:
        """Every file under ``root`` as a root-relative path, skipping what the browser never shows."""
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in FILE_SEARCH_SKIP and not d.startswith("."))
            base = Path(folder).relative_to(root)
            for name in sorted(names):
                yield name if str(base) == "." else f"{base}/{name}"

    def _rg_lines(args: list[str], root: Path, budget: float, cancel: threading.Event | None = None) -> Iterator[str]:
        """Run ripgrep in ``root`` and hand back its output a line at a time, for at most ``budget`` seconds.

        The budget is enforced on the process, not between the lines it writes: a query with no
        matches writes nothing, and a reader waiting on that pipe consults no deadline of its own.
        The watchdog kills ripgrep when the budget is spent or when ``cancel`` is raised, the read
        ends by EOF, and the caller sees a short answer instead of a walk of the whole tree.

        The caller also stops at its own bounds, so the process must not be left to finish a walk
        nobody is reading: closing the generator kills it and drains the pipe, which is why every
        caller wraps this in ``closing``.
        """
        process = subprocess.Popen(
            args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True
        )
        stop_watch = _kill_after(process, budget, cancel)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.rstrip("\n")
                if stripped:
                    yield stripped
        finally:
            stop_watch()
            _end_search(process)
            if process.stdout is not None:
                process.stdout.close()
            process.wait()

    def _rg_file_list(root: Path, budget: float, cancel: threading.Event | None) -> Iterator[str] | None:
        """Ripgrep's own file list, which already honours .gitignore — ``None`` where ripgrep is not installed."""
        if shutil.which("rg") is None:
            return None
        args = ["rg", "--files", "--no-messages"]
        for name in sorted(FILE_SEARCH_SKIP):
            args += ["--glob", f"!{name}"]
        return _rg_lines(args, root, budget, cancel)

    def _search_names(root: Path, query: str, limit: int, cancel: threading.Event | None = None) -> tuple[list[dict[str, Any]], bool, str]:
        """Files and folders under ``root`` whose name matches ``query``: (results, truncated, engine).

        Bounded three ways at once — entries visited, results collected, and wall-clock — because
        this answers a key press in the explorer's filter box and a workspace with a node_modules in
        it is not a size the caller knows in advance. Whichever bound is reached first ends the
        search and sets ``truncated``; a partial answer arrives in the same shape as a whole one.

        Folders are found through the files under them, which is what both walkers produce: a folder
        with nothing in it is not something a file search is looking for.
        """
        deadline = time.monotonic() + FILE_SEARCH_BUDGET_SECONDS
        pattern = query.lower()
        is_glob = any(ch in query for ch in "*?[")

        def matches(name: str, rel: str) -> bool:
            if is_glob:
                return fnmatch.fnmatch(name.lower(), pattern) or fnmatch.fnmatch(rel.lower(), pattern)
            return pattern in name.lower()

        results: list[dict[str, Any]] = []
        offered: set[str] = set()

        def take(rel: str, kind: str) -> None:
            if rel in offered:
                return
            offered.add(rel)
            target = _contained(root, rel)
            if target is None:
                return
            try:
                stat = target.stat()
            except OSError:
                return
            results.append({"path": rel, "kind": kind, "size": stat.st_size, "mtime": stat.st_mtime})

        lister = _rg_file_list(root, FILE_SEARCH_BUDGET_SECONDS, cancel)
        truncated = False
        visited = 0
        with closing(lister if lister is not None else _walk_files(root)) as walker:
            for rel in walker:
                visited += 1
                if visited > FILE_SEARCH_MAX_ENTRIES or time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                    truncated = True
                    break
                parts = rel.split("/")
                for depth in range(1, len(parts)):
                    branch = "/".join(parts[:depth])
                    if matches(parts[depth - 1], branch):
                        take(branch, "dir")
                if matches(parts[-1], rel):
                    take(rel, "file")
                if len(results) >= limit:
                    truncated = True
                    break
        if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
            # The watchdog can end the read before one line has arrived, so the loop above may never
            # have looked at the clock. A search that ran out of time answers with what it has and
            # says so, whether the budget went between two hits or inside one silent walk.
            truncated = True
        return results[:limit], truncated, "rg" if lister is not None else "walk"

    def _search_content(root: Path, query: str, limit: int, cancel: threading.Event | None = None) -> tuple[list[dict[str, Any]], bool]:
        """Lines under ``root`` containing ``query``, as (hits, truncated).

        The query is matched literally, not as a pattern: the box it comes from is a search field,
        and a half-typed bracket there should find nothing rather than fail or run away. Binary
        files, files over the size cap and anything the browser hides are ripgrep's own defaults
        plus the same exclusions the tree uses.
        """
        deadline = time.monotonic() + FILE_GREP_BUDGET_SECONDS
        args = [
            "rg", "--line-number", "--no-heading", "--color", "never", "--no-messages",
            "--fixed-strings", "--smart-case", "--max-filesize", FILE_GREP_MAX_FILESIZE,
            "--max-columns", str(FILE_GREP_MAX_COLUMNS), "--max-columns-preview", "--null",
        ]
        for name in sorted(FILE_SEARCH_SKIP):
            args += ["--glob", f"!{name}"]
        args += ["--", query]
        hits: list[dict[str, Any]] = []
        truncated = False
        with closing(_rg_lines(args, root, FILE_GREP_BUDGET_SECONDS, cancel)) as lines:
            for line in lines:
                if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                    truncated = True
                    break
                # ``--null`` ends the path with a NUL rather than a colon, so a file whose own name
                # carries one is still read exactly; the line number is what follows, up to the
                # first colon after it.
                rel, sep, rest = line.partition("\0")
                number, _, text = rest.partition(":")
                if not sep or not rel or not number.isdigit() or _contained(root, rel) is None:
                    continue
                hits.append({"path": rel, "line": int(number), "text": text[:FILE_GREP_MAX_COLUMNS]})
                if len(hits) >= limit:
                    truncated = True
                    break
        if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
            # The watchdog can end the read before one line has arrived, so the loop above may never
            # have looked at the clock. A search that ran out of time answers with what it has and
            # says so, whether the budget went between two hits or inside one silent walk.
            truncated = True
        return hits, truncated

    def _file_response(root: Path, path: str) -> FileResponse:
        target = _safe_path(root, path)
        if not target.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            filename=target.name,
            headers={"Access-Control-Allow-Origin": "https://web.telegram.org"},
        )

    async def _store_uploads(root: Path, files: list[UploadFile], sub: str = "") -> list[str]:
        """Write uploads under ``root/sub`` without clobbering what is there; returns the names used."""
        folder = _safe_path(root, sub) if sub else root
        folder.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for upload_file in files:
            name = Path(upload_file.filename or "file").name
            target = folder / name
            counter = 1
            while target.exists():
                target = folder / f"{Path(name).stem}-{counter}{Path(name).suffix}"
                counter += 1
            with target.open("wb") as fh:
                while chunk := await upload_file.read(1 << 20):
                    fh.write(chunk)
            names.append(target.name)
        return names

    # -- project directory picker ---------------------------------------------------

    DIRECTORY_PICKER_MAX_ENTRIES = 250
    DIRECTORY_PICKER_BUDGET_SECONDS = 0.25

    def _picker_roots(project_roots: list[Path]) -> list[Path]:
        candidates = [Path.home(), settings.workspaces_dir, settings.state_dir.parent, *project_roots]
        roots: list[Path] = []
        for candidate in candidates:
            try:
                real = candidate.resolve()
            except OSError:
                continue
            if real in roots or sealed_root(str(real), [str(p) for p in manager.protected_paths()]) is not None:
                continue
            if real.is_dir() and os.access(real, os.R_OK):
                roots.append(real)
        return roots

    def _picker_path(raw_root: str, raw_path: str, offered: list[Path]) -> tuple[Path, Path]:
        if ".." in Path(raw_path).parts:
            raise HTTPException(400, "parent traversal is not allowed")
        root = Path(raw_root).expanduser().resolve() if raw_root else (offered[0] if offered else None)
        if root is None or root not in offered:
            raise HTTPException(403, "that browser root is not offered")
        candidate = Path(raw_path).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(403, "that directory escapes the browser root")
        if sealed_root(str(target), [str(p) for p in manager.protected_paths()]) is not None:
            raise HTTPException(403, "that directory belongs to the installation")
        if not target.is_dir():
            raise HTTPException(404, "no such directory")
        return root, target

    @api.get("/api/project-directories")
    async def project_directories(root: str = "", path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """List one safe, bounded directory level for the project folder picker."""
        known_projects = await manager.projects.list()
        folders = [folder for project in known_projects for folder in project.local_folders(manager.projects.local_env)]
        offered = _picker_roots([folder.path for folder in folders if folder.reachable])
        project_ids = {str(folder.path.resolve()): folder.project_id for folder in folders if folder.path.exists()}
        if not root and not path:
            return {
                "roots": [{"name": p.name or str(p), "path": str(p), "readable": True, "writable": os.access(p, os.W_OK), "project_id": project_ids.get(str(p))} for p in offered],
                "docker": not settings.native,
            }
        anchor, target = _picker_path(root, path, offered)
        entries: list[dict[str, Any]] = []
        truncated = False
        deadline = time.monotonic() + DIRECTORY_PICKER_BUDGET_SECONDS
        children = []
        try:
            with os.scandir(target) as scanner:
                for child in scanner:
                    if len(children) >= DIRECTORY_PICKER_MAX_ENTRIES or time.monotonic() >= deadline:
                        truncated = True
                        break
                    children.append(child)
        except OSError as exc:
            raise HTTPException(403, f"that directory cannot be read: {exc.strerror or 'permission denied'}") from exc
        for child in sorted(children, key=lambda entry: entry.name.lower()):
            try:
                if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                    continue
                real = Path(child.path).resolve()
                if anchor not in real.parents or sealed_root(str(real), [str(p) for p in manager.protected_paths()]) is not None:
                    continue
                entries.append({
                    "name": child.name,
                    "path": str(real),
                    "readable": os.access(real, os.R_OK),
                    "writable": os.access(real, os.W_OK),
                    "project_id": project_ids.get(str(real)),
                })
            except OSError:
                entries.append({"name": child.name, "path": str(target / child.name), "readable": False, "writable": False, "project_id": None})
        parents = []
        current = target
        while True:
            parents.append({"name": current.name or str(current), "path": str(current)})
            if current == anchor:
                break
            current = current.parent
        return {"root": str(anchor), "path": str(target), "parents": list(reversed(parents)), "entries": entries, "truncated": truncated}

    async def _files_root(session_id: str, folder_id: str, *, write: bool = False) -> Path:
        """The directory a session's file pane is rooted at: its workspace, or another folder of its project.

        Another folder opens only when the session's walls let it read that folder — a session with
        a directory of its own reads nothing but that directory, and a folder of the other environment
        is no folder of this process at all — and an upload only when they let it write there, so the
        pane cannot put a file where the agent itself could not.
        """
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        services = state.services
        if not folder_id:
            root = state.workspace
        else:
            folder = state.project.folder(folder_id) if state.project is not None else None
            if folder is None:
                raise HTTPException(404, "no such folder in this session's project")
            if services is None or not services.contains(folder.path):
                raise HTTPException(403, f"{folder.path} is not a folder this session can read")
            root = folder.path
        if write and services is not None and not services.contains(root, write=True):
            raise HTTPException(403, f"{root} is read-only for this session")
        return root

    def _pane_target(session_id: str, folder_id: str, root: Path, path: str) -> tuple[Path, str]:
        """The root and path the pane reads ``path`` from: the session's log scratch for its own logs.

        The app names a job's log ``.jobs/<id>.log`` under the session's folder, as it always was;
        when the folder is read-only the host kept that log in the state volume instead, and this is
        the one place the pane learns so. Every other path, and every path of a writable folder, is
        read where it was asked for.
        """
        services = manager.locator_services(session_id)
        if folder_id or services is None or services.log_file(path) is None or services.log_root is None:
            return root, path
        return services.log_root, path

    # Every route of the pane answers under two addresses: the session's own folder, and
    # ``/folders/{folder_id}`` for another folder of its project. The folder is part of the path rather
    # than a query parameter because the app builds each file's address as ``{base}/download?path=…``
    # in many places — the preview, the page preview's links, the panel's history, a download — and a
    # base that already names the folder reaches every one of them unchanged.

    @api.post("/api/sessions/{session_id}/files/upload")
    @api.post("/api/sessions/{session_id}/folders/{folder_id}/files/upload")
    async def session_files_upload(session_id: str, folder_id: str = "", path: str = Form(""), files: list[UploadFile] = File(default=[]), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Put files into the session's workspace, or another folder of its project, without a message to the agent."""
        root = await _files_root(session_id, folder_id, write=True)
        if not files:
            raise HTTPException(400, "no files")
        return {"files": await _store_uploads(root, files, path)}

    @api.get("/api/sessions/{session_id}/files")
    @api.get("/api/sessions/{session_id}/folders/{folder_id}/files")
    async def list_files(session_id: str, folder_id: str = "", path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return _read_path(*_pane_target(session_id, folder_id, await _files_root(session_id, folder_id), path))

    @api.get("/api/sessions/{session_id}/files/search")
    @api.get("/api/sessions/{session_id}/folders/{folder_id}/files/search")
    async def search_files(session_id: str, folder_id: str = "", q: str = "", limit: int = FILE_SEARCH_MAX_RESULTS, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Find a file by name anywhere in the session's tree — the explorer's filter box, unbounded by what it has loaded.

        ``q`` is a case-insensitive substring of the name, or a glob when it carries one of ``*?[``;
        a glob is matched against the name and against the path, so ``*.py`` and ``src/**/*.py``
        both work. Every answer goes through the browser's containment, so a symlink out of the
        tree and a path inside the installation are absent rather than refused.
        """
        root = await _files_root(session_id, folder_id)
        query = q.strip()
        if not query:
            return {"query": "", "results": [], "truncated": False, "engine": "none"}
        wanted = max(1, min(int(limit), FILE_SEARCH_MAX_RESULTS))
        results, truncated, engine = await _run_search(_search_names, root, query, wanted)
        return {"query": query, "results": results, "truncated": truncated, "engine": engine}

    @api.get("/api/sessions/{session_id}/files/grep")
    @api.get("/api/sessions/{session_id}/folders/{folder_id}/files/grep")
    async def grep_files(session_id: str, folder_id: str = "", q: str = "", limit: int = FILE_SEARCH_MAX_RESULTS, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Find a line by its text anywhere in the session's tree, with the line it is on.

        Content search is ripgrep's job and is not reimplemented in Python: an install without it
        gets 501 and a sentence saying so, which the panel shows in place of the results instead of
        offering a search that would walk the whole tree in the event loop.
        """
        root = await _files_root(session_id, folder_id)
        if shutil.which("rg") is None:
            raise HTTPException(501, "content search needs ripgrep (rg), which is not installed here; search by name instead")
        query = q.strip()
        if not query:
            return {"query": "", "hits": [], "truncated": False}
        wanted = max(1, min(int(limit), FILE_SEARCH_MAX_RESULTS))
        hits, truncated = await _run_search(_search_content, root, query, wanted)
        return {"query": query, "hits": hits, "truncated": truncated}

    @api.get("/api/sessions/{session_id}/download")
    @api.get("/api/sessions/{session_id}/folders/{folder_id}/download")
    async def download(session_id: str, path: str, folder_id: str = "", _: dict[str, Any] = Depends(auth)) -> FileResponse:
        return _file_response(*_pane_target(session_id, folder_id, await _files_root(session_id, folder_id), path))

    # -- the steer queue: what was sent to a working agent and has not reached it yet -------

    @api.get("/api/sessions/{session_id}/steer")
    async def list_steer(session_id: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Steers this session has taken in and not yet handed to the model, oldest first."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        return await manager.queued_steers(session_id)

    @api.delete("/api/sessions/{session_id}/steer/{msg_id}")
    async def drop_steer(session_id: str, msg_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Take a queued steer back. 409 once the run has read it: by then it is in the history, not in a queue."""
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if not await manager.drop_queued_steer(session_id, msg_id):
            raise HTTPException(409, "that message has already reached the agent")
        return {"deleted": True}

    # -- memory: what the agent remembered, per session and globally ----------------------

    def _memory_view(record: Any) -> dict[str, Any]:
        return {
            "id": record.id,
            "scope": record.scope.value,
            "scope_key": record.scope_key,
            "kind": record.kind,
            "text": record.text,
            "salience": record.salience,
            "version": record.version,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "last_accessed_at": record.last_accessed_at.isoformat() if getattr(record, "last_accessed_at", None) else None,
        }

    @api.get("/api/memory")
    async def memory_list(scope: str = "", scope_key: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every memory record (or one scope's), with the titles of the sessions the session-scoped ones belong to."""
        try:
            wanted = MemoryScope(scope) if scope else None
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {scope!r}") from exc
        records = manager.memory.records(TENANT, scope=wanted, scope_key=scope_key or None)
        titles = {row["id"]: row["title"] for row in await app.db.fetchall("SELECT id, title FROM sessions")}
        buckets: dict[str, dict[str, Any]] = {}
        for rec in manager.memory.records(TENANT):
            key = f"{rec.scope.value}:{rec.scope_key}"
            b = buckets.setdefault(key, {"scope": rec.scope.value, "scope_key": rec.scope_key, "title": titles.get(rec.scope_key) if rec.scope is MemoryScope.session else None, "count": 0})
            b["count"] += 1
        return {"records": [_memory_view(r) for r in records], "buckets": sorted(buckets.values(), key=lambda b: (b["scope"] != "global", -b["count"])), "sessions": titles}

    @api.post("/api/memory")
    async def memory_add(body: MemoryBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            scope = MemoryScope(body.scope)
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {body.scope!r}") from exc
        if scope is not MemoryScope.global_ and not body.scope_key:
            raise HTTPException(422, "scope_key is required for a non-global scope")
        try:
            result = await manager.memory.write(TENANT, scope, body.scope_key, body.text.strip(), kind=body.kind.strip() or "fact", metadata={"source": "operator"})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"decision": result.decision.value if hasattr(result.decision, "value") else str(result.decision), "record": _memory_view(result.record)}

    @api.patch("/api/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: MemoryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return _memory_view(await manager.memory.update(TENANT, memory_id, text=body.text, kind=body.kind))
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": await manager.memory.delete(TENANT, memory_id)}

    @api.post("/api/memory/delete")
    async def memory_delete_many(body: MemoryDeleteBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        deleted = 0
        for memory_id in body.ids:
            deleted += int(await manager.memory.delete(TENANT, memory_id))
        return {"deleted": deleted}

    # -- usage / balance / status ---------------------------------------------------

    @api.post("/api/usage/direct")
    async def record_direct_usage(body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A model call that bypassed the bot (curl against the key proxy) is booked like any other, at the provider's prices."""
        provider_id = str(body.get("provider_id") or "")
        model = str(body.get("model") or "")
        if not provider_id or not model:
            raise HTTPException(422, "provider_id and model are required")
        tokens = {k: int(body.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")}
        cost: float | None = None
        try:
            pricing = manager.providers.get(provider_id).endpoint.pricing_for(model)
            cost = pricing.cost(tokens) if pricing is not None else None
        except KeyError:
            cost = None
        await manager.usage.record(
            UsageRecord(provider_id=provider_id, model=model, purpose="direct", raw=dict(body), normalized={**tokens, "cost_usd": cost}, cost_usd=cost, duration_ms=int(body.get("duration_ms") or 0), run_id=None, session_id=None)
        )
        return {"recorded": True, "cost_usd": cost}

    @api.get("/api/usage")
    async def usage(days: int = 7, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await app.db.fetchall(
            "SELECT substr(at, 1, 10) day, provider_id, model, count(*) calls, sum(input_tokens) input_tokens,"
            " sum(output_tokens) output_tokens, sum(cache_read_tokens) cache_read_tokens,"
            " sum(reasoning_tokens) reasoning_tokens, sum(cost_usd) cost_usd, sum(cost_usd IS NULL) unmetered"
            " FROM usage_events WHERE at >= ? GROUP BY day, provider_id, model ORDER BY day DESC",
            (since,),
        )
        recent = await app.db.fetchall(
            "SELECT u.at, u.provider_id, u.model, u.purpose, u.session_id, u.run_id, u.input_tokens, u.output_tokens,"
            " u.cache_read_tokens, u.reasoning_tokens, u.cost_usd, u.duration_ms, u.raw, s.title session_title"
            " FROM usage_events u LEFT JOIN sessions s ON s.id = u.session_id ORDER BY u.seq DESC LIMIT 200"
        )
        by_session = await app.db.fetchall(
            "SELECT u.session_id, s.title, count(*) calls, sum(u.input_tokens) input_tokens, sum(u.output_tokens) output_tokens,"
            " sum(u.cost_usd) cost_usd, sum(u.cost_usd IS NULL) unmetered FROM usage_events u LEFT JOIN sessions s ON s.id = u.session_id"
            " WHERE u.at >= ? GROUP BY u.session_id ORDER BY cost_usd DESC NULLS LAST, calls DESC LIMIT 20",
            (since,),
        )
        return {
            "daily": [dict(r) for r in rows],
            "recent": [{**dict(r), "raw": json.loads(r["raw"])} for r in recent],
            "sessions": [dict(r) for r in by_session],
            "subscriptions": await subscription_usage(),
        }

    @api.get("/api/usage/provider/{provider_id}")
    async def usage_provider(provider_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One provider's day so far, its subscription windows when it is a login, its balance when it is metered."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await app.db.fetchone(
            "SELECT count(*) calls, sum(input_tokens) input_tokens, sum(output_tokens) output_tokens, sum(cache_read_tokens) cache_read_tokens,"
            " sum(cost_usd) cost_usd, sum(cost_usd IS NULL) unmetered FROM usage_events WHERE provider_id = ? AND at >= ?",
            (provider_id, today),
        )
        monitor = app.extensions.get("balance")
        balances = await monitor.current() if monitor is not None else {}  # type: ignore[attr-defined]
        subscriptions = await subscription_usage()
        return {"provider": provider_id, "today": dict(row) if row else {}, "subscription": subscriptions.get(provider_id), "balance": balances.get(provider_id)}

    async def subscription_usage() -> dict[str, Any]:
        """Quota windows of the Codex, Grok and Claude subscriptions, read from the key proxy that holds their logins."""
        origin = _keyproxy_origin()
        if not origin:
            return {}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(origin + "/subscriptions/usage")
            return response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    @api.get("/api/balance")
    async def balance(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        monitor = app.extensions.get("balance")
        current = await monitor.current() if monitor is not None else {}  # type: ignore[attr-defined]
        return {"balances": current, "thresholds": app.config.balance.thresholds_usd}

    @api.get("/api/status")
    async def status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        selfdev = app.extensions.get("selfdev")
        supervisor = await selfdev.supervisor_status() if selfdev is not None else None  # type: ignore[attr-defined]
        heartbeat = app.extensions.get("heartbeat")
        return {
            "model": app.config.model.model_dump(),
            "providers": list(manager.providers.available()),
            "supervisor": supervisor,
            "budget_exceeded": manager.budget_exceeded(),
            "sessions": await manager.list_sessions(limit=50),
            "notifications": await app.notifications.summary() if app.notifications is not None else {"unseen": 0, "needs_you": 0},
            "heartbeat": heartbeat.status() if heartbeat is not None else None,  # type: ignore[attr-defined]
        }

    @api.get("/api/system/launcher")
    async def launcher_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The desktop launcher beside this process: its version and the newer release it found."""
        return await launcher_updates.describe(app)

    @api.post("/api/system/launcher/check")
    async def launcher_check(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Look for a newer release now rather than at the launcher's next half-hourly check."""
        try:
            return await launcher_updates.check_now(app)
        except launcher_bridge.LauncherBusy as exc:
            raise HTTPException(409, str(exc)) from None
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.post("/api/system/launcher/download")
    async def launcher_download(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Download and check the offered release while the app stays in use."""
        try:
            return await launcher_updates.download_now(app)
        except launcher_bridge.LauncherBusy as exc:
            raise HTTPException(409, str(exc)) from None
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.post("/api/system/launcher/install")
    async def launcher_install(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Install the offered release from the desktop window. The launcher refuses with its reason
        (no window, Docker mode, a package-managed copy, nothing newer), and that reason is the answer."""
        try:
            await launcher_updates.install_now(app)
        except launcher_bridge.LauncherBusy as exc:
            raise HTTPException(409, str(exc)) from None
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return {"started": True}

    def selfdev_on() -> None:
        """The proposals API answers only where changes are proposed at all.

        With the mode off the answer is 404, not an empty list: an empty list reads like "no changes
        yet" and the app would keep a navigation entry for a screen that can never fill.
        """
        if caps.selfdev.mode == "off":
            raise HTTPException(404, "self-development is off in this installation")

    @api.get("/api/capabilities")
    async def capability_report(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What this installation can do and why — the app hides what is not there instead of offering it.

        The mode was resolved once at startup; whether a change is waiting for a restart was not, and it is
        read here on every poll. It rides on this answer rather than on a route of its own because the app
        already asks this question and a banner that needs a second poll is a banner that arrives late.
        """
        selfdev = app.extensions.get("selfdev")
        pending = selfdev.pending_change() if selfdev is not None else None  # type: ignore[attr-defined]
        last = selfdev.last_change() if selfdev is not None else None  # type: ignore[attr-defined]
        answer = replace(caps, restart_required=pending, last_change=last).as_dict()
        # The components ride here for the same reason the pending change does: the shell already asks
        # this question, and a tab that should be badged because a configured feature is missing its
        # runtime must not wait for a second poll to find out.
        # Off the loop: nine probes, two walks of the models tree, four `which` calls and, the first
        # time, a subprocess. Milliseconds, but this is the path every page polls.
        answer["components"] = await asyncio.to_thread(lambda: component_registry().summary())
        # Whether a browser answers now, beside whether one was configured at startup: the Browser tab
        # says "the browser service is not running" rather than hiding what the agent could use.
        browsers = app.extensions.get("browser")
        answer["browser"]["available"] = any(env["available"] for env in browsers.environments()) if browsers is not None else False  # type: ignore[attr-defined]
        return answer

    @api.post("/api/self/restart")
    async def self_restart(force: bool = False, _: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        """Apply the change the agent committed: the supervisor checks it and restarts onto it.

        The answer comes back before the restart does — this very process is what goes away — so it says
        what was started, and the app learns how it ended from the capabilities it polls afterwards.
        """
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(404, "self-development is off in this installation")
        await _refuse_restart_while_busy(force)
        return {"result": await selfdev.restart_to_apply("the operator asked the app to apply the change")}  # type: ignore[attr-defined]

    # -- doctor -------------------------------------------------------------------------

    def _doctor_context(fix: bool) -> DoctorContext:
        return DoctorContext(settings=settings, config=app.config, db=app.db, manager=manager, front=app.front, extensions=dict(app.extensions), extension_failures=dict(app.extension_failures), guard=app.guard, fix=fix)

    @api.get("/api/doctor")
    async def doctor(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        checks = await run_checks(_doctor_context(False))
        return {"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}

    @api.post("/api/doctor/fix")
    async def doctor_fix(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        checks = await run_checks(_doctor_context(True))
        return {"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}

    # -- notifications ------------------------------------------------------------------

    def notifications_service() -> NotificationService:
        if app.notifications is None:
            raise HTTPException(503, "notifications are not installed")
        return app.notifications

    @api.get("/api/notifications")
    async def notifications_list(
        view: Literal["all", "unseen", "problems", "needs_you"] = "all",
        project: str | None = Query(default=None, max_length=64),
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        return dict(await notifications_service().list(view, project_id=project, before=before, limit=limit))

    @api.get("/api/notifications/summary")
    async def notifications_summary(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return dict(await notifications_service().summary())

    @api.post("/api/notifications/seen")
    async def notifications_seen(body: NotificationsSeenBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = notifications_service()
        if sum((body.ids is not None, body.all, body.session_id is not None)) != 1:
            raise HTTPException(422, "name exactly one of ids, all or session_id")
        marked = await service.mark_seen(body.ids, everything=body.all, session_id=body.session_id)
        return {"marked": marked, "summary": await service.summary()}

    @api.delete("/api/notifications/{entry_id}")
    async def notifications_delete(entry_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = notifications_service()
        if not await service.delete(entry_id):
            raise HTTPException(404, "no such notification")
        return {"deleted": entry_id, "summary": await service.summary()}

    def installed_push() -> PushService | None:
        push = app.extensions.get("push")
        return push if isinstance(push, PushService) else None

    def push_service() -> PushService:
        push = installed_push()
        if push is None:
            raise HTTPException(503, "push is not installed")
        return push

    @api.post("/api/notifications/{entry_id}/act")
    async def notifications_act(entry_id: int, body: NotificationActBody, request: Request) -> Any:
        service = notifications_service()
        # Signed in, any action the entry offers. Otherwise only a pushed message's token, and with
        # it only the actions marked quick: what a lock screen may do, and a host-level permission
        # is never among them.
        quick_only = False
        try:
            await auth(request)
        except HTTPException as exc:
            push = installed_push()
            if exc.status_code != 401 or not body.token or push is None or not await push.verify_token(entry_id, body.token):
                raise
            quick_only = True
        push = installed_push()
        if body.via == "push" and push is not None:
            push.note_actor(entry_id, body.endpoint)
        try:
            resolution, view = await service.act(entry_id, body.action, body.value, via=body.via, quick_only=quick_only)
        except LookupError as exc:
            raise HTTPException(404, "no such notification") from exc
        except ActionRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except ActionConflict as exc:
            # The first answer wins; the second is shown what it was, with the entry as it now stands.
            return JSONResponse({"resolution": exc.resolution, "notification": await service.get(entry_id)}, status_code=409)
        return {"resolution": resolution, "notification": view}

    def _notification_preferences() -> dict[str, Any]:
        return {
            "preferences": app.config.notifications.model_dump(mode="json"),
            "revision": config_revision(app.config),
            "categories": list(NOTIFICATION_CATEGORIES),
            "zone": manager.presence.locale()[1],
        }

    @api.get("/api/notifications/preferences")
    async def notification_preferences(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return _notification_preferences()

    @api.put("/api/notifications/preferences")
    async def put_notification_preferences(body: NotificationPreferencesBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            preferences = NotificationsConfig.model_validate(body.preferences)
        except ValidationError as exc:
            raise HTTPException(400, {"problems": [{"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]}) from exc
        new_config = app.config.model_copy(update={"notifications": preferences})
        try:
            await app.save_config(new_config, expected_revision=body.base_revision)
        except ConfigConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
        return _notification_preferences()

    @api.post("/api/notifications/test")
    async def notifications_test(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"delivered": await notifications_service().test()}

    # -- web push -----------------------------------------------------------------------

    @api.get("/api/push/config")
    async def push_config(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Whether this address can take push subscriptions, and the key a browser subscribes with."""
        return await push_service().config()

    @api.get("/api/push/subscriptions")
    async def push_subscriptions(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"subscriptions": await push_service().devices()}

    @api.post("/api/push/subscriptions")
    async def push_subscribe(body: PushSubscriptionBody, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        push = push_service()
        if push.reason():
            raise HTTPException(409, push.reason())
        try:
            view = await push.subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, device=body.device, user_agent=request.headers.get("user-agent", ""))
        except PushRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"subscription": view}

    @api.post("/api/push/subscriptions/renew")
    async def push_renew(body: PushRenewBody) -> dict[str, Any]:
        """Unauthenticated on purpose: the service worker renewing a subscription has no sign-in, and
        the old subscription's secret is the proof. It can only replace a device, never add one."""
        push = push_service()
        try:
            view = await push.renew(body.old_endpoint, body.old_auth, body.endpoint, body.keys.p256dh, body.keys.auth)
        except PermissionError as exc:
            raise HTTPException(403, "no such subscription") from exc
        except PushRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"subscription": {"id": view["id"]}}

    @api.delete("/api/push/subscriptions/{subscription_id}")
    async def push_unsubscribe(subscription_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await push_service().remove(subscription_id):
            raise HTTPException(404, "no such subscription")
        return {"deleted": subscription_id}

    # -- heartbeat ------------------------------------------------------------------------

    @api.get("/api/heartbeat")
    async def heartbeat_get(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        return {**heartbeat.status(), "template": HEARTBEAT_TEMPLATE}  # type: ignore[attr-defined]

    @api.put("/api/heartbeat")
    async def heartbeat_put(body: HeartbeatBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        if body.text is not None:
            heartbeat.write(body.text)  # type: ignore[attr-defined]
        patch = {k: v for k, v in body.model_dump().items() if k != "text" and v is not None}
        if patch:
            if patch.get("preset") and patch["preset"] not in app.config.presets:
                raise HTTPException(400, f"no such model preset {patch['preset']!r}")
            try:
                heartbeat_config = HeartbeatConfig.model_validate({**app.config.heartbeat.model_dump(), **patch})
            except ValueError as exc:
                raise HTTPException(400, "; ".join(str(e.get("msg")) for e in getattr(exc, "errors", lambda: [])()) or "invalid heartbeat settings") from exc
            new_config = app.config.model_copy(update={"heartbeat": heartbeat_config})
            await app.save_config(new_config)
            if app.front is not None:
                app.front.config = new_config
        return heartbeat.status()  # type: ignore[attr-defined]

    @api.post("/api/heartbeat/run")
    async def heartbeat_run(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            raise HTTPException(503, "heartbeat is not installed")
        try:
            return {"session_id": await heartbeat.fire(manual=True)}  # type: ignore[attr-defined]
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- proposals ------------------------------------------------------------------

    @api.get("/api/proposals")
    async def proposals(_: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> list[dict[str, Any]]:
        rows = await app.db.fetchall("SELECT * FROM change_proposals ORDER BY created_at DESC LIMIT 100")
        return [dict(r) for r in rows]

    @api.get("/api/proposals/{proposal_id}/diff")
    async def proposal_diff(proposal_id: str, _: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        row = await app.db.fetchone("SELECT * FROM change_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise HTTPException(404, "no such proposal")
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(503, "self-development is not installed")
        spec = selfdev.repo(row["repo"])  # type: ignore[attr-defined]
        try:
            diff = await selfdev.gh("pr", "diff", str(row["pr_number"]), cwd=spec.checkout)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not fetch the diff: {exc}") from exc
        return {"id": proposal_id, "diff": diff[:400_000]}

    @api.post("/api/proposals/{proposal_id}/decide")
    async def decide(proposal_id: str, body: DecisionBody, _: dict[str, Any] = Depends(auth), __: None = Depends(selfdev_on)) -> dict[str, Any]:
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            raise HTTPException(503, "self-development is not installed")
        if body.decision not in ("approve", "reject"):
            raise HTTPException(400, "decision must be approve or reject")
        return {"result": await selfdev.decide(proposal_id, body.decision, reason=body.reason)}  # type: ignore[attr-defined]

    # -- schedules ------------------------------------------------------------------

    @api.get("/api/schedules")
    async def schedules(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        scheduler = app.extensions.get("scheduler")
        return await scheduler.list() if scheduler is not None else []  # type: ignore[attr-defined]

    @api.post("/api/schedules")
    async def create_schedule(body: ScheduleBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        try:
            return await scheduler.create(name=body.name, prompt=body.prompt, cron=body.cron, run_at=body.run_at, model=body.model, kind=body.kind, target_session=body.target_session, run_in=body.run_in)  # type: ignore[attr-defined]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.patch("/api/schedules/{schedule_id}")
    async def patch_schedule(schedule_id: str, body: SchedulePatchBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        fields = body.model_dump(exclude_unset=True)
        try:
            return await scheduler.update(schedule_id, **fields)  # type: ignore[attr-defined]
        except KeyError as exc:
            raise HTTPException(404, "no such schedule") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            raise HTTPException(503, "scheduler is not installed")
        return {"deleted": await scheduler.delete(schedule_id)}  # type: ignore[attr-defined]

    @api.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        scheduler = app.extensions.get("scheduler")
        row = await app.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if scheduler is None or row is None:
            raise HTTPException(404, "no such schedule")
        return {"session_id": await scheduler.fire(dict(row))}  # type: ignore[attr-defined]

    # -- settings -------------------------------------------------------------------

    def _settings_view() -> dict[str, Any]:
        data = app.config.model_dump(mode="json")
        data["revision"] = config_revision(app.config)
        data["providers_available"] = list(manager.providers.available())
        data["usd_per_day"] = settings.usd_per_day
        data["prompt"]["default_rules"] = DEFAULT_RULES.strip()
        data["search_backends"] = websearch.catalogue()
        # What an empty orchestrator preset means, so the setting can show it preselected.
        data["orchestrator"]["strongest"] = app.config.strongest_preset() or ""
        # And what an empty main orchestrator preset means: a mid-tier one.
        data["dispatcher"]["middle"] = app.config.middle_preset() or ""
        # What a preset left to its model does with the on-demand tool groups, so the switch can say it.
        data["on_demand_defaults"] = {pid: on_demand_tool_groups_for(None, preset.model) for pid, preset in app.config.presets.items()}
        return mask_provider_keys(data)

    def _keyproxy_origin() -> str:
        """The one address this bot sends its own API token to, or "" when there is nothing to ask.

        It is the address the launcher gave this process, never one read back out of a provider
        entry: the provider list is written through this API, so deriving it from there turned
        "can add a model" into "can be handed the admin token" — the caller chose the host.
        """
        base = keyproxy_base()
        return base if any(is_keyproxy_url(p.base_url) for p in app.config.providers.values()) else ""

    upstreams_cache: dict[str, Any] = {"at": 0.0, "value": None, "client": None}

    def keyproxy_client() -> httpx.AsyncClient:
        """One client for the health probe, kept for the life of the app rather than built per call."""
        client = upstreams_cache["client"]
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=5.0)
            upstreams_cache["client"] = client
        return client

    async def keyproxy_keys() -> dict[str, dict[str, Any]] | None:
        """Per upstream, whether the key proxy really holds a credential and of what kind; None when it cannot be asked.

        The proxy is the only process that knows: the bot's own configuration says which address an
        endpoint is reached at, never whether anything behind it can authenticate. Asking the
        configuration instead is how an installation with one key came to report six ready
        endpoints, five of which answered 404 to the first request made of them.

        ``/keys`` answers the bot alone, on the bot's own API token — which the proxy reads out of
        the same database — so nothing else on the loopback interface can enumerate the credentials.
        Cached for a few seconds and asked through one client: this is on the path of the first
        screen, and a key proxy that hangs made every app load wait out the timeout again.
        """
        origin = _keyproxy_origin()
        if not origin:
            return None
        if upstreams_cache["at"] and time.monotonic() - float(upstreams_cache["at"]) < KEYPROXY_CACHE_SECONDS:
            return upstreams_cache["value"]  # type: ignore[return-value]
        try:
            response = await keyproxy_client().get(origin + "/keys", headers={"x-daedalus-token": api_token})
            listed = response.json().get("upstreams") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        value = None
        if isinstance(listed, dict):
            value = {str(name): {"configured": bool(row.get("configured")), "kind": str(row.get("kind") or "api_key")} for name, row in listed.items() if isinstance(row, dict)}
        upstreams_cache.update(at=time.monotonic(), value=value)
        return value

    async def keyproxy_upstreams() -> list[str] | None:
        """The upstream names the proxy holds a credential for; None when it cannot be asked."""
        keys = await keyproxy_keys()
        return None if keys is None else sorted(name for name, row in keys.items() if row["configured"])

    def _provider_view(pid: str, pc: ProviderConfig, usable: set[str], keys: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
        """One endpoint as the app shows it: its address, and whether a credential for it really exists.

        ``key_held`` is ``None`` only when nothing could answer the question — an endpoint reached
        through the key proxy while the proxy is unreachable. Everything else is a fact: the proxy
        said so, or the endpoint is reached directly and the configuration is the whole truth about
        it. ``key_kind`` says what a missing credential would be, which decides what the app tells
        the operator to do about it.

        An endpoint that may be the key proxy at an address this process was never given is the
        third case: it is not asked — the token goes to one address only — and it is not guessed
        about either, so it reads as unknown rather than as ready.
        """
        via_proxy = is_keyproxy_url(pc.base_url)
        if via_proxy:
            row = (keys or {}).get(keyproxy_upstream(pc.base_url))
            key_held = None if keys is None else bool(row and row["configured"])
            key_kind = str(row["kind"]) if row else "api_key"
        elif keyproxy_unresolved(pc.base_url):
            key_held = None
            key_kind = "api_key"
        else:
            # Reached directly: the registry builds an adapter only for an endpoint that can
            # authenticate, so being in `usable` is the answer, and an endpoint that needs no key
            # (a self-hosted one) is ready without holding anything.
            key_held = pid in usable
            key_kind = "api_key" if (pc.api_key or pc.kind in ("deepseek", "openrouter", "opencode")) else "endpoint"
        return {
            "id": pid,
            "kind": pc.kind,
            "name": pc.name,
            "base_url": pc.base_url,
            "via_proxy": via_proxy,
            "key_held": key_held,
            "key_kind": key_kind,
            "ready": pid in usable and key_held is not False,
        }

    @api.get("/api/onboarding")
    async def onboarding(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What this installation still needs before it can run anything.

        A fresh install has provider endpoints but no model — a provider is an address, not a
        choice of model — so the app opens Add a model instead of a chat that cannot answer.
        """
        usable = set(manager.providers.available())
        keys = await keyproxy_keys()
        providers = [_provider_view(pid, pc, usable, keys) for pid, pc in app.config.providers.items()]
        needs = [n for n, missing in (("provider_key", not any(p["ready"] for p in providers)), ("model", not app.config.has_model)) if missing]
        default = app.config.default_preset()
        return {
            "has_model": app.config.has_model,
            "presets": len(app.config.presets),
            "default_preset": default[0] if default else "",
            "providers": providers,
            "needs": needs,
            "message": "" if app.config.has_model else NO_MODEL_MESSAGE,
        }

    @api.get("/api/settings")
    async def get_settings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        view = _settings_view()
        keyed = await keyproxy_upstreams()
        for entry in view["search_backends"]:
            entry["available"] = True if not entry["needs_key"] else (None if keyed is None else entry["id"] in keyed)
        return view

    def _settings_candidate(body: SettingsBody) -> dict[str, Any]:
        current = app.config.model_dump(mode="json")
        dumped = body.model_dump(exclude_none=True)
        dumped.pop("base_revision", None)
        model_patch = dumped.pop("model", None)
        if isinstance(model_patch, dict):
            resolve_model_patch(current, model_patch)
        if isinstance(dumped.get("mcp"), dict):
            restore_masked_mcp(current, dumped["mcp"])
        restore_masked_secrets(current, dumped)
        for section in ("modes", "webhooks"):
            if isinstance(dumped.get(section), dict):
                current[section] = dumped.pop(section)
        for section, value in dumped.items():
            if isinstance(value, dict):
                current[section] = _deep_merge(current.get(section, {}), value)
            else:
                current[section] = value
        return current

    @api.post("/api/settings/validate")
    async def validate_settings(body: SettingsValidationBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        report, _candidate = validate_candidate(app.config, _settings_candidate(body.candidate), base_revision=body.base_revision)
        return report.model_dump(mode="json")

    @api.post("/api/settings/search-check")
    async def search_check(body: SearchCheckBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Run one query through a search backend and report what came back; the Mini App's "check" button."""
        if body.backend and body.backend not in websearch.BACKENDS:
            raise HTTPException(400, f"unknown backend {body.backend!r}")
        query = websearch.SearchQuery(text=body.query.strip() or "searxng json api", limit=5)
        outcome = await websearch.search(query, app.config.tools.web, chain=[body.backend] if body.backend else None)
        return {
            "backend": outcome.backend,
            "count": len(outcome.hits),
            "attempts": [{"backend": a.backend, "hits": a.hits, "error": a.error, "ms": a.ms} for a in outcome.attempts],
            "hits": [{"title": h.title, "url": h.url, "source": h.source, "published": h.published} for h in outcome.hits],
        }

    @api.put("/api/settings")
    async def put_settings(body: SettingsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if body.base_revision is None:
            raise HTTPException(409, "settings revision is required; reload and try again")
        report, new_config = validate_candidate(app.config, _settings_candidate(body), base_revision=body.base_revision)
        if report.stale:
            raise HTTPException(409, {"message": "settings changed in another window", "current_revision": report.base_revision})
        if not report.valid or new_config is None:
            problems = [problem.model_dump(mode="json") for problem in report.problems]
            if report.problems and all(problem.path in UNKNOWN_REFERENCE_PATHS for problem in report.problems):
                # A well-formed id that names nothing: 422 with the message as the detail, so the app shows it as it is.
                raise HTTPException(422, "; ".join(problem.message for problem in report.problems))
            raise HTTPException(400, {"problems": problems})
        switching_to_topics = app.front is not None and app.front.private_mode() and new_config.telegram.session_mode() == "topics"
        try:
            await app.save_config(new_config, expected_revision=body.base_revision)
        except ConfigConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
            if switching_to_topics:
                # Sessions opened in the private chat have no topic; without one they would all
                # speak in General at once, with nothing saying which is which.
                await app.front.adopt_sessions_into_topics()
        return _settings_view()

    # -- provider endpoints ----------------------------------------------------------------

    async def _save_provider_config(new_config: RuntimeConfig) -> dict[str, Any]:
        await app.save_config(new_config)
        await manager.providers.close_retired()
        if app.front is not None:
            app.front.config = new_config
        return _settings_view()

    def _reference_check(config: RuntimeConfig, provider_id: str) -> str | None:
        users = [pid for pid, preset in config.presets.items() if preset.provider == provider_id]
        if users:
            return f"model preset(s) {', '.join(users)} use it"
        return None

    @api.put("/api/providers/{provider_id}")
    async def put_provider(provider_id: str, body: ProviderPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            return _settings_view()
        raw = app.config.model_dump(mode="json")
        apply_provider_patch(raw.setdefault("providers", {}), provider_id, patch)
        try:
            new_config = type(app.config).model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        if not new_config.providers[provider_id].base_url:
            raise HTTPException(400, "base_url is required for a provider endpoint")
        return await _save_provider_config(new_config)

    @api.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if provider_id not in app.config.providers:
            raise HTTPException(404, "no such provider")
        reason = _reference_check(app.config, provider_id)
        if reason:
            raise HTTPException(400, f"cannot delete {provider_id!r}: {reason}. Point the model settings elsewhere first.")
        raw = app.config.model_dump(mode="json")
        del raw["providers"][provider_id]
        new_config = type(app.config).model_validate(raw)
        return await _save_provider_config(new_config)

    @api.post("/api/providers/lookup-models")
    async def lookup_provider_models(body: ModelsLookupBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """List the models served at an OpenAI-compatible endpoint (``{base}/models``).

        Probes from the bot (a Mini App in a browser or Telegram cannot reach LAN
        addresses), with an optional bearer key. ``base_url`` in the reply is the exact
        root the list was found at (``/v1`` appended when the caller omitted it), ``models``
        the ids, and ``entries`` the ids with the context window, modalities, reasoning
        support and prices the endpoint reported — what Add a model fills the form from.
        """
        base_url, api_key = body.base_url, body.api_key
        if body.provider:
            provider_config = app.config.providers.get(body.provider)
            if provider_config is None:
                raise HTTPException(404, f"no such provider {body.provider!r}")
            base_url = provider_config.base_url or (settings.vllm_base_url if provider_config.kind == "vllm" else "")
            api_key = provider_config.api_key or {
                "deepseek": settings.deepseek_api_key,
                "openrouter": settings.openrouter_api_key,
                "vllm": settings.vllm_api_key,
            }.get(provider_config.kind, "") or None
        if not base_url:
            raise HTTPException(400, "base_url is required (or a provider with one configured)")
        if body.provider and is_keyproxy_url(base_url):
            # Asking an endpoint for its models before asking whether anything can authenticate to
            # it produces three HTTP errors and a wall of URLs, and none of them says "no key".
            keys = await keyproxy_keys()
            row = (keys or {}).get(keyproxy_upstream(base_url))
            if keys is not None and not (row and row["configured"]):
                raise HTTPException(400, no_credential(str(row["kind"]) if row else "api_key", body.provider))
        if body.provider and provider_config.kind == "llamacpp":
            discovered = await discover_llamacpp(base_url, api_key)
            if not discovered.reachable or not discovered.model_id:
                raise HTTPException(502, discovered.reason or "llama.cpp did not identify a model")
            entry: dict[str, Any] = {"id": discovered.model_id}
            if discovered.context_window is not None:
                entry["context_length"] = discovered.context_window
            if discovered.images is not None:
                entry["images"] = discovered.images
                entry["input_modalities"] = ["text", "image"] if discovered.images else ["text"]
            return {
                "base_url": discovered.base_url,
                "models": [discovered.model_id],
                "entries": [entry],
                "discovery": discovered.as_dict(),
            }
        try:
            found = await lookup_openai_models(base_url, api_key)
        except ValueError as exc:
            message = str(exc)
            status = 400 if message.startswith("base_url") else 502
            raise HTTPException(status, message) from exc
        if is_keyproxy_url(base_url) and keyproxy_upstream(base_url) == "codex":
            manager = app.extensions.get("harness")
            store = getattr(manager, "store", None)
            if store is not None:
                rows = await store.catalog_rows()
                extend_model_list(found, [model for row in rows if row.harness == "codex" for model in row.models])
        return found

    # -- model presets -------------------------------------------------------------------

    @api.put("/api/presets/{preset_id}")
    async def put_preset(preset_id: str, body: PresetPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Create or edit a named model: client, model id, label, thinking, effort, images, window, output cap."""
        if not PRESET_ID_RE.fullmatch(preset_id):
            raise HTTPException(400, "preset id: letters, digits, . _ - (max 64)")
        raw = app.config.model_dump(mode="json")
        entry = dict(raw.setdefault("presets", {}).get(preset_id) or ModelPresetConfig().model_dump(mode="json"))
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is not None or key == "on_demand_tool_groups":
                entry[key] = value.strip() if isinstance(value, str) else value
        if entry["provider"] not in raw.get("providers", {}):
            raise HTTPException(400, f"provider {entry['provider']!r} is not a configured client")
        if not entry["model"]:
            raise HTTPException(400, "a preset needs a model id")
        raw["presets"][preset_id] = entry
        model = raw.setdefault("model", {})
        if str(model.get("preset") or "") not in raw["presets"]:
            # The first model added is the one that runs: an install is not finished until one is.
            model["preset"] = preset_id
        vision = raw.setdefault("vision", {})
        if entry.get("images") and str(vision.get("preset") or "") not in raw["presets"]:
            vision["preset"] = preset_id
        try:
            new_config = type(app.config).model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        return await _save_provider_config(new_config)

    @api.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if preset_id not in app.config.presets:
            raise HTTPException(404, "no such preset")
        last = len(app.config.presets) == 1
        if app.config.model.preset == preset_id and not last:
            raise HTTPException(400, "this preset is the global default; pick another default first")
        if app.config.vision.preset == preset_id and not last:
            raise HTTPException(400, "this preset is the ImageView model; pick another in Settings → Tools first")
        raw = app.config.model_dump(mode="json")
        del raw["presets"][preset_id]
        raw["model"]["chain"] = [c for c in raw["model"].get("chain", []) if c != preset_id]
        # Removing the last model is allowed: an installation with none is a state the app knows —
        # it asks for one — and refusing would leave a wrong entry no one can take out.
        for section in ("model", "vision", "voice", "orchestrator", "dispatcher"):
            if raw[section].get("preset") == preset_id:
                raw[section]["preset"] = ""
        return await _save_provider_config(type(app.config).model_validate(raw))

    # -- static mini app ------------------------------------------------------------

    dist = settings.bot_repo_dir / "miniapp" / "dist"
    if dist.is_dir():
        api.mount("/app", SpaFiles(directory=str(dist), html=True), name="miniapp")

        @api.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse("/app/")
    else:

        @api.get("/")
        async def root_missing() -> JSONResponse:
            return JSONResponse({"detail": "Mini App is not built; run `npm run build` in miniapp/"}, status_code=503)

    return api


class _AppServer(uvicorn.Server):
    """uvicorn, with the stop signal it takes for itself handed on to the application as well.

    While it serves, uvicorn replaces the SIGINT, SIGTERM and (on Windows) SIGBREAK handlers with its
    own and gives the signal back only once it has finished serving. On Linux the application heard
    the signal anyway, through the event loop's wakeup descriptor. On Windows its handler is a plain
    ``signal.signal`` one, which uvicorn's replaced: the launcher's CTRL_BREAK reached uvicorn alone,
    uvicorn waited for the open event streams to end — the app keeps one open for as long as it is
    looked at — and the bot was killed 25 seconds later, which its next start counted as a crash.
    """

    def __init__(self, config: uvicorn.Config, on_exit: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_exit = on_exit

    def handle_exit(self, sig: int, frame: Any) -> None:
        super().handle_exit(sig, frame)
        self._on_exit()


async def install(app: Application) -> list[asyncio.Task[None]]:
    token = await app.db.kv_get("api_token")
    if not token:
        token = secrets.token_urlsafe(24)
        await app.db.kv_set("api_token", token)
    api = build_app(app, token)
    # uvicorn's own 20 s WebSocket pings stay on: they are what notices a phone that dropped off the
    # network while a terminal was open. The size cap is the largest frame a terminal socket takes.
    config = uvicorn.Config(api, host=app.settings.api_host, port=app.settings.api_port, log_level="warning", access_log=False, ws_max_size=TERMINAL_WS_MAX_BYTES)
    loop = asyncio.get_running_loop()
    server = _AppServer(config, lambda: loop.call_soon_threadsafe(app.stopping.set))
    app.extensions["api_token"] = token
    app.extensions["api_server"] = server
    base = app.settings.miniapp_public_url or f"http://127.0.0.1:{app.settings.api_port}"
    # An installation with no other way in gets one at start: a link in a file only the operator can
    # read (never in the log, which is copied around and which the agent's own tools can read).
    # Anything else — a bot, an enrolled passkey — is a way in already; a fresh link is one
    # `daedalus auth pair` away.
    if not app.settings.telegram_bot_token and await passkeys.count(app.db) == 0:
        await pairing.announce(app.db, app.settings.state_dir, base)
        logger.warning("pairing link written to %s (opens once; `daedalus auth pair` makes another)", app.settings.state_dir / pairing.URL_FILE)
    else:
        # A link a previous start left behind is at best expired and at worst a way in nobody asked for.
        (app.settings.state_dir / pairing.URL_FILE).unlink(missing_ok=True)
    if app.front is not None:

        async def cmd_app(message, command) -> None:  # type: ignore[no-untyped-def]
            await message.answer(f"Mini App: {base}/app/\nAPI token (for scripts): {token}")

        app.front.command_hooks["app"] = cmd_app

        async def cmd_doctor(message, command) -> None:  # type: ignore[no-untyped-def]
            fix = (command.args or "").strip().lower() == "fix"
            ctx = DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=app.manager, front=app.front, extensions=dict(app.extensions), extension_failures=dict(app.extension_failures), guard=app.guard, fix=fix)
            checks = await run_checks(ctx)
            outbox = TelegramOutbox(app.front.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)  # type: ignore[union-attr]
            for chunk in split_message(redact.redact(render_text(checks))):
                await outbox.send_text(chunk, markdown=False)

        app.front.command_hooks["doctor"] = cmd_doctor
    return [asyncio.create_task(server.serve(), name="api-server")]


__all__ = ["build_app", "install", "message_view", "validate_init_data"]
