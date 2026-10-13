"""The main orchestrator's tools: see the projects, hand them work, follow it, cancel it, create a
project, pass on an answer the operator gave in words, and read its own files.

They are not ``TOOLS`` of this package: three of them share a name with another role's tool (the
companion's ``Delegate`` and ``Projects``, the project orchestrator's ``Answer``), so they are
registered in the main orchestrator's own registry (:func:`build`), which no other session is shown.
Each goes through the dispatcher extension, which first checks that the calling session still holds
the office.
"""

from __future__ import annotations

from typing import Any, ClassVar

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import _guarded, search_hint
from daedalus.tools._common import error, ok, services_for


async def _call(context: ToolContext, operation: str, /, **kwargs: Any) -> ToolResult:
    manager = services_for(context).extra.get("manager")
    hook = manager.service_hooks.get("dispatcher") if manager is not None else None
    if hook is None:
        return error(context, "the main orchestrator is not available on this installation")
    try:
        text = await hook(operation, session_id=context.session_id, **kwargs)
    except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
        return error(context, str(exc))
    return ok(context, str(text))


@search_hint(
    "operator projects overview orchestrator on off open dispatches staff working latest reports project status "
    "проекты проектов какие проекты что по проектам обзор проектов статус проекта оркестратор диспатчи кто работает отчеты"
)
@tool(
    name="Projects",
    description=(
        "The operator's projects. No argument: one line each — orchestrator on or off, open dispatches, tasks doing and in "
        "review, staff working, how many requests wait for the operator, the last report. project (a name or id): its "
        "brief's first lines, folders, board, team, open dispatches, and the latest reports and journal entries."
    ),
)
async def projects(context: ToolContext, project: str | None = None) -> ToolResult:
    return await _call(context, "projects", project=project)


@search_hint(
    "hand work to a project orchestrator dispatch job follow-up correction pass on hand over "
    "поручить поручи передать передай проекту отдать отдай оркестратору диспатч поручение дослать дошли делегировать делегируй"
)
@tool(
    name="Delegate",
    description=(
        "Hand work to a project: its orchestrator is woken with it at once and a dispatch id comes back. Write text as a "
        "full hand-over — what the operator wants, in their words where they matter, and what finished looks like. "
        "title is a few words for the card. dispatch_id instead adds a follow-up to a dispatch already open (a correction, "
        "a detail, what a blocked one was waiting for) rather than a second dispatch for the same job. A project whose "
        "orchestrator is off is refused, unless the operator asked for it to be switched on (enable_orchestrator=true). "
        "files: handles (att:…) of the operator's files this work needs; the project gets them under the same handles."
    ),
)
async def delegate(context: ToolContext, project: str, text: str, title: str = "", dispatch_id: str | None = None, enable_orchestrator: bool = False, files: list[str] | None = None) -> ToolResult:
    return await _call(context, "delegate", project=project, text=text, title=title, dispatch_id=dispatch_id, enable_orchestrator=enable_orchestrator, files=files)


@search_hint(
    "operator attachments uploaded files handles att list read uploaded earlier "
    "вложения приложил прикрепленные файлы от оператора аттачи загрузил скинул в чат"
)
@tool(
    name="Files",
    description=(
        "Your files: the operator's attachments in this chat and what projects reported back, each with a handle att:…. "
        "op='list' (default) lists them; op='read' with file (a handle) shows its lines (offset, limit). Pass one to a "
        "project with Delegate(files=[…]) or CreateProject(files=[…])."
    ),
)
async def files(context: ToolContext, op: str = "list", file: str = "", offset: int = 1, limit: int = 200) -> ToolResult:
    return await _call(context, "files", op=op, file=file, offset=offset, limit=limit)


@search_hint(
    "dispatch progress status of handed over work where does it stand "
    "статус диспатча диспатчи как там работа проекта прогресс поручения что с поручением"
)
@tool(
    name="Progress",
    description=(
        "Where handed-over work stands. dispatch_id: its status, the reports on it, what waits for the operator and the "
        "project's work now. project: every open dispatch there. No argument: every open dispatch everywhere. You are "
        "told when a dispatch reports; call this when the operator asks, not to check."
    ),
)
async def progress(context: ToolContext, dispatch_id: str | None = None, project: str | None = None) -> ToolResult:
    return await _call(context, "progress", dispatch_id=dispatch_id, project=project)


@search_hint(
    "cancel dispatch stop handed over work call off abort "
    "отменить отмени диспатч поручение отозвать работу проекта прервать прерви"
)
@tool(
    name="Cancel",
    description="Stop a dispatch: its project's orchestrator is told to stop that work, and the dispatch closes as cancelled. Only when the operator said so.",
)
async def cancel(context: ToolContext, dispatch_id: str, reason: str = "") -> ToolResult:
    return await _call(context, "cancel", dispatch_id=dispatch_id, reason=reason)


class CreateProject(Tool):
    """Written out rather than decorated: ``folders`` is a list of objects whose shape the model has to be shown."""

    search_hint: ClassVar[str] = (
        "create new project folders goal start orchestrator "
        "создать создай новый проект завести заведи проект папки проекта цель"
    )

    @property
    def name(self) -> str:
        return "CreateProject"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Create a project, after the operator confirms it on a card in this chat — nothing is made before, so a "
                "misheard name never becomes a folder. folders: [{path, env: container or host, readonly?, label?}] — a "
                "container folder must lie under the folders allowed for new projects; a host folder is checked on the "
                "operator's machine. Without folders the project gets a new folder of the installation's own. "
                "create_missing=true makes folders that do not exist yet. goal: what the project is for, in the operator's "
                "words. After the confirmation its orchestrator is switched on (start_orchestrator) and handed dispatch #1: "
                "survey the folders and write the brief. It returns at once; the answer arrives as an event."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "name": {"type": "string", "description": "The project's name."},
                    "folders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "env": {"type": "string", "enum": ["container", "host"]},
                                "readonly": {"type": "boolean"},
                                "label": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                        "description": "Its folders, the first the primary.",
                    },
                    "goal": {"type": "string", "description": "What it is for."},
                    "create_missing": {"type": "boolean", "description": "Make folders that do not exist yet."},
                    "start_orchestrator": {"type": "boolean", "description": "Switch its orchestrator on and have it write the brief (default true)."},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "Handles (att:…) of the operator's files the new project starts with; its orchestrator gets them with dispatch #1."},
                },
                required=["name"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        folders = arguments.get("folders") or []
        if not isinstance(folders, list):
            return error(context, "folders is a list of {path, env, readonly?, label?}")
        return await _call(
            context,
            "create_project",
            name=str(arguments.get("name") or ""),
            folders=[f for f in folders if isinstance(f, dict)] if all(isinstance(f, dict) for f in folders) else [{"path": ""}],
            goal=str(arguments.get("goal") or ""),
            create_missing=bool(arguments.get("create_missing")),
            start_orchestrator=bool(arguments.get("start_orchestrator", True)),
            files=[str(f) for f in arguments.get("files") or [] if isinstance(f, str)],
        )


@search_hint(
    "pass on operator answer question card reply relay answer waiting question "
    "передать ответ ответить ответь на вопрос карточка вопроса оператор ответил переслать перешли ответ проекту"
)
@tool(
    name="Answer",
    description=(
        "Pass on the operator's answer to a question shown as a card in this chat, by its id ([q…]). Only when their latest "
        "message answers it: quote is the exact words of that message that carry the answer, text what to send (default "
        "the quote). Never answer on your own judgement, never a permission, a folder or a new project (those are "
        "pressed on the card). When several questions wait and it is unclear which one they meant, ask them which."
    ),
)
async def answer(context: ToolContext, ask_id: str, quote: str, text: str = "") -> ToolResult:
    return await _call(context, "answer", ask_id=ask_id, quote=quote, text=text)


def build() -> list[Tool]:
    """The main orchestrator's own tools, guarded like every discovered tool."""
    return [_guarded(t() if isinstance(t, type) else t) for t in (projects, delegate, progress, cancel, CreateProject, answer, files)]


__all__ = ["build"]
