"""Learning records: what every run was asked, what it did, how it ended — and a weekly digest.

A record is pure observation written at the end of each run: the ask, the outcome, the
tools used, the tool failures, cost and duration. Once a week the records are folded into
a digest in the inbox: which tools fail most, which asks keep coming back, where the money
went — the raw material for improvement proposals, which stay proposals until the operator
acts on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.extensions.notifications import Draft
from daedalus.host.prompts import split_headline, without_turn_context

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 3600
ERROR_PREFIX_CHARS = 60
ASK_PREFIX_CHARS = 40


class Learning:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if status == "awaiting":
            return
        manager = self.app.manager
        if manager is None:
            return
        if manager.is_private(session_id):
            # No record: it keeps the ask and the headline, which the weekly digest quotes and the
            # memory pass reads, and a private chat is never learned from.
            return
        try:
            state = await manager.get_state(session_id)
            history = list(state.engine.history) if state is not None and state.engine is not None else []
            # The working history spans the whole session; this record is about the run that just ended.
            start = min(state.run_history_start, len(history)) if state is not None else 0
            history = history[start:]
            ask = ""
            headline = ""
            tools: Counter[str] = Counter()
            failures: list[dict[str, str]] = []
            names: dict[str, str] = {}
            for m in history:
                if m.role is MessageRole.user and not ask and m.metadata.get("daedalus.origin") not in (None, "core"):
                    ask = without_turn_context("".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))).strip()
                for b in m.content_blocks:
                    if isinstance(b, ToolUseBlock):
                        tools[b.name] += 1
                        names[b.tool_call_id] = b.name
                    elif isinstance(b, ToolResultBlock) and b.is_error:
                        failures.append({"tool": names.get(b.tool_call_id, "?"), "error": (b.content or "")[:200]})
                if m.role is MessageRole.assistant:
                    text = "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock))
                    _, found = split_headline(text)
                    if found:
                        headline = found
            usage = await self.app.db.fetchone("SELECT sum(cost_usd) usd, count(*) calls, min(at) first_at FROM usage_events WHERE run_id = ?", (run_id,))
            run = await self.app.db.fetchone("SELECT created_at FROM runs WHERE id = ?", (run_id,))
            started = datetime.fromisoformat(run["created_at"]) if run and run["created_at"] else None
            duration = (datetime.now(UTC) - started).total_seconds() if started else None
            await self.app.db.execute(
                "INSERT INTO learning_records(session_id, run_id, at, ask, outcome, headline, tools, failures, iterations, cost_usd, duration_s)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, run_id, datetime.now(UTC).isoformat(), ask[:2000], status, headline[:500],
                    json.dumps(dict(tools)), json.dumps(failures[:50]), int(usage["calls"] or 0) if usage else 0,
                    float(usage["usd"]) if usage and usage["usd"] is not None else None, duration,
                ),
            )
        except Exception:  # noqa: BLE001 — observation must never affect the run
            logger.warning("learning record failed", exc_info=True)

    async def report(self, days: int = 7) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = await self.app.db.fetchall("SELECT * FROM learning_records WHERE at >= ? ORDER BY at", (since,))
        records = [dict(r) for r in rows]
        outcomes = Counter(r["outcome"] for r in records)
        tools: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        asks: Counter[str] = Counter()
        cost = 0.0
        unmetered = 0
        for r in records:
            tools.update(json.loads(r["tools"] or "{}"))
            for f in json.loads(r["failures"] or "[]"):
                failures[f"{f['tool']}: {f['error'][:ERROR_PREFIX_CHARS]}"] += 1
            if r["ask"]:
                asks[r["ask"][:ASK_PREFIX_CHARS].lower()] += 1
            if r["cost_usd"] is None:
                unmetered += 1
            else:
                cost += float(r["cost_usd"])
        return {
            "days": days,
            "runs": len(records),
            "outcomes": dict(outcomes),
            "tools": tools.most_common(15),
            "failures": failures.most_common(10),
            "repeated_asks": [(a, n) for a, n in asks.most_common(10) if n > 1],
            "cost_usd": round(cost, 4),
            "unmetered_runs": unmetered,
            "candidates": self._candidates(failures, asks),
        }

    def _candidates(self, failures: Counter[str], asks: Counter[str]) -> list[str]:
        """Improvement candidates: anything that failed or was asked ``ops.learning_repeat_threshold`` times or more."""
        threshold = self.app.config.ops.learning_repeat_threshold
        out = []
        for key, n in failures.most_common(5):
            if n >= threshold:
                out.append(f"'{key}' failed {n}× — a tool description, a default or a skill may be missing")
        for key, n in asks.most_common(5):
            if n >= threshold:
                out.append(f"the ask '{key}…' came {n}× — a skill or a scheduled task would make it one call")
        return out

    def render(self, data: dict[str, Any]) -> str:
        lines = [f"**Learning digest — last {data['days']} days**", f"runs: {data['runs']} · outcomes: " + ", ".join(f"{k} {v}" for k, v in data["outcomes"].items()) + f" · spent ${data['cost_usd']:.2f}" + (f" (+{data['unmetered_runs']} unmetered)" if data["unmetered_runs"] else "")]
        if data["tools"]:
            lines.append("tools: " + ", ".join(f"{t} ×{n}" for t, n in data["tools"][:10]))
        if data["failures"]:
            lines.append("failures:\n" + "\n".join(f"- {k} ×{n}" for k, n in data["failures"]))
        if data["repeated_asks"]:
            lines.append("repeated asks:\n" + "\n".join(f"- {a}… ×{n}" for a, n in data["repeated_asks"]))
        if data["candidates"]:
            lines.append("improvement candidates (proposals only, nothing is changed):\n" + "\n".join(f"- {c}" for c in data["candidates"]))
        return "\n\n".join(lines)

    async def loop(self) -> None:
        while True:
            try:
                await self.maybe_digest()
            except Exception:  # noqa: BLE001
                logger.exception("learning digest failed")
            await asyncio.sleep(CHECK_EVERY_SECONDS)

    async def maybe_digest(self) -> bool:
        every = self.app.config.ops.learning_digest_days
        last = await self.app.db.kv_get("learning_last_digest", None)
        if last and datetime.now(UTC) - datetime.fromisoformat(last) < timedelta(days=every):
            return False
        data = await self.report(every)
        if data["runs"] == 0:
            return False  # a quiet week does not arm the gate; the first busy week gets its digest
        if self.app.notifications is not None:
            await self.app.notifications.post(Draft("system", f"Digest: {data['runs']} runs, {len(data['candidates'])} improvement candidate(s)", self.render(data), kind="learning_digest", level="quiet", source="learning"))
        await self.app.db.kv_set("learning_last_digest", datetime.now(UTC).isoformat())  # after the post, so a restart in between cannot lose a week
        return True

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "report":
            return self.render(await self.report(int(kwargs.get("days") or 7)))
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    learning = Learning(app)
    app.extensions["learning"] = learning
    assert app.manager is not None
    app.manager.on_finished(learning.on_run_finished)
    app.manager.service_hooks["learning"] = learning.service
    return [asyncio.create_task(learning.loop(), name="learning")]


__all__ = ["Learning", "install"]
