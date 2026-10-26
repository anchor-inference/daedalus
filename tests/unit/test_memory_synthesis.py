"""The background pass that learns from chats, against a scripted model: what it applies, what each
check refuses or holds back, when it runs, what it reads, and that one undo takes a whole pass back.

Every chat here is invented (someone, Beans, an imaginary project); no test reaches a network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMRequest, LLMResponse, LLMResponseUsage
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import MemoryConfig, Settings
from daedalus.extensions.memory import curator, synthesizer
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.memory import synthesis
from daedalus.memory.entries import LOCKED_KEY, entry_of
from daedalus.memory.synthesis import never_store, parse_ops
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.stores.database import Database
from tests.support.models import model_config

H = {"X-Daedalus-Token": "tok"}


class FakeModel:
    """A provider whose every answer is the next scripted text (or exception), keeping what it was sent.
    The follow-up call that writes entry summaries is answered from its own script (``summaries``) and
    kept apart, so a test of the pass's operations counts only those calls."""

    def __init__(self, *answers: Any, cost: float = 0.0, summaries: list[Any] | None = None) -> None:
        self.answers = list(answers)
        self.summaries = list(summaries or [])
        self.requests: list[LLMRequest] = []
        self.summary_requests: list[LLMRequest] = []
        self.cost = cost

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        if any(b.text == synthesis.SUMMARY_PROMPT for m in request.messages for b in m.content_blocks if isinstance(b, TextBlock)):
            self.summary_requests.append(request)
            answer = self.summaries.pop(0) if self.summaries else {"summaries": []}
        else:
            self.requests.append(request)
            answer = self.answers.pop(0) if self.answers else {"ops": []}
        if isinstance(answer, BaseException):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)]), stop_reason=StopReason.end_turn,
            usage=LLMResponseUsage(input_tokens=10, output_tokens=5, response_cost_usd=self.cost),
        )

    def prompt(self, index: int = -1) -> str:
        return "\n".join(b.text for m in self.requests[index].messages for b in m.content_blocks if isinstance(b, TextBlock))


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    m = SessionManager(settings, model_config(), db=db)
    await m.start()
    yield m
    await m.close()


@pytest.fixture
async def app(settings: Settings, db: Database, manager: SessionManager) -> Any:
    return SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session, notifications=None)


@pytest.fixture
async def client(app: Any) -> Any:
    from daedalus.extensions.api import build_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c


def fake(manager: SessionManager, *answers: Any, cost: float = 0.0, summaries: list[Any] | None = None) -> FakeModel:
    model = FakeModel(*answers, cost=cost, summaries=summaries)
    manager.providers.rungs_for = lambda config, preset_id=None: [(model, "fake-model")]  # type: ignore[method-assign]
    return model


_clock = [datetime(2026, 1, 1, tzinfo=UTC)]


def _message(role: MessageRole, text: str, origin: str | None) -> Message:
    _clock[0] += timedelta(seconds=1)
    return Message(role=role, content_blocks=[TextBlock(text=text)], created_at=_clock[0], metadata={"daedalus.origin": origin} if origin else {})


async def chat(manager: SessionManager, title: str, said: list[tuple[str, str]], *, metadata: dict[str, Any] | None = None, quiet_minutes: float = 60) -> str:
    """A session with a transcript: ``said`` is ``(who, text)`` with who in operator / agent / tool / schedule."""
    state = await manager.create_session(title, metadata=metadata or {})
    messages = []
    for who, text in said:
        if who == "operator":
            messages.append(_message(MessageRole.user, text, "operator"))
        elif who == "schedule":
            messages.append(_message(MessageRole.user, text, "schedule"))
        elif who == "tool":
            messages.append(_message(MessageRole.tool, text, None))
        else:
            messages.append(_message(MessageRole.assistant, text, None))
    await manager.sessions.append_transcript(state.session.id, messages)
    at = (datetime.now(UTC) - timedelta(minutes=quiet_minutes)).isoformat()
    await manager.db.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (at, state.session.id))
    return state.session.id


async def seqs(db: Database, session_id: str) -> list[int]:
    rows = await db.fetchall("SELECT seq FROM transcript WHERE session_id = ? AND json_extract(message, '$.metadata.\"daedalus.origin\"') = 'operator' ORDER BY seq", (session_id,))
    return [int(r["seq"]) for r in rows]


async def watermark(db: Database, session_id: str) -> tuple[int, str]:
    row = await db.fetchone("SELECT through_seq, status FROM memory_synthesis WHERE session_id = ?", (session_id,))
    return (int(row["through_seq"]), str(row["status"])) if row else (0, "")


async def last_seq(db: Database, session_id: str) -> int:
    return int((await db.fetchone("SELECT max(seq) m FROM transcript WHERE session_id = ?", (session_id,)))["m"])


def texts(manager: SessionManager) -> dict[str, str]:
    """Every record's text by the slug of its entry (one text per slug is enough for these tests)."""
    found: dict[str, list[str]] = {}
    for record in manager.memory.records(TENANT):
        found.setdefault(entry_of(record), []).append(record.text)
    return {k: " | ".join(sorted(v)) for k, v in found.items()}


async def entry_texts(manager: SessionManager, slug: str, section: str = "topic") -> list[str]:
    entry = await manager.memory_entries.by_slug(section, slug)
    return sorted(r.text for r in manager.memory_entries.records_of(entry.id)) if entry else []


async def sweep(app: Any, trigger: str = "manual") -> dict[str, Any]:
    learning = synthesizer(app)
    return await learning.sweep(await learning.candidates(), trigger)


# -- what a pass applies -------------------------------------------------------------------------


async def test_a_pass_files_what_the_operator_said_with_its_sources_and_moves_the_watermark(app: Any, manager: SessionManager, db: Database) -> None:
    sid = await chat(manager, "dog chat", [
        ("operator", "From now on, always answer in Russian."), ("agent", "Done."),
        ("operator", "My dog is called Beans and I build a garden planner called Sprout."), ("agent", "Nice."),
    ])
    first, second = await seqs(db, sid)
    model = fake(manager, {"ops": [
        {"op": "new_entry", "section": "topic", "slug": "pets", "title": "Pets", "summary": "The operator's animals"},
        {"op": "add", "entry": "topic/pets", "text": "Has a dog called Beans", "seqs": [second], "subjects": []},
        {"op": "add", "entry": "you/preferences", "text": "Always answer in Russian", "seqs": [first], "quote": "always answer in Russian"},
        {"op": "add", "entry": "you/profile", "text": "Builds a garden planner called Sprout", "seqs": [second]},
        {"op": "summary", "entry": "you/profile", "text": "Builds a garden planner"},
    ]})
    report = await sweep(app)
    assert report["chats"] == 1 and report["added"] == 3 and report["new_entries"] == 1 and report["summaries"] == 1
    assert await entry_texts(manager, "pets") == ["Has a dog called Beans"]
    assert await entry_texts(manager, "preferences", "you") == ["Always answer in Russian"]
    assert (await manager.memory_entries.by_slug("you", "profile")).summary == "Builds a garden planner"
    sources = await db.fetchall("SELECT * FROM memory_sources WHERE origin = 'synthesis' ORDER BY from_seq")
    assert {r["session_id"] for r in sources} == {sid} and len(sources) == 3
    preference = next(r for r in sources if r["quote"])
    assert preference["quote"] == "always answer in Russian" and preference["from_seq"] == first
    assert await watermark(db, sid) == (await last_seq(db, sid), "idle")
    history = await db.fetchone("SELECT actor, action, summary FROM memory_history ORDER BY id DESC LIMIT 1")
    assert history["actor"] == "synthesis" and history["action"] == "learn" and "dog chat" in history["summary"]
    # The call: the benchmarked prompt, thinking off, JSON asked for, booked as memory.
    request = model.requests[0]
    assert request.messages[0].role is MessageRole.system and "long-term memory about one person" in model.prompt(0)
    assert request.extra["json_object"] and request.extra["enable_thinking"] is False and request.temperature == 0.1
    assert request.observability.call_purpose == "memory" and request.observability.call_category == "memory"
    # Nothing new: a second sweep reads nothing and asks nothing.
    again = await sweep(app)
    assert again["chats"] == 0 and len(model.requests) == 1


async def test_the_model_reads_only_the_operator_and_the_final_answer_never_tools_or_schedules(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "build", [
        ("operator", "Please set up the backup job."),
        ("agent", "Let me look at the disk first."), ("tool", "TOOL-OUTPUT-SECRET-DIR listing"), ("agent", "Now writing the unit."),
        ("agent", "The backup runs nightly. ⟦ backup | done | job ⟧"),
        ("operator", "Thanks, keep backups for a month."),
        ("schedule", "SCHEDULED-TICK check the backup"), ("agent", "Checked."),
    ])
    model = fake(manager, {"ops": []})
    await sweep(app)
    prompt = model.prompt(0)
    assert "Please set up the backup job." in prompt and "keep backups for a month" in prompt
    assert "The backup runs nightly." in prompt and "⟦" not in prompt
    assert "TOOL-OUTPUT-SECRET-DIR" not in prompt and "Let me look at the disk" not in prompt and "Now writing the unit" not in prompt
    assert "SCHEDULED-TICK" not in prompt


async def test_an_invalid_answer_gets_one_corrective_retry_and_then_applies(app: Any, manager: SessionManager, db: Database) -> None:
    sid = await chat(manager, "c", [("operator", "I play the cello."), ("operator", "Every Sunday.")])
    model = fake(manager, 'Sure! {"ops": [{"op": "add"}]}', {"ops": [{"op": "add", "entry": "topic/music", "text": "Plays the cello on Sundays"}]})
    report = await sweep(app)
    # Two calls for the operations, and the follow-up that summarises the entry the pass made.
    assert report["calls"] == 3 and len(model.requests) == 2 and report["added"] == 1
    assert "not a valid operations object" in model.prompt(1)
    assert await watermark(db, sid) == (await last_seq(db, sid), "idle")


# -- what the checks refuse ----------------------------------------------------------------------


async def test_a_preference_without_the_operators_words_is_never_written(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "c", [("operator", "Write the report about the garden."), ("operator", "Good, send it.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "you/preferences", "text": "Prefers short answers"},
        {"op": "add", "entry": "you/preferences", "text": "Likes one-word replies", "quote": "I love one-word answers"},
    ]})
    report = await sweep(app)
    assert report["added"] == 0 and report["dropped"] == {"no_quote": 1, "bad_quote": 1}
    assert await entry_texts(manager, "preferences", "you") == []


async def test_sensitive_subjects_are_dropped_unless_allowed_and_the_models_label_counts(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "c", [("operator", "I have diabetes and my rent is going up."), ("operator", "Ok.")])
    answer = {"ops": [
        {"op": "add", "entry": "you/profile", "text": "Has diabetes and takes insulin"},
        {"op": "add", "entry": "you/profile", "text": "Rent is going up", "subjects": ["finances"]},
    ]}
    fake(manager, answer)
    report = await sweep(app)
    assert report["added"] == 0 and report["dropped"] == {"sensitive": 2} and report["subjects"] == {"health": 1, "finances": 1}
    manager.config.memory = MemoryConfig(sensitive_allowed=["finances"])
    sid = await chat(manager, "c2", [("operator", "I have diabetes and my rent is going up."), ("operator", "Ok.")])
    fake(manager, answer)
    report = await sweep(app)
    assert report["added"] == 1 and await entry_texts(manager, "profile", "you") == ["Rent is going up"]
    assert sid


async def test_what_is_never_stored_is_dropped_even_with_every_subject_allowed(app: Any, manager: SessionManager, db: Database) -> None:
    manager.config.memory = MemoryConfig(sensitive_allowed=["health", "finances", "intimate", "identity_documents", "precise_location"])
    await chat(manager, "c", [("operator", "I applied for a residence permit; the site password: 482910kx. My tax id ИНН 123456789012."), ("operator", "ok")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "you/profile", "text": "Applied for a residence permit"},
        {"op": "add", "entry": "topic/sites", "text": "The site password: 482910kx"},
        {"op": "add", "entry": "you/profile", "text": "ИНН 123456789012"},
    ]})
    report = await sweep(app)
    assert report["added"] == 0 and report["dropped"] == {"never_store": 3}
    assert manager.memory.records(TENANT) == []


async def test_an_entry_a_never_stored_subject_was_found_in_takes_nothing_more_from_the_pass(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "c", [("operator", "My appeal against the refused residence permit goes to the police first."), ("operator", "Then the second office.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "area/appeal", "text": "The appeal goes to the police first, the second office after"},
        {"op": "add", "entry": "area/appeal", "text": "Appeals a refused residence permit"},
        {"op": "add", "entry": "you/profile", "text": "Writes appeals in person"},
    ]})
    report = await sweep(app)
    assert report["dropped"] == {"never_store": 1, "tainted": 1} and report["added"] == 1
    assert await manager.memory_entries.by_slug("area", "appeal") is None


def test_the_never_store_patterns() -> None:
    assert never_store("Оспаривает отказ в постоянном проживании") == ["immigration"]
    assert never_store("Оформляет бизнес-РВП в другой стране") == ["immigration"]
    assert never_store("has a criminal record") == ["criminal_record"]
    assert never_store("Справка о несудимости готова") == ["criminal_record"]
    assert never_store("пароль поставь 482910") == ["secret"]
    assert never_store("Password: hunter22x") == ["secret"]
    assert never_store("token is <redacted>") == ["redacted"]
    assert never_store("ИИН 990101300123") and never_store("id 123456789012") == ["government_id"]
    assert never_store("The archive is password-protected") == []
    assert never_store("Uses Postgres 16 for the planner") == []


async def test_a_note_the_operator_removed_is_not_learned_again(app: Any, manager: SessionManager, db: Database) -> None:
    await manager.memory_entries.tombstone("Has a dog called Beans", reason="deleted")
    await chat(manager, "c", [("operator", "My dog Beans says hi."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/pets", "text": "has a dog called Beans."}]})
    report = await sweep(app)
    assert report["added"] == 0 and report["dropped"] == {"tombstoned": 1}
    assert await manager.memory_entries.by_slug("topic", "pets") is None


async def test_an_identifier_the_operator_never_wrote_is_held_as_a_suggestion_and_can_be_applied(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    await chat(manager, "c", [("operator", "Set up the planner server for Sprout."), ("operator", "Use the usual stack.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "area/sprout", "text": "Sprout server listens on 0.0.0.0:8102 with SPROUT_TOKEN"},
        {"op": "add", "entry": "area/sprout", "text": "Sprout has a planner server"},
    ]})
    report = await sweep(app)
    assert report["added"] == 1 and report["held"] == 1
    assert await entry_texts(manager, "sprout", "area") == ["Sprout has a planner server"]
    listed = (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]
    held = next(s for s in listed if s["kind"] == "addition")
    assert held["action"] == "add" and "8102" in held["text"]
    applied = await client.post(f"/api/memory/suggestions/{held['id']}/apply", json={}, headers=H)
    assert applied.status_code == 200, applied.text
    assert len(await entry_texts(manager, "sprout", "area")) == 2


async def test_a_locked_bullet_is_never_changed_only_proposed(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    profile = await manager.memory_entries.ensure(synthesis.PROFILE)
    mine, _ = await curator(app).add("Lives in a small town", kind="person", topics=[], entry=profile)
    assert mine.record.metadata.get(LOCKED_KEY)
    await chat(manager, "c", [("operator", "I moved to a big city last month."), ("operator", "ok")])
    model = fake(manager, {"ops": [{"op": "update", "record_id": "b1", "text": "Lives in a big city"}]})
    report = await sweep(app)
    assert "b1: Lives in a small town [locked]" in model.prompt(0)
    assert report["updated"] == 0 and report["held"] == 1
    assert manager.memory.get_record(TENANT, mine.record.id).text == "Lives in a small town"
    listed = (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]
    revision = next(s for s in listed if s["kind"] == "revision")
    assert revision["memory_ids"] == [mine.record.id] and revision["text"] == "Lives in a big city" and revision["action"] == "update"
    assert (await client.post(f"/api/memory/suggestions/{revision['id']}/apply", json={}, headers=H)).status_code == 200
    changed = manager.memory.get_record(TENANT, mine.record.id)
    assert changed.text == "Lives in a big city" and changed.metadata.get(LOCKED_KEY)


# -- the watermark, the budget, the triggers -------------------------------------------------------


async def test_the_watermark_moves_only_after_a_successful_apply(app: Any, manager: SessionManager, db: Database) -> None:
    sid = await chat(manager, "c", [("operator", "I play the cello."), ("operator", "Every Sunday.")])
    fake(manager, RuntimeError("provider down"))
    report = await sweep(app)
    assert await watermark(db, sid) == (0, "error") and report["errors"] and "provider down" in report["errors"][0]["error"]
    fake(manager, "not json", "still not json")
    report = await sweep(app)
    assert await watermark(db, sid) == (0, "error") and "not valid operations twice" in report["errors"][0]["error"]
    fake(manager, {"ops": [{"op": "add", "entry": "topic/music", "text": "Plays the cello on Sundays"}]})
    await sweep(app)
    assert await watermark(db, sid) == (await last_seq(db, sid), "idle")
    assert await entry_texts(manager, "music") == ["Plays the cello on Sundays"]


async def test_a_long_chat_is_read_in_parts_and_a_failed_part_keeps_the_earlier_ones(app: Any, manager: SessionManager, db: Database) -> None:
    said = [("operator", f"Fact number {i}: " + "x" * 1100) for i in range(60)]
    sid = await chat(manager, "long", said)
    fake(manager, {"ops": [{"op": "add", "entry": "topic/numbers", "text": "Counts facts"}]}, RuntimeError("down"))
    report = await sweep(app)
    through, status = await watermark(db, sid)
    assert report["parts"] == 1 and status == "error"
    assert 0 < through < await last_seq(db, sid)
    assert await entry_texts(manager, "numbers") == ["Counts facts"]


async def test_the_daily_budget_stops_the_pass_before_a_paid_call(app: Any, manager: SessionManager, db: Database) -> None:
    manager.config.memory = MemoryConfig(synthesis_usd_per_day=0.5)
    sid = await chat(manager, "c", [("operator", "I play the cello."), ("operator", "Every Sunday.")])
    await db.execute("INSERT INTO usage_events(at, provider_id, model, purpose, cost_usd, raw) VALUES (?, 'p', 'm', 'memory', 0.6, '{}')", (datetime.now(UTC).isoformat(),))
    model = fake(manager, {"ops": []})
    report = await sweep(app)
    assert report["stopped"] == "budget" and model.requests == []
    assert (await watermark(db, sid))[0] == 0
    # A free model is not stopped by it.
    synthesizer(app).costs_nothing = lambda provider_id, model_name: True
    report = await sweep(app)
    assert report["stopped"] == "" and len(model.requests) == 1


async def test_a_quiet_chat_is_read_after_ten_minutes_and_two_messages(app: Any, manager: SessionManager, db: Database) -> None:
    learning = synthesizer(app)
    await db.kv_set(synthesis.NIGHTLY_KEY, datetime.now(learning.zone()).date().isoformat())
    busy = await chat(manager, "busy", [("operator", "One"), ("operator", "Two")], quiet_minutes=5)
    quiet = await chat(manager, "quiet", [("operator", "One"), ("operator", "Two")], quiet_minutes=11)
    single = await chat(manager, "single", [("operator", "Only one")], quiet_minutes=30)
    old = await chat(manager, "old", [("operator", "One"), ("operator", "Two")], quiet_minutes=60 * 24 * 5)
    model = fake(manager, {"ops": []}, {"ops": []}, {"ops": []}, {"ops": []})
    report = await learning.tick()
    assert report is not None and report["trigger"] == "idle" and len(model.requests) == 1
    assert (await watermark(db, quiet))[1] == "idle"
    for sid in (busy, single, old):
        assert await watermark(db, sid) == (0, "")
    # A run in progress is never read; the operator's "update now" reads the rest, one message too.
    learning.running = lambda session_id: session_id == busy
    report = await learning.sweep(await learning.candidates(), "manual")
    assert report["chats"] == 2 and (await watermark(db, single))[1] == "idle" and (await watermark(db, busy))[0] == 0


async def test_the_nightly_sweep_runs_once_a_night_but_not_on_the_first_start(app: Any, manager: SessionManager, db: Database) -> None:
    learning = synthesizer(app)
    await chat(manager, "old", [("operator", "One")], quiet_minutes=60 * 24 * 5)
    model = fake(manager, {"ops": []}, {"ops": []})
    night = datetime(2026, 3, 2, 4, 0, tzinfo=UTC)
    assert await learning.tick(night) is None and model.requests == []
    assert await learning.tick(night + timedelta(days=1)) is not None and len(model.requests) == 1
    assert await learning.tick(night + timedelta(days=1, minutes=5)) is None


async def test_pause_marks_chats_read_and_refuses_the_agents_remember_and_recall(app: Any, manager: SessionManager, db: Database) -> None:
    manager.config.memory = MemoryConfig(paused=True)
    sid = await chat(manager, "c", [("operator", "My dog is Beans."), ("operator", "ok")])
    model = fake(manager, {"ops": [{"op": "add", "entry": "topic/pets", "text": "Has a dog called Beans"}]})
    assert await synthesizer(app).tick() is None
    assert model.requests == [] and await watermark(db, sid) == (await last_seq(db, sid), "paused")
    manager.config.memory = MemoryConfig()
    assert (await sweep(app))["chats"] == 0 and model.requests == []
    # The agent's own saves stop while paused; Forget does not.
    manager.config.memory = MemoryConfig(paused=True)
    state = await manager.get_state(sid)
    assert not manager.memory_block_wanted(state)
    context = ToolContext(session_id=sid, run_id="r", tenant_id=TENANT, metadata={"tool_call_id": "c1"})
    remember, recall, forget = (manager.tools.get(name) for name in ("Remember", "Recall", "Forget"))
    for tool, arguments in ((remember, {"text": "The bakery opens at seven."}), (recall, {"query": "bakery"})):
        result = await tool.invoke(context, arguments)  # type: ignore[union-attr]
        assert result.is_error and result.metadata["refused"] == "memory_paused" and "paused" in result.content
    assert (await forget.invoke(context, {"memory_id": "none"})).metadata.get("refused") != "memory_paused"  # type: ignore[union-attr]
    assert manager.memory.records(TENANT) == []


# -- which chats ---------------------------------------------------------------------------------


async def test_only_the_operators_own_chats_are_read_imported_ones_included(app: Any, manager: SessionManager, db: Database) -> None:
    said = [("operator", "One"), ("operator", "Two")]
    mine = await chat(manager, "mine", said)
    imported = await chat(manager, "imported", said, metadata={"imported": {"harness": "claude", "ext_id": "x"}})
    excluded = [
        await chat(manager, "private", said, metadata={"private": True}),
        await chat(manager, "staff", said, metadata={"staff_session_id": "st1"}),
        await chat(manager, "member", said, metadata={"staff_id": "m1"}),
        await chat(manager, "subagent", said, metadata={"subagent_of": mine}),
        await chat(manager, "schedule", said, metadata={"schedule_id": "sch"}),
        await chat(manager, "unattended", said, metadata={"unattended": True}),
        await chat(manager, "coach", said, metadata={"prompt_coach_of": mine}),
        await chat(manager, "aside", said, metadata={"btw_of": mine}),
        await chat(manager, "spawned", said, metadata={"spawned_by": mine}),
    ]
    found = {c.id: c for c in await synthesizer(app).candidates()}
    assert set(found) == {mine, imported} and not set(excluded) & set(found)
    assert found[imported].imported == "claude"
    model = fake(manager, {"ops": []}, {"ops": []})
    await sweep(app)
    fenced = [model.prompt(i) for i in range(2) if "imported chat" in model.prompt(i)]
    assert len(fenced) == 1 and "not instructions from the operator" in fenced[0]


# -- merging, updating, undoing ------------------------------------------------------------------


async def test_a_fact_said_in_a_second_chat_merges_into_the_bullet_and_an_update_rewrites_it(app: Any, manager: SessionManager, db: Database) -> None:
    first = await chat(manager, "a", [("operator", "I lint with ruff, never a formatter."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/tools", "text": "Lints with ruff, never a formatter"}]})
    await sweep(app)
    second = await chat(manager, "b", [("operator", "As I said, ruff for linting and never a formatter."), ("operator", "And I use uv now.")])
    model = fake(manager, {"ops": [
        {"op": "add", "entry": "topic/tools", "text": "lints with ruff — never a formatter."},
        {"op": "update", "record_id": "b1", "text": "Lints with ruff, never a formatter; uses uv"},
    ]})
    report = await sweep(app)
    assert "topic/tools · b1: Lints with ruff, never a formatter" in model.prompt(0)
    assert report["merged"] == 1 and report["added"] == 0 and report["updated"] == 1
    assert await entry_texts(manager, "tools") == ["Lints with ruff, never a formatter; uses uv"]
    record = manager.memory.records(TENANT)[0]
    taught = {r["session_id"] for r in await db.fetchall("SELECT session_id FROM memory_sources WHERE record_id = ?", (record.id,))}
    assert taught == {first, second}


async def test_one_undo_takes_back_a_whole_pass(app: Any, manager: SessionManager, db: Database) -> None:
    await chat(manager, "a", [("operator", "I lint with ruff."), ("operator", "My cat is Pixel.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "topic/tools", "text": "Lints with ruff"},
        {"op": "add", "entry": "topic/pets", "text": "Has a cat called Pixel"},
    ]})
    await sweep(app)
    before_records = {r.id: (r.text, r.version) for r in manager.memory.records(TENANT)}
    before_sources = await db.fetchall("SELECT record_id, session_id, from_seq FROM memory_sources ORDER BY record_id, session_id")
    before_entries = {e.slug for e in await manager.memory_entries.list()}
    await chat(manager, "b", [("operator", "Ruff for linting, as always; the cat Pixel moved out."), ("operator", "And I keep bees now.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "topic/tools", "text": "Lints with ruff."},
        {"op": "retire", "record_id": "b2", "reason": "moved out"},
        {"op": "update", "record_id": "b1", "text": "Lints with ruff always"},
        {"op": "new_entry", "section": "topic", "slug": "bees", "title": "Bees"},
        {"op": "add", "entry": "topic/bees", "text": "Keeps bees"},
    ]})
    report = await sweep(app)
    assert report["merged"] == 1 and report["retired"] == 1 and report["updated"] == 1 and report["added"] == 1
    history = report["history_ids"][-1]
    await curator(app).undo(history)
    assert {r.id: (r.text, r.version) for r in manager.memory.records(TENANT)} == before_records
    assert await db.fetchall("SELECT record_id, session_id, from_seq FROM memory_sources ORDER BY record_id, session_id") == before_sources
    assert {e.slug for e in await manager.memory_entries.list()} == before_entries


# -- the caps, the summaries, the review of held suggestions, a re-read ------------------------------


async def _seed(app: Any, manager: SessionManager, slug: str, notes: list[tuple[str, str, int]]) -> dict[str, str]:
    """A topic holding ``notes`` as ``(text, origin, days since last confirmed)``; returns text → id.
    An ``operator`` note is the operator's own but left unlocked, to show origin alone protects it."""
    entries = manager.memory_entries
    entry = await entries.ensure(synthesis.Target("topic", slug, slug.title()))
    ids = {}
    for text, origin, days in notes:
        result = await entries.remember(*entries.placement(entry), text, kind="fact", metadata={"source": origin, "topics": []}, origin=origin, entry=entry, similarity_threshold=1.0)
        at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        await manager.db.execute("UPDATE memory_sources SET at = ? WHERE record_id = ?", (at, result.record.id))
        ids[text] = result.record.id
    return ids


async def test_a_note_past_its_entrys_cap_archives_the_least_valuable_one_and_undo_brings_it_back(app: Any, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 4)
    ids = await _seed(app, manager, "garden", [
        ("Grows tomatoes", "synthesis", 30), ("Grows basil", "synthesis", 2), ("Waters at dawn", "operator", 90), ("Uses raised beds", "synthesis", 10),
    ])
    locked, _ = await curator(app).add("Never uses pesticides", kind="fact", topics=[], entry=await manager.memory_entries.by_slug("topic", "garden"))
    await chat(manager, "c", [("operator", "I started growing peppers."), ("operator", "And I compost.")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers"}, {"op": "add", "entry": "topic/garden", "text": "Composts"}]})
    report = await sweep(app)
    # Five live notes in a topic capped at four: the first add makes room for itself and brings the
    # entry under the cap (two go), the second one more. Never the locked one or the operator's own,
    # and the oldest confirmed first.
    assert report["added"] == 2 and report["archived"] == 3 and report["dropped"] == {}
    archived = {r.text for r in manager.memory.records(TENANT) if r.metadata.get("archived")}
    assert archived == {"Grows tomatoes", "Uses raised beds", "Grows basil"}
    assert manager.memory.get_record(TENANT, locked.record.id) is not None and not manager.memory.get_record(TENANT, ids["Waters at dawn"]).metadata.get("archived")
    await curator(app).undo(report["history_ids"][-1])
    assert not any(r.metadata.get("archived") for r in manager.memory.records(TENANT))
    assert sorted(await entry_texts(manager, "garden")) == sorted(["Grows tomatoes", "Grows basil", "Waters at dawn", "Uses raised beds", "Never uses pesticides"])


async def test_an_entry_full_of_locked_notes_holds_the_new_one_as_a_suggestion(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesis, "CAP_TOPIC", 2)
    entry = await manager.memory_entries.ensure(synthesis.Target("topic", "garden", "Garden"))
    for text in ("Grows tomatoes", "Grows basil"):
        await curator(app).add(text, kind="fact", topics=[], entry=entry)
    await chat(manager, "c", [("operator", "I started growing peppers."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers"}]})
    report = await sweep(app)
    assert report["added"] == 0 and report["held"] == 1 and report["archived"] == 0
    held = (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]
    assert [(s["kind"], s["why"], s["text"]) for s in held] == [("addition", "full", "Grows peppers")]


async def test_the_pass_summarises_the_entries_it_touched_and_its_undo_reverts_them_and_withdraws_its_suggestions(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    entries = manager.memory_entries
    garden = await entries.ensure(synthesis.Target("topic", "garden", "Garden"))
    garden = await entries.update(garden.id, if_version=garden.version, summary="Vegetables")
    await chat(manager, "c", [("operator", "I started growing peppers on the balcony."), ("operator", "The planner app is Sprout.")])
    model = fake(manager, {"ops": [
        {"op": "add", "entry": "topic/garden", "text": "Grows peppers on the balcony"},
        {"op": "add", "entry": "topic/garden", "text": "Plans the beds in Sprout with SPROUT_TOKEN"},
    ]}, summaries=[{"summaries": [{"entry": "topic/garden", "text": "A balcony vegetable garden"}, {"entry": "topic/elsewhere", "text": "Not asked"}]}])
    report = await sweep(app)
    assert report["added"] == 1 and report["held"] == 1 and report["summaries"] == 1
    asked = "\n".join(b.text for m in model.summary_requests[0].messages for b in m.content_blocks if isinstance(b, TextBlock))
    assert "entry: topic/garden — Garden" in asked and "current summary: Vegetables" in asked and "Grows peppers on the balcony" in asked
    assert (await entries.get(garden.id)).summary == "A balcony vegetable garden"
    held = (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]
    assert len(held) == 1 and held[0]["pass"].startswith("memory-") and held[0]["session_title"] == "c"
    assert held[0]["quote"] and held[0]["from_seq"]
    await curator(app).undo(report["history_ids"][-1])
    assert (await entries.get(garden.id)).summary == "Vegetables"
    assert (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"] == []
    status = await db.fetchone("SELECT status FROM memory_suggestions WHERE id = ?", (held[0]["id"],))
    assert status["status"] == "withdrawn"


async def test_a_summary_the_operator_rewrote_after_the_pass_survives_its_undo(app: Any, manager: SessionManager, db: Database) -> None:
    entries = manager.memory_entries
    garden = await entries.ensure(synthesis.Target("topic", "garden", "Garden"))
    await chat(manager, "c", [("operator", "I started growing peppers."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/garden", "text": "Grows peppers"}]}, summaries=[{"summaries": [{"entry": "topic/garden", "text": "Peppers"}]}])
    report = await sweep(app)
    fresh = await entries.get(garden.id)
    await entries.update(garden.id, if_version=fresh.version, summary="My garden", by_operator=True)
    await curator(app).undo(report["history_ids"][-1])
    assert (await entries.get(garden.id)).summary == "My garden"


async def _held(app: Any, manager: SessionManager, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    await chat(manager, "c", [("operator", "Set up the planner server for Sprout."), ("operator", "Use the usual stack.")])
    fake(manager, {"ops": [
        {"op": "add", "entry": "area/sprout", "text": "Sprout server listens on 0.0.0.0:8102"},
        {"op": "add", "entry": "area/sprout", "text": "Sprout reads SPROUT_TOKEN"},
        {"op": "add", "entry": "topic/hosting", "text": "Hosting runs on node-7"},
    ]})
    report = await sweep(app)
    assert report["held"] == 3
    return (await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]


async def test_accepting_a_group_is_one_change_whose_undo_opens_the_suggestions_again(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    held = await _held(app, manager, client)
    sprout = [s["id"] for s in held if s["entry_title"] and s["entry_title"].lower() == "sprout"]
    assert len(sprout) == 2
    done = await client.post("/api/memory/suggestions/decide", json={"action": "apply", "ids": sprout}, headers=H)
    assert done.status_code == 200, done.text
    assert done.json()["applied"] == 2 and done.json()["history_id"]
    assert sorted(await entry_texts(manager, "sprout", "area")) == ["Sprout reads SPROUT_TOKEN", "Sprout server listens on 0.0.0.0:8102"]
    assert len((await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]) == 1
    listing = (await client.get("/api/memory/entries", headers=H)).json()
    assert listing["suggestions"] == 1
    assert (await client.post(f"/api/memory/history/{done.json()['history_id']}/undo", headers=H)).status_code == 200
    assert await entry_texts(manager, "sprout", "area") == []
    assert len((await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]) == 3


async def test_dismissing_a_pass_tombstones_its_words_so_a_later_pass_does_not_propose_them_and_undo_lifts_that(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    held = await _held(app, manager, client)
    passes = {s["pass"] for s in held}
    assert len(passes) == 1
    done = (await client.post("/api/memory/suggestions/decide", json={"action": "dismiss", "ids": [s["id"] for s in held]}, headers=H)).json()
    assert done["dismissed"] == 3 and done["history_id"]
    reasons = [r["reason"] for r in await db.fetchall("SELECT reason FROM memory_tombstones")]
    assert reasons == ["dismissed"] * 3
    await chat(manager, "d", [("operator", "Again: the planner server for Sprout."), ("operator", "Same stack.")])
    fake(manager, {"ops": [{"op": "add", "entry": "area/sprout", "text": "Sprout server listens on 0.0.0.0:8102."}]})
    again = await sweep(app)
    assert again["held"] == 0 and again["dropped"] == {"tombstoned": 1}
    await curator(app).undo(done["history_id"])
    assert await db.fetchall("SELECT reason FROM memory_tombstones") == []
    assert len((await client.get("/api/memory/suggestions", headers=H)).json()["suggestions"]) == 3
    # One dismissed alone goes the same way and can be undone from the toast.
    one = (await client.post(f"/api/memory/suggestions/{held[0]['id']}/dismiss", headers=H)).json()
    assert one["dismissed"] and one["history_id"]


async def test_reading_every_chat_again_adds_no_duplicates(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    sid = await chat(manager, "c", [("operator", "I play the cello every Sunday."), ("operator", "Set up the planner server for Sprout.")])
    ops = {"ops": [
        {"op": "add", "entry": "topic/music", "text": "Plays the cello every Sunday"},
        {"op": "add", "entry": "area/sprout", "text": "Sprout server listens on 0.0.0.0:8102"},
    ]}
    fake(manager, ops)
    first = await sweep(app)
    assert first["added"] == 1 and first["held"] == 1
    records = sorted((r.id, r.text) for r in manager.memory.records(TENANT))
    sources = await db.fetchall("SELECT record_id, session_id, from_seq FROM memory_sources ORDER BY record_id")
    suggestions = await db.fetchall("SELECT id FROM memory_suggestions")
    # The model words the same facts a little differently the second time.
    fake(manager, {"ops": [
        {"op": "add", "entry": "topic/music", "text": "plays the cello every sunday."},
        {"op": "add", "entry": "area/sprout", "text": "Sprout server listens on 0.0.0.0:8102"},
    ]})
    started = await client.post("/api/memory/synthesis/reread", headers=H)
    assert started.status_code == 200 and started.json()["started"]
    await synthesizer(app).task
    status = (await client.get("/api/memory/status", headers=H)).json()
    assert status["report"]["trigger"] == "reread" and status["report"]["chats"] == 1
    assert status["report"]["added"] == 0 and status["report"]["merged"] == 1 and status["report"]["held"] == 0
    assert sorted((r.id, r.text) for r in manager.memory.records(TENANT)) == records
    assert await db.fetchall("SELECT record_id, session_id, from_seq FROM memory_sources ORDER BY record_id") == sources
    assert await db.fetchall("SELECT id FROM memory_suggestions") == suggestions
    assert (await watermark(db, sid))[0] == await last_seq(db, sid)
    manager.config.memory = MemoryConfig(paused=True)
    assert (await client.post("/api/memory/synthesis/reread", headers=H)).status_code == 409


# -- the status the settings show ------------------------------------------------------------------


async def test_the_status_reports_the_last_pass_and_update_now_respects_pause(app: Any, client: httpx.AsyncClient, manager: SessionManager, db: Database) -> None:
    await chat(manager, "c", [("operator", "I play the cello."), ("operator", "ok")])
    fake(manager, {"ops": [{"op": "add", "entry": "topic/music", "text": "Plays the cello"}]}, cost=0.002)
    started = await client.post("/api/memory/synthesis/run", headers=H)
    assert started.status_code == 200 and started.json()["started"]
    await synthesizer(app).task
    status = (await client.get("/api/memory/status", headers=H)).json()
    assert status["report"]["chats"] == 1 and status["report"]["added"] == 1 and status["report"]["trigger"] == "manual"
    # The operations call and the summary follow-up, at 0.002 each.
    assert status["report"]["cost_usd"] == pytest.approx(0.004) and status["model"] and status["last_synthesis_at"]
    manager.config.memory = MemoryConfig(paused=True)
    assert (await client.post("/api/memory/synthesis/run", headers=H)).status_code == 409


def test_the_recommended_model_is_chosen_when_none_is_and_a_missing_choice_is_named() -> None:
    config = model_config()
    config.presets["deepseek.deepseek-v4-flash"] = next(iter(config.presets.values()))
    choice = synthesis.choose_model(config)
    assert choice.preset == "deepseek.deepseek-v4-flash" and "opencode.deepseek-v4.1-flash is not configured" in choice.note
    config.presets["opencode.deepseek-v4.1-flash"] = next(iter(config.presets.values()))
    choice = synthesis.choose_model(config)
    assert choice.preset == "opencode.deepseek-v4.1-flash" and choice.fallbacks == ("deepseek.deepseek-v4-flash",) and choice.note == ""
    config.memory = MemoryConfig(synthesis_preset="gone")
    assert "gone is no longer configured" in synthesis.choose_model(config).note


# -- the pure parts ------------------------------------------------------------------------------


def test_the_parser_is_strict_but_digs_json_out_of_prose() -> None:
    assert parse_ops('{"ops": []}').ok and not parse_ops('{"ops": []}').recovered
    fenced = parse_ops('here:\n```json\n{"ops":[{"op":"summary","entry":"you/profile","text":"hi"}]}\n```')
    assert fenced.ok and fenced.recovered and len(fenced.ops) == 1
    assert not parse_ops('{"ops": [{"op": "add", "entry": "topic/x"}]}').ok
    assert not parse_ops('{"ops": [{"op": "delete_everything"}]}').ok
    assert not parse_ops("no json here").ok


def test_condense_keeps_only_the_answer_before_each_operator_message() -> None:
    turns = [synthesis.Turn(1, "operator", "do it"), synthesis.Turn(2, "assistant", "looking"), synthesis.Turn(3, "assistant", "done"), synthesis.Turn(4, "operator", "thanks")]
    assert [t.text for t in synthesis.condense(turns)] == ["do it", "done", "thanks"]


def test_grounding_and_the_quote_check() -> None:
    assert synthesis.ungrounded_tokens("Server on 0.0.0.0:8102 with SPROUT_TOKEN", "set up the server") == ["0.0.0.0:8102", "SPROUT_TOKEN"]
    assert synthesis.ungrounded_tokens("Uses Postgres for Sprout", "Sprout runs on Postgres") == []
    assert synthesis.quote_backed("always  answer in Russian", [5], {5: "From now on, Always answer in Russian."})
    assert not synthesis.quote_backed("I love one-word answers", [5], {5: "write the report"})


def test_deepseek_shaped_endpoints_get_a_json_object_and_memory_calls_are_booked_as_memory() -> None:
    async def body(kind: str) -> dict[str, Any]:
        provider = OpenAICompatibleProvider(ProviderEndpoint(id=kind, kind=kind, base_url="http://127.0.0.1:9"))  # type: ignore[arg-type]
        request = LLMRequest(model="m", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])], max_tokens=10, extra={"enable_thinking": False, "json_object": True})
        return await provider._build_body(request, stream=False)

    import asyncio

    assert asyncio.run(body("opencode"))["response_format"] == {"type": "json_object"}
    assert asyncio.run(body("deepseek"))["thinking"] == {"type": "disabled"}
    assert "response_format" not in asyncio.run(body("openai_compat"))
