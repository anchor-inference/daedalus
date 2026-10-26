"""Autopilot: when a chat's run ends, one small judge call decides whether the work goes on by itself.

A session with autopilot switched on (and the installation's ``autopilot.enabled``) gets the judge
of :mod:`daedalus.host.autopilot` at the end of every run that answered. Its verdict becomes one of:

- ``continue``: the next goal goes into the session as an ordinary user turn, marked as autopilot's,
  and the run starts — at most ``max_continuations`` times after one message of the operator's;
- ``done``: a notification, through the router like every other;
- ``ask``: a question in the session's list beside its side questions;
- ``choice``: the same with 2–4 options, as chips on the question card, buttons on the notification
  and buttons in Telegram. The app and the chat get the labels; each option's next goal stays in the
  decision row, and a tap sends exactly that goal into exactly that session.

What it never does: judge a run that was stopped, failed or parked, a session that is not an
ordinary chat (an orchestrator, the main orchestrator, the companion, staff, a subagent, a coach), a
session waiting on the operator, or one where the operator wrote meanwhile. Every one of these is
checked when the run ends and again right before the verdict is delivered, since the judge takes
seconds and the operator may have typed, stopped or switched autopilot off in them.

Exactly once. A decision row is written before the judge is asked (one per run), and the key a
continuation or an answered choice is admitted under is written into it before the turn is
submitted; the turn is submitted under that key as its input receipt, so a double tap, two fronts
answering at once or a retry finds the receipt instead of sending a second turn. After a restart, a
decision caught in the middle is closed, never replayed: the judge's verdict was never seen, and an
admission whose receipt did not get as far as a run is unknown — sending it again could be twice.

The cache. The judge is a request of its own, and the continuation is a user turn appended through
:meth:`SessionManager.submit` like the operator's, so the session's prefix — its system prompt and
history — is untouched; the turn context rides at the end of the new message as it does for theirs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Action, ActionConflict, ActionOutcome, ActionRefused, ActionRequest, Draft
from daedalus.host import autopilot as judge
from daedalus.host import predict, user_style
from daedalus.host.notify_text import render
from daedalus.host.session_runner import NOT_OPERATOR_FACING

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager, SessionState
    from daedalus.stores.staff import Ask

logger = logging.getLogger(__name__)

SWITCH_KEY = "autopilot"
"""Session metadata: autopilot is switched on for this session."""

MARK_KEY = "daedalus.autopilot"
"""Transcript metadata on a message autopilot sent: its ``kind`` (continue, choice, answer) and decision."""

JUDGED_ORIGINS = frozenset({"operator", "autopilot", "side_questions", "core"})
"""Runs whose end is judged: those that answer the operator — their message, autopilot's on their
behalf, their answers from the side panel, the host resuming their work after an outage. A schedule,
a loop, a reminder or a page's own report answers nobody waiting, and autopilot does not extend it."""

JUDGE_TIMEOUT_SECONDS = 45
SETTLE_WAIT_SECONDS = 60
"""How long the judge waits for the finished run's housekeeping (its snapshot, the queue it drains)
before it looks: a message queued during the run starts the next one there, and the judge must see it."""
CALLBACK = "ap"
"""The Telegram callback prefix: ``ap:<decision id>:<option index>``."""
REF = "autopilot"
"""The notification request prefix: ``autopilot:<session id>:<decision id>``."""


class Refused(ValueError):
    """An answer to a decision that cannot be taken: gone, of another session, or already answered."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Autopilot:
    def __init__(self, app: Application) -> None:
        self.app = app
        manager = app.manager
        assert manager is not None
        self.manager: SessionManager = manager
        self._tasks: set[asyncio.Task[Any]] = set()
        self._admitting: dict[str, asyncio.Lock] = {}

    @property
    def config(self) -> Any:
        return self.app.config.autopilot

    # -- who it is for --------------------------------------------------------------------------

    def ordinary(self, state: SessionState) -> bool:
        """Whether this is an ordinary chat: what the operator talks to, and nothing that works for
        someone else (staff, a subagent), leads others (an orchestrator, the main one), or lives in a
        window over another chat (a coach, a side question)."""
        manager = self.manager
        if manager.is_orchestrator(state) or manager.is_dispatcher(state) or manager.is_companion(state):
            return False
        if manager.is_staff(state) or manager.lives_in_window(state):
            return False
        return not any(state.metadata.get(key) for key in NOT_OPERATOR_FACING)

    @staticmethod
    def switched_on(state: SessionState) -> bool:
        return bool(state.metadata.get(SWITCH_KEY))

    async def set_switch(self, session_id: str, on: bool) -> dict[str, Any]:
        state = await self.manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if on and not self.ordinary(state):
            raise Refused("autopilot is for ordinary chats; this session has a role of its own")
        for metadata in (state.metadata, state.session.metadata):
            if on:
                metadata[SWITCH_KEY] = True
            else:
                metadata.pop(SWITCH_KEY, None)
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        if not on:
            await self._withdraw_open(session_id, "autopilot was switched off")
        return await self.status(session_id, state)

    async def continuations_used(self, session_id: str) -> int:
        """Turns autopilot started on its own since the operator's last message (or their last tap on a
        choice, which is their decision as much as a message is)."""
        row = await self.manager.db.fetchone(
            "SELECT count(*) AS n FROM transcript WHERE session_id = ? AND json_extract(message, '$.role') = 'user' "
            "AND json_extract(message, '$.metadata.\"daedalus.autopilot\".kind') = 'continue' AND seq > ("
            " SELECT coalesce(max(seq), 0) FROM transcript WHERE session_id = ? AND json_extract(message, '$.role') = 'user' AND ("
            "  json_extract(message, '$.metadata.\"daedalus.origin\"') = 'operator'"
            "  OR json_extract(message, '$.metadata.\"daedalus.autopilot\".kind') = 'choice'))",
            (session_id, session_id),
        )
        return int(row["n"]) if row is not None else 0

    async def status(self, session_id: str, state: SessionState | None = None) -> dict[str, Any]:
        """The session's switch as its menu draws it: on, whether the installation runs autopilot at all,
        and how many of the allowed turns this message of the operator's has had."""
        state = state or await self.manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        return {
            "on": self.switched_on(state),
            "enabled": self.config.enabled,
            "eligible": self.ordinary(state),
            "used": await self.continuations_used(session_id) if self.switched_on(state) else 0,
            "allowed": self.config.max_continuations,
        }

    async def overview(self) -> dict[str, Any]:
        """What the settings show: the day's spend against the allowance and the day's verdicts."""
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = await self.manager.db.fetchall(
            "SELECT verdict, state, count(*) AS n FROM autopilot_decisions WHERE created_at >= ? GROUP BY verdict, state", (start,),
        )
        verdicts: dict[str, int] = {}
        skipped = 0
        for row in rows:
            if row["verdict"]:
                verdicts[str(row["verdict"])] = verdicts.get(str(row["verdict"]), 0) + int(row["n"])
            elif row["state"] in ("skipped", "abandoned"):
                skipped += int(row["n"])
        sessions = await self.manager.db.fetchone(
            "SELECT count(*) AS n FROM sessions WHERE json_valid(metadata) AND json_extract(metadata, '$.autopilot') = 1",
        )
        return {
            "enabled": self.config.enabled,
            "preset": predict.helper_preset(self.manager, self.config.preset),
            "max_continuations": self.config.max_continuations,
            "usd_per_day": self.config.usd_per_day,
            "spent_today": round(await predict.spent_today(self.manager.db, judge.PURPOSE), 4),
            "verdicts_today": verdicts,
            "skipped_today": skipped,
            "sessions_on": int(sessions["n"]) if sessions is not None else 0,
        }

    # -- when a run ends ------------------------------------------------------------------------

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        """Judge a run that answered, off the settling path: the judge is a model call of seconds, and the
        rest of the run's housekeeping (and the app's "saving" mark) must not wait for it."""
        if status != "completed" or not self.config.enabled:
            return
        state = await self.manager.get_state(session_id)
        if state is None or not self.switched_on(state) or not self.ordinary(state):
            return
        if state.run_origin not in JUDGED_ORIGINS or state.last_outcome is not None or state.run_cause is not None:
            return
        task = asyncio.create_task(self.judge(session_id, run_id), name=f"autopilot:{session_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_run_started(self, session_id: str, run_id: str) -> None:
        """The operator wrote: whatever autopilot still asks in this session is about a moment that has
        passed, so it is taken back rather than answered into the new turn."""
        state = await self.manager.get_state(session_id)
        if state is not None and state.run_origin == "operator":
            await self._withdraw_open(session_id, "the operator wrote a newer message")

    async def _blocked(self, state: SessionState) -> str:
        """Why autopilot may not act on this session now, or ``""``."""
        if not self.config.enabled or not self.switched_on(state):
            return "off"
        if not self.ordinary(state):
            return "role"
        if state.running or state.pending is not None or state.compacting is not None:
            return "busy"
        queued = await self.manager.live.load(state.session.id)
        if queued.get("follow_up") or queued.get("steer"):
            return "queued"
        if await self.manager.asks.of_session(state.session.id):
            # A side question or an autopilot question still waits: the operator owes an answer, and
            # going on past it would decide for them what they were asked.
            return "question"
        return ""

    async def _last_seq(self, session_id: str) -> int:
        row = await self.manager.db.fetchone("SELECT coalesce(max(seq), 0) AS last FROM transcript WHERE session_id = ?", (session_id,))
        return int(row["last"]) if row is not None else 0

    async def judge(self, session_id: str, run_id: str) -> str:
        """Ask the judge about a finished run and deliver its verdict; returns what happened, for the log
        and the tests: the verdict delivered, or ``skipped:<reason>``."""
        manager = self.manager
        state = await manager.get_state(session_id)
        if state is None:
            return "skipped:gone"
        housekeeping = state.housekeeping
        if housekeeping is not None and housekeeping is not asyncio.current_task():
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(housekeeping), SETTLE_WAIT_SECONDS)
        blocked = await self._blocked(state)
        if blocked:
            return f"skipped:{blocked}"
        preset = predict.helper_preset(manager, self.config.preset)
        try:
            provider, model = manager.providers.rungs_for(manager.config, preset or None)[0]
        except Exception:  # noqa: BLE001 — no model to judge with is a skip, not an error
            return "skipped:no_model"
        free = manager.provider_costs_nothing(provider.endpoint.id, model)
        if not free and (manager.budget_exceeded() or await predict.spent_today(manager.db, judge.PURPOSE) >= self.config.usd_per_day):
            return "skipped:cap"
        anchor = await self._last_seq(session_id)
        decision_id = uuid.uuid4().hex[:16]
        now = _now()
        async with manager.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO autopilot_decisions(id, session_id, run_id, anchor_seq, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'judging', ?, ?) ON CONFLICT(session_id, run_id) DO NOTHING",
                (decision_id, session_id, run_id, anchor, now, now),
            )
            created = cursor.rowcount > 0
            await cursor.close()
        if not created:
            return "skipped:judged"  # this run was judged already (a callback delivered twice)
        used = await self.continuations_used(session_id)
        lines = await predict.conversation_lines(manager, session_id, tail=judge.TAIL_MESSAGES)
        # A private chat is handed nothing learned from the other chats, the judge's view of it included.
        style = "" if manager.is_private(session_id) else user_style.context_block((await user_style.load(manager.db)).summary)
        try:
            answer = await asyncio.wait_for(
                manager.small_model(
                    judge.request(lines, style=style, used=used, allowed=self.config.max_continuations), preset,
                    purpose=judge.PURPOSE, session_id=session_id, max_tokens=judge.MAX_TOKENS, temperature=0.0, instructions=judge.INSTRUCTIONS,
                ),
                timeout=JUDGE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — no verdict means autopilot does nothing
            logger.info("autopilot could not judge session %s: %s", session_id, exc)
            await self._settle(decision_id, "skipped", reason="the judge failed")
            return "skipped:failed"
        verdict = judge.parse(answer)
        if verdict is None:
            logger.info("autopilot's judge gave no verdict for session %s: %.200s", session_id, answer)
            await self._settle(decision_id, "skipped", reason="the judge's answer was not a verdict")
            return "skipped:unreadable"
        # The judge took seconds. What it judged must still be the session as it is now.
        state = await manager.get_state(session_id)
        blocked = "gone" if state is None else await self._blocked(state)
        if not blocked and await self._last_seq(session_id) != anchor:
            blocked = "newer"
        if blocked:
            await self._settle(decision_id, "skipped", verdict=verdict.kind, reason=f"changed while judging: {blocked}", detail=verdict.detail())
            return f"skipped:{blocked}"
        assert state is not None
        if verdict.kind == "continue":
            exceeded = bool(manager.budget_exceeded()) and not manager.provider_costs_nothing(*await self._session_model(session_id))
            if used >= self.config.max_continuations or exceeded:
                # The budget is spent: no silent turn. The goal is offered instead, one tap away.
                lang = self._lang()
                verdict = judge.Verdict(
                    "choice",
                    reason=render("autopilot.limit", lang, count=used, goal=verdict.next_goal[:300]),
                    options=(judge.Option(render("autopilot.continue", lang), verdict.next_goal),),
                )
            else:
                await self._settle(decision_id, "admitting", verdict="continue", detail=verdict.detail())
                outcome = await self._admit(decision_id, session_id, verdict.next_goal, kind="continue", via="autopilot")
                return "continue" if not outcome else f"skipped:{outcome}"
        if verdict.kind == "done":
            await self._notify_done(state, decision_id, verdict)
            return "done"
        await self._ask(state, decision_id, verdict)
        return verdict.kind

    async def _session_model(self, session_id: str) -> tuple[str | None, str | None]:
        """The provider and model the session's next run would use, as ``submit`` reads them."""
        try:
            rungs, _ = self.manager.resolve_model(await self.manager.live.load(session_id))
            return (rungs[0][0].endpoint.id, rungs[0][1]) if rungs else (None, None)
        except Exception:  # noqa: BLE001 — unknown counts as paid
            return None, None

    async def _settle(self, decision_id: str, state: str, *, verdict: str = "", reason: str = "", detail: dict[str, Any] | None = None,
                      ask_id: str | None = None) -> None:
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state, _now()]
        if verdict:
            sets.append("verdict = ?")
            params.append(verdict)
        if reason:
            sets.append("reason = ?")
            params.append(reason[:500])
        if detail is not None:
            sets.append("detail_json = ?")
            params.append(json.dumps(detail, ensure_ascii=False))
        if ask_id is not None:
            sets.append("ask_id = ?")
            params.append(ask_id)
        await self.manager.db.execute(f"UPDATE autopilot_decisions SET {', '.join(sets)} WHERE id = ?", (*params, decision_id))  # noqa: S608 — fixed column names

    async def decision(self, decision_id: str) -> dict[str, Any] | None:
        row = await self.manager.db.fetchone("SELECT * FROM autopilot_decisions WHERE id = ?", (decision_id,))
        if row is None:
            return None
        found = dict(row)
        try:
            found["detail"] = json.loads(found.pop("detail_json") or "{}")
        except ValueError:
            found["detail"] = {}
        return found

    def _lang(self) -> str:
        notifications = self.app.notifications
        return notifications.language() if notifications is not None else ""

    # -- delivering a verdict -------------------------------------------------------------------

    async def _notify_done(self, state: SessionState, decision_id: str, verdict: judge.Verdict) -> None:
        notifications = self.app.notifications
        session_id = state.session.id
        await self._settle(decision_id, "notified", verdict="done", detail=verdict.detail())
        if notifications is None:
            return
        # Under the finished run's own key: its "Finished" line, if there is one, becomes this one
        # rather than a second entry about the same moment.
        await notifications.post(Draft(
            "run_finished", render("autopilot.done", self._lang(), title=state.session.title), verdict.summary, kind="autopilot", tone="ok",
            session_id=session_id, project_id=state.project.id if state.project is not None else None,
            link=f"/app/agents/{session_id}", dedupe_key=f"run:{session_id}", source=REF,
        ))

    async def _ask(self, state: SessionState, decision_id: str, verdict: judge.Verdict) -> None:
        """A question in the session's list; for a choice, with its labels as options, a notification
        with a button each, and the same buttons in the session's Telegram topic."""
        manager = self.manager
        session_id = state.session.id
        labels = [option.label for option in verdict.options]
        text = verdict.question if verdict.kind == "ask" else (verdict.reason or render("autopilot.choice", self._lang(), title=state.session.title))
        ask = await manager.asks.open(
            None, origin="session", kind="question", text=text, routed_to="operator",
            detail={"options": labels, "autopilot": decision_id}, session_id=session_id,
        )
        await self._settle(decision_id, "asking", verdict=verdict.kind, detail=verdict.detail(), ask_id=ask.id)
        with suppress(Exception):
            await manager.bus.publish("ask.side", {"change": "opened", "ask_ids": [ask.id]}, session_id=session_id)
        lang = self._lang()
        title = render("autopilot.choice" if labels else "autopilot.ask", lang, title=state.session.title)
        telegram = await self._telegram_buttons(state, decision_id, f"{title}\n\n{text}", labels)
        notifications = self.app.notifications
        if notifications is None:
            return
        actions = tuple(Action(f"opt:{index}", label, "primary" if index == 0 else "default", quick=True) for index, label in enumerate(labels))
        await notifications.post(Draft(
            "question", title, text, kind="autopilot", session_id=session_id,
            project_id=state.project.id if state.project is not None else None, link=f"/app/agents/{session_id}",
            actions=(*actions, Action("open", render("open", lang), "ghost")),
            request_ref=f"{REF}:{session_id}:{decision_id}" if labels else None, dedupe_key=f"{REF}:{session_id}:{decision_id}",
            source=REF, handled=frozenset({"telegram"}) if telegram else frozenset(),
        ))

    async def _telegram_buttons(self, state: SessionState, decision_id: str, text: str, labels: list[str]) -> bool:
        """The question in the session's topic, with a button per option; whether it was sent there."""
        front = getattr(self.app, "front", None)
        if front is None or state.metadata.get("telegram_detached"):
            return False
        try:
            outbox = await front.outbox_for_session(state.session.id)
            if outbox is None:
                return False
            rows = [[(label, f"{CALLBACK}:{decision_id}:{index}")] for index, label in enumerate(labels)]
            message_id = await front.post(outbox, text, rows or None)
        except Exception:  # noqa: BLE001 — the question waits in the app either way
            logger.warning("autopilot could not put its question for %s in Telegram", state.session.id, exc_info=True)
            return False
        found = await self.decision(decision_id)
        if found is not None:
            detail = {**found["detail"], "telegram": {"chat_id": outbox.chat_id, "message_id": message_id}}
            await self.manager.db.execute("UPDATE autopilot_decisions SET detail_json = ? WHERE id = ?", (json.dumps(detail, ensure_ascii=False), decision_id))
        return True

    # -- admitting a turn, exactly once ---------------------------------------------------------

    async def _admit(self, decision_id: str, session_id: str, text: str, *, kind: str, via: str, label: str = "", question: str = "",
                     origin: str = "autopilot") -> str:
        """Send one turn for a decision; ``""`` when it went in (now or by an earlier call), or why not.

        The key is claimed in the decision row first, by the one caller whose update finds it empty;
        every later caller sees it taken and sends nothing. The turn then goes in under the same key
        as its input receipt, which is what makes a retry of this very call — a lost acknowledgement,
        a restart between the claim and the run — find the turn already placed."""
        key = f"{REF}:{decision_id}"
        async with self._admitting.setdefault(decision_id, asyncio.Lock()):
            async with self.manager.db.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE autopilot_decisions SET admission_key = ?, state = 'admitting', updated_at = ? WHERE id = ? AND admission_key IS NULL",
                    (key, _now(), decision_id),
                )
                claimed = cursor.rowcount > 0
                await cursor.close()
            if not claimed:
                return "taken"
            mark: dict[str, Any] = {"kind": kind, "decision": decision_id}
            if label:
                mark["label"] = label
            if question:
                mark["question"] = question
            try:
                await self.manager.submit(
                    session_id, text, as_answer=False, follow_up=True, origin=origin, client_message_id=key, via=via, autopilot=mark,
                    operator_words=[text] if origin == "operator" else None,
                )
            except Exception as exc:  # noqa: BLE001 — the turn did not go in; the decision says why
                logger.warning("autopilot could not send its turn into %s", session_id, exc_info=True)
                await self._settle(decision_id, "closed", reason=f"not sent: {exc}")
                return "failed"
            await self._settle(decision_id, "admitted")
        self._admitting.pop(decision_id, None)
        return ""

    async def choose(self, decision_id: str, index: int, *, via: str) -> str:
        """A tap on an option of a choice, from Telegram or a notification: close the question as the
        operator's answer (the first answer wins, from wherever it came) and send that option's goal."""
        found = await self.decision(decision_id)
        if found is None or found["verdict"] != "choice" or not found["ask_id"]:
            raise Refused("this choice is gone")
        options = found["detail"].get("options") or []
        if not 0 <= index < len(options):
            raise Refused("that option is not one of this choice's")
        label = str(options[index].get("label") or "")
        if not await self.manager.asks.resolve(str(found["ask_id"]), "operator", {"selected": [label], "via": via}):
            raise ActionConflict(render("autopilot.taken", self._lang()))
        await self._announce(found, "answered")
        outcome = await self._admit(decision_id, str(found["session_id"]), str(options[index].get("next_goal") or ""), kind="choice", via=via, label=label)
        await self._close_elsewhere(found, render("autopilot.chosen", self._lang(), label=label), via=via)
        if outcome == "failed":
            raise Refused("the turn could not be sent; see the session")
        return label

    async def answered(self, ask: Ask, *, via: str) -> str:
        """An autopilot question answered in the side panel (the row already closed by that answer):
        a chosen option sends its goal; words of the operator's own go in as their message, with the
        question quoted so the agent knows what they answer. Returns the error text, ``""`` when sent."""
        decision_id = str(ask.detail.get("autopilot") or "")
        found = await self.decision(decision_id)
        if found is None or str(found["session_id"]) != str(ask.session_id):
            return "this autopilot question is gone"
        resolution = ask.resolution
        selected = [str(s) for s in resolution.get("selected") or []]
        options = found["detail"].get("options") or []
        chosen = next((o for o in options if selected and o.get("label") == selected[0]), None)
        session_id = str(found["session_id"])
        if chosen is not None:
            note = str(resolution.get("note") or "").strip()
            goal = str(chosen.get("next_goal") or "") + (f"\n\n{note}" if note else "")
            outcome = await self._admit(decision_id, session_id, goal, kind="choice", via=via, label=str(chosen.get("label") or ""))
            words = render("autopilot.chosen", self._lang(), label=str(chosen.get("label") or ""))
        else:
            words = str(resolution.get("text") or "").strip()
            question = ask.text.split("\n\n", 1)[0].strip()
            outcome = await self._admit(decision_id, session_id, words, kind="answer", via=via, question=question, origin="operator")
        await self._close_elsewhere(found, words, via=via)
        return "" if outcome in ("", "taken") else "the answer could not be sent; see the session"

    async def _close_elsewhere(self, found: dict[str, Any], words: str, *, via: str) -> None:
        """An answered decision stops asking everywhere it asked: the notification's buttons and the
        Telegram message's."""
        notifications = self.app.notifications
        if notifications is not None:
            with suppress(Exception):
                await notifications.resolve(f"{REF}:{found['session_id']}:{found['id']}", words, via=via)
        sent = found["detail"].get("telegram") or {}
        front = getattr(self.app, "front", None)
        if front is not None and sent.get("message_id"):
            with suppress(Exception):
                await front.edit_post(int(sent["chat_id"]), int(sent["message_id"]), f"{found['detail'].get('reason') or found['detail'].get('question') or ''}\n\n→ {words}".strip())

    async def _announce(self, found: dict[str, Any], change: str) -> None:
        with suppress(Exception):
            await self.manager.bus.publish("ask.side", {"change": change, "ask_ids": [found["ask_id"]]}, session_id=str(found["session_id"]))

    async def _withdraw_open(self, session_id: str, why: str) -> None:
        rows = await self.manager.db.fetchall(
            "SELECT id FROM autopilot_decisions WHERE session_id = ? AND state = 'asking'", (session_id,),
        )
        for row in rows:
            found = await self.decision(str(row["id"]))
            if found is None or not found["ask_id"]:
                continue
            if await self.manager.asks.resolve(str(found["ask_id"]), "system", {"closed": why, "via": "withdrawn", "by": "autopilot"}):
                await self._announce(found, "withdrawn")
                await self._close_elsewhere(found, why, via="autopilot")
            await self._settle(str(found["id"]), "closed", reason=why)

    # -- the fronts' buttons --------------------------------------------------------------------

    async def resolve_action(self, request: ActionRequest) -> ActionOutcome:
        """A notification's option button: ``opt:<index>`` on ``autopilot:<session>:<decision>``."""
        if not request.action.startswith("opt:") or not request.action[4:].isdigit():
            raise ActionRefused(f"this notification offers no {request.action!r}")
        found = await self.decision(request.target)
        if found is None or str(found["session_id"]) != request.scope:
            raise ActionRefused("this choice is gone")
        try:
            label = await self.choose(request.target, int(request.action[4:]), via=request.via)
        except Refused as exc:
            raise ActionRefused(str(exc)) from None
        return ActionOutcome(render("autopilot.chosen", self._lang(), label=label))

    async def on_telegram(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3 or not data[2].isdigit():
            await query.answer("stale button")
            return
        try:
            label = await self.choose(data[1], int(data[2]), via="telegram")
        except ActionConflict as exc:
            await query.answer(str(exc.resolution)[:200])
            return
        except Refused as exc:
            await query.answer(str(exc)[:200])
            return
        await query.answer(render("autopilot.chosen", self._lang(), label=label)[:200])

    # -- a restart ------------------------------------------------------------------------------

    async def recover(self) -> None:
        """Close what a restart caught in the middle, never replaying it: a judge whose verdict nobody
        saw, and an admission that did not get as far as a run (its receipt says how far it got)."""
        db = self.manager.db
        await db.execute(
            "UPDATE autopilot_decisions SET state = 'abandoned', reason = 'the host restarted while judging', updated_at = ? WHERE state = 'judging'",
            (_now(),),
        )
        for row in await db.fetchall("SELECT id, session_id, admission_key FROM autopilot_decisions WHERE state = 'admitting'"):
            receipt = await self.manager.live.receipt(str(row["session_id"]), str(row["admission_key"] or "")) if row["admission_key"] else None
            if receipt is not None and receipt["status"] == "consumed":
                await self._settle(str(row["id"]), "admitted")
            else:
                await self._settle(str(row["id"]), "abandoned", reason="the host restarted before the turn was placed; not sent again")

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with suppress(BaseException):
                await task


async def install(app: Application) -> list[asyncio.Task[None]]:
    if app.manager is None:
        return []
    service = Autopilot(app)
    app.extensions["autopilot"] = service
    await service.recover()
    app.manager.on_finished(service.on_run_finished)
    app.manager.run_started_hooks.append(service.on_run_started)
    if app.notifications is not None:
        app.notifications.register_resolver(REF, service.resolve_action)
    front = getattr(app, "front", None)
    if front is not None:
        front.callback_hooks[CALLBACK] = service.on_telegram
    return []


__all__ = ["CALLBACK", "JUDGED_ORIGINS", "MARK_KEY", "REF", "SWITCH_KEY", "Autopilot", "Refused", "install"]
