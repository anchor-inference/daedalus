"""Retention for the workspace checkpoint stores.

A snapshot is taken before every operator turn and after every run, and nothing ever removed
one: a store grew for as long as the session did, and the stores together for as long as the
installation. Retention gives them two bounds — an age and a total size — and one promise that
overrides both: the newest ``ops.checkpoint_keep_last`` snapshots of a session are always there,
so the undo the operator actually reaches for cannot be taken away by a store somebody else filled.

The snapshots of one workspace are a single chain of commits, so an old one is not dropped by
deleting a ref: :meth:`daedalus.host.checkpoints.Checkpoints.truncate` cuts the chain at the
oldest snapshot that stays and lets ``git gc`` remove what is then unreachable. That is also why
the unit of work here is the *store* and not the session — a subagent shares its leader's
workspace, and both sessions' snapshots are commits in the same chain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from daedalus.config import OpsConfig
from daedalus.host.checkpoints import (
    SESSION_WORKSPACES_SQL,
    WORKSPACE_FILE,
    CheckpointError,
    Checkpoints,
    session_workspace,
    store_dir,
)
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

SIZE_CUT_BATCH = 25
"""Snapshots dropped between two measurements while a store is over the size bound. A checkpoint's
share of the store cannot be known without cutting it, so the size pass measures, cuts a little,
and measures again; the batch is what keeps that from being one ``git gc`` per snapshot."""

DELETE_CHUNK = 400
"""Row ids per DELETE statement, so a pass that drops thousands does not build one enormous query."""


@dataclass(frozen=True, slots=True)
class RetentionBounds:
    """What the operator decided: an age, a total size, and what is kept whatever they say."""

    keep_days: int
    total_max_gb: float
    keep_last: int

    @classmethod
    def from_ops(cls, ops: OpsConfig) -> RetentionBounds:
        return cls(keep_days=ops.checkpoint_keep_days, total_max_gb=ops.checkpoint_total_max_gb, keep_last=ops.checkpoint_keep_last)


@dataclass(slots=True)
class RetentionReport:
    """What one pass did, in the terms the log line and the CLI print."""

    stores: int = 0
    dropped: int = 0
    cut: int = 0
    skipped: int = 0
    """Stores left alone: a session working in one, or a workspace this process cannot see."""
    size_before: int = 0
    size_after: int = 0

    @property
    def freed(self) -> int:
        return max(0, self.size_before - self.size_after)

    def line(self) -> str:
        return (
            f"{self.dropped} checkpoint(s) dropped from {self.cut} store(s) of {self.stores}, "
            f"{self.freed / 1e6:.1f} MB freed, {self.size_after / 1e9:.2f} GB left"
            + (f", {self.skipped} store(s) left alone" if self.skipped else "")
        )


@dataclass(slots=True)
class _Store:
    """One workspace's snapshot store and every checkpoint row committed into it, oldest first."""

    git_dir: Path
    workspace: Path
    sessions: set[str] = field(default_factory=set)
    rows: list[Any] = field(default_factory=list)
    protected: set[int] = field(default_factory=set)
    blocked: bool = False
    """Set when a cut did not happen, so the size pass does not ask the same store again forever."""

    def droppable(self, limit: int, older_than: datetime | None) -> int:
        """How many of the oldest rows may go: unprotected, and older than ``older_than`` if given."""
        count = 0
        for row in self.rows:
            if count >= limit or row["id"] in self.protected:
                break
            if older_than is not None and (_at(row) is None or _at(row) >= older_than):  # type: ignore[operator]
                break
            count += 1
        return count


class StoreSizes:
    """How big each store is, re-measured only when it has been written to.

    The stores are a gigabyte on an installation that has been running for months and the
    maintenance tick comes round every hour; walking all of them each pass to learn that nothing
    changed is the cost this class exists to avoid. ``git add`` and ``git commit`` both replace
    files directly in the git directory, so that directory's own mtime together with the index's
    says whether the last measurement still stands — and a cut invalidates its store by hand.
    """

    def __init__(self) -> None:
        self._sizes: dict[Path, tuple[tuple[int, int], int]] = {}
        self.walks = 0
        """How many measurements actually walked a store; the rest were answered from the cache."""

    def size(self, git_dir: Path) -> int:
        marker = _marker(git_dir)
        cached = self._sizes.get(git_dir)
        if cached is not None and cached[0] == marker:
            return cached[1]
        total = _dir_size(git_dir)
        self.walks += 1
        self._sizes[git_dir] = (marker, total)
        return total

    def forget(self, git_dir: Path) -> None:
        self._sizes.pop(git_dir, None)


class CheckpointRetention:
    """The retention pass: read the rows, decide what goes, cut the stores, write the rows back."""

    def __init__(
        self,
        db: Database,
        *,
        workspaces_dir: Path,
        checkpoints_dir: Path,
        busy: Callable[[], set[str]] | None = None,
        occupants: Callable[[], dict[Path, set[str]]] | None = None,
    ) -> None:
        self.db = db
        self.workspaces_dir = workspaces_dir
        self.checkpoints_dir = checkpoints_dir
        self._busy = busy or (lambda: set())
        self._occupants = occupants or (lambda: {})
        self.sizes = StoreSizes()

    async def total_size(self) -> int:
        """Bytes the checkpoint stores hold, from the cache where the cache still stands."""
        return await self._size_of(await self.stores())

    async def stores(self, keep_last: int = 0) -> list[_Store]:
        """Every store that holds checkpoints, with the rows of all the sessions that share it."""
        rows = await self.db.fetchall(SESSION_WORKSPACES_SQL + " WHERE s.id IN (SELECT session_id FROM checkpoints)")
        workspaces = {row["id"]: session_workspace(row, self.workspaces_dir) for row in rows}
        stores: dict[Path, _Store] = {}
        for session_id, workspace in workspaces.items():
            git_dir = store_dir(self.checkpoints_dir, workspace)
            store = stores.setdefault(git_dir, _Store(git_dir=git_dir, workspace=workspace))
            store.sessions.add(session_id)
        if not stores:
            return []
        checkpoints = await self.db.fetchall("SELECT id, session_id, seq, kind, sha, at FROM checkpoints ORDER BY id")
        by_session: dict[str, list[Any]] = {}
        for row in checkpoints:
            by_session.setdefault(row["session_id"], []).append(row)
        for store in stores.values():
            for session_id in store.sessions:
                session_rows = by_session.get(session_id, [])
                store.rows.extend(session_rows)
                # The promise is per session, not per store: a leader that snapshots every turn
                # must not push its subagent's last fifty out of the chain they share.
                store.protected.update(row["id"] for row in session_rows[-keep_last:] if keep_last)
            store.rows.sort(key=lambda row: int(row["id"]))
        return list(stores.values())

    async def run(self, bounds: RetentionBounds) -> RetentionReport:
        """Bring the stores inside ``bounds`` and report what that freed."""
        await asyncio.to_thread(self.remove_dead_stores)
        found = await self.stores(bounds.keep_last)
        stores = [store for store in found if self._usable(store)]
        report = RetentionReport(stores=len(found), skipped=len(found) - len(stores))
        report.size_before = report.size_after = await self._size_of(found)
        if not stores:
            return report
        cutoff = datetime.now(UTC) - timedelta(days=bounds.keep_days)
        for store in stores:
            await self._cut(store, store.droppable(len(store.rows), cutoff), report)
        # The bound is on every store together, including the ones this pass may not touch: a
        # session working through a big one is a reason to leave it alone, not to stop counting it.
        limit = bounds.total_max_gb * 1e9
        if limit and await self._size_of(found) > limit:
            self._name_the_biggest_untouchable(found, stores)
        while limit and await self._size_of(found) > limit:
            store = self._fullest_with_something_to_drop(stores)
            if store is None:
                break
            await self._cut(store, store.droppable(SIZE_CUT_BATCH, None), report)
        report.size_after = await self._size_of(found)
        return report

    def remove_dead_stores(self) -> list[Path]:
        """Delete the stores whose workspace is gone for good; returns what was removed.

        While the store sat inside its workspace, deleting the workspace deleted the undo with it.
        Out of it, every path that removes a workspace (a deleted chat, a finished schedule, the
        orphan sweep) would leave its store behind for ever, so the stores are swept here instead of
        at each of them. Only a workspace under the managed workspaces folder is known to be gone:
        a project's folder that is missing may be a disk not mounted yet, and its undo stays.
        """
        removed: list[Path] = []
        if not self.checkpoints_dir.is_dir():
            return removed
        managed = self.workspaces_dir.resolve()
        for git_dir in sorted(self.checkpoints_dir.iterdir()):
            try:
                named = Path((git_dir / WORKSPACE_FILE).read_text(encoding="utf-8").strip())
            except OSError:
                continue
            if named.is_relative_to(managed) and not named.exists() and not self._occupants().get(git_dir):
                shutil.rmtree(git_dir, ignore_errors=True)
                self.sizes.forget(git_dir)
                removed.append(git_dir)
        if removed:
            logger.info("removed %d checkpoint store(s) whose workspace was deleted", len(removed))
        return removed

    def _usable(self, store: _Store) -> bool:
        """A store a pass may touch: nobody is working in it and this process can actually see it.

        A workspace that is not there is not an empty workspace — a project's folder is unreachable
        until it is mounted — so its rows stay and its undo with them.

        Who is working in it is asked of the process, not only of the ``checkpoints`` rows: a session
        that has not taken its first snapshot yet has no rows and is about to write into the chain
        anyway. And it is asked again before every cut, because a cut is a ``git gc --prune=now``
        that takes minutes, and a session can start running in any of them.
        """
        if not (store.git_dir / "HEAD").exists():
            return False
        return not (self._sessions_in(store) & self._busy())

    def _sessions_in(self, store: _Store) -> set[str]:
        """Every session that shares this store: the ones with rows, and the ones this process holds open."""
        return store.sessions | self._occupants().get(store.git_dir, set())

    async def _size_of(self, stores: Iterable[_Store]) -> int:
        total = 0
        for store in stores:
            total += await asyncio.to_thread(self.sizes.size, store.git_dir)
        return total

    def _name_the_biggest_untouchable(self, found: Iterable[_Store], stores: Iterable[_Store]) -> None:
        """Say which store the size bound could not reach, when the pass is about to take it out of the others.

        The bound is a total over every store, and a store a session is working in is left alone —
        so one big busy store is paid for by every other session's oldest undos. That is deliberate,
        and worth a line naming the store, because from the outside it looks like retention running
        far harder than the numbers say it should.
        """
        touchable = {store.git_dir for store in stores}
        skipped = [store for store in found if store.git_dir not in touchable]
        if not skipped:
            return
        biggest = max(skipped, key=lambda store: self.sizes.size(store.git_dir))
        logger.warning(
            "checkpoint store %s (%.2f GB) is in use and cannot be cut; the size bound is being met from the other stores",
            biggest.git_dir,
            self.sizes.size(biggest.git_dir) / 1e9,
        )

    def _fullest_with_something_to_drop(self, stores: Iterable[_Store]) -> _Store | None:
        """The biggest store that still has an unprotected snapshot: the size bound is a total, and
        taking from the biggest store first is what reaches it while dropping the fewest undos."""
        candidates = [store for store in stores if not store.blocked and store.droppable(SIZE_CUT_BATCH, None)]
        if not candidates:
            return None
        # Every store was measured by the check that got the loop here, so this reads the cache.
        return max(candidates, key=lambda store: self.sizes.size(store.git_dir))

    async def _cut(self, store: _Store, count: int, report: RetentionReport) -> None:
        """Drop the ``count`` oldest snapshots of one store: the chain first, then the rows."""
        if count <= 0:
            return
        if not self._usable(store):
            # Decided again here rather than once for the pass: every cut before this one ran a
            # ``git gc --prune=now``, which drops the grace period on loose objects — the very
            # objects a session that started meanwhile is writing.
            store.blocked = True
            report.skipped += 1
            return
        doomed, kept = store.rows[:count], store.rows[count:]
        if not kept:
            # keep_last protects the newest of every session, so this is a store whose whole chain
            # was selected — cutting it would leave the session with no snapshot at all.
            store.blocked = True
            return
        boundary = kept[0]
        try:
            cut = await Checkpoints(store.workspace, self.checkpoints_dir).truncate(boundary["sha"])
        except (CheckpointError, OSError) as exc:
            logger.warning("checkpoint store %s could not be cut: %s", store.git_dir, exc)
            cut = False
        if not cut:
            store.blocked = True
            report.skipped += 1
            return
        self.sizes.forget(store.git_dir)
        await self._forget_rows([int(row["id"]) for row in doomed])
        await self._record(store, doomed, boundary)
        store.rows = kept
        report.dropped += len(doomed)
        report.cut += 1

    async def _forget_rows(self, ids: list[int]) -> None:
        for start in range(0, len(ids), DELETE_CHUNK):
            chunk = ids[start : start + DELETE_CHUNK]
            await self.db.execute(f"DELETE FROM checkpoints WHERE id IN ({','.join('?' * len(chunk))})", chunk)

    async def _record(self, store: _Store, doomed: list[Any], boundary: Any) -> None:
        """Remember, per session, that snapshots before this one are gone — the app says so rather
        than showing an undo that would silently restore nothing."""
        now = datetime.now(UTC).isoformat()
        lost: dict[str, int] = {}
        for row in doomed:
            lost[row["session_id"]] = lost.get(row["session_id"], 0) + 1
        for session_id, count in lost.items():
            await self.db.execute(
                "INSERT INTO checkpoint_retention(session_id, removed_before, removed, at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET removed_before = excluded.removed_before, "
                "removed = checkpoint_retention.removed + excluded.removed, at = excluded.at",
                (session_id, boundary["at"], count, now),
            )


def _at(row: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(row["at"]))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _marker(git_dir: Path) -> tuple[int, int]:
    def mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    return (mtime(git_dir), mtime(git_dir / "index"))


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


__all__ = ["CheckpointRetention", "RetentionBounds", "RetentionReport", "StoreSizes"]
