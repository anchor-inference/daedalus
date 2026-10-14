"""``SavePrompt`` — the prompt coach's one way to leave something behind: the finished prompt, as a file."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host import prompt_coach
from daedalus.tools import search_hint
from daedalus.tools._common import error, ok, services_for


@search_hint(
    "save prompt final improved prompt file write result coach "
    "сохранить сохрани промпт итоговый улучшенный промпт файл результат"
)
@tool(
    name="SavePrompt",
    description=(
        "Save the finished prompt into the operator's workspace as prompts/<date>-<title>.md (never over an "
        "existing file) and offer it to the operator to put into their message. Call it once the prompt is "
        "final, with a short title naming the task and exactly the prompt you showed; call it again after a "
        "revision. Returns the saved path."
    ),
)
async def save_prompt(context: ToolContext, title: str, prompt: str) -> ToolResult:
    manager = services_for(context).extra.get("manager")
    if manager is None:
        return error(context, "SavePrompt is unavailable in this session")
    try:
        saved = await prompt_coach.save_prompt(manager, context.session_id, title, prompt)
    except prompt_coach.CoachRefused as exc:
        return error(context, str(exc))
    return ok(context, f"Saved as {saved['path']} in the operator's workspace; the window over their chat now offers it. Finish with one short line.", path=saved["path"])


TOOLS = [save_prompt]

__all__ = ["TOOLS"]
