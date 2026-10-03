"""The event bus: what it stores, in what order it delivers, and what a slow subscriber still gets.

The subscribers that matter most are in-process — a notification router, an orchestrator waiting
for a permission request — and a lost event there is a question nobody answers. So most of these
tests are about the one promise that is easy to break: every persisted event reaches every matching
subscriber, once, in order, however far behind it fell.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.host.events import (
    GAP,
    MAX_PAYLOAD_BYTES,
    AppEvent,
    EventBus,
    EventFilter,
    EventPayloadError,
    UnknownEventType,
)
from daedalus.security.redact import MASK
from daedalus.stores.database import Database


def bell() -> dict[str, Any]:
    return {}


def title(text: str = "shell") -> dict[str, Any]:
    return {"title": text}


def status(value: str = "idle") -> dict[str, Any]:
    return {"status": value}


@pytest.fixture
async def bus(db: Database) -> EventBus:
    instance = EventBus(db)
    await instance.start()
    yield instance  # type: ignore[misc]
    await instance.close()


async def _rows(db: Database) -> list[dict[str, Any]]:
    return [dict(r) for r in await db.fetchall("SELECT * FROM app_events ORDER BY seq")]


async def _take(subscription: Any, count: int, *, timeout: float = 5.0) -> list[AppEvent]:
    out: list[AppEvent] = []
    async with asyncio.timeout(timeout):
        while len(out) < count:
            out.append(await anext(subscription))
    return out


# -- storing -------------------------------------------------------------------------------------


async def test_seq_only_grows_across_a_new_bus_and_a_prune_that_emptied_the_table(db: Database) -> None:
    """A cursor held by a client must never point at a different event later; the plain rowid would
    hand the highest number out again once it was deleted."""
    first = EventBus(db)
    await first.start()
    seqs = [(await first.publish("terminal.title", title(str(i)), terminal_id="t1")).seq for i in range(3)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3 and seqs[0] >= 1
    await first.close()

    second = EventBus(db)
    await second.start()
    assert second.head == seqs[-1]
    assert (await second.publish("terminal.bell", bell())).seq == seqs[-1] + 1
    dropped = await second.prune(keep_days=1, max_rows=0)
    assert dropped == 4 and await _rows(db) == []
    assert second.oldest == second.head + 1
    await second.close()

    third = EventBus(db)
    await third.start()
    assert (third.head, third.oldest) == (seqs[-1] + 1, seqs[-1] + 2)
    assert (await third.publish("terminal.bell", bell())).seq == seqs[-1] + 2
    await third.close()


async def test_domain_event_is_visible_only_after_its_transaction_commits(bus: EventBus, db: Database) -> None:
    async with bus.subscribe() as subscription:
        async with bus.transaction_guard():
            async with db.transaction() as conn:
                event = await bus.persist_in(conn, "terminal.title", title("ready"), terminal_id="t1")
            bus.announce_committed(event)
        delivered = await _take(subscription, 1)
        assert delivered[0].seq == event.seq
        assert bus.head == event.seq

        with pytest.raises(ValueError, match="abort"):
            async with bus.transaction_guard():
                async with db.transaction() as conn:
                    await bus.persist_in(conn, "terminal.title", title("discard"), terminal_id="t1")
                    raise ValueError("abort")
        assert [row["payload_json"] for row in await _rows(db)] == ['{"title": "ready"}']
        assert bus.head == event.seq


async def test_publish_refuses_what_is_not_the_contract(bus: EventBus) -> None:
    with pytest.raises(UnknownEventType):
        await bus.publish("terminal.exploded", {})
    with pytest.raises(UnknownEventType):
        await bus.publish(GAP, {"from": 1, "to": 2})  # synthetic: yielded by the bus, never published
    with pytest.raises(EventPayloadError, match="title"):
        await bus.publish("terminal.title", {})
    with pytest.raises(EventPayloadError):
        await bus.publish("terminal.title", {"title": object()})
    with pytest.raises(EventPayloadError):
        await bus.publish("terminal.progress", {"state": "set", "percent": float("nan")})
    with pytest.raises(EventPayloadError):
        await bus.publish("terminal.title", ["title"])  # type: ignore[arg-type]
    with pytest.raises(EventPayloadError, match="terminal_id"):
        await bus.publish("terminal.bell", {}, terminal_id=7)  # type: ignore[arg-type]
    with pytest.raises(EventPayloadError, match="references"):
        await bus.publish("terminal.title", {"title": "x" * (MAX_PAYLOAD_BYTES + 1)})
    with pytest.raises(UnknownEventType):
        bus.publish_soon("nothing.here", {})
    assert bus.head == 0, "nothing refused may reach the table"


async def test_an_optional_key_may_be_left_out_and_an_extra_one_passes(bus: EventBus, db: Database) -> None:
    """Owners add optional keys to their own types; the bus must not be the thing that stops them."""
    event = await bus.publish("terminal.exited", {"exit_code": 0, "added_later": {"nested": [1, 2]}}, terminal_id="t1")
    assert event.payload["added_later"] == {"nested": [1, 2]}
    stored = json.loads((await _rows(db))[0]["payload_json"])
    assert stored == {"exit_code": 0, "added_later": {"nested": [1, 2]}}


async def test_an_ephemeral_event_is_delivered_live_and_never_written(bus: EventBus, db: Database) -> None:
    async with bus.subscribe() as subscription:
        event = await bus.publish("terminal.progress", {"state": "set", "percent": 40}, terminal_id="t1")
        assert event.seq == 0
        (received,) = await _take(subscription, 1)
    assert received.type == "terminal.progress" and received.seq == 0 and received.payload["percent"] == 40
    assert await _rows(db) == [] and bus.head == 0


async def test_a_secret_is_masked_in_the_row_and_in_what_subscribers_receive(bus: EventBus, db: Database) -> None:
    token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    async with bus.subscribe() as subscription:
        await bus.publish("terminal.command", {"exit_code": 1, "command": f"git push https://x:{token}@example.com/r.git"}, terminal_id="t1")
        (received,) = await _take(subscription, 1)
    assert token not in received.payload["command"] and MASK in received.payload["command"]
    assert token not in (await _rows(db))[0]["payload_json"]


# -- filtering -----------------------------------------------------------------------------------


async def test_filters_by_exact_type_by_prefix_and_by_each_id(bus: EventBus) -> None:
    await bus.publish("terminal.title", title(), terminal_id="t1", project_id="p1")
    await bus.publish("terminal.bell", bell(), terminal_id="t2")
    await bus.publish("session.status", status(), session_id="s1", project_id="p1")
    await bus.publish("staff.report", {"kind": "done", "text": "ok"}, staff_id="w1", project_id="p2")
    await bus.publish("session.unread_result", {"unread": True}, session_id="s2")
    cases: list[tuple[EventFilter, list[str]]] = [
        (EventFilter(), ["terminal.title", "terminal.bell", "session.status", "staff.report", "session.unread_result"]),
        (EventFilter(types=("terminal.bell",)), ["terminal.bell"]),
        (EventFilter(types=("terminal.",)), ["terminal.title", "terminal.bell"]),
        # An underscore is a LIKE wildcard; the prefix test must not treat it as one.
        (EventFilter(types=("session.unread_",)), []),
        (EventFilter(types=("session.",)), ["session.status", "session.unread_result"]),
        (EventFilter(types=("staff.report", "terminal.")), ["terminal.title", "terminal.bell", "staff.report"]),
        (EventFilter(project_id="p1"), ["terminal.title", "session.status"]),
        (EventFilter(session_id="s2"), ["session.unread_result"]),
        (EventFilter(staff_id="w1"), ["staff.report"]),
        (EventFilter(terminal_id="t2"), ["terminal.bell"]),
        (EventFilter(types=("terminal.",), project_id="p1"), ["terminal.title"]),
    ]
    everything = await bus.replay(0)
    for flt, expected in cases:
        assert [e.type for e in await bus.replay(0, flt)] == expected, flt
        assert [e.type for e in everything if flt.matches(e)] == expected, f"matches() and sql() disagree on {flt}"


async def test_a_live_subscription_receives_only_what_its_filter_selects(bus: EventBus) -> None:
    async with bus.subscribe(EventFilter(types=("terminal.",), terminal_id="t1")) as subscription:
        await bus.publish("terminal.bell", bell(), terminal_id="t2")
        await bus.publish("session.status", status(), session_id="s1")
        wanted = await bus.publish("terminal.bell", bell(), terminal_id="t1")
        (received,) = await _take(subscription, 1)
    assert received.seq == wanted.seq


# -- ordering and catching up --------------------------------------------------------------------


async def test_a_cursor_replays_then_joins_the_live_feed_with_nothing_lost_or_repeated(bus: EventBus) -> None:
    for i in range(50):
        await bus.publish("terminal.title", title(str(i)), terminal_id="t1")
    cursor = 20

    async def publisher() -> None:
        for i in range(200):
            await bus.publish("terminal.title", title(f"live {i}"), terminal_id="t1")
            if i % 7 == 0:
                await asyncio.sleep(0)

    async with bus.subscribe(after=cursor, name="resuming") as subscription:
        task = asyncio.create_task(publisher())
        received = await _take(subscription, 230)
        await task
    assert [e.seq for e in received] == list(range(cursor + 1, 251))
    assert subscription.last_seq == 250


async def test_a_subscriber_that_overflows_its_queue_reads_the_rest_from_the_table(bus: EventBus) -> None:
    bus.queue_size = 4
    async with bus.subscribe(name="slow") as subscription:
        for i in range(100):
            await bus.publish("terminal.title", title(str(i)), terminal_id="t1")
        assert subscription.lagging
        received = await _take(subscription, 100)
        assert [e.seq for e in received] == list(range(1, 101))
        assert [e.payload["title"] for e in received] == [str(i) for i in range(100)]
        assert not subscription.lagging, "caught up, it must rejoin the live feed"
        live = await bus.publish("terminal.bell", bell())
        (after,) = await _take(subscription, 1)
        assert after.seq == live.seq


async def test_a_slow_subscriber_keeps_order_while_the_publisher_never_stops(bus: EventBus) -> None:
    """Overflow, catch-up and rejoin, over and over, with events published at every step of it."""
    bus.queue_size = 4
    total = 400

    async def publisher() -> None:
        for i in range(total):
            await bus.publish("terminal.title", title(str(i)), terminal_id="t1")
            if i % 3 == 0:
                await asyncio.sleep(0)

    async with bus.subscribe(EventFilter(types=("terminal.title",)), name="dawdler") as subscription:
        task = asyncio.create_task(publisher())
        received: list[int] = []
        async with asyncio.timeout(10):
            while len(received) < total:
                received.append((await anext(subscription)).seq)
                if len(received) % 5 == 0:
                    await asyncio.sleep(0.001)
        await task
    assert received == list(range(1, total + 1))


async def test_a_lagging_subscriber_loses_ephemeral_events_and_counts_them(bus: EventBus) -> None:
    bus.queue_size = 4
    async with bus.subscribe() as subscription:
        for _ in range(6):
            await bus.publish("terminal.bell", bell())
        await bus.publish("terminal.progress", {"state": "set"})
        received = await _take(subscription, 6)
    assert [e.seq for e in received] == list(range(1, 7))
    assert subscription.dropped == 1


async def test_a_cursor_older_than_retention_is_told_about_the_gap_first(bus: EventBus) -> None:
    for i in range(10):
        await bus.publish("terminal.title", title(str(i)))
    await bus.prune(keep_days=1, max_rows=4)
    assert bus.oldest == 7
    async with bus.subscribe(after=2) as subscription:
        gap, *rest = await _take(subscription, 5)
    assert gap.type == GAP and dict(gap.payload) == {"from": 2, "to": 6}
    assert [e.seq for e in rest] == [7, 8, 9, 10]


async def test_publish_soon_publishes_in_call_order(bus: EventBus) -> None:
    async with bus.subscribe() as subscription:
        for i in range(30):
            bus.publish_soon("terminal.title", title(str(i)), terminal_id="t1")
        received = await _take(subscription, 30)
    assert [e.payload["title"] for e in received] == [str(i) for i in range(30)]


# -- handlers and lifetime -----------------------------------------------------------------------


async def test_a_handler_that_raises_is_logged_and_keeps_receiving(bus: EventBus, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[int] = []
    done = asyncio.Event()

    async def handle(event: AppEvent) -> None:
        seen.append(event.seq)
        if event.seq == 1:
            raise RuntimeError("first one breaks")
        if event.seq == 3:
            done.set()

    with caplog.at_level(logging.ERROR):
        task = bus.on(EventFilter(types=("terminal.",)), handle, name="fragile")
        await asyncio.sleep(0)
        for _ in range(3):
            await bus.publish("terminal.bell", bell())
        async with asyncio.timeout(5):
            await done.wait()
    assert seen == [1, 2, 3]
    assert any("fragile" in r.getMessage() for r in caplog.records)
    assert not task.done()


async def test_close_ends_every_iterator_and_every_handler(db: Database) -> None:
    bus = EventBus(db)
    await bus.start()
    subscription = bus.subscribe()
    waiting = asyncio.create_task(anext(subscription))

    async def never_mind(_: AppEvent) -> None:
        return None

    handler = bus.on(None, never_mind, name="idle")
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await bus.close()
    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await waiting
        await handler
    assert handler.done() and not handler.cancelled()
    # And after the close, a publisher is not the one that pays for a host on its way down.
    assert (await bus.publish("terminal.bell", bell())).seq == 0
    with pytest.raises(StopAsyncIteration):
        await anext(bus.subscribe())


async def test_close_publishes_what_publish_soon_still_had_queued(db: Database) -> None:
    bus = EventBus(db)
    await bus.start()
    for i in range(5):
        bus.publish_soon("terminal.title", title(str(i)))
    await bus.close()
    assert [json.loads(r["payload_json"])["title"] for r in await _rows(db)] == [str(i) for i in range(5)]


async def test_prune_by_age_and_by_row_count(bus: EventBus, db: Database) -> None:
    for i in range(6):
        await bus.publish("terminal.title", title(str(i)))
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    await db.execute("UPDATE app_events SET at = ? WHERE seq <= 2", (old,))
    assert await bus.prune(keep_days=7, max_rows=1000) == 2
    assert [r["seq"] for r in await _rows(db)] == [3, 4, 5, 6] and bus.oldest == 3
    assert await bus.prune(keep_days=7, max_rows=3) == 1
    assert [r["seq"] for r in await _rows(db)] == [4, 5, 6] and bus.oldest == 4
    assert bus.head == 6
