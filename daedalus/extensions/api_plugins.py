"""Manifest validation and operator-managed activation of trusted host adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.plugins import (
    PROJECT_STATUS_MANIFEST,
    PluginRefused,
    PluginRegistry,
    ProjectStatusAdapter,
    validate_manifest,
)
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application


class ManifestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]


class InstallBody(ManifestBody):
    expected_digest: str = Field(min_length=64, max_length=64)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class TestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any]


class SafeModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    adapters = app.extensions.get("plugin_adapters")
    offered = {"project_status": lambda: ProjectStatusAdapter(app.db)}
    if isinstance(adapters, dict):
        offered.update(adapters)
    registry = PluginRegistry(app.db, offered)
    app.extensions["plugin_registry"] = registry
    if hasattr(app, "background"):
        app.background.append(asyncio.create_task(registry.reconcile(), name="plugin-reconcile"))

    @api.get("/api/plugins/catalog")
    async def plugin_catalog(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return [{"manifest": PROJECT_STATUS_MANIFEST, **validate_manifest(PROJECT_STATUS_MANIFEST)}]

    @api.post("/api/plugins/safe-mode")
    async def set_safe_mode(body: SafeModeBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await registry.set_safe_mode_command(
                Principal.operator(who), body.enabled,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/plugins/safe-mode")
    async def get_safe_mode(_: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        return {"enabled": bool(await app.db.kv_get("plugin_safe_mode", False))}

    @api.post("/api/plugins/validate")
    async def validate_plugin(body: ManifestBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return validate_manifest(body.manifest)
        except PluginRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/plugins/install")
    async def install_plugin(body: InstallBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await registry.install_command(
                Principal.operator(who), body.manifest, body.expected_digest,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except (PluginRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.get("/api/plugins")
    async def list_plugins(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        rows = await app.db.fetchall(
            "SELECT id, version, digest, status, health, created_at FROM plugin_manifests ORDER BY created_at DESC"
        )
        revision = await ControlStore(app.db).revision(Scope("global", "global"), Entity("collection", "global"))
        return {"items": [dict(row) for row in rows], "collection_revision": revision}

    @api.get("/api/plugins/{plugin_id}/health")
    async def plugin_health(plugin_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await registry.health(plugin_id)

    @api.post("/api/plugins/{plugin_id}/test")
    async def test_plugin(plugin_id: str, body: TestBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if plugin_id != "project_status" or body.tool != "inspect_project":
            raise HTTPException(409, "only the shipped read-only inspector has a test endpoint")
        try:
            return {"result": await registry.invoke(plugin_id, body.tool, body.arguments, granted={"board.read"})}
        except PluginRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.delete("/api/plugins/{plugin_id}/{version}")
    async def revoke_plugin(plugin_id: str, version: str, body: RevokeBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await registry.revoke_command(
                Principal.operator(who), plugin_id, version,
                expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such plugin") from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
