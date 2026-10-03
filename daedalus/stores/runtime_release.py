"""Release execution ownership only from physical exit or an attested unentered boundary."""

from __future__ import annotations

import aiosqlite

from daedalus.stores.control import one


async def physical_exit_in(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    proof = await one(conn, "SELECT 1 FROM execution_attempts a JOIN runtime_exit_observations e"
                      " ON e.attempt_id = a.id AND e.staff_session_id = a.staff_session_id"
                      " AND e.contract_revision = a.contract_revision AND e.host_generation = a.host_generation"
                      " AND e.provider_session_ref = a.provider_session_ref AND e.runtime_kind = a.runtime_kind"
                      " WHERE a.id = ? AND ((a.runtime_kind = 'daedalus' AND e.runtime_ref = a.native_run_id)"
                      " OR (a.runtime_kind = 'cli' AND e.runtime_instance = a.runtime_instance"
                      " AND a.provider_session_ref = 'terminal:' || e.runtime_ref))", (attempt_id,))
    return proof is not None


async def no_entry_in(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    """An empty runtime reference alone cannot prove that a process was never created."""
    proof = await one(conn, "SELECT 1 FROM runtime_no_entry_observations e"
                      " JOIN execution_attempts a ON a.id = e.attempt_id"
                      " JOIN staff_sessions s ON s.id = a.staff_session_id"
                      " WHERE a.id = ? AND e.staff_session_id = a.staff_session_id"
                      " AND e.contract_revision = a.contract_revision AND e.host_generation = a.host_generation"
                      " AND e.runtime_kind = a.runtime_kind AND a.runtime_entered_at IS NULL"
                      " AND a.provider_session_ref IS NULL AND a.native_run_id IS NULL"
                      " AND a.state IN ('failed','cancelled','superseded') AND s.ended_at IS NOT NULL"
                      " AND s.session_id IS NULL AND s.terminal_id IS NULL"
                      " AND NOT EXISTS (SELECT 1 FROM inference_reservations r WHERE r.execution_attempt_id = a.id"
                      " OR r.comparison_slot_id IN (SELECT id FROM comparison_funding_slots WHERE attempt_id = a.id))"
                      " AND NOT EXISTS (SELECT 1 FROM comparison_funding_slots f WHERE f.attempt_id = a.id"
                      " AND f.launch_started_at IS NOT NULL)", (attempt_id,))
    return proof is not None


async def attempt_released_in(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    return await physical_exit_in(conn, attempt_id) or await no_entry_in(conn, attempt_id)
