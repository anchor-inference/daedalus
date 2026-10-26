"""Keeping memory tidy: merges, edits that can be undone, and the background pass that finds
near-duplicates and records a newer one contradicts.

Every change made here writes a history entry holding the records as they were before it, so the
operator can put them back from the Memory screen — the automatic merges above all, which happen
while nobody is looking. A change the comparison is sure of is applied (when the operator left
``memory.auto_merge`` on); anything less waits as a suggestion.

"Sure" is deliberately narrow. A duplicate is sure when the two records are in the same scope and
share nearly every word. A contradiction is sure when two records in the same scope say the same
words around different values — "the API listens on port 8765" and later "… port 48765" — and then
the newer one wins: the agent learned the second after the first. Everything that merely looks
alike, or sits in different scopes (a session's note and a global one), is a suggestion, because
merging across scopes changes who can recall the record.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from protocore.contracts.memory import MemoryRecord, MemoryScope

from daedalus.memory.entries import (
    ARCHIVED_KEY,
    LOCKED_KEY,
    Entry,
    EntryConflict,
    EntryNotEmpty,
    MemoryEntries,
    Target,
    entry_of,
    is_locked,
)
from daedalus.memory.taxonomy import classify, normalise_topics, record_topics, sensitive_subjects
from daedalus.memory.transfer import target_of

if TYPE_CHECKING:
    from daedalus.config import MemoryConfig
    from daedalus.stores.database import Database
    from daedalus.stores.persistent import PersistentMemory

logger = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 1800
"""How often the background pass looks; it does work only when a record changed since the last pass."""
MAX_RECORDS_COMPARED = 600
"""The newest records the pass compares pairwise; past that the oldest are left as they are."""
MAX_NEW_SUGGESTIONS = 40
EMBEDDINGS_PER_PASS = 64
"""Records encoded per pass when the local embeddings model is ready, so a first pass over a large
memory does not hold the encoder away from the conversation index for minutes."""

DUPLICATE_SURE = 0.85
DUPLICATE_MAYBE = 0.6
COSINE_SURE = 0.96
COSINE_MAYBE = 0.9
CONTRADICTION_WORDS = 0.75
"""Share of the non-value words two records must have in common to be read as one statement with
different values; below it two records with different numbers are simply about different things."""

SCOPE_BREADTH = {MemoryScope.global_: 0, MemoryScope.user: 1, MemoryScope.project: 2, MemoryScope.agent: 3, MemoryScope.custom: 4, MemoryScope.session: 5}

_TOKEN = re.compile(r"[^\W_][\w.:/-]*[^\W_]|[^\W_]", re.UNICODE)


class CuratorConflict(Exception):
    """The records changed since the history entry or suggestion was made; nothing was done."""


def words_of(text: str) -> set[str]:
    """A text's words and values, lower case: what the duplicate comparisons measure overlap on."""
    return {t.lower() for t in _TOKEN.findall(text or "")}


def _values(tokens: set[str]) -> set[str]:
    """The tokens that carry a value — a number, a version, an id — rather than a word."""
    return {t for t in tokens if any(ch.isdigit() for ch in t)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _record_json(record: MemoryRecord) -> dict[str, Any]:
    return json.loads(record.model_dump_json())


def _fingerprint(kind: str, records: Sequence[MemoryRecord]) -> str:
    """One finding's identity: the records and their texts, so a dismissed suggestion stays
    dismissed until one of its records says something different."""
    parts = sorted(f"{r.id}:{hashlib.sha1(r.text.encode()).hexdigest()[:12]}" for r in records)
    return hashlib.sha1(f"{kind}|{'|'.join(parts)}".encode()).hexdigest()


LEARNED_KINDS = ("revision", "addition")
"""Suggestions the background pass that learns from chats leaves (:mod:`daedalus.memory.synthesis`):
a change to a bullet it may not or would not make itself, and a new bullet it held back."""


def _learned_print(kind: str, records: Sequence[MemoryRecord], text: str, detail: dict[str, Any]) -> str:
    """A learned suggestion's identity: the bullet it would change (as it reads now) or the entry it
    would add to, and what it proposes — so the same proposal from a later pass is not offered twice,
    and one whose bullet has since changed reads as stale."""
    parts = sorted(f"{r.id}:{hashlib.sha1(r.text.encode()).hexdigest()[:12]}" for r in records)
    target = str(detail.get("entry_key") or "") if kind == "addition" else str(detail.get("action") or "")
    return hashlib.sha1(f"{kind}|{'|'.join(parts)}|{target}|{' '.join(text.lower().split())}".encode()).hexdigest()


@dataclass(frozen=True)
class Finding:
    kind: str
    """``duplicate`` or ``contradiction``."""
    records: tuple[MemoryRecord, MemoryRecord]
    keep: MemoryRecord
    text: str
    score: float
    sure: bool
    reason: str


def _broadest_then_newest(records: Sequence[MemoryRecord]) -> MemoryRecord:
    return min(records, key=lambda r: (SCOPE_BREADTH.get(r.scope, 9), -r.updated_at.timestamp()))


def find(records: Sequence[MemoryRecord], vectors: dict[str, list[float]] | None = None) -> list[Finding]:
    """Every pair worth a word, best first, each record in at most one finding."""
    pool = sorted(records, key=lambda r: r.updated_at, reverse=True)[:MAX_RECORDS_COMPARED]
    tokens = {r.id: words_of(r.text) for r in pool}
    candidates: list[Finding] = []
    for i, a in enumerate(pool):
        ta = tokens[a.id]
        for b in pool[i + 1:]:
            tb = tokens[b.id]
            if not ta or not tb:
                continue
            same_bucket = a.scope is b.scope and a.scope_key == b.scope_key
            lexical = _jaccard(ta, tb)
            cosine = _cosine(vectors[a.id], vectors[b.id]) if vectors and a.id in vectors and b.id in vectors else 0.0
            va, vb = _values(ta), _values(tb)
            words_a, words_b = ta - va, tb - vb
            if " ".join(a.text.lower().split()) == " ".join(b.text.lower().split()):
                keep = _broadest_then_newest((a, b))
                candidates.append(Finding("duplicate", (a, b), keep, keep.text, 1.0, same_bucket, "same text"))
                continue
            if va and vb and va != vb and len(words_a | words_b) >= 3 and _jaccard(words_a, words_b) >= CONTRADICTION_WORDS:
                newer = a if a.updated_at >= b.updated_at else b
                sure = same_bucket and words_a == words_b
                candidates.append(Finding("contradiction", (a, b), newer, newer.text, _jaccard(words_a, words_b), sure, "same statement, different values"))
                continue
            if lexical >= DUPLICATE_MAYBE or cosine >= COSINE_MAYBE:
                sure = same_bucket and (lexical >= DUPLICATE_SURE or cosine >= COSINE_SURE)
                keep = _broadest_then_newest((a, b))
                longer = max((a, b), key=lambda r: len(r.text))
                candidates.append(Finding("duplicate", (a, b), keep, longer.text, max(lexical, cosine), sure, "nearly the same words" if lexical >= cosine else "nearly the same meaning"))
    candidates.sort(key=lambda f: (f.sure, f.score), reverse=True)
    used: set[str] = set()
    out: list[Finding] = []
    for finding in candidates:
        ids = {r.id for r in finding.records}
        if ids & used:
            continue
        used |= ids
        out.append(finding)
    return out


class MemoryCurator:
    def __init__(
        self,
        db: Database,
        memory: PersistentMemory,
        tenant: str,
        config: Callable[[], MemoryConfig],
        *,
        embed: Callable[[Sequence[MemoryRecord]], Awaitable[dict[str, list[float]]]] | None = None,
        notify: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.memory = memory
        self.tenant = tenant
        self.config = config
        self.embed = embed
        self.notify = notify
        self.entries = MemoryEntries(db, memory, tenant)
        self.lock = asyncio.Lock()
        """One change at a time: a merge reads records, then writes and deletes, and the background
        pass must not merge a record the operator is merging by hand at the same moment."""

    def _get(self, memory_id: str) -> MemoryRecord:
        record = self.memory.get_record(self.tenant, memory_id)
        if record is None:
            raise KeyError(memory_id)
        return record

    async def _journal(self, actor: str, action: str, summary: str, before: Sequence[MemoryRecord], after: Sequence[MemoryRecord], extra: dict[str, Any] | None = None) -> int:
        """One history entry. ``extra`` carries what the change removed besides records — ``entries``
        (rows it deleted), ``sources`` (provenance rows it dropped) — and ``made`` (entries it created),
        so an undo restores all of it together; the tombstones of the records it removed are found by
        their text."""
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO memory_history(tenant_id, at, actor, action, summary, before_json, after_json, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.tenant, datetime.now(UTC).isoformat(), actor, action, summary[:500],
                 json.dumps([_record_json(r) for r in before], ensure_ascii=False), json.dumps([_record_json(r) for r in after], ensure_ascii=False),
                 json.dumps({k: v for k, v in (extra or {}).items() if v}, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid or 0)

    async def journal(self, actor: str, action: str, summary: str, before: Sequence[MemoryRecord], after: Sequence[MemoryRecord], extra: dict[str, Any] | None = None) -> int:
        """One history entry for a change made outside the curator's own methods (the background
        pass's), under the same rules as :meth:`_journal`; the caller holds :attr:`lock`."""
        return await self._journal(actor, action, summary, before, after, extra)

    async def suggest(self, kind: str, records: Sequence[MemoryRecord], text: str, *, reason: str, detail: dict[str, Any]) -> bool:
        """File a learned suggestion (``revision`` or ``addition``); ``False`` when the same one was
        already filed, whatever became of it — a dismissed proposal is not offered again."""
        if kind not in LEARNED_KINDS:
            raise ValueError(f"unknown suggestion kind {kind!r}")
        fingerprint = _learned_print(kind, records, text, detail)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO memory_suggestions(tenant_id, kind, memory_ids, fingerprint, keep_id, proposed_text, score, reason, status, created_at, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'open', ?, ?)",
                (self.tenant, kind, json.dumps([r.id for r in records]), fingerprint, records[0].id if records else "", text,
                 reason[:500], datetime.now(UTC).isoformat(), json.dumps(detail, ensure_ascii=False)),
            )
            return bool(cursor.rowcount)

    async def _merge(self, ids: Sequence[str], *, actor: str, action: str, keep_id: str | None = None, text: str | None = None, category: str | None = None, topics: Sequence[str] | None = None, summary: str = "") -> tuple[MemoryRecord, int]:
        unique = list(dict.fromkeys(ids))
        if len(unique) < 2:
            raise ValueError("a merge needs two records or more")
        records = [self._get(i) for i in unique]
        keep = next((r for r in records if r.id == keep_id), None) or _broadest_then_newest(records)
        body = (text or "").strip() or keep.text
        merged_topics = normalise_topics(list(topics) if topics is not None else [t for r in (keep, *records) for t in record_topics(r.metadata)])
        refs = list(dict.fromkeys(ref for r in records for ref in r.source_refs))
        merged = keep.model_copy(update={
            "text": body,
            "kind": category or keep.kind,
            "metadata": {**keep.metadata, "topics": merged_topics},
            "source_refs": refs,
            "salience": max(r.salience for r in records),
            "access_count": sum(r.access_count for r in records),
            "created_at": min(r.created_at for r in records),
            "version": keep.version + 1,
            "updated_at": datetime.now(UTC),
        })
        await self.memory.restore(merged)
        # The kept record now says what the others said, so it was taught by their chats too; their
        # own rows stay, and an undo that brings them back brings their sources with them.
        await self.entries.copy_sources([r.id for r in records], keep.id)
        for record in records:
            if record.id != keep.id:
                await self.memory.delete(self.tenant, record.id)
        for entry_id in {entry_of(r) for r in records} - {""}:
            await self.entries.touched(entry_id)
        history = await self._journal(actor, action, summary or f"{len(records)} records into one", records, [merged])
        return merged, history

    async def merge(self, ids: Sequence[str], *, text: str | None = None, category: str | None = None, topics: Sequence[str] | None = None, keep_id: str | None = None, actor: str = "operator") -> tuple[MemoryRecord, int]:
        """Fold records into one (the broadest scope's, the newest among equals, unless ``keep_id``
        names another): one text, the union of topics and sources, the others forgotten."""
        async with self.lock:
            return await self._merge(ids, actor=actor, action="merge", keep_id=keep_id, text=text, category=category, topics=topics)

    async def edit(self, memory_id: str, *, text: str | None = None, category: str | None = None, topics: Sequence[str] | None = None, actor: str = "operator", if_version: int | None = None) -> tuple[MemoryRecord, int | None]:
        """Change a record's text, category or topics, with a history entry when anything changed.

        ``if_version`` refuses the edit with :class:`CuratorConflict` when the record moved on since
        it was read. A text the operator rewrote becomes theirs: it is locked, so the background pass
        only ever suggests changes to it."""
        async with self.lock:
            before = self._get(memory_id)
            if if_version is not None and before.version != if_version:
                raise CuratorConflict(f"the record is at version {before.version}, not {if_version}")
            after = await self.memory.update(self.tenant, memory_id, text=text, kind=category, topics=list(topics) if topics is not None else None)
            if after.version == before.version:
                return after, None
            what = [name for name, changed in (("text", after.text != before.text), ("category", after.kind != before.kind), ("topics", record_topics(after.metadata) != record_topics(before.metadata))) if changed]
            if actor == "operator" and "text" in what and not is_locked(after):
                after = after.model_copy(update={"metadata": {**after.metadata, LOCKED_KEY: True}})
                await self.memory.restore(after)
            action = "recategorise" if what and "text" not in what else "edit"
            history = await self._journal(actor, action, ", ".join(what), [before], [after])
            if entry_of(after):
                await self.entries.touched(entry_of(after))
            return after, history

    async def mark(self, memory_id: str, *, locked: bool | None = None, archived: bool | None = None, actor: str = "operator") -> tuple[MemoryRecord, int | None]:
        """Lock or unlock a bullet, archive it or bring it back; the version steps, so a stale edit of
        it is refused, and the history can put it back."""
        async with self.lock:
            before = self._get(memory_id)
            metadata = dict(before.metadata)
            changes: list[str] = []
            for key, value, on, off in ((LOCKED_KEY, locked, "lock", "unlock"), (ARCHIVED_KEY, archived, "archive", "unarchive")):
                if value is None or bool(metadata.get(key)) == value:
                    continue
                if value:
                    metadata[key] = True
                else:
                    metadata.pop(key, None)
                changes.append(on if value else off)
            if not changes:
                return before, None
            after = before.model_copy(update={"metadata": metadata, "version": before.version + 1, "updated_at": datetime.now(UTC)})
            await self.memory.restore(after)
            history = await self._journal(actor, changes[0], ", ".join(changes), [before], [after])
            if entry_of(after):
                await self.entries.touched(entry_of(after))
            return after, history

    async def _remove(self, records: Sequence[MemoryRecord], *, actor: str, action: str, summary: str, tombstone: str | None, extra: dict[str, Any] | None = None) -> int | None:
        """Delete records with one history entry that puts them back, sources and all. The batch twin of
        :meth:`MemoryEntries.forget`: each record is tombstoned with ``tombstone`` as the reason through
        the same :meth:`MemoryEntries.tombstone`, so the background pass does not learn it again, and
        the undo takes the tombstones away. The caller holds the lock."""
        if not records:
            return None
        ids = [r.id for r in records]
        sources = await self.entries.source_rows(record_ids=ids)
        slugs = {e.id: e.slug for e in await self.entries.list()}
        for record in records:
            if tombstone:
                await self.entries.tombstone(record.text, entry_slug=slugs.get(entry_of(record), ""), reason=tombstone)
            await self.memory.delete(self.tenant, record.id)
        await self.entries.drop_sources(record_ids=ids)
        extra = dict(extra or {})
        extra["sources"] = [*extra.get("sources", []), *sources]
        history = await self._journal(actor, action, summary, records, [], extra)
        for entry_id in {entry_of(r) for r in records} - {""}:
            if await self.entries.get(entry_id) is not None:
                await self.entries.touched(entry_id)
        return history

    async def remove(self, ids: Sequence[str], *, actor: str = "operator", reason: str = "deleted") -> tuple[int, int | None]:
        """Forget records; the bullets are tombstoned so the background pass does not learn them again
        from the same chats. Returns how many went and the history entry that brings them back."""
        async with self.lock:
            records = [r for i in dict.fromkeys(ids) if (r := self.memory.get_record(self.tenant, i)) is not None]
            history = await self._remove(records, actor=actor, action="delete", summary=f"{len(records)} record(s)", tombstone=reason)
            return len(records), history

    async def remove_entry(self, entry_id: str, *, if_version: int, actor: str = "operator") -> tuple[int, int | None]:
        """Delete an entry with its bullets, refused with :class:`EntryConflict` when it changed since
        ``if_version``. The You pages stay (they are where the operator's own notes are filed) and only
        lose their bullets. One history entry restores the page and every bullet."""
        async with self.lock:
            entry = await self.entries.get(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if entry.version != if_version:
                raise EntryConflict(f"the entry is at version {entry.version}, not {if_version}")
            records = self.entries.records_of(entry_id)
            rows = [] if entry.section == "you" else await self.entries.entry_rows([entry_id])
            if rows:
                await self.db.execute("DELETE FROM memory_entries WHERE tenant_id = ? AND id = ?", (self.tenant, entry_id))
            history = await self._remove(records, actor=actor, action="delete_entry", summary=entry.title, tombstone="deleted", extra={"entries": rows})
            if history is None and rows:
                history = await self._journal(actor, "delete_entry", entry.title, [], [], {"entries": rows})
            return len(records), history

    async def forget_session(self, session_id: str, *, title: str = "") -> dict[str, Any]:
        """What a deleted chat taught leaves with it.

        A record whose every source is this chat goes; one also taught by another chat, written by the
        operator or imported stays, as does a locked one (the operator made it theirs). The chat's
        provenance rows go either way, so a record kept now because a second chat backs it goes when
        that chat is deleted too. The removed records are tombstoned with the reason
        ``session_deleted``, so a fork or a copy of the chat does not teach them again; the background
        pass may weigh that reason below an operator's own delete. One history entry restores the
        records, every row, and takes the tombstones away."""
        async with self.lock:
            rows = await self.entries.source_rows(session_id=session_id)
            if not rows:
                return {"removed": 0, "kept": 0, "history_id": None}
            taught = list(dict.fromkeys(r["record_id"] for r in rows))
            marks = ",".join("?" * len(taught))
            backed = {row["record_id"] for row in await self.db.fetchall(
                f"SELECT DISTINCT record_id FROM memory_sources WHERE tenant_id = ? AND record_id IN ({marks}) AND session_id != ?",
                (self.tenant, *taught, session_id),
            )}
            live = [r for i in taught if (r := self.memory.get_record(self.tenant, i)) is not None]
            going = [r for r in live if r.id not in backed and not is_locked(r)]
            await self.entries.drop_sources(session_id=session_id)
            await self.db.execute("DELETE FROM memory_synthesis WHERE session_id = ?", (session_id,))
            # Every row the chat left goes into the history, read before they were dropped: the removed
            # records' rows as much as the kept ones', or an undo brings records back with no source.
            history = await self._remove(going, actor="operator", action="session_deleted", summary=title or session_id, tombstone="session_deleted", extra={"sources": rows})
            if history is None:
                history = await self._journal("operator", "session_deleted", title or session_id, [], [], {"sources": rows})
            for entry_id in {entry_of(r) for r in live if r not in going} - {""}:
                await self.entries.touched(entry_id)
            return {"removed": len(going), "kept": len(live) - len(going), "history_id": history}

    async def add(self, text: str, *, kind: str, topics: Sequence[str], entry: Entry | None = None, origin: str = "operator", actor: str = "operator", metadata: dict[str, Any] | None = None, action: str = "add", summary: str = "") -> tuple[Any, int]:
        """The operator's own note, filed by the rule (or straight into ``entry``) through the one
        door every record comes through, locked, with a history entry whose undo takes it away again
        (and the entry it made, when it made one)."""
        async with self.lock:
            return await self._add(text, kind=kind, topics=topics, entry=entry, origin=origin, actor=actor, metadata=metadata, action=action, summary=summary)

    async def quick(self, text: str, *, classifier: Callable[[str], Awaitable[str]] | None = None) -> tuple[Any, int, Entry | None]:
        """Quick add, from the Memory screen's field and from Telegram's ``/remember``: the category and
        topics inferred as Remember infers them, filed by the same rule into global memory, locked,
        undoable. Returns the write's result, its history entry and the entry it landed in."""
        found = await classify(text.strip(), vocabulary=self.memory.vocabulary(self.tenant), ask=classifier)
        result, history = await self.add(text, kind=found.category, topics=found.topics)
        return result, history, await self.entries.get(entry_of(result.record))

    async def _add(self, text: str, *, kind: str, topics: Sequence[str], entry: Entry | None, origin: str, actor: str, metadata: dict[str, Any] | None, action: str, summary: str) -> tuple[Any, int]:
        known = {e.id for e in await self.entries.list()}
        scope, scope_key = self.entries.placement(entry) if entry is not None else (MemoryScope.global_, "")
        meta = {"source": origin, "topics": list(topics), LOCKED_KEY: True, **(metadata or {})}
        # A write that folds into a similar record changes that record: the history must hold it as it
        # was, or the undo would delete a note the operator had before this one.
        was = {r.id: r for r in self.memory.records(self.tenant)}
        result = await self.entries.remember(scope, scope_key, text.strip(), kind=kind, metadata=meta, origin=origin, entry=entry)
        prior = was.get(result.record.id)
        made = [e.id for e in await self.entries.list() if e.id not in known]
        history = await self._journal(actor, action, summary or text.strip()[:120], [prior] if prior else [], [result.record], {"made": made})
        return result, history

    async def rewrite(self, entry_id: str, ops: Sequence[Any], *, if_version: int, title: str | None = None, summary: str | None = None, instruction: str = "", kind: str = "fact", topics: Sequence[str] = ()) -> tuple[Entry, int | None]:
        """Apply the operations of an instruction the operator approved (see :mod:`daedalus.memory.edit`)
        to one entry, refused with :class:`EntryConflict` when the entry moved on since the preview.

        Every change is the operator's, so changed and added bullets are locked; removed ones are
        tombstoned. One history entry undoes the lot, the retitle aside (an entry's own fields are
        not journalled; the screen edits them back directly)."""
        async with self.lock:
            entry = await self.entries.get(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if entry.version != if_version:
                raise EntryConflict(f"the entry is at version {entry.version}, not {if_version}")
            if title is not None or summary is not None:
                entry = await self.entries.update(entry_id, if_version=if_version, title=title, summary=summary, by_operator=True)
            before: list[MemoryRecord] = []
            after: list[MemoryRecord] = []
            removed: list[MemoryRecord] = []
            for op in ops:
                if op.op == "add":
                    continue
                record = self._get(op.id)
                if entry_of(record) != entry_id:
                    raise KeyError(op.id)
                if op.op == "remove":
                    removed.append(record)
                    continue
                changed = await self.memory.update(self.tenant, record.id, text=op.text)
                if changed.version != record.version:
                    changed = changed.model_copy(update={"metadata": {**changed.metadata, LOCKED_KEY: True}})
                    await self.memory.restore(changed)
                    before.append(record)
                    after.append(changed)
            sources = await self.entries.source_rows(record_ids=[r.id for r in removed])
            for record in removed:
                await self.entries.tombstone(record.text, entry_slug=entry.slug, reason="deleted")
                await self.memory.delete(self.tenant, record.id)
            await self.entries.drop_sources(record_ids=[r.id for r in removed])
            was = {r.id: r for r in self.memory.records(self.tenant)}
            for op in ops:
                if op.op != "add":
                    continue
                result = await self.entries.remember(
                    *self.entries.placement(entry), op.text, kind=kind, metadata={"source": "operator", "topics": list(topics), LOCKED_KEY: True},
                    origin="operator", entry=entry,
                )
                if (prior := was.get(result.record.id)) is not None and prior.id not in {b.id for b in before}:
                    before.append(prior)
                after = [r for r in after if r.id != result.record.id] + [result.record]
            history = None
            if before or after or removed:
                history = await self._journal("operator", "instruct", instruction.strip()[:200] or entry.title, [*before, *removed], after, {"sources": sources})
            await self.entries.touched(entry_id)
            fresh = await self.entries.get(entry_id)
            assert fresh is not None
            return fresh, history

    async def import_items(self, items: Sequence[Any], *, projects: Collection[str] = ()) -> tuple[list[MemoryRecord], int | None]:
        """File approved import bullets (:class:`daedalus.memory.transfer.Item`, already scanned by the
        caller) through the one door, origin ``import``, with one history entry that takes them all
        away again, the entries they made included. An area bullet whose project exists here goes to
        that project's area; any other area becomes a free one."""
        async with self.lock:
            known = {e.id for e in await self.entries.list()}
            was = {r.id: r for r in self.memory.records(self.tenant)}
            before: dict[str, MemoryRecord] = {}
            after: dict[str, MemoryRecord] = {}
            for item in items:
                target = Target("area", project_id=item.project_id) if item.section == "area" and item.project_id in projects else target_of(item)
                entry = await self.entries.ensure(target)
                result = await self.entries.remember(
                    *self.entries.placement(entry), item.text, kind=item.category or "fact",
                    metadata={"source": "import", "topics": list(item.topics), **({"imported_date": item.date} if item.date else {})},
                    origin="import", entry=entry,
                )
                if (prior := was.get(result.record.id)) is not None:
                    before.setdefault(prior.id, prior)
                after[result.record.id] = result.record
            made = [e.id for e in await self.entries.list() if e.id not in known]
            history = await self._journal("operator", "import", f"{len(after)} imported", list(before.values()), list(after.values()), {"made": made}) if after else None
            return list(after.values()), history

    def sensitive_records(self, subjects: Collection[str]) -> list[MemoryRecord]:
        """The records touching any of ``subjects``, by the same check Remember refuses them with."""
        wanted = set(subjects)
        return [r for r in self.memory.records(self.tenant) if sensitive_subjects(r.text) & wanted]

    async def purge_sensitive(self, subjects: Collection[str]) -> tuple[int, int | None]:
        """Remove what memory holds on subjects the operator stopped allowing (claude.ai's rule: turning
        a subject off removes the existing notes on it), the operator's own notes included, with one
        history entry that brings them back. Not tombstoned: allowing the subject again may learn it anew."""
        async with self.lock:
            records = self.sensitive_records(subjects)
            history = await self._remove(records, actor="operator", action="sensitive", summary=", ".join(sorted(subjects)), tombstone=None)
            return len(records), history

    async def reset(self) -> dict[str, int]:
        """Delete every memory: records, entries, sources, tombstones, what the background pass read,
        suggestions and the history itself (a history that keeps copies would keep the memory). Not
        undoable; the screen asks for the word typed out first."""
        async with self.lock:
            records = self.memory.records(self.tenant)
            for record in records:
                await self.memory.delete(self.tenant, record.id)
            counts = {"records": len(records)}
            async with self.db.transaction() as conn:
                for table in ("memory_entries", "memory_sources", "memory_tombstones", "memory_suggestions", "memory_history"):
                    cursor = await conn.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (self.tenant,))
                    counts[table.removeprefix("memory_")] = cursor.rowcount
                await conn.execute("DELETE FROM memory_synthesis")
            return counts

    async def rename_topic(self, old: str, new: str, *, actor: str = "operator") -> int:
        """Rename a topic on every record carrying it (or fold it into an existing one). One history
        entry holds every record it touched, so one undo puts them all back."""
        source = (normalise_topics([old]) or [""])[0]
        target = normalise_topics([new])
        if not source:
            raise ValueError("no topic to rename")
        async with self.lock:
            before: list[MemoryRecord] = []
            after: list[MemoryRecord] = []
            for record in self.memory.records(self.tenant):
                topics = record_topics(record.metadata)
                if source not in topics:
                    continue
                renamed = normalise_topics([t for topic in topics for t in ([*target] if topic == source else [topic])])
                before.append(record)
                after.append(await self.memory.update(self.tenant, record.id, topics=renamed))
            if before:
                await self._journal(actor, "topic", f"{source} → {target[0] if target else '(removed)'}", before, after)
            return len(before)

    async def undo(self, history_id: int) -> list[MemoryRecord]:
        """Put the records of a history entry back as they were before it.

        Refused when a record the change produced has changed since (the operator edited the merged
        record, the agent re-saved it): putting the old ones back then would silently lose that."""
        async with self.lock:
            row = await self.db.fetchone("SELECT * FROM memory_history WHERE id = ? AND tenant_id = ?", (history_id, self.tenant))
            if row is None:
                raise KeyError(history_id)
            if row["undone_at"]:
                raise CuratorConflict("this change was already undone")
            before = [MemoryRecord.model_validate(r) for r in json.loads(row["before_json"])]
            after = [MemoryRecord.model_validate(r) for r in json.loads(row["after_json"])]
            for record in after:
                live = self.memory.get_record(self.tenant, record.id)
                if live is None or live.version != record.version:
                    raise CuratorConflict("a record has changed since; undo would lose that change")
            for record in before:
                if self.memory.get_record(self.tenant, record.id) is not None and record.id not in {r.id for r in after}:
                    raise CuratorConflict(f"a record with the id {record.id} exists again")
            extra = json.loads(row["extra_json"] or "{}") if "extra_json" in row.keys() else {}
            await self.entries.revive(extra.get("entries", []))
            dropped = [r.id for r in after if r.id not in {b.id for b in before}]
            for memory_id in dropped:
                await self.memory.delete(self.tenant, memory_id)
            await self.entries.drop_sources(record_ids=dropped)
            for record in before:
                await self.memory.restore(record)
            await self.entries.restore_sources(extra.get("sources", []))
            # Provenance a change added to records it left otherwise as they were (a chat that taught a
            # note memory already had): undone, that chat no longer backs it, or deleting the chat later
            # would keep a note only the undone change attributed to it.
            for source in extra.get("added_sources", []):
                await self.db.execute(
                    "DELETE FROM memory_sources WHERE tenant_id = ? AND record_id = ? AND origin = ? AND session_id = ? AND from_seq = ?",
                    (self.tenant, source["record_id"], source["origin"], source.get("session_id") or "", int(source.get("from_seq") or 0)),
                )
            # An undone removal brings the notes back, and the tombstone that stopped them being learned
            # again would otherwise outlive the operator changing their mind.
            await self.entries.untombstone(r.text for r in before)
            for entry_id in extra.get("made", []):
                with contextlib.suppress(EntryNotEmpty):
                    await self.entries.delete(entry_id)
            for entry_id in {entry_of(r) for r in (*before, *after)} - {""}:
                if await self.entries.get(entry_id) is not None:
                    await self.entries.touched(entry_id)
            await self.db.execute("UPDATE memory_history SET undone_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), history_id))
            return before

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM memory_history WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (self.tenant, limit))
        out = []
        for row in rows:
            before = json.loads(row["before_json"])
            after = json.loads(row["after_json"])
            out.append({
                "id": row["id"], "at": row["at"], "actor": row["actor"], "action": row["action"], "summary": row["summary"],
                "undone_at": row["undone_at"],
                "before": [{"id": r["id"], "text": r["text"], "kind": r["kind"], "topics": record_topics(r.get("metadata"))} for r in before],
                "after": [{"id": r["id"], "text": r["text"], "kind": r["kind"], "topics": record_topics(r.get("metadata"))} for r in after],
            })
        return out

    async def suggestions(self) -> list[dict[str, Any]]:
        """The open suggestions whose records are still as they were; the others are closed on the way."""
        rows = await self.db.fetchall("SELECT * FROM memory_suggestions WHERE tenant_id = ? AND status = 'open' ORDER BY score DESC, id", (self.tenant,))
        titles = {e.id: e.title for e in await self.entries.list()}
        out = []
        for row in rows:
            ids = json.loads(row["memory_ids"])
            if not await self._still_holds(row):
                await self.db.execute("UPDATE memory_suggestions SET status = 'stale', decided_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), row["id"]))
                continue
            detail = _detail(row)
            found = {"id": row["id"], "kind": row["kind"], "memory_ids": ids, "keep_id": row["keep_id"], "text": row["proposed_text"], "score": row["score"], "reason": row["reason"], "created_at": row["created_at"]}
            if row["kind"] in LEARNED_KINDS:
                found.update({"action": detail.get("action") or ("add" if row["kind"] == "addition" else "update"), "entry_id": detail.get("entry_id") or None,
                              "entry_title": titles.get(str(detail.get("entry_id") or "")) or detail.get("entry_title"), "session_id": detail.get("session_id"),
                              "why": detail.get("why") or "", "tokens": list(detail.get("tokens") or [])})
            out.append(found)
        return out

    async def _still_holds(self, row: Any) -> bool:
        """Whether a suggestion still applies to memory as it is: its records exist and read as they did.
        A held-back addition has no record; its entry is made again on apply when it is gone."""
        records = [self.memory.get_record(self.tenant, i) for i in json.loads(row["memory_ids"])]
        if any(r is None for r in records):
            return False
        live = [r for r in records if r is not None]
        if row["kind"] in LEARNED_KINDS:
            return _learned_print(row["kind"], live, row["proposed_text"], _detail(row)) == row["fingerprint"]
        return _fingerprint(row["kind"], live) == row["fingerprint"]

    async def apply(self, suggestion_id: int, *, text: str | None = None) -> tuple[MemoryRecord, int]:
        row = await self.db.fetchone("SELECT * FROM memory_suggestions WHERE id = ? AND tenant_id = ?", (suggestion_id, self.tenant))
        if row is None:
            raise KeyError(suggestion_id)
        if row["status"] != "open":
            raise CuratorConflict("this suggestion is no longer open")
        ids = json.loads(row["memory_ids"])
        if not await self._still_holds(row):
            raise CuratorConflict("the records changed since the suggestion was made")
        if row["kind"] in LEARNED_KINDS:
            record, history = await self._apply_learned(row, text=text)
            await self.db.execute("UPDATE memory_suggestions SET status = 'applied', decided_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), suggestion_id))
            return record, history
        async with self.lock:
            merged, history = await self._merge(ids, actor="operator", action="merge" if row["kind"] == "duplicate" else "replace", keep_id=row["keep_id"], text=text or row["proposed_text"], summary=row["reason"])
        await self.db.execute("UPDATE memory_suggestions SET status = 'applied', decided_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), suggestion_id))
        return merged, history

    async def _apply_learned(self, row: Any, *, text: str | None) -> tuple[MemoryRecord, int]:
        """The operator's yes to a learned suggestion. A revision changes or removes the bullet as the
        operator's own edit (it stays locked: they approved the words); an addition files the held-back
        bullet in its entry with the chat it came from as its source. Either is one undoable change."""
        detail = _detail(row)
        body = (text or row["proposed_text"]).strip()
        if row["kind"] == "revision":
            record = self._get(json.loads(row["memory_ids"])[0])
            if detail.get("action") == "retire":
                async with self.lock:
                    history = await self._remove([record], actor="operator", action="delete", summary=row["reason"] or record.text[:120], tombstone="deleted")
                assert history is not None
                return record, history
            changed, history = await self.edit(record.id, text=body)
            if history is None:
                raise CuratorConflict("the suggestion would not change the bullet")
            return changed, history
        entry = await self._learned_entry(detail)
        async with self.lock:
            was = {r.id: r for r in self.memory.records(self.tenant)}
            scope, scope_key = self.entries.placement(entry)
            seqs = [int(s) for s in detail.get("seqs") or [] if isinstance(s, int | str) and str(s).isdigit()]
            result = await self.entries.remember(
                scope, scope_key, body, kind=str(detail.get("kind") or "fact"), metadata={"source": "synthesis", "topics": list(detail.get("topics") or [])},
                origin="synthesis", session_id=str(detail.get("session_id") or ""), entry=entry,
                from_seq=min(seqs) if seqs else 0, to_seq=max(seqs) if seqs else 0,
            )
            prior = was.get(result.record.id)
            history = await self._journal("operator", "add", body[:120], [prior] if prior else [], [result.record])
            return result.record, history

    async def _learned_entry(self, detail: dict[str, Any]) -> Entry:
        """The entry a held-back addition goes to: the one it named, or the same page made again (the
        pass makes no page for a bullet it only holds back, and removes one it left empty)."""
        found = await self.entries.get(str(detail.get("entry_id") or ""))
        if found is not None:
            return found
        section, slug = str(detail.get("section") or "topic"), str(detail.get("slug") or "")
        if section == "area" and detail.get("project_id"):
            try:
                return await self.entries.ensure(Target("area", project_id=str(detail["project_id"])))
            except ValueError:
                pass  # the project is gone: a free area of the same name
        if section not in ("topic", "area") or not slug:
            raise CuratorConflict("the entry this was meant for is gone")
        return await self.entries.ensure(Target(section, slug, str(detail.get("entry_title") or "") or slug))

    async def dismiss(self, suggestion_id: int) -> bool:
        await self.db.execute("UPDATE memory_suggestions SET status = 'dismissed', decided_at = ? WHERE id = ? AND tenant_id = ? AND status = 'open'", (datetime.now(UTC).isoformat(), suggestion_id, self.tenant))
        row = await self.db.fetchone("SELECT status FROM memory_suggestions WHERE id = ?", (suggestion_id,))
        return bool(row and row["status"] == "dismissed")

    def _state(self) -> str:
        """What the records are now, in one hash: the pass skips a memory that has not changed."""
        records = self.memory.records(self.tenant)
        return hashlib.sha1("|".join(sorted(f"{r.id}:{r.version}" for r in records)).encode()).hexdigest()

    async def consolidate(self, *, force: bool = False) -> dict[str, int]:
        """One pass: apply what is sure (when allowed), file the rest as suggestions."""
        config = self.config()
        state = self._state()
        if not force and await self.db.kv_get(f"memory_consolidated:{self.tenant}", "") == state:
            return {"merged": 0, "suggested": 0}
        records = self.memory.records(self.tenant)
        vectors: dict[str, list[float]] = {}
        if self.embed is not None:
            try:
                vectors = await self.embed(records)
            except Exception:  # noqa: BLE001 — without vectors the comparison is lexical, as it is when the index is off
                logger.warning("memory embeddings failed; comparing words only", exc_info=True)
        findings = find(records, vectors)
        known = {row["fingerprint"] for row in await self.db.fetchall("SELECT fingerprint FROM memory_suggestions WHERE tenant_id = ?", (self.tenant,))}
        merged = suggested = 0
        for finding in findings:
            fingerprint = _fingerprint(finding.kind, finding.records)
            if fingerprint in known:
                continue
            if finding.sure and config.auto_merge:
                async with self.lock:
                    try:
                        await self._merge([r.id for r in finding.records], actor="auto", action="merge" if finding.kind == "duplicate" else "replace", keep_id=finding.keep.id, text=finding.text, summary=finding.reason)
                    except KeyError:
                        continue  # forgotten meanwhile: nothing left to merge
                merged += 1
                continue
            if suggested >= MAX_NEW_SUGGESTIONS:
                continue
            await self.db.execute(
                "INSERT OR IGNORE INTO memory_suggestions(tenant_id, kind, memory_ids, fingerprint, keep_id, proposed_text, score, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (self.tenant, finding.kind, json.dumps([r.id for r in finding.records]), fingerprint, finding.keep.id, finding.text,
                 round(finding.score, 4), finding.reason, datetime.now(UTC).isoformat()),
            )
            suggested += 1
        await self.db.kv_set(f"memory_consolidated:{self.tenant}", self._state())
        if merged or suggested:
            logger.info("memory consolidation: %d merged, %d suggested", merged, suggested)
        if suggested and self.notify is not None:
            await self.notify(suggested)
        return {"merged": merged, "suggested": suggested}

    async def loop(self) -> None:
        while True:
            try:
                if self.config().consolidate:
                    await self.consolidate()
            except Exception:  # noqa: BLE001 — a failed pass costs a log line; the next one tries again
                logger.exception("memory consolidation failed")
            await asyncio.sleep(CHECK_EVERY_SECONDS)


def _detail(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["detail"] or "{}") if "detail" in row.keys() else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["LEARNED_KINDS", "CuratorConflict", "Finding", "MemoryCurator", "find"]
