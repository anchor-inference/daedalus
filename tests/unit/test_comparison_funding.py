"""A real admitted native call consumes its pair escrow once and retains uncertain charges."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest
from protocore.contracts.llm import LLMObservabilityContext, LLMProviderError

from daedalus.config import ModelPresetConfig, RuntimeConfig
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.extensions.staff import Team
from daedalus.host.inference_admission import HostInferenceAdmission, model_quote
from daedalus.host.launch_queue import Entry, LaunchQueue
from daedalus.stores.comparison_funding import ComparisonFunding, PairAllocation
from daedalus.stores.comparisons import create_group
from daedalus.stores.control import ControlStore, Principal, Scope, canonical
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.inference_budget import BudgetRefused, Constraint, InferenceBudget
from daedalus.stores.runtime_release import physical_exit_in
from tests.unit.test_inference_admission import answer, endpoint, provider, request
from tests.unit.test_inference_budget import reserve
from tests.unit.test_runtime_observations import native_session

OPERATOR = Principal.operator({'via': 'cookie', 'user_id': 1})


@pytest.fixture
async def pair(db: Database):
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','now','{}')")
    await db.execute("INSERT INTO project_folders(id,project_id,path,label,env,created_at)"
                     " VALUES ('folder','project','/tmp/comparison-source','Source','container','now')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,folder_id,created_at,updated_at)"
                     " VALUES ('task','Compare','todo',3,'project','folder','now','now')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                     " VALUES ('task',1,'operator','','{}','now')")
    for slot in (1, 2):
        await db.execute("INSERT INTO staff(id,project_id,name,harness,isolation,created_by,created_at)"
                         " VALUES (?,'project',?,'daedalus','worktree','operator','now')", (f'worker{slot}', f'Worker {slot}'))
    store = ExecutionStore(db)
    store.acquire()
    await store.boot()
    quote, _ = model_quote(endpoint(), 'model', 20)
    allocations = tuple(PairAllocation(f'slot{slot}', slot, f'worker{slot}', 'test', 'model', 100,
                                      hashlib.sha256(canonical(quote).encode()).hexdigest(), quote) for slot in (1, 2))
    async with db.transaction() as conn:
        await create_group(conn, group_id='group', task_id='task', contract_revision=1,
                           budget_cap_microusd=200, actor_id='operator')
    try:
        yield store, allocations
    finally:
        store.release()


async def fund(db: Database, pair, *, cap=200):
    store, allocations = pair
    async with db.transaction() as conn:
        return await ComparisonFunding(db).reserve_pair_in(conn, group_id='group', allocations=allocations,
                                                           constraints=(Constraint('total:all', cap),), host_generation=store.generation)


async def native_worker(db: Database, pair, *, launch: bool = True):
    store, _ = pair
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,folder_id,status_at,started_at,worktree_path,branch)"
                     " VALUES ('staff-session','worker1','daedalus','task','folder','now','now','/tmp/alternative-one','alternative-one')")
    grant = await ControlStore(db).issue_grant(OPERATOR, Principal('staff:worker1', 'agent'), Scope('project', 'project'),
                                              operations=['result.submit'], effects=[], task_id='task',
                                              expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    worker = Principal('staff:worker1', 'agent', grant['grant_id'], grant['generation'])
    async with db.transaction() as conn:
        await store.create(conn, attempt_id='attempt1', task_id='task', contract_revision=1,
                           launcher=OPERATOR, worker=worker, staff_session_id='staff-session',
                           runtime_kind='daedalus', fence_token=secrets.token_urlsafe(32), comparison_slot_id='slot1')
        if launch:
            await ComparisonFunding(db).start_launch_in(conn, 'slot1', 'attempt1')
    if launch:
        await native_session(db)
        await admit_native_run(SimpleNamespace(db=db, executions=store), 'staff-session', 'native', 'run')
    config = RuntimeConfig()
    config.limits.usd_per_run = 0
    config.limits.usd_total = 0.0002
    manager = SimpleNamespace(db=db, execution_store=store, config=config, settings=SimpleNamespace(usd_per_day=0),
                              live_state=lambda _: None, mode_for=lambda _: None)
    return manager, request(observability=LLMObservabilityContext(tenant_id='tenant', session_id='native', run_id='run'))


async def test_both_allocations_reserve_atomically_before_either_can_start(db: Database, pair) -> None:
    with pytest.raises(BudgetRefused, match='pair exceeds'):
        await fund(db, pair, cap=199)
    assert not await db.fetchall('SELECT id FROM comparison_funding_slots')
    assert not await db.fetchall('SELECT slot_id FROM comparison_funding_scopes')
    assert len(await fund(db, pair)) == 2
    with pytest.raises(BudgetRefused, match='available balance'):
        await reserve(db, 'ordinary', 1, constraints=(Constraint('total:all', 200),))


async def test_existing_normal_reservation_blocks_pair_without_leaving_one_slot(db: Database, pair) -> None:
    await reserve(db, 'ordinary', 1, constraints=(Constraint('total:all', 200),))
    with pytest.raises(BudgetRefused, match='pair exceeds'):
        await fund(db, pair)
    assert not await db.fetchall('SELECT id FROM comparison_funding_slots')


async def test_actual_native_requests_consume_pool_once_and_preserve_attempt_cost(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer())
    try:
        await adapter.complete_text(observed)
        await adapter.complete_text(observed)
        assert len(sends) == 2
        rows = await db.fetchall('SELECT execution_attempt_id,comparison_slot_id,actual_microusd,state FROM inference_reservations')
        assert [tuple(row) for row in rows] == [('attempt1', 'slot1', 5, 'settled')] * 2
        async with db.transaction() as conn:
            assert await ComparisonFunding(db).observed_cost_in(conn, 'attempt1') == 10
        view = await InferenceBudget(db).spend_view(since=None, total_cap=0.0002, provider_caps={})
        assert view['total']['spent_usd'] == 0.00001
        assert view['total']['reserved_usd'] == 0.00019
        with pytest.raises(BudgetRefused, match='available balance'):
            await reserve(db, 'ordinary', 1, constraints=(Constraint('total:all', 200),))
    finally:
        await adapter.aclose()


async def test_missing_usage_retains_pool_and_blocks_next_send_or_refund(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(raw={}))
    try:
        await adapter.complete_text(observed)
        with pytest.raises(LLMProviderError, match='prepaid balance'):
            await adapter.complete_text(observed)
        assert len(sends) == 1
        async with db.transaction() as conn:
            assert await ComparisonFunding(db).observed_cost_in(conn, 'attempt1') is None
            with pytest.raises(BudgetRefused, match='physical exit'):
                await ComparisonFunding(db).release_in(conn, 'slot1')
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        app = SimpleNamespace(db=db, executions=pair[0])
        assert await observe_exit(app, staff_session_id='staff-session', runtime_ref='run', observed_status='completed')
        async with db.transaction() as conn:
            with pytest.raises(BudgetRefused, match='unresolved provider charge'):
                await ComparisonFunding(db).release_in(conn, 'slot1')
        view = await InferenceBudget(db).spend_view(since='9999', total_cap=0.0002, provider_caps={})
        assert view['total']['reserved_usd'] == 0.0002
        assert view['total']['uncertain_usd'] == 0.00007
    finally:
        await adapter.aclose()


async def test_same_provider_price_change_is_refused_before_transport(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    changed = endpoint()
    from daedalus.providers.pricing import ModelPricing

    changed.pricing['model'] = ModelPricing(input=0.5, output=1, input_limit=50, limit_source='test-provider-ceiling')
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(), configured=changed)
    try:
        with pytest.raises(LLMProviderError, match='rate card changed'):
            await adapter.complete_text(observed)
        assert not sends
        assert not await db.fetchall('SELECT id FROM inference_reservations')
    finally:
        await adapter.aclose()


async def test_allocation_and_charge_binding_cannot_be_rewritten(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    adapter = provider(manager, lambda _: answer())
    try:
        await adapter.complete_text(observed)
        with pytest.raises(aiosqlite.IntegrityError, match='immutable'):
            await db.execute("UPDATE comparison_funding_slots SET allowance_microusd = 999 WHERE id = 'slot1'")
        with pytest.raises(aiosqlite.IntegrityError, match='one execution'):
            await db.execute("UPDATE comparison_funding_slots SET attempt_id = 'attempt1' WHERE id = 'slot1'")
        with pytest.raises(aiosqlite.IntegrityError, match='ownership is immutable'):
            await db.execute("UPDATE inference_reservations SET execution_attempt_id = NULL")
    finally:
        await adapter.aclose()


async def test_refund_requires_exact_exit_and_known_usage_without_erasing_history(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    adapter = provider(manager, lambda _: answer())
    try:
        await adapter.complete_text(observed)
        async with db.transaction() as conn:
            assert not await physical_exit_in(conn, 'attempt1')
            with pytest.raises(BudgetRefused, match='physical exit'):
                await ComparisonFunding(db).release_in(conn, 'slot1')
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        app = SimpleNamespace(db=db, executions=pair[0])
        assert await observe_exit(app, staff_session_id='staff-session', runtime_ref='run', observed_status='completed')
        async with db.transaction() as conn:
            assert await physical_exit_in(conn, 'attempt1')
            await ComparisonFunding(db).release_in(conn, 'slot1')
            assert await ComparisonFunding(db).observed_cost_in(conn, 'attempt1') == 5
        assert (await db.fetchone('SELECT count(*) FROM inference_reservations'))[0] == 1
        view = await InferenceBudget(db).spend_view(since=None, total_cap=0.0002, provider_caps={})
        assert view['total']['reserved_usd'] == 0.0001
        assert view['total']['spent_usd'] == 0.000005
    finally:
        await adapter.aclose()


async def test_child_calls_are_attributed_to_the_host_attested_parent_attempt(db: Database, pair) -> None:
    await fund(db, pair)
    manager, _ = await native_worker(db, pair)
    await db.execute("INSERT INTO sessions(id,tenant_id,project_id,created_at,last_message_at,metadata)"
                     " VALUES ('child','tenant','project','now','now',?)", (json.dumps({'subagent_of': 'native'}),))
    await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('child-run','tenant','child','running','now','now')")
    observed = request(observability=LLMObservabilityContext(tenant_id='tenant', session_id='child', run_id='child-run'))
    adapter = provider(manager, lambda _: answer())
    try:
        await adapter.complete_text(observed)
        row = await db.fetchone('SELECT session_id,run_id,execution_attempt_id,comparison_slot_id FROM inference_reservations')
        assert tuple(row) == ('child', 'child-run', 'attempt1', 'slot1')
        async with db.transaction() as conn:
            assert await ComparisonFunding(db).observed_cost_in(conn, 'attempt1') == 5
    finally:
        await adapter.aclose()


async def test_prepaid_pool_does_not_bypass_a_smaller_current_run_cap(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    manager.config.limits.usd_per_run = 0.00005
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer())
    try:
        with pytest.raises(LLMProviderError, match='run:run'):
            await adapter.complete_text(observed)
        assert not sends
        assert not await db.fetchall('SELECT id FROM inference_reservations')
    finally:
        await adapter.aclose()


async def test_quote_helper_uses_exact_selected_preset_inside_the_command_transaction(db: Database, pair) -> None:
    await db.execute("UPDATE staff SET model = 'chosen' WHERE id = 'worker1'")
    adapter = SimpleNamespace(endpoint=endpoint())
    config = RuntimeConfig()
    config.presets['chosen'] = ModelPresetConfig(provider='test', model='model', max_output_tokens=1024)
    manager = SimpleNamespace(db=db, config=config, resolve_model=lambda overrides: ([(adapter, 'model')], config.presets[overrides['preset']]))
    async with db.transaction() as conn:
        result = await HostInferenceAdmission(manager).quote_for_member_in(conn, 'worker1', slot_id='quoted', slot=1, allowance_microusd=2000)
    assert result.provider_id == 'test' and result.model == 'model'
    assert result.quote['output_bound'] == 1024 and result.quote['input_bound'] == 50
    assert result.quote['provider_id'] == 'test'


async def test_provider_overrun_is_observed_instead_of_clipped_to_the_pool(db: Database, pair) -> None:
    await fund(db, pair)
    manager, observed = await native_worker(db, pair)
    sends = []
    adapter = provider(manager, lambda sent: sends.append(sent) or answer(raw={'cost': 0.00011, 'prompt_tokens': 2, 'completion_tokens': 3}))
    try:
        await adapter.complete_text(observed)
        async with db.transaction() as conn:
            assert await ComparisonFunding(db).observed_cost_in(conn, 'attempt1') == 110
        with pytest.raises(LLMProviderError, match='prepaid balance'):
            await adapter.complete_text(observed)
        assert len(sends) == 1
        view = await InferenceBudget(db).spend_view(since=None, total_cap=0.0002, provider_caps={})
        assert view['total']['spent_usd'] == 0.00011
        assert view['total']['reserved_usd'] == 0.0001
    finally:
        await adapter.aclose()


async def test_real_capacity_counts_promised_slots_once_and_frees_only_observed_exit(db: Database, pair) -> None:
    await db.execute("UPDATE projects SET settings = ? WHERE id = 'project'", (json.dumps({'orchestrator': {'concurrency': 2, 'concurrency_cap': 2}}),))
    await fund(db, pair)
    team = SimpleNamespace(app=SimpleNamespace(db=db, executions=pair[0]))

    async def active(project_id):
        async with db.transaction() as conn:
            return await Team._active_in(team, conn, project_id)

    async def concurrency(project_id):
        async with db.transaction() as conn:
            return await Team._concurrency_in(team, conn, project_id)

    async def active_in(conn, project_id):
        return await Team._active_in(team, conn, project_id)

    async def concurrency_in(conn, project_id):
        return await Team._concurrency_in(team, conn, project_id)

    async def ready(_):
        return None

    starts = []

    async def start(entry):
        starts.append(entry.staff_id)

    queue = LaunchQueue(active=active, concurrency=concurrency, active_in=active_in, concurrency_in=concurrency_in,
                        ready=ready, free=ready, launch=start, capacity=lambda: None, stagger=lambda: 0)
    try:
        async with queue.admission_guard():
            async with db.transaction() as conn:
                assert await Team._active_in(team, conn, 'project') == 2
                assert not await queue.check_pair_capacity_in(conn, 'project')
        assert (await queue.offer(Entry('project', 'third', 'Third', 'ordinary-task', 3, False, 'operator'))).state == 'queued'
        assert not starts
        await native_worker(db, pair)
        async with db.transaction() as conn:
            assert await Team._active_in(team, conn, 'project') == 2
        await db.execute("UPDATE execution_attempts SET state = 'completed' WHERE id = 'attempt1'")
        async with db.transaction() as conn:
            assert await Team._active_in(team, conn, 'project') == 2
        await db.execute("UPDATE runs SET status = 'completed' WHERE id = 'run'")
        assert await observe_exit(team.app, staff_session_id='staff-session', runtime_ref='run', observed_status='completed')
        async with db.transaction() as conn:
            assert await Team._active_in(team, conn, 'project') == 1
        assert (await queue.offer(Entry('project', 'third', 'Third', 'ordinary-task', 3, False, 'operator'))).state == 'started'
        assert starts == ['third']
        assert (await db.fetchone("SELECT state FROM comparison_funding_slots WHERE id = 'slot1'"))[0] == 'held'
    finally:
        queue.close()


@pytest.mark.parametrize('crossed', [False, True])
async def test_failed_bound_attempt_refunds_only_before_the_durable_runtime_boundary(db: Database, pair, crossed: bool) -> None:
    await fund(db, pair)
    await native_worker(db, pair, launch=False)
    if crossed:
        async with db.transaction() as conn:
            await ComparisonFunding(db).start_launch_in(conn, 'slot1', 'attempt1')
            with pytest.raises(BudgetRefused, match='crossed twice'):
                await ComparisonFunding(db).start_launch_in(conn, 'slot1', 'attempt1')
    await db.execute("UPDATE execution_attempts SET state = 'failed' WHERE id = 'attempt1'")
    await db.execute("UPDATE staff_sessions SET ended_at = 'now' WHERE id = 'staff-session'")
    async with db.transaction() as conn:
        if crossed:
            with pytest.raises(BudgetRefused, match='physical exit or unstarted boundary'):
                await ComparisonFunding(db).release_in(conn, 'slot1')
        else:
            await ComparisonFunding(db).release_in(conn, 'slot1')
    state = (await db.fetchone("SELECT state FROM comparison_funding_slots WHERE id = 'slot1'"))[0]
    assert state == ('held' if crossed else 'released')
