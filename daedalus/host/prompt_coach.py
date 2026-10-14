"""The prompt coach: a small agent beside a chat that helps the operator write a better prompt.

A coach is an ordinary session with a flag (:data:`COACH_KEY`, the id of the chat it serves) that
changes four things about it. It reads where its chat works and writes nowhere (its walls are the
chat's, read-only); it may call only :data:`daedalus.config.PROMPT_COACH_TOOLS`; it is told who it is by
a role prompt of its own rather than the working agent's; and no list shows it — it lives in a window
over its chat, and a row in the sidebar would be a second way into a conversation that only makes
sense next to the one it serves. Its one way to leave something behind is :func:`save_prompt`, which
writes the finished prompt into the chat's workspace, never over an existing file.

One coach is kept per chat. Opening the window resumes it while it is unfinished; a new one (the
operator's "Start over", or a new prompt after a saved one) replaces it, and the chat's deletion takes
it along. The saved file is the result worth keeping, so the conversation that led to it is not.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.host.btw import BTW_KEY
from daedalus.host.filesystem import LocalFS
from daedalus.host.services import PathOutsideProject

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager, SessionState

COACH_KEY = "prompt_coach_of"
"""Session metadata: the id of the chat this session coaches a prompt for."""

RESULT_KEY = "prompt_coach_result"
"""Session metadata: the last prompt the coach saved — ``{title, prompt, path, at}``. Present means finished."""

PROMPTS_DIR = "prompts"
"""Where in the chat's workspace the prompts are saved, beside the work they are for."""

PROMPT_MAX_CHARS = 60_000
"""A prompt longer than this is not a prompt any more but a document; the coach is told to shorten it."""

TITLE = "Prompt coach"

SLUG_MAX_CHARS = 60

INHERITED_KEYS = ("worktree_cwd", "home")
"""What decides where the chat works besides its folder and its directory (passed to ``create_session``
by name): a staff member's worktree and a coordinator's home. Copied so the coach reads the same files."""


class CoachRefused(ValueError):
    """What the operator or the coach asked for cannot be done; the message says why, in words for either."""


def coach_of(metadata: dict[str, Any]) -> str:
    """The chat this session coaches a prompt for, or ``""`` when it is not a coach."""
    return str(metadata.get(COACH_KEY) or "")


def prompt_slug(title: str) -> str:
    """The file-name part of a prompt's title: lower-case words joined by hyphens, letters of any script.

    Cyrillic stays Cyrillic — the operator names prompts in their own language, and a transliteration
    would be a name they did not write. Anything that is not a letter, a digit or a hyphen goes, so no
    title can name a directory, climb out of one or hide a file behind a leading dot.
    """
    words = re.findall(r"[^\W_]+", title.lower())
    slug = "-".join(words)[:SLUG_MAX_CHARS].strip("-")
    return slug or "prompt"


def candidate_names(day: str, slug: str, *, limit: int = 1000) -> list[str]:
    """The names a prompt saved on ``day`` may take, in order: the plain one, then ``-2``, ``-3`` …"""
    return [f"{day}-{slug}.md", *(f"{day}-{slug}-{n}.md" for n in range(2, limit + 1))]


def result_of(metadata: dict[str, Any]) -> dict[str, Any] | None:
    found = metadata.get(RESULT_KEY)
    return dict(found) if isinstance(found, dict) and found.get("path") else None


def view(state: SessionState) -> dict[str, Any]:
    """The coach as its window draws it: which session to embed, and what it saved, if anything."""
    result = result_of(state.session.metadata)
    return {"session_id": state.session.id, "parent_id": coach_of(state.session.metadata), "finished": result is not None, "result": result}


async def coaches_of(manager: SessionManager, parent_id: str) -> list[str]:
    """The coach sessions of a chat, oldest first; normally one, never more than a moment's race."""
    rows = await manager.db.fetchall(
        "SELECT id FROM sessions WHERE json_valid(metadata) AND json_extract(metadata, '$.prompt_coach_of') = ? ORDER BY created_at, id",
        (parent_id,),
    )
    return [str(row["id"]) for row in rows]


async def latest(manager: SessionManager, parent_id: str) -> dict[str, Any] | None:
    """The chat's coach as its window draws it, or ``None`` when it has none."""
    for coach_id in reversed(await coaches_of(manager, parent_id)):
        state = await manager.get_state(coach_id)
        if state is not None:
            return view(state)
    return None


async def open_coach(manager: SessionManager, parent_id: str, message: str, *, fresh: bool = False) -> dict[str, Any]:
    """The chat's unfinished coach, or a new one started with ``message`` (the operator's draft, framed).

    A new coach replaces the chat's earlier ones. It works where the chat works, on the chat's model,
    and its first turn is ``message`` as the operator's own words; ``KeyError`` for a chat that is not
    there, :class:`CoachRefused` for one that cannot have a coach.
    """
    parent = await manager.get_state(parent_id)
    if parent is None:
        raise KeyError(parent_id)
    if coach_of(parent.session.metadata):
        raise CoachRefused("a prompt coach has no coach of its own")
    if parent.project is None:
        raise CoachRefused("this session has no project to read")
    existing = await coaches_of(manager, parent_id)
    if existing and not fresh:
        current = await manager.get_state(existing[-1])
        if current is not None and result_of(current.session.metadata) is None:
            return {**view(current), "created": False}
    text = message.strip()
    if not text:
        raise CoachRefused("a new prompt coach starts from a message")
    for coach_id in existing:
        # Its own workspace is the chat's: deleting a coach must never take the chat's directory along.
        await manager.delete_session(coach_id, delete_workspace=False)
    source = parent.session.metadata
    metadata: dict[str, Any] = {
        COACH_KEY: parent_id,
        # Made from the app, not in Telegram: without this, private-chat mode would deliver the coach's
        # questions and answers into the operator's direct messages beside the chat they belong to.
        "telegram_detached": True,
        **{key: source[key] for key in INHERITED_KEYS if source.get(key)},
    }
    directory = str(source.get("directory") or "").strip() or None
    folder_id = str(source.get("folder_id") or "").strip() or None
    state = await manager.create_session(f"{TITLE} · {parent.session.title}"[:200], metadata=metadata, project_id=parent.project.id, folder_id=folder_id, directory=directory)
    coach_id = state.session.id
    await inherit_model(manager, parent_id, coach_id)
    await manager.submit(coach_id, text)
    return {**view(state), "created": True}


async def inherit_model(manager: SessionManager, parent_id: str, child_id: str) -> None:
    """Put a window's session on the model its chat answers on, which the operator chose for this work;
    nothing chosen is the default."""
    overrides = await manager.live.load(parent_id)
    if overrides.get("preset") or (overrides.get("provider") and overrides.get("model_name")):
        await manager.live.set_model(
            child_id,
            model_name=overrides.get("model_name"),
            provider=overrides.get("provider"),
            preset=overrides.get("preset"),
            thinking_enabled=overrides.get("thinking_enabled"),
            reasoning_effort=overrides.get("reasoning_effort"),
        )


async def save_prompt(manager: SessionManager, coach_id: str, title: str, prompt: str) -> dict[str, Any]:
    """Write a coach's finished prompt into its chat's workspace and remember it as the coach's result.

    The file is ``prompts/<day>-<slug>.md`` and is never written over: a name that is taken moves on to
    the next suffix, and locally the file is created exclusively, so two saves in the same moment take
    two names. The path is judged by the chat's own walls, not the coach's, which write nothing — so a
    chat whose folder is read-only refuses the save as it would refuse its own Write.
    """
    coach = await manager.get_state(coach_id)
    parent_id = coach_of(coach.session.metadata) if coach is not None else ""
    if coach is None or not parent_id:
        raise CoachRefused("SavePrompt saves a prompt coach's result, and this session is not a prompt coach")
    parent = await manager.get_state(parent_id)
    if parent is None or parent.services is None:
        raise CoachRefused("the session this prompt is for is gone; give the prompt in your answer instead")
    text = prompt.strip()
    if not text:
        raise CoachRefused("the prompt is empty")
    if len(text) > PROMPT_MAX_CHARS:
        raise CoachRefused(f"the prompt is {len(text)} characters; a prompt longer than {PROMPT_MAX_CHARS} is a document, shorten it")
    clean_title = " ".join(title.split())[:200] or "Prompt"
    services = parent.services
    fs = services.fs
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    body = text + "\n"
    for name in candidate_names(day, prompt_slug(clean_title)):
        relative = f"{PROMPTS_DIR}/{name}"
        try:
            target = services.resolve(relative, write=True)
        except PathOutsideProject as exc:
            raise CoachRefused(f"the prompt cannot be saved in that session's folder ({exc}); give it in your answer instead") from None
        if services.is_protected(target):
            raise CoachRefused(f"{target} is protected and cannot be written")
        if await _create_new(fs, target, body):
            break
    else:
        raise CoachRefused(f"every name for {clean_title!r} today is taken; choose another title")
    result = {"title": clean_title, "prompt": text, "path": relative, "at": datetime.now(UTC).isoformat()}
    for metadata in (coach.metadata, coach.session.metadata):
        metadata[RESULT_KEY] = dict(result)
    await manager.sessions.update_metadata(coach_id, coach.session.metadata)
    return result


async def _create_new(fs: Any, target: Path, body: str) -> bool:
    """Write ``body`` to ``target`` only if nothing is there; whether it was written.

    Locally an exclusive create, so the check and the write are one step. Through the host's shell the
    check comes first: the host offers no exclusive create, and two coaches of one chat saving in the
    same second is a race the one-coach rule already rules out."""
    if isinstance(fs, LocalFS):
        def create() -> bool:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return False
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(body)
            return True

        return await asyncio.to_thread(create)
    if await fs.exists(target):
        return False
    await fs.write_text(target, body)
    return True


def hidden(metadata: Any) -> bool:
    """Whether no list shows this session: a coach, or a side question, lives in its chat's window. Takes
    stored JSON text too."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except ValueError:
            return False
    return isinstance(metadata, dict) and bool(metadata.get(COACH_KEY) or metadata.get(BTW_KEY))


__all__ = [
    "COACH_KEY",
    "PROMPTS_DIR",
    "PROMPT_MAX_CHARS",
    "RESULT_KEY",
    "CoachRefused",
    "candidate_names",
    "coach_of",
    "coaches_of",
    "hidden",
    "inherit_model",
    "latest",
    "open_coach",
    "prompt_slug",
    "result_of",
    "save_prompt",
    "view",
]
