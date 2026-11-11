"""Files, tasks and questions for the owner, on this side of a link: a guest's at our door, and ours at another's.

The doors are real and in process; the model is scripted. What these prove is where things land and what waits for
the operator, not what a model would choose to do.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from protocore.contracts.tool_registry import policy_admits

from daedalus.config import Settings
from daedalus.extensions import agent_talk as extension
from daedalus.extensions import questions
from daedalus.extensions.guest_door import ChatLine, Delivery, GuestBridge
from daedalus.host import link_protocol
from daedalus.host.guest_turns import GuestTurn
from daedalus.stores.database import Database
from tests.unit.test_agent_talk import BASE, LINK_FIELDS, opened, quick, rig_for, until  # noqa: F401 — fixtures by name
from tests.unit.test_guest_turns import (  # noqa: F401
    Host,
    Rig,
    ScriptedProvider,
    manager_with,
    offered,
    pump,
    rig,
    texts,
)

pytestmark = pytest.mark.usefixtures("bare_sandbox")


class Notices:
    def __init__(self) -> None:
        self.posted: list[Any] = []
        self.resolved: list[str] = []

    def language(self) -> str:
        return "en"

    async def post(self, draft: Any, **_: Any) -> None:
        self.posted.append(draft)

    async def resolve(self, ref: str, *_: Any, **__: Any) -> int:
        self.resolved.append(ref)
        return 1


async def bridged(settings: Settings, db: Database, rig: Rig, script: list[dict[str, Any]], tools: str = "read", **limits: Any) -> tuple[Any, Any, GuestBridge, dict[str, str], ScriptedProvider]:
    provider = ScriptedProvider(script)
    manager = await manager_with(settings, db, provider)
    manager.config.action_rules.review = []  # a review model is one more check on what goes out, and there is none here
    state = await manager.create_session("chat", own_directory=True)
    host = Host(settings, db, manager)
    host.notifications = Notices()
    bridge = GuestBridge(host, rig.client)
    host.extensions["guest_door"] = bridge
    manager.on_finished(bridge.on_run_finished)
    talks = extension.AgentTalks(host)
    host.extensions[extension.EXTENSION_KEY] = talks
    manager.service_hooks["agent_talk"] = talks.tool
    fields = {
        "label": "a friend", "topic": "t", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": tools, "price_in": 0, "price_out": 0, "approval": False,
        "usd_limit": 0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 72, "conversation_hours": 168, "idle_hours": 24, "retention_days": 30, **limits,
    }
    made = await bridge.client.call("POST", "/control/links", json=fields)
    await bridge.remember_link(made["link"]["id"], state.session.id, "a friend", "direct", tools)
    token = (await rig.public.post(f"/a/{made['invite']}/redeem")).json()["token"]
    return manager, state, bridge, {"Authorization": f"Bearer {token}"}, provider


async def guest_upload(rig: Rig, headers: dict[str, str], data: bytes, name: str) -> str:
    made = await rig.public.post("/a/files", json={"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, headers=headers)
    file_id = made.json()["id"]
    put = await rig.public.put(f"/a/files/{file_id}?offset=0", content=data, headers=headers)
    assert put.json()["state"] == "ready"
    return file_id


async def answered(bridge: GuestBridge, session_id: str) -> None:
    await pump(bridge)
    line = bridge.lines.get(session_id)
    if line is not None and line.worker is not None:
        await asyncio.wait_for(line.worker, 20)


async def guest_reads(rig: Rig, headers: dict[str, str]) -> list[dict[str, Any]]:
    return (await rig.public.get("/a/messages?after=0&wait=0", headers=headers)).json()["messages"]


# -- what a guest's turn is offered -----------------------------------------------------------------------


@pytest.mark.parametrize(("tools", "allowed"), [("none", {"AgentTalk"}), ("read", {"Read", "Find", "Search", "ImageView", "AgentTalk"})])
async def test_a_guests_turn_has_the_conversation_and_reading_has_pictures(settings: Settings, db: Database, tools: Any, allowed: set[str]) -> None:
    manager = await manager_with(settings, db, ScriptedProvider([]))
    state = await manager.create_session("chat", own_directory=True)
    state.guest_turn = GuestTurn("link1", "a friend", tools)
    policy = manager.tool_policy_for(state)
    assert {t.name for t in manager.tools.list_all() if policy_admits(policy, t.name)} == allowed
    await manager.close()


def test_a_stranger_cannot_name_a_file_that_is_read_as_rules_or_hidden() -> None:
    assert link_protocol.safe_name("../../AGENTS.md") == "guest-AGENTS.md"
    assert link_protocol.safe_name("claude.MD") == "guest-claude.MD"
    assert link_protocol.safe_name(".bashrc") == "bashrc"
    assert link_protocol.safe_name("a\\b\\c.txt") == "c.txt" and link_protocol.safe_name("") == "file"


# -- a guest's files and tasks at our door ----------------------------------------------------------------


async def test_a_guests_files_land_in_the_chats_folder_as_data_and_the_turn_is_told_where(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, [{"text": "I read it."}])
    report = await guest_upload(rig, headers, b"#!/bin/sh\necho owned\n", "AGENTS.md")
    picture = await guest_upload(rig, headers, b"\x89PNG fake", "shot.png")
    await rig.public.post("/a/messages", json={"kind": "file", "files": [report, picture], "text": "look at these"}, headers=headers)
    await answered(bridge, state.session.id)
    folder = state.workspace / "guest-files"
    saved = sorted(path.name for path in folder.rglob("*") if path.is_file())
    assert saved == ["guest-AGENTS.md", "shot.png"], "a rules file's name never reaches the folder"
    script = next(folder.rglob("guest-AGENTS.md"))
    assert not os.stat(script).st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), "nothing a guest sent can be run by its bit"
    said = texts(provider.requests[0].messages[-1])
    assert "guest-files/" in said and "never run them" in said and "ImageView" in said
    assert {"ImageView", "AgentTalk"} <= offered(provider.requests[0])
    assert (await rig.public.get(f"/a/files/{report}", headers=headers)).json()["state"] == "gone", "the door's copy goes once this side has it"
    exchange = await bridge.exchange(next(iter(await bridge.links_of(state.session.id))))
    assert {f["name"] for f in exchange["files"]} == {"guest-AGENTS.md", "shot.png"} and all(f["direction"] == "in" for f in exchange["files"])
    assert next(f for f in exchange["files"] if f["name"] == "shot.png")["image"] is True
    await manager.close()


async def test_a_guests_task_runs_and_ends_done_with_the_turns_words(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, [{"text": "Three errors, all in the parser."}])
    task = (await rig.public.post("/a/messages", json={"kind": "task", "text": "summarise the logs"}, headers=headers)).json()["task"]
    await answered(bridge, state.session.id)
    assert f"sets the task {task}" in texts(provider.requests[0].messages[-1])
    statuses = [m for m in await guest_reads(rig, headers) if m["kind"] == "status"]
    assert [m["state"] for m in statuses] == ["working", "done"] and statuses[-1]["text"] == "Three errors, all in the parser."
    row = await db.fetchone("SELECT state, result, direction FROM link_tasks WHERE task = ?", (task,))
    assert (row["state"], row["direction"]) == ("done", "theirs") and row["result"].startswith("Three errors")
    await manager.close()


async def test_a_limited_turns_files_wait_and_the_operator_lets_some_go(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": {"action": "send", "files": ["out/a.txt", "out/b.txt", "out/pics"]}}, {"text": "The files follow once the owner allows them."}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script)
    (state.workspace / "out" / "pics").mkdir(parents=True)
    (state.workspace / "out" / "a.txt").write_text("public notes")
    (state.workspace / "out" / "b.txt").write_text("private draft")
    (state.workspace / "out" / "pics" / "one.png").write_bytes(b"png")
    await rig.public.post("/a/messages", json={"text": "send me the notes"}, headers=headers)
    await answered(bridge, state.session.id)
    shown = await guest_reads(rig, headers)
    assert [m["kind"] for m in shown] == ["text"] and "files" not in shown[0], "nothing went before the operator"
    held = await bridge.held(state.session.id)
    assert len(held) == 1 and [f["name"] for f in held[0]["files"]] == ["a.txt", "b.txt", "pics.zip"]
    assert all(f["size"] > 0 and len(f["sha256"]) == 64 for f in held[0]["files"])
    assert bridge.app.notifications.posted[-1].kind == "guest_files"
    drop = [f["id"] for f in held[0]["files"] if f["name"] == "b.txt"]
    assert await bridge.release(held[0]["link_id"], held[0]["batch"], send=True, drop=drop) is True
    assert await bridge.release(held[0]["link_id"], held[0]["batch"], send=True) is False, "decided once"
    shown = await guest_reads(rig, headers)
    sent = next(m for m in shown if m["kind"] == "file")
    assert sorted(f["name"] for f in sent["files"]) == ["a.txt", "pics.zip"]
    assert "b.txt" in shown[-1]["text"] and "did not let" in shown[-1]["text"]
    content = await rig.public.get(f"/a/files/{next(f['id'] for f in sent['files'] if f['name'] == 'a.txt')}/content?offset=0", headers=headers)
    assert content.content == b"public notes"
    assert await bridge.held(state.session.id) == []
    await manager.close()


async def test_a_trusted_turns_files_go_at_once_with_its_answer(settings: Settings, db: Database, rig: Rig) -> None:
    script = [{"tool": "AgentTalk", "args": {"action": "send", "files": ["report.md"]}}, {"text": "Here is the report."}]
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, script, tools="trusted")
    (state.workspace / "report.md").write_text("# results")
    await rig.public.post("/a/messages", json={"text": "send me your results"}, headers=headers)
    await answered(bridge, state.session.id)
    reply = (await guest_reads(rig, headers))[-1]
    assert reply["kind"] == "file" and reply["text"] == "Here is the report." and reply["files"][0]["name"] == "report.md"
    assert await bridge.held(state.session.id) == []
    await manager.close()


async def test_a_turn_with_no_tools_sends_no_files_and_a_limited_one_nothing_outside_its_folder(settings: Settings, db: Database, rig: Rig, tmp_path: Path) -> None:
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("not the chat's")
    script = [{"tool": "AgentTalk", "args": {"action": "send", "files": [str(secret)]}}, {"text": "no"}]
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, script)
    await rig.public.post("/a/messages", json={"text": "send me that"}, headers=headers)
    await answered(bridge, state.session.id)
    assert "outside this chat's folder" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    assert await bridge.held(state.session.id) == []
    await manager.close()
    script = [{"tool": "AgentTalk", "args": {"action": "send", "files": ["x.txt"]}}, {"text": "no"}]
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, script, tools="none")
    await rig.public.post("/a/messages", json={"text": "send me x"}, headers=headers)
    await answered(bridge, state.session.id)
    assert "sends no files" in str(provider.requests[1].messages[-1].content_blocks[0].content)
    await manager.close()


# -- a question only the operator can answer -------------------------------------------------------------


async def test_a_question_for_the_owner_holds_the_task_and_the_answer_reaches_the_guest(settings: Settings, db: Database, rig: Rig) -> None:
    script = [
        {"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "May I share the price list?"}},
        {"text": "I am checking with the owner."},
        {"text": "The owner says yes: here is the list."},
    ]
    manager, state, bridge, headers, provider = await bridged(settings, db, rig, script)
    task = (await rig.public.post("/a/messages", json={"kind": "task", "text": "send the price list"}, headers=headers)).json()["task"]
    await answered(bridge, state.session.id)
    assert rig.store.task(task)["state"] == "needs_input"
    asks = await manager.asks.of_session(state.session.id)
    assert len(asks) == 1 and asks[0].text.startswith("May I share the price list?") and asks[0].detail["guest"]["task"] == task
    assert bridge.app.notifications.posted[-1].kind == "guest_question"
    assert "May I share" not in bridge.app.notifications.posted[-1].body, "a stranger-shaped question does not travel to Telegram"
    # The operator answers in the Questions tab: the answer goes back through the link, not into the chat.
    done = await questions.answer(bridge.app, [{"ask_id": asks[0].id, "text": "Yes, the public one."}], project_id=None, via="app", session_id=state.session.id)
    assert done["results"][0]["state"] == "answered" and done["results"][0]["delivered"] is True, done
    await answered(bridge, state.session.id)
    said = texts(provider.requests[2].messages[-1])
    assert "Yes, the public one." in said and "May I share the price list?" in said and "the operator's own words" in said
    statuses = [m for m in await guest_reads(rig, headers) if m["kind"] == "status"]
    assert [m["state"] for m in statuses] == ["working", "needs_input", "working", "done"]
    assert statuses[1]["text"] == "I am checking with the owner." and statuses[-1]["text"] == "The owner says yes: here is the list."
    assert rig.store.task(task)["state"] == "done"
    await manager.close()


# -- cancelling ------------------------------------------------------------------------------------------


async def test_a_cancelled_task_stops_its_turn_and_drops_what_waits_for_it(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [])
    link_id = next(iter(await bridge.links_of(state.session.id)))
    stopped: list[str] = []

    async def stop_run(session_id: str, run_id: str) -> bool:
        stopped.append(run_id)
        return True

    manager.stop_run = stop_run  # type: ignore[method-assign]
    running = Delivery(1, link_id, "c" * 16, "x", None, run_id="run-1", task="t_000000000001")
    waiting = Delivery(2, link_id, "c" * 16, "y", None, task="t_000000000001")
    other = Delivery(3, link_id, "c" * 16, "z", None, task="t_000000000002")
    bridge.turns["run-1"] = running
    line = bridge.lines.setdefault(state.session.id, ChatLine())
    line.waiting.extend([running, waiting, other])
    await bridge.on_event({"kind": "task_cancelled", "link_id": link_id, "detail": "t_000000000001"})
    assert stopped == ["run-1"]
    assert list(line.waiting) == [running, other], "what waited for the cancelled task went; another task's message stays"
    await manager.close()


async def test_the_operator_cancels_a_guests_task_and_the_guest_hears(settings: Settings, db: Database, rig: Rig) -> None:
    manager, state, bridge, headers, _ = await bridged(settings, db, rig, [{"tool": "AgentTalk", "args": {"action": "ask_owner", "text": "ok?"}}, {"text": "checking"}])
    task = (await rig.public.post("/a/messages", json={"kind": "task", "text": "x"}, headers=headers)).json()["task"]
    await answered(bridge, state.session.id)
    link_id = next(iter(await bridge.links_of(state.session.id)))
    await bridge.cancel_task(link_id, task)
    assert rig.store.task(task)["state"] == "cancelled"
    assert (await guest_reads(rig, headers))[-1]["state"] == "cancelled"
    assert (await bridge.exchange(link_id))["tasks"][0]["state"] == "cancelled"
    await manager.close()


# -- our agent at another's door ---------------------------------------------------------------------------


class FarHost:
    """The other side's host: claims what its door has and answers as the scripted test says."""

    def __init__(self, door: Any) -> None:
        self.control = httpx.AsyncClient(transport=httpx.ASGITransport(door.control), base_url="http://guestd")
        self.headers = {"authorization": "Bearer test"}

    async def claim(self) -> list[dict[str, Any]]:
        return (await self.control.get("/control/feed?after=0&wait=0", headers=self.headers)).json()["deliveries"]

    async def file(self, conversation: str, data: bytes, name: str) -> str:
        made = (await self.control.post(f"/control/conversations/{conversation}/files", json={"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, headers=self.headers)).json()
        await self.control.put(f"/control/files/{made['id']}?offset=0", content=data, headers=self.headers)
        return made["id"]

    async def reply(self, message_id: int, **body: Any) -> None:
        answer = await self.control.post(f"/control/deliveries/{message_id}/reply", json={"ok": True, **body}, headers=self.headers)
        assert answer.status_code == 200, answer.text


def direct_door_link(door: Any) -> str:
    _, invite = door.store.create_link({**LINK_FIELDS, "mode": "direct", "briefing": "", "guest_tools": "read", "file_mb": 50, "day_mb": 500})
    return f"{BASE}/a/{invite}"


async def test_our_agent_sets_a_task_wakes_on_its_result_and_gets_its_file(rig_for: Any) -> None:
    talk_rig = await rig_for([{"text": "The report came back: three errors."}])
    state = await talk_rig.chat()
    talk_id = await opened(talk_rig, state, direct_door_link(talk_rig.door))
    set_task = await talk_rig.talks.tool(state.session.id, "task", text="Check the logs and send a summary", wait_seconds=0)
    assert not set_task.error, set_task.text
    far = FarHost(talk_rig.door)
    item = (await far.claim())[0]
    assert item["kind"] == "task"
    task = item["task"]
    row = await talk_rig.db.fetchone("SELECT * FROM link_tasks WHERE side = 'talk' AND owner = ?", (talk_id,))
    assert (row["task"], row["direction"], row["state"]) == (task, "mine", "accepted")
    assert (await talk_rig.talks.talk(talk_id))["task_wakes"] == 1 and (await talk_rig.talks.talk(talk_id))["wake_turns"] == 0
    summary = await far.file(item["conversation_id"], b"three errors\n", "summary.txt")
    await far.reply(item["message_id"], text="Done.", files=[summary], task_state="done")
    talk_rig.talks.follow(talk_id)
    await until(lambda: talk_rig.db.fetchone("SELECT 1 FROM link_tasks WHERE owner = ? AND state = 'done'", (talk_id,)))
    await until(lambda: talk_rig.db.fetchone("SELECT 1 FROM link_files WHERE owner = ? AND state = 'received'", (talk_id,)))
    saved = state.workspace / "guest-files" / talk_id / "summary.txt"
    assert saved.read_bytes() == b"three errors\n"
    # The task's result woke the chat though waking is off: the side that set a task wakes when it moves.
    await until(lambda: len(talk_rig.provider.requests) >= 1, 10)
    said = texts(talk_rig.provider.requests[0].messages[-1])
    assert "Done." in said and f"guest-files/{talk_id}/summary.txt" in said
    await until(lambda: talk_rig.db.fetchone("SELECT 1 FROM agent_talks WHERE id = ? AND task_wakes = 0", (talk_id,)))
    view = await talk_rig.talks.describe(await talk_rig.talks.talk(talk_id), messages=True)
    assert view["tasks"][0]["state"] == "done" and any(f["name"] == "summary.txt" and f["direction"] == "in" for f in view["files"])
    assert any(m["kind"] == "status" and m["task_state"] == "done" and m["task"] == task for m in view["messages"])


async def test_our_files_wait_for_the_operator_like_a_first_message_and_one_can_be_dropped(rig_for: Any) -> None:
    talk_rig = await rig_for([])
    state = await talk_rig.chat()
    talk_id = await opened(talk_rig, state, direct_door_link(talk_rig.door), review="first")
    (state.workspace / "keep.txt").write_text("for them")
    (state.workspace / "drop.txt").write_text("not for them")
    sent = await talk_rig.talks.tool(state.session.id, "send", text="two files", files=["keep.txt", "drop.txt"], wait_seconds=0)
    assert "with the operator" in sent.text
    message = await talk_rig.db.fetchone("SELECT * FROM agent_talk_messages WHERE talk_id = ? AND state = 'held'", (talk_id,))
    assert message["kind"] == "file"
    files = await talk_rig.db.fetchall("SELECT * FROM link_files WHERE message_id = ? ORDER BY id", (message["id"],))
    assert [(f["name"], f["state"]) for f in files] == [("keep.txt", "held"), ("drop.txt", "held")]
    assert "keep.txt" in str((state.metadata.get("policy_pending") or {}).values())
    assert await talk_rig.talks.decide(talk_id, message["id"], True, drop=[files[1]["id"]]) is True
    far = FarHost(talk_rig.door)
    item = (await far.claim())[0]
    assert [f["name"] for f in item["files"]] == ["keep.txt"]
    states = {f["name"]: f["state"] for f in await talk_rig.db.fetchall("SELECT * FROM link_files WHERE message_id = ?", (message["id"],))}
    assert states == {"keep.txt": "sent", "drop.txt": "dropped"}


async def test_a_file_over_the_conversations_limit_is_not_sent_and_no_limit_lifts_it(rig_for: Any) -> None:
    talk_rig = await rig_for([])
    state = await talk_rig.chat()
    talk_id = await opened(talk_rig, state, direct_door_link(talk_rig.door))
    (state.workspace / "big.bin").write_bytes(b"x" * (link_protocol.MB + 1))
    await talk_rig.talks.configure(talk_id, file_mb=1)
    refused = await talk_rig.talks.tool(state.session.id, "send", files=["big.bin"], wait_seconds=0)
    assert refused.error and "at most 1 MB" in refused.text
    await talk_rig.talks.configure(talk_id, file_mb=0)
    sent = await talk_rig.talks.tool(state.session.id, "send", files=["big.bin"], wait_seconds=0)
    assert not sent.error, sent.text
    item = (await FarHost(talk_rig.door).claim())[0]
    assert item["files"][0]["size"] == link_protocol.MB + 1


async def test_a_file_from_outside_the_chats_folder_waits_whatever_the_review(rig_for: Any, tmp_path: Path) -> None:
    talk_rig = await rig_for([])
    state = await talk_rig.chat()
    await opened(talk_rig, state, direct_door_link(talk_rig.door), review="off")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x")
    sent = await talk_rig.talks.tool(state.session.id, "send", files=[str(elsewhere)], wait_seconds=0)
    assert "outside this chat's folder" in sent.text


async def test_our_agent_cancels_its_task_and_the_other_side_hears(rig_for: Any) -> None:
    talk_rig = await rig_for([])
    state = await talk_rig.chat()
    talk_id = await opened(talk_rig, state, direct_door_link(talk_rig.door))
    await talk_rig.talks.tool(state.session.id, "task", text="long work", wait_seconds=0)
    task = (await talk_rig.db.fetchone("SELECT task FROM link_tasks WHERE owner = ?", (talk_id,)))["task"]
    outcome = await talk_rig.talks.tool(state.session.id, "cancel", task=task)
    assert not outcome.error and talk_rig.door.store.task(task)["state"] == "cancelled"
    assert (await talk_rig.db.fetchone("SELECT state FROM link_tasks WHERE owner = ?", (talk_id,)))["state"] == "cancelled"
