"""Durable, content-addressed records of provider requests, kept one version back.

A manifest exists for the cache-miss diagnosis, which compares a session's latest request with the
one before it and reads nothing older. Every request stored one, plus a blob per slot, and nothing
ever removed them: the blobs grew by gigabytes a day. So a session keeps its newest
:data:`KEEP_PER_SESSION` manifests and no others; the rest go, with every blob no remaining
manifest names.

Blobs are content-addressed and shared: one ``tools`` body is named by many manifests, and the same
bytes may have been stored by another part of the host under the same tenant. A blob is therefore
freed only when ``request_manifest_blobs`` holds no reference to it **and** its own metadata still
says ``kind = request_manifest``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.observability import RequestManifest

from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

KIND = "request_manifest"
"""The ``metadata.kind`` this store writes on its blobs, and the only kind it ever deletes."""

KEEP_PER_SESSION = 2
"""The latest request and the one it is compared with. The operator's call: older ones were never
read, and the migration that introduced the references keeps the same two."""

PRUNE_BATCH = 200
"""Manifests one pruning pass deletes. A session normally has exactly one too many after a write;
the bound matters only for a session still carrying its backlog."""

SWEEP_ROW_BATCH = 500
"""Rows one statement of the sweep deletes, so no write lock is held for long."""

SWEEP_BLOB_BATCH = 100
"""Blobs examined and deleted under the store's lock at a time. A request waits behind that lock
for at most one batch of unlinks, which keeps it far inside the core's handoff budget."""


@dataclass(frozen=True)
class SweepReport:
    rows: int
    blobs: int
    bytes: int
    pages: int
    seconds: float


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class RequestManifestStore:
    """Keep the exact request boundary without copying large prompt bodies into SQLite."""

    def __init__(self, db: Database, blobs: FileBlobStore, *, tenant_id: str) -> None:
        self.db = db
        self.blobs = blobs
        self.tenant_id = tenant_id
        # Held from a blob's existence check to its manifest's row being written, and from finding
        # a blob unreferenced to deleting it. Without it, a write that found a body already on disk
        # could insert its reference just after a pruning pass decided nobody named that body, and
        # the newest manifest would point at a deleted file.
        self._lock = asyncio.Lock()
        self._pruning: dict[str | None, asyncio.Task[None]] = {}
        self._again: set[str | None] = set()

    async def record_request_manifest(
        self,
        *,
        manifest: RequestManifest,
        manifest_id: str,
        bodies: Mapping[str, bytes],
    ) -> None:
        # The signature is the core's sink contract, keyword for keyword. A narrower one made every
        # call raise a TypeError the core logs and swallows, so no manifest was recorded at all.
        # `manifest_id` repeats `manifest.manifest_id`; the stored id is the manifest's own.
        identity = dict(manifest.identity)
        session_id = identity.get("session_id")
        async with self._lock:
            refs: dict[str, str] = {}
            for slot, body in bodies.items():
                digest = hashlib.sha256(body).hexdigest()
                # A body already on disk is not written again. Rewriting it fsynced the same tools
                # list twice per request, and replaced the metadata of a blob another part of the
                # host may have stored with the same bytes, claiming it for this store's pruning.
                if await self.blobs.exists(self.tenant_id, digest):
                    refs[slot] = digest
                    continue
                meta = await self.blobs.put(
                    self.tenant_id,
                    body,
                    content_type="application/json",
                    metadata={"kind": KIND, "slot": slot},
                )
                refs[slot] = meta.ref
            stored = manifest.with_blob_refs(refs)
            async with self.db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO request_manifests(manifest_id, run_id, session_id, attempt_id, request_sha256, model, manifest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(manifest_id) DO NOTHING",
                    (
                        stored.manifest_id,
                        identity.get("run_id"),
                        session_id,
                        stored.attempt_id,
                        stored.request_sha256,
                        stored.model,
                        stored.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted = cursor.rowcount > 0
                seq = cursor.lastrowid
                await cursor.close()
                if inserted:
                    await conn.executemany(
                        "INSERT OR IGNORE INTO request_manifest_blobs(manifest_seq, ref) VALUES (?, ?)",
                        [(seq, ref) for ref in sorted(set(refs.values()))],
                    )
        if inserted:
            self._schedule_prune(session_id)

    async def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        rows = await self.recent_for_session(session_id, limit=1)
        return rows[0] if rows else None

    async def recent_for_session(self, session_id: str, *, limit: int = KEEP_PER_SESSION) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT manifest FROM request_manifests WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, max(1, min(limit, KEEP_PER_SESSION))),
        )
        return [json.loads(row["manifest"]) for row in rows]

    # The pruning that follows a write. It runs as its own task: the core awaits the record call
    # between assembling a request and opening its stream, and nothing a pruning pass does is
    # something that request needs. Manifests without a session form one group of their own and
    # keep their newest two like any session, so an out-of-session call still has its comparison.

    def _schedule_prune(self, session_id: str | None) -> None:
        try:
            running = self._pruning.get(session_id)
            if running is not None and not running.done():
                # The running pass may already have looked; it looks once more before it ends.
                self._again.add(session_id)
                return
            self._pruning[session_id] = asyncio.create_task(self._prune_loop(session_id), name="request-manifest-prune")
        except Exception:  # noqa: BLE001 — a pruning that cannot start must not fail the request
            logger.exception("could not start pruning the request manifests of %s", session_id)

    async def _prune_loop(self, session_id: str | None) -> None:
        try:
            while True:
                self._again.discard(session_id)
                while await self.prune_session(session_id) >= PRUNE_BATCH:
                    await asyncio.sleep(0)
                if session_id not in self._again:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — pruning is housekeeping; the next write tries again
            logger.exception("pruning the request manifests of %s failed", session_id)
        finally:
            if self._pruning.get(session_id) is asyncio.current_task():
                del self._pruning[session_id]

    async def prune_session(self, session_id: str | None) -> int:
        """Delete up to :data:`PRUNE_BATCH` of the session's manifests beyond the newest
        :data:`KEEP_PER_SESSION`, and every blob they leave unreferenced; returns how many rows went."""
        rows = await self.db.fetchall(
            "SELECT seq FROM request_manifests WHERE session_id IS ? ORDER BY seq DESC LIMIT ? OFFSET ?",
            (session_id, PRUNE_BATCH, KEEP_PER_SESSION),
        )
        seqs = [int(row["seq"]) for row in rows]
        if not seqs:
            return 0
        await self._delete_rows(seqs)
        return len(seqs)

    async def forget_session(self, session_id: str) -> int:
        """Delete every manifest of a session that is being deleted, and the blobs only they named.

        Called before the session's own deletion: the rows would go by cascade anyway, but inside a
        transaction this store never sees, and with them the references that say which blobs were
        theirs. Returns the bytes freed."""
        rows = await self.db.fetchall("SELECT seq FROM request_manifests WHERE session_id = ?", (session_id,))
        seqs = [int(row["seq"]) for row in rows]
        freed = 0
        for chunk in _chunks(seqs, SWEEP_ROW_BATCH):
            freed += await self._delete_rows(chunk)
        return freed

    async def _delete_rows(self, seqs: Sequence[int]) -> int:
        marks = ", ".join("?" for _ in seqs)
        refs = await self.db.fetchall(
            f"SELECT DISTINCT ref FROM request_manifest_blobs WHERE manifest_seq IN ({marks})", tuple(seqs)
        )
        # The references go with their rows by cascade.
        await self.db.execute(f"DELETE FROM request_manifests WHERE seq IN ({marks})", tuple(seqs))
        return await self._release([str(row["ref"]) for row in refs])

    async def _release(self, refs: Sequence[str]) -> int:
        """Delete each of ``refs`` that no manifest names and whose metadata says it is a manifest's."""
        freed = 0
        for chunk in _chunks(sorted(set(refs)), SWEEP_BLOB_BATCH):
            async with self._lock:
                marks = ", ".join("?" for _ in chunk)
                named = await self.db.fetchall(
                    f"SELECT DISTINCT ref FROM request_manifest_blobs WHERE ref IN ({marks})", tuple(chunk)
                )
                still = {str(row["ref"]) for row in named}
                for ref in chunk:
                    if ref not in still:
                        freed += self.blobs.delete_of_kind(self.tenant_id, ref, KIND)
        return freed

    async def sweep(self) -> SweepReport:
        """Bring the whole store inside its bounds: the one pass a start runs in the background.

        Deletes every manifest beyond each session's newest two and every manifest whose session is
        gone, then every manifest blob no remaining row names, including the ones no row ever named
        (a session deleted before this retention existed took its rows by cascade and left its
        blobs). Idempotent: on a store already inside its bounds it reads the listing and deletes
        nothing. Works in small statements and small locked batches so neither the database nor a
        request in flight waits on it for long."""
        started = time.monotonic()
        rows = await self.db.fetchall(
            "SELECT seq FROM (SELECT seq, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq DESC) AS newest FROM request_manifests) "
            "WHERE newest > ? "
            "UNION SELECT seq FROM request_manifests AS r WHERE r.session_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = r.session_id)",
            (KEEP_PER_SESSION,),
        )
        seqs = [int(row["seq"]) for row in rows]
        for chunk in _chunks(seqs, SWEEP_ROW_BATCH):
            marks = ", ".join("?" for _ in chunk)
            await self.db.execute(f"DELETE FROM request_manifests WHERE seq IN ({marks})", tuple(chunk))
            await asyncio.sleep(0)

        blobs = 0
        freed = 0
        listed = await asyncio.to_thread(self.blobs.described_refs, self.tenant_id)
        for chunk in _chunks(listed, SWEEP_BLOB_BATCH):
            candidates = await asyncio.to_thread(self._manifest_blobs_among, chunk)
            if not candidates:
                continue
            async with self._lock:
                marks = ", ".join("?" for _ in candidates)
                named = await self.db.fetchall(
                    f"SELECT DISTINCT ref FROM request_manifest_blobs WHERE ref IN ({marks})", tuple(candidates)
                )
                still = {str(row["ref"]) for row in named}
                for ref in candidates:
                    if ref in still:
                        continue
                    size = self.blobs.delete_of_kind(self.tenant_id, ref, KIND)
                    if size:
                        blobs += 1
                        freed += size
            await asyncio.sleep(0)

        # The rows freed pages inside the file. With incremental auto-vacuum they are handed back in
        # bounded passes; a full VACUUM would hold the connection for the whole rewrite of a
        # gigabytes-large file, which a start must never do.
        pages = 0
        if seqs:
            while reclaimed := await self.db.reclaim():
                pages += reclaimed
                await asyncio.sleep(0.05)
        return SweepReport(rows=len(seqs), blobs=blobs, bytes=freed, pages=pages, seconds=time.monotonic() - started)

    def _manifest_blobs_among(self, refs: Sequence[str]) -> list[str]:
        return [ref for ref in refs if self.blobs.kind_of(self.tenant_id, ref)[0] == KIND]

    async def settle(self) -> None:
        """Wait for every pruning pass started so far."""
        while pending := [task for task in self._pruning.values() if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Cancel the pruning still running; what it did not reach, the next write or start does."""
        tasks = [task for task in self._pruning.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pruning.clear()


__all__ = ["KEEP_PER_SESSION", "KIND", "RequestManifestStore", "SweepReport"]
