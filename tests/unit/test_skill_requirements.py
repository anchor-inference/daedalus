"""A skill's ``requires:`` is met when the skill is loaded, and the linter keeps every skill honest about it.

A session told by ``frontend-design`` to look at the page with the browser tools found them held back and
spent its steps searching; told to screenshot with Playwright, it installed a second Playwright into the
workspace past a read-only cache. These tests hold the two ends: the Skill tool loads the groups and tools
a skill names into the run at once and says what is missing, and ``scripts/lint_skills.py`` fails a skill
that names a tool which does not exist, a tool its ``requires:`` does not load, or a program it does not
declare.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from protocore.contracts.types import MessageRole, TextBlock, ToolCall
from protocore.runtime.tool_deferral import calls_held_for_rules

from daedalus.config import Settings
from daedalus.host import prompts, tool_groups, toolchain
from daedalus.host.skills import Requirement, classify_requirement
from daedalus.stores.database import Database
from daedalus.tools import TOOL_GROUPS
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import _idle
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_tool_surface import _flat, _manager

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("lint_skills", ROOT / "scripts" / "lint_skills.py")
assert _spec is not None and _spec.loader is not None
lint_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_skills)


@pytest.fixture(autouse=True)
def _toolchain_cache() -> object:
    """The toolchain caches its answers for the life of the process, which is what a test is not."""
    toolchain._state.clear()
    yield
    toolchain._state.clear()


def test_an_item_of_requires_is_read_by_its_form() -> None:
    assert classify_requirement("ImageView", TOOL_GROUPS) == Requirement("tool", "ImageView")
    assert classify_requirement("browser", TOOL_GROUPS) == Requirement("group", "browser")
    assert classify_requirement("python:PIL", TOOL_GROUPS) == Requirement("module", "python:PIL")
    assert classify_requirement("playwright", TOOL_GROUPS) == Requirement("program", "playwright")
    assert classify_requirement("not a name", TOOL_GROUPS) is None


def test_every_skill_of_the_repository_passes_the_linter() -> None:
    assert lint_skills.lint(ROOT / "skills") == []


def _skill(tmp_path: Path, name: str, front: str, body: str) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: a skill under test\n{front}---\n{body}\n", encoding="utf-8")
    return tmp_path


def test_the_linter_fails_a_tool_that_does_not_exist(tmp_path: Path) -> None:
    root = _skill(tmp_path, "ghost", "", "Take a picture with `Screenshot(url)` and attach it.")
    assert lint_skills.lint(root) == ["ghost: names the tool Screenshot, which does not exist"]
    root = _skill(tmp_path / "b", "ghost-required", "requires: Screenshot\n", "Look at the page.")
    assert lint_skills.lint(root) == ["ghost-required: requires the tool Screenshot, which does not exist"]


def test_the_linter_fails_a_held_back_tool_its_requires_does_not_load(tmp_path: Path) -> None:
    body = "Open the page with `BrowserOpen`, then ask `BrowserLook` what it shows; look at files with `ImageView`."
    assert lint_skills.lint(_skill(tmp_path, "look", "", body)) == [
        "look: names BrowserLook, BrowserOpen without the group 'browser' in requires, so the skill does not load them",
    ]
    assert lint_skills.lint(_skill(tmp_path / "b", "look", "requires: browser\n", body)) == []


def test_the_linter_fails_a_program_not_declared_and_an_install_of_what_the_image_has(tmp_path: Path) -> None:
    body = "```python\nfrom playwright.sync_api import sync_playwright\n```\nFirst run uv add playwright and playwright install chromium."
    assert lint_skills.lint(_skill(tmp_path, "shots", "", body)) == [
        "shots: runs playwright without 'playwright' in requires",
        "shots: says 'uv add playwright'; the image carries Playwright and Chromium for the shell's python3 already",
    ]
    warned = "```python\nfrom playwright.sync_api import sync_playwright\n```\nRun it with python3; never `playwright install` it."
    assert lint_skills.lint(_skill(tmp_path / "b", "shots", "requires: playwright\n", warned)) == []


async def test_frontend_design_loads_the_browser_tools_into_the_run_that_loaded_it(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(toolchain._state, "playwright", "ok")
    provider = ScriptedProvider([
        {"tool": "Skill", "args": {"skill": "frontend-design"}},
        {"text": "loaded"},
    ])
    manager = await _manager(settings, db, browser_container_dir=tmp_path / "browser")
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        state = await manager.create_session("design a site")
        sid = state.session.id
        await manager.submit(sid, "design a site about trigonometry")
        await until_await(lambda: _idle(manager, sid), "the run ended")

        def tools(index: int) -> set[str]:
            return {t.name for t in provider.requests[index].tools or []}

        def system(index: int) -> str:
            return "".join(b.text for m in provider.requests[index].messages if m.role is MessageRole.system for b in m.content_blocks if isinstance(b, TextBlock))

        browser = set(tool_groups.members(manager.tools.list_all())["browser"])
        assert browser and not browser & tools(0), "the browser group is held back until something loads it"
        # The request right after the Skill call offers the group whole, and ImageView, without a search.
        assert browser <= tools(1) and "ImageView" in tools(1)
        # Its rules came with it, so the first call of one of its tools runs rather than being held back.
        assert _flat(prompts.BROWSER) in _flat(system(1))
        assert state.engine is not None
        assert calls_held_for_rules(state.engine, [ToolCall(id="c", name="BrowserLook", arguments={})]) == {}
        result = next(m for m in await manager.sessions.list_transcript(sid) if m.role is MessageRole.tool)
        text = "".join(getattr(b, "content", "") or "" for b in result.content_blocks)
        assert "Loaded for this skill, callable from your next step:" in text and "BrowserLook" in text
        # Recall and Remember steps are gone from the skill: the direction comes from the subject.
        assert "Recall(" not in text and "Remember(" not in text and "design directions used" not in text
        # The group is carried whole into the session's later runs, like a group a search loaded.
        assert "browser" in state.metadata[tool_groups.CARRIED_KEY]
    finally:
        await manager.close()


async def test_a_missing_program_is_said_before_the_skill_and_a_tool_always_offered_loads_nothing(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(toolchain._state, "playwright", "Playwright not installed in this image.")
    provider = ScriptedProvider([
        {"tool": "Skill", "args": {"skill": "web-design-reviewer"}},
        {"text": "noted"},
    ])
    manager = await _manager(settings, db, browser_container_dir=tmp_path / "browser")
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        state = await manager.create_session("review")
        sid = state.session.id
        await manager.submit(sid, "review the page")
        await until_await(lambda: _idle(manager, sid), "the run ended")
        result = next(m for m in await manager.sessions.list_transcript(sid) if m.role is MessageRole.tool)
        text = "".join(getattr(b, "content", "") or "" for b in result.content_blocks)
        head = text.split("# Web Design Reviewer", 1)[0]
        assert "Playwright not installed in this image." in head
        # ImageView is in no group: always offered, nothing to load, nothing said.
        assert "Loaded for this skill" not in head
        assert [t.name for t in provider.requests[0].tools or []] == [t.name for t in provider.requests[1].tools or []]
    finally:
        await manager.close()
