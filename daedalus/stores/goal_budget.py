"""Attribute priced inference to one project budget across goal and session revisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import aiosqlite

from daedalus.stores.control import now, one
from daedalus.stores.inference_budget import BudgetRefused, Constraint, _historical, held_in, microusd


@dataclass(frozen=True)
class GoalCharge:
    project_id: str
    root_session_id: str
    coordinator: bool
    goal_revision: int
    budget_id: str


def money(value: str) -> int:
    """An operator's amount is exact to a microdollar before any receipt is written."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", value):
        raise ValueError("budget amounts need nonnegative decimal strings with at most six places")
    return microusd(value)


def usd(amount: int) -> str:
    return f"{Decimal(amount) / Decimal(1_000_000):.6f}"


def _constraints(row: aiosqlite.Row, coordinator: bool) -> tuple[Constraint, ...]:
    limits = [Constraint(f"goal:{row['budget_id']}", row["limit_microusd"], scope_only=True)]
    if coordinator:
        limits.append(Constraint(f"goalcoord:{row['budget_id']}", row["coordination_limit_microusd"],
                                 scope_only=True))
    return tuple(limits)


async def goal_constraints_in(conn: aiosqlite.Connection, project_id: str,
                              *, coordinator: bool) -> tuple[Constraint, ...]:
    row = await one(conn, "SELECT * FROM project_goal_budgets WHERE project_id = ?", (project_id,))
    return _constraints(row, coordinator) if row is not None else ()


async def charge_for_session_in(conn: aiosqlite.Connection, session_id: str) -> GoalCharge | None:
    """Follow host-persisted ancestry to the nearest project with a goal budget.

    Gaps in that bookkeeping charge what can still be attributed instead of refusing the call: a
    removed ancestor ends the walk, a parent in another project is not this project's spend, a
    removed project has no budget, and a retired coordinator still running is charged as ordinary
    work. Each of those used to refuse every call of the session for good.
    """
    current = session_id
    seen: set[str] = set()
    project_id: str | None = None
    root: aiosqlite.Row | None = None
    while current:
        if current in seen or len(seen) >= 256:
            raise BudgetRefused("the session's project spending lineage is cyclic or too deep")
        seen.add(current)
        row = await one(conn, "SELECT id,project_id,metadata FROM sessions WHERE id = ?", (current,))
        if row is None:
            break
        if row["project_id"]:
            if project_id is not None and project_id != row["project_id"]:
                break
            project_id = row["project_id"]
        root = row
        current = str(json.loads(row["metadata"]).get("subagent_of") or "")
    if project_id is None or root is None:
        return None
    budget = await one(conn, "SELECT budget_id FROM project_goal_budgets WHERE project_id = ?", (project_id,))
    if budget is None:
        return None
    project = await one(conn, "SELECT settings,goal_revision FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return None
    metadata = json.loads(root["metadata"])
    office = json.loads(project["settings"]).get("orchestrator", {})
    coordinator = (bool(office.get("enabled")) and office.get("session_id") == root["id"]
                   and metadata.get("orchestrator_of") == project_id)
    # A retired or foreign office cannot spend the current coordination allowance, but it is still
    # the project's work and counts against the whole goal budget.
    return GoalCharge(project_id, root["id"], coordinator, project["goal_revision"], budget["budget_id"])


async def pin_reservation_in(conn: aiosqlite.Connection, reservation_id: str,
                             charge: GoalCharge) -> None:
    """Keep the exact goal revision and class beside the already reserved immutable scope."""
    expected = {limit.key for limit in await goal_constraints_in(conn, charge.project_id,
                                                                 coordinator=charge.coordinator)}
    scopes = await conn.execute("SELECT scope_key FROM inference_reservation_scopes WHERE reservation_id = ?",
                                (reservation_id,))
    try:
        actual = {row["scope_key"] for row in await scopes.fetchall()}
    finally:
        await scopes.close()
    if not expected or not expected <= actual:
        raise BudgetRefused("the inference did not reserve its project goal budget")
    project = await one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (charge.project_id,))
    if project is None or project["goal_revision"] != charge.goal_revision:
        raise BudgetRefused("the project goal changed before spending was attributed")
    await conn.execute("INSERT INTO goal_budget_admissions(reservation_id,budget_id,project_id,goal_revision,"
                       "charge_class,root_session_id,attributed_at) VALUES (?,?,?,?,?,?,?)",
                       (reservation_id, charge.budget_id, charge.project_id, charge.goal_revision,
                        "coordination" if charge.coordinator else "work", charge.root_session_id, now()))


async def pin_allocation_in(conn: aiosqlite.Connection, slot_id: str, project_id: str) -> None:
    budget = await one(conn, "SELECT budget_id FROM project_goal_budgets WHERE project_id = ?", (project_id,))
    if budget is None:
        return
    slot = await one(conn, "SELECT project_id FROM comparison_funding_slots WHERE id = ?", (slot_id,))
    project = await one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
    scope = await one(conn, "SELECT 1 FROM comparison_funding_scopes WHERE slot_id = ? AND scope_key = ?",
                      (slot_id, f"goal:{budget['budget_id']}"))
    if slot is None or slot["project_id"] != project_id or project is None or scope is None:
        raise BudgetRefused("the comparison allocation lacks its project goal reservation")
    await conn.execute("INSERT INTO goal_budget_allocations(slot_id,budget_id,project_id,goal_revision,attributed_at)"
                       " VALUES (?,?,?,?,?)",
                       (slot_id, budget["budget_id"], project_id, project["goal_revision"], now()))


async def view_in(conn: aiosqlite.Connection, project_id: str) -> dict[str, Any] | None:
    row = await one(conn, "SELECT * FROM project_goal_budgets WHERE project_id = ?", (project_id,))
    if row is None:
        return None
    balances: dict[str, dict[str, Any]] = {}
    for label, limit in (("total", _constraints(row, False)[0]), ("coordination", _constraints(row, True)[1])):
        held, _ = await held_in(conn, limit)
        unknown = await one(conn, "SELECT coalesce(sum(r.quoted_microusd),0) AS amount"
                            " FROM inference_reservations r JOIN inference_reservation_scopes s"
                            " ON s.reservation_id = r.id WHERE s.scope_key = ? AND r.state = 'unknown'"
                            " AND r.comparison_slot_id IS NULL", (limit.key,))
        uncertain = unknown["amount"] if unknown is not None else 0
        from daedalus.stores.comparison_funding import (
            pool_balances_in,  # Lazy: funding imports goal constraints; this read shares its balance snapshot.
        )

        uncertain += sum(pool["uncertain"] for pool in await pool_balances_in(conn)
                         if limit.key in pool["scopes"])
        try:
            spent = await _historical(conn, limit)
            available = usd(max(0, limit.cap_microusd - spent - held))
            state = "uncertain" if uncertain else "known"
        except BudgetRefused:
            spent = None
            available = None
            state = "unknown_usage"
        balances[label] = {"limit_usd": usd(limit.cap_microusd),
                           "spent_usd": usd(spent) if spent is not None else None,
                           "held_usd": usd(held), "uncertain_usd": usd(uncertain),
                           "available_usd": available, "state": state}
    project = await one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
    return {"project_id": project_id, "budget_id": row["budget_id"],
            "activated_at": row["activated_at"], "activated_goal_revision": row["activated_goal_revision"],
            "current_goal_revision": project["goal_revision"] if project else None,
            "total": balances["total"], "coordination": balances["coordination"]}


async def set_budget_in(conn: aiosqlite.Connection, *, project_id: str, budget_id: str,
                        expected_goal_revision: int, limit_usd: str,
                        coordination_limit_usd: str) -> dict[str, Any]:
    total, coordination = money(limit_usd), money(coordination_limit_usd)
    if coordination > total:
        raise ValueError("the coordination limit cannot exceed the project goal limit")
    project = await one(conn, "SELECT goal_revision FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise KeyError(project_id)
    if project["goal_revision"] != expected_goal_revision:
        raise BudgetRefused("the project goal revision changed")
    existing = await one(conn, "SELECT budget_id FROM project_goal_budgets WHERE project_id = ?", (project_id,))
    if existing is None:
        # Work already running when the cap is set goes on, and its calls count from the next
        # one. Refusing to set a budget until every worker had stopped and every lost reply had
        # settled (which an unknown one never does) left the project with no cap at all.
        at = now()
        await conn.execute("INSERT INTO project_goal_budgets(project_id,budget_id,limit_microusd,"
                           "coordination_limit_microusd,activated_goal_revision,activated_at,updated_at)"
                           " VALUES (?,?,?,?,?,?,?)",
                           (project_id, budget_id, total, coordination, expected_goal_revision, at, at))
    else:
        await conn.execute("UPDATE project_goal_budgets SET limit_microusd = ?,"
                           "coordination_limit_microusd = ?,updated_at = ? WHERE project_id = ?",
                           (total, coordination, now(), project_id))
    view = await view_in(conn, project_id)
    assert view is not None
    return view

