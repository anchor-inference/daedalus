"""The session's and its project's names: told in a user turn, never written into the system prompt.

A rename that changed the system prompt changed bytes in the middle of the prefix every later request
is cached on, and the provider wrote the whole history again (about 126k tokens after one restart).
So the names ride in the turn context of a run's opening message: on the first turn, on the first
turn after either name changed, and again once a compaction took the turn that told them away.
"""

from __future__ import annotations

from pathlib import Path

from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.host import prompts
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


async def _turn(manager: SessionManager, sid: str, words: str) -> Message:
    """Open a turn the way a run does and keep it in the working history, as the run would."""
    state = await manager.get_state(sid)
    assert state is not None
    opened = await manager._with_turn_context(state, Message(role=MessageRole.user, content_blocks=[TextBlock(text=words)]))
    history = list(await manager.sessions.list_messages(sid, TENANT, limit=10_000))
    await manager.sessions.replace_messages(sid, TENANT, [*history, opened, Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")])])
    return opened


def _prompt(engine: object) -> bytes:
    return "\n".join(engine.config.system_prompt_sections).encode()  # type: ignore[attr-defined]


async def test_the_system_prompt_names_neither_and_survives_a_rename_byte_for_byte(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        project = await manager.projects.create("Bakery site", [str(tmp_path / "bakery")])
        state = await manager.create_session("Menu page", project_id=project.id)
        sid = state.session.id
        before = _prompt(await manager._build_engine(state, "run-1"))
        assert b"Menu page" not in before and b"Bakery site" not in before
        await manager.rename_session(sid, "Prices page")
        await manager.projects.update(project.id, name="Patisserie")
        state = await manager.get_state(sid)
        assert state is not None
        after = _prompt(await manager._build_engine(state, "run-2"))
        assert after == before
        assert b"Prices page" not in after and b"Patisserie" not in after
    finally:
        await manager.close()


async def test_the_names_are_told_once_and_again_only_when_they_change(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        project = await manager.projects.create("Bakery site", [str(tmp_path / "bakery")])
        state = await manager.create_session("Menu page", project_id=project.id)
        sid = state.session.id
        first = await _turn(manager, sid, "write the menu")
        assert prompts.SESSION_TITLE_LINE + "Menu page" in _text(first) and prompts.PROJECT_TITLE_LINE + "Bakery site" in _text(first)
        # The names are the runtime's, in the turn context; the operator's words are untouched.
        assert prompts.without_turn_context(_text(first)) == "write the menu"
        again = await _turn(manager, sid, "and the drinks")
        assert prompts.SESSION_TITLE_LINE not in _text(again) and prompts.PROJECT_TITLE_LINE not in _text(again)

        await manager.rename_session(sid, "Menu and drinks")
        renamed = await _turn(manager, sid, "carry on")
        assert prompts.SESSION_TITLE_LINE + "Menu and drinks" in _text(renamed)
        assert prompts.SESSION_TITLE_LINE not in _text(await _turn(manager, sid, "more"))

        # A project renamed while the session stays loaded is told too: the store is read, not the state's copy.
        await manager.projects.update(project.id, name="Patisserie")
        moved = await _turn(manager, sid, "next")
        assert prompts.PROJECT_TITLE_LINE + "Patisserie" in _text(moved)
        assert prompts.PROJECT_TITLE_LINE not in _text(await _turn(manager, sid, "after"))
    finally:
        await manager.close()


async def test_the_names_are_told_again_after_a_compaction_took_them_away(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("Menu page")
        sid = state.session.id
        await _turn(manager, sid, "first")
        # What a host compaction leaves: a summary (written without the turn context) and the kept tail.
        summary = Message(role=MessageRole.user, content_blocks=[TextBlock(text="<compacted-turn id='auto'>the work so far</compacted-turn>")], metadata={COMPACTION_SUMMARY_METADATA_KEY: True})
        await manager.sessions.replace_messages(sid, TENANT, [summary, Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")])])
        retold = await _turn(manager, sid, "go on")
        assert prompts.SESSION_TITLE_LINE + "Menu page" in _text(retold)
        assert prompts.SESSION_TITLE_LINE not in _text(await _turn(manager, sid, "and on"))
        # A kept tail that still holds the telling needs no second one.
        history = list(await manager.sessions.list_messages(sid, TENANT, limit=10_000))
        await manager.sessions.replace_messages(sid, TENANT, [summary, *history[-4:]])
        assert prompts.SESSION_TITLE_LINE not in _text(await _turn(manager, sid, "once more"))
    finally:
        await manager.close()


def test_a_name_is_one_line_and_what_was_told_reads_back() -> None:
    note = prompts.titles_note("Menu\n page", "  Bakery\tsite ")
    assert note == prompts.SESSION_TITLE_LINE + "Menu page\n" + prompts.PROJECT_TITLE_LINE + "Bakery site"
    assert prompts.told_titles(note) == ("Menu page", "Bakery site")
    # A telling with no project line told that there is none.
    assert prompts.told_titles(prompts.titles_note("Menu", "")) == ("Menu", "")
    assert prompts.told_titles("- something else") is None and prompts.titles_note("", "") == ""
