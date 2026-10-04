"""The Board prices only native inference attached to the accepted worker attempt."""

from types import SimpleNamespace

import aiosqlite
import pytest

from daedalus.extensions.board import Board


class CostDb:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def fetchall(self, query: str, params: tuple[str, ...]) -> list[aiosqlite.Row]:
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchall()


@pytest.mark.asyncio
async def test_accepted_attempt_cost_uses_exact_receipt_and_unknown_when_unpriced() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript("""
            CREATE TABLE board_tasks (id TEXT, accepted_result_id TEXT, accepted_contract_revision INTEGER);
            CREATE TABLE result_receipts (id TEXT, task_id TEXT, attempt_id TEXT, contract_revision INTEGER);
            CREATE TABLE execution_attempts (id TEXT, task_id TEXT, contract_revision INTEGER, runtime_kind TEXT);
            CREATE TABLE inference_reservations (id TEXT, execution_attempt_id TEXT, state TEXT, actual_microusd INTEGER);
            INSERT INTO board_tasks VALUES ('priced','accepted',2),('cli','cli-result',1),('pending','pending-result',1),('stale','stale-result',2),('released','released-result',1);
            INSERT INTO result_receipts VALUES ('accepted','priced','new',2),('cli-result','cli','cli-attempt',1),
                ('pending-result','pending','pending-attempt',1),('stale-result','stale','stale-attempt',1),
                ('released-result','released','released-attempt',1);
            INSERT INTO execution_attempts VALUES ('old','priced',1,'daedalus'),('new','priced',2,'daedalus'),
                ('cli-attempt','cli',1,'cli'),('pending-attempt','pending',1,'daedalus'),('stale-attempt','stale',1,'daedalus'),
                ('released-attempt','released',1,'daedalus');
            INSERT INTO inference_reservations VALUES ('old-charge','old','settled',900000),('new-charge','new','settled',12000),
                ('cli-charge','cli-attempt','settled',30000),('partial','pending-attempt','settled',10000),
                ('unresolved','pending-attempt','unknown',NULL),('stale-charge','stale-attempt','settled',40000),
                ('released-only','released-attempt','released',NULL);
        """)
        tasks = [{"id": name, "accepted_result_id": receipt, "acceptance_state": "operator_approved"}
                 for name, receipt in (("priced", "accepted"), ("cli", "cli-result"),
                                       ("pending", "pending-result"), ("stale", "stale-result"),
                                       ("released", "released-result"))]
        board = Board(SimpleNamespace(db=CostDb(conn)))
        await board._accepted_attempt_costs(tasks)

    assert [task["accepted_attempt_cost_microusd"] for task in tasks] == [12000, None, None, None, None]
