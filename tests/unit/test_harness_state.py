"""The status of a command-line staff session: every state against every event, written out.

The table is the specification. Each row names an event and, in the order of ``ORDER``, where each
state goes; a test fails when an event kind exists that the table does not mention, so a new kind
cannot arrive with an implicit meaning.
"""

from __future__ import annotations

import pytest

from daedalus.harness.contract import EventKind, ScreenClass, StaffEvent
from daedalus.harness.state import OpenRequest, StaffState, StateContext, Transition, next_state

S, W, D, Id, Q, P, E, X, N, Su = (
    StaffState.STARTING,
    StaffState.WORKING,
    StaffState.TURN_DONE_UNSEEN,
    StaffState.IDLE,
    StaffState.QUESTION,
    StaffState.PERMISSION,
    StaffState.ERROR,
    StaffState.EXITED,
    StaffState.NO_SIGNAL,
    StaffState.SUSPENDED,
)
ORDER = (S, W, D, Id, Q, P, E, X, N, Su)

NOTHING_OPEN = StateContext()
FIRST_PROMPT = StateContext(first_prompt_pending=True)
QUESTION_OPEN = StateContext(open_requests=(OpenRequest("question", "which colour?"),))
PERMISSION_OPEN = StateContext(open_requests=(OpenRequest("permission", "npm install grammy"),))

# (case name, event kind, payload, context): targets for S, W, D, Id, Q, P, E, X, N, Su. A suspended
# session is not running, so nothing moves it: the host's own launch sets it starting.
TABLE: list[tuple[str, EventKind, dict[str, str], StateContext, tuple[StaffState, ...]]] = [
    ("ready", EventKind.READY, {}, NOTHING_OPEN, (Id, W, D, Id, Q, P, E, X, N, Su)),
    ("ready, first prompt pending", EventKind.READY, {}, FIRST_PROMPT, (S, W, D, Id, Q, P, E, X, N, Su)),
    ("prompt acknowledged", EventKind.PROMPT_ACKNOWLEDGED, {}, NOTHING_OPEN, (W, W, W, W, W, W, W, X, W, Su)),
    ("prompt acknowledged, a question open", EventKind.PROMPT_ACKNOWLEDGED, {}, QUESTION_OPEN, (W, W, W, W, Q, P, W, X, W, Su)),
    ("turn started", EventKind.TURN_STARTED, {}, NOTHING_OPEN, (W, W, W, W, Q, P, E, X, W, Su)),
    ("tool started", EventKind.TOOL_STARTED, {}, NOTHING_OPEN, (W, W, W, W, Q, P, E, X, W, Su)),
    # A tool's end after the turn is over is that turn's straggler, not a new turn.
    ("tool finished", EventKind.TOOL_FINISHED, {}, NOTHING_OPEN, (W, W, D, Id, Q, P, E, X, W, Su)),
    ("activity", EventKind.ACTIVITY, {}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, W, Su)),
    ("permission requested", EventKind.PERMISSION_REQUESTED, {"summary": "npm install grammy"}, NOTHING_OPEN, (P, P, P, P, P, P, P, X, P, Su)),
    ("question asked", EventKind.QUESTION_ASKED, {"summary": "which colour?"}, NOTHING_OPEN, (Q, Q, Q, Q, Q, P, Q, X, Q, Su)),
    ("request resolved, nothing open", EventKind.REQUEST_RESOLVED, {}, NOTHING_OPEN, (S, W, D, Id, W, W, E, X, N, Su)),
    ("request resolved, a question still open", EventKind.REQUEST_RESOLVED, {}, QUESTION_OPEN, (S, W, D, Id, Q, Q, E, X, N, Su)),
    ("request resolved, a permission still open", EventKind.REQUEST_RESOLVED, {}, PERMISSION_OPEN, (S, W, D, Id, P, P, E, X, N, Su)),
    ("turn completed", EventKind.TURN_COMPLETED, {}, NOTHING_OPEN, (D, D, D, Id, D, D, E, X, D, Su)),
    ("turn completed, a permission open", EventKind.TURN_COMPLETED, {}, PERMISSION_OPEN, (D, D, D, Id, Q, P, E, X, D, Su)),
    ("turn cancelled", EventKind.TURN_CANCELLED, {}, NOTHING_OPEN, (Id, Id, D, Id, Id, Id, E, X, Id, Su)),
    ("turn failed", EventKind.TURN_FAILED, {"failure": "rate limit"}, NOTHING_OPEN, (E, E, E, E, E, E, E, X, E, Su)),
    ("seen", EventKind.SEEN, {}, NOTHING_OPEN, (S, W, Id, Id, Q, P, E, X, N, Su)),
    ("session ended", EventKind.SESSION_ENDED, {}, NOTHING_OPEN, (X, X, X, X, X, X, X, X, X, Su)),
    ("process exited", EventKind.PROCESS_EXITED, {"exit_code": "0"}, NOTHING_OPEN, (X, X, X, X, X, X, X, X, X, Su)),
    ("quiet", EventKind.QUIET, {}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
    ("reconciled: idle composer", EventKind.RECONCILED, {"screen": "idle_composer"}, NOTHING_OPEN, (S, D, D, Id, Q, P, E, X, D, Su)),
    ("reconciled: busy", EventKind.RECONCILED, {"screen": "busy"}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, W, Su)),
    ("reconciled: dialog", EventKind.RECONCILED, {"screen": "dialog"}, NOTHING_OPEN, (S, N, D, Id, Q, P, E, X, N, Su)),
    ("reconciled: unknown", EventKind.RECONCILED, {"screen": "unknown"}, NOTHING_OPEN, (S, N, D, Id, Q, P, E, X, N, Su)),
    ("reconciled: nonsense", EventKind.RECONCILED, {"screen": "sparkles"}, NOTHING_OPEN, (S, N, D, Id, Q, P, E, X, N, Su)),
    ("notification: idle prompt", EventKind.NOTIFICATION, {"type": "idle_prompt"}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
    ("notification: agent needs input", EventKind.NOTIFICATION, {"type": "agent_needs_input"}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
    ("usage", EventKind.USAGE, {}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
    ("transcript", EventKind.TRANSCRIPT, {}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
    # The runtime answers it before the machine; were it ever handed one, nothing would move.
    ("files edited", EventKind.FILES_EDITED, {}, NOTHING_OPEN, (S, W, D, Id, Q, P, E, X, N, Su)),
]


def _cases() -> list[tuple[str, StaffState, EventKind, dict[str, str], StateContext, StaffState]]:
    return [(f"{name} from {state.value}", state, kind, payload, context, target) for name, kind, payload, context, targets in TABLE for state, target in zip(ORDER, targets, strict=True)]


def _step(current: StaffState, kind: EventKind, payload: dict[str, str] | None = None, context: StateContext = NOTHING_OPEN) -> Transition:
    return next_state(current, StaffEvent(kind=kind, at="2026-01-01T00:00:00Z", payload=payload or {}), context)


def test_every_event_kind_and_every_state_has_a_row() -> None:
    covered = {kind for _, kind, *_ in TABLE}
    assert covered == set(EventKind), f"no expectation for {sorted(set(EventKind) - covered)}"
    assert set(ORDER) == set(StaffState)


@pytest.mark.parametrize(("case", "current", "kind", "payload", "context", "target"), _cases(), ids=[c[0] for c in _cases()])
def test_the_transition_table(case: str, current: StaffState, kind: EventKind, payload: dict[str, str], context: StateContext, target: StaffState) -> None:
    assert _step(current, kind, payload, context).state is target, case


@pytest.mark.parametrize("kind", ["idle_prompt", "agent_needs_input", "agent_completed"])
def test_a_notification_is_never_a_question(kind: str) -> None:
    """They fire on a timer while the prompt waits empty; read as questions they were hundreds of
    phantom "needs input" badges in another orchestrator."""
    for state in ORDER:
        step = _step(state, EventKind.NOTIFICATION, {"type": kind, "message": "Claude is waiting for your input"})
        assert step.state is state and not step.changed
        assert step.signal is (state not in (X, Su))


def test_what_it_waits_for_travels_with_the_state() -> None:
    step = _step(W, EventKind.PERMISSION_REQUESTED, {"summary": "npm install grammy"})
    assert (step.state, step.waiting_for, step.changed) == (P, "npm install grammy", True)
    step = _step(W, EventKind.TURN_FAILED, {"failure": "authentication failed"})
    assert (step.state, step.waiting_for) == (E, "authentication failed")
    # A second permission while one is shown changes what the row says, not the state.
    step = _step(P, EventKind.PERMISSION_REQUESTED, {"summary": "rm -rf build"}, StateContext(waiting_for="npm install grammy"))
    assert (step.state, step.waiting_for, step.changed) == (P, "rm -rf build", True)
    # Resolving one of two leaves the other one's words.
    step = _step(P, EventKind.REQUEST_RESOLVED, {}, StateContext(open_requests=(OpenRequest("question", "which colour?"),), waiting_for="npm install grammy"))
    assert (step.state, step.waiting_for, step.changed) == (Q, "which colour?", True)
    # A question asked while a permission is open keeps the permission's words on the row.
    step = _step(P, EventKind.QUESTION_ASKED, {"summary": "which colour?"}, StateContext(waiting_for="npm install grammy"))
    assert (step.state, step.waiting_for, step.changed) == (P, "npm install grammy", False)
    assert len(_step(W, EventKind.PERMISSION_REQUESTED, {"summary": "x" * 500}).waiting_for) == 200


def test_an_answer_after_the_turn_ended_leaves_the_session_done_not_working() -> None:
    # The hold on a team question ran out, the CLI heard "pending" and ended its turn; the answer
    # that comes later goes to it as a message, which waits for a session that is not working.
    step = _step(Q, EventKind.REQUEST_RESOLVED, {}, StateContext(turn_ended=True))
    assert (step.state, step.changed) == (D, True)
    # Answered while the turn still waits on its call: back to work, as before.
    assert _step(Q, EventKind.REQUEST_RESOLVED, {}, NOTHING_OPEN).state is W
    # Another request still open outranks the ended turn.
    assert _step(Q, EventKind.REQUEST_RESOLVED, {}, StateContext(open_requests=PERMISSION_OPEN.open_requests, turn_ended=True)).state is P


def test_silence_is_checked_on_screen_and_shown_grey() -> None:
    quiet = _step(W, EventKind.QUIET)
    assert (quiet.state, quiet.reconcile, quiet.signal, quiet.changed) == (W, True, False, False)
    assert not any(_step(state, EventKind.QUIET).reconcile for state in ORDER if state is not W)

    done = _step(W, EventKind.RECONCILED, {"screen": ScreenClass.IDLE_COMPOSER.value})
    assert (done.state, done.inferred, done.signal) == (D, True, False)
    unknown = _step(W, EventKind.RECONCILED, {"screen": ScreenClass.UNKNOWN.value})
    assert (unknown.state, unknown.inferred) == (N, True)
    assert unknown.state is not E
    dialog = _step(W, EventKind.RECONCILED, {"screen": ScreenClass.DIALOG.value})
    assert (dialog.state, dialog.waiting_for) == (N, "a dialog is open on screen")
    # Busy confirms work: from working that is nothing new, from silence it is back to working.
    assert not _step(W, EventKind.RECONCILED, {"screen": "busy"}).inferred
    assert _step(N, EventKind.RECONCILED, {"screen": "busy"}).inferred


def test_which_events_count_as_a_signal() -> None:
    """``last_signal_at`` is the CLI speaking; timers, the screen, the operator and the daemon are not."""
    silent = {EventKind.QUIET, EventKind.RECONCILED, EventKind.SEEN, EventKind.PROCESS_EXITED}
    for kind in EventKind:
        assert _step(W, kind, {"screen": "busy"}).signal is (kind not in silent), kind
    assert not _step(X, EventKind.ACTIVITY).signal


def test_error_is_left_only_by_new_work() -> None:
    leaving = {kind for kind in EventKind if _step(E, kind, {"summary": "s", "screen": "idle_composer"}).state not in (E,)}
    assert leaving == {EventKind.PROMPT_ACKNOWLEDGED, EventKind.PERMISSION_REQUESTED, EventKind.QUESTION_ASKED, EventKind.SESSION_ENDED, EventKind.PROCESS_EXITED}


def test_exited_is_final() -> None:
    for kind in EventKind:
        step = _step(X, kind, {"summary": "s", "screen": "busy"}, QUESTION_OPEN)
        assert (step.state, step.changed, step.signal) == (X, False, False)


def test_a_scripted_session_reads_as_the_operator_would_tell_it() -> None:
    """Launched with a first prompt, asked for a permission, granted, finished, looked at."""
    script = [
        (EventKind.READY, {}, FIRST_PROMPT, S),
        (EventKind.PROMPT_ACKNOWLEDGED, {}, NOTHING_OPEN, W),
        (EventKind.TOOL_STARTED, {}, NOTHING_OPEN, W),
        (EventKind.PERMISSION_REQUESTED, {"summary": "npm test"}, NOTHING_OPEN, P),
        (EventKind.ACTIVITY, {}, PERMISSION_OPEN, P),
        (EventKind.REQUEST_RESOLVED, {}, NOTHING_OPEN, W),
        (EventKind.NOTIFICATION, {"type": "idle_prompt"}, NOTHING_OPEN, W),
        (EventKind.TURN_COMPLETED, {}, NOTHING_OPEN, D),
        (EventKind.SEEN, {}, NOTHING_OPEN, Id),
        (EventKind.PROCESS_EXITED, {}, NOTHING_OPEN, X),
    ]
    state, seen = S, []
    for kind, payload, context, _ in script:
        state = _step(state, kind, payload, context).state
        seen.append(state)
    assert seen == [expected for *_, expected in script]


def test_a_finished_member_stays_idle_through_activity_that_is_not_a_turn() -> None:
    """Three members had reported, finished and sat at their prompts; three minutes later Claude Code
    wrote its summary of the idle prompt, and its hooks took each of them back to working. Five quiet
    minutes after that all three were "silent" and the orchestrator was woken for it."""
    for finished in (D, Id):
        step = _step(finished, EventKind.ACTIVITY, {"hook": "SubagentStop"})
        assert (step.state, step.changed, step.signal) == (finished, False, True)
        assert not _step(finished, EventKind.QUIET).reconcile
    # A turn still begins the ways a turn begins.
    for kind in (EventKind.PROMPT_ACKNOWLEDGED, EventKind.TURN_STARTED, EventKind.TOOL_STARTED):
        assert _step(Id, kind).state is W, kind
    # And a silent turn that speaks again is working again.
    assert _step(N, EventKind.ACTIVITY).state is W
