"""The steer queue as the composer sees it: what is waiting, taking one back, and when it is too late."""

from __future__ import annotations

import asyncio
import hashlib
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import TextBlock

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import STEER_CARD_CHARS, STEER_CARD_LIMIT, SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_session_runner import ScriptedProvider

H = {"X-Daedalus-Token": "tok"}


async def test_successive_runs_keep_distinct_transcript_identity(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "first answer"}, {"text": "second answer"}]))
    state = await manager.create_session("two runs")
    first = await manager.submit(state.session.id, "first question")
    await state.task
    second = await manager.submit(state.session.id, "second question")
    await state.task
    views = [m for m in await manager.transcript_page(state.session.id) if not m.get("internal")]
    assert first != second
    assert [(m["text"], m["run_id"]) for m in views] == [
        ("first question", first), ("first answer", first),
        ("second question", second), ("second answer", second),
    ]
    await manager.close()


async def test_steers_are_received_between_their_tool_batches(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "sleep 0.2"}},
        {"tool": "Exec", "args": {"command": "sleep 0.3"}},
        {"text": "done"},
    ])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("steering order")
    batches = 0

    async def steer_after_start(session_id: str, event: Any) -> None:
        nonlocal batches
        if event.type.value == "tool_use_start":
            batches += 1
            await manager.submit(session_id, f"instruction {batches}", steer=True)

    manager.add_sink(steer_after_start)
    await manager.submit(state.session.id, "start")
    await state.task
    views = [m for m in await manager.transcript_page(state.session.id) if not m.get("internal")]
    order = ["tool" if m["tool_calls"] else m["text"] for m in views if m["role"] != "tool"]
    assert order == ["start", "tool", "instruction 1", "tool", "instruction 2", "done"]
    await manager.close()


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    return manager


def _client(settings: Settings, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test")  # type: ignore[arg-type]


def _watch(manager: SessionManager) -> list[dict[str, Any]]:
    """Every ``steer_changed`` the manager raises, in order."""
    seen: list[dict[str, Any]] = []

    async def sink(session_id: str, event: Any) -> None:
        if getattr(event.type, "value", "") == "steer_changed":
            seen.append(event.payload)

    manager.add_sink(sink)
    return seen


async def _await_run(manager: SessionManager) -> None:
    done = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        done.set()

    manager.on_finished(on_finished)
    await asyncio.wait_for(done.wait(), timeout=30)


async def test_a_steer_is_listed_while_it_waits_and_can_be_taken_back(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steered")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running

        posted = await client.post(f"/api/sessions/{sid}/messages", json={"text": "also look at the log", "steer": True}, headers=H)
        assert posted.status_code == 200
        queued = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()
        assert len(queued) == 1 and queued[0]["text"] == "also look at the log"
        assert queued[0]["id"] and queued[0]["queued_at"]
        assert changes[-1]["reason"] == "queued" and changes[-1]["count"] == 1
        assert changes[-1]["queued"][0]["id"] == queued[0]["id"]

        dropped = await client.delete(f"/api/sessions/{sid}/steer/{queued[0]['id']}", headers=H)
        assert dropped.status_code == 200 and dropped.json() == {"deleted": True}
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        assert changes[-1]["reason"] == "withdrawn" and changes[-1]["count"] == 0

        # Gone before the run read it: the model is never shown the withdrawn text.
        await _await_run(manager)
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert not any("also look at the log" in t for t in texts)
    await manager.close()


async def test_a_retried_input_returns_one_receipt_and_one_queue_item(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("deduplicated")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        body = {"text": "read this once", "steer": True, "client_message_id": "70a4b61b-cce4-4515-9100-f53340f9b01c"}
        first = await client.post(f"/api/sessions/{sid}/messages", json=body, headers=H)
        repeated = await client.post(f"/api/sessions/{sid}/messages", json=body, headers=H)
        assert first.status_code == repeated.status_code == 200
        assert first.json()["receipt"] == repeated.json()["receipt"]
        assert first.json()["receipt"]["status"] == "queued"
        assert [item["id"] for item in await manager.queued_input(sid)] == [body["client_message_id"]]
        conflict = await client.post(
            f"/api/sessions/{sid}/messages",
            json={**body, "text": "different"},
            headers=H,
        )
        assert conflict.status_code == 409
    await manager.close()


async def test_a_retry_after_placement_reuses_the_transcript_row(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "done"}]))
    state = await manager.create_session("retry after placement")
    sid = state.session.id
    start = manager._start_run
    attempts = 0

    async def interrupted(current: Any, message: Any, *, continue_turn: bool = False) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("start interrupted after transcript placement")
        return await start(current, message, continue_turn=continue_turn)

    manager._start_run = interrupted  # type: ignore[method-assign]
    message_id = "f6459d4f-7760-4d23-b56f-bd11496dfd5e"
    with pytest.raises(RuntimeError, match="start interrupted"):
        await manager.submit(sid, "run this once", client_message_id=message_id)
    run_id = await manager.submit(sid, "run this once", client_message_id=message_id)
    await state.task
    visible = [item for item in await manager.transcript_page(sid) if not item.get("internal")]
    assert [item["text"] for item in visible] == ["run this once", "done"]
    receipt = await manager.live.receipt(sid, message_id)
    assert receipt is not None and receipt["status"] == "consumed" and receipt["run_id"] == run_id
    await manager.close()


async def test_a_retried_upload_keeps_one_file_and_one_message(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "done"}]))
    state = await manager.create_session("upload once")
    sid = state.session.id
    message_id = "35dfddbc-da81-46f0-abf7-56163ba2088d"
    form = {"text": "inspect this", "client_message_id": message_id}
    upload = {"files": ("notes.txt", b"same bytes", "text/plain")}
    async with _client(settings, db, manager) as client:
        first = await client.post(f"/api/sessions/{sid}/upload", data=form, files=upload, headers=H)
        repeated = await client.post(f"/api/sessions/{sid}/upload", data=form, files=upload, headers=H)
        assert first.status_code == repeated.status_code == 200
        assert first.json()["run_id"] == repeated.json()["run_id"]
        assert first.json()["receipt"] == repeated.json()["receipt"]
        assert [path.name for path in (state.workspace / "inbox").iterdir()] == ["notes.txt"]

        conflict = await client.post(
            f"/api/sessions/{sid}/upload",
            data=form,
            files={"files": ("notes.txt", b"different bytes", "text/plain")},
            headers=H,
        )
        assert conflict.status_code == 409
        assert [path.name for path in (state.workspace / "inbox").iterdir()] == ["notes.txt"]
    await state.task
    visible = [item for item in await manager.transcript_page(sid) if not item.get("internal")]
    assert [item["role"] for item in visible] == ["user", "assistant"]
    assert not any((manager.settings.state_dir / "upload-staging").iterdir())
    await manager.close()


async def test_explicit_delivery_keeps_queue_and_upload_intents_distinct(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("explicit delivery")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        premature = await client.post(f"/api/sessions/{sid}/messages", json={"text": "later", "follow_up": True, "expected_running": True}, headers=H)
        assert premature.status_code == 409
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running
        queued = await client.post(f"/api/sessions/{sid}/messages", json={"text": "later", "follow_up": True, "expected_running": True, "client_message_id": "later-1"}, headers=H)
        assert queued.status_code == 200 and queued.json()["receipt"]["status"] == "queued"
        upload = await client.post(
            f"/api/sessions/{sid}/upload",
            data={"text": "now", "steer": "true", "expected_running": "true", "client_message_id": "now-1"},
            files={"files": ("notes.txt", b"same bytes", "text/plain")}, headers=H,
        )
        assert upload.status_code == 200 and upload.json()["receipt"]["status"] == "queued"
        queues = await manager.live.load(sid)
        assert [item["id"] for item in queues["follow_up"]] == ["later-1"]
        assert [item["id"] for item in queues["steer"]] == ["now-1"]
    await manager.close()


async def test_a_steer_the_run_has_read_is_gone_from_the_queue_and_cannot_be_withdrawn(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "first"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steered")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running
        await manager.submit(sid, "and also this", steer=True)
        item_id = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()[0]["id"]

        await _await_run(manager)
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert any("and also this" in t for t in texts)
        assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json() == []
        assert [c["reason"] for c in changes] == ["queued", "consumed"]

        views = await manager.transcript_page(sid)
        visible = [m for m in views if not m.get("internal")]
        received = [i for i, m in enumerate(visible) if m["role"] == "user" and m["text"] == "and also this"]
        assert len(received) == 1
        assert any(m["tool_calls"] for m in visible[:received[0]])
        assert visible[received[0]]["run_id"] == state.run_id
        assert all(m["run_id"] == state.run_id for m in visible if m["role"] == "assistant")

        late = await client.delete(f"/api/sessions/{sid}/steer/{item_id}", headers=H)
        assert late.status_code == 409 and "already reached" in late.json()["detail"]
    await manager.close()


async def test_the_queue_endpoints_answer_for_a_session_and_only_to_a_caller_with_the_token(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "idle"}])
    manager = await _manager(settings, db, provider)
    async with _client(settings, db, manager) as client:
        assert (await client.get("/api/sessions/nope/steer", headers=H)).status_code == 404
        assert (await client.delete("/api/sessions/nope/steer/q_1", headers=H)).status_code == 404
        state = await manager.create_session("quiet")
        assert (await client.delete(f"/api/sessions/{state.session.id}/steer/q_missing", headers=H)).status_code == 409
        assert (await client.get(f"/api/sessions/{state.session.id}/steer")).status_code in {401, 403}
    await manager.close()


@pytest.mark.parametrize("kind", ["steer"])
async def test_the_id_the_app_holds_is_the_id_the_store_wrote(settings: Settings, db: Database, kind: str) -> None:
    """The card's ``×`` quotes an id back, so it has to be the one the persist path keeps."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("ids")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    await manager.submit(sid, "one", steer=True)
    await manager.submit(sid, "two", steer=True)
    listed = await manager.queued_input(sid)
    stored = [item["id"] for item in (await manager.live.load(sid))[kind]]
    assert [item["id"] for item in listed] == stored and len(stored) == 2
    assert await manager.drop_queued_input(sid, stored[0])
    assert [item["id"] for item in await manager.queued_input(sid)] == stored[1:]
    await manager.close()


async def test_a_steer_queued_after_the_round_read_the_queue_survives_the_round(settings: Settings, db: Database) -> None:
    """The round writes back what it is holding, and what arrived behind its back is still waiting."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("racing")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None

    # The round reads the queue — empty — and the operator's message lands after that read.
    await engine.reload_live_control(engine)
    await manager.submit(sid, "and while you are there", steer=True)
    assert list(getattr(engine, "_steer_queue", [])) == []

    await engine.persist_live_control(engine)
    waiting = await manager.queued_input(sid)
    assert [item["text"] for item in waiting] == ["and while you are there"]
    assert [c["reason"] for c in changes] == ["queued"]

    # Now the round is handed it and places it: that, and only that, is consumed.
    await engine.reload_live_control(engine)
    assert [item["id"] for item in engine._steer_queue] == [waiting[0]["id"]]
    engine._steer_queue = []
    await engine.persist_live_control(engine)
    assert await manager.queued_input(sid) == []
    assert [c["reason"] for c in changes] == ["queued", "consumed"]
    await manager.close()


async def test_a_withdrawn_steer_is_not_brought_back_by_a_reload_that_raced_it(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("withdrawn")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None
    await engine.reload_live_control(engine)
    await manager.submit(sid, "forget this", steer=True)
    item_id = (await manager.queued_input(sid))[0]["id"]

    # The row as a reload that started before the withdrawal would be handed it.
    stale = await manager.live.load(sid)
    assert await manager.drop_queued_input(sid, item_id)

    original = manager.live.load

    async def stale_once(session_id: str) -> Any:
        manager.live.load = original  # type: ignore[method-assign]
        return stale

    manager.live.load = stale_once  # type: ignore[method-assign]
    await engine.reload_live_control(engine)
    assert list(getattr(engine, "_steer_queue", [])) == []

    await engine.persist_live_control(engine)
    assert await manager.queued_input(sid) == []
    await manager.close()


async def test_a_change_event_carries_cards_and_the_true_count(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "idle"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("chatty")
    sid = state.session.id
    for i in range(STEER_CARD_LIMIT + 5):
        await manager.live.enqueue(sid, "steer", {"id": f"q_{i}", "text": "x" * (STEER_CARD_CHARS + 50), "queued_at": None})

    cards = await manager.queued_input(sid)
    assert len(cards) == STEER_CARD_LIMIT
    assert all(len(card["text"]) == STEER_CARD_CHARS and card["truncated"] for card in cards)

    await manager.steer_changed(sid, reason="queued")
    assert changes[-1]["count"] == STEER_CARD_LIMIT + 5 and len(changes[-1]["queued"]) == STEER_CARD_LIMIT
    assert state.session.id == sid
    await manager.close()


async def test_a_follow_up_waits_as_a_card_and_can_be_turned_into_a_steer(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steer a follow-up")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running
        body = {"text": "check the log too", "follow_up": True, "expected_running": True, "client_message_id": "later-1"}
        assert (await client.post(f"/api/sessions/{sid}/messages", json=body, headers=H)).status_code == 200
        listed = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()
        assert [(item["id"], item["kind"]) for item in listed] == [("later-1", "follow_up")]
        assert changes[-1]["reason"] == "queued" and changes[-1]["queued"][0]["kind"] == "follow_up"

        steered = await client.post(f"/api/sessions/{sid}/steer/later-1", headers=H)
        assert steered.status_code == 200 and steered.json() == {"steered": True}
        listed = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()
        assert [(item["id"], item["kind"]) for item in listed] == [("later-1", "steer")]
        assert changes[-1]["reason"] == "steered"
        queues = await manager.live.load(sid)
        assert queues["follow_up"] == [] and [item["id"] for item in queues["steer"]] == ["later-1"]
        # A steer has nothing sooner left to become.
        assert (await client.post(f"/api/sessions/{sid}/steer/later-1", headers=H)).status_code == 409

        await _await_run(manager)
        # Read before the run's last model call, not in a turn of its own after it.
        assert len(provider.requests) == 2
        texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
        assert sum("check the log too" in t for t in texts) == 1
        visible = [m for m in await manager.transcript_page(sid) if not m.get("internal")]
        assert [m["text"] for m in visible if m["role"] == "user"] == ["start", "check the log too"]
        receipt = await manager.live.receipt(sid, "later-1")
        assert receipt is not None and receipt["status"] == "consumed"
        assert (await client.post(f"/api/sessions/{sid}/steer/later-1", headers=H)).status_code == 409
    await manager.close()


async def test_a_follow_up_the_engine_holds_is_steered_once_even_past_a_stale_reload(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("held")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None
    await manager.submit(sid, "after this", follow_up=True)
    item_id = (await manager.queued_input(sid))[0]["id"]
    stale = await manager.live.load(sid)
    await engine.reload_live_control(engine)
    assert [item["id"] for item in engine._follow_up_queue] == [item_id]

    assert await manager.steer_queued(sid, item_id)
    assert list(engine._follow_up_queue) == []

    # A reload that read the row before the move must not give the engine its follow-up copy back.
    original = manager.live.load

    async def stale_once(session_id: str) -> Any:
        manager.live.load = original  # type: ignore[method-assign]
        return stale

    manager.live.load = stale_once  # type: ignore[method-assign]
    await engine.reload_live_control(engine)
    assert list(engine._follow_up_queue) == []
    await engine.persist_live_control(engine)
    assert [(item["id"], item["kind"]) for item in await manager.queued_input(sid)] == [(item_id, "steer")]
    assert [c["reason"] for c in changes] == ["queued", "steered"]

    await engine.reload_live_control(engine)
    assert [item["id"] for item in engine._steer_queue] == [item_id]
    assert list(engine._follow_up_queue) == []
    await manager.close()


async def test_a_steered_follow_up_the_round_places_takes_its_card_away(settings: Settings, db: Database) -> None:
    """Once the run reads a message the operator steered, the app is told its card is gone."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    changes = _watch(manager)
    state = await manager.create_session("steered and read")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None
    await manager.submit(sid, "cheaper, please", follow_up=True)
    item_id = (await manager.queued_input(sid))[0]["id"]
    assert await manager.steer_queued(sid, item_id)

    # The next round is handed the steer and places it before any round wrote back in between.
    await engine.reload_live_control(engine)
    assert [item["id"] for item in engine._steer_queue] == [item_id]
    engine._steer_queue = []
    await engine.persist_live_control(engine)
    assert await manager.queued_input(sid) == []
    assert [c["reason"] for c in changes] == ["queued", "steered", "consumed"]
    assert changes[-1]["queued"] == []
    await manager.close()


async def test_a_follow_up_the_run_is_placing_cannot_be_steered(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("placing")
    sid = state.session.id
    await manager.submit(sid, "start")
    await asyncio.sleep(0.3)
    engine = state.engine
    assert engine is not None
    await manager.submit(sid, "after this", follow_up=True)
    item_id = (await manager.queued_input(sid))[0]["id"]
    await engine.reload_live_control(engine)
    # The core took it out of its list to place it; the store has not heard yet.
    engine._follow_up_queue = []
    assert not await manager.steer_queued(sid, item_id)
    assert (await manager.live.load(sid))["steer"] == []
    await manager.close()


# A one-pixel PNG: enough for the host to keep it as an image and hand it to the model.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
# The name a Windows clipboard gives a pasted screenshot, braces and all.
PASTED = "{8928C48B-9635-4A10-B7D6-0123456789AB}.png"


def _behave_like_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open a directory the way native Windows does: it cannot be opened as a file at all."""
    from daedalus.stores import blobs

    real_open = os.open

    def windows_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.path.isdir(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(blobs, "WINDOWS", True, raising=False)
    monkeypatch.setattr(blobs.os, "open", windows_open)


def _images_the_model_saw(provider: ScriptedProvider) -> list[str]:
    return [
        str(ref["ref"])
        for message in provider.requests[-1].messages
        for ref in (message.metadata or {}).get("image_refs") or []
    ]


async def test_a_blob_is_kept_on_a_platform_that_cannot_open_a_directory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.stores.blobs import FileBlobStore

    _behave_like_windows(monkeypatch)
    meta = await FileBlobStore(tmp_path).put("t", PIXEL, content_type="image/png")
    assert await FileBlobStore(tmp_path).get("t", meta.ref) == PIXEL


@pytest.mark.parametrize("delivery", ["follow_up", "steer_now"])
async def test_a_screenshot_sent_while_the_agent_works_waits_with_its_message_and_reaches_the_model(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, delivery: str,
) -> None:
    # Pasted on a native Windows installation during a run, this answered 500: the image's blob
    # write tried to sync its directory. Once that passed, the queued item still carried only words,
    # so the model was told a path and never shown the picture.
    _behave_like_windows(monkeypatch)
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 2"}}, {"text": "done"}, {"text": "looked"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("screenshot during a run")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        assert state.running
        sent = await client.post(
            f"/api/sessions/{sid}/upload",
            data={"text": "", "client_message_id": "shot-1", "follow_up": "true", "expected_running": "true"},
            files={"files": (PASTED, PIXEL, "image/png")}, headers=H,
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["receipt"]["status"] == "queued"
        assert sent.json()["files"] == [PASTED]
        assert (state.workspace / "inbox" / PASTED).read_bytes() == PIXEL

        cards = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()
        assert [(card["id"], card["kind"], card["text"], card["files"]) for card in cards] == [("shot-1", "follow_up", "", [PASTED])]
        if delivery == "steer_now":
            steered = await client.post(f"/api/sessions/{sid}/steer/shot-1", headers=H)
            assert steered.status_code == 200
            assert (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()[0]["files"] == [PASTED]
    await _await_run(manager)
    # One run either way: a steer is placed before the call after the tool, a follow-up at the end of
    # the turn, which the core answers without starting another run.
    assert len(provider.requests) == (2 if delivery == "steer_now" else 3)
    digest = hashlib.sha256(PIXEL).hexdigest()
    assert digest in _images_the_model_saw(provider)
    texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
    assert any(PASTED in text for text in texts)
    await manager.close()


async def test_a_queued_message_with_words_and_a_file_shows_the_words_and_names_the_file(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 2"}}, {"text": "done"}, {"text": "after"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("words and a file")
    sid = state.session.id
    async with _client(settings, db, manager) as client:
        await manager.submit(sid, "start")
        await asyncio.sleep(0.3)
        sent = await client.post(
            f"/api/sessions/{sid}/upload",
            data={"text": "look at this window", "client_message_id": "shot-2", "follow_up": "true", "expected_running": "true"},
            # A client that names the file by its whole Windows path still gives it its own name.
            files={"files": ("C:\\Users\\someone\\AppData\\Local\\Temp\\" + PASTED, PIXEL, "image/png")}, headers=H,
        )
        assert sent.status_code == 200, sent.text
        card = (await client.get(f"/api/sessions/{sid}/steer", headers=H)).json()[0]
        assert card["text"] == "look at this window" and card["files"] == [PASTED]
        assert [path.name for path in (state.workspace / "inbox").iterdir()] == [PASTED]
    await manager.close()
