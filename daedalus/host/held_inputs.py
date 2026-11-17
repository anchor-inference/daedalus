"""The operator's words acknowledged and still with the translator, kept until they reach the session.

In a session whose agent works in another language, what the operator sends from the app — a message, an
answer to the agent's question, the answers to its side questions — is acknowledged as soon as the host
has it and translated in the background: the translator may take its whole time limit, and a front that
waited for it showed a frozen composer or a card stuck on "Sending…" for all of it.

Each held input is a row of ``translating_inputs`` from the acknowledgement until it is delivered or
refused. It used to live in memory only, and a restart in between lost the words the operator had been
told were on their way, along with the "translating" the app drew for them. Now the next start lists
them again at once and, once the runs left behind have been resumed, translates and delivers them.

The deliveries of one session go in the order the inputs were acknowledged, whatever order their
translations come back in; the translations themselves run side by side. Each kind of input is
delivered by the code that owns it (:attr:`HeldInputs.kinds`): the session runner for messages and
answers, the side questions for theirs. A delivery that the stopping process interrupts keeps its row,
so a shutdown is never a refusal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.host.session_runner import Attachment, SessionManager

logger = logging.getLogger(__name__)

KINDS = ("message", "answer", "side")
"""What may be held: an operator's message, their answer to an AskUser question, a batch of side answers."""


@dataclass
class HeldInput:
    session_id: str
    id: str
    """The client's id of a message, or one the host made for an answer (``answer:<call>``, ``side:<batch>``)."""
    kind: str
    entry: dict[str, Any]
    """What the app draws while it is held: ``{id, kind, text, from, to, at, files}`` and what the kind adds."""
    delivery: dict[str, Any]
    """What its deliverer needs to hand it on, as JSON: the words as typed, the arguments of the call."""
    attachments: list[Attachment] = field(default_factory=list)


WaitTurn = Callable[[], Awaitable[None]]
Deliver = Callable[[HeldInput, WaitTurn], Awaitable[dict[str, Any]]]
"""Translates a held input, awaits its turn, and hands it on; returns what its ``settled`` event adds
(``state`` of the translation, ``error``), and raises with the reason to refuse it."""


class Stopping(Exception):
    """The process is stopping: the input stays held, and the next start delivers it."""


class HeldInputs:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.kinds: dict[str, Deliver] = {}
        self._held: dict[str, dict[str, dict[str, Any]]] = {}
        """Per session, the entries of its held inputs by id, oldest first: the table's rows as the app reads
        them, kept here so the session detail, which cannot wait on a query, lists them."""
        self._tail: dict[str, asyncio.Future[None]] = {}
        """Per session, the turn of the last delivery placed: the next one waits for it."""
        self._tasks: set[asyncio.Task[Any]] = set()
        self._delivering: set[tuple[str, str]] = set()

    def listed(self, session_id: str) -> list[dict[str, Any]]:
        """The session's held inputs, oldest first."""
        return [dict(entry) for entry in self._held.get(session_id, {}).values()]

    def holds(self, session_id: str, held_id: str) -> bool:
        return held_id in self._held.get(session_id, {})

    def holds_kind(self, session_id: str, kind: str) -> bool:
        return any(entry.get("kind") == kind for entry in self._held.get(session_id, {}).values())

    def entry(self, session_id: str, held_id: str) -> dict[str, Any] | None:
        found = self._held.get(session_id, {}).get(held_id)
        return dict(found) if found is not None else None

    async def load(self) -> None:
        """Read what the last process left held, so a session lists it before it is delivered."""
        self._held = {}
        for row in await self.manager.db.fetchall("SELECT session_id, id, entry FROM translating_inputs ORDER BY seq"):
            self._held.setdefault(row["session_id"], {})[row["id"]] = json.loads(row["entry"])

    async def hold(self, session_id: str, *, held_id: str, kind: str, entry: dict[str, Any], delivery: dict[str, Any], attachments: Sequence[Attachment] = ()) -> None:
        """Keep an input until it is translated and delivered, and start on it.

        The files move out of the front's staging, which the front empties as soon as this returns, into a
        folder of their own that only the delivery clears. The row is written before this returns, so the
        acknowledgement the caller gives next is one a restart keeps."""
        manager = self.manager
        names = [Path(a.name or a.path.name).name for a in attachments]
        kept: list[Attachment] = []
        folder = manager.settings.state_dir / "translating"
        if attachments:
            folder.mkdir(parents=True, exist_ok=True)
        for item in attachments:
            moved = folder / f"{secrets.token_hex(16)}.part"
            item.path.rename(moved)
            kept.append(replace(item, path=moved, name=item.name or item.path.name))
        entry = {**entry, "id": held_id, "kind": kind, "at": entry.get("at") or datetime.now(UTC).isoformat(),
                 "files": names}
        held = HeldInput(session_id, held_id, kind, entry, delivery, kept)
        # Placed in the order with no await between the check the caller made and here: two sends racing
        # through an await could otherwise each take the other for the one before.
        self._held.setdefault(session_id, {})[held_id] = entry
        previous, turn = self._place(session_id)
        try:
            await manager.db.execute(
                "INSERT INTO translating_inputs(session_id, id, kind, entry, delivery, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, held_id, kind, json.dumps(entry, ensure_ascii=False),
                 json.dumps({**delivery, "attachments": [_attachment_record(a) for a in kept]}, ensure_ascii=False), entry["at"]),
            )
        except BaseException:
            self._forget(session_id, held_id)
            self._release(session_id, turn)
            for item in kept:
                item.path.unlink(missing_ok=True)
            raise
        self._start(held, previous, turn)

    async def resume(self) -> int:
        """Deliver what the last process left held, in the order it was acknowledged; how many."""
        rows = await self.manager.db.fetchall("SELECT session_id, id, kind, entry, delivery FROM translating_inputs ORDER BY seq")
        started = 0
        for row in rows:
            if (row["session_id"], row["id"]) in self._delivering:
                continue
            if row["kind"] not in self.kinds:
                # Its deliverer did not install this time: the row waits for a start where it does.
                logger.warning("session %s: a held %s has nothing to deliver it; it stays held", row["session_id"], row["kind"])
                continue
            delivery = json.loads(row["delivery"])
            attachments = [_attachment_of(record) for record in delivery.pop("attachments", [])]
            entry = json.loads(row["entry"])
            self._held.setdefault(row["session_id"], {})[row["id"]] = entry
            previous, turn = self._place(row["session_id"])
            self._start(HeldInput(row["session_id"], row["id"], row["kind"], entry, delivery, attachments), previous, turn)
            started += 1
        if started:
            logger.warning("%d message(s) acknowledged before the restart are being translated and delivered", started)
        return started

    async def stop(self) -> None:
        """Interrupt the deliveries of a stopping process; their rows stay for the next start."""
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _place(self, session_id: str) -> tuple[asyncio.Future[None] | None, asyncio.Future[None]]:
        previous = self._tail.get(session_id)
        turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._tail[session_id] = turn
        return previous, turn

    def _release(self, session_id: str, turn: asyncio.Future[None]) -> None:
        if not turn.done():
            turn.set_result(None)
        if self._tail.get(session_id) is turn:
            self._tail.pop(session_id, None)

    def _forget(self, session_id: str, held_id: str) -> None:
        self._held.get(session_id, {}).pop(held_id, None)
        if not self._held.get(session_id):
            self._held.pop(session_id, None)

    def _start(self, held: HeldInput, previous: asyncio.Future[None] | None, turn: asyncio.Future[None]) -> None:
        task = asyncio.create_task(self._deliver(held, previous, turn), name=f"translate-send:{held.session_id}")
        self._tasks.add(task)
        self._delivering.add((held.session_id, held.id))

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            self._delivering.discard((held.session_id, held.id))
            if not finished.cancelled() and finished.exception() is not None:
                logger.warning("the delivery of a held %s failed: %s", held.kind, finished.exception())

        task.add_done_callback(done)

    async def announce(self, session_id: str, settled: dict[str, Any] | None = None) -> None:
        from daedalus.host.session_runner import HostEvent, HostEventType  # Lazy: the runner imports this module

        manager = self.manager
        payload: dict[str, Any] = {"session_id": session_id, "translating": self.listed(session_id)}
        if settled is not None:
            payload["settled"] = settled
        state = manager._states.get(session_id)
        try:
            await manager._notify_sinks(session_id, HostEvent(HostEventType.TRANSLATING, (state.run_id if state is not None else "") or "", payload))
        except Exception:  # noqa: BLE001 — the list is on the session; a window that missed the event reads it there
            logger.warning("could not announce the translating messages of session %s", session_id, exc_info=True)

    async def _deliver(self, held: HeldInput, previous: asyncio.Future[None] | None, turn: asyncio.Future[None]) -> None:
        manager = self.manager
        session_id = held.session_id
        settled: dict[str, Any] = {"id": held.id, "kind": held.kind, "text": str(held.entry.get("text") or "")}
        if held.entry.get("ask_ids"):
            settled["ask_ids"] = list(held.entry["ask_ids"])
        if held.entry.get("question"):
            settled["question"] = held.entry["question"]

        async def wait_turn() -> None:
            if previous is not None and not previous.done():
                # Its outcome is its own to report; only its place in the order matters here.
                await asyncio.wait([previous])
            if manager.shutting_down:
                raise Stopping

        keep = False
        try:
            # Said by the delivery itself, so it always reaches a window before the same input's "settled".
            await self.announce(session_id)
            settled.update(outcome="sent", **await self.kinds[held.kind](held, wait_turn))
        except asyncio.CancelledError:
            keep = True
            raise
        except Exception as exc:  # noqa: BLE001 — whatever refused it, the words go back to the operator
            if manager.shutting_down or isinstance(exc, Stopping):
                keep = True
            else:
                logger.warning("session %s: a translated %s could not be delivered: %s", session_id, held.kind, exc)
                settled.update(outcome="refused", error=str(exc) or type(exc).__name__)
        finally:
            self._release(session_id, turn)
            # Kept, neither the row nor the files go: the next start delivers it, and the app keeps drawing it.
            if not keep:
                self._forget(session_id, held.id)
                for item in held.attachments:
                    item.path.unlink(missing_ok=True)
                try:
                    await manager.db.execute("DELETE FROM translating_inputs WHERE session_id = ? AND id = ?", (session_id, held.id))
                except Exception:  # noqa: BLE001 — a row left behind is delivered again, where a receipt or a closed question stops it
                    logger.warning("session %s: could not clear a delivered held %s", session_id, held.kind, exc_info=True)
                await self.announce(session_id, settled)


def _attachment_record(item: Attachment) -> dict[str, Any]:
    return {"path": str(item.path), "name": item.name, "mime_type": item.mime_type, "caption": item.caption, "content_sha256": item.content_sha256}


def _attachment_of(record: dict[str, Any]) -> Attachment:
    from daedalus.host.session_runner import Attachment  # Lazy: the runner imports this module

    return Attachment(path=Path(record["path"]), name=record.get("name"), mime_type=record.get("mime_type") or "application/octet-stream",
                      caption=record.get("caption"), content_sha256=record.get("content_sha256"))


__all__ = ["KINDS", "Deliver", "HeldInput", "HeldInputs", "Stopping", "WaitTurn"]
