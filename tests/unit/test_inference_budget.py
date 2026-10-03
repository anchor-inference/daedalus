"""Concurrent admission and crash recovery never refund an uncertain inference charge."""

from __future__ import annotations

import asyncio
import hashlib
import json

import aiosqlite
import pytest

from daedalus.stores.control import canonical
from daedalus.stores.database import Database
from daedalus.stores.inference_budget import BudgetRefused, Constraint, InferenceBudget, microusd

QUOTE = {"provider": "test", "model": "model", "input_bound": 100, "output_bound": 20,
         "bound_source": "test-provider-enforced-ceiling", "input_rate": "0.5", "output_rate": "2"}


async def reserve(db: Database, identity: str, cost: int = 70, *,
                  constraints: tuple[Constraint, ...] = (Constraint("total", 100),)):
    async with db.transaction() as conn:
        return await InferenceBudget(db).reserve_in(
            conn, reservation_id=identity, provider_id="test", model="model", session_id=None, run_id=None,
            request_digest=hashlib.sha256(identity.encode()).hexdigest(), quoted_microusd=cost,
            rate_version=hashlib.sha256(canonical(QUOTE).encode()).hexdigest(), quote=QUOTE, constraints=constraints,
        )


async def usage(conn, identity: str, cost: float | None, *, raw=None, provider="test") -> int:
    cursor = await conn.execute(
        "INSERT INTO usage_events(at,provider_id,model,purpose,cost_usd,raw,inference_reservation_id)"
        " VALUES ('2026-01-01',?,'model','test',?,?,?)",
        (provider, cost, json.dumps({"prompt_tokens": 1, "completion_tokens": 1} if raw is None else raw), identity),
    )
    seq = cursor.lastrowid
    await cursor.close()
    return seq


async def test_concurrent_calls_cannot_both_spend_the_last_balance(db: Database) -> None:
    results = await asyncio.gather(reserve(db, "first"), reserve(db, "second"), return_exceptions=True)
    assert sum(isinstance(result, BudgetRefused) for result in results) == 1
    assert (await db.fetchone("SELECT count(*),sum(quoted_microusd) FROM inference_reservations"))[:] == (1, 70)


async def test_every_cap_is_checked_before_any_reservation_row_is_inserted(db: Database) -> None:
    with pytest.raises(BudgetRefused, match="session"):
        await reserve(db, "refused", constraints=(Constraint("total", 100), Constraint("session", 50)))
    assert (await db.fetchone("SELECT count(*) FROM inference_reservations"))[0] == 0
    assert (await db.fetchone("SELECT count(*) FROM inference_reservation_scopes"))[0] == 0


async def test_spend_view_keeps_uncertain_and_inflight_quotes_after_counter_reset(db: Database) -> None:
    await reserve(db, "uncertain", 30)
    await reserve(db, "active", 20)
    await reserve(db, "complete", 10)
    async with db.transaction() as conn:
        store = InferenceBudget(db)
        for identity in ("uncertain", "active", "complete"):
            await store.start_in(conn, identity)
        await store.unknown_in(conn, "uncertain", "response lost")
        seq = await usage(conn, "uncertain", None, raw={})
        assert await store.settle_in(conn, "uncertain", seq) == "unknown"
        seq = await usage(conn, "complete", 0.000007)
        assert await store.settle_in(conn, "complete", seq) == "settled"
    store = InferenceBudget(db)
    before = await store.spend_view(since=None, total_cap=0.0001, provider_caps={"unused": 1})
    assert before["total"] == {"spent_usd": 0.000007, "unmetered": 1, "cap_usd": 0.0001,
                               "reserved_usd": 0.00005, "uncertain_usd": 0.00003,
                               "reserved_count": 2, "uncertain_count": 1}
    assert before["per_provider"]["test"]["reserved_usd"] == 0.00005
    assert before["per_provider"]["unused"]["cap_usd"] == 1
    after = await store.spend_view(since="2026-01-02", total_cap=0.0001, provider_caps={})
    assert after["total"]["spent_usd"] == 0 and after["total"]["unmetered"] == 0
    for field in ("reserved_usd", "uncertain_usd", "reserved_count", "uncertain_count"):
        assert after["total"][field] == before["total"][field]
    assert after["per_provider"]["test"]["reserved_count"] == 2


async def test_started_unknown_call_keeps_its_reservation_after_reopen(db: Database) -> None:
    await reserve(db, "uncertain")
    async with db.transaction() as conn:
        await InferenceBudget(db).start_in(conn, "uncertain")
        await InferenceBudget(db).unknown_in(conn, "uncertain", "response lost")
        with pytest.raises(BudgetRefused, match="never crossed"):
            await InferenceBudget(db).cancel_unstarted_in(conn, "uncertain", "try to refund")
    path = db.path
    await db.close()
    await db.open()
    assert db.path == path
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "retry")


async def test_unstarted_cancel_refunds_once_but_start_and_refund_cannot_race(db: Database) -> None:
    await reserve(db, "never-sent")
    async with db.transaction() as conn:
        store = InferenceBudget(db)
        await store.cancel_unstarted_in(conn, "never-sent", "permission withdrawn before transport")
        with pytest.raises(BudgetRefused, match="start twice"):
            await store.start_in(conn, "never-sent")
    assert (await reserve(db, "replacement"))["state"] == "reserved"


async def test_actual_charge_and_settlement_share_a_transaction_and_replay_exactly(db: Database) -> None:
    await reserve(db, "charged")
    async with db.transaction() as conn:
        store = InferenceBudget(db)
        await store.start_in(conn, "charged")
        seq = await usage(conn, "charged", 0.00001)
        assert await store.settle_in(conn, "charged", seq) == "settled"
        assert await store.settle_in(conn, "charged", seq) == "settled"
    assert (await reserve(db, "next"))["quoted_microusd"] == 70
    assert (await db.fetchone("SELECT actual_microusd FROM inference_reservations WHERE id = 'charged'"))[0] == 10


@pytest.mark.parametrize("raw,cost", [({}, 0), ({"other": "field"}, 0), ({"prompt_tokens": 1}, 0),
                                     ({"prompt_tokens": 1, "completion_tokens": 1}, None)])
async def test_incomplete_usage_cannot_free_reserved_balance(db: Database, raw, cost) -> None:
    await reserve(db, "missing")
    async with db.transaction() as conn:
        store = InferenceBudget(db)
        await store.start_in(conn, "missing")
        seq = await usage(conn, "missing", cost, raw=raw)
        assert await store.settle_in(conn, "missing", seq) == "unknown"
    with pytest.raises(BudgetRefused):
        await reserve(db, "next")


async def test_wrong_usage_identity_rolls_back_its_row_and_provider_overrun_remains_visible(db: Database) -> None:
    await reserve(db, "charged")
    async with db.transaction() as conn:
        await InferenceBudget(db).start_in(conn, "charged")
    with pytest.raises(BudgetRefused, match="another admission"):
        async with db.transaction() as conn:
            seq = await usage(conn, "charged", 0.00001, provider="different")
            await InferenceBudget(db).settle_in(conn, "charged", seq)
    assert (await db.fetchone("SELECT count(*) FROM usage_events"))[0] == 0
    async with db.transaction() as conn:
        seq = await usage(conn, "charged", 0.0002)
        assert await InferenceBudget(db).settle_in(conn, "charged", seq) == "overrun"
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "after-overrun", 1)


async def test_quote_and_its_rates_cannot_be_rewritten_after_admission(db: Database) -> None:
    await reserve(db, "pinned")
    with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
        await db.execute("UPDATE inference_reservations SET quoted_microusd = 0 WHERE id = 'pinned'")
    with pytest.raises(aiosqlite.IntegrityError, match="provenance"):
        await db.execute("DELETE FROM inference_reservations WHERE id = 'pinned'")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "wrong", None, "1e100"])
def test_invalid_money_is_not_an_available_balance(bad: str) -> None:
    with pytest.raises(BudgetRefused):
        microusd(bad)


def test_fractional_microdollars_round_up() -> None:
    assert microusd("0.0000001") == 1


async def test_counter_reset_does_not_refund_a_call_which_can_settle_later(db: Database) -> None:
    await reserve(db, "before-reset", constraints=(Constraint("total-old", 100, since="2026-01-01"),))
    async with db.transaction() as conn:
        await InferenceBudget(db).start_in(conn, "before-reset")
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "after-reset", constraints=(Constraint("total-new", 100, since="2027-01-01"),))


async def test_a_new_cap_counts_uncapped_calls_that_are_already_in_flight(db: Database) -> None:
    await reserve(db, "before-cap", constraints=())
    async with db.transaction() as conn:
        await InferenceBudget(db).start_in(conn, "before-cap")
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "after-cap")


async def test_unknown_usage_with_a_known_held_quote_can_use_only_the_other_balance(db: Database) -> None:
    await reserve(db, "missing")
    async with db.transaction() as conn:
        store = InferenceBudget(db)
        await store.start_in(conn, "missing")
        seq = await usage(conn, "missing", None, raw={})
        assert await store.settle_in(conn, "missing", seq) == "unknown"
    await reserve(db, "other", 30)
    with pytest.raises(BudgetRefused, match="available balance"):
        await reserve(db, "over-budget", 1)
