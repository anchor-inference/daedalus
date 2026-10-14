"""A side question (``/btw``): a quick question about a chat, answered beside it while its work goes on.

A side question is an ordinary session with a flag (:data:`BTW_KEY`, the id of the chat it is about),
built on the prompt coach's machinery (:mod:`daedalus.host.prompt_coach`): it reads where its chat works
and writes nowhere, no list shows it, it runs on the chat's model, and it goes when its chat goes. What
it adds is the chat's conversation. Its working history starts as a copy of the chat's, each message
marked lent (:data:`daedalus.stores.sqlite.LENT_KEY`) so that its transcript — what its window draws —
holds only the questions and their answers. Its tools are :data:`daedalus.config.BTW_TOOLS`, which read
and change nothing.

Nothing flows back. The chat's run is neither waited for nor touched: the copy is taken from the chat's
history as it stands, mid-run included, and the chat's history is not written. One side question is kept
per chat; a new one replaces it, and closing the window deletes it, because the answer was the point and
the exchange that led to it is not worth keeping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from protocore.contracts.types import Message, MessageRole, ToolResultBlock, ToolUseBlock

from daedalus.host.engine_factory import TENANT
from daedalus.stores.sqlite import LENT_KEY

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager

BTW_KEY = "btw_of"
"""Session metadata: the id of the chat this session answers a side question about."""

TITLE = "Side question"

QUESTION_MAX_CHARS = 20_000
"""A question longer than this is a task, and a task belongs in the chat itself."""


class BtwRefused(ValueError):
    """The side question cannot be asked; the message says why, in words for the operator."""


def btw_of(metadata: dict[str, Any]) -> str:
    """The chat this session answers a side question about, or ``""`` when it is not a side question."""
    return str(metadata.get(BTW_KEY) or "")


async def btws_of(manager: SessionManager, parent_id: str) -> list[str]:
    """The side-question sessions of a chat, oldest first; normally one, never more than a moment's race."""
    rows = await manager.db.fetchall(
        "SELECT id FROM sessions WHERE json_valid(metadata) AND json_extract(metadata, '$.btw_of') = ? ORDER BY created_at, id",
        (parent_id,),
    )
    return [str(row["id"]) for row in rows]


def lent_history(history: list[Message], parent_id: str) -> list[Message]:
    """The chat's working history as a side question reads it: copies marked lent, ending where it can be answered.

    Copies, because the chat's live engine holds the originals and a mark written into one of them would
    reach the chat's own stored history. A copy taken mid-run can end on a tool call whose result has not
    come back, or on some of a batch's results; the provider refuses a call nobody answered, so the
    unfinished step is left out and the side question sees the run up to its last whole step.
    """
    lent = [
        m.model_copy(update={"metadata": {**{k: v for k, v in m.metadata.items() if k != "daedalus.seq"}, LENT_KEY: parent_id}})
        for m in history
    ]
    while lent:
        last = lent[-1]
        if last.role is MessageRole.assistant and any(isinstance(b, ToolUseBlock) for b in last.content_blocks):
            lent.pop()
            continue
        if last.role is MessageRole.tool:
            start = len(lent)
            while start > 0 and lent[start - 1].role is MessageRole.tool:
                start -= 1
            answered = {b.tool_call_id for m in lent[start:] for b in m.content_blocks if isinstance(b, ToolResultBlock)}
            asked = {b.tool_call_id for b in lent[start - 1].content_blocks if isinstance(b, ToolUseBlock)} if start > 0 else set()
            if asked - answered:
                del lent[start - 1 :]
                continue
        break
    return lent


async def open_btw(manager: SessionManager, parent_id: str, question: str) -> dict[str, Any]:
    """A new side question about ``parent_id``, replacing the chat's earlier one, started with ``question``.

    ``KeyError`` for a chat that is not there, :class:`BtwRefused` for one that cannot be asked about.
    """
    from daedalus.host import prompt_coach  # Lazy: it imports this module to know a side question when it sees one

    parent = await manager.get_state(parent_id)
    if parent is None:
        raise KeyError(parent_id)
    if btw_of(parent.session.metadata) or prompt_coach.coach_of(parent.session.metadata):
        raise BtwRefused("a side question is asked about a chat, not about a window beside one")
    if parent.project is None:
        raise BtwRefused("this session has no project to read")
    text = question.strip()
    if not text:
        raise BtwRefused("a side question needs a question")
    if len(text) > QUESTION_MAX_CHARS:
        raise BtwRefused(f"the question is {len(text)} characters; one longer than {QUESTION_MAX_CHARS} is a task for the chat itself")
    # Taken before anything awaits that could let the chat's run move on: the copy is the conversation as
    # it stood when the operator asked. A live engine's history is ahead of the stored one by the round
    # being written.
    history = list(parent.engine.history) if parent.engine is not None else list(await manager.sessions.list_messages(parent_id, TENANT, limit=10_000))
    lent = lent_history(history, parent_id)
    await close_btw(manager, parent_id)
    source = parent.session.metadata
    metadata: dict[str, Any] = {
        BTW_KEY: parent_id,
        # Made from the app: private-chat mode would otherwise deliver the answer into the operator's
        # direct messages, beside the chat it is about.
        "telegram_detached": True,
        **{key: source[key] for key in prompt_coach.INHERITED_KEYS if source.get(key)},
    }
    directory = str(source.get("directory") or "").strip() or None
    folder_id = str(source.get("folder_id") or "").strip() or None
    state = await manager.create_session(f"{TITLE} · {parent.session.title}"[:200], metadata=metadata, project_id=parent.project.id, folder_id=folder_id, directory=directory)
    btw_id = state.session.id
    await manager.sessions.replace_messages(btw_id, TENANT, lent)
    # What the store holds now, so the first round's hand-over adds the question and the answer to it
    # instead of writing the lent history out again as if it were new.
    state.history_keys = [manager.sessions.transcript_key(m) for m in lent]
    await prompt_coach.inherit_model(manager, parent_id, btw_id)
    await manager.submit(btw_id, text)
    return {"session_id": btw_id, "parent_id": parent_id, "lent": len(lent)}


async def close_btw(manager: SessionManager, parent_id: str) -> int:
    """Delete the chat's side questions, a run in one included; how many there were."""
    found = await btws_of(manager, parent_id)
    for btw_id in found:
        # Its directory is the chat's: deleting a side question must never take the chat's files along.
        await manager.delete_session(btw_id, delete_workspace=False)
    return len(found)


__all__ = ["BTW_KEY", "QUESTION_MAX_CHARS", "BtwRefused", "btw_of", "btws_of", "close_btw", "lent_history", "open_btw"]
