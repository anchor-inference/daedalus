"""The operator's action rules applied to a session's calls: the levels in the policy gate, the review before the call.

:mod:`daedalus.host.action_rules` says what a call is and what the rules make of it; this is where a
session's calls meet that answer. It has three doors, because a call is judged in three places:

* :meth:`RulesGate.apply` — the synchronous policy gate, after the policy itself. It turns an allowed
  call into a question or a handoff when its level says so, and marks one a review stands before;
* :meth:`RulesGate.pre_tool` — the pre-tool hook, which may wait: it runs the review the gate marked;
* :meth:`RulesGate.browser` — a sensitive browser action, judged inside the tool once the browser has
  looked at the element.

A handoff is announced like a policy question (``permission.pending``, kind ``handoff``), so every
front that draws approval cards draws it; it is answered with *Done*, never with *Allow*. A review's
question is a policy question in every respect — the same key, the same grant, the same buttons.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.hooks import HookActionKind, HookResult
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.browser.monitor import PROMPT as INJECTION_PROMPT
from daedalus.browser.monitor import SAMPLE_CHARS as INJECTION_SAMPLE_CHARS
from daedalus.browser.monitor import TIMEOUT_SECONDS as INJECTION_TIMEOUT_SECONDS
from daedalus.host import action_rules as rules
from daedalus.host import prompts
from daedalus.host.engine_factory import TENANT, refusal_text
from daedalus.host.policy import (
    ALLOW,
    ASK,
    DENY,
    HANDOFF,
    Decision,
    approval_key,
    canonical,
    similar_covers,
    similar_grant,
)

logger = logging.getLogger(__name__)

DUE_TTL_SECONDS = 600
"""How long a review the gate marked waits for its hook. The hook follows the gate within the same
dispatch; a mark older than this belongs to a call that never reached it and is dropped."""


class RulesGate:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.log = rules.ReviewLog(manager.db)
        self._due: dict[tuple[str, str], tuple[str, rules.Ruling, float]] = {}

    @property
    def config(self) -> Any:
        return self.manager.config.action_rules

    # -- the gate --------------------------------------------------------------------------------

    def apply(self, state: Any, tool: str, arguments: dict[str, Any], decision: Decision, grants: set[str], similar: list[dict[str, Any]] | None = None) -> Decision:
        """The rules' answer on top of the policy's. Never looser than ``decision``.

        ``grants`` and ``similar`` are the session's open grants and standing "Allow similar" grants:
        a question these rules ask is answered by them exactly as a policy question is."""
        if decision.action == DENY:
            return decision
        ruled = rules.ruling(rules.categories_of(tool, arguments), self.config)
        if ruled is None:
            return decision
        key = approval_key(tool, arguments)
        if ruled.level == "handoff":
            what = self.manager.redactor.redact(canonical(tool, arguments))[:300]
            if state is not None:
                self.handoff(state, key, tool, what, ruled.category)
            return Decision(HANDOFF, f"the operator does actions that would {rules.CATEGORY_WORDS[ruled.category]} themselves", ruled.rule, key=key, hosts=decision.hosts, told=rules.handoff_text(ruled.category, what))
        if decision.action != ALLOW:
            return decision  # the policy's own question stands; a level cannot lift it
        if decision.key or decision.reason.startswith("allowed by the operator"):
            return decision  # the operator approved this very call (or its family) already
        if ruled.level == "ask" or ruled.reviewed:
            # A review's question is answered like the level's own: the answer is the review's verdict.
            rule = ruled.rule if ruled.level == "ask" else f"review.{ruled.category}"
            if key in grants:
                return Decision(ALLOW, f"granted by the operator ({key})", rule, key=key, hosts=decision.hosts)
            family = similar_grant(tool, arguments, rule)
            standing = next((g for g in similar or () if similar_covers(g, tool, rule, family)), None)
            if standing is not None:
                return Decision(ALLOW, f"allowed by the operator for this session: {standing.get('label')}", rule, hosts=decision.hosts)
        if ruled.level == "ask":
            return Decision(ASK, f"the operator's rules ask before an action that would {rules.CATEGORY_WORDS[ruled.category]}", ruled.rule, key=key, hosts=decision.hosts)
        if ruled.reviewed:
            decision.review = ruled
        return decision

    def due(self, session_id: str, run_id: str, tool: str, arguments: dict[str, Any], ruled: rules.Ruling) -> None:
        now = time.monotonic()
        for mark in [m for m, (_, _, at) in self._due.items() if now - at > DUE_TTL_SECONDS]:
            self._due.pop(mark, None)
        self._due[(run_id, approval_key(tool, arguments))] = (session_id, ruled, now)

    # -- the review --------------------------------------------------------------------------------

    async def pre_tool(self, payload: dict[str, Any]) -> HookResult | None:
        """The review of a call the gate marked; ``None`` lets it run."""
        tool = str(payload.get("tool_name") or "")
        arguments = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        assert isinstance(arguments, dict)
        key = approval_key(tool, arguments)
        mark = self._due.pop((str(payload.get("run_id") or ""), key), None)
        if mark is None:
            return None
        session_id, ruled, _ = mark
        state = self.manager.live_state(session_id)
        summary = rules.summarise_action(tool, arguments, self.manager.redactor.redact)
        verdict = await self._review(state, ruled, summary)
        await self._record(session_id, str(payload.get("run_id") or ""), tool, ruled, verdict, self.manager.redactor.redact(canonical(tool, arguments)))
        if verdict.verdict == "allow":
            return None
        if verdict.verdict == "block":
            await self._notice(state, session_id, tool, self.manager.redactor.redact(canonical(tool, arguments))[:300], ruled, verdict, key)
            return HookResult(action=HookActionKind.DENY, reason=rules.block_text(ruled.category, verdict.reason))
        asked = Decision(ASK, f"the auto-review asks: {verdict.reason}", f"review.{ruled.category}", key=key)
        if state is not None:
            self.manager.record_question(state, asked, tool, arguments)
        hint = prompts.STAFF_POLICY_HINT if state is not None and self.manager.is_staff(state) else None
        return HookResult(action=HookActionKind.DENY, reason=refusal_text(asked, hint))

    async def browser(self, session_id: str, ask: Any) -> Any:
        """A sensitive browser action under the rules: the same ask, with its decision tightened."""
        decision: Decision = ask.decision
        if decision.action == DENY:
            return ask
        ruled = rules.ruling(rules.browser_categories(ask.kinds), self.config)
        if ruled is None:
            return ask
        state = self.manager.live_state(session_id)
        what = self.manager.redactor.redact(f"{ask.action} {ask.element or ask.name} on {ask.origin}")[:300]
        if ruled.level == "handoff":
            if state is not None:
                self.handoff(state, decision.key, ask.tool, what, ruled.category)
            return replace(ask, decision=Decision(HANDOFF, "kept by the operator", ruled.rule, key=decision.key, told=rules.handoff_text(ruled.category, what)))
        if decision.action != ALLOW:
            return ask
        grants = self._grants(state)
        if ruled.level == "ask" or (ruled.reviewed and decision.key in grants):
            # A question the gate asks, or answers from the operator's grant for it.
            return replace(ask, decision=Decision(ASK, f"the operator's rules ask before an action that would {rules.CATEGORY_WORDS[ruled.category]}", ruled.rule, key=decision.key))
        if not ruled.reviewed:
            return ask
        summary = rules.summarise_action(ask.tool, {"action": ask.action, "element": ask.element, "name": ask.name, "page": ask.origin, "kinds": list(ask.kinds), "typed_characters": ask.text_len}, self.manager.redactor.redact)
        verdict = await self._review(state, ruled, summary)
        run_id = str(getattr(state, "run_id", "") or "") if state is not None else ""
        await self._record(session_id, run_id, ask.tool, ruled, verdict, what)
        if verdict.verdict == "allow":
            return ask
        if verdict.verdict == "block":
            await self._notice(state, session_id, ask.tool, what, ruled, verdict, decision.key)
            return replace(ask, decision=Decision(DENY, verdict.reason, f"review.{ruled.category}", key=decision.key, told=rules.block_text(ruled.category, verdict.reason)))
        return replace(ask, decision=Decision(ASK, f"the auto-review asks: {verdict.reason}", f"review.{ruled.category}", key=decision.key))

    async def _review(self, state: Any, ruled: rules.Ruling, summary: rules.ActionSummary) -> rules.Verdict:
        # The one reading of who said what that knows an answer to the agent's question is the operator's.
        from daedalus.host.session_runner import operator_passages  # Lazy: the session runner imports this module

        engine = getattr(state, "engine", None) if state is not None else None
        history = list(getattr(engine, "history", None) or ()) if engine is not None else []
        metadata = state.metadata if state is not None else {}
        prompt = rules.build_prompt(
            title=str(getattr(getattr(state, "session", None), "title", "") or ""),
            brief=str(metadata.get("brief") or ""),
            request=rules.operator_request(text for _, text in operator_passages(history)),
            category=ruled.category, level=ruled.level, action=summary.text, redact=self.manager.redactor.redact,
        )
        return await rules.review(self._ask_model, prompt, preset=self.config.review_preset, timeout=float(self.config.review_timeout_seconds), cut=summary.cut)

    async def injection_check(self, origin: str, text: str) -> list[str]:
        """A small model's look at a fetched page, when the operator switched it on: the warning's
        words when it judges the page to be talking to the agent, nothing otherwise.

        It fails open, as the browser's monitor does and unlike the review: it marks text and decides
        nothing, and the fence around the page stands either way."""
        if not self.config.injection_model:
            return []
        try:
            answer, _ = await asyncio.wait_for(
                self._ask_model(INJECTION_PROMPT.format(origin=origin) + text[:INJECTION_SAMPLE_CHARS], self.config.injection_preset, INJECTION_TIMEOUT_SECONDS),
                timeout=INJECTION_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("the injection check could not judge %s: %s", origin, exc)
            return []
        words = answer.strip().split(None, 1)
        return ["a small model judged that it addresses the agent"] if words and words[0].strip(".:,*").upper() == "INJECTION" else []

    async def _ask_model(self, prompt: str, preset: str, timeout: float) -> tuple[str, str]:
        """One request to the review's preset, else a middle one of the table."""
        config = self.manager.config
        if preset not in config.presets:
            preset = config.middle_preset() or ""
        provider, model = self.manager.providers.rungs_for(config, preset or None)[0]
        request = LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompt)])],
            max_tokens=160,
            temperature=0.0,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, call_purpose="action_review", call_category="session"),
        )
        response = await provider.complete_text(request)
        return "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)), model

    async def _record(self, session_id: str, run_id: str, tool: str, ruled: rules.Ruling, verdict: rules.Verdict, summary: str) -> None:
        try:
            await self.log.record(session_id=session_id, run_id=run_id, tool=tool, category=ruled.category, level=ruled.level, verdict=verdict, summary=summary)
        except Exception:  # noqa: BLE001 — the log is the operator's record, not a condition of the action
            logger.warning("could not record the review of %s in session %s", tool, session_id, exc_info=True)
        logger.warning("action review %s %s for %s in session %s: %s", verdict.verdict, ruled.category, tool, session_id, verdict.reason)

    async def _notice(self, state: Any, session_id: str, tool: str, what: str, ruled: rules.Ruling, verdict: rules.Verdict, key: str) -> None:
        hook = self.manager.service_hooks.get("rules_notice")
        if hook is None:
            return
        title = str(getattr(getattr(state, "session", None), "title", "") or "") if state is not None else ""
        project = getattr(state, "project", None) if state is not None else None
        try:
            await hook({
                "session_id": session_id, "project_id": getattr(project, "id", None), "title": title, "tool": tool, "text": what,
                "category": ruled.category, "reason": verdict.reason, "key": key,
            })
        except Exception:  # noqa: BLE001
            logger.warning("could not post the review's notice", exc_info=True)

    @staticmethod
    def _grants(state: Any) -> set[str]:
        if state is None:
            return set()
        now = time.time()
        return {k for k, until in (state.metadata.get("policy_grants") or {}).items() if float(until) > now}

    # -- handoffs ----------------------------------------------------------------------------------

    def handoff(self, state: Any, key: str, tool: str, what: str, category: str) -> None:
        """Record a handoff and announce it once: the agent retrying the same call is the same request."""
        open_ = dict(state.metadata.get("policy_handoffs") or {})
        fresh = key not in open_
        open_[key] = {"tool": tool, "text": what, "category": category, "at": datetime.now(UTC).isoformat()}
        for meta in (state.metadata, state.session.metadata):
            meta["policy_handoffs"] = dict(list(open_.items())[-20:])
        if not fresh:
            return
        self.manager._spawn_background(self.manager.sessions.update_metadata(state.session.id, state.session.metadata), f"handoff:{state.session.id}")
        self.manager._publish_soon(state, "permission.pending", {
            "request_id": key,
            "request_ref": f"handoff:{state.session.id}:{key}",
            "kind": "handoff",
            "title": state.session.title,
            "tool": tool,
            "text": what,
            "category": category,
            "risk": "routine",
            "quick": True,
            "telegram": self.manager._telegram_delivers(state),
        })

    async def handoff_done(self, session_id: str, key: str, *, via: str) -> dict[str, Any]:
        """The operator did what was handed to them, or put it aside: the request is no longer open."""
        state = await self.manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        open_ = dict(state.metadata.get("policy_handoffs") or {})
        done = open_.pop(key, None)
        if done is None:
            raise LookupError("this handoff is not open")
        for meta in (state.metadata, state.session.metadata):
            meta["policy_handoffs"] = dict(open_)
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        await self.manager._publish(state, "permission.resolved", {"request_id": key, "request_ref": f"handoff:{session_id}:{key}", "decision": "done", "via": via})
        return {"key": key, "done": done}


__all__ = ["DUE_TTL_SECONDS", "RulesGate"]
