"""Files that travel between the operator, the orchestrators and staff, by handle.

A path means something in one environment only: the container's inbox of an orchestrator does not
exist on the host where a command-line member works, and a member's worktree on the host does not
exist in the container. So nothing in orchestration passes a path from one agent to another. A file
is kept here once — its bytes in the content-addressed blob store, its name and type in a row — and
agents pass its **handle**, ``att:<id>``, which means the same thing everywhere. The host turns a
handle into bytes where a member can open them (``daedalus.host.handoff``) and takes a member's files
back in as new handles.

A handle is usable in a **scope**: ``main`` (the main orchestrator) or a project's id. A file becomes
another scope's only by an explicit grant (the main orchestrator delegating it, a project reporting
it back), and every movement of a file — attached, shared, delivered, fetched, refused — is a row of
``file_transfers`` with who, what, where, how big and its hash.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from protocore.contracts.blob import BlobNotFoundError

from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database

FILES_TENANT = "files"
"""The blob store's tenant for these files: apart from the images a model is shown, so neither
listing nor cleanup of one touches the other."""
FILE_MAX_BYTES = 50 << 20
"""The largest file handed on. The same bound the host terminal daemon sets on a file it writes into
an inbox; a bigger one belongs in a project folder, where members read it in place."""
HANDOVER_MAX_FILES = 20
"""Files in one hand-over. More is a directory, which is better named as a folder than copied."""
NAME_MAX = 200
MAIN = "main"
"""The main orchestrator's scope; every other scope is a project's id."""
HANDLE_RE = re.compile(r"\batt:([0-9a-f]{12})\b")
_BARE_RE = re.compile(r"^[0-9a-f]{12}$")
ORIGINS = ("operator", "staff", "orchestrator", "folder", "dispatcher", "browser", "schedule")
ACTIONS = ("attached", "shared", "delivered", "fetched", "imported", "refused")


class FileRefused(ValueError):
    """A file that cannot be kept, found or passed on, said so the caller knows what to do instead."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def handle(file_id: str) -> str:
    return f"att:{file_id}"


def parse_handle(text: str) -> str | None:
    """The id in ``att:<id>`` (or a bare id), else ``None``: what a model typed for a handle."""
    raw = (text or "").strip()
    found = HANDLE_RE.search(raw)
    if found and (raw.startswith("att:") or raw == found.group(0)):
        return found.group(1)
    return raw if _BARE_RE.match(raw) else None


def safe_name(name: str) -> str:
    """A file name that is one plain segment: no directory, no control character, not a dot name
    that would hide it, never empty."""
    base = Path(str(name or "").replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    cleaned = cleaned.lstrip(".") or "file"
    return cleaned[:NAME_MAX]


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1 << 20:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1 << 20):.1f} MB"


@dataclass(frozen=True, slots=True)
class StoredFile:
    id: str
    name: str
    mime: str
    size: int
    sha256: str
    origin: str
    origin_ref: str
    created_at: str

    @property
    def handle(self) -> str:
        return handle(self.id)

    def line(self) -> str:
        """How a model is shown the file: its handle first, which is what it passes on."""
        return f"{self.handle} {self.name} ({self.mime}, {self.size} bytes)"

    def short(self) -> str:
        return f"{self.handle} {self.name} ({human_size(self.size)})"

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "handle": self.handle,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "origin": self.origin,
            "created_at": self.created_at,
        }


def file_ref(stored: StoredFile) -> dict[str, Any]:
    """A file as an event carries it (``daedalus.host.events.FileRef``)."""
    return {"id": stored.id, "name": stored.name, "mime": stored.mime, "size": stored.size}


def refs_line(refs: Any) -> str:
    """The files an event carries, as a line ends with them: ``— files: att:… name (size), …``."""
    items = [r for r in refs or [] if isinstance(r, dict) and r.get("id")]
    if not items:
        return ""
    return " — files: " + ", ".join(f"att:{r['id']} {r.get('name') or ''} ({human_size(int(r.get('size') or 0))})" for r in items)


def _file(row: Any) -> StoredFile:
    return StoredFile(
        id=row["id"],
        name=row["name"],
        mime=row["mime"],
        size=int(row["size"]),
        sha256=row["sha256"],
        origin=row["origin"],
        origin_ref=row["origin_ref"],
        created_at=row["created_at"],
    )


class FileStore:
    """The files of orchestration: rows here, bytes in the blob store."""

    def __init__(self, db: Database, blobs: FileBlobStore) -> None:
        self.db = db
        self.blobs = blobs

    # -- keeping ---------------------------------------------------------------------------------------

    async def add(self, data: bytes, *, name: str, mime: str = "", origin: str, origin_ref: str = "", scope: str, actor: str) -> StoredFile:
        """Keep bytes as a new file of ``scope``. The same bytes under the same name in the same scope
        are the same file: a message sent twice, or a report naming the file again, does not make a
        second handle."""
        if len(data) > FILE_MAX_BYTES:
            raise FileRefused(f"{safe_name(name)} is {human_size(len(data))}; files handed on are at most {human_size(FILE_MAX_BYTES)} — put a bigger one in a project folder")
        if origin not in ORIGINS:
            raise ValueError(f"origin is one of {', '.join(ORIGINS)}")
        clean = safe_name(name)
        kind = (mime or mimetypes.guess_type(clean)[0] or "application/octet-stream").split(";")[0].strip()
        digest = hashlib.sha256(data).hexdigest()
        existing = await self.db.fetchone(
            "SELECT f.* FROM files f JOIN file_access a ON a.file_id = f.id WHERE a.scope = ? AND f.sha256 = ? AND f.name = ? ORDER BY f.created_at LIMIT 1",
            (scope, digest, clean),
        )
        if existing is not None:
            stored = _file(existing)
            await self.read(stored)
            return stored
        await self.blobs.put(FILES_TENANT, data, content_type=kind)
        file_id = secrets.token_hex(6)
        at = _now()
        await self.db.execute(
            "INSERT INTO files(id, name, mime, size, sha256, origin, origin_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, clean, kind, len(data), digest, origin, origin_ref[:500], at),
        )
        await self.db.execute("INSERT INTO file_access(file_id, scope, added_at, added_by) VALUES (?, ?, ?, ?)", (file_id, scope, at, actor))
        stored = StoredFile(file_id, clean, kind, len(data), digest, origin, origin_ref[:500], at)
        await self.record(stored, "attached" if origin == "operator" else ("fetched" if origin == "staff" else "imported"), actor=actor, scope=scope, target=origin_ref)
        return stored

    async def add_path(self, path: Path, **kwargs: Any) -> StoredFile:
        size = path.stat().st_size
        if size > FILE_MAX_BYTES:
            raise FileRefused(f"{path.name} is {human_size(size)}; files handed on are at most {human_size(FILE_MAX_BYTES)} — put a bigger one in a project folder")
        return await self.add(path.read_bytes(), **kwargs)

    # -- finding -----------------------------------------------------------------------------------------

    async def get(self, file_id: str) -> StoredFile | None:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        return _file(row) if row is not None else None

    async def many(self, ids: Iterable[str]) -> list[StoredFile]:
        wanted = [i for i in dict.fromkeys(ids) if _BARE_RE.match(i)][:200]
        if not wanted:
            return []
        rows = await self.db.fetchall(f"SELECT * FROM files WHERE id IN ({','.join('?' * len(wanted))})", tuple(wanted))
        found = {row["id"]: _file(row) for row in rows}
        return [found[i] for i in wanted if i in found]

    async def scopes(self, file_id: str) -> list[str]:
        rows = await self.db.fetchall("SELECT scope FROM file_access WHERE file_id = ? ORDER BY added_at", (file_id,))
        return [str(r["scope"]) for r in rows]

    async def in_scope(self, text: str, scope: str) -> StoredFile:
        """The file a handle names, if ``scope`` may use it; otherwise a refusal that says what to do."""
        file_id = parse_handle(text)
        if file_id is None:
            raise FileRefused(f"{text!r} is not a file handle (att:<12 hex>)")
        row = await self.db.fetchone("SELECT f.* FROM files f JOIN file_access a ON a.file_id = f.id WHERE f.id = ? AND a.scope = ?", (file_id, scope))
        if row is None:
            where = "the main orchestrator's" if scope == MAIN else "this project's"
            raise FileRefused(f"{handle(file_id)} is not one of {where} files")
        return _file(row)

    async def in_scope_in(self, conn: Any, text: str, scope: str) -> StoredFile:
        """Resolve a scoped handle inside an existing domain transaction."""
        file_id = parse_handle(text)
        if file_id is None:
            raise FileRefused("the approved attachment is not a file handle")
        cursor = await conn.execute(
            "SELECT f.* FROM files f JOIN file_access a ON a.file_id=f.id"
            " WHERE f.id=? AND a.scope=?", (file_id, scope),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise FileRefused("the approved attachment is outside this project")
        return _file(row)

    async def listing(self, scope: str, *, limit: int = 30) -> list[StoredFile]:
        rows = await self.db.fetchall(
            "SELECT f.* FROM files f JOIN file_access a ON a.file_id = f.id WHERE a.scope = ? ORDER BY a.added_at DESC LIMIT ?",
            (scope, max(1, min(int(limit), 200))),
        )
        return [_file(r) for r in rows]

    async def read(self, stored: StoredFile) -> bytes:
        try:
            data = await self.blobs.get(FILES_TENANT, stored.sha256)
        except BlobNotFoundError as exc:
            raise FileRefused(f"{stored.handle} no longer has kept bytes") from exc
        if len(data) != stored.size or hashlib.sha256(data).hexdigest() != stored.sha256:
            raise FileRefused(f"{stored.handle} no longer matches its kept digest")
        return data

    def path_of(self, stored: StoredFile) -> Path:
        """Where the bytes are on this machine, for a copy that does not read them into memory."""
        return self.blobs.path_of(FILES_TENANT, stored.sha256)

    # -- passing on ------------------------------------------------------------------------------------------

    async def grant(self, stored: StoredFile, scope: str, *, actor: str, target: str = "") -> bool:
        """Make a file another scope's too; recorded as ``shared``. False when it was already."""
        had = await self.db.fetchone("SELECT 1 FROM file_access WHERE file_id = ? AND scope = ?", (stored.id, scope))
        if had is None:
            await self.db.execute("INSERT OR IGNORE INTO file_access(file_id, scope, added_at, added_by) VALUES (?, ?, ?, ?)", (stored.id, scope, _now(), actor))
        await self.record(stored, "shared", actor=actor, scope=scope, target=target)
        return had is None

    async def attach_to_task(self, task_id: str, files: Iterable[StoredFile], *, actor: str) -> None:
        for stored in files:
            await self.db.execute(
                "INSERT OR IGNORE INTO task_files(task_id, file_id, added_at, added_by) VALUES (?, ?, ?, ?)", (task_id, stored.id, _now(), actor)
            )

    async def of_task(self, task_id: str) -> list[StoredFile]:
        rows = await self.db.fetchall(
            "SELECT f.* FROM files f JOIN task_files t ON t.file_id = f.id WHERE t.task_id = ? ORDER BY t.added_at, f.name", (task_id,)
        )
        return [_file(r) for r in rows]

    # -- the audit ---------------------------------------------------------------------------------------------

    async def record(
        self,
        stored: StoredFile | None,
        action: str,
        *,
        actor: str,
        scope: str = "",
        target: str = "",
        env: str = "",
        detail: str = "",
        size: int | None = None,
        sha256: str = "",
    ) -> None:
        if action not in ACTIONS:
            raise ValueError(f"action is one of {', '.join(ACTIONS)}")
        await self.db.execute(
            "INSERT INTO file_transfers(at, file_id, action, actor, scope, target, env, size, sha256, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                stored.id if stored is not None else None,
                action,
                actor[:200],
                scope,
                target[:1000],
                env,
                stored.size if stored is not None and size is None else int(size or 0),
                stored.sha256 if stored is not None and not sha256 else sha256,
                detail[:1000],
            ),
        )

    async def transfers(self, file_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT at, action, actor, scope, target, env, size, sha256, detail FROM file_transfers WHERE file_id = ? ORDER BY id LIMIT ?", (file_id, limit)
        )
        return [dict(r) for r in rows]

    async def deliveries(self, file_id: str, target: str) -> int:
        """How many times these bytes were written to that member-local path, this time included."""
        row = await self.db.fetchone("SELECT count(*) AS n FROM file_transfers WHERE file_id = ? AND action = 'delivered' AND target = ?", (file_id, target))
        return int(row["n"]) if row else 0

    async def delivered(self, file_id: str, target: str) -> bool:
        """Whether these bytes were already written to that member-local path."""
        row = await self.db.fetchone("SELECT 1 FROM file_transfers WHERE file_id = ? AND action = 'delivered' AND target = ? LIMIT 1", (file_id, target))
        return row is not None


__all__ = [
    "FILE_MAX_BYTES",
    "HANDLE_RE",
    "HANDOVER_MAX_FILES",
    "MAIN",
    "FileRefused",
    "FileStore",
    "StoredFile",
    "file_ref",
    "handle",
    "human_size",
    "parse_handle",
    "refs_line",
    "safe_name",
]
