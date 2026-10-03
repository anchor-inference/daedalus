"""Application composition: stores → sessions → API (+ Telegram, scheduler, monitors).

Telegram is one front among the ways in, not the way in: with no bot token the same
installation runs on its API and its app alone. What the operator must hear about is posted to
:attr:`Application.notifications`, which records it whether or not there is a chat to say it in,
and :meth:`Application.create_session` falls back to a plain session when there is no front.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.notifications import Category, Draft, NotificationService, Tone
from daedalus.host.boot_guard import BootGuard
from daedalus.host.component_install import Installer
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.host.events import EventBus
from daedalus.host.session_runner import SessionManager, SessionState
from daedalus.providers.llamacpp import describe_discovery, discover_llamacpp
from daedalus.search.service import ConversationSearch
from daedalus.speech.service import LocalSpeech
from daedalus.speech.tts_service import LocalTts
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront

logger = logging.getLogger(__name__)

API_STOP_SECONDS = 10.0
"""How long a stop waits for the app's lifespan cleanup once the API server is down."""


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = RuntimeConfig.load(settings.config_path)
        self.db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
        self.manager: SessionManager | None = None
        self.front: TelegramFront | None = None
        self.background: list[asyncio.Task[None]] = []
        self.extensions: dict[str, object] = {}
        self.notifications: NotificationService | None = None
        """Where everything the operator must hear about is recorded; set when the extensions install."""
        self.extension_failures: dict[str, str] = {}
        """Subsystems that raised while installing, by name. Empty on a healthy start; see
        :data:`daedalus.extensions.FATAL` for why a failure here is survivable."""
        self.stopping = asyncio.Event()
        self._shut_down = False
        self._config_lock = asyncio.Lock()
        # The local speech models: a directory listing and a configuration read, no engine and no
        # model until something actually asks for words.
        self.search = ConversationSearch(self.db, settings.state_dir)
        self.speech = LocalSpeech(settings.state_dir, self.config)
        # And the voices it speaks with, on the same terms: a directory listing and a configuration
        # read, no engine and no voice until something actually asks to be heard.
        self.tts = LocalTts(settings.state_dir, self.config)
        # What the installation is missing and how a missing piece arrives. It is held here rather
        # than built per request because an install outlives the request that asked for it: the
        # headless browser takes minutes, and the page that started it may be reloaded meanwhile.
        self.components = Installer(settings)
        self.guard = BootGuard(settings.state_dir, window_minutes=self.config.ops.boot_loop_window_minutes, threshold=self.config.ops.boot_loop_threshold)

    @property
    def bus(self) -> EventBus:
        """The event bus; the same object as ``manager.bus``, which exists once the application started."""
        if self.manager is None:
            raise RuntimeError("the event bus exists once the application has started")
        return self.manager.bus

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        async with self._config_lock:
            old = self.config
            if expected_revision is not None and config_revision(old) != expected_revision:
                raise ConfigConflict(config_revision(old))
            config.save(self.settings.config_path)
            try:
                if self.manager is not None:
                    self.manager.reload_config(config)
            except BaseException:
                old.save(self.settings.config_path)
                if self.manager is not None:
                    self.manager.reload_config(old)
                raise
            self.config = config
            self.speech.config = config
            self.tts.config = config
            was = old.stt
            if (was.local_model, was.local_language, was.local_threads) != (config.stt.local_model, config.stt.local_language, config.stt.local_threads):
                # A different model, language or thread count is a different recogniser; the loaded one
                # is now the wrong one and holds most of a gigabyte while being it.
                self.speech.forget()
            spoke, now = old.voice.tts, config.voice.tts
            if (spoke.local_voice, spoke.local_threads) != (now.local_voice, now.local_threads):
                # A different voice or thread count is a different synthesiser. The speaker inside a
                # multi-voice model is not, and neither is the speed: both are arguments to every call,
                # so dropping a loaded model for either is a second of silence bought for nothing.
                self.tts.forget()

    async def start(self) -> None:
        self.guard.on_boot()
        await self.db.open()
        self.manager = SessionManager(self.settings, self.config, db=self.db)
        await self.manager.start()
        self.search.manager = self.manager
        self.search.task = asyncio.create_task(self.search.run(), name="conversation-index")
        await self._log_llamacpp_startup()
        if self.settings.telegram_bot_token and not self.settings.owner_user_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is set without OWNER_USER_ID: the bot would not know whose messages to answer")
        if self.settings.telegram_bot_token:
            self.front = TelegramFront(self.settings, self.config, self.manager, save_config=self.save_config, speech=self.speech)
        await self._install_extensions()
        # The voice the operator chose is built now rather than by the first answer they ask for.
        # It costs a second or two of a start that is already doing several, and it is the difference
        # between an answer that is read out as it is written and one that is read out after it.
        self.tts.warm()
        await self._report_startup()
        if self.guard.skip_recovery:
            note = (
                f"⚠️ {self.guard.unclean_boots} unclean restarts in a row: boot recovery (resuming runs, re-sending "
                "answers) is skipped this once so the bot stays up. Unfinished runs stay parked; /doctor shows them."
            )
            if self.front is not None:
                await self.front.notify(note, markdown=False)  # the chat is the alarm; the entry below is the record
            if self.notifications is not None:
                await self.notifications.post(Draft(
                    "system", "Boot recovery skipped after repeated crashes", note, kind="boot_guard", level="urgent", tone="error",
                    handled=frozenset({"telegram"}) if self.front is not None else frozenset(),
                ))
            if effects := self.extensions.get("effects"):
                effects.enable()
            return
        resumed = await self.manager.resume_unfinished()
        if resumed:
            await self.notice(f"Resumed {len(resumed)} run(s) after restart.", kind="startup", quiet=True)
        if stale := self.manager.stale_runs:
            # Natively the agent is a process under the launcher, so closing the window is a stop.
            # A run that was working when it happened is worth a line: the operator's question the
            # next morning is "what happened to it", and the answer belongs where they will look.
            why = (
                " Closing the app stops the agent with it — leave the window open while a run is working."
                if self.settings.native
                else ""
            )
            await self.notice(f"{len(stale)} run(s) were interrupted by the last stop and could not be resumed.{why}", kind="startup")
        if self.front is not None:
            resent = await self.front.redeliver_pending()
            if resent:
                await self.front.notify(f"Re-sent {resent} answer(s) the previous process had not confirmed as delivered.", markdown=False)
        if effects := self.extensions.get("effects"):
            effects.enable()

    async def _log_llamacpp_startup(self) -> None:
        """Record what each configured local server says before the first run needs it."""

        async def probe(provider_id: str, provider: Any) -> None:
            result = await discover_llamacpp(provider.base_url, provider.api_key)
            logger.info("llama.cpp provider %s: %s", provider.name or provider_id, describe_discovery(result))

        await asyncio.gather(
            *(probe(provider_id, provider) for provider_id, provider in self.config.providers.items() if provider.kind == "llamacpp")
        )

    async def notice(self, text: str, *, kind: str, tone: Tone = "info", quiet: bool = False, category: Category = "system") -> None:
        """Record a line about the installation itself; the notification preferences decide who else hears it.

        The first line is the title and the rest the body. Before the extensions are installed there
        is nowhere to record it, so it goes to the chat alone, or to the log when there is no chat.
        """
        if self.notifications is None:
            if self.front is not None:
                await self.front.notify(text, markdown=False)
            else:
                logger.warning("%s", text)
            return
        headline, _, body = text.partition("\n")
        await self.notifications.post(Draft(
            category, headline.strip().strip("*_ ") or kind, body.strip(), kind=kind, tone=tone,
            level="quiet" if quiet else None,
        ))

    async def create_session(self, title: str, *, metadata: dict[str, Any] | None = None, workspace: Path | None = None, project_id: str | None = None, own_directory: bool = False) -> SessionState:
        """A session with its chat topic where Telegram is configured, a plain session where it is not."""
        assert self.manager is not None
        if workspace is not None and project_id is None:
            project_id = (await self.manager.projects.adopt_directory(title, workspace)).id
        if self.front is None:
            return await self.manager.create_session(title, workspace=workspace, metadata=metadata, project_id=project_id, own_directory=own_directory)
        state, _binding = await self.front.create_session_topic(title, metadata=metadata, project_id=project_id, own_directory=own_directory)
        return state

    async def _report_startup(self) -> None:
        """Tell the operator about a failed rebuild or an exhausted budget."""
        failed = self.settings.state_dir / "good" / "FAILED"
        if failed.exists():
            text = failed.read_text(encoding="utf-8")
            await self.notice("❌ The last rebuild failed preflight and was rolled back:\n\n" + text[-3000:], kind="rebuild", tone="error")
            failed.rename(failed.with_suffix(".reported"))
        last = self.settings.state_dir / "good" / "LAST_REBUILD"
        if last.exists():
            await self.notice("🔄 " + last.read_text(encoding="utf-8").strip()[-1500:], kind="rebuild")
            last.rename(last.with_suffix(".reported"))
        if self.extension_failures:
            # The notifications already hold one entry per subsystem; this is the line in the chat, because
            # a bot that came up without its scheduler looks entirely healthy until something does not happen.
            broken = ", ".join(f"{name} ({reason.split(':')[0]})" for name, reason in self.extension_failures.items())
            await self.notice(f"⚠️ The bot started without {len(self.extension_failures)} subsystem(s): {broken}. The inbox has the error for each.", kind="extension", tone="error")
        exceeded = self.manager.budget_exceeded() if self.manager else None
        if exceeded:
            await self.notice(f"💸 Daily budget exceeded ({exceeded}). New runs are refused until tomorrow or /budget reset.", kind="budget", tone="warning", category="spend")

    async def _install_extensions(self) -> None:
        """Scheduler, balance monitor, self-development, API — each attaches here."""
        from daedalus.extensions import (
            install_all,  # Lazy: extensions import the Application type; a top-level import would be a cycle
        )

        assert self.manager is not None
        self.background.extend(await install_all(self))

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, *((signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ())):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except NotImplementedError:
                # Proactor loops on Windows have no add_signal_handler. CTRL_BREAK
                # reaches this process group when the launcher stops the stack.
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.stopping.set))

    async def run(self) -> None:
        if self.front is None:
            # Nothing to poll: the API, the scheduler and the loops carry the installation on their own.
            await self.stopping.wait()
            await self.shutdown()
            return
        polling = asyncio.create_task(self.front.start(), name="telegram-polling")
        stop_waiter = asyncio.create_task(self.stopping.wait(), name="stop-waiter")
        done, _ = await asyncio.wait({polling, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done and polling.exception() is not None:
            raise polling.exception()  # type: ignore[misc]
        await self.shutdown()
        if not polling.done():
            await polling

    async def _stop_api(self) -> None:
        """Stop the API server and then let the app's lifespan finish, before anything else is cancelled.

        The server is cancelled rather than asked to drain: the app holds event streams open for as
        long as it is looked at, and a drain would wait on them. Cancelled, the server never ran its
        lifespan's shutdown — the app's own cleanup was skipped, and the lifespan task, left waiting,
        was cancelled by the event loop at exit and printed "ERROR: Traceback … CancelledError" at
        the end of every stop.
        """
        server: Any = self.extensions.get("api_server")
        task = next((t for t in self.background if t.get_name() == "api-server" and not t.done()), None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        lifespan = getattr(server, "lifespan", None)
        if lifespan is None:
            return
        try:
            async with asyncio.timeout(API_STOP_SECONDS):
                await lifespan.shutdown()
        except TimeoutError:
            logger.warning("the app's lifespan did not finish within %.0f s", API_STOP_SECONDS)

    async def shutdown(self) -> None:
        """Stop everything; runs are drained before the transport closes so a finishing answer still reaches the chat."""
        if self._shut_down:
            return
        self._shut_down = True
        try:
            await self._stop_api()
            for task in self.background:
                task.cancel()
            await self.search.close()
            await self.speech.downloads.close()
            if self.manager is not None:
                await self.manager.close()
            if self.front is not None:
                await self.front.stop()
            await self.db.close()
        finally:
            self.guard.on_clean_shutdown()  # a deliberate stop is clean even when a step above failed


async def serve(settings: Settings) -> int:
    app = Application(settings)
    app.install_signal_handlers()
    try:
        await app.start()
        await app.run()
    except Exception:
        logger.exception("fatal")
        await app.shutdown()
        return 1
    return 0


__all__ = ["Application", "serve"]
