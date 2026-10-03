"""Delivering messages to a command-line staff member: one at a time, only when it can take one,
and never reported further than the CLI itself confirmed.

A message to a CLI is text put into a TUI that a person may be typing into, that may be showing a
dialog, and that may or may not have taken what it was given. So each live session has one worker
that takes its messages in order and, for each:

1. **waits for the window** — the status allows the timing (``after_turn`` waits for the turn to
   end; ``now`` goes into a running turn where the CLI can take one), no request of the session is
   open, and no dialog is on the screen. Nothing is typed while a dialog may be open: an Enter
   there answers the dialog.
2. **hands it over** — through the CLI's structured channel where it has one (the adapter's
   ``send``), or as a paste: the text (or, over the threshold, a one-line pointer to a file in the
   launch directory), a pause that grows with its size, a look at the composer to see the text
   arrived, then Enter.
3. **sees it submitted** — after each Enter the composer is read until it lets the text go. Text
   still there after a short settle was not submitted (the Enter was lost, or came while the TUI was
   still taking the paste) and gets another Enter, each after a longer wait, a few times at most.
   ``submitted`` is reported only once the screen or the CLI has shown it, never for an Enter that
   was merely typed.
4. **waits for the acknowledgement** — the CLI saying it took the prompt (a hook naming the prompt,
   a channel echoing the message's id). Without one it looks again: the text back in the composer
   gets another Enter while retries remain; the text found in the transcript is acknowledged late;
   nothing is ever pasted again blindly — a duplicate instruction is worse than a missing one,
   because nobody notices it.

Every step is a receipt state (``written``, ``submitted``, ``acknowledged``, ``failed``) reported
through the team's ingress, and a fact in ``harness_deliveries``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from daedalus.config import HarnessConfig
from daedalus.harness.contract import Delivery, HarnessAdapter, ScreenClass, SendMode
from daedalus.harness.state import StaffState
from daedalus.staff_runtime import LiveSession, TeamIngress
from daedalus.stores.harness import HarnessStore
from daedalus.terminals.model import Conflict

if TYPE_CHECKING:
    from daedalus.harness.runtime import CliSession

logger = logging.getLogger(__name__)

WINDOW_POLL_S = 0.5
"""How often a waiting message looks at the session again when nothing woke it."""
COMPOSER_POLL_S = 0.2
SETTLE_MAX_S = 8.0
"""The longest wait after one Enter, however many came before it."""
KEYBOARD_RETRY_S = 1.0
"""After a person held the keyboard through a write's wait, the pause before trying again."""
PASTES_MAX = 2
"""A paste a dialog swallowed is pasted once more when the dialog has gone; never a third time."""
MATCH_CHARS = 200
"""How much of a message's start must match a prompt the CLI reports, whitespace aside: enough to
tell two messages apart, and still a match when the CLI trims the end."""

AFTER_TURN_WINDOW = frozenset({StaffState.IDLE, StaffState.TURN_DONE_UNSEEN, StaffState.ERROR})
"""Where a message for after the turn may go in. ``error`` included: a message is how a failed turn
is retried."""
NOW_WINDOW = AFTER_TURN_WINDOW | {StaffState.WORKING, StaffState.NO_SIGNAL}
"""Where a message for now may go in: a busy session too, which is the point of it."""


def normalised(text: str) -> str:
    return " ".join((text or "").split())


def same_prompt(sent: str, reported: str) -> bool:
    """Whether a prompt the CLI reports is a message that was sent: the same start, spacing aside."""
    a, b = normalised(sent), normalised(reported)
    if not a or not b:
        return False
    return a[:MATCH_CHARS] == b[:MATCH_CHARS]


@dataclass(eq=False)
class Pending:
    """A message on its way to the CLI."""

    message_id: str
    """The staff message it is; empty for one with no row (an answer that arrived after its hold
    ended), which is delivered the same way and reported nowhere."""
    text: str
    """As it goes to the CLI: the orchestrator's prefixed, the operator's as typed."""
    mode: SendMode
    origin: str
    degraded_to: str = ""
    sent_text: str = ""
    """What was typed: the text, or the pointer line when it went as a file."""
    acknowledged: asyncio.Future[bool] | None = None
    reexamine: bool = False
    """Taken up after a restart in ``written`` or ``submitted``: looked for, never typed again."""


@dataclass(eq=False)
class DeliveryWorker:
    """The one writer of a live session's messages. See the module's description."""

    adapter: HarnessAdapter
    session: CliSession
    lookup: Callable[[str], Awaitable[LiveSession | None]]
    ingress: TeamIngress
    store: HarnessStore
    config: Callable[[], HarnessConfig]
    in_transcript: Callable[[CliSession, str], Awaitable[bool]]
    guard_send: Callable[[str], AbstractAsyncContextManager[None]]
    waiting: list[Pending] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    inflight: Pending | None = None
    task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        self.task = asyncio.create_task(self._run(), name=f"harness-deliver-{self.session.staff_session_id}")
        return self.task

    def put(self, pending: Pending) -> None:
        self.waiting.append(pending)
        self.arrived.set()

    def size(self) -> int:
        return len(self.waiting) + (1 if self.inflight is not None else 0)

    def _take(self) -> Pending:
        """The next message: one for now or an interrupt before one for after the turn — it is meant
        for the turn running now, and the other waits for that turn to end — else the oldest."""
        urgent = next((p for p in self.waiting if p.mode != "after_turn" and not p.reexamine), None)
        chosen = urgent or self.waiting[0]
        self.waiting.remove(chosen)
        return chosen

    def degraded(self, mode: SendMode) -> str:
        """What a timing becomes for this CLI: ``now`` that it cannot take into a running turn waits
        for the turn's end, or interrupts the turn and sends."""
        steer = self.adapter.capabilities.steer
        if mode == "now" and steer == "degrade_to_queue":
            return "after_turn"
        if mode == "now" and steer == "cancel_and_send":
            return "interrupt"
        return ""

    # -- acknowledgements, from the runtime's events --------------------------------------------

    def acknowledge(self, prompt: str, message_id: str = "") -> str:
        """The CLI took a prompt: the id of the message in flight it is, or empty."""
        pending = self.inflight
        if pending is None or pending.acknowledged is None or pending.acknowledged.done():
            return ""
        if (message_id and message_id == pending.message_id) or (not message_id and same_prompt(pending.sent_text or pending.text, prompt)):
            pending.acknowledged.set_result(True)
            return pending.message_id or "-"
        return ""

    # -- the loop ------------------------------------------------------------------------------

    async def _run(self) -> None:
        while not self.session.finished:
            while not self.waiting:
                self.arrived.clear()
                await self.arrived.wait()
            pending = self._take()
            try:
                await self._one(pending)
            except asyncio.CancelledError:
                raise
            except _Gone:
                return
            except _Overtaken:
                # A message for now arrived while this waited for the turn to end: it goes first, this after.
                self.waiting.insert(0, pending)
                continue
            except Exception as exc:  # noqa: BLE001 — one message failing must not stop the next
                logger.warning("delivering %s to %s failed: %s", pending.message_id or "an answer", self.session.staff_session_id, exc)
                await self._state(pending, "failed", error=str(exc)[:500])
            finally:
                self.inflight = None

    async def _one(self, pending: Pending) -> None:
        if pending.reexamine:
            await self._reexamine(pending)
            return
        cfg = self.config()
        mode: SendMode = pending.mode
        if mode == "now" and pending.degraded_to:
            mode = pending.degraded_to  # type: ignore[assignment]
        if mode == "interrupt":
            await self._interrupt(cfg)
            mode = "after_turn"
        await self._window(mode, yielding=mode == "after_turn" and pending.mode == "after_turn")
        if self.adapter.capabilities.paste is None:
            await self._structured(pending, mode, cfg)
        else:
            await self._paste(pending, cfg)

    async def _interrupt(self, cfg: HarnessConfig) -> None:
        live = await self._live()
        if StaffState(live.session.status) not in (StaffState.WORKING, StaffState.NO_SIGNAL):
            return
        await self.adapter.interrupt(self.session.term)
        deadline = asyncio.get_running_loop().time() + cfg.interrupt_timeout_s
        while asyncio.get_running_loop().time() < deadline:
            live = await self._live()
            if StaffState(live.session.status) not in (StaffState.WORKING, StaffState.NO_SIGNAL):
                return
            await self._nudged(WINDOW_POLL_S)
        # The turn did not say it stopped; the message then waits for the turn's end like any other.

    async def _window(self, mode: SendMode, *, yielding: bool = False) -> None:
        allowed = NOW_WINDOW if mode == "now" else AFTER_TURN_WINDOW
        while True:
            if yielding and any(p.mode != "after_turn" and not p.reexamine for p in self.waiting):
                raise _Overtaken
            live = await self._live()
            try:
                status = StaffState(live.session.status)
            except ValueError:
                status = StaffState.WORKING
            if status in allowed and not self.session.open:
                screen = await self.session.term.screen()
                if self.adapter.classify_screen(screen) is not ScreenClass.DIALOG:
                    return
            await self._nudged(WINDOW_POLL_S)

    async def _nudged(self, seconds: float) -> None:
        """Wait for the session to change, or a moment, whichever comes first."""
        changed = self.session.changed
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), seconds)
        if changed.is_set():
            self.session.changed = asyncio.Event()

    async def _live(self) -> LiveSession:
        live = await self.lookup(self.session.staff_session_id)
        if live is None or self.session.finished:
            raise _Gone
        return live

    # -- a structured channel ------------------------------------------------------------------

    async def _structured(self, pending: Pending, mode: SendMode, cfg: HarnessConfig) -> None:
        pending.acknowledged = asyncio.get_running_loop().create_future()
        pending.sent_text = pending.text
        self.inflight = pending
        async with self.guard_send(pending.message_id):
            delivery = await self.adapter.send(self.session.term, pending.message_id, pending.text, mode)
        await self._record(pending, delivery)
        if delivery.state in ("acknowledged", "failed"):
            await self._state(pending, delivery.state, error=delivery.error)
            return
        await self._state(pending, delivery.state)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending.acknowledged), cfg.ack_timeout_s)
            await self._state(pending, "acknowledged")
        # A channel that took the message and has not echoed it yet leaves it submitted; its echo
        # arrives as an event and moves it on.

    # -- a paste -------------------------------------------------------------------------------

    async def _paste(self, pending: Pending, cfg: HarnessConfig) -> None:
        term = self.session.term
        text, via = pending.text, "paste"
        if len(text.encode()) > cfg.pointer_threshold_bytes:
            name = f"message-{pending.message_id or uuid.uuid4().hex[:12]}.md"
            path = await term.put_file(name, text.encode())
            text, via = f"Read the message in {path} and act on it.", "pointer"
        pending.sent_text = text
        pending.acknowledged = asyncio.get_running_loop().create_future()
        self.inflight = pending
        human = pending.origin == "operator"
        pastes = 0
        while True:
            await self._write(pending, paste=text, human=human, note=f"message {pending.message_id}".strip())
            pastes += 1
            if pastes == 1:
                await self._record(pending, Delivery(pending.message_id, "written", via=via))
                await self._state(pending, "written")
            await asyncio.sleep(self._enter_delay(text, cfg))
            where = await self._composer(pending, text, cfg)
            if where == "holds":
                break
            if where == "lost" and pastes < PASTES_MAX:
                # A dialog took the paste and has gone: the composer is empty, nothing was submitted,
                # so pasting again is the first delivery of the message, not a second.
                await self._window("now" if pending.mode == "now" and not pending.degraded_to else "after_turn")
                continue
            if pending.acknowledged.done():
                await self._state(pending, "acknowledged")
                return
            await self._state(pending, "failed", error=f"the message did not appear in the command-line agent's composer within {cfg.paste_confirm_s:g} s; no Enter was sent")
            return
        await self._submit(pending, text, via, cfg, human=human)

    async def _submit(self, pending: Pending, text: str, via: str, cfg: HarnessConfig, *, human: bool, note: str = "") -> None:
        """Enter until the composer lets the message go, then wait for the CLI's word that it took it.

        A long paste once sat in Claude Code's prompt as "[Pasted text #3 +19 lines]" with nothing
        sending it. Reporting ``submitted`` the moment an Enter was typed would have shown such a
        message as sent; it is reported only when the screen shows the composer emptied, or the CLI
        acknowledged the prompt."""
        if pending.acknowledged is None:
            pending.acknowledged = asyncio.get_running_loop().create_future()
        settle = cfg.submit_settle_ms / 1000
        enters = 0
        submitted = False
        while True:
            await self._write(pending, keys=["Enter"], human=human, note=note or f"submit {pending.message_id}".strip())
            enters += 1
            if pending.message_id:
                with contextlib.suppress(Exception):
                    await self.store.count_enter(pending.message_id)
            seen = await self._taken(pending, text, settle)
            if seen == "held":
                if enters <= cfg.enter_retries:
                    settle = min(settle * 2, SETTLE_MAX_S)
                    continue  # the Enter was lost; the text is still there and nothing was submitted
                await self._state(pending, "failed", error=f"the message is still in the command-line agent's composer after {enters} Enters; it was not submitted")
                return
            if not submitted:
                submitted = True
                await self._record(pending, Delivery(pending.message_id, "submitted", via=via))
                await self._state(pending, "submitted")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending.acknowledged), cfg.ack_timeout_s)
            if pending.acknowledged.done():
                await self._acknowledged(pending, via)
                return
            screen = await self.session.term.screen()
            if self.adapter.classify_screen(screen) is not ScreenClass.DIALOG and self.adapter.composer_holds(screen, text) and enters <= cfg.enter_retries:
                continue  # the composer holds it again: what emptied it was not a submission
            if await self.in_transcript(self.session, text):
                await self._acknowledged(pending, via)
                return
            # The composer let the message go on an Enter and the CLI has not said it took it. It
            # stays ``submitted``, which is what is known; it is not typed again.
            return

    async def _taken(self, pending: Pending, text: str, settle: float) -> str:
        """After an Enter: ``acknowledged`` when the CLI said it took the prompt, ``cleared`` when the
        composer no longer holds the text, ``held`` when it still does once ``settle`` has passed. A
        dialog on the screen stops the clock: an Enter is never sent into one, and what it covers is
        read once it has gone."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle
        while True:
            if pending.acknowledged is not None and pending.acknowledged.done():
                return "acknowledged"
            screen = await self.session.term.screen()
            if self.adapter.classify_screen(screen) is ScreenClass.DIALOG:
                await self._nudged(WINDOW_POLL_S)
                deadline = loop.time() + settle
                continue
            if not self.adapter.composer_holds(screen, text):
                return "cleared"
            if loop.time() >= deadline:
                return "held"
            await asyncio.sleep(COMPOSER_POLL_S)

    async def _composer(self, pending: Pending, text: str, cfg: HarnessConfig) -> str:
        """``holds`` once the composer shows the text; ``lost`` when a dialog came and went and took
        it; ``missing`` when it never showed.

        The paste is given ``paste_confirm_s`` to show: a TUI has drawn a large paste seconds late,
        and giving up at five left it sitting in the prompt with no Enter ever sent. After a dialog
        the wait is the short one again: the dialog is what took the paste, and it is pasted anew."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.paste_confirm_s
        dialog_seen = False
        while True:
            screen = await self.session.term.screen()
            if self.adapter.classify_screen(screen) is ScreenClass.DIALOG:
                # Never an Enter into a dialog; wait until it has gone, however long that is.
                dialog_seen = True
                await self._nudged(WINDOW_POLL_S)
                deadline = loop.time() + min(cfg.answer_confirm_s, cfg.paste_confirm_s)
                continue
            if self.adapter.composer_holds(screen, text):
                return "holds"
            if pending.acknowledged is not None and pending.acknowledged.done():
                return "missing"
            if loop.time() >= deadline:
                return "lost" if dialog_seen else "missing"
            await asyncio.sleep(COMPOSER_POLL_S)

    def _enter_delay(self, text: str, cfg: HarnessConfig) -> float:
        kib = len(text.encode()) / 1024
        delay = min(max(cfg.enter_delay_base_ms + cfg.enter_delay_per_kib_ms * kib, cfg.enter_delay_base_ms), cfg.enter_delay_max_ms)
        rules = self.adapter.capabilities.paste
        return max(delay, rules.burst_guard_ms if rules is not None else 0) / 1000

    async def _write(self, pending: Pending, *, paste: str | None = None, keys: list[str] | None = None,
                     human: bool, note: str) -> None:
        """A write that waits out a person typing: the operator's own message does not wait for
        the operator, anyone else's waits as long as the person keeps typing."""
        while True:
            try:
                async with self.guard_send(pending.message_id):
                    await self.session.term.write(paste=paste, keys=keys, note=note, wait_keyboard=not human)
                return
            except Conflict as exc:
                if exc.details.get("reason") != "keyboard_held":
                    raise
            await asyncio.sleep(KEYBOARD_RETRY_S)
            await self._live()

    async def _acknowledged(self, pending: Pending, via: str) -> None:
        await self._record(pending, Delivery(pending.message_id, "acknowledged", via=via))
        await self._state(pending, "acknowledged")

    # -- after a restart -----------------------------------------------------------------------

    async def _reexamine(self, pending: Pending) -> None:
        """A message a previous host had written or submitted: acknowledged if the CLI has it,
        submitted like any other if it still sits in the composer, failed otherwise — never typed
        again."""
        text = pending.sent_text or pending.text
        if await self.in_transcript(self.session, text):
            await self._state(pending, "acknowledged")
            return
        screen = await self.session.term.screen()
        if self.adapter.classify_screen(screen) is not ScreenClass.DIALOG and self.adapter.composer_holds(screen, text):
            pending.acknowledged = asyncio.get_running_loop().create_future()
            pending.sent_text = text
            self.inflight = pending
            await self._submit(pending, text, "paste", self.config(), human=pending.origin == "operator", note=f"submit {pending.message_id} after a restart")
            return
        await self._state(pending, "failed", error="unknown after restart")

    # -- reporting -----------------------------------------------------------------------------

    async def _record(self, pending: Pending, delivery: Delivery) -> None:
        if not pending.message_id:
            return
        if pending.degraded_to and not delivery.degraded_to:
            # The receipt said at once what became of the timing; the fact row keeps it too, or a
            # later look at a message that waited for the turn could not tell it was asked for now.
            delivery = replace(delivery, degraded_to=pending.degraded_to)  # type: ignore[arg-type]
        with contextlib.suppress(Exception):
            await self.store.record_delivery(self.session.launch.launch_id, delivery)

    async def _state(self, pending: Pending, state: str, *, error: str = "") -> None:
        if pending.message_id:
            await self.ingress.message_state(pending.message_id, state, error)


class _Gone(Exception):
    """The session ended while a message waited: nothing more is delivered to it."""


class _Overtaken(Exception):
    """A message for after the turn, still waiting for it, steps back for one for now that arrived after it."""


def pending_of(message: Any, *, first: bool = False) -> Pending:
    """A stored message as the worker takes it up after a restart."""
    text = str(message.text)
    if message.origin == "orchestrator" and not first:
        text = f"[orchestrator] {text}"
    return Pending(message.id, text, message.mode, message.origin, reexamine=message.state in ("written", "submitted"))


__all__ = ["DeliveryWorker", "Pending", "normalised", "pending_of", "same_prompt"]
