"""An answer's addresses inside the app reach Telegram as links a tap opens, or as plain words without a public address."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from daedalus.transport.telegram.front import TelegramOutbox
from daedalus.transport.telegram.markdown import app_links

ROW = "/app/settings/voice#asr.transcriber"


def test_with_a_public_address_every_form_of_an_app_address_becomes_a_full_link() -> None:
    base = "https://agent.example.org/app/"
    assert app_links(f"[Распознавание речи]({ROW})", base) == f"[Распознавание речи](https://agent.example.org{ROW})"
    assert app_links(f"Open {ROW}.", base) == f"Open https://agent.example.org{ROW}."
    assert app_links(f"Settings: `{ROW}`", base) == f"Settings: https://agent.example.org{ROW}"
    keys = "/app/settings/models?tab=providers#providers.openrouter.api_key"
    assert app_links(f"({keys})", "https://agent.example.org") == f"(https://agent.example.org{keys})"


def test_without_one_a_link_keeps_its_words_and_a_bare_path_stays_text() -> None:
    assert app_links(f"Turn it on in [Voice & speech]({ROW}).", "") == "Turn it on in Voice & speech."
    assert app_links(f"Open {ROW}", "") == f"Open {ROW}"


def test_code_blocks_urls_and_lookalikes_are_left_alone() -> None:
    base = "https://agent.example.org"
    fenced = f"```\ncurl {ROW}\n```"
    assert app_links(fenced, base) == fenced
    assert app_links("see /application/x and /api/settings", base) == "see /application/x and /api/settings"
    assert app_links("https://other.example.org/app/settings", base) == "https://other.example.org/app/settings"
    assert app_links("[bad](/app/p/../api/settings)", base) == "[bad](/app/p/../api/settings)"


async def test_the_outbox_sends_the_rewritten_answer() -> None:
    sent: list[Any] = []

    class Bot:
        async def send_rich_message(self, chat_id: int, message: Any, **kw: Any) -> Any:
            sent.append(message.markdown)
            return SimpleNamespace(message_id=len(sent))

    await TelegramOutbox(Bot(), 1, None, app_base="https://agent.example.org").send_text(f"[here]({ROW})")  # type: ignore[arg-type]
    await TelegramOutbox(Bot(), 1, None).send_text(f"[here]({ROW})")  # type: ignore[arg-type]
    assert sent == [f"[here](https://agent.example.org{ROW})", "here"]
