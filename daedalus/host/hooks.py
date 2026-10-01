"""The hook manager the engine calls around every tool: redaction on the way in, the operator's scripts around it.

``PostToolUse`` fires inside the core dispatcher before a result is appended to the history, so masking
there covers the transcript, snapshots, compaction summaries and every later model call at once — for
tools that returned. A tool that *raised* takes the dispatcher's failure path, which does not fire the
hook; the session runner masks that text when the event reaches it.

The operator's own scripts (``[hooks]`` in config.toml) run at ``pre_tool_use`` (may deny the call or
rewrite its arguments), ``post_tool_use`` (may rewrite the output) and ``run_finalize`` (fire and forget).
They get JSON on stdin (``event``, ``tool_name``, ``arguments``/``tool_output``, and what the core adds)
and answer with an exit code and, optionally, JSON on stdout. A script that fails or times out is a log
line, not a verdict: the call proceeds as if the script had allowed it. Scripts run with the same
environment tools get (credentials stay out), in their own process group, killed on timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from protocore.contracts.hooks import HookActionKind, HookResult, HookSpec, IHookManager
from protocore.contracts.types import HookEvent

from daedalus.processes import end_tree
from daedalus.security.redact import Redactor
from daedalus.tools.shell import shell_argv, shell_environment

logger = logging.getLogger(__name__)

DENY_EXIT = 2
"""A pre-tool script exits with this to refuse the call; its output is the reason the model sees."""

_EVENT_TO_SCRIPT = {HookEvent.pre_tool_use: "pre_tool", HookEvent.post_tool_use: "post_tool", HookEvent.run_finalize: "run_finished"}


class DaedalusHookManager(IHookManager):
    def __init__(self, redactor: Redactor, *, hooks_config: Callable[[], Any] | None = None) -> None:
        self.redactor = redactor
        self.redacted_calls = 0
        self._specs: dict[str, HookSpec] = {}
        self._hooks_config = hooks_config
        self._detached: set[asyncio.Task[Any]] = set()

    def _script(self, event: HookEvent) -> tuple[str, float]:
        cfg = self._hooks_config() if self._hooks_config is not None else None
        name = _EVENT_TO_SCRIPT.get(event)
        if cfg is None or name is None:
            return "", 0.0
        return str(getattr(cfg, name, "") or "").strip(), float(getattr(cfg, "timeout_seconds", 20.0))

    async def _run_script(self, script: str, payload: dict[str, Any], timeout: float) -> tuple[int, str] | None:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_argv(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=shell_environment(str(payload.get("session_id") or "hook")), start_new_session=True,
            )
            out, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")), timeout=timeout)
        except TimeoutError:
            logger.warning("hook script timed out after %.0fs: %s", timeout, script[:80])
            if proc is not None and proc.returncode is None:
                try:
                    end_tree(proc.pid, hard=True)
                except ProcessLookupError:
                    pass
                await proc.wait()
            return None
        except OSError as exc:
            logger.warning("hook script could not run (%s): %s", exc, script[:80])
            return None
        return proc.returncode or 0, out.decode("utf-8", "replace")

    async def aclose(self) -> None:
        """Let the fire-and-forget scripts finish before the process ends."""
        if self._detached:
            await asyncio.gather(*self._detached, return_exceptions=True)

    async def invoke(self, event: HookEvent, payload: dict[str, Any], tenant_id: str) -> HookResult:
        script, timeout = self._script(event)
        if event is HookEvent.run_finalize:
            if script:
                task = asyncio.create_task(self._run_script(script, {"event": "run_finished", **payload}, timeout), name="hook:run_finished")
                self._detached.add(task)
                task.add_done_callback(self._detached.discard)
            return HookResult(action=HookActionKind.ALLOW)
        if event is HookEvent.pre_tool_use:
            if not script:
                return HookResult(action=HookActionKind.ALLOW)
            answer = await self._run_script(script, {"event": "pre_tool", **payload}, timeout)
            if answer is None:
                return HookResult(action=HookActionKind.ALLOW)
            code, out = answer
            if code == DENY_EXIT:
                return HookResult(action=HookActionKind.DENY, reason=("refused by the operator's pre-tool hook: " + out.strip())[:1000])
            replacement = _json_object(out)
            rewritten = replacement.get("tool_input") if replacement and isinstance(replacement.get("tool_input"), dict) else (replacement.get("arguments") if replacement and isinstance(replacement.get("arguments"), dict) else None)
            if rewritten is not None:
                # The core reads ``tool_input``; a script may write either key.
                return HookResult(action=HookActionKind.MODIFY, reason="arguments rewritten by the operator's pre-tool hook", modifications={"tool_input": rewritten})
            return HookResult(action=HookActionKind.ALLOW)
        if event is not HookEvent.post_tool_use:
            return HookResult(action=HookActionKind.ALLOW)
        output = payload.get("tool_output")
        modifications: dict[str, Any] = {}
        if isinstance(output, str):
            cleaned = self.redactor.redact(output)
            if cleaned != output:
                self.redacted_calls += 1
                logger.warning("secret masked in the output of %s", payload.get("tool_name"))
                modifications["tool_output"] = cleaned
                output = cleaned
        if script:
            answer = await self._run_script(script, {"event": "post_tool", **payload, "tool_output": output}, timeout)
            if answer is not None:
                replacement = _json_object(answer[1])
                if replacement and isinstance(replacement.get("tool_output"), str):
                    modifications["tool_output"] = self.redactor.redact(replacement["tool_output"])
        if modifications:
            return HookResult(action=HookActionKind.MODIFY, reason="tool output rewritten", modifications=modifications)
        return HookResult(action=HookActionKind.ALLOW)

    async def register(self, spec: HookSpec) -> None:
        self._specs[spec.id] = spec

    async def unregister(self, hook_id: str, tenant_id: str) -> None:
        spec = self._specs.get(hook_id)
        if spec is not None and spec.tenant_id == tenant_id:
            self._specs.pop(hook_id)

    async def list(self, tenant_id: str, *, event: HookEvent | None = None) -> Sequence[HookSpec]:
        items = [s for s in self._specs.values() if s.tenant_id == tenant_id]
        if event is not None:
            items = [s for s in items if s.event is event]
        return items


def _json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


__all__ = ["DENY_EXIT", "DaedalusHookManager"]
