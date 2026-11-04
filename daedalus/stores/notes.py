"""The operator's notes on a chat or a project, and the files those notes show.

A note is Markdown with a title. It belongs to one chat or one project (``Scope``), and so does every
file: a picture pasted into one note can be shown by any other note of the same scope, and a chat's
note can show a file of the project the chat is in. A note names a file as plain relative Markdown,
``![name](files/<id>/<name>)`` or ``[name](files/<id>/<name>)``. The id is the identity and the name
only decoration, so renaming a note or a file breaks no reference, and an exported note unpacked next
to a ``files/<id>/<name>`` folder renders the same in any Markdown reader.

Both the app and the agent write here. The app sends back the ``version`` it last saw with every
change to a note's words, and the one that lost is refused with the current note, never merged: the
editor then asks the operator. Pinning, colour and order are not words and never bump the version, so
the editor's next autosave is not refused because someone pinned the note in another window.

Every change is announced as ``notes.changed`` with the scope it touched, so each open panel reads its
listing again; the event carries ids, never a body.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database
from daedalus.stores.media import _probe  # the one picture-header reader the host has

logger = logging.getLogger(__name__)

TENANT = "notes"
"""The blob store's tenant for note files; nothing else writes there, which is what lets the prune
treat every blob in it without a row as an orphan."""

ScopeKind = Literal["chat", "project"]
Author = Literal["operator", "agent"]
SCOPES: tuple[ScopeKind, ...] = ("chat", "project")
AUTHORS: tuple[Author, ...] = ("operator", "agent")
COLORS = ("", "red", "amber", "green", "teal", "blue", "violet")
"""Names, not colours: the app draws each from the current theme's own tokens."""

TITLE_MAX = 200
BODY_MAX = 200_000
NOTES_PER_SCOPE = 500
FILES_PER_SCOPE = 1000
FILE_MAX_BYTES = 50 * 1024 * 1024
NAME_MAX = 180
TRASH_HOURS = 24
"""How long a deleted note can still be brought back. The Undo toast is seconds long; the rest is for
the window that was closed in between and the agent's own mistakes."""

ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
ID_LENGTH = 12
REFERENCE_RE = re.compile(r"\]\(\s*<?files/([a-z0-9]{12})(?=[/)>\s])")
"""A file a note names: the target of a Markdown link or picture that starts with ``files/<id>``."""

Publish = Callable[..., Awaitable[Any]]


class NoteError(ValueError):
    """A request the store will not carry out; ``status`` is what the API answers with."""

    status = 400


class NotFound(NoteError):
    status = 404


class Conflict(NoteError):
    """The note's words changed since the writer read them. ``current`` is the note as it is now."""

    status = 409

    def __init__(self, message: str, current: dict[str, Any]) -> None:
        super().__init__(message)
        self.current = current


class Referenced(NoteError):
    """A file some note still shows; ``notes`` are those notes, so the operator can be told which."""

    status = 409

    def __init__(self, message: str, notes: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.notes = notes


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    id: str

    @property
    def column(self) -> str:
        return "session_id" if self.kind == "chat" else "project_id"

    def ids(self) -> dict[str, str | None]:
        """The two owner columns, one of them empty, as the event bus and the rows want them."""
        return {"session_id": self.id if self.kind == "chat" else None, "project_id": self.id if self.kind == "project" else None}


def new_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def references(body: str) -> list[str]:
    """The ids of the files a body names, in order of first mention."""
    seen: dict[str, None] = {}
    for match in REFERENCE_RE.finditer(body):
        seen.setdefault(match.group(1), None)
    return list(seen)


def reference(file: dict[str, Any]) -> str:
    """The Markdown that shows a file: a picture for an image, a link for anything else. The name is
    escaped for the link text and percent-encoded for the path, so brackets, spaces and parentheses in
    a file name cannot end the link early."""
    name = str(file["name"])
    text = re.sub(r"([\[\]\\])", r"\\\1", name)
    path = "".join(ch if ch.isalnum() or ch in "-_.~" else "".join(f"%{b:02X}" for b in ch.encode()) for ch in name)
    target = f"files/{file['id']}/{path}"
    return f"![{text}]({target})" if str(file.get("mime", "")).startswith("image/") else f"[{text}]({target})"


def _scope_of(row: Any) -> Scope:
    return Scope("chat", str(row["session_id"])) if row["session_id"] else Scope("project", str(row["project_id"]))


def note_view(row: Any) -> dict[str, Any]:
    scope = _scope_of(row)
    return {
        "id": str(row["id"]),
        "scope": scope.kind,
        "scope_id": scope.id,
        "title": str(row["title"]),
        "body": str(row["body"]),
        "color": str(row["color"]),
        "pinned": bool(row["pinned"]),
        "position": float(row["position"]),
        "version": int(row["version"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "updated_by": str(row["updated_by"]),
        "deleted_at": row["deleted_at"],
    }


def file_view(row: Any) -> dict[str, Any]:
    scope = _scope_of(row)
    return {
        "id": str(row["id"]),
        "scope": scope.kind,
        "scope_id": scope.id,
        "name": str(row["name"]),
        "mime": str(row["mime"]),
        "size": int(row["size"]),
        "width": row["width"],
        "height": row["height"],
        "created_at": str(row["created_at"]),
        "created_by": str(row["created_by"]),
    }


def _title(value: str) -> str:
    title = " ".join(str(value).split())
    if len(title) > TITLE_MAX:
        raise NoteError(f"a note's title is at most {TITLE_MAX} characters")
    return title


def _body(value: str) -> str:
    body = str(value).replace("\r\n", "\n")
    if len(body) > BODY_MAX:
        raise NoteError(f"a note is at most {BODY_MAX} characters")
    return body


def _color(value: str) -> str:
    if value not in COLORS:
        raise NoteError(f"a note's colour is one of {', '.join(c for c in COLORS if c)}, or none")
    return value


def _author(value: str) -> Author:
    if value not in AUTHORS:
        raise NoteError("a note is written by the operator or by the agent")
    return value  # type: ignore[return-value]


def _file_name(value: str) -> str:
    # The last path segment only, without control characters: the name is shown and downloaded, and
    # a name with a slash would read as a folder in an exported archive.
    name = re.split(r"[\\/]", str(value))[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip() or "file"
    if len(name) > NAME_MAX:
        stem, dot, suffix = name.rpartition(".")
        name = (stem[: NAME_MAX - len(suffix) - 1] + dot + suffix) if dot and len(suffix) < 12 else name[:NAME_MAX]
    return name


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    """A picture's pixels from its header, for the layout to reserve room before it loads."""
    try:
        kind, _, width, height = _probe(data[:65536])
    except ValueError:
        return None, None
    return (width, height) if kind in ("image", "animation") else (None, None)


NOTE_COLUMNS = "id, session_id, project_id, title, body, color, pinned, position, version, created_at, updated_at, updated_by, deleted_at"
FILE_COLUMNS = "id, session_id, project_id, name, mime, size, sha256, width, height, created_at, created_by"


class NoteStore:
    """Notes and note files over the shared database and blob store. Cheap to build; holds no state
    but its collaborators, so the API and the agent's tool each make their own."""

    def __init__(self, db: Database, blobs: FileBlobStore, *, publish: Publish | None = None) -> None:
        self.db = db
        self.blobs = blobs
        self.publish = publish

    async def _announce(self, scope: Scope, change: str, *, by: Author, note_id: str | None = None, file_id: str | None = None, version: int | None = None) -> None:
        if self.publish is None:
            return
        payload: dict[str, Any] = {"scope": scope.kind, "scope_id": scope.id, "change": change, "by": by}
        if note_id is not None:
            payload["note_id"] = note_id
        if file_id is not None:
            payload["file_id"] = file_id
        if version is not None:
            payload["version"] = version
        try:
            await self.publish("notes.changed", payload, **scope.ids())
        except Exception:  # noqa: BLE001 — the change is made; a panel that missed it reads again on focus
            logger.warning("could not announce a notes change", exc_info=True)

    # -- reading -------------------------------------------------------------------------------------

    async def notes(self, scope: Scope) -> list[dict[str, Any]]:
        """A scope's live notes, pinned first, then in the operator's order."""
        rows = await self.db.fetchall(
            f"SELECT {NOTE_COLUMNS} FROM notes WHERE {scope.column} = ? AND deleted_at IS NULL ORDER BY pinned DESC, position, created_at DESC",
            (scope.id,),
        )
        return [note_view(row) for row in rows]

    async def files(self, scope: Scope) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(f"SELECT {FILE_COLUMNS} FROM note_files WHERE {scope.column} = ? ORDER BY created_at DESC, id", (scope.id,))
        return [file_view(row) for row in rows]

    async def get(self, note_id: str, *, deleted: bool = False) -> dict[str, Any]:
        row = await self.db.fetchone(f"SELECT {NOTE_COLUMNS} FROM notes WHERE id = ?", (note_id,))
        if row is None or (row["deleted_at"] is not None and not deleted):
            raise NotFound("no such note")
        return note_view(row)

    async def file(self, file_id: str) -> dict[str, Any]:
        row = await self.db.fetchone(f"SELECT {FILE_COLUMNS} FROM note_files WHERE id = ?", (file_id,))
        if row is None:
            raise NotFound("no such file")
        return {**file_view(row), "sha256": str(row["sha256"])}

    def file_path(self, file: dict[str, Any]) -> Any:
        return self.blobs.path_of(TENANT, str(file["sha256"]))

    async def search(self, scopes: list[Scope], query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Notes of these scopes whose title or words hold every term of ``query``, case-insensitively."""
        terms = [term for term in query.lower().split() if term][:8]
        if not scopes:
            return []
        owners = " OR ".join(f"{scope.column} = ?" for scope in scopes)
        clauses = " AND ".join("(instr(lower(title), ?) > 0 OR instr(lower(body), ?) > 0)" for _ in terms) or "1"
        params: list[Any] = [scope.id for scope in scopes]
        for term in terms:
            params += [term, term]
        rows = await self.db.fetchall(
            f"SELECT {NOTE_COLUMNS} FROM notes WHERE ({owners}) AND deleted_at IS NULL AND {clauses} ORDER BY updated_at DESC LIMIT ?",
            (*params, limit),
        )
        return [note_view(row) for row in rows]

    async def referencing(self, file_id: str) -> list[dict[str, Any]]:
        """The live notes that show a file, anywhere: a project's file can be shown by any of its chats."""
        rows = await self.db.fetchall(
            f"SELECT {NOTE_COLUMNS} FROM notes WHERE deleted_at IS NULL AND instr(body, ?) > 0 ORDER BY updated_at DESC",
            (f"files/{file_id}",),
        )
        return [{"id": view["id"], "title": view["title"], "scope": view["scope"]} for view in map(note_view, rows) if file_id in references(view["body"])]

    # -- writing notes -------------------------------------------------------------------------------

    async def create(self, scope: Scope, *, title: str = "", body: str = "", color: str = "", pinned: bool = False, by: str = "operator") -> dict[str, Any]:
        title, body, color, author = _title(title), _body(body), _color(color), _author(by)
        stamp = now()
        note_id = new_id()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(f"SELECT count(*), min(position) FROM notes WHERE {scope.column} = ? AND deleted_at IS NULL", (scope.id,))
            count, lowest = await cursor.fetchone()
            await cursor.close()
            if count >= NOTES_PER_SCOPE:
                raise NoteError(f"a {scope.kind} keeps at most {NOTES_PER_SCOPE} notes")
            # A new note goes on top of the operator's order, where they are looking.
            position = (float(lowest) - 1.0) if lowest is not None else 0.0
            await conn.execute(
                "INSERT INTO notes(id, session_id, project_id, title, body, color, pinned, position, version, created_at, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (note_id, scope.ids()["session_id"], scope.ids()["project_id"], title, body, color, int(bool(pinned)), position, stamp, stamp, author),
            )
        await self._purge_trash(scope)
        await self._announce(scope, "created", by=author, note_id=note_id, version=1)
        return await self.get(note_id)

    async def update(self, note_id: str, *, version: int | None, title: str | None = None, body: str | None = None, by: str = "operator", force: bool = False) -> dict[str, Any]:
        """Change a note's words. ``version`` is the one the writer last saw; a newer one on the note
        refuses the change with :class:`Conflict` unless ``force`` (the operator chose to keep theirs)."""
        author = _author(by)
        current = await self.get(note_id)
        new_title = current["title"] if title is None else _title(title)
        new_body = current["body"] if body is None else _body(body)
        if not force and version != current["version"]:
            raise Conflict("the note changed elsewhere since it was read", current)
        if new_title == current["title"] and new_body == current["body"]:
            return current
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ?, updated_by = ? WHERE id = ? AND version = ? AND deleted_at IS NULL",
                (new_title, new_body, now(), author, note_id, current["version"]),
            )
            changed = cursor.rowcount
            await cursor.close()
        if not changed:
            # Another writer got in between the read and the write: the same refusal, with what it wrote.
            raise Conflict("the note changed elsewhere since it was read", await self.get(note_id))
        saved = await self.get(note_id)
        await self._announce(_scope_of_view(saved), "updated", by=author, note_id=note_id, version=saved["version"])
        return saved

    async def set_meta(self, note_id: str, *, pinned: bool | None = None, color: str | None = None, by: str = "operator") -> dict[str, Any]:
        """Pin or colour a note. Neither is the note's words: no version, no new edit time."""
        author = _author(by)
        current = await self.get(note_id)
        sets: list[str] = []
        params: list[Any] = []
        if pinned is not None:
            sets.append("pinned = ?")
            params.append(int(bool(pinned)))
        if color is not None:
            sets.append("color = ?")
            params.append(_color(color))
        if not sets:
            return current
        await self.db.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", (*params, note_id))
        saved = await self.get(note_id)
        await self._announce(_scope_of_view(saved), "meta", by=author, note_id=note_id, version=saved["version"])
        return saved

    async def reorder(self, scope: Scope, ids: list[str], *, by: str = "operator") -> list[dict[str, Any]]:
        """Put the scope's notes in this order. Notes the list leaves out keep their place after it, so
        a window that had not yet seen a new note cannot push it out of the order."""
        author = _author(by)
        present = [row["id"] for row in await self.db.fetchall(f"SELECT id FROM notes WHERE {scope.column} = ? AND deleted_at IS NULL ORDER BY position", (scope.id,))]
        known = set(present)
        wanted = list(dict.fromkeys(i for i in ids if i in known))
        order = wanted + [i for i in present if i not in set(wanted)]
        async with self.db.transaction() as conn:
            await conn.executemany("UPDATE notes SET position = ? WHERE id = ?", [(float(index), note_id) for index, note_id in enumerate(order)])
        await self._announce(scope, "order", by=author)
        return await self.notes(scope)

    async def delete(self, note_id: str, *, by: str = "operator") -> dict[str, Any]:
        author = _author(by)
        current = await self.get(note_id)
        await self.db.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (now(), note_id))
        await self._announce(_scope_of_view(current), "deleted", by=author, note_id=note_id)
        return current

    async def restore(self, note_id: str, *, by: str = "operator") -> dict[str, Any]:
        current = await self.get(note_id, deleted=True)
        if current["deleted_at"] is None:
            return current
        await self.db.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))
        saved = await self.get(note_id)
        await self._announce(_scope_of_view(saved), "restored", by=_author(by), note_id=note_id, version=saved["version"])
        return saved

    async def duplicate(self, note_id: str, *, title: str, by: str = "operator") -> dict[str, Any]:
        source = await self.get(note_id)
        return await self.create(_scope_of_view(source), title=title, body=source["body"], color=source["color"], by=by)

    async def move(self, note_id: str, target: Scope, *, by: str = "operator") -> dict[str, Any]:
        """Hand a note to another scope. A note going from a chat up to its project takes the chat's
        files it shows along, since the project's other chats could not see them otherwise; the chat
        still sees them, as a chat sees its project's files. Going down, the files stay where they are."""
        author = _author(by)
        current = await self.get(note_id)
        source = _scope_of_view(current)
        if source == target:
            return current
        async with self.db.transaction() as conn:
            cursor = await conn.execute(f"SELECT min(position) FROM notes WHERE {target.column} = ? AND deleted_at IS NULL", (target.id,))
            (lowest,) = await cursor.fetchone()
            await cursor.close()
            ids = target.ids()
            await conn.execute(
                "UPDATE notes SET session_id = ?, project_id = ?, position = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (ids["session_id"], ids["project_id"], (float(lowest) - 1.0) if lowest is not None else 0.0, now(), author, note_id),
            )
            if source.kind == "chat" and target.kind == "project":
                for file_id in references(current["body"]):
                    await conn.execute("UPDATE note_files SET session_id = NULL, project_id = ? WHERE id = ? AND session_id = ?", (target.id, file_id, source.id))
        await self._announce(source, "moved", by=author, note_id=note_id)
        await self._announce(target, "moved", by=author, note_id=note_id)
        return await self.get(note_id)

    async def _purge_trash(self, scope: Scope) -> None:
        before = (datetime.now(UTC) - timedelta(hours=TRASH_HOURS)).isoformat(timespec="milliseconds")
        await self.db.execute(f"DELETE FROM notes WHERE {scope.column} = ? AND deleted_at IS NOT NULL AND deleted_at < ?", (scope.id, before))

    # -- files ---------------------------------------------------------------------------------------

    async def add_file(self, scope: Scope, data: bytes, *, name: str, mime: str = "", by: str = "operator") -> dict[str, Any]:
        author = _author(by)
        if not data:
            raise NoteError("the file is empty")
        if len(data) > FILE_MAX_BYTES:
            raise NoteError(f"a note file is at most {FILE_MAX_BYTES // (1024 * 1024)} MB")
        name = _file_name(name)
        kind = (mime or "").partition(";")[0].strip().lower() or mimetypes.guess_type(name)[0] or "application/octet-stream"
        width, height = _image_size(data) if kind.startswith("image/") else (None, None)
        row = await self.db.fetchone(f"SELECT count(*) AS n FROM note_files WHERE {scope.column} = ?", (scope.id,))
        if row is not None and int(row["n"]) >= FILES_PER_SCOPE:
            raise NoteError(f"a {scope.kind} keeps at most {FILES_PER_SCOPE} files")
        meta = await self.blobs.put(TENANT, data, content_type=kind)
        file_id = new_id()
        ids = scope.ids()
        await self.db.execute(
            f"INSERT INTO note_files({FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, ids["session_id"], ids["project_id"], name, kind, len(data), meta.sha256, width, height, now(), author),
        )
        await self._announce(scope, "files", by=author, file_id=file_id)
        found = await self.file(file_id)
        found.pop("sha256", None)
        return found

    async def delete_file(self, file_id: str, *, force: bool = False, by: str = "operator") -> dict[str, Any]:
        """Remove a file. One a note still shows is refused with :class:`Referenced` unless ``force``:
        the operator is told which notes would lose a picture before it goes."""
        author = _author(by)
        found = await self.file(file_id)
        if not force:
            users = await self.referencing(file_id)
            if users:
                raise Referenced("notes still show this file", users)
        await self.db.execute("DELETE FROM note_files WHERE id = ?", (file_id,))
        await self._free(str(found["sha256"]))
        scope = Scope(found["scope"], found["scope_id"])
        await self._announce(scope, "files", by=author, file_id=file_id)
        found.pop("sha256", None)
        return found

    async def _free(self, digest: str) -> None:
        if await self.db.fetchone("SELECT 1 FROM note_files WHERE sha256 = ? LIMIT 1", (digest,)) is None:
            await self.blobs.delete(TENANT, digest)

    async def prune_blobs(self) -> int:
        """Free the bytes no file row names any more: what a deleted chat or project left behind, since
        its rows go by cascade inside a transaction this store never sees."""
        folder = self.blobs.path_of(TENANT, "x").parent
        if not folder.is_dir():
            return 0
        rows = await self.db.fetchall("SELECT DISTINCT sha256 FROM note_files")
        kept = {str(row["sha256"]) for row in rows}
        freed = 0
        for path in folder.iterdir():
            digest = path.name
            if digest.startswith(".") or digest.endswith(".meta.json") or digest in kept:
                continue
            if await self.blobs.delete(TENANT, digest):
                freed += 1
        return freed


HOOK = "notes"
"""Where the one store lives on the session manager (``service_hooks``)."""


def store_for(manager: Any) -> NoteStore:
    """The one store over the manager's database, blobs and bus, made on first use, so a note the
    agent writes reaches every open panel the same way the operator's own edits do."""
    found = manager.service_hooks.get(HOOK)
    if isinstance(found, NoteStore):
        return found
    bus = getattr(manager, "bus", None)
    store = NoteStore(manager.db, manager.blobs, publish=bus.publish if bus is not None else None)
    manager.service_hooks[HOOK] = store
    return store


def _scope_of_view(view: dict[str, Any]) -> Scope:
    return Scope(view["scope"], view["scope_id"])


async def scopes_for_session(db: Database, session_id: str) -> tuple[Scope, Scope | None, dict[str, Any] | None]:
    """The chat's own scope, its project's scope when the project is a real one, and that project's
    name and id. A plain chat's project is a scratch one made for it alone (``ephemeral``): notes
    there would be the chat's notes under another name, so such a chat has no project scope."""
    row = await db.fetchone(
        "SELECT s.project_id, p.name, p.system, json_extract(p.settings, '$.system') AS marked, json_extract(p.settings, '$.ephemeral') AS ephemeral"
        " FROM sessions s JOIN projects p ON p.id = s.project_id WHERE s.id = ?",
        (session_id,),
    )
    if row is None:
        raise NotFound("no such session")
    chat = Scope("chat", session_id)
    # The same test as the app's ``isProject``: a system project is a project even when ephemeral.
    if not (row["system"] or row["marked"]) and row["ephemeral"]:
        return chat, None, None
    return chat, Scope("project", str(row["project_id"])), {"id": str(row["project_id"]), "name": str(row["name"])}


__all__ = [
    "COLORS",
    "Conflict",
    "NoteError",
    "NoteStore",
    "NotFound",
    "Referenced",
    "Scope",
    "TENANT",
    "reference",
    "references",
    "scopes_for_session",
    "store_for",
]
