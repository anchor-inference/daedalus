"""The board keeps failed worker spend and unknown prices with an accepted result."""

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
async def test_accepted_result_cost_includes_failed_attempts_and_preserves_unknowns() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript("""
            CREATE TABLE board_tasks (id TEXT, accepted_result_id TEXT, accepted_contract_revision INTEGER);
            CREATE TABLE result_receipts (id TEXT, task_id TEXT, attempt_id TEXT, contract_revision INTEGER, created_at TEXT);
            CREATE TABLE execution_attempts (id TEXT, task_id TEXT, contract_revision INTEGER, runtime_kind TEXT, created_at TEXT);
            CREATE TABLE runtime_no_entry_observations (attempt_id TEXT PRIMARY KEY);
            CREATE TABLE inference_reservations (id TEXT, execution_attempt_id TEXT, state TEXT, actual_microusd INTEGER,
                created_at TEXT NOT NULL DEFAULT '2026-01-02');
            INSERT INTO board_tasks VALUES ('priced','accepted',2),('cli','cli-result',1),('pending','pending-result',1),
                ('zero','zero-result',1),('manual','manual-result',1);
            INSERT INTO result_receipts VALUES ('accepted','priced','new',2,'2026-01-03'),
                ('cli-result','cli','cli-attempt',1,'2026-01-03'),
                ('pending-result','pending','pending-attempt',1,'2026-01-03'),
                ('zero-result','zero','zero-attempt',1,'2026-01-03'),
                ('manual-result','manual',NULL,1,'2026-01-03');
            INSERT INTO execution_attempts VALUES ('old','priced',1,'daedalus','2026-01-01'),
                ('new','priced',2,'daedalus','2026-01-02'),
                ('future','priced',2,'daedalus','2026-01-04'),
                ('cli-attempt','cli',1,'cli','2026-01-02'),
                ('pending-attempt','pending',1,'daedalus','2026-01-02'),
                ('refused','priced',1,'daedalus','2026-01-02'),
                ('cli-refused','priced',1,'cli','2026-01-02'),
                ('cli-live','priced',1,'cli','2026-01-02'),
                ('zero-attempt','zero',1,'daedalus','2026-01-02'),
                ('manual-old','manual',1,'daedalus','2026-01-01');
            INSERT INTO runtime_no_entry_observations VALUES ('refused'),('cli-refused');
            INSERT INTO inference_reservations (id,execution_attempt_id,state,actual_microusd) VALUES
                ('old-charge','old','settled',900000),
                ('new-charge','new','settled',12000),('future-charge','future','settled',800000),
                ('post-acceptance-charge','old','settled',700000),
                ('partial','pending-attempt','settled',10000),('unresolved','pending-attempt','unknown',NULL),
                ('released-only','refused','released',NULL),('cli-released','cli-refused','released',NULL),
                ('zero-charge','zero-attempt','settled',0),
                ('manual-charge','manual-old','settled',50000);
            UPDATE inference_reservations SET created_at = '2026-01-04' WHERE id = 'post-acceptance-charge';
        """)
        tasks = [{"id": name, "accepted_result_id": receipt, "acceptance_state": "operator_approved"}
                 for name, receipt in (("priced", "accepted"), ("cli", "cli-result"),
                                       ("pending", "pending-result"), ("zero", "zero-result"),
                                       ("manual", "manual-result"))]
        board = Board(SimpleNamespace(db=CostDb(conn)))
        await board._accepted_result_costs(tasks)

    costs = {task["id"]: task["accepted_result_cost_microusd"] for task in tasks}
    known = {task["id"]: task["accepted_result_known_cost_microusd"] for task in tasks}
    reasons = {task["id"]: task["accepted_result_cost_unknown_reasons"] for task in tasks}
    assert costs == {"priced": None, "cli": None, "pending": None, "zero": 0, "manual": None}
    assert known == {"priced": 912000, "cli": 0, "pending": 10000, "zero": 0, "manual": 50000}
    assert reasons == {"priced": ["subscription"], "cli": ["subscription"], "pending": ["unpriced"],
                       "zero": [], "manual": ["unobserved"]}
