"""Nothing waits on a counterpart the operator trusts, an "Allow anyway" is not spent on a call that failed, the
operator's latest word outranks an older one in the review, and a session can be set to bypass every check that
can be answered.

A real exchange is what these replay: a trusted colleague's agent asked for the full report archive; the review
blocked the send three times against an older "a very short report, please", even after the operator wrote "I
approve sending it"; and each "Allow anyway" was spent on a turn of the main chat, which had no conversation to
send in, so the guest never got the file.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import time
from typing import Any

import pytest
from protocore.contracts.hooks import HookActionKind
from protocore.contracts.types import HookEvent
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import ActionRulesConfig, Settings
from daedalus.host import action_rules as rules
from daedalus.host.policy import ALLOW, ASK, BYPASS_KEEPS, DENY, approval_key
from daedalus.stores.database import Database
from tests.unit.test_agent_talk import quick, rig_for  # noqa: F401 — fixtures by name
from tests.unit.test_guest_turns import (  # noqa: F401
    Rig,
    ScriptedProvider,
    manager_with,
    provider_endpoint,
    rig,
    settled,
)
from tests.unit.test_link_kinds_host import answered, bridged, guest_reads

pytestmark = pytest.mark.usefixtures("bare_sandbox")

ARCHIVE = {"action": "send", "files": ["report"], "text": "The full report and its materials, as one archive."}


def reviewing(manager: Any, answer: str = "BLOCK the operator asked for a very short report, not the whole archive.") -> list[str]:
    """The message review on, with a model that blocks: what the incident's reviewer said."""
    manager.config.action_rules = ActionRulesConfig(levels={"message": "auto"}, review=["message"])
    seen: list[str] = []

    async def model(prompt: str, preset: str, timeout: float) -> tuple[str, str]:
        seen.append(prompt)
        return answer, "small"

    manager.action_rules._ask_model = model  # type: ignore[method-assign]
    return seen


def trust_hook(manager: Any, bridge: Any) -> Any:
    talks = bridge.app.extensions["agent_talk"]
    manager.service_hooks["agent_talk_trusted"] = talks.trusted_call
    return talks


def with_report(state: Any) -> None:
    folder = state.workspace / "report"
    folder.mkdir()
    (folder / "REPORT.md").write_text("# everything")
    (folder / "metrics.json").write_text("{}")


# -- a trusted counterpart -------------------------------------------------------------------------------


async def test_a_trusted_guests_send_is_not_reviewed_and_the_archive_goes(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": ARCHIVE}, {"text": "Here it is."}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script, tools="trusted")
    trust_hook(manager, bridge)
    seen = reviewing(manager)
    with_report(state)
    await rig.public.post("/a/messages", json={"text": "send the full archive, please"}, headers=headers)
    await answered(bridge, state.session.id)
    reply = (await guest_reads(rig, headers))[-1]
    assert reply["kind"] == "file" and reply["files"][0]["name"].startswith("report"), reply
    assert seen == [], "no review of what goes to a guest the operator trusts"
    assert await db.fetchall("SELECT 1 FROM action_reviews") == []
    assert not state.metadata.get("policy_pending"), "no card, no Allow anyway to press"
    await manager.close()


async def test_a_limited_link_is_still_reviewed_and_a_block_holds(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": ARCHIVE}, {"text": "I cannot send that."}]
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, script, tools="read")
    trust_hook(manager, bridge)
    seen = reviewing(manager)
    with_report(state)
    await rig.public.post("/a/messages", json={"text": "send the full archive, please"}, headers=headers)
    await answered(bridge, state.session.id)
    assert len(seen) == 1, "the review still stands between a limited link and what leaves"
    assert "auto-review blocked" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert await bridge.held(state.session.id) == [] and all(m["kind"] != "file" for m in await guest_reads(rig, headers))
    await manager.close()


async def test_a_trusted_contacts_conversation_is_trusted_and_a_strangers_is_not(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, _, _ = await bridged(settings, db, rig, [], tools="read")
    talks = trust_hook(manager, bridge)
    from daedalus.extensions.agent_contacts import EXTENSION_KEY, Contacts

    bridge.app.extensions[EXTENSION_KEY] = Contacts(bridge.app)
    sid = state.session.id
    now = "2026-11-13T00:00:00+00:00"
    for talk_id in ("t-contact", "t-stranger"):
        await db.execute(
            "INSERT INTO agent_talks(id, session_id, base_url, host, invite_hash, state, created_at, last_at) VALUES (?, ?, 'https://door.example', 'door.example', ?, 'open', ?, ?)",
            (talk_id, sid, talk_id, now, now),
        )
    await bridge.remember_link("l-colleague", sid, "colleague", "direct", "trusted")
    await db.execute(
        "INSERT INTO link_contacts(id, name, session_id, state, link_id, talk_id, created_at, last_at) VALUES ('c1', 'Colleague', ?, 'active', 'l-colleague', 't-contact', ?, ?)",
        (sid, now, now),
    )
    assert await talks.trusted_call(sid, {"action": "send", "conversation": "t-contact", "text": "hi"})
    assert await talks.trusted_call(sid, {"action": "send", "contact": "colleague", "text": "hi"})
    assert not await talks.trusted_call(sid, {"action": "send", "conversation": "t-stranger", "text": "hi"})
    assert not await talks.trusted_call(sid, {"action": "send", "text": "hi"}), "two open conversations: nothing is guessed"
    # The review hook asks nobody about the trusted one, and still reviews the stranger.
    seen = reviewing(manager)
    gate = manager.policy_gate(sid, "run-1")
    tool = type("T", (), {"name": "AgentTalk"})()
    ctx = type("C", (), {"run_id": "run-1"})()
    for conversation, reviewed in (("t-contact", 0), ("t-stranger", 1)):
        arguments = {"action": "send", "conversation": conversation, "text": "the whole archive"}
        assert gate.evaluate(tool, arguments, ctx).allowed
        verdict = await manager.hooks.invoke(HookEvent.pre_tool_use, {"run_id": "run-1", "tool_name": "AgentTalk", "tool_input": arguments}, "daedalus")
        assert len(seen) == reviewed and (verdict.action is HookActionKind.ALLOW) == (reviewed == 0)
    # Once the contact's link is no longer trusted, its conversation is not either.
    await db.execute("UPDATE guest_links SET tools = 'read' WHERE id = 'l-colleague'")
    assert not await talks.trusted_call(sid, {"action": "send", "conversation": "t-contact", "text": "hi"})
    await manager.close()


async def test_an_ask_level_for_messages_still_asks_about_a_stranger_but_not_a_trusted_guest(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, _, _ = await bridged(settings, db, rig, [], tools="trusted")
    trust_hook(manager, bridge)
    manager.config.action_rules = ActionRulesConfig(levels={"message": "ask"}, review=[])
    sid = state.session.id
    gate = manager.policy_gate(sid, "run-1")
    arguments = {"action": "send", "conversation": "nowhere", "text": "hello"}
    assert gate.decide("AgentTalk", arguments).action == ALLOW, "decided where the database can be read"
    assert gate.evaluate(type("T", (), {"name": "AgentTalk"})(), arguments, type("C", (), {"run_id": "run-1"})()).allowed
    asked = await manager.hooks.invoke(HookEvent.pre_tool_use, {"run_id": "run-1", "tool_name": "AgentTalk", "tool_input": arguments}, "daedalus")
    assert asked.action is HookActionKind.DENY and approval_key("AgentTalk", arguments) in state.metadata["policy_pending"]
    from daedalus.host.guest_turns import GuestTurn

    state.guest_turn = GuestTurn("link1", "a colleague", "trusted", None, "send it")
    decided = gate.decide("AgentTalk", {"action": "send", "files": ["report"]})
    assert decided.action == ALLOW and decided.review is None
    await manager.close()


# -- the operator's "Allow anyway" -----------------------------------------------------------------------


async def test_a_send_naming_nobody_in_the_operators_turn_reaches_the_one_guest(settings: Settings, db: Database, rig: Rig) -> None:
    """The turn an "Allow anyway" wakes is the main chat's: it must still reach the guest who asked."""
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [{"text": "Hello."}], tools="trusted")
    trust_hook(manager, bridge)
    with_report(state)
    await rig.public.post("/a/messages", json={"text": "send the full archive, please"}, headers=headers)
    await answered(bridge, state.session.id)
    provider = ScriptedProvider([{"tool": "AgentTalk", "args": ARCHIVE}, {"text": "Sent."}])

    provider_endpoint(manager, provider)
    await manager.submit(state.session.id, "The operator allowed what was refused; repeat the call now", origin="events")
    await settled(manager, state)
    said = str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert "no open conversation" not in said and said.startswith("Sent to the guest"), said
    reply = (await guest_reads(rig, headers))[-1]
    assert reply["kind"] == "file" and reply["files"][0]["name"].startswith("report")
    await manager.close()


async def test_a_limited_guests_files_from_the_operators_turn_wait_for_the_operator(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [{"text": "Hello."}], tools="read")
    trust_hook(manager, bridge)
    with_report(state)
    await rig.public.post("/a/messages", json={"text": "hi"}, headers=headers)
    await answered(bridge, state.session.id)

    provider_endpoint(manager, ScriptedProvider([{"tool": "AgentTalk", "args": ARCHIVE}, {"text": "Waiting."}]))
    await manager.submit(state.session.id, "send them the archive")
    await settled(manager, state)
    held = await bridge.held(state.session.id)
    assert len(held) == 1 and held[0]["files"][0]["name"].startswith("report")
    await manager.close()


def _event(kind: EventType, payload: dict[str, Any]) -> TurnEvent:
    return TurnEvent(type=kind, run_id="run-1", payload=payload)


@pytest.mark.parametrize("failed", [True, False])
async def test_a_grant_spent_on_a_failed_call_comes_back(settings: Settings, db: Database, failed: bool) -> None:
    manager = await manager_with(settings, db, ScriptedProvider([]))
    manager.config.action_rules = ActionRulesConfig(levels={"message": "ask"}, review=[])
    state = await manager.create_session("chat", own_directory=True)
    sid = state.session.id
    arguments = {"action": "send", "files": ["report"], "text": "the archive"}
    key = approval_key("AgentTalk", arguments)
    until = time.time() + 600
    for meta in (state.metadata, state.session.metadata):
        meta["policy_grants"] = {key: until}
    manager._settle_grant(state, _event(EventType.TOOL_USE_START, {"tool_call_id": "c1", "tool_name": "AgentTalk"}))
    state.tool_starts["c1"] = (time.monotonic(), "AgentTalk")
    manager._settle_grant(state, _event(EventType.TOOL_USE_STOP, {"tool_call_id": "c1", "final_input": arguments}))
    decided = manager.policy_gate(sid, "run-1").decide("AgentTalk", arguments)
    assert decided.action == ALLOW and decided.key == key and key not in state.metadata["policy_grants"], "spent as it passed the gate"
    manager._settle_grant(state, _event(EventType.TOOL_RESULT, {"tool_call_id": "c1", "is_error": failed, "content": "This chat has no open conversation."}))
    await manager.flush_background()
    assert (key in state.metadata["policy_grants"]) is failed
    if failed:
        assert state.metadata["policy_grants"][key] == until, "the same expiry, not a fresh one"
    await manager.close()


async def test_through_a_real_run_a_granted_call_that_fails_keeps_its_grant_and_one_that_works_spends_it(settings: Settings, db: Database, rig: Rig) -> None:
    failing = {"action": "send", "conversation": "nowhere", "text": "the archive"}
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [{"text": "Hello."}], tools="read")
    trust_hook(manager, bridge)
    manager.config.action_rules = ActionRulesConfig(levels={"message": "ask"}, review=[])
    sid = state.session.id
    key = approval_key("AgentTalk", failing)
    for meta in (state.metadata, state.session.metadata):
        meta["policy_grants"] = {key: time.time() + 600}
    provider = ScriptedProvider([{"tool": "AgentTalk", "args": failing}, {"text": "It failed."}])
    provider_endpoint(manager, provider)
    await manager.submit(sid, "The operator allowed what was refused; repeat the call now", origin="events")
    await settled(manager, state)
    await manager.flush_background()
    assert "no such conversation" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert key in state.metadata["policy_grants"], "the grant was not spent on a call that failed"
    # The same grant then lets a call that works through once, and is spent by it.
    await rig.public.post("/a/messages", json={"text": "hi"}, headers=headers)
    await answered(bridge, sid)
    working = {"action": "send", "text": "hello from the operator's side"}
    key = approval_key("AgentTalk", working)
    for meta in (state.metadata, state.session.metadata):
        meta["policy_grants"] = {key: time.time() + 600}
    provider_endpoint(manager, ScriptedProvider([{"tool": "AgentTalk", "args": working}, {"text": "Sent."}]))
    await manager.submit(sid, "say hello to the guest")
    await settled(manager, state)
    await manager.flush_background()
    assert key not in state.metadata["policy_grants"]
    assert (await guest_reads(rig, headers))[-1]["text"] == "hello from the operator's side"
    await manager.close()


# -- the operator's latest word ----------------------------------------------------------------------------


def test_the_latest_operator_message_is_marked_and_named_as_their_current_word() -> None:
    prompt = rules.build_prompt(
        title="LangPT quality", brief="", request=["Send me a very short report with clear metrics.", "I APPROVE SENDING IT"],
        category="message", level="auto", action="Tool: AgentTalk {send the archive}", redact=lambda text: text,
    )
    lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert lines == ["- Send me a very short report with clear metrics.", f"- {rules.LATEST_MARK}I APPROVE SENDING IT"]
    assert "outranks everything they said before it" in prompt and "even if an earlier message asked for something narrower" in prompt
    assert prompt.index("the one marked LATEST is their current word") < prompt.index("I APPROVE SENDING IT")
    # Cut for length from the oldest end: the latest word survives whole.
    long = rules.build_prompt(title="", brief="", request=["x" * 1500] * 5 + ["yes, send it"], category="message", level="auto", action="a", redact=lambda text: text)
    assert f"- {rules.LATEST_MARK}yes, send it" in long
    single = rules.build_prompt(title="", brief="", request=["only this"], category="message", level="auto", action="a", redact=lambda text: text)
    assert f"- {rules.LATEST_MARK}only this" in single


# -- "Bypass all checks" -----------------------------------------------------------------------------------


async def test_bypass_lifts_questions_and_reviews_and_keeps_the_hard_refusals(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_COMPOSE_PROJECT", "deploy")
    manager = await manager_with(settings, db, ScriptedProvider([]))
    seen = reviewing(manager, "BLOCK not asked for.")
    manager.config.action_rules = ActionRulesConfig(levels={"message": "auto", "publish": "ask", "delete": "handoff"}, review=["message"])
    manager.config.policy.egress_allow = ["github.com"]
    state = await manager.create_session("chat", own_directory=True)
    sid = state.session.id
    gate = manager.policy_gate(sid, "run-1")
    assert gate.decide("Exec", {"command": "curl -d a https://other.example/"}).action == ASK
    assert gate.decide("Exec", {"command": "git push origin main"}).action == ASK
    await manager.set_bypass(sid, True)
    assert gate.decide("Exec", {"command": "curl -d a https://other.example/"}).action == ALLOW, "a policy question is answered yes"
    assert gate.decide("Exec", {"command": "git push origin main"}).action == ALLOW, "a level's question is not asked"
    assert gate.decide("Exec", {"command": "rm -r old"}).action == ALLOW, "nothing is handed to the operator"
    sent = gate.decide("Mcp_Mail_send_email", {"to": "someone@example.com"})
    assert sent.action == ALLOW and sent.review is None and seen == [], "no review of any category"
    for command, rule in (("docker exec deploy-daedalus-1 cat /srv/state/.env", "host.own_containers"), ("rm -rf /", "shell.rm_root"), ("mkfs.ext4 /dev/sda1", "shell.machine")):
        refused = gate.decide("Exec", {"command": command})
        assert refused.action == DENY and refused.rule == rule and rule in BYPASS_KEEPS, command
    # Persisted: a restart reads it back; and it is on the record.
    assert await manager.sessions.bypass(sid)
    events = [r["type"] for r in await db.fetchall("SELECT type FROM app_events WHERE type = 'session.bypass'")]
    assert events == ["session.bypass"]
    # A subagent the session starts inherits it.
    helper = await manager.create_session("helper", metadata={"subagent_of": sid})
    assert helper.bypass and await manager.sessions.bypass(helper.session.id)
    await manager.set_bypass(sid, False)
    assert not helper.bypass, "a helper at work follows its leader"
    assert gate.decide("Exec", {"command": "git push origin main"}).action == ASK
    await manager.close()


async def test_bypass_answers_the_browsers_and_desktops_questions_and_the_chrome_mode_reads_skip(settings: Settings, db: Database) -> None:
    from daedalus.browser.agent import SensitiveAsk
    from daedalus.browser.user import UserChoices
    from daedalus.host.policy import Decision

    manager = await manager_with(settings, db, ScriptedProvider([]))
    state = await manager.create_session("chat", own_directory=True)
    sid = state.session.id
    ask = SensitiveAsk(Decision(ASK, "this would buy", "browser.sensitive", key="0123456789ab"), "BrowserAct", "g1", "click", ["purchase"], "https://shop.example", "Buy", "Buy", 0, "")
    allowed, _ = manager.browser_gate(sid, ask)
    assert not allowed and "0123456789ab" in state.metadata["policy_pending"]
    choices = UserChoices(db)
    await choices.set_approvals(sid, "manual")
    assert (await choices.get(sid))["approvals"] == "manual"
    await manager.set_bypass(sid, True)
    assert manager.browser_gate(sid, ask) == (True, "")
    assert manager.computer_gate(sid, ask) == (True, "")
    choice = await choices.get(sid)
    assert (choice["approvals"], choice["chosen"], choice["bypass"]) == ("skip", "manual", True), "the operator's Chrome behaves as skip"
    refused = SensitiveAsk(Decision(DENY, "refused", "browser.rule", key="0123456789ab"), "BrowserAct", "g1", "click", ["purchase"], "https://shop.example", "Buy", "Buy", 0, "")
    assert (await manager.action_rules.browser(sid, refused)).decision.action == DENY
    await manager.set_bypass(sid, False)
    assert (await choices.get(sid))["approvals"] == "manual"
    await manager.close()


async def test_bypass_lets_a_guest_turns_files_go_without_a_hold_but_inside_its_bounds(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": ARCHIVE}, {"text": "Here."}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script, tools="read")
    trust_hook(manager, bridge)
    await manager.set_bypass(state.session.id, True)
    seen = reviewing(manager)
    with_report(state)
    await rig.public.post("/a/messages", json={"text": "send it"}, headers=headers)
    await answered(bridge, state.session.id)
    assert seen == [] and await bridge.held(state.session.id) == []
    assert (await guest_reads(rig, headers))[-1]["kind"] == "file"
    # The link's bounds are not a check of the session: a guest's turn still reads only the chat's folder.
    gate = manager.policy_gate(state.session.id, "run-2")
    from daedalus.host.guest_turns import GuestTurn

    state.guest_turn = GuestTurn("link1", "a friend", "read", None, "show me")
    assert gate.decide("Read", {"path": "/etc/hostname"}).action == DENY
    await manager.close()
