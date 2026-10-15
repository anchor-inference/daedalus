"""A project's environment: what its folders are made of, prepared once, and whether that worked.

The operator declares it — repositories to clone, files to seed, which of the installation's MCP
servers and skills the project's agents see, and a plain-language setup goal — and the host prepares
it the first time work is launched in the project, or when the operator presses Prepare. The record
keeps the spec, the hash of the spec the last preparation ran for, and how that preparation ended,
so a launch can be held while the project is still being prepared or failed to be, and a spec that
changed afterwards shows as stale rather than being run again behind the operator's back.

A project without a record, or whose spec asks for nothing to be prepared, behaves exactly as a
project did before environments existed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectError, folder_name

STATES = ("unprepared", "preparing", "ready", "failed")
REPOS_MAX = 10
SEEDS_MAX = 20
SEED_CONTENT_MAX = 100_000
NAMES_MAX = 100
GOAL_MAX = 4000
URL_MAX = 500
BRANCH_MAX = 200
SEED_PATH_MAX = 200
LOG_MAX = 20_000
"""The preparation's log as kept: the clone and seed lines and the tail of a failure, not an install's output."""
GOAL_MINUTES_DEFAULT = 30
GOAL_MINUTES_MAX = 120
GOAL_USD_DEFAULT = 2.0
GOAL_USD_MAX = 20.0
"""The setup agent's bounds. An install that has not finished in two hours, or a setup that has spent
twenty dollars, is a setup that needs the operator rather than more of the same."""

URL_SCHEMES = ("https", "http", "ssh", "git", "file")
"""The transports a repository may be cloned over. Git's remote helpers (``ext::``, ``fd::``) run
programs named in the URL, which a spec copied from somewhere must never be able to do."""
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^/\\].*$|^[A-Za-z0-9.-]+:[^/\\:][^:]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


class SpecError(ProjectError):
    """A spec the store will not keep, with the field and the reason."""


def _clean_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise SpecError("a repository needs a URL")
    if len(url) > URL_MAX:
        raise SpecError(f"a repository URL is at most {URL_MAX} characters")
    if url.startswith("-") or any(ord(ch) < 33 for ch in url):
        raise SpecError(f"{url!r} is not a repository URL")
    if "::" in url.split("/", 1)[0]:
        raise SpecError(f"{url!r} names a git remote helper; clone over https, ssh or git")
    if url.startswith("/"):
        return url
    scheme = urlsplit(url).scheme.lower()
    if scheme:
        if scheme not in URL_SCHEMES:
            # "host:path" parses with the host as its scheme; the scp-like form below is the
            # one place that shape is allowed.
            if _SCP_LIKE.match(url):
                return url
            raise SpecError(f"{url!r}: a repository is cloned over {', '.join(URL_SCHEMES)}, not {scheme}")
        return url
    if _SCP_LIKE.match(url):
        return url
    raise SpecError(f"{url!r} is not a repository URL: give https://…, ssh://…, user@host:path or an absolute path")


def url_host(url: str) -> str:
    """The network host a clone of ``url`` reaches, or empty for a path on this machine."""
    if url.startswith("/") or url.lower().startswith("file:"):
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() in URL_SCHEMES and parts.hostname:
        return parts.hostname.lower()
    head = url.split(":", 1)[0]
    return head.rsplit("@", 1)[-1].lower()


def _clean_seed_path(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise SpecError("a seed file needs a path")
    if len(text) > SEED_PATH_MAX:
        raise SpecError(f"a seed file's path is at most {SEED_PATH_MAX} characters")
    path = PurePosixPath(text)
    if path.is_absolute() or text.startswith("~") or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise SpecError(f"{text!r}: a seed file's path is relative to the project's primary folder")
    if any(part in ("..", "") for part in text.split("/")) or text.startswith("-"):
        raise SpecError(f"{text!r} climbs out of the folder or is not a plain path")
    if path.parts[0] in (".git", ".agents"):
        raise SpecError(f"{text!r} is inside git's or the agents' own directory")
    return path.as_posix()


def _names(raw: Any, what: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise SpecError(f"{what} is a list of names")
    names = sorted({str(item).strip() for item in raw if str(item).strip()})
    if len(names) > NAMES_MAX:
        raise SpecError(f"at most {NAMES_MAX} {what}")
    return tuple(names)


@dataclass(frozen=True, slots=True)
class RepoSpec:
    url: str
    folder: str
    branch: str = ""

    def dump(self) -> dict[str, str]:
        return {"url": self.url, "branch": self.branch, "folder": self.folder}


@dataclass(frozen=True, slots=True)
class SeedSpec:
    path: str
    content: str

    def dump(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content}


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    """What the operator declared. ``mcp`` and ``skills`` empty mean every server and skill, as before."""

    repos: tuple[RepoSpec, ...] = ()
    seeds: tuple[SeedSpec, ...] = ()
    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    goal: str = ""
    goal_minutes: int = GOAL_MINUTES_DEFAULT
    goal_usd: float = GOAL_USD_DEFAULT

    @classmethod
    def parse(cls, raw: Any) -> EnvironmentSpec:
        """A spec from what the app sent, refused whole with the reason when one part is wrong."""
        data = raw if isinstance(raw, dict) else {}
        repos: list[RepoSpec] = []
        for item in data.get("repos") or []:
            if not isinstance(item, dict):
                raise SpecError("each repository is {url, branch, folder}")
            url = _clean_url(item.get("url"))
            branch = str(item.get("branch") or "").strip()
            if branch and (len(branch) > BRANCH_MAX or not _BRANCH.match(branch) or branch.startswith(("-", "/")) or ".." in branch):
                raise SpecError(f"{branch!r} is not a branch name")
            raw_folder = str(item.get("folder") or "").strip() or _default_folder(url)
            try:
                name = folder_name(raw_folder)
            except ProjectError as exc:
                raise SpecError(f"repository {url}: {exc}") from None
            repos.append(RepoSpec(url=url, folder=name, branch=branch))
        if len(repos) > REPOS_MAX:
            raise SpecError(f"at most {REPOS_MAX} repositories")
        if len({r.folder for r in repos}) != len(repos):
            raise SpecError("two repositories would be cloned into one folder; give each its own folder name")
        seeds: list[SeedSpec] = []
        for item in data.get("seeds") or []:
            if not isinstance(item, dict):
                raise SpecError("each seed file is {path, content}")
            content = str(item.get("content") or "")
            if len(content) > SEED_CONTENT_MAX:
                raise SpecError(f"a seed file is at most {SEED_CONTENT_MAX} characters")
            seeds.append(SeedSpec(path=_clean_seed_path(item.get("path")), content=content))
        if len(seeds) > SEEDS_MAX:
            raise SpecError(f"at most {SEEDS_MAX} seed files")
        if len({s.path for s in seeds}) != len(seeds):
            raise SpecError("two seed files have the same path")
        goal = str(data.get("goal") or "").strip()
        if len(goal) > GOAL_MAX:
            raise SpecError(f"the setup goal is at most {GOAL_MAX} characters")
        try:
            minutes = int(data.get("goal_minutes") or GOAL_MINUTES_DEFAULT)
            usd = float(data.get("goal_usd") if data.get("goal_usd") is not None else GOAL_USD_DEFAULT)
        except (TypeError, ValueError):
            raise SpecError("the setup goal's bounds are a number of minutes and of dollars") from None
        if not 1 <= minutes <= GOAL_MINUTES_MAX:
            raise SpecError(f"the setup goal may run 1 to {GOAL_MINUTES_MAX} minutes")
        if not 0 < usd <= GOAL_USD_MAX:
            raise SpecError(f"the setup goal may spend more than $0 and at most ${GOAL_USD_MAX:.0f}")
        return cls(repos=tuple(repos), seeds=tuple(seeds), mcp=_names(data.get("mcp"), "MCP servers"),
                   skills=_names(data.get("skills"), "skills"), goal=goal, goal_minutes=minutes, goal_usd=round(usd, 2))

    def dump(self) -> dict[str, Any]:
        return {
            "repos": [r.dump() for r in self.repos],
            "seeds": [s.dump() for s in self.seeds],
            "mcp": list(self.mcp),
            "skills": list(self.skills),
            "goal": self.goal,
            "goal_minutes": self.goal_minutes,
            "goal_usd": self.goal_usd,
        }

    @property
    def needs_preparation(self) -> bool:
        """Whether anything has to be done on disk. The visibility subsets alone take effect at the
        next launch and hold nothing back."""
        return bool(self.repos or self.seeds or self.goal)

    @property
    def empty(self) -> bool:
        return not (self.needs_preparation or self.mcp or self.skills)

    def digest(self) -> str:
        """The hash a preparation is recorded against; only what preparing does counts, so narrowing
        the MCP servers does not make a prepared project stale."""
        prepared = {"repos": [r.dump() for r in self.repos], "seeds": [s.dump() for s in self.seeds], "goal": self.goal}
        return hashlib.sha256(json.dumps(prepared, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _default_folder(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


@dataclass(frozen=True, slots=True)
class EnvironmentRecord:
    project_id: str
    spec: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    state: str = "unprepared"
    prepared_hash: str = ""
    attempt_hash: str = ""
    attempt: int = 0
    reason: str = ""
    log: str = ""
    setup_session_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = ""

    @property
    def stale(self) -> bool:
        """Prepared, or failed, for a spec that has changed since: the operator may prepare again."""
        return self.state in ("ready", "failed") and self.spec.needs_preparation and self.attempt_hash != self.spec.digest()

    def hold(self) -> str:
        """Why a member may not start in this project now, or empty when it may.

        Phrased for the orchestrator, which reads it as the reason its Assign is waiting, and for the
        app, which shows it under the project's environment."""
        if not self.spec.needs_preparation:
            return ""
        if self.state == "unprepared":
            return "environment not ready: it has not been prepared yet; preparation starts with the first launch"
        if self.state == "preparing":
            return f"environment not ready: it is being prepared (since {self.started_at[:16].replace('T', ' ')} UTC); members start when it is ready"
        if self.state == "failed":
            return f"environment not ready: preparation failed: {self.reason or 'no reason recorded'}. The operator fixes it in the project's settings and presses Prepare"
        return ""

    def view(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "spec": self.spec.dump(),
            "configured": not self.spec.empty,
            "needs_preparation": self.spec.needs_preparation,
            "state": self.state,
            "stale": self.stale,
            "reason": self.reason,
            "log": self.log,
            "hold": self.hold(),
            "attempt": self.attempt,
            "setup_session_id": self.setup_session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(row: Any) -> EnvironmentRecord:
    try:
        raw = json.loads(row["spec_json"] or "{}")
    except ValueError:
        raw = {}
    try:
        spec = EnvironmentSpec.parse(raw)
    except SpecError:
        # A spec that no longer passes (a bound tightened since it was written) is read as nothing
        # asked for, so a launch is never held by a record nobody can satisfy; saving it again says why.
        spec = EnvironmentSpec()
    return EnvironmentRecord(
        project_id=row["project_id"], spec=spec, state=row["state"], prepared_hash=row["prepared_hash"],
        attempt_hash=row["attempt_hash"], attempt=int(row["attempt"]), reason=row["reason"], log=row["log"],
        setup_session_id=row["setup_session_id"], started_at=row["started_at"] or "",
        finished_at=row["finished_at"] or "", updated_at=row["updated_at"],
    )


class EnvironmentStore:
    """The records, and the compare-and-set that lets exactly one preparation run per project."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, project_id: str) -> EnvironmentRecord:
        row = await self.db.fetchone("SELECT * FROM project_environments WHERE project_id = ?", (project_id,))
        return _record(row) if row is not None else EnvironmentRecord(project_id=project_id)

    async def set_spec(self, project_id: str, spec: EnvironmentSpec) -> EnvironmentRecord:
        """Keep the spec. The state is left as it is: a changed spec shows as stale, and only the
        operator's Prepare (or, for a project never prepared, its first launch) acts on it."""
        now = _now()
        await self.db.execute(
            "INSERT INTO project_environments(project_id, spec_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(project_id) DO UPDATE SET spec_json = excluded.spec_json, updated_at = excluded.updated_at",
            (project_id, json.dumps(spec.dump(), ensure_ascii=False), now),
        )
        return await self.get(project_id)

    async def begin(self, project_id: str, *, first_only: bool) -> EnvironmentRecord | None:
        """Mark a preparation started and return the record it runs for; ``None`` when one is already
        running, or — with ``first_only``, the launch's automatic start — when the project was
        prepared before. Written before anything is done, so a restart finds the attempt it interrupted."""
        record = await self.get(project_id)
        if not record.spec.needs_preparation:
            return None
        allowed = ("unprepared",) if first_only else ("unprepared", "ready", "failed")
        now = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE project_environments SET state = 'preparing', attempt = attempt + 1, attempt_hash = ?, reason = '',"
                f" log = '', setup_session_id = '', started_at = ?, finished_at = NULL, updated_at = ?"
                f" WHERE project_id = ? AND state IN ({','.join('?' * len(allowed))})",
                (record.spec.digest(), now, now, project_id, *allowed),
            )
            if cursor.rowcount != 1:
                return None
        return await self.get(project_id)

    async def note(self, project_id: str, attempt: int, *, log: str | None = None, setup_session_id: str | None = None) -> None:
        """Progress of the running attempt; a write for an attempt that is no longer the current one is dropped."""
        sets, params = [], []
        if log is not None:
            sets.append("log = ?")
            params.append(log[-LOG_MAX:])
        if setup_session_id is not None:
            sets.append("setup_session_id = ?")
            params.append(setup_session_id)
        if not sets:
            return
        await self.db.execute(
            f"UPDATE project_environments SET {', '.join(sets)}, updated_at = ? WHERE project_id = ? AND attempt = ? AND state = 'preparing'",
            (*params, _now(), project_id, attempt),
        )

    async def finish(self, project_id: str, attempt: int, *, ready: bool, reason: str, log: str) -> bool:
        now = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE project_environments SET state = ?, prepared_hash = CASE WHEN ? THEN attempt_hash ELSE prepared_hash END,"
                " reason = ?, log = ?, finished_at = ?, updated_at = ? WHERE project_id = ? AND attempt = ? AND state = 'preparing'",
                ("ready" if ready else "failed", int(ready), reason[:2000], log[-LOG_MAX:], now, now, project_id, attempt),
            )
            return cursor.rowcount == 1

    async def interrupt_all(self) -> list[str]:
        """Fail every preparation a previous process left running: what it did is not known, and
        running it again unasked could spend the setup budget twice. The operator prepares again."""
        rows = await self.db.fetchall("SELECT project_id FROM project_environments WHERE state = 'preparing'")
        if rows:
            now = _now()
            await self.db.execute(
                "UPDATE project_environments SET state = 'failed', reason = 'the host restarted while the environment was being prepared; press Prepare to run it again',"
                " finished_at = ?, updated_at = ? WHERE state = 'preparing'", (now, now))
        return [str(r["project_id"]) for r in rows]


__all__ = [
    "STATES",
    "SpecError",
    "EnvironmentRecord",
    "EnvironmentSpec",
    "EnvironmentStore",
    "RepoSpec",
    "SeedSpec",
    "url_host",
]
