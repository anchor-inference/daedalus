"""Queue remote artifact publication and reconcile exact remote bytes after interruption.

A committed receipt authorizes one effect. A published label requires both the remote digest and
the current effect receipt; a lost SSH response remains unknown until a read-back proves it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.remote_ssh import PinnedSsh, RemoteHost, RemoteUnavailable, inspect_transfer, transfer_file
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application


def _fresh_host(row: Any) -> bool:
    return bool(row and row["identity_state"] == "verified" and row["probe_state"] == "supported"
                and row["probe_expires_at"] and datetime.fromisoformat(row["probe_expires_at"]) > datetime.now(UTC)
                and row["ssh_host"] and row["remote_root"])


def _remote(row: Any) -> RemoteHost:
    return RemoteHost(row["ssh_host"], row["ssh_port"], row["ssh_user"], row["public_key"], row["remote_root"])


class RuntimeTransfers:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.db = app.db
        self.control = ControlStore(app.db)

    async def readiness(self, manifest_id: str) -> dict[str, Any]:
        manifest = await self.db.fetchone("SELECT m.id,m.digest,m.size_bytes,m.file_id,COALESCE(m.project_id,t.project_id) AS project_id"
                                          " FROM artifact_manifests m LEFT JOIN board_tasks t ON t.id = m.task_id WHERE m.id = ?", (manifest_id,))
        if manifest is None:
            raise KeyError(manifest_id)
        file = await self.db.fetchone("SELECT sha256,size FROM files WHERE id = ?", (manifest["file_id"],)) if manifest["file_id"] else None
        available = bool(file and file["sha256"] == manifest["digest"] and file["size"] == manifest["size_bytes"])
        rows = await self.db.fetchall("SELECT id FROM artifact_transfers WHERE manifest_id = ? AND expected_digest = ? ORDER BY created_at DESC",
                                      (manifest_id, manifest["digest"]))
        return {"manifest_id": manifest_id, "project_id": manifest["project_id"], "available": available,
                "reason": "file_recorded" if available else "file_unavailable",
                "transfers": [await self.view(row["id"]) for row in rows]}

    async def view(self, transfer_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT t.*,e.state AS effect_state FROM artifact_transfers t"
                                     " LEFT JOIN effect_outbox e ON e.id = t.effect_id WHERE t.id = ?", (transfer_id,))
        if row is None:
            raise KeyError(transfer_id)
        state = row["state"]
        if row["effect_state"] in ("pending", "claimed"):
            state = "staging" if state != "verifying" else "verifying"
        elif row["effect_state"] == "unknown":
            state = "unknown"
        elif row["effect_state"] in ("failed", "cancelled"):
            state = "failed"
        elif row["effect_state"] != "completed" or row["published_digest"] != row["expected_digest"]:
            state = "unknown"
        return {"transfer_id": row["id"], "project_id": row["project_id"], "host_id": row["host_id"],
                "manifest_id": row["manifest_id"], "state": state, "effect_state": row["effect_state"],
                "verified_bytes": row["verified_bytes"] if state == "published" else 0,
                "total_bytes": row["expected_size"], "expected_digest": row["expected_digest"],
                "published_digest": row["published_digest"] if state == "published" else None,
                "error_code": row["error_code"], "observed_at": row["updated_at"],
                "receipt_id": row["receipt_id"], "effect_id": row["effect_id"]}

    async def start(self, principal: Principal, project_id: str, manifest_id: str, host_id: str, *,
                    expected_entity_revision: int, client_operation_id: str) -> dict[str, Any]:
        payload = {"project_id": project_id, "manifest_id": manifest_id, "host_id": host_id}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            host = await one(conn, "SELECT * FROM execution_hosts WHERE id = ?", (host_id,))
            if not _fresh_host(host):
                raise ControlConflict("remote artifact transfer has no current host proof")
            manifest = await one(conn, "SELECT m.*,COALESCE(m.project_id,t.project_id) AS scope_project"
                                 " FROM artifact_manifests m LEFT JOIN board_tasks t ON t.id = m.task_id WHERE m.id = ?", (manifest_id,))
            if manifest is None or manifest["scope_project"] != project_id:
                raise ControlConflict("artifact does not belong to this project")
            if not manifest["file_id"] or not re.fullmatch(r"[0-9a-f]{64}", manifest["digest"]):
                raise ControlConflict("artifact bytes are not available by verified file handle")
            file = await one(conn, "SELECT sha256,size FROM files WHERE id = ?", (manifest["file_id"],))
            if file is None or file["sha256"] != manifest["digest"] or file["size"] != manifest["size_bytes"]:
                raise ControlConflict("file bytes no longer match the artifact manifest")
            transfer_id = mutation.object_id
            action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="artifact.transfer",
                                                  operation="artifact.transfer", payload={"transfer_id": transfer_id,
                                                  "host_id": host_id, "host_generation": host["generation"],
                                                  "manifest_id": manifest_id, "expected_digest": manifest["digest"]},
                                                  effects=("artifact.transfer",))
            await conn.execute("INSERT INTO artifact_transfers(id,project_id,host_id,manifest_id,expected_digest,expected_size,"
                               "state,effect_id,receipt_id,host_generation,created_at,updated_at)"
                               " VALUES (?,?,?,?,?,?,'staging',?,?,?,?,?)",
                               (transfer_id, project_id, host_id, manifest_id, manifest["digest"], manifest["size_bytes"],
                                action_id, mutation.receipt_id, host["generation"], now(), now()))
            return {"transfer_id": transfer_id, "effect_id": action_id, "state": "queued", "host_generation": host["generation"]}

        return await self.control.mutate(principal, Scope("project", project_id), "artifact.transfer", client_operation_id,
                                         expected_entity_revision, Entity("project", project_id), payload, effect,
                                         effects=("artifact.transfer",))

    async def retry(self, principal: Principal, transfer_id: str, *, expected_entity_revision: int,
                    client_operation_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT project_id FROM artifact_transfers WHERE id = ?", (transfer_id,))
        if row is None:
            raise KeyError(transfer_id)
        project_id = row["project_id"]
        payload = {"transfer_id": transfer_id}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            transfer = await one(conn, "SELECT t.*,e.state AS effect_state FROM artifact_transfers t"
                                 " LEFT JOIN effect_outbox e ON e.id = t.effect_id WHERE t.id = ?", (transfer_id,))
            if transfer is None or transfer["effect_state"] not in ("unknown", "failed", "cancelled"):
                raise ControlConflict("the previous transfer effect is still current")
            host = await one(conn, "SELECT * FROM execution_hosts WHERE id = ?", (transfer["host_id"],))
            if not _fresh_host(host) or host["generation"] != transfer["host_generation"]:
                raise ControlConflict("host identity or capability changed before retry")
            action_id = await OutboxStore.enqueue(conn, mutation, principal, kind="artifact.transfer",
                                                  operation="artifact.transfer.retry", payload={"transfer_id": transfer_id,
                                                  "host_id": transfer["host_id"], "host_generation": host["generation"],
                                                  "manifest_id": transfer["manifest_id"],
                                                  "expected_digest": transfer["expected_digest"]},
                                                  effects=("artifact.transfer",))
            await conn.execute("UPDATE artifact_transfers SET state = 'staging',effect_id = ?,receipt_id = ?,"
                               "error_code = NULL,updated_at = ? WHERE id = ?", (action_id, mutation.receipt_id, now(), transfer_id))
            return {"transfer_id": transfer_id, "effect_id": action_id, "state": "queued", "host_generation": host["generation"]}

        return await self.control.mutate(principal, Scope("project", project_id), "artifact.transfer.retry", client_operation_id,
                                         expected_entity_revision, Entity("project", project_id), payload, effect,
                                         effects=("artifact.transfer",))


class ArtifactTransferEffect:
    def __init__(self, app: Application, ssh: PinnedSsh | None = None) -> None:
        self.app = app
        self.db = app.db
        self.ssh = ssh or PinnedSsh(str(app.settings.remote_ssh_identity_file) if app.settings.remote_ssh_identity_file else None)

    async def _source(self, claim: Claim) -> tuple[Any, Any, Any]:
        transfer = await self.db.fetchone("SELECT * FROM artifact_transfers WHERE id = ? AND effect_id = ?", (claim.payload["transfer_id"], claim.id))
        if transfer is None:
            raise ControlConflict("transfer effect was replaced")
        host = await self.db.fetchone("SELECT * FROM execution_hosts WHERE id = ?", (transfer["host_id"],))
        if not _fresh_host(host) or host["generation"] != transfer["host_generation"]:
            raise ControlConflict("remote host proof is stale")
        manifest = await self.db.fetchone("SELECT * FROM artifact_manifests WHERE id = ?", (transfer["manifest_id"],))
        if manifest is None or manifest["digest"] != transfer["expected_digest"] or manifest["size_bytes"] != transfer["expected_size"]:
            raise ControlConflict("artifact manifest changed")
        stored = await self.app.manager.files.get(manifest["file_id"])
        if stored is None or stored.sha256 != manifest["digest"] or stored.size != manifest["size_bytes"]:
            raise ControlConflict("artifact file is missing or changed")
        return transfer, host, self.app.manager.files.path_of(stored)

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        try:
            transfer, host, source = await self._source(claim)
            await check(claim)
        except (ControlConflict, AttributeError) as exc:
            await self.db.execute("UPDATE artifact_transfers SET state = 'failed',error_code = 'preflight_failed',updated_at = ?"
                                  " WHERE id = ? AND effect_id = ?", (now(), claim.payload["transfer_id"], claim.id))
            return EffectOutcome("failed", str(exc))
        await self.db.execute("UPDATE artifact_transfers SET state = 'verifying',updated_at = ? WHERE id = ? AND effect_id = ?",
                              (now(), transfer["id"], claim.id))
        try:
            answer = await transfer_file(self.ssh, _remote(host), source, name=transfer["id"],
                                         expected_digest=transfer["expected_digest"], expected_size=transfer["expected_size"])
        except RemoteUnavailable as exc:
            await self.db.execute("UPDATE artifact_transfers SET state = 'unknown',error_code = ?,updated_at = ?"
                                  " WHERE id = ? AND effect_id = ?", (exc.code, now(), transfer["id"], claim.id))
            if exc.code == "identity_changed":
                await self.db.execute("UPDATE execution_hosts SET probe_state = 'identity_changed',probe_expires_at = NULL,"
                                      "probe_error = 'identity_changed' WHERE id = ? AND generation = ?",
                                      (host["id"], host["generation"]))
            return EffectOutcome("unknown", exc.code)
        try:
            await check(claim)
        except Exception:  # noqa: BLE001 — the write may exist, but authority to publish it was revoked
            await self.db.execute("UPDATE artifact_transfers SET state = 'unknown',error_code = 'authority_changed',updated_at = ?"
                                  " WHERE id = ? AND effect_id = ?", (now(), transfer["id"], claim.id))
            return EffectOutcome("unknown", "authority_changed")
        current = await self.db.fetchone("SELECT generation,identity_state FROM execution_hosts WHERE id = ?", (host["id"],))
        if current is None or current["generation"] != host["generation"] or current["identity_state"] != "verified":
            await self.db.execute("UPDATE artifact_transfers SET state = 'unknown',error_code = 'host_changed',updated_at = ?"
                                  " WHERE id = ? AND effect_id = ?", (now(), transfer["id"], claim.id))
            return EffectOutcome("unknown", "host_changed")
        await self.db.execute("UPDATE artifact_transfers SET state = 'published',published_digest = ?,verified_bytes = ?,"
                              "error_code = NULL,updated_at = ? WHERE id = ? AND effect_id = ?",
                              (answer["digest"], answer["size"], now(), transfer["id"], claim.id))
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        try:
            transfer, host, _ = await self._source(claim)
            observed = await inspect_transfer(self.ssh, _remote(host), transfer["id"])
        except (ControlConflict, RemoteUnavailable, ValueError, AttributeError):
            return None
        published = observed.get("published") or {}
        if published.get("digest") != transfer["expected_digest"] or published.get("size") != transfer["expected_size"]:
            return None
        await self.db.execute("UPDATE artifact_transfers SET state = 'published',published_digest = ?,verified_bytes = ?,"
                              "error_code = NULL,updated_at = ? WHERE id = ? AND effect_id = ?",
                              (published["digest"], published["size"], now(), transfer["id"], claim.id))
        return EffectResolution("completed", {"digest": published["digest"], "size": published["size"]})
