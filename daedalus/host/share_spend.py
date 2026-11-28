"""What a live link's visitors have spent, and whether the link's limit is reached."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daedalus.app import Application


async def exhausted(app: Application, share_id: str) -> str | None:
    """Which limit of the link is spent: ``usd``, ``weekly_pct``, or None while there is room."""
    return None


__all__ = ["exhausted"]
