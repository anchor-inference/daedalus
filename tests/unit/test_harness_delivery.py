"""The delivery worker's paste path against a terminal that behaves like a TUI's prompt: a paste that
is drawn late, collapsed into a "[Pasted text #N +M lines]" marker, Enters that are lost, and a
session that is still finishing its turn when a message for after it arrives.

The terminal here is a model, not a program: it draws the composer the way Claude Code and Grok
Build draw theirs, so the adapters' own screen readers decide what the worker sees. The whole
sessions against the fake CLIs in real terminals are in the adapters' own test modules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import HarnessConfig
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.delivery import DeliveryWorker, Pending
from daedalus.harness.grok import GrokAdapter

BRIEF = "\n".join(["[orchestrator] Your previous task is closed. Here is your next one."] + [f"line {n} of the brief, long enough to count" for n in range(2, 21)])
"""Twenty lines: collapsed by both TUIs into a paste marker, as the brief that sat unsent was."""


def config(**changes: Any) -> HarnessConfig:
    # Constructed without validation so a test can wait fractions of what the settings allow.
    values = {**HarnessConfig().model_dump(), "ack_timeout_s": 0.5, "submit_settle_ms": 150, "enter_delay_base_ms": 0, "enter_delay_max_ms": 0, "paste_confirm_s": 5.0, "answer_confirm_s": 0.5}
    return HarnessConfig.model_construct(**{**values, **changes})


@dataclass
class Prompt:
    """A TUI's prompt as a terminal shows it. A paste appears ``draw_after`` seconds after it is
    written (a busy or idle-for-long TUI has drawn one that late); the first ``lose`` Enters do
    nothing; any other Enter submits what the composer holds, empties it, and fires the prompt hook
    ``hook_after`` seconds later unless ``hooks`` is off."""

    harness: str
    draw_after: float = 0.0
    lose: int = 0
    hooks: bool = True
    hook_after: float = 0.05
    busy: bool = False
    held: str = ""
    """What the composer shows: the text, or the marker a collapsed paste is drawn as."""
    sent: str = ""
    """What the composer would submit: the whole paste behind a marker."""
    pastes: int = 0
    submitted: list[str] = field(default_factory=list)
    enters: int = 0
    worker: DeliveryWorker | None = None

    def marker(self, text: str) -> str:
        lines = text.count("\n") + 1
        if self.harness == "grok":
            return f"[Pasted: {lines} lines]" if lines >= 4 else text
        return f"[Pasted text #{self.pastes} +{lines - 1} lines]" if len(text) > 800 or lines > 3 else text

    async def write(self, *, text: str | None = None, paste: str | None = None, keys: list[str] | None = None, note: str = "", wait_keyboard: bool = True) -> None:
        loop = asyncio.get_running_loop()
        if paste is not None:
            self.pastes += 1
            shown = self.marker(paste)

            def draw() -> None:
                self.held, self.sent = shown, paste

            loop.call_later(self.draw_after, draw) if self.draw_after else draw()
            return
        if keys == ["Enter"]:
            self.enters += 1
            if self.lose:
                self.lose -= 1
                return
            if not self.held:
                return
            sent = self.sent or self.held
            self.submitted.append(sent)
            self.held = self.sent = ""
            if self.hooks and self.worker is not None:
                worker = self.worker
                loop.call_later(self.hook_after, lambda: worker.acknowledge(sent))

    async def screen(self, *, scrollback: int = 0) -> str:
        if self.harness == "grok":
            footer = "Ctrl+c:cancel" if self.busy else "Enter:send  │  Alt+Enter:newline"
            return "\n".join(["     ❯ earlier prompt", "", "  ╭" + "─" * 60 + "╮", f"  │ ❯ {self.held:<56} │", "  ╰" + "─" * 60 + "╯", "", f"  {footer}"])
        rule = "─" * 120
        footer = "  ⏵⏵ accept edits on · esc to interrupt" if self.busy else "  ⏵⏵ accept edits on (shift+tab to cycle)"
        return "\n".join(["✻ Churned for 1m 51s · done 8:03 PM", "", "※ recap: the script is done and handed in.", "", rule + " mira · promo ─", f"❯ {self.held}", rule, "  [a status line of the operator's own]", footer])


@dataclass
class Receipts:
    states: list[tuple[str, str, str]] = field(default_factory=list)

    async def message_state(self, message_id: str, state: str, error: str = "") -> None:
        self.states.append((message_id, state, error))

    def of(self, message_id: str) -> list[str]:
        return [state for mid, state, _ in self.states if mid == message_id]

    def error(self, message_id: str) -> str:
        return next((error for mid, state, error in reversed(self.states) if mid == message_id and state == "failed"), "")


class Facts:
    def __init__(self) -> None:
        self.recorded: list[Any] = []
        self.enters: dict[str, int] = {}

    async def record_delivery(self, launch_id: str, delivery: Any) -> None:
        self.recorded.append(delivery)

    async def count_enter(self, message_id: str) -> None:
        self.enters[message_id] = self.enters.get(message_id, 0) + 1


@dataclass
class Rig:
    prompt: Prompt
    worker: DeliveryWorker
    receipts: Receipts
    facts: Facts
    status: list[str]

    async def settled(self, message_id: str, timeout: float = 15.0) -> str:
        async def final() -> str:
            while True:
                states = self.receipts.of(message_id)
                if states and states[-1] in ("acknowledged", "failed"):
                    return states[-1]
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(final(), timeout)


def rig(prompt: Prompt, cfg: HarnessConfig, *, status: str = "idle", in_transcript: bool = False,
        guard_send: Any = None) -> Rig:
    adapter: Any = GrokAdapter() if prompt.harness == "grok" else ClaudeCodeAdapter()
    current = [status]
    session: Any = SimpleNamespace(staff_session_id="ss-1", term=prompt, finished=False, open={}, changed=asyncio.Event(), launch=SimpleNamespace(launch_id="l1"))

    async def lookup(_: str) -> Any:
        return SimpleNamespace(session=SimpleNamespace(status=current[0]))

    async def found(_: Any, __: str) -> bool:
        return in_transcript

    receipts, facts = Receipts(), Facts()
    @asynccontextmanager
    async def allow_send(_: str):
        yield

    worker = DeliveryWorker(adapter, session, lookup, receipts, facts, lambda: cfg, found,
                            guard_send or allow_send)  # type: ignore[arg-type]
    prompt.worker = worker
    return Rig(prompt, worker, receipts, facts, current)


@pytest.fixture
async def running() -> Any:
    started: list[asyncio.Task[None]] = []

    def start(r: Rig) -> Rig:
        started.append(r.worker.start())
        return r

    yield start
    for task in started:
        task.cancel()
    await asyncio.gather(*started, return_exceptions=True)


@pytest.mark.parametrize("harness", ["claude", "grok"])
async def test_a_one_line_message_is_submitted_by_one_enter_and_never_retried(harness: str, running: Any) -> None:
    r = running(rig(Prompt(harness), config()))
    r.worker.put(Pending("sm-1", "please also check the footer", "after_turn", "operator"))
    assert await r.settled("sm-1") == "acknowledged"
    assert r.receipts.of("sm-1") == ["written", "submitted", "acknowledged"]
    assert r.prompt.enters == 1 and r.facts.enters == {"sm-1": 1}
    assert r.prompt.submitted == ["please also check the footer"]


async def test_queued_message_rechecks_permission_between_paste_and_enter(running: Any) -> None:
    revoked = False

    class RevokingPrompt(Prompt):
        async def write(self, **kwargs: Any) -> None:
            nonlocal revoked
            await super().write(**kwargs)
            if kwargs.get("paste") is not None:
                revoked = True

    @asynccontextmanager
    async def guard(_: str):
        if revoked:
            raise PermissionError("queued watch permission withdrawn")
        yield

    prompt = RevokingPrompt("claude")
    r = running(rig(prompt, config(), guard_send=guard))
    r.worker.put(Pending("sm-watch", "check the result", "after_turn", "orchestrator"))
    assert await r.settled("sm-watch") == "failed"
    assert prompt.pastes == 1 and prompt.enters == 0 and prompt.submitted == []
    assert "permission withdrawn" in r.receipts.error("sm-watch")


@pytest.mark.parametrize("harness", ["claude", "grok"])
async def test_a_collapsed_paste_left_in_the_prompt_gets_another_enter_and_is_delivered(harness: str, running: Any) -> None:
    """What the operator found: the brief sat in Claude Code's prompt as "[Pasted text #3 +19 lines]"
    with the previous turn finished above it, and nothing ever sent it."""
    r = running(rig(Prompt(harness, lose=2), config()))
    r.worker.put(Pending("sm-2", BRIEF, "after_turn", "orchestrator"))
    assert await r.settled("sm-2") == "acknowledged"
    assert r.prompt.enters == 3 and r.facts.enters == {"sm-2": 3}
    assert r.prompt.submitted == [BRIEF]  # one submission, however many Enters it took
    # "sent" is shown only once the prompt let the brief go, never for an Enter that was lost.
    assert r.receipts.of("sm-2") == ["written", "submitted", "acknowledged"]
    assert [d.state for d in r.facts.recorded] == ["written", "submitted", "acknowledged"]


async def test_a_paste_drawn_seconds_late_is_waited_for_and_then_submitted(running: Any) -> None:
    """The failure on record: the composer showed nothing for the first seconds after the paste, the
    worker gave up without an Enter, and the marker was drawn after it had stopped looking."""
    r = running(rig(Prompt("claude", draw_after=1.2), config(answer_confirm_s=0.5)))
    r.worker.put(Pending("sm-3", BRIEF, "after_turn", "orchestrator"))
    assert await r.settled("sm-3") == "acknowledged"
    assert r.prompt.enters == 1 and r.prompt.submitted == [BRIEF]


async def test_a_message_whose_enters_are_all_lost_is_failed_and_never_shown_as_sent(running: Any) -> None:
    cfg = config(enter_retries=2)
    r = running(rig(Prompt("claude", lose=99), cfg))
    r.worker.put(Pending("sm-4", BRIEF, "after_turn", "orchestrator"))
    assert await r.settled("sm-4") == "failed"
    assert r.receipts.of("sm-4") == ["written", "failed"]
    assert "still in the command-line agent's composer after 3 Enters" in r.receipts.error("sm-4")
    assert r.prompt.enters == 3 and r.prompt.submitted == []


async def test_a_paste_that_never_shows_gets_no_enter_and_says_so(running: Any) -> None:
    r = running(rig(Prompt("claude", draw_after=60), config(paste_confirm_s=0.6)))
    r.worker.put(Pending("sm-5", "a message the prompt never shows", "after_turn", "operator"))
    assert await r.settled("sm-5") == "failed"
    assert r.prompt.enters == 0 and "no Enter was sent" in r.receipts.error("sm-5")


async def test_a_message_for_after_the_turn_goes_in_when_the_turn_ends_and_survives_a_lost_enter(running: Any) -> None:
    """A turn that is just finishing: the message waits for it, and the first Enter, which reaches a
    TUI still settling from the turn, is lost and sent again."""
    prompt = Prompt("claude", lose=1, busy=True)
    r = running(rig(prompt, config(), status="working"))
    r.worker.put(Pending("sm-6", BRIEF, "after_turn", "orchestrator"))
    await asyncio.sleep(0.4)
    assert r.receipts.of("sm-6") == [] and prompt.pastes == 0
    prompt.busy = False
    r.status[0] = "turn_done_unseen"
    assert await r.settled("sm-6") == "acknowledged"
    assert prompt.enters == 2 and prompt.submitted == [BRIEF]


async def test_a_prompt_that_emptied_without_a_word_from_the_cli_stays_submitted(running: Any) -> None:
    """The screen saw the prompt take the message; the hook never came and the transcript has not got
    it yet. That is ``submitted``, which is what is known, and the message is not typed again."""
    r = running(rig(Prompt("claude", hooks=False), config()))
    r.worker.put(Pending("sm-7", "one line", "after_turn", "operator"))
    for _ in range(100):
        if r.worker.inflight is None and r.receipts.of("sm-7"):
            break
        await asyncio.sleep(0.05)
    assert r.receipts.of("sm-7") == ["written", "submitted"]
    assert r.prompt.enters == 1 and r.prompt.pastes == 1


async def test_a_late_transcript_entry_acknowledges_a_submitted_message(running: Any) -> None:
    r = running(rig(Prompt("claude", hooks=False), config(), in_transcript=True))
    r.worker.put(Pending("sm-8", "one line", "after_turn", "operator"))
    assert await r.settled("sm-8") == "acknowledged"
    assert r.receipts.of("sm-8") == ["written", "submitted", "acknowledged"]
