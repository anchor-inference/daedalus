"""A bounded working-rules editor whose proposal cannot apply itself."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import time
from typing import Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest, ProviderDelta
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.host.prompts import DEFAULT_RULES
from daedalus.host.proposals import new_proposal, same_generation, with_activity

KEY = "prompt_change_proposal"
MAX_RULES_CHARS = 40_000
PROPOSE = ToolDefinition(
    name="ProposeWorkingRules",
    description="Return the complete revised working-rules text for human review. This does not apply it.",
    parameters=ToolParameterSchema(
        properties={
            "rules": {"type": "string", "maxLength": MAX_RULES_CHARS},
            "summary": {"type": "string", "maxLength": 2000},
        },
        required=["rules", "summary"],
        additional_properties=False,
    ),
)
PROMPT = """You edit the operator-controlled Working Rules section of an agent system prompt.
The operator gives you the complete current text and describes a desired behavioral change.
Return the complete revised text through ProposeWorkingRules. Preserve every unrelated rule exactly
where practical. Make the smallest coherent change: add, remove or rewrite only what the request
requires. Keep rules direct and operational. Do not add commentary, markdown fences, hidden policy,
or claims about changes outside this section. The governance text is separate and cannot be edited
here. Write the summary in the operator's language and name what changed, not what you considered.
Only a human can review and apply the result.
"""


def effective_rules(config: Any) -> str:
    return (config.prompt.rules.strip() or DEFAULT_RULES.strip())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def rules_diff(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="Current working rules",
        tofile="Proposed working rules",
    ))


class PromptChangePlanner:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()

    async def proposal(self) -> dict[str, Any] | None:
        value = await self.app.db.kv_get(KEY)
        if value and value["state"] in ("planning", "validating") and (self.task is None or self.task.done()):
            value = {**value, "state": "failed", "updated_at": time.time(), "error": "proposal generation was interrupted; submit the request again"}
            await self.app.db.kv_set(KEY, value)
        return value

    async def view(self) -> dict[str, Any]:
        return {
            "proposal": await self.proposal(),
            "models": [{"id": key, "label": preset.display(key)} for key, preset in self.app.config.presets.items()],
        }

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            await self.proposal()

    async def start(self, instruction: str, preset_id: str) -> dict[str, Any]:
        async with self.lock:
            if self.task and not self.task.done():
                raise ValueError("a working-rules proposal is already being prepared")
            if preset_id not in self.app.config.presets:
                raise ValueError("choose a configured model")
            instruction = instruction.strip()
            if not 1 <= len(instruction) <= 2000:
                raise ValueError("describe the change in 1–2000 characters")
            before = effective_rules(self.app.config)
            value = new_proposal(
                "working_rules",
                instruction=instruction,
                preset=preset_id,
                stage="model",
                base_digest=digest(before),
            )
            await self.app.db.kv_set(KEY, value)
            self.task = asyncio.create_task(self._plan(value, before))
            return value

    async def cancel(self, proposal_id: str) -> None:
        async with self.lock:
            value = await self.proposal()
            if not value or value["id"] != proposal_id or value["state"] not in ("planning", "validating", "ready", "failed"):
                raise ValueError("this proposal is no longer cancellable")
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self.app.db.kv_set(KEY, {**value, "state": "cancelled", "stage": "cancelled", "updated_at": time.time()})

    async def approve(self, proposal_id: str) -> dict[str, Any]:
        async with self.lock:
            value = await self.proposal()
            if value and value["id"] == proposal_id and value["state"] == "applied":
                rules = self._validate(value.get("rules"))
                return {"applied": True, "rules": "" if rules == DEFAULT_RULES.strip() else rules}
            if not value or value["id"] != proposal_id or value["state"] != "ready":
                raise ValueError("this proposal is no longer awaiting review")
            if digest(effective_rules(self.app.config)) != value["base_digest"]:
                raise ValueError("working rules changed after this proposal was prepared; generate a new proposal")
            rules = self._validate(value.get("rules"))
            stored = "" if rules == DEFAULT_RULES.strip() else rules
            new_config = self.app.config.model_copy(update={"prompt": self.app.config.prompt.model_copy(update={"rules": stored})})
            await self.app.save_config(new_config)
            applied = {**with_activity(value, "applied"), "state": "applied", "stage": "applied"}
            await self.app.db.kv_set(KEY, applied)
            return {"applied": True, "rules": stored}

    async def _plan(self, value: dict[str, Any], before: str) -> None:
        try:
            async with asyncio.timeout(180):
                result = await self._run(value, before)
            current = await self.app.db.kv_get(KEY)
            if same_generation(current, value) and current["state"] in ("planning", "validating"):
                ready = {**with_activity(value, "ready"), **result, "state": "ready", "stage": "ready"}
                await self.app.db.kv_set(KEY, ready)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = await self.app.db.kv_get(KEY)
            if same_generation(current, value) and current["state"] in ("planning", "validating"):
                failed = {**with_activity(value, "failed"), "state": "failed", "stage": "failed", "error": str(exc)[:2000] or type(exc).__name__}
                await self.app.db.kv_set(KEY, failed)

    async def _run(self, value: dict[str, Any], before: str) -> dict[str, str]:
        manager = self.app.manager
        rungs, preset = manager.resolve_model({"preset": value["preset"]})
        provider, model = rungs[0]
        user = f"Desired change:\n{value['instruction']}\n\nCurrent working rules:\n<working_rules>\n{before}\n</working_rules>"
        messages = [Message(role=MessageRole.system, content_blocks=[TextBlock(text=PROMPT)]), Message(role=MessageRole.user, content_blocks=[TextBlock(text=user)])]
        if manager.budget_exceeded() and not manager.provider_costs_nothing(preset.provider, model):
            raise ValueError("daily inference budget exceeded")
        request = LLMRequest(
            model=model,
            messages=messages,
            tools=[PROPOSE],
            max_tokens=min(12_000, preset.max_output_tokens),
            temperature=0.1,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(run_id=value["id"], call_category="prompt_editing", call_purpose="working_rules_proposal"),
        )
        calls: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        stream_error = ""
        last_progress = time.monotonic()
        async for delta in provider.stream_with_tools(request):
            if time.monotonic() - last_progress >= 2:
                value["updated_at"] = time.time()
                current = await self.app.db.kv_get(KEY)
                if current and current["id"] == value["id"] and current["state"] == "planning":
                    await self.app.db.kv_set(KEY, dict(value))
                last_progress = time.monotonic()
            if not isinstance(delta, ProviderDelta):
                raise ValueError("provider does not support normalized tool streams")
            if delta.kind == "tool_use_start":
                names[delta.tool_call_id or ""] = delta.tool_name or ""
            elif delta.kind == "tool_use_stop":
                if delta.args_partial_truncated or delta.truncated_by_output_cap or delta.tool_input_final is None:
                    stream_error = "the model returned an incomplete proposal; try a model with a larger output limit"
                elif names.get(delta.tool_call_id or "", delta.tool_name or "") == PROPOSE.name:
                    calls.append(delta.tool_input_final)
                else:
                    stream_error = "the model used an unavailable operation"
        if stream_error:
            raise ValueError(stream_error)
        if len(calls) != 1:
            raise ValueError("the model did not return exactly one working-rules proposal")
        args = calls[0]
        if set(args) != {"rules", "summary"} or not isinstance(args["summary"], str) or not 1 <= len(args["summary"].strip()) <= 2000:
            raise ValueError("the model returned an invalid proposal summary")
        rules = self._validate(args.get("rules"))
        patch = rules_diff(before, rules)
        if not patch:
            raise ValueError("the model did not change the working rules")
        return {"rules": rules, "summary": args["summary"].strip(), "diff": patch}

    @staticmethod
    def _validate(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("the model did not return working-rules text")
        rules = value.strip()
        if not rules or len(rules) > MAX_RULES_CHARS:
            raise ValueError(f"working rules must contain 1–{MAX_RULES_CHARS} characters")
        return rules
