"""Talking to the agent in one language while it works in another.

The operator writes and reads in their own language; the agent, in a session switched to another one,
reads and writes only that. A separate model — the translator, a cheap one chosen in Settings — stands
between the two:

* **Outgoing.** The operator's message is translated before it reaches the agent, and only the
  translation is the message: the content the model reads, the words compaction quotes, the history
  the next run replays. The operator's own words ride beside it in the message's metadata
  (:data:`METADATA_KEY`), which the wire never sends, so the app draws them and the model never sees them.
* **Incoming.** A run's final answer — not its interim texts, its tool calls or its reasoning — is
  translated after the run ends, and the translation is written onto the transcript row alone. The
  transcript is the app's copy; the working history the model replays is a different table and keeps
  the original. Feeding the translation back would put two languages into one context and break the
  prompt cache at the first replay.
* **On request.** Any other text of the agent's — a note written between steps, or an answer in a
  session that translates nothing — is translated when the operator asks for it (:meth:`Translations.asked`),
  onto its transcript row the same way, whatever the session's own mode.
* **Questions.** An AskUser question is translated for display before it is announced; the answer the
  operator picks is still the original option, which is what the agent matches on. A side question left
  in the Questions tab is translated the same way once it is open (:meth:`Translations.ask`), and any
  question of the tab may be translated on request whatever its session's mode.

What must survive a translation byte for byte — code, paths, identifiers, addresses, the host's own
tags, quoted strings — is never shown to the translator at all: it is cut out and replaced by numbered
tokens (:func:`protect`), and a translation that loses a token is refused rather than trusted
(:func:`restore`). Every failure, a timeout included, falls back to the original with a mark; nothing
here ever stops a turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import Message, MessageRole, TextBlock, ToolUseBlock

from daedalus.host import prompts

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager, SessionState

logger = logging.getLogger(__name__)

LANGUAGES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "tr": "Turkish",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}
"""The languages offered, by code, with the English name the translator is told."""

METADATA_KEY = "daedalus.translation"
"""Message metadata: ``{direction, state, from, to, text?, error?}``. On an operator's message ``text`` is
their own words; on an answer it is the translation. Never read by the wire."""

ASK_KEY = "translation"
"""A question's detail in the Questions tab: ``{state, from, to, title, text, options, error?, asked?}``, the
words in the operator's language beside the agent's own, which stay the question and its options."""

SESSION_KEY = "agent_language"
"""Session metadata: the language the agent works in, ``""`` for none. Absent = the settings' default."""

PURPOSE = "translation"
"""The label the translator's calls are booked under in Usage, with the session they were made for."""

RESULTS_KEPT = 200
"""Held answers remembered for a front to collect; more than this only when no front collects them."""

RESULT_GRACE_SECONDS = 60
"""How much longer than the translator's own limit a front waits for a held answer before sending the original."""

SUMMARY_CHARS = 200
"""As much of the answer as a finished-run event carries, as the session manager's own summary does."""

TOKEN = "⟪{}⟫"
_TOKEN_RE = re.compile(r"⟪(\d+)⟫")

PROMPT = (
    "You are a translator between an operator and their AI agent. Translate the user's message from "
    "{source} into {target}. Keep the meaning, tone, Markdown structure, lists, headings and line breaks "
    "exactly. Tokens of the form ⟪0⟫, ⟪1⟫ … stand for code, paths, names and quoted text: copy every one "
    "of them unchanged, exactly once, in the place it belongs. Do not translate identifiers, commands or "
    "product names. If the text is already in {target}, return it unchanged. Answer with the translation "
    "only: no preface, no notes, no quotes around it."
)

_PROTECTED = re.compile(
    "|".join(
        (
            r"(?P<fence>^[ \t]*(?P<mark>`{3,}|~{3,})[^\n]*\n[\s\S]*?^[ \t]*(?P=mark)[ \t]*$)",  # a fenced block, whole
            r"`[^`\n]+`",  # inline code
            r"!\[[^\]\n]{0,500}\]\(daedalus-media:[0-9a-f-]{36}\)",  # an inline media reference
            r"«secret:[a-z][a-z0-9_]{0,47}»",  # a secret's placeholder
            r"</?[A-Za-z][\w:-]*(?:\s[^<>\n]*)?/?>",  # a tag: <file path="…"/>, </run>
            r"\[\[[^\]\n]+\]\]",  # a wiki-style reference
            r"\b(?:https?|ftp|file|ssh|git)://[^\s<>()\"'`]+[^\s<>()\"'`.,;:!?]",  # an address
            r"(?<![\w/])(?:~|\.{1,2})?/[\w.@+~-]+(?:/[\w.@+~-]*)+",  # a path with at least two parts
            r"\b[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_@+-]*\.[A-Za-z0-9]+\b",  # a relative path to a file: src/app.ts
            r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@+-]+){2,}/?",  # a relative path of three parts or more; "and/or" is two
            r"\b[A-Za-z]:\\[^\s\"'`]+",  # a Windows path
            r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?",  # an environment variable
            r"(?<![\w-])--?[A-Za-z][\w-]*(?:=[^\s]+)?",  # a command-line flag
            r"\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|json|md|toml|ya?ml|txt|sh|go|rs|html|css|sql|lock|cfg|ini|env|log|csv|db|sqlite|ipynb|c|h|cpp|java|kt|rb|php|swift)\b",  # a file name
            r"\b[A-Za-z_]\w+(?:\.[A-Za-z_]\w*)+(?:\(\))?",  # a dotted name: module.attribute, obj.method()
            r"\b[A-Za-z_]\w*\(\)",  # a call
            r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b",  # snake_case and UPPER_CASE
            r"\b[a-z]+[A-Z][A-Za-z0-9]*\b",  # camelCase
            r"\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b",  # PascalCase with two humps
            r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b",  # a commit or an id; a digit in it, or "defaced" would be one
            r"\"[^\"\n]{1,200}\"",  # a double-quoted string
            r"(?<!\w)'[^'\s]{1,100}'(?!\w)",  # a single-quoted word: 'main'
        )
    ),
    re.MULTILINE,
)


class TranslationFailed(RuntimeError):
    """The translator did not answer in time, refused, or lost a protected part; the original is used."""


def protect(text: str) -> tuple[str, list[str]]:
    """``text`` with every part that must survive verbatim replaced by a numbered token, and the parts.

    A token that the text itself already contains would be ambiguous on the way back, so such text is
    returned with nothing protected and the caller translates it as it is — the restore step then refuses
    any loss, which keeps the guarantee."""
    if _TOKEN_RE.search(text):
        return text, []
    kept: list[str] = []

    def keep(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return TOKEN.format(len(kept) - 1)

    return _PROTECTED.sub(keep, text), kept


def restore(translated: str, kept: list[str]) -> str:
    """Put the protected parts back; :class:`TranslationFailed` when any token is missing or repeated.

    A token lost is a code block or a path the operator would never see again in the agent's words; a
    token doubled is a block the translator invented. Either is worse than no translation."""
    found = [int(n) for n in _TOKEN_RE.findall(translated)]
    if sorted(found) != list(range(len(kept))):
        missing = sorted(set(range(len(kept))) - set(found))
        raise TranslationFailed(f"the translation lost or repeated protected text (missing {missing[:5]})" if missing else "the translation repeated protected text")
    return _TOKEN_RE.sub(lambda match: kept[int(match.group(1))], translated)


def has_words(masked: str) -> bool:
    """Whether anything is left to translate once the protected parts are tokens: a message that is only
    a code block or a path goes to the agent as it is, with no call made."""
    return any(ch.isalpha() for ch in _TOKEN_RE.sub("", masked))


def clean_answer(answer: str) -> str:
    """The translator's answer without what models add around it despite being told not to."""
    text = answer.strip()
    if text.startswith("<translation>") and text.endswith("</translation>"):
        text = text[len("<translation>") : -len("</translation>")].strip()
    return text


def agent_language(metadata: dict[str, Any], config: Any) -> str:
    """The language the agent works in for a session: its own choice, else the settings' default; ``""`` for none."""
    chosen = metadata.get(SESSION_KEY)
    if isinstance(chosen, str):
        return chosen if chosen in LANGUAGES else ""
    default = config.translation.agent_language
    return default if default in LANGUAGES else ""


def pair(metadata: dict[str, Any], config: Any) -> tuple[str, str] | None:
    """``(operator's language, agent's language)`` when the session's messages are translated, else ``None``."""
    agent = agent_language(metadata, config)
    operator = config.translation.operator_language
    if not agent or operator not in LANGUAGES or agent == operator:
        return None
    return operator, agent


def session_view(metadata: dict[str, Any], config: Any) -> dict[str, Any]:
    """What the composer's language chip draws for a session."""
    agent = agent_language(metadata, config)
    operator = config.translation.operator_language
    default = config.translation.agent_language if config.translation.agent_language in LANGUAGES else ""
    # ``default`` names what "follow the default" would pick, so the chip can say it while a session has its own.
    return {"agent": agent, "operator": operator, "active": pair(metadata, config) is not None, "explicit": isinstance(metadata.get(SESSION_KEY), str), "default": default}


def translator_preset(config: Any) -> str:
    """The translator's preset: the one chosen, else the helpers' cheap one (the next-message guess's, or
    a middle one of the table). Read from the configuration alone, so Settings can name it too."""
    wanted = config.translation.preset
    if wanted and wanted in config.presets:
        return wanted
    own = config.assist.predict_preset
    return own if own in config.presets else (config.middle_preset() or "")


def output_tokens(text: str) -> int:
    """Room for the translation: a script with longer words, or one that tokenises worse, needs more than
    the source took. A ceiling keeps one answer from asking a cheap model for its whole window."""
    return min(32_000, max(256, len(text) // 2 + 256))


def view_of(metadata: dict[str, Any], role: MessageRole, body: str) -> tuple[str, dict[str, Any] | None]:
    """What the app draws for a translated message: the text shown by default, and the ``translation`` field.

    An operator's message is shown in their words, with what the agent got as the alternate. An answer
    is shown translated once the translation is there, with the original as the alternate; while it is
    being made, or when it failed, the original is shown with the state saying why."""
    mark = metadata.get(METADATA_KEY)
    if not isinstance(mark, dict) or mark.get("direction") not in ("in", "out"):
        return body, None
    state = str(mark.get("state") or "")
    view: dict[str, Any] = {"direction": mark["direction"], "state": state, "from": str(mark.get("from") or ""), "to": str(mark.get("to") or "")}
    if mark.get("error"):
        view["error"] = str(mark["error"])[:300]
    other = mark.get("text")
    if not isinstance(other, str) or not other.strip():
        return body, view
    if mark["direction"] == "out" and role is MessageRole.user:
        # The operator's own words as written; the model read ``body``, which is offered as the alternate
        # only when it differs, i.e. when the translator really produced it.
        words = without_reply_quote(other, metadata)
        if state == "done":
            view["alternate"] = body
        return words, view
    if mark["direction"] == "in" and role is MessageRole.assistant and state == "done":
        view["alternate"] = body
        return other, view
    return body, view


async def _same(text: str) -> str:
    return text


def without_reply_quote(text: str, metadata: dict[str, Any]) -> str:
    """The operator's words without the quote of what they answer, which the message carries for the model."""
    if metadata.get("daedalus.reply_to") and text.startswith("[In reply to: «"):
        return text.split("\n", 1)[1] if "\n" in text else ""
    return text


@dataclass(slots=True)
class Outgoing:
    """An operator's message after the translator: what the agent gets, and the mark the message carries."""

    text: str
    mark: dict[str, Any] | None


def final_answer(messages: list[Message], run_id: str) -> Message | None:
    """The run's final answer among ``messages``: its last assistant message, when that one says something
    and asks for no tool. An interim text written between steps is not an answer and stays as it is."""
    for message in reversed(messages):
        if message.role is not MessageRole.assistant or message.metadata.get("daedalus.run_id") != run_id:
            continue
        if any(isinstance(block, ToolUseBlock) for block in message.content_blocks):
            return None
        return message if _answer_body(message)[0].strip() else None
    return None


def _answer_body(message: Message) -> tuple[str, str]:
    """The answer's words as the operator reads them, and the headline the host keeps beside them."""
    text = prompts.without_host_notes("".join(block.text for block in message.content_blocks if isinstance(block, TextBlock)))
    return prompts.split_headline(text)


def delivered_text(messages: list[Message], run_id: str) -> tuple[str, str] | None:
    """``(state, text)`` of a run's translated answer as a front delivers it: the translation when it is
    there, the original otherwise; ``None`` when the run left no answer that went through the translator."""
    answer = final_answer(messages, run_id)
    mark = answer.metadata.get(METADATA_KEY) if answer is not None else None
    if answer is None or not isinstance(mark, dict) or mark.get("direction") != "in":
        return None
    if mark.get("state") == "done" and isinstance(mark.get("text"), str):
        return "done", mark["text"]
    return str(mark.get("state") or ""), _answer_body(answer)[0].strip()


class Translations:
    """The translator of one installation, used by the session manager on the three paths above."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self._results: dict[str, asyncio.Future[tuple[str, str] | None]] = {}
        self._asking: set[tuple[str, int]] = set()

    def pair_of(self, state: SessionState) -> tuple[str, str] | None:
        """The languages a session's messages are translated between, or ``None``.

        Only a conversation the operator reads is translated. The settings' default applies to every
        session that never chose, and a subagent, a staff member or a side window answers another agent
        or a window over its chat: translating those would pay for words nobody reads, and would hand a
        leader its helper's report in a language the leader does not work in."""
        from daedalus.host.session_runner import NOT_OPERATOR_FACING  # Lazy: the session runner imports this module

        if not self.manager.config.presets or any(state.metadata.get(key) for key in NOT_OPERATOR_FACING):
            return None
        return pair(state.metadata, self.manager.config)

    def begin(self, run_id: str) -> None:
        """Say that the answer of ``run_id`` is being translated, before any front delivers it.

        A front that delivers answers (Telegram) asks :meth:`waits_for` when the run ends and, when it
        does, sends what :meth:`result` gives instead of the original. Registered synchronously, so no
        front can ask before it is known."""
        self._results[run_id] = asyncio.get_running_loop().create_future()
        while len(self._results) > RESULTS_KEPT:
            # A front that never asked (none is running) leaves its entry; the oldest go first.
            self._results.pop(next(iter(self._results)))

    def waits_for(self, run_id: str) -> bool:
        return run_id in self._results

    async def result(self, run_id: str) -> tuple[str, str] | None:
        """``(state, text)`` to deliver for a held answer once it is ready; ``None`` when there is none,
        or it did not come in time — the front then delivers the original it already has."""
        future = self._results.get(run_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.manager.config.translation.timeout_seconds + RESULT_GRACE_SECONDS)
        except TimeoutError:
            return None
        finally:
            self._results.pop(run_id, None)

    def _settle(self, run_id: str, value: tuple[str, str] | None) -> None:
        future = self._results.get(run_id)
        if future is not None and not future.done():
            future.set_result(value)

    async def translate(self, text: str, source: str, target: str, *, session_id: str) -> str:
        """``text`` in ``target``, its protected parts intact; :class:`TranslationFailed` on any failure."""
        masked, kept = protect(text)
        if not has_words(masked):
            return text
        manager = self.manager
        settings = manager.config.translation
        # An empty source is a text the operator asked about in a session that names no agent language: the
        # translator is left to recognise it, which a model does better than a guess made here would.
        instructions = PROMPT.format(source=LANGUAGES.get(source, source) if source else "the language it is written in", target=LANGUAGES.get(target, target))
        try:
            answer = await asyncio.wait_for(
                manager.small_model(masked, translator_preset(manager.config), purpose=PURPOSE, session_id=session_id, max_tokens=output_tokens(masked), temperature=0.0, instructions=instructions),
                timeout=settings.timeout_seconds,
            )
        except TimeoutError as exc:
            raise TranslationFailed(f"the translator did not answer within {settings.timeout_seconds} s") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a refusal, an outage, a budget: the original goes instead
            raise TranslationFailed(f"the translator failed: {type(exc).__name__}: {exc}"[:300]) from exc
        translated = clean_answer(answer)
        if not translated:
            raise TranslationFailed("the translator answered with nothing")
        return restore(translated, kept)

    async def outgoing(self, state: SessionState, text: str) -> Outgoing:
        """The operator's ``text`` as the agent is to get it.

        On a failure the agent gets the words as written and the message says so; the operator is not
        made to wait twice, and an agent that reads Russian badly still reads it."""
        languages = self.pair_of(state)
        if languages is None or not text.strip():
            return Outgoing(text, None)
        operator, agent = languages
        mark: dict[str, Any] = {"direction": "out", "from": operator, "to": agent, "text": text}
        try:
            translated = await self.translate(text, operator, agent, session_id=state.session.id)
        except TranslationFailed as exc:
            logger.warning("session %s: the operator's message goes untranslated: %s", state.session.id, exc)
            return Outgoing(text, {**mark, "state": "failed", "error": str(exc)})
        return Outgoing(translated, {**mark, "state": "done"})

    async def incoming(self, state: SessionState, run_id: str, finished: dict[str, Any], *, announce: bool = True) -> None:
        """Translate the run's final answer onto its transcript row, then say so on the bus.

        The row goes ``pending`` first, so the app shows "translating" under the original it already
        has; then ``done`` with the translation, or ``failed`` with the reason. ``answer.translated``
        carries the finished-run payload with the summary in the operator's language: the notification
        and Telegram wait for it instead of announcing the answer in a language the operator does not read.
        With ``announce`` false — a run woken by a job or a schedule, which no notification announces — the
        row is translated and nothing is said on the bus, so the translation adds no notice either.
        The event names the run, not the text — events carry references — and a front that delivers the
        answer reads it off the row (:func:`delivered_text`).
        """
        manager = self.manager
        outcome: dict[str, Any] = {**finished, "translation": "none"}
        delivered: tuple[str, str] | None = None
        try:
            delivered = await self._incoming(state, run_id, outcome)
        except Exception:  # noqa: BLE001 — whatever broke, the answer still goes out, untranslated
            logger.exception("translating the answer of run %s failed", run_id)
        finally:
            self._settle(run_id, delivered)
        if not announce:
            return
        try:
            await manager.bus.publish("answer.translated", outcome, **manager._event_ids(state))
        except Exception:  # noqa: BLE001 — the row holds the translation; a front that missed the event reads it
            logger.warning("could not announce the translated answer of run %s", run_id, exc_info=True)

    async def _incoming(self, state: SessionState, run_id: str, outcome: dict[str, Any]) -> tuple[str, str] | None:
        manager = self.manager
        session_id = state.session.id
        languages = self.pair_of(state)
        rows = await manager.sessions.list_transcript(session_id, limit=40)
        answer = final_answer(rows, run_id)
        if languages is not None and answer is not None:
            operator, agent = languages
            key = manager.sessions.transcript_key(answer)
            body, _headline = _answer_body(answer)
            mark: dict[str, Any] = {"direction": "in", "from": agent, "to": operator, "at": datetime.now(UTC).isoformat()}
            await self._mark(state, key, answer, {**mark, "state": "pending"})
            try:
                translated = await self.translate(body.strip(), agent, operator, session_id=session_id)
            except TranslationFailed as exc:
                logger.warning("session %s: the answer of run %s stays untranslated: %s", session_id, run_id, exc)
                await self._mark(state, key, answer, {**mark, "state": "failed", "error": str(exc)})
                outcome.update(translation="failed", summary=" ".join(body.split())[:SUMMARY_CHARS])
                return "failed", body.strip()
            await self._mark(state, key, answer, {**mark, "state": "done", "text": translated})
            outcome.update(translation="done", summary=" ".join(translated.split())[:SUMMARY_CHARS])
            return "done", translated
        return None

    async def asked(self, state: SessionState, seq: int) -> Message:
        """Translate one of the agent's texts into the operator's language because the operator asked to.

        Any text of the agent's in a conversation: a note between steps, which the automatic path leaves as
        it is, or an answer in a session that translates nothing. It goes onto the transcript row exactly
        as an automatic translation does — ``pending``, then ``done`` or ``failed`` — so the app draws it with
        the same "Show original"; the working history is not touched. Returns the row as it now stands.

        A text already translated, or being translated by an earlier click, is returned as it is: a second
        click, or a second window, must not pay for the same words twice. A failed one may be asked again.
        Raises :class:`LookupError` for a row the session does not hold and :class:`ValueError` for one that
        is not the agent's text."""
        manager = self.manager
        session_id = state.session.id
        key = (session_id, seq)
        # Claimed before the first await: two clicks that both read the row before either marked it would
        # both have paid the translator.
        busy = key in self._asking
        self._asking.add(key)
        try:
            rows = await manager.sessions.expand_transcript(session_id, seq, seq)
            if not rows:
                raise LookupError(f"no message {seq} in this session")
            message = rows[0][1]
            body = _answer_body(message)[0].strip() if message.role is MessageRole.assistant else ""
            if not body:
                raise ValueError("only a text the agent wrote can be translated")
            mark = message.metadata.get(METADATA_KEY)
            if busy or (isinstance(mark, dict) and mark.get("direction") == "in" and mark.get("state") == "done"):
                # Being translated, the row says ``pending`` and the click that started it is the one that waits.
                return message
            operator = manager.config.translation.operator_language
            if operator not in LANGUAGES:
                raise ValueError(f"the operator's language {operator!r} is not one the translator knows")
            # The session's agent language names the source when it has one; otherwise the translator is told
            # to recognise it, and a text already in the operator's language comes back as it is.
            source = agent_language(state.metadata, manager.config)
            source = "" if source == operator else source
            row_key = manager.sessions.transcript_key(message)
            base: dict[str, Any] = {"direction": "in", "from": source, "to": operator, "at": datetime.now(UTC).isoformat(), "asked": True}
            message = await self._mark(state, row_key, message, {**base, "state": "pending"})
            try:
                translated = await self.translate(body, source, operator, session_id=session_id)
            except TranslationFailed as exc:
                logger.warning("session %s: message %s stays untranslated: %s", session_id, seq, exc)
                return await self._mark(state, row_key, message, {**base, "state": "failed", "error": str(exc)})
            return await self._mark(state, row_key, message, {**base, "state": "done", "text": translated})
        finally:
            if not busy:
                self._asking.discard(key)

    async def ask(self, ask_id: str, *, asked: bool) -> dict[str, Any]:
        """Translate a question of the Questions tab — its title, its text and each option — into the
        operator's language, beside the agent's words in the request's detail (:data:`ASK_KEY`).

        ``asked`` is the operator's "Translate", which works in any session and on any open question;
        without it this is the automatic path for a side question of a translated session. The options
        keep their own words: an answer names the option the agent wrote, whatever the card showed. A
        question already translated, or being translated, is answered with what it has. Raises
        :class:`LookupError` for a question that is not open any more."""
        manager = self.manager
        key = ("ask", ask_id)
        busy = key in self._asking
        self._asking.add(key)
        try:
            found = await manager.asks.get(ask_id)
            if found is None or not found.open:
                raise LookupError(f"no open question {ask_id}")
            mark = found.detail.get(ASK_KEY)
            if busy or (isinstance(mark, dict) and mark.get("state") == "done"):
                return dict(mark) if isinstance(mark, dict) else {"state": "pending"}
            operator = manager.config.translation.operator_language
            if operator not in LANGUAGES:
                raise LookupError(f"the operator's language {operator!r} is not one the translator knows")
            state = await manager.get_state(found.session_id) if found.session_id else None
            source = agent_language(state.metadata, manager.config) if state is not None else ""
            source = "" if source == operator else source
            base: dict[str, Any] = {"from": source, "to": operator, **({"asked": True} if asked else {})}
            await self._mark_ask(found.id, {**base, "state": "pending"})
            options = [str(o) for o in found.detail.get("options") or [] if isinstance(o, str)]
            session_id = found.session_id or None
            try:
                # One call per part: a question's text runs over several lines, and numbering the parts in
                # one text, as the run's question does, would be parsed back wrongly from a model that
                # moved a line. A side question has at most a handful of short options.
                title, text, *labels = await asyncio.gather(*(
                    self.translate(part, source, operator, session_id=session_id or "") if part.strip() else _same(part)
                    for part in (found.title, found.text, *options)
                ))
            except TranslationFailed as exc:
                logger.warning("question %s stays untranslated: %s", found.id, exc)
                return await self._mark_ask(found.id, {**base, "state": "failed", "error": str(exc)})
            return await self._mark_ask(found.id, {**base, "state": "done", "title": title, "text": text, "options": labels})
        finally:
            if not busy:
                self._asking.discard(key)

    async def _mark_ask(self, ask_id: str, mark: dict[str, Any]) -> dict[str, Any]:
        """Write the mark into the question's detail and have the session's Questions tab read it again."""
        manager = self.manager
        current = await manager.asks.get(ask_id)
        if current is None or not current.open:
            return mark
        await manager.asks.revise(ask_id, title=current.title, text=current.text, detail={**current.detail, ASK_KEY: mark})
        if current.session_id:
            try:
                await manager.bus.publish("ask.side", {"change": "translated", "ask_ids": [ask_id]}, session_id=current.session_id)
            except Exception:  # noqa: BLE001 — the row holds it; a window that missed the event reads it on its next poll
                logger.warning("could not announce the translation of question %s", ask_id, exc_info=True)
        return mark

    async def answer_words(self, state: SessionState, words: str) -> str:
        """The operator's own words in an answer to a side question, as the agent is to read them: in its
        language when the session is translated, as written otherwise or when the translator fails."""
        languages = self.pair_of(state)
        if languages is None or not words.strip():
            return words
        operator, agent = languages
        try:
            return await self.translate(words, operator, agent, session_id=state.session.id)
        except TranslationFailed as exc:
            logger.warning("session %s: an answer to a side question goes untranslated: %s", state.session.id, exc)
            return words

    async def _mark(self, state: SessionState, key: str, message: Message, mark: dict[str, Any]) -> Message:
        """Write the mark onto the transcript row alone and have the app read the end of the chat again.

        The message is copied: the row read back is the transcript's, but a copy keeps even a cached one
        from sharing the dict with the working history, which must never carry the translation."""
        updated = message.model_copy(update={"metadata": {**{k: v for k, v in message.metadata.items() if k != "daedalus.seq"}, METADATA_KEY: mark}})
        await self.manager.sessions.replace_transcript_message(state.session.id, key, updated)
        await self.manager.announce_transcript(state.session.id)
        return updated

    async def question(self, state: SessionState, payload: dict[str, Any]) -> None:
        """Add each question's text and options in the operator's language, for display only.

        One call for the whole question, its options numbered in the text, so a question with five
        options costs one round trip and the options are translated in the question's context. Kept
        under ``translation`` on each question; the options keep their labels, which the answer names."""
        languages = self.pair_of(state)
        questions = [q for q in payload.get("questions") or [] if isinstance(q, dict)]
        if languages is None or not questions:
            return
        operator, agent = languages
        lines: list[str] = []
        for qi, question in enumerate(questions):
            lines.append(f"Q{qi}: {question.get('question') or ''}")
            for oi, option in enumerate(question.get("options") or []):
                if isinstance(option, dict):
                    lines.append(f"Q{qi}.{oi}: {option.get('label') or ''}")
                    if option.get("description"):
                        lines.append(f"Q{qi}.{oi}.d: {option.get('description')}")
        try:
            translated = await self.translate("\n".join(lines), agent, operator, session_id=state.session.id)
        except TranslationFailed as exc:
            logger.warning("session %s: the question stays untranslated: %s", state.session.id, exc)
            return
        found: dict[str, str] = {}
        for line in translated.splitlines():
            match = re.match(r"\s*(Q\d+(?:\.\d+)?(?:\.d)?)\s*:\s*(.*)$", line)
            if match:
                found[match.group(1)] = match.group(2).strip()
        for qi, question in enumerate(questions):
            options = [o for o in question.get("options") or [] if isinstance(o, dict)]
            question["translation"] = {
                "question": found.get(f"Q{qi}") or str(question.get("question") or ""),
                "options": [
                    {"label": found.get(f"Q{qi}.{oi}") or str(option.get("label") or ""), "description": found.get(f"Q{qi}.{oi}.d") or str(option.get("description") or "")}
                    for oi, option in enumerate(options)
                ],
            }


__all__ = [
    "ASK_KEY",
    "LANGUAGES",
    "METADATA_KEY",
    "PURPOSE",
    "SESSION_KEY",
    "Outgoing",
    "TranslationFailed",
    "Translations",
    "agent_language",
    "delivered_text",
    "final_answer",
    "pair",
    "protect",
    "restore",
    "session_view",
    "translator_preset",
    "view_of",
    "without_reply_quote",
]
