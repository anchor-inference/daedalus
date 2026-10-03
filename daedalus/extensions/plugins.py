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
READ_MODELS = {
    "task_counts": {"type": "object", "additionalProperties": False,
                    "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 64}},
                    "required": ["project_id"]},
    "accepted_results": {"type": "object", "additionalProperties": False,
                         "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 64},
                                        "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                                                  "title": "Maximum results"}},
                         "required": ["project_id"]},
}


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
    declarative = False
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) not in ({"name", "input_schema"}, {"name", "input_schema", "read_model"}) or not isinstance(tool["name"], str) or not IDENT.fullmatch(tool["name"]) or tool["name"] in names:
            raise PluginRefused("invalid or duplicate tool")
        names.add(tool["name"])
        if not isinstance(tool["input_schema"], dict) or tool["input_schema"].get("type") != "object":
            raise PluginRefused("tool input must be an object schema")
        try:
            Draft202012Validator.check_schema(tool["input_schema"])
        except SchemaError as exc:
            raise PluginRefused("invalid tool schema") from exc
        if "read_model" in tool:
            declarative = True
            if (not isinstance(tool["read_model"], str) or tool["read_model"] not in READ_MODELS
                    or tool["input_schema"] != READ_MODELS[tool["read_model"]]):
                raise PluginRefused("declarative read model or its bounded schema is unsupported")
    if declarative and (len(tools) > 4 or any("read_model" not in tool for tool in tools)
                        or caps != ["board.read"] or manifest["events"] or manifest["dependencies"]):
        raise PluginRefused("declarative read cards may only use bounded board.read models")
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
        if not isinstance(extension, dict) or set(extension) != {"id", "slot", "schema_version", "component", "tool"} or not isinstance(extension["id"], str) or not IDENT.fullmatch(extension["id"]) or extension["slot"] not in ALLOWED_SLOTS or extension["schema_version"] != 1 or extension["component"] not in {"key_value", "status", "action"} or extension["tool"] not in names:
            raise PluginRefused("unsupported UI extension descriptor")
        if declarative and extension["slot"] != "project.settings":
            raise PluginRefused("declarative read cards belong to project settings")
    return {"valid": True, "digest": hashlib.sha256(canonical(manifest)).hexdigest(), "required_capabilities": caps, "ui_extensions": extensions}


class PluginRegistry:
    def __init__(self, db: Database, adapters: dict[str, Callable[[], Any]] | None = None, dispatcher: Any = None) -> None:
        self.db = db
        self.adapters = adapters or {}
        self.active: dict[str, Any] = {}
        self.active_versions: dict[str, tuple[str, str]] = {}
        self.safe_mode = False
        self.dispatcher = dispatcher
        self.lifecycle = asyncio.Lock()

    def _factory(self, manifest: dict[str, Any]) -> Callable[[], Any] | None:
        factory = self.adapters.get(manifest["id"])
        if factory is not None:
            if any("read_model" in tool for tool in manifest["tools"]):
                return None
            return factory
        if manifest["tools"] and all("read_model" in tool for tool in manifest["tools"]):
            return lambda: DeclarativeReadAdapter(self.db)
        return None

    async def activation(self, plugin_id: str, version: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT state,error,payload_json FROM effect_outbox WHERE kind = 'plugin.register'"
            " AND json_extract(payload_json,'$.data.id') = ?"
            " AND json_extract(payload_json,'$.data.version') = ?"
            " ORDER BY created_at DESC,id DESC LIMIT 1",
            (plugin_id, version),
        )
        return dict(row) if row is not None else None

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
        replaces_version: str | None = None,
    ) -> dict[str, Any]:
        checked = validate_manifest(manifest)
        if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
            raise PluginRefused("extensions are disabled in safe mode")
        if checked["digest"] != expected_digest:
            raise PluginRefused("manifest digest changed")
        if self._factory(manifest) is None:
            raise PluginRefused("no trusted host adapter is registered")

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            async with conn.execute(
                "SELECT version,manifest FROM plugin_manifests WHERE id = ? AND status = 'active'",
                (manifest["id"],),
            ) as cursor:
                active = await cursor.fetchall()
            if len(active) > 1 or (active and active[0]["version"] != replaces_version) or (
                    not active and replaces_version is not None):
                raise PluginRefused("the exact active version changed before installation")
            if active and (not all("read_model" in tool for tool in manifest["tools"])
                           or not all("read_model" in tool for tool in json.loads(active[0]["manifest"])["tools"])):
                raise PluginRefused("version switching is limited to declarative read cards")
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
                payload={"id": manifest["id"], "version": manifest["version"],
                         "replaces_version": replaces_version}, effects=("plugin.register",),
            )
            return {"id": manifest["id"], "version": manifest["version"], "status": "staged", **checked}

        response = await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "plugin.install", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"manifest": manifest, "expected_digest": expected_digest,
             "replaces_version": replaces_version}, effect, effects=("plugin.register",),
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
        counts: dict[str, int] = {}
        for row in active_rows:
            counts[row["id"]] = counts.get(row["id"], 0) + 1
        for row in active_rows:
            plugin_id = str(row["id"])
            if counts[plugin_id] != 1:
                bound = self.active.pop(plugin_id, None)
                self.active_versions.pop(plugin_id, None)
                if bound is not None:
                    try:
                        bound.unregister()
                    except Exception:
                        # The binding is still barred from host calls; an adapter's own cleanup
                        # may require operator repair and cannot make either version authoritative.
                        pass
                await self.db.execute(
                    "UPDATE plugin_manifests SET health = 'ambiguous_active_version' WHERE id = ? AND status = 'active'",
                    (plugin_id,),
                )
                continue
            if plugin_id in self.active:
                if self.active_versions.get(plugin_id) == (row["version"], row["digest"]):
                    continue
                previous = self.active.pop(plugin_id)
                self.active_versions.pop(plugin_id, None)
                try:
                    previous.unregister()
                except Exception:
                    await self.db.execute(
                        "UPDATE plugin_manifests SET health = 'unregistration_failed' WHERE id = ? AND version = ?",
                        (plugin_id, row["version"]),
                    )
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
            factory = self._factory(manifest)
            if factory is None:
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
            self.active_versions[plugin_id] = (row["version"], row["digest"])

    async def run(self, claim: Claim, check: Callable[[Claim], Any]) -> EffectOutcome:
        plugin_id, version = claim.payload["id"], claim.payload["version"]
        replaces_version = claim.payload.get("replaces_version")
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
        factory = self._factory(manifest)
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
            active_row = await self.db.fetchone(
                "SELECT version,digest,manifest FROM plugin_manifests WHERE id = ? AND status = 'active'",
                (plugin_id,),
            )
            if (active_row is None and replaces_version is not None) or (
                    active_row is not None and active_row["version"] != replaces_version):
                return EffectOutcome("failed", "the exact previous version is no longer active")
            if active_row is not None and (self.active_versions.get(plugin_id) !=
                                           (active_row["version"], active_row["digest"])):
                return EffectOutcome("failed", "the previous version is not bound to its exact manifest")
            if active_row is not None and (not all("read_model" in tool for tool in manifest["tools"])
                                           or not all("read_model" in tool for tool in json.loads(active_row["manifest"])["tools"])):
                return EffectOutcome("failed", "only declarative read cards can switch versions")
            await check(claim)
            adapter = factory()
            try:
                adapter.register(manifest)
                async with self.db.transaction() as conn:
                    async with conn.execute(
                        "SELECT version,digest FROM plugin_manifests WHERE id = ? AND status = 'active'",
                        (plugin_id,),
                    ) as cursor:
                        active_now = await cursor.fetchall()
                    if len(active_now) > 1 or (not active_now and replaces_version is not None) or (
                            active_now and active_now[0]["version"] != replaces_version):
                        raise PluginRefused("the exact previous version changed during registration")
                    if active_now:
                        cursor = await conn.execute(
                            "UPDATE plugin_manifests SET status = 'revoked' WHERE id = ? AND version = ?"
                            " AND digest = ? AND status = 'active'",
                            (plugin_id, replaces_version, active_now[0]["digest"]),
                        )
                        if cursor.rowcount != 1:
                            raise PluginRefused("the previous version could not be retired")
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
            old_adapter = self.active.get(plugin_id)
            self.active[plugin_id] = adapter
            self.active_versions[plugin_id] = (version, row["digest"])
            if old_adapter is not None:
                old_adapter.unregister()
            return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        plugin_id, version = claim.payload["id"], claim.payload["version"]
        row = await self.db.fetchone(
            "SELECT status,digest,manifest FROM plugin_manifests WHERE id = ? AND version = ?", (plugin_id, version)
        )
        if row is None:
            return EffectResolution("failed", {"observed": "manifest_missing"})
        if row["status"] == "active":
            if (self.active_versions.get(plugin_id) == (version, row["digest"])
                    and validate_manifest(json.loads(row["manifest"]))["digest"] == row["digest"]):
                return EffectResolution("completed", {"observed": "active_registered_manifest", "digest": row["digest"]})
        if row["status"] == "staged" and self.active_versions.get(plugin_id, (None, None))[0] != version:
            manifest = json.loads(row["manifest"])
            if (plugin_id == "project_status" or (manifest["tools"] and all(
                    "read_model" in tool for tool in manifest["tools"]))):
                # These host adapters only hold in-process read bindings; after a process restart
                # no staged registration has a durable external side effect to rediscover.
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
            if self.active_versions.get(plugin_id, (None, None))[0] == version:
                adapter = self.active.pop(plugin_id, None)
                self.active_versions.pop(plugin_id, None)
                if adapter is not None:
                    adapter.unregister()
            return response

    async def rollback_command(
        self, principal: Principal, plugin_id: str, target_version: str, current_version: str,
        *, expected_collection_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            async with conn.execute(
                "SELECT version,manifest FROM plugin_manifests WHERE id = ? AND status = 'active'",
                (plugin_id,),
            ) as cursor:
                active = await cursor.fetchall()
            async with conn.execute(
                "SELECT digest,manifest,status FROM plugin_manifests WHERE id = ? AND version = ?",
                (plugin_id, target_version),
            ) as cursor:
                target = await cursor.fetchone()
            if (len(active) != 1 or active[0]["version"] != current_version or target is None
                    or target["status"] != "revoked"):
                raise PluginRefused("rollback target or current active version changed")
            if not all("read_model" in tool for tool in json.loads(active[0]["manifest"])["tools"]):
                raise PluginRefused("only declarative read cards support rollback")
            manifest = json.loads(target["manifest"])
            if not manifest["tools"] or not all("read_model" in tool for tool in manifest["tools"]):
                raise PluginRefused("rollback target is not a declarative read card")
            if validate_manifest(manifest)["digest"] != target["digest"]:
                raise PluginRefused("rollback source digest changed")
            await conn.execute(
                "UPDATE plugin_manifests SET status = 'staged' WHERE id = ? AND version = ? AND status = 'revoked'",
                (plugin_id, target_version),
            )
            await OutboxStore.enqueue(
                conn, mutation, principal, kind="plugin.register", operation="plugin.rollback",
                payload={"id": plugin_id, "version": target_version,
                         "replaces_version": current_version}, effects=("plugin.register",),
            )
            return {"id": plugin_id, "version": target_version, "status": "staged",
                    "replaces_version": current_version, "digest": target["digest"]}

        response = await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "plugin.rollback", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"id": plugin_id, "target_version": target_version, "current_version": current_version},
            effect, effects=("plugin.register",),
        )
        if self.dispatcher is not None:
            self.dispatcher.notify()
        return response

    async def retry_command(
        self, principal: Principal, plugin_id: str, version: str, *,
        expected_collection_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            async with conn.execute(
                "SELECT digest,manifest,status FROM plugin_manifests WHERE id = ? AND version = ?",
                (plugin_id, version),
            ) as cursor:
                target = await cursor.fetchone()
            async with conn.execute(
                "SELECT state,payload_json FROM effect_outbox WHERE kind = 'plugin.register'"
                " AND json_extract(payload_json,'$.data.id') = ?"
                " AND json_extract(payload_json,'$.data.version') = ?"
                " ORDER BY created_at DESC,id DESC LIMIT 1",
                (plugin_id, version),
            ) as cursor:
                previous = await cursor.fetchone()
            if target is None or target["status"] != "staged" or previous is None or previous["state"] not in {"failed", "cancelled"}:
                raise PluginRefused("only a terminal failed activation can be retried")
            manifest = json.loads(target["manifest"])
            if validate_manifest(manifest)["digest"] != target["digest"] or self._factory(manifest) is None:
                raise PluginRefused("the pinned host adapter is unavailable or changed")
            if not (plugin_id == "project_status" or all("read_model" in tool for tool in manifest["tools"])):
                raise PluginRefused("retry requires a pure host read adapter")
            replaces = json.loads(previous["payload_json"])["data"].get("replaces_version")
            async with conn.execute(
                "SELECT version FROM plugin_manifests WHERE id = ? AND status = 'active'", (plugin_id,),
            ) as cursor:
                active = await cursor.fetchall()
            if len(active) > 1 or (active and active[0]["version"] != replaces) or (
                    not active and replaces is not None):
                raise PluginRefused("the exact previous version changed before retry")
            async with conn.execute("SELECT value FROM kv WHERE key = 'plugin_safe_mode'") as cursor:
                safe = await cursor.fetchone()
            if self.safe_mode or (safe is not None and json.loads(safe["value"])):
                raise PluginRefused("extensions are disabled in safe mode")
            await OutboxStore.enqueue(
                conn, mutation, principal, kind="plugin.register", operation="plugin.retry",
                payload={"id": plugin_id, "version": version,
                         "replaces_version": replaces}, effects=("plugin.register",),
            )
            return {"id": plugin_id, "version": version, "status": "staged",
                    "activation_state": "pending", "digest": target["digest"]}

        response = await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "plugin.retry", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"id": plugin_id, "version": version}, effect, effects=("plugin.register",),
        )
        if self.dispatcher is not None:
            self.dispatcher.notify()
        return response

    async def health(self, plugin_id: str) -> dict[str, Any]:
        if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
            return {"id": plugin_id, "state": "disabled", "reason": "safe_mode"}
        adapter = self.active.get(plugin_id)
        if adapter is None:
            return {"id": plugin_id, "state": "inactive"}
        rows = await self.db.fetchall(
            "SELECT version,digest FROM plugin_manifests WHERE id = ? AND status = 'active'", (plugin_id,)
        )
        if len(rows) != 1 or self.active_versions.get(plugin_id) != (rows[0]["version"], rows[0]["digest"]):
            return {"id": plugin_id, "state": "unhealthy", "reason": "active_version_ambiguous"}
        try:
            result = adapter.health()
            return {"id": plugin_id, "state": "configured_unverified" if result else "unhealthy"}
        except Exception:
            return {"id": plugin_id, "state": "unhealthy", "reason": "adapter_error"}

    async def preview_read(self, principal: Principal, project_id: str, manifest: dict[str, Any],
                           expected_digest: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        checked = validate_manifest(manifest)
        if checked["digest"] != expected_digest:
            raise PluginRefused("manifest digest changed before preview")
        if principal.origin_class != "operator":
            raise PluginRefused("an authenticated operator must review this preview")
        async with self.lifecycle:
            if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
                raise PluginRefused("extensions are disabled in safe mode")
            async with self.db.transaction() as conn:
                await ControlStore(self.db).attest(conn, principal, Scope("project", project_id))
            factory = self._factory(manifest)
            if factory is None or set(manifest["capabilities"]) != {"board.read"}:
                raise PluginRefused("preview only supports trusted board.read adapters")
            tool = next((item for item in manifest["tools"] if item["name"] == tool_name), None)
            if tool is None:
                raise PluginRefused("tool is not declared")
            if "project_id" in arguments and arguments["project_id"] != project_id:
                raise PluginRefused("tool arguments name another project")
            bound = {**arguments, "project_id": project_id}
            if list(Draft202012Validator(tool["input_schema"]).iter_errors(bound)):
                raise PluginRefused("tool input failed schema validation")
            adapter = factory()
            try:
                adapter.register(manifest)
                result = await adapter.invoke(tool_name, bound)
            finally:
                adapter.unregister()
            return {"preview_only": True, "digest": checked["digest"], "result": result,
                    "required_capabilities": checked["required_capabilities"]}

    async def invoke_read(self, principal: Principal, project_id: str, plugin_id: str,
                          tool_name: str, arguments: dict[str, Any]) -> Any:
        # Registration and calls share the lock: revocation must wait for an entered host adapter
        # call, then no later call may observe the old binding.
        async with self.lifecycle:
            scope = Scope("project", project_id)
            async with self.db.transaction() as conn:
                if principal.origin_class == "agent":
                    await ControlStore(self.db)._office(conn, principal.actor_id, scope)
                elif principal.origin_class == "operator":
                    await ControlStore(self.db).attest(conn, principal, scope)
                else:
                    raise PluginRefused("only the operator or current project coordinator can read this plugin")
            if self.safe_mode or await self.db.kv_get("plugin_safe_mode", False):
                raise PluginRefused("plugin is inactive")
            adapter = self.active.get(plugin_id)
            binding = self.active_versions.get(plugin_id)
            if adapter is None or binding is None:
                raise PluginRefused("plugin is inactive")
            rows = await self.db.fetchall(
                "SELECT version,digest,manifest FROM plugin_manifests WHERE id = ? AND status = 'active'",
                (plugin_id,),
            )
            if len(rows) != 1 or binding != (rows[0]["version"], rows[0]["digest"]):
                raise PluginRefused("the active plugin version is ambiguous or changed")
            manifest = json.loads(rows[0]["manifest"])
            if validate_manifest(manifest)["digest"] != rows[0]["digest"]:
                raise PluginRefused("plugin digest changed")
            if not await self.dependencies_active(manifest):
                raise PluginRefused("plugin dependency pin is inactive")
            if set(manifest["capabilities"]) != {"board.read"}:
                raise PluginRefused("this shared read surface only permits board.read")
            tool = next((item for item in manifest["tools"] if item["name"] == tool_name), None)
            if tool is None:
                raise PluginRefused("tool is not declared")
            if "project_id" in arguments and arguments["project_id"] != project_id:
                raise PluginRefused("tool arguments name another project")
            arguments = {**arguments, "project_id": project_id}
            errors = list(Draft202012Validator(tool["input_schema"]).iter_errors(arguments))
            if errors:
                raise PluginRefused("tool input failed schema validation")
            try:
                return await adapter.invoke(tool_name, arguments)
            except Exception as exc:
                await self.db.execute(
                    "UPDATE plugin_manifests SET health = 'adapter_error' WHERE id = ? AND version = ?",
                    (plugin_id, binding[0]),
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
            if self.safe_mode:
                for plugin_id, adapter in self.active.items():
                    try:
                        adapter.unregister()
                    except Exception:
                        await self.db.execute(
                            "UPDATE plugin_manifests SET health = 'unregistration_failed' WHERE id = ? AND status = 'active'",
                            (plugin_id,),
                        )
                self.active.clear()
                self.active_versions.clear()
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
    "ui_extensions": [{"id": "project_status", "slot": "project.settings", "schema_version": 1,
                       "component": "status", "tool": "inspect_project"}],
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


class DeclarativeReadAdapter:
    """Interpret reviewed read models through fixed host queries, without loading authored code."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tools: dict[str, str] = {}

    def register(self, manifest: dict[str, Any]) -> None:
        validate_manifest(manifest)
        if not manifest["tools"] or not all("read_model" in tool for tool in manifest["tools"]):
            raise PluginRefused("the manifest does not declare bounded read models")
        self.tools = {tool["name"]: tool["read_model"] for tool in manifest["tools"]}

    def unregister(self) -> None:
        self.tools.clear()

    def health(self) -> bool:
        return bool(self.tools)

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        model = self.tools.get(tool_name)
        if model is None:
            raise PluginRefused("the declarative tool is not registered")
        project_id = arguments["project_id"]
        project = await self.db.fetchone("SELECT id,name FROM projects WHERE id = ?", (project_id,))
        if project is None:
            raise PluginRefused("no such project")
        if model == "task_counts":
            rows = await self.db.fetchall(
                "SELECT status,COUNT(*) AS count FROM board_tasks WHERE project_id = ? GROUP BY status",
                (project_id,),
            )
            return {"project_id": project_id, "name": project["name"],
                    "tasks": {row["status"]: row["count"] for row in rows}}
        if model == "accepted_results":
            rows = await self.db.fetchall(
                "SELECT t.id AS task_id,t.title,t.contract_revision,r.id AS result_id,"
                " r.original_digest AS result_digest FROM board_tasks t"
                " JOIN result_receipts r ON r.id = t.accepted_result_id AND r.task_id = t.id"
                " WHERE t.project_id = ? AND t.status = 'done'"
                " AND t.accepted_contract_revision = t.contract_revision"
                " ORDER BY t.id DESC LIMIT ?",
                (project_id, arguments.get("limit", 10)),
            )
            return {"project_id": project_id, "items": [dict(row) for row in rows]}
        raise PluginRefused("unsupported declarative read model")


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
