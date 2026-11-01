"""Taking a waiting message back while the agent works: one answer per message, and no trace of it after.

Each test here is a race the operator can lose with a press of the button: a withdrawal against the
round that reads the queue, against the core placing the message, against the drain that opens the
next turn, against a second device, and against a stop. The receipt in the store decides every one
of them; the engine's copy and the app's bubble follow it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from protocore.contracts.types import TextBlock

from daedalus.config import Settings
from daedalus.stores.database import Database
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_steer_queue import H, _await_run, _client, _held_tool, _manager, _start_in_tool, _watch


def _model_saw(provider: ScriptedProvider, words: str) -> bool:
    return any(words in block.text for request in provider.requests for message in request.messages for block in message.content_blocks if isinstance(block, TextBlock))


async def _rows_with(manager: Any, session_id: str, words: str) -> list[dict[str, Any]]:
    """Every transcript row that carries the words, hidden ones included."""
    return [view for view in await manager.transcript_page(session_id) if words in str(view.get("text") or "")]


async def _send(client: Any, session_id: str, text: str, client_id: str) -> dict[str, Any]:
    """What the app sends while a run is on: a follow-up with its receipt id."""
    posted = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"text": text, "follow_up": True, "expected_running": True, "client_message_id": client_id},
        headers=H,
    )
    assert posted.status_code == 200, posted.text
    return dict(posted.json()["receipt"])


async def test_a_queued_then_steered_message_withdrawn_leaves_nothing_anywhere(settings: Settings, db: Database) -> None:
    """The reported case: send while it works, press Steer, take it back before the next step."""
    provider = ScriptedProvider([_held_tool(settings), {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("taken back")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        receipt = await _send(client, sid, "use the staging database", "c-steer-1")
        assert receipt["status"] == "queued"
        assert (await client.post(f"/api/sessions/{sid}/steer/c-steer-1", headers=H)).status_code == 200
        assert [item["kind"] for item in (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()] == ["steer"]

        dropped = await client.delete(f"/api/sessions/{sid}/steer/c-steer-1", headers=H)
        assert dropped.status_code == 200
        assert changes[-1]["reason"] == "withdrawn" and changes[-1]["withdrawn"] == ["c-steer-1"]
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        assert (await manager.live.receipt(sid, "c-steer-1"))["status"] == "withdrawn"
        # The hidden row written when it was queued is gone too: nothing for the chat, the agent's
        # transcript search or the semantic index to find.
        assert await _rows_with(manager, sid, "staging database") == []
        assert await manager.sessions.search_transcript("staging", session_id=sid) == []

        await _await_run(manager)
        assert not _model_saw(provider, "staging database")
        assert await _rows_with(manager, sid, "staging database") == []
    await manager.close()


async def test_a_withdrawal_that_arrives_during_a_reload_waits_for_it_and_still_wins(settings: Settings, db: Database) -> None:
    """A reload had read the row and not yet handed it over when the withdrawal came.

    Before the queue lock the withdrawal emptied an engine list the reload then filled from its
    stale read, and the model was handed a message the operator had been told was withdrawn.
    """
    provider = ScriptedProvider([_held_tool(settings), {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("reload race")
    sid = state.session.id
    await _start_in_tool(manager, sid)
    engine = state.engine
    assert engine is not None
    await manager.submit(sid, "drop the table", steer=True, client_message_id="c-race")

    read, release = asyncio.Event(), asyncio.Event()
    original = manager.live.load_waiting

    async def slow_read(session_id: str) -> Any:
        data = await original(session_id)
        read.set()
        await release.wait()
        return data

    manager.live.load_waiting = slow_read  # type: ignore[method-assign]
    reload = asyncio.create_task(engine.reload_live_control(engine))
    await read.wait()
    manager.live.load_waiting = original  # type: ignore[method-assign]
    withdrawal = asyncio.create_task(manager.drop_queued_input(sid, "c-race"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not withdrawal.done(), "the withdrawal must wait for the reload that holds the queue"
    release.set()
    await reload
    assert await withdrawal == "withdrawn"
    assert [item["id"] for item in engine._steer_queue] == []
    await engine.persist_live_control(engine)
    assert await manager.queued_input(sid) == []
    await _await_run(manager)
    assert not _model_saw(provider, "drop the table")
    await manager.close()


async def test_a_message_the_core_is_placing_cannot_be_withdrawn(settings: Settings, db: Database) -> None:
    """The core took the steer out of its list to place it; the receipt is stamped a few awaits later.

    The withdrawal used to find the receipt still ``queued``, mark it withdrawn and answer so — and the
    stamp that followed raised on the withdrawn receipt while the model read the words anyway.
    """
    provider = ScriptedProvider([_held_tool(settings), {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("placing")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        engine = state.engine
        assert engine is not None
        await manager.submit(sid, "check the logs first", steer=True, client_message_id="c-placing")
        await engine.reload_live_control(engine)
        assert [item["id"] for item in engine._steer_queue] == ["c-placing"]
        engine._steer_queue = []  # the core's injection: out of the list and into the history

        late = await client.delete(f"/api/sessions/{sid}/steer/c-placing", headers=H)
        assert late.status_code == 409 and "already reached" in late.json()["detail"]
        assert (await manager.live.receipt(sid, "c-placing"))["status"] == "queued"
        assert not any(change["reason"] == "withdrawn" for change in changes)

        # Its queue_update arrives and the round writes back: now it is consumed, and still refused.
        state.placed.add("c-placing")
        await engine.persist_live_control(engine)
        assert await manager.queued_input(sid) == []
        assert changes[-1]["reason"] == "consumed"
        assert (await client.delete(f"/api/sessions/{sid}/steer/c-placing", headers=H)).status_code == 409
    await manager.close()


async def test_a_delivered_steer_is_refused_and_shown_once_as_delivered(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([_held_tool(settings), {"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("delivered")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        await _send(client, sid, "and the second file", "c-delivered")
        assert (await client.post(f"/api/sessions/{sid}/steer/c-delivered", headers=H)).status_code == 200
        await _await_run(manager)
        assert _model_saw(provider, "and the second file")

        late = await client.delete(f"/api/sessions/{sid}/steer/c-delivered", headers=H)
        assert late.status_code == 409 and "already reached" in late.json()["detail"]
        receipt = await manager.live.receipt(sid, "c-delivered")
        assert receipt["status"] == "consumed" and receipt["run_id"] == state.run_id
        shown = [view for view in await _rows_with(manager, sid, "and the second file") if not view.get("internal")]
        assert len(shown) == 1 and "c-delivered" in (shown[0].get("client_message_ids") or [])
    await manager.close()


async def test_withdrawing_the_middle_of_three_keeps_the_others_in_their_order(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([_held_tool(settings), {"text": "first answer"}, {"text": "second answer"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("three")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        for number, words in enumerate(("alpha note", "beta note", "gamma note")):
            await _send(client, sid, words, f"c-order-{number}")
        assert (await client.delete(f"/api/sessions/{sid}/steer/c-order-1", headers=H)).status_code == 200
        assert [item["text"] for item in (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()] == ["alpha note", "gamma note"]
        await _await_run(manager)
        for _ in range(200):
            if len(provider.requests) >= 3 and not state.running:
                break
            await asyncio.sleep(0.05)
        last = provider.requests[-1]
        opening = [block.text for message in last.messages for block in message.content_blocks if isinstance(block, TextBlock) and "alpha note" in block.text]
        assert opening and opening[-1].index("alpha note") < opening[-1].index("gamma note")
        assert not _model_saw(provider, "beta note")
        assert await _rows_with(manager, sid, "beta note") == []
        assert {r: (await manager.live.receipt(sid, r))["status"] for r in ("c-order-0", "c-order-1", "c-order-2")} == {
            "c-order-0": "consumed", "c-order-1": "withdrawn", "c-order-2": "consumed",
        }
    await manager.close()


async def test_two_devices_withdrawing_one_message_both_see_it_gone_and_it_is_announced_once(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([_held_tool(settings), {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("two devices")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        await _send(client, sid, "never mind", "c-both")
        desktop, phone = await asyncio.gather(
            client.delete(f"/api/sessions/{sid}/steer/c-both", headers=H),
            client.delete(f"/api/sessions/{sid}/steer/c-both", headers=H),
        )
        assert (desktop.status_code, phone.status_code) == (200, 200)
        assert [change.get("withdrawn") for change in changes if change["reason"] == "withdrawn"] == [["c-both"]]
        # One device steers what the other has just withdrawn: refused, nothing moves.
        assert (await client.post(f"/api/sessions/{sid}/steer/c-both", headers=H)).status_code == 409
        await _await_run(manager)
        assert not _model_saw(provider, "never mind")
    await manager.close()


async def test_a_drain_and_a_withdrawal_have_exactly_one_winner(settings: Settings, db: Database) -> None:
    """The run ended; its leftover queue opens the next turn while the operator takes one back."""
    provider = ScriptedProvider([{"text": "idle"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("drain race")
    sid = state.session.id
    for client_id in ("c-drained", "c-kept-back"):
        item = {"id": client_id, "kind": "follow_up", "text": client_id, "attachments": []}
        await manager.live.accept(sid, client_id, "input", {"text": client_id}, queue_item=item, queue_kind="follow_up")
    assert await manager.drop_queued_input(sid, "c-kept-back") == "withdrawn"
    taken = await manager.live.take(sid, outcome="consumed")
    assert [item["id"] for item in taken] == ["c-drained"]
    # Taken first, withdrawn after: the withdrawal is refused, because the turn has the message.
    assert await manager.drop_queued_input(sid, "c-drained") == "delivered"
    assert (await manager.live.receipt(sid, "c-drained"))["status"] == "consumed"
    await manager.close()


async def test_a_stop_hands_the_operators_waiting_messages_back_and_keeps_the_rest(settings: Settings, db: Database) -> None:
    """Stop means nothing more happens: queued words come back to the composer, unsent and untraced.

    The stop used to empty the engine's lists and the write-back took that for reading: the
    messages vanished from the store, the app was told the model had read them, and their bubbles
    stayed on the screen as sent.
    """
    provider = ScriptedProvider([_held_tool(settings), {"text": "never reached"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("stopped")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        engine = state.engine
        assert engine is not None
        await _send(client, sid, "first thought", "c-stop-1")
        await _send(client, sid, "second thought", "c-stop-2")
        assert (await client.post(f"/api/sessions/{sid}/steer/c-stop-2", headers=H)).status_code == 200
        await manager.submit(sid, "a job finished", steer=True, as_answer=False, origin="job")
        # The run has been handed all three, as a round would hand them.
        await engine.reload_live_control(engine)

        done = asyncio.Event()

        async def finished(session_id: str, run_id: str, status: str) -> None:
            done.set()

        manager.on_finished(finished)
        assert (await client.post(f"/api/sessions/{sid}/stop", headers=H)).status_code == 200
        gate = settings.state_dir / "release-the-tool"
        gate.touch()
        await asyncio.wait_for(done.wait(), timeout=30)

        returned = [change for change in changes if change["reason"] == "returned"]
        assert len(returned) == 1
        assert [(item["id"], item["text"]) for item in returned[0]["returned"]] == [("c-stop-1", "first thought"), ("c-stop-2", "second thought")]
        assert not any(change["reason"] == "consumed" for change in changes)
        for client_id in ("c-stop-1", "c-stop-2"):
            assert (await manager.live.receipt(sid, client_id))["status"] == "withdrawn"
        assert await _rows_with(manager, sid, "thought") == []
        # The job's notice is the agent's to read: it waits for the next run.
        assert [item["text"] for item in await manager.queued_input(sid)] == ["a job finished"]
        assert not _model_saw(provider, "thought")
    await manager.close()


async def test_editing_a_waiting_message_gives_its_whole_words_back_and_withdraws_it(settings: Settings, db: Database) -> None:
    """Changing a queued message is taking it back into the field: the card has only its first part."""
    provider = ScriptedProvider([_held_tool(settings), {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("edited")
    sid = state.session.id
    long = "rename the column " + "and keep the index " * 30
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        await _send(client, sid, long, "c-edit")
        card = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()[0]
        assert card.get("truncated") and len(card["text"]) < len(long)
        taken = await client.delete(f"/api/sessions/{sid}/steer/c-edit", headers=H)
        assert taken.status_code == 200 and taken.json()["message"]["text"] == long.strip()
        await _await_run(manager)
        assert not _model_saw(provider, "rename the column")
    await manager.close()


def _given_back(manager: Any) -> list[tuple[str, list[dict[str, Any]], str]]:
    """Every hand-back the fronts hear, as the Telegram front would."""
    heard: list[tuple[str, list[dict[str, Any]], str]] = []

    async def hook(session_id: str, messages: list[dict[str, Any]], cause: str) -> None:
        heard.append((session_id, messages, cause))

    manager.handback_hooks.append(hook)
    return heard


async def test_a_stop_gives_a_message_typed_in_telegram_back_to_the_topic_it_came_from(settings: Settings, db: Database) -> None:
    """A Telegram message has no composer in the app to come back to; it used to reach only the open
    app windows, and the operator who typed it in Telegram saw nothing at all."""
    provider = ScriptedProvider([_held_tool(settings), {"text": "never reached"}])
    manager = await _manager(settings, db, provider)
    heard = _given_back(manager)
    state = await manager.create_session("stopped from the phone")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await _start_in_tool(manager, sid)
        await manager.submit(sid, "check the second server too", via="telegram", address={"chat_id": -100, "thread_id": 42})
        await _send(client, sid, "typed in the app", "c-app")
        done = asyncio.Event()

        async def finished(session_id: str, run_id: str, status: str) -> None:
            done.set()

        manager.on_finished(finished)
        assert (await client.post(f"/api/sessions/{sid}/stop", headers=H)).status_code == 200
        (settings.state_dir / "release-the-tool").touch()
        await asyncio.wait_for(done.wait(), timeout=30)

    assert len(heard) == 1
    session_id, messages, cause = heard[0]
    assert (session_id, cause) == (sid, "stop")
    assert [(m["text"], m["via"], m["address"]) for m in messages] == [
        ("check the second server too", "telegram", {"chat_id": -100, "thread_id": 42}),
        ("typed in the app", "app", {}),
    ]
    # Handed back, never queued again.
    assert await manager.queued_input(sid) == []
    assert not _model_saw(provider, "second server")
    await manager.close()


async def _waiting(manager: Any, sid: str) -> None:
    """A message with a receipt and a job's notice waiting while the session is idle, as a drain that
    could not start the next turn leaves them."""
    item = {"id": "c-ghost", "kind": "follow_up", "text": "and rename the table", "attachments": [], "origin": "operator", "queued_at": "2026-11-02T10:00:00+00:00", "via": "telegram", "address": {"chat_id": 7, "thread_id": 0}}
    await manager.live.accept(sid, "c-ghost", "input", {"text": "and rename the table"}, queue_item=item, queue_kind="follow_up")
    await manager.live.enqueue(sid, "steer", {"id": "q-job", "kind": "steer", "text": "a job finished", "attachments": [], "origin": "job"})


async def test_a_clear_withdraws_what_was_waiting_and_hands_the_operators_words_back(settings: Settings, db: Database) -> None:
    """A clear emptied the queue columns and left the receipt ``queued`` for a message that no longer
    existed anywhere, and the operator's words went without a word to them."""
    provider = ScriptedProvider([{"text": "first answer"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    heard = _given_back(manager)
    state = await manager.create_session("cleared")
    sid = state.session.id
    await manager.submit(sid, "first request")
    await _await_run(manager)
    # The run's housekeeping drains the queue; what is queued after it stays queued.
    assert state.housekeeping is not None
    await state.housekeeping
    await _waiting(manager, sid)

    await manager.clear_history(sid)

    assert (await manager.live.receipt(sid, "c-ghost"))["status"] == "withdrawn"
    assert await manager.queued_input(sid) == []
    returned = [change for change in changes if change["reason"] == "returned"]
    assert [(change["cause"], [m["text"] for m in change["returned"]]) for change in returned] == [("clear", ["and rename the table"])]
    # The job's notice was written to the conversation that was cleared: it goes, and is nobody's to resend.
    assert [(cause, [(m["text"], m["via"], m["address"]) for m in messages]) for _, messages, cause in heard] == [("clear", [("and rename the table", "telegram", {"chat_id": 7, "thread_id": 0})])]
    await manager.close()


async def test_a_revert_withdraws_what_was_waiting_so_it_cannot_reach_the_rewound_conversation(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "first answer"}, {"text": "after the revert"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    heard = _given_back(manager)
    state = await manager.create_session("reverted")
    sid = state.session.id
    await manager.submit(sid, "first request")
    await _await_run(manager)
    # The run's housekeeping drains the queue; what is queued after it stays queued.
    assert state.housekeeping is not None
    await state.housekeeping
    await _waiting(manager, sid)
    seq = next(int(m.metadata["daedalus.seq"]) for m in await manager.sessions.list_transcript(sid) if m.text.startswith("first request"))

    await manager.revert(sid, seq)

    assert (await manager.live.receipt(sid, "c-ghost"))["status"] == "withdrawn"
    assert await manager.queued_input(sid) == []
    assert [change.get("cause") for change in changes if change["reason"] == "returned"] == ["revert"]
    assert [cause for _, _, cause in heard] == ["revert"]
    # The next turn starts clean: nothing from before the revert is drained into it.
    await manager.submit(sid, "a new start")
    await _await_run(manager)
    assert not _model_saw(provider, "rename the table") and not _model_saw(provider, "a job finished")
    assert await manager.drop_queued_input(sid, "c-ghost") == "withdrawn"
    await manager.close()
