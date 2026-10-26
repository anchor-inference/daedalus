"""Memory entries: the titled pages memory is read in, each holding the records that are its bullets.

A record stays what it was — one atomic note with a category, topics, deduplication, history and
undo — and gains ``metadata["entry"]``, the id of the one entry it belongs to. Entries come in three
sections:

* ``you`` — ``preferences`` (how the operator wants the agent to work) and ``profile`` (who they
  are), both in global memory;
* ``topic`` — subjects that are not a project: ``people``, ``recent-work`` (where a note waits until
  something better files it), and one per subject that earns a page;
* ``area`` — one per project of the operator's, holding the project-scoped records; the entry
  survives its project (the foreign key sets ``project_id`` to null) as a free area.

The rule that files a record (:func:`target_for`) is pure, so the migration that filed the records
kept before entries existed runs the same rule as the running host. An entry's ``version`` steps on
every change to it or its bullets, so an edit made against an older version is refused rather than
silently overwriting what changed meanwhile.

Provenance lives in ``memory_sources`` rather than in the record: records are JSON blobs, and
"which records did this chat teach" must be one indexed query when a chat is deleted. A merge copies
the absorbed records' rows to the kept one and leaves theirs in place, so undoing the merge brings
the records back with their sources.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from protocore.contracts.memory import MemoryRecord, MemoryScope, MemoryWriteResult

from daedalus.memory.taxonomy import record_topics, sensitive_subjects

if TYPE_CHECKING:
    from daedalus.stores.database import Database
    from daedalus.stores.persistent import PersistentMemory

SECTIONS: tuple[str, ...] = ("you", "topic", "area")
ORIGINS: tuple[str, ...] = ("synthesis", "remember", "operator", "import", "migration")
"""Who taught a record: the background pass, the agent's Remember, the operator's own hand, an
import from another assistant, or the migration that filed the records kept before entries."""

SUMMARY_MAX_CHARS = 150
"""An entry's summary is the one line the list shows under its title."""

ENTRY_KEY = "entry"
"""The record metadata key naming the record's entry."""

LOCKED_KEY = "locked"
"""Record metadata: the operator wrote or edited this bullet, so the background pass may suggest a
change to it but never makes one."""

ARCHIVED_KEY = "archived"
"""Record metadata: kept, shown folded under the entry's older bullets, and not handed to the agent."""

_OPERATOR_WORDS = re.compile(r"\b(?:operator|operator's|user|user's)\b|оператор\w*|пользовател\w*", re.IGNORECASE)
_SLUG_JUNK = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class Target:
    """Where a record belongs: a section and slug, or the area of a project (``project_id``), whose
    slug is settled when the area is first made, from the project's name."""

    section: str
    slug: str = ""
    title: str = ""
    project_id: str = ""


PREFERENCES = Target("you", "preferences", "Preferences")
PROFILE = Target("you", "profile", "Profile")
PEOPLE = Target("topic", "people", "People")
RECENT_WORK = Target("topic", "recent-work", "Recent work")


def slugify(text: str) -> str:
    """A slug from a name: lower case, words joined by hyphens, letters of any script kept."""
    return _SLUG_JUNK.sub("-", (text or "").strip().lower()).replace("_", "-").strip("-")[:48].strip("-")


def topic_title(slug: str) -> str:
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else slug


def about_operator(text: str) -> bool:
    """Whether a note about a person is about the operator: the notes the agent writes name them in
    the third person ("the operator works nights"), so the word itself is the cue."""
    return bool(_OPERATOR_WORDS.search(text or ""))


def target_for(kind: str, scope: MemoryScope, scope_key: str, text: str, topics: Sequence[str], *, known_topics: Collection[str] = ()) -> Target:
    """The entry a record is filed in, from what it is and where it lives.

    A project-scoped record belongs to its project's area whatever it says: the entry follows the
    scope, or the area would show a note the project's chats cannot recall. In global memory a
    preference is the operator's, a note about a person is their profile or about someone else, and
    anything else goes to the first of its topics that already has a page, else to recent work —
    pages are made for subjects that earn one, not for every tag a note was given."""
    if scope is MemoryScope.project and scope_key:
        return Target("area", project_id=scope_key)
    if kind == "preference":
        return PREFERENCES
    if kind == "person":
        return PROFILE if about_operator(text) else PEOPLE
    known = set(known_topics)
    for topic in topics:
        slug = slugify(topic)
        if slug in known and slug not in {PEOPLE.slug, RECENT_WORK.slug}:
            return Target("topic", slug, topic_title(slug))
    return RECENT_WORK


def entry_of(record: MemoryRecord) -> str:
    return str((record.metadata or {}).get(ENTRY_KEY) or "")


def is_locked(record: MemoryRecord) -> bool:
    return bool((record.metadata or {}).get(LOCKED_KEY))


def is_archived(record: MemoryRecord) -> bool:
    return bool((record.metadata or {}).get(ARCHIVED_KEY))


def new_entry_id() -> str:
    return f"ent-{uuid.uuid4().hex[:12]}"


def now_text() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Entry:
    id: str
    section: str
    slug: str
    title: str
    summary: str = ""
    project_id: str | None = None
    aliases: tuple[str, ...] = ()
    sensitive: tuple[str, ...] = ()
    edited_by_operator: bool = False
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    project_name: str | None = field(default=None, compare=False)
    """The bound project's name, when the row was read with it."""

    @classmethod
    def from_row(cls, row: Any) -> Entry:
        keys = row.keys()
        return cls(
            id=row["id"], section=row["section"], slug=row["slug"], title=row["title"], summary=row["summary"],
            project_id=row["project_id"], aliases=tuple(_json_list(row["aliases"])), sensitive=tuple(_json_list(row["sensitive"])),
            edited_by_operator=bool(row["edited_by_operator"]), version=int(row["version"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            project_name=row["project_name"] if "project_name" in keys else None,
        )

    def view(self, *, count: int | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {
            "id": self.id, "section": self.section, "slug": self.slug, "title": self.title, "summary": self.summary,
            "project_id": self.project_id, "project_name": self.project_name, "aliases": list(self.aliases),
            "sensitive": list(self.sensitive), "edited_by_operator": self.edited_by_operator, "version": self.version,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        if count is not None:
            found["count"] = count
        return found


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


class EntryConflict(Exception):
    """An edit made against an older version of the entry."""


class EntryNotEmpty(Exception):
    """An entry still holding records cannot be deleted: its records would belong nowhere."""


class UnknownProject(ValueError):
    """A project-scoped record named a project that does not exist."""


_SELECT = (
    "SELECT e.*, p.name AS project_name FROM memory_entries e LEFT JOIN projects p ON p.id = e.project_id"
)


class MemoryEntries:
    """The entries of one tenant, over the records the persistent memory holds."""

    def __init__(self, db: Database, memory: PersistentMemory, tenant: str) -> None:
        self.db = db
        self.memory = memory
        self.tenant = tenant
        self._making = asyncio.Lock()
        """One entry made at a time: two notes for the same new area arriving together would
        otherwise both see it missing and the second would take a suffixed slug."""

    # -- reading ---------------------------------------------------------------------------------

    async def list(self, *, section: str | None = None, project_id: str | None = None) -> list[Entry]:
        clauses, params = ["e.tenant_id = ?"], [self.tenant]
        if section:
            clauses.append("e.section = ?")
            params.append(section)
        if project_id:
            clauses.append("e.project_id = ?")
            params.append(project_id)
        rows = await self.db.fetchall(
            f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY CASE e.section WHEN 'you' THEN 0 WHEN 'topic' THEN 1 ELSE 2 END, e.updated_at DESC, e.id",
            params,
        )
        return [Entry.from_row(r) for r in rows]

    async def get(self, entry_id: str) -> Entry | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE e.tenant_id = ? AND e.id = ?", (self.tenant, entry_id))
        return Entry.from_row(row) if row else None

    async def by_slug(self, section: str, slug: str) -> Entry | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE e.tenant_id = ? AND e.section = ? AND e.slug = ?", (self.tenant, section, slug))
        return Entry.from_row(row) if row else None

    async def area_of(self, project_id: str) -> Entry | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE e.tenant_id = ? AND e.section = 'area' AND e.project_id = ?", (self.tenant, project_id))
        return Entry.from_row(row) if row else None

    def records_of(self, entry_id: str) -> list[MemoryRecord]:
        """The entry's bullets, newest first."""
        return [r for r in self.memory.records(self.tenant) if entry_of(r) == entry_id]

    def counts(self) -> dict[str, int]:
        """How many records each entry holds; records filed nowhere are counted under ``""``."""
        found: dict[str, int] = {}
        for record in self.memory.records(self.tenant):
            key = entry_of(record)
            found[key] = found.get(key, 0) + 1
        return found

    async def sources(self, record_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Each record's provenance rows, oldest first, with the title of the chat while it exists."""
        ids = list(dict.fromkeys(record_ids))
        found: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        if not ids:
            return found
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(
            "SELECT m.*, s.title AS session_title FROM memory_sources m LEFT JOIN sessions s ON s.id = m.session_id AND m.session_id != ''"
            f" WHERE m.tenant_id = ? AND m.record_id IN ({marks}) ORDER BY m.at, m.origin",
            (self.tenant, *ids),
        )
        for row in rows:
            found[row["record_id"]].append({
                "origin": row["origin"], "session_id": row["session_id"] or None, "session_title": row["session_title"],
                "from_seq": row["from_seq"] or None, "to_seq": row["to_seq"] or None, "run_id": row["run_id"] or None,
                "quote": row["quote"], "at": row["at"],
            })
        return found

    async def saved_by_run(self, run_id: str) -> list[Entry]:
        """The entries a run's Remember calls filed something in, for the "saved to memory" note under
        the answer; a record forgotten since does not count."""
        rows = await self.db.fetchall("SELECT DISTINCT record_id FROM memory_sources WHERE tenant_id = ? AND run_id = ? AND origin = 'remember'", (self.tenant, run_id))
        ids = list(dict.fromkeys(entry_of(r) for row in rows if (r := self.memory.get_record(self.tenant, row["record_id"])) is not None))
        found = [e for e in [await self.get(i) for i in ids if i] if e is not None]
        return found

    # -- writing ---------------------------------------------------------------------------------

    async def create(self, section: str, slug: str, title: str, *, summary: str = "", project_id: str | None = None, aliases: Sequence[str] = ()) -> Entry:
        """A new entry; ``ValueError`` when the section is unknown or the slug is taken."""
        if section not in SECTIONS:
            raise ValueError(f"unknown section {section!r}; one of {', '.join(SECTIONS)}")
        slug = slugify(slug)
        if not slug or not title.strip():
            raise ValueError("an entry needs a slug and a title")
        async with self._making:
            if await self.by_slug(section, slug) is not None:
                raise ValueError(f"{section}/{slug} exists")
            return await self._insert(section, slug, title.strip(), summary=summary, project_id=project_id, aliases=aliases)

    async def _insert(self, section: str, slug: str, title: str, *, summary: str = "", project_id: str | None = None, aliases: Sequence[str] = ()) -> Entry:
        at = now_text()
        entry_id = new_entry_id()
        await self.db.execute(
            "INSERT INTO memory_entries(id, tenant_id, section, slug, title, summary, project_id, aliases, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, self.tenant, section, slug, title, summary.strip()[:SUMMARY_MAX_CHARS], project_id, json.dumps(list(aliases), ensure_ascii=False), at, at),
        )
        made = await self.get(entry_id)
        assert made is not None
        return made

    async def ensure(self, target: Target) -> Entry:
        """The entry ``target`` names, made when it does not exist yet."""
        async with self._making:
            if target.project_id:
                found = await self.area_of(target.project_id)
                if found is not None:
                    return found
                row = await self.db.fetchone("SELECT name FROM projects WHERE id = ?", (target.project_id,))
                if row is None:
                    raise UnknownProject(f"no project with the id {target.project_id!r}")
                taken = {e.slug for e in await self.list(section="area")}
                return await self._insert("area", area_slug(str(row["name"]), target.project_id, taken), str(row["name"]), project_id=target.project_id)
            found = await self.by_slug(target.section, target.slug)
            if found is not None:
                return found
            return await self._insert(target.section, target.slug, target.title or topic_title(target.slug))

    async def update(self, entry_id: str, *, if_version: int, title: str | None = None, summary: str | None = None, aliases: Sequence[str] | None = None, by_operator: bool = False) -> Entry:
        """Change an entry's title, summary or aliases, refused with :class:`EntryConflict` when it
        changed since ``if_version`` was read; ``KeyError`` when there is no such entry."""
        current = await self.get(entry_id)
        if current is None:
            raise KeyError(entry_id)
        if current.version != if_version:
            raise EntryConflict(f"the entry is at version {current.version}, not {if_version}")
        changes: dict[str, Any] = {}
        if title is not None and title.strip() and title.strip() != current.title:
            changes["title"] = title.strip()
        if summary is not None and summary.strip()[:SUMMARY_MAX_CHARS] != current.summary:
            changes["summary"] = summary.strip()[:SUMMARY_MAX_CHARS]
        if aliases is not None and tuple(aliases) != current.aliases:
            changes["aliases"] = json.dumps(list(aliases), ensure_ascii=False)
        if not changes:
            return current
        if by_operator:
            changes["edited_by_operator"] = 1
        sets = ", ".join(f"{name} = ?" for name in changes)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE memory_entries SET {sets}, version = version + 1, updated_at = ? WHERE tenant_id = ? AND id = ? AND version = ?",
                (*changes.values(), now_text(), self.tenant, entry_id, if_version),
            )
            if not cursor.rowcount:
                raise EntryConflict("the entry changed while it was being edited")
        updated = await self.get(entry_id)
        assert updated is not None
        return updated

    async def delete(self, entry_id: str) -> bool:
        """Remove an empty entry; :class:`EntryNotEmpty` while records still belong to it."""
        if self.records_of(entry_id):
            raise EntryNotEmpty(entry_id)
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM memory_entries WHERE tenant_id = ? AND id = ?", (self.tenant, entry_id))
            return bool(cursor.rowcount)

    async def touched(self, entry_id: str) -> None:
        """Step an entry's version after its bullets changed, and refresh its sensitive subjects (the
        union of its bullets', which the list shows as a badge)."""
        subjects = sorted({s for r in self.records_of(entry_id) for s in sensitive_subjects(r.text)})
        await self.db.execute(
            "UPDATE memory_entries SET version = version + 1, updated_at = ?, sensitive = ? WHERE tenant_id = ? AND id = ?",
            (now_text(), json.dumps(subjects), self.tenant, entry_id),
        )

    async def add_source(self, record_id: str, origin: str, *, session_id: str = "", run_id: str = "", from_seq: int = 0, to_seq: int = 0, quote: str = "", at: str | None = None) -> None:
        if origin not in ORIGINS:
            raise ValueError(f"unknown origin {origin!r}")
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_sources(record_id, tenant_id, origin, session_id, from_seq, to_seq, run_id, quote, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, self.tenant, origin, session_id or "", int(from_seq or 0), int(to_seq or 0), run_id or "", quote or "", at or now_text()),
        )

    async def copy_sources(self, from_ids: Iterable[str], to_id: str) -> None:
        """Give ``to_id`` every source the records ``from_ids`` had (a merge's absorbed records)."""
        ids = [i for i in dict.fromkeys(from_ids) if i != to_id]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_sources(record_id, tenant_id, origin, session_id, from_seq, to_seq, run_id, quote, at)"
            f" SELECT ?, tenant_id, origin, session_id, from_seq, to_seq, run_id, quote, at FROM memory_sources WHERE tenant_id = ? AND record_id IN ({marks})",
            (to_id, self.tenant, *ids),
        )

    async def target(self, kind: str, scope: MemoryScope, scope_key: str, text: str, topics: Sequence[str]) -> Entry:
        """The entry a new record with these properties is filed in, made when needed."""
        known = {e.slug for e in await self.list(section="topic")}
        return await self.ensure(target_for(kind, scope, scope_key, text, topics, known_topics=known))

    async def remember(
        self,
        scope: MemoryScope,
        scope_key: str,
        text: str,
        *,
        kind: str,
        metadata: dict[str, Any],
        origin: str,
        session_id: str = "",
        run_id: str = "",
        entry: Entry | None = None,
        **write: Any,
    ) -> MemoryWriteResult:
        """Write a record into memory filed in its entry, with a source row saying who taught it.

        The one door every new record comes through — the agent's Remember and the operator's own
        note — so a record is never kept without an entry and a source. ``entry`` files it in that
        entry instead of the one :func:`target_for` picks (the caller has matched the scope to it).
        Keyword arguments past the named ones go to the store's write (similarity threshold,
        per-scope cap)."""
        entry = entry or await self.target(kind, scope, scope_key, text, record_topics(metadata))
        result = await self.memory.write(self.tenant, scope, scope_key, text, kind=kind, metadata={**metadata, ENTRY_KEY: entry.id}, **write)
        await self.add_source(result.record.id, origin, session_id=session_id, run_id=run_id)
        await self.touched(entry_of(result.record) or entry.id)
        return result

    @staticmethod
    def placement(entry: Entry) -> tuple[MemoryScope, str]:
        """The scope a bullet written straight into ``entry`` lives in: its project's, for the area of
        a project, so the project's chats recall it; global for everything else, a free area included."""
        if entry.section == "area" and entry.project_id:
            return MemoryScope.project, entry.project_id
        return MemoryScope.global_, ""

    async def entry_rows(self, entry_ids: Iterable[str]) -> list[dict[str, Any]]:
        """The entries' rows as they are stored, for a history entry that may have to put them back."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(f"SELECT * FROM memory_entries WHERE tenant_id = ? AND id IN ({marks})", (self.tenant, *ids))
        return [dict(row) for row in rows]

    async def revive(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Put entry rows back as :meth:`entry_rows` read them; one whose section and slug were taken
        meanwhile is skipped, and its records are filed again at the next start."""
        for row in rows:
            names = list(row.keys())
            await self.db.execute(
                f"INSERT OR IGNORE INTO memory_entries({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                tuple(row[n] for n in names),
            )

    async def source_rows(self, *, record_ids: Iterable[str] = (), session_id: str = "") -> list[dict[str, Any]]:
        """Raw provenance rows: those of some records, or every row a chat left."""
        if session_id:
            rows = await self.db.fetchall("SELECT * FROM memory_sources WHERE tenant_id = ? AND session_id = ?", (self.tenant, session_id))
        else:
            ids = list(dict.fromkeys(record_ids))
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            rows = await self.db.fetchall(f"SELECT * FROM memory_sources WHERE tenant_id = ? AND record_id IN ({marks})", (self.tenant, *ids))
        return [dict(row) for row in rows]

    async def restore_sources(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            names = list(row.keys())
            await self.db.execute(
                f"INSERT OR IGNORE INTO memory_sources({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                tuple(row[n] for n in names),
            )

    async def drop_sources(self, *, session_id: str = "", record_ids: Iterable[str] = ()) -> None:
        if session_id:
            await self.db.execute("DELETE FROM memory_sources WHERE tenant_id = ? AND session_id = ?", (self.tenant, session_id))
        ids = list(dict.fromkeys(record_ids))
        if ids:
            marks = ",".join("?" * len(ids))
            await self.db.execute(f"DELETE FROM memory_sources WHERE tenant_id = ? AND record_id IN ({marks})", (self.tenant, *ids))

    async def file_unfiled(self) -> int:
        """File every record that has no entry, or names one that is gone; returns how many.

        Records reach the store without an entry only by a path that bypasses :meth:`remember` — a
        test, a restore of an old export, an entry removed by hand — and a record filed nowhere is one
        no entry page shows. Run at start, where it costs a pass over a few dozen records."""
        known = {e.id for e in await self.list()}
        filed = 0
        touched: set[str] = set()
        for record in self.memory.records(self.tenant):
            if entry_of(record) in known:
                continue
            try:
                entry = await self.target(record.kind, record.scope, record.scope_key, record.text, record_topics(record.metadata))
            except UnknownProject:
                # A record of a project since deleted: it waits in recent work, where the operator sees it.
                entry = await self.ensure(RECENT_WORK)
            known.add(entry.id)
            await self.memory.restore(record.model_copy(update={"metadata": {**record.metadata, ENTRY_KEY: entry.id}}))
            touched.add(entry.id)
            filed += 1
        for entry_id in touched:
            await self.touched(entry_id)
        return filed

    async def forget(self, record_id: str, *, actor: str, reason: str = "forgotten") -> MemoryRecord | None:
        """Remove a record as the agent's Forget or the operator's delete does: its entry steps, a
        tombstone keeps the background pass from learning it again, and a history row lets the
        operator undo it. ``None`` when there is no such record.

        Forget used to be the core's bare delete: the entry kept its version, so an edit made against
        it went through as if nothing had changed, and nothing remembered that the note was unwanted."""
        record = self.memory.get_record(self.tenant, record_id)
        if record is None or not await self.memory.delete(self.tenant, record_id):
            return None
        entry_id = entry_of(record)
        entry = await self.get(entry_id) if entry_id else None
        if entry is not None:
            await self.touched(entry.id)
        await self.tombstone(record.text, entry_slug=entry.slug if entry else "", reason=reason)
        await self.db.execute(
            "INSERT INTO memory_history(tenant_id, at, actor, action, summary, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, '[]')",
            (self.tenant, now_text(), actor, "forget", record.text[:500], json.dumps([json.loads(record.model_dump_json())], ensure_ascii=False)),
        )
        return record

    async def tombstone(self, text: str, *, entry_slug: str = "", reason: str = "deleted") -> None:
        """Remember that a note was removed, by its :func:`fingerprint`, so it is never learned again."""
        await self.db.execute(
            "INSERT INTO memory_tombstones(tenant_id, fingerprint, entry_slug, reason, at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(tenant_id, fingerprint) DO UPDATE SET entry_slug = excluded.entry_slug, reason = excluded.reason, at = excluded.at",
            (self.tenant, fingerprint(text), entry_slug, reason, now_text()),
        )

    async def untombstone(self, texts: Iterable[str]) -> None:
        """Drop the tombstones of notes put back (an undone forget): a note the operator wants is not unwanted."""
        prints = list(dict.fromkeys(fingerprint(t) for t in texts))
        if prints:
            await self.db.execute(
                f"DELETE FROM memory_tombstones WHERE tenant_id = ? AND fingerprint IN ({','.join('?' * len(prints))})", (self.tenant, *prints),
            )

    async def tombstoned(self, text: str) -> bool:
        return await self.db.fetchone("SELECT 1 FROM memory_tombstones WHERE tenant_id = ? AND fingerprint = ?", (self.tenant, fingerprint(text))) is not None

    async def named(self, name: str) -> list[Entry]:
        """The entries ``name`` refers to: ``section/slug`` names one; a bare slug names every entry
        with that slug (an area and a topic may share one), a project's area by its project id too."""
        section, _, slug = name.strip().rpartition("/")
        slug = slugify(slug)
        if not slug:
            return []
        if section:
            found = await self.by_slug(section.strip().lower(), slug)
            return [found] if found is not None else []
        listed = await self.list()
        return [e for e in listed if e.slug == slug] or [e for e in listed if e.project_id and e.project_id == name.strip()]

    async def listing(self) -> list[dict[str, Any]]:
        """Every entry with its bullet count, in section order: what the Memory page lists and the
        seam the turn-context memory block will be rendered from."""
        counts = self.counts()
        return [entry.view(count=counts.get(entry.id, 0)) for entry in await self.list()]


def migrated_scope(scope: str, scope_key: str, kind: str, *, session_project: Mapping[str, str], projects: Collection[str]) -> tuple[MemoryScope, str]:
    """Where a record kept before entries lives now (the migration's rule, kept here beside the
    filing rule it feeds so the two are read together).

    ``session_project`` maps a session id to the operator's project it belongs to (empty for a lone
    chat or a system project); ``projects`` are the ids of the operator's projects. A per-chat record
    moves to its chat's project, or to global memory when the chat belongs to none or is gone — except
    a preference or a note about a person, which is about the operator and their world wherever it
    was said, and goes to global memory, where the You section and People live. A project record whose
    project is gone or is only a chat goes to global memory too: nothing would recall it otherwise."""
    if scope == MemoryScope.project.value and scope_key in projects:
        return MemoryScope.project, scope_key
    if scope == MemoryScope.session.value:
        project = session_project.get(scope_key, "")
        if project and kind not in ("preference", "person"):
            return MemoryScope.project, project
    return MemoryScope.global_, ""


_FINGERPRINT_JUNK = re.compile(r"[\W_]+", re.UNICODE)


def fingerprint(text: str) -> str:
    """A removed note's identity for the tombstones: its words, case and punctuation aside, so the
    same note said again with a different full stop is still recognised."""
    words = _FINGERPRINT_JUNK.sub(" ", (text or "").lower()).strip()
    return hashlib.sha256(words.encode("utf-8")).hexdigest()[:32]


def area_slug(name: str, project_id: str, taken: Collection[str]) -> str:
    """A project's area slug from its name, made unique with the project's id when the name is taken."""
    base = slugify(name) or "project"
    if base not in taken:
        return base
    return f"{base}-{slugify(project_id)[:8] or uuid.uuid4().hex[:6]}"


__all__ = [
    "ARCHIVED_KEY",
    "ENTRY_KEY",
    "LOCKED_KEY",
    "ORIGINS",
    "PEOPLE",
    "PREFERENCES",
    "PROFILE",
    "RECENT_WORK",
    "SECTIONS",
    "SUMMARY_MAX_CHARS",
    "Entry",
    "EntryConflict",
    "EntryNotEmpty",
    "MemoryEntries",
    "Target",
    "UnknownProject",
    "about_operator",
    "area_slug",
    "entry_of",
    "fingerprint",
    "is_archived",
    "is_locked",
    "migrated_scope",
    "slugify",
    "target_for",
    "topic_title",
]
