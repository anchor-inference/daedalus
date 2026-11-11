"""Files at the door: bodies arriving in pieces, checked whole, kept for a bounded time, then gone.

A file is announced first, with its name, size and sha256, and then sent in pieces of at most
:data:`CHUNK_BYTES`, each at the offset the door says it has reached. A dropped connection costs the piece
that was on the way and nothing else: the guest asks where the door got to and goes on from there. The
pieces are small because every proxy in front of the door has a body limit of its own, and the front holds
each one in a connection with a whole life of under a minute.

The door's disk is the operator's and a stranger fills it, so three bounds hold together: a link's limit per
file and per day (both ways, counted when a file is announced, so starting many at once cannot pass it), and
:data:`DISK_BYTES` for everything the door holds at once, where an announced file counts at its full size from
the moment it is announced. A file is kept for :data:`KEEP_HOURS` and no longer: one from a guest goes the
moment the host has its copy, one for a guest when its time is up, an upload nobody finished after
:data:`STALLED_HOURS`.

Nothing here opens a file by a name a guest chose. A body is ``<id>`` in one directory, and the id is the door's.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import AGENT, GUEST, NO_LIMIT, READY, UPLOADING, Store
from .text import one_line

CHUNK_BYTES = 512 * 1024
"""One piece. Under nginx's default body limit of a megabyte with room to spare, and small enough that the
front and the tunnel's router can each hold a few hundred at once in the memory they are given."""
MB = 1024 * 1024
FILE_MAX_BYTES = 4 * 1024 * MB
"""A file's size as anybody may announce it, whatever a link allows: a number past this is a mistake or a probe."""
DISK_BYTES = int(float(os.environ.get("GUEST_FILES_MAX_MB", "2048")) * MB)
"""Everything the door holds at once. The volume has no size of its own, and it is on the operator's disk."""
KEEP_HOURS = 72.0
STALLED_HOURS = 24.0
NAME_CHARS = 160
FILE_ID = re.compile(r"f_[0-9a-f]{16}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class FileRefused(Exception):
    """Why a file or a piece of one is not taken, in the words the guest's agent reads, and the status to send."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True, slots=True)
class Announced:
    name: str
    size: int
    sha256: str


def clean_name(value: Any) -> str:
    """A file name to show and to save under, never a path: the last part of whatever was sent, made inert."""
    text = one_line(str(value or ""), 400)
    text = re.split(r"[\\/]", text)[-1].strip().lstrip(".") or "file"
    return text[:NAME_CHARS]


def announced(body: Any) -> Announced:
    if not isinstance(body, dict):
        raise FileRefused(400, "invalid", 'Send JSON: {"name": "report.pdf", "size": 12345, "sha256": "<64 hex characters>"}.')
    size, sha = body.get("size"), str(body.get("sha256") or "").lower()
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= FILE_MAX_BYTES:
        raise FileRefused(400, "invalid", "size is the file's length in bytes, more than 0.")
    if not SHA256.fullmatch(sha):
        raise FileRefused(400, "invalid", "sha256 is the file's SHA-256 as 64 hex characters.")
    return Announced(clean_name(body.get("name")), size, sha)


def mb_limit(value: Any) -> int | None:
    """A limit in megabytes as bytes; ``None`` for :data:`~.store.NO_LIMIT`."""
    return None if float(value) == NO_LIMIT else int(float(value) * MB)


class Bodies:
    """The bodies on disk, by id, in one directory nothing else writes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, file_id: str) -> Path:
        if not FILE_ID.fullmatch(file_id):
            raise ValueError("not a file id")
        return self.root / file_id

    def write(self, file_id: str, offset: int, data: bytes) -> None:
        path = self.path(file_id)
        # Never through a link: the directory is the door's own, and this is the one place a guest's bytes land.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as file:
            file.truncate(offset)
            file.seek(offset)
            file.write(data)

    def read(self, file_id: str, offset: int, length: int) -> bytes:
        with open(self.path(file_id), "rb") as file:
            file.seek(offset)
            return file.read(length)

    def sha256(self, file_id: str) -> str:
        digest = hashlib.sha256()
        with open(self.path(file_id), "rb") as file:
            while piece := file.read(1024 * 1024):
                digest.update(piece)
        return digest.hexdigest()

    def remove(self, file_id: str) -> None:
        with contextlib.suppress(FileNotFoundError, ValueError):
            self.path(file_id).unlink()

    def ids(self) -> set[str]:
        return {entry.name for entry in self.root.iterdir() if FILE_ID.fullmatch(entry.name)}


class Desk:
    """The rules of files, for both doors: the public one for a guest's files, the control one for the host's."""

    def __init__(self, store: Store, bodies: Bodies, *, disk_bytes: int = DISK_BYTES) -> None:
        self.store = store
        self.bodies = bodies
        self.disk_bytes = disk_bytes

    def announce(self, conversation: Any, link: Any, origin: str, body: Any) -> dict[str, Any]:
        wanted = announced(body)
        per_file = mb_limit(link["file_mb"])
        if per_file is not None and wanted.size > per_file:
            raise FileRefused(413, "file_limit", f"A file may be at most {link['file_mb']:g} MB on this link.", limit_mb=float(link["file_mb"]))
        per_day = mb_limit(link["day_mb"])
        if per_day is not None and self.store.bytes_today(link["id"]) + wanted.size > per_day:
            raise FileRefused(429, "day_limit", f"This link has used its {link['day_mb']:g} MB of files for today. Tell your user; the limit lifts at midnight UTC.", limit_mb=float(link["day_mb"]))
        if self.store.bytes_held() + wanted.size > self.disk_bytes:
            raise FileRefused(507, "door_full", "This door is holding as many files as it can. Try again later, or send a smaller file.")
        file_id = self.store.create_file(conversation["id"], origin, wanted.name, wanted.size, wanted.sha256)
        self.bodies.write(file_id, 0, b"")
        return {**self.view(self.store.file(file_id)), "chunk_bytes": CHUNK_BYTES}

    def put(self, file: Any, offset: int, data: bytes) -> dict[str, Any]:
        """One piece, at the offset the door reached. Anything else is answered with that offset, so a client
        that lost track starts again from the right place instead of writing a hole or a piece twice."""
        if file["state"] != UPLOADING:
            raise FileRefused(409, "not_uploading", "This file is not being uploaded any more.", received=int(file["received"]), state=str(file["state"]))
        if offset != int(file["received"]):
            raise FileRefused(409, "offset", f"Send the piece that starts at byte {file['received']}.", received=int(file["received"]))
        if not data or len(data) > CHUNK_BYTES:
            raise FileRefused(413, "chunk", f"A piece is 1 to {CHUNK_BYTES} bytes.", chunk_bytes=CHUNK_BYTES)
        if offset + len(data) > int(file["size"]):
            raise FileRefused(413, "past_end", f"The file was announced as {file['size']} bytes.", received=int(file["received"]))
        self.bodies.write(file["id"], offset, data)
        received = offset + len(data)
        if received < int(file["size"]):
            self.store.file_received(file["id"], received)
            return self.view(self.store.file(file["id"]))
        if self.bodies.sha256(file["id"]) != file["sha256"]:
            # The whole file again from the start: which piece was wrong cannot be told from here.
            self.bodies.write(file["id"], 0, b"")
            self.store.file_received(file["id"], 0)
            raise FileRefused(422, "sha256", "The bytes do not match the sha256 you announced. Send the file again from byte 0.", received=0)
        self.store.file_received(file["id"], received, READY)
        return self.view(self.store.file(file["id"]))

    def content(self, file: Any, offset: int, length: int = CHUNK_BYTES) -> bytes:
        if file["state"] != READY:
            raise FileRefused(409, "not_ready", "This file is not complete, or is no longer kept here.", state=str(file["state"]))
        if not 0 <= offset <= int(file["size"]):
            raise FileRefused(416, "offset", f"offset is from 0 to {file['size']}.")
        return self.bodies.read(file["id"], offset, max(1, min(length, CHUNK_BYTES)))

    def ready_of(self, conversation_id: str, ids: Any, origin: str) -> list[Any]:
        """The files a message names: each one this conversation's, from ``origin``, and whole. ``FileRefused`` otherwise."""
        if not isinstance(ids, list) or not ids or len(ids) > 20 or not all(isinstance(i, str) for i in ids):
            raise FileRefused(400, "invalid", "files is a list of 1 to 20 file ids from your uploads.")
        found = []
        for file_id in dict.fromkeys(ids):
            file = self.store.file(file_id)
            if file is None or file["conversation_id"] != conversation_id or file["origin"] != origin:
                raise FileRefused(404, "no_file", f"There is no file {file_id[:24]} of yours in this conversation.")
            if file["state"] != READY:
                raise FileRefused(409, "not_ready", f"The file {file_id} is not complete yet.", received=int(file["received"]))
            found.append(file)
        return found

    def forget(self, now: float) -> int:
        """Delete what the door no longer keeps, and any body whose row is gone with its link."""
        dropped = 0
        for file in self.store.files_to_forget(stalled_before=now - STALLED_HOURS * 3600, kept_before=now - KEEP_HOURS * 3600):
            self.bodies.remove(file["id"])
            self.store.file_gone(file["id"])
            dropped += 1
        for orphan in self.bodies.ids() - self.store.file_ids():
            self.bodies.remove(orphan)
            dropped += 1
        return dropped

    def fetched(self, file: Any) -> None:
        """The host has its copy of a guest's file: the door's goes at once."""
        self.bodies.remove(file["id"])
        self.store.file_fetched(file["id"])

    @staticmethod
    def view(file: Any) -> dict[str, Any]:
        return {
            "id": file["id"], "name": file["name"], "size": int(file["size"]), "sha256": file["sha256"], "received": int(file["received"]), "state": file["state"],
            "from": "guest" if file["origin"] == GUEST else "agent",
        }


__all__ = ["AGENT", "CHUNK_BYTES", "DISK_BYTES", "FILE_MAX_BYTES", "GUEST", "KEEP_HOURS", "MB", "STALLED_HOURS", "Bodies", "Desk", "FileRefused", "announced", "clean_name", "mb_limit"]
