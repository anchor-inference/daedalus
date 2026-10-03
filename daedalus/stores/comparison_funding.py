"""Prepay a pair once; its requests consume that allocation without reserving it twice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import aiosqlite

from daedalus.config import PROVIDER_KINDS
from daedalus.stores.control import canonical, now, one
from daedalus.stores.database import Database
from daedalus.stores.inference_budget import BudgetRefused, Constraint, _historical


@dataclass(frozen=True)
class PairAllocation:
    """The host's priced model and exact worker for one half of a comparison."""

    id: str
    slot: int
    staff_id: str
    provider_id: str
    model: str
    allowance_microusd: int
    rate_version: str
    quote: dict[str, Any]


async def physical_exit_in(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    proof = await one(conn, "SELECT 1 FROM execution_attempts a JOIN runtime_exit_observations e"
                      " ON e.attempt_id = a.id AND e.staff_session_id = a.staff_session_id"
                      " AND e.contract_revision = a.contract_revision AND e.host_generation = a.host_generation"
                      " AND e.provider_session_ref = a.provider_session_ref AND e.runtime_kind = a.runtime_kind"
                      " WHERE a.id = ? AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                      " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance"
                      " AND a.provider_session_ref = 'terminal:' || e.runtime_ref))", (attempt_id,))
    return proof is not None


async def pool_balances_in(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Unknown requests stay inside their prepaid pool, including after a price overrun."""
    async with conn.execute(
        "SELECT s.*,coalesce(sum(CASE WHEN r.state IN ('settled','overrun') THEN r.actual_microusd ELSE 0 END),0) AS charged,"
        "coalesce(sum(CASE WHEN r.state IN ('reserved','inflight','unknown') THEN r.quoted_microusd ELSE 0 END),0) AS open_quotes,"
        "coalesce(sum(CASE WHEN r.state = 'unknown' THEN r.quoted_microusd ELSE 0 END),0) AS uncertain,"
        "sum(r.state = 'unknown') AS uncertain_count FROM comparison_funding_slots s"
        " LEFT JOIN inference_reservations r ON r.comparison_slot_id = s.id WHERE s.state = 'held' GROUP BY s.id"
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row['held'] = max(0, row['allowance_microusd'] - row['charged'], row['open_quotes'])
        async with conn.execute("SELECT scope_key FROM comparison_funding_scopes WHERE slot_id = ?", (row['id'],)) as cursor:
            row['scopes'] = {item['scope_key'] for item in await cursor.fetchall()}
    return rows


def pool_matches(row: dict[str, Any], limit: Constraint) -> bool:
    if limit.provider_id is not None and row['provider_id'] != limit.provider_id:
        return False
    if limit.run_id is not None or limit.session_ids:
        return limit.key in row['scopes']
    return True


class ComparisonFunding:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def reserve_pair_in(self, conn: aiosqlite.Connection, *, group_id: str,
                              allocations: tuple[PairAllocation, PairAllocation],
                              constraints: tuple[Constraint, ...], host_generation: int) -> list[dict[str, Any]]:
        """Called under the launch queue's capacity lock in the authorized command transaction."""
        from daedalus.stores.inference_budget import held_in  # Lazy: both owners share one ledger snapshot.

        if len(allocations) != 2 or {item.slot for item in allocations} != {1, 2} or len({item.id for item in allocations}) != 2:
            raise BudgetRefused('a comparison reserves exactly two distinct slots')
        if len({limit.key for limit in constraints}) != len(constraints):
            raise BudgetRefused('each applicable balance must be supplied once')
        group = await one(conn, "SELECT g.*,t.project_id,t.contract_revision AS current_contract"
                          " FROM comparison_groups g JOIN board_tasks t ON t.id = g.task_id WHERE g.id = ?", (group_id,))
        generation = await one(conn, "SELECT value FROM kv WHERE key = 'execution_host_generation'")
        if (group is None or group['state'] != 'planned' or not group['project_id']
                or group['contract_revision'] != group['current_contract'] or generation is None
                or type(host_generation) is not int or json.loads(generation['value']) != host_generation):
            raise BudgetRefused('the pair has no current host and task contract')
        if await one(conn, "SELECT 1 FROM comparison_funding_slots WHERE group_id = ?", (group_id,)):
            raise BudgetRefused('the comparison was already funded')
        for item in allocations:
            member = await one(conn, "SELECT project_id,harness,isolation,archived_at FROM staff WHERE id = ?", (item.staff_id,))
            if (member is None or member['project_id'] != group['project_id'] or member['archived_at']
                    or member['harness'] != 'daedalus' or member['isolation'] != 'worktree'):
                raise BudgetRefused('priced comparisons require native workers in isolated worktrees')
            async with conn.execute("SELECT attempt_id FROM comparison_funding_slots WHERE staff_id = ? AND state = 'held'", (item.staff_id,)) as cursor:
                prior = await cursor.fetchall()
            for allocation in prior:
                if allocation['attempt_id'] is None or not await physical_exit_in(conn, allocation['attempt_id']):
                    raise BudgetRefused('another comparison still owns this worker allocation')
            if not item.id or not item.provider_id or not item.model or type(item.allowance_microusd) is not int or not 0 < item.allowance_microusd <= 2**63 - 1:
                raise BudgetRefused('each alternative needs a finite positive allocation')
            if (hashlib.sha256(canonical(item.quote).encode()).hexdigest() != item.rate_version
                    or item.quote.get('model') != item.model or item.quote.get('provider_id') != item.provider_id):
                raise BudgetRefused('the alternative needs its pinned host quote')
            kind = item.quote.get('provider_kind')
            if kind not in PROVIDER_KINDS:
                raise BudgetRefused('this provider has no bounded host inference admission')
            try:
                rates = [Decimal(str(item.quote[key])) for key in ('input_rate', 'output_rate')]
            except (KeyError, InvalidOperation) as exc:
                raise BudgetRefused('the allocation needs complete known prices') from exc
            if any(not rate.is_finite() or not 0 <= rate <= 2**63 - 1 for rate in rates):
                raise BudgetRefused('the allocation needs complete known prices')
            if kind == 'llamacpp' and any(rates):
                raise BudgetRefused('local inference must have its known zero hosted charge')
            bound = item.quote.get('input_bound')
            output = item.quote.get('output_bound')
            if (type(bound) is not int or bound < (0 if kind == 'llamacpp' else 1)
                    or bound > 2**63 - 1 or type(output) is not int or output < 1 or not item.quote.get('bound_source')):
                raise BudgetRefused('the allocation needs provider input and output ceilings')
            minimum = (rates[0] * bound + rates[1] * output)
            if minimum > item.allowance_microusd:
                raise BudgetRefused('the alternative allocation cannot cover one admitted request')
        if len({item.staff_id for item in allocations}) != 2:
            raise BudgetRefused('the alternatives must own different workers')
        total = sum(item.allowance_microusd for item in allocations)
        if total > group['budget_cap_microusd']:
            raise BudgetRefused('the pair allocations exceed the comparison cap')
        for limit in constraints:
            if type(limit.cap_microusd) is not int or not 0 <= limit.cap_microusd <= 2**63 - 1 or not limit.key:
                raise BudgetRefused('a finite integer balance is required')
            charge = sum(item.allowance_microusd for item in allocations
                         if limit.provider_id is None or item.provider_id == limit.provider_id)
            if await _historical(conn, limit) + (await held_in(conn, limit))[0] + charge > limit.cap_microusd:
                raise BudgetRefused(f'{limit.key}: the pair exceeds available balance')
        result = []
        for item in allocations:
            await conn.execute(
                "INSERT INTO comparison_funding_slots(id,group_id,slot,staff_id,project_id,task_id,contract_revision,"
                "host_generation,provider_id,model,allowance_microusd,rate_version,quote_json,state,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'held',?)",
                (item.id, group_id, item.slot, item.staff_id, group['project_id'], group['task_id'], group['contract_revision'],
                 host_generation, item.provider_id, item.model, item.allowance_microusd, item.rate_version, canonical(item.quote), now()),
            )
            for limit in constraints:
                if limit.provider_id is None or item.provider_id == limit.provider_id:
                    await conn.execute("INSERT INTO comparison_funding_scopes(slot_id,scope_key,cap_microusd) VALUES (?,?,?)",
                                       (item.id, limit.key, limit.cap_microusd))
            result.append({'slot_id': item.id, 'slot': item.slot, 'reserved_microusd': item.allowance_microusd,
                           'rate_version': item.rate_version, 'state': 'held'})
        return result

    async def bind_attempt_in(self, conn: aiosqlite.Connection, slot_id: str, attempt_id: str) -> dict[str, Any]:
        slot = await one(conn, 'SELECT * FROM comparison_funding_slots WHERE id = ?', (slot_id,))
        attempt = await one(conn, "SELECT a.*,s.staff_id FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id WHERE a.id = ?", (attempt_id,))
        if (slot is None or attempt is None or slot['state'] != 'held' or slot['attempt_id'] is not None
                or attempt['task_id'] != slot['task_id'] or attempt['contract_revision'] != slot['contract_revision']
                or attempt['host_generation'] != slot['host_generation'] or attempt['staff_id'] != slot['staff_id']
                or attempt['runtime_kind'] != 'daedalus' or attempt['state'] != 'queued'):
            raise BudgetRefused('the allocation belongs to another execution')
        await conn.execute('UPDATE comparison_funding_slots SET attempt_id = ? WHERE id = ?', (attempt_id, slot_id))
        return dict(slot) | {'attempt_id': attempt_id}

    async def check_slot_in(self, conn: aiosqlite.Connection, attempt_id: str, provider_id: str, model: str,
                            quoted_microusd: int, quote: dict[str, Any]) -> str | None:
        slot = await one(conn, "SELECT s.*,g.state AS group_state,t.contract_revision AS current_contract"
                         " FROM comparison_funding_slots s JOIN comparison_groups g ON g.id = s.group_id"
                         " JOIN board_tasks t ON t.id = s.task_id WHERE s.attempt_id = ?", (attempt_id,))
        if slot is None:
            member = await one(conn, 'SELECT 1 FROM comparison_group_attempts WHERE attempt_id = ?', (attempt_id,))
            if member is not None:
                raise BudgetRefused('the comparison member has no prepaid inference allocation')
            return None
        if (slot['state'] != 'held' or slot['group_state'] not in ('planned', 'active')
                or slot['current_contract'] != slot['contract_revision'] or slot['provider_id'] != provider_id or slot['model'] != model):
            raise BudgetRefused('the inference no longer owns this comparison allocation')
        pinned = json.loads(slot['quote_json'])
        for field in ('provider_kind', 'provider_id', 'model', 'input_bound', 'input_rate', 'output_rate', 'bound_source', 'rate_card'):
            if canonical(quote.get(field)) != canonical(pinned.get(field)):
                raise BudgetRefused('the comparison rate card changed; approve a newly priced pair before inference')
        if type(quote.get('output_bound')) is not int or not 0 < quote['output_bound'] <= pinned['output_bound']:
            raise BudgetRefused('the inference exceeds the pinned comparison output ceiling')
        row = await one(conn, "SELECT coalesce(sum(CASE WHEN state IN ('settled','overrun') THEN actual_microusd"
                        " WHEN state IN ('reserved','inflight','unknown') THEN quoted_microusd ELSE 0 END),0) AS used"
                        " FROM inference_reservations WHERE comparison_slot_id = ?", (slot['id'],))
        if int(row['used']) + quoted_microusd > slot['allowance_microusd']:
            raise BudgetRefused('the alternative quote exceeds its prepaid balance')
        return slot['id']

    async def observed_cost_in(self, conn: aiosqlite.Connection, attempt_id: str) -> int | None:
        slot = await one(conn, 'SELECT id FROM comparison_funding_slots WHERE attempt_id = ?', (attempt_id,))
        if slot is None:
            return None
        if await one(conn, "SELECT 1 FROM inference_reservations WHERE comparison_slot_id = ?"
                     " AND execution_attempt_id IS NOT ? LIMIT 1", (slot['id'], attempt_id)):
            return None
        row = await one(conn, "SELECT count(*) AS count,sum(state NOT IN ('settled','overrun','released')) AS uncertain,"
                        "coalesce(sum(actual_microusd),0) AS cost FROM inference_reservations"
                        " WHERE execution_attempt_id = ?", (attempt_id,))
        return None if row['uncertain'] else int(row['cost'])

    async def start_launch_in(self, conn: aiosqlite.Connection, slot_id: str, attempt_id: str) -> None:
        """Cross a durable boundary before the runtime may create any provider work."""
        cursor = await conn.execute("UPDATE comparison_funding_slots SET launch_started_at = ?"
                                    " WHERE id = ? AND attempt_id = ? AND state = 'held' AND launch_started_at IS NULL",
                                    (now(), slot_id, attempt_id))
        changed = cursor.rowcount
        await cursor.close()
        if changed != 1:
            raise BudgetRefused('the comparison launch boundary cannot be crossed twice')

    async def release_in(self, conn: aiosqlite.Connection, slot_id: str) -> None:
        """An elapsed lease or a logical done report cannot refund a running or unknown send."""
        slot = await one(conn, 'SELECT * FROM comparison_funding_slots WHERE id = ?', (slot_id,))
        if slot is None or slot['state'] != 'held':
            raise BudgetRefused('the comparison allocation is not held')
        if slot['attempt_id'] is not None and not await physical_exit_in(conn, slot['attempt_id']):
            unsent = await one(conn, "SELECT 1 FROM execution_attempts a JOIN staff_sessions s ON s.id = a.staff_session_id"
                               " WHERE a.id = ? AND a.state IN ('failed','cancelled','superseded') AND s.ended_at IS NOT NULL"
                               " AND a.provider_session_ref IS NULL AND a.native_run_id IS NULL"
                               " AND NOT EXISTS (SELECT 1 FROM inference_reservations r WHERE r.execution_attempt_id = a.id OR r.comparison_slot_id = ?)",
                               (slot['attempt_id'], slot_id))
            if slot['launch_started_at'] is not None or unsent is None:
                raise BudgetRefused('the allocation needs its exact host-observed physical exit or unstarted boundary')
        if await one(conn, "SELECT 1 FROM inference_reservations WHERE comparison_slot_id = ?"
                     " AND state IN ('reserved','inflight','unknown') LIMIT 1", (slot_id,)):
            raise BudgetRefused('the allocation still owns an unresolved provider charge')
        await conn.execute("UPDATE comparison_funding_slots SET state = 'released',released_at = ? WHERE id = ?", (now(), slot_id))
