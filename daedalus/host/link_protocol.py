"""What both directions of an agent link share on this side: the kinds of message, a task's states, and files.

The door (``deploy/guestd``) speaks the protocol to a stranger's agent; this installation speaks the same one when
its own agent is the guest of somebody else's door (:mod:`daedalus.extensions.agent_talk`). A message is ``text``,
``file``, ``task``, ``question`` or ``status``; a task has an id and moves through :data:`TASK_STATES`; a file is
announced with its size and sha256 and sent in pieces of :data:`CHUNK_BYTES`, each at the offset the far side
says it reached, so a dropped connection resumes rather than starting again.

What arrives from the other side lands in ``guest-files/<conversation>/`` in the chat's folder, under a name that is
never a path and never the name of a file this installation reads as rules. It is data: written without an
execute bit, and the turn that reads it is told whose it is. Whatever goes out is first staged here: a folder
becomes one zip, every file is hashed, and the limits of the conversation are checked before a byte is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import re
import secrets
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from daedalus.host.nested_rules import RULE_FILES
from daedalus.security.untrusted import inert

TEXT, FILE, TASK, QUESTION, STATUS = "text", "file", "task", "question", "status"
KINDS = (TEXT, FILE, TASK, QUESTION, STATUS)

ACCEPTED, WORKING, NEEDS_INPUT, DONE, REFUSED, CANCELLED = "accepted", "working", "needs_input", "done", "refused", "cancelled"
TASK_STATES = (ACCEPTED, WORKING, NEEDS_INPUT, DONE, REFUSED, CANCELLED)
OPEN_TASK_STATES = frozenset({ACCEPTED, WORKING, NEEDS_INPUT})
WAKING_STATES = frozenset({NEEDS_INPUT, DONE, REFUSED, CANCELLED})
"""The changes the side that set a task is woken for. ``working`` is not one: it says nothing to act on."""

CHUNK_BYTES = 512 * 1024
"""One piece, as the door takes it (``guestd.files.CHUNK_BYTES``)."""
MB = 1024 * 1024
DEFAULT_FILE_MB = 50.0
DEFAULT_DAY_MB = 500.0
"""A conversation's limits on files when nobody chose others. 0 is no limit, as for every other limit of a link."""
MAX_FILES = 20
"""Files in one message, as the door takes them."""
NAME_CHARS = 160
GUEST_FILES = "guest-files"
STAGING = ".link-outbox"
"""Where a folder is zipped before it goes, under the state directory: never in the chat's folder, where a turn could
swap the zip between the operator's look and the send."""

SHA256 = re.compile(r"[0-9a-f]{64}")
CONVERSATION = re.compile(r"[A-Za-z0-9_-]{1,64}")
RULE_NAMES = frozenset(name.lower() for name in RULE_FILES)
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
"""What the app shows as a picture. Anything else, an SVG among them, is a name, a size and a download."""


class LinkFileError(ValueError):
    """A file that cannot go or be kept, in words the agent and the operator read."""


@dataclass(frozen=True, slots=True)
class Staged:
    """A file ready to go: what it is called there, where its bytes are here, and what they are."""

    name: str
    path: Path
    size: int
    sha256: str
    source: str
    """What the agent named: the file, or the folder that was zipped into ``path``."""

    @property
    def mime(self) -> str:
        return mime_of(self.name)


def mime_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def safe_name(value: object) -> str:
    """A name to save a stranger's file under: the last part of what they sent, made inert, never hidden, and never
    the name of a file this installation reads as somebody's rules."""
    text = " ".join(inert(str(value or "")).split())
    text = re.split(r"[\\/]", text)[-1].strip().lstrip(".") or "file"
    text = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", text)[:NAME_CHARS]
    return f"guest-{text}" if text.lower() in RULE_NAMES else text


def incoming_dir(root: Path, conversation: str) -> Path:
    if not CONVERSATION.fullmatch(conversation):
        raise LinkFileError("not a conversation")
    return root / GUEST_FILES / conversation


def free_path(directory: Path, name: str) -> Path:
    """``directory/name``, or the same name with a number before its extension when that is taken."""
    candidate = directory / name
    stem, dot, suffix = name.rpartition(".")
    stem, suffix = (stem, f".{suffix}") if dot and stem else (name, "")
    number = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while piece := file.read(MB):
            digest.update(piece)
    return digest.hexdigest()


def limit_bytes(mb: float) -> int | None:
    """A limit in megabytes as bytes; ``None`` for 0, which is no limit."""
    return int(float(mb) * MB) if float(mb) > 0 else None


def check_limits(sizes: Iterable[int], *, file_mb: float, day_mb: float, used_today: int) -> str:
    """Why these files cannot go under these limits, or "" when they can."""
    sizes = list(sizes)
    per_file, per_day = limit_bytes(file_mb), limit_bytes(day_mb)
    if per_file is not None and any(size > per_file for size in sizes):
        return f"a file may be at most {float(file_mb):g} MB in this conversation"
    if per_day is not None and used_today + sum(sizes) > per_day:
        return f"this conversation has {max(0, per_day - used_today) // MB} MB of its {float(day_mb):g} MB for today left"
    return ""


def _zip(folder: Path, target: Path) -> None:
    """``folder`` as one zip. Links are left out, as is anything that is not a file: a link inside the folder could
    name any file on this machine, and a zip of it would carry that file out under an innocent name."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            archive.write(path, path.relative_to(folder).as_posix())


def stage(paths: Iterable[Path], staging: Path) -> list[Staged]:
    """Each path as a file to send: a file as it is, a folder as one zip in ``staging``. Blocking; run it in a thread.

    The path is resolved once, here, and what goes is what was hashed: a zip is made in ``staging``, and a file is
    copied there when it is not, so a file that changes after this point changes nothing that is sent."""
    staging.mkdir(parents=True, exist_ok=True)
    out: list[Staged] = []
    for path in paths:
        if path.is_symlink() and not path.exists():
            raise LinkFileError(f"{path.name} is a link to nothing")
        if path.is_dir():
            target = staging / f"{secrets.token_hex(6)}.zip"
            _zip(path, target)
            name = safe_name(f"{path.name or 'folder'}.zip")
        elif path.is_file():
            target = staging / f"{secrets.token_hex(6)}-{safe_name(path.name)}"
            with open(path, "rb") as source, open(target, "wb") as copy:
                while piece := source.read(MB):
                    copy.write(piece)
            name = safe_name(path.name)
        else:
            raise LinkFileError(f"{path} is not a file or a folder")
        out.append(Staged(name, target, target.stat().st_size, sha256_of(target), str(path)))
    return out


def unstage(files: Iterable[Staged]) -> None:
    for file in files:
        with contextlib.suppress(OSError):
            file.path.unlink()


async def send_pieces(path: Path, size: int, received: int, put: Callable[[int, bytes], Awaitable[int]]) -> None:
    """Send ``path`` from ``received`` on, a piece at a time. ``put`` sends one piece at an offset and returns how far
    the far side has got, which is where the next piece starts: the far side's count, never this side's guess."""
    while received < size:
        piece = await asyncio.to_thread(_read_piece, path, received)
        if not piece:
            raise LinkFileError("the file grew shorter while it was being sent")
        reached = await put(received, piece)
        if reached <= received and reached < size:
            raise LinkFileError("the other side did not take the piece")
        received = reached


def _read_piece(path: Path, offset: int) -> bytes:
    with open(path, "rb") as file:
        file.seek(offset)
        return file.read(CHUNK_BYTES)


async def fetch_pieces(target: Path, size: int, sha256: str, get: Callable[[int], Awaitable[bytes]]) -> Path:
    """Fetch a file a piece at a time into ``target``, checked against ``sha256`` before it gets its name.

    A partial file waits beside the target under a hidden name, so a fetch cut short goes on from where it stopped
    the next time, and nothing half-written is ever found under the file's own name."""
    partial = target.with_name(f".{target.name}.part")
    have = partial.stat().st_size if partial.exists() else 0
    if have > size:
        partial.unlink()
        have = 0
    while have < size:
        piece = await get(have)
        if not piece:
            raise LinkFileError("the other side sent nothing for a piece")
        piece = piece[: size - have]
        await asyncio.to_thread(_append, partial, piece)
        have += len(piece)
    if await asyncio.to_thread(sha256_of, partial) != sha256:
        partial.unlink()
        raise LinkFileError("the bytes did not match the sha256 the other side announced")
    os.chmod(partial, 0o644)
    partial.replace(target)
    return target


def _append(path: Path, data: bytes) -> None:
    with open(path, "ab") as file:
        file.write(data)


__all__ = [
    "ACCEPTED", "CANCELLED", "CHUNK_BYTES", "DEFAULT_DAY_MB", "DEFAULT_FILE_MB", "DONE", "FILE", "GUEST_FILES", "IMAGE_TYPES", "KINDS", "MAX_FILES", "MB",
    "NEEDS_INPUT", "OPEN_TASK_STATES", "QUESTION", "REFUSED", "SHA256", "STAGING", "STATUS", "TASK", "TASK_STATES", "TEXT", "WAKING_STATES", "WORKING",
    "LinkFileError", "Staged", "check_limits", "fetch_pieces", "free_path", "incoming_dir", "limit_bytes", "mime_of", "safe_name", "send_pieces", "sha256_of",
    "stage", "unstage",
]
