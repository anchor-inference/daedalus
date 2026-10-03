"""Operator-pinned remote host identities and signed observations.

A signature proves possession of the proposed key for one fresh challenge. It does not prove
remote execution capability, and a rotated key stays blocked until another signed observation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from daedalus.extensions.remote_ssh import PinnedSsh, RemoteHost, ssh_key_blob
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Principal, Scope, now, one
from daedalus.stores.database import Database


def _key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the host public key must be base64") from exc
    if len(raw) != 32:
        raise ValueError("the host public key must be Ed25519")
    return raw


def fingerprint(public_key: str) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(ssh_key_blob(public_key)).digest()).decode().rstrip("=")


def _signed_message(host_id: str, nonce: str, key: bytes) -> bytes:
    return b"daedalus-host-identity-v1\x00" + host_id.encode() + b"\x00" + nonce.encode() + b"\x00" + key


def _signature(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the host signature must be base64") from exc
    if len(raw) != 64:
        raise ValueError("the host signature must be Ed25519")
    return raw


def _transport(host: str | None, port: int | None, user: str | None, root: str | None) -> dict[str, Any]:
    if not any(value is not None for value in (host, port, user, root)):
        return {"ssh_host": None, "ssh_port": None, "ssh_user": None, "remote_root": None}
    if not all(value is not None for value in (host, port, user, root)):
        raise ValueError("a remote host needs address, port, user, and approved root together")
    assert host is not None and port is not None and user is not None and root is not None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host) or ".." in host:
        raise ValueError("invalid remote host address")
    if isinstance(port, bool) or not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise ValueError("invalid remote SSH port or user")
    path = PurePosixPath(root)
    if not root.startswith("/") or root == "/" or any(part in ("..", ".") for part in root.split("/")) or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", root) or str(path) != root:
        raise ValueError("the approved remote root must be a fixed absolute directory")
    return {"ssh_host": host, "ssh_port": port, "ssh_user": user, "remote_root": root}


class RuntimeHosts:
    def __init__(self, db: Database, ssh: PinnedSsh | None = None, *, identity_file: Path | None = None) -> None:
        self.db = db
        self.control = ControlStore(db)
        self.ssh = ssh or PinnedSsh(str(identity_file) if identity_file else None)

    async def list(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT * FROM execution_hosts ORDER BY created_at,id")
        revision = await self.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='global' AND scope_id='global'")
        items = []
        current = datetime.now(UTC)
        for row in rows:
            identity = row["identity_state"]
            probe_current = bool(row["probe_expires_at"] and datetime.fromisoformat(row["probe_expires_at"]) > current)
            transfer_ready = probe_current and row["probe_state"] == "supported" and identity == "verified"
            items.append({"host_id": row["id"], "label": row["label"], "entity_revision": row["entity_revision"],
                          "identity_state": identity, "fingerprint": row["fingerprint"],
                          "public_key": row["public_key"],
                          "ssh_configured": bool(row["ssh_host"]), "remote_root": row["remote_root"],
                          "pending_fingerprint": row["pending_fingerprint"], "host_generation": row["generation"],
                          "reachability": "reachable" if transfer_ready else "unknown",
                          "capabilities_state": "unknown", "observed_at": row["observed_at"],
                          "artifact_transfer_state": "supported" if transfer_ready else "unknown",
                          "probe_expires_at": row["probe_expires_at"], "probe_error": row["probe_error"],
                          "effects_allowed": False, "blockers": ["remote_execution_unavailable"] if identity == "verified" else ["identity_unverified"]})
        return {"items": items, "collection_revision": int(revision["revision"]) if revision else None}

    async def enroll(self, principal: Principal, *, label: str, public_key: str,
                     expected_collection_revision: int, client_operation_id: str,
                     ssh_host: str | None = None, ssh_port: int | None = None,
                     ssh_user: str | None = None, remote_root: str | None = None) -> dict[str, Any]:
        if not label.strip() or len(label) > 120:
            raise ValueError("a short host label is required")
        _key(public_key)
        transport = _transport(ssh_host, ssh_port, ssh_user, remote_root)
        pin = fingerprint(public_key)
        payload = {"label": label.strip(), "public_key": public_key, **transport}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            await conn.execute("INSERT INTO execution_hosts(id,label,public_key,fingerprint,ssh_host,ssh_port,ssh_user,remote_root,identity_state,created_at)"
                               " VALUES (?,?,?,?,?,?,?,?,'pending',?)",
                               (mutation.object_id, label.strip(), public_key, pin, ssh_host, ssh_port, ssh_user, remote_root, now()))
            return {"host_id": mutation.object_id, "identity_state": "pending", "fingerprint": pin,
                    "capabilities_state": "unknown", "effects_allowed": False}

        return await self.control.mutate(principal, Scope("global", "global"), "host.enroll", client_operation_id,
                                         expected_collection_revision, Entity("collection", "global"), payload, effect)

    async def probe(self, principal: Principal, host_id: str, *, expected_collection_revision: int,
                    expected_host_revision: int, client_operation_id: str) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            await self.control.authorize(conn, principal, Scope("global", "global"), "host.probe")
        row = await self.db.fetchone("SELECT * FROM execution_hosts WHERE id = ?", (host_id,))
        if row is None:
            raise KeyError(host_id)
        if row["entity_revision"] != expected_host_revision or row["identity_state"] == "rejected":
            raise ControlConflict("host identity changed or was rejected")
        if not row["ssh_host"]:
            raise ValueError("this host has no SSH transport")
        host = RemoteHost(row["ssh_host"], row["ssh_port"], row["ssh_user"], row["public_key"], row["remote_root"])
        observation = await self.ssh.probe(host)
        payload = {"host_id": host_id, "expected_host_revision": expected_host_revision}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            current = await one(conn, "SELECT entity_revision,generation,identity_state FROM execution_hosts WHERE id = ?", (host_id,))
            if current is None or current["entity_revision"] != expected_host_revision or current["generation"] != row["generation"]:
                raise ControlConflict("host identity changed during the probe")
            candidate = observation.candidate_public_key
            changed = observation.state == "identity_changed" and candidate is not None and candidate != row["public_key"]
            if current["identity_state"] == "changed" and changed and candidate != row["pending_public_key"]:
                raise ControlConflict("a different key change is already awaiting a decision")
            identity = "changed" if changed else ("verified" if observation.state == "supported" and current["identity_state"] != "changed" else current["identity_state"])
            expiry = (datetime.now(UTC) + timedelta(seconds=30)).isoformat() if observation.state == "supported" else None
            await conn.execute("UPDATE execution_hosts SET identity_state = ?,pending_public_key = ?,pending_fingerprint = ?,"
                               "probe_state = ?,probe_expires_at = ?,probe_error = ?,observed_at = ?,"
                               "entity_revision = entity_revision + 1 WHERE id = ?",
                               (identity, candidate if changed else row["pending_public_key"] if identity == "changed" else None,
                                fingerprint(candidate) if changed else row["pending_fingerprint"] if identity == "changed" else None,
                                observation.state, expiry, None if observation.state == "supported" else observation.state,
                                now(), host_id))
            return {"host_id": host_id, "identity_state": identity, "probe_state": observation.state,
                    "artifact_transfer_state": "supported" if observation.state == "supported" and identity == "verified" else "unknown",
                    "effects_allowed": False, "host_generation": current["generation"]}

        return await self.control.mutate(principal, Scope("global", "global"), "host.probe", client_operation_id,
                                         expected_collection_revision, Entity("collection", "global"), payload, effect)

    async def challenge(self, principal: Principal, host_id: str, *, expected_collection_revision: int,
                        expected_host_revision: int, client_operation_id: str,
                        candidate_public_key: str | None = None) -> dict[str, Any]:
        if candidate_public_key is not None:
            _key(candidate_public_key)
        payload = {"host_id": host_id, "expected_host_revision": expected_host_revision,
                   "candidate_public_key": candidate_public_key}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT entity_revision,identity_state,public_key,generation FROM execution_hosts WHERE id = ?", (host_id,))
            if row is None:
                raise KeyError(host_id)
            if row["entity_revision"] != expected_host_revision or row["identity_state"] == "rejected":
                raise ControlConflict("host identity changed or was rejected")
            nonce = secrets.token_urlsafe(32)
            expiry = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            await conn.execute("INSERT INTO execution_host_challenges(id,host_id,generation,nonce_digest,expires_at,created_at) VALUES (?,?,?,?,?,?)",
                               (mutation.object_id, host_id, row["generation"], hashlib.sha256(nonce.encode()).hexdigest(), expiry, now()))
            signing_key = candidate_public_key or row["public_key"]
            message = _signed_message(host_id, nonce, _key(signing_key))
            return {"challenge_id": mutation.object_id, "host_id": host_id, "nonce": nonce,
                    "public_key": signing_key, "message_base64": base64.b64encode(message).decode(), "expires_at": expiry}

        return await self.control.mutate(principal, Scope("global", "global"), "host.challenge", client_operation_id,
                                         expected_collection_revision, Entity("collection", "global"), payload, effect)

    async def observe(self, host_id: str, *, challenge_id: str, nonce: str, public_key: str, signature: str) -> dict[str, Any]:
        key = _key(public_key)
        sign = _signature(signature)
        digest = hashlib.sha256(sign).hexdigest()
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(sign, _signed_message(host_id, nonce, key))
        except InvalidSignature as exc:
            raise ValueError("host identity signature did not verify") from exc
        async with self.db.transaction() as conn:
            challenge = await one(conn, "SELECT * FROM execution_host_challenges WHERE id = ? AND host_id = ?", (challenge_id, host_id))
            if challenge is None or challenge["nonce_digest"] != hashlib.sha256(nonce.encode()).hexdigest():
                raise ControlConflict("unknown host challenge")
            host = await one(conn, "SELECT * FROM execution_hosts WHERE id = ?", (host_id,))
            if host is None or host["identity_state"] == "rejected" or challenge["generation"] != host["generation"]:
                raise ControlConflict("host challenge belongs to an earlier or rejected identity")
            if challenge["used_at"]:
                if challenge["signature_digest"] == digest:
                    response = json.loads(challenge["response_json"])
                    if response["identity_state"] != host["identity_state"]:
                        raise ControlConflict("host identity changed after this observation")
                    return response
                raise ControlConflict("host challenge was already used")
            if datetime.fromisoformat(challenge["expires_at"]) <= datetime.now(UTC):
                raise ControlConflict("host challenge expired")
            observed_pin = fingerprint(public_key)
            if host["identity_state"] in ("pending", "rotating") and observed_pin != host["fingerprint"]:
                raise ControlConflict("the observed key is not the pinned key")
            if host["identity_state"] == "changed" and observed_pin != host["pending_fingerprint"]:
                raise ControlConflict("a different key change is already awaiting a decision")
            state = "verified" if observed_pin == host["fingerprint"] else "changed"
            observed_at = now()
            await conn.execute("UPDATE execution_hosts SET identity_state = ?,pending_public_key = ?,pending_fingerprint = ?,"
                               "observed_at = ?,entity_revision = entity_revision + 1 WHERE id = ?",
                               (state, public_key if state == "changed" else None,
                                observed_pin if state == "changed" else None, observed_at, host_id))
            await conn.execute("INSERT INTO execution_host_observations(id,host_id,generation,fingerprint,challenge_id,signature_digest,observed_at)"
                               " VALUES (?,?,?,?,?,?,?)", (secrets.token_hex(16), host_id, host["generation"], observed_pin,
                                                        challenge_id, digest, observed_at))
            response = {"host_id": host_id, "identity_state": state, "fingerprint": observed_pin,
                        "host_generation": host["generation"], "observed_at": observed_at,
                        "effects_allowed": False, "capabilities_state": "unknown"}
            await conn.execute("UPDATE execution_host_challenges SET used_at = ?,signature_digest = ?,response_json = ? WHERE id = ?",
                               (observed_at, digest, json.dumps(response, sort_keys=True), challenge_id))
            return response

    async def decide(self, principal: Principal, host_id: str, *, decision: str, reason: str,
                     observed_generation: int, expected_host_revision: int,
                     expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        if decision not in ("accept_rotation", "reject") or not reason.strip():
            raise ValueError("a rotation decision needs a reason")
        payload = {"host_id": host_id, "decision": decision, "reason": reason.strip(),
                   "observed_generation": observed_generation, "expected_host_revision": expected_host_revision}

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            host = await one(conn, "SELECT * FROM execution_hosts WHERE id = ?", (host_id,))
            if host is None:
                raise KeyError(host_id)
            if host["identity_state"] != "changed" or host["entity_revision"] != expected_host_revision or host["generation"] != observed_generation:
                raise ControlConflict("the host identity decision is stale")
            if decision == "accept_rotation":
                await conn.execute("UPDATE execution_hosts SET public_key = pending_public_key,fingerprint = pending_fingerprint,"
                                   "pending_public_key = NULL,pending_fingerprint = NULL,identity_state = 'rotating',"
                                   "generation = generation + 1,entity_revision = entity_revision + 1,observed_at = NULL WHERE id = ?", (host_id,))
                state, generation = "rotating", observed_generation + 1
            else:
                await conn.execute("UPDATE execution_hosts SET identity_state = 'rejected',entity_revision = entity_revision + 1,observed_at = NULL WHERE id = ?", (host_id,))
                state, generation = "rejected", observed_generation
            await conn.execute("INSERT INTO execution_host_decisions(id,host_id,observed_generation,old_fingerprint,new_fingerprint,"
                               "decision,reason,actor_id,receipt_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (mutation.object_id, host_id, observed_generation, host["fingerprint"], host["pending_fingerprint"],
                                decision, reason.strip(), principal.actor_id, mutation.receipt_id, now()))
            return {"host_id": host_id, "identity_state": state, "host_generation": generation,
                    "effects_allowed": False, "capabilities_state": "unknown"}

        return await self.control.mutate(principal, Scope("global", "global"), "host.identity.decide", client_operation_id,
                                         expected_collection_revision, Entity("collection", "global"), payload, effect)
