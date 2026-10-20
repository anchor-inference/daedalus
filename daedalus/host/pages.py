"""Custom pages kept as data: a folder per page in the state volume, never a change to the app.

    <state>/pages/<slug>/
        page.json          the manifest: what the page is called, its icon, its versions
        draft/             the files being worked on
        versions/<n>/      each published version, exactly as it was published

Publishing copies the draft into a fresh version directory under a temporary name and renames it
into place, then rewrites the manifest by replacing it: a reader sees the old version or the new one,
never half of either, and a process killed in between leaves an orphan directory that the next
publish numbers past rather than a manifest pointing at nothing.

What a page contains is the operator's business. There is no allow-list of file types, frameworks or
sizes; the caps in ``[pages]`` only stop a runaway writer from filling the disk, and 0 lifts them.
The only things refused are a path that would leave the page's folder and a slug that cannot be a
single path segment, because those are what keep one page from writing over another or over the
rest of the state volume.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from daedalus.config import PagesConfig
from daedalus.slugs import is_page_slug

if TYPE_CHECKING:
    from daedalus.stores.database import Database

Change = Literal["created", "updated", "draft", "published", "rolled_back", "removed"]


KINDS = ("static", "service", "link")
Kind = Literal["static", "service", "link"]

ICON_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]{1,4000}$")
"""A single SVG path's ``d``: commands and numbers only, so it can be drawn inside the app's own
``<svg>`` without carrying markup in with it."""
ICON_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
TITLE_MAX = 120
MANIFEST = "page.json"
EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
"""A name a page listens for with ``Daedalus.on(name)``."""
RESERVED_EVENTS = ("theme", "agent.")
"""Names the bridge itself delivers: an emitted ``theme`` would be written onto the page's root as
its colours, and ``agent.*`` are the page agent's own conversation, which the app forwards."""


class PageError(ValueError):
    """A request the store refuses; ``status`` is the HTTP answer it maps to."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_slug(slug: str) -> str:
    if not is_page_slug(slug):
        raise PageError("a page's slug is 1 to 63 lower-case letters (of any script), digits and hyphens, starting with a letter or digit")
    return slug


def clean_path(path: str) -> str:
    """A file's path inside a page, normalised; refuses anything that could leave the page's folder.

    Checked as text before anything touches the disk: an absolute path, a drive letter, a backslash
    (a separator on Windows, where the desktop install runs), ``..`` or ``.`` as a segment, an empty
    segment, or a NUL. The resolved location is checked again where a file is opened.
    """
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path or len(path) > 1024:
        raise PageError(f"not a usable path inside a page: {path!r}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise PageError(f"a path inside a page is relative: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PageError(f"not a usable path inside a page: {path!r}")
    return str(PurePosixPath(*parts))


def clean_title(title: Any) -> str | dict[str, str]:
    """A plain title, or one per language (``{"en": …, "ru": …}``)."""
    if isinstance(title, str):
        text = title.strip()
        if not text or len(text) > TITLE_MAX:
            raise PageError(f"a page's title is 1 to {TITLE_MAX} characters")
        return text
    if isinstance(title, dict) and title:
        out: dict[str, str] = {}
        for lang, text in title.items():
            if not isinstance(lang, str) or not re.fullmatch(r"[a-z]{2,3}", lang) or not isinstance(text, str) or not text.strip() or len(text) > TITLE_MAX:
                raise PageError(f"a page's title per language maps a language code to 1 to {TITLE_MAX} characters")
            out[lang] = text.strip()
        return out
    raise PageError("a page needs a title: a string, or one per language")


def title_text(title: str | dict[str, str], lang: str = "en") -> str:
    if isinstance(title, str):
        return title
    return title.get(lang) or title.get("en") or next(iter(title.values()))


def clean_target(kind: str, target: Any) -> int | str | None:
    """What a service or a link page points at: a port or a service's name, or an http(s) address."""
    if kind == "static":
        return None
    if kind == "service":
        if isinstance(target, bool):
            raise PageError("a service page's target is a port or a service's name")
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            port = int(target)
            if not 1 <= port <= 65535:
                raise PageError("a service page's port is 1 to 65535")
            return port
        if isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", target):
            return target
        raise PageError("a service page's target is a port or a service's name")
    if kind == "link":
        if isinstance(target, str) and re.match(r"^https?://[^\s]+$", target) and len(target) <= 2000:
            return target
        raise PageError("a link page's target is an http or https address")
    raise PageError(f"a page's kind is one of {', '.join(KINDS)}")


@dataclass(frozen=True, slots=True)
class Served:
    """What the live (or draft) side of a page serves: its kind, entry, target and file folder."""

    kind: Kind
    entry: str
    target: int | str | None
    root: Path
    version: int | None


class PageStore:
    """The pages folder, and each page's storage in the database.

    Every change to one page holds that page's lock; reads take none, because every write that a
    reader could observe is a rename. ``config`` may be a function, so the caps follow a settings
    change without a restart; ``on_change`` hears of every change that an open app should redraw for.
    """

    def __init__(self, root: Path, config: PagesConfig | Callable[[], PagesConfig] | None = None, *, db: Database | None = None,
                 on_change: Callable[[str, Change, int | None], Awaitable[None]] | None = None,
                 on_emit: Callable[[str, str, Any], Awaitable[None]] | None = None) -> None:
        self.root = root
        self._config = config or PagesConfig()
        self.db = db
        self.on_change = on_change
        self.on_emit = on_emit
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def config(self) -> PagesConfig:
        return self._config() if callable(self._config) else self._config

    async def _changed(self, slug: str, change: Change, version: int | None) -> None:
        if self.on_change is not None:
            await self.on_change(slug, change, version)

    # -- paths ----------------------------------------------------------------------------------

    def folder(self, slug: str) -> Path:
        return self.root / check_slug(slug)

    def _lock(self, slug: str) -> asyncio.Lock:
        return self._locks.setdefault(slug, asyncio.Lock())

    @staticmethod
    def _inside(base: Path, rel: str) -> Path:
        target = base / clean_path(rel)
        # A link planted inside a page's folder (by hand, or by an older tool) must not become a way
        # out of it: the resolved location has to stay under the resolved folder.
        resolved_base = base.resolve()
        resolved = target.resolve()
        if resolved != resolved_base and resolved_base not in resolved.parents:
            raise PageError(f"{rel!r} leads outside the page")
        return target

    # -- the manifest ---------------------------------------------------------------------------

    def _read(self, slug: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.folder(slug) / MANIFEST).read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise PageError(f"the page {slug!r} has an unreadable manifest: {exc}", 500) from exc
        return data if isinstance(data, dict) else None

    def _write(self, slug: str, manifest: dict[str, Any]) -> None:
        folder = self.folder(slug)
        temporary = folder / f".{MANIFEST}.{secrets.token_hex(4)}"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, folder / MANIFEST)

    async def get(self, slug: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read, slug)

    async def require(self, slug: str) -> dict[str, Any]:
        manifest = await self.get(slug)
        if manifest is None:
            raise PageError(f"no page {slug!r}", 404)
        return manifest

    async def list(self) -> list[dict[str, Any]]:
        def scan() -> list[dict[str, Any]]:
            if not self.root.is_dir():
                return []
            out = []
            for entry in sorted(self.root.iterdir()):
                # macOS's older filesystems hand names back decomposed; the slug is the composed form.
                slug = unicodedata.normalize("NFC", entry.name)
                if not entry.is_dir() or not is_page_slug(slug):
                    continue
                try:
                    manifest = self._read(slug)
                except PageError:
                    continue  # one broken page must not hide the others
                if manifest is not None:
                    out.append(manifest)
            return out

        return await asyncio.to_thread(scan)

    # -- creating and describing ----------------------------------------------------------------

    async def create(self, slug: str, *, title: Any, kind: str = "static", entry: str = "index.html", target: Any = None,
                     icon: str | None = None, icon_path: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        check_slug(slug)
        if kind not in KINDS:
            raise PageError(f"a page's kind is one of {', '.join(KINDS)}")
        manifest: dict[str, Any] = {
            "slug": slug,
            "title": clean_title(title),
            "icon": self._icon(icon),
            "icon_path": self._icon_path(icon_path),
            "session_id": session_id or None,
            "created_at": _now(),
            "updated_at": _now(),
            "version": None,
            "versions": [],
            "draft": {"kind": kind, "entry": clean_path(entry), "target": clean_target(kind, target), "updated_at": _now()},
        }
        async with self._lock(slug):
            def make() -> None:
                folder = self.folder(slug)
                if (folder / MANIFEST).exists():
                    raise PageError(f"a page {slug!r} already exists", 409)
                (folder / "draft").mkdir(parents=True, exist_ok=True)
                (folder / "versions").mkdir(exist_ok=True)
                self._write(slug, manifest)

            await asyncio.to_thread(make)
        await self._changed(slug, "created", None)
        return manifest

    async def update(self, slug: str, **fields: Any) -> dict[str, Any]:
        """Change what describes a page. The title, icon and session apply at once; the kind, entry
        and target are the draft's and go live with the next publish."""
        async with self._lock(slug):
            manifest = await self.require(slug)
            draft = dict(manifest["draft"])
            for name, value in fields.items():
                if name == "title":
                    manifest["title"] = clean_title(value)
                elif name == "icon":
                    manifest["icon"] = self._icon(value)
                elif name == "icon_path":
                    manifest["icon_path"] = self._icon_path(value)
                elif name == "session_id":
                    manifest["session_id"] = value or None
                elif name == "kind":
                    if value not in KINDS:
                        raise PageError(f"a page's kind is one of {', '.join(KINDS)}")
                    draft["kind"] = value
                elif name == "entry":
                    draft["entry"] = clean_path(value)
                elif name == "target":
                    draft["target"] = value
                else:
                    raise PageError(f"a page has no field {name!r}")
            draft["target"] = clean_target(draft["kind"], draft.get("target"))
            if draft != manifest["draft"]:
                draft["updated_at"] = _now()
            manifest["draft"] = draft
            manifest["updated_at"] = _now()
            await asyncio.to_thread(self._write, slug, manifest)
        await self._changed(slug, "updated", manifest.get("version"))
        return manifest

    @staticmethod
    def _icon(icon: Any) -> str | None:
        if icon in (None, ""):
            return None
        if not isinstance(icon, str) or not ICON_NAME_RE.fullmatch(icon):
            raise PageError("a page's icon is the name of one of the app's icons")
        return icon

    @staticmethod
    def _icon_path(icon_path: Any) -> str | None:
        if icon_path in (None, ""):
            return None
        if not isinstance(icon_path, str) or not ICON_PATH_RE.fullmatch(icon_path.strip()):
            raise PageError("a page's icon_path is a single SVG path (its d attribute), on the 24-unit grid")
        return icon_path.strip()

    # -- the draft's files ----------------------------------------------------------------------

    def _usage(self, base: Path) -> tuple[int, int]:
        files = size = 0
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files += 1
                size += path.stat().st_size
        return files, size

    def files(self, slug: str, version: int | None = None) -> list[dict[str, Any]]:
        """The files of the draft (``version`` None) or of one published version, synchronous."""
        base = self.folder(slug) / ("draft" if version is None else f"versions/{version}")
        if not base.is_dir():
            return []
        out = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                out.append({"path": path.relative_to(base).as_posix(), "bytes": stat.st_size,
                            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")})
        return out

    async def list_files(self, slug: str, version: int | None = None) -> list[dict[str, Any]]:
        manifest = await self.require(slug)
        if version is not None and not any(v["n"] == version for v in manifest["versions"]):
            raise PageError(f"the page {slug!r} has no version {version}", 404)
        return await asyncio.to_thread(self.files, slug, version)

    def draft_file(self, slug: str, path: str) -> Path:
        return self._inside(self.folder(slug) / "draft", path)

    def version_file(self, slug: str, version: int, path: str) -> Path:
        return self._inside(self.folder(slug) / "versions" / str(version), path)

    async def write_file(self, slug: str, path: str, data: bytes) -> dict[str, Any]:
        async def one() -> AsyncIterator[bytes]:
            yield data

        return await self.write_stream(slug, path, one())

    async def write_stream(self, slug: str, path: str, chunks: AsyncIterator[bytes]) -> dict[str, Any]:
        """Write one draft file from a stream, refusing it the moment it would cross a cap.

        The bytes go to a temporary file beside the target and are renamed over it only when the whole
        body arrived inside the caps, so a refused or interrupted upload leaves the old file as it was.
        """
        async with self._lock(slug):
            manifest = await self.require(slug)
            target = self.draft_file(slug, path)
            config = self.config
            room: int | None = None
            # The draft is walked only when a cap asks for its size: with both lifted, a page of tens of
            # thousands of files must not pay for a walk of all of them on every write.
            if config.max_files or config.max_page_bytes:
                files, used = await asyncio.to_thread(self._usage, self.folder(slug) / "draft")
                previous = target.stat().st_size if target.is_file() else 0
                if not target.is_file() and config.max_files and files + 1 > config.max_files:
                    raise PageError(f"the page already holds {files} files, the most [pages] max_files allows", 413)
                room = config.max_page_bytes - (used - previous) if config.max_page_bytes else None
            try:
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            except OSError as exc:
                raise PageError(f"{path!r} runs through a file of this page: {exc.strerror}", 409) from None
            if target.is_dir():
                raise PageError(f"{path!r} is a folder in this page", 409)
            temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
            written = 0
            handle = await asyncio.to_thread(temporary.open, "wb")
            try:
                async for chunk in chunks:
                    written += len(chunk)
                    if room is not None and written > room:
                        raise PageError(f"the page would hold more than [pages] max_page_bytes ({self.config.max_page_bytes} bytes)", 413)
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.close)
                await asyncio.to_thread(os.replace, temporary, target)
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
            manifest["draft"]["updated_at"] = _now()
            manifest["updated_at"] = _now()
            await asyncio.to_thread(self._write, slug, manifest)
        await self._changed(slug, "draft", manifest.get("version"))
        return {"path": clean_path(path), "bytes": written}

    async def delete_file(self, slug: str, path: str) -> bool:
        async with self._lock(slug):
            manifest = await self.require(slug)
            target = self.draft_file(slug, path)

            def remove() -> bool:
                if target.is_dir():
                    shutil.rmtree(target)
                    return True
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    return True
                return False

            removed = await asyncio.to_thread(remove)
            if removed:
                manifest["draft"]["updated_at"] = _now()
                manifest["updated_at"] = _now()
                await asyncio.to_thread(self._write, slug, manifest)
        if removed:
            await self._changed(slug, "draft", manifest.get("version"))
        return removed

    async def replace_draft(self, slug: str, source: Path) -> dict[str, Any]:
        """Make the draft a copy of a folder (an agent's workspace), within the caps; links are not followed."""
        async with self._lock(slug):
            manifest = await self.require(slug)

            def copy() -> tuple[int, int]:
                files, size = self._usage(source)
                if self.config.max_files and files > self.config.max_files:
                    raise PageError(f"{files} files is more than [pages] max_files allows", 413)
                if self.config.max_page_bytes and size > self.config.max_page_bytes:
                    raise PageError(f"{size} bytes is more than [pages] max_page_bytes allows", 413)
                folder = self.folder(slug)
                fresh = folder / f".draft.{secrets.token_hex(4)}"
                shutil.copytree(source, fresh, symlinks=False, ignore=_skip_links)
                old = folder / f".draft-old.{secrets.token_hex(4)}"
                if (folder / "draft").exists():
                    (folder / "draft").rename(old)
                fresh.rename(folder / "draft")
                shutil.rmtree(old, ignore_errors=True)
                return files, size

            files, size = await asyncio.to_thread(copy)
            manifest["draft"]["updated_at"] = _now()
            manifest["updated_at"] = _now()
            await asyncio.to_thread(self._write, slug, manifest)
        await self._changed(slug, "draft", manifest.get("version"))
        return {"files": files, "bytes": size}

    # -- publishing -----------------------------------------------------------------------------

    async def publish(self, slug: str, note: str = "") -> dict[str, Any]:
        """The draft becomes a new version and the live one. Returns the manifest after."""
        async with self._lock(slug):
            manifest = await self.require(slug)
            draft = manifest["draft"]

            def snapshot() -> dict[str, Any]:
                folder = self.folder(slug)
                versions = folder / "versions"
                versions.mkdir(exist_ok=True)
                taken = [int(p.name) for p in versions.iterdir() if p.name.isdigit()] + [v["n"] for v in manifest["versions"]]
                number = max(taken, default=0) + 1
                temporary = versions / f".{number}.{secrets.token_hex(4)}"
                shutil.copytree(folder / "draft", temporary, symlinks=False, ignore=_skip_links)
                files, size = self._usage(temporary)
                temporary.rename(versions / str(number))
                return {"n": number, "at": _now(), "note": note.strip()[:500], "kind": draft["kind"], "entry": draft["entry"],
                        "target": draft.get("target"), "files": files, "bytes": size}

            if draft["kind"] == "static" and not await asyncio.to_thread((self.folder(slug) / "draft" / draft["entry"]).is_file):
                raise PageError(f"the draft has no {draft['entry']!r}, the page's entry", 409)
            version = await asyncio.to_thread(snapshot)
            manifest["versions"].append(version)
            manifest["version"] = version["n"]
            manifest["updated_at"] = _now()
            dropped = self._prune(manifest)
            await asyncio.to_thread(self._write, slug, manifest)
            await asyncio.to_thread(self._delete_versions, slug, dropped)
        await self._changed(slug, "published", manifest["version"])
        return manifest

    def _prune(self, manifest: dict[str, Any]) -> list[int]:
        keep = self.config.keep_versions
        versions = manifest["versions"]
        if len(versions) <= keep:
            return []
        live = [v for v in versions if v["n"] == manifest["version"]]
        others = [v for v in versions if v["n"] != manifest["version"]]
        room = keep - len(live)
        kept_numbers = {v["n"] for v in live + (others[-room:] if room > 0 else [])}
        manifest["versions"] = [v for v in versions if v["n"] in kept_numbers]
        return [v["n"] for v in versions if v["n"] not in kept_numbers]

    def _delete_versions(self, slug: str, numbers: list[int]) -> None:
        for number in numbers:
            shutil.rmtree(self.folder(slug) / "versions" / str(number), ignore_errors=True)

    async def rollback(self, slug: str, version: int) -> dict[str, Any]:
        """Make an earlier (or later) published version the live one; nothing is deleted."""
        async with self._lock(slug):
            manifest = await self.require(slug)
            if not any(v["n"] == version for v in manifest["versions"]):
                raise PageError(f"the page {slug!r} has no version {version}", 404)
            manifest["version"] = version
            manifest["updated_at"] = _now()
            await asyncio.to_thread(self._write, slug, manifest)
        await self._changed(slug, "rolled_back", version)
        return manifest

    async def remove(self, slug: str) -> bool:
        """The page and every version of it are gone; nothing is kept."""
        async with self._lock(slug):
            folder = self.folder(slug)

            def wipe() -> bool:
                if not folder.exists():
                    return False
                # Renamed out of the way first, so a request in flight sees no page rather than half of one.
                doomed = self.root / f".removed-{slug}-{secrets.token_hex(4)}"
                folder.rename(doomed)
                shutil.rmtree(doomed, ignore_errors=True)
                return True

            removed = await asyncio.to_thread(wipe)
            if self.db is not None:
                await self.db.execute("DELETE FROM page_storage WHERE slug = ?", (slug,))
        self._locks.pop(slug, None)
        if removed:
            await self._changed(slug, "removed", None)
        return removed

    # -- the page's own storage -----------------------------------------------------------------

    def _database(self) -> Database:
        if self.db is None:
            raise PageError("page storage needs the database", 503)
        return self.db

    @staticmethod
    def _key(key: str) -> str:
        if not isinstance(key, str) or not key or len(key) > 512 or "\x00" in key:
            raise PageError("a storage key is 1 to 512 characters")
        return key

    async def storage_get(self, slug: str, key: str) -> dict[str, Any] | None:
        await self.require(slug)
        row = await self._database().fetchone("SELECT value, updated_at FROM page_storage WHERE slug = ? AND key = ?", (slug, self._key(key)))
        if row is None:
            return None
        return {"key": key, "value": json.loads(row["value"]), "updated_at": row["updated_at"]}

    async def storage_set(self, slug: str, key: str, value: Any) -> dict[str, Any]:
        await self.require(slug)
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        limit = self.config.storage_value_max_bytes
        if limit and len(text.encode("utf-8")) > limit:
            raise PageError(f"the value is larger than [pages] storage_value_max_bytes ({limit} bytes)", 413)
        at = _now()
        await self._database().execute(
            "INSERT INTO page_storage(slug, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slug, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (slug, self._key(key), text, at),
        )
        return {"key": key, "updated_at": at}

    async def storage_delete(self, slug: str, key: str) -> bool:
        await self.require(slug)
        db = self._database()
        found = await db.fetchone("SELECT 1 FROM page_storage WHERE slug = ? AND key = ?", (slug, self._key(key)))
        if found is None:
            return False
        await db.execute("DELETE FROM page_storage WHERE slug = ? AND key = ?", (slug, key))
        return True

    async def storage_list(self, slug: str, prefix: str = "") -> list[dict[str, Any]]:
        await self.require(slug)
        rows = await self._database().fetchall(
            "SELECT key, length(CAST(value AS BLOB)) AS bytes, updated_at FROM page_storage WHERE slug = ? AND substr(key, 1, ?) = ? ORDER BY key",
            (slug, len(prefix), prefix),
        )
        return [{"key": row["key"], "bytes": row["bytes"], "updated_at": row["updated_at"]} for row in rows]

    # -- the open page ----------------------------------------------------------------------------

    async def emit(self, slug: str, event: str, data: Any = None) -> None:
        """Hand ``data`` to every open instance of the page, as ``Daedalus.on(event)`` there.

        Nothing is kept: a page opened afterwards never sees it, which is why lasting state belongs in
        the page's storage and an event only says that it changed."""
        await self.require(slug)
        if not isinstance(event, str) or not EVENT_RE.fullmatch(event):
            raise PageError("an event's name is 1 to 64 letters, digits and . _ : - (starting with a letter or digit)")
        if event == RESERVED_EVENTS[0] or event.startswith(RESERVED_EVENTS[1]):
            raise PageError(f"{event!r} is a name the bridge delivers itself; pick another")
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PageError(f"an event's data is JSON: {exc}") from None
        if self.on_emit is None:
            raise PageError("this installation cannot reach open pages", 503)
        await self.on_emit(slug, event, data)

    # -- serving --------------------------------------------------------------------------------

    def served(self, manifest: dict[str, Any], *, draft: bool = False) -> Served | None:
        """The side of a page a request reads: the live version, or the draft when asked for. None for a
        page with nothing published yet (unless the draft is asked for)."""
        slug = manifest["slug"]
        if draft:
            d = manifest["draft"]
            return Served(d["kind"], d["entry"], d.get("target"), self.folder(slug) / "draft", None)
        live = next((v for v in manifest["versions"] if v["n"] == manifest.get("version")), None)
        if live is None:
            return None
        return Served(live["kind"], live["entry"], live.get("target"), self.folder(slug) / "versions" / str(live["n"]), live["n"])


def _skip_links(directory: str, names: list[str]) -> list[str]:
    """Links are left out of a copy: a page is its own files, never a pointer at someone else's."""
    return [name for name in names if os.path.islink(os.path.join(directory, name))]


def listing_row(manifest: dict[str, Any]) -> dict[str, Any]:
    """One page as ``GET /api/pages`` names it: the shape the navigation reads."""
    title = manifest["title"]
    return {
        "slug": manifest["slug"],
        "title": title_text(title),
        "titles": title if isinstance(title, dict) else {},
        "icon": manifest.get("icon"),
        "icon_path": manifest.get("icon_path"),
        "path": f"/app/p/{manifest['slug']}",
        "version": manifest.get("version"),
        "updated_at": manifest.get("updated_at"),
    }


def store_for(app: Any) -> PageStore:
    """The application's one page store, made on first use.

    The API and the agent's tools share it, so a page an agent publishes reaches every open app as a
    ``pages.changed`` event, and the per-page locks are the same whoever writes.
    """
    found = app.extensions.get("pages")
    if isinstance(found, PageStore):
        return found

    async def announce(slug: str, change: Change, version: int | None) -> None:
        bus = getattr(getattr(app, "manager", None), "bus", None)
        if bus is not None:
            await bus.publish("pages.changed", {"slug": slug, "change": change, "version": version})

    async def emit(slug: str, event: str, data: Any) -> None:
        bus = getattr(getattr(app, "manager", None), "bus", None)
        if bus is None:
            raise PageError("this installation cannot reach open pages", 503)
        try:
            await bus.publish("pages.emit", {"slug": slug, "event": event, "data": data})
        except ValueError as exc:
            # The bus carries references, not content: a payload past its limit is refused there.
            raise PageError(f"{exc}; keep the data in the page's storage or a file of the page and send its key", 413) from None

    db = getattr(app, "db", None)
    made = PageStore(app.settings.state_dir / "pages", lambda: app.config.pages, db=db, on_change=announce, on_emit=emit)
    app.extensions["pages"] = made
    return made


def agent_brief(manifest: dict[str, Any]) -> str:
    """The standing instructions of a session made to be a page's own agent: which page it serves,
    how the operator reaches it, and the tools that change the page and talk to it."""
    slug = manifest["slug"]
    title = title_text(manifest["title"])
    kind = manifest["draft"]["kind"]
    live = f"version {manifest['version']} is live" if manifest.get("version") is not None else "nothing is published yet"
    return (
        f"You are the agent of the custom page «{title}» (slug {slug!r}, kind {kind}; {live}), a screen of the operator's "
        f"own in the Daedalus app at /app/p/{slug}. The operator works in the page and talks to you in a panel beside it. "
        "A message that opens with \"(Sent from the page …)\" was sent by the page itself: a button, the page's own composer, "
        "or a report of what the operator did there.\n"
        "- Change the page with the pages tools (load the daedalus-pages skill first): PageRead / PageWrite / PageImport the "
        "draft, PageCheck it, PagePublish. A publish never reloads the page under the operator; they see a quiet "
        "\"New version\" chip and reload when it suits them.\n"
        "- Push data to the open page with PageEmit(slug, event, data), which the page hears with Daedalus.on(event); keep "
        "whatever must last (a scene, settings, results) in the page's storage with PageStorage, which the page reads with "
        "Daedalus.storage, and emit only that it changed.\n"
        "- Files the page needs (models, data) can live in your workspace and go into the page with PageImport."
    )


__all__ = ["KINDS", "PageError", "PageStore", "Served", "agent_brief", "check_slug", "clean_path", "clean_target", "clean_title", "listing_row", "store_for", "title_text"]
