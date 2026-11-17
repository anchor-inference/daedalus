"""The translator's guarantees that do not need a model: what is protected, what a lost part does, and
how a translated message is drawn, queued, handed back and delivered to Telegram."""

from __future__ import annotations

from pathlib import Path

import pytest
from protocore.contracts.types import OPERATOR_WORDS_METADATA_KEY, Message, MessageRole, TextBlock, ToolUseBlock
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import TranslationConfig
from daedalus.host import translation
from daedalus.host.session_runner import _merged_words, _returned_message, _steer_cards
from daedalus.host.transcript_view import message_view
from daedalus.transport.telegram.render import RunRenderer, RunView
from tests.support.models import model_config
from tests.unit.test_telegram_render import FakeOutbox


def test_code_paths_names_and_addresses_never_reach_the_translator() -> None:
    text = (
        "Почини `parse_config` в src/app/config.py и /etc/hosts, см. https://example.com/a?b=1.\n"
        "```python\nprint('привет')\n```\n"
        "Запусти с флагом --verbose, $HOME, коммит abc1234d, строка \"как есть\", ветка 'main', obj.method() и fooBar."
    )
    masked, kept = translation.protect(text)
    for part in ("`parse_config`", "src/app/config.py", "/etc/hosts", "https://example.com/a?b=1", "```python\nprint('привет')\n```", "--verbose", "$HOME", "abc1234d", '"как есть"', "'main'", "obj.method()", "fooBar"):
        assert part in kept and part not in masked
    assert "Почини" in masked and "Запусти" in masked
    assert translation.restore(masked, kept) == text


def test_ordinary_words_stay_translatable() -> None:
    masked, kept = translation.protect("Use it e.g. here, and/or there; it was defaced. Это и/или то.")
    assert kept == []
    assert translation.has_words(masked)


def test_a_translation_that_loses_or_repeats_a_part_is_refused() -> None:
    masked, kept = translation.protect("Смотри `x_y` и src/a/b.py")
    with pytest.raises(translation.TranslationFailed):
        translation.restore(masked.replace("⟪1⟫", ""), kept)
    with pytest.raises(translation.TranslationFailed):
        translation.restore(masked + " ⟪0⟫", kept)


def test_text_that_is_only_code_needs_no_call() -> None:
    masked, _ = translation.protect("```\nls -la\n```")
    assert not translation.has_words(masked)


def test_the_pair_follows_the_session_then_the_default() -> None:
    config = model_config(translation=TranslationConfig(operator_language="ru", agent_language="en"))
    assert translation.pair({}, config) == ("ru", "en")
    assert translation.pair({translation.SESSION_KEY: ""}, config) is None
    assert translation.pair({translation.SESSION_KEY: "ru"}, config) is None  # the operator's own: nothing to do
    assert translation.pair({translation.SESSION_KEY: "xx"}, config) is None
    assert translation.session_view({translation.SESSION_KEY: "de"}, config) == {"agent": "de", "operator": "ru", "active": True, "explicit": True, "default": "en"}


def _user(text: str, mark: dict) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator", translation.METADATA_KEY: mark})


def test_the_app_shows_the_operators_words_and_offers_what_the_agent_got() -> None:
    view = message_view(_user("Fix it", {"direction": "out", "state": "done", "from": "ru", "to": "en", "text": "Почини"}))
    assert view["text"] == "Почини"
    assert view["translation"] == {"direction": "out", "state": "done", "from": "ru", "to": "en", "alternate": "Fix it"}


def test_the_app_shows_an_answer_translated_once_it_is_and_the_original_until_then() -> None:
    answer = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Done.")])
    assert message_view(answer)["translation"] is None
    answer.metadata[translation.METADATA_KEY] = {"direction": "in", "state": "pending", "from": "en", "to": "ru"}
    pending = message_view(answer)
    assert pending["text"] == "Done." and pending["translation"]["state"] == "pending"
    answer.metadata[translation.METADATA_KEY] = {"direction": "in", "state": "done", "from": "en", "to": "ru", "text": "Готово."}
    done = message_view(answer)
    assert done["text"] == "Готово." and done["translation"]["alternate"] == "Done."
    answer.metadata[translation.METADATA_KEY] = {"direction": "in", "state": "failed", "from": "en", "to": "ru", "error": "timeout"}
    failed = message_view(answer)
    assert failed["text"] == "Done." and failed["translation"]["error"] == "timeout"


def test_a_queued_message_shows_and_hands_back_the_operators_words() -> None:
    mark = {"direction": "out", "state": "done", "from": "ru", "to": "en", "text": "Почини"}
    item = {"id": "a", "text": "Fix it", "origin": "operator", "operator_words": ["Fix it"], "display": {translation.METADATA_KEY: mark}}
    assert _steer_cards([("follow_up", item)])[0]["text"] == "Почини"
    assert _returned_message(item)["text"] == "Почини"
    other = {**item, "id": "b", "text": "And test it", "operator_words": ["And test it"], "display": {translation.METADATA_KEY: {**mark, "text": "И проверь"}}}
    merged = _merged_words([item, other])
    # The model's side and the operator's side are joined the same way, so the row draws both.
    assert merged[translation.METADATA_KEY]["text"] == "Почини\n\nИ проверь"
    assert merged[OPERATOR_WORDS_METADATA_KEY] == ["Fix it", "And test it"]


def test_the_final_answer_is_the_last_one_and_never_an_interim_text() -> None:
    interim = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Looking"), ToolUseBlock(tool_call_id="c", name="Read", arguments_json="{}")], metadata={"daedalus.run_id": "r"})
    final = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Found it")], metadata={"daedalus.run_id": "r"})
    assert translation.final_answer([interim], "r") is None
    assert translation.final_answer([interim, final], "r") is final
    assert translation.final_answer([interim, final], "other") is None


async def test_telegram_holds_a_translated_answer_and_sends_the_translation(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    outbox.drafts = []
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(TurnEvent(type=EventType.MESSAGE_START, run_id="r1", payload={}))
    await renderer.handle(TurnEvent(type=EventType.CONTENT_BLOCK_DELTA, run_id="r1", payload={"delta": {"type": "text_delta", "text": "All done"}}))
    await renderer.handle(TurnEvent(type=EventType.MESSAGE_STOP, run_id="r1", payload={"stop_reason": "end_turn"}))
    await renderer.finish("completed", workspace=tmp_path, held=True)
    assert [t for t, md in outbox.sent if md] == []
    assert not any("without a reply" in t for t, _ in outbox.sent)
    await renderer.deliver_final("Всё готово", tmp_path)
    assert [t for t, md in outbox.sent if md] == ["Всё готово"]
