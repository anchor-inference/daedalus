"""A project's topic in Telegram: the one place there that is the project's, spoken in only by its orchestrator.

When a project's orchestrator is switched on and a forum is bound, it gets a topic under the project's
name. The operator writes there and the orchestrator receives it, through the ordinary topic binding;
the orchestrator's turns do not stream there (its session is quiet, see ``front.is_quiet``) and its
answers stay in the app. What does appear is chosen here, by a bus subscriber, and nothing else posts:

- the orchestrator's reports (``ProjectReport``) and its own notifications (``Notify``);
- every request of the project that waits on the operator — the orchestrator's questions, the folders it
  asks for, the staff requests it escalated or that go to the operator directly — with buttons that
  answer it (first answer wins; a late tap is told who was first) and a way to answer in words.

The notification router never posts these (an orchestrated project's items are routed to the
orchestrator), and staff, subagents and the router are never given the topic, so each item appears once.

Telegram is optional: without a bot nothing here is installed, and everything above lives in the app.
In private mode (no forum) the same posts go to the private chat under the project's name, and the
private chat stays the window onto whatever session the operator is using; a reply to one of these posts
goes to the project's orchestrator (or answers the request it shows), never to that session.

The topic follows the office: replacing the orchestrator re-points it to the successor, switching it off
closes it, renaming the project renames it, and a start reconciles every enabled project. Telegram's
rate limit on opening topics leaves a project without one until the next reconcile or enable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions.dispatcher_telegram import is_mirrored
from daedalus.extensions.staff import AlreadyAnswered
from daedalus.host.events import AppEvent, EventFilter
from daedalus.stores.projects import Project
from daedalus.stores.staff import Ask
from daedalus.transport.telegram.front import TelegramBusy, TelegramRefused

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.transport.telegram.front import TelegramFront, TelegramOutbox, TopicBinding

logger = logging.getLogger(__name__)

POSTS_KEY = "project_topics.posts"
"""kv: the messages posted here, ``"<chat>:<message>" -> {"project", "ask"}``, so a reply to one after a
restart still finds its project and a late answer can still close its buttons."""
POSTS_KEPT = 400
OPTIONS_MAX = 8
TEXT_MAX = 3500
"""Below Telegram's 4096 characters, with room for the header and the request's short id."""
REPORT_SOURCES = ("project_report",)
EVENTS = ("notify", "ask.pending", "permission.pending", "ask.answered", "permission.resolved", "project.changed", "staff.status")
"""What can mean a request now waits on the operator, or no longer does. The request row is the truth
and each of these only says "look again": a staff member's question is announced by its session before
its row exists, and an escalation reaches the operator as a released notification or a new one, not as
a request event, so the rows are read again rather than the events trusted to carry them."""


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ProjectTopics:
    def __init__(self, app: Application, front: TelegramFront) -> None:
        self.app = app
        self.front = front
        assert app.manager is not None
        self.manager = app.manager
        self._prompts: dict[int, str] = {}
        """A "type your answer" prompt's message id → the request it answers."""
        self._lock = asyncio.Lock()

    # -- where a project speaks ------------------------------------------------------------------

    @staticmethod
    def orchestrator_of(project: Project) -> str:
        o = project.settings.orchestrator
        return o.session_id if o.enabled else ""

    async def binding(self, project: Project) -> TopicBinding | None:
        """The project's topic: the binding of its orchestrator's session, when it has one."""
        session_id = self.orchestrator_of(project)
        return await self.front.binding_for_session(session_id) if session_id else None

    async def outbox(self, project: Project) -> TelegramOutbox | None:
        """Where the project's posts go: its topic, or the private chat under the project's name."""
        if not self.orchestrator_of(project):
            return None
        binding = await self.binding(project)
        if binding is not None:
            return self.front.outbox(binding.chat_id, binding.thread_id)
        owner = self.front.settings.owner_user_id
        if self.front.private_mode() and owner:
            name = project.name
            return self.front.outbox(owner, None, header=lambda: f"[{name}]")
        # A forum is bound but the topic could not be opened yet: nothing falls through to General.
        return None

    # -- the topic follows the office ---------------------------------------------------------------

    async def ensure(self, project: Project) -> TopicBinding | None:
        """Open (or keep) the project's topic for its current orchestrator; None in private mode or when
        Telegram would not open one now."""
        session_id = self.orchestrator_of(project)
        if not session_id:
            return None
        try:
            binding = await self.front.open_quiet_topic(session_id, project.name)
        except KeyError:
            return None
        except (TelegramBusy, TelegramRefused) as exc:
            logger.warning("no topic for project %s yet: %s", project.name, exc)
            await self.manager.projects.record(project.id, "system", "telegram", f"Telegram did not open the project's topic ({exc}); it is tried again at the next start or switch-on.", {})
            return None
        if binding is not None and project.settings.orchestrator.telegram_topic_id != binding.thread_id:
            await self.manager.projects.update_orchestrator(project.id, telegram_topic_id=binding.thread_id)
        return binding

    async def repoint(self, project: Project) -> None:
        """A replaced orchestrator: its successor takes over the same topic."""
        successor = self.orchestrator_of(project)
        topic = project.settings.orchestrator.telegram_topic_id
        forum = self.front.config.telegram.forum_chat_id
        if not successor:
            return
        if topic and forum:
            old = await self.front.binding_for_topic(forum, topic)
            if old is not None and old.session_id != successor:
                # The successor is marked quiet before the topic is its own, as a fresh topic's session is.
                state = await self.manager.get_state(successor)
                if state is not None:
                    state.metadata["telegram_quiet"] = True
                    state.session.metadata["telegram_quiet"] = True
                    state.metadata.pop("telegram_detached", None)
                    state.session.metadata.pop("telegram_detached", None)
                    await self.manager.sessions.update_metadata(successor, state.session.metadata)
                await self.front.repoint_topic(old, successor)
                await self._detach(old.session_id)
                return
        await self.ensure(project)

    async def _detach(self, session_id: str) -> None:
        """A retired orchestrator is off Telegram for good: no topic of its own, no private chat."""
        state = await self.manager.get_state(session_id)
        if state is None:
            return
        state.metadata["telegram_detached"] = True
        state.session.metadata["telegram_detached"] = True
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)

    async def close(self, project: Project) -> None:
        """Switched off: the topic is closed (its history stays in Telegram) and the setting cleared."""
        topic = project.settings.orchestrator.telegram_topic_id
        forum = self.front.config.telegram.forum_chat_id
        if topic and forum:
            binding = await self.front.binding_for_topic(forum, topic)
            if binding is not None:
                await self.front.close_topic(binding)
                await self._detach(binding.session_id)
        if topic:
            await self.manager.projects.update_orchestrator(project.id, telegram_topic_id=0)

    async def rename(self, project: Project) -> None:
        binding = await self.binding(project)
        if binding is not None and binding.thread_id and binding.title != project.name:
            await self.front.rename_topic(binding, project.name)

    async def reconcile(self) -> int:
        """At start: every enabled project has its topic, under its current name. Returns how many have one."""
        count = 0
        for project in await self.manager.projects.list():
            if not self.orchestrator_of(project):
                continue
            binding = await self.ensure(project)
            if binding is not None:
                await self.rename(project)
                count += 1
        return count

    # -- the poster --------------------------------------------------------------------------------

    async def on_event(self, event: AppEvent) -> None:
        if not event.project_id:
            return
        project = await self.manager.projects.get(event.project_id)
        if project is None:
            return
        try:
            if event.type == "project.changed":
                await self._on_changed(project, str(event.payload.get("change") or ""))
            elif event.type == "notify":
                await self._on_notify(project, event)
            elif event.type in ("ask.pending", "permission.pending", "staff.status"):
                await self.post_requests(project)
            else:
                await self.close_answered(project)
        except Exception:  # noqa: BLE001 — Telegram is a window; a failed post never stops the bus
            logger.warning("project topic: could not handle %s for %s", event.type, project.name, exc_info=True)

    async def _on_changed(self, project: Project, change: str) -> None:
        if change == "orchestrator.enabled":
            await self.ensure(project)
            await self.post_requests(project)
        elif change == "orchestrator.replaced":
            await self.repoint(project)
        elif change == "orchestrator.disabled":
            await self.close(project)
        elif change == "settings":
            await self.rename(project)
        elif change.startswith("staff.") or change == "folders":
            # An escalation or a folder request may have just reached the operator without an event of its own.
            await self.post_requests(project)

    async def _on_notify(self, project: Project, event: AppEvent) -> None:
        note = event.payload.get("notification") or {}
        await self.post_requests(project)
        if event.payload.get("merged") or note.get("request_ref"):
            # A request is posted from its row, with its buttons (just above); a repeat of a keyed
            # notification is a refresh of one already posted, not news for the topic.
            return
        source = str(note.get("source") or "")
        own = self.orchestrator_of(project)
        if not (source in REPORT_SOURCES or source.startswith("orchestrator") or (own and note.get("session_id") == own)):
            return
        outbox = await self.outbox(project)
        if outbox is None:
            return
        title, body = str(note.get("title") or "").strip(), str(note.get("body") or "").strip()
        mark = {"warning": "⚠️", "ok": "✅", "error": "⛔"}.get(str(note.get("tone") or ""), "🧭")
        text = _clip(f"{mark} {title}\n\n{body}" if body else f"{mark} {title}")
        message_id = await self.front.post(outbox, text)
        await self._remember(outbox.chat_id, message_id, project.id, None)

    # -- requests to the operator ---------------------------------------------------------------------

    async def _posts(self) -> dict[str, dict[str, Any]]:
        value = await self.manager.db.kv_get(POSTS_KEY, {})
        return dict(value) if isinstance(value, dict) else {}

    async def _remember(self, chat_id: int, message_id: int, project_id: str, ask_id: str | None) -> None:
        posts = await self._posts()
        posts[f"{chat_id}:{message_id}"] = {"project": project_id, "ask": ask_id}
        if len(posts) > POSTS_KEPT:
            posts = dict(list(posts.items())[-POSTS_KEPT:])
        await self.manager.db.kv_set(POSTS_KEY, posts)

    async def _forget(self, key: str) -> None:
        posts = await self._posts()
        if posts.pop(key, None) is not None:
            await self.manager.db.kv_set(POSTS_KEY, posts)

    async def post_requests(self, project: Project) -> int:
        """Post every request of the project that waits on the operator and is not posted yet."""
        if not self.orchestrator_of(project):
            return 0
        async with self._lock:
            outbox = await self.outbox(project)
            if outbox is None:
                return 0
            posted = {entry.get("ask") for entry in (await self._posts()).values()}
            count = 0
            for ask in await self.manager.asks.open_for(project.id, routed_to="operator"):
                if ask.id in posted:
                    continue
                if await is_mirrored(self.app, ask):
                    # Shown in the main orchestrator's chat, so posted once, where it lives (General or
                    # its header in the private chat); a second post here would be a second set of buttons.
                    continue
                text, rows = await self._request(project, ask)
                message_id = await self.front.post(outbox, text, rows)
                await self._remember(outbox.chat_id, message_id, project.id, ask.id)
                count += 1
            return count

    async def _request(self, project: Project, ask: Ask) -> tuple[str, list[list[tuple[str, str]]]]:
        member = await self.manager.staff.get(ask.staff_id) if ask.staff_id else None
        who = "The orchestrator" if ask.origin == "orchestrator" else (member.name if member is not None else "A staff member")
        what = {"question": "asks", "permission": "asks for permission", "folder": "asks for a folder"}.get(ask.kind, "asks")
        lines = [f"❓ {who} {what} [{ask.short_id}]", "", _clip(ask.text, 3000)]
        if ask.suggestion:
            lines += ["", f"The orchestrator suggests: {_clip(ask.suggestion, 300)}"]
        rows: list[list[tuple[str, str]]] = []
        if ask.kind == "question":
            options = [str(o) for o in (ask.detail.get("options") or []) if str(o).strip()][:OPTIONS_MAX]
            rows = [[(option[:60], f"pq:{ask.id}:{i}")] for i, option in enumerate(options)]
            rows.append([("✍️ Answer", f"pw:{ask.id}")])
        elif await self._host_level(ask, member):
            # The operator's rule: a request to act on the host itself is answered in the app, where the
            # whole of it is shown, never from a chat button.
            lines += ["", "This one acts on the host: answer it in the app."]
        else:
            rows = [[("✅ Allow", f"pp:{ask.id}:allow"), ("✖ Deny", f"pp:{ask.id}:deny")]]
        return "\n".join(lines), rows

    async def _host_level(self, ask: Ask, member: Any) -> bool:
        if ask.kind == "folder":
            return str(ask.detail.get("env") or "") == "host"
        if ask.kind != "permission" or member is None:
            return False
        if member.env == "host":
            return True
        if ask.staff_session_id:
            row = await self.manager.db.fetchone(
                "SELECT f.env FROM staff_sessions s JOIN project_folders f ON f.id = s.folder_id WHERE s.id = ?", (ask.staff_session_id,)
            )
            return row is not None and row["env"] == "host"
        return False

    async def close_answered(self, project: Project) -> None:
        """Take the buttons off every posted request of the project that is answered now, saying how."""
        for key, entry in list((await self._posts()).items()):
            if entry.get("project") != project.id or not entry.get("ask"):
                continue
            ask = await self.manager.asks.get(str(entry["ask"]))
            if ask is None or ask.open:
                continue
            chat_id, _, message_id = key.partition(":")
            await self.front.edit_post(int(chat_id), int(message_id), _clip(f"{ask.text}\n\n— {self.answered(ask)}"))
            await self._forget(key)

    @staticmethod
    def answered(ask: Ask) -> str:
        r = ask.resolution or {}
        via = str(r.get("via") or "")
        where = {"telegram": "in Telegram", "app": "in the app", "notification": "from a notification", "push": "from a notification", "orchestrator": "by the orchestrator"}.get(via, "in the app")
        if ask.resolved_by == "system":
            return f"withdrawn: {r.get('closed') or 'the request ended'}"
        if ask.kind in ("permission", "folder") and r.get("allow") is not None:
            answer = "allowed" if r.get("allow") else "denied"
        else:
            answer = ", ".join(str(s) for s in r.get("selected") or []) or str(r.get("text") or "") or "answered"
        who = "the orchestrator" if ask.resolved_by == "orchestrator" else "you"
        return f"answered {where} by {who}: {answer}"

    # -- the operator answers in Telegram ------------------------------------------------------------

    async def _answer(self, ask_id: str, **answer: Any) -> str:
        """Answer through the team, first answer wins; the words for the operator either way."""
        team = self.app.extensions.get("staff")
        if team is None:
            return "the team is not running here"
        try:
            await team.answer(ask_id, by="operator", via="telegram", **answer)  # type: ignore[attr-defined]
        except AlreadyAnswered as exc:
            ask = await self.manager.asks.get(ask_id)
            return f"too late: {self.answered(ask) if ask is not None else exc}"
        except KeyError:
            return "that request no longer exists"
        except ValueError as exc:
            return str(exc)[:180]
        ask = await self.manager.asks.get(ask_id)
        if ask is not None:
            await self.close_answered(await self._project(ask.project_id))
        return "answered"

    async def _project(self, project_id: str) -> Project:
        project = await self.manager.projects.get(project_id)
        assert project is not None
        return project

    async def on_callback(self, query: Any, data: list[str]) -> None:
        """``pq:<ask>:<i>`` picks an option, ``pp:<ask>:allow|deny`` decides, ``pw:<ask>`` asks for words."""
        kind = data[0]
        ask_id = data[1] if len(data) > 1 else ""
        ask = await self.manager.asks.get(ask_id) if ask_id else None
        if ask is None:
            await query.answer("stale button")
            return
        if kind == "pw":
            message = query.message
            chat_id = message.chat.id if message is not None else self.front.settings.owner_user_id
            thread_id = getattr(message, "message_thread_id", None) if message is not None else None
            prompt = await self.front.send_force_reply(chat_id, thread_id, f"Your answer to [{ask.short_id}]:")
            self._prompts[prompt] = ask.id
            await query.answer()
            return
        if kind == "pq":
            options = [str(o) for o in (ask.detail.get("options") or [])]
            try:
                choice = options[int(data[2])]
            except (IndexError, ValueError):
                await query.answer("stale button")
                return
            said = await self._answer(ask.id, selected=[choice])
        else:
            if len(data) != 3 or data[2] not in ("allow", "deny"):
                await query.answer("stale button")
                return
            said = await self._answer(ask.id, allow=data[2] == "allow")
        await query.answer(said[:190])
        if said.startswith("too late") and query.message is not None:
            await self.close_answered(await self._project(ask.project_id))

    async def on_message(self, message: Any) -> bool:
        """A reply to one of the project's posts: an answer to the request it shows, or words for the
        orchestrator. Anything else is left to the front's ordinary routing."""
        reply = getattr(message, "reply_to_message", None)
        if reply is None:
            return False
        text = (message.text or message.caption or "").strip()
        ask_id = self._prompts.pop(reply.message_id, None)
        entry = None
        if ask_id is None:
            entry = (await self._posts()).get(f"{message.chat.id}:{reply.message_id}")
            if entry is None:
                return False
            ask_id = entry.get("ask")
        if ask_id:
            ask = await self.manager.asks.get(str(ask_id))
            if ask is not None and ask.open and ask.kind == "question" and text:
                said = await self._answer(ask.id, text=text)
                await message.reply(said if said != "answered" else f"Answered [{ask.short_id}].")
                return True
            # A permission is decided with its buttons; words about it are for the orchestrator, below.
        project = await self.manager.projects.get(str((entry or {}).get("project") or "")) if entry else None
        if project is None and ask_id:
            ask = await self.manager.asks.get(str(ask_id))
            project = await self.manager.projects.get(ask.project_id) if ask is not None else None
        session_id = self.orchestrator_of(project) if project is not None else ""
        if not session_id or not text:
            return False
        binding = await self.front.binding_for_session(session_id)
        if binding is not None and binding.chat_id == message.chat.id and binding.thread_id == (message.message_thread_id or 0):
            return False  # inside the project's topic the binding already routes it, with the front's reactions
        await self.manager.submit(session_id, text, via="telegram", address={"chat_id": message.chat.id, "thread_id": message.message_thread_id or 0})
        await message.reply(f"Sent to the orchestrator of {project.name}.")  # type: ignore[union-attr]
        return True

    def attach(self) -> asyncio.Task[None]:
        self.front.callback_hooks.update({"pq": self.on_callback, "pp": self.on_callback, "pw": self.on_callback})
        self.front.message_interceptors.append(self.on_message)
        return self.manager.bus.on(EventFilter(types=EVENTS), self.on_event, name="project-topics")


async def install(app: Application) -> list[asyncio.Task[None]]:
    front = app.front
    if front is None:
        return []  # no bot: projects live in the app alone
    topics = ProjectTopics(app, front)
    app.extensions["project_topics"] = topics
    handler = topics.attach()

    async def reconcile() -> None:
        try:
            await topics.reconcile()
            for project in await app.manager.projects.list():  # type: ignore[union-attr]
                await topics.post_requests(project)
        except Exception:  # noqa: BLE001 — a start never fails on Telegram
            logger.warning("project topics: reconcile failed", exc_info=True)

    return [handler, asyncio.create_task(reconcile(), name="project-topics-reconcile")]


__all__ = ["ProjectTopics", "install"]
