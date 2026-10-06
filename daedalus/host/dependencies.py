"""A bounded dependency planner; approval is an API action, never a model tool."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest, ProviderDelta
from protocore.contracts.types import (
    Message,
    MessageRole,
    TextBlock,
    ToolDefinition,
    ToolParameterSchema,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus import supervisor_client
from daedalus.host.proposals import new_proposal, same_generation, with_activity
from daedalus.security.redact import redact

KEY = "dependency_proposal"
TOOLS = [
    ToolDefinition(name="InspectEnvironment", description="Read available tools, installed agent Python packages and installation capabilities.", parameters=ToolParameterSchema(additional_properties=False)),
    ToolDefinition(name="ProposeDependencies", description="Prepare an additive package recipe for human review. This does not install anything. Use package names only; Python may use ==version pins.", parameters=ToolParameterSchema(
        properties={"python": {"type": "array", "items": {"type": "string"}, "maxItems": 64}, "system": {"type": "array", "items": {"type": "string"}, "maxItems": 64}, "explanation": {"type": "string", "maxLength": 4000}},
        required=["python", "system", "explanation"], additional_properties=False,
    )),
]
PROMPT = """You prepare dependency additions for Daedalus, not general programming tasks.
InspectEnvironment first. Then propose the smallest set of packages satisfying the operator request.
Include only explicitly requested tools or one necessary package-manager equivalent per tool.
Do not enumerate transitive dependencies: the package manager resolves those. Do not add optional
utilities, alternate implementations, related tool collections or packages for future tasks.
Each package list has at most 64 entries. Keep the explanation below 2000 characters: briefly group
the additions and name requested tools that cannot be installed through these package managers.
Python packages come from PyPI into an isolated agent environment, never the application's environment.
System packages come from the reported OS package manager. Respect installation capabilities.
Use the reported distribution and version, not the catalogue of another apt-based distribution.
ProposeDependencies checks configured apt repositories and may reject unavailable packages.
If rejected, remove those packages and explain which requested tools remain unsupported.
Never offer commands, repository edits, downloads, custom indexes, privilege elevation or removals.
Explain each addition in the user's language. Explain uncertainty and transitive dependencies;
do not claim a package version or installation has been verified. Unpinned versions resolve at install.
Use ProposeDependencies to finish. Only a human can approve installation and restart.
"""


class DependencyPlanner:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.inventory_cache: tuple[float, dict[str, Any]] | None = None

    async def rpc(self, op: str, **params: Any) -> Any:
        settings = self.app.settings
        result = await supervisor_client.call(settings.supervisor_address, op, token_path=settings.supervisor_token_path, timeout=90, **params)
        if not isinstance(result, dict):
            raise RuntimeError("update the supervisor to enable dependency management")
        return result

    async def proposal(self) -> dict[str, Any] | None:
        value = await self.app.db.kv_get(KEY)
        if value and value["state"] in ("planning", "validating") and (self.task is None or self.task.done()):
            await self._progress(value, "failed")
            value = {**value, "state": "failed", "error": "dependency planning was interrupted; submit the request again"}
            await self.app.db.kv_set(KEY, value)
        return value

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            await self.proposal()

    async def view(self) -> dict[str, Any]:
        status = await self.rpc("dependencies_status")
        if (status.get("job") or {}).get("log"):
            status["job"]["log"] = redact(status["job"]["log"])
        if self.inventory_cache is None or time.monotonic() - self.inventory_cache[0] > 30:
            inventory = await self.rpc("dependencies_inventory")
            self.inventory_cache = time.monotonic(), inventory
        proposal = await self.proposal()
        if proposal and proposal["state"] == "ready" and (status.get("job") or {}).get("id") == proposal["id"]:
            # The supervisor may accept just before the HTTP connection is lost to the restart.
            # Its durable receipt wins over a browser which never saw the acknowledgement.
            proposal = {**with_activity(proposal, "applying"), "state": "applying"}
            await self.app.db.kv_set(KEY, proposal)
        return {**self.inventory_cache[1], **status, "proposal": proposal, "models": [{"id": key, "label": preset.display(key)} for key, preset in self.app.config.presets.items()]}

    async def start(self, text: str, preset: str) -> dict[str, Any]:
        async with self.lock:
            if self.task and not self.task.done():
                raise ValueError("dependency planning is already running")
            if preset not in self.app.config.presets:
                raise ValueError("choose a configured model")
            if not text.strip() or len(text) > 2000:
                raise ValueError("describe the request in 1–2000 characters")
            status = await self.rpc("dependencies_status")
            if (status.get("job") or {}).get("state") in ("installing", "restarting"):
                raise ValueError("dependency installation is running")
            if not status["capability"]["python"]:
                raise ValueError(status["capability"]["reason"])
            value = new_proposal("dependencies", request=text.strip(), preset=preset)
            await self.app.db.kv_set(KEY, value)
            self.task = asyncio.create_task(self._plan(value))
            return value

    async def cancel(self, proposal_id: str) -> None:
        async with self.lock:
            value = await self.proposal()
            if not value or value["id"] != proposal_id or value["state"] not in ("planning", "validating", "ready", "failed"):
                raise ValueError("this proposal is no longer cancellable")
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self._progress(value, "cancelled")
            await self.app.db.kv_set(KEY, {**value, "state": "cancelled"})

    async def approve(self, proposal_id: str) -> dict[str, Any]:
        async with self.lock:
            value = await self.proposal()
            if value and value["id"] == proposal_id and value["state"] == "applying":
                return {"id": proposal_id, "state": "applying"}
            if not value or value["id"] != proposal_id or value["state"] != "ready":
                raise ValueError("this proposal is no longer awaiting approval")
            # Close the new-run gate before checking existing runs: submit can await storage while
            # this request awaits the supervisor, but may not start in that interval.
            marker = self.app.settings.state_dir / "dependencies" / "maintenance"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(value["id"])
            try:
                if self.app.manager.dependency_installation_busy():
                    raise ValueError("stop running agents and wait for their results to be saved before installing")
                result = await self.rpc("dependencies_apply", proposal=value["proposal"], id=value["id"])
            except Exception:
                # A lost acknowledgement is ambiguous; keep the gate until status proves that no
                # job is running. Retrying acceptance cannot create a second installation.
                try:
                    status = await self.rpc("dependencies_status")
                    job = status.get("job")
                    if not job or job["state"] not in ("installing", "restarting"):
                        marker.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            await self.app.db.kv_set(KEY, {**with_activity(value, "applying"), "state": "applying"})
            return result

    async def _plan(self, value: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(180):
                result = await self._run(value)
            await self._progress(value, "ready")
            current = await self.app.db.kv_get(KEY)
            if same_generation(current, value) and current["state"] in ("planning", "validating"):
                await self.app.db.kv_set(KEY, {**value, "state": "ready", **result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._progress(value, "failed")
            current = await self.app.db.kv_get(KEY)
            if same_generation(current, value) and current["state"] in ("planning", "validating"):
                await self.app.db.kv_set(KEY, {**value, "state": "failed", "error": str(exc)[:2000] or type(exc).__name__})

    async def _progress(self, value: dict[str, Any], stage: str, detail: str = "") -> None:
        now = time.time()
        value.update(with_activity(value, stage, detail))
        events = value.setdefault("progress", [])
        if not events or (events[-1]["stage"], events[-1].get("detail", "")) != (stage, detail):
            events.append({"at": now, "stage": stage, "detail": detail[:240]})
            del events[:-40]
        current = await self.app.db.kv_get(KEY)
        if same_generation(current, value) and current["state"] in ("planning", "validating"):
            if stage == "check":
                value["state"] = "validating"
            await self.app.db.kv_set(KEY, dict(value))

    async def _run(self, value: dict[str, Any]) -> dict[str, Any]:
        manager = self.app.manager
        rungs, preset = manager.resolve_model({"preset": value["preset"]})
        provider, model = rungs[0]
        messages = [Message(role=MessageRole.system, content_blocks=[TextBlock(text=PROMPT)]), Message(role=MessageRole.user, content_blocks=[TextBlock(text=value["request"])])]
        inspected = False
        for _ in range(5):
            await self._progress(value, "model")
            if manager.budget_exceeded() and not manager.provider_costs_nothing(preset.provider, model):
                raise ValueError("daily inference budget exceeded")
            request = LLMRequest(model=model, messages=messages, tools=TOOLS, max_tokens=2500, temperature=0.2, extra={"enable_thinking": False}, observability=LLMObservabilityContext(run_id=value["id"], call_category="dependency_planning", call_purpose="dependency_proposal"))
            names: dict[str, str] = {}
            calls: list[tuple[str, str, dict[str, Any]]] = []
            thinking = ""
            stream_error = ""
            last_progress = time.monotonic()
            async for delta in provider.stream_with_tools(request):
                if time.monotonic() - last_progress >= 2:
                    # Only operational activity is public, never the model's private reasoning.
                    await self._progress(value, "model")
                    last_progress = time.monotonic()
                if not isinstance(delta, ProviderDelta):
                    raise ValueError("provider does not support normalized tool streams")
                if delta.kind == "thinking":
                    thinking += delta.content or ""
                    if len(thinking) > 32000:
                        thinking = thinking[:32000]
                        stream_error = "dependency planner exceeded the reasoning limit"
                elif delta.kind == "tool_use_start":
                    names[delta.tool_call_id or ""] = delta.tool_name or ""
                elif delta.kind == "tool_use_stop":
                    if delta.args_partial_truncated or delta.truncated_by_output_cap or delta.tool_input_final is None:
                        stream_error = "the model returned incomplete tool arguments; no proposal was accepted"
                        if delta.truncated_by_output_cap:
                            stream_error = "the model exhausted its 2500-token response limit before completing the package proposal; no proposal was accepted. Request a smaller package group or choose another model"
                        continue
                    calls.append((delta.tool_call_id or "", names.get(delta.tool_call_id or "", delta.tool_name or ""), delta.tool_input_final))
                    if len(calls) > 4:
                        calls = calls[:4]
                        stream_error = "too many dependency tool calls"
            # Usage is emitted after tool stops. Rejecting inside the stream hides the cost of
            # failed generations; drain it before rejecting, and never execute a partial batch.
            if stream_error:
                raise ValueError(stream_error)
            if not calls:
                raise ValueError("the model did not prepare a dependency proposal")
            messages.append(Message(role=MessageRole.assistant, reasoning_content=thinking or None, content_blocks=[ToolUseBlock(tool_call_id=cid, name=name, arguments_json=json.dumps(args)) for cid, name, args in calls]))
            results = []
            for cid, name, args in calls:
                try:
                    if name == "InspectEnvironment" and not args:
                        await self._progress(value, "inspect")
                        outcome = await self.rpc("dependencies_inventory")
                        inspected = True
                    elif name == "ProposeDependencies" and inspected:
                        await self._progress(value, "check")
                        if set(args) != {"python", "system", "explanation"} or not isinstance(args["explanation"], str) or not 1 <= len(args["explanation"]) <= 4000:
                            raise ValueError("provide package lists and a short explanation")
                        proposal = await self.rpc("dependencies_preview", additions={"python": args["python"], "system": args["system"]})
                        return {"proposal": proposal, "explanation": args["explanation"]}
                    else:
                        raise ValueError("only InspectEnvironment and then ProposeDependencies are available")
                    results.append(ToolResultBlock(tool_call_id=cid, content=json.dumps(outcome)))
                except (ValueError, RuntimeError) as exc:
                    await self._progress(value, "correction", redact(str(exc)))
                    results.append(ToolResultBlock(tool_call_id=cid, content=str(exc), is_error=True))
            # Provider serialization only preserves tool results on tool-role messages;
            # a user-role wrapper silently drops them and leaves unanswered tool calls.
            messages.append(Message(role=MessageRole.tool, content_blocks=results))
        raise ValueError("dependency planning reached its five-turn limit")
