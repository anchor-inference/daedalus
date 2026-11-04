"""Content-addressed blob store on the local disk."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from protocore.contracts.blob import BlobNotFoundError, IBlobStore
from protocore.contracts.types import BlobMetadata

WINDOWS = os.name == "nt"


def sync_directory(directory: Path | str) -> None:
    """Make a rename into ``directory`` durable, where the platform lets a directory be flushed.

    Windows refuses ``os.open`` on a directory with ``PermissionError``: a directory handle there
    needs ``CreateFile`` with backup semantics, which ``os.open`` cannot ask for, and NTFS journals
    the rename itself. Opening it anyway failed every blob write on a native Windows install: an
    image pasted into the composer answered 500, and every picture a vision model was asked about
    died before it left the machine, which the agent read as the vision model not taking images.
    """
    if WINDOWS:
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".blob-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FileBlobStore(IBlobStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _paths(self, tenant_id: str, ref: str) -> tuple[Path, Path]:
        safe = "".join(ch for ch in ref if ch.isalnum() or ch in "-_.")
        base = self._root / tenant_id
        return base / safe, base / (safe + ".meta.json")

    async def put(
        self,
        tenant_id: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> BlobMetadata:
        ref = hashlib.sha256(content).hexdigest()
        data_path, meta_path = self._paths(tenant_id, ref)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if not data_path.exists():
            _atomic_write(data_path, content)
        elif hashlib.sha256(data_path.read_bytes()).hexdigest() != ref:
            raise ValueError("a stored blob no longer matches its digest")
        meta = BlobMetadata(
            ref=ref,
            sha256=ref,
            tenant_id=tenant_id,
            content_type=content_type,
            size_bytes=len(content),
            created_at=datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        _atomic_write(meta_path, meta.model_dump_json().encode("utf-8"))
        return meta

    async def get(self, tenant_id: str, ref: str) -> bytes:
        data_path, _ = self._paths(tenant_id, ref)
        if not data_path.exists():
            raise BlobNotFoundError(ref)
        return data_path.read_bytes()

    async def get_stream(self, tenant_id: str, ref: str) -> AsyncIterator[bytes]:
        data_path, _ = self._paths(tenant_id, ref)
        if not data_path.exists():
            raise BlobNotFoundError(ref)
        with data_path.open("rb") as fh:
            while chunk := fh.read(1 << 16):
                yield chunk

    async def head(self, tenant_id: str, ref: str) -> BlobMetadata:
        _, meta_path = self._paths(tenant_id, ref)
        if not meta_path.exists():
            raise BlobNotFoundError(ref)
        return BlobMetadata.model_validate_json(meta_path.read_text())

    async def exists(self, tenant_id: str, ref: str) -> bool:
        data_path, _ = self._paths(tenant_id, ref)
        return data_path.exists()

    async def delete(self, tenant_id: str, ref: str) -> bool:
        data_path, meta_path = self._paths(tenant_id, ref)
        existed = data_path.exists()
        for p in (data_path, meta_path):
            if p.exists():
                p.unlink()
        return existed

    async def list_prefix(
        self, tenant_id: str, prefix: str = "", *, limit: int = 100
    ) -> Sequence[BlobMetadata]:
        base = self._root / tenant_id
        if not base.exists():
            return []
        out: list[BlobMetadata] = []
        for meta_path in sorted(base.glob("*.meta.json")):
            meta = BlobMetadata.model_validate(json.loads(meta_path.read_text()))
            if meta.ref.startswith(prefix):
                out.append(meta)
            if len(out) >= limit:
                break
        return out

    def path_of(self, tenant_id: str, ref: str) -> Path:
        return self._paths(tenant_id, ref)[0]

    def described_refs(self, tenant_id: str) -> list[str]:
        """Every ref in the tenant that has a metadata file, read from the directory listing alone.

        Synchronous on purpose: a tenant holding a hundred thousand blobs is listed in a worker
        thread by the caller, never on the event loop."""
        base = self._root / tenant_id
        if not base.is_dir():
            return []
        suffix = ".meta.json"
        with os.scandir(base) as entries:
            return [entry.name[: -len(suffix)] for entry in entries if entry.name.endswith(suffix) and not entry.name.startswith(".")]

    def kind_of(self, tenant_id: str, ref: str) -> tuple[str | None, int]:
        """The ``kind`` a blob's own metadata names, and the bytes its data and metadata take.

        ``(None, 0)`` when there is no readable metadata: a blob nobody described belongs to nobody
        this store can name, and every caller treats it as not theirs."""
        data_path, meta_path = self._paths(tenant_id, ref)
        try:
            meta = json.loads(meta_path.read_bytes())
            size = meta_path.stat().st_size + (data_path.stat().st_size if data_path.exists() else 0)
        except (OSError, ValueError):
            return None, 0
        kind = (meta.get("metadata") or {}).get("kind") if isinstance(meta, dict) else None
        return (kind if isinstance(kind, str) else None), size

    def delete_of_kind(self, tenant_id: str, ref: str, kind: str) -> int:
        """Delete a blob and its metadata only when that metadata still names ``kind``; returns the
        bytes freed, 0 when the blob was kept or was already gone.

        A digest is shared by every writer of the same bytes, and the metadata is whichever writer
        came last, so the kind is read again at the moment of deletion rather than trusted from an
        earlier scan: a blob some other part of the host wrote is never removed for being unreferenced
        by the caller's records."""
        found, size = self.kind_of(tenant_id, ref)
        if found != kind:
            return 0
        data_path, meta_path = self._paths(tenant_id, ref)
        for path in (data_path, meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return size


__all__ = ["FileBlobStore"]
