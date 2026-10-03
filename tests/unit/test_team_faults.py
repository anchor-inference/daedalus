"""A staff member's question survives every fault between it and its answer, and ends exactly once.

A command-line member asks its orchestrator through the team tools; the question is held open on
the daemon's hook listener and recorded as an ask. Here each thing that can break between the two
breaks while the question waits:

- the terminal daemon dies (and a fresh one takes its place);
- the host restarts;
- the CLI exits in the middle of the call;
- the call is held longer than the CLI waits for it;
- the post is replayed after it was answered;
- the member is resumed, and the old launch's token is used again.

Whatever happens, the ask ends once — answered, or withdrawn with the reason in its resolution and
on the member's status — and never twice, and never silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

import pytest

from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from daedalus.stores.staff import Ask
from tests.support.authorized_launch import operator_assignment
from tests.support.fake_cli.tui import read_log
from tests.support.live_ptyd import LivePtyd
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand, terminals_service, trust
from tests.unit.test_harness_claude import post, started

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

QUESTION = "Which oven should the bread go in?"


def claude(**config: Any) -> dict[str, Any]:
    return {"adapter": ClaudeCodeAdapter(), **config}


async def asks_of(s: Stand, member_id: str) -> list[Ask]:
    rows = await s.manager.db.fetchall("SELECT id FROM asks WHERE staff_id = ? ORDER BY created_at", (member_id,))
    found = [await s.manager.asks.get(r["id"]) for r in rows]
    return [a for a in found if a is not None]


async def the_open_question(s: Stand, member_id: str) -> Ask:
    async def opened() -> bool:
        return any(a.open and a.kind == "question" for a in await asks_of(s, member_id))

    await eventually(opened, "the question was asked")
    [ask] = [a for a in await asks_of(s, member_id) if a.open]
    return ask


async def ended_once(s: Stand, member_id: str, *, timeout: float = 30.0) -> Ask:
    """The member's one ask, once it has ended; fails if a second one was ever opened."""

    async def closed() -> bool:
        return all(not a.open for a in await asks_of(s, member_id))

    await eventually(closed, "the question ended", timeout=timeout)
    asks = await asks_of(s, member_id)
    assert len(asks) == 1, [(a.id, a.request_ref, a.resolution) for a in asks]
    return asks[0]


def answers_seen(s: Stand) -> list[str]:
    """What the fake model was told by its AskOrchestrator calls, in order, from its transcripts."""
    seen: list[str] = []
    for path in sorted((s.home / ".claude" / "projects").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("type") != "assistant":
                continue
            content = (record.get("message") or {}).get("content") or []
            text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if text.startswith("AskOrchestrator:"):
                seen.append(text)
    return seen


def cli_pids(s: Stand) -> set[int]:
    return {int(e["pid"]) for e in read_log(s.log) if e.get("cli") == "claude"}


async def test_a_question_held_when_the_daemon_dies_is_withdrawn_with_the_reason(settings: Any, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, f"askorch:{QUESTION};echo:done")
        await the_open_question(s, ada.id)
        # The daemon goes, and every process it held goes with it; a fresh one starts empty.
        await s.ptyd.stop()
        s.ptyd = LivePtyd(s.root / "run", home=s.home, bin_dir=s.root / "bin", base_env={"FAKE_CLI_LOG": str(s.log), "FAKE_CLI_TIME_SCALE": "0.05"})
        await s.ptyd.start()
        ask = await ended_once(s, ada.id, timeout=60)
        assert ask.resolved_by == "system"
        assert "session ended" in str(ask.resolution.get("closed")), ask.resolution
        exited = await s.status_event(ada, "exited", timeout=30)
        assert exited.payload.get("detail"), exited.payload
        # Answering it now is refused, not delivered to nobody.
        with pytest.raises(Exception):  # noqa: B017 - whichever refusal the team gives, it must give one
            await s.team.answer(ask.short_id, text="the left one", by="orchestrator")
        assert len(await asks_of(s, ada.id)) == 1


async def test_a_question_held_across_a_host_restart_is_answered_once_on_its_held_call(settings: Any, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, f"askorch:{QUESTION};echo:done")
        before = await the_open_question(s, ada.id)
        # The host goes; the daemon and the CLI, still waiting on its call, stay.
        s.runtime.close()
        await s.terminals.close()
        s.terminals = terminals_service(db, s.manager, s.root / "run", s.home)
        s.team.app.extensions["terminals"] = s.terminals
        await s.terminals.start()
        assert await s.terminals.wait_available("container")
        runtime = s.restart_runtime(ClaudeCodeAdapter())
        assert await runtime.reconcile(wait=5) == 1
        await asyncio.sleep(0.5)  # anything the daemon replays to the new host has arrived
        still = [a for a in await asks_of(s, ada.id) if a.open]
        assert [a.id for a in still] == [before.id]
        await s.team.answer(before.short_id, text="the left one", by="orchestrator")
        ask = await ended_once(s, ada.id)
        assert ask.resolved_by == "orchestrator"
        await s.status_event(ada, "turn_done_unseen")
        assert answers_seen(s) == ["AskOrchestrator: the left one"]


async def test_a_cli_that_exits_mid_call_has_its_question_withdrawn_once(settings: Any, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, f"askorch:{QUESTION};echo:done")
        await the_open_question(s, ada.id)
        for pid in cli_pids(s):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        ask = await ended_once(s, ada.id)
        assert ask.resolved_by == "system" and "session ended" in str(ask.resolution.get("closed")), ask.resolution
        exited = await s.status_event(ada, "exited")
        assert exited.payload.get("detail")
        with pytest.raises(Exception):  # noqa: B017
            await s.team.answer(ask.short_id, text="the left one", by="orchestrator")


async def test_a_call_held_past_its_wait_is_answered_once_as_a_message(settings: Any, db: Database) -> None:
    # The hold ends before anyone answers: the CLI hears "pending", finishes its turn, and the answer
    # that comes later goes to it as a message.
    async with stand(settings, db, ask_hold_s=1, **claude()) as s:
        trust(s)
        ada = await started(s, f"askorch:{QUESTION};echo:done")
        ask = await the_open_question(s, ada.id)

        async def turn_over() -> bool:
            return any(e["event"] == "hook" and e.get("name") == "Stop" for e in read_log(s.log))

        # The member stays waiting on its question, but its turn is over: it heard "pending".
        await eventually(turn_over, "the turn ended after the hold")
        assert answers_seen(s) and answers_seen(s)[0].startswith("AskOrchestrator: Pending")
        assert ask.open
        await s.team.answer(ask.short_id, text="the left one", by="orchestrator")
        assert (await ended_once(s, ada.id)).resolved_by == "orchestrator"

        async def delivered() -> bool:
            return any("the left one" in e["text"] for e in read_log(s.log) if e["event"] == "submitted")

        await asyncio.sleep(3)
        sess = s.runtime.sessions[(await s.session_row(ada)).id]
        print("STATUSES", [(e.payload["status"], e.payload.get("waiting_for")) for e in await s.events("staff.status", staff_id=ada.id)])
        print("SESSION", sess.open, sess.worker.waiting, sess.worker.inflight, sess.turn_ended_waiting)
        print("SCREEN", await sess.term.screen())
        await eventually(delivered, "the answer was delivered as a message")
        told = [e["text"] for e in read_log(s.log) if e["event"] == "submitted" and "the left one" in e["text"]]
        assert len(told) == 1 and told[0].startswith("[the orchestrator answers your question]")


async def test_a_caller_that_gave_up_gets_the_answer_as_a_message(settings: Any, db: Database) -> None:
    # The CLI's own timer on the call fired first: it cancelled the call, the team bridge closed its
    # post, and the daemon holds nothing any more (both proven in the daemon's own tests). The answer
    # must not be taken as delivered into the closed post: it goes to the CLI as a message, once the
    # silence check has seen the CLI sit at an empty prompt.
    async with stand(settings, db, no_signal_after_s=0.5, reconcile_gap_ms=200, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert launch is not None
        body = {"tool": "ask", "question": QUESTION, "options": [], "call_id": f"{launch.launch_id}:beef:1"}
        gave_up = asyncio.create_task(asyncio.to_thread(post_giving_up, s, launch.launch_id, body))
        ask = await the_open_question(s, ada.id)
        await gave_up
        await asyncio.sleep(0.3)
        await s.team.answer(ask.short_id, text="the left one", by="orchestrator")
        assert (await ended_once(s, ada.id)).resolved_by == "orchestrator"
        assert not [r for r in s.ptyd.replies if "the left one" in json.dumps(r.get("body"))]

        async def delivered() -> bool:
            return any("the left one" in e["text"] for e in read_log(s.log) if e["event"] == "submitted")

        await eventually(delivered, "the answer was delivered as a message")


def post_giving_up(s: Stand, launch_id: str, body: dict[str, Any]) -> None:
    """A team post held for a minute whose caller stops waiting after two seconds."""
    import urllib.request

    launch = s.ptyd.launches[launch_id]
    request = urllib.request.Request(
        f"http://127.0.0.1:{s.ptyd.hook_port}/hook/{launch_id}/team?wait_ms=60000",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {launch.token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2):
            raise AssertionError("the post was answered before anyone answered it")
    except TimeoutError:
        pass


async def test_a_replayed_post_after_its_answer_opens_nothing_new(settings: Any, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert launch is not None
        body = {"tool": "ask", "question": QUESTION, "options": ["left", "right"], "call_id": f"{launch.launch_id}:cafe:7"}
        first = asyncio.create_task(asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=20_000))
        ask = await the_open_question(s, ada.id)
        await s.team.answer(ask.short_id, text="left", by="orchestrator")
        assert await first == (200, {"text": "left"})
        # The same post again — a CLI retrying, or a hook replayed — is the answered question, not a new one.
        again = await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=3_000)
        assert again[0] in (200, 204)
        await asyncio.sleep(0.3)
        assert len(await asks_of(s, ada.id)) == 1
        # And after a host restart, when the runtime has forgotten the calls it saw.
        s.runtime.close()
        runtime = s.restart_runtime(ClaudeCodeAdapter())
        assert await runtime.reconcile(wait=5) == 1
        await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=3_000)
        await asyncio.sleep(0.3)
        asks = await asks_of(s, ada.id)
        assert len(asks) == 1 and not asks[0].open, [(a.request_ref, a.open) for a in asks]


async def test_the_old_launchs_token_is_gone_once_the_member_runs_again(settings: Any, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        old = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert old is not None
        old_token = s.ptyd.launches[old.launch_id].token
        assert await s.team.release(ada)
        # Taken up again (a member's next session is always a launch of its own).
        task_id = await s.task("Next page;echo:back again;")
        assert (await operator_assignment(s.team, ada, task_id))["state"] == "queued"
        await s.status_event(ada, "turn_done_unseen")

        async def relaunched() -> bool:
            launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
            return launch is not None and launch.launch_id != old.launch_id

        await eventually(relaunched, "the member runs in a new launch")
        new = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert new is not None and new.launch_id != old.launch_id
        assert s.ptyd.launches[new.launch_id].token != old_token
        body = {"tool": "ask", "question": QUESTION, "options": [], "call_id": f"{old.launch_id}:dead:1"}
        status = await asyncio.to_thread(post_with_token, s, old.launch_id, old_token, body)
        assert status == 410
        assert await asks_of(s, ada.id) == []


def post_with_token(s: Stand, launch_id: str, token: str, body: dict[str, Any]) -> int:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{s.ptyd.hook_port}/hook/{launch_id}/team?wait_ms=2000",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
