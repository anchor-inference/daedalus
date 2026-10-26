"""The status of a command-line staff session, decided in one place.

Adapters report what happened (``StaffEvent``); only ``next_state`` decides what that means for the
status the operator and the orchestrator see. It is a pure function so that the whole table can be
tested pair by pair, and so that five adapters cannot drift into five meanings of "done".

Three rules shape the table more than any other:

- A notification that the CLI is idle or wants input never becomes ``question``. Those fire on a
  timer while the prompt sits empty; mapping them to a question is how another orchestrator showed
  hundreds of phantom "needs input" badges.
- Silence is grey, never red. A working session with no signal is checked on screen, and only two
  identical readings of an idle composer infer that the turn ended; anything less certain is
  ``no_signal``. Only a turn that is running can fall silent: activity that is not a turn (a
  summary the CLI writes of an idle prompt, a server's chatter between turns) never makes an idle
  member working.
- A session waiting on a request stays waiting while any request of it is still open, whatever else
  arrives: the operator must not lose the one thing that needs them under a burst of activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from daedalus.harness.contract import EventKind, ScreenClass, StaffEvent


class StaffState(StrEnum):
    STARTING = "starting"
    WORKING = "working"
    TURN_DONE_UNSEEN = "turn_done_unseen"
    IDLE = "idle"
    QUESTION = "question"
    PERMISSION = "permission"
    ERROR = "error"
    EXITED = "exited"
    NO_SIGNAL = "no_signal"
    SUSPENDED = "suspended"
    """The host stopped the idle CLI and kept its native session. No event of the CLI reaches it,
    since the CLI is not running; the host's own launch on the next message sets it ``starting``."""


WAITING = frozenset({StaffState.QUESTION, StaffState.PERMISSION})


@dataclass(frozen=True, slots=True)
class OpenRequest:
    kind: str
    """``permission`` or ``question``."""
    waiting_for: str


@dataclass(frozen=True, slots=True)
class StateContext:
    """What the machine needs besides the event, supplied by the runtime."""

    first_prompt_pending: bool = False
    """The first prompt was given (on the command line or to go by channel) and not yet acknowledged."""
    open_requests: tuple[OpenRequest, ...] = ()
    """The session's requests still open *after* this event: for ``request_resolved`` it no longer
    holds the one just resolved."""
    waiting_for: str = ""
    """The current ``waiting_for``, kept when the state does not change."""
    turn_ended: bool = False
    """The CLI's turn ended while a request was open (it told the model "pending" and stopped).
    Answering that request then leaves a CLI with nothing to do, not a working one: its answer goes
    to it as the next message, which waits for exactly that."""


@dataclass(frozen=True, slots=True)
class Transition:
    state: StaffState
    waiting_for: str = ""
    inferred: bool = False
    """The state was read off the screen rather than reported by the CLI."""
    changed: bool = False
    signal: bool = False
    """The event was a structured signal from the CLI: it refreshes ``last_signal_at``."""
    reconcile: bool = False
    """The runtime should read the screen now and report ``reconciled``."""


_ACTIVITY = frozenset({EventKind.TURN_STARTED, EventKind.TOOL_STARTED, EventKind.TOOL_FINISHED, EventKind.ACTIVITY})
_ENDING = frozenset({EventKind.SESSION_ENDED, EventKind.PROCESS_EXITED})
_SILENT = frozenset({EventKind.QUIET, EventKind.RECONCILED, EventKind.SEEN, EventKind.PROCESS_EXITED})
"""Events that are not the CLI speaking: timers, the screen, the operator, the daemon."""


def _stay(current: StaffState, context: StateContext, *, signal: bool, reconcile: bool = False) -> Transition:
    return Transition(current, context.waiting_for, signal=signal, reconcile=reconcile)


def _to(current: StaffState, state: StaffState, *, waiting_for: str = "", inferred: bool = False, signal: bool) -> Transition:
    # ``changed`` is settled in ``next_state``, which also sees the current ``waiting_for``.
    return Transition(state, waiting_for, inferred=inferred, signal=signal)


def _waiting(open_requests: tuple[OpenRequest, ...]) -> tuple[StaffState, str]:
    """The waiting state for what is still open. A permission outranks a question: it holds the
    process itself, while a question only holds the conversation."""
    for request in open_requests:
        if request.kind == "permission":
            return StaffState.PERMISSION, request.waiting_for
    return StaffState.QUESTION, open_requests[0].waiting_for


def _summary(event: StaffEvent) -> str:
    return str(event.payload.get("summary") or "")[:200]


def next_state(current: StaffState, event: StaffEvent, context: StateContext = StateContext()) -> Transition:
    """Where ``event`` takes a session that is in ``current``.

    A session starts in ``starting``: the orchestrator's claim creates its row that way, so there is
    no "launched" event. ``exited`` is final: a new launch is a new session row. ``changed`` is true
    when the status or what it waits for differs, which is when the row is written and
    ``staff.status`` published.
    """
    step = _next(current, event, context)
    changed = step.state != current or step.waiting_for != context.waiting_for
    return Transition(step.state, step.waiting_for, inferred=step.inferred, changed=changed, signal=step.signal, reconcile=step.reconcile)


def _next(current: StaffState, event: StaffEvent, context: StateContext) -> Transition:
    kind = event.kind
    signal = kind not in _SILENT
    if current in (StaffState.EXITED, StaffState.SUSPENDED):
        # A suspended session hears nothing until the host launches it again; whatever a CLI on its
        # way out still says must not set the row of a stopped process working.
        return _stay(current, context, signal=False)
    if kind in _ENDING:
        return _to(current, StaffState.EXITED, signal=signal)
    if kind is EventKind.TURN_FAILED:
        failure = str(event.payload.get("failure") or "the turn failed")[:200]
        return _to(current, StaffState.ERROR, waiting_for=failure, signal=signal)
    if kind is EventKind.PERMISSION_REQUESTED:
        return _to(current, StaffState.PERMISSION, waiting_for=_summary(event), signal=signal)
    if kind is EventKind.QUESTION_ASKED:
        # A question arriving while a permission is open does not hide the permission.
        if current is StaffState.PERMISSION:
            return _stay(current, context, signal=signal)
        return _to(current, StaffState.QUESTION, waiting_for=_summary(event), signal=signal)
    if current in WAITING:
        return _from_waiting(current, event, context, signal=signal)

    if kind is EventKind.READY:
        if current is StaffState.STARTING and not context.first_prompt_pending:
            return _to(current, StaffState.IDLE, signal=signal)
        return _stay(current, context, signal=signal)
    if kind is EventKind.PROMPT_ACKNOWLEDGED:
        # The one way out of ``error``: someone gave it work again.
        return _to(current, StaffState.WORKING, signal=signal)
    if kind is EventKind.ACTIVITY and current in (StaffState.IDLE, StaffState.TURN_DONE_UNSEEN):
        # Bare activity says the CLI is alive, not that a turn began: Claude Code writes an "away
        # summary" three minutes into an idle prompt and its hooks fire for it. Taking that for work
        # set three finished members working, the silence check then found them quiet, and the
        # orchestrator was woken to hear that idle members had "gone silent". A turn starts with a
        # prompt, a turn start or a tool, each of which says so.
        return _stay(current, context, signal=signal)
    if kind is EventKind.TOOL_FINISHED and current in (StaffState.IDLE, StaffState.TURN_DONE_UNSEEN):
        # The end of a tool belongs to a turn that is already under way: every CLI reports the tool's
        # start first. Seen with the turn over, it is that turn's straggler. A turn interrupted in
        # the middle of a tool sends the tool's end and the cancellation as two hook posts, which
        # can arrive in either order; taken as work, the late tool end set the member working for
        # good, and a message waiting for the turn to end never went.
        return _stay(current, context, signal=signal)
    if kind in _ACTIVITY:
        if current in (StaffState.IDLE, StaffState.TURN_DONE_UNSEEN, StaffState.NO_SIGNAL):
            return _to(current, StaffState.WORKING, signal=signal)
        if current is StaffState.STARTING and kind is not EventKind.ACTIVITY:
            # The CLI began a turn on a first prompt it was given on its command line.
            return _to(current, StaffState.WORKING, signal=signal)
        return _stay(current, context, signal=signal)
    if kind is EventKind.TURN_COMPLETED:
        if current in (StaffState.WORKING, StaffState.NO_SIGNAL, StaffState.STARTING):
            return _to(current, StaffState.TURN_DONE_UNSEEN, signal=signal)
        return _stay(current, context, signal=signal)
    if kind is EventKind.TURN_CANCELLED:
        if current in (StaffState.WORKING, StaffState.NO_SIGNAL, StaffState.STARTING):
            return _to(current, StaffState.IDLE, signal=signal)
        return _stay(current, context, signal=signal)
    if kind is EventKind.SEEN:
        if current is StaffState.TURN_DONE_UNSEEN:
            return _to(current, StaffState.IDLE, signal=False)
        return _stay(current, context, signal=False)
    if kind is EventKind.QUIET:
        return _stay(current, context, signal=False, reconcile=current is StaffState.WORKING)
    if kind is EventKind.RECONCILED:
        return _reconciled(current, event, context)
    # NOTIFICATION, USAGE, TRANSCRIPT, REQUEST_RESOLVED with nothing open: the CLI is alive and said
    # something, which is all they mean for the status.
    return _stay(current, context, signal=signal)


def _from_waiting(current: StaffState, event: StaffEvent, context: StateContext, *, signal: bool) -> Transition:
    kind = event.kind
    if kind is EventKind.REQUEST_RESOLVED or (kind is EventKind.PROMPT_ACKNOWLEDGED and not context.open_requests):
        if context.open_requests:
            state, waiting_for = _waiting(context.open_requests)
            return Transition(state, waiting_for, signal=signal)
        if kind is EventKind.REQUEST_RESOLVED and context.turn_ended:
            return _to(current, StaffState.TURN_DONE_UNSEEN, signal=signal)
        return _to(current, StaffState.WORKING, signal=signal)
    if kind is EventKind.TURN_CANCELLED:
        # A cancelled turn withdraws what it asked; the runtime withdraws the requests themselves.
        return _to(current, StaffState.IDLE, signal=signal)
    if kind is EventKind.TURN_COMPLETED and not context.open_requests:
        return _to(current, StaffState.TURN_DONE_UNSEEN, signal=signal)
    return _stay(current, context, signal=signal)


def _reconciled(current: StaffState, event: StaffEvent, context: StateContext) -> Transition:
    """The screen reconcile's verdict. ``idle_composer`` means two identical readings of an idle
    composer, ``gap`` apart; the runtime reports ``unknown`` for anything less."""
    if current not in (StaffState.WORKING, StaffState.NO_SIGNAL):
        return _stay(current, context, signal=False)
    try:
        screen = ScreenClass(str(event.payload.get("screen") or "unknown"))
    except ValueError:
        screen = ScreenClass.UNKNOWN
    if screen is ScreenClass.IDLE_COMPOSER:
        return _to(current, StaffState.TURN_DONE_UNSEEN, inferred=True, signal=False)
    if screen is ScreenClass.BUSY:
        return _to(current, StaffState.WORKING, inferred=current is not StaffState.WORKING, signal=False)
    # A dialog nobody reported is not guessed at: it may be a permission the hooks missed, or the
    # CLI's own menu. It is shown as silence with the reason, and the operator looks.
    waiting_for = "a dialog is open on screen" if screen is ScreenClass.DIALOG else ""
    return _to(current, StaffState.NO_SIGNAL, waiting_for=waiting_for, inferred=True, signal=False)


__all__ = ["WAITING", "OpenRequest", "StaffState", "StateContext", "Transition", "next_state"]
