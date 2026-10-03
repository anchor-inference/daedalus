"""Capture explicitly selected project files without following links or preserving host paths."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

MAX_SELECTED_FILES = 1000
MAX_SELECTED_FILE_BYTES = 64 << 20
MAX_SELECTED_TOTAL_BYTES = 192 << 20
RELATIVE_PART = re.compile(r"[^/\\\x00-\x1f\x7f]+\Z")
FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


class WorkspaceFileRefused(ValueError):
    """A chosen file cannot be read as a bounded, ordinary relative file."""


def relative_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise WorkspaceFileRefused("a relative file path is required")
    parts = tuple(path.split("/"))
    if (len(parts) > 32 or len(path.encode()) > 1024 or any(
        part in ("", ".", "..") or RELATIVE_PART.fullmatch(part) is None for part in parts
    )):
        raise WorkspaceFileRefused("file path is unsafe")
    if any(part in (".git", ".checkpoints", ".ssh", ".aws") for part in parts):
        raise WorkspaceFileRefused("runtime or credential directories cannot be archived")
    return parts


def read_selected(root: Path, path: str) -> bytes:
    """Read one regular file from a pinned directory, refusing symlinks at every level."""
    parts = relative_parts(path)
    opened: list[int] = []
    try:
        if not root.is_absolute() or ".." in root.parts:
            raise WorkspaceFileRefused("selected folder is not an absolute stable path")
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        opened.append(descriptor)
        for part in root.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            opened.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            opened.append(descriptor)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        opened.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SELECTED_FILE_BYTES:
            raise WorkspaceFileRefused("selected path is not a bounded regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(MAX_SELECTED_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if (len(content) > MAX_SELECTED_FILE_BYTES or len(content) != before.st_size
                or (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)):
            raise WorkspaceFileRefused("selected file changed during capture")
        return content
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
        if isinstance(exc, WorkspaceFileRefused):
            raise
        raise WorkspaceFileRefused("selected file is unavailable or unsafe") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def ensure_private_root(root: Path) -> None:
    if not root.is_absolute() or ".." in root.parts:
        raise WorkspaceFileRefused("restore root is not an absolute stable path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise WorkspaceFileRefused("restore root is unavailable or unsafe") from exc
    finally:
        os.close(descriptor)


def capture_selected(folders: dict[str, Path], selections: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if len(selections) > MAX_SELECTED_FILES:
        raise WorkspaceFileRefused("too many selected files")
    records: list[dict[str, Any]] = []
    content: dict[str, bytes] = {}
    total = 0
    for selected in selections:
        if not isinstance(selected, dict) or set(selected) != {"folder_id", "path"}:
            raise WorkspaceFileRefused("a folder and relative file path are required")
        folder_id, path = selected["folder_id"], selected["path"]
        if not isinstance(folder_id, str) or FOLDER_ID.fullmatch(folder_id) is None or folder_id not in folders:
            raise WorkspaceFileRefused("selected folder is unavailable to this host")
        relative_parts(path)
        key = folder_id + "/" + path
        if key in content:
            raise WorkspaceFileRefused("selected file was repeated")
        data = read_selected(folders[folder_id], path)
        total += len(data)
        if total > MAX_SELECTED_TOTAL_BYTES:
            raise WorkspaceFileRefused("selected files exceed the archive limit")
        digest = hashlib.sha256(data).hexdigest()
        records.append({"folder_id": folder_id, "path": path, "size": len(data), "sha256": digest})
        content[key] = data
    return records, content


def validate_selected(records: Any, content: dict[str, bytes], folder_ids: set[str]) -> None:
    if not isinstance(records, list) or len(records) > MAX_SELECTED_FILES:
        raise WorkspaceFileRefused("selected file inventory is invalid")
    required: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"folder_id", "path", "size", "sha256"}:
            raise WorkspaceFileRefused("selected file inventory is invalid")
        folder_id, path = record["folder_id"], record["path"]
        if not isinstance(folder_id, str) or FOLDER_ID.fullmatch(folder_id) is None or folder_id not in folder_ids:
            raise WorkspaceFileRefused("selected file names an unknown folder")
        relative_parts(path)
        key = folder_id + "/" + path
        if key in required or key not in content:
            raise WorkspaceFileRefused("selected file is missing or repeated")
        data = content[key]
        total += len(data)
        if (len(data) > MAX_SELECTED_FILE_BYTES or record["size"] != len(data)
                or record["sha256"] != hashlib.sha256(data).hexdigest()):
            raise WorkspaceFileRefused("selected file checksum is invalid")
        required.add(key)
    if required != set(content) or total > MAX_SELECTED_TOTAL_BYTES:
        raise WorkspaceFileRefused("selected file inventory does not match its bytes")


def stage_selected(root: Path, project_id: str, records: list[dict[str, Any]], content: dict[str, bytes],
                   folder_ids: set[str] | None = None) -> tuple[Path, bool]:
    """Materialize verified bytes below a private root; the caller removes a new tree on DB failure."""
    if not re.fullmatch(r"[0-9a-f]{32}", project_id):
        raise WorkspaceFileRefused("restored project identity is invalid")
    if any(FOLDER_ID.fullmatch(folder_id) is None for folder_id in folder_ids or ()):
        raise WorkspaceFileRefused("restored folder identity is invalid")
    validate_selected(records, content, folder_ids or {record["folder_id"] for record in records})
    ensure_private_root(root)
    target = root / project_id
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise WorkspaceFileRefused("restored file tree is unsafe")
        for folder_id in folder_ids or ():
            if not (target / folder_id).is_dir() or (target / folder_id).is_symlink():
                raise WorkspaceFileRefused("restored folder tree is unsafe")
        expected_files = {record["folder_id"] + "/" + record["path"] for record in records}
        actual_files: set[str] = set()
        for directory, directories, filenames in os.walk(target, followlinks=False):
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise WorkspaceFileRefused("restored folder tree is unsafe")
            for name in filenames:
                actual_files.add((Path(directory) / name).relative_to(target).as_posix())
        if actual_files != expected_files:
            raise WorkspaceFileRefused("restored folder tree has unexpected files")
        for record in records:
            if hashlib.sha256(read_selected(target / record["folder_id"], record["path"])).hexdigest() != record["sha256"]:
                raise WorkspaceFileRefused("restored file tree has changed")
        return target, False
    temporary = Path(tempfile.mkdtemp(prefix=".workspace-", dir=root))
    published = False
    try:
        for folder_id in folder_ids or ():
            (temporary / folder_id).mkdir()
        for record in records:
            directory = temporary / record["folder_id"]
            path = directory.joinpath(*relative_parts(record["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content[record["folder_id"] + "/" + record["path"]])
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, target)
        published = True
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return target, True
    except BaseException:
        if published:
            shutil.rmtree(target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
