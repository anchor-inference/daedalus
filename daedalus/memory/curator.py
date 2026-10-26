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
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from protocore.contracts.memory import MemoryRecord, MemoryScope

from daedalus.memory.entries import MemoryEntries, entry_of
from daedalus.memory.taxonomy import normalise_topics, record_topics

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


def _tokens(text: str) -> set[str]:
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
    tokens = {r.id: _tokens(r.text) for r in pool}
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

    async def _journal(self, actor: str, action: str, summary: str, before: Sequence[MemoryRecord], after: Sequence[MemoryRecord]) -> int:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO memory_history(tenant_id, at, actor, action, summary, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.tenant, datetime.now(UTC).isoformat(), actor, action, summary[:500],
                 json.dumps([_record_json(r) for r in before], ensure_ascii=False), json.dumps([_record_json(r) for r in after], ensure_ascii=False)),
            )
            return int(cursor.lastrowid or 0)

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

    async def edit(self, memory_id: str, *, text: str | None = None, category: str | None = None, topics: Sequence[str] | None = None, actor: str = "operator") -> tuple[MemoryRecord, int | None]:
        """Change a record's text, category or topics, with a history entry when anything changed."""
        async with self.lock:
            before = self._get(memory_id)
            after = await self.memory.update(self.tenant, memory_id, text=text, kind=category, topics=list(topics) if topics is not None else None)
            if after.version == before.version:
                return after, None
            what = [name for name, changed in (("text", after.text != before.text), ("category", after.kind != before.kind), ("topics", record_topics(after.metadata) != record_topics(before.metadata))) if changed]
            action = "recategorise" if what and "text" not in what else "edit"
            return after, await self._journal(actor, action, ", ".join(what), [before], [after])

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
            for record in after:
                if record.id not in {r.id for r in before}:
                    await self.memory.delete(self.tenant, record.id)
            for record in before:
                await self.memory.restore(record)
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
        out = []
        for row in rows:
            ids = json.loads(row["memory_ids"])
            records = [self.memory.get_record(self.tenant, i) for i in ids]
            if any(r is None for r in records) or _fingerprint(row["kind"], [r for r in records if r is not None]) != row["fingerprint"]:
                await self.db.execute("UPDATE memory_suggestions SET status = 'stale', decided_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), row["id"]))
                continue
            out.append({"id": row["id"], "kind": row["kind"], "memory_ids": ids, "keep_id": row["keep_id"], "text": row["proposed_text"], "score": row["score"], "reason": row["reason"], "created_at": row["created_at"]})
        return out

    async def apply(self, suggestion_id: int, *, text: str | None = None) -> tuple[MemoryRecord, int]:
        row = await self.db.fetchone("SELECT * FROM memory_suggestions WHERE id = ? AND tenant_id = ?", (suggestion_id, self.tenant))
        if row is None:
            raise KeyError(suggestion_id)
        if row["status"] != "open":
            raise CuratorConflict("this suggestion is no longer open")
        ids = json.loads(row["memory_ids"])
        records = [self.memory.get_record(self.tenant, i) for i in ids]
        if any(r is None for r in records) or _fingerprint(row["kind"], [r for r in records if r is not None]) != row["fingerprint"]:
            raise CuratorConflict("the records changed since the suggestion was made")
        async with self.lock:
            merged, history = await self._merge(ids, actor="operator", action="merge" if row["kind"] == "duplicate" else "replace", keep_id=row["keep_id"], text=text or row["proposed_text"], summary=row["reason"])
        await self.db.execute("UPDATE memory_suggestions SET status = 'applied', decided_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), suggestion_id))
        return merged, history

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


__all__ = ["CuratorConflict", "Finding", "MemoryCurator", "find"]
