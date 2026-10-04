"""Checkpoint the exact native runs captured by a durable update admission fence."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from daedalus.stores.control import ControlConflict, Entity, Principal, Scope
from daedalus.stores.lifecycle import LifecycleRefused
from daedalus.stores.update_drains import UpdateDrains

if TYPE_CHECKING:
    from daedalus.app import Application


class UpdateDrainRuntime:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.drains = UpdateDrains(app.db, app.executions)

    async def prepare(self, principal: Principal, **request: Any) -> dict[str, Any]:
        command = await self.drains.begin(principal, **request)
        return await self._observe(principal, command, request)

    async def recheck(self, principal: Principal, **request: Any) -> dict[str, Any]:
        command = await self.drains.recheck(principal, **request)
        return await self._observe(principal, command, request)

    async def _observe(self, principal: Principal, command: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        current = await self.drains.read()
        drain = current['drain']
        if drain is None or drain['id'] != command['drain_id']:
            return {'command': command, **current}
        if drain['state'] not in ('draining', 'blocked'):
            return {'command': command, **current}
        async with self.app.db.transaction() as conn:
            generation = await self.app.executions._host(conn)
            if generation != drain['host_generation'] or generation != request['expected_host_generation']:
                raise ControlConflict('the draining host changed; explicit recovery is required')
        parked = set()
        unconfirmed = set()
        if drain['policy'] == 'checkpoint_supported':
            observed = await self.app.manager.park_for_update({run['id'] for run in drain['active_runs']})
            parked = set(observed['parked'])
            unconfirmed = set(observed['unconfirmed'])
        else:
            lifecycle = self.app.extensions.get('lifecycle')
            if lifecycle is not None:
                for task_id in sorted({attempt['task_id'] for attempt in drain['active_attempts']}):
                    try:
                        preview = await lifecycle.preview('task', task_id)
                        pinned = {attempt['contract_revision'] for attempt in drain['active_attempts']
                                  if attempt['task_id'] == task_id}
                        if pinned != {preview['source_revision']}:
                            continue
                        if preview['cancel_state'] != 'active':
                            continue
                        operation = hashlib.sha256((drain['id'] + ':' + task_id).encode()).hexdigest()
                        await lifecycle.cancel_command(
                            principal, 'task', task_id, 'operator requested stopping work before an update',
                            expected_entity_revision=preview['entity_revision'],
                            expected_source_revision=preview['source_revision'],
                            preview_fingerprint=preview['preview_fingerprint'], client_operation_id='update:' + operation,
                        )
                    except (KeyError, ControlConflict, LifecycleRefused):
                        # A changed ownership graph is not authority to stop its replacement.
                        # Assessment retains the unresolved attempt as a visible blocker.
                        continue
            if drain['active_runs']:
                observed = await self.app.manager.stop_for_update(drain['active_runs'])
                unconfirmed = set(observed['unconfirmed'])
        revision = await self.drains.control.revision(Scope('global', 'global'), Entity('collection', 'global'))
        operation = command['receipt_id']
        await self.drains.assess(principal, drain_id=drain['id'], parked_run_ids=parked,
                                 expected_host_generation=request['expected_host_generation'],
                                 expected_collection_revision=revision, client_operation_id='checkpoint:' + operation,
                                 unconfirmed_run_ids=unconfirmed)
        return {'command': command, **await self.drains.read()}
