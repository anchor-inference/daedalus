"""Contained writers keep a durable project claim through uncertain process outcomes."""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from daedalus.extensions.staff import Team
from daedalus.stores.control import ControlConflict
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.staff import StaffError
from daedalus.stores.writer_leases import KEY, WriterLeases


async def _owner(db: Database) -> ExecutionStore:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    store = ExecutionStore(db)
    store.acquire()
    await store.boot()
    return store


async def _attempt(db: Database, generation: int, *, kind: str = "cli", strict: bool = True) -> None:
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('task','Work','doing',3,'project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('worker','project','Worker','codex','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                     " VALUES ('staff-session','worker','cli','task','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO execution_attempts(id,task_id,contract_revision,host_generation,"
                     "fence_token_hash,state,created_at,updated_at,runtime_kind,staff_session_id,"
                     "provider_session_ref,runtime_instance)"
                     " VALUES ('attempt','task',1,?,'digest','starting','2026-01-01','2026-01-01',?,"
                     "'staff-session','terminal:terminal','daemon')",
                     (generation, kind))
    if strict:
        await db.execute("INSERT INTO operation_receipts(id,scope_kind,scope_id,project_id,actor_id,"
                         "operation_kind,client_operation_id,payload_hash,entity_revision,state,response_json,created_at)"
                         " VALUES ('receipt','project','project','project','operator','profile','one','digest',1,'committed','{}','2026-01-01')")
        await db.execute("INSERT INTO resource_profile_versions(project_id,revision,state,memory_bytes,"
                         "cpu_millis,process_count,disk_bytes,receipt_id,created_at)"
                         " VALUES ('project',1,'enabled',16777216,10,1,0,'receipt','2026-01-01')")
        await db.execute("INSERT INTO attempt_resource_bindings(attempt_id,project_id,profile_revision,"
                         "host_generation,env,daemon_instance,limits_json,state,created_at,updated_at)"
                         " VALUES ('attempt','project',1,?,'container','daemon','{}','reserved','2026-01-01','2026-01-01')",
                         (str(generation),))


async def test_two_contained_writers_cannot_claim_the_same_project(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        with pytest.raises(ControlConflict, match="still owns"):
            await leases.acquire("project")
        assert await leases.release_if_safe(first)
        second = await leases.acquire("project")
        assert second.revision == first.revision + 1
        assert not await leases.release_if_safe(first)
        assert (await db.kv_get(KEY))["state"] == "held"
    finally:
        owner.release()


async def test_two_projects_cannot_bypass_the_claim_with_the_same_folder(db: Database) -> None:
    owner = await _owner(db)
    try:
        await db.execute("INSERT INTO projects(id,name,created_at,settings)"
                         " VALUES ('other','Other','2026-01-01','{}')")
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        with pytest.raises(ControlConflict, match="still owns"):
            await leases.acquire("other")
        assert await leases.release_if_safe(first)
        second = await leases.acquire("other")
        assert second.revision == first.revision + 1
    finally:
        owner.release()


async def test_unclaimed_existing_contained_process_blocks_first_claim(db: Database) -> None:
    owner = await _owner(db)
    try:
        await _attempt(db, owner.generation)
        with pytest.raises(ControlConflict, match="earlier contained worker"):
            await WriterLeases(owner).acquire("project")
        assert await db.kv_get(KEY) is None
    finally:
        owner.release()


async def test_unknown_process_keeps_claim_and_stale_generation_cannot_release(db: Database, monkeypatch) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        await leases.begin_effects(first)
        await _attempt(db, first.host_generation)
        await leases.bind(first, "attempt")

        async def unknown(_conn, _attempt_id):
            return False

        monkeypatch.setattr("daedalus.stores.writer_leases.attempt_released_in", unknown)
        assert not await leases.release_if_safe(first)
        with pytest.raises(ControlConflict, match="still owns"):
            await leases.acquire("project")
    finally:
        owner.release()
    next_owner = ExecutionStore(db)
    next_owner.acquire()
    try:
        assert await next_owner.boot() == first.host_generation + 1
        with pytest.raises(ControlConflict, match="still owns"):
            await WriterLeases(next_owner).acquire("project")
        assert not await WriterLeases(next_owner).release_if_safe(first)
    finally:
        next_owner.release()


async def test_exact_empty_container_exit_releases_bound_attempt(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        await leases.begin_effects(first)
        await _attempt(db, first.host_generation)
        await leases.bind(first, "attempt")
        handoff = await leases.begin_handoff("attempt")
        await db.execute("UPDATE attempt_resource_bindings SET state = 'released',launch_id = 'launch'"
                         " WHERE attempt_id = 'attempt'")
        await db.execute("INSERT INTO attempt_resource_observations(id,attempt_id,host_generation,"
                         "daemon_instance,launch_id,observation_kind,enforced,populated,observed_at)"
                         " VALUES ('exit','attempt',?,'daemon','launch','exit',1,0,'2026-01-01')",
                         (str(first.host_generation),))
        await db.execute("INSERT INTO runtime_exit_observations(attempt_id,runtime_ref,provider_session_ref,"
                         "staff_session_id,contract_revision,host_generation,runtime_kind,runtime_instance,"
                         "observed_status,observed_at)"
                         " VALUES ('attempt','terminal','terminal:terminal','staff-session',1,?,'cli',"
                         "'daemon','exited','2026-01-01')", (first.host_generation,))
        assert not await leases.release_if_safe(first)
        with pytest.raises(ControlConflict, match="still owns"):
            await leases.acquire("project")
        await leases.finish_handoff("attempt", handoff)
        assert await leases.release_if_safe(first)
        assert (await leases.acquire("project")).revision == first.revision + 1
    finally:
        owner.release()


async def test_bind_refuses_an_uncontained_or_native_attempt(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        lease = await leases.acquire("project")
        await leases.begin_effects(lease)
        await _attempt(db, lease.host_generation, kind="daedalus", strict=False)
        with pytest.raises(ControlConflict, match="strict CLI"):
            await leases.bind(lease, "attempt")
        assert not await leases.release_if_safe(lease)
    finally:
        owner.release()


async def test_crash_during_file_handoff_keeps_an_unbound_claim(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        await leases.begin_effects(first)
        assert not await leases.release_if_safe(first)
    finally:
        owner.release()
    next_owner = ExecutionStore(db)
    next_owner.acquire()
    try:
        await next_owner.boot()
        with pytest.raises(ControlConflict, match="still owns"):
            await WriterLeases(next_owner).acquire("project")
    finally:
        next_owner.release()


async def test_unknown_followup_handoff_stays_held_across_restart(db: Database, monkeypatch) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        first = await leases.acquire("project")
        await leases.begin_effects(first)
        await _attempt(db, first.host_generation)
        await leases.bind(first, "attempt")
        await leases.begin_handoff("attempt")

        async def exited(_conn, _attempt_id):
            return True

        monkeypatch.setattr("daedalus.stores.writer_leases.attempt_released_in", exited)
        assert not await leases.release_if_safe(first)
    finally:
        owner.release()
    next_owner = ExecutionStore(db)
    next_owner.acquire()
    try:
        await next_owner.boot()
        with pytest.raises(ControlConflict, match="still owns"):
            await WriterLeases(next_owner).acquire("project")
    finally:
        next_owner.release()


async def test_contained_start_reserves_before_staff_preparation(db: Database) -> None:
    owner = await _owner(db)
    team = object.__new__(Team)
    team.app = SimpleNamespace(executions=owner)
    team._execution_locks = {}
    member = SimpleNamespace(id="worker", project_id="project", isolation="shared")

    async def check_authority():
        pass

    async def prepare(_self, _member, _task, **kwargs):
        row = await db.kv_get(KEY)
        assert (row["state"], row["effects_started"], row["attempt_id"]) == ("held", False, None)
        assert kwargs["writer_lease"] is not None
        await WriterLeases(owner).begin_effects(kwargs["writer_lease"])
        return "prepared"

    team._start = MethodType(prepare, team)
    try:
        assert await team.start(member, SimpleNamespace(), principal=SimpleNamespace(),
                                check_authority=check_authority, resources={}) == "prepared"
        # A preparation that might have sent a write remains held without a bound exit proof.
        assert (await db.kv_get(KEY))["state"] == "held"
    finally:
        owner.release()


async def test_invalid_launch_releases_its_unused_reservation(db: Database) -> None:
    owner = await _owner(db)
    team = object.__new__(Team)
    team.app = SimpleNamespace(executions=owner)
    team._execution_locks = {}
    member = SimpleNamespace(id="worker", project_id="project", isolation="shared")

    async def check_authority():
        pass

    try:
        with pytest.raises(StaffError, match="host-attested"):
            await team.start(member, SimpleNamespace(),
                             principal=SimpleNamespace(origin_class="agent", grant_id=None),
                             check_authority=check_authority, resources={})
        assert (await db.kv_get(KEY))["state"] == "released"
        assert (await WriterLeases(owner).acquire("project")).revision == 2
    finally:
        owner.release()


async def test_readonly_staff_does_not_wait_for_the_writer_claim(db: Database) -> None:
    owner = await _owner(db)
    team = object.__new__(Team)
    team.app = SimpleNamespace(executions=owner)
    team._execution_locks = {}
    member = SimpleNamespace(id="reader", project_id="project", isolation="readonly")
    await WriterLeases(owner).acquire("project")

    async def check_authority():
        pass

    async def prepare(_self, _member, _task, **kwargs):
        assert kwargs["writer_lease"] is None
        return "read"

    team._start = MethodType(prepare, team)
    try:
        assert await team.start(member, SimpleNamespace(), principal=SimpleNamespace(),
                                check_authority=check_authority, resources={}) == "read"
    finally:
        owner.release()


async def test_uncontained_start_does_not_claim_a_contained_writer_lease(db: Database) -> None:
    owner = await _owner(db)
    team = object.__new__(Team)
    team.app = SimpleNamespace(executions=owner)
    team._execution_locks = {}
    member = SimpleNamespace(id="ordinary", project_id="project", isolation="shared")

    async def check_authority():
        pass

    async def prepare(_self, _member, _task, **kwargs):
        assert kwargs["writer_lease"] is None
        return "ordinary"

    team._start = MethodType(prepare, team)
    try:
        assert await team.start(member, SimpleNamespace(), principal=SimpleNamespace(),
                                check_authority=check_authority, resources=None) == "ordinary"
        assert await db.kv_get(KEY) is None
    finally:
        owner.release()


async def test_failed_file_delivery_keeps_the_contained_attempt_claim(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        lease = await leases.acquire("project")
        await leases.begin_effects(lease)
        await _attempt(db, lease.host_generation)
        await leases.bind(lease, "attempt")
        team = object.__new__(Team)
        team.app = SimpleNamespace(executions=owner)
        team.manager = SimpleNamespace(db=db)
        live = SimpleNamespace(id="staff-session", session=SimpleNamespace(task_id="task"))

        async def live_of(_self, _member):
            return live

        async def cwd_of(_self, _live):
            return SimpleNamespace(), "folder"

        async def hand_files(_self, *_args, **_kwargs):
            raise RuntimeError("copy outcome unknown")

        team.live_of = MethodType(live_of, team)
        team.cwd_of = MethodType(cwd_of, team)
        team.hand_files = MethodType(hand_files, team)
        with pytest.raises(RuntimeError, match="unknown"):
            await team.tell(SimpleNamespace(), "message", files=[SimpleNamespace()])
        assert len((await db.kv_get(KEY))["handoffs"]) == 1
        assert not await leases.release_if_safe(lease)
    finally:
        owner.release()
