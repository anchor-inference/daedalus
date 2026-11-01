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
import re
import shlex
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, BinaryIO, Protocol


@dataclass(slots=True)
class ExecOutcome:
    exit_code: int
    output: str
    timed_out: bool = False


class ExecBackend(Protocol):
    """Runs one shell command somewhere else and returns its interleaved output."""

    async def run(self, command: str, *, cwd: str | None, env: dict[str, str] | None, timeout: float) -> ExecOutcome: ...


TEXT_MAX_BYTES = 32 << 20
"""The largest file read whole as text, for an edit: past it the edit is refused and the file is left
to Exec, because a whole read of a multi-gigabyte log sits in this process's memory."""
LINE_COUNT_MAX_BYTES = 256 << 20
"""The largest file whose lines Read counts for its "N more lines" note; past it the note names the
size instead, so a read of the first lines of a huge file does not stream all of it."""
READ_CHUNK_BYTES_LOCAL = 1 << 20
SNIFF_BYTES = 8192
"""How far into a file to look for a NUL before calling it binary. A text file does not carry one; a
compiled object, an archive or an image carries one early, and 8 KiB is past every plausible header."""


class NotRegularFile(OSError):
    """The path names a named pipe, a device, a socket or a directory, which a file tool will not open."""


class FileTooLarge(ValueError):
    """The file holds more bytes than the caller reads whole."""


def _kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a named pipe (FIFO)"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISDIR(mode):
        return "a directory"
    return "not a regular file"


def open_regular(path: Path) -> BinaryIO:
    """``path`` opened for reading, refused unless it is a regular file.

    A whole read of a named pipe blocked a worker thread for good (a thread cannot be cancelled), and
    one of ``/dev/zero`` read until this process ran out of memory. The type is checked before the
    open, so a device is never opened at all, and again on the open handle, because the name can be
    swapped for a pipe in between; ``O_NONBLOCK`` keeps that swapped pipe from holding the open itself.
    A link to a regular file is followed, as before: the walls check the real path, not this.
    """
    before = os.stat(path)
    if not stat.S_ISREG(before.st_mode):
        raise NotRegularFile(f"{path} is {_kind(before.st_mode)}; only a regular file can be read")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise NotRegularFile(f"{path} is {_kind(after.st_mode)}; only a regular file can be read")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _read_capped(path: Path, limit: int) -> bytes:
    """The bytes of a regular file, a :class:`FileTooLarge` naming the size when there are more than ``limit``.

    Measured by reading, not by the size the inode reports: a file that grows while it is read (a log)
    is still cut off at ``limit`` and refused, rather than read until it stops growing."""
    with open_regular(path) as handle:
        data = handle.read(limit + 1)
        if len(data) > limit:
            size = max(os.fstat(handle.fileno()).st_size, len(data))
            raise FileTooLarge(f"{path} is {size} bytes, more than the {limit} read here")
        return data


@dataclass(slots=True)
class TextWindow:
    """A run of a text file's lines, as Read shows them, and what it knows of the rest."""

    lines: list[str]
    """The window's lines without their endings, each cut to the width asked for."""
    start: int
    """The index of the first line, counting from 0."""
    more: bool
    """At least one line follows the window."""
    size: int
    """The file's size in bytes."""
    total_lines: int | None = None
    """Every line in the file, or ``None`` when it was too large to count cheaply."""
    cut: list[int] = field(default_factory=list)
    """Indices into ``lines`` of the lines that ran past the width and were cut."""


_BYTE_BREAK = re.compile(rb"\r\n|\r|\n")


def _byte_lines(chunks: Iterable[bytes], hold: int) -> Iterator[tuple[bytes, int]]:
    """Each line of the stream without its ending, kept to ``hold`` bytes, with its whole length.

    Broken at CRLF, CR and LF, the way Read numbers lines (``split_lines``); UTF-8 never carries those
    bytes inside a character, so the split is safe before decoding. Only ``hold`` bytes of a line are
    ever held, so a single gigabyte-long line costs no more memory than a short one.
    """
    kept = bytearray()
    length = 0
    after_cr = False
    for chunk in chunks:
        if after_cr and chunk.startswith(b"\n"):
            # The CR that ended the previous chunk and this LF are one CRLF, already counted.
            chunk = chunk[1:]
        after_cr = chunk.endswith(b"\r")
        position = 0
        for match in _BYTE_BREAK.finditer(chunk):
            piece = chunk[position : match.start()]
            if len(kept) < hold:
                kept += piece[: hold - len(kept)]
            yield bytes(kept), length + len(piece)
            kept, length = bytearray(), 0
            position = match.end()
        rest = chunk[position:]
        if len(kept) < hold:
            kept += rest[: hold - len(kept)]
        length += len(rest)
    if length:
        yield bytes(kept), length


def _count_lines(handle: BinaryIO) -> int:
    """The number of lines from the start of ``handle``, counted as :func:`_byte_lines` breaks them."""
    handle.seek(0)
    count = 0
    after_cr = False
    last = b""
    while chunk := handle.read(READ_CHUNK_BYTES_LOCAL):
        count += chunk.count(b"\n") + chunk.count(b"\r") - chunk.count(b"\r\n")
        if after_cr and chunk.startswith(b"\n"):
            count -= 1
        after_cr = chunk.endswith(b"\r")
        last = chunk[-1:]
    if last and last not in (b"\n", b"\r"):
        count += 1
    return count


def _decode_line(data: bytes, *, cut: bool) -> str:
    """A line's text; a line cut short may end inside a character, which is dropped, not refused."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if cut and exc.start >= len(data) - 3 and exc.reason == "unexpected end of data":
            return data[: exc.start].decode("utf-8")
        raise


def fit_window(lines: Iterable[tuple[Any, bool]], *, max_lines: int, max_chars: int, line_chars: int, decode: Callable[[Any], str] = str) -> tuple[list[str], list[int], bool]:
    """``(shown, cut, more)``: as many of ``lines`` as fit ``max_lines`` and ``max_chars``, each cut to ``line_chars``.

    ``lines`` yields each line, still to be ``decode``-d, with whether it was already cut short. The
    budget counts the line-number column too, and the first line is always taken, so a window is never
    empty while the file has lines left. ``lines`` is read one past what fits, to know whether more
    follow; that line is decoded only when the budget has to measure it.
    """
    shown: list[str] = []
    cut: list[int] = []
    used = 0
    for raw, short in lines:
        if len(shown) >= max_lines:
            return shown, cut, True
        text = decode(raw)
        line = text[:line_chars]
        cost = len(line) + 8
        if shown and used + cost > max_chars:
            return shown, cut, True
        if short or len(text) > line_chars:
            cut.append(len(shown))
        shown.append(line)
        used += cost
    return shown, cut, False


def read_window_local(path: Path, *, start: int, max_lines: int, max_chars: int, line_chars: int) -> TextWindow:
    """The window of a local file's lines from ``start``, streamed: nothing before it or after it is held.

    A ``UnicodeDecodeError`` when the file is binary (a NUL in its head, or a window line that is not
    UTF-8), :class:`NotRegularFile` when it is not a regular file.
    """
    hold = line_chars * 4 + 4
    with open_regular(path) as handle:
        size = os.fstat(handle.fileno()).st_size
        head = handle.read(SNIFF_BYTES)
        if b"\x00" in head:
            at = head.index(b"\x00")
            raise UnicodeDecodeError("utf-8", head, at, at + 1, "a NUL byte: the file is binary")

        def window() -> Iterator[tuple[tuple[bytes, bool], bool]]:
            for index, (kept, length) in enumerate(_byte_lines(chain([head], iter(lambda: handle.read(READ_CHUNK_BYTES_LOCAL), b"")), hold)):
                if index >= start:
                    yield (kept, length > len(kept)), length > len(kept)

        shown, cut, more = fit_window(window(), max_lines=max_lines, max_chars=max_chars, line_chars=line_chars, decode=lambda item: _decode_line(item[0], cut=item[1]))
        total = _count_lines(handle) if size <= LINE_COUNT_MAX_BYTES else None
    return TextWindow(lines=shown, start=start, more=more, size=size, total_lines=total, cut=cut)


def write_untranslated(path: Path, content: str) -> None:
    """Write ``content`` exactly, creating the parent folders.

    ``newline=""`` stops Python translating ``\\n``: on native Windows every LF file came back CRLF
    after a single edit, the mirror of the read side losing CRLF on Linux.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


class LocalFS:
    """The local filesystem; the default."""

    remote = False

    async def read_text(self, path: Path, *, limit: int | None = None) -> str:
        """The file's text with its line endings as they are on disk.

        Universal newlines turned every CRLF into LF on the way in, so one Edit of a Windows file
        rewrote all its lines; the file tools now see the real endings and choose what to write.
        Only a regular file of at most ``limit`` bytes (:data:`TEXT_MAX_BYTES` unless told) is read (:class:`NotRegularFile`,
        :class:`FileTooLarge`); Read itself goes through :meth:`read_window` and has no such bound.
        """
        cap = TEXT_MAX_BYTES if limit is None else limit

        def _read() -> str:
            return _read_capped(path, cap).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def read_window(self, path: Path, *, start: int, max_lines: int, max_chars: int, line_chars: int) -> TextWindow:
        return await asyncio.to_thread(read_window_local, path, start=start, max_lines=max_lines, max_chars=max_chars, line_chars=line_chars)

    async def write_text(self, path: Path, content: str) -> None:
        await asyncio.to_thread(write_untranslated, path, content)

    async def exists(self, path: Path) -> bool:
        return path.exists()

    async def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    async def is_file(self, path: Path) -> bool:
        return path.is_file()

    async def size(self, path: Path) -> int:
        return path.stat().st_size

    async def read_bytes(self, path: Path, *, limit: int) -> bytes:
        """The file's bytes; a ``ValueError`` naming the size when it holds more than ``limit``.

        The size the inode reports was the only check, and a pipe or ``/dev/zero`` reports 0: the
        read then blocked for good or ran until memory ran out. Only a regular file is opened now,
        and the bound is enforced on what is actually read."""
        return await asyncio.to_thread(_read_capped, path, limit)

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
NOT_REGULAR_MARKER = "__DAEDALUS_NOT_REGULAR__"
MISSING_MARKER = "__DAEDALUS_MISSING__"
TOO_LARGE_MARKER = "__DAEDALUS_TOO_LARGE__"
WINDOW_MARKER = "__DAEDALUS_WINDOW__"
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

    async def read_text(self, path: Path, *, limit: int | None = None) -> str:
        """The file's text, refused unless it is a regular file of at most ``limit`` bytes.

        ``grep`` and ``cat`` of a named pipe wait for a writer until the command times out, and of a
        device never end; ``test -f`` is false for both, so neither is opened."""
        limit = TEXT_MAX_BYTES if limit is None else limit
        quoted = shlex.quote(str(path))
        outcome = await self._run(
            f"if [ ! -f {quoted} ]; then echo {NOT_REGULAR_MARKER}; exit 4; fi; "
            f"size=$(wc -c < {quoted} | tr -d ' '); if [ \"$size\" -gt {int(limit)} ]; then echo {TOO_LARGE_MARKER} $size; exit 5; fi; "
            f"if grep -qI . {quoted} || [ ! -s {quoted} ]; then cat {quoted}; else echo {BINARY_MARKER}; exit 3; fi"
        )
        if outcome.exit_code == 3 and BINARY_MARKER in outcome.output:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file")
        if outcome.exit_code == 5 and TOO_LARGE_MARKER in outcome.output:
            size = outcome.output.split(TOO_LARGE_MARKER, 1)[1].split()[0]
            raise FileTooLarge(f"{path} is {size} bytes, more than the {limit} read here")
        if outcome.exit_code == 4 and NOT_REGULAR_MARKER in outcome.output:
            raise NotRegularFile(f"{path} is not a regular file; only a regular file can be read")
        if outcome.exit_code != 0:
            raise FileNotFoundError(outcome.output.strip() or str(path))
        return outcome.output

    async def read_window(self, path: Path, *, start: int, max_lines: int, max_chars: int, line_chars: int) -> TextWindow:
        """The window of lines from ``start``, cut on the other machine so only the window comes back.

        ``tail``/``head`` break lines at LF only, so a file whose lines end in a bare CR (old Mac text)
        reads as one long line here, where the local read breaks it; CRLF reads the same both ways.
        The type is checked first, so a pipe or a device is refused without being opened.
        """
        quoted = shlex.quote(str(path))
        hold = line_chars * 4 + 4
        byte_cap = max_chars * 4 + hold
        command = (
            f"f={quoted}; "
            f"if [ ! -e \"$f\" ]; then echo {MISSING_MARKER}; exit 4; fi; "
            "if [ ! -f \"$f\" ]; then k='not a regular file'; [ -d \"$f\" ] && k='a directory'; [ -p \"$f\" ] && k='a named pipe (FIFO)'; "
            "[ -c \"$f\" ] && k='a character device'; [ -b \"$f\" ] && k='a block device'; [ -S \"$f\" ] && k='a socket'; "
            f"echo \"{NOT_REGULAR_MARKER} $k\"; exit 5; fi; "
            "size=$(wc -c < \"$f\" | tr -d ' '); "
            f"nul=$(head -c {SNIFF_BYTES} \"$f\" | tr -dc '\\000' | wc -c | tr -d ' '); "
            f"if [ \"$size\" -le {LINE_COUNT_MAX_BYTES} ]; then lines=$(wc -l < \"$f\" | tr -d ' '); "
            "if [ \"$size\" -gt 0 ] && [ -n \"$(tail -c 1 \"$f\" | tr -d '\\n')\" ]; then lines=$((lines + 1)); fi; else lines=-1; fi; "
            f"echo \"{WINDOW_MARKER} $size $lines $nul\"; "
            "if [ \"$nul\" -gt 0 ]; then exit 3; fi; "
            # Each stage silenced, as in read_bytes: where SIGPIPE is ignored a stage cut off by the one
            # after it prints "Broken pipe", and the merged stream would carry that into the window.
            f"tail -n +{max(start, 0) + 1} \"$f\" 2>/dev/null | head -n {max_lines + 1} 2>/dev/null | cut -b 1-{hold} 2>/dev/null | head -c {byte_cap}"
        )
        outcome = await self._run(command)
        output = outcome.output
        if outcome.exit_code == 4 and MISSING_MARKER in output:
            raise FileNotFoundError(f"no such file: {path}")
        if outcome.exit_code == 5 and NOT_REGULAR_MARKER in output:
            kind = (output.split(NOT_REGULAR_MARKER, 1)[1].strip().splitlines() or ["not a regular file"])[0]
            raise NotRegularFile(f"{path} is {kind}; only a regular file can be read")
        if WINDOW_MARKER not in output:
            raise OSError(output.strip()[-300:] or f"could not read {path}")
        meta, _, body = output.split(WINDOW_MARKER, 1)[1].partition("\n")
        try:
            size, lines, nul = (int(part) for part in meta.split()[:3])
        except ValueError:
            raise OSError(f"could not read {path}: {meta.strip()[:200]}") from None
        if nul > 0 or outcome.exit_code == 3:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "a NUL byte: the file is binary")
        if outcome.exit_code != 0:
            raise OSError(body.strip()[-300:] or f"could not read {path}")
        raw = body.split("\n")
        if body.endswith("\n"):
            raw.pop()
        clipped = len(body.encode("utf-8", "replace")) >= byte_cap and len(raw) > 1
        if clipped:
            raw.pop()  # cut by the byte cap partway through: shown on the next call instead
        items = [(line.removesuffix("\r"), len(line.encode("utf-8", "replace")) >= hold) for line in raw]
        shown, cut, more = fit_window(items, max_lines=max_lines, max_chars=max_chars, line_chars=line_chars)
        total = lines if lines >= 0 else None
        more = more or clipped or (total is not None and start + len(shown) < total)
        return TextWindow(lines=shown, start=start, more=more, size=size, total_lines=total, cut=cut)

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


__all__ = ["ExecBackend", "ExecOutcome", "FileTooLarge", "LocalFS", "NotRegularFile", "ShellFS", "TextWindow", "open_regular", "write_untranslated"]
