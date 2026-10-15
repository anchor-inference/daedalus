"""How fast a subscription's quota window is being spent, and when it will run out at that pace.

The key proxy reports each logged-in subscription's windows (share used, when the window resets), and
a Codex member's transcript carries the same windows beside every token count. One reading says how
full a window is; only a series of them says whether it will last. Every reading is kept here for a
little over a week, the longest window there is, and the forecast is read from the readings of the
window that is open now.

The burn rate is a Theil–Sen slope, the median of the slopes between every pair of readings, rather
than a least-squares line: one reading taken just after a burst, or a share the provider rounded,
moves a fitted line a long way and the median hardly at all. The same pairwise slopes give the band:
the 10th and 90th percentiles of them bound the time to exhaustion at 80 %. Fewer than three readings
are not a trend, and the forecast says so instead of drawing a line through two points.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from daedalus.stores.database import Database

RETENTION = timedelta(days=8)
"""A week's window plus a day: a forecast never needs a reading older than the window it is in."""
MIN_GAP = timedelta(seconds=60)
"""Readings of one window closer than this are one reading: the Usage screen polls every two
minutes, and several open windows must not fill the table with copies."""
MIN_SAMPLES = 3
MAX_SAMPLES = 200
"""The newest readings a forecast looks at: enough for a week at the sampler's pace, few enough that
the pairwise slopes (n² / 2 of them) stay cheap."""
RESET_TOLERANCE = timedelta(minutes=2)
"""Two readings whose reset times differ by less than this are of the same window: providers round
the reset time, and a fresh fetch can move it by seconds."""

Status = Literal["ok", "insufficient", "idle", "exhausted"]


@dataclass(frozen=True, slots=True)
class Sample:
    at: datetime
    used_percent: float
    resets_at: datetime | None = None
    window_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class QuotaForecast:
    account: str
    window: str
    status: Status
    """``ok``: a rate and an estimate. ``insufficient``: fewer than three readings in this window.
    ``idle``: readings enough, but nothing is being spent. ``exhausted``: the window is used up."""
    used_percent: float
    samples: int
    sampled_at: str | None
    burn_per_hour: float | None
    """Percentage points of the window spent per hour, the median pace."""
    eta_at: str | None
    """When the window runs out at the median pace; None when it does not, or cannot be told."""
    eta_early_at: str | None
    eta_late_at: str | None
    """The 80 % band around ``eta_at``: the late end is None when the slow end of the pace is no
    spend at all."""
    resets_at: str | None
    exhausts_before_reset: bool | None
    """Whether the median estimate comes before the window resets; None without both."""

    def runs_out_within(self, seconds: float, *, now: datetime | None = None) -> bool:
        """Whether the window is predicted to run out within ``seconds``, by the early end of the band
        so that a caller choosing a runtime errs towards caution. Never true without a forecast."""
        if self.status == "exhausted":
            return True
        early = _parse(self.eta_early_at or self.eta_at)
        if self.status != "ok" or early is None:
            return False
        reset = _parse(self.resets_at)
        if reset is not None and reset <= early:
            return False
        return early <= (now or datetime.now(UTC)) + timedelta(seconds=seconds)

    def view(self) -> dict[str, Any]:
        return {"account": self.account, "window": self.window, "status": self.status,
                "used_percent": self.used_percent, "samples": self.samples, "sampled_at": self.sampled_at,
                "burn_per_hour": self.burn_per_hour, "eta_at": self.eta_at, "eta_early_at": self.eta_early_at,
                "eta_late_at": self.eta_late_at, "resets_at": self.resets_at,
                "exhausts_before_reset": self.exhausts_before_reset}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def reset_time(value: Any) -> datetime | None:
    """A window's reset as the providers give it: unix seconds, or an ISO time."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC) if value > 0 else None
    return _parse(str(value))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    position = (len(sorted_values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def current_window(samples: Sequence[Sample]) -> list[Sample]:
    """The readings of the window that is open now, oldest first.

    With a reset time, the window is the readings that share the newest one's. Without one (a CLI
    transcript gives only the window's length) a fall in the share is a reset, and so is a reading
    older than the window's length."""
    ordered = sorted(samples, key=lambda s: s.at)
    if not ordered:
        return []
    newest = ordered[-1]
    if newest.resets_at is not None:
        return [s for s in ordered if s.resets_at is not None and abs(s.resets_at - newest.resets_at) <= RESET_TOLERANCE]
    start = 0
    for index in range(1, len(ordered)):
        if ordered[index].used_percent < ordered[index - 1].used_percent - 0.5:
            start = index
    out = ordered[start:]
    if newest.window_seconds:
        floor = newest.at - timedelta(seconds=newest.window_seconds)
        out = [s for s in out if s.at >= floor]
    return out


def forecast(account: str, window: str, samples: Sequence[Sample]) -> QuotaForecast:
    """The forecast for one window from its readings (any order, any window: the current one is picked)."""
    current = current_window(samples)[-MAX_SAMPLES:]
    last = current[-1] if current else None
    used = last.used_percent if last is not None else 0.0
    resets = last.resets_at if last is not None else None

    def result(status: Status, *, burn: float | None = None, eta: datetime | None = None,
               early: datetime | None = None, late: datetime | None = None) -> QuotaForecast:
        before = (eta <= resets) if eta is not None and resets is not None else None
        return QuotaForecast(
            account=account, window=window, status=status, used_percent=round(used, 2), samples=len(current),
            sampled_at=last.at.isoformat() if last is not None else None,
            burn_per_hour=round(burn, 3) if burn is not None else None,
            eta_at=eta.isoformat() if eta is not None else None,
            eta_early_at=early.isoformat() if early is not None else None,
            eta_late_at=late.isoformat() if late is not None else None,
            resets_at=resets.isoformat() if resets is not None else None, exhausts_before_reset=before,
        )

    if last is not None and used >= 100:
        return result("exhausted", eta=last.at, early=last.at, late=last.at)
    if len(current) < MIN_SAMPLES or last is None:
        return result("insufficient")
    slopes = sorted(
        (b.used_percent - a.used_percent) / (b.at - a.at).total_seconds()
        for i, a in enumerate(current) for b in current[i + 1:]
        if (b.at - a.at).total_seconds() > 0
    )
    if not slopes:
        return result("insufficient")
    median = _quantile(slopes, 0.5)
    if median <= 0:
        return result("idle", burn=max(0.0, median) * 3600)
    fast, slow = _quantile(slopes, 0.9), _quantile(slopes, 0.1)
    left = 100.0 - used

    def at(rate: float) -> datetime | None:
        return last.at + timedelta(seconds=left / rate) if rate > 0 else None

    return result("ok", burn=median * 3600, eta=at(median), early=at(fast), late=at(slow))


async def record(db: Database, account: str, windows: Iterable[dict[str, Any]], *, source: str,
                 at: datetime | None = None) -> int:
    """Keep one reading of each window of ``account``; the number kept. ``windows`` are dicts with
    ``name``, ``used_percent`` and optionally ``resets_at`` and ``window_seconds``. A reading within a
    minute of the window's last one is dropped, and readings past the retention go."""
    stamp = (at or datetime.now(UTC)).astimezone(UTC)
    kept = 0
    async with db.transaction() as conn:
        for window in windows:
            name = str(window.get("name") or "").strip()
            used = window.get("used_percent")
            if not name or isinstance(used, bool) or not isinstance(used, int | float):
                continue
            async with conn.execute("SELECT max(sampled_at) AS newest FROM quota_samples WHERE account = ? AND window_name = ?",
                                    (account, name)) as cursor:
                row = await cursor.fetchone()
            newest = _parse(row["newest"]) if row is not None else None
            if newest is not None and stamp - newest < MIN_GAP:
                continue
            resets = reset_time(window.get("resets_at"))
            seconds = window.get("window_seconds")
            await conn.execute(
                "INSERT OR IGNORE INTO quota_samples(account,window_name,sampled_at,used_percent,resets_at,window_seconds,source)"
                " VALUES (?,?,?,?,?,?,?)",
                (account, name, stamp.isoformat(), float(used), resets.isoformat() if resets is not None else None,
                 int(seconds) if isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds > 0 else None, source))
            kept += 1
        await conn.execute("DELETE FROM quota_samples WHERE sampled_at < ?", ((stamp - RETENTION).isoformat(),))
    return kept


async def record_subscriptions(db: Database, subscriptions: dict[str, Any], *, at: datetime | None = None) -> int:
    """Keep a reading of every window in the key proxy's ``/subscriptions/usage`` answer."""
    kept = 0
    for account, view in subscriptions.items():
        if isinstance(view, dict) and view.get("logged_in") and isinstance(view.get("windows"), list):
            kept += await record(db, str(account), [w for w in view["windows"] if isinstance(w, dict)], source="keyproxy", at=at)
    return kept


def window_name(minutes: int) -> str:
    """A CLI window's name from its length: "5h", "7d", "90m"."""
    if minutes > 0 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes > 0 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


async def record_cli(db: Database, harness: str, env: str, windows: Iterable[Sequence[Any]], *,
                     at: datetime | None = None) -> int:
    """Keep the windows a CLI member's transcript reported, each ``(minutes, used_percent)``. The
    account is the CLI in its environment: the member's login there is not necessarily the one the
    key proxy holds, so the two are never merged."""
    rows = [{"name": window_name(int(minutes)), "used_percent": float(used), "window_seconds": int(minutes) * 60}
            for minutes, used in windows]
    return await record(db, f"cli:{harness}@{env}", rows, source="cli", at=at) if rows else 0


async def quota_forecast(db: Database, *, account: str | None = None, window: str | None = None) -> list[QuotaForecast]:
    """The forecast of every window with readings, or of one account or one window of it.

    This is what other code asks before it starts work on a subscription, for example to pass over a
    runtime whose window is predicted to run out: ``f.runs_out_within(seconds)`` on the result."""
    clauses, params = [], []
    if account is not None:
        clauses.append("account = ?")
        params.append(account)
    if window is not None:
        clauses.append("window_name = ?")
        params.append(window)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.fetchall("SELECT account,window_name,sampled_at,used_percent,resets_at,window_seconds FROM quota_samples"
                             + where + " ORDER BY account,window_name,sampled_at", tuple(params))
    grouped: dict[tuple[str, str], list[Sample]] = {}
    for row in rows:
        at = _parse(row["sampled_at"])
        if at is None:
            continue
        grouped.setdefault((row["account"], row["window_name"]), []).append(
            Sample(at, float(row["used_percent"]), _parse(row["resets_at"]), row["window_seconds"]))
    return [forecast(acct, name, samples) for (acct, name), samples in grouped.items()]


__all__ = ["QuotaForecast", "Sample", "current_window", "forecast", "quota_forecast", "record", "record_cli",
           "record_subscriptions", "reset_time", "window_name"]
