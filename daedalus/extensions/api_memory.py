"""The Memory screen's routes: the records with their categories and topics, the entries they are
filed in, edits, merges, the background tidying's suggestions, and the history every change can be
undone from."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from protocore.contracts.memory import MemoryRecord, MemoryScope
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.memory import curator
from daedalus.host.session_runner import TENANT
from daedalus.memory.curator import CuratorConflict
from daedalus.memory.entries import SECTIONS, UnknownProject, entry_of
from daedalus.memory.taxonomy import CATEGORIES, classify, normalise_category, record_topics

if TYPE_CHECKING:
    from daedalus.app import Application


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    category: str = ""
    """Empty: inferred as Remember infers it."""
    topics: list[str] | None = None
    scope: Literal["global", "project"] = "global"
    scope_key: str = ""
    """The project's id for a project note."""


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
        "entry": entry_of(record) or None,
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

    async def project_names() -> dict[str, str]:
        return {row["id"]: row["name"] for row in await app.db.fetchall("SELECT id, name FROM projects")}

    @api.get("/api/memory")
    async def memory_list(scope: str = "", scope_key: str = "", source_session: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every memory record (or one scope's, or the ones a chat taught), the topics and categories
        in use, the names of the projects the project records belong to, and how many suggestions wait."""
        try:
            wanted = MemoryScope(scope) if scope else None
        except ValueError as exc:
            raise HTTPException(422, f"unknown scope {scope!r}") from exc
        memory = manager().memory
        records = memory.records(TENANT, scope=wanted, scope_key=scope_key or None)
        if source_session:
            taught = {row["record_id"] for row in await app.db.fetchall("SELECT record_id FROM memory_sources WHERE tenant_id = ? AND session_id = ?", (TENANT, source_session))}
            records = [r for r in records if r.id in taught]
        names = await project_names()
        buckets: dict[str, dict[str, Any]] = {}
        everything = memory.records(TENANT)
        for rec in everything:
            key = f"{rec.scope.value}:{rec.scope_key}"
            b = buckets.setdefault(key, {"scope": rec.scope.value, "scope_key": rec.scope_key, "title": names.get(rec.scope_key) if rec.scope is MemoryScope.project else None, "count": 0})
            b["count"] += 1
        topics = memory.vocabulary(TENANT)
        categories = Counter(rec.kind for rec in everything)
        waiting = await app.db.fetchone("SELECT count(*) n FROM memory_suggestions WHERE tenant_id = ? AND status = 'open'", (TENANT,))
        offered = {row["id"]: row["name"] for row in await app.db.fetchall(
            "SELECT id, name FROM projects WHERE system = ''"
            " AND NOT COALESCE(CASE WHEN json_valid(settings) THEN json_extract(settings, '$.ephemeral') END, 0) ORDER BY name COLLATE NOCASE"
        )}
        return {
            "records": [memory_view(r) for r in records],
            "buckets": sorted(buckets.values(), key=lambda b: (b["scope"] != "global", -b["count"])),
            # The projects a note can be filed under, and the names of any a record still names.
            "projects": {**offered, **{k: names[k] for k in {r.scope_key for r in everything if r.scope is MemoryScope.project} if k in names}},
            "topics": [{"topic": t, "count": n} for t, n in sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))],
            "categories": {c: categories.get(c, 0) for c in CATEGORIES},
            "suggestions": int(waiting["n"]) if waiting else 0,
        }

    @api.post("/api/memory")
    async def memory_add(body: MemoryBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's own note. Never refused for a sensitive subject: the setting guards what the
        agent keeps on its own, and the operator writing it here has decided. Marked locked, so the
        background pass may suggest a change to it but never makes one."""
        scope = MemoryScope(body.scope)
        if scope is MemoryScope.project and not body.scope_key:
            raise HTTPException(422, "scope_key (the project's id) is required for a project note")
        memory = manager().memory
        category = _category(body.category)
        topics = body.topics
        if category is None or topics is None:
            found = await classify(body.text.strip(), vocabulary=memory.vocabulary(TENANT), ask=manager().memory_classifier())
            category = category or found.category
            topics = topics if topics is not None else found.topics
        try:
            result = await manager().memory_entries.remember(
                scope, body.scope_key if scope is MemoryScope.project else "", body.text.strip(),
                kind=category, metadata={"source": "operator", "topics": topics, "locked": True}, origin="operator",
            )
        except UnknownProject as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"decision": result.decision.value, "record": memory_view(result.record)}

    @api.get("/api/memory/entries")
    async def memory_entries(section: str = "", project_id: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The entries memory is read in, section by section, each with how many records it holds."""
        if section and section not in SECTIONS:
            raise HTTPException(422, f"unknown section {section!r}; one of {', '.join(SECTIONS)}")
        entries = manager().memory_entries
        counts = entries.counts()
        found = await entries.list(section=section or None, project_id=project_id or None)
        every = found if not (section or project_id) else await entries.list()
        known = {e.id for e in every}
        return {
            "entries": [e.view(count=counts.get(e.id, 0)) for e in found],
            "sections": {name: sum(1 for e in every if e.section == name) for name in SECTIONS},
            "records": sum(counts.values()),
            "unfiled": sum(n for key, n in counts.items() if key not in known),
        }

    @api.get("/api/memory/entries/{entry_id}")
    async def memory_entry(entry_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One entry with its records, newest first, and where each record was learned."""
        entries = manager().memory_entries
        entry = await entries.get(entry_id)
        if entry is None:
            raise HTTPException(404, "no such entry")
        records = entries.records_of(entry_id)
        sources = await entries.sources(r.id for r in records)
        return {"entry": entry.view(count=len(records)), "records": [{**memory_view(r), "sources": sources.get(r.id, [])} for r in records]}

    @api.patch("/api/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: MemoryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            record, history = await curator(app).edit(memory_id, text=body.text, category=_category(body.category), topics=body.topics)
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc
        return {**memory_view(record), "history_id": history}

    async def forget(memory_ids: list[str]) -> int:
        memory = manager().memory
        touched = {entry_of(r) for i in memory_ids if (r := memory.get_record(TENANT, i)) is not None} - {""}
        deleted = 0
        for memory_id in memory_ids:
            deleted += int(await memory.delete(TENANT, memory_id))
        for entry_id in touched:
            await manager().memory_entries.touched(entry_id)
        return deleted

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": bool(await forget([memory_id]))}

    @api.post("/api/memory/delete")
    async def memory_delete_many(body: MemoryIds, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"deleted": await forget(body.ids)}

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
