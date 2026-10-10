"""Where a session's files live: on this machine, or behind a shell the session drives.

The file tools (Read, Write, Edit, Find, Search) and Exec normally act on the local filesystem.
A benchmark harness hands the session a container instead: every command runs there through an
:class:`ExecBackend`, and the file tools go through the same shell so the model sees one
consistent world. The tools ask :class:`SessionServices` for ``fs`` and never touch ``Path``
directly when a backend is present.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class ExecOutcome:
    exit_code: int
    output: str
    timed_out: bool = False


class ExecBackend(Protocol):
    """Runs one shell command somewhere else and returns its interleaved output."""

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome: ...


class LocalFS:
    """The local filesystem; the default."""

    remote = False

    async def read_text(self, path: Path) -> str:
        return await asyncio.to_thread(path.read_text, "utf-8")

    async def write_text(self, path: Path, content: str) -> None:
        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def exists(self, path: Path) -> bool:
        return path.exists()

    async def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    async def is_file(self, path: Path) -> bool:
        return path.is_file()

    async def size(self, path: Path) -> int:
        return path.stat().st_size

    async def read_bytes(self, path: Path, *, limit: int) -> bytes:
        """The file's bytes; a ``ValueError`` naming the size when it holds more than ``limit``."""
        size = path.stat().st_size
        if size > limit:
            raise ValueError(f"{path} is {size} bytes, more than the {limit} read here")
        return await asyncio.to_thread(path.read_bytes)

    async def listdir(self, path: Path) -> list[str]:
        return sorted(os.listdir(path))

    async def find(self, root: Path, pattern: str, limit: int) -> list[str]:
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "__pycache__"}]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    matches.append(rel)
                    if len(matches) >= limit:
                        return sorted(matches)
        return sorted(matches)

    async def search(self, root: Path, pattern: str, *, glob: str | None, case_insensitive: bool, limit: int) -> tuple[int, str, str]:
        args = ["rg", "-n", "--no-heading", "--color", "never", "-m", str(limit)]
        if case_insensitive:
            args.append("-i")
        if glob:
            args += ["-g", glob]
        args += ["-e", pattern, str(root)]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


BINARY_MARKER = "__DAEDALUS_BINARY__"
WRITE_CHUNK_CHARS = 60_000
READ_CHUNK_BYTES = 384 * 1024
"""Bytes read per command by :meth:`ShellFS.read_bytes`: their base64 is 512 KiB, under the 640 KiB
the host terminal daemon carries back in one reply."""
"""Base64 characters per command when writing through a shell: well under the kernel's 128 KiB per-argument cap."""


class ShellFS:
    """The filesystem behind an :class:`ExecBackend`, reached with portable shell commands."""

    remote = True

    def __init__(self, backend: ExecBackend, *, timeout: float = 120.0) -> None:
        self.backend = backend
        self.timeout = timeout

    async def _run(self, command: str) -> ExecOutcome:
        return await self.backend.run(command, cwd=None, env=None, timeout=self.timeout)

    async def read_text(self, path: Path) -> str:
        quoted = shlex.quote(str(path))
        outcome = await self._run(f"if grep -qI . {quoted} || [ ! -s {quoted} ]; then cat {quoted}; else echo {BINARY_MARKER}; exit 3; fi")
        if outcome.exit_code == 3 and BINARY_MARKER in outcome.output:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file")
        if outcome.exit_code != 0:
            raise FileNotFoundError(outcome.output.strip() or str(path))
        return outcome.output

    async def read_bytes(self, path: Path, *, limit: int) -> bytes:
        """The file's bytes, through the shell as base64, a piece per command.

        Text read with ``cat`` would come back decoded and lose every byte that is not UTF-8, which is
        every image; base64 is plain ASCII whatever the file holds. A piece is small enough that its
        encoding fits the host daemon's cap on one reply, so a large file is many round trips rather
        than one cut short. A ``FileNotFoundError`` when it is not a file, a ``ValueError`` naming the
        size when it holds more than ``limit``.
        """
        quoted = shlex.quote(str(path))
        probe = await self._run(f"test -f {quoted} && wc -c < {quoted}")
        try:
            size = int(probe.output.strip().splitlines()[-1]) if probe.exit_code == 0 else -1
        except (ValueError, IndexError):
            size = -1
        if size < 0:
            raise FileNotFoundError(f"no such file: {path}")
        if size > limit:
            raise ValueError(f"{path} is {size} bytes, more than the {limit} read here")
        data = bytearray()
        while len(data) < size:
            wanted = min(READ_CHUNK_BYTES, size - len(data))
            # ``tail`` is silenced on purpose: ``head`` closes the pipe after its piece, and where SIGPIPE
            # is ignored (a systemd unit's default, and the host daemon runs as one) ``tail`` does not die
            # quietly but prints "Broken pipe" into the stream, which ``exec 2>&1`` merges into the base64,
            # so every file larger than one piece failed to read at byte 0.
            outcome = await self._run(f"tail -c +{len(data) + 1} {quoted} 2>/dev/null | head -c {wanted} | base64")
            try:
                piece = base64.b64decode("".join(outcome.output.split()), validate=True)
            except ValueError:
                piece = b""
            if outcome.exit_code != 0 or not piece:
                raise OSError(f"could not read {path} at byte {len(data)}: {outcome.output.strip()[-300:] or 'no answer'}")
            data += piece
        return bytes(data)

    async def write_bytes(self, path: Path, data: bytes) -> None:
        """Write ``data`` byte for byte, as :meth:`write_text` writes text."""
        await self._write_encoded(path, base64.b64encode(data).decode("ascii"))

    async def write_text(self, path: Path, content: str) -> None:
        """Write the content byte for byte: base64 over the shell, in pieces small enough for one argument.

        A heredoc would need a delimiter the content cannot contain and always ends its last line; base64
        has neither problem, and splitting the encoded text keeps every command under the kernel's
        per-argument limit (128 KiB on Linux). The first piece replaces the file, the rest append.
        """
        await self._write_encoded(path, base64.b64encode(content.encode("utf-8")).decode("ascii"))

    async def _write_encoded(self, path: Path, encoded: str) -> None:
        pieces = [encoded[i : i + WRITE_CHUNK_CHARS] for i in range(0, len(encoded), WRITE_CHUNK_CHARS)] or [""]
        target = shlex.quote(str(path))
        for index, piece in enumerate(pieces):
            lead = f"mkdir -p {shlex.quote(str(path.parent))} && " if index == 0 else ""
            redirect = ">" if index == 0 else ">>"
            outcome = await self._run(f"{lead}printf %s {shlex.quote(piece)} | base64 -d {redirect} {target}")
            if outcome.exit_code != 0:
                raise OSError(outcome.output.strip() or f"could not write {path}")

    async def exists(self, path: Path) -> bool:
        return (await self._run(f"test -e {shlex.quote(str(path))}")).exit_code == 0

    async def is_dir(self, path: Path) -> bool:
        return (await self._run(f"test -d {shlex.quote(str(path))}")).exit_code == 0

    async def is_file(self, path: Path) -> bool:
        return (await self._run(f"test -f {shlex.quote(str(path))}")).exit_code == 0

    async def size(self, path: Path) -> int:
        outcome = await self._run(f"wc -c < {shlex.quote(str(path))}")
        try:
            return int(outcome.output.strip())
        except ValueError:
            return 0

    async def listdir(self, path: Path) -> list[str]:
        outcome = await self._run(f"ls -1A {shlex.quote(str(path))}")
        return sorted(line for line in outcome.output.splitlines() if line)

    async def find(self, root: Path, pattern: str, limit: int) -> list[str]:
        prune = " -o ".join(f"-name {shlex.quote(d)}" for d in (".git", "node_modules", ".venv", "__pycache__"))
        outcome = await self._run(f"cd {shlex.quote(str(root))} && find . \\( {prune} \\) -prune -o -type f -print 2>/dev/null | sed 's|^\\./||'")
        matches = [rel for rel in outcome.output.splitlines() if rel and (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(rel), pattern))]
        return sorted(matches)[:limit]

    async def search(self, root: Path, pattern: str, *, glob: str | None, case_insensitive: bool, limit: int) -> tuple[int, str, str]:
        flags = "-i" if case_insensitive else ""
        include = f"--include={shlex.quote(glob)}" if glob else ""
        # ripgrep when the container has it, grep otherwise; both return 1 for "no match".
        rg = f"rg -n --no-heading --color never -m {limit} {flags} {('-g ' + shlex.quote(glob)) if glob else ''} -e {shlex.quote(pattern)} {shlex.quote(str(root))}"
        grep = f"grep -rnE {flags} {include} --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv -e {shlex.quote(pattern)} {shlex.quote(str(root))} | head -n {limit}"
        outcome = await self._run(f"if command -v rg >/dev/null 2>&1; then {rg}; else {grep}; fi")
        code = outcome.exit_code
        if code == 0 and not outcome.output.strip():
            code = 1
        return code, outcome.output, ""


__all__ = ["ExecBackend", "ExecOutcome", "LocalFS", "ShellFS"]
