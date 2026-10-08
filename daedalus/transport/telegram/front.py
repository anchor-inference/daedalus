"""The Telegram side: topics as sessions, the operator channel, files, questions."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    Message,
    ReactionTypeEmoji,
    ReplyParameters,
)
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import NO_MODEL_MESSAGE, NoModelConfigured, RuntimeConfig, Settings
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.prompts import DEFAULT_RULES, split_headline
from daedalus.host.session_runner import Attachment, SessionManager, SessionState
from daedalus.security.operator_secrets import SecretError
from daedalus.speech.service import LocalSpeech, recogniser_available, transcribe_recording
from daedalus.stores.sqlite import DeliveryLedger
from daedalus.transport.telegram.markdown import markdown_to_html, split_message, strip_tags
from daedalus.transport.telegram.render import Outbox, RunRenderer, RunView
from daedalus.transport.telegram.voice import TranscriptionError, voice_note_text

logger = logging.getLogger(__name__)

HELP_TOPICS = """<b>Daedalus</b>
Each forum topic is one agent session in a project. Write in a topic to talk to that session; files you send land in its working directory.

/bind — (in a supergroup with topics) make it the session hub
/new &lt;title&gt; — new session (new topic)
/stop — stop the current run · /detach — keep the agent on the site and close its topic
/close — close this topic (asks whether to delete the agent)
/sessions · /status — what exists, what is running
"""

HELP_PRIVATE = """<b>Daedalus</b>
Every session lives in this chat, which is a window onto one of them at a time. Write here and the current session hears you; files you send land in its project directory. When another session speaks — a scheduled report, a loop agent, a question — its name is the line above its words, and an answer goes back to it.

/sessions · /status — the sessions, numbered; what is running
/use &lt;n|title&gt; — write to that session from now on
/new &lt;title&gt; — new session, and write to it
/stop — stop the current run · /close — put the current session away (asks whether to delete the agent)
/bind — (in a supergroup with topics) give every session a topic of its own instead
"""

HELP_PRIVATE_FORUM = """<b>Daedalus</b>
This chat is a window onto one session at a time. /new opens a topic in the bound group, and that session speaks there. A session started on the site stays on the site.

/sessions · /status — the sessions, numbered; what is running
/use &lt;n|title&gt; — write to that session from now on
/new &lt;title&gt; — new session, in its own topic
/stop — stop the current run · /close — put the current session away (asks whether to delete the agent)
"""


def yagni_argument(args: str, current: bool) -> bool | None:
    """What ``/yagni`` asks for: ``on``, ``off``, or the other state with no argument; ``None`` for anything else."""
    return {"": not current, "on": True, "off": False}.get(args.strip().lower())


HELP_TAIL = """/main — the main orchestrator: in a group it lives in General; here it speaks under 🧭 Main, and a reply to one of its posts reaches it
/rename &lt;title&gt; — rename this session (and its topic) · /compact [focus] — replace the history with a summary
/delete &lt;id&gt; · /cleanup — delete a session; delete every session whose topic is already closed
/model [provider/]&lt;name&gt;|default · /thinking on|off|low|medium|high — model settings (per session; in a group's General topic, the default)
/usage · /balance — spend and provider balances
/schedules · /schedule run|on|off|delete &lt;id&gt; — scheduled tasks
/inbox [all|clear] · /heartbeat [on|off|run] · /doctor · /intents — inbox, the periodic check, health, standing intents
/mode [quick|deep|careful|default] — limits and rules for this session · /yagni [on|off] — the smallest change that does the job
/secret &lt;name&gt; &lt;value&gt; — hand this session a password or key to use without reading it; the message is deleted
/board [all] · /peer here &lt;name&gt;|list|forget &lt;name&gt; — the task board; name this session as a peer other sessions can ask
/approval manual|auto · /verbosity 0|1|2 — self-change approval, chat detail
/rebuild · /rollback [n] · /panic — supervisor operations
/prompt — show the editable working rules (edit them in the Mini App → Settings)
/settings · /app — configuration, Mini App link
"""


@dataclass(slots=True)
class TopicBinding:
    chat_id: int
    thread_id: int
    session_id: str
    title: str


@dataclass(slots=True)
class InboundBuffer:
    """Merges the fragments Telegram makes of one long message or one media album."""

    text: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


_FLOOD_UNTIL: dict[int, float] = {}
"""Per chat: the monotonic time until which Telegram asked us to stop. Shared by every
message in the chat, so one flood-controlled edit pauses all the cosmetic edits at once."""


def flooded(chat_id: int) -> bool:
    return time.monotonic() < _FLOOD_UNTIL.get(chat_id, 0.0)


def note_flood(chat_id: int, seconds: float) -> None:
    _FLOOD_UNTIL[chat_id] = max(_FLOOD_UNTIL.get(chat_id, 0.0), time.monotonic() + seconds)


async def tg_call(fn: Callable[..., Awaitable[Any]], *args: Any, attempts: int = 4, flood_chat: int | None = None, **kwargs: Any) -> Any:
    """Call a Bot API method, waiting out flood-control pauses instead of failing.

    ``flood_chat`` records the pause for that chat so optional edits skip it instead of queueing.
    """
    for attempt in range(attempts):
        try:
            result = await fn(*args, **kwargs)
            if attempt and flood_chat is not None:
                _FLOOD_UNTIL.pop(flood_chat, None)  # the pause was waited out; edits may resume
            return result
        except TelegramRetryAfter as exc:
            if flood_chat is not None:
                note_flood(flood_chat, float(exc.retry_after))
            if attempt == attempts - 1:
                raise
            logger.warning("telegram flood control: waiting %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 0.5)
    raise RuntimeError("unreachable")


FREE_REACTIONS = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)
"""The emoji a bot may react with (Telegram rejects anything else)."""

FORCE_REPLY_MAX = 50
"""How many open "type your answer" prompts are remembered by the session that asked."""
VOICE_PENDING_TTL_SECONDS = 2 * 3600
VOICE_PENDING_MAX = 50
SPEECH_MIME_TYPES = {"audio/ogg", "audio/opus", "audio/oga", "audio/wav", "audio/x-wav", "audio/webm"}
RUN_REACTIONS = {"received": "👀", "steered": "✍", "completed": "🔥", "failed": "💔", "cancelled": "🫡", "awaiting": "🤔", "interrupted": "😴", "busy": "🤝"}
TOPIC_STATUS_PREFIX = {"running": "🟢", "awaiting": "🔴", "completed": "🏁", "failed": "💥", "cancelled": "⏹", "interrupted": "⏸", "compacting": "🗜"}
"""Telegram silently drops some emoji from the start of a topic name (✅ ❓ ✔️ ☑️ were measured to
vanish, and a repeat rename then fails with TOPIC_NOT_MODIFIED); every prefix here was verified to survive."""
CURRENT_SESSION_KEY = "telegram.current_session"
"""Which session the private chat is a window onto; in kv, so a restart resumes the same one."""
SESSION_HEADER = "▸"
MAIN_HEADER = "🧭 Main"
"""What the main orchestrator's posts carry in the private chat, which it shares with the current session:
the operator always sees which of the two is speaking, and a reply to one of these reaches it."""
POSTS_REMEMBERED = 2000
"""Messages of the shared channels (General, the private chat) remembered with the session that posted
them, so a reply to one reaches that session. Older ones fall back to the header they carry."""
"""Marks the line that names a session speaking in the private chat out of its turn."""
SESSION_LIST_LIMIT = 30
"""How many sessions /sessions prints — and therefore the largest number /use may be given:
a number the operator was never shown resolves to a session they did not mean."""
TOPIC_RENAME_DEBOUNCE_SECONDS = 2.0
TOPIC_RENAME_MAX_WAIT_SECONDS = 60.0
STALE_NOTICE_DELAY_SECONDS = 3.0
"""Stale messages delivered in a burst after a restart are answered with one notice per chat."""

assert set(RUN_REACTIONS.values()) <= FREE_REACTIONS, "a run reaction is not one Telegram lets bots use"


class TelegramBusy(RuntimeError):
    """Telegram asked for a pause; ``retry_after`` seconds. The session (if any) was created without its topic."""

    def __init__(self, retry_after: int, session_id: str = "") -> None:
        super().__init__(f"Telegram asks to wait {retry_after}s")
        self.retry_after = retry_after
        self.session_id = session_id


class TelegramRefused(RuntimeError):
    """Telegram rejected the call outright."""

    def __init__(self, reason: str, session_id: str = "") -> None:
        super().__init__(reason)
        self.session_id = session_id


def is_subagent(metadata: dict[str, Any] | None) -> bool:
    """Whether a session is a subagent, which never speaks in Telegram, whatever its leader does.

    A subagent's work reaches the operator through its leader, which reads the result and decides
    what to say. Copying the leader's ``telegram_detached`` at spawn time was not enough: a leader
    muted or detached after the spawn left its workers in the private chat, and the worker of a
    Telegram-bound leader spoke there in its own right, or opened a topic of its own in a group.
    So the question is asked of the session itself, at every delivery, not inherited once.
    """
    return bool((metadata or {}).get("subagent_of"))


def is_dispatcher(metadata: dict[str, Any] | None) -> bool:
    """Whether a session is the main orchestrator. It never gets a topic of its own: in a forum General is
    its home, and in the private chat it speaks under :data:`MAIN_HEADER` beside the current session."""
    return bool((metadata or {}).get("dispatcher"))


def is_quiet(metadata: dict[str, Any] | None) -> bool:
    """Whether a session is heard in its topic but never speaks there itself: a project's orchestrator.

    Its topic is the project's, and what appears there is chosen — its reports, its notifications and
    the questions it puts to the operator, posted by the project topic poster — not the stream of its
    turns, which stay in the app. The operator's messages in the topic still reach it through the topic
    binding. A retired orchestrator keeps the mark, so a sweep never opens a topic of its own for it.
    """
    metadata = metadata or {}
    return bool(metadata.get("telegram_quiet") or metadata.get("orchestrator_of") or metadata.get("orchestrator_retired_of"))


def _telegram_media(source: Path | str) -> FSInputFile | str:
    """Telegram fetches a link itself. A workspace file is still uploaded."""
    return source if isinstance(source, str) else FSInputFile(source)


def _delivery_source(item: dict[str, Any]) -> Path | str:
    url = str(item.get("url") or "")
    return url if url else Path(str(item.get("path") or ""))


class TelegramOutbox(Outbox):
    """One chat (or topic) as seen by the renderer.

    Text goes out as a rich message (Telegram renders Markdown natively: tables, headings,
    code, quotes, collapsible blocks). When the server refuses rich content the same text
    falls back to HTML entities and finally to plain text.

    ``header`` is asked, at every send, for the line that names the session this outbox belongs
    to. It returns text only where the chat carries more than one session at once — the private
    chat holding a session other than the current one — so the operator always knows who is
    talking; in a topic, where the topic itself is the name, it stays empty.
    """

    def __init__(self, bot: Bot, chat_id: int, thread_id: int | None, *, header: Callable[[], str] | None = None, on_sent: Callable[[int, int], None] | None = None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id or None
        self.header = header
        self.on_sent = on_sent
        """``(chat_id, message_id)`` for each text message sent: a channel several sessions share
        remembers who said what, so a reply goes back to the one that said it."""

    def _sent(self, message_id: int) -> int:
        if self.on_sent is not None:
            self.on_sent(self.chat_id, message_id)
        return message_id

    def attributed(self, text: str) -> str:
        """``text`` with the session's name above it, for plain and Markdown messages.

        The blank line is what keeps Markdown from folding the name into the first paragraph.
        """
        head = self.header() if self.header is not None else ""
        return f"{head}\n\n{text}" if head else text

    def attributed_html(self, text: str) -> str:
        """The same line for rich HTML, where the title has to be escaped."""
        head = self.header() if self.header is not None else ""
        return f"<p>{html.escape(head, quote=False)}</p>{text}" if head else text

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        text = self.attributed(text)
        if markdown:
            try:
                msg = await tg_call(
                    self.bot.send_rich_message,
                    self.chat_id,
                    InputRichMessage(markdown=text),
                    message_thread_id=self.thread_id,
                    flood_chat=self.chat_id,
                )
                return self._sent(msg.message_id)
            except TelegramBadRequest as exc:
                logger.warning("rich markdown refused (%s); falling back to HTML", exc)
            try:
                msg = await tg_call(
                    self.bot.send_message,
                    self.chat_id,
                    markdown_to_html(text),
                    message_thread_id=self.thread_id,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    flood_chat=self.chat_id,
                )
                return self._sent(msg.message_id)
            except TelegramBadRequest:
                pass
        msg = await tg_call(self.bot.send_message, self.chat_id, text, message_thread_id=self.thread_id, parse_mode=None, flood_chat=self.chat_id)
        return self._sent(msg.message_id)

    async def send_html(self, html: str) -> int:
        html = self.attributed_html(html)
        try:
            msg = await tg_call(
                self.bot.send_rich_message, self.chat_id, InputRichMessage(html=html), message_thread_id=self.thread_id, flood_chat=self.chat_id
            )
            return self._sent(msg.message_id)
        except TelegramBadRequest as exc:
            logger.warning("rich html refused (%s); sending plain text", exc)
        msg = await tg_call(self.bot.send_message, self.chat_id, strip_tags(html), message_thread_id=self.thread_id, parse_mode=None, flood_chat=self.chat_id)
        return self._sent(msg.message_id)

    async def react(self, message_id: int, emoji: str | None) -> None:
        """Set (or clear, with ``None``) the bot's reaction on a message; never raises."""
        if emoji is not None and emoji not in FREE_REACTIONS:
            return
        try:
            await self.bot.set_message_reaction(
                self.chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else []
            )
        except TelegramRetryAfter as exc:
            note_flood(self.chat_id, float(exc.retry_after))
        except Exception:  # noqa: BLE001 — a reaction is decoration
            logger.debug("reaction failed", exc_info=True)

    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None:
        text = self.attributed_html(text) if html else self.attributed(text)
        if flooded(self.chat_id):
            return  # Telegram asked for a pause; a status edit is skipped, the next one carries the newer state
        try:
            if html:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id, message_id=message_id, rich_message=InputRichMessage(html=text)
                    )
                    return
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc):
                        return
                    text = strip_tags(text)
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=message_id, parse_mode=None)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        except TelegramRetryAfter as exc:
            note_flood(self.chat_id, float(exc.retry_after))

    async def send_document(self, path: Path, caption: str | None = None) -> int:
        caption = self.attributed(caption or "").strip() or None
        msg = await tg_call(
            self.bot.send_document,
            self.chat_id,
            FSInputFile(path),
            caption=(caption or "")[:1000] or None,
            message_thread_id=self.thread_id,
            parse_mode=None,
        )
        return msg.message_id

    async def send_photo(self, path: Path | str, caption: str | None = None) -> int:
        caption = self.attributed(caption or "").strip() or None
        msg = await tg_call(
            self.bot.send_photo,
            self.chat_id,
            _telegram_media(path),
            caption=(caption or "")[:1000] or None,
            message_thread_id=self.thread_id,
            parse_mode=None,
        )
        return msg.message_id

    async def send_video(self, path: Path | str, caption: str | None = None) -> int:
        msg = await tg_call(self.bot.send_video, self.chat_id, _telegram_media(path), caption=(self.attributed(caption or "").strip() or None), message_thread_id=self.thread_id, parse_mode=None)
        return msg.message_id

    async def send_audio(self, path: Path | str, caption: str | None = None) -> int:
        msg = await tg_call(self.bot.send_audio, self.chat_id, _telegram_media(path), caption=(self.attributed(caption or "").strip() or None), message_thread_id=self.thread_id, parse_mode=None)
        return msg.message_id

    async def send_animation(self, path: Path | str, caption: str | None = None) -> int:
        msg = await tg_call(self.bot.send_animation, self.chat_id, _telegram_media(path), caption=(self.attributed(caption or "").strip() or None), message_thread_id=self.thread_id, parse_mode=None)
        return msg.message_id

    async def send_album(self, paths: list[Path | str], caption: str | None = None) -> list[int]:
        head = self.attributed(caption or "").strip() or None
        media = [InputMediaPhoto(media=_telegram_media(path), caption=head[:1000] if index == 0 and head else None) for index, path in enumerate(paths)]
        messages = await tg_call(self.bot.send_media_group, self.chat_id, media=media, message_thread_id=self.thread_id)
        return [message.message_id for message in messages]

    async def delete(self, message_id: int) -> None:
        try:
            await self.bot.delete_message(self.chat_id, message_id)
        except TelegramBadRequest:
            pass

    async def send_draft(self, draft_id: int, text: str) -> bool:
        """Live draft (Bot API 9.5+). Telegram allows it in private chats only.

        A draft carries no header, so a session that would need one streams nothing: its answer
        arrives as a message with its name on it rather than as unattributed text being typed.
        """
        if self.chat_id < 0 or (self.header is not None and self.header()):
            return False
        try:
            if text:
                try:
                    await self.bot.send_rich_message_draft(self.chat_id, draft_id, rich_message=InputRichMessage(markdown=text), message_thread_id=self.thread_id, can_stop=True)
                except TelegramBadRequest:
                    await self.bot.send_message_draft(self.chat_id, draft_id, message_thread_id=self.thread_id, text=text, parse_mode=None, can_stop=True)
            else:
                await self.bot.send_message_draft(self.chat_id, draft_id, message_thread_id=self.thread_id, text="", parse_mode=None)
            return True
        except TelegramRetryAfter:
            return True  # skip this frame; the next one carries more text
        except TelegramBadRequest as exc:
            logger.warning("drafts unavailable in chat %s: %s", self.chat_id, exc)
            return False


APPROVAL_KEY_RE = re.compile(r"Approval key: ([0-9a-f]{12})")
"""How a policy refusal names its key in the tool result; the button under it grants exactly that call."""


class TelegramFront:
    """Wires a :class:`SessionManager` to Telegram."""

    def __init__(
        self,
        settings: Settings,
        config: RuntimeConfig,
        manager: SessionManager,
        *,
        save_config: Callable[[RuntimeConfig], Awaitable[None]],
        speech: LocalSpeech | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.manager = manager
        self.username = ""
        """The bot's own name, once polling starts: what a link that opens the Mini App names."""
        self.save_config = save_config
        self.speech = speech if speech is not None else LocalSpeech(settings.state_dir, config)
        """Local speech recognition, which answers before any endpoint is asked. Handed in by the
        application so the front and the API share one models directory and one loaded model; built
        here only for a front constructed on its own, as the tests do."""
        api = TelegramAPIServer.from_base(settings.telegram_api_base, is_local=settings.telegram_local_mode)
        self.bot = Bot(
            settings.telegram_bot_token,
            session=AiohttpSession(api=api),
            default=DefaultBotProperties(parse_mode=None),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        self._renderers: dict[str, RunRenderer] = {}
        self._buffers: dict[tuple[int, int, str], InboundBuffer] = {}
        """One merge buffer per (chat, thread, session): in the private chat two sessions share a
        chat, and their messages must not be merged into one submission."""
        self._question_state: dict[str, dict[str, Any]] = {}
        self._answered_task: asyncio.Task[None] | None = None
        """Listens for questions answered on another front, whose keyboards here must go."""
        self._current_session: str | None = None
        """The private chat's session, cached from kv; None until it is read the first time."""
        self._force_reply_targets: dict[int, str] = {}
        """Message id of a "type your answer" prompt → the session that asked, so a reply in a
        chat shared by several sessions is attributed to the asker and not to the current one."""
        self._compact_focus: dict[str, str] = {}
        self._last_operator_message: dict[str, tuple[int, int]] = {}
        """Per session: the chat and id of the operator's latest message, for the outcome reaction."""
        self._topic_status: dict[str, str] = {}
        self._topic_status_tasks: dict[str, asyncio.Task[None]] = {}
        self._stale_counts: dict[tuple[int, int], int] = {}
        self._voice_pending: dict[str, tuple[str, str, Attachment, int, int, float]] = {}
        """Transcripts awaiting the operator's ✓/✗, by token; entries expire (VOICE_PENDING_TTL_SECONDS) and the dict is capped."""
        self.ledger = DeliveryLedger(manager.db, max_attempts=config.ops.delivery_max_attempts, max_age_hours=config.ops.delivery_max_age_hours, keep_days=config.ops.delivery_keep_days)
        self._stale_notices: dict[tuple[int, int], asyncio.Task[None]] = {}
        self.operator_hooks: dict[str, Callable[..., Awaitable[str]]] = {}
        """rebuild / rollback / panic, installed by the application."""
        self.command_hooks: dict[str, Callable[[Message, CommandObject], Awaitable[None]]] = {}
        self.callback_hooks: dict[str, Callable[[CallbackQuery, list[str]], Awaitable[None]]] = {}
        self.message_interceptors: list[Callable[[Message], Awaitable[bool]]] = []
        """Return True to consume a message before it reaches a session (e.g. a rejection reason)."""
        self._posts: dict[tuple[int, int], str] = {}
        """(chat, message) → the session that posted it, in the channels several sessions share."""
        self._register_handlers()
        manager.add_sink(self._on_event)
        manager.on_finished(self._on_finished)
        manager.on_pending_restored(self._on_pending_restored)
        manager.compaction_hooks.append(self._on_auto_compaction)
        manager.service_hooks.update(
            {
                "send_file": self._service_send_file,
                "spawn_agent": self._service_spawn_agent,
                "progress": self._service_progress,
            }
        )

    # -- lifecycle ------------------------------------------------------------------

    def listen(self) -> None:
        """Subscribe to what the bus says about questions answered on another front."""
        if self._answered_task is None:
            self._answered_task = self.manager.bus.on(EventFilter(types=("ask.answered",)), self._on_answered_elsewhere, name="telegram-questions")

    async def stop_listening(self) -> None:
        task, self._answered_task = self._answered_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def start(self) -> None:
        self.listen()  # before polling, which does not return until the front stops
        me = await self.bot.get_me()
        self.username = str(me.username or "")
        logger.warning("telegram: polling as @%s", me.username)
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def stop(self) -> None:
        await self.stop_listening()
        for task in list(self._topic_status_tasks.values()) + list(self._stale_notices.values()):
            task.cancel()
        for renderer in self._renderers.values():
            renderer.close()
        await self.dp.stop_polling()
        await self.bot.session.close()

    # -- helpers --------------------------------------------------------------------

    def _is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.settings.owner_user_id

    def private_mode(self) -> bool:
        """True when every session lives in the operator's private chat rather than in its own topic."""
        return self.config.telegram.session_mode() == "private"

    async def current_session_id(self) -> str:
        """The session the private chat is a window onto; remembered in kv across restarts."""
        if self._current_session is None:
            self._current_session = str(await self.manager.db.kv_get(CURRENT_SESSION_KEY, "") or "")
        return self._current_session

    async def set_current_session(self, session_id: str) -> None:
        self._current_session = session_id
        await self.manager.db.kv_set(CURRENT_SESSION_KEY, session_id)

    async def current_state(self) -> SessionState | None:
        """The session the private chat talks to; the first message creates it."""
        session_id = await self.current_session_id()
        state = await self.manager.get_state(session_id) if session_id else None
        if state is None:
            binding = await self.binding_for_topic(self.settings.owner_user_id, 0)
            # The chat's single session from before it held several: keep talking to that one.
            state = await self.manager.get_state(binding.session_id) if binding is not None else None
        if state is None:
            state = await self.manager.create_session("direct")
        await self.set_current_session(state.session.id)
        return state

    def _general_outbox(self, *, header: Callable[[], str] | None = None) -> TelegramOutbox | None:
        """The operator channel: the General topic of the bound forum, or the private chat.

        ``header`` names the session speaking, for the sessions that share this one channel
        because they have no topic of their own.
        """
        if self.private_mode():
            return TelegramOutbox(self.bot, self.settings.owner_user_id, None, header=header) if self.settings.owner_user_id else None
        chat_id = self.config.telegram.forum_chat_id or self.settings.owner_user_id
        if not chat_id:
            return None
        return TelegramOutbox(self.bot, chat_id, self.config.telegram.general_topic_id or None, header=header)

    async def notify(self, text: str, *, markdown: bool = True) -> None:
        """Post to the operator channel (General topic or the private chat)."""
        outbox = self._general_outbox()
        if outbox is None:
            return
        for chunk in split_message(text):
            await outbox.send_text(chunk, markdown=markdown)

    async def send_choice(self, outbox: TelegramOutbox, text: str, rows: list[list[tuple[str, str]]]) -> int:
        """Send a message with inline buttons ``[(label, callback_data), …]`` per row; returns the message id.

        Extensions build their approval keyboards through this so only the transport speaks aiogram.
        """
        for row in rows:
            for _label, data in row:
                if len(data.encode("utf-8")) > 64:
                    raise ValueError(f"callback_data over Telegram's 64-byte limit: {data!r}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label[:60], callback_data=data) for label, data in row] for row in rows])
        msg = await tg_call(self.bot.send_message, outbox.chat_id, outbox.attributed(text), message_thread_id=outbox.thread_id, reply_markup=keyboard, flood_chat=outbox.chat_id)
        return int(msg.message_id)

    async def send_force_reply(self, chat_id: int, thread_id: int | None, text: str) -> int:
        """Ask for a one-message free-text reply (the client opens the reply box); returns its message id."""
        msg = await tg_call(self.bot.send_message, chat_id, text, message_thread_id=thread_id, reply_markup=ForceReply(selective=True), flood_chat=chat_id)
        return int(msg.message_id)

    async def binding_for_session(self, session_id: str) -> TopicBinding | None:
        row = await self.manager.db.fetchone("SELECT * FROM topics WHERE session_id = ? AND closed_at IS NULL", (session_id,))
        return TopicBinding(row["chat_id"], row["thread_id"], row["session_id"], row["title"]) if row else None

    async def binding_for_topic(self, chat_id: int, thread_id: int) -> TopicBinding | None:
        row = await self.manager.db.fetchone(
            "SELECT * FROM topics WHERE chat_id = ? AND thread_id = ? AND closed_at IS NULL", (chat_id, thread_id)
        )
        return TopicBinding(row["chat_id"], row["thread_id"], row["session_id"], row["title"]) if row else None

    async def bind_topic(self, chat_id: int, thread_id: int, session_id: str, title: str) -> TopicBinding:
        await self.manager.db.execute(
            "INSERT OR REPLACE INTO topics(chat_id, thread_id, session_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, session_id, title, datetime.now(UTC).isoformat()),
        )
        state = await self.manager.get_state(session_id)
        if state is not None and state.session.metadata.pop("telegram_detached", None) is not None:
            state.metadata.pop("telegram_detached", None)
            await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        return TopicBinding(chat_id, thread_id, session_id, title)

    # -- a project's topic --------------------------------------------------------------------

    async def open_quiet_topic(self, session_id: str, title: str) -> TopicBinding | None:
        """A topic for a session that listens there and does not stream into it: a project's orchestrator.

        The mark goes on before the topic is bound, because binding lifts ``telegram_detached`` and a
        turn running at that moment would otherwise start streaming into the new topic. Returns None
        without a forum (private mode, or no group bound); raises :class:`TelegramBusy` or
        :class:`TelegramRefused` as :meth:`ensure_topic` does.
        """
        state = await self.manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if not state.metadata.get("telegram_quiet"):
            state.metadata["telegram_quiet"] = True
            state.session.metadata["telegram_quiet"] = True
            await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        if self.private_mode() or not self.config.telegram.forum_chat_id:
            return None
        return await self.ensure_topic(session_id, title)

    async def repoint_topic(self, binding: TopicBinding, session_id: str) -> TopicBinding:
        """The same topic, now bound to another session: a replaced orchestrator's successor inherits it,
        so the operator's thread with the project goes on where it was."""
        await self.manager.db.execute("UPDATE topics SET session_id = ? WHERE chat_id = ? AND thread_id = ?", (session_id, binding.chat_id, binding.thread_id))
        return TopicBinding(binding.chat_id, binding.thread_id, session_id, binding.title)

    async def close_topic(self, binding: TopicBinding) -> None:
        """Close a topic and end its binding; the history in Telegram stays."""
        try:
            await self._close_topic(binding)
        except TelegramAPIError as exc:
            logger.warning("could not close topic %s: %s", binding.thread_id, exc)

    async def rename_topic(self, binding: TopicBinding, title: str) -> None:
        """Rename a topic and remember the name; a refusal is logged, the binding stays as it was."""
        try:
            await tg_call(self.bot.edit_forum_topic, binding.chat_id, binding.thread_id, name=title[:128], attempts=2, flood_chat=binding.chat_id)
        except TelegramAPIError as exc:
            logger.warning("could not rename topic %s: %s", binding.thread_id, exc)
            return
        await self.manager.db.execute("UPDATE topics SET title = ? WHERE chat_id = ? AND thread_id = ?", (title, binding.chat_id, binding.thread_id))

    def outbox(self, chat_id: int, thread_id: int | None, *, header: Callable[[], str] | None = None) -> TelegramOutbox:
        """An outbox for a chat and thread chosen by the caller (a project's topic, or the private chat)."""
        return TelegramOutbox(self.bot, chat_id, thread_id, header=header)

    async def post(self, outbox: TelegramOutbox, text: str, rows: list[list[tuple[str, str]]] | None = None) -> int:
        """Plain text, with inline buttons when ``rows`` has any; returns the message id."""
        if rows:
            return await self.send_choice(outbox, text, rows)
        msg = await tg_call(self.bot.send_message, outbox.chat_id, outbox.attributed(text), message_thread_id=outbox.thread_id, parse_mode=None, flood_chat=outbox.chat_id)
        return int(msg.message_id)

    async def edit_post(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a posted message's text and take its buttons away; a message already gone is not an error."""
        try:
            await tg_call(self.bot.edit_message_text, text, chat_id=chat_id, message_id=message_id, reply_markup=None, attempts=2, flood_chat=chat_id)
        except TelegramAPIError as exc:
            logger.debug("could not edit message %s: %s", message_id, exc)

    async def detach_session(self, session_id: str) -> bool:
        """Make a topic-bound session web-only without removing its history or workspace."""
        state = await self.manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        binding = await self.binding_for_session(session_id)
        if binding is None:
            return False
        if binding.thread_id:
            try:
                await tg_call(self.bot.close_forum_topic, binding.chat_id, binding.thread_id, flood_chat=binding.chat_id)
            except TelegramAPIError as exc:
                raise TelegramRefused(str(exc), session_id) from exc
        state.session.metadata["telegram_detached"] = True
        state.metadata["telegram_detached"] = True
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        task = self._topic_status_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._topic_status.pop(session_id, None)
        return True

    def remember_post(self, session_id: str) -> Callable[[int, int], None]:
        """The ``on_sent`` of an outbox on a shared channel: each message it sends is remembered as this session's."""

        def sent(chat_id: int, message_id: int) -> None:
            self._posts[(chat_id, message_id)] = session_id
            while len(self._posts) > POSTS_REMEMBERED:
                self._posts.pop(next(iter(self._posts)))

        return sent

    async def dispatcher_session(self) -> str:
        """The main orchestrator's session, when there is one (read from where its extension keeps it)."""
        return str(await self.manager.db.kv_get("dispatcher.session", "") or "")

    def _main_outbox(self, session_id: str) -> TelegramOutbox | None:
        """The main orchestrator's channel: General in a forum, the private chat (under its header) otherwise."""
        if not self.private_mode() and self.config.telegram.forum_chat_id:
            return TelegramOutbox(self.bot, self.config.telegram.forum_chat_id, self.config.telegram.general_topic_id or None, on_sent=self.remember_post(session_id))
        if not self.settings.owner_user_id:
            return None
        return TelegramOutbox(self.bot, self.settings.owner_user_id, None, header=lambda: MAIN_HEADER, on_sent=self.remember_post(session_id))

    async def outbox_for_session(self, session_id: str) -> TelegramOutbox | None:
        """Where this session's output goes: the one place that tells the two modes apart."""
        state = await self.manager.get_state(session_id)
        # Detachment is a per-session delivery veto, not merely the absence of a topic.
        # It must win over the global private-chat mode or background output leaks into DMs.
        if state is not None and (state.metadata.get("telegram_detached") or is_subagent(state.metadata) or is_quiet(state.metadata)):
            return None
        if state is not None and is_dispatcher(state.metadata):
            return self._main_outbox(session_id)
        binding = await self.binding_for_session(session_id)
        if binding is not None:
            return TelegramOutbox(self.bot, binding.chat_id, binding.thread_id)
        # A mode change governs sessions without a destination. Existing topic bindings remain
        # authoritative; otherwise changing one session must not reroute every other session to DM.
        if self.private_mode():
            return await self._private_outbox(session_id)
        title = state.session.title if state is not None else session_id
        try:
            binding = await self.ensure_topic(session_id, title)
        except (TelegramBusy, TelegramRefused) as exc:
            # A session that outlived private mode, or one Telegram would not open a topic for yet.
            logger.warning("session %s has no topic: %s", session_id, exc)
        if binding is not None:
            return TelegramOutbox(self.bot, binding.chat_id, binding.thread_id)
        general = self._general_outbox(header=lambda: f"{SESSION_HEADER} {title}")
        if general is not None:
            # It shares General with the main orchestrator; a reply to it must reach it, not the main one.
            general.on_sent = self.remember_post(session_id)
        return general

    async def _private_outbox(self, session_id: str) -> TelegramOutbox | None:
        """The private chat, naming the session whenever it is not the one the operator is writing to."""
        if not self.settings.owner_user_id:
            return None
        await self.current_session_id()  # warm the cache: the header below is read on a send, which cannot await

        def header() -> str:
            if session_id == self._current_session:
                return ""
            state = self.manager.live_state(session_id)
            return f"{SESSION_HEADER} {state.session.title if state is not None else session_id}"

        return TelegramOutbox(self.bot, self.settings.owner_user_id, None, header=header, on_sent=self.remember_post(session_id))

    async def ensure_topic(self, session_id: str, title: str, *, chat_id: int | None = None) -> TopicBinding | None:
        """The session's own topic in the bound forum, opened now if it has none.

        Returns None when there is no forum to open one in. Raises :class:`TelegramBusy` when
        Telegram asks for a pause, so the caller can leave the session without a topic for now
        rather than lose it: the session exists either way, and gets its topic when it next speaks.
        """
        binding = await self.binding_for_session(session_id)
        if binding is not None:
            return binding
        forum = chat_id or self.config.telegram.forum_chat_id
        if not forum:
            return None
        state = await self.manager.get_state(session_id)
        if state is not None and is_subagent(state.metadata):
            return None  # a subagent speaks through its leader, so a topic of its own would only ever be empty
        if state is not None and is_dispatcher(state.metadata):
            return None  # General is the main orchestrator's; a topic of its own would split its one conversation
        try:
            topic = await tg_call(self.bot.create_forum_topic, forum, title[:128], attempts=2, flood_chat=forum)
        except TelegramRetryAfter as exc:
            raise TelegramBusy(int(exc.retry_after), session_id) from exc
        except TelegramAPIError as exc:
            raise TelegramRefused(str(exc), session_id) from exc
        return await self.bind_topic(forum, topic.message_thread_id, session_id, title)

    async def adopt_sessions_into_topics(self) -> int:
        """Give every session without a binding a topic of its own; returns how many were opened.

        Sessions born in the private chat carry no topic. After a switch to topics they would
        speak in General with nothing naming them, which is the confusion topics exist to end.
        Telegram rate-limits topic creation, so a pause stops the sweep instead of failing it —
        whatever is left over is opened by :meth:`outbox_for_session` on the session's next output.
        """
        opened = 0
        for session in await self.manager.list_sessions():
            # A session started on the site has no topic on purpose. Binding the group must
            # not pull it into the forum: that is the leak the flag exists to stop.
            if (session.get("metadata") or {}).get("telegram_detached") or is_subagent(session.get("metadata")) or is_quiet(session.get("metadata")) or is_dispatcher(session.get("metadata")):
                continue
            if await self.binding_for_session(session["id"]) is not None:
                continue
            try:
                if await self.ensure_topic(session["id"], session["title"]) is not None:
                    opened += 1
            except TelegramBusy as exc:
                logger.warning("topic sweep stopped after %s: %s", opened, exc)
                break
            except TelegramRefused as exc:
                logger.warning("no topic for session %s: %s", session["id"], exc)
        return opened

    async def forget_session(self, session_id: str) -> None:
        """Drop what the chat holds for a session that is about to be deleted.

        Called before the deletion, while the binding row is still there: afterwards there is
        nothing left to say which topic was the session's.
        """
        binding = await self.binding_for_session(session_id)
        if binding is not None and binding.thread_id:
            try:
                await self.bot.delete_forum_topic(binding.chat_id, binding.thread_id)
            except TelegramAPIError as exc:
                logger.warning("could not delete the topic of session %s: %s", session_id, exc)
        await self._release_current(session_id)

    async def _keep_off_telegram(self, session_id: str) -> None:
        """A session whose topic Telegram would not open must not fall through into the private chat."""
        state = await self.manager.get_state(session_id)
        if state is None:
            return
        state.metadata["telegram_detached"] = True
        state.session.metadata["telegram_detached"] = True
        await self.manager.sessions.update_metadata(session_id, state.session.metadata)

    async def create_session_topic(
        self, title: str, *, metadata: dict[str, Any] | None = None, chat_id: int | None = None, topic: bool = True, project_id: str | None = None, own_directory: bool = False, force_topic: bool = False
    ) -> tuple[SessionState, TopicBinding]:
        """Create a session and, in topics mode, its topic.

        In private mode there is no topic and nothing to bind: the session is reachable from
        /sessions, /use and the Mini App, and speaks in the private chat under its own name.
        ``force_topic`` is the exception a command asks for: the private chat stays a window,
        and the new session speaks in a thread of the bound group.
        """
        state = await self.manager.create_session(title, metadata=metadata, project_id=project_id, own_directory=own_directory)
        forum_id = chat_id or self.config.telegram.forum_chat_id
        # Neither a topic nor the private chat's binding: binding a subagent to the private chat
        # would make it the session the operator's next message goes to.
        if is_subagent(metadata) or is_dispatcher(metadata) or (self.private_mode() and not (force_topic and forum_id)):
            return state, TopicBinding(self.settings.owner_user_id, 0, state.session.id, title)
        forum = forum_id if (topic or force_topic) else 0
        binding = await self.ensure_topic(state.session.id, title, chat_id=forum) if forum else None
        if binding is None:
            return state, await self.bind_topic(self.settings.owner_user_id, 0, state.session.id, title)
        try:
            await tg_call(
                self.bot.send_message,
                forum,
                f"Session {state.session.id} — {title}\nproject directory: {state.workspace}",
                message_thread_id=binding.thread_id,
                flood_chat=forum,
            )
        except Exception:  # noqa: BLE001 — the banner is cosmetic
            logger.warning("could not post the session banner", exc_info=True)
        return state, binding

    async def _replied_session(self, message: Message) -> SessionState | None:
        """The session whose post this message replies to, in a channel several sessions share.

        Asked before anything else: a reply in General to a session that fell back there is for that
        session, not for the main orchestrator General belongs to, and a reply in the private chat to
        the main orchestrator's post is for it, not for the current session.
        """
        reply = message.reply_to_message
        if reply is None or reply.forum_topic_created is not None or not (reply.from_user and reply.from_user.is_bot):
            return None
        session_id = self._posts.get((message.chat.id, reply.message_id))
        if session_id is None and (reply.text or reply.caption or "").startswith(MAIN_HEADER):
            # Remembered posts do not survive a restart; the main orchestrator's header does.
            session_id = await self.dispatcher_session() or None
        if not session_id:
            return None
        state = await self.manager.get_state(session_id)
        if state is None or is_subagent(state.metadata) or state.metadata.get("telegram_detached"):
            return None
        return state

    async def _session_for_message(self, message: Message) -> SessionState | None:
        chat_id = message.chat.id
        thread_id = message.message_thread_id or 0
        replied = await self._replied_session(message)
        if replied is not None:
            return replied
        if message.chat.type != "private" and chat_id == self.config.telegram.forum_chat_id and thread_id in (0, self.config.telegram.general_topic_id or 0):
            # Plain text in General is the main orchestrator's. Nothing else listened there before.
            main = await self.dispatcher_session()
            state = await self.manager.get_state(main) if main else None
            if state is not None and is_dispatcher(state.metadata) and not self.private_mode():
                return state
        if message.chat.type == "private":
            thread_id = 0
            if self.private_mode():
                return await self.current_state()
        binding = await self.binding_for_topic(chat_id, thread_id)
        if binding is not None:
            return await self.manager.get_state(binding.session_id)
        if message.chat.type == "private":
            state, _ = await self.create_session_topic("direct", topic=False)
            return state
        if message.is_topic_message and thread_id:
            # A topic the operator created by hand: adopt it as a new session.
            title = None
            if message.reply_to_message and message.reply_to_message.forum_topic_created:
                title = message.reply_to_message.forum_topic_created.name
            title = title or f"topic {thread_id}"
            state = await self.manager.create_session(title)
            await self.bind_topic(chat_id, thread_id, state.session.id, title)
            return state
        return None

    def _is_general(self, message: Message) -> bool:
        if message.chat.type == "private":
            return True
        return not message.is_topic_message or (message.message_thread_id or 0) == 0

    # -- handlers -------------------------------------------------------------------

    def _register_handlers(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, Command("start", "help"))
        r.message.register(self.cmd_new, Command("new"))
        r.message.register(self.cmd_use, Command("use"))
        r.message.register(self.cmd_main, Command("main"))
        r.message.register(self.cmd_stop, Command("stop"))
        r.message.register(self.cmd_close, Command("close"))
        r.message.register(self.cmd_rename, Command("rename"))
        r.message.register(self.cmd_compact, Command("compact"))
        r.message.register(self.cmd_clear, Command("clear"))
        r.message.register(self.cmd_prompt, Command("prompt"))
        r.message.register(self.cmd_delete, Command("delete"))
        r.message.register(self.cmd_cleanup, Command("cleanup"))
        r.message.register(self.on_topic_closed, F.forum_topic_closed)
        r.message.register(self.cmd_sessions, Command("sessions"))
        r.message.register(self.cmd_model, Command("model"))
        r.message.register(self.cmd_thinking, Command("thinking"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_usage, Command("usage"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_bind, Command("bind"))
        r.message.register(self.cmd_detach, Command("detach"))
        r.message.register(self.cmd_operator, Command("rebuild", "rollback", "panic", "schedules", "verbosity", "approval", "balance", "schedule", "inbox", "heartbeat", "doctor", "intents", "board", "peer", "allow"))
        r.message.register(self.cmd_mode, Command("mode"))
        r.message.register(self.cmd_yagni, Command("yagni"))
        r.message.register(self.cmd_secret, Command("secret"))
        r.message.register(
            self.on_message,
            F.text | F.caption | F.document | F.photo | F.audio | F.video | F.voice | F.video_note | F.animation | F.sticker | F.location | F.contact | F.poll,
        )
        r.message.register(self.on_unsupported)
        r.callback_query.register(self.on_callback)
        r.stopped_message_generation.register(self.on_generation_stopped)

    async def cmd_start(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if self.private_mode() and self.config.telegram.forum_chat_id:
            help_text = HELP_PRIVATE_FORUM
        elif self.private_mode():
            help_text = HELP_PRIVATE
        else:
            help_text = HELP_TOPICS
        await message.answer(help_text + HELP_TAIL, parse_mode=ParseMode.HTML)

    async def cmd_bind(self, message: Message) -> None:
        """Bind this forum supergroup as the session hub."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if message.chat.type not in ("supergroup", "group") or not message.chat.is_forum:
            await message.answer("Run /bind inside a supergroup with topics enabled.")
            return
        self.config.telegram.forum_chat_id = message.chat.id
        self.config.telegram.general_topic_id = 0
        self.config.telegram.mode = "topics"
        await self.save_config(self.config)
        opened = await self.adopt_sessions_into_topics()
        await message.answer(
            "Bound. Create sessions with /new <title>; each topic is a session. "
            + (f"The {opened} session(s) that were already open have topics of their own now. " if opened else "")
            + "Mini App → Settings → Chat puts them back in the private chat if you prefer it."
        )

    async def cmd_detach(self, message: Message) -> None:
        """Leave this session available on the site while closing its Telegram topic."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if self.private_mode() or self._is_general(message):
            await message.answer("Use /detach inside the session topic you want to disconnect.")
            return
        binding = await self.binding_for_topic(message.chat.id, message.message_thread_id or 0)
        if binding is None:
            await message.answer("This topic is not connected to a session.")
            return
        # Telegram cannot acknowledge into a topic after it has been closed, so announce
        # the transition without claiming success before the API call completes.
        await message.answer("Disconnecting this topic. The agent and its history stay available in the Mini App.")
        await self.detach_session(binding.session_id)

    async def cmd_new(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        title = (command.args or "").strip() or datetime.now(UTC).strftime("session %m-%d %H:%M")
        # A bound group is where a command-made session speaks, even while this chat is only a
        # window. Leaving it in the private chat is how a session the operator did not ask to
        # see there used to arrive as a message from the bot.
        if self.private_mode() and self.config.telegram.forum_chat_id:
            try:
                state, _binding = await self.create_session_topic(title, chat_id=self.config.telegram.forum_chat_id, force_topic=True)
            except (TelegramBusy, TelegramRefused) as exc:
                if exc.session_id:
                    await self._keep_off_telegram(exc.session_id)
                await message.answer(f"Telegram did not open a topic: {exc}")
                return
            await message.answer(f"Created topic '{title}' (session {state.session.id}).")
            return
        if self.private_mode():
            state, _ = await self.create_session_topic(title)
            await self.set_current_session(state.session.id)
            await message.answer(f"New session '{title}' ({state.session.id}). You are writing to it; /sessions lists the others.")
            return
        if message.chat.type == "private" and not self.config.telegram.forum_chat_id:
            await message.answer(
                "Topics need a supergroup with topics enabled: add me there as an admin "
                "(with 'manage topics') and send /bind in it. In this private chat there is "
                "one session; just write to it."
            )
            return
        forum = self.config.telegram.forum_chat_id if message.chat.type == "private" else message.chat.id
        state, binding = await self.create_session_topic(title, chat_id=forum)
        if message.chat.type == "private":
            await message.answer(f"Created topic '{title}' (session {state.session.id}).")

    async def cmd_use(self, message: Message, command: CommandObject) -> None:
        """Point the private chat at another session: by its number in /sessions, its title, or its id."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if not self.private_mode():
            await message.answer("/use belongs to the private chat; in a bound group, write in the session's own topic.")
            return
        arg = (command.args or "").strip()
        if not arg:
            await message.answer("usage: /use <number from /sessions | title | session id>")
            return
        sessions = await self.manager.list_sessions(limit=SESSION_LIST_LIMIT)
        chosen: dict[str, Any] | None = None
        if arg.isdigit() and 1 <= int(arg) <= len(sessions):
            chosen = sessions[int(arg) - 1]
        else:
            lowered = arg.lower()
            chosen = (
                next((s for s in sessions if s["id"] == arg), None)
                or next((s for s in sessions if s["title"].lower().startswith(lowered)), None)
                or next((s for s in sessions if lowered in s["title"].lower()), None)
            )
        if chosen is None:
            await message.answer(f"No session matches {arg!r}; /sessions lists them.")
            return
        if is_subagent(chosen.get("metadata")):
            # Its answers would go nowhere: a subagent never writes here, so the chat would fall silent.
            await message.answer(f"'{chosen['title']}' is a subagent; it answers through the session that started it.")
            return
        await self.set_current_session(str(chosen["id"]))
        await message.answer(f"Writing to '{chosen['title']}' ({chosen['id']}). What the others say still arrives here, under their names.")

    async def cmd_main(self, message: Message) -> None:
        """Talk to the main orchestrator: in the private chat it becomes the current session; in a forum it is General."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        main = await self.dispatcher_session()
        if not main or await self.manager.get_state(main) is None:
            await message.answer("There is no main orchestrator yet; open it once in the app (Main, at the top of the list).")
            return
        if not self.private_mode() and self.config.telegram.forum_chat_id:
            await message.answer("The main orchestrator lives in General: write there, or reply to one of its posts.")
            return
        await self.set_current_session(main)
        await message.answer(f"{MAIN_HEADER}: writing to the main orchestrator. /use switches back to a session.")

    async def cmd_stop(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or not await self.manager.stop(state.session.id):
            await message.answer("Nothing is running here.")
            return
        await message.answer("Stopping…")

    async def cmd_close(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if self.private_mode():
            session_id = await self.current_session_id()
            state = await self.manager.get_state(session_id) if session_id else None
            if state is None:
                await message.answer("No session is open here; /new <title> starts one.")
                return
            await self._ask_close(state.session.id, state.session.title, message.chat.id, None)
            return
        if self._is_general(message):
            await message.answer("Use /close inside a session topic.")
            return
        binding = await self.binding_for_topic(message.chat.id, message.message_thread_id or 0)
        if binding is None:
            return
        await self._ask_close(binding.session_id, binding.title, message.chat.id, message.message_thread_id)

    async def cmd_rename(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        title = (command.args or "").strip()
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /rename inside a session topic (or the private chat).")
            return
        if not title:
            await message.answer(f"Current title: {state.session.title}\nusage: /rename <new title>")
            return
        await self.rename_session(state.session.id, title)
        await message.answer(f"Renamed to: {title}")

    async def rename_session(self, session_id: str, title: str) -> None:
        """Rename the session and, when it has a topic, the topic itself."""
        state = await self.manager.rename_session(session_id, title)
        binding = await self.binding_for_session(session_id)
        if binding is not None and binding.thread_id:
            try:
                await tg_call(self.bot.edit_forum_topic, binding.chat_id, binding.thread_id, name=self._topic_name(session_id, state.session.title), flood_chat=binding.chat_id)
            except TelegramBadRequest as exc:
                logger.warning("could not rename topic: %s", exc)

    # -- ambient status: reactions and topic names ----------------------------------------

    def _topic_name(self, session_id: str, title: str) -> str:
        prefix = TOPIC_STATUS_PREFIX.get(self._topic_status.get(session_id, "")) if self.config.telegram.topic_status_emoji else None
        name = f"{prefix} {title}" if prefix else title
        return name[:128]

    def set_topic_status(self, session_id: str, status: str) -> None:
        """Show the session's state in its topic name (debounced: one rename per burst of changes)."""
        if self.private_mode() or not self.config.telegram.topic_status_emoji or self._topic_status.get(session_id) == status:
            return
        self._topic_status[session_id] = status
        task = self._topic_status_tasks.get(session_id)
        if task is None or task.done():
            self._topic_status_tasks[session_id] = asyncio.create_task(self._apply_topic_status(session_id))

    async def _apply_topic_status(self, session_id: str) -> None:
        """Rename after a short debounce; a flood pause defers the rename rather than dropping it.

        The end of a run is exactly when the chat is busiest (final status edit, the answer,
        the reaction), so the completion rename is the one most likely to hit flood control.
        """
        try:
            await asyncio.sleep(TOPIC_RENAME_DEBOUNCE_SECONDS)
            binding = await self.binding_for_session(session_id)
            if binding is None or not binding.thread_id:
                return
            state = self.manager._states.get(session_id)
            title = state.session.title if state is not None else binding.title
            deadline = time.monotonic() + TOPIC_RENAME_MAX_WAIT_SECONDS
            for _ in range(3):
                while flooded(binding.chat_id):
                    if time.monotonic() > deadline:
                        return  # give up; the next state change carries the newer state anyway
                    await asyncio.sleep(1.0)
                name = self._topic_name(session_id, title)
                try:
                    await tg_call(self.bot.edit_forum_topic, binding.chat_id, binding.thread_id, name=name, attempts=1, flood_chat=binding.chat_id)
                    logger.debug("topic %s renamed to %r", binding.thread_id, name)
                    return
                except TelegramBadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        logger.warning("could not mark topic status: %s", exc)
                    return
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(float(exc.retry_after) + 0.5, max(0.0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a cosmetic rename must never become an unretrieved task exception
            logger.warning("topic status rename failed", exc_info=True)
        finally:
            if self._topic_status_tasks.get(session_id) is asyncio.current_task():
                self._topic_status_tasks.pop(session_id, None)

    async def react_to_last(self, session_id: str, kind: str) -> None:
        """Put the run's outcome on the operator's message that started it."""
        if not self.config.telegram.reactions:
            return
        ref = self._last_operator_message.get(session_id)
        emoji = RUN_REACTIONS.get(kind)
        if ref is None or emoji is None:
            return
        await TelegramOutbox(self.bot, ref[0], None).react(ref[1], emoji)

    async def cmd_compact(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /compact inside a session topic (or the private chat).")
            return
        count = len(await self.manager.sessions.list_messages(state.session.id, "daedalus", limit=10_000))
        if count == 0:
            await message.answer("Nothing to compact yet.")
            return
        focus = (command.args or "").strip()
        self._compact_focus[state.session.id] = focus
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🗜 Replace {count} messages with a summary", callback_data=f"cm:{state.session.id}:go")],
                [InlineKeyboardButton(text="Cancel", callback_data=f"cm:{state.session.id}:cancel")],
            ]
        )
        await message.answer(
            "Compact the history? The agent keeps only a model-written summary; the full transcript is saved "
            "in the project files as .history-<time>.jsonl." + (f"\nFocus: {focus}" if focus else ""),
            reply_markup=keyboard,
        )

    async def cmd_clear(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /clear inside a session topic (or the private chat).")
            return
        count = len(await self.manager.sessions.list_messages(state.session.id, "daedalus", limit=10_000))
        if count == 0:
            await message.answer("The history is already empty.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🧹 Drop {count} messages, keep project files", callback_data=f"hc:{state.session.id}:go")],
                [InlineKeyboardButton(text="Cancel", callback_data=f"hc:{state.session.id}:cancel")],
            ]
        )
        await message.answer("Start over with an empty history? The project files, the brief and the session's settings stay; the transcript keeps the old turns.", reply_markup=keyboard)

    async def _on_clear_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, action = data
        if action != "go":
            await query.answer("cancelled")
            if query.message is not None:
                await query.message.edit_text("Clear cancelled.", reply_markup=None)
            return
        try:
            result = await self.manager.clear_history(session_id)
        except Exception as exc:  # noqa: BLE001
            await query.answer("failed")
            if query.message is not None:
                await query.message.edit_text(f"⚠️ clear failed: {exc}", reply_markup=None)
            return
        await query.answer("cleared")
        if query.message is not None:
            await query.message.edit_text(f"🧹 History cleared: {result['dropped']} message(s) dropped. The project files and settings stay.", reply_markup=None)

    async def _on_compact_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, action = data
        focus = self._compact_focus.pop(session_id, "")
        if action != "go":
            await query.answer("cancelled")
            if query.message is not None:
                await query.message.edit_text("Compact cancelled.", reply_markup=None)
            return
        await query.answer("compacting…")
        if query.message is not None:
            await query.message.edit_text("🗜 Compacting the history…", reply_markup=None)
        try:
            summary = await self.manager.compact(session_id, focus)
        except Exception as exc:  # noqa: BLE001
            if query.message is not None:
                await query.message.edit_text(f"⚠️ compact failed: {exc}")
            return
        outbox = await self.outbox_for_session(session_id)
        if query.message is not None:
            try:
                await query.message.delete()
            except TelegramBadRequest:
                pass
        if outbox is not None:
            await outbox.send_html(
                "<p><b>🗜 History compacted.</b> The session continues from this summary.</p>"
                f"<details><summary>Summary</summary>{markdown_to_html(summary)}</details>"
            )

    async def cmd_prompt(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        rules = self.config.prompt.rules.strip() or DEFAULT_RULES.strip()
        origin = "custom (config)" if self.config.prompt.rules.strip() else "built-in default"
        outbox = TelegramOutbox(self.bot, message.chat.id, message.message_thread_id if message.is_topic_message else None)
        await outbox.send_html(f"<p><b>Working rules</b> — {origin}. Edit in the Mini App → Settings.</p><pre>{html.escape(rules)}</pre>")

    async def _ask_close(self, session_id: str, title: str, chat_id: int, thread_id: int | None) -> None:
        state = await self.manager.get_state(session_id)
        size = 0
        if state is not None and state.workspace.exists():
            size = sum(f.stat().st_size for f in state.workspace.rglob("*") if f.is_file())
        keep = "📦 Put it away, keep the agent" if self.private_mode() else "📦 Close the topic, keep the agent"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Close and delete the agent", callback_data=f"cl:{session_id}:delete")],
                [InlineKeyboardButton(text=keep, callback_data=f"cl:{session_id}:keep")],
                [InlineKeyboardButton(text="Cancel", callback_data=f"cl:{session_id}:cancel")],
            ]
        )
        await tg_call(
            self.bot.send_message,
            chat_id,
            f"Close session '{title}' ({session_id})? Its working directory holds {size / 1_048_576:.1f} MB; shared project files are kept.",
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )

    async def _release_current(self, session_id: str) -> None:
        """A session that was put away or deleted stops being the private chat's window."""
        if await self.current_session_id() == session_id:
            await self.set_current_session("")

    async def _close_topic(self, binding: TopicBinding) -> None:
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        if binding.thread_id:
            try:
                await self.bot.close_forum_topic(binding.chat_id, binding.thread_id)
            except TelegramBadRequest:
                pass

    async def _on_policy_decision(self, query: CallbackQuery, data: list[str]) -> None:
        """The operator lets a refused call through once, or leaves it refused, from the buttons under the refusal.

        ``pa:<session>:<key>`` allows; ``pa:<session>:<key>:no`` refuses. The refusal carries the key
        so the request is closed everywhere else it is shown, not only in this chat.
        """
        if len(data) == 4 and data[3] == "no":
            _, session_id, key, _no = data
            try:
                await self.manager.refuse(session_id, key, via="telegram")
            except (KeyError, ValueError) as exc:
                await query.answer(str(exc)[:180])
                return
            await query.answer("left refused")
            if query.message is not None:
                await query.message.edit_text("Left refused.", reply_markup=None)
            return
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, key = data
        try:
            result = await self.manager.grant(session_id, key, via="telegram")
        except (KeyError, ValueError) as exc:
            await query.answer(str(exc)[:180])
            return
        approves = result.get("approves")
        what = f"{approves['tool']}: {approves['text']}" if approves else "the refused call"
        await query.answer("granted once")
        if query.message is not None:
            await query.message.edit_text(f"✅ Allowed once ({key}): {what}. The agent retries it on its next step; the grant expires in {result['expires_in_minutes']} minutes.", reply_markup=None)

    async def _on_close_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, session_id, action = data
        binding = await self.binding_for_session(session_id)
        if action == "cancel":
            await query.answer("kept open")
            if query.message is not None:
                await query.message.edit_text("Close cancelled.", reply_markup=None)
            return
        if action == "keep":
            await self.manager.stop(session_id)
            if binding is not None:
                await self._close_topic(binding)
            await self._release_current(session_id)
            await query.answer("closed")
            if query.message is not None:
                where = "/sessions, /use, Mini App" if self.private_mode() else "/sessions, Mini App"
                what = "Put away" if self.private_mode() else "Topic closed"
                await query.message.edit_text(f"{what}; session {session_id} and its project files are kept ({where}).", reply_markup=None)
            return
        if binding is not None:
            await self._close_topic(binding)
        await self._release_current(session_id)
        removed = await self.manager.delete_session(session_id, delete_workspace=True)
        await query.answer("deleted" if removed else "already gone")
        if query.message is not None:
            await query.message.edit_text(f"Session {session_id} deleted. Shared project files were kept.", reply_markup=None)
        if binding is not None and binding.thread_id:
            try:
                await self.bot.delete_forum_topic(binding.chat_id, binding.thread_id)
            except TelegramBadRequest:
                pass

    async def on_topic_closed(self, message: Message) -> None:
        """The operator closed a topic by hand: ask in General what to do with its session."""
        binding = await self.binding_for_topic(message.chat.id, message.message_thread_id or 0)
        if binding is None:
            return
        await self.manager.stop(binding.session_id)
        await self.manager.db.execute(
            "UPDATE topics SET closed_at = ? WHERE chat_id = ? AND thread_id = ?",
            (datetime.now(UTC).isoformat(), binding.chat_id, binding.thread_id),
        )
        await self._ask_close(binding.session_id, binding.title, message.chat.id, self.config.telegram.general_topic_id or None)

    async def cmd_delete(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        session_id = (command.args or "").strip()
        if not session_id:
            await message.answer("usage: /delete <session id>  (see /sessions)")
            return
        binding = await self.binding_for_session(session_id)
        if binding is None:
            removed = await self.manager.delete_session(session_id)
            if removed:
                await self._release_current(session_id)
            await message.answer("deleted" if removed else "no such session")
            return
        await self._ask_close(binding.session_id, binding.title, message.chat.id, message.message_thread_id if message.is_topic_message else None)

    async def cmd_cleanup(self, message: Message) -> None:
        """Delete every session whose topic is already closed."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        orphans = await self.manager.closed_topic_sessions()
        swept = await self.manager.sweep_orphan_workspaces()
        if not orphans:
            await message.answer("No sessions with closed topics." + (f" Removed {len(swept)} unused managed folder(s)." if swept else ""))
            return
        total = sum(o["bytes"] for o in orphans) / 1_048_576
        lines = [f"• {o['title']} ({o['session_id']}) {o['bytes'] / 1_048_576:.1f} MB" for o in orphans[:30]]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🗑 Delete all {len(orphans)} ({total:.1f} MB)", callback_data="cu:all")],
                [InlineKeyboardButton(text="Cancel", callback_data="cu:cancel")],
            ]
        )
        await message.answer("Sessions whose topics are closed:\n" + "\n".join(lines), reply_markup=keyboard)

    async def _on_cleanup_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if data[-1] != "all":
            await query.answer("cancelled")
            if query.message is not None:
                await query.message.edit_text("Cleanup cancelled.", reply_markup=None)
            return
        removed = 0
        for orphan in await self.manager.closed_topic_sessions():
            if await self.manager.delete_session(orphan["session_id"]):
                removed += 1
                try:
                    if orphan["thread_id"]:
                        await self.bot.delete_forum_topic(orphan["chat_id"], orphan["thread_id"])
                except TelegramBadRequest:
                    pass
        await query.answer(f"deleted {removed}")
        if query.message is not None:
            await query.message.edit_text(f"Deleted {removed} session(s). Shared project files were kept.", reply_markup=None)

    async def cmd_sessions(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        sessions = await self.manager.list_sessions(limit=SESSION_LIST_LIMIT)
        private = self.private_mode()
        if not sessions:
            await message.answer("No sessions yet." + (" /new <title> starts one." if private else ""))
            return
        current = await self.current_session_id() if private else ""
        lines = [
            f"{i}. {'▶' if s['status'] == 'running' else '❓' if s['status'] == 'waiting' else '·'} {s['title']} — {s['id']} ({s['status']})"
            + ("  ← you are writing here" if s["id"] == current else "")
            for i, s in enumerate(sessions, 1)
        ]
        await message.answer("\n".join(lines) + ("\n\n/use <number> writes to another one." if private else ""))

    async def cmd_model(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip()
        found = self.config.default_preset()
        if found is None:
            await message.answer(NO_MODEL_MESSAGE)
            return
        default_id, default = found
        if not arg:
            lines = [f"  {pid} — {p.display(pid)}{'  (default)' if pid == default_id else ''}" for pid, p in self.config.presets.items()]
            await message.answer(
                f"default: {default.display(default_id)}\nmodels:\n" + "\n".join(lines)
                + "\nusage: /model <preset-id> (in General: sets the default; in a topic: this session) · /model default · /model provider/model-id"
            )
            return
        general = self._is_general(message) and message.chat.type != "private"
        if arg in self.config.presets:
            if general:
                self.config.model.preset = arg
                self.config.model.chain = [c for c in self.config.model.chain if c != arg]
                await self.save_config(self.config)
                await message.answer(f"Default model: {self.config.presets[arg].display(arg)}")
                return
            state = await self._session_for_message(message)
            if state is not None:
                await self.manager.set_model(state.session.id, preset=arg)
                await message.answer(f"Session model: {self.config.presets[arg].display(arg)} (from the next model call)")
            return
        if general:
            await message.answer("In General, /model takes a preset id (see /model). Create presets in the Mini App → Settings → Models.")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        if arg in ("default", "reset"):
            await self.manager.set_model(state.session.id, clear=True)
            await message.answer("Session model: back to the global default")
            return
        provider, _, name = arg.partition("/")
        if not name or provider not in self.manager.providers.available():
            await message.answer("usage: /model <preset-id> | provider/model-id | default")
            return
        await self.manager.set_model(state.session.id, model_name=name, provider=provider)
        await message.answer(f"Session model: {provider}/{name} (from the next model call)")

    async def cmd_thinking(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip().lower()
        thinking: bool | None = None
        effort: str | None = None
        found = self.config.default_preset()
        if found is None:
            await message.answer(NO_MODEL_MESSAGE)
            return
        default_id, default = found
        if arg in ("on", "off"):
            thinking = arg == "on"
        elif arg in ("low", "medium", "high"):
            thinking, effort = True, arg
        else:
            await message.answer(f"{default.display(default_id)}: thinking={default.thinking} effort={default.reasoning_effort}\nusage: /thinking on|off|low|medium|high")
            return
        if self._is_general(message) and message.chat.type != "private":
            default.thinking = bool(thinking)
            if effort:
                default.reasoning_effort = effort  # type: ignore[assignment]
            await self.save_config(self.config)
            await message.answer(f"{default.display(default_id)}: thinking={default.thinking} effort={default.reasoning_effort}")
            return
        state = await self._session_for_message(message)
        if state is None:
            return
        await self.manager.set_model(state.session.id, thinking=thinking, reasoning_effort=effort)
        await message.answer(f"Session thinking={thinking} effort={effort or default.reasoning_effort}")

    async def cmd_mode(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /mode inside a session topic (or the private chat).")
            return
        arg = (command.args or "").strip().lower()
        modes = self.config.modes
        if not arg:
            current = state.metadata.get("mode") or "default"
            lines = [f"  {name} — {m.description or ''} (iterations {m.max_iterations or self.config.limits.max_iterations}, cap ${m.usd_per_run if m.usd_per_run is not None else self.config.limits.usd_per_run})" for name, m in modes.items()]
            await message.answer(f"mode: {current}\navailable:\n" + "\n".join(lines) + "\nusage: /mode <name> · /mode default")
            return
        try:
            chosen = await self.manager.set_mode(state.session.id, None if arg in ("default", "off", "reset") else arg)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"mode: {chosen or 'default'} (applies from the next run)")

    async def cmd_yagni(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /yagni inside a session topic (or the private chat).")
            return
        wanted = yagni_argument(command.args or "", bool(state.metadata.get("yagni")))
        if wanted is None:
            await message.answer("usage: /yagni [on|off]")
            return
        await self.manager.set_yagni(state.session.id, wanted)
        await message.answer(f"YAGNI: {'on' if wanted else 'off'} (the next turn is told)")

    async def cmd_secret(self, message: Message, command: CommandObject) -> None:
        """``/secret <name> <value>``: keep a value for this session to use without reading it, and delete the
        message that carried it. Registered as a command so it never reaches the model as text, which an
        unknown command otherwise does."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        name, _, value = (command.args or "").strip().partition(" ")
        value = value.strip() if "\n" not in value else value.strip("\n ")
        deleted = False
        if value:
            # First, whatever happens next: the value must not stay in the chat's history.
            with suppress(TelegramAPIError):
                deleted = bool(await self.bot.delete_message(message.chat.id, message.message_id))
        state = await self._session_for_message(message)
        if state is None or (self._is_general(message) and message.chat.type != "private"):
            await message.answer("Use /secret inside a session topic (or the private chat).")
            return
        if not name or not value:
            await message.answer("usage: /secret <name> <value> — for example /secret router_admin hunter2. The message is deleted once it is kept.")
            return
        try:
            secret = await self.manager.secrets.put("session", state.session.id, name, value)
        except SecretError as exc:
            await message.answer(f"Not kept: {exc}")
            return
        gone = "The message with the value was deleted." if deleted else "Delete the message with the value yourself: the bot could not."
        await message.answer(f"🔒 Kept {secret.placeholder} for this session ({secret.env} in its commands). {gone}")

    async def cmd_status(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        sessions = await self.manager.list_sessions(limit=50)
        active = [s for s in sessions if s["status"] in ("running", "waiting")]
        if not active:
            await message.answer("Idle. No active runs.")
            return
        await message.answer("\n".join(f"{s['status']}: {s['title']} ({s['id']})" for s in active))

    async def cmd_usage(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await self.manager.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd,"
            " sum(cost_usd IS NULL) unmetered FROM usage_events WHERE at >= ?",
            (today,),
        )
        text = f"today: {row['c'] or 0} calls · in {row['i'] or 0:,} · out {row['o'] or 0:,} · cached {row['ch'] or 0:,}"
        text += _cost_words(row["usd"], int(row["unmetered"] or 0))
        # In the private chat there is no General to stand apart from: the spend of the session
        # the chat is on is as much "here" as a topic's own is.
        state = await self._session_for_message(message) if (self.private_mode() or not self._is_general(message)) else None
        if state is not None:
            srow = await self.manager.db.fetchone(
                "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered"
                " FROM usage_events WHERE session_id = ?",
                (state.session.id,),
            )
            text += f"\nthis session: {srow['c'] or 0} calls · in {srow['i'] or 0:,} · out {srow['o'] or 0:,}"
            text += _cost_words(srow["usd"], int(srow["unmetered"] or 0))
        if self.config.limits.usd_per_run > 0:
            text += f"\nper-run cap: ${self.config.limits.usd_per_run:.2f} (limits.usd_per_run)"
        await message.answer(text)

    async def cmd_settings(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        c = self.config
        default = c.default_preset()
        model_line = f"{default[1].display(default[0])} thinking={default[1].thinking} effort={default[1].reasoning_effort}" if default else f"none — {NO_MODEL_MESSAGE}"
        await message.answer(
            f"model: {model_line}\n"
            f"fallback: {', '.join(c.model.chain) or 'none'}\n"
            f"self-change approval: {c.self_change.approval}, auto_rebuild={c.self_change.auto_rebuild}\n"
            f"limits: ${self.settings.usd_per_day}/day (env), {c.limits.max_iterations} iterations, tool timeout {c.limits.tool_timeout_seconds:.0f}s\n"
            f"balance thresholds: {c.balance.thresholds_usd} (every {c.balance.poll_seconds}s)\n"
            f"verbosity: {c.telegram.verbosity}\n"
            + (
                "chat: one private chat, a window onto one session at a time (/sessions, /use)"
                if self.private_mode()
                else f"chat: one topic per session in forum {c.telegram.forum_chat_id or 'not bound'}"
            )
        )

    async def cmd_operator(self, message: Message, command: CommandObject) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        name = command.command
        if name == "allow":
            state = await self._session_for_message(message)
            if state is None:
                await message.answer("/allow works inside a session's topic or the private chat")
                return
            try:
                result = await self.manager.grant(state.session.id, command.args or "", via="telegram")
            except ValueError as exc:
                await message.answer(f"usage: /allow <key> — {exc}")
                return
            approves = result.get("approves")
            what = f"{approves['tool']}: {approves['text']}" if approves else "a call this host has not seen refused yet (the key is taken on trust)"
            await message.answer(f"Granted {result['key']} for {what}. The same call passes once within {result['expires_in_minutes']} minutes.")
            return
        if name == "verbosity":
            try:
                self.config.telegram.verbosity = max(0, min(2, int((command.args or "").strip())))
            except ValueError:
                await message.answer("usage: /verbosity 0|1|2")
                return
            await self.save_config(self.config)
            await message.answer(f"verbosity={self.config.telegram.verbosity}")
            return
        if name == "approval":
            arg = (command.args or "").strip()
            if arg not in ("manual", "auto"):
                await message.answer(f"approval={self.config.self_change.approval}\nusage: /approval manual|auto")
                return
            self.config.self_change.approval = arg  # type: ignore[assignment]
            await self.save_config(self.config)
            await message.answer(f"approval={arg}")
            return
        hook = self.command_hooks.get(name) or None
        if hook is not None:
            await hook(message, command)
            return
        op = self.operator_hooks.get(name)
        if op is None:
            await message.answer(f"/{name} is not available yet.")
            return
        await message.answer(await op(command.args or ""))

    async def on_message(self, message: Message) -> None:
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        if message.text and message.text.startswith("/") and not self.config.telegram.forward_unknown_commands:
            return
        for interceptor in self.message_interceptors:
            if await interceptor(message):
                return
        if self._is_stale(message):
            self._note_stale(message)
            return
        state = await self._force_reply_session(message) or await self._session_for_message(message)
        if state is None:
            return
        if await self._maybe_custom_answer(message, state):
            if self.config.telegram.reactions:
                await TelegramOutbox(self.bot, message.chat.id, None).react(message.message_id, RUN_REACTIONS["received"])
            return
        oversize = self._oversize(message)
        if oversize:
            await message.reply(f"⚠️ {oversize}")
            return
        key = (message.chat.id, message.message_thread_id or 0, state.session.id)
        text = self._text_of(message)
        self._last_operator_message[state.session.id] = (message.chat.id, message.message_id)
        if self.config.telegram.reactions:
            await TelegramOutbox(self.bot, message.chat.id, None).react(message.message_id, RUN_REACTIONS["received"])
        attachment = await self._download(message, state)  # may take a while for big files
        if attachment is not None and (message.voice or (message.audio and (message.audio.mime_type or "") in SPEECH_MIME_TYPES)) and recogniser_available(self.speech, self.config):
            if await self._voice_to_text(message, state, attachment, text):
                return
        buffer = self._buffers.setdefault(key, InboundBuffer())
        if text:
            buffer.text.append(text)
        if attachment is not None:
            buffer.attachments.append(attachment)
        if buffer.task is not None:
            buffer.task.cancel()
        wait = self.config.telegram.inbound_merge_window_seconds
        if message.photo is not None and not any(buffer.text):
            # A bare photo is usually followed by the words about it (often a voice note).
            wait = max(wait, self.config.telegram.photo_caption_wait_seconds)
        buffer.task = asyncio.create_task(self._flush_inbound(key, state, wait))

    async def _voice_to_text(self, message: Message, state: SessionState, attachment: Attachment, caption: str) -> bool:
        """Transcribe a voice note; the transcript is confirmed before it goes to the agent (unless autosend)."""
        duration = int(getattr(message.voice or message.audio, "duration", 0) or 0)
        if duration > self.config.asr.max_seconds:
            await message.reply(f"🎙 {duration}s is over the transcription limit ({self.config.asr.max_seconds}s); the file is attached as is.")
            return False
        try:
            transcript = await transcribe_recording(self.speech, self.config, self.manager, attachment.path)
        except TranscriptionError as exc:
            await message.reply(f"🎙 could not transcribe ({exc}); the file is attached as is.")
            return False
        # The agent reads the words, marked as a transcript; the audio itself does not travel with them.
        text = voice_note_text(transcript, caption)
        if self.config.asr.autosend:
            await message.reply(f"🎙 {transcript[:1000]}")
            self._discard_audio(state, attachment)
            await self._enqueue_text(state, message, text, None)
            return True
        token = uuid.uuid4().hex[:8]
        now = time.monotonic()
        for stale in [k for k, v in self._voice_pending.items() if now - v[5] > VOICE_PENDING_TTL_SECONDS]:
            self._voice_pending.pop(stale, None)
        while len(self._voice_pending) >= VOICE_PENDING_MAX:
            self._voice_pending.pop(next(iter(self._voice_pending)), None)
        self._voice_pending[token] = (state.session.id, text, attachment, message.chat.id, message.message_thread_id or 0, now)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✓ Send to the agent", callback_data=f"vc:{token}:go"), InlineKeyboardButton(text="✗ Discard", callback_data=f"vc:{token}:no")]])
        await message.reply(f"🎙 I heard:\n\n{transcript[:3500]}", reply_markup=keyboard)
        return True

    @staticmethod
    def _discard_audio(state: SessionState | None, attachment: Attachment) -> None:
        """A transcribed voice note has served its purpose: the file leaves the inbox."""
        try:
            if state is not None and attachment.path.is_relative_to(state.workspace):
                attachment.path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _enqueue_text(self, state: SessionState, message: Message, text: str, attachment: Attachment | None) -> None:
        key = (message.chat.id, message.message_thread_id or 0, state.session.id)
        buffer = self._buffers.setdefault(key, InboundBuffer())
        buffer.text.append(text)
        if attachment is not None:
            buffer.attachments.append(attachment)
        if buffer.task is not None:
            buffer.task.cancel()
        buffer.task = asyncio.create_task(self._flush_inbound(key, state, self.config.telegram.inbound_merge_window_seconds))

    async def _on_voice_decision(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 3:
            await query.answer("stale button")
            return
        _, token, action = data
        pending = self._voice_pending.pop(token, None)
        if pending is None:
            await query.answer("This transcript is no longer open; the audio file is still in the session inbox.", show_alert=True)
            return
        session_id, text, attachment, chat_id, thread_id, _ = pending
        if action != "go":
            self._discard_audio(await self.manager.get_state(session_id), attachment)
            await query.answer("discarded (the audio file was removed)")
            if query.message is not None:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
            return
        await query.answer("sent")
        if query.message is not None:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
        state = await self.manager.get_state(session_id)
        if state is None:
            return
        self._discard_audio(state, attachment)
        key = (chat_id, thread_id, session_id)
        buffer = self._buffers.setdefault(key, InboundBuffer())
        buffer.text.append(text)
        if buffer.task is not None:
            buffer.task.cancel()
        buffer.task = asyncio.create_task(self._flush_inbound(key, state, 0.1))

    def _is_stale(self, message: Message) -> bool:
        limit = self.config.telegram.stale_after_seconds
        if limit <= 0:
            return False
        return (datetime.now(UTC) - message.date).total_seconds() > limit

    def _note_stale(self, message: Message) -> None:
        """Count stale messages per chat and answer the burst with one notice."""
        key = (message.chat.id, message.message_thread_id or 0)
        self._stale_counts[key] = self._stale_counts.get(key, 0) + 1
        task = self._stale_notices.get(key)
        if task is None or task.done():
            self._stale_notices[key] = asyncio.create_task(self._send_stale_notice(key, message))

    async def _send_stale_notice(self, key: tuple[int, int], sample: Message) -> None:
        await asyncio.sleep(STALE_NOTICE_DELAY_SECONDS)
        count = self._stale_counts.pop(key, 0)
        self._stale_notices.pop(key, None)
        if not count:
            return
        age = int((datetime.now(UTC) - sample.date).total_seconds() // 60)
        what = "this message" if count == 1 else f"{count} messages"
        try:
            await sample.reply(f"⏳ Ignored: {what} arrived {age}+ min late (sent while the bot was down). Send it again if it still applies.")
        except TelegramBadRequest:
            pass

    def _oversize(self, message: Message) -> str | None:
        """Refuse a file by its declared size before spending the download."""
        media = message.document or message.video or message.audio or message.voice or message.video_note or message.animation
        size = getattr(media, "file_size", None) if media is not None else None
        limit = self.config.telegram.max_inbound_file_mb
        if size and size > limit * 1_048_576:
            return f"file refused: {size / 1_048_576:.0f} MB is over the {limit} MB limit (telegram.max_inbound_file_mb)"
        return None

    def _text_of(self, message: Message) -> str:
        """The operator's words plus, for content Telegram cannot hand over as a file, a faithful description."""
        text = message.text or message.caption or ""
        if message.sticker is not None:
            text = f"[sticker {message.sticker.emoji or ''} from set {message.sticker.set_name or '?'}]"
        elif message.location is not None:
            text = f"[location: {message.location.latitude}, {message.location.longitude}]" + (f"\n{text}" if text else "")
        elif message.contact is not None:
            c = message.contact
            text = f"[contact: {c.first_name} {c.last_name or ''} {c.phone_number}]".replace("  ", " ")
        elif message.poll is not None:
            text = f"[poll: {message.poll.question}: " + "; ".join(o.text for o in message.poll.options) + "]"
        reply = message.reply_to_message
        if reply is not None and reply.forum_topic_created is None:
            quoted = (reply.text or reply.caption or "").strip()
            if quoted:
                who = "the agent" if (reply.from_user and reply.from_user.is_bot) else "the operator"
                text = f'[replying to {who}: "{quoted[:500]}"]\n\n{text}'
        return text

    async def on_unsupported(self, message: Message) -> None:
        """Anything the bot has no path for gets a named answer instead of silence."""
        if not self._is_owner(message.from_user.id if message.from_user else None):
            return
        fields = [name for name in ("game", "invoice", "story", "giveaway", "checklist", "paid_media", "dice", "venue") if getattr(message, name, None) is not None]
        if not fields:
            return  # a service message (topic reopened, chat title changed, …): nothing to read, nothing to say
        try:
            await message.reply(f"⚠️ I cannot read {', '.join(fields)}; send text, a file, a photo, a voice note or a location.")
        except TelegramBadRequest:
            pass

    async def _flush_inbound(self, key: tuple[int, int, str], state: SessionState, wait: float) -> None:
        await asyncio.sleep(wait)
        buffer = self._buffers.pop(key, None)
        if buffer is None:
            return
        text = "\n".join(buffer.text).strip()
        if not text and not buffer.attachments:
            return
        if not text:
            text = "Files attached." if len(buffer.attachments) > 1 else "File attached."
        was_running = state.running and state.pending is None
        try:
            await self.manager.submit(state.session.id, text, buffer.attachments, via="telegram")
        except NoModelConfigured as exc:
            # Not a failure: the installation has not been finished yet. Say so, without a stack trace.
            outbox = await self.outbox_for_session(state.session.id)
            if outbox is not None:
                await outbox.send_text(str(exc), markdown=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("submit failed")
            outbox = await self.outbox_for_session(state.session.id)
            if outbox is not None:
                await outbox.send_text(f"⚠️ could not start: {exc}", markdown=False)
            await self.react_to_last(state.session.id, "failed")
            return
        if was_running:
            await self.react_to_last(state.session.id, "steered")
            renderer = self._renderers.get(state.session.id)
            if renderer is not None:
                renderer.view.narration.append(f"↪ steer (applies before the next model call): {text[:160]}")
                renderer._mark()
        else:
            self.set_topic_status(state.session.id, "running")

    async def _download(self, message: Message, state: SessionState) -> Attachment | None:
        file_id: str | None = None
        name: str | None = None
        mime = "application/octet-stream"
        if message.document:
            file_id, name, mime = message.document.file_id, message.document.file_name, message.document.mime_type or mime
        elif message.photo:
            photo = message.photo[-1]
            file_id, name, mime = photo.file_id, f"photo-{photo.file_unique_id}.jpg", "image/jpeg"
        elif message.video:
            file_id, name, mime = message.video.file_id, message.video.file_name or f"video-{message.video.file_unique_id}.mp4", message.video.mime_type or "video/mp4"
        elif message.audio:
            file_id, name, mime = message.audio.file_id, message.audio.file_name or f"audio-{message.audio.file_unique_id}.mp3", message.audio.mime_type or "audio/mpeg"
        elif message.voice:
            file_id, name, mime = message.voice.file_id, f"voice-{message.voice.file_unique_id}.ogg", "audio/ogg"
        elif message.video_note:
            file_id, name, mime = message.video_note.file_id, f"video-note-{message.video_note.file_unique_id}.mp4", "video/mp4"
        elif message.animation:
            file_id, name, mime = message.animation.file_id, message.animation.file_name or f"animation-{message.animation.file_unique_id}.mp4", message.animation.mime_type or "video/mp4"
        if file_id is None:
            return None
        inbox = state.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / (Path(name).name if name else file_id)
        try:
            tg_file = await self.bot.get_file(file_id)
            if self.settings.telegram_local_mode and tg_file.file_path and Path(tg_file.file_path).is_absolute():
                shutil.copy2(tg_file.file_path, target)
            else:
                await self.bot.download(tg_file, destination=target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("download failed")
            await message.reply(f"⚠️ could not download {name}: {exc}")
            return None
        return Attachment(path=target, mime_type=mime or mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    # -- questions ------------------------------------------------------------------

    async def _ask(self, session_id: str, payload: dict[str, Any]) -> None:
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return
        questions = payload.get("questions", [])
        state = {"answers": [{"question": q.get("question"), "selected": [], "custom": None} for q in questions], "index": 0, "questions": questions, "message_ids": []}
        self._question_state[session_id] = state
        await self._send_question(session_id, outbox)

    async def _send_question(self, session_id: str, outbox: TelegramOutbox) -> None:
        state = self._question_state[session_id]
        index = state["index"]
        q = state["questions"][index]
        options = q.get("options") or []
        rows = []
        selected = set(state["answers"][index]["selected"])
        for oi, option in enumerate(options):
            label = option.get("label", "")
            mark = "☑ " if label in selected else ""
            rows.append([InlineKeyboardButton(text=f"{mark}{label}"[:60], callback_data=f"aq:{session_id}:{index}:{oi}")])
        if q.get("multiSelect"):
            rows.append([InlineKeyboardButton(text="✅ Done", callback_data=f"aq:{session_id}:{index}:done")])
        if q.get("allow_custom") or not options:
            rows.append([InlineKeyboardButton(text="✍️ Type an answer", callback_data=f"aq:{session_id}:{index}:custom")])
        text = f"❓ {q.get('question')}"
        details = [f"• {o.get('label')}: {o.get('description')}" for o in options if o.get("description")]
        if details:
            text += "\n" + "\n".join(details)
        if len(state["questions"]) > 1:
            text += f"\n({index + 1}/{len(state['questions'])})"
        msg = await tg_call(
            self.bot.send_message, outbox.chat_id, outbox.attributed(text), message_thread_id=outbox.thread_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
        state["message_ids"].append(msg.message_id)

    async def on_generation_stopped(self, event: Any) -> None:
        """The operator pressed Stop on a streaming draft: cancel that session's run."""
        chat_id = event.chat.id
        thread_id = getattr(event, "message_thread_id", None) or 0
        if self.private_mode() and chat_id == self.settings.owner_user_id:
            # Only the current session streams a draft here, so the Stop belongs to it.
            session_id = await self.current_session_id()
            if session_id:
                await self.manager.stop(session_id)
            return
        binding = await self.binding_for_topic(chat_id, thread_id)
        if binding is not None:
            await self.manager.stop(binding.session_id)

    async def on_callback(self, query: CallbackQuery) -> None:
        if not self._is_owner(query.from_user.id):
            await query.answer()
            return
        try:
            await self._dispatch_callback(query)
        except Exception:  # noqa: BLE001 — a failed button must not kill polling or lose the answer
            logger.exception("callback handling failed")
            try:
                await query.answer("Something went wrong; try again.")
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch_callback(self, query: CallbackQuery) -> None:
        data = (query.data or "").split(":")
        if not data:
            return
        if data[0] == "aq":
            await self._on_answer(query, data)
            return
        if data[0] == "cl":
            await self._on_close_decision(query, data)
            return
        if data[0] == "cu":
            await self._on_cleanup_decision(query, data)
            return
        if data[0] == "pa":
            await self._on_policy_decision(query, data)
            return
        if data[0] == "hc":
            await self._on_clear_decision(query, data)
            return
        if data[0] == "cm":
            await self._on_compact_decision(query, data)
            return
        if data[0] == "vc":
            await self._on_voice_decision(query, data)
            return
        hook = self.callback_hooks.get(data[0])
        if hook is not None:
            await hook(query, data)
            return
        await query.answer()

    async def _on_answer(self, query: CallbackQuery, data: list[str]) -> None:
        if len(data) != 4 or not data[2].isdigit() or not (data[3].isdigit() or data[3] in ("done", "custom")):
            await query.answer("stale button")
            return
        _, session_id, index_s, choice = data
        state = self._question_state.get(session_id)
        if state is None:
            await query.answer("This question is no longer open.")
            return
        index = int(index_s)
        q = state["questions"][index]
        answer = state["answers"][index]
        if choice == "custom":
            await query.answer()
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                # Through the outbox, so the prompt carries the asking session's name wherever the
                # question card did — a bare "Type your answer:" in a shared chat says nothing.
                prompt_id = await self.send_force_reply(outbox.chat_id, outbox.thread_id, outbox.attributed("Type your answer:"))
                # What the operator replies to says whose question it answers, so a chat holding
                # several sessions still routes the answer to the session that asked.
                self._force_reply_targets[prompt_id] = session_id
                while len(self._force_reply_targets) > FORCE_REPLY_MAX:
                    self._force_reply_targets.pop(next(iter(self._force_reply_targets)))
            state["awaiting_custom"] = index
            return
        if choice == "done":
            await query.answer()
            await self._advance_question(session_id, query)
            return
        label = (q.get("options") or [])[int(choice)].get("label")
        if q.get("multiSelect"):
            if label in answer["selected"]:
                answer["selected"].remove(label)
            else:
                answer["selected"].append(label)
            await query.answer(", ".join(answer["selected"]) or "nothing selected")
            outbox = await self.outbox_for_session(session_id)
            if query.message is not None and outbox is not None:
                state["index"] = index
                rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                new_rows = []
                for row in rows:
                    new_row = []
                    for btn in row:
                        cd = btn.callback_data or ""
                        base = btn.text[2:] if btn.text.startswith("☑ ") else btn.text
                        if cd.endswith(":done") or cd.endswith(":custom"):
                            new_row.append(btn)
                        else:
                            new_row.append(InlineKeyboardButton(text=("☑ " if base in answer["selected"] else "") + base, callback_data=cd))
                    new_rows.append(new_row)
                try:
                    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))
                except Exception:  # noqa: BLE001
                    pass
            return
        answer["selected"] = [label]
        await query.answer(label)
        await self._advance_question(session_id, query)

    ANSWERED_ELSEWHERE = {
        "app": "Answered in the app.",
        "notification": "Answered from a notification.",
        "push": "Answered from a notification.",
        "cli": "Answered from the command line.",
        "orchestrator": "Answered by the orchestrator.",
    }
    """What the topic is told when its question was answered somewhere else. ``timeout`` is absent:
    the scheduler closes the question itself, with a note that says why."""

    async def _on_answered_elsewhere(self, event: AppEvent) -> None:
        """A question answered in the app used to leave a live keyboard here, whose buttons then
        answered a question that no longer existed."""
        via = str(event.payload.get("via") or "")
        note = self.ANSWERED_ELSEWHERE.get(via)
        if note is None or event.session_id is None or event.session_id not in self._question_state:
            return
        await self.close_question(event.session_id, note)

    async def close_question(self, session_id: str, note: str) -> None:
        """Retire an open question's keyboard (someone else answered it) and say why in the topic."""
        state = self._question_state.pop(session_id, None)
        outbox = await self.outbox_for_session(session_id)
        if state is not None and outbox is not None:
            for message_id in state.get("message_ids", []):
                try:
                    await self.bot.edit_message_reply_markup(chat_id=outbox.chat_id, message_id=message_id, reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
        if outbox is not None:
            try:
                await outbox.send_text(note, markdown=False)
            except Exception:  # noqa: BLE001
                pass

    async def _advance_question(self, session_id: str, query: CallbackQuery | None) -> None:
        state = self._question_state.get(session_id)
        if state is None:
            return
        if query is not None and query.message is not None:
            try:
                answered = state["answers"][state["index"]]
                summary = ", ".join(answered["selected"]) or (answered["custom"] or "")
                await query.message.edit_text(f"{query.message.text}\n→ {summary}", reply_markup=None)
            except Exception:  # noqa: BLE001 — cosmetic edit; flood limits must never block the answer
                logger.warning("could not freeze the question message", exc_info=True)
        state["index"] += 1
        if state["index"] < len(state["questions"]):
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                await self._send_question(session_id, outbox)
            return
        try:
            await self.manager.answer(session_id, state["answers"], via="telegram")
        except RuntimeError as exc:
            outbox = await self.outbox_for_session(session_id)
            if outbox is not None:
                await outbox.send_text(f"⚠️ could not deliver the answer: {exc}. Answer again in a moment.", markdown=False)
            state["index"] = max(0, len(state["questions"]) - 1)
            return
        self._question_state.pop(session_id, None)

    async def _force_reply_session(self, message: Message) -> SessionState | None:
        """The session whose "type your answer" prompt this message replies to, if any.

        The target is popped: the prompt answers one question. A second reply to the same prompt
        is an ordinary message to the current session, which is what a second thought usually is.
        """
        reply = message.reply_to_message
        session_id = self._force_reply_targets.pop(reply.message_id, None) if reply is not None else None
        return await self.manager.get_state(session_id) if session_id else None

    async def _maybe_custom_answer(self, message: Message, state: SessionState) -> bool:
        qs = self._question_state.get(state.session.id)
        if qs is None or "awaiting_custom" not in qs:
            return False
        index = qs.pop("awaiting_custom")
        qs["answers"][index]["custom"] = self._text_of(message)
        qs["index"] = index
        await self._advance_question(state.session.id, None)
        return True

    # -- events from runs -----------------------------------------------------------

    async def _renderer_for(self, session_id: str, run_id: str) -> RunRenderer | None:
        renderer = self._renderers.get(session_id)
        if renderer is not None and renderer.view.run_id == run_id:
            return renderer
        if renderer is not None:
            renderer.close()  # a new run replaces it; its timers must not keep editing a dead status
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return None
        state = await self.manager.get_state(session_id)
        default = self.config.default_preset()
        model = state.engine.effective_model_name if state and state.engine else (default[1].model if default else "")

        async def cost_lookup(rid: str) -> float | None:
            row = await self.manager.db.fetchone("SELECT sum(cost_usd) c, count(*) n FROM usage_events WHERE run_id = ?", (rid,))
            return float(row["c"]) if row and row["c"] is not None else None

        mode = self.manager.mode_for(state) if state is not None else None
        verbosity = mode.verbosity if mode is not None and mode.verbosity is not None else self.config.telegram.verbosity
        renderer = RunRenderer(
            outbox,
            RunView(run_id=run_id, model=model, verbosity=verbosity),
            edit_interval=self.config.telegram.status_edit_interval_seconds,
            edit_tiers=self.config.telegram.status_edit_tiers,
            slow_tool_seconds=self.config.telegram.slow_tool_seconds,
            cost_lookup=cost_lookup,
            streaming=self.config.telegram.streaming and outbox.chat_id > 0,
            draft_interval=self.config.telegram.draft_interval_seconds,
        )
        self._renderers[session_id] = renderer
        return renderer

    async def _on_event(self, session_id: str, event: TurnEvent) -> None:
        renderer = await self._renderer_for(session_id, event.run_id)
        if renderer is None:
            return
        if event.type is EventType.STATE_CHANGED and event.payload.get("to") == "running":
            # A compaction pass is a moment inside a run, not a state of the topic: renaming the topic there and
            # back is two chat notices per pass. The status message shows it instead.
            self.set_topic_status(session_id, "running")
        await renderer.handle(event)
        if event.type is EventType.COMPACTION_COMPLETED:
            p = event.payload
            before, after = int(p.get("tokens_before") or 0), int(p.get("tokens_after") or 0)
            summarised = int(p.get("tier2_summarised") or 0) + int(p.get("tier3_folded") or 0)
            dropped = int(p.get("floor_dropped") or 0)
            # A pass that changed nothing is not news; the events keep the record.
            outbox = await self.outbox_for_session(session_id) if summarised or dropped or (before and before - after >= 0.05 * before) else None
            if outbox is not None:
                # The floor removes spans without a summary; that is worth saying in so many words.
                floor = f" {dropped} message(s) removed without a summary (their exact values are in the compaction ledger)." if dropped else ""
                try:
                    await outbox.send_html(
                        f"<p>🗜 <b>Context compacted</b> ({p.get('reason', 'routine')}): "
                        f"{int(p.get('tokens_before') or 0):,} → {int(p.get('tokens_after') or 0):,} tokens; "
                        f"{int(p.get('tier2_summarised') or 0)} turn(s) summarised.{floor} Older detail is now a summary in the transcript.</p>"
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("could not post the compaction note", exc_info=True)
        if event.type is EventType.TOOL_CALL_PENDING and event.payload.get("kind") == "ask_user":
            await renderer.flush()
            await self._ask(session_id, dict(event.payload.get("ask_user_payload") or {}))
        if event.type is EventType.TOOL_RESULT and event.payload.get("is_error"):
            content = str(event.payload.get("content") or "")
            match = APPROVAL_KEY_RE.search(content)
            if match:
                outbox = await self.outbox_for_session(session_id)
                state = await self.manager.get_state(session_id)
                pending = (state.metadata.get("policy_pending") or {}).get(match.group(1)) if state is not None else None
                what = f"{pending['tool']}: {pending['text']}" if pending else "a call the policy wants approved"
                if outbox is not None:
                    try:
                        await self.send_choice(outbox, f"🛂 The policy stopped {what}\n\nAllow it once? The agent retries on its next step.", [[("✅ Allow once", f"pa:{session_id}:{match.group(1)}"), ("✖ Leave refused", f"pa:{session_id}:{match.group(1)}:no")]])
                    except Exception:  # noqa: BLE001
                        logger.warning("could not post the approval buttons", exc_info=True)

    async def _on_pending_restored(self, session_id: str, pending: Any) -> None:
        """After a restart, post the open question again with a fresh keyboard."""
        outbox = await self.outbox_for_session(session_id)
        if outbox is not None:
            await outbox.send_text("↩️ Restarted while waiting for your answer; here is the question again.", markdown=False)
        await self._ask(session_id, dict(pending.payload))

    async def _on_auto_compaction(self, session_id: str, info: dict[str, Any]) -> None:
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return
        try:
            await outbox.send_text(
                f"🗜 Context compacted: {info['before_messages']} → {info['after_messages']} messages, the prompt was {info['before_tokens'] // 1000}k of {info['window'] // 1000}k tokens ({info['seconds']} s). The full transcript stays searchable.",
                markdown=False,
            )
        except Exception:  # noqa: BLE001
            logger.warning("compaction notice failed", exc_info=True)

    async def _on_finished(self, session_id: str, run_id: str, status: str) -> None:
        self.set_topic_status(session_id, status)
        await self.react_to_last(session_id, status)
        renderer = self._renderers.get(session_id)
        if renderer is None:
            return
        if renderer.view.run_id != run_id:
            renderer.close()
            self._renderers.pop(session_id, None)
            return
        state = await self.manager.get_state(session_id)
        if status == "awaiting":
            renderer.view.state = "awaiting"
            await renderer.flush()
            return
        services = state.services if state is not None else None
        quiet = services is not None and services.extra.get("silent_run") == run_id
        # An interrupted run keeps its snapshot and resumes after the restart; its half-written
        # text is not an answer to deliver, so it never enters the ledger.
        final = split_headline(renderer.view.text_buffer.strip())[0] if not quiet and status != "interrupted" else ""
        if final:
            await self.ledger.begin(run_id, session_id, final)
        await renderer.finish(status, workspace=state.workspace if state else Path("/tmp"), quiet=quiet)
        if not quiet and status != "interrupted":
            renderer.view.delivery_failed = not await self._deliver_inline_media(session_id, run_id, renderer.outbox) or renderer.view.delivery_failed
        if final:
            await self.ledger.settle(run_id, delivered=not renderer.view.delivery_failed, error="delivery failed" if renderer.view.delivery_failed else "")
        self._renderers.pop(session_id, None)

    async def _deliver_inline_media(self, session_id: str, run_id: str, outbox: Outbox) -> bool:
        """Deliver the media whose placeholders the renderer removed from the text answer."""
        presentations = await self.manager.media.ready_for_run(session_id, run_id)
        delivered = True
        for presentation in presentations:
            try:
                items = presentation["items"]
                if presentation["layout"] == "album" and all(item["kind"] == "image" for item in items):
                    await outbox.send_album([_delivery_source(item) for item in items], items[0].get("caption") or None)
                    continue
                # The photo-only transport album cannot carry a mixed presentation. Preserve
                # every attachment and its order using the existing typed delivery methods.
                for item in items:
                    path = _delivery_source(item)
                    caption = item.get("caption") or None
                    if item["kind"] == "video":
                        await outbox.send_video(path, caption)
                    elif item["kind"] == "audio":
                        await outbox.send_audio(path, caption)
                    elif item["kind"] == "animation":
                        await outbox.send_animation(path, caption)
                    else:
                        await outbox.send_photo(path, caption)
            except Exception:  # noqa: BLE001 — the text answer still reaches the chat; the app keeps the original
                delivered = False
                logger.exception("could not deliver inline media %s for run %s", presentation["id"], run_id)
        return delivered

    async def redeliver_pending(self) -> int:
        """After a restart, re-send answers the previous process generated but never confirmed sent.

        Honest at-least-once: the text is marked as recovered because Telegram may already have it.
        """
        rows = await self.ledger.recoverable()
        sent = 0
        for row in rows:
            outbox = await self.outbox_for_session(row["session_id"])
            if outbox is None:
                continue
            await self.ledger.begin(row["run_id"], row["session_id"], row["text"])
            try:
                await outbox.send_text("↩️ _recovered reply — the run finished right before a restart; you may already have it_", markdown=True)
                for chunk in split_message(row["text"]):
                    await outbox.send_text(chunk)
                await self.ledger.settle(row["run_id"], delivered=True)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                await self.ledger.settle(row["run_id"], delivered=False, error=f"{type(exc).__name__}: {exc}")
        await self.ledger.prune()
        return sent

    # -- services for tools ---------------------------------------------------------

    async def _service_send_file(self, session_id: str, path: Path, caption: str | None) -> str:
        outbox = await self.outbox_for_session(session_id)
        if outbox is None:
            return "no chat bound"
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime.startswith("image/") and path.stat().st_size < 10_000_000:
            await outbox.send_photo(path, caption)
        else:
            await outbox.send_document(path, caption)
        return "delivered"

    async def _service_spawn_agent(
        self,
        session_id: str,
        *,
        title: str,
        brief: str,
        files: list[str],
        first_message: str | None,
        preset: str | None,
        mode: str | None,
        mcp: list[str],
        peer_name: str | None,
        tools_off: list[str] | None = None,
        loop: dict[str, Any] | None = None,
    ) -> str:
        """A standing agent: its own topic and workspace, a brief in its system prompt, copies of the files it needs.

        Spawned from a project session it joins that project. An independent agent outside a project
        is a normal thing to want; an agent that carries the operator's repository out of the folder
        they walled it into, in one tool call, is not — and that is what a child without the project
        and a list of absolute paths from inside it came to.
        """
        metadata: dict[str, Any] = {"brief": brief.strip(), "spawned_by": session_id}
        if tools_off:
            metadata["tools_off"] = sorted(set(tools_off))
        project = await self.manager.project_of(session_id)
        parent = await self.manager.get_state(session_id)
        # A site session's child stays on the site. Opening it a topic would put the bot's
        # words back into Telegram for a chat the operator started in the Mini App.
        if parent is not None and parent.metadata.get("telegram_detached"):
            metadata["telegram_detached"] = True
            state = await self.manager.create_session(title, metadata=metadata, project_id=project.id if project is not None else None)
        else:
            state, _ = await self.create_session_topic(title, metadata=metadata, project_id=project.id if project is not None else None)
        copied: list[str] = []
        for raw in files:
            source = Path(raw)
            if not source.exists():
                continue
            target = state.workspace / "inbox" / source.name
            if target == source or (project is not None and source.is_relative_to(state.workspace)):
                # Already where the new agent works: the two share the project root.
                copied.append(str(source))
                continue
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
            copied.append(str(target))
        if preset:
            await self.manager.set_model(state.session.id, preset=preset)
        if mode:
            await self.manager.set_mode(state.session.id, mode)
        for server in mcp:
            await self.manager.set_mcp(state.session.id, server, True)
        peers = self.manager.service_hooks.get("peers")
        if peer_name and peers is not None:
            await peers("register", name=peer_name, session_id=state.session.id)
        prompt = (first_message or "").strip()
        if prompt or copied:
            body = prompt or "Read your brief and the files in your inbox, then report in one message what you are set up to do."
            if copied:
                body += "\n\nFiles copied into your workspace inbox:\n" + "\n".join(f"- {c}" for c in copied)
            await self.manager.submit(state.session.id, body, [], as_answer=False, origin=f"spawn:{session_id}")
        loops = self.manager.service_hooks.get("loops")
        if loop and loops is not None:
            # The first iteration starts once any first message has been answered; the loop's own tick handles a busy session.
            await loops("create", session_id=state.session.id, instruction=str(loop.get("instruction") or ""), mode=str(loop.get("mode") or "interval"),
                        interval_seconds=loop.get("interval_seconds"), max_runs=loop.get("max_runs"), start_now=True)
        return state.session.id

    async def _service_progress(self, session_id: str, line: str) -> None:
        renderer = self._renderers.get(session_id)
        if renderer is not None:
            await renderer.progress(line)


def _cost_words(usd: float | None, unmetered: int) -> str:
    """Spend for a summary line: a priced total, and an honest count of calls with no known price."""
    if usd is None:
        return " · cost unknown (no price for these calls)" if unmetered else ""
    text = f" · ${usd:.4f}"
    if unmetered:
        text += f" (+{unmetered} unmetered call{'s' if unmetered != 1 else ''})"
    return text


def _unused(_: ReplyParameters | None = None) -> None:
    return None


__all__ = ["TelegramFront", "TelegramOutbox", "TopicBinding"]
