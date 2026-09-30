"""A fake Claude Code: the interactive TUI, its hooks, its transcript and its small commands.

What it imitates, and why each matters to the harness. The shapes marked (measured) were recorded
from the real Claude Code 2.1.282 in a throwaway configuration; the recordings are in
``recorded/claude/`` beside this module.

- **Arguments** as the real ``claude`` takes them for an interactive session: ``--session-id``
  (a UUID, refused when already used), ``--resume``, ``--settings`` (a file or inline JSON),
  ``--mcp-config`` and ``--add-dir`` (both variadic: every following word that is not a flag is
  taken, so a prompt after them must follow ``--``), ``-n/--name``, ``--agent``, ``--model``,
  ``--effort``, ``--permission-mode`` (``manual``, not ``default``: the flag's name for what hooks
  report as ``default``), ``--append-system-prompt`` and ``--append-system-prompt-file`` (measured: a missing file is an error), ``--dangerously-skip-permissions``, and the
  first prompt as the last positional argument. Anything else is an error.
- **Before the composer** (measured). A configuration that never finished its first run shows the
  theme picker and then "Select login method" even when signed in (``FAKE_CLAUDE_FIRST_RUN=1``);
  signed out (``FAKE_CLAUDE_LOGGED_IN=0``) it shows the sign-in choice. A folder not yet trusted
  shows "Accessing workspace:" … "Quick safety check: Is this a project you created or one you
  trust?" with the rows ``❯ No, exit`` (highlighted) and ``Yes, I trust this folder``, unnumbered:
  Down then Enter accepts. The answer is kept in ``~/.claude.json`` (or under
  ``CLAUDE_CONFIG_DIR``), so it is asked once per folder. No settings-file hook fires before these
  screens are passed, so ``SessionStart`` arriving is the proof that the gate passed.
  ``bypassPermissions`` adds its own warning with ``❯ No, exit`` highlighted, unless
  ``skipDangerousModePermissionPrompt`` is set in the settings (measured: the overlay's is honoured).
- **Hooks** from ``--settings`` merged with the user's own ``settings.json`` (lists merge), both
  ``http`` (headers expanded from ``allowedEnvVars`` only) and ``command``, with matchers on tool
  names: ``SessionStart`` (``source`` startup or resume), ``UserPromptSubmit`` (``prompt``,
  ``prompt_id``; a ``block`` decision stops the turn), ``PreToolUse`` (``permissionDecision``
  allow/deny/ask and ``updatedInput``; measured: an ``allow`` with ``updatedInput.answers`` answers
  ``AskUserQuestion`` with no dialog), ``PermissionRequest`` (measured: no ``tool_use_id`` in it;
  ``decision.behavior`` allow/deny, and the other shape, ``permissionDecision``, is honoured too),
  ``PostToolUse``, ``Stop`` (``last_assistant_message``), ``StopFailure`` (``error``),
  ``Notification`` (``permission_prompt`` about six seconds after a dialog opens, "Claude needs your
  permission"; ``idle_prompt`` sixty seconds after a turn ends — both scaled — and never anything a
  harness may take for a question), ``SessionEnd``.
- **The permission dialog is drawn at once, while the ``PermissionRequest`` hook runs** (measured):
  a held hook that answers closes it and the tool runs ("Allowed by PermissionRequest hook"); an
  answer typed into the dialog first lets the tool run while the hook is left to finish on its own.
- **Enter while busy** queues the message, and ``UserPromptSubmit`` fires for it at once (measured),
  not when it is injected at the next tool boundary; the transcript records a ``queue-operation``.
  **Esc** interrupts: the transcript gets "[Request interrupted by user for tool use]" (during a
  tool) or "[Request interrupted by user]", the screen "Interrupted · What should Claude do
  instead?", and no ``Stop`` fires (measured). Two Esc on an idle composer open the rewind dialog —
  the reason a harness sends Esc once.
- **Pastes** collapse to ``[Pasted text #N]`` over 800 characters, and to ``[Pasted text #N +K
  lines]`` from four lines on, K counting the line breaks (measured); an Enter right after a paste
  is taken (measured), and ``FAKE_CLAUDE_BURST_MS`` makes a window that swallows one for tests.
  A collapsed paste is submitted — to ``UserPromptSubmit`` and to the transcript — wrapped in
  ``<pasted_content id="…">`` tags (measured on 2.1.282); the model reads the text itself.
- **The transcript** is JSON lines in Claude's record shape under
  ``~/.claude/projects/<cwd with every non-alphanumeric as ->/<session>.jsonl``.
- **Team tools**: the ``--mcp-config`` servers are started as the real CLI starts them, and the
  script's ``report:``/``askorch:`` steps call ``mcp__daedalus_team__Report`` /
  ``…__AskOrchestrator``, which, like every tool, need an allow rule not to prompt. ``MCP_TOOL_TIMEOUT``
  (milliseconds) bounds a call. Skills under ``<--add-dir>/.claude/skills/<name>/SKILL.md`` are
  found (measured) and logged as ``skills``; ``--append-system-prompt`` is logged as ``system_prompt``.
- **Commands**: ``--version``, ``agents --json`` (every live fake session: ``pid, cwd, kind,
  sessionId, name, status, waitingFor``), ``auth status --json`` (the real one's keys, no
  address), ``update``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FakeAgent  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Faults,
    Look,
    McpClient,
    exit_with,
    expand_env,
    installed_version,
    latest_version,
    logged_in,
    new_id,
    now_iso,
    pause,
    post_json,
    run_command_hook,
    set_version,
    settle,
    usage_error,
)

DEFAULT_VERSION = "2.1.281"
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FLAGS = {
    "--session-id": 1, "--resume": 1, "--settings": 1, "--mcp-config": -1, "--add-dir": -1, "--name": 1,
    "--agent": 1, "--model": 1, "--effort": 1, "--permission-mode": 1, "--append-system-prompt": 1, "--append-system-prompt-file": 1,
    "--dangerously-skip-permissions": 0, "--strict-mcp-config": 0, "--version": 0,
}
ALIASES = {"-n": "--name", "-r": "--resume", "-v": "--version"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path(os.environ.get("HOME") or "/tmp") / ".claude"


def global_state_file() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) / ".claude.json" if configured else Path(os.environ.get("HOME") or "/tmp") / ".claude.json"


def mangle(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_path(cwd: str, session_id: str) -> Path:
    return config_dir() / "projects" / mangle(cwd) / f"{session_id}.jsonl"


def agents_dir() -> Path:
    path = config_dir() / "fake-agents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text.startswith("{"):
        text = Path(value).read_text(encoding="utf-8")
    loaded = json.loads(text)
    return loaded if isinstance(loaded, dict) else {}


def merge_settings(user: dict[str, Any], launch: dict[str, Any]) -> dict[str, Any]:
    """Lists merge and the launch wins on scalars, as Claude merges settings sources."""
    out = dict(user)
    for key, value in launch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_settings(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = out[key] + value
        else:
            out[key] = value
    return out


class FakeClaude(FakeAgent):
    cli = "claude"
    look = Look(
        "claude", "✻ Welcome to Claude Code (fake)", prompt="❯ ", idle_hint="⏸ manual mode on · ? for shortcuts", busy_hint="⏸ manual mode on · esc to interrupt",
        busy_word="Working…", alt_screen=False, collapse_chars=800, collapse_lines=3, burst_guard_ms=int(os.environ.get("FAKE_CLAUDE_BURST_MS") or 0),
        marker_style="claude",
    )
    interrupted_line = "  ⎿  Interrupted · What should Claude do instead?"

    def __init__(self, args: Args) -> None:
        super().__init__(os.getcwd())
        self.args = args
        mode = args.get("--permission-mode", "manual")
        if args.has("--dangerously-skip-permissions"):
            mode = "bypassPermissions"
        if mode not in PERMISSION_MODES:
            usage_error("claude", f"option '--permission-mode <mode>' argument '{mode}' is invalid. Allowed choices are {', '.join(PERMISSION_MODES)}.")
        effort = args.get("--effort")
        if effort and effort not in EFFORTS:
            usage_error("claude", f"option '--effort <level>' argument '{effort}' is invalid.")
        self.mode = mode
        self.resuming = args.get("--resume")
        session = self.resuming or args.get("--session-id") or new_id()
        if not UUID.match(session):
            usage_error("claude", f"Invalid session ID. Must be a valid UUID: {session}")
        self.session_id = session
        self.transcript = transcript_path(self.cwd, session)
        if args.has("--session-id") and self.transcript.exists():
            usage_error("claude", f"Session ID {session} is already in use.")
        if self.resuming and not self.transcript.exists():
            usage_error("claude", f"No conversation found with session ID: {session}")
        self.name = args.get("--name")
        self.model = args.get("--model", "sonnet")
        user_file = config_dir() / "settings.json"
        user = load_json(str(user_file)) if user_file.exists() else {}
        launch: dict[str, Any] = {}
        for value in args.all("--settings"):
            launch = merge_settings(launch, load_json(value))
        self.settings = merge_settings(user, launch)
        self.allow = [str(rule) for rule in (self.settings.get("permissions") or {}).get("allow", [])]
        self.mcp: dict[str, McpClient] = {}
        self.first_prompt = args.positional[-1] if args.positional else None
        self.parent_uuid: str | None = None
        self.last_esc = 0.0
        self.idle_timer: asyncio.Task[None] | None = None
        self.permission_notice: asyncio.Task[None] | None = None
        self.last_text = ""
        self.prompt_id = ""

    # -- start -------------------------------------------------------------------------------

    async def before_ready(self) -> bool:
        state = self._global_state()
        if os.environ.get("FAKE_CLAUDE_FIRST_RUN") == "1" and not state.get("hasCompletedOnboarding"):
            # The first run's own screens come before anything else, signed in or not (measured).
            await self._ask("theme", "Choose the text style that looks best with your terminal", ["To change this later, run /theme"], ["Auto (match terminal)", "Dark mode ✔", "Light mode"], selected=1, numbered=True)
            self.tui.open_dialog(Dialog("login", "Select login method:", ["Claude Code can be used with your Claude subscription or billed based on API usage through your Console account."], ["Claude account with subscription · Pro, Max, Team, or Enterprise", "Anthropic Console account · API usage billing", "3rd-party platform · Amazon Bedrock, Microsoft Foundry, or Vertex AI"], on_choose=lambda i: None))
            self.log("login_required", first_run=True)
            return False
        if not logged_in("claude"):
            self.tui.open_dialog(Dialog("login", "Select login method:", [], ["Claude account with subscription · Pro, Max, Team, or Enterprise", "Anthropic Console account · API usage billing", "3rd-party platform · Amazon Bedrock, Microsoft Foundry, or Vertex AI"], on_choose=lambda i: None))
            self.log("login_required")
            return False
        project = (state.get("projects") or {}).get(self.cwd) or {}
        if not project.get("hasTrustDialogAccepted"):
            body = [" Accessing workspace:", "", f" {self.cwd}", "", " Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source",
                    " project, or work from your team). If not, take a moment to review what's in this folder first.", "", " Claude Code'll be able to read, edit, and execute files here.", "", " Security guide"]
            if await self._ask("trust", "", body, ["No, exit", "Yes, I trust this folder"], numbered=False) != 1:
                await self.quit(1)
                return False
            state = self._global_state()
            state.setdefault("projects", {}).setdefault(self.cwd, {})["hasTrustDialogAccepted"] = True
            self._save_global_state(state)
        if self.mode == "bypassPermissions" and not self.settings.get("skipDangerousModePermissionPrompt"):
            body = ["  In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially dangerous", "  commands."]
            if await self._ask("bypass", "  WARNING: Claude Code running in Bypass Permissions mode", body, ["No, exit", "Yes, I accept"], numbered=False) != 1:
                await self.quit(1)
                return False
        return True

    async def _ask(self, kind: str, title: str, body: list[str], options: list[str], *, selected: int = 0, numbered: bool = True) -> int:
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        # Esc means the refusing row: "No, exit" is the first of the unnumbered questions.
        refuse = 0 if not numbered else len(options) - 1
        self.tui.open_dialog(Dialog(kind, title, body, options, selected=selected, numbered=numbered, digits=numbered, on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, refuse)))
        return await future

    def _global_state(self) -> dict[str, Any]:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(global_state_file().read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        return {}

    def _save_global_state(self, state: dict[str, Any]) -> None:
        path = global_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    async def on_ready(self) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()
        self._register()
        skills = sorted(p.parent.name for d in self.args.all("--add-dir") for p in Path(d).glob(".claude/skills/*/SKILL.md"))
        self.log("skills", names=skills)
        appended = self.args.get("--append-system-prompt")
        if self.args.has("--append-system-prompt-file"):
            appended = Path(self.args.get("--append-system-prompt-file")).read_text(encoding="utf-8")
        if appended:
            self.log("system_prompt", text=appended)
        await self._start_mcp()
        await self.hook("SessionStart", {"source": "resume" if self.resuming else "startup", "model": self.model})

    async def _start_mcp(self) -> None:
        for value in self.args.all("--mcp-config"):
            try:
                servers = load_json(value).get("mcpServers") or {}
            except (OSError, json.JSONDecodeError) as exc:
                self.tui.say(f"MCP config error: {exc}")
                continue
            for name, spec in servers.items():
                env = {k: expand_env(str(v)) for k, v in (spec.get("env") or {}).items()}
                client = McpClient(name, expand_env(str(spec.get("command", ""))), [expand_env(str(a)) for a in spec.get("args") or []], env)
                if await client.start():
                    self.mcp[name] = client
                else:
                    self.tui.say(f"1 MCP server failed to connect: {name} ({client.error})")

    def _register(self) -> None:
        (agents_dir() / f"{os.getpid()}.json").write_text(json.dumps(self._agent_view()), encoding="utf-8")

    def _agent_view(self) -> dict[str, Any]:
        dialog = self.tui.dialog
        status = "waiting" if dialog is not None else "busy" if self.tui.busy else "idle"
        waiting = "dialog open" if dialog is not None else ""
        return {"pid": os.getpid(), "cwd": self.cwd, "kind": "interactive", "sessionId": self.session_id, "name": self.name, "status": status, "waitingFor": waiting}

    # -- hooks ---------------------------------------------------------------------------------

    async def hook(self, event: str, fields: dict[str, Any], *, tool: str = "") -> list[Any]:
        """Run every hook of ``event`` whose matcher fits ``tool``; the parsed answers, in order."""
        entries = (self.settings.get("hooks") or {}).get(event) or []
        payload: dict[str, Any] = {"session_id": self.session_id, "transcript_path": str(self.transcript), "cwd": self.cwd}
        if self.prompt_id and event not in ("SessionStart", "SessionEnd"):
            payload["prompt_id"] = self.prompt_id
        if event not in ("SessionStart", "SessionEnd", "Notification"):
            # The real payloads of these three carry no mode (measured).
            payload["permission_mode"] = "default" if self.mode == "manual" else self.mode
        payload.update({"hook_event_name": event, **fields})
        answers: list[Any] = []
        for entry in entries:
            matcher = str(entry.get("matcher") or "")
            if tool and matcher and matcher != "*" and not re.fullmatch(matcher, tool):
                continue
            for spec in entry.get("hooks") or []:
                timeout = float(spec.get("timeout") or 60)
                if spec.get("type") == "http":
                    allowed = [str(n) for n in spec.get("allowedEnvVars") or []]
                    headers = {str(k): expand_env(str(v), allowed) for k, v in (spec.get("headers") or {}).items()}
                    status, body = await post_json(str(spec.get("url")), payload, headers, timeout)
                    self.log("hook", name=event, status=status)
                    if 200 <= status < 300 and body:
                        answers.append(body)
                elif spec.get("type") == "command":
                    code, body = await run_command_hook(str(spec.get("command")), payload, timeout)
                    self.log("hook", name=event, status=code)
                    if code == 0 and body:
                        answers.append(body)
        return answers

    # -- the transcript ---------------------------------------------------------------------------

    def record(self, kind: str, message: dict[str, Any], **extra: Any) -> None:
        entry = {
            "parentUuid": self.parent_uuid, "isSidechain": False, "userType": "external", "cwd": self.cwd,
            "sessionId": self.session_id, "version": installed_version("claude", DEFAULT_VERSION), "type": kind,
            "message": message, "uuid": new_id(), "timestamp": now_iso(), **extra,
        }
        self.parent_uuid = entry["uuid"]
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def assistant_record(self, content: list[dict[str, Any]], stop_reason: str | None) -> None:
        self.record("assistant", {
            "id": "msg_" + new_id().replace("-", "")[:24], "type": "message", "role": "assistant", "model": f"claude-{self.model}-fake",
            "content": content, "stop_reason": stop_reason,
            "usage": {"input_tokens": 12, "output_tokens": max(1, len(json.dumps(content)) // 4), "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        })

    # -- the turn ---------------------------------------------------------------------------------

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        self._cancel_idle()
        if queued:
            # Its UserPromptSubmit fired when it was queued (measured); now it is only taken in.
            self.record_plain({"type": "queue-operation", "operation": "dequeue", "timestamp": now_iso(), "sessionId": self.session_id})
        else:
            await self._submit_hook(text)
        self.record("user", {"role": "user", "content": self.tui.reported.get(text, text)})
        self._register()

    async def _submit_hook(self, text: str) -> None:
        self.prompt_id = new_id()
        answers = await self.hook("UserPromptSubmit", {"prompt": self.tui.reported.get(text, text)})
        for answer in answers:
            if isinstance(answer, dict) and answer.get("decision") == "block":
                self.tui.say(f"⎿ UserPromptSubmit operation blocked by hook: {answer.get('reason', '')}")
                raise asyncio.CancelledError

    async def busy_enter(self, text: str) -> None:
        await self._submit_hook(text)
        self.record_plain({"type": "queue-operation", "operation": "enqueue", "timestamp": now_iso(), "sessionId": self.session_id, "content": text})
        await super().busy_enter(text)

    def record_plain(self, entry: dict[str, Any]) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def on_assistant(self, text: str) -> None:
        self.last_text = text
        self.assistant_record([{"type": "text", "text": text}], "end_turn")

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.assistant_record([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}], "tool_use")

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        self.record("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": not ok}]})
        await self.hook("PostToolUse", {"tool_name": name, "tool_input": tool_input, "tool_response": {"stdout": output, "stderr": "", "interrupted": False}, "tool_use_id": tool_id}, tool=name)

    async def on_tool_denied(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        self.record("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "The user doesn't want to proceed with this tool use. The tool use was rejected.", "is_error": True}]})

    async def on_turn_completed(self) -> None:
        self._register()
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
        else:
            await self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": self.last_text, "background_tasks": [], "session_crons": []})
        self.idle_timer = asyncio.ensure_future(self._idle_notification())

    async def on_turn_failed(self, kind: str) -> None:
        self._register()
        await self.hook("StopFailure", {"error": kind, "error_details": f"the fake failed with {kind}"})

    async def on_turn_cancelled(self) -> None:
        self._register()
        words = "[Request interrupted by user for tool use]" if self.interrupted_in_tool else "[Request interrupted by user]"
        self.record("user", {"role": "user", "content": [{"type": "text", "text": words}]})

    async def on_session_end(self) -> None:
        self._cancel_idle()
        for client in self.mcp.values():
            await client.close()
        with contextlib.suppress(OSError):
            (agents_dir() / f"{os.getpid()}.json").unlink()
        if self.tui.ready:
            await self.hook("SessionEnd", {"reason": "prompt_input_exit"})

    async def _idle_notification(self) -> None:
        await pause(60)
        await self.hook("Notification", {"message": "Claude is waiting for your input", "notification_type": "idle_prompt"})

    def _cancel_idle(self) -> None:
        if self.idle_timer is not None:
            self.idle_timer.cancel()
            self.idle_timer = None

    async def escape(self) -> None:
        if self.busy:
            await super().escape()
            return
        if self.tui.dialog is None and time.monotonic() - self.last_esc < 0.8:
            self.tui.open_dialog(Dialog("rewind", "Rewind", ["Restore the conversation to the point before…"], ["(current)"], on_choose=lambda i: None, on_escape=lambda: None))
        self.last_esc = time.monotonic()

    # -- permissions and questions ------------------------------------------------------------------

    def _allowed(self, tool: str, tool_input: dict[str, Any]) -> bool:
        if self.mode == "bypassPermissions":
            return True
        for rule in self.allow:
            name, _, pattern = rule.partition("(")
            if name.startswith("mcp__") and name.count("__") == 1 and tool.startswith(name + "__"):
                return True  # a server's rule: every tool of that MCP server (measured on 2.1.283)
            if name != tool:
                continue
            if not pattern:
                return True
            prefix = pattern.rstrip(")").removesuffix(":*").removesuffix("*")
            if str(tool_input.get("command", "")).startswith(prefix):
                return True
        return False

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        await self._cancel_permission_notice()
        decided = await self._pre_tool_use(tool, tool_input, tool_id)
        if decided in ("allow", "deny"):
            return "allow_once" if decided == "allow" else "deny"
        if decided != "ask" and self._allowed(tool, tool_input):
            return "allow_once"
        # The real request names no tool_use_id (measured): the tool and its input say which it is.
        request = {"tool_name": tool, "tool_input": tool_input, "permission_suggestions": [{"type": "addDirectories", "directories": [self.cwd], "destination": "session"}, {"type": "setMode", "mode": "acceptEdits", "destination": "session"}]}
        return await self._dialog(tool, tool_input, summary, request)

    async def _dialog(self, tool: str, tool_input: dict[str, Any], summary: str, request: dict[str, Any]) -> str:
        """The permission dialog, drawn at once while the ``PermissionRequest`` hook runs (measured):
        whichever answers first decides; a hook still running after an answer on screen is left to
        finish, and what it says then changes nothing."""
        options = ["Yes", f"Yes, and don't ask again for {summary.split()[0] if summary else tool} commands in {self.cwd}", "No"]
        body = [f"   {summary}", f"   {tool_input.get('description') or ''}".rstrip(), " Do you want to proceed?"]
        hook_task = asyncio.ensure_future(self._late_hook(tool, request))
        # The hook is posted before the dialog is drawn (measured: about a quarter of a second), so
        # keys typed the moment the hook arrives land in the composer, not in the dialog.
        await asyncio.sleep(float(os.environ.get("FAKE_CLAUDE_DIALOG_DELAY_MS") or 250) / 1000)
        if hook_task.done() and hook_task.result():
            self.tui.say("  ⎿  Allowed by PermissionRequest hook" if hook_task.result() == "allow_once" else "  ⎿  Denied by PermissionRequest hook")
            return str(hook_task.result())
        dialog_task = asyncio.ensure_future(self.permission_dialog(tool, summary, options, ["allow_once", "allow_always", "deny"], title=f" {tool} command", body=body, footer=" Esc to cancel · Tab to amend"))
        await asyncio.sleep(0)  # let the dialog open before the session says it waits
        self._register()
        self.permission_notice = asyncio.ensure_future(self._permission_notification(tool))
        try:
            waiting: set[asyncio.Future[Any]] = {dialog_task, hook_task}
            while waiting:
                done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if dialog_task in done:
                    return dialog_task.result()
                if hook_task in done and hook_task.result():
                    dialog_task.cancel()
                    self.tui.close_dialog()
                    self.tui.say("  ⎿  Allowed by PermissionRequest hook" if hook_task.result() == "allow_once" else "  ⎿  Denied by PermissionRequest hook")
                    return str(hook_task.result())
            return "deny"
        finally:
            await self._cancel_permission_notice()
            self._register()

    async def _late_hook(self, tool: str, request: dict[str, Any]) -> str:
        if self.faults.late_permission_notification:
            await pause(2)
        answers = await self.hook("PermissionRequest", request, tool=tool)
        verdict = self._verdict(answers)
        if verdict == "allow_once":
            self._take_rules(answers)
        return verdict

    def _take_rules(self, answers: list[Any]) -> None:
        """An allow's ``updatedPermissions``: rules added for the session count from the next call on."""
        for answer in answers:
            output = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
            decision = output.get("decision") if isinstance(output, dict) else None
            for update in (decision.get("updatedPermissions") or []) if isinstance(decision, dict) else []:
                if update.get("type") == "addRules" and update.get("behavior") == "allow":
                    rules = [str(r["toolName"]) for r in update.get("rules") or []]
                    self.allow += rules
                    self.log("rules_added", rules=rules, destination=update.get("destination"))

    async def _permission_notification(self, tool: str) -> None:
        await pause(12 if self.faults.late_permission_notification else 6)
        await self.hook("Notification", {"message": "Claude needs your permission", "notification_type": "permission_prompt"})

    async def _cancel_permission_notice(self) -> None:
        if self.permission_notice is not None:
            self.permission_notice.cancel()
            self.permission_notice = None

    @staticmethod
    def _verdict(answers: list[Any]) -> str:
        for answer in answers:
            output = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
            if not isinstance(output, dict):
                continue
            decision = output.get("decision")
            behavior = decision.get("behavior") if isinstance(decision, dict) else output.get("permissionDecision")
            if behavior == "allow":
                return "allow_once"
            if behavior == "deny":
                return "deny"
        return ""

    async def _pre_tool_use(self, tool: str, tool_input: dict[str, Any], tool_id: str) -> str:
        answers = await self.hook("PreToolUse", {"tool_name": tool, "tool_input": tool_input, "tool_use_id": tool_id}, tool=tool)
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            output = answer.get("hookSpecificOutput") or {}
            decision = output.get("permissionDecision") or {"approve": "allow", "block": "deny"}.get(str(answer.get("decision")), "")
            if output.get("updatedInput"):
                tool_input.clear()
                tool_input.update(output["updatedInput"])
            if decision in ("allow", "deny", "ask"):
                return str(decision)
        return ""

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        tool_input: dict[str, Any] = {"questions": [{"question": question, "header": "Question", "options": [{"label": o, "description": ""} for o in options], "multiSelect": False}]}
        self.assistant_record([{"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": tool_input}], "tool_use")
        await self._pre_tool_use("AskUserQuestion", tool_input, tool_id)
        answers = tool_input.get("answers") if isinstance(tool_input.get("answers"), dict) else None
        if answers and question in answers:
            answer = str(answers[question])
        else:
            self._register()
            answer = await self.question_dialog(question, options)
        self.record("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": f'User has answered your questions: "{question}"="{answer}".', "is_error": False}]})
        await self.hook("PostToolUse", {"tool_name": "AskUserQuestion", "tool_input": tool_input, "tool_response": {"answers": {question: answer}}, "tool_use_id": tool_id}, tool="AskUserQuestion")
        return answer

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        return await self.mcp_tool("daedalus_team", name, arguments, tool_id)

    async def mcp_tool(self, server: str, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        client = self.mcp.get(server)
        full = f"mcp__{server}__{name}"
        if client is None or name not in client.tools:
            return f"error: no tool {full}"
        decision = await self.permission(full, arguments, name, tool_id)
        if not decision.startswith("allow"):
            await self.on_tool_denied(full, arguments, tool_id)
            return "rejected"
        await self.on_tool_start(full, arguments, tool_id)
        timeout = float(os.environ.get("MCP_TOOL_TIMEOUT") or 120_000) / 1000
        try:
            result = await client.call(name, arguments, timeout=timeout)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            result = f"error: {exc or 'the tool timed out'}"
        await self.on_tool_end(full, arguments, tool_id, result, not result.startswith("error"))
        return result


# -- the small commands ------------------------------------------------------------------------------


def command(argv: list[str]) -> int | None:
    if not argv:
        return None
    if argv[0] in ("--version", "-v"):
        print(f"{installed_version('claude', DEFAULT_VERSION)} (Claude Code)")
        return 0
    if argv[:2] == ["agents", "--json"]:
        rows = []
        for path in sorted(agents_dir().glob("*.json")):
            with contextlib.suppress(OSError, json.JSONDecodeError, ValueError):
                row = json.loads(path.read_text(encoding="utf-8"))
                os.kill(int(row["pid"]), 0)
                rows.append(row)
        print(json.dumps(rows))
        return 0
    if argv[:2] == ["auth", "status"]:
        state = {"loggedIn": logged_in("claude"), "authMethod": "claude.ai" if logged_in("claude") else "none", "subscriptionType": "max" if logged_in("claude") else None}
        print(json.dumps(state) if "--json" in argv else ("Logged in" if state["loggedIn"] else "Not logged in"))
        return 0 if state["loggedIn"] else 1
    if argv[0] == "update":
        current, latest = installed_version("claude", DEFAULT_VERSION), latest_version("claude", DEFAULT_VERSION)
        print(f"Current version: {current}")
        print("Checking for updates...")
        if current == latest:
            print(f"Claude Code is up to date ({current})")
        else:
            set_version("claude", latest)
            print(f"Successfully updated from {current} to version {latest}")
        return 0
    return None


def main() -> None:
    argv = sys.argv[1:]
    code = command(argv)
    if code is not None:
        raise SystemExit(code)
    args = Args("claude", argv, flags=FLAGS, aliases=ALIASES)
    if args.has("--version"):
        raise SystemExit(command(["--version"]))
    if Faults.from_env().fail_at_start:
        print("Error: the model given with --model is not available to this account", flush=True)
        raise SystemExit(1)
    agent = FakeClaude(args)
    exit_with(agent.main)


if __name__ == "__main__":
    main()
