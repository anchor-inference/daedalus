"""The shipped extension is available through the actual project-agent tool surface."""

from __future__ import annotations

from pathlib import Path

from protocore.contracts.types import MessageRole

from daedalus.config import Settings
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.plugins import PROJECT_STATUS_MANIFEST, PluginRegistry, ProjectStatusAdapter, validate_manifest
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import _idle, rig
from tests.unit.test_staff_runtime import close_team


async def test_project_agent_invokes_installed_read_extension_in_a_real_turn(
        settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [
        {"tool": "ProjectExtension", "args": {"plugin_id": "project_status", "tool_name": "inspect_project",
                                                "arguments": {"project_id": "foreign"}}},
        {"tool": "ProjectExtension", "args": {"plugin_id": "project_status", "tool_name": "inspect_project"}},
        {"text": "The project status was checked."},
    ])
    try:
        dispatcher = EffectDispatcher(OutboxStore(db))
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)}, dispatcher)
        dispatcher.register("plugin.register", registry)
        r.team.app.extensions["plugin_registry"] = registry
        revision = await ControlStore(db).revision(Scope("global", "global"), Entity("collection", "global"))
        await registry.install_command(
            Principal.operator({"via": "token", "user_id": 1}), PROJECT_STATUS_MANIFEST,
            validate_manifest(PROJECT_STATUS_MANIFEST)["digest"],
            expected_collection_revision=revision, client_operation_id="install-for-agent-tool",
        )
        assert await dispatcher.step()
        session_id = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await r.manager.submit(session_id, "Check the installed project extension")
        await until_await(lambda: _idle(r.manager, session_id), "the extension tool turn ended")
        transcript = await r.manager.sessions.list_transcript(session_id)
        outputs = [str(block.content) for message in transcript if message.role is MessageRole.tool
                   for block in message.content_blocks if hasattr(block, "content")]
        assert any("another project" in output for output in outputs)
        assert any(r.project.id in output and "tasks" in output for output in outputs)
        assert any(tool.name == "ProjectExtension" for tool in r.manager.tools.list_all())
    finally:
        await close_team(r.manager)
        await r.manager.close()
