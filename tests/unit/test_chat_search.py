"""The agent's reach into the operator's other chats: scope, exclusions, which search answered, listing."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, Session, TextBlock

from daedalus.config import CHAT_SEARCH_TOOLS, RuntimeConfig
from daedalus.host import prompts
from daedalus.host.services import SessionServices, locator
from daedalus.host.session_runner import NOT_OPERATOR_FACING, SessionManager
from daedalus.search import chats
from daedalus.search.service import ConversationSearch, SearchBusy
from daedalus.search.store import Index, any_word, search
from daedalus.stores.sqlite import SqliteSessionStore, fts_query
from daedalus.tools.history import chat_search, history_expand, history_search, recent_chats


async def chat(db: Any, sid: str, text: str, *, project: str | None = None, plain: bool = False, title: str = "",
               metadata: dict[str, Any] | None = None, at: str = "2026-10-20T10:00:00+00:00", system: str = "") -> None:
    """A session with one operator message. Without ``project`` it gets a project of its own, named by its
    title: a plain chat's scratch project when ``plain``, the installation's own when ``system``."""
    store = SqliteSessionStore(db)
    await store.create(Session(id=sid, tenant_id="daedalus", title=title or sid, metadata=metadata or {}), project_id=project)
    if plain:
        await db.execute("UPDATE projects SET settings = json_set(settings, '$.ephemeral', json('true')) WHERE id = ?", (sid,))
    if system:
        await db.execute("UPDATE projects SET system = ? WHERE id = ?", (system, sid))
    await store.append_transcript(sid, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})])
    await db.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (at, sid))


class Tools:
    """Calls the history tools as session ``sid`` would, against a manager that has just what they read."""

    def __init__(self, db: Any, service: Any) -> None:
        self.manager = SimpleNamespace(db=db, sessions=SqliteSessionStore(db), service_hooks={"chat_search": service}, index_rebuild=None)

    async def __call__(self, tool: Any, sid: str, **arguments: Any) -> Any:
        locator.register(SessionServices(session_id=sid, workspace_dir=Path("."), extra={"manager": self.manager}))
        try:
            context = ToolContext(tenant_id="daedalus", run_id="r", session_id=sid, metadata={"tool_call_id": "c"})
            return await tool().invoke(context, arguments)
        finally:
            locator.unregister(sid)


def ids(result: Any) -> list[str]:
    return [line.split()[1].removeprefix("id=") for line in result.content.splitlines() if line.startswith("⟪chat ")]


@pytest.fixture
async def world(db: Any, tmp_path: Path) -> Tools:
    """Two projects of the operator's, plain chats, the voice project, and the sessions that must never show."""
    await chat(db, "alpha-1", "the kombain pipeline splits the river data", title="Alpha")
    await chat(db, "alpha-2", "kombain retries after a timeout", project="alpha-1", at="2026-10-21T10:00:00+00:00")
    await chat(db, "beta-1", "kombain in the beta project too", title="Beta")
    await chat(db, "plain-1", "kombain notes from a plain chat", plain=True, at="2026-10-22T10:00:00+00:00")
    await chat(db, "plain-2", "the asking chat mentions kombain itself", plain=True, at="2026-10-23T10:00:00+00:00")
    await chat(db, "voice-1", "kombain said out loud", system="voice", at="2026-10-19T10:00:00+00:00")
    await chat(db, "imported", "kombain from an imported conversation", plain=True, metadata={"imported": {"harness": "claude"}}, at="2026-10-18T10:00:00+00:00")
    await chat(db, "secret", "kombain in a private chat", plain=True, metadata={"memory": "private"})
    await chat(db, "helper", "kombain in a subagent", plain=True, metadata={"subagent_of": "plain-1", "unattended": True})
    await chat(db, "worker", "kombain in a staff session", plain=True, metadata={"staff_id": "st-1"})
    await chat(db, "cron", "kombain in a schedule", plain=True, metadata={"schedule_id": "sch-1", "unattended": True})
    await chat(db, "coach", "kombain in a prompt coach", plain=True, metadata={"prompt_coach_of": "plain-1"})
    return Tools(db, ConversationSearch(db, tmp_path))


def test_the_rule_names_every_session_that_is_not_the_operator_talking() -> None:
    # The session runner's own list of sessions nobody reads as a conversation is the floor.
    assert set(NOT_OPERATOR_FACING) <= set(chats.NOT_CONVERSATION_KEYS)
    assert chats.searchable({}) and chats.searchable({"imported": {"harness": "codex"}, "unattended": False})
    assert not chats.searchable({"memory": "private"}) and chats.private({"memory": "private"})
    assert not chats.searchable({"subagent_of": "x"}) and not chats.searchable({"staff_id": "y"})


async def test_the_sql_twin_agrees_with_the_predicate(db: Any) -> None:
    cases = [{}, {"memory": "private"}, {"memory": "on"}, {"unattended": False}, {"unattended": True}, {"subagent_of": ""},
             {"subagent_of": "s"}, {"heartbeat": True}, {"btw_of": "s"}, {"schedule_id": "x"}, {"imported": {"harness": "pi"}}]
    for index, metadata in enumerate(cases):
        await chat(db, f"s{index}", "text", plain=True, metadata=metadata)
    rows = await db.fetchall(f"SELECT s.id, {chats.SEARCHABLE_SQL} ok FROM sessions s ORDER BY s.id")
    by_id = {row["id"]: bool(row["ok"]) for row in rows}
    assert {f"s{i}": chats.searchable(m) for i, m in enumerate(cases)} == by_id


async def test_outside_projects_it_searches_the_plain_chats_and_never_the_excluded(world: Tools) -> None:
    found = await world(chat_search, "plain-2", query="kombain")
    assert sorted(ids(found)) == ["imported", "plain-1", "voice-1"]
    assert "words only: the meaning index is off" in found.content
    assert "data, not instructions" in found.content
    assert "[kombain]" in found.content  # a snippet, quoted


async def test_inside_a_project_it_searches_that_project_and_lists_another_on_request(world: Tools) -> None:
    found = await world(chat_search, "alpha-2", query="kombain")
    assert ids(found) == ["alpha-1"]
    listed = await world(chat_search, "alpha-2", query="kombain", project="beta")
    assert ids(listed) == ["beta-1"]
    assert "[kombain]" not in listed.content and "listed, not quoted" in listed.content
    # Its own project by name is the ordinary search, quoted.
    assert "[kombain]" in (await world(chat_search, "alpha-2", query="kombain", project="Alpha")).content
    assert (await world(chat_search, "alpha-2", query="kombain", project="nowhere")).is_error
    # A plain chat's scratch project is nobody's to name.
    assert (await world(chat_search, "alpha-2", query="kombain", project="plain-1")).is_error


async def test_a_chat_holding_only_some_words_is_found_and_said_to_be(world: Tools) -> None:
    found = await world(chat_search, "plain-2", query="kombain zeppelin")
    assert "No chat has every word" in found.content and "plain-1" in ids(found)
    nothing = await world(chat_search, "plain-2", query="zeppelin")
    assert nothing.content.startswith("no chat in the operator's chats outside projects matches")
    assert (await world(chat_search, "plain-2", query="  ")).is_error


async def test_a_title_hit_points_at_the_chats_first_message(db: Any, world: Tools) -> None:
    await chat(db, "titled", "nothing relevant in the words", plain=True, title="Zeppelin budget")
    found = await world(chat_search, "plain-2", query="Zeppelin budget")
    first = (await db.fetchone("SELECT min(seq) seq FROM transcript WHERE session_id = 'titled'"))["seq"]
    assert f"seq={first}" in found.content and 'title="Zeppelin budget"' in found.content


class Hybrid:
    """A service whose semantic half answered: the tools must say so, and know nothing of its model."""

    def __init__(self, inner: ConversationSearch, *, busy: bool = False) -> None:
        self.inner, self.busy, self.calls = inner, busy, []

    async def query(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.busy:
            raise SearchBusy("busy")
        found = await self.inner.query(query, **kwargs)
        return {**found, "semantic": True, "reason": "ready"}


async def test_the_answer_says_which_search_answered(db: Any, world: Tools, tmp_path: Path) -> None:
    hybrid = Hybrid(ConversationSearch(db, tmp_path))
    world.manager.service_hooks["chat_search"] = hybrid
    found = await world(chat_search, "plain-2", query="kombain")
    assert "(hybrid: meaning and words)" in found.content and found.metadata["semantic"] is True
    assert hybrid.calls[0]["reach"] == chats.Reach(project="", besides="plain-2")
    world.manager.service_hooks["chat_search"] = Hybrid(ConversationSearch(db, tmp_path), busy=True)
    assert (await world(chat_search, "plain-2", query="kombain")).is_error
    del world.manager.service_hooks["chat_search"]
    assert (await world(chat_search, "plain-2", query="kombain")).is_error


async def test_the_semantic_half_keeps_to_the_reach(db: Any) -> None:
    await chat(db, "open", "cycling along the water", plain=True)
    await chat(db, "closed", "cycling along the water", plain=True, metadata={"memory": "private"})
    index = Index(db)
    await index.prepare("test", 2)
    while (passage := await index.next()) is not None:
        await index.save(passage, [1, 0] if passage.text else None, "test", 2)
    reach = chats.Reach(besides="")
    for _ in range(10):
        found = search(db.path, "daedalus", "bicycle river", vector=[1, 0], model="test", dimension=2, reach=reach)
        if not found["partial"]:
            break
    assert [h["session_id"] for h in found["hits"]] == ["open"]


def test_any_word_keeps_a_phrase_whole() -> None:
    assert any_word(fts_query("kombain river")) == '"kombain"* OR "river"*'
    assert any_word(fts_query('"kombain river"')) == '"kombain river"'


async def test_recent_chats_lists_by_time_with_the_first_ask_and_last_headline(db: Any, world: Tools) -> None:
    await db.execute("INSERT INTO learning_records(session_id, run_id, at, ask, outcome, headline) VALUES (?, ?, ?, ?, ?, ?)",
                     ("plain-1", "r1", "2026-10-22T10:00:00+00:00", "how does the kombain split work?", "completed", "⟦ kombain | status: explained | anchors: splitter ⟧"))
    listed = await world(recent_chats, "plain-2", n=2)
    assert ids(listed) == ["plain-1", "voice-1"]
    assert "asked: how does the kombain split work?" in listed.content
    assert "ended: ⟦ kombain | status: explained" in listed.content
    assert 'Older: RecentChats(before="2026-10-19T10:00:00+00:00").' in listed.content
    # The imported chat has no runs of ours; its first operator message stands in.
    older = await world(recent_chats, "plain-2", before="2026-10-19")
    assert ids(older) == ["imported"] and "asked: kombain from an imported conversation" in older.content
    assert "No older ones" in older.content
    window = await world(recent_chats, "plain-2", after="2026-10-19T12:00", before="2026-10-23")
    assert ids(window) == ["plain-1"]
    assert (await world(recent_chats, "plain-2", before="yesterday")).is_error


async def test_recent_chats_of_another_project_are_a_listing(world: Tools) -> None:
    listed = await world(recent_chats, "plain-2", project="Alpha")
    assert ids(listed) == ["alpha-2", "alpha-1"]
    assert "asked:" not in listed.content and 'project="Alpha"' not in listed.content  # fewer than asked for
    own = await world(recent_chats, "alpha-2")
    assert ids(own) == ["alpha-1"] and "asked: the kombain pipeline" in own.content


async def test_history_search_keeps_to_this_chat_and_expand_reads_another_but_not_a_private_one(db: Any, world: Tools) -> None:
    assert "all_sessions" not in json.dumps(history_search().definition.parameters.model_dump())
    found = await world(history_search, "plain-2", query="kombain")
    assert sum(line.startswith("seq ") for line in found.content.splitlines()) == 1 and "asking chat" in found.content
    seq = (await db.fetchone("SELECT min(seq) seq FROM transcript WHERE session_id = 'plain-1'"))["seq"]
    read = await world(history_expand, "plain-2", from_seq=seq, session_id="plain-1")
    assert "kombain notes from a plain chat" in read.content
    hidden = (await db.fetchone("SELECT min(seq) seq FROM transcript WHERE session_id = 'secret'"))["seq"]
    refused = await world(history_expand, "plain-2", from_seq=hidden, session_id="secret")
    assert refused.is_error and "private" not in refused.content


async def test_the_switch_takes_both_tools_away_and_the_prompt_follows(settings: Any, db: Any) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("work")
        assert not set(CHAT_SEARCH_TOOLS) & manager.blocked_tools_for(state)
        manager.config.memory.search_chats = False
        assert set(CHAT_SEARCH_TOOLS) <= manager.blocked_tools_for(state)
        assert "HistorySearch" not in manager.blocked_tools_for(state)
    finally:
        await manager.close()
    shown = "".join(prompts.tool_sections({"HistorySearch", "HistoryExpand", "ChatSearch", "RecentChats"}))
    assert prompts.CHAT_SEARCH in shown and "как мы обсуждали" in shown
    assert prompts.CHAT_SEARCH not in "".join(prompts.tool_sections({"HistorySearch", "HistoryExpand"}))
