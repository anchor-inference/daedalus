"""The agent's side of the Notes tab: read and write the notes of its own chat and of the chat's project.

Notes are the operator's document beside the conversation, never part of the prompt: the agent reads
them when the operator points at them or when a note would answer the question, through these tools.
Each write is marked as the agent's and announced, so the panel the operator has open shows it at once
with an "agent" mark. Edits carry the version the agent last read, as the editor's do: a note the
operator is typing in is refused rather than overwritten, and the agent reads it again.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.stores.notes import (
    FILE_MAX_BYTES,
    Conflict,
    NoteError,
    NoteStore,
    Scope,
    reference,
    scopes_for_session,
    store_for,
)
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, refuse_protected, services_for

EXCERPT = 160
STALE = "the note changed since you read it (the operator may be editing it); NoteRead it again and redo the edit on the new text"


def _manager(context: ToolContext) -> Any:
    return services_for(context).extra["manager"]


def _store(context: ToolContext) -> NoteStore:
    return store_for(_manager(context))


async def _scopes(context: ToolContext) -> tuple[Scope, Scope | None]:
    chat, project, _ = await scopes_for_session(_manager(context).db, context.session_id)
    return chat, project


async def _scope(context: ToolContext, kind: str) -> Scope:
    chat, project = await _scopes(context)
    if kind == "chat":
        return chat
    if kind == "project":
        if project is None:
            raise NoteError("this chat is in no project, so it has only chat notes")
        return project
    raise NoteError("scope is chat or project")


async def _visible(context: ToolContext, note_id: str) -> dict[str, Any]:
    """A note of this chat or of its project; any other is reported as missing, not as forbidden."""
    note = await _store(context).get(note_id)
    chat, project = await _scopes(context)
    if Scope(note["scope"], note["scope_id"]) not in (chat, project):
        raise NoteError(f"no note {note_id} in this chat or its project")
    return note


def _excerpt(body: str) -> str:
    text = " ".join(body.split())
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…"


def _line(note: dict[str, Any]) -> str:
    flags = " · pinned" if note["pinned"] else ""
    return f"- {note['id']} · {note['scope']} · v{note['version']} · {note['title'] or '(untitled)'} · edited {note['updated_at'][:16]} by {note['updated_by']}{flags}\n  {_excerpt(note['body'])}"


@tool_group("notes")
@search_hint("notes notebook memo jot write down read list заметки заметка записать запиши записку блокнот прочитай мои заметки проекта чата")
@tool(
    name="NotesList",
    description=(
        "List the operator's notes beside this chat: scope chat, project or all (both). Each line gives the note id, "
        "version, title, who edited it last and a short excerpt; query keeps notes holding every word. Also lists the "
        "files the notes can show. Read one with NoteRead."
    ),
)
async def notes_list(context: ToolContext, scope: str = "all", query: str = "") -> ToolResult:
    try:
        chat, project = await _scopes(context)
        wanted = [s for s in (chat, project) if s is not None and scope in ("all", s.kind)]
        if scope not in ("all", "chat", "project"):
            raise NoteError("scope is chat, project or all")
        if scope == "project" and project is None:
            raise NoteError("this chat is in no project, so it has only chat notes")
        store = _store(context)
        notes = await store.search(wanted, query, limit=100) if query.strip() else [n for s in wanted for n in await store.notes(s)]
        files = [f for s in wanted for f in await store.files(s)]
    except NoteError as exc:
        return error(context, str(exc))
    parts = [f"{len(notes)} note(s)" + (f" matching {query!r}" if query.strip() else "") + ":"]
    parts += [_line(note) for note in notes] or ["(none)"]
    if files:
        parts.append(f"{len(files)} file(s) notes can show (reference them with the Markdown given):")
        parts += [f"- {f['id']} · {f['scope']} · {f['name']} · {f['mime']} · {f['size']} bytes · {reference(f)}" for f in files[:100]]
    return ok(context, "\n".join(parts), count=len(notes))


@tool_group("notes")
@search_hint("read open show note text contents прочитать открыть показать заметку текст содержимое записки глянуть")
@tool(
    name="NoteRead",
    description="Read one note of this chat or its project in full: its title, Markdown body and the version an edit must pass back.",
)
async def note_read(context: ToolContext, note_id: str) -> ToolResult:
    try:
        note = await _visible(context, note_id)
    except NoteError as exc:
        return error(context, str(exc))
    head = f"note {note['id']} · {note['scope']} · version {note['version']} · edited {note['updated_at'][:16]} by {note['updated_by']}"
    return ok(context, f"{head}\n# {note['title'] or '(untitled)'}\n\n{note['body']}", note_id=note["id"], version=note["version"])


@tool_group("notes")
@search_hint("write create edit update append note save jot написать создать изменить дописать обновить сохранить заметку записать в заметки")
@tool(
    name="NoteWrite",
    description=(
        "Create or change a note the operator sees in the chat's Notes tab. Without note_id it creates one in scope chat "
        "or project, with title and body (Markdown: headings, lists, - [ ] tasks, tables, code, mermaid). With note_id and "
        "the version from NoteRead it changes it: body replaces the whole text, append adds to the end, find+replace "
        "changes one exact passage, title renames. A file is shown with the Markdown NotesList or NoteAttach gives."
    ),
)
async def note_write(
    context: ToolContext,
    note_id: str | None = None,
    version: int | None = None,
    scope: str = "chat",
    title: str | None = None,
    body: str | None = None,
    append: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    pinned: bool | None = None,
) -> ToolResult:
    store = _store(context)
    try:
        if not note_id:
            if append is not None or find is not None:
                raise NoteError("a new note takes title and body; append and find+replace change an existing note")
            target = await _scope(context, scope)
            made = await store.create(target, title=title or "", body=body or "", pinned=bool(pinned), by="agent")
            return ok(context, f"created note {made['id']} in the {target.kind} notes, version {made['version']}", note_id=made["id"], version=made["version"])
        current = await _visible(context, note_id)
        if version is None:
            raise NoteError("pass the version NoteRead gave you")
        if version != current["version"]:
            return error(context, f"{STALE}; it is at version {current['version']}")
        text = current["body"]
        if body is not None:
            text = body
        if find is not None:
            if replace is None:
                raise NoteError("find needs replace (an empty string removes the passage)")
            count = text.count(find)
            if count != 1:
                raise NoteError(f"find must match exactly one passage of the note; it matches {count}")
            text = text.replace(find, replace, 1)
        if append is not None:
            text = (text.rstrip("\n") + "\n\n" + append.lstrip("\n")) if text.strip() else append
        saved = current
        if title is not None or text != current["body"]:
            saved = await store.update(note_id, version=version, title=title, body=text, by="agent")
        if pinned is not None and pinned != current["pinned"]:
            saved = await store.set_meta(note_id, pinned=pinned, by="agent")
    except Conflict:
        return error(context, STALE)
    except NoteError as exc:
        return error(context, str(exc))
    return ok(context, f"saved note {saved['id']}, version {saved['version']}", note_id=saved["id"], version=saved["version"])


@tool_group("notes")
@search_hint("delete remove discard note trash удалить удали убрать стереть снести заметку выбросить записку")
@tool(name="NoteDelete", description="Delete a note of this chat or its project. The operator can still bring it back from the app for a day.")
async def note_delete(context: ToolContext, note_id: str) -> ToolResult:
    try:
        await _visible(context, note_id)
        gone = await _store(context).delete(note_id, by="agent")
    except NoteError as exc:
        return error(context, str(exc))
    return ok(context, f"deleted note {gone['id']} ({gone['title'] or 'untitled'})", note_id=gone["id"])


@tool_group("notes")
@search_hint("attach file image picture chart screenshot to note прикрепить файл картинку изображение график скриншот к заметке вложить")
@tool(
    name="NoteAttach",
    description=(
        "Add a file from the workspace (a chart, a screenshot, a PDF) to the chat's or the project's note files and get "
        "the Markdown that shows it in a note: a picture for an image, a link for anything else. Put that Markdown into "
        "a note with NoteWrite."
    ),
)
async def note_attach(context: ToolContext, path: str, scope: str = "chat") -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if refusal := refuse_protected(context, services, target, "attached"):
        return refusal
    try:
        where = await _scope(context, scope)
        if not await services.fs.is_file(target):
            return error(context, f"no such file: {target}")
        data = await services.fs.read_bytes(target, limit=FILE_MAX_BYTES + 1)
        made = await _store(context).add_file(where, data, name=target.name, by="agent")
    except NoteError as exc:
        return error(context, str(exc))
    except (OSError, ValueError) as exc:
        return error(context, str(exc))
    return ok(context, f"added {made['name']} to the {where.kind} note files; show it with: {reference(made)}", file_id=made["id"])


TOOLS = [notes_list, note_read, note_write, note_delete, note_attach]

__all__ = ["TOOLS"]
