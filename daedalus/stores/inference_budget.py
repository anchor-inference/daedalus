"""Reserve known quotes atomically before inference; uncertain sends retain their balance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import aiosqlite

from daedalus.stores.control import canonical, now, one
from daedalus.stores.database import Database


class BudgetRefused(ValueError):
    """The host cannot prove that a quoted call fits every applicable balance."""


def microusd(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetRefused("a finite nonnegative dollar amount is required") from exc
    if not amount.is_finite() or amount < 0:
        raise BudgetRefused("a finite nonnegative dollar amount is required")
    if amount > Decimal(2**63 - 1) / 1_000_000:
        raise BudgetRefused("the dollar amount exceeds the ledger's integer range")
    result = int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if result > 2**63 - 1:
        raise BudgetRefused("the dollar amount exceeds the ledger's integer range")
    return result


@dataclass(frozen=True)
class Constraint:
    """A host-selected cap and the exact historical usage slice it owns."""

    key: str
    cap_microusd: int
    provider_id: str | None = None
    run_id: str | None = None
    session_ids: tuple[str, ...] = ()
    since: str | None = None


async def _historical(conn: aiosqlite.Connection, limit: Constraint) -> int:
    clauses, args = [], []
    for column, value in (("provider_id", limit.provider_id), ("run_id", limit.run_id), ("at", limit.since)):
        if value is not None:
            clauses.append(f"{column} {'>=' if column == 'at' else '='} ?")
            args.append(value)
    if limit.session_ids:
        clauses.append("session_id IN (" + ",".join("?" for _ in limit.session_ids) + ")")
        args.extend(limit.session_ids)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    cursor = await conn.execute("SELECT cost_usd,inference_reservation_id,provider_id,model,session_id,run_id FROM usage_events" + where, tuple(args))
    total = 0
    try:
        async for row in cursor:
            if row["cost_usd"] is None:
                escrow = await one(conn, "SELECT state,provider_id,model,session_id,run_id FROM inference_reservations WHERE id = ?",
                                   (row["inference_reservation_id"],))
                if (escrow is not None and escrow["state"] in ("inflight", "unknown")
                        and all(escrow[field] == row[field] for field in ("provider_id", "model", "session_id", "run_id"))):
                    # This charge is counted at its still-held quote below. An older unpriced
                    # usage row without an admitted ceiling cannot be treated the same way.
                    continue
                raise BudgetRefused(f"{limit.key}: prior usage has an unknown price")
            total += microusd(row["cost_usd"])
    finally:
        await cursor.close()
    return total


async def held_in(conn: aiosqlite.Connection, limit: Constraint) -> tuple[int, set[str]]:
    """Count a prepaid allocation once, including its unfinished descendant requests."""
    from daedalus.stores.comparison_funding import (  # Lazy: funding uses budget constraints and historical usage.
        pool_balances_in,
        pool_matches,
    )

    pools = [row for row in await pool_balances_in(conn) if pool_matches(row, limit)]
    covered = {row['id'] for row in pools}
    total = sum(row['held'] for row in pools)
    clauses = ["r.state IN ('reserved','inflight','unknown')"]
    args: list[Any] = []
    for column, value in (("provider_id", limit.provider_id), ("run_id", limit.run_id)):
        if value is not None:
            clauses.append(f"r.{column} = ?")
            args.append(value)
    if limit.session_ids:
        clauses.append("(r.session_id IN (" + ",".join("?" for _ in limit.session_ids) + ")"
                       " OR EXISTS (SELECT 1 FROM inference_reservation_scopes s"
                       " WHERE s.reservation_id = r.id AND s.scope_key = ?))")
        args.extend((*limit.session_ids, limit.key))
    async with conn.execute("SELECT r.comparison_slot_id,r.quoted_microusd FROM inference_reservations r"
                            " WHERE " + " AND ".join(clauses), tuple(args)) as cursor:
        for row in await cursor.fetchall():
            if row['comparison_slot_id'] not in covered:
                total += row['quoted_microusd']
    return total, covered


class InferenceBudget:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def spend_view(self, *, since: str | None, total_cap: float,
                         provider_caps: dict[str, float]) -> dict[str, Any]:
        """Read observed charges and all outstanding quotes from one ledger snapshot.

        Resetting historical counters does not release a request which may still charge.
        """
        def balance(cap: float) -> dict[str, Any]:
            return {"spent_usd": 0.0, "unmetered": 0, "cap_usd": cap,
                    "reserved_usd": 0.0, "uncertain_usd": 0.0,
                    "reserved_count": 0, "uncertain_count": 0}

        total = balance(total_cap)
        providers = {identity: balance(cap) for identity, cap in provider_caps.items()}
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT provider_id,sum(cost_usd) AS usd,sum(cost_usd IS NULL) AS unmetered"
                " FROM usage_events" + (" WHERE at >= ?" if since else "") + " GROUP BY provider_id",
                (since,) if since else (),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                view = providers.setdefault(row["provider_id"], balance(0.0))
                view["spent_usd"] = float(row["usd"] or 0.0)
                view["unmetered"] = int(row["unmetered"] or 0)
                total["spent_usd"] += view["spent_usd"]
                total["unmetered"] += view["unmetered"]
            cursor = await conn.execute(
                "SELECT provider_id,sum(quoted_microusd) AS held,count(*) AS count,"
                "sum(CASE WHEN state = 'unknown' THEN quoted_microusd ELSE 0 END) AS uncertain,"
                "sum(state = 'unknown') AS uncertain_count FROM inference_reservations"
                " WHERE state IN ('reserved','inflight','unknown') AND comparison_slot_id IS NULL GROUP BY provider_id",
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                view = providers.setdefault(row["provider_id"], balance(0.0))
                view.update(reserved_usd=int(row["held"]) / 1_000_000,
                            uncertain_usd=int(row["uncertain"]) / 1_000_000,
                            reserved_count=int(row["count"]), uncertain_count=int(row["uncertain_count"]))
                for field in ("reserved_usd", "uncertain_usd", "reserved_count", "uncertain_count"):
                    total[field] += view[field]
            from daedalus.stores.comparison_funding import (
                pool_balances_in,  # Lazy: funding uses budget constraints and historical usage.
            )

            for row in await pool_balances_in(conn):
                view = providers.setdefault(row['provider_id'], balance(0.0))
                additions = {'reserved_usd': row['held'] / 1_000_000,
                             'uncertain_usd': row['uncertain'] / 1_000_000,
                             'reserved_count': 1, 'uncertain_count': int(row['uncertain_count'] or 0)}
                for field, value in additions.items():
                    view[field] += value
                    total[field] += value
        for view in (total, *providers.values()):
            for field in ("spent_usd", "reserved_usd", "uncertain_usd"):
                view[field] = round(view[field], 6)
        return {"since": since or "", "total": total, "per_provider": providers}

    async def reserve_in(self, conn: aiosqlite.Connection, *, reservation_id: str,
                         provider_id: str, model: str, session_id: str | None, run_id: str | None,
                         request_digest: str, quoted_microusd: int, rate_version: str,
                         quote: dict[str, Any], constraints: tuple[Constraint, ...],
                         execution_attempt_id: str | None = None) -> dict[str, Any]:
        """The host supplies a verified quote; this store never invents an input token bound."""
        if (type(quoted_microusd) is not int or not 0 <= quoted_microusd <= 2**63 - 1 or not reservation_id or
                not provider_id or not model or not rate_version or not re.fullmatch(r"[0-9a-f]{64}", request_digest) or
                hashlib.sha256(canonical(quote).encode()).hexdigest() != rate_version):
            raise BudgetRefused("a pinned priced quote and request identity are required")
        if len({limit.key for limit in constraints}) != len(constraints):
            raise BudgetRefused("each applicable balance must be supplied once")
        if await one(conn, "SELECT 1 FROM inference_reservations WHERE id = ?", (reservation_id,)) is not None:
            raise BudgetRefused("this inference admission already exists")
        slot_id = None
        if execution_attempt_id is not None:
            from daedalus.stores.comparison_funding import (
                ComparisonFunding,  # Lazy: funding uses budget constraints and historical usage.
            )

            if await one(conn, 'SELECT 1 FROM execution_attempts WHERE id = ?', (execution_attempt_id,)) is None:
                raise BudgetRefused('the inference has no host-attested execution attempt')
            slot_id = await ComparisonFunding(self.db).check_slot_in(conn, execution_attempt_id, provider_id,
                                                                   model, quoted_microusd, quote)
        for limit in constraints:
            if not limit.key or type(limit.cap_microusd) is not int or not 0 <= limit.cap_microusd <= 2**63 - 1:
                raise BudgetRefused("a finite integer balance is required")
            spent = await _historical(conn, limit)
            held, covered_slots = await held_in(conn, limit)
            # A funded request consumes its own held pool. A new child/run-specific cap still
            # sees the individual quote; no parent allocation can bypass that narrower cap.
            increment = 0 if slot_id in covered_slots else quoted_microusd
            if spent + held + increment > limit.cap_microusd:
                raise BudgetRefused(f"{limit.key}: the quote exceeds available balance")
        await conn.execute("INSERT INTO inference_reservations(id,provider_id,model,session_id,run_id,"
                           "request_digest,quoted_microusd,rate_version,quote_json,state,created_at,execution_attempt_id,comparison_slot_id)"
                           " VALUES (?,?,?,?,?,?,?,?,?,'reserved',?,?,?)",
                           (reservation_id, provider_id, model, session_id, run_id, request_digest,
                            quoted_microusd, rate_version, canonical(quote), now(), execution_attempt_id, slot_id))
        for limit in constraints:
            await conn.execute("INSERT INTO inference_reservation_scopes(reservation_id,scope_key,cap_microusd)"
                               " VALUES (?,?,?)", (reservation_id, limit.key, limit.cap_microusd))
        return {"reservation_id": reservation_id, "quoted_microusd": quoted_microusd, "state": "reserved"}

    async def start_in(self, conn: aiosqlite.Connection, reservation_id: str) -> None:
        """Persist the send boundary before transport; a restart cannot silently refund it."""
        cursor = await conn.execute("UPDATE inference_reservations SET state = 'inflight',started_at = ?"
                                    " WHERE id = ? AND state = 'reserved'", (now(), reservation_id))
        changed = cursor.rowcount
        await cursor.close()
        if changed != 1:
            raise BudgetRefused("the inference reservation cannot start twice")

    async def settle_in(self, conn: aiosqlite.Connection, reservation_id: str, usage_event_seq: int) -> str:
        """Settle with the actual usage row in its transaction, preserving provider overcharges."""
        reservation = await one(conn, "SELECT * FROM inference_reservations WHERE id = ?", (reservation_id,))
        usage = await one(conn, "SELECT * FROM usage_events WHERE seq = ?", (usage_event_seq,))
        if (reservation is None or usage is None or usage["provider_id"] != reservation["provider_id"] or
                usage["model"] != reservation["model"] or usage["session_id"] != reservation["session_id"] or
                usage["run_id"] != reservation["run_id"] or usage["inference_reservation_id"] != reservation_id):
            raise BudgetRefused("the usage observation belongs to another admission")
        if reservation["state"] in ("settled", "overrun"):
            if reservation["usage_event_seq"] != usage_event_seq:
                raise BudgetRefused("the reservation already has a different usage observation")
            return reservation["state"]
        if reservation["state"] not in ("inflight", "unknown"):
            raise BudgetRefused("an unsent admission cannot settle provider usage")
        raw = json.loads(usage["raw"])
        has_counts = isinstance(raw, dict) and (
            ("prompt_tokens" in raw and "completion_tokens" in raw) or
            ("input_tokens" in raw and "output_tokens" in raw))
        has_cost = isinstance(raw, dict) and raw.get("cost") is not None
        local = json.loads(reservation["quote_json"]).get("provider_kind") == "llamacpp" and usage["cost_usd"] == 0
        if usage["cost_usd"] is None or not (has_counts or has_cost or local):
            await conn.execute("UPDATE inference_reservations SET state = 'unknown',last_error = ? WHERE id = ?",
                               ("the provider did not supply complete priced usage", reservation_id))
            return "unknown"
        cost = microusd(usage["cost_usd"])
        state = "overrun" if cost > reservation["quoted_microusd"] else "settled"
        await conn.execute("UPDATE inference_reservations SET state = ?,actual_microusd = ?,"
                           "usage_event_seq = ?,settled_at = ?,last_error = ? WHERE id = ?",
                           (state, cost, usage_event_seq, now(), "provider usage exceeded its quote" if state == "overrun" else "",
                            reservation_id))
        return state

    async def unknown_in(self, conn: aiosqlite.Connection, reservation_id: str, reason: str) -> None:
        await conn.execute("UPDATE inference_reservations SET state = 'unknown',last_error = ?"
                           " WHERE id = ? AND state = 'inflight'", (reason[:1000], reservation_id))

    async def cancel_unstarted_in(self, conn: aiosqlite.Connection, reservation_id: str, reason: str) -> None:
        cursor = await conn.execute("UPDATE inference_reservations SET state = 'released',last_error = ?"
                                    " WHERE id = ? AND state = 'reserved'", (reason[:1000], reservation_id))
        changed = cursor.rowcount
        await cursor.close()
        if changed != 1:
            raise BudgetRefused("only an admission that never crossed the send boundary can be refunded")
