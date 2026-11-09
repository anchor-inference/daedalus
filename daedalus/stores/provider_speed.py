"""How fast each provider and model answered over a window: the medians the Usage screen shows.

Read from the speed columns of ``usage_events``, which only a streamed call that produced a token
fills (see ``CallTiming`` in the provider). SQLite has no percentile, so the window's measured calls
are read and ranked here; a call row is three numbers, and a busy month is tens of thousands of them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
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


MIN_TURN_TOKENS = 20
"""Below this a turn's speed is noise: a one-line answer's rate is mostly its first token's luck."""

RUN_BATCH = 400
"""Runs asked about in one statement, well under SQLite's limit on bound values."""


def turn_speed(rows: Sequence[Any]) -> dict[str, Any] | None:
    """A run's generation speed: the output its streamed calls produced over the time they took to produce it.

    Only the run's own calls (purpose ``stream``) with a rate, which only a streamed call long enough
    to time has (``CallTiming.measures``). Each call's time is its output at its own measured rate, so
    what the tools did between the calls and the wait for each first token are not in it, and the
    figure is what the run's rows on Usage add up to: their output over their output at their rates.
    The rows keep the rate and not the clock it was taken from; the call's span from its first token to
    its end is no stand-in, since a stream that ends on a tool call's arguments goes on long after its
    last counted token. ``None`` when no call was timed or the turn wrote under :data:`MIN_TURN_TOKENS`.
    """
    total_tokens = 0
    total_ms = 0.0
    by_model: dict[str, int] = {}
    calls = 0
    for row in rows:
        rate = row["output_tokens_per_second"]
        tokens = int(row["output_tokens"] or 0)
        if rate is None or float(rate) <= 0 or tokens <= 0:
            continue
        calls += 1
        total_tokens += tokens
        total_ms += tokens / float(rate) * 1000
        by_model[str(row["model"])] = by_model.get(str(row["model"]), 0) + tokens
    if not calls or total_tokens < MIN_TURN_TOKENS:
        return None
    return {
        "tokens": total_tokens,
        "ms": round(total_ms),
        "rate": round(total_tokens / (total_ms / 1000), 2),
        "calls": calls,
        "model": max(by_model, key=lambda name: by_model[name]),
    }


async def run_speeds(db: Database, session_id: str, run_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """:func:`turn_speed` of each of the session's runs named, by run id; a run with none is left out.

    Asked for the runs on the page the app reads, by the run index: a chat of twenty thousand turns
    costs what its page costs, and the poll that reads one message asks about one run. The ``+`` keeps
    the planner off the session's index, which it preferred and which walks every call the session made.
    """
    wanted = sorted({run for run in run_ids if run})
    grouped: dict[str, list[Any]] = {}
    for start in range(0, len(wanted), RUN_BATCH):
        batch = wanted[start:start + RUN_BATCH]
        marks = ",".join("?" * len(batch))
        rows = await db.fetchall(
            "SELECT run_id, model, output_tokens, output_tokens_per_second"
            f" FROM usage_events WHERE run_id IN ({marks}) AND +session_id = ? AND purpose = 'stream'"  # noqa: S608 — placeholders only
            " AND output_tokens_per_second IS NOT NULL",
            (*batch, session_id),
        )
        for row in rows:
            grouped.setdefault(str(row["run_id"]), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for run, rows in grouped.items():
        speed = turn_speed(rows)
        if speed is not None:
            out[run] = speed
    return out


__all__ = ["MAX_WINDOW_HOURS", "MIN_TURN_TOKENS", "ModelSpeed", "percentile", "provider_speed", "run_speeds", "turn_speed"]
