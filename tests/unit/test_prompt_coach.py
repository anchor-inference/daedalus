"""The prompt coach: what it may call, where it may read and write, where its prompt lands, and that no
list shows it while its chat's window keeps finding it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import PROMPT_COACH_ONLY_TOOLS, PROMPT_COACH_TOOLS, Settings
from daedalus.extensions import prompt_coach as coach_extension
from daedalus.extensions.api import build_app
from daedalus.host import prompt_coach, prompts
from daedalus.host.prompt_coach import (
    COACH_KEY,
    RESULT_KEY,
    CoachRefused,
    candidate_names,
    open_coach,
    prompt_slug,
    save_prompt,
)
from daedalus.host.services import PathOutsideProject
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config

H = {"X-Daedalus-Token": "tok"}


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.submitted = []  # type: ignore[attr-defined]

    async def fake_submit(session_id: str, text: str, attachments: Any = (), **_: Any) -> str:
        manager.submitted.append((session_id, text))  # type: ignore[attr-defined]
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    yield manager
    await manager.close()


async def _chat(manager: SessionManager, *, own_directory: bool = True) -> Any:
    return await manager.create_session("Bakery site", own_directory=own_directory)


# -- what it may call ------------------------------------------------------------------------------


async def test_the_coach_may_ask_read_and_save_and_nothing_else(manager: SessionManager) -> None:
    chat = await _chat(manager)
    opened = await open_coach(manager, chat.session.id, "Help me improve this prompt:\n\nfix the menu")
    coach = await manager.get_state(opened["session_id"])
    assert coach is not None and manager.is_prompt_coach(coach)
    known = {t.name for t in manager.tools.list_all()}
    blocked = manager.blocked_tools_for(coach)
    assert blocked == known - set(PROMPT_COACH_TOOLS)
    for name in ("Write", "Edit", "MultiEdit", "Exec", "WebFetch", "WebSearch", "SubAgent", "SpawnAgent", "ToolSearch", "Skill", "Remember", "Notify"):
        assert name in blocked, name
    for name in PROMPT_COACH_TOOLS:
        assert name not in blocked, name
    # Saving into another session's workspace is the coach's alone.
    assert set(PROMPT_COACH_ONLY_TOOLS) <= manager.blocked_tools_for(chat)


async def test_the_coach_is_told_who_it_is_and_answers_nobody_in_the_lists(manager: SessionManager) -> None:
    chat = await _chat(manager)
    coach = await manager.get_state((await open_coach(manager, chat.session.id, "draft"))["session_id"])
    sections = prompts.coach_sections(answer_language="", workspace=str(coach.workspace))
    assert str(coach.workspace) in sections[0] and "SavePrompt" in sections[0] and "AskUser" in sections[0]
    assert "Daedalus, a personal autonomous agent" not in "".join(sections), "not the working agent's persona"
    # Its runs are not the operator's conversation: no unread dot, no "finished" notice for a hidden session.
    coach.run_origin = "operator"
    assert not manager._operator_facing(coach)
    assert manager.tool_group_loads_for(coach) and set(manager.tool_group_loads_for(coach).values()) == {"eager"}


# -- where it reads --------------------------------------------------------------------------------


async def test_the_coach_reads_its_chats_own_directory_and_writes_nowhere(manager: SessionManager) -> None:
    chat = await _chat(manager)
    (chat.workspace / "notes.md").write_text("the menu lives in menu.html\n")
    coach = await manager.get_state((await open_coach(manager, chat.session.id, "draft"))["session_id"])
    assert coach.workspace == chat.workspace
    assert coach.metadata["directory"] == chat.metadata["directory"]
    walls = coach.services.walls
    assert walls is not None and walls.writable == ()
    # The chat's directory, and the scratch the host keeps a read-only session's command logs in.
    assert [Path(p) for p in walls.readable][0] == chat.workspace and len(walls.readable) == 2
    assert coach.services.resolve("notes.md") == (chat.workspace / "notes.md").resolve()
    with pytest.raises(PathOutsideProject):
        coach.services.resolve("notes.md", write=True)
    with pytest.raises(PathOutsideProject):
        coach.services.resolve("../../elsewhere.txt")
    # A coach never shares its chat's directory with the delete: removing the coach leaves the chat's files.
    await manager.delete_session(coach.session.id)
    assert (chat.workspace / "notes.md").exists()


# -- where the prompt lands ------------------------------------------------------------------------


def test_a_title_becomes_a_safe_file_name() -> None:
    assert prompt_slug("Fix the bakery menu!") == "fix-the-bakery-menu"
    assert prompt_slug("../../etc/passwd") == "etc-passwd"
    assert prompt_slug(".hidden") == "hidden"
    assert prompt_slug("Промпт для меню") == "промпт-для-меню"
    assert prompt_slug("") == prompt_slug("/// ...") == "prompt"
    assert "/" not in prompt_slug("a/b\\c") and ".." not in prompt_slug("a..b")
    assert len(prompt_slug("word " * 100)) <= 60
    assert candidate_names("2026-10-14", "menu", limit=3) == ["2026-10-14-menu.md", "2026-10-14-menu-2.md", "2026-10-14-menu-3.md"]


async def test_the_prompt_lands_in_the_chats_own_directory_and_never_over_a_file(manager: SessionManager) -> None:
    chat = await _chat(manager)
    assert chat.metadata["directory"] == f".agents/{chat.session.id}"
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    first = await save_prompt(manager, coach_id, "Menu page", "Rebuild the menu page.\n")
    assert first["path"].startswith("prompts/") and first["path"].endswith("-menu-page.md")
    target = chat.workspace / first["path"]
    assert target.read_text() == "Rebuild the menu page.\n"
    assert target.parent == chat.workspace / "prompts", "inside the chat's own directory, not the project folder"
    # The same title again takes the next name and leaves the first file as it was.
    second = await save_prompt(manager, coach_id, "Menu page", "Rebuild the menu page, with prices.")
    assert second["path"] == first["path"].replace(".md", "-2.md")
    assert target.read_text() == "Rebuild the menu page.\n"
    # A title that tries to climb out stays a file name in prompts/.
    third = await save_prompt(manager, coach_id, "../../../outside", "x")
    assert (chat.workspace / third["path"]).parent == chat.workspace / "prompts"
    # The coach remembers the last one, which is what its window offers.
    coach = await manager.get_state(coach_id)
    assert coach.session.metadata[RESULT_KEY]["path"] == third["path"]
    assert (await prompt_coach.latest(manager, chat.session.id))["finished"] is True


async def test_a_file_already_there_is_never_written_over(manager: SessionManager) -> None:
    chat = await _chat(manager, own_directory=False)
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    taken = await save_prompt(manager, coach_id, "Plan", "one")
    (chat.workspace / taken["path"].replace(".md", "-2.md")).write_text("the operator's own file")
    again = await save_prompt(manager, coach_id, "Plan", "two")
    assert again["path"].endswith("-plan-3.md")
    assert (chat.workspace / taken["path"].replace(".md", "-2.md")).read_text() == "the operator's own file"


async def test_only_a_coach_saves_and_only_a_real_prompt(manager: SessionManager) -> None:
    chat = await _chat(manager)
    with pytest.raises(CoachRefused):
        await save_prompt(manager, chat.session.id, "Plan", "text")
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    with pytest.raises(CoachRefused):
        await save_prompt(manager, coach_id, "Plan", "   ")
    with pytest.raises(CoachRefused):
        await save_prompt(manager, coach_id, "Plan", "x" * (prompt_coach.PROMPT_MAX_CHARS + 1))


# -- hidden, and found again -----------------------------------------------------------------------


async def test_no_list_shows_the_coach_and_it_does_not_make_a_chat_a_project(manager: SessionManager) -> None:
    chat = await _chat(manager)
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    assert [row["id"] for row in await manager.list_sessions()] == [chat.session.id]
    assert [row["id"] for row in await manager.session_catalog()] == [chat.session.id]
    assert await manager.list_sessions(ids=[coach_id]) == []
    project = await manager.projects.get(chat.project.id)
    assert project is not None and project.settings.ephemeral, "a chat with a coach is still a chat"
    assert (await manager.projects.summary())[chat.project.id]["total"] == 1
    assert [u["id"] for u in await manager.workspace_users(chat.workspace)] == [chat.session.id]


async def test_the_open_coach_is_resumed_and_a_finished_one_replaced(manager: SessionManager) -> None:
    chat = await _chat(manager)
    first = await open_coach(manager, chat.session.id, "Help me improve this prompt:\n\nfix the menu")
    assert first["created"] is True and manager.submitted == [(first["session_id"], "Help me improve this prompt:\n\nfix the menu")]  # type: ignore[attr-defined]
    again = await open_coach(manager, chat.session.id, "another draft")
    assert again["session_id"] == first["session_id"] and again["created"] is False
    assert len(manager.submitted) == 1, "resuming does not send the draft again"  # type: ignore[attr-defined]
    # Start over replaces it, and only one coach is kept.
    fresh = await open_coach(manager, chat.session.id, "draft", fresh=True)
    assert fresh["session_id"] != first["session_id"]
    assert await prompt_coach.coaches_of(manager, chat.session.id) == [fresh["session_id"]]
    assert await manager.get_state(first["session_id"]) is None
    # A finished coach is not resumed: the next prompt starts a new one.
    await save_prompt(manager, fresh["session_id"], "Menu", "Rebuild the menu.")
    after = await open_coach(manager, chat.session.id, "a new draft")
    assert after["created"] is True and after["session_id"] != fresh["session_id"]
    with pytest.raises(CoachRefused):
        await open_coach(manager, chat.session.id, "", fresh=True)
    with pytest.raises(CoachRefused):
        await open_coach(manager, after["session_id"], "a coach of a coach")
    with pytest.raises(KeyError):
        await open_coach(manager, "nobody", "draft")


async def test_the_coach_answers_on_the_chats_model(manager: SessionManager) -> None:
    chat = await _chat(manager)
    preset = next(iter(manager.config.presets))
    await manager.set_model(chat.session.id, preset=preset)
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    assert (await manager.live.load(coach_id)).get("preset") == preset


async def test_the_coach_goes_with_its_chat_and_with_its_chats_move(manager: SessionManager, settings: Settings, db: Database) -> None:
    await coach_extension.install(SimpleNamespace(manager=manager))  # type: ignore[arg-type]
    chat = await _chat(manager)
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    other = await manager.create_session("Elsewhere")
    await manager.attach_project(chat.session.id, other.project)
    assert await manager.get_state(coach_id) is None, "a coach reading the chat's old place is gone"
    coach_id = (await open_coach(manager, chat.session.id, "draft"))["session_id"]
    await manager.delete_session(chat.session.id)
    assert await manager.get_state(coach_id) is None
    assert await db.fetchone("SELECT 1 FROM sessions WHERE json_extract(metadata, '$.prompt_coach_of') = ?", (chat.session.id,)) is None


# -- the routes ------------------------------------------------------------------------------------


async def test_the_window_opens_and_reads_the_coach_over_the_api(manager: SessionManager, settings: Settings, db: Database) -> None:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    chat = await _chat(manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.get(f"/api/sessions/{chat.session.id}/coach", headers=H)).json() == {"coach": None}
        opened = await client.post(f"/api/sessions/{chat.session.id}/coach", json={"message": "Help me write a prompt."}, headers=H)
        assert opened.status_code == 200, opened.text
        coach = opened.json()["coach"]
        assert coach["parent_id"] == chat.session.id and coach["finished"] is False and coach["created"] is True
        state = await manager.get_state(coach["session_id"])
        assert state.session.metadata[COACH_KEY] == chat.session.id and state.session.metadata["telegram_detached"] is True
        await save_prompt(manager, coach["session_id"], "Plan", "Write the plan.")
        read = (await client.get(f"/api/sessions/{chat.session.id}/coach", headers=H)).json()["coach"]
        assert read["finished"] is True and read["result"]["prompt"] == "Write the plan." and read["result"]["path"].startswith("prompts/")
        detail = (await client.get(f"/api/sessions/{coach['session_id']}", headers=H)).json()
        assert detail["prompt_coach_of"] == chat.session.id
        assert (await client.post(f"/api/sessions/{coach['session_id']}/coach", json={"message": "x"}, headers=H)).status_code == 400
        assert (await client.post("/api/sessions/nobody/coach", json={"message": "x"}, headers=H)).status_code == 404
        assert (await client.post(f"/api/sessions/{chat.session.id}/coach", json={"message": "x", "extra": 1}, headers=H)).status_code == 422
        listed = (await client.get("/api/sessions", headers=H)).json()
        assert [row["id"] for row in listed["sessions"]] == [chat.session.id]
