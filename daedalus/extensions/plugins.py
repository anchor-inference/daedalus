"""Opt-in, versioned extension manifests with a fail-closed host adapter boundary.

Only adapters registered by the host can run. A manifest is data, not a Python import path; a
third-party manifest alone cannot cause code to load into this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, SchemaError

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application

IDENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VERSION = re.compile(r"^[0-9][a-z0-9_.-]{0,63}$")
ALLOWED_SLOTS = {"project.settings", "task.detail", "result.detail", "integration.detail"}
ALLOWED_CAPABILITIES = {"board.read", "board.write", "files.read", "files.write", "events.subscribe", "notifications.write"}


class PluginRefused(ValueError):
    """A manifest, pin or runtime adapter failed the activation boundary."""


def canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or len(canonical(manifest)) > 64_000:
        raise PluginRefused("manifest exceeds its size bound")
    expected = {"id", "version", "display_name", "description", "host_api", "capabilities", "tools", "events", "dependencies", "ui_extensions"}
    if set(manifest) != expected or not isinstance(manifest.get("id"), str) or not IDENT.fullmatch(manifest["id"]) or not isinstance(manifest.get("version"), str) or not VERSION.fullmatch(manifest["version"]):
        raise PluginRefused("manifest keys, id or version are invalid")
    if not isinstance(manifest["display_name"], str) or not 1 <= len(manifest["display_name"]) <= 100 or not isinstance(manifest["description"], str) or not 20 <= len(manifest["description"]) <= 500:
        raise PluginRefused("manifest needs a bounded name and description")
    if manifest["host_api"] != "1":
        raise PluginRefused("unsupported host API")
    caps = manifest["capabilities"]
    if not isinstance(caps, list) or len(caps) > 16 or not all(isinstance(cap, str) for cap in caps) or len(caps) != len(set(caps)) or not set(caps) <= ALLOWED_CAPABILITIES:
        raise PluginRefused("unsupported or duplicate capability")
    tools = manifest["tools"]
    if not isinstance(tools, list) or len(tools) > 32:
        raise PluginRefused("too many tools")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "input_schema"} or not isinstance(tool["name"], str) or not IDENT.fullmatch(tool["name"]) or tool["name"] in names:
            raise PluginRefused("invalid or duplicate tool")
        names.add(tool["name"])
        if not isinstance(tool["input_schema"], dict) or tool["input_schema"].get("type") != "object":
            raise PluginRefused("tool input must be an object schema")
        try:
            Draft202012Validator.check_schema(tool["input_schema"])
        except SchemaError as exc:
            raise PluginRefused("invalid tool schema") from exc
    events = manifest["events"]
    if not isinstance(events, list) or len(events) > 32 or not all(isinstance(event, str) and IDENT.fullmatch(event) for event in events):
        raise PluginRefused("invalid event list")
    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) > 16:
        raise PluginRefused("too many dependencies")
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) != {"id", "version", "digest"} or not all(isinstance(dep[key], str) and dep[key] for key in dep) or not re.fullmatch(r"[0-9a-f]{64}", dep["digest"]):
            raise PluginRefused("dependency needs an exact version and digest")
    extensions = manifest["ui_extensions"]
    if not isinstance(extensions, list) or len(extensions) > 16:
        raise PluginRefused("too many UI extensions")
    for extension in extensions:
        if not isinstance(extension, dict) or set(extension) != {"id", "slot", "schema_version", "component"} or not isinstance(extension["id"], str) or not IDENT.fullmatch(extension["id"]) or extension["slot"] not in ALLOWED_SLOTS or extension["schema_version"] != 1 or extension["component"] not in {"key_value", "status", "action"}:
            raise PluginRefused("unsupported UI extension descriptor")
    return {"valid": True, "digest": hashlib.sha256(canonical(manifest)).hexdigest(), "required_capabilities": caps, "ui_extensions": extensions}


class PluginRegistry:
    def __init__(self, db: Database, adapters: dict[str, Callable[[], Any]] | None = None, dispatcher: Any = None) -> None:
        self.db = db
        self.adapters = adapters or {}
        self.active: dict[str, Any] = {}
        self.safe_mode = False
        self.dispatcher = dispatcher
        self.lifecycle = asyncio.Lock()

    async def dependencies_active(self, manifest: dict[str, Any]) -> bool:
        for dep in manifest["dependencies"]:
            row = await self.db.fetchone(
                "SELECT 1 FROM plugin_manifests WHERE id = ? AND version = ? AND digest = ? AND status = 'active'",
                (dep["id"], dep["version"], dep["digest"]),
            )
            if row is None:
                return False
        return True

    async def install_command(
        self, principal: Principal, manifest: dict[str, Any], expected_digest: str,
        *, expected_collection_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        checked = validate_manifest(manifest)
        if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
            raise PluginRefused("extensions are disabled in safe mode")
        if checked["digest"] != expected_digest:
            raise PluginRefused("manifest digest changed")
        if manifest["id"] not in self.adapters:
            raise PluginRefused("no trusted host adapter is registered")

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            for dep in manifest["dependencies"]:
                cursor = await conn.execute(
                    "SELECT 1 FROM plugin_manifests WHERE id = ? AND version = ? AND digest = ? AND status = 'active'",
                    (dep["id"], dep["version"], dep["digest"]),
                )
                pinned = await cursor.fetchone()
                await cursor.close()
                if pinned is None:
                    raise PluginRefused("dependency pin is not active")
            await conn.execute(
                "INSERT INTO plugin_manifests(id, version, digest, manifest, status, health, created_at) "
                "VALUES (?, ?, ?, ?, 'staged', 'unknown', ?)",
                (manifest["id"], manifest["version"], expected_digest, canonical(manifest).decode(), datetime.now(UTC).isoformat()),
            )
            await OutboxStore.enqueue(
                conn, mutation, principal, kind="plugin.register", operation="plugin.install",
                payload={"id": manifest["id"], "version": manifest["version"]}, effects=("plugin.register",),
            )
            return {"id": manifest["id"], "version": manifest["version"], "status": "staged", **checked}

        response = await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "plugin.install", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"manifest": manifest, "expected_digest": expected_digest}, effect, effects=("plugin.register",),
        )
        if self.dispatcher is not None:
            self.dispatcher.notify()
        return response

    async def load_active(self) -> None:
        async with self.lifecycle:
            await self._load_active_locked()

    async def _load_active_locked(self) -> None:
        self.safe_mode = bool(await self.db.kv_get("plugin_safe_mode", False))
        if self.safe_mode:
            return
        active_rows = await self.db.fetchall(
            "SELECT id, version, digest, manifest FROM plugin_manifests WHERE status = 'active' ORDER BY created_at"
        )
        for row in active_rows:
            plugin_id = str(row["id"])
            if plugin_id in self.active:
                continue
            factory = self.adapters.get(plugin_id)
            if factory is None:
                continue
            try:
                manifest = json.loads(row["manifest"])
                valid = validate_manifest(manifest)["digest"] == row["digest"]
            except (ValueError, TypeError, PluginRefused):
                valid = False
            if valid and not await self.dependencies_active(manifest):
                valid = False
            if not valid:
                await self.db.execute(
                    "UPDATE plugin_manifests SET health = 'digest_mismatch' WHERE id = ? AND version = ?",
                    (plugin_id, row["version"]),
                )
                continue
            adapter = factory()
            try:
                adapter.register(manifest)
            except Exception:
                await self.db.execute(
                    "UPDATE plugin_manifests SET health = 'registration_failed' WHERE id = ? AND version = ?",
                    (plugin_id, row["version"]),
                )
                continue
            self.active[plugin_id] = adapter

    async def run(self, claim: Claim, check: Callable[[Claim], Any]) -> EffectOutcome:
        plugin_id, version = claim.payload["id"], claim.payload["version"]
        row = await self.db.fetchone(
            "SELECT manifest,digest,status FROM plugin_manifests WHERE id = ? AND version = ?", (plugin_id, version)
        )
        if row is None or row["status"] != "staged":
            return EffectOutcome("failed", "plugin is no longer staged")
        manifest = json.loads(row["manifest"])
        if validate_manifest(manifest)["digest"] != row["digest"]:
            return EffectOutcome("failed", "plugin digest changed")
        if not await self.dependencies_active(manifest):
            return EffectOutcome("failed", "plugin dependency pin is inactive")
        factory = self.adapters.get(plugin_id)
        if factory is None:
            return EffectOutcome("failed", "trusted adapter is unavailable")
        if await self.db.kv_get("plugin_safe_mode", False):
            return EffectOutcome("failed", "extensions are disabled in safe mode")
        await check(claim)
        async with self.lifecycle:
            current = await self.db.fetchone(
                "SELECT status,digest FROM plugin_manifests WHERE id = ? AND version = ?", (plugin_id, version)
            )
            if current is None or current["status"] != "staged" or current["digest"] != row["digest"]:
                return EffectOutcome("failed", "plugin lifecycle changed before registration")
            if await self.db.kv_get("plugin_safe_mode", False) or not await self.dependencies_active(manifest):
                return EffectOutcome("failed", "plugin policy changed before registration")
            await check(claim)
            adapter = factory()
            try:
                adapter.register(manifest)
                async with self.db.transaction() as conn:
                    cursor = await conn.execute(
                        "UPDATE plugin_manifests SET status = 'active', health = 'unknown' WHERE id = ? AND version = ? AND status = 'staged'",
                        (plugin_id, version),
                    )
                    if cursor.rowcount != 1:
                        raise PluginRefused("plugin lifecycle changed during registration")
            except Exception as exc:
                try:
                    adapter.unregister()
                except Exception:
                    pass
                return EffectOutcome("unknown", f"registration requires reconciliation: {type(exc).__name__}")
            self.active[plugin_id] = adapter
            return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        plugin_id, version = claim.payload["id"], claim.payload["version"]
        row = await self.db.fetchone(
            "SELECT status,digest,manifest FROM plugin_manifests WHERE id = ? AND version = ?", (plugin_id, version)
        )
        if row is None:
            return EffectResolution("failed", {"observed": "manifest_missing"})
        if row["status"] == "active":
            if plugin_id in self.active and validate_manifest(json.loads(row["manifest"]))["digest"] == row["digest"]:
                return EffectResolution("completed", {"observed": "active_registered_manifest", "digest": row["digest"]})
        if plugin_id == "project_status" and row["status"] == "staged" and plugin_id not in self.active:
            # This shipped adapter has no durable side effect; an interrupted process cannot retain its registration.
            return EffectResolution("failed", {"observed": "staged_without_registered_adapter"})
        return None

    async def revoke_command(
        self, principal: Principal, plugin_id: str, version: str, *,
        expected_collection_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute(
                "SELECT status FROM plugin_manifests WHERE id = ? AND version = ?", (plugin_id, version)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(plugin_id)
            await conn.execute(
                "UPDATE plugin_manifests SET status = 'revoked' WHERE id = ? AND version = ?",
                (plugin_id, version),
            )
            return {"id": plugin_id, "version": version, "status": "revoked"}

        async with self.lifecycle:
            response = await ControlStore(self.db).mutate(
                principal, Scope("global", "global"), "plugin.revoke", client_operation_id,
                expected_collection_revision, Entity("collection", "global"),
                {"id": plugin_id, "version": version}, effect,
            )
            adapter = self.active.pop(plugin_id, None)
            if adapter is not None:
                adapter.unregister()
            return response

    async def health(self, plugin_id: str) -> dict[str, Any]:
        if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
            return {"id": plugin_id, "state": "disabled", "reason": "safe_mode"}
        adapter = self.active.get(plugin_id)
        if adapter is None:
            return {"id": plugin_id, "state": "inactive"}
        try:
            result = adapter.health()
            return {"id": plugin_id, "state": "configured_unverified" if result else "unhealthy"}
        except Exception:
            return {"id": plugin_id, "state": "unhealthy", "reason": "adapter_error"}

    async def invoke(self, plugin_id: str, tool_name: str, arguments: dict[str, Any], *, granted: set[str]) -> Any:
        if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False) or plugin_id not in self.active:
            raise PluginRefused("plugin is inactive")
        row = await self.db.fetchone(
            "SELECT manifest FROM plugin_manifests WHERE id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (plugin_id,),
        )
        if row is None:
            raise PluginRefused("plugin has no active manifest")
        manifest = json.loads(row["manifest"])
        if not set(manifest["capabilities"]) <= granted:
            raise PluginRefused("current grant does not cover plugin capabilities")
        tool = next((item for item in manifest["tools"] if item["name"] == tool_name), None)
        if tool is None:
            raise PluginRefused("tool is not declared")
        errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(arguments))
        if errors:
            raise PluginRefused("tool input failed schema validation")
        try:
            return await self.active[plugin_id].invoke(tool_name, arguments)
        except Exception as exc:
            await self.db.execute(
                "UPDATE plugin_manifests SET health = 'adapter_error' WHERE id = ? AND status = 'active'",
                (plugin_id,),
            )
            raise PluginRefused("plugin adapter failed") from exc

    async def set_safe_mode_command(
        self, principal: Principal, enabled: bool, *, expected_collection_revision: int,
        client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            await conn.execute(
                "INSERT INTO kv(key, value) VALUES ('plugin_safe_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(enabled),),
            )
            return {"safe_mode": enabled}

        async with self.lifecycle:
            response = await ControlStore(self.db).mutate(
                principal, Scope("global", "global"), "plugin.safe_mode", client_operation_id,
                expected_collection_revision, Entity("collection", "global"), {"enabled": enabled}, effect,
            )
            self.safe_mode = bool(await self.db.kv_get("plugin_safe_mode", False))
        if not self.safe_mode:
            await self.load_active()
        return response


PROJECT_STATUS_MANIFEST: dict[str, Any] = {
    "id": "project_status",
    "version": "1.0.0",
    "display_name": "Project status",
    "description": "Read task counts by status for one selected project.",
    "host_api": "1",
    "capabilities": ["board.read"],
    "tools": [{
        "name": "inspect_project",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["project_id"],
        },
    }],
    "events": [],
    "dependencies": [],
    "ui_extensions": [{"id": "project_status", "slot": "project.settings", "schema_version": 1, "component": "status"}],
}


class ProjectStatusAdapter:
    """A shipped read-only adapter whose preview and invocation use the same scoped query."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.registered = False

    def register(self, manifest: dict[str, Any]) -> None:
        if manifest != PROJECT_STATUS_MANIFEST:
            raise PluginRefused("this adapter only accepts its shipped manifest")
        self.registered = True

    def unregister(self) -> None:
        self.registered = False

    def health(self) -> bool:
        return self.registered

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.registered or tool_name != "inspect_project":
            raise PluginRefused("tool is not active")
        project_id = arguments["project_id"]
        row = await self.db.fetchone("SELECT id, name FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise PluginRefused("no such project")
        counts = await self.db.fetchall(
            "SELECT status, COUNT(*) count FROM board_tasks WHERE project_id = ? GROUP BY status", (project_id,)
        )
        return {"project_id": project_id, "name": row["name"], "tasks": {item["status"]: item["count"] for item in counts}}


async def install(app: Application) -> list[Any]:
    dispatcher = app.extensions["effects"]
    adapters = app.extensions.get("plugin_adapters")
    offered = {"project_status": lambda: ProjectStatusAdapter(app.db)}
    if isinstance(adapters, dict):
        offered.update(adapters)
    registry = PluginRegistry(app.db, offered, dispatcher)
    await registry.load_active()
    dispatcher.register("plugin.register", registry)
    app.extensions["plugin_registry"] = registry
    return []
