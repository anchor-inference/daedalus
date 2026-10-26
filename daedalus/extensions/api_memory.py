"""The Memory screen's routes: the records with their categories and topics, the entries they are
filed in, edits, merges, the background tidying's suggestions, and the history every change can be
undone from."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from protocore.contracts.memory import MemoryRecord, MemoryScope
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.memory import curator, synthesizer
from daedalus.host.session_runner import TENANT
from daedalus.memory import edit, transfer
from daedalus.memory.curator import CuratorConflict
from daedalus.memory.entries import (
    SECTIONS,
    EntryConflict,
    UnknownProject,
    entry_of,
    fingerprint,
    is_archived,
    is_locked,
)
from daedalus.memory.taxonomy import CATEGORIES, classify, normalise_category, record_topics
from daedalus.security.untrusted import injection_signs

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


logger = logging.getLogger(__name__)


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    category: str | None = None
    topics: list[str] | None = None
    version: int | None = None
    """The record's version the edit was made against; a newer one refuses it with 409."""


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locked: bool | None = None
    archived: bool | None = None


class EntryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Literal["topic", "area"]
    """The You pages exist already; new pages are topics or free areas (a project's area is made
    with the project's first note)."""
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=400)


class EntryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    title: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=400)
    aliases: list[str] | None = Field(default=None, max_length=8)


class BulletBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=edit.BULLET_MAX_CHARS)
    category: str = ""


class QuickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=edit.BULLET_MAX_CHARS)


class OpBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "remove", "change"]
    id: str | None = None
    text: str | None = None


class InstructBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(default="", max_length=edit.INSTRUCTION_MAX_CHARS)
    version: int
    apply: bool = False
    """False asks the model for a preview; true applies ``ops`` (the preview the operator approved)."""
    ops: list[OpBody] = Field(default_factory=list, max_length=edit.MAX_OPS)
    title: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=400)


class ImportPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=transfer.MAX_IMPORT_BYTES)
    archive: str = Field(default="", max_length=transfer.MAX_IMPORT_BYTES * 2)
    """A zip (a Daedalus export) as base64."""


class ImportItemBody(BaseModel):
    """One bullet to import. The preview's own items come back as they are, so what they carry beyond
    these fields (what the scan said) is ignored: it is read again here, never trusted."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=transfer.MAX_BULLET_CHARS)
    category: str = ""
    topics: list[str] = Field(default_factory=list, max_length=8)
    section: str = ""
    slug: str = ""
    title: str = ""
    project_id: str | None = None
    date: str | None = None


class ImportApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImportItemBody] = Field(min_length=1, max_length=transfer.MAX_IMPORT_ITEMS)


class SensitiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: list[str] = Field(min_length=1, max_length=8)
    apply: bool = False
    """False counts what would go; true removes it."""


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str


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


class TidyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["apply", "dismiss"]
    ids: list[int] = Field(min_length=1, max_length=500)
    """The suggestions the operator saw in the group, so one filed since is not decided unseen."""


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
        "locked": is_locked(record),
        "archived": is_archived(record),
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
        waiting = {str(row["entry_id"] or ""): int(row["n"]) for row in await app.db.fetchall(
            "SELECT json_extract(detail, '$.entry_id') AS entry_id, count(*) AS n FROM memory_suggestions"
            " WHERE tenant_id = ? AND status = 'open' AND kind IN ('revision', 'addition') GROUP BY 1",
            (TENANT,),
        )}
        return {
            "entries": [{**e.view(count=counts.get(e.id, 0)), "suggestions": waiting.get(e.id, 0)} for e in found],
            # What the background pass holds for the operator's word, by entry (the badge on each row)
            # and in all (the review banner, which also counts notes meant for an entry not made yet).
            "suggestions": sum(waiting.values()),
            "sections": {name: sum(1 for e in every if e.section == name) for name in SECTIONS},
            "records": sum(counts.values()),
            "unfiled": sum(n for key, n in counts.items() if key not in known),
            "status": await status(),
        }

    @api.get("/api/memory/entries/{entry_id}")
    async def memory_entry(entry_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One entry with its records, newest first, and where each record was learned."""
        return await entry_detail(entry_id)

    async def entry_or_404(entry_id: str) -> Any:
        entry = await manager().memory_entries.get(entry_id)
        if entry is None:
            raise HTTPException(404, "no such entry")
        return entry

    async def entry_detail(entry_id: str) -> dict[str, Any]:
        entries = manager().memory_entries
        entry = await entry_or_404(entry_id)
        records = entries.records_of(entry_id)
        sources = await entries.sources(r.id for r in records)
        return {"entry": entry.view(count=len(records)), "records": [{**memory_view(r), "sources": sources.get(r.id, [])} for r in records]}

    async def status() -> dict[str, Any]:
        """What the Memory screen's pill and settings say: on, paused or not generating, when the
        background pass last read a chat and how many it is reading, today's spend on memory's model
        calls, the model the pass runs on (and why, when it is not the chosen one) and what the last
        pass did."""
        config = app.config.memory
        row = await app.db.fetchone("SELECT max(last_at) last_at, sum(CASE WHEN status = 'running' THEN 1 ELSE 0 END) running FROM memory_synthesis")
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        spent = await app.db.fetchone("SELECT sum(cost_usd) c FROM usage_events WHERE purpose LIKE 'memory%' AND at >= ?", (midnight,))
        return {
            "generate": config.generate, "paused": config.paused, "search_chats": config.search_chats,
            "synthesis_preset": config.synthesis_preset, "budget_usd": config.synthesis_usd_per_day,
            "spent_today_usd": float(spent["c"] or 0.0) if spent else 0.0,
            "last_synthesis_at": row["last_at"] if row else None, "learning": int(row["running"] or 0) if row else 0,
            **await synthesizer(app).status(),
        }

    @api.post("/api/memory/synthesis/run")
    async def memory_synthesis_run(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Read every chat with something unread now, in the background ("update memory now").
        Refused while memory is paused, not learning from chats, or has no budget at all."""
        learning = synthesizer(app)
        blocked = learning.blocked()
        if blocked:
            raise HTTPException(409, {"paused": "memory is paused", "off": "learning from chats is off", "no_budget": "the daily budget is 0"}.get(blocked, blocked))
        return {"started": await learning.run_now()}

    @api.post("/api/memory/synthesis/reread")
    async def memory_synthesis_reread(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Read every chat again from its start, in the background: what an earlier pass dropped (at
        an entry's cap, say) is learned now, and what memory holds already is not written twice.
        Refused as "update now" is."""
        learning = synthesizer(app)
        blocked = learning.blocked()
        if blocked:
            raise HTTPException(409, {"paused": "memory is paused", "off": "learning from chats is off", "no_budget": "the daily budget is 0"}.get(blocked, blocked))
        return {"started": await learning.reread()}

    @api.post("/api/memory/tidy")
    async def memory_tidy(body: TidyBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The one-off tidy-up: merge reworded notes, fold archived notes into their live twins, delete
        legacy test notes, close duplicate held suggestions and bring back what the archive alone
        holds. ``dry_run`` answers with what it would do (and the test notes it would delete); the
        real run is one change the history can undo. Refused while a pass is reading chats."""
        found = await synthesizer(app).tidy(dry_run=body.dry_run)
        if found is None:
            raise HTTPException(409, "a memory pass is reading chats; try again when it is done")
        return found

    @api.get("/api/memory/status")
    async def memory_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await status()

    @api.post("/api/memory/entries")
    async def memory_entry_create(body: EntryBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            entry = await manager().memory_entries.create(body.section, body.title, body.title, summary=body.summary)
        except ValueError as exc:
            raise HTTPException(409 if "exists" in str(exc) else 422, str(exc)) from exc
        return {"entry": entry.view(count=0)}

    @api.patch("/api/memory/entries/{entry_id}")
    async def memory_entry_update(entry_id: str, body: EntryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Rename an entry, rewrite its summary or aliases, refused with 409 when it changed since
        ``version`` was read (a bullet added by the agent meanwhile steps it too)."""
        try:
            entry = await manager().memory_entries.update(entry_id, if_version=body.version, title=body.title, summary=body.summary, aliases=body.aliases, by_operator=True)
        except KeyError as exc:
            raise HTTPException(404, "no such entry") from exc
        except EntryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"entry": entry.view(count=len(manager().memory_entries.records_of(entry_id)))}

    @api.delete("/api/memory/entries/{entry_id}")
    async def memory_entry_delete(entry_id: str, version: int, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Delete an entry and its bullets as one change the history can undo; a You page only loses
        its bullets."""
        try:
            removed, history = await curator(app).remove_entry(entry_id, if_version=version)
        except KeyError as exc:
            raise HTTPException(404, "no such entry") from exc
        except EntryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"deleted": removed, "history_id": history}

    @api.post("/api/memory/entries/{entry_id}/bullets")
    async def memory_entry_bullet(entry_id: str, body: BulletBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A bullet the operator adds on the entry's own page: filed there, whatever the rule would say."""
        entry = await entry_or_404(entry_id)
        category = _category(body.category) or (await classify(body.text.strip(), vocabulary=manager().memory.vocabulary(TENANT), ask=None)).category
        result, history = await curator(app).add(body.text, kind=category, topics=[], entry=entry)
        return {"decision": result.decision.value, "record": memory_view(result.record), "history_id": history, **(await entry_detail(entry_id))}

    @api.post("/api/memory/entries/{entry_id}/instruct")
    async def memory_entry_instruct(entry_id: str, body: InstructBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Change an entry in the operator's words. Without ``apply`` a model proposes operations and
        the answer is the diff to approve; with ``apply`` the approved operations are applied, refused
        with 409 when the entry moved on since the preview."""
        entries = manager().memory_entries
        entry = await entry_or_404(entry_id)
        records = entries.records_of(entry_id)
        if entry.version != body.version:
            raise HTTPException(409, f"the entry is at version {entry.version}, not {body.version}")
        if not body.apply:
            if not body.instruction.strip():
                raise HTTPException(422, "say what to change")
            ask = edit.model_ask(app.config, manager().providers, purpose="memory_edit")
            if ask is None:
                raise HTTPException(503, "no model is configured to edit memory with")
            try:
                proposal = await edit.propose(entry, records, body.instruction, ask)
            except ValueError as exc:
                raise HTTPException(502, str(exc)) from exc
            except TimeoutError as exc:
                raise HTTPException(504, "the model took too long to answer") from exc
            except Exception as exc:  # noqa: BLE001 - a provider's failure is reported, not raised as a 500
                logger.warning("memory edit preview failed", exc_info=True)
                raise HTTPException(502, f"the model call failed: {exc}") from exc
            return {
                "version": entry.version, "ops": [op.view() for op in proposal.ops], "title": proposal.title, "summary": proposal.summary,
                "note": proposal.note, "dropped": proposal.dropped, "flags": proposal.flags, "diff": edit.diff(records, proposal.ops),
            }
        try:
            ops = edit.ops_from([op.model_dump() for op in body.ops], records)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        flagged = [op.text for op in ops if op.text and injection_signs(op.text)]
        if flagged:
            raise HTTPException(422, "a new bullet reads like an instruction to an AI agent; edit it by hand if it is really yours")
        if not ops and body.title is None and body.summary is None:
            raise HTTPException(422, "nothing to apply")
        try:
            fresh, history = await curator(app).rewrite(entry_id, ops, if_version=body.version, title=body.title, summary=body.summary, instruction=body.instruction)
        except EntryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(409, "a bullet changed or moved since the preview") from exc
        return {"history_id": history, **(await entry_detail(fresh.id))}

    @api.post("/api/memory/quick")
    async def memory_quick(body: QuickBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Quick add: one line the operator typed, filed by the same rule as everything else (category
        and topics inferred as Remember infers them) in global memory, locked, undoable."""
        if not body.text.strip():
            raise HTTPException(422, "nothing to remember")
        result, history, entry = await curator(app).quick(body.text, classifier=manager().memory_classifier())
        return {"decision": result.decision.value, "record": memory_view(result.record), "entry": entry.view() if entry else None, "history_id": history}

    @api.get("/api/memory/export.zip")
    async def memory_export(_: dict[str, Any] = Depends(auth)) -> Response:
        entries = manager().memory_entries
        listed = await entries.list()
        records = manager().memory.records(TENANT)
        by_entry: dict[str, list[MemoryRecord]] = {}
        for record in records:
            by_entry.setdefault(entry_of(record), []).append(record)
        known = {e.id for e in listed}
        sources = await entries.sources(r.id for r in records)
        blob = await asyncio.to_thread(transfer.export_zip, listed, by_entry, sources, unfiled=[r for r in records if entry_of(r) not in known])
        name = f"daedalus-memory-{datetime.now(UTC).strftime('%Y-%m-%d')}.zip"
        return Response(blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def scanned(items: list[transfer.Item]) -> list[transfer.Item]:
        memory = manager().memory
        existing = {fingerprint(r.text) for r in memory.records(TENANT)}
        tombstones = {row["fingerprint"] for row in await app.db.fetchall("SELECT fingerprint FROM memory_tombstones WHERE tenant_id = ?", (TENANT,))}
        topics = {e.slug for e in await manager().memory_entries.list(section="topic")}
        return [transfer.scan(transfer.file_item(item, known_topics=topics), existing=existing, tombstones=tombstones) for item in items]

    @api.post("/api/memory/import/preview")
    async def memory_import_preview(body: ImportPreviewBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What an import would add, each bullet filed and scanned; nothing is written."""
        try:
            if body.archive:
                try:
                    blob = base64.b64decode(body.archive, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("the archive is not valid base64") from exc
                items = await asyncio.to_thread(transfer.read_archive, blob)
            else:
                items = transfer.read_text(body.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if len(items) > transfer.MAX_IMPORT_ITEMS:
            raise HTTPException(413, f"{len(items)} notes; one import takes at most {transfer.MAX_IMPORT_ITEMS}")
        found = await scanned(items)
        return {"items": [item.view(n) for n, item in enumerate(found)], "count": len(found)}

    @api.post("/api/memory/import/apply")
    async def memory_import_apply(body: ImportApplyBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """File the bullets the operator ticked. Scanned again here, whatever the preview said: one that
        addresses an AI agent is refused, because memory is handed to the agent in later chats."""
        items = await scanned([transfer.Item(text=" ".join(i.text.split()), category=i.category, topics=i.topics, section=i.section, slug=i.slug, title=i.title, project_id=i.project_id or "", date=i.date or "") for i in body.items])
        accepted = [i for i in items if not i.injection]
        projects = {row["id"] for row in await app.db.fetchall("SELECT id FROM projects")}
        records, history = await curator(app).import_items(accepted, projects=projects)
        return {"imported": len(records), "refused": len(items) - len(accepted), "history_id": history}

    @api.post("/api/memory/sensitive")
    async def memory_sensitive(body: SensitiveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Count, then remove, what memory holds on subjects the operator stops allowing."""
        if not body.apply:
            return {"count": len(curator(app).sensitive_records(body.subjects))}
        removed, history = await curator(app).purge_sensitive(body.subjects)
        return {"removed": removed, "history_id": history}

    @api.post("/api/memory/reset")
    async def memory_reset(body: ResetBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if body.confirm.strip().lower() not in ("reset", "сбросить"):
            raise HTTPException(422, "type the confirmation word to reset memory")
        return {"deleted": await curator(app).reset()}

    @api.patch("/api/memory/{memory_id}")
    async def memory_edit(memory_id: str, body: MemoryPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            record, history = await curator(app).edit(memory_id, text=body.text, category=_category(body.category), topics=body.topics, if_version=body.version)
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc
        except CuratorConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**memory_view(record), "history_id": history}

    @api.post("/api/memory/{memory_id}/mark")
    async def memory_mark(memory_id: str, body: MarkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Lock a bullet (the background pass may only suggest changes to it) or archive it (kept,
        folded away, not handed to the agent), and back."""
        try:
            record, history = await curator(app).mark(memory_id, locked=body.locked, archived=body.archived)
        except KeyError as exc:
            raise HTTPException(404, "no such memory") from exc
        return {**memory_view(record), "history_id": history}

    async def forget(memory_ids: list[str]) -> tuple[int, int | None]:
        """Forget records with one history entry; each is tombstoned, so the background pass does not
        learn it again from the chats it came from."""
        return await curator(app).remove(memory_ids)

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        deleted, history = await forget([memory_id])
        return {"deleted": bool(deleted), "history_id": history}

    @api.post("/api/memory/delete")
    async def memory_delete_many(body: MemoryIds, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        deleted, history = await forget(body.ids)
        return {"deleted": deleted, "history_id": history}

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
        dismissed, history = await curator(app).dismiss(suggestion_id)
        return {"dismissed": dismissed, "history_id": history}

    @api.post("/api/memory/suggestions/decide")
    async def memory_suggestions_decide(body: DecideBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Accept or dismiss a group of the background pass's suggestions (an entry's, or one pass's)
        as one change the history can undo."""
        return await curator(app).decide(body.ids, body.action)

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
