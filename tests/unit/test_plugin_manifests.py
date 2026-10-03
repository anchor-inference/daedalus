"""A manifest cannot load undeclared authority or silently change its pinned bytes."""

from __future__ import annotations

import pytest

from daedalus.extensions.plugins import (
    PROJECT_STATUS_MANIFEST,
    PluginRefused,
    PluginRegistry,
    ProjectStatusAdapter,
    validate_manifest,
)
from daedalus.stores.control import Principal
from daedalus.stores.database import Database


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
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)})
        digest = validate_manifest(PROJECT_STATUS_MANIFEST)["digest"]
        with pytest.raises(PluginRefused, match="digest changed"):
            await registry.install(PROJECT_STATUS_MANIFEST, "0" * 64)
        installed = await registry.install(PROJECT_STATUS_MANIFEST, digest)
        assert installed["status"] == "active"
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
        registry = PluginRegistry(db, {"project_status": lambda: ProjectStatusAdapter(db)})
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
        rows = await db.fetchall("SELECT state FROM effect_outbox WHERE kind = 'plugin.register'")
        assert [row["state"] for row in rows] == ["completed"]
        assert (await registry.health("project_status"))["state"] == "healthy"
    finally:
        await db.close()
