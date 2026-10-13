"""The background tidying of the agent's memory, and the curator the Memory screen works through."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from protocore.contracts.memory import MemoryRecord

from daedalus.extensions.notifications import Draft
from daedalus.host.session_runner import TENANT
from daedalus.memory.curator import EMBEDDINGS_PER_PASS, MemoryCurator
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


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None

    async def notify(suggested: int) -> None:
        if app.notifications is None:
            return
        await app.notifications.post(Draft(
            "system", f"Memory: {suggested} suggestion(s) to tidy it", "Records that look like duplicates or contradict each other. Memory → Suggestions: merge them, or keep both.",
            kind="memory_suggestions", level="quiet", link="/app/memory?suggestions=1", dedupe_key="memory_suggestions", source="memory",
        ))

    made = MemoryCurator(app.db, app.manager.memory, TENANT, lambda: app.config.memory, embed=embedder(app), notify=notify)
    app.extensions["memory_curator"] = made
    return [asyncio.create_task(made.loop(), name="memory-consolidation")]


__all__ = ["curator", "embedder", "install"]
