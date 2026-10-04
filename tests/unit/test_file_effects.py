"""Local revocation waits for actual file threads, including after their caller disappears."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.host.file_effects import ClaimedLocalFS
from daedalus.host.services import SessionServices, locator
from daedalus.stores.control import ControlConflict, ControlDenied, canonical
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.writer_leases import WriterLeases
from daedalus.tools.files import edit_file, multi_edit, write_file
from tests.unit.test_writer_leases import _attempt, _owner


async def _wait_until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("cancel_writer", [False, True])
async def test_revocation_waits_for_the_actual_paused_mutation(db: Database, tmp_path: Path, monkeypatch,
                                                             cancel_writer: bool) -> None:
    owner = await _owner(db)
    leases = WriterLeases(owner)
    lease = await leases.acquire("project")
    fs = await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files")
    started, resume = threading.Event(), threading.Event()
    original = ClaimedLocalFS._write_text

    def paused(path, content):
        started.set()
        if not resume.wait(10):
            raise RuntimeError("the physical write was never resumed")
        original(path, content)

    monkeypatch.setattr(ClaimedLocalFS, "_write_text", staticmethod(paused))
    target = tmp_path / "folder" / "old.txt"
    writer = asyncio.create_task(fs.write_text(target, "old-owner"))
    revocation = None
    try:
        await _wait_until(started.is_set)
        await _attempt(db, lease.host_generation)
        await leases.bind(lease, "attempt")

        async def exited(_conn, _attempt_id):
            return True

        monkeypatch.setattr("daedalus.stores.writer_leases.attempt_released_in", exited)
        if cancel_writer:
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
        assert not fs._writes[0].done()
        revocation = asyncio.create_task(fs.revoke())
        await _wait_until(lambda: fs._revocation is not None)
        assert (await leases.effect_owners())[0]["state"] == "revoking"
        assert not revocation.done()
        assert not target.exists()
        with pytest.raises(ControlConflict, match="revoked"):
            await fs.write_text(tmp_path / "replacement.txt", "replacement")
        with pytest.raises(ControlConflict, match="still owns"):
            await leases.acquire("project")
        assert not await leases.release_if_safe(lease)
        revocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await revocation
        assert not fs._revocation.done()
        assert not fs._writes[0].done()
        resume.set()
        receipt = await asyncio.wait_for(fs.revoke(), timeout=5)
        if not cancel_writer:
            await writer
        assert target.read_text() == "old-owner"
        assert receipt["mutations"] == ["written"]
        persisted = await db.kv_get("local_file_barrier:" + fs.effect.id)
        assert persisted == receipt
        digest = hashlib.sha256(canonical(receipt).encode()).hexdigest()
        assert (await leases.effect_owners())[0]["result_digest"] == digest
        receipt["mutations"].append("failed")
        assert await fs.revoke() == persisted
        with pytest.raises(ControlConflict, match="already entered"):
            await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files")
        assert not await leases.release_if_safe(lease)
    finally:
        resume.set()
        try:
            await asyncio.wait_for(asyncio.shield(writer), timeout=5)
        except asyncio.CancelledError:
            pass
        finally:
            owner.release()


async def test_shutdown_cancelling_the_drain_cannot_publish_thread_completion(db: Database, tmp_path: Path, monkeypatch) -> None:
    owner = await _owner(db)
    leases = WriterLeases(owner)
    lease = await leases.acquire("project")
    fs = await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files")
    started, resume = threading.Event(), threading.Event()
    original = ClaimedLocalFS._write_text

    def paused(path, content):
        started.set()
        if not resume.wait(10):
            raise RuntimeError("the physical write was never resumed")
        original(path, content)

    monkeypatch.setattr(ClaimedLocalFS, "_write_text", staticmethod(paused))
    writer = asyncio.create_task(fs.write_text(tmp_path / "old.txt", "old-owner"))
    try:
        await _wait_until(started.is_set)
        revocation = asyncio.create_task(fs.revoke())
        await _wait_until(lambda: fs._revocation is not None)
        fs._revocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await revocation
        assert not fs._writes[0].done()
        assert await db.kv_get("local_file_barrier:" + fs.effect.id) is None
        resume.set()
        await asyncio.wait_for(writer, timeout=5)
        assert (tmp_path / "old.txt").read_text() == "old-owner"
        assert (await leases.effect_owners())[0]["state"] == "revoking"
        assert await db.kv_get("local_file_barrier:" + fs.effect.id) is None
        assert not await leases.release_if_safe(lease)
    finally:
        resume.set()
        try:
            await asyncio.wait_for(writer, timeout=5)
        finally:
            owner.release()


async def test_restart_does_not_reopen_an_owner_while_its_old_thread_writes(db: Database, tmp_path: Path, monkeypatch) -> None:
    owner = await _owner(db)
    leases = WriterLeases(owner)
    lease = await leases.acquire("project")
    fs = await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files")
    started, resume = threading.Event(), threading.Event()
    original = ClaimedLocalFS._write_text

    def paused(path, content):
        started.set()
        if not resume.wait(10):
            raise RuntimeError("the physical write was never resumed")
        original(path, content)

    monkeypatch.setattr(ClaimedLocalFS, "_write_text", staticmethod(paused))
    target = tmp_path / "old.txt"
    writer = asyncio.create_task(fs.write_text(target, "old-owner"))
    successor = None
    try:
        await _wait_until(started.is_set)
        revocation = asyncio.create_task(fs.revoke())
        await _wait_until(lambda: fs._revocation is not None)
        owner.release()
        await db.close()
        await db.open()
        successor = ExecutionStore(db)
        successor.acquire()
        await successor.boot()
        recovered = WriterLeases(successor)
        assert (await recovered.effect_owners())[0]["state"] == "revoking"
        with pytest.raises(ControlConflict, match="still owns"):
            await recovered.acquire("project")
        with pytest.raises(ControlConflict, match="claim changed"):
            await ClaimedLocalFS.create(recovered, lease, owner_instance="new-host", operation_id="replacement")
        late = tmp_path / "replacement.txt"
        with pytest.raises(ControlConflict, match="revoked"):
            await fs.write_text(late, "new-owner")
        assert not late.exists()
        resume.set()
        await asyncio.wait_for(writer, timeout=5)
        with pytest.raises(ControlDenied, match="generation"):
            await asyncio.wait_for(revocation, timeout=5)
        assert target.read_text() == "old-owner"
        assert await db.kv_get("local_file_barrier:" + fs.effect.id) is None
        assert (await recovered.effect_owners())[0]["state"] == "revoking"
        with pytest.raises(ControlConflict, match="still owns"):
            await recovered.acquire("project")
    finally:
        resume.set()
        try:
            await asyncio.wait_for(writer, timeout=5)
        finally:
            owner.release()
            if successor is not None:
                successor.release()


async def test_duplicate_creation_cannot_split_the_physical_mutation_frontier(db: Database) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        lease = await leases.acquire("project")
        first, second = await asyncio.gather(
            ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files"),
            ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ClaimedLocalFS) for result in (first, second)) == 1
        assert sum(isinstance(result, ControlConflict) for result in (first, second)) == 1
        assert len(await leases.effect_owners()) == 1
        with pytest.raises(TypeError, match="factory"):
            ClaimedLocalFS()
    finally:
        owner.release()


async def test_failed_file_mutation_settles_without_becoming_success(db: Database, tmp_path: Path) -> None:
    owner = await _owner(db)
    try:
        leases = WriterLeases(owner)
        lease = await leases.acquire("project")
        fs = await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id="local-files")
        path = tmp_path / "a-file"
        path.write_text("existing")
        with pytest.raises(OSError):
            await fs.write_text(path / "impossible.txt", "content")
        receipt = await fs.revoke()
        assert receipt["mutations"] == ["failed"]
        assert not await leases.release_if_safe(lease)
        assert await db.kv_get("local_file_barrier:" + fs.effect.id) == receipt
    finally:
        owner.release()


async def test_native_file_tools_share_one_claimed_mutation_frontier(db: Database, tmp_path: Path) -> None:
    owner = await _owner(db)
    session_id = "claimed-file-tools"
    try:
        leases = WriterLeases(owner)
        lease = await leases.acquire("project")
        fs = await ClaimedLocalFS.create(leases, lease, owner_instance="host", operation_id=session_id)
        services = SessionServices(session_id=session_id, workspace_dir=tmp_path, claimed_fs=fs)
        locator.register(services)
        context = ToolContext(tenant_id="t", run_id="r", session_id=session_id,
                              metadata={"tool_call_id": "call"})
        assert services.fs is fs
        written = await write_file().invoke(context, {"path": "note.txt", "content": "one two"})
        edited = await edit_file().invoke(context, {"path": "note.txt", "old_string": "one", "new_string": "three"})
        multiple = await multi_edit().invoke(context, {"path": "note.txt", "edits": [
            {"old_string": "three", "new_string": "four"},
            {"old_string": "two", "new_string": "five"},
        ]})
        assert not written.is_error and not edited.is_error and not multiple.is_error
        assert (tmp_path / "note.txt").read_text() == "four five"
        receipt = await fs.revoke()
        assert receipt["mutations"] == ["written", "written", "written"]
        refused = await write_file().invoke(context, {"path": "after.txt", "content": "late"})
        assert refused.is_error
        assert not (tmp_path / "after.txt").exists()
        assert await db.kv_get("local_file_barrier:" + fs.effect.id) == receipt
        assert not await leases.release_if_safe(lease)
    finally:
        locator.unregister(session_id)
        owner.release()
