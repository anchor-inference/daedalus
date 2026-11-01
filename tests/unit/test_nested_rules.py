"""A folder's own AGENTS.md reaches the agent at the head of the first file tool result in a run that
touches that folder, never through the system prompt, and only from inside the session's working folder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import MessageRole, ToolResultBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.host import nested_rules
from daedalus.host.filesystem import ShellFS
from daedalus.host.services import SessionServices, locator
from daedalus.stores.database import Database
from daedalus.tools.files import edit_file, find_files, read_file, search_files
from tests.unit.test_bench import LocalShellBackend
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

APP_RULES = "Use the i18n dictionary for every string.\nRun npm test before you finish."


def _ctx(session_id: str) -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": "c1"})


def _tree(root: Path) -> None:
    (root / "AGENTS.md").write_text("root rules, read at the start of every run\n")
    (root / "app" / "src" / "deep").mkdir(parents=True)
    (root / "app" / "AGENTS.md").write_text(APP_RULES + "\n")
    (root / "app" / "src" / "main.ts").write_text("export const a = 1;\n")
    (root / "app" / "src" / "deep" / "x.ts").write_text("export const x = 2;\n")
    (root / "app" / "src" / "deep" / "CLAUDE.md").write_text("Deep folder: keep x pure.\n")
    (root / "app" / "src" / "deep" / "AGENTS.md").write_text("Deep folder: keep x pure.\n")
    (root / "app" / "node_modules" / "lib").mkdir(parents=True)
    (root / "app" / "node_modules" / "lib" / "AGENTS.md").write_text("a dependency's rules\n")
    (root / "app" / "node_modules" / "lib" / "index.js").write_text("module.exports = 1;\n")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "AGENTS.md").write_text("hidden rules\n")
    (root / ".hidden" / "f.txt").write_text("f\n")


async def test_a_folder_s_rules_head_the_first_result_that_reaches_it_once_a_run(tmp_path: Path) -> None:
    _tree(tmp_path)
    services = SessionServices(session_id="rules", workspace_dir=tmp_path)
    locator.register(services)
    try:
        first = await read_file().invoke(_ctx("rules"), {"path": "app/src/main.ts"})
        assert first.content.startswith("[rules from app/AGENTS.md, for the work under app/")
        assert APP_RULES in first.content and "     1\texport const a = 1;" in first.content
        assert "root rules" not in first.content, "the root's file travels in the run's opening message"
        again = await read_file().invoke(_ctx("rules"), {"path": "app/src/main.ts"})
        assert again.content.startswith("     1\t"), "shown once a run"
        deep = await edit_file().invoke(_ctx("rules"), {"path": "app/src/deep/x.ts", "old_string": "2", "new_string": "3"})
        assert deep.content.count("Deep folder: keep x pure.") == 1, "CLAUDE.md that says the same is not shown twice"
        assert "app/AGENTS.md" not in deep.content and "edited " in deep.content
        nested_rules.forget(services)
        later = await read_file().invoke(_ctx("rules"), {"path": str(tmp_path / "app" / "src" / "main.ts")})
        assert APP_RULES in later.content, "a new run shows them again, by an absolute path as well"
    finally:
        locator.unregister("rules")


async def test_dependencies_hidden_folders_and_paths_outside_the_workspace_bring_no_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    _tree(workspace)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "AGENTS.md").write_text("rules of a folder that is not the session's\n")
    (tmp_path / "elsewhere" / "notes.txt").write_text("notes\n")
    locator.register(SessionServices(session_id="walls", workspace_dir=workspace))
    try:
        dependency = await read_file().invoke(_ctx("walls"), {"path": "app/node_modules/lib/index.js"})
        assert "a dependency's rules" not in dependency.content and APP_RULES in dependency.content
        hidden = await read_file().invoke(_ctx("walls"), {"path": ".hidden/f.txt"})
        assert "hidden rules" not in hidden.content
        outside = await read_file().invoke(_ctx("walls"), {"path": str(tmp_path / "elsewhere" / "notes.txt")})
        assert not outside.is_error and outside.content.startswith("     1\tnotes")
    finally:
        locator.unregister("walls")


async def test_find_and_search_bring_the_rules_of_the_folder_they_look_in(tmp_path: Path) -> None:
    _tree(tmp_path)
    locator.register(SessionServices(session_id="look", workspace_dir=tmp_path))
    try:
        found = await find_files().invoke(_ctx("look"), {"pattern": "*.ts", "path": "app/src"})
        assert APP_RULES in found.content and "main.ts" in found.content
        searched = await search_files().invoke(_ctx("look"), {"pattern": "export", "path": "app/src/deep"})
        assert "Deep folder: keep x pure." in searched.content and APP_RULES not in searched.content
        whole = await find_files().invoke(_ctx("look"), {"pattern": "*.md"})
        assert not whole.content.startswith("[rules"), "the workspace root brings nothing"
    finally:
        locator.unregister("look")


async def test_the_setting_turns_it_off(tmp_path: Path) -> None:
    _tree(tmp_path)
    config = RuntimeConfig()
    config.tools.nested_rules = False
    locator.register(SessionServices(session_id="off", workspace_dir=tmp_path, extra={"manager": SimpleNamespace(config=config)}))
    try:
        result = await read_file().invoke(_ctx("off"), {"path": "app/src/main.ts"})
        assert APP_RULES not in result.content
    finally:
        locator.unregister("off")


async def test_rules_are_found_through_a_shell_on_another_machine(tmp_path: Path) -> None:
    _tree(tmp_path)
    backend = LocalShellBackend(tmp_path)
    locator.register(SessionServices(session_id="remote", workspace_dir=tmp_path, exec_backend=backend))
    try:
        result = await read_file().invoke(_ctx("remote"), {"path": "app/src/deep/x.ts"})
        assert isinstance(locator.get("remote").fs, ShellFS)
        assert APP_RULES in result.content and "Deep folder: keep x pure." in result.content
        assert sum("for f in" in command for command in backend.commands) == 1, "one command asks after every folder"
    finally:
        locator.unregister("remote")


def test_the_core_s_own_discovery_stays_off() -> None:
    from daedalus.host.engine_factory import runtime_constants

    rc = runtime_constants(RuntimeConfig(), context_window=128_000, max_output_tokens=32_000, thinking=True)
    assert rc.rules_discovery_enabled is False


async def test_a_run_reads_the_rules_without_moving_the_system_prompt(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([
        {"tool": "Read", "args": {"path": "app/src/main.ts"}},
        {"tool": "Read", "args": {"path": "app/src/deep/x.ts"}},
        {"text": "read both"},
        {"tool": "Read", "args": {"path": "app/src/main.ts"}},
        {"text": "read it again"},
    ])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session("rules")
        _tree(state.workspace)
        for ask in ("look at the app", "and once more"):
            waiter = asyncio.create_task(_wait_finished(manager))
            await manager.submit(state.session.id, ask)
            assert (await waiter)[0][2] == "completed"

        def results(request) -> list[str]:  # type: ignore[no-untyped-def]
            return [block.content if isinstance(block.content, str) else str(block.content)
                    for message in request.messages if message.role is MessageRole.tool
                    for block in message.content_blocks if isinstance(block, ToolResultBlock)]

        def system(request) -> str:  # type: ignore[no-untyped-def]
            return "\n".join(str(message.content_blocks) for message in request.messages if message.role is MessageRole.system)

        first_run = provider.requests[:3]
        assert system(first_run[0]), "the request carries its system prompt as a message"
        assert len({system(request) for request in first_run}) == 1, "the system prompt never moved inside the run"
        assert all(APP_RULES not in system(request) for request in provider.requests)
        shown = results(first_run[-1])
        assert APP_RULES in shown[0] and APP_RULES not in shown[1] and "Deep folder" in shown[1]
        assert APP_RULES in results(provider.requests[-1])[-1], "the next run shows them again on first touch"
    finally:
        await manager.close()
