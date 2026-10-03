"""A review gate for activating locally authored instruction skills.

The gate checks an exact Markdown digest and dependency pins. It does not execute a skill's text or
claim that a static example proves its behavior; the operator's activation is recorded separately.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from daedalus.host.skills import DirectorySkillStore, parse_skill_markdown
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database

SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class SkillRefused(ValueError):
    """The authored skill does not meet the activation contract."""


def assess(skill_id: str, markdown: str, dependencies: list[dict[str, str]]) -> dict[str, Any]:
    errors: list[str] = []
    if not SKILL_ID.fullmatch(skill_id):
        errors.append("invalid skill id")
    if not 80 <= len(markdown) <= 60_000:
        errors.append("skill Markdown length is outside the supported range")
    meta, body = parse_skill_markdown(markdown)
    if not meta.get("name") or not 12 <= len(meta.get("description", "")) <= 500:
        errors.append("name and descriptive purpose are required")
    if "## When to use" not in body or "## Procedure" not in body or "## Checks" not in body:
        errors.append("usage, procedure and checks sections are required")
    if not isinstance(dependencies, list) or len(dependencies) > 16:
        errors.append("too many dependencies")
    else:
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != {"id", "version", "digest"} or not all(isinstance(value, str) and value for value in dependency.values()) or not re.fullmatch(r"[0-9a-f]{64}", dependency["digest"]):
                errors.append("dependencies need exact id, version and digest pins")
                break
    return {"valid": not errors, "errors": errors, "digest": hashlib.sha256(markdown.encode()).hexdigest()}


class SkillQuality:
    def __init__(self, db: Database, directory: DirectorySkillStore) -> None:
        self.db = db
        self.directory = directory

    async def submit(self, skill_id: str, version: str, markdown: str, dependencies: list[dict[str, str]]) -> dict[str, Any]:
        check = assess(skill_id, markdown, dependencies)
        if not check["valid"] or not re.fullmatch(r"[0-9][a-z0-9_.-]{0,63}", version):
            raise SkillRefused("; ".join(check["errors"]) or "invalid version")
        manifest = {"id": skill_id, "version": version, "digest": check["digest"], "markdown": markdown, "dependencies": dependencies}
        await self.db.execute(
            "INSERT INTO skill_manifests(id, version, digest, manifest, status, created_at) "
            "VALUES (?, ?, ?, ?, 'candidate', ?)",
            (skill_id, version, check["digest"], json.dumps(manifest, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        return {"id": skill_id, "version": version, "status": "candidate", **check}

    async def submit_command(
        self, principal: Principal, skill_id: str, version: str, markdown: str,
        dependencies: list[dict[str, str]], *, expected_collection_revision: int,
        client_operation_id: str,
    ) -> dict[str, Any]:
        check = assess(skill_id, markdown, dependencies)
        if not check["valid"] or not re.fullmatch(r"[0-9][a-z0-9_.-]{0,63}", version):
            raise SkillRefused("; ".join(check["errors"]) or "invalid version")
        manifest = {"id": skill_id, "version": version, "digest": check["digest"], "markdown": markdown, "dependencies": dependencies}

        async def effect(conn: Any, _mutation: Any) -> dict[str, Any]:
            await conn.execute(
                "INSERT INTO skill_manifests(id, version, digest, manifest, status, created_at) "
                "VALUES (?, ?, ?, ?, 'candidate', ?)",
                (skill_id, version, check["digest"], json.dumps(manifest, sort_keys=True), datetime.now(UTC).isoformat()),
            )
            return {"id": skill_id, "version": version, "status": "candidate", **check}

        return await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "skill.submit", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"id": skill_id, "version": version, "digest": check["digest"], "dependencies": dependencies}, effect,
        )

    async def activate_command(
        self, principal: Principal, skill_id: str, version: str, expected_digest: str, *,
        expected_collection_revision: int, client_operation_id: str,
    ) -> dict[str, Any]:
        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            cursor = await conn.execute(
                "SELECT digest, manifest, status FROM skill_manifests WHERE id = ? AND version = ?",
                (skill_id, version),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["status"] != "candidate" or row["digest"] != expected_digest:
                raise SkillRefused("candidate or digest changed")
            manifest = json.loads(row["manifest"])
            for dependency in manifest["dependencies"]:
                cursor = await conn.execute(
                    "SELECT 1 FROM skill_manifests WHERE id = ? AND version = ? AND digest = ? AND status = 'active'",
                    (dependency["id"], dependency["version"], dependency["digest"]),
                )
                pinned = await cursor.fetchone()
                await cursor.close()
                if pinned is None:
                    raise SkillRefused("dependency pin is not active")
            if hashlib.sha256(manifest["markdown"].encode()).hexdigest() != expected_digest:
                raise SkillRefused("stored skill body changed")
            await conn.execute(
                "UPDATE skill_manifests SET status = 'staged' WHERE id = ? AND version = ?",
                (skill_id, version),
            )
            await conn.execute(
                "INSERT INTO effect_outbox(id, receipt_id, kind, payload_json, created_at) "
                "VALUES (?, ?, 'skill.publish', ?, ?)",
                (uuid.uuid4().hex, mutation.receipt_id, json.dumps({"id": skill_id, "version": version}), datetime.now(UTC).isoformat()),
            )
            return {"id": skill_id, "version": version, "status": "staged", "digest": expected_digest}

        response = await ControlStore(self.db).mutate(
            principal, Scope("global", "global"), "skill.activate", client_operation_id,
            expected_collection_revision, Entity("collection", "global"),
            {"id": skill_id, "version": version, "expected_digest": expected_digest}, effect,
            effects=("skill.publish",),
        )
        await self.reconcile()
        return response

    async def reconcile(self) -> None:
        completed = await self.db.fetchall(
            "SELECT s.id, s.digest, s.manifest FROM skill_manifests s "
            "JOIN effect_outbox o ON s.id = json_extract(o.payload_json, '$.id') "
            "AND s.version = json_extract(o.payload_json, '$.version') "
            "WHERE o.kind = 'skill.publish' AND o.state = 'completed' AND s.status = 'active'"
        )
        for row in completed:
            target = self.directory.root / row["id"]
            entry = target / "SKILL.md"
            if (target / ".published").exists() or not entry.is_file():
                continue
            if hashlib.sha256(entry.read_bytes()).hexdigest() == row["digest"]:
                (target / ".disabled").unlink(missing_ok=True)
                (target / ".published").touch()
        pending = await self.db.fetchall(
            "SELECT o.id outbox_id, s.id, s.version, s.digest, s.manifest "
            "FROM effect_outbox o JOIN skill_manifests s "
            "ON s.id = json_extract(o.payload_json, '$.id') "
            "AND s.version = json_extract(o.payload_json, '$.version') "
            "WHERE o.kind = 'skill.publish' AND o.state = 'pending' AND s.status = 'staged'"
        )
        for row in pending:
            target = self.directory.root / row["id"]
            manifest = json.loads(row["manifest"])
            if hashlib.sha256(manifest["markdown"].encode()).hexdigest() != row["digest"]:
                await self.db.execute(
                    "UPDATE effect_outbox SET state = 'unknown', error = 'skill digest mismatch' WHERE id = ?",
                    (row["outbox_id"],),
                )
                continue
            if target.exists():
                await self.db.execute(
                    "UPDATE effect_outbox SET state = 'unknown', error = 'skill directory collision' WHERE id = ?",
                    (row["outbox_id"],),
                )
                continue
            try:
                target.mkdir()
                (target / ".disabled").touch()
                (target / "SKILL.md").write_text(manifest["markdown"], encoding="utf-8")
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "UPDATE skill_manifests SET status = 'active' WHERE id = ? AND version = ? AND status = 'staged'",
                        (row["id"], row["version"]),
                    )
                    await conn.execute(
                        "UPDATE effect_outbox SET state = 'completed', completed_at = ? WHERE id = ? AND state = 'pending'",
                        (datetime.now(UTC).isoformat(), row["outbox_id"]),
                    )
                (target / ".disabled").unlink()
                (target / ".published").touch()
            except BaseException:
                await self.db.execute(
                    "UPDATE effect_outbox SET state = 'unknown', error = 'publication outcome requires review' WHERE id = ?",
                    (row["outbox_id"],),
                )
                continue

    async def activate(self, skill_id: str, version: str, expected_digest: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT digest, manifest, status FROM skill_manifests WHERE id = ? AND version = ?",
            (skill_id, version),
        )
        if row is None or row["status"] != "candidate" or row["digest"] != expected_digest:
            raise SkillRefused("candidate or digest changed")
        manifest = json.loads(row["manifest"])
        for dependency in manifest["dependencies"]:
            pinned = await self.db.fetchone(
                "SELECT 1 FROM skill_manifests WHERE id = ? AND version = ? AND digest = ? AND status = 'active'",
                (dependency["id"], dependency["version"], dependency["digest"]),
            )
            if pinned is None:
                raise SkillRefused("dependency pin is not active")
        if hashlib.sha256(manifest["markdown"].encode()).hexdigest() != expected_digest:
            raise SkillRefused("stored skill body changed")
        target = self.directory.root / skill_id
        if target.exists():
            raise SkillRefused("skill directory already exists; replacing it requires a separate review")
        target.mkdir()
        try:
            (target / ".disabled").touch()
            (target / "SKILL.md").write_text(manifest["markdown"], encoding="utf-8")
            await self.db.execute(
                "UPDATE skill_manifests SET status = 'active' WHERE id = ? AND version = ? AND status = 'candidate'",
                (skill_id, version),
            )
            (target / ".disabled").unlink()
        except BaseException:
            (target / "SKILL.md").unlink(missing_ok=True)
            (target / ".disabled").unlink(missing_ok=True)
            target.rmdir()
            raise
        return {"id": skill_id, "version": version, "status": "active", "digest": expected_digest}
