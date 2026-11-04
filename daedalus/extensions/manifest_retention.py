"""The start's one pass over the request manifests: everything beyond each session's newest two, and
every manifest blob nothing names any more (``RequestManifestStore.sweep``).

A background task, not a migration: the backlog it was written for was tens of thousands of rows and
close to fifty gigabytes of files, and a migration would have held the start for all of it. Every
later start runs it again and finds nothing, which is what proves the retention on write holds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

START_DELAY_SECONDS = 30.0
"""Lets the start restore its sessions and answer its first requests before the sweep reads a
hundred thousand files beside them."""


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    store = manager.request_manifests

    async def sweep() -> None:
        await asyncio.sleep(START_DELAY_SECONDS)
        try:
            report = await store.sweep()
        except Exception:  # noqa: BLE001 — housekeeping; the next start tries again
            logger.exception("the request manifest sweep failed")
            return
        if report.rows or report.blobs:
            logger.warning(
                "request manifests: %d rows and %d blobs (%.2f GB) freed, %d database pages reclaimed, in %.1f s",
                report.rows, report.blobs, report.bytes / 1e9, report.pages, report.seconds,
            )
        else:
            logger.info("request manifests: nothing to free (%.1f s)", report.seconds)

    return [asyncio.create_task(sweep(), name="request-manifest-sweep")]


__all__ = ["install"]
