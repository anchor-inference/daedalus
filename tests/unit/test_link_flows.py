"""What agents do together over a link: the rules on their own, and the flows through real doors in process.

The doors are real and the model is scripted, as in ``test_link_kinds_host``: what these prove is where things land,
what waits for the operator and what each link's level allows, not what a model would choose to do.
"""
# ruff: noqa: F811 — the rigs' fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions import link_flows as flows_module
from daedalus.extensions.link_flows import APPROVE_HOOK, LinkFlows, approval_key_of
from daedalus.host import link_flows
from daedalus.host.link_flows import PatchError
from daedalus.stores.database import Database
from tests.unit.test_agent_talk import BASE, LINK_FIELDS, opened, quick, rig_for, until  # noqa: F401 — fixtures by name
from tests.unit.test_guest_turns import Rig, pump, rig, texts  # noqa: F401
from tests.unit.test_link_kinds_host import FarHost, answered, bridged, direct_door_link, guest_reads

pytestmark = pytest.mark.usefixtures("bare_sandbox")

SOON = (datetime.now(UTC) + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)


# -- the rules ----------------------------------------------------------------------------------------------


def test_each_level_has_what_it_may_and_a_guest_that_is_not_trusted_never_sees_titles() -> None:
    assert [link_flows.standing(level, "apply_patch") for level in ("trusted", "all", "read", "none")] == ["now", "ask", "ask", "no"]
    assert [link_flows.standing(level, "share_service") for level in ("trusted", "all", "read", "none")] == ["now", "ask", "ask", "ask"]
    assert [link_flows.standing(level, "meet") for level in ("trusted", "all", "read", "none")] == ["now", "ask", "ask", "ask"]
    assert [link_flows.standing(level, "busy_titles") for level in ("trusted", "all", "read", "none")] == ["now", "no", "no", "no"]
    assert link_flows.standing("trusted", "anything else") == "no", "an action nobody named is nobody's"
    assert link_flows.standing("owner", "subscribe") == "no", "a level nobody named has nothing"


def test_a_card_from_the_other_side_keeps_only_what_is_named_and_checked() -> None:
    assert link_flows.card_view({"type": "script", "run": "rm -rf /"}) is None
    summary = link_flows.card_view({
        "type": "summary", "title": "Prices\x1b[31m", "promises": [{"by": "sender", "what": "send the list", "due": "Friday", "exec": "x"}, {"by": "operator", "what": "?"}],
        "links": ["https://example.test/a", "javascript:alert(1)"], "files": ["a.pdf"], "html": "<script>",
    })
    assert summary is not None and set(summary) == {"type", "title", "promises", "links", "files", "note"}
    assert summary["title"] == "Prices" and summary["promises"] == [{"by": "sender", "what": "send the list", "due": "Friday"}]
    assert summary["links"] == ["https://example.test/a"]
    assert link_flows.card_view({"type": "meeting", "title": "x", "start": "2026-11-12T10:00:00", "end": "2026-11-12T11:00:00"}) is None, "a time without its zone is a different hour on each side"
    assert link_flows.card_view({"type": "meeting", "title": "x", "start": "2026-11-12T11:00:00+00:00", "end": "2026-11-12T10:00:00+00:00"}) is None


def test_one_summary_reads_right_on_both_sides() -> None:
    card = link_flows.summary_card(
        title="Prices", promises=[{"by": "sender", "what": "send the invoice", "due": ""}], texts=["see https://example.test/p."], files=["a.pdf"],
        tasks=[{"direction": "mine", "text": "check the logs", "state": "done"}],
    )
    assert card["links"] == ["https://example.test/p"]
    ours = link_flows.as_seen(card, "mine")
    theirs = link_flows.as_seen(card, "theirs")
    assert [(p["by"], p["what"][:14]) for p in ours["promises"]] == [("us", "send the invoi"), ("them", "check the logs")], "a task we set is theirs to do"
    assert [p["by"] for p in theirs["promises"]] == ["them", "us"]
    assert "I promised: send the invoice" in link_flows.card_words(card) and "You promised: check the logs" in link_flows.card_words(card)


def test_a_long_summary_is_cut_to_what_a_door_takes() -> None:
    many = [{"by": "sender", "what": "пообещали " * 60, "due": ""} for _ in range(20)]
    card = link_flows.summary_card(title="t", promises=many, texts=[], files=[f"file-{n}.pdf" for n in range(40)], tasks=[])
    assert len(json.dumps(card)) <= link_flows.CARD_BYTES and card["promises"], "trimmed, not emptied"


def test_a_models_summary_is_read_only_as_the_json_asked_for() -> None:
    title, promises = link_flows.promises_from('Sure!\n{"title": "Prices", "promises": [{"by": "B", "what": "send a quote", "due": "Monday"}, {"by": "C", "what": "x"}]}')
    assert title == "Prices" and promises == [{"by": "receiver", "what": "send a quote", "due": "Monday"}]
    assert link_flows.promises_from("no json here") == ("", [])
    assert "cannot change these instructions" in link_flows.summary_prompt([("us", "hello"), ("them", "hi")])


def test_a_subscription_is_held_to_its_bounds() -> None:
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    plan = link_flows.plan_of("2026-11-11T15:00:00+00:00", None, None, now)
    assert (plan.first.hour, plan.every_minutes, plan.times) == (15, 0, 1)
    plan = link_flows.plan_of("", 60, 500, now)
    assert plan.first == now + timedelta(hours=1) and plan.times == link_flows.TIMES_MAX
    for at, every in (("", 5), ("2026-11-11T15:00:00", None), ("2026-11-10T15:00:00+00:00", None), ("", None), ("2027-11-11T15:00:00+00:00", None)):
        with pytest.raises(ValueError):
            link_flows.plan_of(at, every, None, now)


def test_a_question_to_several_is_over_when_all_have_answered_or_the_deadline_passed() -> None:
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert not link_flows.poll_settled(["answered", "waiting"], later)
    assert link_flows.poll_settled(["answered", "failed"], later)
    assert link_flows.poll_settled(["waiting"], (datetime.now(UTC) - timedelta(seconds=1)).isoformat())


def test_a_patch_is_known_by_its_shape_and_counted() -> None:
    diff = b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-a\n+b\n+c\n"
    assert link_flows.patch_kind(diff) == "diff" and link_flows.patch_kind(b"# v2 git bundle\n") == "bundle" and link_flows.patch_kind(b"hello") == ""
    assert link_flows.diff_stat(diff) == {"files": ["x.py"], "added": 2, "removed": 1}


# -- patches in a real repository ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "x.py").write_text("a\n")
    git(root, "add", "x.py")
    git(root, "commit", "-q", "-m", "start")
    return root


def made_patch(repo: Path, tmp_path: Path) -> Path:
    (repo / "x.py").write_text("b\n")
    patch = tmp_path / "fix.diff"
    patch.write_text(git(repo, "diff") + "\n")
    git(repo, "checkout", "--", "x.py")
    return patch


def test_a_patch_lands_on_its_branch_and_never_in_the_operators_tree(repo: Path, tmp_path: Path) -> None:
    patch = made_patch(repo, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch hook-ran\nexit 1\n")
    hook.chmod(0o755)
    commit = link_flows.apply_patch(repo, patch, "fix/x", "from a friend")
    assert git(repo, "rev-parse", "fix/x") == commit
    assert git(repo, "show", f"{commit}:x.py") == "b"
    assert (repo / "x.py").read_text() == "a\n" and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main", "the operator's tree is as it was"
    assert not (repo / "hook-ran").exists(), "no hook runs on a stranger's patch"
    assert git(repo, "worktree", "list").count("\n") == 0, "the scratch tree is gone"
    with pytest.raises(PatchError, match="checked out"):
        link_flows.apply_patch(repo, patch, "main", "x")
    with pytest.raises(PatchError, match="branch"):
        link_flows.apply_patch(repo, patch, "../evil", "x")


def test_a_bundle_only_moves_its_branch_forward(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "feature")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "checkout", "-q", "feature")
    (clone / "y.py").write_text("new\n")
    git(clone, "add", "y.py")
    git(clone, "commit", "-q", "-m", "more")
    bundle = tmp_path / "work.bundle"
    git(clone, "bundle", "create", str(bundle), "feature")
    tip = link_flows.apply_patch(repo, bundle, "feature", "x")
    assert tip == git(clone, "rev-parse", "HEAD") == git(repo, "rev-parse", "feature")
    (repo / "z.py").write_text("other\n")
    git(repo, "checkout", "-q", "-b", "elsewhere")
    git(repo, "add", "z.py")
    git(repo, "commit", "-q", "-m", "diverged")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(PatchError, match="throw commits away"):
        link_flows.apply_patch(repo, bundle, "elsewhere", "x")


# -- cards through the door ---------------------------------------------------------------------------------


async def with_flows(bridge: Any) -> LinkFlows:
    flows = LinkFlows(bridge.app)
    bridge.app.extensions[flows_module.EXTENSION_KEY] = flows
    bridge.app.manager.answer_hooks[APPROVE_HOOK] = flows.approved
    return flows


async def test_a_card_alone_reaches_the_host_without_a_turn_and_one_with_words_is_read_inside_the_fence(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, [{"text": "Thanks."}])
    flows = await with_flows(bridge)
    link_id = next(iter(await bridge.links_of(state.session.id)))
    summary = {"type": "summary", "title": "Prices", "promises": [{"by": "sender", "what": "pay on Friday", "due": ""}], "links": [], "files": []}
    alone = await rig.public.post("/a/messages", json={"kind": "status", "card": summary, "text": "our summary"}, headers=headers)
    assert alone.status_code == 200, alone.text
    await pump(bridge)
    assert not provider.requests, "a card on its own spends no turn"
    cards = await flows.cards_of("door", link_id)
    assert len(cards) == 1 and cards[0]["direction"] == "theirs" and cards[0]["card"]["promises"][0]["by"] == "them"
    slots = {"type": "slots", "title": "a call", "duration_minutes": 30, "slots": [{"start": SOON.isoformat(), "end": (SOON + timedelta(hours=1)).isoformat()}]}
    await rig.public.post("/a/messages", json={"text": "when suits you?", "card": slots}, headers=headers)
    await answered(bridge, state.session.id)
    said = texts(provider.requests[0].messages[-1])
    assert "Free times for a call" in said and said.index("when suits you?") < said.index("Free times"), "the card is read as the guest's, inside the fence"
    assert [card["type"] for card in await flows.cards_of("door", link_id)] == ["summary", "slots"]
    for bad in ({"type": "Not A Word"}, {"type": "summary", "promises": ["x" * 4000] * 10}, ["not", "an", "object"]):
        refused = await rig.public.post("/a/messages", json={"text": "x", "card": bad}, headers=headers)
        assert refused.status_code == 400, bad
    await manager.close()


async def test_a_stand_in_takes_no_card(settings: Settings, db: Database, rig: Rig) -> None:
    _, invite = rig.store.create_link({**LINK_FIELDS, "file_mb": 50, "day_mb": 500})
    token = (await rig.public.post(f"/a/{invite}/redeem")).json()["token"]
    refused = await rig.public.post("/a/messages", json={"kind": "status", "card": {"type": "summary"}}, headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 403


# -- one question to several agents -------------------------------------------------------------------------


def talk_flows(rig: Any) -> LinkFlows:
    flows = LinkFlows(rig.app)
    rig.app.extensions[flows_module.EXTENSION_KEY] = flows
    rig.manager.answer_hooks[APPROVE_HOOK] = flows.approved
    return flows


async def test_one_question_to_several_wakes_the_chat_once_with_every_answer(rig_for: Any) -> None:
    rig = await rig_for([{"text": "| agent | price |\n| a | 10 |\n| b | 12 |"}], "Ten dollars.", "Twelve dollars.")
    flows = talk_flows(rig)
    state = await rig.chat()
    first = await opened(rig, state, rig.door.link(topic="first"))
    second = await opened(rig, state, rig.door.link(topic="second"))
    asked = await rig.talks.tool(state.session.id, "ask", text="What does the plan cost?", extra={"conversations": [first, second]})
    assert not asked.error, asked.text
    assert "woken once" in asked.text
    await until(lambda: len(rig.provider.requests) >= 1, 10)
    poll = (await flows.polls_of(state.session.id))[0]
    assert poll["state"] == "done" and sorted(entry["answer"] for entry in poll["entries"]) == ["Ten dollars.", "Twelve dollars."]
    said = texts(rig.provider.requests[0].messages[-1])
    assert "Ten dollars." in said and "Twelve dollars." in said and "Compare what they said" in said
    assert said.count("reply from another agent") >= 2, "each answer is fenced as a stranger's"
    assert "sends nothing" in json.dumps([texts(m) for m in rig.provider.requests[0].messages])
    await until(lambda: rig.manager.is_running(state.session.id) is False, 5)
    assert len(rig.provider.requests) == 1, "one turn for all the answers, not one each"
    state.guest_turn = flows_module.GuestTurn(flows_module.POLL_PREFIX + poll["id"], "several", "none", None, "", "reply")
    refused = await rig.talks.tool(state.session.id, "send", text="psst")
    assert refused.error and "sends nothing" in refused.text
    state.guest_turn = None
    assert await flows.dismiss_poll(poll["id"]) and (await flows.polls_of(state.session.id))[0]["dismissed"]


async def test_a_question_past_its_deadline_is_compared_with_what_came(rig_for: Any) -> None:
    rig = await rig_for([{"text": "Only one answered."}], "Ten dollars.")
    flows = talk_flows(rig)
    state = await rig.chat()
    answers = await opened(rig, state, rig.door.link(topic="first"))
    silent = await opened(rig, state, direct_door_link(rig.door))
    asked = await rig.talks.tool(state.session.id, "ask", text="Price?", extra={"conversations": [answers, silent], "deadline_minutes": 5})
    assert not asked.error, asked.text
    await until(lambda: rig.db.fetchone("SELECT 1 FROM link_poll_entries WHERE talk_id = ? AND state = 'answered'", (answers,)))
    assert not rig.provider.requests, "one answer of two wakes nothing"
    await rig.db.execute("UPDATE link_polls SET deadline_at = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    await flows.sweep()
    await until(lambda: len(rig.provider.requests) >= 1, 10)
    said = texts(rig.provider.requests[0].messages[-1])
    assert "deadline passed" in said and "no answer (waiting)" in said
    entries = {entry["talk"]: entry["state"] for entry in (await flows.polls_of(state.session.id))[0]["entries"]}
    assert entries == {answers: "answered", silent: "waiting"}


async def test_a_question_needs_several_open_conversations_of_this_chat(rig_for: Any) -> None:
    rig = await rig_for([])
    talk_flows(rig)
    state = await rig.chat()
    only = await opened(rig, state, rig.door.link())
    for conversations in ([only], [only, "nope"]):
        refused = await rig.talks.tool(state.session.id, "ask", text="?", extra={"conversations": conversations})
        assert refused.error


# -- a summary on both sides --------------------------------------------------------------------------------


async def test_our_summary_reaches_their_host_as_a_card_and_becomes_a_note(rig_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await rig_for([])
    flows = talk_flows(rig)

    async def promises(session_id: str, lines: Any) -> Any:
        return "Prices", [{"by": "sender", "what": "send the quote", "due": "Monday"}, {"by": "receiver", "what": "confirm the order", "due": ""}]

    monkeypatch.setattr(flows, "_promises", promises)
    state = await rig.chat()
    talk_id = await opened(rig, state, direct_door_link(rig.door))
    done = await rig.talks.tool(state.session.id, "summary")
    assert not done.error, done.text
    far = FarHost(rig.door)
    feed = (await far.control.get("/control/feed?after=0&wait=0", headers=far.headers)).json()
    assert not feed["deliveries"], "a summary spends the other side no turn"
    card_event = next(event for event in feed["events"] if event["kind"] == "card")
    assert "send the quote" in card_event["detail"]
    view = await rig.talks.describe(await rig.talks.talk(talk_id), messages=True)
    assert view["messages"][-1]["card"]["promises"][0] == {"by": "us", "what": "send the quote", "due": "Monday"}
    assert view["cards"][0]["direction"] == "mine"
    note = await flows.to_notes(view["cards"][0]["id"])
    body = (await rig.db.fetchone("SELECT body FROM notes WHERE id = ?", (note["note_id"],)))["body"]
    assert "**We promised**\n- [ ] send the quote (by Monday)" in body and "**They promised**\n- [ ] confirm the order" in body


async def test_a_summary_from_the_other_side_is_kept_and_wakes_nothing(rig_for: Any) -> None:
    rig = await rig_for([{"text": "should not run"}])
    flows = talk_flows(rig)
    state = await rig.chat()
    talk_id = await opened(rig, state, direct_door_link(rig.door))
    await rig.talks.configure(talk_id, wake_turns=-1, wake_usd=-1)
    await rig.talks.tool(state.session.id, "send", text="hello", wait_seconds=0)
    far = FarHost(rig.door)
    item = (await far.claim())[0]
    card = {"type": "summary", "title": "x", "promises": [{"by": "sender", "what": "ship it", "due": "Friday"}], "links": [], "files": []}
    said = await far.control.post(f"/control/conversations/{item['conversation_id']}/messages", json={"text": "", "card": card, "kind": "text"}, headers=far.headers)
    assert said.status_code == 422, "a card alone from the host still says something"
    said = await far.control.post(f"/control/conversations/{item['conversation_id']}/messages", json={"text": "Our summary.", "card": card}, headers=far.headers)
    assert said.status_code == 200, said.text
    rig.talks.follow(talk_id)
    await until(lambda: rig.db.fetchone("SELECT 1 FROM link_cards WHERE owner = ? AND direction = 'theirs'", (talk_id,)))
    kept = await flows.cards_of("talk", talk_id)
    assert kept[0]["card"]["promises"][0]["by"] == "them"
    await asyncio.sleep(0.3)
    assert not rig.provider.requests, "a summary asks nothing back, so it opens no turn"


async def test_the_summary_of_our_link_goes_to_the_guest_with_its_card(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [{"text": "Done: three errors."}])
    flows = await with_flows(bridge)
    link_id = next(iter(await bridge.links_of(state.session.id)))
    await rig.public.post("/a/messages", json={"kind": "task", "text": "summarise the logs"}, headers=headers)
    await answered(bridge, state.session.id)
    card = await flows.summarise_link(link_id)
    assert card["promises"][0]["by"] == "sender" and "summarise the logs" in card["promises"][0]["what"], "a task the guest set is our promise"
    shown = (await guest_reads(rig, headers))[-1]
    assert shown["card"]["type"] == "summary" and "I promised: summarise the logs" in shown["text"]
    assert (await bridge.exchange(link_id))["cards"][0]["card"]["promises"][0]["by"] == "us"
    await manager.close()


# -- a guest's "tell me when" -------------------------------------------------------------------------------


async def test_a_guest_is_told_later_and_a_check_with_nothing_to_tell_says_nothing(settings: Settings, db: Database, rig: Rig) -> None:
    script = [
        {"tool": "AgentTalk", "args": {"action": "subscribe", "text": "tell them when the build is green", "every_minutes": 15, "times": 2}},
        {"text": "I will tell you."},
        {"text": "Still red. [nothing yet]"},
        {"text": "The build is green."},
    ]
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, script)
    flows = await with_flows(bridge)
    await rig.public.post("/a/messages", json={"text": "tell me when the build is green"}, headers=headers)
    await answered(bridge, state.session.id)
    row = await db.fetchone("SELECT * FROM link_subscriptions")
    assert row["state"] == "active" and row["times_left"] == 2 and "Subscribed" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    for _ in range(2):
        await db.execute("UPDATE link_subscriptions SET next_at = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
        await flows.fire_due()
        await answered(bridge, state.session.id)
    said = texts(provider.requests[2].messages[-1])
    assert "check 1 of 2" in said and "tell them when the build is green" in said and "guest message" in said, "the request is fenced as the guest's"
    agent_lines = [m["text"] for m in await guest_reads(rig, headers) if m["from"] == "agent"]
    assert agent_lines == ["I will tell you.", "The build is green."], "nothing to tell sent nothing"
    row = await db.fetchone("SELECT * FROM link_subscriptions")
    assert (row["state"], row["reason"], row["fired"]) == ("ended", "done", 2)
    turns = rig.store.link(row["link_id"])["turns_used"]
    assert turns == 3, "each check is a counted turn of the link"
    await manager.close()


async def test_a_subscription_ends_with_its_link(settings: Settings, db: Database, rig: Rig) -> None:
    at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    script = [{"tool": "AgentTalk", "args": {"action": "subscribe", "text": "remind them", "at": at}}, {"text": "ok"}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script, tools="none")
    await with_flows(bridge)
    await rig.public.post("/a/messages", json={"text": "remind me in two hours"}, headers=headers)
    await answered(bridge, state.session.id)
    row = await db.fetchone("SELECT * FROM link_subscriptions")
    assert row["state"] == "active", "a turn with no tools may still be asked to write later"
    await bridge.revoked(row["link_id"])
    assert (await db.fetchone("SELECT state, reason FROM link_subscriptions"))["reason"] == "closed"
    await manager.close()


# -- patches ------------------------------------------------------------------------------------------------


def chat_repo(state: Any, tmp_path: Path) -> Path:
    root = state.workspace
    git(root, "init", "-q", "-b", "main")
    (root / "x.py").write_text("a\n")
    git(root, "add", "x.py")
    git(root, "commit", "-q", "-m", "start")
    (root / "x.py").write_text("b\n")
    (root / "fix.diff").write_text(git(root, "diff") + "\n")
    git(root, "checkout", "--", "x.py")
    return root


PATCH_SCRIPT = [{"tool": "AgentTalk", "args": {"action": "apply_patch", "files": ["fix.diff"], "branch": "fix/x"}}, {"text": "Done."}]


async def test_a_trusted_guests_patch_is_applied_at_once_and_the_commit_reported(settings: Settings, db: Database, rig: Rig, tmp_path: Path) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, PATCH_SCRIPT, tools="trusted")
    await with_flows(bridge)
    root = chat_repo(state, tmp_path)
    await rig.public.post("/a/messages", json={"kind": "task", "text": "apply my patch to fix/x"}, headers=headers)
    await answered(bridge, state.session.id)
    commit = git(root, "rev-parse", "fix/x")
    assert f"Applied as commit {commit[:12]}" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert any(commit[:12] in m["text"] for m in await guest_reads(rig, headers))
    await manager.close()


async def test_a_limited_guests_patch_waits_for_the_operator(settings: Settings, db: Database, rig: Rig, tmp_path: Path) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, PATCH_SCRIPT * 2, tools="all")
    flows = await with_flows(bridge)
    root = chat_repo(state, tmp_path)
    await rig.public.post("/a/messages", json={"text": "apply my patch"}, headers=headers)
    await answered(bridge, state.session.id)
    action = await db.fetchone("SELECT * FROM link_actions")
    assert action["state"] == "held" and "+1 −1" in json.loads(action["args"])["stat"]
    assert approval_key_of(action["id"]) in (state.metadata.get("policy_pending") or {}), "the operator is asked on a card"
    assert not git(root, "branch", "--list", "fix/x"), "nothing applied before the operator"
    await flows.approved(state.session.id, {"flow": action["id"]}, True)
    commit = git(root, "rev-parse", "fix/x")
    assert any(commit[:12] in m["text"] for m in await guest_reads(rig, headers))
    await rig.public.post("/a/messages", json={"text": "and again"}, headers=headers)
    await answered(bridge, state.session.id)
    second = await db.fetchone("SELECT * FROM link_actions WHERE state = 'held'")
    await flows.approved(state.session.id, {"flow": second["id"]}, False)
    assert "did not let the patch" in (await guest_reads(rig, headers))[-1]["text"]
    await flows.approved(state.session.id, {"flow": second["id"]}, True)
    assert (await db.fetchone("SELECT state FROM link_actions WHERE id = ?", (second["id"],)))["state"] == "refused", "decided once"
    await manager.close()


async def test_a_turn_with_no_tools_applies_no_patch(settings: Settings, db: Database, rig: Rig, tmp_path: Path) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, PATCH_SCRIPT, tools="none")
    await with_flows(bridge)
    chat_repo(state, tmp_path)
    await rig.public.post("/a/messages", json={"text": "apply my patch"}, headers=headers)
    await answered(bridge, state.session.id)
    assert "applies no patch" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert await db.fetchone("SELECT 1 FROM link_actions") is None
    await manager.close()


# -- a running service shown to a guest ---------------------------------------------------------------------


class Services:
    async def get(self, session_id: str, name: str) -> dict[str, Any] | None:
        return {"name": name, "port": 8123, "status": "running"} if name == "web" else None

    async def ensure_slug(self, session_id: str, name: str) -> str:
        return "web-0123456789ab"


SHARE_SCRIPT = [{"tool": "AgentTalk", "args": {"action": "share_service", "service": "web"}}, {"text": "Here it is."}]


@pytest.mark.parametrize("tools", ["trusted", "read"])
async def test_a_guest_gets_a_one_time_address_that_closes_with_the_link(settings: Settings, db: Database, rig: Rig, tools: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flows_module.public_address, "services_base", lambda _: "https://svc.example.test")
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, SHARE_SCRIPT, tools=tools)
    flows = await with_flows(bridge)
    bridge.app.extensions["services"] = Services()
    await rig.public.post("/a/messages", json={"text": "can I see it running?"}, headers=headers)
    await answered(bridge, state.session.id)
    action = await db.fetchone("SELECT * FROM link_actions")
    if tools == "read":
        assert action["state"] == "held" and not [m for m in await guest_reads(rig, headers) if m.get("card")], "a limited link's guest waits for the operator"
        await flows.approved(state.session.id, {"flow": action["id"]}, True)
    shown = next(m for m in await guest_reads(rig, headers) if m.get("card"))
    assert shown["card"]["type"] == "service" and shown["card"]["url"].startswith("https://svc.example.test/s/web-0123456789ab/?phone=")
    assert await db.fetchone("SELECT 1 FROM phone_passes WHERE slug = 'web-0123456789ab'") is not None
    await bridge.revoked(action["owner"])
    assert await db.fetchone("SELECT 1 FROM phone_passes WHERE slug = 'web-0123456789ab'") is None, "the guest's way in ends with the link"
    assert (await db.fetchone("SELECT state FROM link_actions"))["state"] == "revoked"
    await manager.close()


# -- meetings -----------------------------------------------------------------------------------------------


async def dentist(db: Database) -> None:
    from daedalus.stores.calendar import CalendarStore

    await CalendarStore(db).create({"title": "Dentist, root canal", "start_at": SOON.isoformat(), "end_at": (SOON + timedelta(hours=1)).isoformat(), "timezone": "UTC"})


def slots_script() -> list[dict[str, Any]]:
    args = {"action": "slots", "text": "planning", "duration_minutes": 30, "start": (SOON - timedelta(days=1)).isoformat(), "end": (SOON + timedelta(days=3)).isoformat()}
    return [{"tool": "AgentTalk", "args": args}, {"text": "Those are my free times."}]


@pytest.mark.parametrize(("tools", "titles"), [("read", False), ("trusted", True)])
async def test_free_times_tell_a_guest_that_is_not_trusted_nothing_but_busy_or_free(settings: Settings, db: Database, rig: Rig, tools: str, titles: bool) -> None:
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, slots_script(), tools=tools)
    await with_flows(bridge)
    await dentist(db)
    await rig.public.post("/a/messages", json={"text": "when are you free?"}, headers=headers)
    await answered(bridge, state.session.id)
    shown = next(m for m in await guest_reads(rig, headers) if m.get("card"))
    assert shown["card"]["type"] == "slots"
    assert ("Dentist" in json.dumps(shown)) is titles
    await manager.close()


async def test_a_meeting_waits_for_the_operator_and_lands_in_the_calendar(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": {"action": "meet", "text": "Planning", "start": SOON.isoformat(), "end": (SOON + timedelta(minutes=30)).isoformat()}}, {"text": "Asked."}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script)
    flows = await with_flows(bridge)
    await rig.public.post("/a/messages", json={"text": "let us meet then"}, headers=headers)
    await answered(bridge, state.session.id)
    action = await db.fetchone("SELECT * FROM link_actions")
    assert action["state"] == "held" and await db.fetchone("SELECT 1 FROM calendar_events WHERE title LIKE 'Planning%'") is None
    await flows.approved(state.session.id, {"flow": action["id"]}, True)
    assert await db.fetchone("SELECT 1 FROM calendar_events WHERE title LIKE 'Planning%'") is not None
    shown = [m for m in await guest_reads(rig, headers) if m.get("card")][-1]
    assert shown["card"] == {**shown["card"], "type": "meeting", "state": "booked", "title": "Planning"}
    await manager.close()


async def test_our_free_times_go_to_another_door_without_titles_and_our_booking_waits(rig_for: Any) -> None:
    rig = await rig_for([])
    flows = talk_flows(rig)
    await dentist(rig.db)
    state = await rig.chat()
    talk_id = await opened(rig, state, direct_door_link(rig.door))
    window = {"duration_minutes": 30, "start": (SOON - timedelta(days=1)).isoformat(), "end": (SOON + timedelta(days=3)).isoformat()}
    sent = await rig.talks.tool(state.session.id, "slots", text="planning", wait_seconds=0, extra=window)
    assert not sent.error, sent.text
    item = (await FarHost(rig.door).claim())[0]
    assert item["card"]["type"] == "slots" and "busy" not in item["card"] and "Dentist" not in json.dumps(item)
    asked = await rig.talks.tool(state.session.id, "meet", text="Planning", extra={"start": SOON.isoformat(), "end": (SOON + timedelta(minutes=30)).isoformat()})
    assert not asked.error and "waits for the operator" in asked.text
    action = await rig.db.fetchone("SELECT * FROM link_actions WHERE owner = ?", (talk_id,))
    await flows.approved(state.session.id, {"flow": action["id"]}, True)
    assert await rig.db.fetchone("SELECT 1 FROM calendar_events WHERE title LIKE 'Planning%'") is not None
    view = await rig.talks.describe(await rig.talks.talk(talk_id), messages=True)
    assert view["messages"][-1]["card"]["state"] == "booked"


async def test_ending_a_conversation_that_said_something_leaves_a_summary_once(rig_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await rig_for([], "Noted, we ship on Friday.")
    flows = talk_flows(rig)

    async def promises(session_id: str, lines: Any) -> Any:
        return "Shipping", [{"by": "receiver", "what": "ship", "due": "Friday"}]

    monkeypatch.setattr(flows, "_promises", promises)
    state = await rig.chat()
    talk_id = await opened(rig, state, rig.door.link())
    quiet = await opened(rig, state, rig.door.link(topic="other"))
    await rig.talks.tool(state.session.id, "send", conversation=talk_id, text="When do you ship?", wait_seconds=5)
    await rig.talks.end(talk_id)
    await rig.talks.end(quiet)
    assert [card["type"] for card in await flows.cards_of("talk", talk_id)] == ["summary"]
    assert await flows.cards_of("talk", quiet) == [], "nothing was said there, so there is nothing to sum up"


async def test_one_question_can_open_the_links_the_operator_gave(rig_for: Any) -> None:
    rig = await rig_for([{"text": "compared"}], "Ten.", "Twelve.")
    talk_flows(rig)
    state = await rig.chat()
    links = [rig.door.link(topic="a"), rig.door.link(topic="b")]
    await rig.manager.sessions.append_transcript(state.session.id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"ask {links[0]} and {links[1]}")], metadata={"daedalus.origin": "operator"})])
    state.run_origin = "operator"
    asked = await rig.talks.tool(state.session.id, "ask", text="Price?", extra={"conversations": links})
    assert not asked.error, asked.text
    assert len(await rig.talks.talks_of(state.session.id)) == 2
    stranger = rig.door.link(topic="c")
    refused = await rig.talks.tool(state.session.id, "ask", text="Price?", extra={"conversations": [stranger, links[0]]})
    assert refused.error and "did not come from the operator" in refused.text


async def test_our_agent_applies_a_patch_the_other_side_sent_only_once_the_operator_allows(rig_for: Any, tmp_path: Path) -> None:
    rig = await rig_for([])
    flows = talk_flows(rig)
    state = await rig.chat()
    talk_id = await opened(rig, state, direct_door_link(rig.door))
    root = chat_repo(state, tmp_path)
    asked = await rig.talks.tool(state.session.id, "apply_patch", files=["fix.diff"], extra={"branch": "from-them"})
    assert not asked.error and "waits for the operator" in asked.text, asked.text
    assert not git(root, "branch", "--list", "from-them")
    action = await rig.db.fetchone("SELECT * FROM link_actions WHERE owner = ?", (talk_id,))
    await flows.approved(state.session.id, {"flow": action["id"]}, True)
    commit = git(root, "rev-parse", "from-them")
    item = (await FarHost(rig.door).claim())[0]
    assert commit[:12] in item["text"], "the other side hears the commit"
