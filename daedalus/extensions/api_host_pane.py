"""The session file pane over a folder on the host, for a Docker installation.

A session that works in a host folder — a plain chat started on the host, or any session of a
project whose folder is the host's — writes its files where this container cannot see them: its
directory here holds nothing but the inbox. So every route of the pane that would read the
session's folder with ``os`` reads it through the host terminal bridge instead, and answers in the
same shapes, so the app cannot tell the two apart.

The bridge offers files and listings (``fs.stat``, ``fs.list``, ``fs.read``) under the daemon's
roots, and ``exec.run`` of ``bash`` for the rest: resolving a path, finding a name, searching the
text, and writing an upload (the daemon's own ``fs.write`` takes nothing outside a staff inbox, so an
upload is put on the host the way a message's attachment is, through ``cat`` reading stdin).

Containment is held twice. Here, a path must be relative, without ``..``, and its real path on the
host — symlinks resolved there — must stay under the real path of the folder; the daemon then
refuses anything outside its roots or on its deny list in its own words. The daemon's roots are
every host folder of every project, so the check here is what keeps the pane inside this one.
"""

from __future__ import annotations

import asyncio
import fnmatch
import mimetypes
import posixpath
import re
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from daedalus.host.host_exec import PRELUDE, STDIN_CHUNK

if TYPE_CHECKING:
    from daedalus.terminals.bridge import HostBridge

_T = TypeVar("_T")
HOST = "host"

LIST_SKIP = frozenset({".git", "__pycache__", "node_modules", ".venv"})
"""What the pane never lists, as the local listing leaves it out."""
SEARCH_SKIP = (".git", "__pycache__", "node_modules", ".venv", ".checkpoints", ".mypy_cache", ".ruff_cache", ".pytest_cache")
LIST_LIMIT = 5000
"""The most entries one listing asks the daemon for: its own cap on ``fs.list``."""
TEXT_PREVIEW_BYTES = 512_000
"""What a file read shows before it says ``truncated``: the local pane's cut, and under the 640 KiB
one reply of the daemon carries, so a preview is one round trip."""
READ_CHUNK = 512 << 10
"""Bytes asked for per ``fs.read`` while a download streams: the most one reply carries comfortably."""
DOWNLOAD_MAX_BYTES = 512 << 20
"""The largest file sent whole through the bridge. Every half megabyte is a round trip to the host,
so a file past this is refused with the reason rather than tying up the daemon for minutes; a range
request (a video seeking) is served from a file of any size, since it reads only what it names."""
SEARCH_TIMEOUT_SECONDS = 5.0
"""How long a name search or a text search may run on the host. Longer than the local budget: the
round trip and the profile cost what a local walk does not, and the search answers a key press only
after the app has debounced it."""
SEARCH_MAX_LINES = 20_000
"""The most paths a name search reads back, as the local walk visits at most this many entries."""
GREP_MAX_FILESIZE = "1M"
GREP_MAX_COLUMNS = 300
EXEC_TIMEOUT_SECONDS = 20.0
STAT_CONCURRENCY = 16

NO_GREP = "content search needs ripgrep (rg) or grep on the host, and neither is installed there; search by name instead"


def _mtime(value: Any) -> float:
    """The daemon's modification time (RFC 3339 with nanoseconds, from Go) as the epoch seconds the
    local pane gives; Python reads at most microseconds, so the fraction is cut to six digits."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "")
    if not text:
        return 0.0
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _lines(output: str, cut: bool) -> list[str]:
    """Non-empty lines of a program's output; the last one is dropped when the output was cut, since
    it may end in the middle of a path."""
    lines = output.split("\n")
    if cut and lines:
        lines = lines[:-1]
    return [line for line in lines if line]


def content_disposition(name: str, kind: str = "attachment") -> str:
    """The header Starlette's ``FileResponse`` writes, so a host download names its file the same way."""
    quoted = quote(name)
    return f"{kind}; filename*=utf-8''{quoted}" if quoted != name else f'{kind}; filename="{name}"'


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """One ``bytes=`` range as (first, last) inclusive, or ``None`` for no usable range (the whole
    file is sent). Several ranges are answered as the whole file too, which HTTP allows."""
    match = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if match is None or size <= 0:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:
        length = int(last)
        if length == 0:
            raise HTTPException(416, "the range asks for nothing")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise HTTPException(416, f"the range lies outside the file of {size} bytes")
    return start, end


class HostPane:
    """The pane rooted at ``root``, a folder on the host, read and written through ``bridge``."""

    def __init__(self, bridge: HostBridge, root: str) -> None:
        self.bridge = bridge
        self.root = posixpath.normpath(root)
        self._real_root: str | None = None

    # -- paths ------------------------------------------------------------------------------

    def target(self, rel: str) -> str:
        """``rel`` under the folder, as the host spells it. Refused when absolute, when it climbs with
        ``..``, or when it carries a byte no path may: the pane names files relative to its root and
        nothing a request types reaches past it."""
        raw = (rel or "").replace("\\", "/")
        if "\0" in raw:
            raise HTTPException(400, "a path cannot contain a NUL byte")
        if raw.startswith("/"):
            raise HTTPException(400, "path escapes the workspace")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if ".." in parts:
            raise HTTPException(400, "path escapes the workspace")
        return posixpath.join(self.root, *parts) if parts else self.root

    def inside(self, real: str, real_root: str) -> bool:
        return real == real_root or real.startswith(real_root.rstrip("/") + "/")

    async def reals(self, paths: list[str]) -> list[str | None]:
        """The real paths on the host of ``paths``, symlinks resolved there, in order; ``None`` for one
        that does not resolve. One program run for all of them, NUL-separated so any name survives."""
        script = 'for p in "$@"; do r=$(realpath -- "$p" 2>/dev/null) || r=""; printf "%s\\0" "$r"; done'
        result = await self._exec(["bash", "-c", script, "realpath", *paths], cwd=self.root)
        if result.exit_code != 0:
            raise HTTPException(502, f"the host could not resolve the path: {(result.stderr or result.stdout).strip()[-300:]}")
        answers = result.stdout.split("\0")
        return [(answers[i] or None) if i < len(answers) else None for i in range(len(paths))]

    def _found_root(self, real: str | None) -> str:
        """The folder's real path as the host answered it. No answer means the check cannot be made —
        no ``realpath`` there, or the folder gone — and the pane then refuses rather than serving a
        path it could not hold to the folder."""
        if real is None:
            raise HTTPException(502, f"the host could not say where {self.root} really is, so no path in it can be checked; is realpath installed there?")
        self._real_root = real
        return real

    async def root_real(self) -> str:
        """The folder's own real path on the host, asked once per request."""
        if self._real_root is None:
            [real] = await self.reals([self.root])
            return self._found_root(real)
        return self._real_root

    async def contained(self, target: str) -> str:
        """``target`` once its real path is known to be under the folder's; 400 when a symlink takes it
        out. A path that does not exist yet is checked through its nearest existing parent."""
        if target == self.root:
            return target
        probe = target
        if self._real_root is None:
            # One program run answers both, the folder's real path and the path's.
            root_real, real = await self.reals([self.root, probe])
            real_root = self._found_root(root_real)
        else:
            [real] = await self.reals([probe])
            real_root = self._real_root
        while real is None and probe != self.root:
            probe = posixpath.dirname(probe)
            [real] = await self.reals([probe])
        if real is not None and not self.inside(real, real_root):
            raise HTTPException(400, "path escapes the workspace")
        return target

    # -- the bridge -------------------------------------------------------------------------

    async def _host(self, pending: Awaitable[_T], what: str) -> _T:
        """A bridge call with its errors as the pane's answers: the bridge being down is 503 with the
        way to bring it back, a missing path 404, any other refusal of the daemon 403 in its words."""
        try:
            return await pending
        except HTTPException:
            raise
        except ConnectionError as exc:
            raise HTTPException(503, f"this folder is on the host, and {exc}") from None
        except FileNotFoundError:
            raise HTTPException(404, f"no such path: {what}") from None
        except OSError as exc:
            raise HTTPException(403, str(exc) or f"the host refused {what}") from None

    async def _exec(self, argv: list[str], *, cwd: str, timeout: float = EXEC_TIMEOUT_SECONDS, stdin: bytes | None = None) -> Any:
        return await self._host(self.bridge.exec_run(HOST, argv, cwd=cwd, timeout=timeout, stdin=stdin), argv[0])

    async def _stat(self, target: str) -> dict[str, Any]:
        return await self._host(self.bridge.stat(target), target)

    async def _list(self, target: str) -> dict[str, Any]:
        return await self._host(self.bridge.list_dir(target, limit=LIST_LIMIT), target)

    # -- the routes -------------------------------------------------------------------------

    async def read_path(self, rel: str) -> dict[str, Any]:
        """A directory listing or a file's text, in the shape of the local pane's ``_read_path``."""
        target = await self.contained(self.target(rel))
        stat = await self._stat(target)
        if not stat.get("exists"):
            raise HTTPException(404, "no such path")
        if stat.get("type") == "file":
            size = int(stat.get("size") or 0)
            chunk = await self._host(self.bridge.read(target, offset=0, max_bytes=TEXT_PREVIEW_BYTES), target)
            if size > TEXT_PREVIEW_BYTES:
                return {"path": rel, "kind": "file", "truncated": True, "content": chunk.data.decode("utf-8", errors="replace")}
            try:
                return {"path": rel, "kind": "file", "content": chunk.data.decode("utf-8")}
            except UnicodeDecodeError:
                return {"path": rel, "kind": "binary", "size": size}
        if stat.get("type") != "dir":
            raise HTTPException(404, "no such path")
        listing = await self._list(target)
        raw = [entry for entry in listing.get("entries") or [] if str(entry.get("name")) not in LIST_SKIP]
        links = [entry for entry in raw if entry.get("type") == "symlink"]
        followed: dict[str, dict[str, Any]] = {}
        if links:
            # A symlink is listed by the daemon as one, not followed: it is shown only when it points
            # inside the folder, and then as what it points at, as the local listing shows it.
            paths = [posixpath.join(target, str(entry.get("name"))) for entry in links]
            reals = await self.reals(paths)
            real_root = await self.root_real()
            for entry, path, real in zip(links, paths, reals, strict=True):
                if real is not None and self.inside(real, real_root):
                    try:
                        followed[str(entry.get("name"))] = await self.bridge.stat(path)
                    except OSError:
                        continue
        entries = []
        for entry in raw:
            name = str(entry.get("name"))
            kind = entry.get("type")
            size, mtime = entry.get("size"), entry.get("mtime")
            if kind == "symlink":
                seen = followed.get(name)
                if seen is None or not seen.get("exists"):
                    continue
                kind, size, mtime = seen.get("type"), seen.get("size"), seen.get("mtime")
            if kind not in ("file", "dir"):
                continue
            entries.append({"name": name, "dir": kind == "dir", "size": int(size or 0), "mtime": _mtime(mtime)})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"path": rel, "kind": "dir", "entries": entries}

    async def download(self, rel: str, range_header: str = "") -> StreamingResponse:
        """The file streamed from the host a reply at a time, with the headers ``FileResponse`` gives
        and a single byte range honoured, so an image shows, a text previews and a video seeks."""
        target = await self.contained(self.target(rel))
        stat = await self._stat(target)
        if not stat.get("exists") or stat.get("type") != "file":
            raise HTTPException(404, "no such file")
        size = int(stat.get("size") or 0)
        wanted = parse_range(range_header, size)
        if wanted is None and size > DOWNLOAD_MAX_BYTES:
            raise HTTPException(413, f"the file is {size >> 20} MB, more than the {DOWNLOAD_MAX_BYTES >> 20} MB fetched whole from the host; copy it off the host another way")
        first, last = wanted if wanted is not None else (0, size - 1)
        name = posixpath.basename(target)
        headers = {
            "Access-Control-Allow-Origin": "https://web.telegram.org",
            "Content-Disposition": content_disposition(name),
            "Accept-Ranges": "bytes",
            "Content-Length": str(max(0, last - first + 1)),
        }
        if wanted is not None:
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"

        async def body() -> AsyncIterator[bytes]:
            offset = first
            while offset <= last:
                chunk = await self.bridge.read(target, offset=offset, max_bytes=min(READ_CHUNK, last - offset + 1))
                if not chunk.data:
                    return  # shrunk while streaming: what was there is what is sent
                yield chunk.data
                offset += len(chunk.data)

        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return StreamingResponse(body(), status_code=206 if wanted is not None else 200, media_type=media, headers=headers)

    async def search_names(self, query: str, limit: int) -> tuple[list[dict[str, Any]], bool, str]:
        """Files and folders whose name matches ``query``, as the local ``_search_names`` answers.

        The list of files comes from the host — ripgrep's when it is there, which honours
        .gitignore, else ``find`` — and the matching is done here, by the same rule as the local
        search. A plain query is narrowed on the host first with ``grep -F``: every path a name of
        it matches contains the query, so nothing that would match is lost, and the output the
        daemon carries back stays small. A glob, or a query outside ASCII where a host without a
        UTF-8 locale folds case differently, is matched on the whole list.
        """
        pattern = query.lower()
        is_glob = any(ch in query for ch in "*?[")
        narrow = "" if is_glob or not query.isascii() else query
        skip_rg = " ".join(f"--glob {shlex.quote('!' + name)}" for name in SEARCH_SKIP)
        prune = " -o ".join(f"-name {shlex.quote(name)}" for name in SEARCH_SKIP)
        script = PRELUDE + (
            'cd -- "$1" || exit 3\n'
            "if command -v rg >/dev/null 2>&1; then echo rg; list() { rg --files --no-messages " + skip_rg + "; }\n"
            "else echo walk; list() { find . -mindepth 1 -type d \\( -name '.*' -o " + prune + " \\) -prune -o -type f -print; }; fi\n"
            'if [ -n "$2" ]; then list 2>/dev/null | grep -iF -e "$2" | head -n "$3"; else list 2>/dev/null | head -n "$3"; fi\n'
        )
        result = await self._exec(["bash", "-c", script, "search", self.root, narrow, str(SEARCH_MAX_LINES + 1)], cwd=self.root, timeout=SEARCH_TIMEOUT_SECONDS)
        lines = _lines(result.stdout, bool(result.truncated or result.timed_out))
        engine = lines.pop(0) if lines and lines[0] in ("rg", "walk") else "walk"
        truncated = bool(result.timed_out or result.truncated) or len(lines) > SEARCH_MAX_LINES
        lines = lines[:SEARCH_MAX_LINES]

        def matches(name: str, rel: str) -> bool:
            if is_glob:
                return fnmatch.fnmatch(name.lower(), pattern) or fnmatch.fnmatch(rel.lower(), pattern)
            return pattern in name.lower()

        found: list[tuple[str, str]] = []
        offered: set[str] = set()
        for line in lines:
            rel = line[2:] if line.startswith("./") else line
            parts = rel.split("/")
            if not rel or ".." in parts:
                continue
            for depth in range(1, len(parts)):
                branch = "/".join(parts[:depth])
                if branch not in offered and matches(parts[depth - 1], branch):
                    offered.add(branch)
                    found.append((branch, "dir"))
            if rel not in offered and matches(parts[-1], rel):
                offered.add(rel)
                found.append((rel, "file"))
            if len(found) >= limit:
                truncated = True
                break
        found = found[:limit]
        gate = asyncio.Semaphore(STAT_CONCURRENCY)

        async def described(rel: str, kind: str) -> dict[str, Any] | None:
            async with gate:
                try:
                    stat = await self.bridge.stat(posixpath.join(self.root, rel))
                except OSError:
                    return None
            if not stat.get("exists"):
                return None
            return {"path": rel, "kind": kind, "size": int(stat.get("size") or 0), "mtime": _mtime(stat.get("mtime"))}

        results = [r for r in await asyncio.gather(*(described(rel, kind) for rel, kind in found)) if r is not None]
        return results, truncated, engine

    async def search_content(self, query: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """Lines containing ``query`` literally, as the local ``_search_content`` answers: ripgrep on the
        host with the same options, else ``grep -r``; 501 when the host has neither."""
        skip_rg = " ".join(f"--glob {shlex.quote('!' + name)}" for name in SEARCH_SKIP)
        skip_grep = " ".join(f"--exclude-dir={shlex.quote(name)}" for name in SEARCH_SKIP)
        # ripgrep's --smart-case, spelled for grep: case matters only when the query has a capital.
        fold = "" if any(ch.isupper() for ch in query) else "-i "
        script = PRELUDE + (
            'cd -- "$1" || exit 3\n'
            "if command -v rg >/dev/null 2>&1; then echo rg; rg --line-number --no-heading --color never --no-messages"
            f" --fixed-strings --smart-case --max-filesize {GREP_MAX_FILESIZE} --max-columns {GREP_MAX_COLUMNS}"
            f' --max-columns-preview --null {skip_rg} -- "$2" 2>/dev/null | head -n "$3"\n'
            f'elif command -v grep >/dev/null 2>&1; then echo grep; grep -rnIF --null {fold}{skip_grep} -e "$2" -- . 2>/dev/null | head -n "$3"\n'
            "else echo none; fi\n"
        )
        result = await self._exec(["bash", "-c", script, "grep", self.root, query, str(limit + 1)], cwd=self.root, timeout=SEARCH_TIMEOUT_SECONDS)
        lines = _lines(result.stdout, bool(result.truncated or result.timed_out))
        engine = lines.pop(0) if lines and lines[0] in ("rg", "grep", "none") else ""
        if engine == "none":
            raise HTTPException(501, NO_GREP)
        hits: list[dict[str, Any]] = []
        truncated = bool(result.timed_out or result.truncated)
        for line in lines:
            rel, sep, rest = line.partition("\0")
            number, _, text = rest.partition(":")
            rel = rel[2:] if rel.startswith("./") else rel
            if not sep or not rel or not number.isdigit() or rel.startswith("/") or ".." in rel.split("/"):
                continue
            hits.append({"path": rel, "line": int(number), "text": text[:GREP_MAX_COLUMNS]})
            if len(hits) >= limit:
                truncated = True
                break
        return hits, truncated

    async def store_uploads(self, files: list[UploadFile], sub: str = "") -> list[str]:
        """Write uploads under ``sub`` without clobbering what is there; returns the names used.

        Each file goes up in pieces under the daemon's cap on stdin, read from the upload as it
        arrives rather than held whole. The first piece is written with the shell's noclobber, so a
        file that appeared since the names were listed is refused rather than overwritten.
        """
        folder = await self.contained(self.target(sub))
        try:
            listing = await self.bridge.list_dir(folder, limit=LIST_LIMIT)
            taken = {str(entry.get("name")) for entry in listing.get("entries") or []}
        except FileNotFoundError:
            taken = set()
        except ConnectionError as exc:
            raise HTTPException(503, f"this folder is on the host, and {exc}") from None
        except OSError as exc:
            raise HTTPException(403, str(exc)) from None
        names: list[str] = []
        for upload_file in files:
            name = posixpath.basename((upload_file.filename or "file").replace("\\", "/")) or "file"
            stem, suffix = posixpath.splitext(name)
            chosen = name
            counter = 1
            while chosen in taken:
                chosen = f"{stem}-{counter}{suffix}"
                counter += 1
            taken.add(chosen)
            await self._put(folder, chosen, upload_file.read)
            names.append(chosen)
        return names

    async def _put(self, folder: str, name: str, read: Callable[[int], Awaitable[bytes]]) -> None:
        path = posixpath.join(folder, name)
        first = True
        while True:
            piece = await read(STDIN_CHUNK)
            if not piece and not first:
                return
            if first:
                script = 'mkdir -p -- "$1" && set -C && cat > "$2"'
            else:
                script = 'cat >> "$2"'
            result = await self._exec(["bash", "-c", script, "put", folder, path], cwd=self.root, timeout=120.0, stdin=piece)
            if result.exit_code != 0:
                raise HTTPException(409 if first else 502, f"could not write {name} on the host: {(result.stderr or result.stdout).strip()[-300:] or 'no answer'}")
            first = False
            if not piece:
                return


__all__ = ["HostPane", "content_disposition", "parse_range"]
