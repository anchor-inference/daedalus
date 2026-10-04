"""A durable drain fences new work and survives the host that requested it."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from protocore.contracts.types import Run, RunStatus

from daedalus.config import Settings
from daedalus.extensions import api_update_drains
from daedalus.extensions.api_update_drains import register
from daedalus.extensions.update_drain import UpdateDrainRuntime
from daedalus.host.inference_admission import HostInferenceAdmission
from daedalus.stores.control import ControlConflict, ControlDenied, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.sqlite import SqliteRunStore
from daedalus.stores.update_drains import UpdateDrainPaused, UpdateDrains, assert_admission_open_in
from tests.support.waiting import until
from tests.unit.test_inference_admission import endpoint, request
from tests.unit.test_session_runner import ScriptedProvider, _manager

OPERATOR = Principal.operator({'via': 'cookie', 'user_id': 1})
GLOBAL = Scope('global', 'global')
COLLECTION = Entity('collection', 'global')
CANDIDATE = {'candidate_bot_sha': 'a' * 40, 'candidate_core_sha': 'b' * 40,
             'candidate_compatibility_digest': 'c' * 64}


@pytest.fixture
async def drains(db: Database):
    executions = ExecutionStore(db)
    executions.acquire()
    try:
        await executions.boot()
        yield UpdateDrains(db, executions)
    finally:
        executions.release()


async def begin(drains: UpdateDrains, **changes):
    fields = dict(policy='checkpoint_supported', **CANDIDATE,
                  expected_host_generation=drains.executions.generation,
                  expected_collection_revision=await drains.control.revision(GLOBAL, COLLECTION),
                  client_operation_id='drain-one')
    fields.update(changes)
    return await drains.begin(OPERATOR, **fields)


async def test_drain_replay_is_durable_before_any_checkpoint_claim(drains: UpdateDrains, db: Database) -> None:
    revision = await drains.control.revision(GLOBAL, COLLECTION)
    first = await begin(drains, expected_collection_revision=revision)
    again = await begin(drains, expected_collection_revision=revision)
    assert first == again
    assert first['state'] == 'draining' and first['checkpoint_refs'] == []
    state = await drains.read()
    assert not state['admission_open'] and state['drain']['id'] == first['drain_id']
    assert (await db.fetchone('SELECT count(*) FROM update_drains'))[0] == 1
    assert (await db.fetchone("SELECT count(*) FROM operation_receipts WHERE operation_kind = 'runtime.update.drain'"))[0] == 1
    with pytest.raises(ControlConflict, match='different request'):
        await begin(drains, policy='stop_all', expected_collection_revision=revision)


async def test_stale_host_cannot_close_or_advance_admission(drains: UpdateDrains, db: Database) -> None:
    revision = await drains.control.revision(GLOBAL, COLLECTION)
    with pytest.raises(ControlConflict, match='host changed'):
        await begin(drains, expected_host_generation=drains.executions.generation + 1)
    assert (await drains.read())['admission_open']
    assert await drains.control.revision(GLOBAL, COLLECTION) == revision
    assert (await db.fetchone('SELECT count(*) FROM operation_receipts'))[0] == 0


async def test_unauthorized_agent_cannot_request_a_global_drain(drains: UpdateDrains) -> None:
    with pytest.raises(ControlDenied):
        await drains.begin(Principal('staff:worker', 'agent'), policy='stop_all', **CANDIDATE,
                           expected_host_generation=drains.executions.generation,
                           expected_collection_revision=await drains.control.revision(GLOBAL, COLLECTION),
                           client_operation_id='unapproved')
    assert (await drains.read())['drain'] is None


async def test_host_restart_retains_the_old_drain_instead_of_reopening(drains: UpdateDrains, db: Database) -> None:
    first = await begin(drains)
    drains.executions.release()
    successor = ExecutionStore(db)
    successor.acquire()
    try:
        assert await successor.boot() > first['host_generation']
        async with db.transaction() as conn:
            assert await successor._host(conn) == successor.generation
            with pytest.raises(ControlDenied, match='keeps new work closed'):
                await assert_admission_open_in(conn)
        assert not (await UpdateDrains(db, successor).read())['admission_open']
    finally:
        successor.release()


async def test_unavailable_model_during_restore_retains_saved_work_for_a_later_retry(
    settings: Settings, db: Database,
) -> None:
    entered = asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream_with_tools(self, model_request):
            entered.set()
            await asyncio.Event().wait()
            async for delta in super().stream_with_tools(model_request):
                yield delta

    manager = await _manager(settings, db, WaitingProvider([{'text': 'unfinished'}]))
    state = await manager.create_session('t')
    run_id = await manager.submit(state.session.id, 'keep this work')
    await asyncio.wait_for(entered.wait(), 30)
    await manager.close()
    original = await manager.events.load_snapshot(run_id)
    assert original is not None

    restored = await _manager(settings, db, ScriptedProvider([{'text': 'resumed'}]))
    build_engine = restored._build_engine
    restored._build_engine = AsyncMock(side_effect=RuntimeError('model temporarily unavailable'))
    try:
        assert await restored.resume_unfinished() == []
        assert await restored.events.load_snapshot(run_id) == original
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] != 'cancelled'
        restored._build_engine = build_engine
        assert await restored.resume_unfinished() == [run_id]
        await until(lambda: not restored._states[state.session.id].running, 'the restored turn finished')
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] == 'completed'
    finally:
        await restored.close()


async def test_new_run_is_denied_without_creating_a_durable_run(drains: UpdateDrains, db: Database) -> None:
    await begin(drains)
    at = datetime.now(UTC)
    run = Run(id='new', tenant_id='tenant', session_id='session', status=RunStatus.running,
              created_at=at, updated_at=at)
    with pytest.raises(ControlDenied, match='keeps new work closed'):
        await SqliteRunStore(db).create(run)
    assert (await db.fetchone('SELECT count(*) FROM runs'))[0] == 0


async def test_pending_effect_is_not_claimed_after_update_admission_closes(
    drains: UpdateDrains, db: Database,
) -> None:
    async def enqueue(conn, mutation):
        effect_id = await OutboxStore.enqueue(conn, mutation, OPERATOR, kind='test.effect',
                                              operation='test.effect', payload={})
        return {'effect_id': effect_id}

    command = await drains.control.mutate(
        OPERATOR, GLOBAL, 'test.effect', 'queued-effect',
        await drains.control.revision(GLOBAL, COLLECTION), COLLECTION, {}, enqueue)
    opened = await begin(drains)
    assert opened['active_receipts'][0]['id'] == command['effect_id']
    assert await OutboxStore(db).claim(('test.effect',)) is None
    assert (await db.fetchone('SELECT state FROM effect_outbox WHERE id = ?',
                              (command['effect_id'],)))['state'] == 'pending'


async def test_native_send_parks_without_creating_an_inference_reservation(drains: UpdateDrains, db: Database) -> None:
    await begin(drains)
    admission = HostInferenceAdmission(SimpleNamespace(db=db))
    with pytest.raises(UpdateDrainPaused, match='keeps new work closed'):
        await admission.start(endpoint(), request(), {'max_tokens': 20})
    assert (await db.fetchone('SELECT count(*) FROM inference_reservations'))[0] == 0


async def test_native_pause_preserves_the_actual_engine_snapshot_and_does_not_publish_completion(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausingProvider(ScriptedProvider):
        async def stream_with_tools(self, model_request):
            entered.set()
            await release.wait()
            await HostInferenceAdmission(SimpleNamespace(db=db)).start(endpoint(), model_request, {'max_tokens': 20})
            async for delta in super().stream_with_tools(model_request):
                yield delta

    provider = PausingProvider([{'text': 'the result'}])
    manager = await _manager(settings, db, provider)
    completed = []

    async def finished(*args):
        completed.append(args)

    manager.on_finished(finished)
    try:
        state = await manager.create_session('t')
        run_id = await manager.submit(state.session.id, 'keep this request')
        await asyncio.wait_for(entered.wait(), 30)
        captured = await begin(drains)
        assert any(row['id'] == run_id for row in captured['active_runs'])
        release.set()
        await until(lambda: not state.running, 'the drained run is parked')
        snapshots = await manager.events.unfinished_snapshots()
        assert any(row['run_id'] == run_id for row in snapshots)
        assert not completed and not provider.requests
        before = await manager.events.load_snapshot(run_id)
        assert await manager.resume_unfinished() == []
        assert await manager.events.load_snapshot(run_id) == before
        assert not state.running and not provider.requests
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] == 'paused'
        assert (await db.fetchone('SELECT count(*) FROM inference_reservations'))[0] == 0
    finally:
        release.set()
        await manager.close()


async def test_update_parking_cancels_an_inflight_provider_without_settling_or_losing_its_snapshot(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    entered = asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream_with_tools(self, model_request):
            entered.set()
            await asyncio.Event().wait()
            async for delta in super().stream_with_tools(model_request):
                yield delta

    provider = WaitingProvider([{'text': 'unexpected completion'}])
    manager = await _manager(settings, db, provider)
    completed = []

    async def finished(*args):
        completed.append(args)

    manager.on_finished(finished)
    try:
        state = await manager.create_session('t')
        run_id = await manager.submit(state.session.id, 'preserve the turn')
        await asyncio.wait_for(entered.wait(), 30)
        with pytest.raises(RuntimeError, match='close update admission'):
            await manager.park_for_update({run_id})
        assert state.running
        await begin(drains)
        parked = await manager.park_for_update({run_id, 'unknown-run'})
        assert parked == {'parked': [run_id], 'unconfirmed': ['unknown-run']}
        assert not state.running and not completed and not provider.requests
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] == 'paused'
        snapshot = await manager.events.load_snapshot(run_id)
        assert snapshot is not None
        assert await manager.resume_unfinished() == []
        assert await manager.events.load_snapshot(run_id) == snapshot
        assert not completed and not state.running
    finally:
        await manager.close()


async def assess(drains: UpdateDrains, drain_id: str, parked_run_ids: set[str], **changes):
    fields = dict(drain_id=drain_id, parked_run_ids=parked_run_ids,
                  expected_host_generation=drains.executions.generation,
                  expected_collection_revision=await drains.control.revision(GLOBAL, COLLECTION),
                  client_operation_id='assess-one')
    fields.update(changes)
    return await drains.assess(OPERATOR, **fields)


async def test_empty_host_can_be_ready_but_admission_remains_closed(drains: UpdateDrains) -> None:
    opened = await begin(drains)
    result = await assess(drains, opened['drain_id'], set())
    assert result['state'] == 'ready' and not result['admission_open']
    assert not result['checkpoint_refs'] and not result['blockers']
    assert not (await drains.read())['admission_open']


async def test_saved_snapshot_alone_cannot_attest_a_parked_run(drains: UpdateDrains, db: Database) -> None:
    at = datetime.now(UTC)
    run = Run(id='unobserved', tenant_id='tenant', session_id='session', status=RunStatus.running,
              created_at=at, updated_at=at)
    await SqliteRunStore(db).create(run)
    await db.execute('INSERT INTO snapshots(run_id,tenant_id,session_id,state,snapshot,updated_at) VALUES (?,?,?,?,?,?)',
                     ('unobserved', 'tenant', 'session', 'running', '{"history":[]}', at.isoformat()))
    opened = await begin(drains)
    result = await assess(drains, opened['drain_id'], set())
    assert result['state'] == 'blocked' and not result['checkpoint_refs']
    assert 'unobserved' in result['blockers'][0]
    await SqliteRunStore(db).update_status('unobserved', 'tenant', RunStatus.paused)
    again = await assess(drains, opened['drain_id'], set(), client_operation_id='assess-without-host')
    assert again['state'] == 'blocked'
    malformed = await assess(drains, opened['drain_id'], {'unobserved'}, client_operation_id='assess-malformed')
    assert malformed['state'] == 'blocked' and not malformed['checkpoint_refs']
    await db.execute('UPDATE snapshots SET snapshot = ? WHERE run_id = ?',
                     (json.dumps({'schema_version': 7, 'run_id': 'unobserved', 'session_id': 'session',
                                  'tenant_id': 'tenant', 'model_name': 'model', 'history': []}), 'unobserved'))
    parked = await assess(drains, opened['drain_id'], {'unobserved'}, client_operation_id='assess-host-observed')
    assert parked['state'] == 'ready'
    assert parked['checkpoint_refs'][0]['run_id'] == 'unobserved'
    assert len(parked['checkpoint_refs'][0]['sha256']) == 64


async def test_stop_policy_never_promises_resume_from_a_native_checkpoint(drains: UpdateDrains, db: Database) -> None:
    at = datetime.now(UTC)
    run = Run(id='paused', tenant_id='tenant', session_id='session', status=RunStatus.paused,
              created_at=at, updated_at=at)
    await SqliteRunStore(db).create(run)
    await db.execute('INSERT INTO snapshots(run_id,tenant_id,session_id,state,snapshot,updated_at) VALUES (?,?,?,?,?,?)',
                     ('paused', 'tenant', 'session', 'awaiting', '{"history":[]}', at.isoformat()))
    opened = await begin(drains, policy='stop_all')
    result = await assess(drains, opened['drain_id'], {'paused'})
    assert result['state'] == 'blocked' and not result['checkpoint_refs']
    assert not (await drains.read())['admission_open']


async def test_update_api_replays_exact_command_and_exposes_current_saved_state(
    drains: UpdateDrains, tmp_path,
) -> None:
    api = FastAPI()
    manager = SimpleNamespace(park_for_update=AsyncMock(return_value={'parked': [], 'unconfirmed': []}))
    token_path = tmp_path / 'supervisor.token'
    token_path.write_text('unit-supervisor', encoding='utf-8')
    app = SimpleNamespace(db=drains.db, executions=drains.executions, manager=manager,
                          settings=SimpleNamespace(supervisor_token_path=token_path))
    register(api, app, lambda: {'via': 'cookie', 'user_id': 1})
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url='http://test',
                                 headers={'x-daedalus-supervisor-token': 'unit-supervisor'}) as client:
        initial = (await client.get('/api/runtime/updates/drain')).json()
        assert initial['admission_open']
        body = {'policy': 'checkpoint_supported', **CANDIDATE,
                'expected_host_generation': initial['host_generation'],
                'expected_collection_revision': initial['collection_revision'], 'client_operation_id': 'prepare-api'}
        denied = await client.post('/api/runtime/updates/drain', json=body,
                                   headers={'x-daedalus-supervisor-token': 'wrong'})
        assert denied.status_code == 403 and (await drains.read())['drain'] is None
        first = await client.post('/api/runtime/updates/drain', json=body)
        assert first.status_code == 200
        saved = first.json()
        assert saved['command']['state'] == 'draining' and saved['drain']['state'] == 'ready'
        assert not saved['admission_open']
        replay = await client.post('/api/runtime/updates/drain', json=body)
        assert replay.status_code == 200 and replay.json() == saved
        assert manager.park_for_update.await_count == 1
        assert (await client.get('/api/runtime/updates/drain')).json()['drain']['id'] == saved['command']['drain_id']
        conflict = await client.post('/api/runtime/updates/drain', json={**body, 'policy': 'stop_all'})
        assert conflict.status_code == 409
        forged = await client.post('/api/runtime/updates/drain', json={**body, 'parked_run_ids': ['fabricated']})
        assert forged.status_code == 422
        current = (await client.get('/api/runtime/updates/drain')).json()
        # Command kinds have distinct keys even when the client reuses the same text ID.
        check_body = {'expected_host_generation': current['host_generation'],
                      'expected_collection_revision': current['collection_revision'], 'client_operation_id': 'prepare-api'}
        target = '/api/runtime/updates/drain/' + saved['command']['drain_id'] + '/recheck'
        fresh = await client.post(target, json=check_body)
        assert fresh.status_code == 200 and fresh.json()['drain']['state'] == 'ready'
        assert fresh.json()['command']['receipt_id'] != saved['command']['receipt_id']
        assert manager.park_for_update.await_count == 2
        again = await client.post(target, json=check_body)
        assert again.status_code == 200 and again.json() == fresh.json()
        assert manager.park_for_update.await_count == 2


async def test_closed_update_admission_keeps_the_pending_question_and_answer_claim_untouched(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    provider = ScriptedProvider([
        {'tool': 'AskUser', 'args': {'questions': [{'question': 'Color?', 'options': [{'label': 'Blue'}]}]}},
        {'text': 'unexpected answer'},
    ])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session('t')
        await manager.submit(state.session.id, 'ask me')
        await until(lambda: state.pending is not None and not state.running and not manager._settling(state),
                    'the question is saved and waiting')
        pending = state.pending
        history = list(state.engine.history)
        snapshot = await manager.events.load_snapshot(state.run_id)
        claim = AsyncMock(return_value=None)
        manager.answer_claims.append(claim)
        await begin(drains)
        with pytest.raises(ControlDenied, match='keeps new work closed'):
            await manager.answer(state.session.id, [{'selected': ['Blue']}], via='app')
        assert state.pending is pending and state.engine.history == history
        assert not claim.await_count
        assert await manager.events.load_snapshot(state.run_id) == snapshot
        assert len(provider.requests) == 1
    finally:
        await manager.close()


async def decide(drains: UpdateDrains, drain_id: str, decision: str, **changes):
    references = (await drains.read())['drain']['checkpoint_refs']
    fields = dict(drain_id=drain_id, decision=decision, **CANDIDATE,
                  checkpoint_refs_digest=hashlib.sha256(json.dumps(references, sort_keys=True,
                                                                   separators=(',', ':')).encode()).hexdigest(),
                  expected_host_generation=drains.executions.generation,
                  expected_collection_revision=await drains.control.revision(GLOBAL, COLLECTION),
                  client_operation_id='decision-' + decision)
    fields.update(changes)
    return await drains.decide(OPERATOR, **fields)


async def test_commit_requires_confirmed_readiness_and_cannot_be_aborted_by_the_old_host(drains: UpdateDrains) -> None:
    opened = await begin(drains)
    with pytest.raises(ControlConflict, match='quiescent checkpoint'):
        await decide(drains, opened['drain_id'], 'commit')
    await assess(drains, opened['drain_id'], set())
    revision = await drains.control.revision(GLOBAL, COLLECTION)
    committed = await decide(drains, opened['drain_id'], 'commit', expected_collection_revision=revision)
    assert committed['state'] == 'committed' and not committed['admission_open']
    saved = (await drains.read())['drain']
    assert saved['commit_receipt_id'] == committed['commit_receipt_id'] == committed['receipt_id']
    assert saved['candidate_bot_sha'] == CANDIDATE['candidate_bot_sha']
    assert await decide(drains, opened['drain_id'], 'commit', expected_collection_revision=revision) == committed
    with pytest.raises(ControlConflict, match='successor recovery'):
        await decide(drains, opened['drain_id'], 'abort')
    assert not (await drains.read())['admission_open']


async def test_commit_rejects_refs_changed_after_candidate_verification(drains: UpdateDrains) -> None:
    opened = await begin(drains)
    await assess(drains, opened['drain_id'], set())
    with pytest.raises(ControlConflict, match='references changed'):
        await decide(drains, opened['drain_id'], 'commit', checkpoint_refs_digest='0' * 64)
    assert (await drains.read())['drain']['state'] == 'ready'


async def test_successor_reopens_only_the_exact_committed_installed_pair(drains: UpdateDrains, db: Database) -> None:
    opened = await begin(drains, policy='stop_all')
    await assess(drains, opened['drain_id'], set())
    committed = await decide(drains, opened['drain_id'], 'commit')
    old_generation = drains.executions.generation
    drains.executions.release()
    successor = ExecutionStore(db)
    successor.acquire()
    try:
        await successor.boot()
        recovery = UpdateDrains(db, successor)
        revision = await recovery.control.revision(GLOBAL, COLLECTION)
        async def recover(**changes):
            fields = dict(drain_id=opened['drain_id'], commit_receipt_id=committed['commit_receipt_id'],
                          installed_bot_sha=CANDIDATE['candidate_bot_sha'],
                          installed_core_sha=CANDIDATE['candidate_core_sha'],
                          expected_host_generation=successor.generation,
                          expected_collection_revision=revision,
                          client_operation_id='successor-recovery')
            fields.update(changes)
            return await recovery.recover(OPERATOR, **fields)

        with pytest.raises(ControlConflict, match='installed revision'):
            await recover(installed_core_sha='d' * 40)
        with pytest.raises(ControlConflict, match='installed revision'):
            await recover(commit_receipt_id='different')
        with pytest.raises(ControlConflict, match='current successor'):
            await recover(expected_host_generation=old_generation)
        assert not (await recovery.read())['admission_open']
        result = await recover()
        assert result['state'] == 'resumed' and result['admission_open']
        assert await recover() == result
        async with db.transaction() as conn:
            await assert_admission_open_in(conn)
    finally:
        successor.release()


async def test_successor_keeps_changed_checkpoint_fenced(drains: UpdateDrains, db: Database) -> None:
    at = datetime.now(UTC)
    run = Run(id='parked', tenant_id='tenant', session_id='session', status=RunStatus.paused,
              created_at=at, updated_at=at)
    await SqliteRunStore(db).create(run)
    saved = {'schema_version': 7, 'run_id': 'parked', 'session_id': 'session',
             'tenant_id': 'tenant', 'model_name': 'model', 'history': []}
    await db.execute('INSERT INTO snapshots(run_id,tenant_id,session_id,state,snapshot,updated_at) VALUES (?,?,?,?,?,?)',
                     ('parked', 'tenant', 'session', 'running', json.dumps(saved), at.isoformat()))
    opened = await begin(drains)
    assert (await assess(drains, opened['drain_id'], {'parked'}))['state'] == 'ready'
    committed = await decide(drains, opened['drain_id'], 'commit')
    drains.executions.release()
    successor = ExecutionStore(db)
    successor.acquire()
    try:
        await successor.boot()
        recovery = UpdateDrains(db, successor)
        fields = dict(drain_id=opened['drain_id'], commit_receipt_id=committed['commit_receipt_id'],
                      installed_bot_sha=CANDIDATE['candidate_bot_sha'],
                      installed_core_sha=CANDIDATE['candidate_core_sha'],
                      expected_host_generation=successor.generation,
                      expected_collection_revision=await recovery.control.revision(GLOBAL, COLLECTION),
                      client_operation_id='checkpoint-recovery')
        await db.execute('UPDATE snapshots SET snapshot = ? WHERE run_id = ?',
                         (json.dumps({**saved, 'history': [{'role': 'user', 'content': 'changed'}]}), 'parked'))
        with pytest.raises(ControlConflict, match='checkpoint changed'):
            await recovery.recover(OPERATOR, **fields)
        assert not (await recovery.read())['admission_open']
        await db.execute('UPDATE snapshots SET snapshot = ? WHERE run_id = ?', (json.dumps(saved), 'parked'))
        recovered = await recovery.recover(OPERATOR, **fields)
        assert recovered['state'] == 'resumed' and recovered['checkpoint_refs'][0]['run_id'] == 'parked'
    finally:
        successor.release()


async def test_successor_recovery_api_requires_launcher_secret_and_installed_heads(
    drains: UpdateDrains, db: Database, tmp_path, monkeypatch,
) -> None:
    opened = await begin(drains, policy='stop_all')
    await assess(drains, opened['drain_id'], set())
    committed = await decide(drains, opened['drain_id'], 'commit')
    drains.executions.release()
    successor = ExecutionStore(db)
    successor.acquire()
    try:
        await successor.boot()
        recovery = UpdateDrains(db, successor)
        token_path = tmp_path / 'supervisor.token'
        token_path.write_text('launcher-secret', encoding='utf-8')
        manager = SimpleNamespace(resume_unfinished=AsyncMock(return_value=[]))
        effects = SimpleNamespace(enable=Mock())
        app = SimpleNamespace(db=db, executions=successor, manager=manager,
                              extensions={'effects': effects}, guard=SimpleNamespace(skip_recovery=False),
                              settings=SimpleNamespace(supervisor_token_path=token_path,
                                                       bot_repo_dir=tmp_path / 'bot',
                                                       core_repo_dir=tmp_path / 'core'))
        api = FastAPI()
        register(api, app, lambda: {'via': 'cookie', 'user_id': 1})
        monkeypatch.setattr(api_update_drains.subprocess, 'run', lambda args, **kwargs: SimpleNamespace(
            returncode=0, stdout=CANDIDATE['candidate_bot_sha'] + '\n' if str(args[2]).endswith('bot')
            else CANDIDATE['candidate_core_sha'] + '\n'))
        body = {'commit_receipt_id': committed['commit_receipt_id'],
                'installed_bot_sha': CANDIDATE['candidate_bot_sha'],
                'installed_core_sha': CANDIDATE['candidate_core_sha'],
                'expected_host_generation': successor.generation,
                'expected_collection_revision': await recovery.control.revision(GLOBAL, COLLECTION),
                'client_operation_id': 'api-recovery'}
        path = f"/api/runtime/updates/drain/{opened['drain_id']}/recover"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
            forbidden = await client.post(path, json=body)
            assert forbidden.status_code == 403
            mismatch = await client.post(path, json={**body, 'installed_core_sha': 'd' * 40},
                                         headers={'x-daedalus-supervisor-token': 'launcher-secret'})
            assert mismatch.status_code == 409
            assert not (await recovery.read())['admission_open']
            accepted = await client.post(path, json=body,
                                         headers={'x-daedalus-supervisor-token': 'launcher-secret'})
            assert accepted.status_code == 200 and accepted.json()['admission_open']
        manager.resume_unfinished.assert_not_awaited()
        effects.enable.assert_called_once()
    finally:
        successor.release()


async def test_aborting_before_update_reopens_admission_without_deleting_saved_work(drains: UpdateDrains) -> None:
    opened = await begin(drains)
    aborted = await decide(drains, opened['drain_id'], 'abort')
    assert aborted['state'] == 'aborted' and aborted['admission_open']
    async with drains.db.transaction() as conn:
        await assert_admission_open_in(conn)
    new = await begin(drains, client_operation_id='another-update')
    assert new['drain_id'] != opened['drain_id'] and not new['admission_open']


async def test_update_racing_the_first_core_entry_preserves_and_resumes_the_exact_input_once(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    provider = ScriptedProvider([{'text': 'continued after aborted update'}])
    manager = await _manager(settings, db, provider)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_publish = manager._publish

    async def publish(state, kind, payload):
        if kind == 'run.started':
            entered.set()
            await release.wait()
        return await original_publish(state, kind, payload)

    manager._publish = publish
    submit = None
    try:
        state = await manager.create_session('t')
        submit = asyncio.create_task(manager.submit(state.session.id, 'the exact first request'))
        await asyncio.wait_for(entered.wait(), 30)
        opened = await begin(drains)
        release.set()
        run_id = await submit
        await until(lambda: not state.running, 'the first core entry is parked')
        assert not provider.requests
        snapshot = await manager.events.load_snapshot(run_id)
        assert snapshot is not None and snapshot['run_id'] == run_id
        user_messages = [message for message in snapshot['history'] if message['role'] == 'user']
        assert len(user_messages) == 1
        assert 'the exact first request' in json.dumps(user_messages)
        await decide(drains, opened['drain_id'], 'abort')
        assert await manager.resume_unfinished() == [run_id]
        await until(lambda: not state.running and not manager._settling(state), 'the retained first request completes')
        assert len(provider.requests) == 1
        messages = [message for message in provider.requests[0].messages if str(message.role) == 'user']
        assert len(messages) == 1
        assert 'the exact first request' in messages[0].model_dump_json()
    finally:
        release.set()
        if submit is not None:
            await asyncio.gather(submit, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize('uncertain', [
    {'background_task_ids': ['owned-background']},
    {'open_intents': [{'state': 'DISPATCHED', 'outcome': 'pending', 'tool_name': 'Bash'}]},
    {'open_intents': [{'state': 'SETTLED', 'outcome': 'unknown', 'tool_name': 'WriteFile'}]},
])
async def test_parked_core_does_not_prove_its_external_side_effects_stopped(
    drains: UpdateDrains, db: Database, uncertain: dict,
) -> None:
    at = datetime.now(UTC)
    run = Run(id='external', tenant_id='tenant', session_id='session', status=RunStatus.paused,
              created_at=at, updated_at=at)
    await SqliteRunStore(db).create(run)
    snapshot = {'schema_version': 7, 'run_id': 'external', 'session_id': 'session', 'tenant_id': 'tenant',
                'model_name': 'model', 'history': [], **uncertain}
    await db.execute('INSERT INTO snapshots(run_id,tenant_id,session_id,state,snapshot,updated_at) VALUES (?,?,?,?,?,?)',
                     ('external', 'tenant', 'session', 'running', json.dumps(snapshot), at.isoformat()))
    opened = await begin(drains)
    observation = await assess(drains, opened['drain_id'], {'external'})
    assert observation['state'] == 'blocked' and not observation['checkpoint_refs']
    assert observation['blockers'] and not observation['admission_open']
    with pytest.raises(ControlConflict, match='quiescent checkpoint'):
        await decide(drains, opened['drain_id'], 'commit')


async def test_old_prepare_replay_cannot_park_a_successor_hosts_runtime(drains: UpdateDrains, db: Database) -> None:
    generation = drains.executions.generation
    revision = await drains.control.revision(GLOBAL, COLLECTION)
    await begin(drains, expected_collection_revision=revision)
    drains.executions.release()
    successor = ExecutionStore(db)
    successor.acquire()
    try:
        await successor.boot()
        manager = SimpleNamespace(park_for_update=AsyncMock(return_value={'parked': [], 'unconfirmed': []}))
        runtime = UpdateDrainRuntime(SimpleNamespace(db=db, executions=successor, manager=manager))
        with pytest.raises(ControlConflict, match='host changed'):
            await runtime.prepare(OPERATOR, policy='checkpoint_supported', **CANDIDATE,
                                  expected_host_generation=generation,
                                  expected_collection_revision=revision, client_operation_id='drain-one')
        assert not manager.park_for_update.await_count
        assert not (await runtime.drains.read())['admission_open']
    finally:
        successor.release()


async def test_stop_all_settles_an_exact_native_provider_run_instead_of_promising_a_resume(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    entered = asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream_with_tools(self, model_request):
            entered.set()
            await asyncio.Event().wait()
            async for delta in super().stream_with_tools(model_request):
                yield delta

    manager = await _manager(settings, db, WaitingProvider([{'text': 'unexpected answer'}]))
    try:
        state = await manager.create_session('t')
        run_id = await manager.submit(state.session.id, 'stop before update')
        await asyncio.wait_for(entered.wait(), 30)
        opened = await begin(drains, policy='stop_all')
        outcome = await manager.stop_for_update([{'id': run_id, 'session_id': state.session.id}])
        await until(lambda: not state.running and not manager._settling(state), 'the stopped native run has settled')
        assert outcome == {'stopped': [run_id], 'unconfirmed': []}
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] == 'cancelled'
        assessed = await assess(drains, opened['drain_id'], set())
        assert assessed['state'] == 'ready' and assessed['checkpoint_refs'] == []
        assert not assessed['admission_open']
    finally:
        await manager.close()


async def test_stop_all_closes_a_saved_question_through_normal_settlement(
    drains: UpdateDrains, settings: Settings, db: Database,
) -> None:
    provider = ScriptedProvider([
        {'tool': 'AskUser', 'args': {'questions': [{'question': 'Continue?', 'options': [{'label': 'Yes'}]}]}},
    ])
    manager = await _manager(settings, db, provider)
    try:
        state = await manager.create_session('t')
        run_id = await manager.submit(state.session.id, 'ask first')
        await until(lambda: state.pending is not None and not state.running and not manager._settling(state),
                    'the pending question is saved')
        await begin(drains, policy='stop_all')
        outcome = await manager.stop_for_update([{'id': run_id, 'session_id': state.session.id}])
        assert outcome == {'stopped': [run_id], 'unconfirmed': []}
        assert state.pending is None
        assert await db.fetchone('SELECT 1 FROM pending_questions WHERE session_id = ?', (state.session.id,)) is None
        assert (await db.fetchone('SELECT status FROM runs WHERE id = ?', (run_id,)))['status'] == 'cancelled'
        assert len(provider.requests) == 1
    finally:
        await manager.close()


async def test_terminal_run_row_does_not_override_an_unfinished_runtime_settlement(drains: UpdateDrains) -> None:
    opened = await begin(drains, policy='stop_all')
    observation = await assess(drains, opened['drain_id'], set(), unconfirmed_run_ids={'still-writing'})
    assert observation['state'] == 'blocked'
    assert observation['blockers'] == ['native run still-writing has not finished its runtime settlement']
    with pytest.raises(ControlConflict, match='quiescent checkpoint'):
        await decide(drains, opened['drain_id'], 'commit')
