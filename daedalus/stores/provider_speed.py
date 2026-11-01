"""How fast each provider and model answered over a window: the medians the Usage screen shows.

Read from the speed columns of ``usage_events``, which only a streamed call that produced a token
fills (see ``CallTiming`` in the provider). SQLite has no percentile, so the window's measured calls
are read and ranked here; a call row is three numbers, and a busy month is tens of thousands of them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from daedalus.stores.database import Database

MAX_WINDOW_HOURS = 24 * 90
"""The longest window asked about: past it the usage table may be pruned, and a summary over a
season says nothing about the provider as it is now."""


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """The ``fraction`` percentile of ``values`` by the nearest rank: a value some call really had,
    never one between two, so a p90 over ten calls is the ninth slowest rather than an invented one."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class ModelSpeed:
    provider_id: str
    model: str
    calls: int
    """Streamed calls in the window that produced a token: what the times are taken over."""
    rated: int
    """Of those, the calls long enough to have an output rate."""
    first_token_p50_ms: float | None
    first_token_p90_ms: float | None
    first_byte_p50_ms: float | None
    tokens_per_second_p50: float | None
    last_at: str

    def view(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "calls": self.calls,
            "rated": self.rated,
            "first_token_p50_ms": self.first_token_p50_ms,
            "first_token_p90_ms": self.first_token_p90_ms,
            "first_byte_p50_ms": self.first_byte_p50_ms,
            "tokens_per_second_p50": self.tokens_per_second_p50,
            "last_at": self.last_at,
        }


async def provider_speed(db: Database, *, hours: int, provider_id: str | None = None, model: str | None = None,
                         now: datetime | None = None) -> list[ModelSpeed]:
    """Each provider and model's speed over the last ``hours``, the most used first."""
    hours = max(1, min(int(hours), MAX_WINDOW_HOURS))
    since = ((now or datetime.now(UTC)) - timedelta(hours=hours)).isoformat()
    clauses = ["at >= ?", "first_token_ms IS NOT NULL"]
    params: list[Any] = [since]
    for column, value in (("provider_id", provider_id), ("model", model)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    rows = await db.fetchall(
        "SELECT provider_id, model, at, first_byte_ms, first_token_ms, output_tokens_per_second FROM usage_events"
        f" WHERE {' AND '.join(clauses)}",  # noqa: S608 — fixed clauses, values bound
        tuple(params),
    )
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        groups.setdefault((str(row["provider_id"]), str(row["model"])), []).append(row)
    out: list[ModelSpeed] = []
    for (provider, name), calls in groups.items():
        first_tokens = [float(r["first_token_ms"]) for r in calls]
        first_bytes = [float(r["first_byte_ms"]) for r in calls if r["first_byte_ms"] is not None]
        rates = [float(r["output_tokens_per_second"]) for r in calls if r["output_tokens_per_second"] is not None]
        out.append(ModelSpeed(
            provider_id=provider, model=name, calls=len(calls), rated=len(rates),
            first_token_p50_ms=percentile(first_tokens, 0.5), first_token_p90_ms=percentile(first_tokens, 0.9),
            first_byte_p50_ms=percentile(first_bytes, 0.5), tokens_per_second_p50=percentile(rates, 0.5),
            last_at=max(str(r["at"]) for r in calls),
        ))
    out.sort(key=lambda speed: (-speed.calls, speed.provider_id, speed.model))
    return out


__all__ = ["MAX_WINDOW_HOURS", "ModelSpeed", "percentile", "provider_speed"]
