"""A manifest cannot load undeclared authority or silently change its pinned bytes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.extensions import plugins, skill_quality
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.plugins import (
    PROJECT_STATUS_MANIFEST,
    PluginRefused,
    PluginRegistry,
    ProjectStatusAdapter,
    validate_manifest,
)
from daedalus.host.skills import DirectorySkillStore
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


def _manifest() -> dict:
    return {
        "id": "sample", "version": "1.0.0", "display_name": "Sample inspector",
        "description": "Read one sample task by its identifier.", "host_api": "1",
        "capabilities": ["board.read"],
        "tools": [{"name": "inspect", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}],
        "events": [], "dependencies": [],
        "ui_extensions": [{"id": "detail", "slot": "task.detail", "schema_version": 1, "component": "status"}],
    }


def test_manifest_is_typed_and_digest_pinned() -> None:
    manifest = _manifest()
    checked = validate_manifest(manifest)
    assert checked["required_capabilities"] == ["board.read"]
    assert len(checked["digest"]) == 64
    manifest["capabilities"].append("board.write")
    assert validate_manifest(manifest)["digest"] != checked["digest"]


@pytest.mark.parametrize("change", [
    lambda m: m.update({"host_api": "2"}),
    lambda m: m["capabilities"].append("filesystem.write"),
    lambda m: m["tools"][0].update({"input_schema": {"type": "object", "properties": {"x": {"type": "nope"}}}}),
    lambda m: m["ui_extensions"][0].update({"component": "script"}),
    lambda m: m["dependencies"].append({"id": "x", "version": "1", "digest": "unverified"}),
])
def test_bad_manifest_fails_before_registration(change) -> None:
    manifest = _manifest()
    change(manifest)
    with pytest.raises(PluginRefused):
        validate_manifest(manifest)


async def test_shipped_adapter_install_and_invoke_is_scoped(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id, name, created_at) VALUES ('project', 'Project', 'now')")
        dispatcher = EffectDispatcher(OutboxStore(db))
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)}, dispatcher)
        dispatcher.register("plugin.register", registry)
        digest = validate_manifest(PROJECT_STATUS_MANIFEST)["digest"]
        principal = Principal.operator({"via": "token", "user_id": 1})
        revision = (await db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind = 'global' AND scope_id = 'global'"))["revision"]
        with pytest.raises(PluginRefused, match="digest changed"):
            await registry.install_command(
                principal, PROJECT_STATUS_MANIFEST, "0" * 64,
                expected_collection_revision=revision, client_operation_id="wrong-digest",
            )
        installed = await registry.install_command(
            principal, PROJECT_STATUS_MANIFEST, digest,
            expected_collection_revision=revision, client_operation_id="install-status",
        )
        assert installed["status"] == "staged"
        assert await dispatcher.step()
        assert (await registry.invoke("project_status", "inspect_project", {"project_id": "project"}, granted={"board.read"}))["name"] == "Project"
        with pytest.raises(PluginRefused, match="grant"):
            await registry.invoke("project_status", "inspect_project", {"project_id": "project"}, granted=set())
        registry.safe_mode = True
        with pytest.raises(PluginRefused, match="inactive"):
            await registry.invoke("project_status", "inspect_project", {"project_id": "project"}, granted={"board.read"})
    finally:
        await db.close()


async def test_receipted_plugin_install_survives_replay(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        dispatcher = EffectDispatcher(OutboxStore(db))
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)}, dispatcher)
        dispatcher.register("plugin.register", registry)
        digest = validate_manifest(PROJECT_STATUS_MANIFEST)["digest"]
        principal = Principal.operator({"via": "token", "user_id": 1})
        first = await registry.install_command(
            principal, PROJECT_STATUS_MANIFEST, digest,
            expected_collection_revision=1, client_operation_id="plugin-install-one",
        )
        assert first == await registry.install_command(
            principal, PROJECT_STATUS_MANIFEST, digest,
            expected_collection_revision=1, client_operation_id="plugin-install-one",
        )
        assert await dispatcher.step()
        rows = await db.fetchall("SELECT state FROM effect_outbox WHERE kind = 'plugin.register'")
        assert [row["state"] for row in rows] == ["completed"]
        assert (await registry.health("project_status"))["state"] == "configured_unverified"
    finally:
        await db.close()


@pytest.mark.parametrize("change", ["safe_mode", "revoke"])
async def test_plugin_registration_rechecks_lifecycle_after_claim(tmp_path, change: str) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)})
        await registry.install_command(
            Principal.operator({"via": "token", "user_id": 1}), PROJECT_STATUS_MANIFEST,
            validate_manifest(PROJECT_STATUS_MANIFEST)["digest"],
            expected_collection_revision=1, client_operation_id="install-before-policy-change",
        )
        claim = await OutboxStore(db).claim(("plugin.register",))
        assert claim is not None

        async def interleave(_claim) -> None:
            if change == "safe_mode":
                await db.kv_set("plugin_safe_mode", True)
            else:
                await db.execute("UPDATE plugin_manifests SET status = 'revoked' WHERE id = 'project_status'")

        outcome = await registry.run(claim, interleave)
        assert outcome.state == "failed"
        assert registry.active == {}
    finally:
        await db.close()


async def test_plugin_registration_does_not_complete_after_late_revocation(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)})
        await registry.install_command(
            Principal.operator({"via": "token", "user_id": 1}), PROJECT_STATUS_MANIFEST,
            validate_manifest(PROJECT_STATUS_MANIFEST)["digest"],
            expected_collection_revision=1, client_operation_id="install-before-late-revocation",
        )
        claim = await OutboxStore(db).claim(("plugin.register",))
        assert claim is not None
        checks = 0

        async def interleave(_claim) -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                await db.execute("UPDATE plugin_manifests SET status = 'revoked' WHERE id = 'project_status'")

        outcome = await registry.run(claim, interleave)
        assert outcome.state == "unknown"
        assert registry.active == {}
    finally:
        await db.close()


async def test_extension_install_registers_durable_handlers_before_api(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        dispatcher = EffectDispatcher(OutboxStore(db))
        app = SimpleNamespace(
            db=db, extensions={"effects": dispatcher},
            manager=SimpleNamespace(skills=DirectorySkillStore(tmp_path / "skills")),
        )
        assert await plugins.install(app) == []
        assert await skill_quality.install(app) == []
        assert set(dispatcher.handlers) == {"plugin.register", "skill.publish"}
        assert app.extensions["plugin_registry"] is dispatcher.handlers["plugin.register"]
        assert app.extensions["skill_quality"] is dispatcher.handlers["skill.publish"]
    finally:
        await db.close()
