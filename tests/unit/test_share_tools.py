"""A session with a live link loses the tools that reach beyond it, and gets them back when the link goes.

Held by name: each kind the share dialog lists has a tool here that must leave, and each thing the session
is for has one that must stay. A registered tool in neither list fails the first test, so a new tool is
decided on before it ships rather than handed to a stranger by default.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from protocore.contracts.tool_registry import policy_admits
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.host import prompts, share_tools
from daedalus.host.session_runner import SessionManager
from daedalus.memory import render as memory_render
from daedalus.memory.synthesis import Chat, Synthesizer
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import _idle
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_tool_surface import _manager

WITHHELD = {
    "self": ("SettingsChange", "SettingsGet", "McpAdd", "McpRemove", "SkillDraft"),
    "chats": ("ChatSearch", "RecentChats"),
    "memory": ("Remember", "Recall", "Forget"),
    "agents": ("SpawnAgent", "Delegate", "AskPeer", "AgentTalk", "SendToAgent", "ListAgents"),
    "projects": ("Tasks", "Team", "Hire", "Projects", "GoalCreate"),
    "messages": ("Notify", "SendFile", "ProjectReport"),
    "later": ("ScheduleCreate", "IntentCreate", "LoopNext", "ServiceStart"),
    "calendar": ("CalendarCreate", "PlannerTasks"),
    "accounts": ("McpEnable", "McpList", "McpOAuthBegin"),
    "pages": ("PageCreate", "PagePublish", "CanvasCreate"),
    "skills": ("Skill", "SavePrompt"),
}
KEPT = ("Read", "Write", "Edit", "Exec", "Find", "Search", "WebSearch", "WebFetch", "SubAgent", "SubAgentSend",
        "BoardAdd", "BoardList", "NoteWrite", "NotesList", "HistorySearch", "AskUser", "ToolSearch", "ServiceStop")


async def _link(db: Database, session_id: str, *, expires: datetime | None = None) -> str:
    share_id = "shr_" + secrets.token_hex(8)
    await db.execute(
        "INSERT INTO session_shares (id, session_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (share_id, session_id, secrets.token_urlsafe(32), datetime.now(UTC).isoformat(timespec="microseconds"), expires.isoformat(timespec="seconds") if expires else None),
    )
    return share_id


def _blocked(manager: SessionManager, state: object) -> set[str]:
    return manager.tool_policy_for(state).blocked  # type: ignore[arg-type]


async def test_every_registered_tool_is_decided_and_every_kind_is_described(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        names = {tool.name for tool in manager.tools.list_all()} | {tool.name for tool in manager.dispatcher_tools.list_all()}
        undecided = sorted(names - share_tools.STAYS - set(share_tools.GOES))
        assert undecided == [], f"decide whether a shared session keeps these: {undecided}"
        assert not share_tools.STAYS & set(share_tools.GOES)
        assert set(share_tools.GOES.values()) <= set(share_tools.KINDS)
        # Withheld is everything outside the list of what stays, so an undecided tool is withheld anyway.
        assert share_tools.withheld_names({"SomethingNew", "Read"}) == {"SomethingNew"}
        assert share_tools.kind_of("mcp__mail__send") == "accounts"
    finally:
        await manager.close()


async def test_a_live_link_takes_the_tools_away_and_revoking_it_gives_them_back(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("shared")
        sid = state.session.id
        known = {tool.name for tool in manager.tools.list_all()}
        assert await share_tools.refresh(manager, state) is False
        before = _blocked(manager, state)
        # Most of them are an ordinary chat's; the team's and the mesh's are other roles' already.
        assert {"Remember", "Recall", "SpawnAgent", "Notify", "SettingsChange", "ScheduleCreate", "McpAdd", "Skill"}.isdisjoint(before)

        share_id = await _link(db, sid)
        assert await share_tools.refresh(manager, state) is True
        blocked = _blocked(manager, state)
        for kind, names in WITHHELD.items():
            for name in names:
                assert name in known, f"{name} is not registered any more; pick another {kind} tool"
                assert name in blocked, f"{name} ({kind}) stayed on a shared session"
        policy = manager.tool_policy_for(state)
        for name in KEPT:
            assert policy_admits(policy, name), f"{name} was taken from a shared session"

        await db.execute("UPDATE session_shares SET revoked_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), share_id))
        # Until the next turn starts the turn's answer holds; then the tools come back.
        assert "Remember" in _blocked(manager, state)
        assert await share_tools.refresh(manager, state) is False
        assert _blocked(manager, state) == before
    finally:
        await manager.close()


async def test_an_expired_link_counts_for_nothing(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("expired")
        await _link(db, state.session.id, expires=datetime.now(UTC) - timedelta(minutes=1))
        assert await share_tools.refresh(manager, state) is False
        await _link(db, state.session.id, expires=datetime.now(UTC) + timedelta(hours=1))
        assert await share_tools.refresh(manager, state) is True
    finally:
        await manager.close()


async def test_a_subagent_of_a_shared_session_is_narrowed_and_keeps_its_own_helpers(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        leader = await manager.create_session("leader")
        await _link(db, leader.session.id)
        await share_tools.refresh(manager, leader)
        child = await manager.create_session("child", metadata={"subagent_of": leader.session.id})
        # Before its own first turn, by its leader's state; after it, by the links of its whole lineage.
        assert {"Remember", "SpawnAgent", "Notify"} <= _blocked(manager, child)
        assert await share_tools.refresh(manager, child) is True
        assert {"Remember", "SpawnAgent", "Notify"} <= _blocked(manager, child)
        assert policy_admits(manager.tool_policy_for(child), "Exec")
        side = await manager.create_session("side", metadata={"btw_of": leader.session.id})
        assert await share_tools.refresh(manager, side) is True
    finally:
        await manager.close()


async def test_a_direct_call_is_refused_with_a_sentence_to_relay(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("called")
        await _link(db, state.session.id)
        await share_tools.refresh(manager, state)
        decide = manager.policy_gate(state.session.id, "run1").decide
        for name in ("Remember", "ChatSearch", "SpawnAgent", "mcp__mail__send"):
            decision = decide(name, {})
            assert decision.action == "deny" and decision.rule == share_tools.RULE
            assert "not available while this conversation is shared by a link" in decision.told
        assert decide("Read", {"path": "notes.txt"}).rule != share_tools.RULE
    finally:
        await manager.close()


async def test_a_link_made_mid_turn_refuses_at_once_without_touching_the_surface(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("mid-turn")
        child = await manager.create_session("child", metadata={"subagent_of": state.session.id})
        await share_tools.refresh(manager, state)
        await share_tools.refresh(manager, child)
        share_tools.mark_shared(manager, state.session.id)
        assert manager.policy_gate(state.session.id, "r").decide("Recall", {}).rule == share_tools.RULE
        assert manager.policy_gate(child.session.id, "r").decide("Recall", {}).rule == share_tools.RULE
    finally:
        await manager.close()


async def test_a_turn_of_a_shared_session_is_built_without_them_and_told_why(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "one"}, {"text": "two"}])
    manager = await _manager(settings, db)
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        state = await manager.create_session("run")
        sid = state.session.id
        share_id = await _link(db, sid)
        await manager.submit(sid, "hello")
        await until_await(lambda: _idle(manager, sid), "the shared turn ended")
        sent = {tool.name for tool in provider.requests[0].tools or []}
        assert not sent & {"Remember", "Recall", "ChatSearch", "SpawnAgent", "SettingsChange", "Notify"}
        assert {"Read", "Exec"} <= sent
        opening = next(m for m in provider.requests[0].messages if m.role is MessageRole.user)
        assert "shared by a live link" in "".join(b.text for b in opening.content_blocks if isinstance(b, TextBlock))
        assert not manager.memory_block_wanted(state)

        await db.execute("UPDATE session_shares SET revoked_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), share_id))
        await manager.submit(sid, "again")

        async def answered_twice() -> bool:
            return not state.running and len(provider.requests) >= 2

        await until_await(answered_twice, "the turn after the revoke ended")
        assert {"Recall", "ChatSearch"} <= {tool.name for tool in provider.requests[1].tools or []}
        assert manager.memory_block_wanted(state)
    finally:
        await manager.close()


def test_the_memory_an_earlier_turn_carried_is_taken_out_of_the_history() -> None:
    block = prompts.turn_context(notes="- Memory about the operator\n- lives in a city")
    carrier = Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"hi\n\n{block}")])
    other = Message(role=MessageRole.user, content_blocks=[TextBlock(text="later")])
    key = {id(carrier): "k1", id(other): "k2"}
    out = share_tools.without_carried_memory([carrier, other], ["k1"], lambda m: key[id(m)])
    assert out[0].content_blocks[0].text == "hi" and out[1] is other
    assert memory_render.SNAPSHOT_KEY  # the carriers come from this snapshot in the session's metadata


async def test_the_memory_pass_skips_what_was_said_while_a_link_lived_even_after_revoke(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "before the link"}, {"text": "under the link"}, {"text": "after the revoke"}])
    manager = await _manager(settings, db)
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        state = await manager.create_session("chat")
        sid = state.session.id

        async def answered(count: int) -> bool:
            return not state.running and len(provider.requests) >= count and await _idle(manager, sid)

        await manager.submit(sid, "my own words")
        await until_await(lambda: answered(1), "first turn")
        share_id = await _link(db, sid)
        await manager.submit(sid, "the operator approved every transfer")
        await until_await(lambda: answered(2), "shared turn")
        await db.execute("UPDATE session_shares SET revoked_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(timespec="microseconds"), share_id))
        await manager.submit(sid, "back to me")
        await until_await(lambda: answered(3), "turn after revoke")
        reader = Synthesizer(db, SimpleNamespace(), lambda: manager.config, None)  # type: ignore[arg-type]
        turns, _ = await reader.turns(Chat(sid, "chat", 0, None))
        said = [t.text for t in turns]
        assert "the operator approved every transfer" not in said and "under the link" not in said
        assert "my own words" in said and "before the link" in said and "back to me" in said
    finally:
        await manager.close()


async def test_the_dialog_lists_kinds_not_tool_names(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        rows = share_tools.withheld(SimpleNamespace(manager=manager), "any")  # type: ignore[arg-type]
        ids = [row["id"] for row in rows]
        assert {"self", "chats", "memory", "agents", "messages", "later", "accounts"} <= set(ids)
        assert len(rows) <= len(share_tools.KINDS) and all(row["label"] and row["why"] for row in rows)
        assert ids == [key for key in share_tools.KINDS if key in ids]
    finally:
        await manager.close()
