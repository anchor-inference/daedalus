"""Side questions: what an agent would like to ask the operator but need not wait for.

A long session picks up small questions along the way — which of two names reads better, whether an
old script may go — that are the operator's to settle and block nothing. AskUser stops the run until
it is answered, which is right for what the agent cannot continue without and wrong for these. A side
question is left in the Questions tab of the session's right panel instead (``SideQuestion``), the
agent keeps working, and the operator answers when they get to it. The answers reach the session as
one message: a steer into the turn it is in, or a new turn when it is idle, through the same
:meth:`SessionManager.submit` every other message takes, with any files the operator attached
ingested the way a chat upload is.

The agent takes a question back as soon as it settles it itself (``DropSideQuestion``), so the
operator never answers what no longer matters; it may take back only its own. Each session has a
small number open at once, so a model that asks about everything is told to drop some first.

The rows live in ``asks`` beside the orchestrators' requests, with origin ``session``, the asking
session's id and no project: no project's list, count or orchestrator ever reads them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from daedalus.stores.staff import TITLE_MAX, Ask, StaffError

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.held_inputs import HeldInput, WaitTurn
    from daedalus.host.session_runner import Attachment, SessionManager, SessionState

logger = logging.getLogger(__name__)

TOOL = "SideQuestion"
"""The tool whose availability decides whether a session may leave side questions at all."""
OPEN_MAX = 6
"""Side questions one session may have waiting at once. A handful is what a person answers between
two other things; more is a model asking about everything instead of deciding."""
QUESTION_MAX = 600
WHY_MAX = 1500
OPTIONS_MAX = 6
OPTION_MAX = 120
REASON_MAX = 300
ORIGIN = "side_questions"
"""The origin of the message that carries the answers into the session, which the app draws as a note."""


class Refused(ValueError):
    """A side question that cannot be left or taken back, said so the agent can do the right thing instead."""


def _manager(app: Application) -> SessionManager:
    manager = app.manager
    assert manager is not None
    return manager


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _line(ask: Ask) -> str:
    return f"[{ask.short_id}] {ask.heading}"


def may_ask(manager: SessionManager, state: SessionState) -> bool:
    """Whether this session leaves side questions: wherever its tool is not refused to it.

    The tool's own blocking is the one rule. The companion, an orchestrator, the main orchestrator,
    staff and subagents each have their own way to the operator (or to whoever leads them), and the
    lists in :mod:`daedalus.config` take the tool from them; asking the same list here means no other
    rule can drift from it."""
    if manager.is_dispatcher(state):
        return False
    registry = manager.registry_for(state)
    return registry.get(TOOL) is not None and TOOL not in manager.blocked_tools_for(state)


async def ask(app: Application, *, session_id: str, question: str, why: str = "", options: Sequence[str] = ()) -> str:
    """Leave a side question for the operator; return at once with its short id."""
    manager = _manager(app)
    state = await manager.get_state(session_id)
    if state is None:
        raise Refused("no such session")
    if not may_ask(manager, state):
        raise Refused("this session does not leave side questions; ask the way your role asks")
    words = str(question or "").strip()
    reason = str(why or "").strip()
    if not words:
        raise Refused("a side question needs the question itself")
    if len(words) > QUESTION_MAX:
        raise Refused(f"keep the question to {QUESTION_MAX} characters; put the background in why")
    if len(reason) > WHY_MAX:
        raise Refused(f"why is at most {WHY_MAX} characters")
    choices = [_clean(o) for o in options if _clean(o)]
    if len(choices) > OPTIONS_MAX or any(len(o) > OPTION_MAX for o in choices):
        raise Refused(f"at most {OPTIONS_MAX} options of {OPTION_MAX} characters each")
    open_now = await manager.asks.of_session(session_id)
    same = next((a for a in open_now if _clean(a.text.split("\n\n", 1)[0]).lower() == _clean(words).lower()), None)
    if same is not None:
        raise Refused(f"that question is already waiting as [{same.short_id}]; do not ask it twice")
    if len(open_now) >= OPEN_MAX:
        listed = "; ".join(_line(a) for a in open_now)
        raise Refused(f"{OPEN_MAX} side questions already wait for the operator ({listed}). Drop the ones you have settled "
                      "with DropSideQuestion, or decide this one yourself")
    text = words + (f"\n\n{reason}" if reason else "")
    # The run that asked, so a stop can take back what that run left open and nothing older.
    run_id = state.run_id if state.running else None
    try:
        made = await manager.asks.open(None, origin="session", kind="question", text=text, routed_to="operator",
                                       detail={"options": list(dict.fromkeys(choices)), "run_id": run_id}, session_id=session_id)
    except StaffError as exc:
        raise Refused(str(exc)) from exc
    await _announce(manager, session_id, "opened", [made.id])
    if manager.translations.pair_of(state) is not None:
        # The operator reads the tab in their own language, as they read the chat; the agent is not kept
        # waiting for it, and the card shows the agent's words until the translation is there.
        manager._spawn_background(manager.translations.ask(made.id, asked=False), f"translate-ask:{made.id}")
    others = [a for a in open_now if a.id != made.id]
    tail = f" Also waiting: {'; '.join(_line(a) for a in others)}." if others else ""
    return (f"left for the operator as [{made.short_id}]. Keep working — the answer arrives later as a message of its own. "
            f"Drop it with DropSideQuestion as soon as you settle it yourself.{tail}")


async def withdraw(app: Application, *, session_id: str, ids: Sequence[str], reason: str) -> str:
    """Take back side questions of this session's own, each with the agent's reason."""
    manager = _manager(app)
    why = _clean(reason)
    if not why:
        raise Refused("say why in a few words (the operator sees it): found it in the README, the plan changed, …")
    if len(why) > REASON_MAX:
        raise Refused(f"the reason is at most {REASON_MAX} characters")
    refs = [str(i).strip().strip("[]") for i in ids if str(i).strip()]
    if not refs:
        raise Refused("name the questions to drop by their short ids")
    lines: list[str] = []
    dropped: list[Ask] = []
    for ref in dict.fromkeys(refs):
        found = await _own(manager, session_id, ref)
        if found is None:
            lines.append(f"[{ref}]: not one of your side questions")
            continue
        if not found.open:
            lines.append(f"[{found.short_id}]: {_closed_words(found)}")
            continue
        if await manager.asks.resolve(found.id, "system", {"closed": why, "via": "withdrawn", "by": "agent"}):
            dropped.append(found)
        else:
            current = await manager.asks.get(found.id)
            lines.append(f"[{found.short_id}]: {_closed_words(current or found)}")
    if dropped:
        await _announce(manager, session_id, "withdrawn", [a.id for a in dropped], reason=why)
    if not dropped:
        raise Refused("nothing dropped: " + "; ".join(lines))
    head = "dropped " + ", ".join(f"[{a.short_id}]" for a in dropped)
    return head + ("; " + "; ".join(lines) if lines else "")


STOPPED_REASON = "the operator stopped the run that asked it"


async def withdraw_stopped(app: Application, session_id: str, run_id: str) -> list[Ask]:
    """Take back the side questions a run the operator stopped left open.

    A side question outlives the turn that asked it when the turn ends on its own: the answer starts
    the next one. A stopped run is different: the operator ended the work the question was about,
    and the question stayed in the panel asking about it (one blamed the operator's ad blocker for
    a block of our own and was still waiting after the cancel). Questions from earlier runs, and
    those made before questions carried their run, stay."""
    manager = _manager(app)
    dropped: list[Ask] = []
    for found in await manager.asks.of_session(session_id):
        if not run_id or found.detail.get("run_id") != run_id:
            continue
        if await manager.asks.resolve(found.id, "system", {"closed": STOPPED_REASON, "via": "withdrawn", "by": "stop"}):
            dropped.append(found)
    if dropped:
        await _announce(manager, session_id, "withdrawn", [a.id for a in dropped], reason=STOPPED_REASON)
    return dropped


async def _own(manager: SessionManager, session_id: str, ref: str) -> Ask | None:
    """A side question of this session by its id or short id, the latest when a short id was used twice."""
    row = await manager.db.fetchone(
        "SELECT id FROM asks WHERE session_id = ? AND (id = ? OR short_id = ?) ORDER BY resolved_at IS NULL DESC, created_at DESC LIMIT 1",
        (session_id, ref, ref.lower()),
    )
    return await manager.asks.get(row["id"]) if row is not None else None


def answer_words(ask: Ask) -> str:
    """What the operator answered, in one line, as the agent and a refusal read it."""
    r = ask.resolution
    picked = ", ".join(str(s) for s in r.get("selected") or [])
    words = str(r.get("text") or "")
    note = str(r.get("note") or "")
    said = f"{picked} — {note}" if picked and note else picked or words
    return said or "(no words)"


def _closed_words(ask: Ask) -> str:
    if ask.resolution.get("via") == "withdrawn":
        return "already dropped"
    if ask.resolution.get("dismissed"):
        return "the operator put it aside unanswered"
    return f"already answered by the operator: {answer_words(ask)}"


async def dismiss(app: Application, *, session_id: str, ask_id: str) -> Ask:
    """The operator puts a side question aside without answering it. The agent is not woken for it:
    a question set aside is one the operator did not want to spend a turn on either."""
    manager = _manager(app)
    found = await _own(manager, session_id, ask_id)
    if found is None:
        raise KeyError(ask_id)
    if not found.open:
        raise Refused(_closed_words(found))
    if not await manager.asks.resolve(found.id, "operator", {"via": "dismissed", "dismissed": True}):
        current = await manager.asks.get(found.id)
        raise Refused(_closed_words(current or found))
    await _announce(manager, session_id, "dismissed", [found.id])
    return (await manager.asks.get(found.id)) or found


async def waiting(app: Application, session_id: str) -> list[Ask]:
    return await _manager(app).asks.of_session(session_id)


async def record(app: Application, ask: Ask, shape: dict[str, Any], *, via: str, batch_id: str, files: Sequence[str]) -> bool:
    """Close one side question with the operator's answer. True for the answer that got there first."""
    resolution: dict[str, Any] = {"text": str(shape.get("text") or ""), "selected": list(shape.get("selected") or []), "via": via, "batch": batch_id}
    if shape.get("note"):
        resolution["note"] = str(shape["note"])
    if files:
        resolution["files"] = list(files)
    return await _manager(app).asks.resolve(ask.id, "operator", resolution)


def delivery_text(answered: Sequence[tuple[Ask, Sequence[str]]], said: dict[str, str] | None = None, *, shown: bool = False) -> str:
    """The message that carries a batch of answers into the asking session: each question quoted
    beside its answer, so the agent needs no memory of what it asked to read them. ``said`` holds an
    answer's words as the agent is to read them, when they are not the operator's own (a translation).
    ``shown`` quotes each question as the operator's card showed it, translated when it was: that is the
    operator's side of a translated delivery, which the chat draws while the agent reads the other."""
    blocks = []
    for ask_row, files in answered:
        mark = ask_row.detail.get("translation") if shown else None
        asked = str(mark.get("text") or "") if isinstance(mark, dict) and mark.get("state") == "done" else ""
        question = (asked or ask_row.text).split("\n\n", 1)[0].strip()
        if len(question) > TITLE_MAX * 3:
            question = question[: TITLE_MAX * 3 - 1].rstrip() + "…"
        lines = [f"[{ask_row.short_id}] {question}", f"Answer: {(said or {}).get(ask_row.id) or answer_words(ask_row)}"]
        if files:
            lines.append("Attached with this answer: " + ", ".join(files))
        blocks.append("\n".join(lines))
    head = "Answers to your side questions (the operator answered them in the side panel):"
    return head + "\n\n" + "\n\n".join(blocks)


async def deliver(app: Application, session_id: str, answered: Sequence[tuple[Ask, Sequence[str]]], attachments: Sequence[Attachment], *, hold_as: str = "") -> str:
    """Hand a batch of answers to the session that asked: a steer while it works, a turn when idle.

    Returns the error text when it could not be delivered (no model, the daily budget spent, a
    read-only folder for the files). The questions are then opened again, so an answer that never
    reached the agent is asked for again rather than lost behind a closed row; the caller shows the
    error on each card.

    ``hold_as`` (the app's sends) acknowledges a batch with typed words in a translated session at once:
    it is kept under that id until the translator is done and delivered then, in its place among the
    session's other held inputs (:mod:`daedalus.host.held_inputs`), with ``""`` returned now. A refusal
    after that opens the questions again in the same way and says so on the session's stream."""
    manager = _manager(app)
    state = await manager.get_state(session_id)
    languages = manager.translations.pair_of(state) if state is not None else None
    if hold_as and languages is not None and any(_typed(ask_row).strip() for ask_row, _files in answered):
        manager.held.kinds.setdefault("side", lambda held, wait_turn: _deliver_held(app, held, wait_turn))
        await manager.held.hold(
            session_id, held_id=hold_as, kind="side",
            entry={"text": "\n".join(answer_words(ask_row) for ask_row, _files in answered), "from": languages[0], "to": languages[1],
                   "ask_ids": [ask_row.id for ask_row, _files in answered]},
            delivery={"asks": [{"id": ask_row.id, "files": list(files)} for ask_row, files in answered]},
            attachments=list(attachments),
        )
        # Other windows read the list again, where the questions are now answered and translating.
        await _announce(manager, session_id, "translating", [ask_row.id for ask_row, _files in answered])
        return ""
    said, translated = await _translated(manager, state, answered)
    try:
        await manager.submit(session_id, delivery_text(answered, said), list(attachments), steer=True, as_answer=False, origin=ORIGIN, translated=translated)
    except (RuntimeError, KeyError, OSError) as exc:
        logger.warning("could not hand the side answers to %s", session_id, exc_info=True)
        await _reopen(manager, session_id, answered, str(exc) or type(exc).__name__)
        return f"{exc or type(exc).__name__}; the question waits again"
    await _announce(manager, session_id, "answered", [a.id for a, _ in answered])
    return ""


def _typed(ask_row: Ask) -> str:
    """What the operator wrote in an answer: the note beside a chosen option, or the answer itself."""
    r = ask_row.resolution
    return str(r.get("note") or "") if r.get("selected") else str(r.get("text") or "")


async def _translated(manager: SessionManager, state: SessionState | None, answered: Sequence[tuple[Ask, Sequence[str]]]) -> tuple[dict[str, str], dict[str, Any] | None]:
    """The answers' words as the agent is to read them, and the mark of the message that carries them."""
    languages = manager.translations.pair_of(state) if state is not None else None
    if state is None or languages is None:
        return {}, None
    said: dict[str, str] = {}
    failed = ""
    # The options chosen are the agent's own words already; what the operator typed is translated, as
    # their messages are, so the agent reads one language. The row keeps the words as typed.
    for ask_row, _files in answered:
        picked = ", ".join(str(s) for s in ask_row.resolution.get("selected") or [])
        if (typed := _typed(ask_row)).strip():
            words, error = await manager.translations.answer_words(state, typed)
            failed = failed or error
            said[ask_row.id] = f"{picked} — {words}" if picked else words
    # The chat shows the operator's side, with "show what the agent got", as for a message of theirs; or,
    # when the translator failed, that the words went as written.
    operator, agent = languages
    mark: dict[str, Any] = {"direction": "out", "state": "failed" if failed else "done", "from": operator, "to": agent, "text": delivery_text(answered, shown=True)}
    if failed:
        mark["error"] = failed
    return said, mark


async def _reopen(manager: SessionManager, session_id: str, answered: Sequence[tuple[Ask, Sequence[str]]], reason: str) -> None:
    reopened = [a.id for a, _ in answered if await manager.asks.reopen(a.id)]
    if reopened:
        # With the reason: a window whose answer was held hears here that it came back, and why.
        await _announce(manager, session_id, "opened", reopened, reason=reason[:REASON_MAX])


async def _deliver_held(app: Application, held: HeldInput, wait_turn: WaitTurn) -> dict[str, Any]:
    """A held batch of answers, translated and handed on in its turn; refused with the questions open again."""
    manager = _manager(app)
    session_id = held.session_id
    if (receipt := await manager.live.receipt(session_id, held.id)) is not None and receipt["status"] != "accepted":
        # Handed on by the process before a restart, which stopped before it could clear the row.
        return {}
    answered: list[tuple[Ask, list[str]]] = []
    for item in held.delivery.get("asks") or []:
        if (ask_row := await manager.asks.get(str(item.get("id") or ""))) is not None:
            answered.append((ask_row, [str(name) for name in item.get("files") or []]))
    if not answered:
        raise RuntimeError("the questions are gone")
    state = await manager.get_state(session_id)
    said, translated = await _translated(manager, state, answered)
    await wait_turn()
    try:
        # Under the held id, which a receipt keeps: delivered once even if a restart comes before the row goes.
        await manager.submit(session_id, delivery_text(answered, said), held.attachments, steer=True, as_answer=False, origin=ORIGIN,
                             translated=translated, client_message_id=held.id)
    except (RuntimeError, KeyError, OSError) as exc:
        if manager.shutting_down:
            raise
        logger.warning("could not hand the side answers to %s", session_id, exc_info=True)
        await _reopen(manager, session_id, answered, str(exc) or type(exc).__name__)
        raise RuntimeError(f"{exc or type(exc).__name__}; the question waits again") from exc
    await _announce(manager, session_id, "answered", [a.id for a, _ in answered])
    return {"state": str((translated or {}).get("state") or ""), **({"error": str(translated["error"])[:300]} if translated and translated.get("error") else {})}


async def _announce(manager: SessionManager, session_id: str, change: str, ask_ids: list[str], *, reason: str = "") -> None:
    try:
        await manager.bus.publish("ask.side", {"change": change, "ask_ids": ask_ids, **({"reason": reason} if reason else {})}, session_id=session_id)
    except Exception:  # noqa: BLE001 — the row is the truth; a window that missed the event reads it on its next poll
        logger.warning("could not announce the side questions of %s", session_id, exc_info=True)


async def service(app: Application, op: str, **kwargs: Any) -> str:
    if op == "ask":
        return await ask(app, **kwargs)
    if op == "withdraw":
        return await withdraw(app, **kwargs)
    raise ValueError(op)


async def install(app: Application) -> list[Any]:
    manager = _manager(app)

    async def hook(op: str, **kwargs: Any) -> str:
        return await service(app, op, **kwargs)

    manager.service_hooks["side_questions"] = hook
    # Here as well as where a batch is first held: a batch the last process held is delivered at the start.
    manager.held.kinds["side"] = lambda held, wait_turn: _deliver_held(app, held, wait_turn)

    async def stopped(session_id: str, run_id: str) -> None:
        await withdraw_stopped(app, session_id, run_id)

    manager.stop_hooks.append(stopped)
    return []


__all__ = ["OPEN_MAX", "ORIGIN", "STOPPED_REASON", "Refused", "ask", "deliver", "delivery_text", "dismiss", "install", "may_ask", "record", "waiting", "withdraw", "withdraw_stopped"]
