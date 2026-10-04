"""Read a proposed attachment once through a project folder, without following path links."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from daedalus.stores.control import ControlDenied

MAX_ATTACHMENT_BYTES = 25_000_000


def read_project_file(name: str, roots: list[str]) -> tuple[str, bytes]:
    """Return bytes from one stable descriptor inside a configured project folder.

    Every component is opened relative to its held parent descriptor so replacing a link or
    directory name cannot redirect the read after the project containment check.
    """
    supplied = Path(name)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ControlDenied("an attachment needs an absolute path without parent traversal")
    candidate = supplied.absolute()
    for configured in roots:
        root = Path(configured).absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            raise ControlDenied("an attachment must be a file inside a project folder")
        descriptors: list[int] = []
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(parent)
            for part in (*root.parts[1:], *relative.parts[:-1]):
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
            file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=parent)
            descriptors.append(file_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ATTACHMENT_BYTES:
                raise ControlDenied("an attachment is not a bounded regular file")
            with os.fdopen(os.dup(file_fd), "rb") as stream:
                data = stream.read(MAX_ATTACHMENT_BYTES + 1)
            after = os.fstat(file_fd)
            def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
                return (value.st_dev, value.st_ino, value.st_size,
                        value.st_mtime_ns, value.st_ctime_ns)
            if len(data) > MAX_ATTACHMENT_BYTES or len(data) != before.st_size or identity(before) != identity(after):
                raise ControlDenied("an attachment changed while it was read")
            return str(candidate), data
        except OSError as exc:
            raise ControlDenied("an attachment is unavailable or contains a symbolic link") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    raise ControlDenied("an attachment is outside the proposing project folders")
