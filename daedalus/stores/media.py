"""Immutable media prepared by an agent for a final answer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import stat
import struct
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database

MEDIA_REF_RE = re.compile(r"!\[(?P<alt>[^\]\n]{0,500})\]\(daedalus-media:(?P<id>[0-9a-f-]{36})\)")
MAX_ITEMS = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AV_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MEDIA_TENANT = "inline-media"


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        at += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if at + 2 > len(data):
            return None
        length = int.from_bytes(data[at : at + 2], "big")
        if length < 2 or at + length > len(data):
            return None
        if marker in range(0xC0, 0xC4):
            return int.from_bytes(data[at + 5 : at + 7], "big"), int.from_bytes(data[at + 3 : at + 5], "big")
        at += length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _probe(head: bytes) -> tuple[str, str, int | None, int | None]:
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return "image", "image/png", width, height
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        return "animation", "image/gif", width, height
    if head.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(head)
        return "image", "image/jpeg", *(size or (None, None))
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        size = _webp_size(head)
        return "image", "image/webp", *(size or (None, None))
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video", "video/mp4", None, None
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm", None, None
    if head.startswith(b"OggS"):
        return "audio", "audio/ogg", None, None
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio", "audio/wav", None, None
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio", "audio/mpeg", None, None
    raise ValueError("unsupported or damaged media file")


# The link is not fetched. The extension is the only hint that does not require a download;
# a link with none of these says its kind explicitly.
_LINK_KINDS = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
    ".gif": ("animation", "image/gif"),
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"),
    ".ogg": ("audio", "audio/ogg"),
    ".mp3": ("audio", "audio/mpeg"),
    ".wav": ("audio", "audio/wav"),
}
_KIND_MIME = {"image": "image/jpeg", "animation": "image/gif", "video": "video/mp4", "audio": "audio/mpeg"}
_MEDIA_SUFFIXES = frozenset(_LINK_KINDS)


def _file_kind(path: Path) -> tuple[str, str, int | None, int | None]:
    """A file that is no picture, clip or sound: a document, a spreadsheet, an archive, code. It is
    kept as it is and drawn as a file card; nothing is probed, since nothing is decoded."""
    return "file", mimetypes.guess_type(path.name)[0] or "application/octet-stream", None, None


def _admit_link(url: str, kind_hint: str, alt: str, caption: str) -> dict[str, Any]:
    """A remote item is stored as the URL itself. Nothing is downloaded."""
    alt, caption = alt.strip(), caption.strip()
    if len(alt) > 500 or len(caption) > 1000:
        raise ValueError("alt or caption is too long")
    if len(url) > 2000:
        raise ValueError("media link is too long")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("a media link must be an http(s) address without credentials")
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix in _LINK_KINDS:
        kind, mime_type = _LINK_KINDS[suffix]
    elif kind_hint in _KIND_MIME:
        kind, mime_type = kind_hint, _KIND_MIME[kind_hint]
    else:
        raise ValueError("a link needs a media extension, or a kind: image, video, animation, audio")
    name = Path(unquote(parsed.path)).name or "media"
    return {"blob_ref": "", "source_url": url, "kind": kind, "mime_type": mime_type, "filename": name[:200], "byte_size": 0, "width": None, "height": None, "alt": alt, "caption": caption}


class MediaStore:
    """Stages immutable blobs, then binds referenced presentations to one persisted answer."""

    def __init__(self, db: Database, blobs: FileBlobStore) -> None:
        self.db = db
        self.blobs = blobs
        self._lock = asyncio.Lock()

    async def stage(
        self,
        session_id: str,
        run_id: str,
        items: list[dict[str, str]],
        *,
        layout: str,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._prune_staged_locked(keep_hours=24)
            return await self._stage(session_id, run_id, items, layout=layout)

    async def _stage(
        self,
        session_id: str,
        run_id: str,
        items: list[dict[str, str]],
        *,
        layout: str,
    ) -> dict[str, Any]:
        if layout not in ("single", "album"):
            raise ValueError("layout must be single or album")
        if not run_id:
            raise ValueError("inline media can only be prepared during an active run")
        if not items or len(items) > MAX_ITEMS:
            raise ValueError(f"attach between 1 and {MAX_ITEMS} items")
        if layout == "single" and len(items) != 1:
            raise ValueError("single layout accepts exactly one item")
        admitted = []
        for item in items:
            url = str(item.get("url") or "").strip()
            if url:
                admitted.append(_admit_link(url, str(item.get("kind") or ""), item.get("alt", ""), item.get("caption", "")))
            else:
                admitted.append(await asyncio.to_thread(self._admit, Path(item["path"]), item.get("alt", ""), item.get("caption", ""), str(item.get("kind") or "")))
        if layout == "album" and (len(admitted) < 2 or any(item["kind"] not in ("image", "video", "animation") for item in admitted)):
            raise ValueError("an album needs 2-10 images, videos or animations; audio and other files are attached one per call")
        presentation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO media_presentations(id, session_id, run_id, layout, state, created_at) VALUES (?, ?, ?, ?, 'staged', ?)",
                (presentation_id, session_id, run_id, layout, now),
            )
            await conn.executemany(
                "INSERT INTO media_items(id, presentation_id, ordinal, blob_ref, source_url, kind, mime_type, filename, byte_size, width, height, alt, caption)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(uuid.uuid4()), presentation_id, index, item["blob_ref"], item.get("source_url", ""), item["kind"], item["mime_type"],
                        item["filename"], item["byte_size"], item["width"], item["height"], item["alt"], item["caption"],
                    )
                    for index, item in enumerate(admitted)
                ],
            )
        alt = admitted[0]["alt"] or admitted[0]["caption"] or admitted[0]["filename"]
        return {"id": presentation_id, "kind": "album" if layout == "album" else admitted[0]["kind"], "markdown": f"![{alt}](daedalus-media:{presentation_id})"}

    def _admit(self, path: Path, alt: str, caption: str, kind_hint: str = "") -> dict[str, Any]:
        alt = alt.strip()
        caption = caption.strip()
        if len(alt) > 500 or len(caption) > 1000:
            raise ValueError("alt or caption is too long")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("media source must be a regular file")
            head = os.read(fd, 64 * 1024)
            if kind_hint == "file":
                kind, mime_type, width, height = _file_kind(path)
            else:
                try:
                    kind, mime_type, width, height = _probe(head)
                except ValueError:
                    # A file named as media that does not decode is a broken picture, said so; any
                    # other file is a document the answer carries as a card.
                    if path.suffix.lower() in _MEDIA_SUFFIXES:
                        raise
                    kind, mime_type, width, height = _file_kind(path)
            limit = MAX_FILE_BYTES if kind == "file" else MAX_IMAGE_BYTES if kind in ("image", "animation") else MAX_AV_BYTES
            if kind in ("image", "animation") and (not width or not height or width * height > MAX_IMAGE_PIXELS):
                raise ValueError("image dimensions are missing or exceed the 40 megapixel limit")
            if before.st_size > limit:
                raise ValueError(f"{path.name} is larger than the {limit // (1024 * 1024)} MiB limit")
            os.lseek(fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            target_dir = self.blobs.path_of(MEDIA_TENANT, "x").parent
            target_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".media-", delete=False) as out:
                temporary = Path(out.name)
                while chunk := os.read(fd, 1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            after = os.fstat(fd)
            stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if not stable:
                temporary.unlink(missing_ok=True)
                raise ValueError("media file changed while it was being attached")
            ref = digest.hexdigest()
            data_path, meta_path = self.blobs._paths(MEDIA_TENANT, ref)
            if data_path.exists():
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, data_path)
            if not meta_path.exists():
                meta_path.write_text(json.dumps({"ref": ref, "sha256": ref, "tenant_id": MEDIA_TENANT, "content_type": mime_type, "size_bytes": before.st_size, "created_at": datetime.now(UTC).isoformat(), "metadata": {"filename": path.name}}))
            return {"blob_ref": ref, "source_url": "", "kind": kind, "mime_type": mime_type, "filename": path.name, "byte_size": before.st_size, "width": width, "height": height, "alt": alt, "caption": caption}
        finally:
            os.close(fd)

    async def bind_message(self, conn: Any, session_id: str, message_key: str, message: Message) -> Message:
        if message.role is not MessageRole.assistant:
            return message
        ids = list(dict.fromkeys(match.group("id") for match in MEDIA_REF_RE.finditer(message.text)))
        if not ids:
            return message
        run_id = str(message.metadata.get("daedalus.run_id") or "")
        if not run_id:
            return message
        marks = ",".join("?" for _ in ids)
        cursor = await conn.execute(
            f"SELECT id FROM media_presentations WHERE session_id = ? AND run_id = ? AND state = 'staged' AND id IN ({marks})",
            (session_id, run_id, *ids),
        )
        allowed = {str(row["id"]) for row in await cursor.fetchall()}
        if allowed != set(ids):
            return message
        await conn.execute(
            f"UPDATE media_presentations SET state = 'ready', message_key = ? WHERE session_id = ? AND id IN ({marks})",
            (message_key, session_id, *ids),
        )
        presentations = await self._views(conn, session_id, ids)
        message.metadata["daedalus.media"] = presentations
        return message

    async def _views(self, conn: Any, session_id: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        cursor = await conn.execute(
            f"SELECT p.id presentation_id, p.layout, i.* FROM media_presentations p JOIN media_items i ON i.presentation_id = p.id"
            f" WHERE p.session_id = ? AND p.id IN ({marks}) ORDER BY p.created_at, i.ordinal",
            (session_id, *ids),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in await cursor.fetchall():
            pid = str(row["presentation_id"])
            view = {key: row[key] for key in ("id", "kind", "mime_type", "filename", "byte_size", "width", "height", "alt", "caption")}
            view["url"] = str(row["source_url"] or "")
            grouped.setdefault(pid, {"id": pid, "layout": row["layout"], "items": []})["items"].append(view)
        return [grouped[item] for item in ids if item in grouped]

    async def item(self, session_id: str, presentation_id: str, item_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT i.*, p.message_key FROM media_items i JOIN media_presentations p ON p.id = i.presentation_id"
            " WHERE p.session_id = ? AND p.id = ? AND i.id = ? AND p.state = 'ready'"
            " AND EXISTS (SELECT 1 FROM transcript t WHERE t.session_id = p.session_id AND t.key = p.message_key)",
            (session_id, presentation_id, item_id),
        )
        return dict(row) if row else None

    async def ready_for_run(self, session_id: str, run_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id FROM media_presentations WHERE session_id = ? AND run_id = ? AND state = 'ready' ORDER BY created_at",
            (session_id, run_id),
        )
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return []
        async with self.db.transaction() as conn:
            views = await self._views(conn, session_id, ids)
        for presentation in views:
            for item in presentation["items"]:
                row = await self.db.fetchone("SELECT blob_ref, source_url FROM media_items WHERE id = ?", (item["id"],))
                url = str(row["source_url"] or "") if row else ""
                item["url"] = url
                item["path"] = "" if url or not row else str(self.blobs.path_of(MEDIA_TENANT, str(row["blob_ref"])))
        return views

    async def prune_staged(self, *, keep_hours: int = 24) -> int:
        """Remove media that never became part of an answer, including unreferenced blob bytes."""
        async with self._lock:
            return await self._prune_staged_locked(keep_hours=keep_hours)

    async def _prune_staged_locked(self, *, keep_hours: int) -> int:
        before = (datetime.now(UTC) - timedelta(hours=keep_hours)).isoformat()
        rows = await self.db.fetchall(
            "SELECT DISTINCT i.blob_ref FROM media_items i JOIN media_presentations p ON p.id = i.presentation_id"
            " WHERE p.state = 'staged' AND p.created_at < ?",
            (before,),
        )
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM media_presentations WHERE state = 'staged' AND created_at < ?", (before,))
            dropped = max(0, int(cursor.rowcount or 0))
        for row in rows:
            ref = str(row["blob_ref"])
            # A link has no bytes. An empty ref is shared by every remote item, so it is not a blob.
            if not ref:
                continue
            if await self.db.fetchone("SELECT 1 FROM media_items WHERE blob_ref = ? LIMIT 1", (ref,)) is None:
                await self.blobs.delete(MEDIA_TENANT, ref)
        return dropped

    async def drop_tail(self, conn: Any, session_id: str, seq: int) -> None:
        await conn.execute(
            "DELETE FROM media_presentations WHERE session_id = ? AND message_key IN"
            " (SELECT key FROM transcript WHERE session_id = ? AND seq >= ?)",
            (session_id, session_id, seq),
        )

    async def clone_references(self, source_session: str, target_session: str, message: Message) -> Message:
        """Give an explicit fork its own scoped presentations over the same immutable blobs."""
        media = message.metadata.get("daedalus.media") if isinstance(message.metadata, dict) else None
        ids = [str(item.get("id")) for item in media or [] if isinstance(item, dict) and item.get("id")]
        if not ids:
            return message
        replacements: dict[str, str] = {}
        async with self.db.transaction() as conn:
            for old_id in ids:
                row = await (await conn.execute(
                    "SELECT layout FROM media_presentations WHERE id = ? AND session_id = ? AND state = 'ready'",
                    (old_id, source_session),
                )).fetchone()
                if row is None:
                    continue
                new_id = str(uuid.uuid4())
                replacements[old_id] = new_id
                await conn.execute(
                    "INSERT INTO media_presentations(id, session_id, run_id, layout, state, created_at) VALUES (?, ?, ?, ?, 'staged', ?)",
                    (new_id, target_session, str(message.metadata.get("daedalus.run_id") or "fork"), row["layout"], datetime.now(UTC).isoformat()),
                )
                items = await (await conn.execute("SELECT * FROM media_items WHERE presentation_id = ? ORDER BY ordinal", (old_id,))).fetchall()
                await conn.executemany(
                    "INSERT INTO media_items(id, presentation_id, ordinal, blob_ref, source_url, kind, mime_type, filename, byte_size, width, height, alt, caption)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(str(uuid.uuid4()), new_id, item["ordinal"], item["blob_ref"], item["source_url"], item["kind"], item["mime_type"], item["filename"], item["byte_size"], item["width"], item["height"], item["alt"], item["caption"]) for item in items],
                )
        if not replacements:
            return message
        blocks = []
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                text = block.text
                for old_id, new_id in replacements.items():
                    text = text.replace(f"daedalus-media:{old_id}", f"daedalus-media:{new_id}")
                block = block.model_copy(update={"text": text})
            blocks.append(block)
        metadata = {key: value for key, value in message.metadata.items() if key not in ("daedalus.seq", "daedalus.media")}
        return message.model_copy(update={"content_blocks": tuple(blocks), "metadata": metadata})


__all__ = ["MEDIA_REF_RE", "MEDIA_TENANT", "MediaStore"]
