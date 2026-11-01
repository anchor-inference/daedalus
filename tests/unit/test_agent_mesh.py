"""The agents of a project talking to each other: ListAgents, ReadAgent and SendToAgent.

Written against the team with fake runtimes: what is under test is who may be reached, what is
refused and why, the hop cap, the wait for a reply, and that every message is logged and reaches the
orchestrator's wake-up.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import Settings
from daedalus.extensions.agent_mesh import MAX_HOPS, Speaker
from daedalus.extensions.api import build_app
from daedalus.extensions.orchestrator import Orchestrators
from daedalus.extensions.staff import Team
from daedalus.host.events import AppEvent
from daedalus.host.session_runner import SessionManager
from daedalus.staff_runtime import FakeStaffRuntime, LiveSession, ReadPage
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_assignment
from tests.unit.test_staff_runtime import Capacity, board_task, close_team, events, fake_team, repository


async def working(team: Team, manager: SessionManager, project: Project, name: str) -> tuple[Staff, LiveSession]:
    member = await manager.staff.hire(project.id, name=name, isolation="shared")
    await operator_assignment(team, member, await board_task(manager, project, f"{name}'s task"))
    live = await team.live_of(member)
    assert live is not None
    return member, live


async def speaker(team: Team, live: LiveSession) -> Speaker:
    fresh = await team.live_of(live.staff)
    assert fresh is not None
    return await team.mesh.speaker_of(fresh)


async def send(team: Team, live: LiveSession, to: str, text: str, **fields: Any) -> tuple[str, bool]:
    return await team.mesh.call(await speaker(team, live), "send_to_agent", {"agent": to, "text": text, **fields})


async def logged(manager: SessionManager, project: Project) -> list[dict[str, Any]]:
    return [dict(r) for r in await manager.db.fetchall("SELECT * FROM agent_messages WHERE project_id = ? ORDER BY rowid", (project.id,))]


async def test_a_message_reaches_a_member_of_the_project_under_its_header_and_is_logged(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada, ada_live = await working(team, manager, project, "Ada")
        bob, bob_live = await working(team, manager, project, "Bob")
        before = len(runtime.sent)
        text, failed = await send(team, ada_live, "bob", "Which port does your server listen on?")
        assert not failed and "Sent to Bob as am-" in text and f"hop 1 of {MAX_HOPS}" in text
        [(to, message)] = runtime.sent[before:]
        assert to == bob_live.id and message.origin == "agent" and message.mode == "now"
        assert message.text.startswith("[message am-") and "from Ada, another agent of your project, hop 1 of 5" in message.text
        assert "not your orchestrator's or the operator's" in message.text and message.text.endswith("Which port does your server listen on?")
        stored = await manager.staff.message(message.id)
        assert stored is not None and stored.origin == "agent" and stored.staff_id == bob.id
        [row] = await logged(manager, project)
        assert (row["sender_name"], row["sender_staff_id"], row["target_name"], row["target_staff_id"]) == ("Ada", ada.id, "Bob", bob.id)
        assert (row["hop"], row["state"], row["kind"], row["staff_message_id"]) == (1, "sent", "message", message.id)
        assert row["text"] == "Which port does your server listen on?" and row["created_at"]
        [event] = await events(manager, "agent.message")
        assert event.payload["from"] == "Ada" and event.payload["to"] == "Bob" and event.payload["state"] == "sent" and event.project_id == project.id
    finally:
        await close_team(manager)
        await manager.close()


async def test_reach_is_the_project(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        other = await manager.projects.create("Brewery", [FolderSpec(str(repository(tmp_path / "other")))])
        await manager.projects.update_orchestrator(other.id, enabled=True, autonomy="normal", concurrency=6)
        zed = await manager.staff.hire(other.id, name="Zed", isolation="shared")
        await operator_assignment(team, zed, await board_task(manager, other, "Zed's task"))
        before = len(runtime.sent)
        text, failed = await send(team, ada_live, "Zed", "hello")
        assert failed and "no agent called 'Zed'" in text
        text, failed = await send(team, ada_live, zed.id, "hello")
        assert failed and "Zed is not a member of Bakery" in text
        now = datetime.now(UTC).isoformat()
        await db.execute("INSERT INTO terminals(id, env, project_id, owner_kind, owner_id, title, cwd, created_at) VALUES ('t-far', 'container', ?, 'project', ?, 'far shell', '/', ?)",
                         (other.id, other.id, now))
        text, failed = await send(team, ada_live, "t-far", "hello")
        assert failed and "that terminal is not in your project" in text
        assert len(runtime.sent) == before, "nothing went anywhere"
        listing, _ = await team.mesh.call(await speaker(team, ada_live), "list_agents", {})
        assert "Ada" in listing and "Zed" not in listing
        rows = await logged(manager, project)
        assert [r["state"] for r in rows] == ["refused"] * 3 and all(r["target_staff_id"] is None for r in rows)
        assert not await logged(manager, other)
    finally:
        await close_team(manager)
        await manager.close()


async def test_the_sixth_hop_in_a_row_is_refused_until_the_orchestrator_speaks(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada, ada_live = await working(team, manager, project, "Ada")
        bob, bob_live = await working(team, manager, project, "Bob")
        pair = [(ada_live, "Bob"), (bob_live, "Ada")]
        for hop in range(1, MAX_HOPS + 1):
            sender, to = pair[(hop - 1) % 2]
            text, failed = await send(team, sender, to, f"message {hop}")
            assert not failed and f"hop {hop} of {MAX_HOPS}" in text, text
        sent = len(runtime.sent)
        text, failed = await send(team, bob_live, "Ada", "message 6")
        assert failed and "6th agent-to-agent message in a row" in text and "Report to your orchestrator" in text
        assert len(runtime.sent) == sent
        rows = await logged(manager, project)
        assert [r["hop"] for r in rows] == [1, 2, 3, 4, 5, 6] and rows[-1]["state"] == "refused"
        assert rows[1]["parent_id"] == rows[0]["id"], "each hop names the message it follows"
        # The orchestrator speaks to Bob: what Bob says next is about that, a first hop again.
        await team.tell(bob, "Carry on with the menu.", by="orchestrator")
        text, failed = await send(team, bob_live, "Ada", "a fresh question")
        assert not failed and f"hop 1 of {MAX_HOPS}" in text
        # Ada answers that, and so continues the new chain rather than the one that was stopped.
        text, failed = await send(team, ada_live, "Bob", "here is the answer")
        assert not failed and f"hop 2 of {MAX_HOPS}" in text
        del ada
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_terminal_with_no_agent_in_it_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        now = datetime.now(UTC).isoformat()
        await db.execute("INSERT INTO terminals(id, env, project_id, owner_kind, owner_id, title, cwd, created_at) VALUES ('t-shell', 'container', ?, 'project', ?, 'build shell', '/', ?)",
                         (project.id, project.id, now))
        text, failed = await send(team, ada_live, "t-shell", "ls")
        assert failed and "the terminal build shell has no agent running in it" in text and "run as commands" in text
        # A command-line member whose CLI has gone from its terminal: the same refusal, by its name.
        cli = FakeStaffRuntime(kind="claude")
        cli.continuity = lambda live: "gone"  # type: ignore[attr-defined]
        # Each fake runtime numbers its terminals from 1, so Ada's is term-1 as well: Cleo's terminal
        # needs a name of its own, as every real terminal has, or the lookup by id finds either.
        cli._ids = itertools.count(100)
        team.runtimes["claude"] = cli
        team._capacity = Capacity()
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="readonly")
        await operator_assignment(team, cleo, await board_task(manager, project, "Cleo's task"))
        cleo_live = await team.live_of(cleo)
        assert cleo_live is not None and cleo_live.session.kind == "cli"
        text, failed = await send(team, ada_live, "Cleo", "are you there?")
        assert failed and "Cleo's terminal has no agent running in it now" in text
        assert not cli.sent
        listing, _ = await team.mesh.call(await speaker(team, ada_live), "list_agents", {})
        assert "no agent running in its terminal (messages refused)" in listing
        # Its terminal, named by id, is the member: the same refusal.
        await db.execute("INSERT INTO terminals(id, env, project_id, owner_kind, owner_id, title, cwd, created_at) VALUES (?, 'container', ?, 'staff', ?, 'claude', '/', ?)",
                         (cleo_live.terminal_id, project.id, cleo_live.id, now))
        text, failed = await send(team, ada_live, str(cleo_live.terminal_id), "are you there?")
        assert failed and "Cleo's terminal has no agent running" in text
        assert [r["state"] for r in await logged(manager, project)] == ["refused"] * 3
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_member_waiting_on_a_permission_or_a_question_is_not_typed_into(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        _, bob_live = await working(team, manager, project, "Bob")
        await team.ingress.status(bob_live, "permission", "Bash: rm -rf build")
        sent = len(runtime.sent)
        text, failed = await send(team, ada_live, "Bob", "y")
        assert failed and "Bob is waiting on a permission: Bash: rm -rf build" in text and "could answer it" in text
        await team.ingress.status(bob_live, "question", "Which database?")
        text, failed = await send(team, ada_live, "Bob", "postgres")
        assert failed and "waiting on a question" in text
        assert len(runtime.sent) == sent
        listing, _ = await team.mesh.call(await speaker(team, ada_live), "list_agents", {})
        assert "waiting on a question: Which database? (messages refused until it is settled)" in listing
        rows = await logged(manager, project)
        assert [r["state"] for r in rows] == ["refused", "refused"] and "permission" in rows[0]["detail"]
        await team.ingress.status(bob_live, "working")
        text, failed = await send(team, ada_live, "Bob", "and now?")
        assert not failed
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_wait_for_a_reply_ends_at_its_timeout(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        await working(team, manager, project, "Bob")
        text, failed = await send(team, ada_live, "Bob", "Which port?", wait_for_reply=True, timeout_s=1)
        assert not failed and "no reply came within 1 s" in text and "ReadAgent Bob later" in text
        [row] = await logged(manager, project)
        assert row["state"] == "unanswered" and row["wait_s"] == 1
        assert not team.mesh.waiters, "the wait is let go"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_reply_goes_to_the_waiting_call_and_is_never_typed_into_the_sender(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        _, bob_live = await working(team, manager, project, "Bob")
        waiting = asyncio.create_task(send(team, ada_live, "Bob", "Which port?", wait_for_reply=True, timeout_s=30))
        while not team.mesh.waiters:
            await asyncio.sleep(0.01)
        typed = len(runtime.sent)
        text, failed = await send(team, bob_live, "Ada", "8080")
        assert not failed and "Handed to Ada, who was waiting for your reply" in text
        answer, failed = await asyncio.wait_for(waiting, 5)
        assert not failed and answer.startswith("Bob replied to am-") and answer.endswith("8080")
        assert len(runtime.sent) == typed, "the reply went to the call, not into Ada's session"
        message, reply = await logged(manager, project)
        assert (message["state"], reply["kind"], reply["parent_id"], reply["hop"]) == ("answered", "reply", message["id"], 2)
        assert (reply["sender_name"], reply["target_name"]) == ("Bob", "Ada")
    finally:
        await close_team(manager)
        await manager.close()


async def test_without_a_reply_the_end_of_the_receivers_turn_answers(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        _, bob_live = await working(team, manager, project, "Bob")
        runtime.page = ReadPage("The server listens on 8080.", None, False)
        waiting = asyncio.create_task(send(team, ada_live, "Bob", "Which port?", wait_for_reply=True, timeout_s=30))
        while not team.mesh.waiters:
            await asyncio.sleep(0.01)
        [staff_message_id] = list(team.mesh.waiters)
        # A turn that ends before Bob took the message is not its answer.
        await team.ingress.status(bob_live, "turn_done_unseen")
        await asyncio.sleep(0.2)
        assert not waiting.done()
        await team.ingress.status(bob_live, "working")
        await team.ingress.message_state(staff_message_id, "acknowledged")
        await team.ingress.status(bob_live, "turn_done_unseen")
        answer, failed = await asyncio.wait_for(waiting, 5)
        assert not failed and "the end of its turn" in answer and answer.endswith("The server listens on 8080.")
        message, reply = await logged(manager, project)
        assert message["state"] == "answered" and reply["detail"] == "the end of its turn" and reply["parent_id"] == message["id"]
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_replayed_call_sends_nothing_twice(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        await working(team, manager, project, "Bob")
        call_id = f"launch:{uuid.uuid4().hex}:1"
        first, _ = await send(team, ada_live, "Bob", "hello", call_id=call_id)
        sent = len(runtime.sent)
        again, failed = await send(team, ada_live, "Bob", "hello", call_id=call_id)
        assert not failed and "it is not sent twice" in again and len(runtime.sent) == sent
        assert len(await logged(manager, project)) == 1
        # A model that numbers its calls the same in every conversation: Bob's first call has Ada's id,
        # and is a message of its own, not a replay of hers.
        bob_live = await team.live_of(await manager.staff.by_name(project.id, "Bob"))  # type: ignore[arg-type]
        assert bob_live is not None
        text, failed = await send(team, bob_live, "Ada", "hello back", call_id=call_id)
        assert not failed and "Sent to Ada" in text and len(runtime.sent) == sent + 1
        assert len(await logged(manager, project)) == 2
        del first
    finally:
        await close_team(manager)
        await manager.close()


async def test_read_agent_and_the_orchestrators_reach(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        _, ada_live = await working(team, manager, project, "Ada")
        await working(team, manager, project, "Bob")
        runtime.page = ReadPage("\n".join(f"line {n}" for n in range(1, 101)), None, False)
        text, failed = await team.mesh.call(await speaker(team, ada_live), "read_agent", {"agent": "Bob", "lines": 3})
        assert not failed and text.splitlines()[1:] == ["line 98", "line 99", "line 100"] and "its last reply" in text
        # A Daedalus member has no screen: its last reply stands for it.
        text, _ = await team.mesh.call(await speaker(team, ada_live), "read_agent", {"agent": "Bob", "what": "screen"})
        assert runtime.reads[-1][1].what == "last"
        # Only staff and the project's orchestrator reach its agents.
        chat = await manager.create_session("a chat")
        text, failed = await team.mesh.service("list_agents", session_id=chat.session.id)
        assert failed and "only the staff of a project and its orchestrator" in text
        orchestrator = (await manager.create_session("orchestrator", metadata={"orchestrator_of": project.id}, project_id=project.id)).session.id
        assert await manager.projects.set_orchestrator(project.id, expect="", value=orchestrator)
        listing, failed = await team.mesh.service("list_agents", session_id=orchestrator)
        assert not failed and "(you are the orchestrator)" in listing and "Ada" in listing and "Bob" in listing
        before = len(runtime.sent)
        text, failed = await team.mesh.service("send_to_agent", session_id=orchestrator, agent="Bob", text="Pair with Ada on the port.")
        assert not failed and runtime.sent[before][1].text.startswith("[message am-") and "from your orchestrator]" in runtime.sent[before][1].text
        # A member does not message its orchestrator this way.
        text, failed = await send(team, ada_live, "orchestrator", "done")
        assert failed and "Report and AskOrchestrator" in text
    finally:
        await close_team(manager)
        await manager.close()


async def test_the_orchestrator_hears_every_message_and_the_app_reads_the_log(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        ada, ada_live = await working(team, manager, project, "Ada")
        bob, bob_live = await working(team, manager, project, "Bob")
        await send(team, ada_live, "Bob", "Which port?")
        await team.ingress.status(bob_live, "permission", "Bash: make")
        await send(team, ada_live, "Bob", "Still there?")
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"staff": team}, guard=None)
        orchestrators = Orchestrators(app)  # type: ignore[arg-type]
        orchestrator = (await manager.create_session("orchestrator", metadata={"orchestrator_of": project.id}, project_id=project.id)).session.id
        assert await manager.projects.set_orchestrator(project.id, expect="", value=orchestrator)
        found = await events(manager, "agent.message")
        assert [e.payload["state"] for e in found] == ["sent", "refused"]
        lines = []
        for event in found:
            wake = await orchestrators.classify(project.id, event)
            assert wake is not None and not wake.urgent
            lines.append(await orchestrators.line(await manager.projects.get(project.id), event))
        assert lines[0].startswith("Ada → Bob, agent message am-") and '"Which port?"' in lines[0]
        assert lines[1].startswith("Ada's message to Bob was refused (Bob is waiting on a permission")
        # Its own messages are not news to it.
        own = AppEvent(seq=10**6, type="agent.message", at=datetime.now(UTC).isoformat(), payload=found[0].payload, session_id=orchestrator, project_id=project.id)
        assert await orchestrators.classify(project.id, own) is None

        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            listing = await client.get(f"/api/projects/{project.id}/agent-messages", headers={"X-Daedalus-Token": "tok"})
            assert listing.status_code == 200
            rows = listing.json()["messages"]
            assert [r["state"] for r in rows] == ["refused", "sent"], "newest first"
            assert rows[1]["sender"]["name"] == "Ada" and rows[1]["target"]["staff_id"] == bob.id and rows[1]["delivery"] == "submitted"
            mine = await client.get(f"/api/projects/{project.id}/agent-messages?staff={ada.id}&limit=1", headers={"X-Daedalus-Token": "tok"})
            assert len(mine.json()["messages"]) == 1 and mine.json()["more"] is True
            older = await client.get(f"/api/projects/{project.id}/agent-messages?before={rows[0]['id']}", headers={"X-Daedalus-Token": "tok"})
            assert [r["id"] for r in older.json()["messages"]] == [rows[1]["id"]]
            assert (await client.get(f"/api/projects/{project.id}/agent-messages")).status_code == 401
            assert (await client.get("/api/projects/nope/agent-messages", headers={"X-Daedalus-Token": "tok"})).status_code == 404
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_command_line_member_reaches_the_mesh_by_its_team_token(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        await working(team, manager, project, "Ada")
        cli = FakeStaffRuntime(kind="claude")
        team.runtimes["claude"] = cli
        team._capacity = Capacity()
        cleo = await manager.staff.hire(project.id, name="Cleo", harness="claude", isolation="readonly")
        await operator_assignment(team, cleo, await board_task(manager, project, "Cleo's task"))
        [req] = cli.started
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"staff": team}, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            url = f"/api/team/{req.staff_session_id}"
            assert (await client.post(f"{url}/list_agents", headers={"X-Daedalus-Team-Token": "nope"}, json={})).status_code == 401
            listing = await client.post(f"{url}/list_agents", headers={"X-Daedalus-Team-Token": req.team_token}, json={})
            assert listing.status_code == 200 and "Ada" in listing.json()["text"] and "Cleo" in listing.json()["text"]
            sent = await client.post(f"{url}/send_to_agent", headers={"X-Daedalus-Team-Token": req.team_token}, json={"agent": "Ada", "text": "hi"})
            assert sent.json()["error"] is False and "Sent to Ada" in sent.json()["text"]
            # The team's own routes are still its own.
            report = await client.post(f"{url}/report", headers={"X-Daedalus-Team-Token": req.team_token},
                                       json={"kind": "checkpoint", "note": "half", "client_operation_id": "c1"})
            assert report.status_code == 200
        [row] = await logged(manager, project)
        assert (row["sender_name"], row["target_name"]) == ("Cleo", "Ada")
    finally:
        await close_team(manager)
        await manager.close()
