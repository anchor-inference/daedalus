"""The two tools a staff member reports through: ``Report`` and ``AskOrchestrator``.

They exist for staff sessions alone (``STAFF_ONLY_TOOLS``), and a command-line staff member has the
same two under the same names on its team server. Both go through the host's staff service, which
writes the rows and publishes the events; nothing here touches a table.
"""

from __future__ import annotations

from typing import Any, ClassVar

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult
from protocore.tools.ask_user import AskUserInput, AskUserOption, AskUserPauseRequested, AskUserQuestion
from protocore.tools.decorator import tool

from daedalus.tools import search_hint
from daedalus.tools._common import call_id, error, ok, services_for

QUESTION_MAX = 2000
OPTION_MAX = 200


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("staff") if manager is not None else None


@search_hint(
    "report to team progress checkpoint stuck needs input done deliverable "
    "отчитаться отчитайся доложить прогресс застрял готово нужна помощь чекпоинт сдать сдаю"
)
@tool(
    name="Report",
    description=(
        "Tell your team how your task stands. kind: 'checkpoint' (progress worth knowing), 'needs_input' (you "
        "cannot go on without a decision), 'stuck' (something outside your task blocks you) or 'done' (the "
        "deliverable meets the task's done-when; it hands the task in: work on your own branch goes to review for "
        "the operator to merge, and a worktree with uncommitted changes is refused — commit first; any other task "
        "also goes to review until the exact result is checked). note: a factual report preserved in full. "
        "evidence (with done): [{item, how, result}] for each check (C1 …) and requirement (R1 …) of the task — "
        "what you ran or looked at and what it showed. acknowledged: the requirements (R…) sent to you that you "
        "have taken into your plan. operator_steps: when the operator has to do something themselves — sign in, press, check — the steps for them, which reach them word for word: {goal, steps: […], roles?: [{account, purpose}], expected?, check?, limits?, verified: 'on-running-version' when you walked every step on the version that runs, else 'unverified', verified_how?}. Never a password in them: name where it is kept. artifacts: paths or links of what you produced. remember: one "
        "line to keep in your notes for every later session."
    ),
)
async def report(
    context: ToolContext,
    kind: str,
    note: str,
    artifacts: list[str] | None = None,
    remember: str | None = None,
    evidence: list[dict[str, str]] | None = None,
    acknowledged: list[str] | None = None,
    operator_steps: dict[str, Any] | None = None,
) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the team is not available in this installation")
    try:
        text = await hook(
            "report", session_id=context.session_id, kind=kind, note=note, artifacts=artifacts, remember=remember, evidence=evidence, acknowledged=acknowledged,
            operator_steps=operator_steps, call_id=call_id(context),
        )
    except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
        return error(context, str(exc))
    return ok(context, text)


# The decorator describes a list of dicts as a list of anything; the member is shown the three fields
# a piece of evidence has, so it does not invent its own and have them matched to nothing.
report().definition.parameters.properties["evidence"] = {
    "type": "array",
    "description": "With kind='done': one entry per check (C1 …) and requirement (R1 …).",
    "items": {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "The check or requirement: C1, R2, or its words."},
            "how": {"type": "string", "description": "What you ran, opened or looked at to check it."},
            "result": {"type": "string", "description": "What that showed."},
        },
        "required": ["item", "how", "result"],
    },
}
report().definition.parameters.properties["operator_steps"] = {
    "type": "object",
    "description": "Steps the operator follows themselves, delivered to them word for word.",
    "properties": {
        "goal": {"type": "string", "description": "What the operator achieves by following them."},
        "steps": {"type": "array", "items": {"type": "string"}, "description": "One step each, in order, naming what to open and press."},
        "roles": {
            "type": "array",
            "items": {"type": "object", "properties": {"account": {"type": "string"}, "purpose": {"type": "string"}}, "required": ["account", "purpose"]},
            "description": "Which account is which, when there are several.",
        },
        "expected": {"type": "string", "description": "What the operator sees when it worked."},
        "check": {"type": "string", "description": "A safe way to check it."},
        "limits": {"type": "string", "description": "What it does not cover."},
        "verified": {"type": "string", "enum": ["on-running-version", "unverified"]},
        "verified_how": {"type": "string", "description": "How you walked them, or which step you could not and why."},
    },
    "required": ["goal", "steps", "verified"],
}


class AskOrchestrator(Tool):
    """Written out rather than decorated: its third argument is called ``context``, as on the team
    server of a command-line member, and the decorator keeps that name for the tool context."""

    search_hint: ClassVar[str] = (
        "ask orchestrator lead manager question which option decision unsure stuck between options advice "
        "спросить спроси оркестратора лида руководителя посоветоваться уточнить какой вариант не уверен какой лучше"
    )

    @property
    def name(self) -> str:
        return "AskOrchestrator"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Ask your project's orchestrator a question you cannot settle yourself. Your run pauses until the "
                "answer arrives, and the answer is this tool's result. Give the options you see when there are some, "
                "and the context the orchestrator needs to decide without reading your whole session."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "question": {"type": "string", "description": "The question, in one or two sentences."},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "The choices you see, if any."},
                    "context": {"type": "string", "description": "What the orchestrator needs to know to decide."},
                },
                required=["question"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        hook = _hook(context)
        if hook is None:
            return error(context, "the team is not available in this installation")
        try:
            await hook("can_ask", session_id=context.session_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            return error(context, str(exc))
        body = str(arguments.get("question") or "").strip()
        extra = str(arguments.get("context") or "").strip()
        if not body:
            return error(context, "the question is empty")
        if extra:
            body = f"{body}\n\nContext: {extra}"
        options = arguments.get("options") or []
        if not isinstance(options, list):
            return error(context, "options is a list of strings")
        labels = list(dict.fromkeys(str(o).strip()[:OPTION_MAX] for o in options if str(o or "").strip()))[:20]
        # The same pause as AskUser: the core parks the run on it and the host resumes it with the
        # answer. The host tells it apart from AskUser by this tool's name.
        raise AskUserPauseRequested(
            AskUserInput(questions=[AskUserQuestion(question=body[:QUESTION_MAX], options=[AskUserOption(label=label) for label in labels], allow_custom=True)])
        )


TOOLS = [report, AskOrchestrator]

__all__ = ["TOOLS"]
