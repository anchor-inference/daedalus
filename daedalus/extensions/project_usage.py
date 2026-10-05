"""What a project spends, where the work is: per staff member, its orchestrator and its other sessions.

Two sources, never added together for one session:

- a Daedalus session's calls are in ``usage_events``, priced or not. A session's subagents are counted
  with the session that started them (the way the spend cap counts them), so a staff member who hands
  work to a subagent is charged for it, and the subagent is not counted a second time as "other";
- a command-line member's spend is what its CLI reports, the latest snapshot per session in
  ``staff_sessions.usage_json``: tokens, a price when the CLI is metered, the share of the subscription
  window when it is not. A snapshot is cumulative and carries no time of its own, so a session counts
  in a window when it was last active inside it.

A call the provider did not price is counted as ``unpriced`` rather than as zero dollars, so "$0.00"
never hides spend nobody measured. Everything is read with one indexed query over the project's
sessions (``usage_events_by_session``) and one over its staff sessions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager

WINDOWS = ("today", "week", "all")


@dataclass(slots=True)
class Spend:
    usd: float = 0.0
    tokens: int = 0
    # Missing USD from provider calls and subscriptions must remain separate from known zero.
    unpriced: int = 0
    subscriptions: int = 0

    def add(self, usd: float | None, tokens: int, unpriced: int = 0, subscriptions: int = 0) -> None:
        self.usd += float(usd or 0.0)
        self.tokens += int(tokens or 0)
        self.unpriced += int(unpriced or 0)
        self.subscriptions += int(subscriptions or 0)

    def view(self) -> dict[str, Any]:
        return {"usd": round(self.usd, 4), "tokens": self.tokens, "unpriced": self.unpriced,
                "subscriptions": self.subscriptions}


@dataclass(slots=True)
class Line:
    """One row of the summary: a window each, and the subscription window a CLI reported last."""

    spend: dict[str, Spend] = field(default_factory=lambda: {w: Spend() for w in WINDOWS})
    subscription: dict[str, Any] | None = None

    def add(self, window: str, usd: float | None, tokens: int, unpriced: int = 0,
            subscriptions: int = 0) -> None:
        self.spend[window].add(usd, tokens, unpriced, subscriptions)

    def view(self) -> dict[str, Any]:
        return {**{w: self.spend[w].view() for w in WINDOWS}, "subscription": self.subscription}


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _metadata(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class ProjectUsage:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def starts(self, now: datetime | None = None) -> dict[str, str]:
        """Where "today" and "the last 7 days" begin, in UTC: today is the operator's, from the zone their app reported."""
        now = now or datetime.now(UTC)
        zone = _zone(self.manager.presence.locale()[1]) if getattr(self.manager, "presence", None) is not None else UTC
        local = now.astimezone(zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"today": midnight.astimezone(UTC).isoformat(), "week": (midnight - timedelta(days=6)).astimezone(UTC).isoformat()}

    async def summary(self, project_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """``{staff: [{staff_id, name, harness, …windows, subscription}], orchestrator, other, total, since}``."""
        db = self.manager.db
        since = self.starts(now)
        sessions = {r["id"]: _metadata(r["metadata"]) for r in await db.fetchall("SELECT id, metadata FROM sessions WHERE project_id = ?", (project_id,))}
        staff_rows = await db.fetchall(
            "SELECT s.id, s.staff_id, s.kind, s.session_id, s.usage_json, s.status_at, s.started_at, m.name, m.harness, m.archived_at "
            "FROM staff_sessions s JOIN staff m ON m.id = s.staff_id WHERE m.project_id = ?",
            (project_id,),
        )
        members = {r["id"]: dict(r) for r in await db.fetchall("SELECT id, name, harness, archived_at FROM staff WHERE project_id = ? ORDER BY created_at", (project_id,))}
        staff_of = {r["session_id"]: r["staff_id"] for r in staff_rows if r["session_id"]}

        def root(session_id: str) -> str:
            seen: set[str] = set()
            current = session_id
            while current not in seen:
                seen.add(current)
                leader = str(sessions.get(current, {}).get("subagent_of") or "")
                if not leader or leader not in sessions:
                    return current
                current = leader
            return current

        def owner(session_id: str) -> tuple[str, str | None]:
            top = root(session_id)
            meta = sessions.get(top, {})
            if meta.get("orchestrator_of") or meta.get("orchestrator_retired_of"):
                return "orchestrator", None
            member = staff_of.get(top) or (str(meta.get("staff_id") or "") or None)
            if member:
                return "staff", member
            return "other", None

        lines: dict[str, Line] = {m: Line() for m in members}
        orchestrator, other = Line(), Line()

        def line_for(session_id: str) -> Line:
            kind, member = owner(session_id)
            if kind == "orchestrator":
                return orchestrator
            if kind == "staff" and member is not None:
                return lines.setdefault(member, Line())
            return other

        if sessions:
            ids = list(sessions)
            marks = ",".join("?" for _ in ids)
            rows = await db.fetchall(
                "SELECT session_id, "
                "sum(cost_usd) AS usd, sum(cost_usd IS NULL) AS unpriced, sum(input_tokens + output_tokens) AS tokens, "
                "sum(CASE WHEN at >= ? THEN cost_usd END) AS usd_today, sum(CASE WHEN at >= ? AND cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_today, "
                "sum(CASE WHEN at >= ? THEN input_tokens + output_tokens ELSE 0 END) AS tokens_today, "
                "sum(CASE WHEN at >= ? THEN cost_usd END) AS usd_week, sum(CASE WHEN at >= ? AND cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_week, "
                "sum(CASE WHEN at >= ? THEN input_tokens + output_tokens ELSE 0 END) AS tokens_week "
                f"FROM usage_events WHERE session_id IN ({marks}) GROUP BY session_id",  # noqa: S608 — placeholders only
                (since["today"], since["today"], since["today"], since["week"], since["week"], since["week"], *ids),
            )
            for r in rows:
                target = line_for(r["session_id"])
                target.add("all", r["usd"], r["tokens"], r["unpriced"])
                target.add("week", r["usd_week"], r["tokens_week"], r["unpriced_week"])
                target.add("today", r["usd_today"], r["tokens_today"], r["unpriced_today"])

        for r in staff_rows:
            if r["kind"] != "cli":
                continue  # a Daedalus member's calls are in the events above; its snapshot repeats them
            snapshot = _metadata(r["usage_json"])
            if not snapshot:
                continue
            target = lines.setdefault(r["staff_id"], Line())
            tokens = int(snapshot.get("input_tokens") or 0) + int(snapshot.get("output_tokens") or 0)
            usd = snapshot.get("cost_usd")
            unpriced = 1 if usd is None and snapshot.get("source") != "subscription" else 0
            subscriptions = 1 if snapshot.get("source") == "subscription" else 0
            active = str(r["status_at"] or r["started_at"] or "")
            for window in WINDOWS:
                if window == "all" or active >= since[window]:
                    target.add(window, usd, tokens, unpriced, subscriptions)
            if snapshot.get("window_used_pct") is not None or snapshot.get("source") == "subscription":
                current = target.subscription
                if current is None or active >= str(current.get("at") or ""):
                    target.subscription = {"window_used_pct": snapshot.get("window_used_pct"), "source": snapshot.get("source"), "at": active}

        total = Line()
        for line in (*lines.values(), orchestrator, other):
            for window in WINDOWS:
                s = line.spend[window]
                total.add(window, s.usd, s.tokens, s.unpriced, s.subscriptions)
        staff = []
        for member_id, line in lines.items():
            member = members.get(member_id, {"name": member_id, "harness": "", "archived_at": None})
            staff.append({"staff_id": member_id, "name": member["name"], "harness": member["harness"], "archived": member["archived_at"] is not None, **line.view()})
        return {"project_id": project_id, "since": since, "staff": staff, "orchestrator": orchestrator.view(), "other": other.view(), "total": total.view()}


def tokens_words(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1000:
        return f"{round(tokens / 1000)}k"
    return str(tokens)


__all__ = ["WINDOWS", "ProjectUsage", "tokens_words"]
