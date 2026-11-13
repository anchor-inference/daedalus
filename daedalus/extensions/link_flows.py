"""What agents do together over a link, on top of its messages: both directions, one place.

Our agent as a guest at somebody else's door (:mod:`daedalus.extensions.agent_talk`) and a guest's agent at ours
(:class:`daedalus.extensions.guest_door.GuestBridge`) each keep their own protocol work. What sits on top of both is
here, and :mod:`daedalus.host.link_flows` holds its rules:

- **One question to several agents.** Sent as a message in each conversation, so each keeps its limits and its
  approval. The replies to it are taken out of the conversations' own waking: the chat wakes once, for all of them,
  when every one is in or the deadline has passed, with a turn that may only compare.
- **Cards** beside a message's words: a summary both sides keep, free times, a meeting, a service shown.
- **A summary of a conversation**, on the operator's button or at its end: the promises a cheap call on the chat's
  model finds, the tasks either side set, the addresses and files. Sent as a card, which the other side keeps
  without a turn; the operator can put it in the chat's notes or on the board.
- **A guest's "tell me when"**: a time or a check every so often, carried out by a counted turn of the link through
  the door's ``resume``, so the door's limits hold, and ended with the link.
- **Actions that wait for the operator or outlive a turn**: a patch applied to a branch, a running service shown
  through a one-time address, a meeting booked. A trusted link's turn has them at once; any other waits for the
  operator's card (:data:`APPROVE_HOOK`), and a turn with no tools gets no patch, since it cannot read one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus import public_address
from daedalus.host import agent_talk, guest_turns, link_flows, link_protocol
from daedalus.host.engine_factory import TENANT
from daedalus.host.guest_turns import GuestTurn, GuestTurnBusy
from daedalus.host.link_flows import ANSWERED, ASK, FAILED, HELD, NO, NOW, WAITING, PatchError

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "link_flows"
APPROVE_HOOK = "link_flows.approve"
TALK, DOOR = "talk", "door"
"""``side`` of a row: a conversation of ours at another door, or a link of ours."""
SWEEP_SECONDS = 30.0
CHAT_FREE_SECONDS = 900.0
SUMMARY_SECONDS = 60.0
POLL_PREFIX = agent_talk.COMPARE_PREFIX
"""The ``link_id`` of the turn that compares the answers to one question: no conversation has such an id."""
SUB_PREFIX = "sub_"
"""What the door's ``resume`` carries for a subscription's turn, where an answer of the operator's carries its ask id."""
STAGING = "link-actions"
SERVICE_HOURS = 12
WORDS_CHARS = 8000
"""The words that go with a card, cut to what a door takes of a message (``guestd.mailbox.MAX_TEXT``)."""
FLOW_ACTIONS = frozenset({"ask", "summary", "slots", "meet", "apply_patch"})
"""What ``AgentTalk`` does here in a turn of the operator's."""
GUEST_ACTIONS = frozenset({"summary", "slots", "meet", "apply_patch", "share_service", "subscribe", "unsubscribe"})
"""What it does here in a guest's turn."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return _now().isoformat()


class LinkFlows:
    def __init__(self, app: Application | Any) -> None:
        self.app = app
        self._background: set[asyncio.Task[Any]] = set()
        self._waking: set[str] = set()
        self._cards_seen: OrderedDict[int, None] = OrderedDict()

    @property
    def db(self) -> Any:
        return self.app.db

    @property
    def manager(self) -> Any:
        return self.app.manager

    def talks(self) -> Any:
        from daedalus.extensions.agent_talk import talks_of  # Lazy: agent_talk imports this module's hooks.

        return talks_of(self.app)

    def bridge(self) -> Any:
        from daedalus.extensions.guest_door import bridge_of  # Lazy: guest_door imports this module.

        return bridge_of(self.app)

    def _spawn(self, work: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(work, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # -- cards ------------------------------------------------------------------------------------------

    async def keep_card(self, side: str, owner: str, session_id: str, direction: str, card: dict[str, Any], *, conversation: str = "", text: str = "") -> int:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO link_cards(side, owner, conversation, session_id, direction, type, card, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (side, owner, conversation, session_id, direction, card["type"], json.dumps(card, ensure_ascii=False), text[:4000], _stamp()),
            )
            card_id = int(cursor.lastrowid or 0)
            await cursor.close()
        return card_id

    async def cards_of(self, side: str, owner: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM link_cards WHERE side = ? AND owner = ? ORDER BY id", (side, owner))
        out = []
        for row in rows:
            card = link_flows.card_view(json.loads(row["card"]))
            if card is not None:
                out.append({"id": row["id"], "direction": row["direction"], "type": row["type"], "card": link_flows.as_seen(card, str(row["direction"])), "at": row["created_at"]})
        return out

    async def card_from_guest(self, link_id: str, detail: str, event_id: Any = None) -> None:
        """A card a guest handed the door on its own, as an event: kept and shown, and no turn is spent on it. An event
        read twice, as the feed hands out again what a crash kept it from marking, is kept once."""
        if isinstance(event_id, int):
            if event_id in self._cards_seen:
                return
            self._cards_seen[event_id] = None
            while len(self._cards_seen) > 1024:
                self._cards_seen.popitem(last=False)
        row = await self.bridge().known_link(link_id) if self.bridge() is not None else None
        if row is None or row["revoked_at"]:
            return
        try:
            body = json.loads(detail)
        except (TypeError, ValueError):
            return
        card = link_flows.card_view(body.get("card") if isinstance(body, dict) else None)
        if card is None:
            return
        conversation = str(body.get("conversation") or "")
        await self.keep_card(DOOR, link_id, str(row["session_id"]), "theirs", card, conversation=conversation if link_protocol.CONVERSATION.fullmatch(conversation) else "")

    # -- the tool's actions ------------------------------------------------------------------------------

    async def talk_action(self, state: Any, action: str, talk: Any, *, text: str, files: list[str], extra: dict[str, Any]) -> tuple[str, bool]:
        """``AgentTalk`` in a turn of the operator's: ``ask`` is handled before a conversation is chosen."""
        if action == "summary":
            card = await self.summarise_talk(talk["id"])
            return f"Sent the summary to {talk['host']}, and it is kept with the conversation:\n{link_flows.card_words(link_flows.as_seen(card, 'mine'))}", False
        if action == "slots":
            return await self._talk_slots(state, talk, text, extra)
        if action == "meet":
            return await self._talk_meet(state, talk, text, extra)
        if action == "apply_patch":
            return await self._request_patch(state, TALK, talk["id"], "", str(talk["host"]), files, extra, level=await self._talk_level(state, talk))
        return "Not an action of a conversation.", True

    async def guest_action(self, state: Any, delivery: Any, action: str, *, text: str, files: list[str], extra: dict[str, Any]) -> tuple[str, bool]:
        """``AgentTalk`` in a guest's turn at our door, for the conversation that turn answers."""
        turn = state.guest_turn
        level = "trusted" if turn.trusted else turn.tools
        if getattr(state, "bypass", False) and level != "none":
            # The operator lifted this session's checks: what waits for their yes at the link's level goes at once.
            # What the level does not allow at all (titles in the calendar, a patch with no folder) stays out.
            level = "trusted" if link_flows.standing(level, action) == link_flows.ASK else level
        if action not in GUEST_ACTIONS:
            return "Not an action of a guest's turn.", True
        if action == "unsubscribe":
            return await self._unsubscribe(delivery.link_id, str(extra.get("subscription") or ""))
        if link_flows.standing(level, action) == NO:
            return {"apply_patch": "This turn cannot read the chat's folder, so it applies no patch."}.get(action, "This link's level does not allow that."), True
        if action == "summary":
            card = await self.summarise_link(delivery.link_id)
            return f"Sent the guest the summary, and it is kept with the link:\n{link_flows.card_words(link_flows.as_seen(card, 'mine'))}", False
        if action == "subscribe":
            return await self._subscribe(state, delivery, text, extra)
        if action == "slots":
            return await self._guest_slots(state, delivery, level, text, extra)
        if action == "meet":
            return await self._guest_meet(state, delivery, level, text, extra)
        if action == "share_service":
            return await self._share_service(state, delivery, level, extra)
        return await self._request_patch(state, DOOR, delivery.link_id, delivery.conversation_id, await self._label(delivery.link_id), files, extra, level=level, task=delivery.task)

    async def _label(self, link_id: str) -> str:
        bridge = self.bridge()
        row = await bridge.known_link(link_id) if bridge is not None else None
        return str(row["label"]) if row is not None and row["label"] else link_id

    # -- one question to several agents --------------------------------------------------------------------

    async def ask_many(self, state: Any, conversations: list[str], text: str, deadline_minutes: Any) -> tuple[str, bool]:
        talks = self.talks()
        session_id = state.session.id
        chosen = list(dict.fromkeys(str(item) for item in conversations if isinstance(item, str)))
        if not 2 <= len(chosen) <= link_flows.MAX_ASKED:
            return f"conversations names 2 to {link_flows.MAX_ASKED} of this chat's open conversations, or links the operator gave in this chat.", True
        if not text.strip():
            return "text is the question each of them is asked.", True
        rows = []
        for talk_id in chosen:
            if agent_talk.find_links(talk_id):
                # A link the operator gave is opened here, by the same rules as ``open``: only one they wrote in this chat.
                opened = await talks._open(state, talk_id)
                if opened.error:
                    return opened.text, True
                talk_id = opened.talk
            talk = await talks.talk(talk_id)
            if talk is None or talk["session_id"] != session_id:
                return f"This chat has no conversation {talk_id}.", True
            if talk["state"] in ("closed", "opening"):
                return f"The conversation {talk_id} with {talk['host']} is not open.", True
            rows.append(talk)
        if await self.db.fetchone(
            f"SELECT 1 FROM link_poll_entries e JOIN link_polls p ON p.id = e.poll_id WHERE p.state = ? AND e.talk_id IN ({','.join('?' * len(chosen))})",
            (link_flows.POLL_OPEN, *chosen),
        ):
            return "One of these conversations is already answering an earlier question to several. Wait for it to be compared.", True
        poll_id = secrets.token_hex(6)
        deadline = link_flows.deadline_of(deadline_minutes)
        await self.db.execute(
            "INSERT INTO link_polls(id, session_id, question, state, deadline_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (poll_id, session_id, text.strip()[:4000], link_flows.POLL_OPEN, deadline.isoformat(), _stamp()),
        )
        lines: list[str] = []
        read: list[str] = []
        for talk in rows:
            await self.db.execute("INSERT INTO link_poll_entries(poll_id, talk_id, state) VALUES (?, ?, ?)", (poll_id, talk["id"], link_flows.ASKING))
            # Each through the conversation's own send: its limits, its review and its approval hold as for any message.
            sent = await talks._send(state, talk, text, 0)
            mine = await self.db.fetchone("SELECT id, state FROM agent_talk_messages WHERE talk_id = ? AND role = 'us' ORDER BY id DESC LIMIT 1", (talk["id"],))
            if mine is not None and str(mine["state"]) in ("held", "sending", "sent", "unsure"):
                await self.db.execute("UPDATE link_poll_entries SET message_id = ?, state = ? WHERE poll_id = ? AND talk_id = ?", (mine["id"], HELD if mine["state"] == "held" else WAITING, poll_id, talk["id"]))
                lines.append(f"- {talk['host']} ({talk['id']}): " + ("waits for the operator's approval" if mine["state"] == "held" else "asked"))
            else:
                await self.db.execute("UPDATE link_poll_entries SET state = ? WHERE poll_id = ? AND talk_id = ?", (FAILED, poll_id, talk["id"]))
                lines.append(f"- {talk['host']} ({talk['id']}): not asked. {sent.text.splitlines()[0] if sent.text else ''}")
            if "reply from another agent" in sent.text:
                # Older replies the send handed this run on its way: they are read now, so they are shown.
                read.append(sent.text)
        await self.check_poll(poll_id)
        head = f"Asked {len(rows)} agents (question {poll_id}). Their answers are gathered into one table in the chat, and you are woken once, when all have answered or at {deadline.isoformat(timespec='minutes')}. End your turn now."
        return "\n".join([head, *lines, *read]), False

    async def _poll_entries(self, poll_id: str) -> list[dict[str, Any]]:
        """The entries of a question with each one's state as it stands: a message the operator refused, or a
        conversation that closed, is an answer that will not come."""
        rows = await self.db.fetchall(
            "SELECT e.*, t.host AS host, t.state AS talk_state, m.state AS message_state FROM link_poll_entries e "
            "JOIN agent_talks t ON t.id = e.talk_id LEFT JOIN agent_talk_messages m ON m.id = e.message_id WHERE e.poll_id = ? ORDER BY e.id",
            (poll_id,),
        )
        out = []
        for row in rows:
            state = str(row["state"])
            if state not in link_flows.SETTLED:
                message = str(row["message_state"] or "")
                if message in ("refused", "failed") or row["talk_state"] == "closed":
                    state = FAILED
                elif message == "held":
                    state = HELD
                elif message in ("sent", "sending", "unsure"):
                    state = WAITING
            out.append({"talk": row["talk_id"], "host": row["host"], "state": state, "answer": row["answer"], "answered_at": row["answered_at"], "reply_id": row["reply_id"]})
        return out

    async def poll_reply(self, talk_id: str) -> bool:
        """Replies came on ``talk_id``: the answer to an open question to several is taken from them. ``True`` while
        the conversation is part of an open question, so that its own waking stays out of it: the question wakes the
        chat once, for all."""
        rows = await self.db.fetchall(
            "SELECT e.*, p.id AS poll FROM link_poll_entries e JOIN link_polls p ON p.id = e.poll_id WHERE p.state = ? AND e.talk_id = ?", (link_flows.POLL_OPEN, talk_id),
        )
        if not rows:
            return False
        for row in rows:
            if row["state"] in link_flows.SETTLED or row["message_id"] is None:
                continue
            reply = await self.db.fetchone("SELECT id, text FROM agent_talk_messages WHERE talk_id = ? AND role = 'them' AND id > ? ORDER BY id LIMIT 1", (talk_id, row["message_id"]))
            if reply is not None:
                await self.db.execute(
                    "UPDATE link_poll_entries SET state = ?, answer = ?, reply_id = ?, answered_at = ? WHERE id = ?",
                    (ANSWERED, str(reply["text"])[: link_flows.ANSWER_CHARS], reply["id"], _stamp(), row["id"]),
                )
        for poll in {str(row["poll"]) for row in rows}:
            await self.check_poll(poll)
        return True

    async def check_poll(self, poll_id: str) -> None:
        """Settle a question that is over, and wake the chat for it once."""
        poll = await self.db.fetchone("SELECT * FROM link_polls WHERE id = ?", (poll_id,))
        if poll is None:
            return
        if poll["state"] == link_flows.POLL_OPEN:
            entries = await self._poll_entries(poll_id)
            if not link_flows.poll_settled([entry["state"] for entry in entries], str(poll["deadline_at"])):
                return
            async with self.db.transaction() as conn:
                cursor = await conn.execute("UPDATE link_polls SET state = ?, settled_at = ? WHERE id = ? AND state = ?", (link_flows.POLL_DONE, _stamp(), poll_id, link_flows.POLL_OPEN))
                won = cursor.rowcount > 0
                await cursor.close()
            if not won:
                return
            for entry in entries:
                if entry["state"] == FAILED:
                    await self.db.execute("UPDATE link_poll_entries SET state = ? WHERE poll_id = ? AND talk_id = ?", (FAILED, poll_id, entry["talk"]))
            poll = await self.db.fetchone("SELECT * FROM link_polls WHERE id = ?", (poll_id,))
        if poll is not None and not poll["woke"] and poll_id not in self._waking:
            self._waking.add(poll_id)
            self._spawn(self._wake_poll(poll_id), f"link-poll-wake:{poll_id}")

    async def _wake_poll(self, poll_id: str) -> None:
        try:
            await self._open_poll_turn(poll_id)
        except Exception:  # noqa: BLE001 — the table is in the app either way; a turn that could not open is said in the log
            logger.exception("the answers to question %s could not open a turn", poll_id)
        finally:
            self._waking.discard(poll_id)

    async def _open_poll_turn(self, poll_id: str) -> None:
        """One turn for all the answers, opened once the chat is free. It is a turn opened by strangers' words with
        nobody asked: it reads only those conversations, it is bounded as a reply's turn is, and it sends nothing."""
        poll = await self.db.fetchone("SELECT * FROM link_polls WHERE id = ?", (poll_id,))
        if poll is None or poll["woke"]:
            return
        session_id = str(poll["session_id"])
        manager = self.manager
        talks = self.talks()
        entries = await self._poll_entries(poll_id)
        answered = [entry for entry in entries if entry["state"] == ANSWERED]
        await self.db.execute("UPDATE link_polls SET woke = 1 WHERE id = ?", (poll_id,))
        if not answered:
            return
        why = await manager.guest_ready(session_id, "none")
        price = await manager.guest_price(session_id)
        if why or price is None:
            logger.info("the answers to question %s could not wake session %s: %s", poll_id, session_id, why or "the chat's model has no known price")
            return
        late = len(answered) < len(entries)
        body = link_flows.poll_body(str(poll["question"]), [(entry["host"], entry["state"], entry["answer"]) for entry in entries], agent_talk.fence_reply, late=late)
        runs: list[str] = []
        for entry in entries:
            talk = await talks.talk(entry["talk"])
            if talk is not None:
                runs.extend(await talks.runs_of(talk))
        runs = list(dict.fromkeys(runs))[-24:]
        usd_left = guest_turns.TURN_USD if any(price) else None
        if usd_left is not None and await manager.guest_first_call_usd(session_id, price, runs=runs, extra_chars=len(body)) > usd_left:
            logger.info("the answers to question %s would pass a turn's bound on the first call; left for the operator", poll_id)
            return
        hosts = ", ".join(entry["host"] for entry in entries)
        turn = GuestTurn(POLL_PREFIX + poll_id, f"several agents ({hosts})", "none", usd_left, str(poll["question"]), "reply", tuple(runs))
        deadline = time.monotonic() + CHAT_FREE_SECONDS
        while time.monotonic() < deadline:
            try:
                run_id = await manager.submit(session_id, body, as_answer=False, origin=turn.origin, via="agent_talk", guest=turn)
            except GuestTurnBusy:
                await asyncio.sleep(1.0)
                continue
            except (RuntimeError, KeyError) as exc:
                logger.warning("the answers to question %s could not open a turn in session %s: %s", poll_id, session_id, exc)
                return
            manager.touch_run(session_id)
            for entry in answered:
                await self.db.execute("UPDATE agent_talk_messages SET state = 'read', run_id = ? WHERE id = ? AND state = 'unread'", (run_id, entry["reply_id"]))
            await self.db.execute("UPDATE link_polls SET run_id = ? WHERE id = ?", (run_id, poll_id))
            return

    async def polls_of(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM link_polls WHERE session_id = ? ORDER BY created_at DESC LIMIT 20", (session_id,))
        return [
            {"id": row["id"], "question": row["question"], "state": row["state"], "deadline_at": row["deadline_at"], "created_at": row["created_at"], "settled_at": row["settled_at"],
             "woke": bool(row["woke"]), "dismissed": bool(row["dismissed"]), "entries": [{key: entry[key] for key in ("talk", "host", "state", "answer", "answered_at")} for entry in await self._poll_entries(row["id"])]}
            for row in rows
        ]

    async def dismiss_poll(self, poll_id: str) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("UPDATE link_polls SET dismissed = 1 WHERE id = ?", (poll_id,))
            done = cursor.rowcount > 0
            await cursor.close()
        return done

    # -- summaries ---------------------------------------------------------------------------------------

    async def _promises(self, session_id: str, lines: list[tuple[str, str]]) -> tuple[str, list[dict[str, str]]]:
        """What a cheap call on the chat's own model finds promised. Nothing when there is no model or it fails: the
        tasks, links and files still make a summary."""
        if not any(text.strip() for _, text in lines):
            return "", []
        manager = self.manager
        try:
            rungs, _ = manager.resolve_model(await manager.live.load(session_id))
            if not rungs:
                return "", []
            provider, model = rungs[0]
            request = LLMRequest(
                model=model, messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=link_flows.summary_prompt(lines))])], max_tokens=900, temperature=0.1,
                extra={"enable_thinking": False}, observability=LLMObservabilityContext(tenant_id=TENANT, session_id=session_id, call_purpose="link_summary", call_category="session"),
            )
            response = await asyncio.wait_for(provider.complete_text(request), timeout=SUMMARY_SECONDS)
        except Exception as exc:  # noqa: BLE001 — a summary without the model's part is still a summary
            logger.info("the summary of a link conversation in session %s has no promises: %s", session_id, exc)
            return "", []
        return link_flows.promises_from("".join(block.text for block in response.message.content_blocks if isinstance(block, TextBlock)))

    async def summarise_talk(self, talk_id: str, *, send: bool = True) -> dict[str, Any]:
        """The summary of a conversation of ours at another door: kept here, and sent there as a card that costs the
        other side no turn."""
        talks = self.talks()
        talk = await talks.talk(talk_id)
        if talk is None:
            raise KeyError(talk_id)
        messages = await talks.messages(talk_id)
        lines = [("us" if row["role"] == "us" else "them", str(row["text"])) for row in messages if row["role"] in ("us", "them") and row["state"] not in ("held", "refused")]
        files = [str(row["name"]) for row in await self.db.fetchall("SELECT name FROM link_files WHERE side = ? AND owner = ? AND state NOT IN ('refused', 'dropped', 'failed', 'held') ORDER BY id", (TALK, talk_id))]
        tasks = [dict(row) for row in await self.db.fetchall("SELECT direction, text, state FROM link_tasks WHERE side = ? AND owner = ? ORDER BY id", (TALK, talk_id))]
        title, promises = await self._promises(str(talk["session_id"]), lines)
        card = link_flows.summary_card(title=title, promises=promises, texts=[text for _, text in lines], files=files, tasks=tasks)
        words = link_flows.card_words(card)[:WORDS_CHARS]
        await self.keep_card(TALK, talk_id, str(talk["session_id"]), "mine", card, text=words)
        # A stand-in takes text alone, so it is not sent a card it would refuse.
        if send and talk["state"] != "closed" and talk["token"] and talk["mode"] != "stand_in":
            try:
                answer = await talks.dialer.call("POST", f"{talk['base_url']}/a/messages", token=talk["token"], body={"kind": "status", "text": words, "card": card, **talks.signature(talk, words, kind="status", card=card)})
                sent = answer.status in (200, 202)
            except Exception as exc:  # noqa: BLE001 — the summary is kept here whatever the door does with it
                logger.info("the summary of conversation %s did not reach its door: %s", talk_id, exc)
                sent = False
            await talks._insert_message(talk_id, "us", words, "sent" if sent else "failed", "", "summary", kind=link_protocol.STATUS, card=card)
        return card

    async def summarise_link(self, link_id: str, *, send: bool = True, conversation: str = "") -> dict[str, Any]:
        """The summary of a link of ours: from the door's transcript of its conversation and this side's tasks and files.
        ``conversation`` picks one guest's of a link several share; without it the door's newest."""
        bridge = self.bridge()
        row = await bridge.known_link(link_id) if bridge is not None else None
        if row is None:
            raise KeyError(link_id)
        picked = f"?conversation={conversation}" if re.fullmatch(r"[0-9a-f]{16}", conversation) else ""
        view = (await bridge.client.call("GET", f"/control/links/{link_id}{picked}")).get("link") or {}
        conversation = (view.get("conversation") or {}).get("id") or ""
        lines = [("us" if item.get("role") == "agent" else "them", str(item.get("text") or "")) for item in view.get("messages") or [] if item.get("role") in ("agent", "guest")]
        files = [str(file["name"]) for file in await self.db.fetchall("SELECT name FROM link_files WHERE side = ? AND owner = ? AND state NOT IN ('refused', 'dropped', 'failed', 'held') ORDER BY id", (DOOR, link_id))]
        tasks = [dict(task) for task in await self.db.fetchall("SELECT direction, text, state FROM link_tasks WHERE side = ? AND owner = ? ORDER BY id", (DOOR, link_id))]
        title, promises = await self._promises(str(row["session_id"]), lines)
        card = link_flows.summary_card(title=title, promises=promises, texts=[text for _, text in lines], files=files, tasks=tasks)
        words = link_flows.card_words(card)
        await self.keep_card(DOOR, link_id, str(row["session_id"]), "mine", card, conversation=str(conversation), text=words)
        if send and conversation and (view.get("conversation") or {}).get("state") == "open":
            await self._say(link_id, str(conversation), words, card)
        return card

    async def _say(self, link_id: str, conversation: str, text: str, card: dict[str, Any] | None = None) -> bool:
        """A word of this side's to a guest of our door, outside any turn. ``False`` when the door did not take it.
        Through the bridge, which signs it: a contact on the other side refuses a word that is not."""
        from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable  # Lazy: guest_door imports this module.

        body: dict[str, Any] = {"kind": "text", "text": text[:WORDS_CHARS]}
        if card is not None:
            body["card"] = card
        try:
            await self.bridge().say(link_id, conversation, body)
        except (DoorRefused, DoorUnavailable) as exc:
            logger.info("the door did not take a word for conversation %s: %s", conversation, exc)
            return False
        return True

    async def card_row(self, card_id: int) -> Any:
        return await self.db.fetchone("SELECT * FROM link_cards WHERE id = ?", (card_id,))

    async def _host_of(self, row: Any) -> str:
        if row["side"] == TALK:
            talk = await self.talks().talk(str(row["owner"]))
            return str(talk["host"]) if talk is not None else "another agent"
        return await self._label(str(row["owner"]))

    async def to_notes(self, card_id: int) -> dict[str, Any]:
        """A summary as a note of its chat, in the operator's terms of us and them."""
        from daedalus.stores.notes import scopes_for_session, store_for  # Lazy: the stores import the app's.

        row = await self.card_row(card_id)
        card = link_flows.card_view(json.loads(row["card"])) if row is not None else None
        if row is None or card is None or card["type"] != link_flows.SUMMARY:
            raise KeyError(card_id)
        seen = link_flows.as_seen(card, str(row["direction"]))
        host = await self._host_of(row)
        chat, _, _ = await scopes_for_session(self.db, str(row["session_id"]))
        note = await store_for(self.manager).create(chat, title=f"{host}: {seen['title'] or 'summary'}"[:120], body=link_flows.summary_markdown(seen, host), by="operator")
        return {"note_id": note["id"]}

    async def to_board(self, card_id: int, authenticated: dict[str, Any]) -> dict[str, Any]:
        """Each promise of a summary as a task on the chat's board: ours as they are, theirs as something to expect."""
        from daedalus.extensions.board_commands import BoardCommands  # Lazy: it imports the app's stores.
        from daedalus.stores.control import ControlStore, Entity, Principal, Scope  # Lazy: for this route alone.

        row = await self.card_row(card_id)
        card = link_flows.card_view(json.loads(row["card"])) if row is not None else None
        if row is None or card is None or card["type"] != link_flows.SUMMARY:
            raise KeyError(card_id)
        seen = link_flows.as_seen(card, str(row["direction"]))
        host = await self._host_of(row)
        session = await self.db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (row["session_id"],))
        scope = Scope("project", session["project_id"]) if session is not None and session["project_id"] else Scope("global", "global")
        commands = BoardCommands(self.db, bus=self.manager.bus)
        control = ControlStore(self.db)
        made = []
        for index, item in enumerate(seen["promises"]):
            title = item["what"] if item["by"] == link_flows.US else f"{host}: {item['what']}"
            revision = await control.revision(scope, Entity("collection", scope.id))
            created = await commands.create(
                Principal.operator(authenticated), scope, client_operation_id=f"link-card:{card_id}:{index}", expected_collection_revision=revision, title=title[:200],
                acceptance=f"Due {item['due']}" if item["due"] else "", notes=f"From the summary of the conversation with {host}.", source_session_id=str(row["session_id"]),
            )
            made.append(created["task_id"])
        return {"tasks": made}

    # -- a guest's "tell me when" -------------------------------------------------------------------------

    async def _subscribe(self, state: Any, delivery: Any, text: str, extra: dict[str, Any]) -> tuple[str, bool]:
        request = " ".join(text.split())[: link_flows.REQUEST_CHARS]
        if not request:
            return "text says what to tell the guest about, readable on its own when it fires.", True
        try:
            plan = link_flows.plan_of(str(extra.get("at") or ""), extra.get("every_minutes"), extra.get("times"))
        except ValueError as exc:
            return str(exc), True
        active = await self.db.fetchone("SELECT count(*) AS n FROM link_subscriptions WHERE link_id = ? AND state = 'active'", (delivery.link_id,))
        if int(active["n"]) >= link_flows.ACTIVE_MAX:
            return f"This link has {link_flows.ACTIVE_MAX} subscriptions already; end one first.", True
        sub_id = SUB_PREFIX + secrets.token_hex(6)
        await self.db.execute(
            "INSERT INTO link_subscriptions(id, link_id, conversation, session_id, request, next_at, every_minutes, times_left, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (sub_id, delivery.link_id, delivery.conversation_id, state.session.id, request, plan.first.isoformat(), plan.every_minutes, plan.times, _stamp()),
        )
        every = f", then every {plan.every_minutes} minutes, {plan.times} times in all" if plan.every_minutes else ""
        return (
            f"Subscribed ({sub_id}): the first check is at {plan.first.isoformat(timespec='minutes')}{every}. Each is a turn of this link, counted against its limits, "
            "and it ends with the link or when the guest asks. Tell the guest."
        ), False

    async def _unsubscribe(self, link_id: str, sub_id: str) -> tuple[str, bool]:
        if not await self.end_subscription(sub_id, "guest", link_id=link_id):
            return f"This link has no live subscription {sub_id}.", True
        return f"The subscription {sub_id} is ended.", False

    async def end_subscription(self, sub_id: str, reason: str, *, link_id: str | None = None) -> bool:
        query = "UPDATE link_subscriptions SET state = 'ended', reason = ?, ended_at = ? WHERE id = ? AND state = 'active'"
        params: tuple[Any, ...] = (reason, _stamp(), sub_id)
        if link_id is not None:
            query += " AND link_id = ?"
            params = (*params, link_id)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(query, params)
            done = cursor.rowcount > 0
            await cursor.close()
        return done

    async def subscriptions_of(self, link_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM link_subscriptions WHERE link_id = ? ORDER BY created_at DESC LIMIT 20", (link_id,))
        return [{key: row[key] for key in ("id", "request", "next_at", "every_minutes", "times_left", "fired", "state", "reason", "created_at")} for row in rows]

    async def fire_due(self) -> None:
        """Every subscription whose time has come: a counted turn of its link through the door, or its end."""
        from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable  # Lazy: guest_door imports this module.

        bridge = self.bridge()
        if bridge is None:
            return
        now = _now()
        for row in await self.db.fetchall("SELECT * FROM link_subscriptions WHERE state = 'active' AND next_at <= ? ORDER BY next_at", (now.isoformat(),)):
            link = await bridge.known_link(str(row["link_id"]))
            if link is None or link["revoked_at"]:
                await self.end_subscription(str(row["id"]), "closed")
                continue
            try:
                await bridge.client.call("POST", f"/control/conversations/{row['conversation']}/resume", json={"ask": row["id"]})
            except DoorRefused as exc:
                logger.info("subscription %s ends: the door took no more turns for it (%s)", row["id"], exc.detail)
                await self.end_subscription(str(row["id"]), "closed")
                continue
            except DoorUnavailable:
                return  # the next sweep tries again
            left = int(row["times_left"]) - 1
            if left > 0 and row["every_minutes"]:
                following = max(now, datetime.fromisoformat(str(row["next_at"]))) + timedelta(minutes=int(row["every_minutes"]))
                await self.db.execute("UPDATE link_subscriptions SET fired = fired + 1, times_left = ?, next_at = ? WHERE id = ?", (left, following.isoformat(), row["id"]))
            else:
                await self.db.execute("UPDATE link_subscriptions SET fired = fired + 1, times_left = 0, state = 'ended', reason = 'done', ended_at = ? WHERE id = ?", (_stamp(), row["id"]))

    async def subscription_input(self, session_id: str, delivery: Any, label: str) -> str:
        """What the turn of a subscription reads; ``ValueError`` for a resume that is not this link's."""
        row = await self.db.fetchone("SELECT * FROM link_subscriptions WHERE id = ?", (delivery.ask,))
        if row is None or row["session_id"] != session_id or row["link_id"] != delivery.link_id:
            raise ValueError("that subscription is not this link's")
        times = int(row["fired"]) + int(row["times_left"])
        return link_flows.subscription_body(str(row["request"]), int(row["fired"]), max(times, int(row["fired"])), _now(), lambda text: guest_turns.body_for(text, label))

    # -- actions that wait for the operator ---------------------------------------------------------------

    async def _action(self, side: str, owner: str, conversation: str, session_id: str, kind: str, args: dict[str, Any], state: str) -> str:
        action_id = secrets.token_hex(8)
        await self.db.execute(
            "INSERT INTO link_actions(id, side, owner, conversation, session_id, kind, args, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (action_id, side, owner, conversation, session_id, kind, json.dumps(args, ensure_ascii=False), state, _stamp()),
        )
        return action_id

    async def _ask(self, session_id: str, action_id: str, text: str) -> bool:
        asked = self.manager.ask_operator(session_id, key=f"link-flow:{action_id}", tool=agent_talk.TOOL, text=text, on_answer=APPROVE_HOOK, extra={"flow": action_id})
        if not asked:
            await self.db.execute("UPDATE link_actions SET state = 'refused', result = 'the chat was not open to ask', decided_at = ? WHERE id = ?", (_stamp(), action_id))
        return asked

    async def actions_of(self, side: str, owner: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM link_actions WHERE side = ? AND owner = ? ORDER BY created_at DESC LIMIT 30", (side, owner))
        out = []
        for row in rows:
            args = json.loads(row["args"])
            shown = {key: args[key] for key in ("name", "branch", "stat", "title", "start", "end", "service", "url") if key in args}
            out.append({"id": row["id"], "kind": row["kind"], "state": row["state"], "result": row["result"], "created_at": row["created_at"], "expires_at": row["expires_at"], **shown})
        return out

    async def approved(self, session_id: str, request: dict[str, Any], allowed: bool) -> None:
        """The operator's answer on an action's card."""
        action_id = str(request.get("flow") or "")
        async with self.db.transaction() as conn:
            cursor = await conn.execute("UPDATE link_actions SET state = ?, decided_at = ? WHERE id = ? AND session_id = ? AND state = 'held'", ("running" if allowed else "refused", _stamp(), action_id, session_id))
            claimed = cursor.rowcount > 0
            await cursor.close()
        if not claimed:
            return
        row = await self.db.fetchone("SELECT * FROM link_actions WHERE id = ?", (action_id,))
        if not allowed:
            await self._refused(row)
            return
        await self._carry_out(row)

    async def _carry_out(self, row: Any) -> str:
        args = json.loads(row["args"])
        try:
            if row["kind"] == "patch":
                result = await self._apply(row, args)
            elif row["kind"] == "service":
                result = await self._open_service(row, args)
            else:
                result = await self._book(row, args)
        except (PatchError, ValueError, OSError, KeyError) as exc:
            await self.db.execute("UPDATE link_actions SET state = 'failed', result = ? WHERE id = ?", (str(exc)[:500], row["id"]))
            await self._tell_other(row, f"That did not work here: {str(exc)[:300]}")
            return f"It failed: {exc}"
        return result

    async def _refused(self, row: Any) -> None:
        args = json.loads(row["args"])
        await self.db.execute("UPDATE link_actions SET result = 'the operator said no' WHERE id = ?", (row["id"],))
        if row["kind"] == "meeting":
            card = link_flows.card_view({"type": link_flows.MEETING, "title": args["title"], "start": args["start"], "end": args["end"], "timezone": args.get("timezone", ""), "state": "declined"})
            await self._tell_other(row, link_flows.card_words(card) if card else "The owner declined the meeting.", card)
        else:
            await self._tell_other(row, {"patch": "The owner did not let the patch be applied.", "service": "The owner did not open that service to you."}.get(str(row["kind"]), "The owner said no."))
        self._cleanup(args)

    async def _tell_other(self, row: Any, text: str, card: dict[str, Any] | None = None) -> None:
        """A word to the other side of an action's conversation, outside any turn."""
        if row["side"] == DOOR:
            if row["conversation"]:
                await self._say(str(row["owner"]), str(row["conversation"]), text, card)
            return
        talks = self.talks()
        talk = await talks.talk(str(row["owner"]))
        if talk is None or talk["state"] == "closed" or not talk["token"]:
            return
        body: dict[str, Any] = {"text": text, **talks.signature(talk, text, card=card)}
        if card is not None:
            body["card"] = card
        try:
            answer = await talks.dialer.call("POST", f"{talk['base_url']}/a/messages", token=talk["token"], body=body)
            sent = answer.status in (200, 202)
        except Exception as exc:  # noqa: BLE001 — the action is done here whatever the door does with the word
            logger.info("a word on an action did not reach conversation %s: %s", talk["id"], exc)
            sent = False
        await talks._insert_message(talk["id"], "us", text, "sent" if sent else "failed", "", "action", kind=link_protocol.TEXT, card=card)
        if sent:
            await self.db.execute("UPDATE agent_talks SET waiting_for = 'reply', last_at = ? WHERE id = ?", (_stamp(), talk["id"]))
            talks.follow(talk["id"])

    @staticmethod
    def _cleanup(args: dict[str, Any]) -> None:
        staged = args.get("staged")
        if staged:
            shutil.rmtree(Path(staged).parent, ignore_errors=True)

    # -- patches ------------------------------------------------------------------------------------------

    async def _request_patch(self, state: Any, side: str, owner: str, conversation: str, label: str, files: list[str], extra: dict[str, Any], *, level: str, task: str = "") -> tuple[str, bool]:
        branch = str(extra.get("branch") or "").strip()
        if len(files) != 1:
            return "files names the one patch to apply: a diff, a format-patch file or a git bundle, as it lies in this chat's folder.", True
        if not link_flows.BRANCH_RE.fullmatch(branch):
            return "branch names the branch to apply it to, such as fix/parser. It must not be checked out.", True
        fs = state.services.fs if state.services is not None else None
        if fs is not None and getattr(fs, "remote", False):
            return "Patches are applied only in a chat whose folder is on this machine.", True
        root = Path(os.path.realpath(state.services.workspace_dir if state.services is not None else state.workspace))
        given = Path(files[0]).expanduser()
        real = Path(os.path.realpath(given if given.is_absolute() else root / given))
        if not real.is_relative_to(root) or not real.is_file():
            return f"There is no {files[0]} in this chat's folder.", True
        size = real.stat().st_size
        if size > link_flows.PATCH_BYTES:
            return f"A patch is at most {link_flows.PATCH_BYTES // link_protocol.MB} MB.", True
        data = await asyncio.to_thread(real.read_bytes)
        kind = link_flows.patch_kind(data[:4096])
        if not kind:
            return f"{files[0]} is neither a diff nor a git bundle.", True
        stat = link_flows.diff_stat(data) if kind != "bundle" else {"files": [], "added": 0, "removed": 0}
        words = link_flows.stat_words(stat) if kind != "bundle" else "a git bundle"
        # A copy of its own, so the turn cannot change the file between the operator's look and the apply.
        staging = self.app.settings.state_dir / STAGING / secrets.token_hex(8)
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
        staged = staging / link_protocol.safe_name(real.name)
        await asyncio.to_thread(shutil.copyfile, real, staged)
        args = {"repo": str(root), "staged": str(staged), "name": real.name, "branch": branch, "stat": words, "task": task, "label": label}
        standing = NOW if level == "trusted" else ASK
        action_id = await self._action(side, owner, conversation, state.session.id, "patch", args, "running" if standing == NOW else "held")
        if standing == NOW:
            row = await self.db.fetchone("SELECT * FROM link_actions WHERE id = ?", (action_id,))
            result = await self._carry_out(row)
            failed = (await self.db.fetchone("SELECT state FROM link_actions WHERE id = ?", (action_id,)))["state"] == "failed"
            return result, failed
        whose = f"from the guest of “{label}”" if side == DOOR else f"from {label}"
        if not await self._ask(state.session.id, action_id, f"Apply a patch {whose} to branch {branch}: {words}"):
            return "The operator could not be asked, so the patch was not applied.", True
        return f"The patch ({words}) waits for the operator. It goes to branch {branch} once they allow it, and the other side hears the commit.", False

    async def _apply(self, row: Any, args: dict[str, Any]) -> str:
        message = f"Apply a patch from {args.get('label') or 'an agent link'}"
        try:
            commit = await asyncio.to_thread(link_flows.apply_patch, Path(args["repo"]), Path(args["staged"]), args["branch"], message)
        finally:
            self._cleanup(args)
        result = f"Applied as commit {commit[:12]} on branch {args['branch']}."
        await self.db.execute("UPDATE link_actions SET state = 'done', result = ? WHERE id = ?", (result, row["id"]))
        await self._tell_other(row, f"Your patch is applied here: commit {commit[:12]} on branch {args['branch']}.")
        return result

    # -- a running service shown to a guest ---------------------------------------------------------------

    async def _share_service(self, state: Any, delivery: Any, level: str, extra: dict[str, Any]) -> tuple[str, bool]:
        name = str(extra.get("service") or "").strip()
        services = self.app.extensions.get("services")
        row = await services.get(state.session.id, name) if services is not None and name else None
        if row is None or not row.get("port") or row.get("status") != "running":
            return "service names a running service of this chat with a port.", True
        if not public_address.services_base(self.app.settings):
            return "This installation has no public address for services, so a guest cannot reach one.", True
        args = {"name": name, "service": name}
        standing = link_flows.standing(level, "share_service")
        action_id = await self._action(DOOR, delivery.link_id, delivery.conversation_id, state.session.id, "service", args, "running" if standing == NOW else "held")
        if standing == NOW:
            got = await self._carry_out(await self.db.fetchone("SELECT * FROM link_actions WHERE id = ?", (action_id,)))
            return got, got.startswith("It failed")
        if not await self._ask(state.session.id, action_id, f"Show the service {name} to the guest of “{await self._label(delivery.link_id)}” for up to {SERVICE_HOURS} hours"):
            return "The operator could not be asked, so the service was not shown.", True
        return "The operator decides; tell the guest the address follows once they allow it.", False

    async def _open_service(self, row: Any, args: dict[str, Any]) -> str:
        """A one-time address of the service for this guest: a phone's pass through the services door, which ends
        with the conversation, the link, or its hours, whichever comes first."""
        from daedalus.stores import pairing  # Lazy: the stores import the app's.

        services = self.app.extensions.get("services")
        base = public_address.services_base(self.app.settings)
        if services is None or not base:
            raise ValueError("there is no public address for services")
        slug = await services.ensure_slug(str(row["session_id"]), args["name"])
        code = await pairing.mint_phone(self.db, slug, lan=False)
        url = f"{base}/s/{slug}/?phone={code}"
        expires = (_now() + timedelta(hours=SERVICE_HOURS)).isoformat()
        args = {**args, "slug": slug, "code_hash": pairing.code_digest(code)}
        await self.db.execute("UPDATE link_actions SET state = 'done', args = ?, expires_at = ? WHERE id = ?", (json.dumps(args), expires, row["id"]))
        card = link_flows.card_view({"type": link_flows.SERVICE, "title": args["name"], "url": url, "expires_at": expires, "state": "open"})
        await self._tell_other(row, link_flows.card_words(card) if card else url, card)
        return f"The guest has a one-time address of {args['name']}, open until {expires[:16]} or the conversation's end."

    async def revoke_service(self, action_id: str, reason: str = "revoked") -> bool:
        from daedalus.stores import pairing  # Lazy: the stores import the app's.

        row = await self.db.fetchone("SELECT * FROM link_actions WHERE id = ? AND kind = 'service' AND state = 'done'", (action_id,))
        if row is None:
            return False
        args = json.loads(row["args"])
        await pairing.forget_phone(self.db, str(args.get("code_hash") or ""))
        await self.db.execute("UPDATE link_actions SET state = 'revoked', result = ? WHERE id = ?", (reason, action_id))
        card = link_flows.card_view({"type": link_flows.SERVICE, "title": args.get("name", ""), "url": "", "state": "revoked"})
        if reason != "closed" and card is not None:
            await self._tell_other(row, link_flows.card_words(card), card)
        return True

    # -- meetings -----------------------------------------------------------------------------------------

    async def _free(self, text: str, extra: dict[str, Any], *, titles: bool) -> dict[str, Any]:
        from daedalus.tools.calendar import free_time  # Lazy: the tools import the host.

        minutes = extra.get("duration_minutes")
        found = await free_time(
            self.db, duration_minutes=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else 30, start=str(extra.get("start") or ""), end=str(extra.get("end") or ""),
            limit=link_flows.MAX_SLOTS, with_busy=titles,
        )
        card = link_flows.card_view({"type": link_flows.SLOTS, "title": text, "duration_minutes": minutes if isinstance(minutes, int) else 30, "timezone": found["timezone"], "slots": found["slots"], "busy": found.get("busy") or []})
        assert card is not None
        return card

    async def _guest_slots(self, state: Any, delivery: Any, level: str, text: str, extra: dict[str, Any]) -> tuple[str, bool]:
        try:
            card = await self._free(" ".join(text.split())[:160], extra, titles=link_flows.standing(level, "busy_titles") == NOW)
        except ValueError as exc:
            return str(exc), True
        words = link_flows.card_words(card)
        if not await self._say(delivery.link_id, delivery.conversation_id, words, card):
            return "The door did not take the free times; try again.", True
        await self.keep_card(DOOR, delivery.link_id, state.session.id, "mine", card, conversation=delivery.conversation_id, text=words)
        return f"Sent the guest these free times (busy or free only{', with titles: the link is trusted' if card.get('busy') else ''}):\n{words}", False

    async def _talk_slots(self, state: Any, talk: Any, text: str, extra: dict[str, Any]) -> tuple[str, bool]:
        try:
            # A door is never trusted from this side: it hears when the operator is free and nothing more.
            card = await self._free(" ".join(text.split())[:160], extra, titles=False)
        except ValueError as exc:
            return str(exc), True
        sent = await self.talks()._send(state, talk, link_flows.card_words(card), 0, card=card)
        await self.keep_card(TALK, talk["id"], state.session.id, "mine", card, text=link_flows.card_words(card))
        return sent.text, sent.error

    def _meeting(self, text: str, extra: dict[str, Any], state: str = "proposed") -> dict[str, Any] | None:
        return link_flows.card_view({"type": link_flows.MEETING, "title": " ".join(text.split())[:160] or "Meeting", "start": extra.get("start"), "end": extra.get("end"), "timezone": extra.get("timezone") or "", "state": state})

    async def _guest_meet(self, state: Any, delivery: Any, level: str, text: str, extra: dict[str, Any]) -> tuple[str, bool]:
        card = self._meeting(text, extra)
        if card is None:
            return "start and end are ISO 8601 times with their zone, the end after the start; text is the meeting's title.", True
        label = await self._label(delivery.link_id)
        args = {"title": card["title"], "start": card["start"], "end": card["end"], "timezone": card["timezone"], "with": label}
        standing = link_flows.standing(level, "meet")
        action_id = await self._action(DOOR, delivery.link_id, delivery.conversation_id, state.session.id, "meeting", args, "running" if standing == NOW else "held")
        if standing == NOW:
            got = await self._carry_out(await self.db.fetchone("SELECT * FROM link_actions WHERE id = ?", (action_id,)))
            return got, got.startswith("It failed")
        if not await self._ask(state.session.id, action_id, f"Book “{card['title']}” with the guest of “{label}”, {card['start']} to {card['end']}"):
            return "The operator could not be asked, so nothing was booked.", True
        return "The meeting waits for the operator. The guest hears when it is booked or declined.", False

    async def _talk_level(self, state: Any, talk: Any) -> str:
        """``trusted`` for a conversation with a contact the operator trusts as themselves, or in a session set to
        bypass its checks: what such a conversation asks for is done at once. ``""`` (always asked) otherwise."""
        from daedalus.extensions.agent_talk import talks_of  # Lazy: agent_talk imports this module.

        talks = talks_of(self.app)
        return "trusted" if getattr(state, "bypass", False) or (talks is not None and await talks.talk_trusted(talk)) else ""

    async def _talk_meet(self, state: Any, talk: Any, text: str, extra: dict[str, Any]) -> tuple[str, bool]:
        card = self._meeting(text, extra)
        if card is None:
            return "start and end are ISO 8601 times with their zone, the end after the start; text is the meeting's title.", True
        args = {"title": card["title"], "start": card["start"], "end": card["end"], "timezone": card["timezone"], "with": str(talk["host"])}
        if await self._talk_level(state, talk) == "trusted":
            action_id = await self._action(TALK, talk["id"], "", state.session.id, "meeting", args, "running")
            row = await self.db.fetchone("SELECT * FROM link_actions WHERE id = ?", (action_id,))
            result = await self._carry_out(row)
            failed = (await self.db.fetchone("SELECT state FROM link_actions WHERE id = ?", (action_id,)))["state"] == "failed"
            return result, failed
        action_id = await self._action(TALK, talk["id"], "", state.session.id, "meeting", args, "held")
        # Always the operator's: the time came from a stranger, and a calendar entry outlives this turn.
        if not await self._ask(state.session.id, action_id, f"Book “{card['title']}” with {talk['host']}, {card['start']} to {card['end']}"):
            return "The operator could not be asked, so nothing was booked.", True
        return "The meeting waits for the operator. Once they allow it, it is in the calendar and the other side hears it is booked.", False

    async def _book(self, row: Any, args: dict[str, Any]) -> str:
        from daedalus.stores.calendar import CalendarStore  # Lazy: the stores import the app's.

        store = CalendarStore(self.db)
        zone = args.get("timezone") or (await store.settings())["timezone"]
        event = await store.create(
            {"title": f"{args['title']} — {args['with']}"[:200], "start_at": args["start"], "end_at": args["end"], "timezone": zone, "description": f"Agreed over an agent link with {args['with']}."},
            f"agent:{row['session_id']}",
        )
        result = f"Booked: {args['title']}, {args['start']} to {args['end']}."
        await self.db.execute("UPDATE link_actions SET state = 'done', result = ? WHERE id = ?", (result, row["id"]))
        card = link_flows.card_view({"type": link_flows.MEETING, "title": args["title"], "start": args["start"], "end": args["end"], "timezone": zone, "state": "booked"})
        if card is not None:
            await self._tell_other(row, link_flows.card_words(card), card)
            await self.keep_card(str(row["side"]), str(row["owner"]), str(row["session_id"]), "mine", card, conversation=str(row["conversation"]))
        return f"{result} (event {event.get('event_id', '')})"

    # -- the end of a link, and the slow clock ------------------------------------------------------------

    async def link_ended(self, link_id: str) -> None:
        """A link of ours is revoked or its conversation closed: what it was promised ends with it."""
        await self.db.execute("UPDATE link_subscriptions SET state = 'ended', reason = 'closed', ended_at = ? WHERE link_id = ? AND state = 'active'", (_stamp(), link_id))
        for row in await self.db.fetchall("SELECT id FROM link_actions WHERE side = ? AND owner = ? AND kind = 'service' AND state = 'done'", (DOOR, link_id)):
            await self.revoke_service(str(row["id"]), "closed")
        await self.db.execute("UPDATE link_actions SET state = 'refused', result = 'the link closed first', decided_at = ? WHERE side = ? AND owner = ? AND state = 'held'", (_stamp(), DOOR, link_id))

    async def sweep(self) -> None:
        from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable  # Lazy: guest_door imports this module.

        for row in await self.db.fetchall("SELECT id FROM link_polls WHERE state = ? OR (woke = 0 AND settled_at IS NOT NULL)", (link_flows.POLL_OPEN,)):
            await self.check_poll(str(row["id"]))
        await self.fire_due()
        now = _stamp()
        for row in await self.db.fetchall("SELECT id FROM link_actions WHERE kind = 'service' AND state = 'done' AND expires_at <= ?", (now,)):
            await self.revoke_service(str(row["id"]), "expired")
        bridge = self.bridge()
        if bridge is None:
            return
        live = await self.db.fetchall(
            "SELECT DISTINCT link_id FROM link_subscriptions WHERE state = 'active' UNION SELECT DISTINCT owner FROM link_actions WHERE side = ? AND kind = 'service' AND state = 'done'", (DOOR,),
        )
        for row in live:
            link_id = str(row[0])
            known = await bridge.known_link(link_id)
            if known is None or known["revoked_at"]:
                await self.link_ended(link_id)
                continue
            try:
                view = (await bridge.client.call("GET", f"/control/links/{link_id}")).get("link") or {}
            except DoorRefused:
                await self.link_ended(link_id)
                continue
            except DoorUnavailable:
                return
            conversation = view.get("conversation") or {}
            if view.get("state") in ("revoked", "expired") or conversation.get("state") == "closed":
                await self.link_ended(link_id)

    async def serve(self) -> None:
        try:
            while True:
                try:
                    await self.sweep()
                except Exception:  # noqa: BLE001 — the next sweep may do better; nothing here may end the task
                    logger.exception("the sweep of link flows failed")
                await asyncio.sleep(SWEEP_SECONDS)
        finally:
            for task in list(self._background):
                task.cancel()


def flows_of(app: Application | Any) -> LinkFlows | None:
    return (getattr(app, "extensions", None) or {}).get(EXTENSION_KEY)


async def install(app: Application) -> list[asyncio.Task[None]]:
    flows = LinkFlows(app)
    app.extensions[EXTENSION_KEY] = flows
    if app.manager is None:
        return []
    app.manager.answer_hooks[APPROVE_HOOK] = flows.approved
    return [asyncio.create_task(flows.serve(), name="link-flows")]


__all__ = ["APPROVE_HOOK", "EXTENSION_KEY", "FLOW_ACTIONS", "GUEST_ACTIONS", "POLL_PREFIX", "SUB_PREFIX", "LinkFlows", "flows_of", "install"]
