"""Reads the subscriptions' quota windows from the key proxy on a timer, so their forecast has a series.

The Usage screen reads them too, every two minutes while it is open, and each read is kept; but the
windows are spent while the operator is looking elsewhere, and a forecast built only from the
minutes someone had the screen open would have nothing in it for the hours that matter. The timer
reads only after something was spent since the last read (a model call, or a member at work), so an
idle installation does not ask the providers for numbers that cannot have moved.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from daedalus.config import is_keyproxy_url, keyproxy_base
from daedalus.stores.quota import record_subscriptions

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)
INTERVAL_S = 600
"""Ten minutes: a five-hour window spent in an afternoon moves a few points in that time, enough for
a slope, and the providers' usage endpoints are not asked more than the Usage screen asks them."""


def keyproxy_origin(app: Application) -> str:
    """The key proxy's address when a provider goes through it, else "": the one address this bot
    sends its own token to, taken from the launcher and never from a provider entry."""
    base = keyproxy_base()
    return base if any(is_keyproxy_url(p.base_url) for p in app.config.providers.values()) else ""


async def fetch_subscriptions(app: Application) -> dict[str, Any]:
    """The key proxy's ``/subscriptions/usage`` answer, kept as readings; {} when it cannot be had."""
    origin = keyproxy_origin(app)
    if not origin:
        return {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(origin + "/subscriptions/usage")
        found = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}
    if isinstance(found, dict):
        try:
            await record_subscriptions(app.db, found)
        except Exception:  # noqa: BLE001 — a reading that cannot be kept must not cost the answer
            logger.warning("the quota readings could not be kept", exc_info=True)
    return found if isinstance(found, dict) else {}


async def spent_since(app: Application, since: datetime) -> bool:
    """Whether anything may have moved a window since ``since``: a model call, or a member at work."""
    call = await app.db.fetchone("SELECT 1 FROM usage_events WHERE at >= ? LIMIT 1", (since.isoformat(),))
    if call is not None:
        return True
    member = await app.db.fetchone("SELECT 1 FROM staff_sessions WHERE ended_at IS NULL AND kind = 'cli' LIMIT 1")
    return member is not None


async def sample_loop(app: Application) -> None:
    last = datetime.now(UTC) - timedelta(seconds=INTERVAL_S)
    while True:
        try:
            if await spent_since(app, last):
                last = datetime.now(UTC)
                await fetch_subscriptions(app)
        except Exception:  # noqa: BLE001 — the timer outlives one bad read
            logger.exception("the quota sampler failed")
        await asyncio.sleep(INTERVAL_S)


async def install(app: Application) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(sample_loop(app), name="quota-sampler")]
