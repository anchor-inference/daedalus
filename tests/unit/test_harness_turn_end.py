"""A command-line member's turn end: hooks that come after Claude Code's ``Stop`` are not a new turn,
and a turn end that was missed all the same is read from the screen, so the member's next message is
not held behind a turn that is over.

What the operator saw: a Claude Code member finished ("Brewed for 3m 5s · done 8:38 PM", an empty
prompt), four seconds later a hook set it "working" again with no prompt, and it stayed so — the
screen check never recognised Claude's idle prompt under the operator's own status line — while the
orchestrator's next task sat queued for after the turn and the orchestrator said the member was
still busy.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from daedalus.config import HarnessConfig
from daedalus.harness.claude import ClaudeCodeAdapter, _Launch
from daedalus.harness.contract import EventKind, HookPost, ScreenClass, StaffEvent
from daedalus.harness.runtime import CliStaffRuntime
from daedalus.harness.state import StaffState, StateContext, next_state

RULE = "─" * 150
FINISHED = "\n".join([
    "  Скрипт сборки лежит в той же папке. Ничего не коммитил и не пушил.",
    "",
    "✻ Brewed for 3m 5s · done 8:38 PM",
    "",
    "※ recap: пять роликов ускорены и лежат в папке задачи; жду следующую задачу.",
    "",
    RULE + " nils · Скрыть 7 репо ─",
    "❯ ",
    RULE,
    "  [Opus 5.5 · medium] ████░░░░░░ 39% (388k/1.0M) | dev/ascorblack | Usage 5h 4% (resets in 4h 52m)",
    "  ⏵⏵ accept edits on (shift+tab to cycle) · ← 1 agent",
])
"""The member's screen as it was, the operator's status line and the permission mode in the footer
where "? for shortcuts" would be."""


def working(spinner: str) -> str:
    return FINISHED.replace("✻ Brewed for 3m 5s · done 8:38 PM", spinner)


def test_claudes_idle_prompt_is_recognised_under_a_status_line_of_the_operators_own() -> None:
    adapter = ClaudeCodeAdapter()
    assert adapter.classify_screen(FINISHED) is ScreenClass.IDLE_COMPOSER
    assert adapter.classify_screen(working("✶ Brewing… (12s · ↓ 1.2k tokens)")) is ScreenClass.BUSY
    assert adapter.classify_screen(working("✻ Connection refused — a firewall may be blocking it · Retrying in 3s · attempt 2/10")) is ScreenClass.BUSY
    assert adapter.classify_screen(FINISHED.replace("← 1 agent", "esc to interrupt")) is ScreenClass.BUSY


def post(name: str, **body: Any) -> HookPost:
    return HookPost(name, body)


def test_tool_hooks_after_stop_are_not_a_turn_and_the_next_prompt_opens_one() -> None:
    adapter = ClaudeCodeAdapter()
    launch = _Launch()

    def kinds(*posts: HookPost) -> list[EventKind]:
        return [e.kind for p in posts for e in adapter._map(None, launch, p)]  # type: ignore[arg-type]

    assert kinds(post("UserPromptSubmit", prompt="[orchestrator] speed the videos up"), post("PreToolUse", tool_name="Bash", tool_input={"command": "ffmpeg"}, tool_use_id="t1")) == [EventKind.PROMPT_ACKNOWLEDGED, EventKind.TOOL_STARTED]
    assert kinds(post("PostToolUse", tool_name="Bash", tool_input={"command": "ffmpeg"}, tool_use_id="t1"), post("Stop", last_assistant_message="done")) == [EventKind.TOOL_FINISHED, EventKind.TURN_COMPLETED]
    # Seconds after the turn ended, with no prompt: alive, not at work.
    late = kinds(post("PreToolUse", tool_name="Read", tool_input={"file_path": "notes.md"}, tool_use_id="t2"), post("PostToolUse", tool_name="Read", tool_input={"file_path": "notes.md"}, tool_use_id="t2"))
    assert late == [EventKind.ACTIVITY, EventKind.ACTIVITY]
    assert kinds(post("UserPromptSubmit", prompt="<task-notification>the server exited</task-notification>"), post("PreToolUse", tool_name="Bash", tool_input={}, tool_use_id="t3")) == [EventKind.TURN_STARTED, EventKind.TOOL_STARTED]


def test_the_missed_stop_leaves_the_member_finished_through_the_state_machine() -> None:
    """The sequence on record: the turn ends, and a hook four seconds later. It no longer sets the
    member working."""
    adapter = ClaudeCodeAdapter()
    launch = _Launch()
    state = StaffState.WORKING
    for hook in (post("UserPromptSubmit", prompt="go"), post("Stop", last_assistant_message="done"), post("PreToolUse", tool_name="Read", tool_input={}, tool_use_id="t9")):
        for event in adapter._map(None, launch, hook):  # type: ignore[arg-type]
            state = next_state(state, event, StateContext()).state
    assert state is StaffState.TURN_DONE_UNSEEN


class Screen:
    def __init__(self, text: str) -> None:
        self.text = text
        self.id = "t-nils"

    async def screen(self, *, scrollback: int = 0) -> str:
        return self.text


def runtime(status: list[str], applied: list[StaffEvent], *, clock: list[float]) -> Any:
    """The runtime's silence and idle checks, with the rest of it stubbed out: the status is what the
    applied events make of it through the real state machine."""
    cfg = HarnessConfig.model_construct(**{**HarnessConfig().model_dump(), "reconcile_gap_ms": 10, "idle_settle_s": 60})
    rt: Any = object.__new__(CliStaffRuntime)
    rt.adapter = ClaudeCodeAdapter()
    rt.config = lambda: cfg
    rt.clock = lambda: clock[0]

    async def lookup(_: str) -> Any:
        return SimpleNamespace(session=SimpleNamespace(status=status[0], waiting_for=""))

    async def apply(session: Any, event: StaffEvent) -> None:
        applied.append(event)
        status[0] = next_state(StaffState(status[0]), event, StateContext()).state.value

    async def expects_signal(_: Any) -> bool:
        return False  # the task is in review: the silence alarm has nothing to say

    rt.lookup = lookup
    rt._apply = apply
    rt.ingress = SimpleNamespace(expects_signal=expects_signal)
    return rt


def session(text: str, *, last_signal: float) -> Any:
    return SimpleNamespace(staff_session_id="ss-nils", term=Screen(text), last_signal=last_signal, quiet_checked=0.0, idle_checked=0.0, launch=SimpleNamespace(launch_id="l-nils"))


async def test_an_idle_prompt_under_a_turn_held_as_running_ends_the_turn_after_the_settle() -> None:
    status, applied, clock = ["working"], [], [1000.0]
    rt = runtime(status, applied, clock=clock)
    nils = session(FINISHED, last_signal=990.0)
    await rt._check_quiet(nils, rt.config())
    assert applied == [] and status == ["working"]  # ten seconds is not yet a missed turn end
    clock[0] = 1051.0
    await rt._check_quiet(nils, rt.config())
    assert [e.kind for e in applied] == [EventKind.RECONCILED] and applied[0].payload["screen"] == "idle_composer"
    assert status == ["turn_done_unseen"]


async def test_a_busy_or_changing_screen_is_never_taken_for_a_turn_end() -> None:
    status, applied, clock = ["working"], [], [2000.0]
    rt = runtime(status, applied, clock=clock)
    await rt._check_quiet(session(working("✶ Brewing… (4m 12s · ↓ 9k tokens)"), last_signal=1000.0), rt.config())
    assert applied == [] and status == ["working"]

    class Ticking(Screen):
        """An idle-looking frame that changes between the two readings: a TUI between two frames."""

        def __init__(self) -> None:
            super().__init__(FINISHED)
            self.reads = 0

        async def screen(self, *, scrollback: int = 0) -> str:
            self.reads += 1
            return FINISHED + ("" if self.reads % 2 else " ")

    ticking = session(FINISHED, last_signal=1000.0)
    ticking.term = Ticking()
    await rt._check_quiet(ticking, rt.config())
    assert applied == [] and status == ["working"]


class Ingress:
    """The ingress calls a turn's end makes, recorded; the status kept as the store would keep it."""

    def __init__(self, status: list[str]) -> None:
        self.status_now = status
        self.implicit: list[tuple[str, str, str]] = []

    async def status(self, live: Any, status: str, waiting_for: str = "", *, detail: str = "") -> None:
        self.status_now[0] = status

    async def signal(self, live: Any) -> None:
        pass

    async def resolved(self, live: Any, ref: str, *, by: str = "operator", via: str = "terminal") -> bool:
        return True

    async def message_state(self, message_id: str, state: str, error: str = "") -> None:
        pass

    async def implicit_report(self, live: Any, kind: str, text: str, *, refused: str = "") -> None:
        self.implicit.append((kind, text, refused))


async def test_a_report_in_a_turn_that_ended_waiting_does_not_stand_for_the_next_turn() -> None:
    """A turn reported, then ended with its question still open; the next turn ended with no report,
    and the orchestrator heard nothing of it: the first turn's flag was cleared only on the way to
    ``turn_done_unseen``, which that turn never took."""
    from daedalus.harness.runtime import CliSession
    from daedalus.harness.state import OpenRequest

    status = ["question"]
    ingress = Ingress(status)
    rt: Any = object.__new__(CliStaffRuntime)
    rt.adapter = ClaudeCodeAdapter()
    rt.ingress = ingress
    rt._background = set()
    rt.clock = lambda: 0.0

    async def nothing(_: Any) -> None:
        return None

    rt.usage = nothing
    class Row:
        waiting_for = ""

        @property
        def status(self) -> str:
            return status[0]

    live = SimpleNamespace(id="ss-ada", session=Row())
    ada = CliSession("ss-ada", SimpleNamespace(launch_id="l-ada"), Screen(""), None, "/work")  # type: ignore[arg-type]
    ada.reported = True
    ada.open["team:r1"] = OpenRequest("question", "Euros or dollars?")
    for event in (StaffEvent(EventKind.TURN_COMPLETED, "t1", {"last_message": "asked; waiting"}),
                  StaffEvent(EventKind.REQUEST_RESOLVED, "t2", {"via": "orchestrator"}, native_id="team:r1"),
                  StaffEvent(EventKind.PROMPT_ACKNOWLEDGED, "t3", {"prompt": "[orchestrator] euros"}),
                  StaffEvent(EventKind.TURN_COMPLETED, "t4", {"last_message": "Priced in euros; menu.md updated."})):
        await rt._apply_locked(ada, live, event)
    assert status == ["turn_done_unseen"]
    assert [(kind, text.split(" (")[0]) for kind, text, _ in ingress.implicit] == [("turn_done", "Priced in euros; menu.md updated.")]
