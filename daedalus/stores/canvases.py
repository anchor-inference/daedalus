"""Canvases: JSON Canvas boards that belong to a chat or a project, with their history and files.

A canvas lives in the database, not as a file in a workspace, because it has to outlive and reach
beyond any one folder: the history, a public link, a project's board that every chat of the project
opens. Obsidian interchange is an export and an import of the ``.canvas`` text (``canvas_doc.write``).

It belongs to one chat or one project (``Scope``, as notes do), and it has an owner chat: the one its
comment bar and its pointing send into. For a chat's canvas that is the chat; for a project's it is
the chat that made it, and when that chat is gone the board asks which chat to write to.

Both the board and the agent change a canvas by operations on items (``canvas_doc.apply_ops``), each
carrying the version its writer last saw. A stale version is still accepted when none of the items it
touches changed since, and refused, naming those items, when one did. The history is coarser than the
versions: the operator's saves within ``COALESCE_SECONDS`` fold into one revision, while an agent's
edit, a restore, an import and a creation always keep a row of their own.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

import aiosqlite

from daedalus.stores import canvas_doc
from daedalus.stores.calendar import now
from daedalus.stores.canvas_doc import TITLE_KEY, CanvasError, Conflict
from daedalus.stores.canvas_preview import preview_svg
from daedalus.stores.database import Database
from daedalus.stores.notes import NotFound as NoSuchChat
from daedalus.stores.notes import Scope, scopes_for_session

logger = logging.getLogger(__name__)

SOURCES = ("operator", "agent")
KINDS = ("create", "edit", "restore", "import")
COALESCE_SECONDS = 600
"""How long a run of the operator's saves stays one revision, counted from its first save, so an hour
of arranging cards still leaves a revision every ten minutes to go back to."""
REVISION_CAP = 100
TITLE_MAX = 160
CANVASES_PER_SCOPE = 500
LIST_LIMIT = 500

Publish = Callable[..., Awaitable[Any]]


class NotFound(KeyError):
    pass


class Stale(Exception):
    """The writer's version is behind and what it changes was changed meanwhile. ``ids`` are those
    items (``#title`` for the title), ``current`` the canvas as it is now."""

    def __init__(self, ids: list[str], current: dict[str, Any]) -> None:
        super().__init__(f"changed since your version: {', '.join(ids)}")
        self.ids = ids
        self.current = current


def _title(title: str) -> str:
    title = " ".join(str(title or "").split())
    if not 1 <= len(title) <= TITLE_MAX:
        raise CanvasError(f"a canvas title needs 1 to {TITLE_MAX} characters")
    return title


def _source(source: str) -> str:
    if source not in SOURCES:
        raise CanvasError("a canvas is saved by the operator or by an agent")
    return source


def _age(earlier: str, later: str) -> float:
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
    except ValueError:
        return float("inf")


def _scope_of(row: Any) -> Scope:
    return Scope("chat", str(row["session_id"])) if row["session_id"] else Scope("project", str(row["project_id"]))


ITEM_COLUMNS = (
    "c.id,c.title,c.version,c.created_at,c.updated_at,c.updated_by,c.share_token,c.session_id,c.project_id,c.owner_session_id,"
    "c.node_count,c.edge_count,COALESCE(s.title,p.name,'') AS scope_name"
)
ITEM_FROM = " FROM canvases c LEFT JOIN sessions s ON s.id=c.session_id LEFT JOIN projects p ON p.id=c.project_id"


def _item(row: Any) -> dict[str, Any]:
    scope = _scope_of(row)
    return {
        "id": row["id"],
        "title": row["title"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "shared": bool(row["share_token"]),
        "scope": scope.kind,
        "scope_id": scope.id,
        "scope_name": row["scope_name"] or "",
        "owner_session_id": row["owner_session_id"],
        "nodes": row["node_count"],
        "edges": row["edge_count"],
    }


async def record_revision(conn: aiosqlite.Connection, canvas_id: str, version: int, title: str, doc: dict[str, Any], at: str, source: str, kind: str, restored_from: int | None = None) -> None:
    """Write the revision a save produced, folding it into the previous one when it continues the
    operator's run of saves. Runs inside the caller's transaction."""
    recent = await (await conn.execute(
        "SELECT version,source,kind,started_at FROM canvas_revisions WHERE canvas_id=? ORDER BY version DESC LIMIT 2", (canvas_id,),
    )).fetchall()
    latest = recent[0] if recent else None
    coalesce = (
        kind == "edit" and source == "operator" and latest is not None and latest["source"] == "operator" and latest["kind"] == "edit"
        and _age(latest["started_at"] or at, at) < COALESCE_SECONDS
    )
    # The summary is against the revision the reader sees listed before this one: after folding,
    # that is the row before the folded one, not the save a second ago.
    base_version = (recent[1]["version"] if len(recent) > 1 else None) if coalesce else (latest["version"] if latest else None)
    base = None
    if base_version is not None:
        base = await (await conn.execute("SELECT title,doc_json FROM canvas_revisions WHERE canvas_id=? AND version=?", (canvas_id, base_version))).fetchone()
    summary = canvas_doc.change_summary(json.loads(base["doc_json"]) if base else None, base["title"] if base else None, doc, title)
    if kind == "restore":
        summary = {**summary, "restored_from": restored_from}
    encoded, preview = json.dumps(doc, ensure_ascii=False), preview_svg(doc)
    if coalesce and latest is not None:
        await conn.execute(
            "UPDATE canvas_revisions SET version=?,title=?,doc_json=?,saved_at=?,summary_json=?,preview_svg=? WHERE canvas_id=? AND version=?",
            (version, title, encoded, at, json.dumps(summary), preview, canvas_id, latest["version"]),
        )
    else:
        await conn.execute(
            "INSERT INTO canvas_revisions(canvas_id,version,title,doc_json,saved_at,started_at,source,kind,restored_from,summary_json,preview_svg)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (canvas_id, version, title, encoded, at, at, source, kind, restored_from, json.dumps(summary), preview),
        )
    await conn.execute(
        "DELETE FROM canvas_revisions WHERE canvas_id=? AND version NOT IN"
        " (SELECT version FROM canvas_revisions WHERE canvas_id=? ORDER BY version DESC LIMIT ?)",
        (canvas_id, canvas_id, REVISION_CAP),
    )


async def insert_canvas(
    conn: aiosqlite.Connection, *, canvas_id: str, title: str, scope: Scope, owner_session_id: str | None, doc: dict[str, Any], at: str, source: str, kind: str,
) -> None:
    """The row of a new canvas and its first revision, inside the caller's transaction. Shared with the
    migration that brought the diagrams over, so a converted canvas is made exactly as a new one is."""
    nodes, edges = canvas_doc.counts(doc)
    clock = canvas_doc.tick({}, [item["id"] for item in (*doc["nodes"], *doc["edges"])] + [TITLE_KEY], 1, doc)
    await conn.execute(
        "INSERT INTO canvases(id,title,session_id,project_id,owner_session_id,doc_json,clock_json,version,created_at,updated_at,updated_by,preview_svg,node_count,edge_count)"
        " VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (canvas_id, title, scope.ids()["session_id"], scope.ids()["project_id"], owner_session_id, json.dumps(doc, ensure_ascii=False), json.dumps(clock), at, at, source, preview_svg(doc), nodes, edges),
    )
    await record_revision(conn, canvas_id, 1, title, doc, at, source, kind)


class CanvasStore:
    """Canvases over the shared database. Cheap to build: the API and the agent's tools each make one,
    and both announce through the same bus, so an open board sees either writer's change at once."""

    def __init__(self, db: Database, *, publish: Publish | None = None) -> None:
        self.db = db
        self.publish = publish

    async def _announce(self, canvas_id: str, scope: Scope, change: str, *, by: str, version: int | None = None) -> None:
        if self.publish is None:
            return
        payload: dict[str, Any] = {"canvas_id": canvas_id, "scope": scope.kind, "scope_id": scope.id, "change": change, "by": by}
        if version is not None:
            payload["version"] = version
        try:
            await self.publish("canvas.changed", payload, **scope.ids())
        except Exception:  # noqa: BLE001 — the change is saved; an open board that missed it reads on its next poll
            logger.warning("could not announce a canvas change", exc_info=True)

    # -- scopes ----------------------------------------------------------------------------------------

    async def scope_names(self, scopes: Iterable[Scope]) -> list[dict[str, Any]]:
        named = []
        for scope in scopes:
            if scope.kind == "chat":
                row = await self.db.fetchone("SELECT title FROM sessions WHERE id=?", (scope.id,))
            else:
                row = await self.db.fetchone("SELECT name AS title FROM projects WHERE id=?", (scope.id,))
            named.append({"kind": scope.kind, "id": scope.id, "name": (row["title"] if row else "") or ""})
        return named

    async def scopes_of_chat(self, session_id: str) -> tuple[Scope, Scope | None]:
        try:
            chat, project, _ = await scopes_for_session(self.db, session_id)
        except NoSuchChat as exc:
            raise NotFound(session_id) from exc
        return chat, project

    async def check_scope(self, scope: Scope, owner_session_id: str | None) -> str | None:
        """The owner chat the scope allows, or :class:`CanvasError`. A chat's canvas is owned by that
        chat; a project's by one of the project's chats, or by none."""
        if scope.kind not in ("chat", "project"):
            raise CanvasError("a canvas belongs to a chat or a project")
        if scope.kind == "chat":
            if await self.db.fetchone("SELECT 1 FROM sessions WHERE id=?", (scope.id,)) is None:
                raise NotFound(scope.id)
            return scope.id
        if await self.db.fetchone("SELECT 1 FROM projects WHERE id=?", (scope.id,)) is None:
            raise NotFound(scope.id)
        if owner_session_id and await self.db.fetchone("SELECT 1 FROM sessions WHERE id=? AND project_id=?", (owner_session_id, scope.id)) is None:
            raise CanvasError("the owner chat is not a chat of that project")
        return owner_session_id or None

    # -- reading ---------------------------------------------------------------------------------------

    async def list(self, *, session_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """The canvases of a chat and of its project, of one project, or of everything, each with its
        scope, so the page can head them by where they belong rather than pile them up."""
        if session_id:
            chat, project = await self.scopes_of_chat(session_id)
            scopes = [chat, *([project] if project else [])]
            clauses = " OR ".join(f"c.{scope.column}=?" for scope in scopes)
            rows = await self.db.fetchall(f"SELECT {ITEM_COLUMNS}{ITEM_FROM} WHERE {clauses} ORDER BY c.updated_at DESC LIMIT ?", (*[scope.id for scope in scopes], LIST_LIMIT))
            return {"scopes": await self.scope_names(scopes), "items": [_item(row) for row in rows]}
        if project_id:
            if await self.db.fetchone("SELECT 1 FROM projects WHERE id=?", (project_id,)) is None:
                raise NotFound(project_id)
            rows = await self.db.fetchall(f"SELECT {ITEM_COLUMNS}{ITEM_FROM} WHERE c.project_id=? ORDER BY c.updated_at DESC LIMIT ?", (project_id, LIST_LIMIT))
            return {"scopes": await self.scope_names([Scope("project", project_id)]), "items": [_item(row) for row in rows]}
        rows = await self.db.fetchall(f"SELECT {ITEM_COLUMNS}{ITEM_FROM} ORDER BY c.updated_at DESC LIMIT ?", (LIST_LIMIT,))
        items = [_item(row) for row in rows]
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            seen.setdefault((item["scope"], item["scope_id"]), {"kind": item["scope"], "id": item["scope_id"], "name": item["scope_name"]})
        return {"scopes": list(seen.values()), "items": items}

    async def get(self, canvas_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(f"SELECT {ITEM_COLUMNS},c.doc_json{ITEM_FROM} WHERE c.id=?", (canvas_id,))
        if row is None:
            return None
        files = await self.db.fetchall("SELECT id,name,mime,size FROM canvas_files WHERE canvas_id=? ORDER BY created_at", (canvas_id,))
        return {**_item(row), "doc": json.loads(row["doc_json"]), "share_token": row["share_token"], "files": [dict(file) for file in files]}

    async def require(self, canvas_id: str) -> dict[str, Any]:
        found = await self.get(canvas_id)
        if found is None:
            raise NotFound(canvas_id)
        return found

    async def head(self, canvas_id: str) -> dict[str, Any] | None:
        """What changed last, without the document: an open board polls this and fetches the document
        only when the version moved."""
        row = await self.db.fetchone("SELECT id,title,version,updated_at,updated_by FROM canvases WHERE id=?", (canvas_id,))
        return dict(row) if row else None

    async def file(self, canvas_id: str, file_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT id,name,mime,size,data FROM canvas_files WHERE canvas_id=? AND id=?", (canvas_id, file_id))
        return dict(row) if row else None

    # -- writing ---------------------------------------------------------------------------------------

    async def create(self, title: str, scope: Scope, *, owner_session_id: str | None = None, doc: Any = None, source: str = "operator", kind: str = "create") -> dict[str, Any]:
        title, source = _title(title), _source(source)
        checked = canvas_doc.check(doc if doc is not None else canvas_doc.empty())
        owner = await self.check_scope(scope, owner_session_id)
        count = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM canvases WHERE {scope.column}=?", (scope.id,))
        if count is not None and count["n"] >= CANVASES_PER_SCOPE:
            raise CanvasError(f"a {scope.kind} holds at most {CANVASES_PER_SCOPE} canvases")
        canvas_id, at = uuid.uuid4().hex, now()
        async with self.db.transaction() as conn:
            await insert_canvas(conn, canvas_id=canvas_id, title=title, scope=scope, owner_session_id=owner, doc=checked, at=at, source=source, kind=kind)
        await self._announce(canvas_id, scope, "created", by=source, version=1)
        return await self.require(canvas_id)

    async def apply(self, canvas_id: str, base_version: int, ops: list[Any], *, title: str | None = None, source: str = "operator") -> tuple[dict[str, Any], bool]:
        """Apply item operations written against ``base_version``; returns the canvas and whether the
        save went on top of a newer version (``merged``). :class:`Stale` when an item the operations
        touch, or the title they rename, changed after ``base_version``."""
        source = _source(source)
        new_title = _title(title) if title is not None else None
        if not ops and new_title is None:
            raise CanvasError("nothing to change")
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT title,doc_json,clock_json,version,session_id,project_id FROM canvases WHERE id=?", (canvas_id,))).fetchone()
            if row is None:
                raise NotFound(canvas_id)
            current, clock, version = json.loads(row["doc_json"]), json.loads(row["clock_json"] or "{}"), int(row["version"])
            if base_version > version or base_version < 1:
                raise CanvasError(f"version {base_version} is not a version of this canvas (it is at {version})")
            merged = base_version < version
            renamed = new_title is not None and new_title != row["title"]
            try:
                doc, touched = canvas_doc.apply_ops(current, ops)
            except CanvasError:
                if not merged:
                    raise
                # A stale writer's edge onto a node the other writer removed is a conflict about the node.
                gone = sorted(canvas_doc.referenced(ops) - {node["id"] for node in current["nodes"]})
                if gone:
                    stale_ids = gone
                else:
                    raise
            else:
                stale_ids = canvas_doc.conflicts(clock, base_version, touched | ({TITLE_KEY} if renamed else set())) if merged else []
            if stale_ids:
                found = await self._get_in(conn, canvas_id)
                raise Stale(stale_ids, found)
            if not touched and not renamed:
                found = await self._get_in(conn, canvas_id)
                return found, False
            final_title = new_title if renamed and new_title is not None else row["title"]
            at, next_version = now(), version + 1
            clock = canvas_doc.tick(clock, touched | ({TITLE_KEY} if renamed else set()), next_version, doc)
            await self._write(conn, canvas_id, version, final_title, doc, clock, at, source)
            await record_revision(conn, canvas_id, next_version, final_title, doc, at, source, "edit")
            scope = _scope_of(row)
        await self._announce(canvas_id, scope, "updated", by=source, version=next_version)
        return await self.require(canvas_id), merged

    async def replace(self, canvas_id: str, doc: Any, version: int, *, title: str | None = None, source: str = "operator", kind: str = "edit", restored_from: int | None = None) -> dict[str, Any]:
        """Put a whole document in place, at exactly ``version``: a restore or an import over a canvas.
        A whole document cannot be merged item by item with somebody's newer work, so it is refused
        when the canvas moved on."""
        source = _source(source)
        checked = canvas_doc.check(doc)
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT title,doc_json,clock_json,version,session_id,project_id FROM canvases WHERE id=?", (canvas_id,))).fetchone()
            if row is None:
                raise NotFound(canvas_id)
            if int(row["version"]) != version:
                raise Stale([], await self._get_in(conn, canvas_id))
            final_title = _title(title) if title is not None else row["title"]
            touched = canvas_doc.changed_ids(json.loads(row["doc_json"]), checked) | ({TITLE_KEY} if final_title != row["title"] else set())
            at, next_version = now(), version + 1
            clock = canvas_doc.tick(json.loads(row["clock_json"] or "{}"), touched, next_version, checked)
            await self._write(conn, canvas_id, version, final_title, checked, clock, at, source)
            await record_revision(conn, canvas_id, next_version, final_title, checked, at, source, kind, restored_from=restored_from)
            scope = _scope_of(row)
        await self._announce(canvas_id, scope, "updated", by=source, version=next_version)
        return await self.require(canvas_id)

    async def rename(self, canvas_id: str, title: str, version: int, *, source: str = "operator") -> dict[str, Any]:
        """A new title from the list page, which holds the version but not the document. Renaming is
        an operation like any other: a canvas whose cards moved since is still renamed."""
        found, _ = await self.apply(canvas_id, version, [], title=title, source=source)
        return {key: found[key] for key in ("id", "title", "version", "updated_at", "updated_by")}

    async def restore(self, canvas_id: str, revision: int, version: int) -> dict[str, Any]:
        """Make an earlier revision current again, as a new version on top of ``version``. Nothing is
        rewound: the restore is itself a revision, so restoring the wrong one is undone by restoring
        the one before it."""
        row = await self.db.fetchone("SELECT title,doc_json FROM canvas_revisions WHERE canvas_id=? AND version=?", (canvas_id, revision))
        if row is None:
            if await self.head(canvas_id) is None:
                raise NotFound(canvas_id)
            raise CanvasError("that version is no longer in the history")
        return await self.replace(canvas_id, json.loads(row["doc_json"]), version, title=row["title"], kind="restore", restored_from=revision)

    async def duplicate(self, canvas_id: str, title: str | None = None) -> dict[str, Any]:
        found = await self.require(canvas_id)
        name = (title or "").strip() or f"{found['title']} (copy)"[:TITLE_MAX]
        made = await self.create(name, Scope(found["scope"], found["scope_id"]), owner_session_id=found["owner_session_id"], doc=found["doc"])
        if found["files"]:
            # Attachments go with the copy under the same ids, so the copy's file nodes keep working.
            async with self.db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO canvas_files(id,canvas_id,name,mime,size,data,created_at) SELECT id,?,name,mime,size,data,created_at FROM canvas_files WHERE canvas_id=?",
                    (made["id"], canvas_id),
                )
            made = await self.require(made["id"])
        return made

    async def move(self, canvas_id: str, scope: Scope) -> dict[str, Any]:
        """Hand a canvas to another chat or project. The owner chat stays when it still fits the new
        scope (a chat's canvas moved up to that chat's project keeps writing into the chat)."""
        found = await self.require(canvas_id)
        owner = found["owner_session_id"]
        if scope.kind == "project" and owner and await self.db.fetchone("SELECT 1 FROM sessions WHERE id=? AND project_id=?", (owner, scope.id)) is None:
            owner = None
        owner = await self.check_scope(scope, owner)
        ids = scope.ids()
        await self.db.execute("UPDATE canvases SET session_id=?,project_id=?,owner_session_id=? WHERE id=?", (ids["session_id"], ids["project_id"], owner, canvas_id))
        await self._announce(canvas_id, Scope(found["scope"], found["scope_id"]), "moved", by="operator")
        await self._announce(canvas_id, scope, "moved", by="operator")
        return await self.require(canvas_id)

    async def set_owner(self, canvas_id: str, owner_session_id: str) -> dict[str, Any]:
        found = await self.require(canvas_id)
        owner = await self.check_scope(Scope(found["scope"], found["scope_id"]), owner_session_id)
        await self.db.execute("UPDATE canvases SET owner_session_id=? WHERE id=?", (owner, canvas_id))
        return await self.require(canvas_id)

    async def delete(self, canvas_id: str) -> None:
        found = await self.require(canvas_id)
        await self.db.execute("DELETE FROM canvases WHERE id=?", (canvas_id,))
        await self._announce(canvas_id, Scope(found["scope"], found["scope_id"]), "deleted", by="operator")

    async def _get_in(self, conn: aiosqlite.Connection, canvas_id: str) -> dict[str, Any]:
        row = await (await conn.execute(f"SELECT {ITEM_COLUMNS},c.doc_json{ITEM_FROM} WHERE c.id=?", (canvas_id,))).fetchone()
        files = await (await conn.execute("SELECT id,name,mime,size FROM canvas_files WHERE canvas_id=? ORDER BY created_at", (canvas_id,))).fetchall()
        return {**_item(row), "doc": json.loads(row["doc_json"]), "share_token": row["share_token"], "files": [dict(file) for file in files]}

    async def _write(self, conn: aiosqlite.Connection, canvas_id: str, version: int, title: str, doc: dict[str, Any], clock: dict[str, int], at: str, source: str) -> None:
        nodes, edges = canvas_doc.counts(doc)
        cursor = await conn.execute(
            "UPDATE canvases SET title=?,doc_json=?,clock_json=?,version=version+1,updated_at=?,updated_by=?,preview_svg=?,node_count=?,edge_count=? WHERE id=? AND version=?",
            (title, json.dumps(doc, ensure_ascii=False), json.dumps(clock), at, source, preview_svg(doc), nodes, edges, canvas_id, version),
        )
        if cursor.rowcount == 0:
            raise NotFound(canvas_id)

    # -- history ---------------------------------------------------------------------------------------

    async def versions(self, canvas_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT version,title,saved_at,started_at,source,kind,restored_from,summary_json FROM canvas_revisions WHERE canvas_id=? ORDER BY version DESC LIMIT ?",
            (canvas_id, REVISION_CAP),
        )
        return [{**{key: row[key] for key in row.keys() if key != "summary_json"}, "summary": json.loads(row["summary_json"] or "{}")} for row in rows]

    async def version(self, canvas_id: str, version: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT version,title,doc_json,saved_at,source,kind FROM canvas_revisions WHERE canvas_id=? AND version=?", (canvas_id, version))
        if row is None:
            return None
        return {**{key: row[key] for key in row.keys() if key != "doc_json"}, "doc": json.loads(row["doc_json"])}

    async def preview(self, canvas_id: str, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            row = await self.db.fetchone("SELECT version,preview_svg FROM canvases WHERE id=?", (canvas_id,))
        else:
            row = await self.db.fetchone("SELECT version,preview_svg FROM canvas_revisions WHERE canvas_id=? AND version=?", (canvas_id, version))
        return {"version": row["version"], "svg": row["preview_svg"]} if row else None

    # -- sharing and interchange ----------------------------------------------------------------------

    async def share(self, canvas_id: str) -> str:
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT share_token,session_id,project_id FROM canvases WHERE id=?", (canvas_id,))).fetchone()
            if row is None:
                raise NotFound(canvas_id)
            if row["share_token"]:
                return str(row["share_token"])
            token = secrets.token_urlsafe(24)
            await conn.execute("UPDATE canvases SET share_token=? WHERE id=?", (token, canvas_id))
            scope = _scope_of(row)
        await self._announce(canvas_id, scope, "shared", by="operator")
        return token

    async def revoke_share(self, canvas_id: str) -> None:
        found = await self.require(canvas_id)
        await self.db.execute("UPDATE canvases SET share_token='' WHERE id=?", (canvas_id,))
        await self._announce(canvas_id, Scope(found["scope"], found["scope_id"]), "shared", by="operator")

    async def shared(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        row = await self.db.fetchone("SELECT id FROM canvases WHERE share_token=?", (token,))
        if row is None:
            return None
        found = await self.require(row["id"])
        return {"id": found["id"], "title": found["title"], "version": found["version"], "updated_at": found["updated_at"], "doc": found["doc"], "files": found["files"]}

    async def export_text(self, canvas_id: str) -> tuple[str, str]:
        """The title and the ``.canvas`` text, laid out as Obsidian writes it."""
        found = await self.require(canvas_id)
        return found["title"], canvas_doc.write(found["doc"])

    async def import_text(self, title: str, text: str, scope: Scope, *, owner_session_id: str | None = None) -> dict[str, Any]:
        """A new canvas from a ``.canvas`` file. File nodes keep their paths: a path into a vault means
        nothing here, and a card that says which file it was is more use than a silently dropped one."""
        return await self.create(title, scope, owner_session_id=owner_session_id, doc=canvas_doc.parse(text), kind="import")


HOOK = "canvases"


def store_for(manager: Any) -> CanvasStore:
    """The one store over the manager's database and bus, made on first use, so what the agent writes
    reaches every open board the same way the operator's own edits do."""
    found = manager.service_hooks.get(HOOK)
    if isinstance(found, CanvasStore):
        return found
    bus = getattr(manager, "bus", None)
    store = CanvasStore(manager.db, publish=bus.publish if bus is not None else None)
    manager.service_hooks[HOOK] = store
    return store


__all__ = ["CanvasError", "CanvasStore", "Conflict", "NotFound", "Stale", "insert_canvas", "record_revision", "store_for"]
