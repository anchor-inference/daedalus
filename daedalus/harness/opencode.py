"""OpenCode (major version 1) as a staff member: its TUI in a terminal, driven through the HTTP server
the TUI carries inside it.

Measured against OpenCode 1.18.23 in a throwaway home with a free model; the event stream, the
screens and a session's messages are in ``tests/support/fake_cli/recorded/opencode`` and the fake
OpenCode replays their shapes. What matters here:

- **One process.** ``opencode <dir> --port P --hostname 127.0.0.1`` serves on P from inside the TUI.
  ``OPENCODE_SERVER_PASSWORD`` (random per launch) puts it behind basic authentication — without it
  any process on the machine could drive the session. The host reaches the port through the
  daemon's ``net.dial``, the port being one the launch registered.
- **The launch's configuration** rides in ``OPENCODE_CONFIG_CONTENT``: the team bridge as a local MCP
  server (OpenCode passes its own environment on, so the launch's variables reach it; its hello came
  with the token), the member's instructions as an ``instructions`` file and the ``daedalus-team``
  skill under ``skills.paths`` (both read by the model: it answered with the word only the file
  held, and knew the skill), permissions by the project's autonomy, and the launch directory
  readable for messages sent by pointer.
- **Messages** go by ``POST /session/:id/prompt_async`` with a ``messageID`` of the host's choosing,
  which OpenCode keeps as the user message's id (measured); the ``message.updated`` event for it is
  the acknowledgement, and the TUI shows the message as it does one typed there. The model is named
  on each prompt when the member has one: a session made over HTTP does not take the TUI's ``-m``
  (measured: it fell back to the provider's default, a model that could not run tools).
- **Status** comes from ``session.status`` (``busy``/``idle``) and ``session.idle``; a
  ``session.error`` before the idle makes the turn failed (``MessageAbortedError``: cancelled).
- **Requests**: ``permission.asked`` is answered with ``POST /permission/:id/reply`` (``once``,
  ``always``, ``reject``), measured; questions with ``POST /question/:id/reply``. Answered in the TUI
  instead, ``permission.replied`` arrives without the host having replied.
- **No steering**: a message for a running turn waits for it to end (the capability says so, and the
  receipt names the degradation).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import random
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daedalus.harness import register
from daedalus.harness.capabilities import capabilities
from daedalus.harness.contract import (
    LAUNCH_DIR,
    Answer,
    Catalog,
    CheckResult,
    CheckStep,
    Delivery,
    EnvironmentPort,
    EventKind,
    InstallInfo,
    Launch,
    LaunchPlan,
    LaunchSpec,
    LoginState,
    ReadyStep,
    ScreenClass,
    SendMode,
    StaffEvent,
    TerminalPort,
    ToolUse,
    Turn,
    TurnUsage,
    UpdateResult,
)
from daedalus.harness.streams import EventStream, HttpAnswer, http_request
from daedalus.harness.team import SKILL_NAME
from daedalus.harness.tools import tooling

logger = logging.getLogger(__name__)

SYSTEM_FILE = "system.md"
SKILLS_DIR = "skills"
SKILL_FILE = f"{SKILLS_DIR}/{SKILL_NAME}/SKILL.md"
CONNECT_S = 60.0
"""How long the host keeps asking the TUI's server for its health while the TUI starts."""
PROBE_S = 2.0
"""How long one health request may go unanswered before it is given up and asked again on a fresh
connection. The TUI accepts connections a moment before its server can answer them, and the first
one accepted in that moment is never answered (measured on 1.18.34, three starts out of three: held
past 120 s while the next one, a second later, was answered at once; a 1.18.23 launch stalled the
same way). Waiting on it without a bound left the member never ready."""
SETUP_S = 30.0
"""How long the steps after the health check (the event stream, the session, showing it in the TUI)
may take together, so a server that stops answering fails the launch by name instead of silently."""
TIMEOUT_SLACK_MS = 60_000
PERMISSIONS = {
    "ask": {"edit": "ask", "bash": "ask", "webfetch": "ask"},
    "edits": {"edit": "allow", "bash": "ask", "webfetch": "ask"},
    "all": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
}
"""The project's autonomy as OpenCode's permission rules; OpenCode itself allows everything."""
DIALOG_MARKERS = ("Permission required", "Allow once", "Allow always")
BUSY_HINTS = ("esc interrupt", "esc to interrupt", "esc again to interrupt")
IDLE_HINTS = ("ctrl+p", "enter send", "Ask anything")
"""The footer of an idle TUI; at the width a launch gets it wraps, "ctrl+p" and "commands" on two lines."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class _Server:
    port: int
    password: str
    title: str
    model: str
    agent: str
    resume: str


@dataclass(eq=False)
class _Launch:
    queue: asyncio.Queue[StaffEvent | None] = field(default_factory=asyncio.Queue)
    server: _Server | None = None
    session: str = ""
    busy: bool = False
    failure: str = ""
    cancelled: bool = False
    sent: dict[str, tuple[str, str]] = field(default_factory=dict)
    """OpenCode message id → (the staff message, the text), for the acknowledgement."""
    seen: set[str] = field(default_factory=set)
    assistant: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    last_message: str = ""
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    answered: set[str] = field(default_factory=set)
    stopping: bool = False
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


def _server_from(stored: str) -> _Server | None:
    """The server a launch row remembers, or ``None`` when it remembers none (or something else)."""
    try:
        values = json.loads(stored) if stored else None
        return _Server(**values) if isinstance(values, dict) else None
    except (ValueError, TypeError):
        return None


def split_model(model: str) -> tuple[str, str]:
    """``provider/model`` as OpenCode's two parts; the model's own name may hold slashes."""
    provider, _, name = model.partition("/")
    return (provider, name) if name else ("", provider)


@register
class OpenCodeAdapter:
    """:class:`daedalus.harness.contract.HarnessAdapter` for OpenCode 1.x. See the module's description."""

    name = "opencode"
    capabilities = capabilities("opencode")

    def __init__(self) -> None:
        self.tooling = tooling("opencode")
        self._planned: dict[str, _Server] = {}
        self._launches: dict[str, _Launch] = {}
        self._live: dict[str, TerminalPort] = {}
        """Session id → the terminal of the launch that runs it, for reading a live transcript."""

    # -- the manager's half ---------------------------------------------------------------------

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return await self.tooling.installed(env)

    async def latest(self, env: EnvironmentPort) -> str:
        return ""

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        """Only ever to a named 1.x version, which the harness manager chooses; a bare upgrade goes
        to the next major, a different program."""
        info = await self.installed(env)
        return UpdateResult(False, info.version, info.version, error="OpenCode is updated by the harness manager to a named 1.x version")

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        info = await self.installed(env)
        return CheckResult(info.installed, (CheckStep("version", info.installed, info.version or info.detail), CheckStep("session", True, "run by the harness manager", skipped=True)), info.version, 0)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        return await self.tooling.catalog(env, cwd)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        return await self.tooling.login_state(env)

    # -- launching ------------------------------------------------------------------------------

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan:
        return self._plan(spec, "")

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return self._plan(spec, ref)

    def _plan(self, spec: LaunchSpec, resume: str) -> LaunchPlan:
        low, high = spec.port_range
        port = random.randint(low, high)
        server = _Server(port, secrets.token_urlsafe(24), spec.title, spec.model, spec.agent, resume)
        self._planned[spec.launch_id] = server
        permission: dict[str, Any] = {**PERMISSIONS.get(spec.permission_level, PERMISSIONS["ask"]), "external_directory": {f"{LAUNCH_DIR}/**": "allow"}}
        config: dict[str, Any] = {
            "mcp": {
                "daedalus_team": {
                    "type": "local",
                    "command": ["sh", "-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'],
                    "environment": {"DAEDALUS_ASK_HOLD_MS": str(spec.ask_hold_ms), "DAEDALUS_REPORT_HOLD_MS": str(spec.report_hold_ms)},
                    "timeout": max(spec.ask_hold_ms, spec.report_hold_ms) + TIMEOUT_SLACK_MS,
                }
            },
            "permission": permission,
        }
        # OpenCode runs every MCP tool unasked; the host asks the operator about what is sensitive.
        for tools in spec.tool_sets:
            config["mcp"][tools.server] = {"type": "local", "command": ["sh", "-c", tools.command()], "environment": {"DAEDALUS_TOOLS_HOLD_MS": str(tools.hold_ms)}, "timeout": tools.hold_ms + TIMEOUT_SLACK_MS}
        files: dict[str, bytes] = {tools.path: tools.file for tools in spec.tool_sets}
        if spec.team_block:
            files[SYSTEM_FILE] = spec.team_block.encode()
            config["instructions"] = [f"{LAUNCH_DIR}/{SYSTEM_FILE}"]
        if spec.team_skill:
            files[SKILL_FILE] = spec.team_skill.encode()
            config["skills"] = {"paths": [f"{LAUNCH_DIR}/{SKILLS_DIR}"]}
        argv = ["opencode", spec.cwd, "--port", str(port), "--hostname", "127.0.0.1"]
        if spec.agent:
            argv += ["--agent", spec.agent]
        if spec.model:
            argv += ["-m", spec.model]
        if resume:
            argv += ["-s", resume]
        env = {"OPENCODE_SERVER_PASSWORD": server.password, "OPENCODE_CONFIG_CONTENT": json.dumps(config, ensure_ascii=False)}
        return LaunchPlan(
            adapter_state=json.dumps(dataclasses.asdict(server)),
            argv=tuple(argv),
            env=env,
            cwd=spec.cwd,
            files=files,
            ports=(port,),
            session_ref=resume,
            first_prompt=spec.first_prompt,
            first_prompt_via="channel",
        )

    def readiness(self, screen: str) -> ReadyStep:
        if "Failed to start server on port" in screen:
            return ReadyStep("fail", reason="OpenCode could not listen on its port (another program has it); start the member again")
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        if plan.first_prompt:
            await self.send(term, "", plan.first_prompt, "after_turn")

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        self._launches.setdefault(term.id, _Launch())

    # -- the server -----------------------------------------------------------------------------

    def _state(self, term: TerminalPort) -> _Launch:
        return self._launches.setdefault(term.id, _Launch())

    @staticmethod
    def _auth(server: _Server) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(f"opencode:{server.password}".encode()).decode()}

    async def _http(self, term: TerminalPort, state: _Launch, method: str, path: str, body: Any = None) -> HttpAnswer:
        assert state.server is not None
        port = state.server.port
        return await http_request(lambda: term.dial(f"tcp:127.0.0.1:{port}"), method, path, body=body, headers=self._auth(state.server))

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        state = self._state(term)
        # A launch this host planned is in memory; one an earlier host started is read back from the
        # launch row, so a member still running is taken up rather than failed.
        state.server = self._planned.pop(launch.launch_id, None)
        if state.server is None and (stored := _server_from(launch.adapter_state)) is not None:
            # The session the TUI already shows is taken up, not a new one made beside it.
            state.server = dataclasses.replace(stored, resume=launch.session_ref or stored.resume)

        async def until_launch_ends() -> None:
            async for _ in term.hooks():
                pass  # OpenCode posts no hooks; the stream ending is the launch ending
            state.queue.put_nowait(None)

        async def run() -> None:
            try:
                await self._run(term, state, launch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the session's failure, shown on it
                logger.info("opencode launch %s: %s", launch.launch_id, exc)
                if not state.stopping:
                    state.queue.put_nowait(StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": f"OpenCode's server: {exc}"}))

        state.tasks = [asyncio.create_task(until_launch_ends(), name=f"opencode-end-{term.id}"), asyncio.create_task(run(), name=f"opencode-run-{term.id}")]
        try:
            while True:
                event = await state.queue.get()
                if event is None:
                    return
                yield event
        finally:
            for task in state.tasks:
                task.cancel()
            if state.session:
                self._live.pop(state.session, None)
            self._launches.pop(term.id, None)

    async def _run(self, term: TerminalPort, state: _Launch, launch: Launch) -> None:
        if state.server is None:
            raise ConnectionError("this launch does not say which port its server listens on or how to sign in to it")
        server = state.server
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_S
        while True:
            try:
                if (await asyncio.wait_for(self._http(term, state, "GET", "/global/health"), PROBE_S)).status == 200:
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — not listening yet
                if loop.time() >= deadline:
                    raise ConnectionError(f"no answer on port {server.port} within {CONNECT_S:g} s: {exc}") from None
            await asyncio.sleep(0.2)
        step = "opening the event stream"
        try:
            async with asyncio.timeout(SETUP_S):
                stream = await EventStream.open(lambda: term.dial(f"tcp:127.0.0.1:{server.port}"), "/event", headers=self._auth(server))
        except TimeoutError:
            raise ConnectionError(f"no answer within {SETUP_S:g} s while {step}") from None
        try:
            events = stream.events()
            try:
                async with asyncio.timeout(SETUP_S):
                    step = "waiting for the event stream's first event"
                    first = await anext(events, None)
                    if not isinstance(first, dict) or first.get("type") != "server.connected":
                        raise ConnectionError(f"the event stream began with {first!r}")
                    if server.resume:
                        step = "reading the session to take up"
                        answer = await self._http(term, state, "GET", f"/session/{server.resume}")
                        if answer.status != 200:
                            raise ConnectionError(f"session {server.resume} was not found ({answer.status})")
                        state.session = server.resume
                    else:
                        step = "making the session"
                        body: dict[str, Any] = {"title": server.title or "staff"}
                        provider, model = split_model(server.model)
                        if provider:
                            body["model"] = {"providerID": provider, "id": model}
                        answer = await self._http(term, state, "POST", "/session", body)
                        if answer.status != 200:
                            raise ConnectionError(f"no session was made ({answer.status}): {answer.body[:200]!r}")
                        state.session = str((answer.json() or {}).get("id") or "")
                    self._live[state.session] = term
                    step = "showing the session in the TUI"
                    await self._http(term, state, "POST", "/tui/select-session", {"sessionID": state.session})
            except TimeoutError:
                raise ConnectionError(f"no answer within {SETUP_S:g} s while {step}") from None
            state.connected.set()
            state.queue.put_nowait(StaffEvent(EventKind.TRANSCRIPT, _now(), {"ref": state.session, "session_ref": state.session}))
            state.queue.put_nowait(StaffEvent(EventKind.READY, _now(), {"session": state.session}))
            async for event in events:
                if isinstance(event, dict):
                    self._event(state, str(event.get("type") or ""), event.get("properties") or {})
        finally:
            await stream.close()
        if not state.stopping:
            state.queue.put_nowait(StaffEvent(EventKind.TURN_FAILED, _now(), {"failure": "side channel lost: OpenCode's event stream closed"}))

    def _put(self, state: _Launch, kind: EventKind, payload: Mapping[str, Any] | None = None, native_id: str = "") -> None:
        if kind is EventKind.ACTIVITY and not state.busy:
            # Between turns the server still publishes (session updates, diffs); none of it is work,
            # and "activity" would take a finished turn back to working.
            return
        state.queue.put_nowait(StaffEvent(kind, _now(), dict(payload or {}), native_id=native_id))

    def _event(self, state: _Launch, kind: str, props: Mapping[str, Any]) -> None:
        session = props.get("sessionID") or ((props.get("info") or {}).get("sessionID") if isinstance(props.get("info"), dict) else None) or ((props.get("part") or {}).get("sessionID") if isinstance(props.get("part"), dict) else None)
        if session and session != state.session:
            return
        if kind == "session.status":
            if (props.get("status") or {}).get("type") == "busy" and not state.busy:
                state.busy, state.failure, state.cancelled, state.last_message = True, "", False, ""
                self._put(state, EventKind.TURN_STARTED, {})
        elif kind == "session.idle":
            if state.busy:
                state.busy = False
                self._turn_ended(state)
        elif kind == "session.error":
            error = props.get("error") or {}
            if error.get("name") == "MessageAbortedError":
                state.cancelled = True
            else:
                state.failure = str((error.get("data") or {}).get("message") or error.get("name") or "the turn failed")
        elif kind == "message.updated":
            info = props.get("info") or {}
            ident = str(info.get("id") or "")
            if info.get("role") == "assistant":
                state.assistant.add(ident)
            elif info.get("role") == "user" and ident not in state.seen:
                state.seen.add(ident)
                if ident in state.sent:
                    message_id, text = state.sent.pop(ident)
                    self._put(state, EventKind.PROMPT_ACKNOWLEDGED, {"message_id": message_id, "prompt": text})
                else:
                    self._put(state, EventKind.ACTIVITY, {"user_message": ident})
        elif kind == "message.part.updated":
            self._part(state, props.get("part") or {})
        elif kind == "permission.asked":
            ref = str(props.get("id") or "")
            metadata = props.get("metadata") or {}
            summary = str(metadata.get("command") or metadata.get("filePath") or " ".join(str(p) for p in props.get("patterns") or []) or props.get("permission") or "a permission")
            state.requests[ref] = {"kind": "permission"}
            self._put(state, EventKind.PERMISSION_REQUESTED, {"tool": str(props.get("permission") or ""), "summary": " ".join(summary.split())[:300], "options": ["allow_once", "allow_always", "deny"]}, ref)
        elif kind == "question.asked":
            ref = str(props.get("id") or "")
            questions = [q for q in props.get("questions") or [] if isinstance(q, dict)]
            text = "\n".join(str(q.get("question") or "") for q in questions).strip() or "OpenCode asks a question"
            options = [str(o.get("label") or "") for o in (questions[0].get("options") if questions else None) or [] if isinstance(o, dict)]
            state.requests[ref] = {"kind": "question", "count": len(questions)}
            self._put(state, EventKind.QUESTION_ASKED, {"tool": "question", "summary": text[:200], "text": text, "options": options}, ref)
        elif kind in ("permission.replied", "question.replied", "question.rejected"):
            ref = str(props.get("requestID") or props.get("id") or "")
            if state.requests.pop(ref, None) is not None and ref not in state.answered:
                self._put(state, EventKind.REQUEST_RESOLVED, {}, ref)
            state.answered.discard(ref)
        elif kind not in ("server.heartbeat", "server.connected"):
            self._put(state, EventKind.ACTIVITY, {"event": kind})

    def _part(self, state: _Launch, part: Mapping[str, Any]) -> None:
        kind = part.get("type")
        if kind == "tool":
            call = str(part.get("callID") or part.get("id") or "")
            status = str((part.get("state") or {}).get("status") or "")
            tool = str(part.get("tool") or "")
            if status in ("running", "pending") and call not in state.tools:
                state.tools.add(call)
                self._put(state, EventKind.TOOL_STARTED, {"tool": tool}, call)
            elif status in ("completed", "error"):
                state.tools.discard(call)
                self._put(state, EventKind.TOOL_FINISHED, {"tool": tool, "ok": status == "completed"}, call)
        elif kind == "text" and str(part.get("messageID") or "") in state.assistant and str(part.get("text") or "").strip():
            state.last_message = str(part["text"])

    def _turn_ended(self, state: _Launch) -> None:
        for ref in list(state.requests):
            self._put(state, EventKind.REQUEST_RESOLVED, {}, ref)
        state.requests.clear()
        if state.cancelled:
            self._put(state, EventKind.TURN_CANCELLED, {"via": "abort"})
        elif state.failure:
            self._put(state, EventKind.TURN_FAILED, {"failure": state.failure})
        else:
            self._put(state, EventKind.TURN_COMPLETED, {"last_message": state.last_message})

    # -- what it does ---------------------------------------------------------------------------

    async def _ready(self, term: TerminalPort) -> _Launch:
        state = self._state(term)
        await asyncio.wait_for(state.connected.wait(), CONNECT_S)
        return state

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        state = await self._ready(term)
        assert state.server is not None
        ident = "msg_" + secrets.token_hex(12)
        state.sent[ident] = (message_id, text)
        body: dict[str, Any] = {"messageID": ident, "parts": [{"type": "text", "text": text}]}
        provider, model = split_model(state.server.model)
        if provider:
            body["model"] = {"providerID": provider, "modelID": model}
        if state.server.agent:
            body["agent"] = state.server.agent
        answer = await self._http(term, state, "POST", f"/session/{state.session}/prompt_async", body)
        if answer.status not in (200, 204):
            state.sent.pop(ident, None)
            return Delivery(message_id, "failed", via="prompt_async", error=f"OpenCode refused the message ({answer.status}): {answer.body[:200].decode('utf-8', 'replace')}")
        return Delivery(message_id, "submitted", via="prompt_async", client_ref=ident)

    async def interrupt(self, term: TerminalPort) -> None:
        state = await self._ready(term)
        await self._http(term, state, "POST", f"/session/{state.session}/abort")

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        state = self._state(term)
        request = state.requests.get(request_ref)
        if request is None:
            return False
        state.answered.add(request_ref)
        if request["kind"] == "permission":
            reply = {"allow_once": "once", "allow_always": "always"}.get(answer.choice, "reject")
            body: dict[str, Any] = {"reply": reply}
            if reply == "reject" and answer.note:
                body["message"] = answer.note
            result = await self._http(term, state, "POST", f"/permission/{request_ref}/reply", body)
        else:
            chosen = answer.said
            result = await self._http(term, state, "POST", f"/question/{request_ref}/reply", {"answers": [[chosen]] * max(1, int(request.get("count") or 1))})
        if result.status != 200:
            state.answered.discard(request_ref)
            return False
        return True

    async def stop(self, term: TerminalPort) -> None:
        state = self._state(term)
        state.stopping = True
        screen = await term.screen()
        if self.classify_screen(screen) is ScreenClass.DIALOG:
            return  # typing would answer the dialog; the runtime ends the terminal after its grace
        await term.write(text="/exit")
        await asyncio.sleep(0.3)
        await term.write(keys=["Enter"])

    # -- the screen -----------------------------------------------------------------------------

    def classify_screen(self, text: str) -> ScreenClass:
        lines = [line for line in text.splitlines() if line.strip()]
        bottom = "\n".join(lines[-24:])
        if any(marker in bottom for marker in DIALOG_MARKERS):
            return ScreenClass.DIALOG
        tail = "\n".join(lines[-4:])
        if any(hint in tail for hint in BUSY_HINTS):
            return ScreenClass.BUSY
        if any(hint in tail for hint in IDLE_HINTS):
            return ScreenClass.IDLE_COMPOSER
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        return False  # nothing is typed into OpenCode's composer; messages go by its server

    # -- the transcript -------------------------------------------------------------------------

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        """The session's messages from the live server, or — once OpenCode has exited — from
        ``opencode export``, which reads its own store."""
        term = self._live.get(ref)
        state = self._launches.get(term.id) if term is not None else None
        if term is not None and state is not None and state.connected.is_set():
            answer = await self._http(term, state, "GET", f"/session/{ref}/message")
            messages = answer.json() if answer.status == 200 else []
        else:
            result = await env.run(["opencode", "export", ref], timeout=60)
            if result.exit_code != 0:
                return []
            try:
                messages = (json.loads(result.stdout) or {}).get("messages") or []
            except ValueError:
                return []
        return parse_messages(messages if isinstance(messages, list) else [])[since:]


def parse_messages(messages: list[Any]) -> list[Turn]:
    """OpenCode's messages (``{info, parts}``) as turns: a user message's text, an assistant
    message's text and tools with their outcome, and its tokens."""
    turns: list[Turn] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") or {}
        parts = [p for p in message.get("parts") or [] if isinstance(p, dict)]
        created = (info.get("time") or {}).get("created")
        at = datetime.fromtimestamp(created / 1000, UTC).isoformat() if isinstance(created, int | float) else ""
        text = "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text" and not p.get("synthetic")).strip()
        if info.get("role") == "user":
            if text:
                turns.append(Turn(len(turns), "orchestrator" if text.startswith("[orchestrator]") else "user", text, started_at=at, ended_at=at))
            continue
        if info.get("role") != "assistant":
            continue
        tools = []
        for part in parts:
            if part.get("type") != "tool":
                continue
            tool_state = part.get("state") or {}
            given = tool_state.get("input") or {}
            summary = str(given.get("command") or given.get("filePath") or given.get("pattern") or tool_state.get("title") or part.get("tool") or "") if isinstance(given, dict) else ""
            status = tool_state.get("status")
            tools.append(ToolUse(str(part.get("tool") or ""), " ".join(summary.split())[:300], True if status == "completed" else False if status == "error" else None))
        tokens = info.get("tokens") or {}
        usage = None
        if isinstance(tokens, dict) and (tokens.get("input") or tokens.get("output")):
            cache = tokens.get("cache") or {}
            cost = info.get("cost")
            usage = TurnUsage(int(tokens.get("input") or 0), int(tokens.get("output") or 0), int(cache.get("read") or 0) if isinstance(cache, dict) else 0, float(cost) if isinstance(cost, int | float) else None)
        if text or tools:
            turns.append(Turn(len(turns), "assistant", text, tuple(tools), at, at, usage))
    return turns


__all__ = ["OpenCodeAdapter", "parse_messages", "split_model"]
