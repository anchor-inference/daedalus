"""Moving files to where a staff member can open them, and taking theirs back.

Orchestrators pass files by handle (``daedalus.stores.files``). This module is where a handle becomes
bytes on the member's side and where a member's path becomes a handle again, and it is the only
place that knows which environment is which:

- a member of this process's own environment — a Daedalus member in the agent's container, a
  command-line member in the container's terminal service, anything natively — gets a direct copy;
- a command-line member on the host, seen from the container, gets the file through the host
  terminal daemon's ``fs.write``, which writes nowhere but an inbox under a project folder.

Either way the file lands in ``<the member's working folder>/.agents/inbox/<task>/<name>``, and only
once it is there is the member told that path: a brief never names a file the member cannot open.
The inbox carries a ``.gitignore`` of ``*``, so a repository whose exclude file the host cannot
write (one on the host) does not show it; ``.agents/`` itself may hold the operator's own files and
is left alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from daedalus.stores.files import (
    FILE_MAX_BYTES,
    HANDOVER_MAX_FILES,
    FileRefused,
    FileStore,
    StoredFile,
    human_size,
    parse_handle,
    safe_name,
)
from daedalus.stores.projects import Project, ProjectFolder

INBOX = (".agents", "inbox")
WRITE_CHUNK = 512 << 10
"""What one ``fs.write`` carries: the daemon's own bound for one frame with base64."""
READ_CHUNK = 512 << 10
IGNORE_ALL = b"*\n"
MESSAGES_BOX = "messages"
"""The inbox of files told to a member with no task: a Tell outside any task."""


class HostFiles(Protocol):
    """What moving a file to or from the host needs of the host bridge
    (:class:`daedalus.terminals.bridge.HostBridge`): a daemon that is down is a ``ConnectionError``,
    a missing path a ``FileNotFoundError``, an existing one at offset 0 a ``FileExistsError``, any
    other refusal an ``OSError`` in the daemon's words."""

    def available(self) -> bool: ...
    async def stat(self, path: str) -> dict[str, Any]: ...
    async def read(self, path: str, *, offset: int, max_bytes: int) -> Any: ...
    async def write(self, path: str, data: bytes, *, offset: int = 0, actor: str = "system") -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Delivered:
    """A file where the member can open it."""

    file: StoredFile
    path: str

    def line(self) -> str:
        return f"- {self.path} ({self.file.mime}, {self.file.size} bytes; {self.file.handle})"


def box_name(task_id: str | None) -> str:
    """The inbox directory of a task: its id, which is a plain segment, else ``messages``."""
    cleaned = safe_name(task_id or "")
    return cleaned if task_id and cleaned == task_id else MESSAGES_BOX


def _variant(name: str, digest: str, counter: int = 0) -> str:
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    tail = f"-{digest[:8]}" + (f"-{counter}" if counter else "")
    return f"{stem}{tail}.{suffix}" if suffix else f"{stem}{tail}"


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class Handoff:
    """Files between the project's store and its members' working folders."""

    def __init__(self, files: FileStore, *, local_env: str, host: Callable[[], HostFiles | None]) -> None:
        self.files = files
        self.local_env = local_env
        self._host = host

    def host(self) -> HostFiles:
        bridge = self._host()
        if bridge is None:
            raise FileRefused("a member on the host is reached through the host terminal bridge, and this installation has none (bash deploy/setup.sh offers to install it)")
        if not bridge.available():
            raise FileRefused("the host terminal bridge is not answering now, so nothing can be handed to a member on the host; ask the operator to check it (systemctl --user status daedalus-ptyd)")
        return bridge

    def reachable(self, env: str) -> str:
        """Why files cannot be moved to or from ``env`` now, or ``""`` when they can."""
        if env == self.local_env:
            return ""
        if env != "host":
            return f"nothing here reaches the {env}"
        try:
            self.host()
        except FileRefused as exc:
            return str(exc)
        return ""

    # -- what an orchestrator names ----------------------------------------------------------------------

    async def resolve(self, refs: Sequence[str] | None, *, project: Project, actor: str) -> list[StoredFile]:
        """The files an orchestrator named for a hand-over — handles of the project, or paths in its
        folders (taken in as new handles first, so the member's copy does not change under it). All are
        checked before any is used: one bad name refuses the hand-over and says which."""
        wanted = [str(r).strip() for r in refs or [] if str(r or "").strip()]
        if len(wanted) > HANDOVER_MAX_FILES:
            raise FileRefused(f"{len(wanted)} files in one hand-over; at most {HANDOVER_MAX_FILES} — name a folder in the brief instead")
        found: list[StoredFile] = []
        for ref in wanted:
            if parse_handle(ref) is not None:
                found.append(await self.files.in_scope(ref, project.id))
            else:
                found.append(await self.import_path(ref, project=project, actor=actor))
        return list({f.id: f for f in found}.values())

    def locate(self, raw: str, project: Project) -> tuple[ProjectFolder, str]:
        """The folder a path lies in, and the path made absolute: an absolute path must be inside one
        of the project's folders; a relative one is taken from the primary folder."""
        text = raw.strip()
        if text.startswith("~"):
            raise FileRefused(f"{text} names a home directory; give a path inside one of the project's folders or a file handle")
        if not text.startswith("/"):
            if project.primary is None:
                raise FileRefused(f"{project.name} has no folder for {text!r} to be in")
            text = posixpath.join(str(project.primary.path), text)
        clean = posixpath.normpath(text)
        for folder in sorted(project.folders, key=lambda f: -len(str(f.path))):
            if _inside(clean, posixpath.normpath(str(folder.path))):
                return folder, clean
        raise FileRefused(f"{clean} is not in any of {project.name}'s folders, and it is not a file handle (att:…); Peek(op='files') lists the project's files")

    async def import_path(self, raw: str, *, project: Project, actor: str) -> StoredFile:
        folder, path = self.locate(raw, project)
        data = await self._read(folder.env, path, allowed=[str(f.path) for f in project.folders if f.env == folder.env])
        return await self.files.add(data, name=PurePosixPath(path).name, origin="folder", origin_ref=path, scope=project.id, actor=actor)

    async def read(self, env: str, path: str, *, allowed: Sequence[str]) -> bytes:
        """The bytes of a file of ``env`` inside ``allowed``, read locally or through the host bridge;
        a refusal says why. What a member's browser uploads is read here, under the member's walls."""
        return await self._read(env, path, allowed=allowed)

    async def _read(self, env: str, path: str, *, allowed: Sequence[str], check: Callable[[Path], str] | None = None) -> bytes:
        """The bytes of a file of ``env`` inside ``allowed``; a refusal says why."""
        if env == self.local_env:
            real = Path(os.path.realpath(path))
            if not any(_inside(str(real), os.path.realpath(root)) for root in allowed):
                raise FileRefused(f"{path} leads outside the project's folders")
            reason = check(real) if check is not None else ""
            if reason:
                raise FileRefused(reason)
            if not real.exists():
                raise FileNotFoundError(path)
            if not real.is_file():
                raise FileRefused(f"{path} is a folder, not a file; name the files in it")
            size = real.stat().st_size
            if size > FILE_MAX_BYTES:
                raise FileRefused(f"{path} is {human_size(size)}; files handed on are at most {human_size(FILE_MAX_BYTES)}")
            return await asyncio.to_thread(real.read_bytes)
        if env != "host":
            raise FileRefused(f"{path} is in the {env}, which nothing here reaches")
        if not any(_inside(path, posixpath.normpath(root)) for root in allowed):
            raise FileRefused(f"{path} is outside the project's folders")
        bridge = self.host()
        try:
            stat = await bridge.stat(path)
            if not stat.get("exists"):
                raise FileNotFoundError(path)
            if stat.get("type") != "file":
                raise FileRefused(f"{path} is a folder, not a file; name the files in it")
            size = int(stat.get("size") or 0)
            if size > FILE_MAX_BYTES:
                raise FileRefused(f"{path} is {human_size(size)}; files handed on are at most {human_size(FILE_MAX_BYTES)}")
            out = bytearray()
            while True:
                chunk = await bridge.read(path, offset=len(out), max_bytes=READ_CHUNK)
                out.extend(chunk.data)
                if chunk.eof or not chunk.data or len(out) > FILE_MAX_BYTES:
                    break
        except (FileNotFoundError, FileRefused):
            raise
        except ConnectionError as exc:
            raise FileRefused(str(exc)) from exc
        except OSError as exc:
            raise FileRefused(f"the host would not give {path}: {exc}") from exc
        if len(out) > FILE_MAX_BYTES:
            raise FileRefused(f"{path} grew past {human_size(FILE_MAX_BYTES)} while it was read")
        return bytes(out)

    # -- to a member -------------------------------------------------------------------------------------------

    def check_target(self, folder: ProjectFolder) -> None:
        """Refuse before anything is written: a read-only folder takes no inbox, and an environment
        nothing reaches takes no file."""
        if folder.readonly:
            raise FileRefused(f"{folder.path} is read-only, so no file can be put there for the member; make the folder writable or hand the member the text itself")
        reason = self.reachable(folder.env)
        if reason:
            raise FileRefused(reason)

    async def deliver(self, files: Sequence[StoredFile], *, env: str, cwd: str, box: str, actor: str, member: str = "") -> list[Delivered]:
        """Put each file into ``<cwd>/.agents/inbox/<box>/`` of ``env`` and return where each is.

        The same bytes already delivered to that place are not written again; a name taken by other
        bytes gets the hash's first characters. Each delivery is recorded. The first failure stops and
        is raised: the caller must not send a brief that names a file that is not there."""
        if not files:
            return []
        inbox = posixpath.join(posixpath.normpath(cwd), *INBOX)
        target_dir = posixpath.join(inbox, box)
        out: list[Delivered] = []
        for stored in files:
            try:
                if env == self.local_env:
                    path = await self._copy_local(stored, cwd=cwd, inbox=inbox, target_dir=target_dir)
                elif env == "host":
                    path = await self._write_host(stored, inbox=inbox, target_dir=target_dir, actor=actor)
                else:
                    raise FileRefused(f"nothing here reaches the {env}")
            except FileRefused as exc:
                await self.files.record(stored, "refused", actor=actor, target=target_dir, env=env, detail=str(exc))
                raise
            except (OSError, ConnectionError) as exc:
                await self.files.record(stored, "refused", actor=actor, target=target_dir, env=env, detail=str(exc))
                raise FileRefused(f"{stored.name} could not be put in {target_dir}: {exc}") from exc
            await self.files.record(stored, "delivered", actor=actor, target=path, env=env, detail=member)
            out.append(Delivered(stored, path))
        return out

    async def _copy_local(self, stored: StoredFile, *, cwd: str, inbox: str, target_dir: str) -> str:
        root = os.path.realpath(cwd)
        if not os.path.isdir(root):
            raise FileRefused(f"the member's folder {cwd} is not there")
        # A `.agents` or an inbox that is a link to elsewhere must not be written through: checked as
        # far as it exists before anything is made, and again once it is.
        if not _inside(os.path.realpath(target_dir), root):
            raise FileRefused(f"{target_dir} leads outside the member's folder")
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        real_dir = os.path.realpath(target_dir)
        if not _inside(real_dir, root):
            raise FileRefused(f"{target_dir} leads outside the member's folder")
        ignore = Path(inbox) / ".gitignore"
        if not ignore.exists() and not ignore.is_symlink():
            ignore.write_bytes(IGNORE_ALL)
        source = self.files.path_of(stored)
        for name in self._names(stored):
            target = Path(real_dir) / name
            if target.is_symlink():
                continue
            if target.exists():
                if target.is_file() and target.stat().st_size == stored.size and await self._same_local(target, stored):
                    return str(Path(target_dir) / name)
                continue
            temporary = Path(real_dir) / f".{name}.{uuid.uuid4().hex}.part"
            try:
                await asyncio.to_thread(shutil.copyfile, source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return str(Path(target_dir) / name)
        raise FileRefused(f"no free name for {stored.name} in {target_dir}")

    async def _same_local(self, target: Path, stored: StoredFile) -> bool:
        def digest() -> str:
            h = hashlib.sha256()
            with target.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    h.update(chunk)
            return h.hexdigest()

        return await asyncio.to_thread(digest) == stored.sha256

    def _names(self, stored: StoredFile) -> list[str]:
        return [stored.name, _variant(stored.name, stored.sha256), *(_variant(stored.name, stored.sha256, n) for n in range(1, 20))]

    async def _write_host(self, stored: StoredFile, *, inbox: str, target_dir: str, actor: str) -> str:
        bridge = self.host()
        ignore = posixpath.join(inbox, ".gitignore")
        try:
            if not (await bridge.stat(ignore)).get("exists"):
                await bridge.write(ignore, IGNORE_ALL, actor=actor)
        except FileExistsError:
            pass
        for name in self._names(stored):
            target = posixpath.join(target_dir, name)
            stat = await bridge.stat(target)
            if stat.get("exists"):
                if int(stat.get("size") or -1) == stored.size and await self.files.delivered(stored.id, target):
                    return target
                continue
            data = await self.files.read(stored)
            try:
                offset = 0
                while True:
                    piece = data[offset : offset + WRITE_CHUNK]
                    await bridge.write(target, piece, offset=offset, actor=actor)
                    offset += len(piece)
                    if offset >= len(data):
                        break
            except FileExistsError:
                continue
            return target
        raise FileRefused(f"no free name for {stored.name} in {target_dir}")

    # -- from a member ------------------------------------------------------------------------------------------

    async def read_artifacts(
        self,
        refs: Sequence[str] | None,
        *,
        env: str,
        cwd: str,
        project: Project,
        check: Callable[[Path], str] | None = None,
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """Read allowed artifact bytes without creating durable file or access rows."""
        staged: list[tuple[str, str, bytes]] = []
        notes: list[str] = []
        allowed = [str(f.path) for f in project.folders if f.env == env]
        for raw in [str(r).strip() for r in refs or [] if str(r or "").strip()][:HANDOVER_MAX_FILES]:
            if "://" in raw or raw.startswith(("agent/", "#")) or " " in raw.strip():
                continue
            path = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(cwd, raw))
            try:
                data = await self._read(env, path, allowed=allowed, check=check)
            except FileNotFoundError:
                continue  # a name that is not a file here: a branch, a commit, a page
            except FileRefused as exc:
                notes.append(f"{raw}: {exc}")
                continue
            if len(data) > FILE_MAX_BYTES:
                notes.append(f"{raw}: file exceeds the handoff size limit")
                continue
            staged.append((safe_name(PurePosixPath(path).name), path, data))
        return staged, notes

    async def fetch(
        self,
        refs: Sequence[str] | None,
        *,
        env: str,
        cwd: str,
        project: Project,
        actor: str,
        origin_ref: str = "",
        check: Callable[[Path], str] | None = None,
    ) -> tuple[list[StoredFile], list[str]]:
        """Keep a member's allowed artifact bytes as project files."""
        staged, notes = await self.read_artifacts(refs, env=env, cwd=cwd, project=project, check=check)
        stored: list[StoredFile] = []
        for name, path, data in staged:
            try:
                stored.append(await self.files.add(data, name=name, origin="staff",
                                                   origin_ref=origin_ref or path, scope=project.id, actor=actor))
            except FileRefused as exc:
                notes.append(f"{path}: {exc}")
        return stored, notes


__all__ = ["Delivered", "Handoff", "HostFiles", "box_name"]
