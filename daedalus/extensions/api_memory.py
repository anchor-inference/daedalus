"""The Memory screen's routes: the records with their categories and topics, edits, merges, the
background tidying's suggestions, and the history every change can be undone from."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from protocore.contracts.memory import MemoryRecord, MemoryScope
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.memory import curator
from daedalus.host.session_runner import TENANT
from daedalus.memory.curator import CuratorConflict
from daedalus.memory.taxonomy import CATEGORIES, classify, normalise_category, record_topics

if TYPE_CHECKING:
    from daedalus.app import Application


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    category: str = ""
    """Empty: inferred as Remember infers it."""
    topics: list[str] | None = None
    scope: str = "global"
    scope_key: str = ""


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    category: str | None = None
    topics: list[str] | None = None


class MemoryIds(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=2, max_length=50)
    text: str | None = None
    category: str | None = None
    topics: list[str] | None = None
    keep_id: str | None = None


class TopicRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=64)
    to: str = Field(default="", max_length=64)
    """Empty takes the topic off every record carrying it."""


class ApplyBody(BaseModel):
    text: str | None = None


def memory_view(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "scope": record.scope.value,
        "scope_key": record.scope_key,
        "category": record.kind,
        "topics": record_topics(record.metadata),
        "text": record.text,
        "salience": record.salience,
        "version": record.version,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "last_accessed_at": record.last_accessed_at.isoformat() if record.last_accessed_at else None,
    }


def _category(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    category = normalise_category(value)
    if category is None:
        raise HTTPException(422, f"unknown category {value!r}; one of {', '.join(CATEGORIES)}")
    return category


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def manager() -> Any:
        if app.manager is None:
            raise HTTPException(503, "the agent is still starting")
        return app.manager

    @api.get("/api/memory")
    async def memory_list(scope: str = "", scope_key: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every memory record (or one scope's), the topics and categories in use, the session titles
        the session-scoped records belong to, and how many suggestions wait."""
        try:
            wanted = MemoryScope(scope) if scope else None
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {scope!r}") from exc
        memory = manager().memory
        records = memory.records(TENANT, scope=wanted, scope_key=scope_key or None)
        titles = {row["id"]: row["title"] for row in await app.db.fetchall("SELECT id, title FROM sessions")}
        buckets: dict[str, dict[str, Any]] = {}
        everything = memory.records(TENANT)
        for rec in everything:
            key = f"{rec.scope.value}:{rec.scope_key}"
            b = buckets.setdefault(key, {"scope": rec.scope.value, "scope_key": rec.scope_key, "title": titles.get(rec.scope_key) if rec.scope is MemoryScope.session else None, "count": 0})
            b["count"] += 1
        topics = memory.vocabulary(TENANT)
        categories = Counter(rec.kind for rec in everything)
        waiting = await app.db.fetchone("SELECT count(*) n FROM memory_suggestions WHERE tenant_id = ? AND status = 'open'", (TENANT,))
        return {
            "records": [memory_view(r) for r in records],
            "buckets": sorted(buckets.values(), key=lambda b: (b["scope"] != "global", -b["count"])),
            "sessions": titles,
            "topics": [{"topic": t, "count": n} for t, n in sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))],
            "categories": {c: categories.get(c, 0) for c in CATEGORIES},
            "suggestions": int(waiting["n"]) if waiting else 0,
        }

    @api.post("/api/memory")
    async def memory_add(body: MemoryBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's own note. Never refused for a sensitive subject: the setting guards what the
        agent keeps on its own, and the operator writing it here has decided."""
        try:
            scope = MemoryScope(body.scope)
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {body.scope!r}") from exc
        if scope is not MemoryScope.global_ and not body.scope_key:
            raise HTTPException(422, "scope_key is required for a non-global scope")
        memory = manager().memory
        category = _category(body.category)
        topics = body.topics
        if category is None or topics is None:
            found = await classify(body.text.strip(), vocabulary=memory.vocabulary(TENANT), ask=manager().memory_classifier())
            category = category or found.category
            topics = topics if topics is not None else found.topics
        try:
            result = await memory.write(TENANT, scope, body.scope_key, body.text.strip(), kind=category, metadata={"source": "operator", "topics": topics})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"decision": result.decision.value, "record": memory_view(result.record)}

    @api.patch("/api/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: MemoryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            record, history = await curator(app).edit(memory_id, text=body.text, category=_category(body.category), topics=body.topics)
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc
        return {**memory_view(record), "history_id": history}

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": await manager().memory.delete(TENANT, memory_id)}

    @api.post("/api/memory/delete")
    async def memory_delete_many(body: MemoryIds, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        deleted = 0
        for memory_id in body.ids:
            deleted += int(await manager().memory.delete(TENANT, memory_id))
        return {"deleted": deleted}

    @api.post("/api/memory/merge")
    async def memory_merge(body: MergeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            record, history = await curator(app).merge(body.ids, text=body.text, category=_category(body.category), topics=body.topics, keep_id=body.keep_id)
        except KeyError as exc:
            raise HTTPException(404, f"no such memory {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"record": memory_view(record), "history_id": history}

    @api.post("/api/memory/topics/rename")
    async def memory_topic_rename(body: TopicRename, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"changed": await curator(app).rename_topic(body.topic, body.to)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/memory/suggestions")
    async def memory_suggestions(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await curator(app).suggestions()
        memory = manager().memory
        records = {i: memory_view(r) for s in found for i in s["memory_ids"] if (r := memory.get_record(TENANT, i)) is not None}
        return {"suggestions": found, "records": records}

    @api.post("/api/memory/suggestions/{suggestion_id}/apply")
    async def memory_suggestion_apply(suggestion_id: int, body: ApplyBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            record, history = await curator(app).apply(suggestion_id, text=body.text)
        except KeyError as exc:
            raise HTTPException(404, "no such suggestion") from exc
        except CuratorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"record": memory_view(record), "history_id": history}

    @api.post("/api/memory/suggestions/{suggestion_id}/dismiss")
    async def memory_suggestion_dismiss(suggestion_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"dismissed": await curator(app).dismiss(suggestion_id)}

    @api.get("/api/memory/history")
    async def memory_history(limit: int = 50, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"entries": await curator(app).history(max(1, min(limit, 200)))}

    @api.post("/api/memory/history/{history_id}/undo")
    async def memory_undo(history_id: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            restored = await curator(app).undo(history_id)
        except KeyError as exc:
            raise HTTPException(404, "no such change") from exc
        except CuratorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"restored": [memory_view(r) for r in restored]}

    @api.post("/api/memory/consolidate")
    async def memory_consolidate(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Tidy now rather than at the next background pass, even when nothing changed since the last one."""
        return await curator(app).consolidate(force=True)


__all__ = ["memory_view", "register"]
