"""Explicit retry budgets; only effects proven read-only may repeat automatically."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

# New read-only handlers must opt in by exact kind. A prefix would let a write inherit a
# read's retry budget when a provider adds a new operation.
READ_ONLY_BUDGETS: dict[str, int] = {"provider.catalog.read": 3, "provider.resume.status_read": 3}
TRANSIENT_ERRORS = frozenset({"rate_limit", "timeout", "unavailable"})


def budget(kind: str) -> int:
    return READ_ONLY_BUDGETS.get(kind, 1)


def next_retry_at(effect_id: str, ordinal: int, at: str) -> str:
    """Bound exponential delay with stable jitter so a restart cannot redraw an earlier deadline."""
    base = min(60, 2 ** (ordinal - 1))
    seed = hashlib.sha256(f"{effect_id}:{ordinal}".encode()).digest()
    delay = base + int.from_bytes(seed[:2], "big") / 65535 * base
    instant = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return (instant + timedelta(seconds=delay)).astimezone(UTC).isoformat()
