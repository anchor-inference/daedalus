"""Board tools: the plan as durable, queryable state instead of a paragraph in the context."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import call_id, error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("board") if manager is not None else None


def _command_hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("board_commands") if manager is not None else None


@tool_group("board")
@search_hint(
    "add todo item to my board backlog ticket checklist step plan card new entry "
    "добавить добавь завести заведи закинуть закинь кинь таска таску тудушка тикет доска доску пункт план чеклист"
)
@tool(
    name="BoardAdd",
    description=(
        "Add a task to your board (this session's, shared with its subagents). Use it when work has more "
        "than a few steps or must survive compaction and restarts: title, acceptance criteria (how anyone can tell it "
        "is done), an optional checklist, dependencies (task ids that must finish first — the task "
        "stays 'blocked' until they do), priority 1 (highest) to 5. Pass the collection revision "
        "shown by BoardList; stale writes are refused. Returns the task id."
    ),
)
async def board_add(
    context: ToolContext, title: str, expected_collection_revision: int,
    acceptance: str = "", checklist: list[str] | None = None,
    depends_on: list[str] | None = None, priority: int = 3, notes: str = "",
) -> ToolResult:
    hook = _command_hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        task = await hook("add", title=title, acceptance=acceptance, checklist=checklist,
                          depends_on=depends_on, priority=priority, session_id=context.session_id,
                          notes=notes, client_operation_id=call_id(context),
                          expected_collection_revision=expected_collection_revision)
    except (ValueError, PermissionError) as exc:
        return error(context, str(exc))
    return ok(context, f"task {task['id']} added ({task['status']}): {task['title']}", task_id=task["id"])


@tool_group("board")
@search_hint(
    "move mark status done doing blocked progress note tick checklist item close reopen "
    "обновить обнови отметить отметь пометить пометь закрыть закрой статус готово сделано чеклист галочка продвинуть"
)
@tool(
    name="BoardUpdate",
    description=(
        "Annotate a board task or move it to todo, blocked or dropped. Pass the exact entity revision "
        "shown by BoardGet. Check or uncheck stable criterion IDs (C1, C2, …); unknown IDs are refused. "
        "Execution, review and completion require their dedicated receipt commands."
    ),
)
async def board_update(
    context: ToolContext, task_id: str, expected_entity_revision: int,
    status: str | None = None, note: str = "", check: list[str] | None = None,
    uncheck: list[str] | None = None, priority: int | None = None,
) -> ToolResult:
    hook = _command_hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        task = await hook("update", task_id=task_id, status=status, note=note,
                          check_ids=check, uncheck_ids=uncheck, priority=priority,
                          session_id=context.session_id, client_operation_id=call_id(context),
                          expected_entity_revision=expected_entity_revision)
    except KeyError:
        return error(context, f"no task {task_id}")
    except (ValueError, PermissionError) as exc:
        return error(context, str(exc))
    done = sum(1 for c in task["checklist"] if c["done"])
    return ok(context, f"task {task['id']} is now {task['status']}" + (f", checklist {done}/{len(task['checklist'])}" if task["checklist"] else ""))


@tool_group("board")
@search_hint(
    "my board kanban open todo items backlog overview what is planned "
    "доска доску канбан таски тудушки бэклог список открытые запланировано показать покажи глянуть глянь"
)
@tool(
    name="BoardList",
    description=(
        "Show your board: open tasks by status and priority (include_done=true adds finished ones). It holds the "
        "tasks this session and its subagents created and the ones the operator posted to nobody in particular; "
        "other agents' tasks are not on it. A project's staff and orchestrator see the project's whole board instead. "
        "A task's acceptance, brief, notes and checklist come with BoardGet. The collection revision "
        "is needed for BoardAdd."
    ),
)
async def board_list(context: ToolContext, status: str | None = None, include_done: bool = False) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    listing = await hook("render", status=status, include_done=include_done, actor=context.session_id)
    command = _command_hook(context)
    if command is not None:
        try:
            revision = await command("revision", session_id=context.session_id)
            listing = f"collection revision {revision}\n{listing}"
        except PermissionError:
            pass
    return ok(context, listing)


@tool_group("board")
@search_hint(
    "one card details acceptance criteria checklist dependencies notes who works on it "
    "подробности детали карточка тикета критерии приемки чеклист зависимости заметки кто делает открыть открой"
)
@tool(name="BoardGet", description="Everything about one board task: acceptance criteria, checklist, dependencies, notes, who works on it.")
async def board_get(context: ToolContext, task_id: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "the board is not available")
    try:
        t = await hook("get", task_id=task_id, actor=context.session_id)
    except KeyError:
        return error(context, f"no task {task_id}")
    checklist = "\n".join(f"  [{'x' if c['done'] else ' '}] {c['id']}: {c['text']}"
                          for c in t["checklist"]) or "  (none)"
    brief = "".join(f"{key.replace('_', ' ')}: {value}\n" for key, value in (t.get("brief") or {}).items() if value)
    return ok(
        context,
        f"{t['id']} · {t['status']} · p{t['priority']} · {t['title']}\nentity revision {t['entity_revision']}\n{brief}acceptance: {t['acceptance'] or '(none)'}\nchecklist:\n{checklist}\n"
        f"depends on: {', '.join(t['depends_on']) or 'nothing'}\nsession: {t['session_id'] or '-'}\nnotes:\n{t['notes'] or '(none)'}",
    )


TOOLS = [board_add, board_update, board_list, board_get]

__all__ = ["TOOLS"]
