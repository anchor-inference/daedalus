"""A project orchestrator's own tools: the brief, the folders, the journal, the team, the board, a
read-only look into the files, its two ways of speaking to the operator, and the team tools that hire,
assign, talk to, read, answer and control staff.

They exist for orchestrator sessions alone (``ORCHESTRATOR_ONLY_TOOLS``). Each goes through the
orchestrator extension, which first checks that the calling session still holds its project's
office; a replaced orchestrator is refused with the name of its successor.
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolParameterSchema, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint
from daedalus.tools._common import call_id, error, ok, services_for

WATCH_EVENTS = (
    "staff_finished", "staff_question", "staff_permission", "staff_crashed", "staff_silent",
    "task_moved", "terminal_output", "git_commit", "pr", "ci", "webhook",
)
"""The kinds of event a watch waits for, as the extension knows them (a tool may not import it)."""


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("orchestrator") if manager is not None else None


def _command_id(context: ToolContext) -> str:
    call_id = context.metadata.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("the host did not identify this watch command")
    return "watch-tool:" + hashlib.sha256(f"{context.run_id}:{call_id}".encode()).hexdigest()


async def _call(tool_context: ToolContext, operation: str, /, **kwargs: Any) -> ToolResult:
    """Positional-only, because the tools' own arguments include ``op`` and ``context``."""
    hook = _hook(tool_context)
    if hook is None:
        return error(tool_context, "orchestrators are not available on this installation")
    try:
        text = await hook(operation, session_id=tool_context.session_id, **kwargs)
    except (KeyError, ValueError, RuntimeError, PermissionError) as exc:
        return error(tool_context, str(exc))
    return ok(tool_context, str(text))


@search_hint(
    "project brief goals constraints preferences done when allowed without operator scope "
    "бриф проекта цели ограничения предпочтения критерии готовности рамки прочитать прочитай дописать допиши"
)
@tool(
    name="Brief",
    description=(
        "Read or write the project's brief. No arguments: the whole brief. section alone: that section. section and "
        "body: write it (append=true adds to it). Sections: goals, constraints, preferences, done_when, "
        "allowed_without_operator, notes. allowed_without_operator is the operator's alone — you cannot write it; "
        "propose a change with AskOperator."
    ),
)
async def brief(context: ToolContext, section: str | None = None, body: str | None = None, append: bool = False) -> ToolResult:
    return await _call(context, "brief", section=section, body=body, append=append)


@search_hint(
    "project folders directories add folder mount path readonly "
    "папки проекта директории добавить папку подключить папку путь папок каталоги список"
)
@tool(
    name="Folders",
    description=(
        "The project's folders. op='list' (default) shows them with their ids. op='add' with path (label, env, "
        "readonly optional) adds one; a host folder in a container installation goes to the operator for "
        "confirmation and the answer arrives as an event. op='update' with folder (id or label): a new label, or "
        "readonly=true to lock it (only the operator unlocks). op='remove' with folder: refused while anyone works "
        "in it; files are never deleted."
    ),
)
async def folders(
    context: ToolContext,
    op: str = "list",
    path: str | None = None,
    folder: str | None = None,
    label: str | None = None,
    env: str | None = None,
    readonly: bool | None = None,
) -> ToolResult:
    return await _call(context, "folders", op=op, path=path, folder=folder, label=label, env=env, readonly=readonly)


@search_hint(
    "project journal log decisions record why decision diary rationale "
    "журнал проекта решения записать запиши журнал почему решили дневник обоснование"
)
@tool(
    name="Journal",
    description=(
        "The project's journal, which survives compaction and restarts. op='write' (default): text, why (the reason), "
        "kind (decision, plan, answer, reassignment, note …). Record every decision the operator would want to find "
        "later. kind='rule' records a standing instruction of the operator's ('whenever X, do Y'), in their words: "
        "it is shown whole in every turn's state block and given to every member with each task until "
        "op='lift' with rule=<its id> takes it out of force. op='read': newest first, limit entries, before=<id> "
        "for older ones, kind to read only one kind. kind='commitment' records something you took on for the operator "
        "— one per ask when a message asks several things, with task_id when a card carries it: the state block and the "
        "operator's list show it until op='keep' with commitment=<id> (and why), or until its card is accepted."
    ),
)
async def journal(
    context: ToolContext, op: str = "write", text: str = "", why: str = "", kind: str = "", rule: int | None = None, before: int | None = None, limit: int = 20,
    commitment: int | None = None, task_id: str | None = None,
) -> ToolResult:
    return await _call(context, "journal", op=op, text=text, why=why, kind=kind, rule=rule, before=before, limit=limit, commitment=commitment, task_id=task_id)


@search_hint(
    "team members staff roster who is working who is free status of people stuck busy workload "
    "команда сотрудники штат кто работает кто свободен состав команды застрял загружен свободные руки"
)
@tool(
    name="Team",
    description=(
        "The team. No arguments: every member with status, task, what they wait for and last signal. staff (a name or "
        "id): one member in detail — sessions, branch, queued assignments, recent messages. concurrency: how many "
        "staff may work at once, within the project's cap."
    ),
)
async def team(context: ToolContext, staff: str | None = None, concurrency: int | None = None) -> ToolResult:
    return await _call(context, "team", staff=staff, concurrency=concurrency)


@search_hint(
    "project board tasks of the team create assign update tasks for staff project backlog import github issues "
    "задачи проекта доска проекта таски команды задачу создать создай бэклог проекта статус задачи импорт issues гитхаб"
)
@tool(
    name="Tasks",
    description=(
        "The project's board. op='list' (open tasks; status narrows it), 'get' (task_id: brief, branch, notes), "
        "'create' (title and the four brief parts: objective, deliverable, boundaries, done_when; priority 1-5, "
        "depends_on, assignee to queue it), 'update' (task_id and what changes), "
        "'move' (task_id, status: todo|blocked|dropped; with blocked, waiting_on says who or what it "
        "waits for — the operator, someone outside, a date, another task), 'next' (task_id, next_kind: "
        "answer_question|provide_input|review|retry|assign|wait, owner: operator, you, system or a member's name; "
        "next_kind='none' clears it — the card's next step as the operator's attention list shows it, and a "
        "card whose next step is assign to a member is handed to it once its dependencies are ready). Execution, review and completion "
        "use their exact receipt commands. 'list' and 'get' show the collection and entity revisions required "
        "for writes; 'get' also shows the card's "
        "checks (C…), requirements (R…) and acceptance. A new card for work already on the board is refused with "
        "that card's id — hand the card on instead, or new=true with a reason saying how the work differs. "
        "GitHub issues: op='issues' previews the open issues of repository (owner/repo; default: the project "
        "folder's GitHub remote), label narrows them, and says which would become cards or update theirs; "
        "op='import_issues' with issues=[numbers] and the collection revision 'issues' showed imports them as cards "
        "linked to their issue. Use these, not a shell gh call or hand-copied cards."
    ),
)
async def tasks(
    context: ToolContext,
    op: str = "list",
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
    assignee: str | None = None,
    note: str = "",
    waiting_on: str = "",
    new: bool = False,
    reason: str = "",
    next_kind: str | None = None,
    owner: str = "",
    expected_entity_revision: int | None = None,
    expected_collection_revision: int | None = None,
    repository: str = "",
    label: str = "",
    issues: list[int] | None = None,
) -> ToolResult:
    return await _call(
        context, "tasks", op=op, task_id=task_id, title=title, objective=objective, deliverable=deliverable, boundaries=boundaries,
        done_when=done_when, status=status, priority=priority, depends_on=depends_on, assignee=assignee, note=note, waiting_on=waiting_on,
        new=new, reason=reason, next_kind=next_kind, owner=owner, client_operation_id=call_id(context),
        expected_entity_revision=expected_entity_revision,
        expected_collection_revision=expected_collection_revision,
        repository=repository, label=label, issues=issues,
    )


@search_hint(
    "peek into project files git log git diff commits read-only look inside repo "
    "заглянуть загляни файлы проекта коммиты дифф гит лог посмотреть репозиторий"
)
@tool(
    name="Peek",
    description=(
        "Look into the project's files, read-only, when you must check something yourself. op: 'read' (path, offset, "
        "limit lines), 'ls' (path), 'find' (glob pattern), 'search' (regex pattern), 'git_log' (ref, path, limit), "
        "'git_diff' (ref or range such as main..agent/ira/t1, path), 'git_status', 'files' (the project's kept files: "
        "the operator's attachments and what staff reported back, each with a handle att:…), 'dispatch' (ref: a "
        "dispatch's id — its whole text and what was said on it). folder: id or label "
        "(default the primary folder); paths are relative to it. path='att:…' reads a kept file. Output is bounded; "
        "narrow the path to see more."
    ),
)
async def peek(context: ToolContext, op: str, path: str = "", folder: str | None = None, pattern: str = "", ref: str = "", offset: int = 1, limit: int = 200) -> ToolResult:
    return await _call(context, "peek", op=op, path=path, folder=folder, pattern=pattern, ref=ref, offset=offset, limit=limit)


QUESTION_PROPERTIES: dict[str, Any] = {
    "title": {"type": "string", "description": "A few words naming the decision, shown as the question's name in the operator's list."},
    "text": {"type": "string", "description": "The decision in one or two sentences, with what hangs on it. Markdown."},
    "options": {"type": "array", "items": {"type": "string"}, "description": "The choices, if there are some."},
    "multi": {"type": "boolean", "description": "Several options may be chosen together."},
    "context": {"type": "string", "description": "What the operator needs to decide without reading the whole conversation."},
    "task_id": {"type": "string", "description": "The task it is about, if any."},
    "urgent": {"type": "boolean", "description": "Whether work is blocked until it is answered."},
    "dispatch_id": {"type": "string", "description": "The main orchestrator's dispatch this question belongs to, if any."},
}


class AskOperator(Tool):
    """Written out rather than decorated: its questions carry an argument called ``context``, which the
    decorator keeps for the tool context."""

    search_hint: ClassVar[str] = (
        "ask operator decision question for the owner needs approval from the human "
        "спросить спроси оператора решение владельца вопрос оператору уточнить оператора"
    )

    @property
    def name(self) -> str:
        return "AskOperator"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Put decisions only the operator can make to them. Ask everything you need at once: questions=[{title, "
                "text, options?, multi?, urgent?, dispatch_id?, context?}, …] (up to 12), or one question with title and "
                "text. The operator sees them as a list, answers any of them and sends the answers together. It returns "
                "at once with each question's short id; do not wait — the answers arrive as events in a later wake-up, "
                "in one wake-up when they were sent together. A question still waiting is never asked again: "
                "op='update' with its id and the new title, text, options or context changes it in place (an answer the "
                "operator began to write stays), op='withdraw' with ids and reason takes questions back; a new question on "
                "the subject of one still open is refused with that one's id."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "op": {"type": "string", "enum": ["ask", "update", "withdraw"], "description": "ask (the default), update one open question in place, or withdraw some."},
                    "id": {"type": "string", "description": "With op='update': the short id of your open question."},
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "With op='withdraw': the short ids to take back."},
                    "reason": {"type": "string", "description": "With op='withdraw': a few words the operator sees where each question was."},
                    "questions": {
                        "type": "array",
                        "description": "Several questions at once, each with its own title and text.",
                        "items": {"type": "object", "properties": QUESTION_PROPERTIES, "required": ["title", "text"]},
                    },
                    **QUESTION_PROPERTIES,
                },
                required=[],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        questions = arguments.get("questions")
        if questions is not None and not isinstance(questions, list):
            return error(context, "questions is a list of objects, each with a title and a text")
        options = arguments.get("options") or []
        if not isinstance(options, list):
            return error(context, "options is a list of strings")
        ids = arguments.get("ids") or []
        return await _call(
            context,
            "ask_operator",
            op=str(arguments.get("op") or "ask"),
            id=str(arguments["id"]) if arguments.get("id") else None,
            ids=[str(i) for i in ids] if isinstance(ids, list) else None,
            reason=str(arguments.get("reason") or ""),
            questions=questions or None,
            title=str(arguments.get("title") or ""),
            text=str(arguments.get("text") or ""),
            # None when not given, so an update that changes only the words keeps the options.
            options=[str(o) for o in options] if arguments.get("options") is not None else None,
            multi=bool(arguments.get("multi")),
            context=str(arguments.get("context") or ""),
            task_id=str(arguments["task_id"]) if arguments.get("task_id") else None,
            urgent=bool(arguments.get("urgent")),
            dispatch_id=str(arguments["dispatch_id"]) if arguments.get("dispatch_id") else None,
        )


@search_hint(
    "withdraw take back questions no longer needed retract cancel questions "
    "отозвать отзови вопросы снять вопросы забрать заберите вопросы неактуально отменить отмени"
)
@tool(
    name="WithdrawQuestions",
    description=(
        "Take back questions of yours that still wait for the operator, one or many, when the answer no longer "
        "matters — the work changed, you found the answer yourself, a newer question replaces it. ids: their short "
        "ids. reason: a few words the operator sees where each question was. A question already answered is reported "
        "with its answer instead."
    ),
)
async def withdraw_questions(context: ToolContext, ids: list[str], reason: str) -> ToolResult:
    return await _call(context, "withdraw_questions", ids=ids, reason=reason)


@search_hint(
    "report to operator milestone done blocker decision notify phone journal entry outcome "
    "доложить доложи сообщить оператору отчитаться отчитайся готово блокер итог блокировка результат"
)
@tool(
    name="ProjectReport",
    description=(
        "Tell the operator something that matters — a task done, a decision, a blocker — as a notification on their "
        "phone and an entry in the journal. kind: progress, done, blocked or decision. Not a running commentary: "
        "routine progress goes in the journal. dispatch_id: the main orchestrator's dispatch it answers, if any. files: "
        "handles (att:…) of what the operator should get — a staff member's report, a document — shown to them as "
        "downloads and passed to the main orchestrator with the report."
    ),
)
async def project_report(context: ToolContext, text: str, title: str = "", kind: str = "progress", task_id: str | None = None, dispatch_id: str | None = None, files: list[str] | None = None) -> ToolResult:
    return await _call(context, "project_report", text=text, title=title, kind=kind, task_id=task_id, dispatch_id=dispatch_id, files=files)


@search_hint(
    "hire add member onboard new staff developer recruit coder engineer "
    "нанять найми нанимать взять возьми сотрудника разработчика команду новый сотрудник кодера"
)
@tool(
    name="Hire",
    description=(
        "Add a member to the team. name (unique, it names their branch), role (their lasting area of work), harness "
        "(daedalus, or a command-line agent: claude, codex, cursor, grok, opencode, pi — Harnesses shows what is installed and "
        "what each offers), agent (a persona for daedalus, the CLI's agent otherwise), model (a preset for daedalus, "
        "the CLI's model otherwise), effort (Daedalus: off, low, medium, high or xhigh; empty inherits the preset), permission_mode (CLI only), env (container or host), folder (their "
        "default folder), isolation (worktree: their own branch, the default in a git folder; shared; readonly), "
        "instructions (standing guidance), one_off=true for a helper dismissed when their task is done. Hire for a "
        "lasting need; a team of a few well-briefed members beats a crowd."
    ),
)
async def hire(
    context: ToolContext,
    name: str,
    role: str,
    harness: str = "daedalus",
    agent: str = "",
    model: str = "",
    effort: str = "",
    permission_mode: str = "",
    env: str = "",
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str = "",
    one_off: bool = False,
) -> ToolResult:
    return await _call(
        context, "hire", name=name, role=role, harness=harness, agent=agent, model=model, effort=effort, permission_mode=permission_mode,
        env=env, folder=folder, isolation=isolation, instructions=instructions, one_off=one_off,
    )


@search_hint(
    "change member settings role model permissions instructions reconfigure "
    "поменять поменяй модель сотруднику роль права настройки сотрудника сменить смени"
)
@tool(
    name="StaffEdit",
    description=(
        "Change a member (staff: name or id): role, agent, model, effort, permission_mode, env, folder, isolation, "
        "instructions, notes (what they carry between sessions; this replaces it). Name and harness cannot change — "
        "hire someone else for that. It takes effect from their next session."
    ),
)
async def staff_edit(
    context: ToolContext,
    staff: str,
    role: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    env: str | None = None,
    folder: str | None = None,
    isolation: str | None = None,
    instructions: str | None = None,
    notes: str | None = None,
) -> ToolResult:
    return await _call(
        context, "staff_edit", staff=staff, role=role, agent=agent, model=model, effort=effort, permission_mode=permission_mode,
        env=env, folder=folder, isolation=isolation, instructions=instructions, notes=notes,
    )


@search_hint(
    "dismiss remove member from team fire let go roster "
    "уволить уволь убрать убери из команды выгнать выгони исключить состав"
)
@tool(
    name="Dismiss",
    description=(
        "Take a member off the team. Refused while they have a live session unless release=true, which ends it first "
        "(their unfinished task goes back to todo). keep_worktree=false removes a clean worktree; an unmerged branch "
        "is always kept. A member already gone — a one-off helper leaves with its task — is said to be, and a card "
        "they left in doing is freed."
    ),
)
async def dismiss(context: ToolContext, staff: str, release: bool = False, keep_worktree: bool = True) -> ToolResult:
    return await _call(context, "dismiss", staff=staff, release=release, keep_worktree=keep_worktree)


@search_hint(
    "assign hand task to member give work to staff put someone on it "
    "назначить назначь поручить поручи сотруднику дать задачу выдать выдай посадить посади"
)
@tool(
    name="Assign",
    description=(
        "Hand a member a task: task_id of a task on the board, or title plus the brief for a new one. A revision or "
        "the next step of work a member handed in is the same task: pass its task_id (it is reopened, its history "
        "kept) — without staff it goes back to whoever worked it last, and to anyone else only with reason. Without "
        "a task_id the work gets a card of its own; work already on the board (a card open, or finished in the last "
        "hours) is refused with that card's id — hand it on with task_id, or new=true with a reason when it is "
        "separate work after all. A task_id never takes a card someone is still working on to other work. "
        "The brief has four parts, each a real sentence, on the task or given here: objective (what and why), deliverable (what "
        "exists when done), boundaries (where to work, what not to touch), done_when (a check anyone can run; each "
        "line of it becomes a check C1 … the result is accepted against, or give checks=[…]). requirements: the "
        "operator's concrete conditions for this work, in their words (a string each, or {text, kind: quality|scope|"
        "constraint, source}). inputs: files the work starts from (handles), which the member must open before it can "
        "hand the work in. folder, priority (1 first … 5) and depends_on are optional. files: handles (att:…) or "
        "paths in the project's folders; the host copies each where the member can open it before the brief is sent, "
        "and the brief names that copy — never put a path of your own into a brief. It starts now or waits in the "
        "project's durable queue; the answer says which and why. Give the collection revision from Tasks(list) "
        "for a new card or entity revision from Tasks(get) for an existing card. resume_from is an optional "
        "session id from StaffSessions: "
        "it resumes that CLI conversation only in the same launch folder and worktree branch. "
        "effort overrides a Daedalus member's default for this assignment: off, low, medium, high or xhigh. "
        "secrets: names of the operator's secrets this chat may use (router_admin), handed to the member by name; "
        "their launch carries the values. Never write a password or key into a brief or a message."
    ),
)
async def assign(
    context: ToolContext,
    staff: str | None = None,
    task_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    deliverable: str | None = None,
    boundaries: str | None = None,
    done_when: str | None = None,
    folder: str | None = None,
    priority: int | None = None,
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
    new: bool = False,
    requirements: list[Any] | None = None,
    inputs: list[str] | None = None,
    checks: list[str] | None = None,
    reason: str = "",
    resume_from: str | None = None,
    expected_entity_revision: int | None = None,
    expected_collection_revision: int | None = None,
    effort: str | None = None,
    secrets: list[str] | None = None,
) -> ToolResult:
    return await _call(
        context, "assign", staff=staff, task_id=task_id, title=title, objective=objective, deliverable=deliverable, boundaries=boundaries,
        done_when=done_when, folder=folder, priority=priority, depends_on=depends_on, files=files, new=new,
        requirements=requirements, inputs=inputs, checks=checks, reason=reason, resume_from=resume_from,
        client_operation_id=call_id(context), expected_entity_revision=expected_entity_revision,
        expected_collection_revision=expected_collection_revision, effort=effort, secrets=secrets,
    )


@search_hint("resume past staff chats sessions conversation history продолжить возобновить старый чат сессия сотрудник история беседа")
@tool(
    name="StaffSessions",
    description=(
        "List recorded CLI conversations for a member's harness in this project's launch folder, including dismissed "
        "members' sessions. For worktree isolation, pass task_id to identify sessions in that task's exact branch. "
        "Use a listed session id as Assign(resume_from=...). before pages through older sessions."
    ),
)
async def staff_sessions(context: ToolContext, staff: str, task_id: str | None = None, before: str | None = None, limit: int = 20) -> ToolResult:
    return await _call(context, "staff_sessions", staff=staff, task_id=task_id, before=before, limit=limit)


REQUIREMENT_ITEM: dict[str, Any] = {
    "anyOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["quality", "scope", "constraint"]},
                "source": {"type": "string", "description": "operator (their words in this chat, the default), orchestrator, answer:<request id> or rule:<id>."},
                "why": {"type": "string", "description": "Why, for a constraint of yours that narrows what the operator allowed."},
            },
            "required": ["text"],
        },
    ]
}
assign().definition.parameters.properties["requirements"] = {
    "type": "array",
    "description": "The operator's conditions for this work, one each: quality bars, formats, what not to do.",
    "items": REQUIREMENT_ITEM,
}


@search_hint(
    "requirement condition quality bar operator demand add requirement to task input file reference must read "
    "требование условие к задаче планка качества добавить требование референс обязательный файл поправка к задаче"
)
@tool(
    name="Require",
    description=(
        "Put a requirement on a card: a condition all of its work must meet — usually the operator's, stated in the "
        "chat (source='operator'), sometimes yours (source='orchestrator'), or from an answer of theirs "
        "(source='answer:<request id>'). kind: quality, scope, constraint, or input with file=<att:…>: a file the work "
        "starts from, which the member must open before handing in. It is numbered (R1 …) in the next "
        "contract. On a card a member is working, it goes into that run: the member is told in the turn it is in "
        "and keeps working, so do not also Tell them the same thing. restart=true is only for a change the work "
        "cannot absorb: it stops the run, applies the requirement once the run has exited, and you Assign the card "
        "again. A card nobody works gets it applied at once and its next brief carries it. op='stop' or "
        "op='apply' with intent_id finishes a change that could not be applied in its own call. replaces=R2 puts it in "
        "place of an older one; withdraw=R2 takes one out. The operator's own requirement is replaced or withdrawn "
        "only with their answer as source — ask them first. What the operator allows for the work ('if something "
        "needs fixing, fix it') is kind='scope' in their words; a constraint of yours that narrows it takes why, "
        "and the operator is told."
    ),
)
async def require(
    context: ToolContext,
    task_id: str,
    text: str = "",
    kind: str = "quality",
    source: str = "orchestrator",
    replaces: str | None = None,
    withdraw: str | None = None,
    file: str | None = None,
    why: str = "",
    op: str = "stage",
    intent_id: str | None = None,
    client_operation_id: str = "",
    expected_entity_revision: int | None = None,
    restart: bool = False,
) -> ToolResult:
    return await _call(context, "require", task_id=task_id, text=text, kind=kind, source=source,
                       replaces=replaces, withdraw=withdraw, file=file, why=why, op=op,
                       intent_id=intent_id, client_operation_id=client_operation_id or call_id(context),
                       expected_entity_revision=expected_entity_revision, restart=restart)


require().definition.parameters.properties["kind"]["enum"] = ["quality", "scope", "constraint", "input"]


@search_hint(
    "review result check handed in verification evidence return rework verdict exact report "
    "принять результат проверить сданное приемка вернуть на доработку отметить пункты одобрить"
)
@tool(
    name="ReviewResult",
    description=(
        "Inspect immutable reports for a task, or append an independent verdict to an exact result. "
        "op='inspect' returns result ids, current candidate and entity revision. op='verdict' requires "
        "result_id, expected_entity_revision, verification (verified, failed or stale), accepted, "
        "evidence_ids and reason; branch work also needs exact head/base. An accepted verdict means the "
        "reviewer approved that result, not that the operator accepted the task. op='accept' is for work "
        "with no branch — a report, a diagnosis, an answer: with result_id, expected_entity_revision and "
        "reason (what the report showed against the done-when) it accepts that result and finishes the "
        "card. op='return' requires "
        "the exact result_id, verdict_id, contract_revision, expected_entity_revision and reason. "
        "Mutations require a host-issued reviewer grant; tool arguments cannot choose their actor."
    ),
)
async def review_result(
    context: ToolContext,
    task_id: str,
    op: str = "inspect",
    result_id: str | None = None,
    verdict_id: str | None = None,
    expected_entity_revision: int | None = None,
    verification: str = "unverified",
    accepted: bool = False,
    evidence_ids: list[str] | None = None,
    head: str | None = None,
    base: str | None = None,
    environment_digest: str | None = None,
    reason: str = "",
    contract_revision: int | None = None,
) -> ToolResult:
    return await _call(context, "review_result", task_id=task_id, op=op, result_id=result_id,
                       verdict_id=verdict_id, expected_entity_revision=expected_entity_revision,
                       verification=verification, accepted=accepted, evidence_ids=evidence_ids,
                       head=head, base=base, environment_digest=environment_digest, reason=reason,
                       contract_revision=contract_revision, client_operation_id=call_id(context))


review_result().definition.parameters.properties["op"]["enum"] = ["inspect", "verdict", "accept", "return"]
review_result().definition.parameters.properties["verification"]["enum"] = ["unverified", "verified", "failed", "stale"]


@search_hint(
    "decide nothing further close result no next step waiting reason decision "
    "решить закрыть результат ничего дальше без следующего шага причина решение"
)
@tool(
    name="Decide",
    description=(
        "Say that nothing further follows a result or a card, and why: it leaves the state block's list of results "
        "waiting for your decision, and the journal keeps the reason. task_id, or loop (the L… the state block "
        "gives). It does not move the card: a handed-in report is finished with ReviewResult(op='accept'), "
        "a card nobody will do is Tasks(op='move', status='dropped'), and for a card that waits on the operator, "
        "someone outside or a date, Tasks(op='move', status='blocked', waiting_on=…) says so on the board."
    ),
)
async def decide(context: ToolContext, why: str, task_id: str | None = None, loop: str | None = None) -> ToolResult:
    return await _call(context, "decide", why=why, task_id=task_id, loop=loop)


@search_hint(
    "tell member message correction say to staff live session nudge send message teammate colleague "
    "сказать скажи написать напиши сотруднику передать поправку поправить курс отправить отправь сообщение коллеге"
)
@tool(
    name="Tell",
    description=(
        "Say something to a member's live session. when decides when they read it: now (the default) goes into the "
        "turn they are working on — they see it between two steps and can change course, without losing what they "
        "did; use it for a correction, a detail or a new constraint for the work in hand. after_turn waits until "
        "they finish the turn, for what should not disturb it: the next piece of work, a note for later. interrupt "
        "stops the turn first, for when what they are doing is wrong or wasted. Where an executor cannot take a "
        "message into a running turn, the receipt says what happened instead. files: handles (att:…) or paths in the "
        "project's folders, copied where the member can open them; the message names the copies. Returns the "
        "delivery receipt: queued, written, submitted, acknowledged or failed. secrets: names of the operator's "
        "secrets this chat may use, handed to the member by name (never paste a value into text). The host appends "
        "a note saying where the member finds each one, which differs by executor and by whether it was already "
        "running; do not name a variable or a path yourself. Handing a secret again sends its current value."
    ),
)
async def tell(context: ToolContext, staff: str, text: str, when: str = "now", files: list[str] | None = None, secrets: list[str] | None = None) -> ToolResult:
    return await _call(context, "tell", staff=staff, text=text, when=when, files=files, secrets=secrets)


# The decorator describes a str as any string at all; the model is shown the three timings as the only
# ones, so it does not guess a word such as "queue" and have the call refused.
tell().definition.parameters.properties["when"]["enum"] = ["now", "after_turn", "interrupt"]


@search_hint(
    "what did member do last reply turns terminal screen diff reports of staff recent actions activity full report "
    "что сделал сотрудник посмотреть работу ответ сотрудника его изменения дифф экран успел сделать активность"
)
@tool(
    name="ReadStaff",
    description=(
        "Read what a member did, in a bounded page. what: last (their last reply, the default), turns (the last "
        "turns, one line per tool call), screen (a command-line agent's terminal), diff (their changes against the "
        "base), reports (their last reports whole — turns says how many; where a member put its answer when its "
        "last reply only says it answered). cursor from an earlier read shows only what came after it; max_chars "
        "widens the page up to a limit. "
        "Reading a finished turn marks it seen."
    ),
)
async def read_staff(context: ToolContext, staff: str, what: str = "last", turns: int = 1, cursor: str | None = None, max_chars: int | None = None) -> ToolResult:
    return await _call(context, "read_staff", staff=staff, what=what, turns=turns, cursor=cursor, max_chars=max_chars)


@search_hint(
    "answer staff request permission grant deny approve reject question from staff "
    "ответить ответь сотруднику разрешить разреши отклонить отклони одобрить одобри запрос стаффа стейфа"
)
@tool(
    name="Answer",
    description=(
        "Answer a staff request by its id ([q…] in the events and the state block). A question: text, or selected "
        "options. A permission: allow=true or false; a grant needs basis — under normal autonomy the exact line of "
        "the brief's 'allowed without the operator' that covers it, under full your reason. Denying is always "
        "allowed. escalate=true hands it to the operator instead (text becomes your suggestion, basis the reason)."
    ),
)
async def answer(
    context: ToolContext,
    request_id: str,
    allow: bool | None = None,
    text: str | None = None,
    selected: list[str] | None = None,
    basis: str = "",
    escalate: bool = False,
) -> ToolResult:
    return await _call(context, "answer", request_id=request_id, allow=allow, text=text, selected=selected, basis=basis, escalate=escalate)


@search_hint(
    "interrupt stop current turn escape member break in colleague cut short "
    "прервать прерви остановить ход сотрудника перебить перебей эскейп коллегу оборвать оборви"
)
@tool(name="Interrupt", description="Stop a member's current turn (Esc for a command-line agent). The session stays; Tell says what next.")
async def interrupt(context: ToolContext, staff: str) -> ToolResult:
    return await _call(context, "interrupt", staff=staff)


@search_hint(
    "pause member finish turn commit wip hold start nothing new "
    "пауза поставить на паузу стопнуть стопни притормозить притормози сотрудника дописать коммит"
)
@tool(
    name="Pause",
    description="Let a member finish the current turn, commit their work in progress on their branch, and start nothing new until Tell or Assign.",
)
async def pause(context: ToolContext, staff: str) -> ToolResult:
    return await _call(context, "pause", staff=staff)


@search_hint(
    "release end member session free up unassign task back to todo "
    "освободить освободи завершить сессию сотрудника снять с задачи отпустить отпусти"
)
@tool(
    name="Release",
    description=(
        "End a member's live session. Their unfinished task goes back to todo, unassigned; their branch stays. "
        "A member whose session already ended, or who was dismissed, has the cards they still hold in doing freed "
        "the same way. keep_worktree=false also removes a clean worktree. Look (ReadStaff) before releasing someone "
        "who went silent."
    ),
)
async def release(context: ToolContext, staff: str, keep_worktree: bool = True) -> ToolResult:
    return await _call(context, "release", staff=staff, keep_worktree=keep_worktree)


@search_hint(
    "harnesses executors claude codex grok opencode installed signed in cli execution environments capabilities "
    "харнесы исполнители установлены какие cli агенты доступны версия окружения модели возможности"
)
@tool(
    name="Harnesses",
    description=(
        "The executors staff can run on. No arguments: each one per environment — installed, version, signed in, "
        "whether it can run staff here. harness: what it offers (models — the ones the operator chose to offer, when "
        "there is a choice; the others still work when named —, agents, permission modes, efforts); folder "
        "adds the agents that folder defines; env narrows to container or host. Check here before Hire."
    ),
)
async def harnesses(context: ToolContext, harness: str | None = None, env: str | None = None, folder: str | None = None) -> ToolResult:
    return await _call(context, "harnesses", harness=harness, env=env, folder=folder)


@search_hint(
    "wake me alarm timer remind me later in minutes at time cron "
    "разбуди будильник таймер напомни мне через минут позже разбудить поставь будильник"
)
@tool(
    name="WakeMe",
    description=(
        "Save an alarm for yourself. A one-shot within your explicit coordinator wake approval can fire; other "
        "alarms wait for the operator to approve them in the app. Give "
        "exactly one of in_minutes (at least 1), at (ISO 8601; without an offset it is the operator's time) or cron "
        "(minute hour day month weekday, in UTC; at most every 10 minutes by default). The note says what to look at "
        "— write it for yourself without this conversation. The scheduler looks every 30 seconds, so a wake-up can "
        "come up to half a minute late. Unwatch(id) cancels an alarm owned by your current approval; "
        "the state block lists yours."
    ),
)
async def wake_me(context: ToolContext, note: str, at: str | None = None, in_minutes: int | None = None, cron: str | None = None) -> ToolResult:
    return await _call(context, "wake_me", note=note, at=at, in_minutes=in_minutes, cron=cron,
                       client_operation_id=_command_id(context))


class Watch(Tool):
    """Written out rather than decorated: ``when`` and ``then`` are objects whose shape the model has to
    be shown, and the decorator describes a dict as nothing more than an object."""

    search_hint: ClassVar[str] = (
        "watch event staff finishes crashes goes silent task moves react without polling subscribe whenever alert me "
        "следить следи отслеживать когда закончит упадет замолчит событие подписаться при событии как только дай знать"
    )

    @property
    def name(self) -> str:
        return "Watch"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "When something happens in the project, do something — without polling. when.event is one of: "
                "staff_finished, staff_question, staff_permission, staff_crashed (each with an optional staff); "
                "staff_silent (staff, minutes: silent that long while working); task_moved (task, to — both optional); "
                "terminal_output (terminal id or title, or a command-line staff member; regex, at most 200 characters, "
                "no lookarounds); git_commit (folder, branch — optional: a new commit on a branch of that folder); pr "
                "(provider, repo, conclusion such as opened or merged); ci (provider, repo, conclusion such as failure); "
                "webhook (provider, regex over the payload). then.action is wake (you are woken with the note), tell "
                "(staff, text, when: now, after_turn or interrupt, as in Tell) or notify (title, text, level: quiet, normal or urgent). A watch fires at most once "
                "per cooldown, once=true removes it after the first fire, and one that fires twelve times in an hour "
                "switches itself off. Nothing you do yourself fires a watch. Unwatch(id) removes it."
            ),
            parameters=ToolParameterSchema(
                properties={
                    "when": {
                        "type": "object",
                        "description": "What to wait for, e.g. {\"event\": \"staff_finished\", \"staff\": \"Max\"} or {\"event\": \"ci\", \"provider\": \"github\", \"conclusion\": \"failure\"}.",
                        "properties": {
                            "event": {"type": "string", "enum": list(WATCH_EVENTS)},
                            "staff": {"type": "string"}, "minutes": {"type": "integer"}, "task": {"type": "string"}, "to": {"type": "string"},
                            "terminal": {"type": "string"}, "regex": {"type": "string"}, "folder": {"type": "string"}, "branch": {"type": "string"},
                            "provider": {"type": "string"}, "repo": {"type": "string"}, "conclusion": {"type": "string"},
                        },
                        "required": ["event"],
                    },
                    "then": {
                        "type": "object",
                        "description": "What to do, e.g. {\"action\": \"wake\"} or {\"action\": \"tell\", \"staff\": \"Max\", \"text\": \"…\"}.",
                        "properties": {
                            "action": {"type": "string", "enum": ["wake", "tell", "notify"]},
                            "note": {"type": "string"}, "staff": {"type": "string"}, "text": {"type": "string"},
                            "when": {"type": "string", "enum": ["now", "after_turn", "interrupt"]}, "title": {"type": "string"},
                            "level": {"type": "string", "enum": ["quiet", "normal", "urgent"]},
                        },
                        "required": ["action"],
                    },
                    "cooldown_minutes": {"type": "number", "description": "Least time between two fires; at least 1, default 10."},
                    "once": {"type": "boolean", "description": "Remove the watch after it fires once."},
                    "note": {"type": "string", "description": "Why you set it, for you and the operator."},
                    "deadline_at": {"type": "string", "description": "Optional ISO 8601 deadline within one year; the watch stops before a later event."},
                },
                required=["when", "then"],
            ),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            command_id = _command_id(context)
        except ValueError as exc:
            return error(context, str(exc))
        return await _call(
            context,
            "watch",
            when=arguments.get("when"),
            then=arguments.get("then"),
            cooldown_minutes=arguments.get("cooldown_minutes", 10),
            once=bool(arguments.get("once")),
            note=str(arguments.get("note") or ""),
            deadline_at=arguments.get("deadline_at"),
            client_operation_id=command_id,
        )


@search_hint(
    "cancel wake-up remove watch unsubscribe stop watching alarm "
    "отменить отмени будильник снять слежку перестать следить отписаться отпишись убрать"
)
@tool(
    name="Unwatch",
    description="Cancel a wake-up or remove a watch, by the id the state block or WakeMe/Watch gave you.",
)
async def unwatch(context: ToolContext, id: str) -> ToolResult:
    try:
        command_id = _command_id(context)
    except ValueError as exc:
        return error(context, str(exc))
    return await _call(context, "unwatch", id=id, client_operation_id=command_id)


@search_hint(
    "installed project extension plugin inspect status custom read tool "
    "расширение проекта плагин проверить статус прочитать инструмент вызвать"
)
@tool(
    name="ProjectExtension",
    description=(
        "Run an installed project extension's read-only tool. Give its plugin_id and tool_name from the "
        "operator-reviewed extension catalog; arguments follow that tool's schema. The host binds the "
        "current project and refuses inactive, changed, or unapproved capabilities."
    ),
)
async def project_extension(context: ToolContext, plugin_id: str, tool_name: str,
                            arguments: dict[str, Any] | None = None) -> ToolResult:
    return await _call(context, "plugin_read", plugin_id=plugin_id, tool_name=tool_name,
                       arguments=arguments or {})


TOOLS = [
    brief, folders, journal, team, tasks, peek, AskOperator, withdraw_questions, project_report,
    hire, staff_edit, dismiss, assign, staff_sessions, require, review_result, decide, tell, read_staff, answer, interrupt, pause, release, harnesses,
    wake_me, Watch, unwatch, project_extension,
]

__all__ = ["TOOLS"]
