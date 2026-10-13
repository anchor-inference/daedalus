"""Durable memory and workspace stores.

The core ships full behavioural implementations of ``IMemory`` and ``IWorkspace``
that live in process memory. For a single-user bot that behaviour is exactly
right; what is missing is durability. These subclasses load the records from
SQLite at start and write every mutation back, so the ranking, dedup and quota
semantics stay the core's own.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.memory import MemoryHit, MemoryRecord, MemoryScope, MemoryWriteDecision, MemoryWriteResult
from protocore.contracts.workspace import WorkspaceUnit, WorkspaceWriteOutcome
from protocore.tests_support.adapters import InMemoryMemory, InMemoryWorkspace

from daedalus.memory.taxonomy import normalise_topics, record_topics
from daedalus.stores.database import Database

_WORD = re.compile(r"[\w][\w'-]*", re.UNICODE)
_MEMORY_ID = re.compile(r"^mem-(\d+)$")


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if len(w) > 1]


class PersistentMemory(InMemoryMemory):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

    async def load(self) -> None:
        rows = await self._db.fetchall("SELECT tenant_id, id, record FROM memory_records")
        for row in rows:
            record = MemoryRecord.model_validate(json.loads(row["record"]))
            self._store[(row["tenant_id"], row["id"])] = record
        # The next id continues after the highest one, not after the count: with a record forgotten,
        # the count pointed at an id still in use and the next write replaced that record silently
        # (52 records numbered up to 63 meant the 53rd write overwrote mem-000053).
        self._counter = max((int(m.group(1)) for row in rows if (m := _MEMORY_ID.match(row["id"]))), default=0)

    async def _persist(self, record: MemoryRecord) -> None:
        await self._db.execute(
            "INSERT INTO memory_records(tenant_id, id, record) VALUES (?, ?, ?)"
            " ON CONFLICT(tenant_id, id) DO UPDATE SET record = excluded.record",
            (record.tenant_id, record.id, record.model_dump_json()),
        )

    async def write(self, tenant_id: str, scope: MemoryScope, scope_key: str, text: str, **kwargs: Any) -> MemoryWriteResult:  # type: ignore[override]
        metadata = dict(kwargs.get("metadata") or {})
        if "topics" in metadata:
            metadata["topics"] = normalise_topics(metadata["topics"])
            kwargs["metadata"] = metadata
        before = dict(self._store)
        result = await super().write(tenant_id, scope, scope_key, text, **kwargs)
        previous = before.get((tenant_id, result.record.id))
        if result.decision is MemoryWriteDecision.merged and previous is not None:
            # The reference store merges metadata by replacing keys, so a merge dropped the topics
            # the record had; a fact said again is still about everything it was about before.
            topics = normalise_topics([*record_topics(previous.metadata), *record_topics(result.record.metadata)])
            merged = result.record.model_copy(update={"metadata": {**result.record.metadata, "topics": topics}})
            self._store[(tenant_id, merged.id)] = merged
            result = result.model_copy(update={"record": merged})
        await self._persist(result.record)
        return result

    async def delete(self, tenant_id: str, memory_id: str, **kwargs: Any) -> bool:  # type: ignore[override]
        deleted = await super().delete(tenant_id, memory_id, **kwargs)
        if deleted:
            await self._db.execute(
                "DELETE FROM memory_records WHERE tenant_id = ? AND id = ?", (tenant_id, memory_id)
            )
        return deleted

    def records(self, tenant_id: str, *, scope: MemoryScope | None = None, scope_key: str | None = None) -> list[MemoryRecord]:
        """Every record of the tenant, newest first; narrowed to one scope (and one bucket) when asked."""
        out = [
            rec
            for (t, _), rec in self._store.items()
            if t == tenant_id and (scope is None or rec.scope is scope) and (scope_key is None or rec.scope_key == scope_key)
        ]
        out.sort(key=lambda r: (r.created_at.isoformat() if r.created_at else "", r.id), reverse=True)
        return out

    def get_record(self, tenant_id: str, memory_id: str) -> MemoryRecord | None:
        return self._store.get((tenant_id, memory_id))

    def vocabulary(self, tenant_id: str) -> Counter[str]:
        """The topics in use and how many records carry each: what a new record's topics reuse."""
        counts: Counter[str] = Counter()
        for (t, _), rec in self._store.items():
            if t == tenant_id:
                counts.update(record_topics(rec.metadata))
        return counts

    async def update(
        self,
        tenant_id: str,
        memory_id: str,
        *,
        text: str | None = None,
        kind: str | None = None,
        topics: list[str] | None = None,
    ) -> MemoryRecord:
        """Rewrite a record in place (the operator's edit): its id, scope and history stay, the version steps."""
        record = self._store.get((tenant_id, memory_id))
        if record is None:
            raise KeyError(memory_id)
        changes: dict[str, Any] = {}
        if text is not None and text.strip() and text.strip() != record.text:
            changes["text"] = text.strip()
        if kind is not None and kind.strip() and kind.strip() != record.kind:
            changes["kind"] = kind.strip()
        if topics is not None and normalise_topics(topics) != record_topics(record.metadata):
            changes["metadata"] = {**record.metadata, "topics": normalise_topics(topics)}
        if not changes:
            return record
        updated = record.model_copy(update={**changes, "version": record.version + 1, "updated_at": datetime.now(UTC)})
        self._store[(tenant_id, memory_id)] = updated
        await self._persist(updated)
        return updated

    async def restore(self, record: MemoryRecord) -> None:
        """Put a record back exactly as it was, id and all: what an undone merge or edit returns to."""
        self._store[(record.tenant_id, record.id)] = record
        await self._persist(record)

    async def recall(self, tenant_id: str, query: str, **kwargs: Any):  # type: ignore[override]
        return await self.search(tenant_id, query, **{"limit": 10, **kwargs})

    async def search(  # type: ignore[override]
        self,
        tenant_id: str,
        query: str,
        *,
        scopes: Sequence[MemoryScope] | None = None,
        scope_keys: dict[MemoryScope, str] | None = None,
        kinds: Sequence[str] | None = None,
        topics: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryHit]:
        """Ranked records, as the core's search, narrowed to the records carrying any of ``topics``."""
        hits = self._rank(tenant_id, query, scopes, scope_keys, kinds, limit, topics=topics)
        for hit in hits:
            await self._persist(hit.record)
        return hits

    def _rank(self, tenant_id: str, query: str, scopes: Any, scope_keys: Any, kinds: Any, limit: int, *, topics: Sequence[str] | None = None) -> list[MemoryHit]:  # type: ignore[override]
        """Ranked recall: BM25 over the record texts, a bonus for the phrase itself, a tie-break on recency
        and on how often the record was useful before. The reference store scores by token overlap alone,
        which ranks a long note that shares two common words above a short one that names the thing."""
        from protocore.contracts.memory import (  # Lazy: keeps the store's import list to the contract it implements
            DEFAULT_RECALL_SCOPES,
            MemoryScope,
        )

        eff_scopes = tuple(scopes) if scopes else DEFAULT_RECALL_SCOPES
        keys = scope_keys or {}
        kind_set = set(kinds) if kinds else None
        topic_set = set(normalise_topics(list(topics))) if topics else None
        pool: list[MemoryRecord] = []
        for (t, _), rec in self._store.items():
            if t != tenant_id or rec.scope not in eff_scopes or (kind_set is not None and rec.kind not in kind_set):
                continue
            if topic_set is not None and not topic_set & set(record_topics(rec.metadata)):
                continue
            if rec.scope is not MemoryScope.global_ and keys.get(rec.scope) != rec.scope_key:
                continue
            pool.append(rec)
        q_tokens = _tokens(query)
        if not q_tokens:
            pool.sort(key=lambda r: r.updated_at.timestamp(), reverse=True)
            scored = [(r.updated_at.timestamp(), r) for r in pool]
        else:
            docs = [_tokens(r.text) for r in pool]
            avg = (sum(len(d) for d in docs) / len(docs)) if docs else 1.0
            n = len(docs)
            df = {tok: sum(1 for d in docs if tok in d) for tok in set(q_tokens)}
            phrase = " ".join(q_tokens)
            scored = []
            for rec, doc in zip(pool, docs, strict=True):
                if not doc:
                    continue
                score = 0.0
                for tok in q_tokens:
                    tf = doc.count(tok)
                    if not tf:
                        continue
                    idf = math.log(1 + (n - df[tok] + 0.5) / (df[tok] + 0.5))
                    score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(doc) / avg))
                if score <= 0:
                    continue
                if phrase in " ".join(doc):
                    score *= 1.5
                age_days = max(0.0, (datetime.now(UTC) - rec.updated_at).total_seconds() / 86400)
                score *= 1 + 0.15 * min(rec.access_count, 10) / 10 + (0.1 if age_days < 7 else 0.0)
                scored.append((score, rec))
            scored.sort(key=lambda pair: (pair[0], pair[1].updated_at.timestamp()), reverse=True)
        now = datetime.now(UTC)
        hits: list[MemoryHit] = []
        for score, rec in scored[:limit]:
            reinforced = rec.model_copy(update={"access_count": rec.access_count + 1, "last_accessed_at": now})
            self._store[(tenant_id, rec.id)] = reinforced
            hits.append(MemoryHit(record=reinforced, score=float(score)))
        return hits


class PersistentWorkspace(InMemoryWorkspace):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

    @staticmethod
    def _row_key(key: tuple[str, str, str, str]) -> str:
        return json.dumps(list(key))

    async def load(self) -> None:
        rows = await self._db.fetchall("SELECT key, unit FROM workspace_units")
        for row in rows:
            key = tuple(json.loads(row["key"]))
            self._store[key] = WorkspaceUnit.model_validate(json.loads(row["unit"]))  # type: ignore[index]
        self._counter = len(rows)

    async def _sync_all(self) -> None:
        """Rewrite the table from the in-memory state (units are few and small)."""
        rows = [(self._row_key(k), v.model_dump_json()) for k, v in self._store.items()]
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM workspace_units")
            await conn.executemany("INSERT INTO workspace_units(key, unit) VALUES (?, ?)", rows)

    async def write(self, *args: Any, **kwargs: Any) -> WorkspaceWriteOutcome:  # type: ignore[override]
        outcome = await super().write(*args, **kwargs)
        await self._sync_all()
        return outcome

    async def delete(self, *args: Any, **kwargs: Any) -> bool:  # type: ignore[override]
        deleted = await super().delete(*args, **kwargs)
        if deleted:
            await self._sync_all()
        return deleted

    async def clear_scope(self, *args: Any, **kwargs: Any) -> int:  # type: ignore[override]
        removed = await super().clear_scope(*args, **kwargs)
        await self._sync_all()
        return removed


__all__ = ["PersistentMemory", "PersistentWorkspace"]


def _unused(_: Sequence[Any]) -> None:  # keeps Sequence imported for type readers
    return None
