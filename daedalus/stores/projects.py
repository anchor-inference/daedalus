"""Projects: the folders and grouping every session belongs to, and what the operator writes about them."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.stores.database import Database

ENVIRONMENTS = ("container", "host")
"""Where a folder lives: inside the container the agent runs in, or on the machine around it. In
native mode the two are the same machine and this process's folders are all ``host``."""

AUTONOMY = ("ask", "normal", "full")
CONCURRENCY_DEFAULT = 6
"""How many staff of one project may work at once unless the orchestrator or the operator says otherwise."""
CONCURRENCY_CAP_DEFAULT = 10
"""The most the orchestrator may raise that to by itself; the operator can move the cap."""

BRIEF_SECTIONS = ("goals", "constraints", "preferences", "done_when", "allowed_without_operator", "notes")
OPERATOR_ONLY_SECTIONS = frozenset({"allowed_without_operator"})
"""Sections nobody but the operator may write. What the orchestrator may grant without asking is
bounded by this section, so an orchestrator able to edit it could grant itself anything."""
BRIEF_AUTHORS = ("operator", "orchestrator", "system")
JOURNAL_AUTHORS = ("orchestrator", "operator", "system")
JOURNAL_PAGE_MAX = 200
RULE_KIND = "rule"
"""A journal entry that is a standing rule of the operator's: in force until an entry of
``RULE_LIFTED_KIND`` names it, shown whole to the orchestrator every turn and to every member with
each task."""
RULE_LIFTED_KIND = "rule_lifted"
RULE_TEXT_MAX = 600
"""A rule is an instruction, not a document: what it needs is in the brief or the journal."""
RULES_MAX = 10
RULES_CHARS = 1500
"""How many rules, and how much of them, may be in force at once. Bounded when a rule is written
rather than when one is shown, so every rule in force is always shown whole: a cut at shown time is
what hid the operator's instruction in the first place."""


class ProjectError(ValueError):
    """A project the store will not keep: a relative folder, a file, or a folder tangled with another's."""


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    """The per-project orchestrator. Off until the operator switches it on.

    ``session_id`` is written only through :meth:`ProjectStore.set_orchestrator`, a compare-and-set,
    so two starts racing for one project cannot both believe they are its orchestrator.
    """

    enabled: bool = False
    session_id: str = ""
    model: str = ""
    autonomy: str = "normal"
    concurrency: int = CONCURRENCY_DEFAULT
    concurrency_cap: int = CONCURRENCY_CAP_DEFAULT
    telegram_topic_id: int = 0

    @classmethod
    def load(cls, raw: Any) -> OrchestratorSettings:
        data = raw if isinstance(raw, dict) else {}
        cap = max(1, _int(data.get("concurrency_cap"), CONCURRENCY_CAP_DEFAULT))
        autonomy = str(data.get("autonomy") or "normal")
        return cls(
            enabled=bool(data.get("enabled", False)),
            session_id=str(data.get("session_id") or ""),
            model=str(data.get("model") or ""),
            autonomy=autonomy if autonomy in AUTONOMY else "normal",
            concurrency=min(cap, max(1, _int(data.get("concurrency"), CONCURRENCY_DEFAULT))),
            concurrency_cap=cap,
            telegram_topic_id=_int(data.get("telegram_topic_id"), 0),
        )

    def dump(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "session_id": self.session_id,
            "model": self.model,
            "autonomy": self.autonomy,
            "concurrency": self.concurrency,
            "concurrency_cap": self.concurrency_cap,
            "telegram_topic_id": self.telegram_topic_id,
        }


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    """What the operator decides per project.

    ``snapshots`` is off by default for a folder the operator points at: it may be a large existing
    repository, and committing a hundred thousand files into ``.checkpoints`` twice a turn costs
    more than the undo is worth. Automatically created projects opt in because they begin empty.

    Only the known keys are loaded and dumped. A key nothing reads is a key nothing keeps honest,
    and the settings column is the one place several later features write into.
    """

    snapshots: bool = False
    system: str = ""
    """Non-empty on a project the installation made for itself rather than the operator: ``"voice"``
    is the folder the concierge and the agents it delegates to work in. A system project keeps its
    name and snapshot switch editable but cannot be removed."""
    ephemeral: bool = False
    """Made implicitly by a new chat, with a scratch folder of the installation's own, and removed
    with its last session. "Keep as a project" clears it."""
    default_env: str = ""
    """Where a new agent of this project runs when nothing names a folder; empty until the store
    fills it with the environment this process runs in."""
    orchestrator: OrchestratorSettings = field(default_factory=OrchestratorSettings)

    @classmethod
    def load(cls, raw: Any, *, default_env: str = "") -> ProjectSettings:
        data = raw if isinstance(raw, dict) else {}
        env = str(data.get("default_env") or "")
        return cls(
            snapshots=bool(data.get("snapshots", False)),
            system=str(data.get("system") or ""),
            ephemeral=bool(data.get("ephemeral", False)),
            default_env=env if env in ENVIRONMENTS else default_env,
            orchestrator=OrchestratorSettings.load(data.get("orchestrator")),
        )

    def dump(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "system": self.system,
            "ephemeral": self.ephemeral,
            "default_env": self.default_env,
            "orchestrator": self.orchestrator.dump(),
        }


@dataclass(frozen=True, slots=True)
class FolderSpec:
    """A folder asked for: the path as the operator gave it, and what they said about it."""

    path: str
    label: str = ""
    env: str | None = None
    """``None`` is the environment this process runs in."""
    readonly: bool = False


@dataclass(frozen=True, slots=True)
class ProjectFolder:
    id: str
    project_id: str
    path: Path
    label: str
    env: str
    is_git: bool
    readonly: bool
    position: int
    created_at: datetime
    managed: bool = False
    """Whether the folder is one the installation chose for itself — a folder under the managed
    projects tree, made when the project was made — rather than a folder the operator pointed at.

    The two are missing for opposite reasons, so they cannot share one answer. A managed folder that
    is not there was never created or has been swept away, and nobody but this installation was ever
    going to create it: it is remade on demand. A folder the operator named is theirs, and an empty
    directory conjured at its path would shadow the real one when the mount or the disk comes back —
    so that one stays the error it is, naming the path so they can restore or mount it.
    """

    @property
    def reachable(self) -> bool:
        """Whether this process can actually reach the folder — false in a container until it is mounted.

        A folder of the other environment answers from this process's filesystem too, which is the
        truth for this process: the path may exist here and mean something else there. Whoever asks
        on behalf of an agent asks :meth:`local` first.
        """
        return self.path.is_dir() and os.access(self.path, os.R_OK)

    @property
    def writable(self) -> bool:
        return not self.readonly and self.reachable and os.access(self.path, os.W_OK)

    def local(self, local_env: str) -> bool:
        """Whether this process's own tools can work in the folder at all."""
        return self.env == local_env

    def view(self) -> dict[str, Any]:
        reachable = self.reachable
        return {
            "id": self.id,
            "path": str(self.path),
            "label": self.label,
            "env": self.env,
            "is_git": self.is_git,
            "readonly": self.readonly,
            "position": self.position,
            "managed": self.managed,
            "reachable": reachable,
            "writable": not self.readonly and reachable and os.access(self.path, os.W_OK),
        }


@dataclass(frozen=True, slots=True)
class Project:
    """A project and its folders, the first of which is the primary: where a session works when it
    names no other folder, and where an ephemeral project's scratch lives."""

    id: str
    name: str
    created_at: datetime
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    folders: tuple[ProjectFolder, ...] = ()
    setup_by: str = ""
    """``dispatcher`` while the main orchestrator is setting the project up: every request of the
    project is then shown in its chat too. Empty once the setup is over."""

    @property
    def primary(self) -> ProjectFolder:
        if not self.folders:
            # The store never keeps a project without one; a row that has none is a database edited
            # by hand, and the message says which project so it can be mended.
            raise ProjectError(f"the project {self.name} ({self.id}) has no folder")
        return self.folders[0]

    def folder(self, folder_id: str) -> ProjectFolder | None:
        return next((f for f in self.folders if f.id == folder_id), None)

    def local_folders(self, env: str) -> tuple[ProjectFolder, ...]:
        return tuple(f for f in self.folders if f.local(env))

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "settings": self.settings.dump(),
            "system": self.settings.system,
            "folders": [f.view() for f in self.folders],
            "setup_by": self.setup_by,
        }


@dataclass(frozen=True, slots=True)
class BriefSection:
    section: str
    body: str = ""
    updated_at: str = ""
    updated_by: str = ""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: int
    project_id: str
    at: str
    author: str
    kind: str
    text: str
    refs: dict[str, Any]

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "at": self.at, "author": self.author, "kind": self.kind, "text": self.text, "refs": self.refs}


def _entry(row: Any) -> JournalEntry:
    try:
        refs = json.loads(row["refs_json"] or "{}")
    except (TypeError, ValueError):
        refs = {}
    return JournalEntry(int(row["id"]), row["project_id"], row["at"], row["author"], row["kind"], row["text"], refs if isinstance(refs, dict) else {})


def _is_managed(path: Path, managed_root: Path | None) -> bool:
    """Whether this folder is one of ours: a folder *inside* the managed projects tree.

    The tree itself is not a project and never a folder of one, so containment is the whole test — a
    path equal to the tree is as foreign here as one outside it.
    """
    return managed_root is not None and managed_root in path.parents


def _nested_under(session_id: str, metadata: dict[str, Any], known: set[str]) -> bool:
    """Whether the Agents screen draws this session inside another row rather than as one of its own.

    The same rule the screen nests by, and it has to stay the same rule or the count over a folder
    stops describing the list under it: a subagent hangs under its leader, a fork under the session
    it was taken from, and one whose leader or origin no longer exists hangs under nothing.
    """
    leader = metadata.get("subagent_of")
    if isinstance(leader, str) and leader in known:
        return True
    forked = metadata.get("forked_from")
    origin = forked.get("session_id") if isinstance(forked, dict) else None
    return isinstance(origin, str) and origin != session_id and origin in known


def _detect_git(path: Path) -> bool:
    """A work tree has ``.git`` as a directory, a linked worktree or a submodule has it as a file."""
    try:
        return (path / ".git").exists()
    except OSError:
        return False


PSEUDO_FILESYSTEMS = (Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"))
"""Kernel interfaces the operating system mounts, not folders with work in them. A project folder on
one of them would list a running machine's processes and devices as if they were files to edit."""


def normalise_root(raw: str) -> Path:
    """The path a project folder is anchored at: absolute, ``~`` expanded, no trailing slash, no ``..``.

    Not resolved against the filesystem — a folder that is not mounted yet has nothing to resolve
    against, and a folder that IS mounted is realpath'd by the containment check every time it is
    used, which is where a symlink swapped later would have to be caught anyway.
    """
    text = (raw or "").strip()
    if not text:
        raise ProjectError("a project needs a folder")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ProjectError(f"a project folder must be an absolute path, not {text!r}")
    path = Path(os.path.normpath(path))
    if path == Path(path.root):
        raise ProjectError("the filesystem root is not a project")
    for pseudo in PSEUDO_FILESYSTEMS:
        if path == pseudo or pseudo in path.parents:
            raise ProjectError(f"{pseudo} is the kernel's, not a folder of work; a project is a folder with files in it")
    if path.is_file():
        raise ProjectError(f"{path} is a file, not a folder")
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectStore:
    """The projects and their folders, briefs and journals, and the one column on ``sessions`` that points into them."""

    def __init__(
        self,
        db: Database,
        *,
        managed_root: Path | None = None,
        reserved: Iterable[Path] = (),
        home: Path | None = None,
        local_env: str | None = None,
    ) -> None:
        self._db = db
        self._write = asyncio.Lock()
        """Held across the whole of a read-check-insert. Every way of adding a folder looks the table
        up and then writes to it, with awaits in between; nothing else serialised them, so two callers
        that asked at the same moment each saw a table without the row the other was about to add."""
        self._roots: tuple[Path, ...] = ()
        self._reserved = tuple(dict.fromkeys(Path(os.path.normpath(Path(p).expanduser())) for p in reserved))
        """This installation's own directories. A project folder may not be one, contain one or sit inside one."""
        self._home = Path(os.path.normpath(Path(home).expanduser())) if home is not None else None
        """The operator's home folder, refused as a whole and allowed one folder in: every project
        lives inside it, and taking the whole of it is what switches off the rule that asks before a
        path in the home folder is touched."""
        self._managed_root = Path(os.path.normpath(managed_root.expanduser())) if managed_root is not None else None
        self.local_env = local_env or db.local_env
        """The environment this process runs in; its folders are the ones its own tools can reach."""

    @property
    def roots(self) -> tuple[Path, ...]:
        """Every folder of this environment that belongs to a project, as the last read of the table saw them.

        The tool policy is built while a call is being judged, which is not a place to wait on a
        query. Every write below refreshes this, and the manager reads the table on the way up, so
        the list is current whenever a project has ever existed in this process. A folder of the
        other environment is not here: its path names nothing this process's tools can touch.
        """
        return self._roots

    # -- reading -------------------------------------------------------------------

    def _folder(self, row: Any) -> ProjectFolder:
        path = Path(row["path"])
        return ProjectFolder(
            id=row["id"],
            project_id=row["project_id"],
            path=path,
            label=row["label"],
            env=row["env"],
            is_git=bool(row["is_git"]),
            readonly=bool(row["readonly"]),
            position=int(row["position"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            managed=_is_managed(path, self._managed_root),
        )

    def _project(self, row: Any, folders: Sequence[ProjectFolder]) -> Project:
        try:
            settings = json.loads(row["settings"] or "{}")
        except (TypeError, ValueError):
            settings = {}
        return Project(
            id=row["id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            settings=ProjectSettings.load(settings, default_env=self.local_env),
            folders=tuple(folders),
            setup_by=str(row["setup_by"] or "") if "setup_by" in row.keys() else "",
        )

    async def list(self) -> list[Project]:
        rows = await self._db.fetchall("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        folder_rows = await self._db.fetchall("SELECT * FROM project_folders ORDER BY project_id, position, created_at")
        by_project: dict[str, list[ProjectFolder]] = {}
        for folder_row in folder_rows:
            by_project.setdefault(folder_row["project_id"], []).append(self._folder(folder_row))
        projects = [self._project(r, by_project.get(r["id"], [])) for r in rows]
        self._roots = tuple(f.path for p in projects for f in p.folders if f.local(self.local_env))
        return projects

    async def get(self, project_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            return None
        folders = await self._db.fetchall("SELECT * FROM project_folders WHERE project_id = ? ORDER BY position, created_at", (project_id,))
        return self._project(row, [self._folder(f) for f in folders])

    async def folder_by_id(self, folder_id: str) -> ProjectFolder | None:
        """A folder by its own id, whichever project holds it: what a form that names only the folder
        (the hiring form asking which agents a CLI offers there) needs."""
        row = await self._db.fetchone("SELECT * FROM project_folders WHERE id = ?", (folder_id,))
        return self._folder(row) if row is not None else None

    async def for_path(self, path: Path) -> Project | None:
        """The project that first took this exact path as a folder. A folder may belong to several
        projects; the one that had it first is the one an internal working directory is adopted into,
        so the answer does not change when another project adds it later."""
        target = Path(os.path.normpath(path.expanduser()))
        row = await self._db.fetchone("SELECT project_id FROM project_folders WHERE path = ? ORDER BY created_at, id LIMIT 1", (str(target),))
        return await self.get(row["project_id"]) if row is not None else None

    async def holders(self, path: Path | str, *, besides: str | None = None) -> list[str]:
        """The names of the projects that have exactly this folder, but for ``besides``: what a
        confirmation says when the folder asked for is already part of other work."""
        target = normalise_root(str(path))
        rows = await self._db.fetchall(
            "SELECT DISTINCT p.name FROM project_folders f JOIN projects p ON p.id = f.project_id WHERE f.path = ? AND p.id != ? ORDER BY p.name COLLATE NOCASE",
            (str(target), besides or ""),
        )
        return [str(r["name"]) for r in rows]

    async def check_folder(self, project_id: str, path: str) -> Path:
        """Whether ``path`` could be added to the project, by the same rules :meth:`add_folder` holds,
        without adding it; the normalised path. A request that the operator is asked to confirm is
        checked with this first, so an approval is never spent on a folder that was bound to fail."""
        target = normalise_root(path)
        self._refuse_reserved(target)
        if await self.get(project_id) is None:
            raise KeyError(project_id)
        await self._refuse_overlap(target, project_id)
        return target

    # -- making projects -------------------------------------------------------------

    async def create(
        self,
        name: str,
        folders: Sequence[FolderSpec | str] | None = None,
        *,
        settings: ProjectSettings | None = None,
        project_id: str | None = None,
    ) -> Project:
        """A project with the folders asked for, in that order; with none, a managed scratch folder of its own."""
        label = (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        if isinstance(folders, str):
            # A string is a sequence too, of one-character "folders"; it is a caller's slip, not a request.
            raise TypeError("folders is a list of folders, not one path")
        project_id = project_id or uuid.uuid4().hex[:12]
        specs = [FolderSpec(f) if isinstance(f, str) else f for f in folders or ()]
        planned: list[tuple[Path, FolderSpec, bool]] = []
        if not specs:
            if self._managed_root is None:
                raise ProjectError("automatic project folders are not configured")
            planned.append((self._managed_root / project_id, FolderSpec(str(self._managed_root / project_id)), True))
        for spec in specs:
            path = normalise_root(spec.path)
            self._refuse_reserved(path)
            planned.append((path, spec, False))
        self._refuse_among(p for p, _, _ in planned)
        async with self._write:
            for path, _, _ in planned:
                await self._refuse_overlap(path)
            for path, _, ours in planned:
                if ours:
                    self._make_root(path)
            project = await self._insert(project_id, label, settings or ProjectSettings(), [(path, spec) for path, spec, _ in planned])
            await self.list()
        return project

    async def adopt_directory(self, name: str, root: Path) -> Project:
        """Make an internal working directory a project, or return the project already owning it."""
        existing = await self.for_path(root)
        if existing is not None:
            return existing
        label = (name or "").strip() or root.name or "Project"
        path = Path(os.path.normpath(root.expanduser()))
        async with self._write:
            existing = await self.for_path(path)
            if existing is not None:
                return existing
            await self._refuse_overlap(path)
            project = await self._insert(uuid.uuid4().hex[:12], label, ProjectSettings(snapshots=True), [(path, FolderSpec(str(path)))])
            await self.list()
            return project

    async def _insert(self, project_id: str, name: str, settings: ProjectSettings, folders: Sequence[tuple[Path, FolderSpec]]) -> Project:
        """One project row and its folders, in one transaction.

        The system flag is written to its own column as well as into the settings blob: the column
        exists for the partial unique index over it, and an invariant a query enforces holds even
        when the code that was meant to check it lost a race.
        """
        if not settings.default_env:
            settings = replace(settings, default_env=self.local_env)
        created = datetime.now(UTC)
        made: list[ProjectFolder] = []
        for position, (path, spec) in enumerate(folders):
            env = spec.env or self.local_env
            if env not in ENVIRONMENTS:
                raise ProjectError(f"a folder lives in the container or on the host, not {env!r}")
            made.append(
                ProjectFolder(
                    id=f"f-{uuid.uuid4().hex[:12]}",
                    project_id=project_id,
                    path=path,
                    label=spec.label.strip(),
                    env=env,
                    is_git=env == self.local_env and _detect_git(path),
                    readonly=spec.readonly,
                    position=position,
                    created_at=created,
                    managed=_is_managed(path, self._managed_root),
                )
            )
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO projects(id, name, created_at, settings, system) VALUES (?, ?, ?, ?, ?)",
                (project_id, name, created.isoformat(), json.dumps(settings.dump()), settings.system),
            )
            for folder in made:
                await conn.execute(
                    "INSERT INTO project_folders(id, project_id, path, label, env, is_git, readonly, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (folder.id, project_id, str(folder.path), folder.label, folder.env, int(folder.is_git), int(folder.readonly), folder.position, created.isoformat()),
                )
        return Project(id=project_id, name=name, created_at=created, settings=settings, folders=tuple(made))

    async def ensure_system(self, kind: str, *, name: str, root: Path) -> Project:
        """The installation's own project of this kind, made the first time something needs it.

        Its folder sits under the managed projects root. The overlap check is kept — another project
        that already contains this folder would make containment mean two things at once.

        One per kind, and the database says so: the check and the insert are held under one lock, and
        a partial unique index over the ``system`` column is what makes the invariant survive a lost
        race rather than turn into a second undeletable folder.
        """
        async with self._write:
            existing = await self.system(kind)
            if existing is not None:
                # The row can outlive its folder — an upgrade that wrote the row from the sessions it
                # found, a sweep, a restore of the database without the tree beside it. This is the
                # one call every user of a system project already makes, so it is where the folder is
                # put back rather than at each of them.
                await self.ensure_reachable(existing.primary)
                return existing
            path = Path(os.path.normpath(Path(root).expanduser()))
            await self._refuse_overlap(path)
            self._make_root(path)
            try:
                project = await self._insert(uuid.uuid4().hex[:12], name, ProjectSettings(system=kind), [(path, FolderSpec(str(path)))])
            except sqlite3.IntegrityError:
                # The unique index refused a second project of this kind. Somebody else made it —
                # from another process against the same file, which is the one race the lock above
                # cannot see. Read theirs rather than raising at a caller that only asked for it.
                made = await self.system(kind)
                if made is None:
                    raise
                return made
            await self.list()
        return project

    async def ensure_reachable(self, folder: ProjectFolder) -> bool:
        """Whether the folder can be worked in — making it first when it is one of ours.

        Every guard that refuses to start something in a project asks this instead of reading
        ``reachable`` directly. For a folder under the managed tree the answer is a folder: nothing
        outside this installation was ever going to create it, and a row whose directory is missing
        is a gap in our own bookkeeping, not news for the operator. For a folder they pointed at the
        answer is the plain truth, and the caller raises with the path in it. A folder of the other
        environment is never reachable to this process, whatever its path happens to name here.
        """
        if not folder.local(self.local_env):
            return False
        if folder.managed and not folder.path.is_dir():
            await asyncio.to_thread(self._make_root, folder.path)
        return folder.reachable

    async def ensure_roots(self) -> list[Project]:
        """Read the table, put back any of our own folders that are not on disk, and look again at
        which folders are git repositories.

        Called on the way up, so a run started straight after a start does not have to be the thing
        that discovers the gap. ``is_git`` is refreshed here because a folder becomes a repository
        (or stops being one) without telling anybody, and a worktree for a staff member is only
        offered on a repository.
        """
        projects = await self.list()
        changed = False
        for project in projects:
            for folder in project.local_folders(self.local_env):
                if folder.managed and not folder.path.is_dir():
                    await asyncio.to_thread(self._make_root, folder.path)
                if not folder.reachable:
                    continue
                is_git = await asyncio.to_thread(_detect_git, folder.path)
                if is_git != folder.is_git:
                    await self._db.execute("UPDATE project_folders SET is_git = ? WHERE id = ?", (int(is_git), folder.id))
                    changed = True
        return await self.list() if changed else projects

    def _make_root(self, path: Path) -> None:
        """Create one of our own project folders the way the ones beside it were created.

        Mode and ownership are copied from a sibling under the managed tree, or from the tree itself
        when there is no sibling yet: a folder made by a process running as somebody else would
        otherwise be a workspace the bot cannot write into, which is the same outage wearing a
        different message. Both are best effort — a filesystem that will not take a chown is not a
        reason to leave the folder unmade.
        """
        template = self._template_root(path)
        fresh = [p for p in (path, *path.parents) if self._managed_root is not None and self._managed_root in p.parents and not p.exists()]
        path.mkdir(parents=True, exist_ok=True)
        inbox = path / "inbox"
        if not inbox.exists():
            inbox.mkdir()
            fresh.append(inbox)
        if template is None:
            return
        try:
            stat = template.stat()
        except OSError:
            return
        for made in fresh:
            try:
                os.chmod(made, stat.st_mode & 0o7777)
            except OSError:
                pass
            try:
                os.chown(made, stat.st_uid, stat.st_gid)
            except (OSError, AttributeError):
                pass

    def _template_root(self, path: Path) -> Path | None:
        """A folder already under the managed tree to copy mode and ownership from."""
        root = self._managed_root
        if root is None or not root.is_dir():
            return None
        try:
            siblings = sorted(child for child in root.iterdir() if child.is_dir() and child != path)
        except OSError:
            siblings = []
        return siblings[0] if siblings else root

    async def system(self, kind: str) -> Project | None:
        """The installation's project of this kind if it has been made; ``None`` before it is needed."""
        row = await self._db.fetchone("SELECT id FROM projects WHERE system = ?", (kind,))
        return await self.get(row["id"]) if row is not None else None

    async def summary(self, active: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
        """Per project — and under ``""`` the sessions with no project — how many, how busy, how recently.

        One query over the sessions table and no transcript read at all: the counts are right for an
        installation with more sessions than any one page of the list shows, which is the whole
        reason they are not counted from the rows the app was sent.

        A session the screen draws *inside* another row is not counted: a subagent is listed under
        its leader and a fork under the session it was taken from, so counting them made a folder
        header say "3 agents" over a body that listed two. Whose leader or origin is gone is nobody's
        child and is counted, which is exactly the rule the screen nests by.
        """
        working = set(active)
        out: dict[str, dict[str, Any]] = {}
        rows = await self._db.fetchall("SELECT id, project_id, last_message_at, metadata FROM sessions")
        parsed: list[tuple[Any, dict[str, Any]]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            parsed.append((row, metadata if isinstance(metadata, dict) else {}))
        known = {row["id"] for row, _ in parsed}
        for row, metadata in parsed:
            bucket = out.setdefault(str(row["project_id"]), {"total": 0, "active": 0, "loops": 0, "last_message_at": ""})
            last = str(row["last_message_at"] or "")
            if last > bucket["last_message_at"]:
                bucket["last_message_at"] = last
            bucket["members"] = bucket.get("members", 0) + 1
            if _nested_under(row["id"], metadata, known):
                continue
            bucket["total"] += 1
            if row["id"] in working:
                bucket["active"] += 1
            loop = metadata.get("loop")
            if isinstance(loop, dict) and loop.get("status") == "active":
                bucket["loops"] += 1
        return out

    # -- changing projects -----------------------------------------------------------

    async def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        snapshots: bool | None = None,
        ephemeral: bool | None = None,
        default_env: str | None = None,
    ) -> Project:
        """Change what the operator edits directly. Each field is its own argument so a change to
        one never writes the others back from a stale copy — the orchestrator block in particular
        has its own writers, and a rename must not reset it."""
        project = await self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        label = project.name if name is None else (name or "").strip()
        if not label:
            raise ProjectError("a project needs a name")
        patch: dict[str, Any] = {}
        if snapshots is not None:
            patch["snapshots"] = bool(snapshots)
        if ephemeral is not None:
            patch["ephemeral"] = bool(ephemeral)
        if default_env is not None:
            if default_env not in ENVIRONMENTS:
                raise ProjectError(f"a project runs its agents in the container or on the host, not {default_env!r}")
            patch["default_env"] = default_env
        await self._db.execute(
            "UPDATE projects SET name = ?, settings = json_patch(CASE WHEN json_valid(settings) THEN settings ELSE '{}' END, ?) WHERE id = ?",
            (label, json.dumps(patch), project_id),
        )
        await self.list()
        updated = await self.get(project_id)
        assert updated is not None
        return updated

    async def update_orchestrator(
        self,
        project_id: str,
        *,
        enabled: bool | None = None,
        model: str | None = None,
        autonomy: str | None = None,
        concurrency: int | None = None,
        concurrency_cap: int | None = None,
        telegram_topic_id: int | None = None,
    ) -> Project:
        """Change the orchestrator block, except its session, which only :meth:`set_orchestrator` writes.

        An ephemeral or system project cannot have an orchestrator: the one is removed with its last
        chat and the other is the installation's own. The concurrency stays within the cap.
        """
        project = await self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        current = project.settings.orchestrator
        if enabled and (project.settings.ephemeral or project.settings.system):
            kind = "the installation's own project" if project.settings.system else "a chat's own scratch project"
            raise ProjectError(f"{project.name} is {kind} and cannot have an orchestrator; keep it as a project first")
        if autonomy is not None and autonomy not in AUTONOMY:
            raise ProjectError(f"autonomy is one of {', '.join(AUTONOMY)}, not {autonomy!r}")
        cap = current.concurrency_cap if concurrency_cap is None else int(concurrency_cap)
        if cap < 1:
            raise ProjectError("the concurrency cap is at least one")
        wanted = current.concurrency if concurrency is None else int(concurrency)
        if concurrency is not None and not 1 <= wanted <= cap:
            raise ProjectError(f"concurrency is between 1 and the cap of {cap}, not {wanted}")
        patch: dict[str, Any] = {"concurrency_cap": cap, "concurrency": min(wanted, cap)}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if model is not None:
            patch["model"] = model.strip()
        if autonomy is not None:
            patch["autonomy"] = autonomy
        if telegram_topic_id is not None:
            patch["telegram_topic_id"] = int(telegram_topic_id)
        await self._db.execute(
            "UPDATE projects SET settings = json_patch(settings, ?) WHERE id = ?",
            (json.dumps({"orchestrator": patch}), project_id),
        )
        updated = await self.get(project_id)
        assert updated is not None
        return updated

    async def set_orchestrator(self, project_id: str, *, expect: str, value: str) -> bool:
        """Point the project at a new orchestrator session if it still points at ``expect``; whether this call won.

        A compare-and-set in one statement: two starts, or a start and a replacement, that both read
        the old value cannot both write, and the loser learns it lost instead of running a second
        orchestrator nobody can see.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE projects SET settings = json_set(settings, '$.orchestrator.session_id', ?) "
                "WHERE id = ? AND COALESCE(json_extract(settings, '$.orchestrator.session_id'), '') = ?",
                (value, project_id, expect),
            )
            won = cursor.rowcount == 1
            await cursor.close()
        return won

    async def set_setup(self, project_id: str, by: str, *, expect: str | None = None) -> bool:
        """Mark a project as being set up by ``by`` (empty: the setup is over). With ``expect``, only
        when it is still marked that way; whether the row changed."""
        if expect is None:
            sql, params = "UPDATE projects SET setup_by = ? WHERE id = ?", (by, project_id)
        else:
            sql, params = "UPDATE projects SET setup_by = ? WHERE id = ? AND setup_by = ?", (by, project_id, expect)
        async with self._db.transaction() as conn:
            cursor = await conn.execute(sql, params)
            changed = cursor.rowcount == 1
            await cursor.close()
        return changed

    async def delete(self, project_id: str) -> None:
        """Forget an empty project, with its folders, brief and journal. Nothing on disk is touched."""
        project = await self.get(project_id)
        if project is not None and project.settings.system:
            raise ProjectError(f"{project.name} is the installation's own project and cannot be removed")
        if await self.sessions_of(project_id):
            raise ProjectError(f"{project.name if project else 'this project'} still has sessions")
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self.list()

    # -- folders ---------------------------------------------------------------------

    async def add_folder(self, project_id: str, path: str, *, label: str = "", env: str | None = None, readonly: bool = False) -> ProjectFolder:
        """Another folder for a project, last in its order. The same rules as the first one: absolute,
        none of the installation's own, and nesting with no other folder anywhere — this project's
        included, since two folders of one project that nest would make "read-only" mean one thing
        through one path and another through the other."""
        target = normalise_root(path)
        self._refuse_reserved(target)
        folder_env = env or self.local_env
        if folder_env not in ENVIRONMENTS:
            raise ProjectError(f"a folder lives in the container or on the host, not {folder_env!r}")
        async with self._write:
            project = await self.get(project_id)
            if project is None:
                raise KeyError(project_id)
            await self._refuse_overlap(target, project_id)
            folder = ProjectFolder(
                id=f"f-{uuid.uuid4().hex[:12]}",
                project_id=project_id,
                path=target,
                label=label.strip(),
                env=folder_env,
                is_git=folder_env == self.local_env and _detect_git(target),
                readonly=readonly,
                position=len(project.folders),
                created_at=datetime.now(UTC),
                managed=_is_managed(target, self._managed_root),
            )
            await self._db.execute(
                "INSERT INTO project_folders(id, project_id, path, label, env, is_git, readonly, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (folder.id, project_id, str(target), folder.label, folder.env, int(folder.is_git), int(folder.readonly), folder.position, folder.created_at.isoformat()),
            )
            await self.list()
        await self.record(project_id, "system", "folder", f"added the folder {target} ({folder_env}{', read-only' if readonly else ''})", {"folder_id": folder.id})
        return folder

    async def update_folder(self, project_id: str, folder_id: str, *, label: str | None = None, readonly: bool | None = None, position: int | None = None) -> ProjectFolder:
        """Rename, lock or move a folder. Moving one to position 0 makes it the primary."""
        async with self._write:
            project = await self.get(project_id)
            folder = project.folder(folder_id) if project is not None else None
            if project is None or folder is None:
                raise KeyError(folder_id)
            async with self._db.transaction() as conn:
                if label is not None or readonly is not None:
                    await conn.execute(
                        "UPDATE project_folders SET label = ?, readonly = ? WHERE id = ?",
                        (folder.label if label is None else label.strip(), int(folder.readonly if readonly is None else readonly), folder_id),
                    )
                if position is not None:
                    order = [f for f in project.folders if f.id != folder_id]
                    order.insert(max(0, min(int(position), len(order))), folder)
                    for index, each in enumerate(order):
                        await conn.execute("UPDATE project_folders SET position = ? WHERE id = ?", (index, each.id))
            await self.list()
            updated = (await self.get(project_id)).folder(folder_id)  # type: ignore[union-attr]
        assert updated is not None
        changes = [
            *([f"label {updated.label!r}"] if label is not None and updated.label != folder.label else []),
            *(["read-only" if updated.readonly else "writable"] if readonly is not None and updated.readonly != folder.readonly else []),
            *([f"position {updated.position}"] if position is not None and updated.position != folder.position else []),
        ]
        if changes:
            await self.record(project_id, "system", "folder", f"changed the folder {updated.path}: {', '.join(changes)}", {"folder_id": folder_id})
        return updated

    async def remove_folder(self, project_id: str, folder_id: str) -> None:
        """Forget a folder. Nothing on disk is touched. The last folder stays: a project is somewhere.

        Whether a session still works in the folder is the caller's to refuse, because only the
        caller knows which sessions are loaded and where they are.
        """
        async with self._write:
            project = await self.get(project_id)
            folder = project.folder(folder_id) if project is not None else None
            if project is None or folder is None:
                raise KeyError(folder_id)
            if len(project.folders) == 1:
                raise ProjectError(f"{folder.path} is the only folder of {project.name}; a project needs at least one")
            async with self._db.transaction() as conn:
                await conn.execute("DELETE FROM project_folders WHERE id = ?", (folder_id,))
                for index, each in enumerate(f for f in project.folders if f.id != folder_id):
                    await conn.execute("UPDATE project_folders SET position = ? WHERE id = ?", (index, each.id))
            await self.list()
        await self.record(project_id, "system", "folder", f"removed the folder {folder.path}", {"folder_id": folder_id})

    def _refuse_reserved(self, path: Path) -> None:
        """The folders that are the installation's, or are the whole of the operator's home.

        A project folder is inside the wall as well as outside it: every path under it is reachable
        to the agents of that project, and it is in the policy's open roots, which is what makes the
        home-folder question stop being asked for anything under it. So the state directory, the
        secrets and the two checkouts are refused in both directions — as the folder, above it and
        below it. The managed projects root is refused as a whole but its children are exactly where
        automatic and adopted projects belong. The home folder is refused as a whole while any
        folder inside it stays the ordinary case.
        """
        if self._home is not None and path == self._home:
            raise ProjectError(f"{path} is your home folder; a project is a folder inside it, not the whole of it")
        for reserved in self._reserved:
            if path == reserved:
                raise ProjectError(f"{path} belongs to the installation itself and cannot be a project")
            if self._managed_root is not None and reserved == self._managed_root:
                if path in reserved.parents:
                    raise ProjectError(f"{path} contains {reserved}, which belongs to the installation itself")
                continue
            if reserved in path.parents:
                raise ProjectError(f"{path} is inside {reserved}, which belongs to the installation itself")
            if path in reserved.parents:
                raise ProjectError(f"{path} contains {reserved}, which belongs to the installation itself")

    @staticmethod
    def _refuse_among(paths: Iterable[Path]) -> None:
        """The folders asked for in one request may not nest among themselves either."""
        seen: list[Path] = []
        for path in paths:
            for other in seen:
                if other == path:
                    raise ProjectError(f"{path} is named twice")
                if other in path.parents or path in other.parents:
                    raise ProjectError(f"{path} and {other} nest; a project's folders are side by side")
            seen.append(path)

    async def _refuse_overlap(self, path: Path, project_id: str | None = None) -> None:
        """No two folders anywhere may nest, because containment would then mean two different
        things at once; the same folder may belong to several projects, but only once to each.

        A session in the outer folder may write anywhere in the inner one while the inner folder's
        own sessions may not see out — a boundary that holds in one direction is not a boundary. The
        same folder in two projects has no such asymmetry: it is a reference to one place on disk,
        each project's staff work in worktrees of their own, and each project's rules (read-only
        included) bind that project's sessions. Refusing it once left an operator unable to give a
        second project a repository it plainly worked on, with the refusal reaching nobody. The rule
        holds across environments too: a path is compared as written, and the same absolute path is
        how both environments name a folder mounted into the container.
        """
        rows = await self._db.fetchall("SELECT f.path, f.project_id, p.name FROM project_folders f JOIN projects p ON p.id = f.project_id")
        for row in rows:
            other = Path(row["path"])
            if other == path:
                if row["project_id"] == project_id:
                    raise ProjectError(f"{row['name']} already has the folder {path}")
                continue
            if path in other.parents:
                raise ProjectError(f"that folder contains the project {row['name']} ({other})")
            if other in path.parents:
                raise ProjectError(f"that folder is inside the project {row['name']} ({other})")

    # -- the brief and the journal ---------------------------------------------------

    async def brief(self, project_id: str) -> dict[str, BriefSection]:
        """Every section of the brief, in order; one never written is there, empty."""
        rows = await self._db.fetchall("SELECT section, body, updated_at, updated_by FROM project_briefs WHERE project_id = ?", (project_id,))
        written = {r["section"]: BriefSection(r["section"], r["body"], r["updated_at"], r["updated_by"]) for r in rows}
        return {section: written.get(section, BriefSection(section)) for section in BRIEF_SECTIONS}

    async def set_brief(self, project_id: str, section: str, body: str, by: str) -> BriefSection:
        if section not in BRIEF_SECTIONS:
            raise ProjectError(f"the brief has the sections {', '.join(BRIEF_SECTIONS)}, not {section!r}")
        if by not in BRIEF_AUTHORS:
            raise ProjectError(f"a brief is written by the operator, the orchestrator or the system, not {by!r}")
        if section in OPERATOR_ONLY_SECTIONS and by != "operator":
            raise ProjectError(f"only the operator writes {section}: it bounds what may be granted without asking them")
        if await self.get(project_id) is None:
            raise KeyError(project_id)
        at = _now()
        await self._db.execute(
            "INSERT INTO project_briefs(project_id, section, body, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, section) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (project_id, section, body, at, by),
        )
        return BriefSection(section, body, at, by)

    async def record(self, project_id: str, author: str, kind: str, text: str, refs: dict[str, Any] | None = None) -> JournalEntry:
        """Write one journal entry. The host writes the system ones itself — every grant, hire,
        folder change and merge — so the record does not depend on a model remembering to."""
        if author not in JOURNAL_AUTHORS:
            raise ProjectError(f"a journal entry is by the orchestrator, the operator or the system, not {author!r}")
        at = _now()
        refs = dict(refs or {})
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO project_journal(project_id, at, author, kind, text, refs_json) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, at, author, kind, text, json.dumps(refs)),
            )
            entry_id = int(cursor.lastrowid or 0)
            await cursor.close()
        return JournalEntry(entry_id, project_id, at, author, kind, text, refs)

    async def journal(self, project_id: str, *, before: int | None = None, limit: int = 50, kind: str | None = None) -> list[JournalEntry]:
        """Newest first, one page; ``before`` is the id of the oldest entry already shown, ``kind``
        keeps only the entries of that kind."""
        limit = max(1, min(int(limit), JOURNAL_PAGE_MAX))
        where, args = "project_id = ?", [project_id]
        if before is not None:
            where, args = where + " AND id < ?", [*args, int(before)]
        if kind:
            where, args = where + " AND kind = ?", [*args, kind]
        rows = await self._db.fetchall(f"SELECT * FROM project_journal WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit))  # noqa: S608 — the clauses are this method's own
        return [_entry(r) for r in rows]

    async def rules(self, project_id: str) -> list[JournalEntry]:
        """The operator's rules in force, oldest first: every rule no lifting names.

        Lifting is an entry of its own rather than a mark on the rule, so the journal stays a record
        of what happened, in order, and needs no column for it."""
        rows = await self._db.fetchall(
            "SELECT * FROM project_journal WHERE project_id = ? AND kind = ? AND id NOT IN ("
            "SELECT CAST(json_extract(refs_json, '$.rule_id') AS INTEGER) FROM project_journal WHERE project_id = ? AND kind = ?"
            ") ORDER BY id",
            (project_id, RULE_KIND, project_id, RULE_LIFTED_KIND),
        )
        return [_entry(r) for r in rows]

    async def lifted_rules(self, project_id: str) -> set[int]:
        rows = await self._db.fetchall("SELECT json_extract(refs_json, '$.rule_id') AS rule_id FROM project_journal WHERE project_id = ? AND kind = ?", (project_id, RULE_LIFTED_KIND))
        return {int(r["rule_id"]) for r in rows if r["rule_id"] is not None}

    async def add_rule(self, project_id: str, author: str, text: str) -> JournalEntry:
        """Write a standing rule, within the bounds that keep every rule in force on show."""
        body = (text or "").strip()
        if not body:
            raise ProjectError("a rule needs its text")
        if len(body) > RULE_TEXT_MAX:
            raise ProjectError(f"a rule is at most {RULE_TEXT_MAX} characters: say the instruction, and keep the detail in the brief or a note")
        standing = await self.rules(project_id)
        if len(standing) >= RULES_MAX:
            raise ProjectError(f"{len(standing)} rules are in force already, the most there may be: lift one, or fold two into one, first")
        if sum(len(r.text) for r in standing) + len(body) > RULES_CHARS:
            raise ProjectError(f"the rules in force would come to more than {RULES_CHARS} characters with this one: lift one, or say them shorter, first")
        return await self.record(project_id, author, RULE_KIND, body)

    async def lift_rule(self, project_id: str, rule_id: int, author: str, why: str = "") -> tuple[JournalEntry, JournalEntry]:
        """Take a rule out of force; the rule and the entry that lifts it."""
        row = await self._db.fetchone("SELECT * FROM project_journal WHERE id = ? AND project_id = ?", (int(rule_id), project_id))
        if row is None or row["kind"] != RULE_KIND:
            raise ProjectError(f"entry #{rule_id} is not a rule of this project")
        rule = _entry(row)
        if rule.id in await self.lifted_rules(project_id):
            raise ProjectError(f"rule #{rule.id} was lifted already")
        said = " ".join(rule.text.split())
        text = f"Lifted rule #{rule.id}: {said[:200]}{'…' if len(said) > 200 else ''}" + (f"\nWhy: {why.strip()}" if why.strip() else "")
        return rule, await self.record(project_id, author, RULE_LIFTED_KIND, text, {"rule_id": rule.id})

    # -- the link to sessions ------------------------------------------------------

    async def for_session(self, session_id: str) -> Project | None:
        row = await self._db.fetchone("SELECT project_id FROM sessions WHERE id = ?", (session_id,))
        pid = row["project_id"] if row is not None else None
        return await self.get(pid) if pid else None

    async def attach(self, session_id: str, project_id: str) -> None:
        await self._db.execute("UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id))

    async def sessions_of(self, project_id: str) -> list[dict[str, str]]:
        rows = await self._db.fetchall("SELECT id, title FROM sessions WHERE project_id = ? ORDER BY last_message_at DESC", (project_id,))
        return [{"id": r["id"], "title": r["title"]} for r in rows]

    async def sessions_in_folder(self, project_id: str, folder_id: str) -> Sequence[dict[str, str]]:
        """The sessions that work in this folder: those that name it, and, when it is the primary,
        those that name none — a session without a ``folder_id`` works in whichever folder is first,
        so removing or demoting the primary moves it as surely as removing the folder it names."""
        project = await self.get(project_id)
        if project is None or project.folder(folder_id) is None:
            return []
        primary = project.primary.id == folder_id
        return [{"id": sid, "title": title} for sid, title, named in await self._named_folders(project_id) if named == folder_id or (primary and not named)]

    async def sessions_by_default(self, project_id: str) -> Sequence[dict[str, str]]:
        """The sessions that name no folder and so work in the primary, whichever folder that is."""
        return [{"id": sid, "title": title} for sid, title, named in await self._named_folders(project_id) if not named]

    async def _named_folders(self, project_id: str) -> Sequence[tuple[str, str, str]]:
        """``(session id, title, the folder it names or "")`` for every session of the project, newest first."""
        rows = await self._db.fetchall("SELECT id, title, metadata FROM sessions WHERE project_id = ? ORDER BY last_message_at DESC", (project_id,))
        out = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            named = str(metadata.get("folder_id") or "") if isinstance(metadata, dict) else ""
            out.append((row["id"], row["title"], named))
        return tuple(out)

    async def by_session(self) -> dict[str, str]:
        """``{session_id: project_id}`` for every session that has one, in one query."""
        rows = await self._db.fetchall("SELECT id, project_id FROM sessions")
        return {r["id"]: r["project_id"] for r in rows}


__all__ = [
    "BRIEF_SECTIONS",
    "BriefSection",
    "FolderSpec",
    "JournalEntry",
    "OrchestratorSettings",
    "Project",
    "ProjectError",
    "ProjectFolder",
    "ProjectSettings",
    "ProjectStore",
    "RULES_CHARS",
    "RULES_MAX",
    "RULE_KIND",
    "RULE_LIFTED_KIND",
    "RULE_TEXT_MAX",
    "normalise_root",
]
