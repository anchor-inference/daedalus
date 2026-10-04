"""Pin the report and reviewed database evidence carried into a branch merge."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import aiosqlite


async def source_identity(conn: aiosqlite.Connection, result_id: str, verdict_id: str) -> dict[str, Any]:
    """Hash the exact report, verdict, evidence, and artifact records selected for merge."""
    async def rows(sql: str, args: tuple[str, ...]) -> list[dict[str, Any]]:
        async with conn.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    result = await rows("SELECT * FROM result_receipts WHERE id = ?", (result_id,))
    verdict = await rows("SELECT * FROM review_verdicts WHERE id = ? AND result_id = ?",
                         (verdict_id, result_id))
    if len(result) != 1 or len(verdict) != 1:
        raise ValueError("merge source result or verdict is missing")
    evidence_ids = json.loads(verdict[0]["evidence_json"])
    if not isinstance(evidence_ids, list) or not all(isinstance(item, str) for item in evidence_ids):
        raise ValueError("merge verdict evidence is invalid")
    evidence = []
    for evidence_id in evidence_ids:
        found = await rows("SELECT * FROM review_evidence WHERE id = ? AND result_id = ?",
                           (evidence_id, result_id))
        if len(found) != 1:
            raise ValueError("merge verdict evidence is missing")
        evidence.extend(found)
    artifacts = await rows("SELECT m.* FROM result_artifacts a JOIN artifact_manifests m"
                           " ON m.id = a.manifest_id WHERE a.result_id = ? ORDER BY m.id", (result_id,))
    if not artifacts:
        raise ValueError("merge source artifacts are missing")
    source = {"result": result[0], "verdict": verdict[0], "evidence": evidence, "artifacts": artifacts}
    digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"digest": digest, "report_digest": result[0]["original_digest"],
            "report_size": result[0]["original_size_bytes"], "snapshot": source}


def stage_report(root: Path, action_id: str, original: bytes, digest: str, size: int) -> Path:
    """Keep a private copy by action so later loss of the report blob cannot erase the merge source."""
    if len(original) != size or hashlib.sha256(original).hexdigest() != digest:
        raise ValueError("merge source report bytes changed")
    if len(action_id) != 32 or any(letter not in "0123456789abcdef" for letter in action_id):
        raise ValueError("invalid merge source action")
    directory = root / "merge-sources"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / action_id
    if path.exists():
        kept = path.read_bytes()
        if len(kept) != size or hashlib.sha256(kept).hexdigest() != digest:
            raise ValueError("kept merge source report bytes changed")
        return path
    descriptor, temporary = tempfile.mkstemp(prefix=f".{action_id}-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o400)
        try:
            os.link(temporary, path)
        except FileExistsError:
            kept = path.read_bytes()
            if len(kept) != size or hashlib.sha256(kept).hexdigest() != digest:
                raise ValueError("kept merge source report bytes changed") from None
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        os.unlink(temporary)
    return path


def file_identity(path: Path) -> tuple[str, int]:
    """Hash a kept artifact without loading a potentially large file into the host process."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
