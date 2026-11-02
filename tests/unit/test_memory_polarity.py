"""A note and its negation are two facts, however alike their words and vectors: the polarity check,
and every "same fact" line drawn without a model refusing to merge the two. Every note is invented,
and no test reaches a network."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from protocore.contracts.memory import MemoryRecord, MemoryScope, MemoryWriteDecision

from daedalus.config import MemoryConfig
from daedalus.memory import curator as curator_module
from daedalus.memory import polarity, similarity
from daedalus.memory.curator import MemoryCurator, find
from daedalus.memory.similarity import Judge, Note, surely_same
from daedalus.stores.database import Database
from daedalus.stores.persistent import PersistentMemory

T = "t"

REVERSED = [
    ("Always answer in Russian", "Never answer in Russian"),
    ("Всегда отвечай на русском", "Никогда не отвечай на русском"),
    ("Отвечай на русском языке в рабочих чатах", "Не отвечай на русском языке в рабочих чатах"),
    ("Можно пушить в main без ревью", "Нельзя пушить в main без ревью"),
    ("Пушить в main разрешено", "Пушить в main запрещено"),
    ("Включить уведомления о сборках", "Выключить уведомления о сборках"),
    ("Enable notifications for builds", "Disable notifications for builds"),
    ("You can push to main", "You cannot push to main"),
    ("Should touch the production box", "Shouldn’t touch the production box"),
    ("The staging service is available", "The staging service is unavailable"),
    ("Сервер в офисе рабочий", "Сервер в офисе нерабочий"),
    ("Тесты перед коммитом обязательно", "Тесты перед коммитом необязательно"),
    ("Нужно отвечать по-русски", "Можно отвечать по-русски"),
    ("Не всегда отвечать по-русски", "Никогда не отвечать по-русски"),
    ("Не надо будить агента, ждущего скрипт", "Надо будить агента, ждущего скрипт"),
    ("Не стоит будить агента", "Стоит будить агента"),
    ("Отвечать без эмодзи", "Отвечать с эмодзи"),
    ("Never push to main, keep prod untouched", "Always push to main, keep prod untouched"),
    ("Не пушить в main, не трогать прод", "Пушить в main, трогать прод"),
]
AGREEING = [
    ("Do not use pip for the host", "Never use pip for the host"),
    ("Выключить уведомления о сборках", "Не включать уведомления о сборках"),
    ("Сервер в офисе не рабочий", "Сервер в офисе нерабочий"),
    ("Не пьёт ни чай, ни кофе по вечерам", "Не пьет ни чая, ни кофе по вечерам"),
    ("Prefers a dark theme in every editor", "The operator prefers a dark theme in every editor"),
    ("Deploys go through deploy.sh on Fridays", "deploys go through deploy.sh on Fridays only"),
    # Words that begin like a glued "не" and are not one, beside a note that says the remainder.
    ("Смотрит на небо каждую неделю", "Каждую неделю смотрит на небо"),
    ("Немного устаёт к концу недели", "Много устаёт к концу недели"),
    ("Некоторые модели дорогие", "Которые модели дорогие"),
    ("Несколько серверов в офисе", "Сколько серверов в офисе"),
    ("Недавно переехал в другой город", "Давно переехал в другой город"),
    # A negation about something the other note does not mention is no reversal of what both say.
    ("Шаблон CI для админов без внутренних IP и доменов", "Шаблон CI для админов без наших IP и доменов"),
    ("The account is someone; the other nickname has nothing to do with it", "The account is someone, the site is someone.example"),
]


@pytest.mark.parametrize(("a", "b"), REVERSED)
def test_a_reversal_differs_in_polarity_either_way_round(a: str, b: str) -> None:
    assert polarity.differ(a, b) and polarity.differ(b, a)


@pytest.mark.parametrize(("a", "b"), AGREEING)
def test_a_restatement_and_a_word_that_merely_starts_with_ne_do_not(a: str, b: str) -> None:
    assert not polarity.differ(a, b) and not polarity.differ(b, a)


def test_the_word_test_never_calls_a_note_and_its_negation_one_fact() -> None:
    # "не" is too short to be a stem, so the two have every stem in common.
    assert not surely_same("Не отвечай оператору на русском языке в рабочих чатах", "Отвечай оператору на русском языке в рабочих чатах")
    assert surely_same("Отвечай оператору на русском языке в рабочих чатах", "отвечай оператору на русском языке в рабочих чатах.")


def _at(cosine: float) -> list[float]:
    return [cosine, math.sqrt(1 - cosine * cosine)]


async def test_the_judge_puts_a_negation_to_the_model_and_takes_a_yes_as_a_changed_value() -> None:
    kept = Note("r:1", "Отвечай оператору на русском языке в рабочих чатах", "active")
    negated = "Не отвечай оператору на русском языке в рабочих чатах"
    asked: list[str] = []

    async def ask(system: str, user: str) -> str:
        asked.append(user)
        assert "opposite" in system
        return json.dumps({"answers": [{"item": 1, "same": 1, "values_differ": False}]})

    found = await Judge(ask=ask).twins([(negated, [kept])])
    assert len(asked) == 1 and f"NEW: {negated}" in asked[0]
    # The model said "same, nothing changed"; a reversal is a changed value all the same, so the
    # callers keep the newer statement instead of the longer text.
    assert found[0] is not None and found[0].how == "model" and found[0].values_differ
    # Without a model the two stay apart.
    assert await Judge().twins([(negated, [kept])]) == [None]


async def test_the_judge_does_not_decide_a_negation_by_cosine_and_still_decides_a_rewording() -> None:
    base, negated, reworded = "Answers in Russian at work", "Never answers in Russian at work", "Replies in Russian at work"
    table = {base: [1.0, 0.0], negated: _at(0.99), reworded: _at(similarity.COSINE_SURE + 0.02)}
    asked: list[str] = []

    async def vectors(texts: Any) -> list[list[float]]:
        return [table[t] for t in texts]

    async def ask(system: str, user: str) -> str:
        asked.append(user)
        return json.dumps({"answers": []})

    note = Note("r:1", base, "active")
    found = await Judge(ask=ask, vectors=vectors).twins([(negated, [note]), (reworded, [note])])
    assert found[0] is None and len(asked) == 1 and f"NEW: {negated}" in asked[0]
    assert found[1] is not None and found[1].how == "cosine"


def _record(record_id: str, text: str, *, age_days: int = 0) -> MemoryRecord:
    at = datetime(2026, 10, 20, tzinfo=UTC) - timedelta(days=age_days)
    return MemoryRecord(id=record_id, tenant_id=T, scope=MemoryScope.global_, scope_key="", text=text, kind="fact", created_at=at, updated_at=at)


LONG = "отвечать оператору на русском языке во всех рабочих чатах и в личных сообщениях кроме кода"


def test_the_background_pass_files_a_long_note_and_its_negation_as_a_contradiction_keeping_the_newer() -> None:
    # The negation is older and one word longer: the word line used to merge the two as a sure
    # duplicate and keep the longer text, turning the newer rule into its opposite.
    older, newer = _record("a", "Не " + LONG, age_days=3), _record("b", LONG.capitalize())
    assert curator_module._jaccard(curator_module.words_of(older.text), curator_module.words_of(newer.text)) >= curator_module.DUPLICATE_SURE
    found = find([older, newer])
    assert len(found) == 1
    assert found[0].kind == "contradiction" and not found[0].sure
    assert found[0].keep is newer and found[0].text == newer.text
    # The same through the vectors alone: a negation scores about 0.99.
    a, b = _record("a", "Keeps the staging box switched on overnight", age_days=1), _record("b", "Never keeps the staging box on at night")
    found = find([a, b], {"a": [1.0, 0.0], "b": _at(0.99)})
    assert len(found) == 1 and found[0].kind == "contradiction" and not found[0].sure and found[0].keep is b


def test_the_background_pass_still_merges_a_plain_duplicate() -> None:
    a, b = _record("a", LONG.capitalize(), age_days=1), _record("b", LONG.capitalize() + ", даже коротко")
    found = find([a, b])
    assert len(found) == 1 and found[0].kind == "duplicate" and found[0].sure
    a, b = _record("a", "Keeps bees behind the house", age_days=1), _record("b", "Owns three hives in the garden")
    found = find([a, b], {"a": [1.0, 0.0], "b": _at(curator_module.COSINE_SURE + 0.01)})
    assert len(found) == 1 and found[0].kind == "duplicate" and found[0].sure


@pytest.fixture
async def memory(db: Database) -> PersistentMemory:
    store = PersistentMemory(db)
    await store.load()
    return store


async def test_auto_merge_leaves_a_negation_to_the_operator(db: Database, memory: PersistentMemory) -> None:
    older = (await memory.write(T, MemoryScope.global_, "", "Не " + LONG, metadata={"topics": []}, similarity_threshold=1.1)).record
    stamp = datetime.now(UTC) - timedelta(days=3)
    await memory.restore(older.model_copy(update={"updated_at": stamp, "created_at": stamp}))
    newer = (await memory.write(T, MemoryScope.global_, "", LONG.capitalize(), metadata={"topics": []}, similarity_threshold=1.1)).record
    settings = MemoryConfig(auto_merge=True)
    curator = MemoryCurator(db, memory, T, lambda: settings)
    assert await curator.consolidate() == {"merged": 0, "suggested": 1}
    waiting = await curator.suggestions()
    assert waiting[0]["kind"] == "contradiction" and waiting[0]["keep_id"] == newer.id
    merged, _ = await curator.apply(waiting[0]["id"])
    assert [r.text for r in memory.records(T)] == [newer.text] and merged.id == newer.id


async def test_the_store_writes_a_reversal_as_a_record_of_its_own(memory: PersistentMemory) -> None:
    first = await memory.write(T, MemoryScope.global_, "", "Не " + LONG)
    # The store's own merge measures word share (0.85 by default) and keeps the longer text: the
    # correction would have vanished into the older, longer rule.
    second = await memory.write(T, MemoryScope.global_, "", LONG.capitalize())
    assert second.decision is MemoryWriteDecision.created and second.record.id != first.record.id
    assert {r.text for r in memory.records(T)} == {"Не " + LONG, LONG.capitalize()}
    # A restatement of the same polarity is still the store's to merge or skip.
    third = await memory.write(T, MemoryScope.global_, "", LONG.capitalize() + ".")
    assert third.decision is not MemoryWriteDecision.created
