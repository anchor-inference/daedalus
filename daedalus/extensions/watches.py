"""Watches: "when X happens in the project, do Y", for the orchestrator and the operator.

``app.extensions["watches"]`` keeps every enabled watch of the installation in memory and fires them
from three sources, none of which polls per watch:

* **The event bus.** One subscription (resumed from a cursor after a restart) matches staff turns,
  questions, permissions and crashes, task moves, and the webhooks the inbound route publishes as
  ``webhook.received`` — pull requests, CI conclusions, or any provider with a bounded regex.
* **One ticker** every ``watches.tick_seconds``: a working member whose last signal is older than the
  watch's minutes (once per silence), and the branch heads of the folders a ``git_commit`` watch
  names, read with one ``git for-each-ref`` per folder every ``watches.git_poll_seconds``.
* **The terminal daemon.** A ``terminal_output`` watch holds one ``terminal.wait_for`` on its
  terminal's output; the daemon matches with Go's RE2, which runs in linear time whatever the
  pattern. It is re-armed after each match once the cooldown is over, and the ticker re-arms a watch
  whose terminal was not there (a member between sessions).

What bounds them, and why each bound exists:

* a cooldown of at least ``watches.min_cooldown_seconds`` between two fires of a watch;
* ``watches.max_fires_per_hour``, past which the watch switches itself off and the journal says so —
  that is what stops a ``tell`` whose answer triggers the same watch from going round for ever;
* at most ``watches.max_per_project`` enabled watches in a project;
* a pattern of at most ``watches.regex_max_chars``, refused when it nests a quantifier or uses what
  RE2 lacks; a webhook pattern runs in Python, so it is searched in a throwaway process with a time
  limit over a summary that is itself capped;
* the orchestrator's own doing never fires a watch: an event it caused (``actor: orchestrator``) is
  dropped, and a terminal match that is only the echo of a message it just typed into a member's
  terminal is skipped.

Actions: ``wake`` publishes ``watch.fired`` (the orchestrator's wake queue delivers it at once),
``tell`` sends a member a message through the team, ``notify`` posts a notification directly (the
model's own ``Notify`` budget is for its own calls, not for a watch the operator agreed to).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from daedalus.extensions.inbound import _NESTED_QUANTIFIER, search_bounded
from daedalus.extensions.watch_delivery import WatchDeliveries
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.gitrun import GitError, run_command
from daedalus.stores.staff import MESSAGE_MODES
from daedalus.terminals.bridge import HostBridge
from daedalus.terminals.model import EnvUnavailable, InvalidRequest, TerminalError

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionManager
    from daedalus.stores.projects import Project, ProjectFolder
    from daedalus.stores.staff import Staff
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

STAFF_EVENTS = ("staff_finished", "staff_question", "staff_permission", "staff_crashed", "staff_silent")
WHEN_EVENTS = (*STAFF_EVENTS, "task_moved", "terminal_output", "git_commit", "pr", "ci", "webhook")
ACTIONS = ("wake", "tell", "notify")
NOTIFY_LEVELS = ("quiet", "normal", "urgent")
TASK_STATUSES = ("todo", "doing", "review", "done", "blocked", "dropped")
BUS_TYPES = ("staff.status", "ask.pending", "permission.pending", "task.moved", "webhook.received")
CI_EVENTS = ("check_suite", "workflow_run", "status")
"""GitHub's events that carry a CI conclusion. ``check_run`` is left out on purpose: one CI run has a
check run per job, and a watch on "CI failed" would hear the same failure once per job."""
CURSOR_KEY = "watches_cursor"
NOTE_MAX = 300
TEXT_MAX = 2000
TITLE_MAX = 120
REF_MAX = 200
COOLDOWN_MAX_MINUTES = 7 * 24 * 60
SILENT_MINUTES = (5, 24 * 60)
WAIT_SECONDS = 25 * 60.0
"""One wait on a terminal: under the daemon's thirty-minute ceiling, re-issued from where it stopped."""
ECHO_SECONDS = 60.0
"""How long after the orchestrator typed into a member's terminal a match inside that text is its echo."""
BACKOFF_SECONDS = 60.0
WEBHOOK_TEXT_MAX = 2000
GIT_TIMEOUT_SECONDS = 20.0
_RE2_MISSING = re.compile(r"\(\?<?[=!]|\\[1-9]|\(\?P=|\(\?[aiLmsux]*-?[aiLmsux]*\)")
"""Lookarounds, back-references and inline flag groups: Python has them, the daemon's RE2 does not."""
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_GIT_SAFE = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "--no-pager")
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


class WatchRefused(ValueError):
    """A watch that will not be set or changed, said so the one who asked can set a different one."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(at: str | None) -> datetime | None:
    if not at:
        return None
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass
class Watch:
    id: str
    project_id: str
    pattern: dict[str, Any]
    action: dict[str, Any]
    cooldown_s: int
    once: bool
    note: str
    created_by: str
    created_at: str
    last_fired_at: str | None
    fire_count: int
    enabled: bool
    state: dict[str, Any] = field(default_factory=dict)
    condition_revision: int = 1
    deadline_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Watch:
        def load(text: str | None) -> dict[str, Any]:
            try:
                value = json.loads(text or "{}")
            except ValueError:
                value = {}
            return value if isinstance(value, dict) else {}

        return cls(
            id=str(row["id"]), project_id=str(row["project_id"]), pattern=load(row["pattern_json"]), action=load(row["action_json"]),
            cooldown_s=int(row["cooldown_s"]), once=bool(row["once"]), note=str(row["note"] or ""), created_by=str(row["created_by"]),
            created_at=str(row["created_at"]), last_fired_at=row["last_fired_at"], fire_count=int(row["fire_count"] or 0),
            enabled=bool(row["enabled"]), state=load(row["state_json"]),
            condition_revision=int(row["condition_revision"]) if "condition_revision" in row.keys() else 1,
            deadline_at=row["deadline_at"],
        )

    @property
    def event(self) -> str:
        return str(self.pattern.get("event") or "")

    def view(self) -> dict[str, Any]:
        """The watch as the app and the tools show it. ``state`` is the evaluator's own bookkeeping
        (branch heads, fire times) and stays out, apart from why it stopped and its last error."""
        return {
            "id": self.id, "project_id": self.project_id, "when": self.pattern, "then": self.action,
            "cooldown_minutes": max(1, round(self.cooldown_s / 60)), "once": self.once, "note": self.note,
            "created_by": self.created_by, "created_at": self.created_at, "last_fired_at": self.last_fired_at,
            "fire_count": self.fire_count, "enabled": self.enabled, "stopped": str(self.state.get("stopped") or ""),
            "condition_revision": self.condition_revision,
            "deadline_at": self.deadline_at,
            # ``stopped`` is a code: once · budget · pattern · slow_pattern.
            "last_error": str(self.state.get("last_error") or ""), "describe": describe(self),
        }


def describe(watch: Watch) -> str:
    """One line in English for the orchestrator: what it waits for and what it then does."""
    p, a = watch.pattern, watch.action
    who = str(p.get("staff") or "anyone on the team")
    kind = watch.event
    when = {
        "staff_finished": f"{who} finishes a turn",
        "staff_question": f"{who} asks a question",
        "staff_permission": f"{who} needs a permission",
        "staff_crashed": f"{who} stops with an error or their session ends by itself",
        "staff_silent": f"{who} has been silent {p.get('minutes')} min while working",
        "task_moved": f"task {p.get('task') or 'any task'} moves" + (f" to {p['to']}" if p.get("to") else ""),
        "terminal_output": f"{p.get('terminal_title') or p.get('staff') or p.get('terminal')}'s terminal prints /{p.get('regex')}/",
        "git_commit": f"a commit lands on {p.get('branch') or 'any branch'} in {p.get('folder_label') or p.get('folder')}",
        "pr": f"a {p.get('provider')} pull request" + (f" in {p['repo']}" if p.get("repo") else "") + (f" is {p['conclusion']}" if p.get("conclusion") else " changes"),
        "ci": f"{p.get('provider')} CI" + (f" in {p['repo']}" if p.get("repo") else "") + (f" ends {p['conclusion']}" if p.get("conclusion") else " finishes"),
        "webhook": f"a {p.get('provider')} webhook arrives" + (f" matching /{p['regex']}/" if p.get("regex") else ""),
    }.get(kind, kind)
    then = {"wake": "wake the orchestrator", "tell": f"tell {a.get('staff')}: \"{_one_line(a.get('text'), 60)}\"", "notify": f"notify the operator: \"{_one_line(a.get('title'), 60)}\""}.get(str(a.get("action")), str(a.get("action")))
    return f"when {when} → {then}"


def check_regex(pattern: Any, limit: int) -> str:
    """A pattern both engines accept, bounded: refused when too long, when it does not compile, when it
    nests a quantifier (``(a+)+``) or uses what RE2 lacks (lookarounds, back-references)."""
    text = str(pattern or "")
    if not text:
        raise WatchRefused("the pattern is empty")
    if len(text) > limit:
        raise WatchRefused(f"a pattern is at most {limit} characters")
    try:
        re.compile(text)
    except re.error as exc:
        raise WatchRefused(f"the pattern does not compile: {exc}") from exc
    if _NESTED_QUANTIFIER.search(text):
        raise WatchRefused("the pattern nests a quantifier inside a quantified group (like (a+)+), which can take for ever to match; simplify it")
    if _RE2_MISSING.search(text):
        raise WatchRefused("the pattern uses a lookaround, a back-reference or an inline flag group, which the terminal's matcher does not have")
    return text


def webhook_matches(watch: Watch, payload: dict[str, Any]) -> str | None:
    """Whether a ``webhook.received`` event is what a ``pr``/``ci`` watch waits for (the regex of a
    ``webhook`` watch needs a process of its own, so it is checked by the caller). The detail line, or None."""
    p = watch.pattern
    if str(payload.get("provider") or "") != p.get("provider"):
        return None
    event = str(payload.get("event") or "")
    repo = str(payload.get("repo") or "")
    if p.get("repo") and repo.lower() != str(p["repo"]).lower() and repo.lower().rsplit("/", 1)[-1] != str(p["repo"]).lower():
        return None
    conclusion = str(payload.get("conclusion") or "").lower()
    if watch.event == "pr":
        if event != "pull_request":
            return None
        if p.get("conclusion") and conclusion != str(p["conclusion"]).lower():
            return None
        title = _one_line(payload.get("title"), 120)
        return f"pull request {conclusion or 'changed'} in {repo or p['provider']}" + (f": \"{title}\"" if title else "")
    if watch.event == "ci":
        if event not in CI_EVENTS or not conclusion or conclusion == "pending":
            return None
        if p.get("conclusion") and conclusion != str(p["conclusion"]).lower():
            return None
        branch = str(payload.get("branch") or "")
        return f"CI {conclusion} in {repo or p['provider']}" + (f" on {branch}" if branch else "")
    if watch.event == "webhook":
        return f"{p['provider']} webhook {event or '(no event name)'}"
    return None


def event_matches(watch: Watch, event: AppEvent) -> str | None:
    """Whether a project event is what a staff or task watch waits for; the detail line, or None."""
    p, payload, kind = watch.pattern, event.payload, watch.event
    if payload.get("actor") == "orchestrator":
        return None
    if kind in STAFF_EVENTS and p.get("staff_id") and event.staff_id != p["staff_id"]:
        return None
    who = str(p.get("staff") or "a member")
    if kind == "staff_finished" and event.type == "staff.status" and payload.get("status") == "turn_done_unseen" and event.staff_id:
        return f"{who} finished a turn"
    if kind == "staff_question" and event.type == "ask.pending" and event.staff_id:
        return f"{who} asks a question"
    if kind == "staff_permission" and event.type == "permission.pending" and event.staff_id:
        return f"{who} needs a permission: {_one_line(payload.get('text'), 160)}"
    if kind == "staff_crashed" and event.type == "staff.status" and event.staff_id:
        status = payload.get("status")
        if status == "error" or (status == "exited" and payload.get("actor") not in ("operator", "orchestrator")):
            detail = _one_line(payload.get("detail"), 200)
            return f"{who} {'stopped with an error' if status == 'error' else 'had their session end'}" + (f": {detail}" if detail else "")
        return None
    if kind == "task_moved" and event.type == "task.moved":
        if p.get("task_id") and payload.get("task_id") != p["task_id"]:
            return None
        if p.get("to") and payload.get("to") != p["to"]:
            return None
        return f"\"{_one_line(payload.get('title'), 80)}\" ({payload.get('task_id')}) moved {payload.get('from')} → {payload.get('to')}"
    return None


class Watches:
    """Every project's watches of this installation, and what fires them."""

    def __init__(self, app: Application) -> None:
        self.app = app
        assert app.manager is not None
        self.manager: SessionManager = app.manager
        self.clock: Callable[[], datetime] = _now
        """The wall clock; a test moves it."""
        self.nap: Callable[[float], Awaitable[None]] = asyncio.sleep
        """How a terminal watch waits out its cooldown; a test whose clock jumps naps briefly and looks again."""
        self._watches: dict[str, Watch] = {}
        self._loaded = False
        self._firing = asyncio.Lock()
        self.deliveries = WatchDeliveries(app)
        self._followers: dict[str, asyncio.Task[None]] = {}
        self._handler: asyncio.Task[None] | None = None
        self._seq = 0
        self._polled: dict[str, datetime] = {}
        """When each watched folder's branch heads were last read."""

    @property
    def config(self) -> Any:
        return self.manager.config.watches

    # -- the store ---------------------------------------------------------------------------------

    async def load(self) -> None:
        rows = await self.manager.db.fetchall("SELECT * FROM watches")
        self._watches = {str(r["id"]): Watch.from_row(r) for r in rows}
        self._loaded = True

    async def _reload(self, watch_id: str) -> Watch | None:
        row = await self.manager.db.fetchone("SELECT * FROM watches WHERE id = ?", (watch_id,))
        if row is None:
            self._watches.pop(watch_id, None)
            return None
        watch = Watch.from_row(row)
        self._watches[watch_id] = watch
        return watch

    def of_project(self, project_id: str, *, enabled_only: bool = False) -> list[Watch]:
        found = [w for w in self._watches.values() if w.project_id == project_id and (w.enabled or not enabled_only)]
        return sorted(found, key=lambda w: w.created_at)

    def get(self, project_id: str, watch_id: str) -> Watch | None:
        watch = self._watches.get(watch_id)
        return watch if watch is not None and watch.project_id == project_id else None

    async def _save(self, watch: Watch) -> None:
        await self.manager.db.execute(
            "UPDATE watches SET pattern_json = ?, action_json = ?, cooldown_s = ?, once = ?, note = ?, last_fired_at = ?, fire_count = ?, enabled = ?, state_json = ?, condition_revision = ?, deadline_at = ? WHERE id = ?",
            (json.dumps(watch.pattern), json.dumps(watch.action), watch.cooldown_s, int(watch.once), watch.note, watch.last_fired_at, watch.fire_count, int(watch.enabled), json.dumps(watch.state), watch.condition_revision, watch.deadline_at, watch.id),
        )

    # -- setting one -------------------------------------------------------------------------------

    async def create(self, project: Project, *, when: Any, then: Any, cooldown_minutes: Any = 10,
                     once: bool = False, note: str = "", by: str = "orchestrator",
                     deadline_at: str | None = None) -> Watch:
        if by not in ("orchestrator", "operator"):
            raise WatchRefused("a watch is set by the orchestrator or the operator")
        live = [w for w in self.of_project(project.id, enabled_only=True)]
        if len(live) >= self.config.max_per_project:
            raise WatchRefused(f"{project.name} already has {self.config.max_per_project} watches; remove one first")
        pattern = await self._check_when(project, when)
        action = await self._check_then(project, then)
        cooldown_s = self._check_cooldown(cooldown_minutes)
        text = " ".join(str(note or "").split())
        if len(text) > NOTE_MAX:
            raise WatchRefused(f"a note is at most {NOTE_MAX} characters")
        if deadline_at is not None:
            deadline = _parse(deadline_at)
            if deadline is None or deadline <= self.clock() or deadline > self.clock() + timedelta(days=365):
                raise WatchRefused("watch deadline must be within the next year")
            deadline_at = deadline.isoformat()
        watch_id = "w" + uuid.uuid4().hex[:7]
        now = self.clock().isoformat()
        await self.manager.db.execute(
            "INSERT INTO watches(id, project_id, pattern_json, action_json, cooldown_s, once, note, created_by, created_at, enabled, state_json, deadline_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '{}', ?)",
            (watch_id, project.id, json.dumps(pattern), json.dumps(action), cooldown_s, int(bool(once)), text, by, now, deadline_at),
        )
        watch = await self._reload(watch_id)
        assert watch is not None
        if watch.event == "terminal_output":
            self._follow(watch)
        await self._changed(project.id, by)
        return watch

    async def update(self, project_id: str, watch_id: str, *, enabled: bool | None = None, note: str | None = None, cooldown_minutes: Any = None, by: str = "operator") -> Watch:
        watch = self.get(project_id, watch_id)
        if watch is None:
            raise KeyError(watch_id)
        if note is not None:
            text = " ".join(note.split())
            if len(text) > NOTE_MAX:
                raise WatchRefused(f"a note is at most {NOTE_MAX} characters")
            watch.note = text
        if cooldown_minutes is not None:
            watch.cooldown_s = self._check_cooldown(cooldown_minutes)
        if enabled is not None and enabled != watch.enabled:
            if enabled and len(self.of_project(project_id, enabled_only=True)) >= self.config.max_per_project:
                raise WatchRefused(f"the project already has {self.config.max_per_project} watches switched on")
            watch.enabled = enabled
            if enabled:
                # Switched on again by a person: it starts over, with a fresh hour and no old reason.
                watch.state.pop("stopped", None)
                watch.state.pop("fires", None)
                watch.state.pop("last_error", None)
        watch.condition_revision += 1
        await self._save(watch)
        if watch.event == "terminal_output":
            if watch.enabled:
                self._follow(watch)
            else:
                self._unfollow(watch.id)
        await self._changed(project_id, by)
        return watch

    async def remove(self, project_id: str, watch_id: str, *, by: str = "operator") -> bool:
        if self.get(project_id, watch_id) is None:
            return False
        await self.manager.db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        self._watches.pop(watch_id, None)
        self._unfollow(watch_id)
        await self._changed(project_id, by)
        return True

    def _check_cooldown(self, minutes: Any) -> int:
        try:
            value = float(minutes)
        except (TypeError, ValueError) as exc:
            raise WatchRefused("cooldown_minutes is a number of minutes") from exc
        floor = self.config.min_cooldown_seconds
        if value * 60 < floor or value > COOLDOWN_MAX_MINUTES:
            raise WatchRefused(f"the cooldown is between {max(1, round(floor / 60))} and {COOLDOWN_MAX_MINUTES} minutes")
        return int(value * 60)

    async def _member(self, project: Project, ref: Any) -> Staff:
        name = str(ref or "").strip()
        if not name:
            raise WatchRefused("name the staff member")
        member = await self.manager.staff.find(project.id, name)
        if member is None or not member.active:
            raise WatchRefused(f"{project.name} has nobody called {name!r}; Team() lists the team")
        return member

    async def _check_when(self, project: Project, when: Any) -> dict[str, Any]:
        if not isinstance(when, dict):
            raise WatchRefused("when is an object with an event, e.g. {\"event\": \"staff_finished\", \"staff\": \"Max\"}")
        kind = str(when.get("event") or "")
        if kind not in WHEN_EVENTS:
            raise WatchRefused(f"when.event is one of {', '.join(WHEN_EVENTS)}")
        pattern: dict[str, Any] = {"event": kind}
        if kind in STAFF_EVENTS:
            if when.get("staff") or kind == "staff_silent":
                member = await self._member(project, when.get("staff"))
                pattern.update(staff=member.name, staff_id=member.id)
            if kind == "staff_silent":
                try:
                    minutes = int(when["minutes"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise WatchRefused("staff_silent needs minutes") from exc
                if not SILENT_MINUTES[0] <= minutes <= SILENT_MINUTES[1]:
                    raise WatchRefused(f"minutes is between {SILENT_MINUTES[0]} and {SILENT_MINUTES[1]}")
                pattern["minutes"] = minutes
            return pattern
        if kind == "task_moved":
            if when.get("task"):
                row = await self.manager.db.fetchone("SELECT id, title FROM board_tasks WHERE id = ? AND project_id = ?", (str(when["task"]), project.id))
                if row is None:
                    raise WatchRefused(f"no task {when['task']} on {project.name}'s board")
                pattern.update(task=row["title"], task_id=row["id"])
            if when.get("to"):
                if when["to"] not in TASK_STATUSES:
                    raise WatchRefused(f"to is one of {', '.join(TASK_STATUSES)}")
                pattern["to"] = when["to"]
            return pattern
        if kind == "terminal_output":
            pattern["regex"] = check_regex(when.get("regex"), self.config.regex_max_chars)
            terminals = self.app.extensions.get("terminals")
            if terminals is None:
                raise WatchRefused("terminal watches need the terminals service, which is not running on this installation")
            if when.get("staff"):
                member = await self._member(project, when["staff"])
                if member.harness == "daedalus":
                    raise WatchRefused(f"{member.name} works without a terminal; watch staff_finished or staff_crashed instead")
                pattern.update(staff=member.name, staff_id=member.id)
                return pattern
            ref = str(when.get("terminal") or "").strip()
            if not ref:
                raise WatchRefused("terminal_output needs a terminal (its id or title) or a staff member")
            rows = await self.manager.db.fetchall("SELECT id, title FROM terminals WHERE project_id = ? AND status = 'running'", (project.id,))
            found = [r for r in rows if r["id"] == ref] or [r for r in rows if (r["title"] or "").lower() == ref.lower()]
            if len(found) != 1:
                names = ", ".join(f"{r['title'] or r['id']} ({r['id']})" for r in rows[:10]) or "none"
                raise WatchRefused(f"{'several' if found else 'no'} running terminals of {project.name} match {ref!r}; they are: {names}")
            pattern.update(terminal=found[0]["id"], terminal_title=found[0]["title"] or found[0]["id"])
            return pattern
        if kind == "git_commit":
            folder = self._folder(project, when.get("folder"))
            if not folder.is_git:
                raise WatchRefused(f"{folder.label or folder.path} is not a git repository")
            pattern.update(folder=folder.id, folder_label=folder.label or Path(folder.path).name)
            if when.get("branch"):
                branch = str(when["branch"]).strip()
                if not _BRANCH.match(branch):
                    raise WatchRefused(f"{branch!r} is not a branch name")
                pattern["branch"] = branch
            return pattern
        provider = str(when.get("provider") or "").strip()
        if not provider:
            raise WatchRefused(f"{kind} needs the webhook provider's name, as in Settings")
        if provider not in self.manager.config.webhooks:
            configured = ", ".join(sorted(self.manager.config.webhooks)) or "none yet"
            raise WatchRefused(f"no webhook provider {provider!r} is configured (configured: {configured}); the operator adds one under webhooks in the settings")
        pattern["provider"] = provider
        for key in ("repo", "conclusion"):
            if when.get(key) and kind in ("pr", "ci"):
                value = str(when[key]).strip()
                if len(value) > REF_MAX:
                    raise WatchRefused(f"{key} is at most {REF_MAX} characters")
                pattern[key] = value
        if kind == "webhook" and when.get("regex"):
            pattern["regex"] = check_regex(when["regex"], self.config.regex_max_chars)
        return pattern

    def _folder(self, project: Project, ref: Any) -> ProjectFolder:
        if not ref:
            return project.primary
        text = str(ref)
        for folder in project.folders:
            if text in (folder.id, folder.label, folder.path) or (folder.label and folder.label.lower() == text.lower()):
                return folder
        raise WatchRefused(f"{project.name} has no folder {text!r}; Folders() lists them")

    async def _check_then(self, project: Project, then: Any) -> dict[str, Any]:
        if not isinstance(then, dict):
            raise WatchRefused("then is an object with an action: wake, tell or notify")
        action = str(then.get("action") or "")
        if action not in ACTIONS:
            raise WatchRefused(f"then.action is one of {', '.join(ACTIONS)}")
        if action == "wake":
            if not project.settings.orchestrator.enabled:
                raise WatchRefused(f"{project.name} has no orchestrator to wake; use notify, or switch it on first")
            note = " ".join(str(then.get("note") or "").split())
            return {"action": "wake", **({"note": note[:NOTE_MAX]} if note else {})}
        if action == "tell":
            member = await self._member(project, then.get("staff"))
            text = str(then.get("text") or "").strip()
            if not text or len(text) > TEXT_MAX:
                raise WatchRefused(f"tell needs a text of at most {TEXT_MAX} characters")
            when = str(then.get("when") or "now")
            if when not in MESSAGE_MODES:
                raise WatchRefused(f"then.when is one of {', '.join(MESSAGE_MODES)}")
            return {"action": "tell", "staff": member.name, "staff_id": member.id, "text": text, "when": when}
        title = " ".join(str(then.get("title") or "").split())
        text = str(then.get("text") or "").strip()
        if not title or len(title) > TITLE_MAX:
            raise WatchRefused(f"notify needs a title of at most {TITLE_MAX} characters")
        if len(text) > 1000:
            raise WatchRefused("the text of a notification is at most 1000 characters")
        level = str(then.get("level") or "normal")
        if level not in NOTIFY_LEVELS:
            raise WatchRefused(f"level is one of {', '.join(NOTIFY_LEVELS)}")
        return {"action": "notify", "title": title, "text": text, "level": level}

    # -- firing ------------------------------------------------------------------------------------

    def cooling(self, watch: Watch) -> float:
        """Seconds left of the watch's cooldown; 0 when it may fire."""
        last = _parse(watch.last_fired_at)
        if last is None:
            return 0.0
        return max(0.0, watch.cooldown_s - (self.clock() - last).total_seconds())

    async def fire(self, watch: Watch, detail: str, *, source_cursor: str, staff_id: str | None = None) -> bool:
        """Reserve one bounded standing-rule intent before invoking its effect."""
        async with self._firing:
            current = self._watches.get(watch.id)
            if current is None or not current.enabled or self.cooling(current) > 0:
                return False
            watch = current
            now = self.clock()
            if watch.deadline_at and (_parse(watch.deadline_at) or now) <= now:
                await self._stop(watch, "expired", "its deadline has passed")
                return False
            hour_ago = now - timedelta(hours=1)
            counted = await self.manager.db.fetchone(
                "SELECT COUNT(*) AS n FROM watch_deliveries WHERE watch_id = ? AND created_at > ?"
                " AND status != 'cancelled'", (watch.id, hour_ago.isoformat()),
            )
            fires = int(counted["n"]) if counted else 0
            if fires >= self.config.max_fires_per_hour:
                await self._stop(watch, "budget", f"it fired {fires} times within an hour, the most a watch may")
                return False
            dedup_key = hashlib.sha256(f"{watch.id}:{watch.condition_revision}:{source_cursor}".encode()).hexdigest()
            delivery_id = uuid.uuid4().hex
            at = now.isoformat()
            state = dict(watch.state)
            state["fires"] = [f for f in state.get("fires", []) if (_parse(f) or now) > hour_ago] + [at]
            if watch.once:
                state["stopped"] = "once"
            snapshot = {"action": watch.action, "pattern": watch.pattern,
                        "note": str(watch.action.get("note") or watch.note),
                        "created_by": watch.created_by, "fire_count": watch.fire_count + 1}
            async with self.manager.db.transaction() as conn:
                async with conn.execute("SELECT enabled,condition_revision,deadline_at FROM watches WHERE id = ?",
                                        (watch.id,)) as cursor:
                    stored = await cursor.fetchone()
                if (stored is None or not stored["enabled"] or stored["condition_revision"] != watch.condition_revision
                        or stored["deadline_at"] != watch.deadline_at):
                    return False
                inserted = await conn.execute(
                    "INSERT OR IGNORE INTO watch_deliveries "
                    "(id, watch_id, condition_revision, source_cursor, dedup_key, status, action_json, detail,"
                    " staff_id, project_id, deadline_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                    (delivery_id, watch.id, watch.condition_revision, source_cursor, dedup_key,
                     json.dumps(snapshot), detail[:TEXT_MAX], staff_id, watch.project_id, watch.deadline_at, at, at),
                )
                if inserted.rowcount == 0:
                    return False
                await conn.execute(
                    "UPDATE watches SET last_fired_at = ?,fire_count = fire_count + 1,enabled = ?,"
                    " state_json = ? WHERE id = ? AND condition_revision = ?",
                    (at, 0 if watch.once else 1, json.dumps(state), watch.id, watch.condition_revision),
                )
            watch.state = state
            watch.last_fired_at = at
            watch.fire_count += 1
            if watch.once:
                watch.enabled = False
            if not watch.enabled:
                self._unfollow(watch.id)
            await self.deliveries.deliver(delivery_id)
            return True

    async def _stop(self, watch: Watch, code: str, why: str) -> None:
        """Switch a watch off by itself, and say so where the orchestrator and the operator both look.
        ``code`` is what the app words in the reader's language; ``why`` is the journal's sentence."""
        watch.enabled = False
        watch.state["stopped"] = code
        watch.state["last_error"] = why[:500]
        await self._save(watch)
        self._unfollow(watch.id)
        try:
            await self.manager.projects.record(watch.project_id, "system", "watch", f"The watch {watch.id} ({describe(watch)}) switched itself off: {why}.", {"watch_id": watch.id})
        except Exception:  # noqa: BLE001 — the watch is off either way
            logger.warning("could not journal the watch %s stopping", watch.id, exc_info=True)
        await self._changed(watch.project_id, "system")

    async def _changed(self, project_id: str, by: str) -> None:
        try:
            await self.manager.bus.publish("project.changed", {"change": "watches", "actor": by}, project_id=project_id)
        except Exception:  # noqa: BLE001 — the change stands; the event is a courtesy
            logger.warning("could not publish project.changed for %s", project_id, exc_info=True)

    # -- the bus -----------------------------------------------------------------------------------

    async def on_event(self, event: AppEvent) -> None:
        self._seq = max(self._seq, event.seq)
        if event.type == "webhook.received":
            await self._on_webhook(event)
            return
        if not event.project_id:
            return
        for watch in self.of_project(event.project_id, enabled_only=True):
            detail = event_matches(watch, event)
            if detail:
                await self.fire(watch, detail, staff_id=event.staff_id, source_cursor=f"bus:{event.seq}")

    async def _on_webhook(self, event: AppEvent) -> None:
        payload = dict(event.payload)
        for watch in [w for w in self._watches.values() if w.enabled and w.event in ("pr", "ci", "webhook")]:
            detail = webhook_matches(watch, payload)
            if not detail or self.cooling(watch) > 0:
                continue
            if watch.event == "webhook" and watch.pattern.get("regex"):
                text = f"event: {payload.get('event') or ''}\n{payload.get('summary') or ''}"[:WEBHOOK_TEXT_MAX]
                matched = await asyncio.to_thread(search_bounded, str(watch.pattern["regex"]), text)
                if matched is None:
                    await self._stop(watch, "slow_pattern", "its pattern did not finish matching in time")
                    continue
                if not matched:
                    continue
            await self.fire(watch, detail, source_cursor=f"bus:{event.seq}")

    # -- the ticker --------------------------------------------------------------------------------

    async def tick(self) -> None:
        now = self.clock()
        for watch in [w for w in self._watches.values() if w.enabled]:
            try:
                if watch.deadline_at and (_parse(watch.deadline_at) or now) <= now:
                    await self._stop(watch, "expired", "its deadline has passed")
                    continue
                if watch.event == "staff_silent":
                    await self._check_silence(watch, now)
                elif watch.event == "terminal_output" and watch.id not in self._followers:
                    self._follow(watch)
            except Exception:  # noqa: BLE001 — one watch must not keep the others from being looked at
                logger.exception("watch %s could not be checked", watch.id)
        await self._poll_git(now)
        if self._seq:
            await self.manager.db.kv_set(CURSOR_KEY, self._seq)

    async def _check_silence(self, watch: Watch, now: datetime) -> None:
        session = await self.manager.staff.live(str(watch.pattern.get("staff_id") or ""))
        if session is None or session.status not in ("starting", "working", "no_signal"):
            return
        since = session.last_signal_at or session.status_at
        last = _parse(since)
        if last is None or (now - last) < timedelta(minutes=int(watch.pattern.get("minutes") or 0)):
            return
        if watch.state.get("silent_since") == since:
            return
        # Once per silence: the same last signal never fires twice, whatever the cooldown.
        watch.state["silent_since"] = since
        if not await self.fire(watch, f"{watch.pattern.get('staff')} has been silent since {since[:16].replace('T', ' ')} UTC while working", staff_id=session.staff_id, source_cursor=f"silence:{session.staff_id}:{since}"):
            await self._save(watch)

    async def _poll_git(self, now: datetime) -> None:
        watched = [w for w in self._watches.values() if w.enabled and w.event == "git_commit"]
        by_folder: dict[tuple[str, str], list[Watch]] = {}
        for watch in watched:
            by_folder.setdefault((watch.project_id, str(watch.pattern.get("folder"))), []).append(watch)
        for (project_id, folder_id), watches in by_folder.items():
            key = f"{project_id}:{folder_id}"
            last = self._polled.get(key)
            if last is not None and (now - last).total_seconds() < self.config.git_poll_seconds:
                continue
            self._polled[key] = now
            project = await self.manager.projects.get(project_id)
            folder = project.folder(folder_id) if project is not None else None
            if folder is None:
                continue
            try:
                heads = await self.heads(folder)
            except (GitError, OSError, ConnectionError) as exc:
                for watch in watches:
                    watch.state["last_error"] = f"could not read the branches: {str(exc)[-300:]}"
                    await self._save(watch)
                continue
            for watch in watches:
                await self._compare_heads(watch, heads)

    async def _compare_heads(self, watch: Watch, heads: dict[str, tuple[str, str]]) -> None:
        branch = watch.pattern.get("branch")
        seen = {name: sha for name, (sha, _subject) in heads.items() if not branch or name == branch}
        before = watch.state.get("heads")
        watch.state["heads"] = seen
        watch.state.pop("last_error", None)
        if not isinstance(before, dict):
            # The first look is the baseline: what was already there is not news.
            await self._save(watch)
            return
        moved = [name for name, sha in seen.items() if before.get(name) != sha]
        if not moved:
            await self._save(watch)
            return
        lines = [f"{name}: {heads[name][0][:8]} \"{_one_line(heads[name][1], 100)}\"" for name in moved[:5]]
        more = f" and {len(moved) - 5} more" if len(moved) > 5 else ""
        cursor = "git:" + str(watch.pattern.get("folder")) + ":" + ",".join(f"{name}@{seen[name]}" for name in sorted(moved))
        if not await self.fire(watch, f"new commits in {watch.pattern.get('folder_label')} — " + "; ".join(lines) + more, source_cursor=cursor):
            await self._save(watch)

    async def heads(self, folder: ProjectFolder) -> dict[str, tuple[str, str]]:
        """``branch -> (commit, subject)`` of a folder's local branches, read without running any hook."""
        argv = [*_GIT_SAFE, "for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(contents:subject)", "refs/heads"]
        if folder.local(self.manager.projects.local_env):
            out = await run_command(argv, cwd=Path(folder.path), env=_GIT_ENV, timeout=GIT_TIMEOUT_SECONDS)
        else:
            bridge = HostBridge(lambda: cast("Terminals | None", self.app.extensions.get("terminals")))
            result = await bridge.exec_run(folder.env, argv, cwd=str(folder.path), env_vars=_GIT_ENV, timeout=GIT_TIMEOUT_SECONDS)
            if result.exit_code != 0:
                raise GitError(result.stderr[-300:] or f"git exited {result.exit_code}")
            out = result.stdout
        heads: dict[str, tuple[str, str]] = {}
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) >= 2:
                heads[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
        return heads

    # -- terminals ---------------------------------------------------------------------------------

    def _follow(self, watch: Watch) -> None:
        running = self._followers.get(watch.id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._follow_terminal(watch.id), name=f"watch:{watch.id}")
        self._followers[watch.id] = task
        watch_id = watch.id

        def forget(done: asyncio.Task[None]) -> None:
            if self._followers.get(watch_id) is done:
                self._followers.pop(watch_id, None)

        task.add_done_callback(forget)

    def _unfollow(self, watch_id: str) -> None:
        task = self._followers.pop(watch_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def terminal_of(self, watch: Watch) -> str | None:
        """The running terminal a watch reads: its own, or its member's live session's."""
        if watch.pattern.get("staff_id"):
            session = await self.manager.staff.live(str(watch.pattern["staff_id"]))
            terminal_id = session.terminal_id if session is not None else None
        else:
            terminal_id = str(watch.pattern.get("terminal") or "") or None
        if not terminal_id:
            return None
        row = await self.manager.db.fetchone("SELECT status FROM terminals WHERE id = ?", (terminal_id,))
        return terminal_id if row is not None and row["status"] == "running" else None

    async def _follow_terminal(self, watch_id: str) -> None:
        """One wait at a time on the terminal's output, until the watch goes or its terminal ends; the
        ticker starts it again when the terminal is back (a member's next session)."""
        since: int | None = None
        while True:
            watch = self._watches.get(watch_id)
            if watch is None or not watch.enabled:
                return
            left = self.cooling(watch)
            if left > 0:
                await self.nap(left)
                since = None  # what was printed during the cooldown is not waited for
                continue
            terminals: Any = self.app.extensions.get("terminals")
            terminal_id = await self.terminal_of(watch)
            if terminals is None or terminal_id is None:
                return
            try:
                result = await terminals.wait_for(terminal_id, regex=str(watch.pattern.get("regex")), scope="output", since_seq=since, timeout=WAIT_SECONDS)
            except InvalidRequest as exc:
                await self._stop(watch, "pattern", f"the terminal refused its pattern: {exc}")
                return
            except EnvUnavailable:
                await asyncio.sleep(BACKOFF_SECONDS)
                continue
            except TerminalError:
                return
            matched = result.get("matched")
            seq = result.get("seq")
            if matched == "timeout":
                since = int(seq) if isinstance(seq, int) else None
                continue
            if matched != "regex":
                return
            text = str(result.get("match") or "")
            since = int(seq) if isinstance(seq, int) else None
            if await self._own_echo(watch, text):
                continue
            title = watch.pattern.get("terminal_title") or watch.pattern.get("staff") or terminal_id
            await self.fire(watch, f"{title}'s terminal printed \"{_one_line(text, 200)}\"", staff_id=watch.pattern.get("staff_id"), source_cursor=f"terminal:{terminal_id}:{since}")

    async def _own_echo(self, watch: Watch, text: str) -> bool:
        """Whether a match is only the orchestrator's own message showing in the member's terminal."""
        staff_id = watch.pattern.get("staff_id")
        if not staff_id or not text:
            return False
        cutoff = (self.clock() - timedelta(seconds=ECHO_SECONDS)).isoformat()
        rows = await self.manager.db.fetchall(
            "SELECT text FROM staff_messages WHERE staff_id = ? AND origin = 'orchestrator' AND created_at >= ? ORDER BY created_at DESC LIMIT 20",
            (staff_id, cutoff),
        )
        return any(text in str(r["text"]) for r in rows)

    # -- life --------------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.manager.db.execute(
            "UPDATE watch_deliveries SET status = 'reconciling', updated_at = ? WHERE status = 'pending'",
            (self.clock().isoformat(),),
        )
        await self.load()
        cursor = await self.manager.db.kv_get(CURSOR_KEY)
        after = int(cursor) if isinstance(cursor, int) else None
        # A cursor replays what arrived while the host was down; a fire already recorded is inside its
        # cooldown, so a replayed event cannot fire the same watch twice.
        self._handler = self.manager.bus.on(EventFilter(types=BUS_TYPES), self.on_event, name="watches", after=after)
        for watch in self._watches.values():
            if watch.enabled and watch.event == "terminal_output":
                self._follow(watch)

    async def loop(self) -> None:
        while True:
            try:
                await self.deliveries.sweep()
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("the watches tick failed")
            await asyncio.sleep(self.config.tick_seconds)

    async def close(self) -> None:
        for task in list(self._followers.values()):
            task.cancel()
        for task in list(self._followers.values()):
            with contextlib.suppress(BaseException):
                await task
        self._followers.clear()
        if self._handler is not None:
            self._handler.cancel()
            with contextlib.suppress(BaseException):
                await self._handler


async def install(app: Application) -> list[asyncio.Task[None]]:
    watches = Watches(app)
    app.extensions["watches"] = watches
    await watches.start()
    return [asyncio.create_task(watches.loop(), name="watches")]


__all__ = ["ACTIONS", "WHEN_EVENTS", "Watch", "WatchRefused", "Watches", "check_regex", "describe", "event_matches", "install", "webhook_matches"]
