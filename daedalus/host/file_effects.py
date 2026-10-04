"""Local file writes retain their actual executor futures until revocation drains every mutation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from daedalus.host.filesystem import LocalFS
from daedalus.stores.control import ControlConflict
from daedalus.stores.writer_leases import WriterEffect, WriterLease, WriterLeases


class ClaimedLocalFS(LocalFS):
    """One claim-bound write owner; cancellation of its caller does not cancel an admitted thread.

    Recreating an entered owner is refused. A restart keeps the writer claim held because an old
    thread's completion cannot be inferred from a fresh Python object or a newer SQL generation.
    """

    leases: WriterLeases
    lease: WriterLease
    effect: WriterEffect
    _writes: list[asyncio.Future[None]]
    _revocation: asyncio.Task[dict[str, Any]] | None

    def __init__(self) -> None:
        raise TypeError("a claimed local filesystem must enter through its asynchronous factory")

    @classmethod
    async def create(cls, leases: WriterLeases, lease: WriterLease, *, owner_instance: str,
                     operation_id: str) -> ClaimedLocalFS:
        effect = await leases.register_effect(lease, kind="file", owner_instance=owner_instance,
                                               operation_id=operation_id)
        await leases.enter_file_owner(lease, effect)
        instance = object.__new__(cls)
        instance.leases, instance.lease, instance.effect = leases, lease, effect
        instance._lock = asyncio.Lock()
        instance._closed = False
        instance._writes = []
        instance._revocation = None
        return instance

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    async def write_text(self, path: Path, content: str) -> None:
        async with self._lock:
            if self._closed or len(self._writes) >= 256:
                raise ControlConflict("the local file owner is revoked or its mutation frontier is full")
            await self.leases.check_file_admission(self.lease, self.effect)
            # A Task wrapping to_thread can be marked cancelled while the thread still writes.
            # Retain the executor Future itself and shield every consumer from cancelling it.
            future = asyncio.get_running_loop().run_in_executor(None, self._write_text, path, content)
            self._writes.append(future)
            future.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        await asyncio.shield(future)

    async def revoke(self) -> dict[str, Any]:
        """Close admission durably, then wait for all accepted mkdir/write threads to settle."""
        async with self._lock:
            self._closed = True
            if self._revocation is None:
                await self.leases.observe_effect(self.lease, self.effect, state="revoking")
                self._revocation = asyncio.create_task(self._drain())
            pending = self._revocation
        receipt = await asyncio.shield(pending)
        return {**receipt, "mutations": list(receipt["mutations"])}

    async def _drain(self) -> dict[str, Any]:
        await asyncio.shield(asyncio.gather(*self._writes, return_exceptions=True))
        if any(future.cancelled() or not future.done() for future in self._writes):
            raise ControlConflict("the actual local write outcome is unknown")
        receipt = {"effect_id": self.effect.id, "owner_instance": self.effect.owner_instance,
                   "project_id": self.lease.project_id, "claim_revision": self.lease.revision,
                   "host_generation": self.lease.host_generation,
                   "mutations": ["failed" if future.exception() is not None else "written" for future in self._writes]}
        await self.leases.finish_file_barrier(self.lease, self.effect, receipt)
        return receipt
