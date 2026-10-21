"""Delete rows together with every row that would be left pointing at them.

The schema keeps a project's and a session's history in many tables, and several of them are
ledgers that refuse a ``DELETE`` outright (a ``BEFORE DELETE`` trigger that raises) and reference
their owner without ``ON DELETE CASCADE``. Such a row made its owner impossible to remove: deleting
an orchestrator's chat that had once met a provider refusal failed on the foreign key of the
refusal's record, *after* the deletion hooks had already switched the orchestrator off, so the
project silently fell out of orchestration and kept a chat nobody could delete.

:class:`Purge` follows the foreign keys the schema declares, so a table added later is covered
without anyone remembering this module. It first marks every row that depends on the rows asked
for, then deletes them all with the foreign keys deferred to the commit: a row the walk could not
reach still fails the commit, and the whole removal rolls back instead of half happening. Marking
by table rather than walking every path matters: the graph of the board, its attempts and their
receipts has so many paths that walking each one separately took minutes for one project.

A ledger's refusal to delete is lifted only inside the caller's transaction and put back before it
commits: no other connection ever sees the table without it, and the exemption covers exactly the
rows of a removal the operator asked for. A child whose key is ``ON DELETE SET NULL`` or
``SET DEFAULT`` outlives its parent by the schema's own choice and is left to SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite

_MARKS = "temp.purge_marks"


@dataclass(frozen=True)
class _Reference:
    child: str
    child_columns: tuple[str, ...]
    parent_columns: tuple[str, ...]
    on_delete: str


def _quoted(columns: Sequence[str]) -> str:
    return ", ".join(f'"{column}"' for column in columns)


class Purge:
    """One removal inside an open transaction: ``await purge.rows(table, where, params)`` for each
    set of rows to remove, then ``await purge.finish()``, which deletes them all and their
    dependants and restores the lifted triggers, before the transaction commits."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._children: dict[str, list[_Reference]] | None = None
        self._order: list[str] = []
        self._ready = False

    async def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        cursor = await self._conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def _changed(self, sql: str, params: Sequence[Any] = ()) -> int:
        cursor = await self._conn.execute(sql, tuple(params))
        count = cursor.rowcount
        await cursor.close()
        return count

    async def _graph(self) -> dict[str, list[_Reference]]:
        if self._children is not None:
            return self._children
        children: dict[str, list[_Reference]] = {}
        primary: dict[str, tuple[str, ...]] = {}
        tables = [row[0] for row in await self._fetch(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = await self._fetch(f'PRAGMA table_info("{table}")')
            primary[table.lower()] = tuple(c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5]))
        for table in tables:
            keys: dict[int, list[Any]] = {}
            for row in await self._fetch(f'PRAGMA foreign_key_list("{table}")'):
                keys.setdefault(row[0], []).append(row)
            for rows in keys.values():
                rows.sort(key=lambda r: r[1])
                parent = str(rows[0][2]).lower()
                parent_columns = tuple(r[4] for r in rows) if all(r[4] for r in rows) else primary.get(parent, ())
                on_delete = str(rows[0][6]).upper()
                if not parent_columns or on_delete in ("SET NULL", "SET DEFAULT"):
                    continue
                children.setdefault(parent, []).append(_Reference(
                    child=table, child_columns=tuple(r[3] for r in rows),
                    parent_columns=parent_columns, on_delete=on_delete,
                ))
        self._children = children
        return children

    async def _prepare(self) -> None:
        if self._ready:
            return
        await self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_marks (tbl TEXT NOT NULL, rid INTEGER NOT NULL, PRIMARY KEY (tbl, rid))")
        await self._conn.execute(f"DELETE FROM {_MARKS}")
        # Deferred to the commit, so the order the rows go in does not matter to a NO ACTION key, and
        # a reference the walk missed still fails the commit rather than surviving it. SQLite resets
        # this at the end of the transaction.
        await self._conn.execute("PRAGMA defer_foreign_keys = ON")
        self._ready = True

    async def rows(self, table: str, where: str, params: Sequence[Any] = ()) -> None:
        """Mark the rows of ``table`` matching ``where``, and every row that depends on them."""
        await self._prepare()
        graph = await self._graph()
        if await self._changed(f"INSERT OR IGNORE INTO {_MARKS}(tbl, rid) SELECT ?, rowid FROM \"{table}\" WHERE {where}",
                               (table.lower(), *params)):
            self._seen(table)
        pending = [table]
        while pending:
            parent = pending.pop()
            for reference in graph.get(parent.lower(), []):
                added = await self._changed(
                    f'INSERT OR IGNORE INTO {_MARKS}(tbl, rid) SELECT ?, c.rowid FROM "{reference.child}" AS c '
                    f"WHERE ({', '.join(f'c."{column}"' for column in reference.child_columns)}) IN "
                    f'(SELECT {_quoted(reference.parent_columns)} FROM "{parent}" '
                    f"WHERE rowid IN (SELECT rid FROM {_MARKS} WHERE tbl = ?))",
                    (reference.child.lower(), parent.lower()),
                )
                if added:
                    self._seen(reference.child)
                    pending.append(reference.child)

    def _seen(self, table: str) -> None:
        # The latest discovery wins, so a child is deleted before the parent it was found through;
        # that matters only to a RESTRICT key, which SQLite checks at once even when deferred.
        if table in self._order:
            self._order.remove(table)
        self._order.append(table)

    async def finish(self) -> None:
        """Delete every marked row, children first, then put back the triggers lifted for it."""
        lifted: dict[str, str] = {}
        for table in reversed(self._order):
            for name, sql in await self._fetch(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? COLLATE NOCASE", (table,),
            ):
                text = " ".join(str(sql or "").upper().split()).replace("RAISE (", "RAISE(")
                if name not in lifted and "BEFORE DELETE" in text and "RAISE(ABORT" in text:
                    lifted[name] = str(sql)
                    await self._conn.execute(f'DROP TRIGGER "{name}"')
            await self._conn.execute(f'DELETE FROM "{table}" WHERE rowid IN (SELECT rid FROM {_MARKS} WHERE tbl = ?)',
                                     (table.lower(),))
        for sql in lifted.values():
            await self._conn.execute(sql)
        if self._ready:
            await self._conn.execute(f"DELETE FROM {_MARKS}")
        self._order.clear()
