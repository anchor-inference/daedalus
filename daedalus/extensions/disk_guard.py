"""The disk guard: who is filling the disk, told once per threshold, and stale throwaway swept.

One session once spent two weeks copying a whole repository for every experiment it ran and never
deleted a copy; its home under the workspaces root reached 209 GB, the host disk filled, and nothing
in the installation had looked. The guard does three things on a timer:

1. It measures every top-level directory under the workspaces root — a session's home or a managed
   project folder — and when one passes ``disk.workspace_soft_limit_gb`` (and again at twice that) it
   asks the owning session's agent to clean up, the way a finished background job wakes it
   (:mod:`daedalus.extensions.jobs`, origin ``disk``), and posts a notice for the operator. The level
   already told is kept in the ``kv`` table, so a restart does not tell it again, and a workspace has
   to shrink well below a threshold (``REARM_SHARE``) before crossing it counts as new.
2. It watches the free space of the volume the workspaces live on and tells the operator when it
   drops below ``disk.min_free_gb`` or ``disk.min_free_percent``, whoever filled it.
3. It deletes directories named like throwaway (``disk.throwaway_patterns``) that nothing inside has
   changed in for ``disk.cleanup_after_days``, never one a checkout tracks and never outside the
   managed root, and leaves the agent a short note of what went.

Measuring is the expensive part, so it is rationed: a workspace is walked again on the short cadence
only while it is big or its owner has run since the last walk; an idle, small one waits
``disk.idle_recheck_hours``. Every walk runs in a thread and is bounded by ``disk.walk_max_entries``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.config import DiskConfig
from daedalus.extensions.notifications import Draft
from daedalus.host import disk_usage
from daedalus.host.disk_usage import Measurement, Refused
from daedalus.host.notify_text import render

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

STATE_KEY = "disk_guard"
TICK_SECONDS = 60.0
"""How often the cheap parts run: the free-space reading and handing over notes that wait for a turn."""
FIRST_PASS_DELAY_SECONDS = 120.0
"""The first measurement waits this long after start, so it does not compete with the start itself."""
REARM_SHARE = 0.8
"""A workspace told about a threshold is told about it again only after shrinking below this share of
it: a workspace hovering at the limit would otherwise warn on every pass."""
FREE_REARM_FACTOR = 1.25
"""The same for free space: told once below the floor, again only after rising past this times it."""
LISTED_TOP = 6
LISTED_NESTED = 3
NOTES_KEPT = 5
"""Notes of removed throwaway kept per session until it next runs; the oldest go first."""
DEPTH = 2
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


@dataclass(slots=True)
class Owner:
    """Whose a workspace is: a session's home, or a project's managed folder, or nobody's."""

    kind: str
    id: str = ""
    title: str = ""
    session_id: str = ""
    """The session to ask to clean up: the owner itself, or a project's most recently active session."""
    project_id: str = ""
    active: float = 0.0
    """When anyone working in it last ran, as a timestamp."""

    def view(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "title": self.title}


NOBODY = Owner("none")


def level_for(size: int, limit: int) -> int:
    """0 under the soft limit, 1 past it, 2 past twice it; always 0 with no limit."""
    if limit <= 0:
        return 0
    return 2 if size >= 2 * limit else 1 if size >= limit else 0


def rearmed(told: int, size: int, limit: int) -> int:
    """The level still counted as told after the workspace shrank: a level is forgotten once the
    size is below :data:`REARM_SHARE` of its threshold."""
    while told > 0 and (limit <= 0 or size < REARM_SHARE * told * limit):
        told -= 1
    return told


def _timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def largest_lines(measurement: Measurement, now: float) -> list[str]:
    """The biggest folders of a workspace as lines: the top ones, and inside the two biggest, theirs."""
    lines: list[str] = []
    for index, (path, slot) in enumerate(measurement.children("")[:LISTED_TOP]):
        lines.append(f"- {path}/  {disk_usage.human(slot.bytes)} ({slot.files} files, last change {_ago(now - slot.newest)})")
        if index < 2:
            for inner, child in measurement.children(path)[:LISTED_NESTED]:
                lines.append(f"  - {inner}/  {disk_usage.human(child.bytes)}")
    return lines


class DiskGuard:
    """The measurements, the told levels and the timer; the routes read and refresh the same cache."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self.sizes: dict[str, Measurement] = {}
        self.swept: dict[str, float] = {}
        self.free: tuple[int, int] = (0, 0)
        self.checked_at: float | None = None
        self.state: dict[str, Any] = {"levels": {}, "low": False, "notes": {}}
        self._loaded = False
        self._measuring = asyncio.Lock()

    @property
    def config(self) -> DiskConfig:
        return self.app.config.disk

    @property
    def root(self) -> Path:
        return Path(os.path.normpath(self.app.settings.workspaces_dir.expanduser()))

    @property
    def limit(self) -> int:
        return int(self.config.workspace_soft_limit_gb * 1e9)

    def floor(self, total: int) -> int:
        """The free space below which the volume counts as nearly full."""
        return int(max(self.config.min_free_gb * 1e9, total * self.config.min_free_percent / 100))

    # -- state ------------------------------------------------------------------------------------

    async def load(self) -> None:
        if self._loaded:
            return
        stored = await self.app.db.kv_get(STATE_KEY, None)
        if isinstance(stored, dict):
            self.state = {"levels": dict(stored.get("levels") or {}), "low": bool(stored.get("low")), "notes": dict(stored.get("notes") or {})}
        self._loaded = True

    async def save(self) -> None:
        await self.app.db.kv_set(STATE_KEY, self.state)

    # -- who owns what ----------------------------------------------------------------------------

    def workspaces(self) -> list[str]:
        """The top-level directories of the root: real directories only, never a link to elsewhere."""
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return []
        return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))

    async def owners(self) -> dict[str, Owner]:
        """Each top-level directory's owner, from the sessions and the project folders.

        A session's own directory wins over a project's folder of the same name: an ephemeral
        project's folder is named after the chat that made it, and the chat is who to ask.
        """
        from daedalus.host.session_runner import home_of, host_home_name  # Lazy: the runner imports the world.

        db = self.app.db
        sessions = await db.fetchall("SELECT id, project_id, title, metadata, last_message_at FROM sessions")
        folders = await db.fetchall("SELECT f.project_id AS project_id, f.path AS path, p.name AS name FROM project_folders f JOIN projects p ON p.id = f.project_id")
        busy = self.app.manager.busy_sessions() if self.app.manager is not None else set()
        now = time.time()
        latest: dict[str, tuple[float, str]] = {}
        out: dict[str, Owner] = {}
        for row in folders:
            path = Path(os.path.normpath(str(row["path"])))
            if path.parent == self.root:
                out[path.name] = Owner("project", str(row["project_id"]), str(row["name"]), project_id=str(row["project_id"]))
        direct: dict[str, Owner] = {}
        for row in sessions:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            sid = str(row["id"])
            active = now if sid in busy else _timestamp(row["last_message_at"])
            project_id = str(row["project_id"] or "")
            if project_id and not metadata.get("subagent_of") and active >= latest.get(project_id, (-1.0, ""))[0]:
                latest[project_id] = (active, sid)
            names = {sid, host_home_name(sid)}
            if home := home_of(metadata):
                names.add(home)
            if workspace := str(metadata.get("workspace") or ""):
                path = Path(os.path.normpath(workspace))
                if path.parent == self.root:
                    names.add(path.name)
            for name in names:
                current = direct.get(name)
                if current is None or active > current.active:
                    direct[name] = Owner("session", sid, str(row["title"] or sid), session_id=sid, project_id=project_id, active=active)
        for owner in out.values():
            when, sid = latest.get(owner.project_id, (0.0, ""))
            owner.session_id, owner.active = sid, when
        out.update(direct)
        return out

    # -- measuring --------------------------------------------------------------------------------

    def due(self, name: str, owner: Owner, now: float) -> bool:
        previous = self.sizes.get(name)
        if previous is None:
            return True
        age = now - previous.measured_at
        if age >= self.config.idle_recheck_hours * 3600:
            return True
        if age < self.config.check_minutes * 60 - 5:
            return False
        return bool(self.limit and previous.bytes >= self.limit / 2) or owner.active > previous.measured_at

    async def measure(self, name: str) -> Measurement:
        measurement = await asyncio.to_thread(disk_usage.measure, self.root / name, max_entries=self.config.walk_max_entries, depth=DEPTH)
        self.sizes[name] = measurement
        return measurement

    async def check(self, now: float | None = None) -> None:
        """One full pass: free space, sizes and warnings, then the throwaway sweep."""
        async with self._measuring:
            await self.load()
            now = time.time() if now is None else now
            await self.check_free()
            owners = await self.owners()
            names = self.workspaces()
            for gone in set(self.sizes) - set(names):
                self.sizes.pop(gone, None)
            levels: dict[str, int] = self.state["levels"]
            for gone in set(levels) - set(names):
                levels.pop(gone, None)
            for name in names:
                owner = owners.get(name, NOBODY)
                try:
                    if self.due(name, owner, now):
                        await self.measure(name)
                        await self.judge(name, owner, now)
                    if self.config.cleanup_after_days and now - self.swept.get(name, 0.0) >= self.config.idle_recheck_hours * 3600:
                        self.swept[name] = now
                        await self.sweep(name, owner, now)
                except Exception:  # noqa: BLE001 — one unreadable workspace must not cost the rest their check
                    logger.exception("the disk guard could not check the workspace %s", name)
            self.checked_at = now
            await self.save()

    async def judge(self, name: str, owner: Owner, now: float) -> None:
        """Tell the owner and the operator when the workspace crossed a threshold not yet told."""
        measurement = self.sizes[name]
        levels: dict[str, int] = self.state["levels"]
        told = rearmed(int(levels.get(name, 0)), measurement.bytes, self.limit)
        level = level_for(measurement.bytes, self.limit)
        if level > told:
            # Marked before telling, which yields: a route measuring meanwhile must not tell it twice.
            levels[name] = level
            await self.save()
            await self.warn(name, owner, measurement, level, now)
        else:
            levels[name] = told
        if not levels.get(name):
            levels.pop(name, None)

    async def warn(self, name: str, owner: Owner, measurement: Measurement, level: int, now: float) -> None:
        lines = largest_lines(measurement, now)
        size = ("at least " if measurement.truncated else "") + disk_usage.human(measurement.bytes)
        limit = disk_usage.human(self.limit)
        manager = self.app.manager
        if owner.session_id and manager is not None:
            twice = " — twice the limit" if level >= 2 else ""
            text = (
                f"[disk] Your workspace {self.root / name} takes {size} on disk, past the soft limit of {limit}{twice}. "
                "The largest folders:\n" + "\n".join(lines) + "\n\n"
                "Delete the throwaway copies you no longer need now: old experiment trees, per-case repository copies, "
                "private package caches. Keep only what the current task still uses. From now on put experiments under "
                "one scratch directory and delete it as soon as the check is done; use git worktree, hard links or small "
                "fixtures instead of copying a whole repository, and the shared package cache instead of a private one. "
                "Say briefly what you removed."
            )
            await self.tell(owner.session_id, text, steer=True)
        language = self.app.notifications.language() if self.app.notifications is not None else ""
        body = render("disk.quota.body", language, name=name, limit=limit, twice=render("disk.quota.twice", language) if level >= 2 else "", largest="\n".join(lines))
        if not owner.session_id:
            body += "\n\n" + render("disk.quota.unowned", language)
        await self.post(Draft(
            "system",
            render("disk.quota", language, owner=owner.title or name, size=size),
            body,
            kind="disk_quota",
            tone="warning",
            session_id=owner.session_id if owner.kind == "session" else None,
            project_id=owner.project_id or None,
            link=f"/app/project/{owner.project_id}" if owner.kind == "project" and owner.project_id else "",
            dedupe_key=f"disk-quota:{name}:{level}",
            source="disk",
        ))

    async def check_free(self) -> None:
        """Read the free space and tell the operator once when it fell below the floor."""
        free, total = await asyncio.to_thread(disk_usage.free_space, self.root)
        self.free = (free, total)
        if not total:
            return
        floor = self.floor(total)
        if self.state.get("low"):
            if free >= floor * FREE_REARM_FACTOR:
                self.state["low"] = False
                await self.save()
            return
        if free >= floor:
            return
        self.state["low"] = True
        await self.save()
        language = self.app.notifications.language() if self.app.notifications is not None else ""
        biggest = sorted(self.sizes.items(), key=lambda item: item[1].bytes, reverse=True)[:LISTED_TOP]
        largest = "\n".join(f"- {name}  {disk_usage.human(m.bytes)}" for name, m in biggest) or "-"
        await self.post(Draft(
            "system",
            render("disk.low", language, free=disk_usage.human(free)),
            render("disk.low.body", language, free=disk_usage.human(free), total=disk_usage.human(total), floor=disk_usage.human(floor), largest=largest),
            kind="disk_low",
            level="urgent",
            tone="error",
            dedupe_key="disk-low",
            source="disk",
        ))

    # -- the throwaway sweep ----------------------------------------------------------------------

    async def sweep(self, name: str, owner: Owner, now: float) -> list[tuple[str, int]]:
        """Delete the stale throwaway directories of one workspace; returns what went and its size."""
        workspace = self.root / name
        if not disk_usage.inside_root(workspace, self.root):
            return []
        config = self.config
        cutoff = now - config.cleanup_after_days * 86400
        candidates = await asyncio.to_thread(disk_usage.find_throwaway, workspace, list(config.throwaway_patterns))
        removed: list[tuple[str, int]] = []
        for path in candidates:
            is_stale, _size = await asyncio.to_thread(disk_usage.stale, path, older_than=cutoff, max_entries=config.walk_max_entries)
            if not is_stale or await asyncio.to_thread(disk_usage.git_tracked, path, workspace):
                continue
            relative = path.relative_to(workspace).as_posix()
            try:
                target = disk_usage.resolve_target(workspace, relative)
            except Refused:
                continue
            freed = await asyncio.to_thread(disk_usage.remove, target, max_entries=config.walk_max_entries)
            logger.info("disk guard removed the stale throwaway %s/%s (%s)", name, relative, disk_usage.human(freed))
            removed.append((relative, freed))
        if not removed:
            return removed
        self.sizes.pop(name, None)  # measured again next pass, with what is left
        total = sum(freed for _, freed in removed)
        lines = [f"- {relative}/  {disk_usage.human(freed)}" for relative, freed in removed]
        if owner.session_id:
            note = (
                f"[disk] Throwaway folders in your workspace untouched for {config.cleanup_after_days} days were removed, "
                f"{disk_usage.human(total)} freed:\n" + "\n".join(lines)
            )
            notes: dict[str, list[str]] = self.state["notes"]
            notes[owner.session_id] = [*notes.get(owner.session_id, []), note][-NOTES_KEPT:]
            await self.save()
        language = self.app.notifications.language() if self.app.notifications is not None else ""
        await self.post(Draft(
            "system",
            render("disk.cleaned", language, size=disk_usage.human(total)),
            render("disk.cleaned.body", language, lines=f"{owner.title or name}:\n" + "\n".join(lines)),
            kind="disk_cleaned",
            level="quiet",
            tone="ok",
            session_id=owner.session_id if owner.kind == "session" else None,
            project_id=owner.project_id or None,
            source="disk",
        ))
        return removed

    async def deliver_notes(self) -> None:
        """Hand each waiting sweep note to its session while a turn of it runs.

        A note is information, not work: delivered to an idle session it would start a turn — a
        model call — only to be acknowledged. So it waits for the session's next turn and rides in
        as a steer then.
        """
        notes: dict[str, list[str]] = self.state["notes"]
        manager = self.app.manager
        if not notes or manager is None or manager.shutting_down or manager.recovering:
            return
        busy = manager.busy_sessions()
        for session_id in [sid for sid in notes if sid in busy]:
            text = "\n\n".join(notes.pop(session_id))
            await self.save()
            await self.tell(session_id, text, steer=True)

    # -- talking ----------------------------------------------------------------------------------

    async def tell(self, session_id: str, text: str, *, steer: bool) -> None:
        manager = self.app.manager
        if manager is None or manager.shutting_down:
            return
        try:
            await manager.submit(session_id, text, steer=steer, as_answer=False, origin="disk")
        except KeyError:
            logger.info("the disk guard had a note for session %s, which is gone", session_id)
        except Exception:  # noqa: BLE001 — the operator's notice still goes; the agent hears next time
            logger.exception("the disk guard could not reach session %s", session_id)

    async def post(self, draft: Draft) -> None:
        notifications = self.app.notifications
        if notifications is None:
            return
        try:
            await notifications.post(draft)
        except Exception:  # noqa: BLE001 — a notice that failed is logged; the next crossing tries again
            logger.exception("the disk guard could not post its notice")

    # -- for the routes ---------------------------------------------------------------------------

    def target_workspace(self, name: str) -> Path | None:
        """The workspace a route names, or None when the name is not a real top-level directory."""
        if not NAME_RE.fullmatch(name) or name in (".", ".."):
            return None
        workspace = self.root / name
        return workspace if disk_usage.inside_root(workspace, self.root) else None

    async def clean(self, name: str, paths: list[str], *, include_tracked: bool) -> dict[str, Any]:
        """Remove the chosen paths of one workspace; what went, what was refused and why."""
        workspace = self.target_workspace(name)
        if workspace is None:
            raise KeyError(name)
        removed: list[str] = []
        refused: list[dict[str, str]] = []
        freed = 0
        for relative in dict.fromkeys(paths):
            try:
                target = disk_usage.resolve_target(workspace, relative)
                if not include_tracked and await asyncio.to_thread(disk_usage.git_tracked, target, workspace):
                    raise Refused("tracked")
            except Refused as refusal:
                refused.append({"path": relative, "reason": refusal.reason})
                continue
            freed += await asyncio.to_thread(disk_usage.remove, target, max_entries=self.config.walk_max_entries)
            removed.append(relative)
        logger.info("operator cleaned up %d path(s) in workspace %s, %s freed", len(removed), name, disk_usage.human(freed))
        measurement = await self.measure(name)
        return {"freed_bytes": freed, "removed": removed, "refused": refused, "bytes": measurement.bytes}

    async def run(self) -> None:
        await asyncio.sleep(FIRST_PASS_DELAY_SECONDS)
        last_pass = 0.0
        while True:
            try:
                await self.load()
                if time.monotonic() - last_pass >= self.config.check_minutes * 60 or not last_pass:
                    last_pass = time.monotonic()
                    await self.check()
                else:
                    await self.check_free()
                await self.deliver_notes()
            except Exception:  # noqa: BLE001 — one bad round must not end the watching
                logger.exception("the disk guard failed a round")
            await asyncio.sleep(TICK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    guard = DiskGuard(app)
    app.extensions["disk_guard"] = guard
    return [asyncio.create_task(guard.run(), name="disk-guard")]
