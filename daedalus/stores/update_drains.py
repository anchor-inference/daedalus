"""Fence new work without calling a stop request or an old snapshot a completed drain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite
from protocore.contracts.snapshot import migrate_snapshot

from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, one
from daedalus.stores.runtime_release import attempt_released_in

if TYPE_CHECKING:
    from daedalus.stores.database import Database
    from daedalus.stores.executions import ExecutionStore


class UpdateDrainActive(ControlDenied):
    """New work is fenced by a durable drain, including after its original host exited."""


class UpdateDrainPaused(asyncio.CancelledError):
    """Park native inference with its snapshot instead of settling it as a failed result."""


async def assert_admission_open_in(conn: aiosqlite.Connection) -> None:
    # A new host generation cannot reopen a drain whose launcher disappeared after closing it.
    row = await one(conn, "SELECT id,state FROM update_drains"
                    " WHERE state IN ('draining','blocked','ready','committed') LIMIT 1")
    if row is not None:
        raise UpdateDrainActive(f"update drain {row['id']} keeps new work closed ({row['state']})")


class UpdateDrains:
    def __init__(self, db: Database, executions: ExecutionStore) -> None:
        self.db = db
        self.executions = executions
        self.control = ControlStore(db)

    async def begin(self, principal: Principal, *, policy: str, candidate_bot_sha: str,
                    candidate_core_sha: str, candidate_compatibility_digest: str,
                    expected_host_generation: int,
                    expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        if policy not in ('checkpoint_supported', 'stop_all'):
            raise ValueError('an explicit checkpoint or stop policy is required')
        if (not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', candidate_bot_sha)
                or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', candidate_core_sha)
                or not re.fullmatch(r'[0-9a-f]{64}', candidate_compatibility_digest)):
            raise ValueError('an exact candidate and compatibility digest are required')

        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            generation = await self.executions._host(conn)
            if generation != expected_host_generation:
                raise ControlConflict('the execution host changed before admission could close')
            await assert_admission_open_in(conn)
            async with conn.execute("SELECT id,task_id,contract_revision,host_generation,runtime_kind,state"
                                    " FROM execution_attempts WHERE state IN"
                                    " ('queued','starting','running','waiting','recovering') ORDER BY id") as cursor:
                attempts = [dict(row) for row in await cursor.fetchall()]
            async with conn.execute("SELECT id,tenant_id,session_id,status FROM runs"
                                    " WHERE status IN ('queued','running','paused') ORDER BY id") as cursor:
                runs = [dict(row) for row in await cursor.fetchall()]
            async with conn.execute("SELECT id,receipt_id,attempt_id,kind,state,claim_generation"
                                    " FROM effect_outbox WHERE state IN ('pending','claimed','unknown')"
                                    " ORDER BY id") as cursor:
                receipts = [dict(row) for row in await cursor.fetchall()]
            at = datetime.now(UTC).isoformat()
            await conn.execute("INSERT INTO update_drains(id,host_generation,policy,"
                               "candidate_bot_sha,candidate_core_sha,candidate_compatibility_digest,"
                               "state,admission_closed_at,"
                               "active_attempts_json,active_runs_json,active_receipts_json,receipt_id,created_at,updated_at)"
                               " VALUES (?,?,?,?,?,?,'draining',?,?,?,?,?,?,?)",
                               (mutation.object_id, generation, policy, candidate_bot_sha,
                                candidate_core_sha, candidate_compatibility_digest, at, json.dumps(attempts),
                                json.dumps(runs), json.dumps(receipts), mutation.receipt_id, at, at))
            return {'drain_id': mutation.object_id, 'state': 'draining', 'host_generation': generation,
                    'candidate_bot_sha': candidate_bot_sha, 'candidate_core_sha': candidate_core_sha,
                    'candidate_compatibility_digest': candidate_compatibility_digest,
                    'active_attempts': attempts, 'active_runs': runs, 'active_receipts': receipts, 'checkpoint_refs': [],
                    'admission_open': False}

        return await self.control.mutate(
            principal, Scope('global', 'global'), 'runtime.update.drain', client_operation_id,
            expected_collection_revision, Entity('collection', 'global'),
            {'policy': policy, 'expected_host_generation': expected_host_generation,
             'candidate_bot_sha': candidate_bot_sha, 'candidate_core_sha': candidate_core_sha,
             'candidate_compatibility_digest': candidate_compatibility_digest}, effect,
        )

    async def recheck(self, principal: Principal, *, drain_id: str, expected_host_generation: int,
                      expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        """Authorize another observation without creating a second update admission fence."""
        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            generation = await self.executions._host(conn)
            drain = await one(conn, 'SELECT * FROM update_drains WHERE id = ?', (drain_id,))
            if drain is None:
                raise KeyError(drain_id)
            if generation != expected_host_generation or generation != drain['host_generation']:
                raise ControlConflict('the draining host changed; explicit recovery is required')
            if drain['state'] not in ('draining', 'blocked', 'ready'):
                raise ControlConflict('this drain no longer accepts runtime observations')
            await conn.execute("UPDATE update_drains SET state = 'draining',updated_at = ? WHERE id = ?",
                               (datetime.now(UTC).isoformat(), drain_id))
            return {'drain_id': drain_id, 'state': 'draining', 'host_generation': generation, 'admission_open': False}

        return await self.control.mutate(
            principal, Scope('global', 'global'), 'runtime.update.recheck', client_operation_id,
            expected_collection_revision, Entity('collection', 'global'),
            {'drain_id': drain_id, 'expected_host_generation': expected_host_generation}, effect,
        )

    async def assess(self, principal: Principal, *, drain_id: str, parked_run_ids: set[str],
                     expected_host_generation: int, expected_collection_revision: int,
                     client_operation_id: str, unconfirmed_run_ids: set[str] | None = None) -> dict[str, Any]:
        """Record host-observed quiescence; saved bytes alone cannot attest a stopped runtime.

        ``parked_run_ids`` comes from the host manager after its tasks and persistence
        finished, never from an HTTP body or an agent's account of what it stopped.
        """
        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            generation = await self.executions._host(conn)
            drain = await one(conn, 'SELECT * FROM update_drains WHERE id = ?', (drain_id,))
            if drain is None:
                raise KeyError(drain_id)
            if generation != expected_host_generation or generation != drain['host_generation']:
                raise ControlConflict('the draining host changed; explicit recovery is required')
            if drain['state'] not in ('draining', 'blocked', 'ready'):
                raise ControlConflict('this drain no longer accepts checkpoint observations')
            checkpoints = []
            blockers = ['native run ' + run_id + ' has not finished its runtime settlement'
                        for run_id in sorted(unconfirmed_run_ids or ())]
            async with conn.execute("SELECT id,tenant_id,session_id,status FROM runs"
                                    " WHERE status IN ('queued','running','paused') ORDER BY id") as cursor:
                runs = await cursor.fetchall()
            for run in runs:
                snapshot = await one(conn, 'SELECT snapshot,state,session_id,updated_at FROM snapshots WHERE run_id = ?',
                                     (run['id'],))
                if (drain['policy'] != 'checkpoint_supported' or run['id'] not in parked_run_ids
                        or run['status'] != 'paused' or snapshot is None
                        or snapshot['session_id'] != run['session_id']
                        or snapshot['state'] not in ('running', 'compacting', 'pending', 'awaiting')):
                    blockers.append('native run ' + run['id'] + ' has no confirmed parked checkpoint')
                    continue
                try:
                    saved = json.loads(snapshot['snapshot'])
                except (TypeError, ValueError):
                    blockers.append('native run ' + run['id'] + ' has an unreadable checkpoint')
                    continue
                try:
                    saved = migrate_snapshot(saved)
                except ValueError:
                    blockers.append('native run ' + run['id'] + ' has an unsupported checkpoint schema')
                    continue
                if (saved.get('run_id') != run['id'] or saved.get('session_id') != run['session_id']
                        or saved.get('tenant_id') != run['tenant_id'] or not saved.get('model_name')):
                    blockers.append('native run ' + run['id'] + ' has a checkpoint with a different identity')
                    continue
                # A parked Python task does not prove that a Bash command or another
                # side effect it dispatched has left the external runtime.
                if saved.get('background_task_ids'):
                    blockers.append('native run ' + run['id'] + ' still owns background commands')
                    continue
                intents = saved.get('open_intents') or []
                if (not isinstance(intents, list) or any(
                        not isinstance(intent, dict) or intent.get('state') == 'DISPATCHED'
                        or intent.get('outcome') == 'unknown' for intent in intents)):
                    blockers.append('native run ' + run['id'] + ' has an unconfirmed tool outcome')
                    continue
                checkpoints.append({'run_id': run['id'], 'session_id': run['session_id'],
                                    'sha256': hashlib.sha256(snapshot['snapshot'].encode()).hexdigest(),
                                    'updated_at': snapshot['updated_at']})
            checkpoint_ids = {row['run_id'] for row in checkpoints}
            async with conn.execute('SELECT id,runtime_kind,native_run_id FROM execution_attempts ORDER BY id') as cursor:
                attempts = await cursor.fetchall()
            for attempt in attempts:
                if await attempt_released_in(conn, attempt['id']):
                    continue
                if attempt['runtime_kind'] == 'daedalus' and attempt['native_run_id'] in checkpoint_ids:
                    continue
                blockers.append('attempt ' + attempt['id'] + ' has no confirmed runtime exit or parked checkpoint')
            # Pending work stays durable for reopening. A claimed or uncertain effect may
            # still be entering a runtime and cannot be certified by its missing reference.
            async with conn.execute("SELECT id FROM effect_outbox WHERE state IN ('claimed','unknown') ORDER BY id") as cursor:
                for row in await cursor.fetchall():
                    blockers.append('effect ' + row['id'] + ' has an unconfirmed physical outcome')
            async with conn.execute("SELECT id FROM inference_reservations WHERE state = 'inflight' ORDER BY id") as cursor:
                for row in await cursor.fetchall():
                    blockers.append('inference ' + row['id'] + ' is still in flight')
            state = 'blocked' if blockers else 'ready'
            blocker = '; '.join(blockers) if blockers else None
            await conn.execute('UPDATE update_drains SET state = ?,checkpoint_refs_json = ?,blocker = ?,updated_at = ?'
                               ' WHERE id = ?', (state, json.dumps(checkpoints), blocker,
                                                  datetime.now(UTC).isoformat(), drain_id))
            return {'drain_id': drain_id, 'state': state, 'host_generation': generation,
                    'checkpoint_refs': checkpoints, 'blockers': blockers, 'admission_open': False}

        return await self.control.mutate(
            principal, Scope('global', 'global'), 'runtime.update.assess', client_operation_id,
            expected_collection_revision, Entity('collection', 'global'),
            {'drain_id': drain_id, 'expected_host_generation': expected_host_generation}, effect,
        )

    async def decide(self, principal: Principal, *, drain_id: str, decision: str,
                     candidate_bot_sha: str, candidate_core_sha: str,
                     candidate_compatibility_digest: str, checkpoint_refs_digest: str,
                     expected_host_generation: int, expected_collection_revision: int,
                     client_operation_id: str) -> dict[str, Any]:
        """Commit a verified checkpoint or abort before replacement of its host.

        A committed update cannot be reopened by its old host. A successor must
        reconcile the saved checkpoint and the actual installed build separately.
        """
        if decision not in ('commit', 'abort'):
            raise ValueError('an explicit commit or abort decision is required')

        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            generation = await self.executions._host(conn)
            drain = await one(conn, 'SELECT * FROM update_drains WHERE id = ?', (drain_id,))
            if drain is None:
                raise KeyError(drain_id)
            if generation != expected_host_generation or generation != drain['host_generation']:
                raise ControlConflict('the draining host changed; explicit recovery is required')
            if (candidate_bot_sha != drain['candidate_bot_sha'] or candidate_core_sha != drain['candidate_core_sha']
                    or candidate_compatibility_digest != drain['candidate_compatibility_digest']):
                raise ControlConflict('the candidate changed after update preparation')
            references = json.loads(drain['checkpoint_refs_json'])
            actual_digest = hashlib.sha256(json.dumps(references, sort_keys=True,
                                                       separators=(',', ':')).encode()).hexdigest()
            if checkpoint_refs_digest != actual_digest:
                raise ControlConflict('the checkpoint references changed after candidate verification')
            if decision == 'abort':
                if drain['state'] not in ('draining', 'blocked', 'ready'):
                    raise ControlConflict('a committed update requires successor recovery')
                state = 'aborted'
            else:
                if drain['state'] != 'ready':
                    raise ControlConflict('the update has no confirmed quiescent checkpoint')
                for reference in references:
                    snapshot = await one(conn, 'SELECT snapshot,session_id,updated_at FROM snapshots WHERE run_id = ?',
                                         (reference['run_id'],))
                    if (snapshot is None or snapshot['session_id'] != reference['session_id']
                            or snapshot['updated_at'] != reference['updated_at']
                            or hashlib.sha256(snapshot['snapshot'].encode()).hexdigest() != reference['sha256']):
                        raise ControlConflict('a verified checkpoint changed before update commitment')
                reference_ids = {reference['run_id'] for reference in references}
                async with conn.execute("SELECT id,status FROM runs WHERE status IN ('queued','running','paused')") as cursor:
                    for run in await cursor.fetchall():
                        if run['status'] != 'paused' or run['id'] not in reference_ids:
                            raise ControlConflict('a native run changed after checkpoint verification')
                async with conn.execute('SELECT id,runtime_kind,native_run_id FROM execution_attempts') as cursor:
                    for attempt in await cursor.fetchall():
                        if await attempt_released_in(conn, attempt['id']):
                            continue
                        if attempt['runtime_kind'] != 'daedalus' or attempt['native_run_id'] not in reference_ids:
                            raise ControlConflict('an attempt still needs physical confirmation before commitment')
                pending = await one(conn, "SELECT id FROM effect_outbox WHERE state IN ('claimed','unknown') LIMIT 1")
                inflight = await one(conn, "SELECT id FROM inference_reservations WHERE state = 'inflight' LIMIT 1")
                if pending is not None or inflight is not None:
                    raise ControlConflict('a physical effect still needs confirmation before update commitment')
                state = 'committed'
            await conn.execute('UPDATE update_drains SET state = ?,commit_receipt_id = ?,updated_at = ? WHERE id = ?',
                               (state, mutation.receipt_id if state == 'committed' else None,
                                datetime.now(UTC).isoformat(), drain_id))
            return {'drain_id': drain_id, 'state': state, 'host_generation': generation,
                    'candidate_bot_sha': candidate_bot_sha, 'candidate_core_sha': candidate_core_sha,
                    'candidate_compatibility_digest': candidate_compatibility_digest,
                    'commit_receipt_id': mutation.receipt_id if state == 'committed' else None,
                    'admission_open': state == 'aborted'}

        return await self.control.mutate(
            principal, Scope('global', 'global'), 'runtime.update.' + decision, client_operation_id,
            expected_collection_revision, Entity('collection', 'global'),
            {'drain_id': drain_id, 'decision': decision, 'expected_host_generation': expected_host_generation,
             'candidate_bot_sha': candidate_bot_sha, 'candidate_core_sha': candidate_core_sha,
             'candidate_compatibility_digest': candidate_compatibility_digest,
             'checkpoint_refs_digest': checkpoint_refs_digest}, effect,
        )

    async def recover(self, principal: Principal, *, drain_id: str, commit_receipt_id: str,
                      installed_bot_sha: str, installed_core_sha: str, expected_host_generation: int,
                      expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        """Open a committed drain only on its installed successor and intact parked work."""
        async def effect(conn: aiosqlite.Connection, mutation: Any) -> dict[str, Any]:
            generation = await self.executions._host(conn)
            drain = await one(conn, 'SELECT * FROM update_drains WHERE id = ?', (drain_id,))
            if drain is None:
                raise KeyError(drain_id)
            if (generation != expected_host_generation or generation <= drain['host_generation']
                    or drain['state'] != 'committed'):
                raise ControlConflict('the committed drain has no current successor')
            if (commit_receipt_id != drain['commit_receipt_id'] or not commit_receipt_id
                    or installed_bot_sha != drain['candidate_bot_sha']
                    or installed_core_sha != drain['candidate_core_sha']):
                raise ControlConflict('the installed revision does not match the committed update')
            try:
                references = json.loads(drain['checkpoint_refs_json'])
            except (TypeError, ValueError) as exc:
                raise ControlConflict('the committed checkpoint list is unreadable') from exc
            if not isinstance(references, list) or (drain['policy'] == 'stop_all' and references):
                raise ControlConflict('the committed checkpoint policy changed')
            run_ids: set[str] = set()
            for reference in references:
                if (not isinstance(reference, dict) or not isinstance(reference.get('run_id'), str)
                        or reference['run_id'] in run_ids):
                    raise ControlConflict('the committed checkpoint list is invalid')
                run_ids.add(reference['run_id'])
                run = await one(conn, 'SELECT tenant_id,session_id,status FROM runs WHERE id = ?',
                                (reference['run_id'],))
                snapshot = await one(conn, 'SELECT snapshot,session_id,updated_at FROM snapshots WHERE run_id = ?',
                                     (reference['run_id'],))
                if (run is None or run['status'] != 'paused' or snapshot is None
                        or run['session_id'] != reference.get('session_id')
                        or snapshot['session_id'] != reference.get('session_id')
                        or snapshot['updated_at'] != reference.get('updated_at')
                        or hashlib.sha256(snapshot['snapshot'].encode()).hexdigest() != reference.get('sha256')):
                    raise ControlConflict('a committed checkpoint changed before successor recovery')
                try:
                    saved = migrate_snapshot(json.loads(snapshot['snapshot']))
                except (TypeError, ValueError) as exc:
                    raise ControlConflict('a committed checkpoint is unreadable by the successor') from exc
                intents = saved.get('open_intents') or []
                if (saved.get('run_id') != reference['run_id']
                        or saved.get('session_id') != run['session_id']
                        or saved.get('tenant_id') != run['tenant_id'] or not saved.get('model_name')
                        or saved.get('background_task_ids') or not isinstance(intents, list)
                        or any(not isinstance(intent, dict) or intent.get('state') == 'DISPATCHED'
                               or intent.get('outcome') == 'unknown' for intent in intents)):
                    raise ControlConflict('a committed checkpoint cannot safely resume')
            async with conn.execute("SELECT id,status FROM runs WHERE status IN ('queued','running','paused')") as cursor:
                for run in await cursor.fetchall():
                    if run['status'] != 'paused' or run['id'] not in run_ids:
                        raise ControlConflict('an unverified native run remains during successor recovery')
            async with conn.execute('SELECT id,runtime_kind,native_run_id FROM execution_attempts') as cursor:
                for attempt in await cursor.fetchall():
                    if await attempt_released_in(conn, attempt['id']):
                        continue
                    if (drain['policy'] != 'checkpoint_supported' or attempt['runtime_kind'] != 'daedalus'
                            or attempt['native_run_id'] not in run_ids):
                        raise ControlConflict('an unverified execution attempt remains during successor recovery')
            if (await one(conn, "SELECT id FROM effect_outbox WHERE state IN ('claimed','unknown') LIMIT 1")
                    or await one(conn, "SELECT id FROM inference_reservations WHERE state = 'inflight' LIMIT 1")):
                raise ControlConflict('a physical effect remains uncertain during successor recovery')
            await conn.execute("UPDATE update_drains SET state = 'resumed',updated_at = ? WHERE id = ?",
                               (datetime.now(UTC).isoformat(), drain_id))
            return {'drain_id': drain_id, 'state': 'resumed', 'host_generation': generation,
                    'checkpoint_refs': references, 'admission_open': True}

        return await self.control.mutate(
            principal, Scope('global', 'global'), 'runtime.update.recover', client_operation_id,
            expected_collection_revision, Entity('collection', 'global'),
            {'drain_id': drain_id, 'commit_receipt_id': commit_receipt_id,
             'installed_bot_sha': installed_bot_sha, 'installed_core_sha': installed_core_sha,
             'expected_host_generation': expected_host_generation}, effect,
        )

    async def read(self) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM update_drains ORDER BY created_at DESC LIMIT 1")
        if row is None:
            return {'admission_open': True, 'drain': None}
        result = dict(row)
        for key in ('active_attempts', 'active_runs', 'active_receipts', 'checkpoint_refs'):
            result[key] = json.loads(result.pop(key + '_json'))
        return {'admission_open': row['state'] in ('resumed', 'aborted'), 'drain': result}
