"""Side questions: leave the operator a question that does not stop the work, and take it back.

Through ``manager.service_hooks["side_questions"]`` (``daedalus.extensions.side_questions``). Every
ordinary working session has them; the companion, orchestrators, the main orchestrator, staff and
subagents are refused both by the lists in :mod:`daedalus.config`, since each reaches the operator (or
its leader) its own way.
"""

from __future__ import annotations

from typing import Any, ClassVar

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint
from daedalus.tools._common import error, ok, services_for


async def _call(context: ToolContext, op: str, /, **kwargs: Any) -> ToolResult:
    manager = services_for(context).extra.get("manager")
    hook = manager.service_hooks.get("side_questions") if manager is not None else None
    if hook is None:
        return error(context, "side questions are not available on this installation")
    try:
        return ok(context, str(await hook(op, session_id=context.session_id, **kwargs)))
    except (KeyError, ValueError, RuntimeError) as exc:
        return error(context, str(exc))


class SideQuestion(Tool):
    """Written out rather than decorated so each argument carries its own description: the decorator
    gives a parameter only its type, and "why" or "options" alone do not say what goes in them."""

    search_hint: ClassVar[str] = (
        "side question for later non-blocking ask the operator when they can note a question keep working "
        "вопрос на потом побочный вопрос не блокируя спросить позже вопрос оператору отложенный вопрос"
    )

    @property
    def name(self) -> str:
        return "SideQuestion"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Leave the operator a question that can wait, and keep working. It returns at once with a short id; "
                "the operator answers in the side panel when they can, and the answer arrives later as a message of its "
                "own. For what you can continue without — a preference, a naming choice, whether something may go. "
                "What you cannot continue without is AskUser, which stops until answered. Never ask what you can find "
                "out yourself, and never ask the same thing twice; a few may wait at once."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "question": {"type": "string", "description": "The question in one sentence, readable without the conversation."},
                    "why": {"type": "string", "description": "Optional: what hangs on it, or what you will assume until answered."},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "Optional: suggested answers; the operator may still answer in words."},
                },
                required=["question"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        options = arguments.get("options") or []
        if not isinstance(options, list):
            return error(context, "options is a list of strings")
        return await _call(context, "ask", question=str(arguments.get("question") or ""), why=str(arguments.get("why") or ""),
                           options=[str(o) for o in options])


@search_hint(
    "drop withdraw side question answered it myself no longer needed take back "
    "снять вопрос отозвать побочный вопрос нашёл ответ сам убрать вопрос неактуально"
)
@tool(
    name="DropSideQuestion",
    description=(
        "Take back side questions of yours the moment you settle them yourself — you found the answer, the plan "
        "changed — so the operator does not answer them for nothing; they leave the operator's panel at once. ids: "
        "their short ids. reason: a few words the operator sees (\"found it in the README\")."
    ),
)
async def drop_side_question(context: ToolContext, ids: list[str], reason: str) -> ToolResult:
    if not isinstance(ids, list):
        return error(context, "ids is a list of short ids")
    return await _call(context, "withdraw", ids=[str(i) for i in ids], reason=reason)


TOOLS = [SideQuestion, drop_side_question]

__all__ = ["TOOLS", "SideQuestion"]
