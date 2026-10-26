"""The background work on the agent's memory — the tidying and the pass that learns from chats — and
the curator the Memory screen works through."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from protocore.contracts.memory import MemoryRecord

from daedalus.extensions.notifications import Draft
from daedalus.host.session_runner import NOT_OPERATOR_FACING, TENANT
from daedalus.memory.curator import EMBEDDINGS_PER_PASS, MemoryCurator
from daedalus.memory.synthesis import Synthesizer
from daedalus.search.service import offload

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def curator(app: Application) -> MemoryCurator:
    """The application's curator, made on first use when the extension did not install one (a test
    application, or a start where this extension failed): the screen's edits work either way."""
    existing = app.extensions.get("memory_curator")
    if isinstance(existing, MemoryCurator):
        return existing
    assert app.manager is not None
    made = MemoryCurator(app.db, app.manager.memory, TENANT, lambda: app.config.memory)
    app.extensions["memory_curator"] = made
    return made


def synthesizer(app: Application) -> Synthesizer:
    """The application's learning pass, made on first use when the extension did not install one, so
    the settings' status and "update now" answer either way."""
    existing = app.extensions.get("memory_synthesis")
    if isinstance(existing, Synthesizer):
        return existing
    made = _synthesizer(app)
    app.extensions["memory_synthesis"] = made
    return made


def _synthesizer(app: Application, notify: Any = None) -> Synthesizer:
    manager = app.manager
    assert manager is not None

    def zone() -> tzinfo:
        name = manager.presence.locale()[1] if getattr(manager, "presence", None) is not None else ""
        try:
            return ZoneInfo(name) if name else UTC
        except (ZoneInfoNotFoundError, ValueError):
            return UTC

    return Synthesizer(
        app.db, curator(app), lambda: app.config, manager.providers,
        running=manager.is_running, costs_nothing=manager.provider_costs_nothing, zone=zone, notify=notify,
        vectors=text_vectors(app),
    )


def embedder(app: Application):  # type: ignore[no-untyped-def]
    """Vectors for memory records from the conversation index's local model, when it is on and idle.

    Only what the index already loaded is used: memory never downloads a model or wakes the encoder
    on its own, and a busy machine gets the lexical comparison, as an installation without the index
    does. Vectors are kept per record version, so a pass encodes only what changed."""
    cache: dict[tuple[str, int], list[float]] = {}

    async def embed(records: Sequence[MemoryRecord]) -> dict[str, list[float]]:
        search = getattr(app, "search", None)
        if search is None or search.reason() != "ready" or search.encoder is None or search.busy():
            return {}
        fresh = [r for r in records if (r.id, r.version) not in cache][:EMBEDDINGS_PER_PASS]
        for record in fresh:
            async with search.lock:
                encoder = search.encoder
                if encoder is None:
                    break
                cache[(record.id, record.version)] = await offload(encoder.encode, record.text[:2000])
        return {r.id: cache[(r.id, r.version)] for r in records if (r.id, r.version) in cache}

    return embed


TEXTS_PER_CALL = 256
"""Texts encoded for one comparison when the index's model is ready: a pass's adds and their pools
fit, and a first tidy-up over the whole memory is told no rather than holding the encoder for long."""


def text_vectors(app: Application):  # type: ignore[no-untyped-def]
    """Vectors for plain texts (new notes, held suggestions, kept notes) from the conversation index's
    model, for the comparison that finds a restatement; ``None`` when the index is off, not ready,
    busy, or the request is larger than :data:`TEXTS_PER_CALL` uncached texts. The model behind the
    index is replaceable, and the comparison takes whatever it returns."""
    cache: dict[str, list[float]] = {}

    async def vectors(texts: Sequence[str]) -> list[list[float]] | None:
        search = getattr(app, "search", None)
        if search is None or search.reason() != "ready" or search.encoder is None or search.busy():
            return None
        fresh = [t for t in dict.fromkeys(texts) if t not in cache]
        if len(fresh) > TEXTS_PER_CALL:
            return None
        for text in fresh:
            async with search.lock:
                encoder = search.encoder
                if encoder is None:
                    return None
                cache[text] = await offload(encoder.encode, text[:2000])
        return [cache[t] for t in texts]

    return vectors


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None

    async def notify(suggested: int) -> None:
        if app.notifications is None:
            return
        await app.notifications.post(Draft(
            "system", f"Memory: {suggested} suggestion(s) to tidy it", "Records that look like duplicates or contradict each other. Memory → Suggestions: merge them, or keep both.",
            kind="memory_suggestions", level="quiet", link="/app/memory?suggestions=1", dedupe_key="memory_suggestions", source="memory",
        ))

    async def held(report: dict[str, Any]) -> None:
        if app.notifications is None:
            return
        await app.notifications.post(Draft(
            "system", f"Memory: {report['held']} note(s) from chats wait for your word", "The background pass held these back: a change to a note you wrote, or a note naming something you never said. Memory → Suggestions: apply or dismiss them.",
            kind="memory_suggestions", level="quiet", link="/app/memory?suggestions=1", dedupe_key="memory_suggestions", source="memory",
        ))

    made = MemoryCurator(app.db, app.manager.memory, TENANT, lambda: app.config.memory, embed=embedder(app), notify=notify)
    app.extensions["memory_curator"] = made
    learning = _synthesizer(app, notify=held)
    app.extensions["memory_synthesis"] = learning
    app.manager.delete_hooks.append(lambda session_id: session_deleted(app, session_id))
    if app.front is not None:
        app.front.memory_curator = made
    return [asyncio.create_task(made.loop(), name="memory-consolidation"), asyncio.create_task(learning.loop(), name="memory-synthesis")]


async def session_deleted(app: Application, session_id: str) -> None:
    """A chat the operator deletes takes what only it taught out of memory (see
    :meth:`MemoryCurator.forget_session`). Only the operator's own conversations: a subagent, a staff
    member or a side window is cleaned up by the host when its work is done, and the notes it left in
    a project's memory are the team's, kept after the session that wrote them is gone."""
    assert app.manager is not None
    state = await app.manager.get_state(session_id)
    if state is None or app.manager.is_staff(state) or any(state.metadata.get(key) for key in NOT_OPERATOR_FACING):
        return
    found = await curator(app).forget_session(session_id, title=state.session.title or "")
    if found["removed"] or found["kept"]:
        logger.info("memory: deleting %s removed %d record(s), kept %d backed elsewhere", session_id, found["removed"], found["kept"])


__all__ = ["curator", "embedder", "install", "session_deleted", "synthesizer", "text_vectors"]
