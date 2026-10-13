"""The agent's documentation search answers how-to questions about Daedalus with the right place first.

The questions are the operator's kind, in both languages, and each names what must come first: a
settings row (``setting:<anchor>``), a settings section (``section:<id>``) or a page of the shipped
documentation (its page id). They run against this repository's own settings index and documentation,
so a row renamed without its index entry, or a page rewritten so it no longer answers, fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.host.agent_settings import describe_section
from daedalus.host.help_search import INDEX_FILE, help_index, parse_query, setting_link, stem
from daedalus.tools.docs import docs_search

REPO = Path(__file__).resolve().parents[2]

QUESTIONS: list[tuple[str, str]] = [
    # Speech recognition in the chat: the question the agent once answered with the voice page.
    ("Как мне включить STT?", "setting:asr.transcriber"),
    ("how do I enable speech to text in the chat", "setting:asr.transcriber"),
    ("как включить диктовку голосовых сообщений", "setting:asr.transcriber"),
    ("распознавание речи не работает, где это настроить", "setting:asr.transcriber"),
    ("voice notes are not transcribed, where is the setting", "setting:asr.transcriber"),
    ("отправлять расшифровку голосового сразу без подтверждения", "setting:asr.autosend"),
    ("where do I download a local speech recognition model", "setting:stt.models"),
    # Models.
    ("how to change the default model", "setting:model.preset"),
    ("как поменять модель по умолчанию", "setting:model.preset"),
    ("где ввести API ключ OpenRouter", "setting:providers.list"),
    ("add an API key for a provider", "setting:providers.list"),
    ("which model describes images and screenshots", "setting:vision.preset"),
    ("запасная модель если провайдер упал", "setting:model.chain"),
    ("модель оркестратора проекта", "setting:orchestrator.preset"),
    # Notifications.
    ("как включить тихие часы", "setting:notifications.quiet_hours"),
    ("do not disturb at night", "setting:notifications.quiet_hours"),
    ("turn on push notifications on this phone", "setting:notifications.push"),
    ("уведомления приходят дважды, и в телеграм и пушем", "setting:notifications.telegram_covers_push"),
    # Languages.
    ("как сменить язык интерфейса", "setting:language"),
    ("change the app language to Russian", "setting:language"),
    ("отвечай всегда по-английски, где это настроить", "setting:answer_language"),
    ("set the language the agent answers in", "setting:answer_language"),
    # Limits and the history.
    ("set a spend limit per run", "setting:limits.usd_per_run"),
    ("как ограничить общие расходы на модели", "setting:limits.usd_total"),
    ("when is the conversation history compacted", "setting:compaction.auto_ratio"),
    ("какая модель сжимает историю", "setting:compaction.preset"),
    ("max iterations per run", "setting:limits.max_iterations"),
    # Tools, the chat, the look.
    ("how do I choose the web search engine", "setting:tools.web.search.backend"),
    ("как добавить сервер MCP", "setting:mcp.servers"),
    ("как включить тёмную тему", "setting:appearance.theme"),
    ("Telegram topics instead of the private chat", "setting:telegram.mode"),
    ("change the voice that reads answers aloud", "setting:voice.tts.local_voice"),
    ("модель для голосового режима консьержа", "setting:voice.preset"),
    ("как поменять порядок пунктов в боковой панели", "setting:navigation.rail"),
    ("разрешить агенту менять свой код без моего подтверждения", "setting:self_change.approval"),
    ("how many browsers can run at once", "setting:browser.running_cap"),
    # Asked after the ranking was tuned on the ones above, and kept as they came.
    ("как отключить реакции в телеграме", "setting:telegram.reactions"),
    ("how do I make the font bigger", "setting:appearance.scale|setting:appearance.prose"),
    ("где настроить предупреждения о низком балансе", "setting:balance.thresholds_usd"),
    ("change how often the heartbeat runs", "setting:heartbeat.interval_minutes"),
    ("как подключить прокси для веб-запросов", "setting:tools.web.proxy"),
    ("record what the browser does", "setting:browser.record_frames"),
    ("сбросить счётчик потраченных денег", "setting:limits.total_since"),
    ("как выйти из аккаунта на этом браузере", "setting:logout"),
    ("how does voice mode work", "docs:voice"),
    # The documentation.
    # Two pages answer this one: the operator's guide, and the one written for an agent doing it for them.
    ("how to install Daedalus on a server", "docs:install|docs:agent-setup"),
    ("как установить десктопное приложение", "docs:install"),
    ("what is orchestration and how does the coordinator work", "docs:orchestration"),
    ("how do custom pages work", "docs:pages"),
    ("как дать агенту доступ к GitHub", "docs:github-access"),
    ("how do I sign in without Telegram", "docs:install"),
]


@pytest.fixture(scope="module")
def index():  # type: ignore[no-untyped-def]
    return help_index(REPO)


@pytest.mark.parametrize(("question", "want"), QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_the_right_place_comes_first(index, question: str, want: str) -> None:  # type: ignore[no-untyped-def]
    hits = index.search(question, 5)
    assert hits, f"nothing found for {question!r}"
    assert hits[0].entry.id in want.split("|"), f"{question!r}: {[(hit.entry.id, round(hit.score, 2)) for hit in hits]}"


def test_the_question_set_is_large_and_bilingual() -> None:
    russian = [q for q, _ in QUESTIONS if parse_query(q).russian]
    assert len(QUESTIONS) >= 25 and len(russian) >= 12 and len(QUESTIONS) - len(russian) >= 12


def test_words_are_reduced_so_that_inflected_russian_still_meets() -> None:
    assert stem("диктовку") == stem("диктовка") == stem("диктовки")
    assert stem("голосовые").startswith(stem("голос"))
    assert stem("notifications") == stem("notification")
    query = parse_query("Как мне включить STT?")
    assert query.terms == ("stt",) and query.intent and query.russian
    assert parse_query("как это сделать?").terms == ()


def test_a_russian_question_reaches_the_english_documentation_through_the_glossary() -> None:
    query = parse_query("как установить оркестрацию")
    assert "orchestration" in query.added or stem("orchestration") in query.added


def test_the_settings_index_is_whole_and_names_real_configuration() -> None:
    raw = json.loads((REPO / INDEX_FILE).read_text(encoding="utf-8"))
    sections = {section["id"] for section in raw["sections"]}
    anchors = [field["anchor"] for field in raw["fields"]]
    assert len(anchors) == len(set(anchors)), "an anchor names one row"
    known: set[str] = set()
    for name in ("model", "presets", "providers", "prompt", "vision", "tools", "mcp", "self_change", "limits", "balance", "scheduler",
                 "notifications", "compaction", "heartbeat", "asr", "stt", "voice", "terminals", "browser", "navigation", "telegram",
                 "orchestrator", "dispatcher", "action_rules", "assist", "answer_language"):
        known.update(describe_section(name))
    for field in raw["fields"]:
        assert field["section"] in sections, field["anchor"]
        assert field["title"]["en"] and field["title"]["ru"] and field["about"]["en"] and field["about"]["ru"], field["anchor"]
        assert field["synonyms"], field["anchor"]
        if field["config"]:
            assert field["config"] in known, f"{field['anchor']}: {field['config']} is not a configuration path"


def test_a_link_names_the_section_the_tab_and_the_row() -> None:
    assert setting_link("voice", "asr.transcriber") == "/app/settings/voice#asr.transcriber"
    assert setting_link("models", "providers.list", "providers") == "/app/settings/models?tab=providers#providers.list"
    assert setting_link("limits") == "/app/settings/limits"


@pytest.mark.asyncio
async def test_docs_search_answers_with_the_link_and_the_configuration_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    context = SimpleNamespace(metadata={})
    manager = SimpleNamespace(settings=SimpleNamespace(bot_repo_dir=REPO))
    services = SimpleNamespace(extra={"manager": manager}, max_tool_output_chars=60_000)
    monkeypatch.setattr("daedalus.tools.docs.services_for", lambda _: services)
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    found = await docs_search().invoke(context, {"query": "Как мне включить STT?"})
    first = found.content.splitlines()[0]
    assert "(/app/settings/voice#asr.transcriber)" in first and "`asr.transcriber`" in first
    assert first.index("Распознавание речи") < first.index("Speech recognition"), "a Russian question reads Russian first"
    assert found.metadata["settings"] >= 1
    english = await docs_search().invoke(context, {"query": "change the app language"})
    assert "only the operator changes it" in english.content.splitlines()[0]
