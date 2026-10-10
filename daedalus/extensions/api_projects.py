"""The project routes: projects, their folders, their brief and their journal.

They live in a module of their own so the features built on projects (staff, the board of a project,
its orchestrator) add their routes here rather than growing ``api.py`` further. Like ``api.py`` this
module may import the HTTP framework; nothing below the extensions may.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions import questions, wakeups
from daedalus.extensions.coordinator_handoff import CoordinatorHandoff
from daedalus.extensions.project_commands import ProjectCommands
from daedalus.extensions.project_usage import ProjectUsage
from daedalus.extensions.watch_authority import status as watch_authority_status
from daedalus.extensions.watch_commands import WatchCommands
from daedalus.extensions.watches import WatchRefused
from daedalus.host import prompts
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.projects import RULE_KIND, FolderSpec, Project, ProjectError, ProjectFolder, ProjectSettings

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

JOURNAL_NOTE_MAX_CHARS = 4000
"""An operator's note is a sentence or a paragraph; the journal is read as a list, not as documents."""
BRIEF_SECTION_MAX_CHARS = 20000
"""A brief section is re-read into the orchestrator's prompt every turn, so it is bounded."""

Env = Literal["container", "host"]


class WatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: dict[str, Any]
    then: dict[str, Any]
    cooldown_minutes: float = 10
    once: bool = False
    note: str = ""
    deadline_at: str | None = None
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class WatchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    note: str | None = None
    cooldown_minutes: float | None = None
    when: dict[str, Any] | None = None
    then: dict[str, Any] | None = None
    deadline_at: str | None = None
    expected_entity_revision: int = Field(ge=1)
    expected_condition_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class WatchDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    expected_condition_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class WakeupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str
    at: str | None = None
    """ISO 8601; without an offset it is the operator's own time."""
    in_minutes: int | None = None
    cron: str | None = None
    """In UTC, like every schedule."""
    expires_at: str
    expected_collection_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class WakeupDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_collection_revision: int = Field(ge=1, strict=True)
    expected_schedule_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)


class FolderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str = ""
    env: Env | None = None
    """Empty is the environment this process runs in."""
    readonly: bool = False


class MutationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class FolderCommandBody(FolderBody, MutationBody):
    pass


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    folders: list[FolderBody] = Field(default_factory=list)
    """In order, the first the primary. None makes a scratch folder of the installation's own."""
    default_env: Env | None = None
    snapshots: bool | None = None
    """None is the default for the folder: on for a new folder, off for one the operator points at."""
    folder_name: str = Field(default="", max_length=80)
    """The new folder's name under the workspaces when ``folders`` is empty; empty is the project's id."""


class ProjectPatch(MutationBody):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    snapshots: bool | None = None
    default_env: Env | None = None
    setup_command: str | None = Field(default=None, max_length=2000)
    keep: Literal[True] | None = None
    """Keep a chat's scratch project as a project of its own. There is no way back: an ephemeral
    project is one made implicitly, and nothing the operator does makes one."""
    archived: bool | None = None
    """Archive (true) or restore (false): hidden from the lists and never woken, nothing deleted."""


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool


class FolderPatch(MutationBody):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    readonly: bool | None = None
    position: int | None = Field(default=None, ge=0)


class BriefBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str
    body: str = Field(max_length=BRIEF_SECTION_MAX_CHARS)


class JournalNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=JOURNAL_NOTE_MAX_CHARS)
    kind: Literal["note", "rule"] = "note"
    """A note, or a standing rule the orchestrator and every member are shown until it is lifted."""


class RuleLift(BaseModel):
    model_config = ConfigDict(extra="forbid")

    why: str = Field(default="", max_length=1000)


class OrchestratorBody(BaseModel):
    """Switching a project's orchestrator on. Each field left out keeps what the project has, and a
    project that never had one starts from the installation's defaults (Settings)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=200)
    """A model preset id; empty is the Settings default for project orchestrators."""
    autonomy: Literal["ask", "normal", "full"] | None = None
    concurrency_cap: int | None = Field(default=None, ge=1)


class OrchestratorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=200)
    autonomy: Literal["ask", "normal", "full"] | None = None
    concurrency: int | None = Field(default=None, ge=1)
    concurrency_cap: int | None = Field(default=None, ge=1)


class ReplaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=300)
    client_operation_id: str = Field(min_length=1, max_length=160)
    expected_entity_revision: int = Field(ge=1)
    expected_coordinator_session_id: str = Field(min_length=1, max_length=200)


class MainReplaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=300)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class AnswerItem(BaseModel):
    """One answer of a batch: an option (several where the question allows), words of the operator's
    own, a note beside a chosen option, or allow/deny for a permission."""

    model_config = ConfigDict(extra="forbid")

    ask_id: str = Field(min_length=1, max_length=64)
    selected: list[str] | None = None
    text: str | None = None
    note: str | None = None
    allow: bool | None = None
    always: bool = False
    # "Always" for every tool of the asked tool's MCP server. The Questions list sends it with the
    # server-wide choice, and a model without it refused the whole send as extra input.
    server: bool = False


def host_bridge(settings: Any, terminals: Any = None) -> bool:
    """Whether a terminal daemon answers on the host, so a host folder can be worked in by anything.

    With the terminals service running, its live connection is the answer: a daemon that was killed
    leaves its ``endpoint`` behind, and a folder accepted on the strength of that file would be a row
    nothing could open. Without the service (the doctor, a test) the directory is read at the moment
    of asking, because the bridge is installed and removed while this process runs, and the directory
    is mounted either way; an empty or missing one is a bridge that is not installed.
    """
    if terminals is not None and terminals.configured("host"):
        return bool(terminals.available("host"))
    directory = getattr(settings, "terminals_host_dir", None)
    if not directory:
        return False
    return (Path(directory) / "endpoint").is_file()


def environments(settings: Any, local_env: str, terminals: Any = None) -> dict[str, Any]:
    """Which environments a folder of this installation may live in.

    Natively the process is on the host and there is no container. In Docker the container is where
    the process is, and the host is reachable only through its terminal bridge, and only by what runs
    in a terminal: a folder there without a bridge would be a row nothing could ever open.
    """
    bridge = host_bridge(settings, terminals)
    available = [local_env] + (["host"] if local_env == "container" and bridge else [])
    # ``host_configured`` tells a bridge that is installed but not answering from one never installed:
    # the folder browser keeps its host tab for the first and says how to start it, rather than
    # hiding the machine whenever the daemon has stopped.
    configured = bool(terminals is not None and terminals.configured("host")) or bridge
    workspaces = getattr(settings, "workspaces_dir", None)
    return {"local": local_env, "available": available, "host_bridge": bridge, "docker": local_env == "container",
            "host_configured": local_env == "container" and configured,
            "workspaces_root": str(workspaces) if workspaces else "", "home": str(Path.home())}


def reach(folder: ProjectFolder, local_env: str, bridge: bool) -> str:
    """Who can work in a folder: ``agents`` (every agent, this process's own tools included),
    ``terminals`` (everything, but through the host terminal daemon: CLI staff and the operator's
    shells in host terminals, and a Daedalus agent whose commands and file tools the daemon runs), or
    ``none``. Whether the folder is mounted yet is ``reachable``; this is whether it ever can be."""
    if folder.local(local_env):
        return "agents"
    if folder.env == "host" and bridge:
        return "terminals"
    return "none"


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    settings = app.settings

    def view(project: Project, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        bridge = host_bridge(settings, app.extensions.get("terminals"))
        body = project.view()
        for folder_view, folder in zip(body["folders"], project.folders, strict=True):
            folder_view["reach"] = reach(folder, manager.projects.local_env, bridge)
        return {**body, "sessions": sessions}

    async def sessions_of(project_id: str) -> list[dict[str, Any]]:
        # The narrower question on purpose: a session whose run has ended and whose snapshot is
        # still being written may not be rewritten, but it is not working, and a list that draws it
        # as running contradicts its own screen — which says idle, because it is.
        busy = manager.active_sessions()
        return [{**s, "running": s["id"] in busy} for s in await manager.projects.sessions_of(project_id)]

    async def existing(project_id: str) -> Project:
        project = await manager.projects.get(project_id)
        if project is None:
            raise HTTPException(404, "no such project")
        return project

    def refuse_env(env: str | None) -> None:
        """A folder or a default in an environment this installation cannot reach is refused at the door."""
        if env is None:
            return
        offered = environments(settings, manager.projects.local_env, app.extensions.get("terminals"))
        if env in offered["available"]:
            return
        if env == "host":
            raise HTTPException(400, "a host folder needs the host terminal bridge, which is not installed here; install it, or add the folder to the container")
        raise HTTPException(400, "this installation runs on the host without a container; a folder here is a host folder")

    async def reload_command_project(project_id: str) -> None:
        await manager.reload_project(await manager.projects.get(project_id), project_id)

    def project_commands() -> ProjectCommands:
        return ProjectCommands(app.db, manager.projects, manager.bus, refuse_env, reload_command_project)

    def conflict(exc: ControlConflict) -> HTTPException:
        detail: Any = str(exc) if exc.current_revision is None else {
            "reason": str(exc), "current_revision": exc.current_revision,
        }
        return HTTPException(409, detail)

    @api.get("/api/projects")
    async def list_projects(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        """Every project, with who works in it and, per folder, whether this process can reach it.

        ``reachable`` is the Docker seam: the row exists as soon as the operator adds the folder, but
        in a container the folder is only there once it is bind-mounted, so the app can say "restart
        to mount this" instead of showing a project whose files are mysteriously absent. ``reach``
        is who could ever work there.
        """
        return [view(project, await sessions_of(project.id)) for project in await manager.projects.list()]

    @api.get("/api/project-environments")
    async def project_environments(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Where a folder may live, for the environment choice when a folder is added."""
        return environments(settings, manager.projects.local_env, app.extensions.get("terminals"))

    @api.post("/api/projects")
    async def create_project(body: ProjectBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        for folder in body.folders:
            refuse_env(folder.env)
        refuse_env(body.default_env)
        specs = [FolderSpec(f.path, label=f.label, env=f.env, readonly=f.readonly) for f in body.folders]
        # A scratch folder begins empty, so undo costs nothing there; a folder the operator points at
        # may be a large repository, where it is theirs to switch on. Either way an explicit choice
        # from the new-project dialog wins. A project made here is never ephemeral: the operator made
        # it on purpose, so it is a project at once, even before it has a chat.
        snapshots = body.snapshots if body.snapshots is not None else not specs
        project_settings = ProjectSettings(snapshots=snapshots, default_env=body.default_env or "", ephemeral=False)
        try:
            project = await manager.projects.create(body.name, specs or None, settings=project_settings,
                                                    managed_name=body.folder_name)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await manager.bus.publish("project.changed", {"change": "created", "actor": "operator"}, project_id=project.id)
        return view(project, [])

    @api.patch("/api/projects/{project_id}")
    async def patch_project(project_id: str, body: ProjectPatch, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            response = await project_commands().settings(
                Principal.operator(who), project_id, client_operation_id=body.client_operation_id,
                expected_entity_revision=body.expected_entity_revision, name=body.name,
                snapshots=body.snapshots, default_env=body.default_env, keep=bool(body.keep),
                setup_command=body.setup_command, archived=body.archived,
            )
        except ControlConflict as exc:
            raise conflict(exc) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return response

    @api.put("/api/projects/{project_id}/pin")
    async def pin_project(project_id: str, body: PinBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Pin a project, or a chat by its scratch project, to the top of the sidebar, or unpin it.

        Its own address rather than a field of the settings write: that write is revisioned and
        journalled, and a pin is a view preference toggled from a list, which must neither be refused
        because the project was renamed elsewhere nor make an open settings form stale. The event tells the other screens to read the
        listing again, so a pin made on the phone appears on the desktop without a reload.
        """
        await existing(project_id)
        pinned_at = await manager.projects.pin(project_id, body.pinned)
        await manager.bus.publish("project.changed", {"change": "pinned", "actor": "operator"}, project_id=project_id)
        return {"project_id": project_id, "pinned_at": pinned_at}

    @api.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget an empty project. Files are never deleted."""
        project = await existing(project_id)
        sessions = await manager.projects.sessions_of(project_id)
        if sessions:
            one = len(sessions) == 1
            raise HTTPException(409, f"{len(sessions)} agent{'' if one else 's'} {'works' if one else 'work'} in {project.name}; move or remove {'it' if one else 'them'} first")
        # What the project itself holds (its terminals) ends before the row goes, so nothing is left
        # running for an owner that no longer exists.
        for hook in manager.project_delete_hooks:
            try:
                await hook(project_id)
            except Exception:  # noqa: BLE001 — a hook that fails must not keep the project
                logger.exception("project delete hook failed for %s", project_id)
        try:
            await manager.projects.delete(project_id)
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc
        await manager.reload_project(None, project_id)
        await manager.bus.publish("project.changed", {"change": "removed", "actor": "operator"}, project_id=project_id)
        return {"ok": True}

    # -- folders -----------------------------------------------------------------------

    @api.post("/api/projects/{project_id}/folders")
    async def add_folder(project_id: str, body: FolderCommandBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Another folder for the project. The answer is the whole project, so the new folder's
        ``reach`` and ``reachable`` say at once who can work in it and whether it is mounted yet."""
        await existing(project_id)
        try:
            response = await project_commands().add_folder(
                Principal.operator(who), project_id, client_operation_id=body.client_operation_id,
                expected_entity_revision=body.expected_entity_revision, path=body.path,
                label=body.label, env=body.env, readonly=body.readonly,
            )
        except ControlConflict as exc:
            raise conflict(exc) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return response

    @api.patch("/api/projects/{project_id}/folders/{folder_id}")
    async def patch_folder(project_id: str, folder_id: str, body: FolderPatch, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            response = await project_commands().change_folder(
                Principal.operator(who), project_id, folder_id, client_operation_id=body.client_operation_id,
                expected_entity_revision=body.expected_entity_revision, label=body.label,
                readonly=body.readonly, position=body.position,
            )
        except ControlConflict as exc:
            raise conflict(exc) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc
        return response

    @api.delete("/api/projects/{project_id}/folders/{folder_id}")
    async def remove_folder(project_id: str, folder_id: str, body: MutationBody,
                            who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a folder; nothing on disk is touched. Refused, by name, while a session works in it."""
        await existing(project_id)
        try:
            response = await project_commands().remove_folder(
                Principal.operator(who), project_id, folder_id, client_operation_id=body.client_operation_id,
                expected_entity_revision=body.expected_entity_revision,
            )
        except ControlConflict as exc:
            raise conflict(exc) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "no such folder in this project") from exc
        return response

    # -- the brief and the journal -----------------------------------------------------

    @api.get("/api/projects/{project_id}/brief")
    async def get_brief(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        sections = await manager.projects.brief(project_id)
        return {"sections": [{"section": s.section, "body": s.body, "updated_at": s.updated_at, "updated_by": s.updated_by} for s in sections.values()]}

    @api.put("/api/projects/{project_id}/brief")
    async def put_brief(project_id: str, body: BriefBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator writes a section. Every section is theirs to write, including the one that
        bounds what may be granted without asking them."""
        await existing(project_id)
        try:
            written = await manager.projects.set_brief(project_id, body.section, body.body, "operator")
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await manager.bus.publish("project.changed", {"change": "brief", "actor": "operator"}, project_id=project_id)
        return {"section": written.section, "body": written.body, "updated_at": written.updated_at, "updated_by": written.updated_by}

    @api.get("/api/projects/{project_id}/journal")
    async def get_journal(project_id: str, before: int | None = None, limit: int = 50, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Newest first, one page; ``next_before`` is what to ask with for the page after, or null at the end."""
        await existing(project_id)
        entries = await manager.projects.journal(project_id, before=before, limit=limit)
        wanted = max(1, min(int(limit), 200))
        lifted = await manager.projects.lifted_rules(project_id)
        views = [{**e.view(), "lifted": True} if e.kind == RULE_KIND and e.id in lifted else e.view() for e in entries]
        # The rules in force ride with every page: the page shows them pinned above the entries,
        # whichever page of the journal the entries are.
        rules = [r.view() for r in await manager.projects.rules(project_id)]
        return {"entries": views, "next_before": entries[-1].id if len(entries) == wanted else None, "rules": rules}

    async def tell_the_team(project_id: str, text: str) -> None:
        team = app.extensions.get("staff")
        if team is not None:
            await team.announce_rule(project_id, text, by="operator")

    @api.post("/api/projects/{project_id}/journal")
    async def post_journal(project_id: str, body: JournalNote, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "a note needs some text")
        if body.kind == RULE_KIND:
            try:
                entry = await manager.projects.add_rule(project_id, "operator", text)
            except ProjectError as exc:
                raise HTTPException(400, str(exc)) from exc
            await tell_the_team(project_id, prompts.STAFF_RULE_ADDED.format(text=entry.text))
        else:
            entry = await manager.projects.record(project_id, "operator", "note", text)
        await manager.bus.publish("project.changed", {"change": "journal", "actor": "operator"}, project_id=project_id)
        return entry.view()

    @api.post("/api/projects/{project_id}/journal/{rule_id}/lift")
    async def lift_rule(project_id: str, rule_id: int, body: RuleLift, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            rule, entry = await manager.projects.lift_rule(project_id, rule_id, "operator", body.why)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await tell_the_team(project_id, prompts.STAFF_RULE_LIFTED.format(text=rule.text))
        await manager.bus.publish("project.changed", {"change": "journal", "actor": "operator"}, project_id=project_id)
        return entry.view()

    # -- the orchestrator ----------------------------------------------------------------

    def orchestrators() -> Any:
        found = app.extensions.get("orchestrator")
        if found is None:
            raise HTTPException(503, "orchestrators are not available on this installation")
        return found

    def office(project: Project) -> dict[str, Any]:
        orchestrator = project.settings.orchestrator
        return {**orchestrator.dump(), "effective_model": orchestrators().model_of(project), "project_id": project.id}

    @api.post("/api/projects/{project_id}/orchestrator/preflight")
    async def preflight_orchestrator(project_id: str, body: OrchestratorBody, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        """Check the first coordinator call before the operator creates its session."""
        await existing(project_id)
        try:
            selected = await orchestrators().preflight_enable(project_id, model=body.model,
                                                                concurrency_cap=body.concurrency_cap)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"effective_model": selected}

    @api.post("/api/projects/{project_id}/orchestrator")
    async def enable_orchestrator(project_id: str, body: OrchestratorBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Switch the orchestrator on; a project that has one already only changes what is sent."""
        await existing(project_id)
        try:
            project = await orchestrators().enable(project_id, model=body.model, autonomy=body.autonomy, concurrency_cap=body.concurrency_cap)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return office(project)

    @api.patch("/api/projects/{project_id}/orchestrator")
    async def patch_orchestrator(project_id: str, body: OrchestratorPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            project = await orchestrators().update(project_id, model=body.model, autonomy=body.autonomy, concurrency=body.concurrency, concurrency_cap=body.concurrency_cap)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return office(project)

    @api.delete("/api/projects/{project_id}/orchestrator")
    async def disable_orchestrator(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Switch it off. Its chat stays, with its history; what it was asked goes to the operator."""
        await existing(project_id)
        return office(await orchestrators().disable(project_id))

    @api.post("/api/projects/{project_id}/orchestrator/replace")
    async def replace_orchestrator(project_id: str, body: ReplaceBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Prepare the successor and commit an exact receipted office handoff."""
        await existing(project_id)
        try:
            return await orchestrators().replace_command(
                project_id, body.reason, principal=Principal.operator(who),
                client_operation_id=body.client_operation_id,
                expected_entity_revision=body.expected_entity_revision,
                expected_coordinator_session_id=body.expected_coordinator_session_id,
            )
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ProjectError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/projects/{project_id}/orchestrator/replace")
    async def replacement_status(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)

        return {"handoff": await CoordinatorHandoff(orchestrators()).view(project_id)}

    @api.get("/api/projects/{project_id}/state")
    async def project_state(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The state block the orchestrator reads at the start of each turn: what it sees, as it sees it."""
        project = await existing(project_id)
        orchestrator = project.settings.orchestrator
        session_id = orchestrator.session_id if orchestrator.enabled else ""
        text = await orchestrators().project_state(project, session_id=session_id or None)
        return {"project_id": project_id, "session_id": session_id or None, "text": text, "chars": len(text), "max_chars": manager.config.orchestrator.state_max_chars}

    @api.get("/api/projects/{project_id}/focus-state")
    async def focus_state(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The project as the orchestrator's chat shows it in one line: work in hand, results waiting for
        a decision, questions for the operator, and what became of the operator's messages."""
        return await orchestrators().focus_state(await existing(project_id))

    @api.get("/api/projects/{project_id}/usage")
    async def project_usage(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the project spends: per staff member, its orchestrator and its other sessions, today, over
        the last 7 days and in all, with calls nobody priced counted apart."""
        await existing(project_id)
        return await ProjectUsage(manager).summary(project_id)
    # -- wake-ups ------------------------------------------------------------------------

    @api.get("/api/projects/{project_id}/wakeups")
    async def get_wakeups(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The orchestrator's wake-ups, whoever set them, soonest first."""
        await existing(project_id)
        revision = await app.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                         " WHERE scope_kind='project' AND scope_id=?", (project_id,))
        return {"wakeups": await wakeups.wakeups(app, project_id),
                "max": manager.config.orchestrator.wakeups_max,
                "collection_revision": revision["revision"] if revision else None}

    @api.post("/api/projects/{project_id}/wakeups")
    async def post_wakeup(project_id: str, body: WakeupBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator leaves the orchestrator a wake-up with a note."""
        project = await existing(project_id)
        try:
            wakeup = await wakeups.set_wakeup(
                app, project, note=body.note, at=body.at, in_minutes=body.in_minutes,
                cron=body.cron, principal=Principal.operator(who),
                client_operation_id=body.client_operation_id,
                expected_collection_revision=body.expected_collection_revision,
                expires_at=body.expires_at,
            )
        except (wakeups.WakeupRefused, ValueError, ControlDenied) as exc:
            raise HTTPException(409 if isinstance(exc, ControlConflict) else 400, str(exc)) from exc
        return wakeup

    @api.delete("/api/projects/{project_id}/wakeups/{wakeup_id}")
    async def delete_wakeup(project_id: str, wakeup_id: str, body: WakeupDelete,
                            who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            removed = await wakeups.cancel(
                app, project_id, wakeup_id, principal=Principal.operator(who),
                expected_collection_revision=body.expected_collection_revision,
                expected_schedule_revision=body.expected_schedule_revision,
                client_operation_id=body.client_operation_id,
            )
            if removed is None:
                raise HTTPException(404, "no such wake-up")
        except (wakeups.WakeupRefused, ValueError, ControlDenied) as exc:
            raise HTTPException(409, str(exc)) from exc
        return removed

    # -- watches -------------------------------------------------------------------------

    def keeper() -> Any:
        found = app.extensions.get("watches")
        if found is None:
            raise HTTPException(503, "watches are not running on this installation")
        return found

    def watch_commands() -> WatchCommands:
        return WatchCommands(keeper())

    @api.get("/api/projects/{project_id}/watches")
    async def get_watches(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every watch of the project, switched on or off, oldest first, with what bounds them."""
        await existing(project_id)
        config = manager.config.watches
        delivery_rows = await manager.db.fetchall(
            "SELECT id,watch_id,status,receipt_id,last_error,created_at FROM ("
            " SELECT id,watch_id,status,receipt_id,last_error,created_at,"
            " ROW_NUMBER() OVER (PARTITION BY watch_id ORDER BY created_at DESC,id DESC) AS rank"
            " FROM watch_deliveries WHERE project_id = ?) WHERE rank = 1", (project_id,),
        )
        latest_delivery: dict[str, dict[str, Any]] = {}
        for row in delivery_rows:
            latest_delivery.setdefault(row["watch_id"], {
                "id": row["id"], "status": row["status"], "receipt_id": row["receipt_id"],
                "last_error": row["last_error"], "created_at": row["created_at"],
            })
        watches = keeper().of_project(project_id)
        authority: dict[str, str] = {}
        async with manager.db.transaction() as conn:
            for watched in watches:
                authority[watched.id] = await watch_authority_status(
                    conn, manager.db, watch_id=watched.id,
                    condition_revision=watched.condition_revision, project_id=project_id,
                    action=watched.action,
                )
        return {
            "watches": [{**w.view(), "latest_delivery": latest_delivery.get(w.id),
                         "authority_state": authority[w.id]} for w in watches],
            "collection_revision": await ControlStore(manager.db).revision(
                Scope("project", project_id), Entity("collection", project_id)),
            "project_entity_revision": await ControlStore(manager.db).revision(
                Scope("project", project_id), Entity("project", project_id)),
            "max": config.max_per_project,
            "min_cooldown_minutes": max(1, round(config.min_cooldown_seconds / 60)),
            "providers": sorted(manager.config.webhooks),
        }

    @api.post("/api/projects/{project_id}/watches")
    async def post_watch(project_id: str, body: WatchBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        project = await existing(project_id)
        try:
            return await watch_commands().create(
                Principal.operator(who), project, when=body.when, then=body.then,
                cooldown_minutes=body.cooldown_minutes, once=body.once, note=body.note,
                deadline_at=body.deadline_at, expected_collection_revision=body.expected_collection_revision,
                client_operation_id=body.client_operation_id,
            )
        except WatchRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.patch("/api/projects/{project_id}/watches/{watch_id}")
    async def patch_watch(project_id: str, watch_id: str, body: WatchPatch, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        project = await existing(project_id)
        try:
            return await watch_commands().change(
                Principal.operator(who), project, watch_id, enabled=body.enabled,
                note=body.note, cooldown_minutes=body.cooldown_minutes, when=body.when,
                then=body.then, deadline_at=body.deadline_at,
                expected_entity_revision=body.expected_entity_revision,
                expected_condition_revision=body.expected_condition_revision,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such watch") from exc
        except WatchRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    @api.delete("/api/projects/{project_id}/watches/{watch_id}")
    async def delete_watch(project_id: str, watch_id: str, body: WatchDelete, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await existing(project_id)
        try:
            return await watch_commands().remove(
                Principal.operator(who), project_id, watch_id,
                expected_entity_revision=body.expected_entity_revision,
                expected_condition_revision=body.expected_condition_revision,
                client_operation_id=body.client_operation_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such watch") from exc
        except ControlConflict as exc:
            raise HTTPException(409, {"reason": str(exc), "current_revision": exc.current_revision}) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    # -- the main orchestrator -----------------------------------------------------------------

    def dispatcher() -> Any:
        found = app.extensions.get("dispatcher")
        if found is None:
            raise HTTPException(503, "the main orchestrator is not running on this installation")
        return found

    def dispatches() -> Any:
        found = app.extensions.get("dispatches")
        if found is None:
            raise HTTPException(503, "dispatches are not running on this installation")
        return found

    @api.get("/api/main")
    async def get_main(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The main chat as the app draws it: its session (empty until first opened), the dispatches
        newest first, and the requests shown in it — open ones as cards, answered ones as their line."""
        return await dispatcher().view()  # type: ignore[no-any-return]

    @api.post("/api/main")
    async def open_main(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The main orchestrator's session, made the first time the operator opens it."""
        return {"session_id": await dispatcher().ensure()}

    @api.post("/api/main/replace")
    async def replace_main(body: MainReplaceBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A fresh main orchestrator in place of the current one; the old chat keeps its history."""
        return {"session_id": await dispatcher().replace(body.reason, by="operator")}

    @api.get("/api/dispatches/{dispatch_id}")
    async def get_dispatch(dispatch_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        dispatch = await manager.dispatches.get(dispatch_id)
        if dispatch is None:
            raise HTTPException(404, "no such dispatch")
        project = await manager.projects.get(dispatch.project_id)
        return {
            **dispatch.view(),
            "project_name": project.name if project is not None else "",
            "messages": [m.view() for m in await manager.dispatches.messages(dispatch.id, limit=100)],
            "asks": [a.view() for a in await manager.asks.of_dispatches([dispatch.id], open_only=False)],
        }

    @api.post("/api/dispatches/{dispatch_id}/cancel")
    async def cancel_dispatch(dispatch_id: str, body: CancelBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator stops a dispatch themselves: the project is told, and the main orchestrator is told too."""
        dispatch = await manager.dispatches.get(dispatch_id)
        if dispatch is None:
            raise HTTPException(404, "no such dispatch")
        try:
            done = await dispatches().cancel(dispatch, reason=body.reason if body is not None else "", by="operator")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return dict(done.view())

    @api.get("/api/questions")
    async def list_questions(project: str | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What waits for the operator: one project's requests, or every orchestrated project's and the
        main orchestrator's own when no project is named. Oldest first."""
        if project is not None:
            await existing(project)
        return {"questions": await questions.waiting(app, project)}

    async def answer_batch(items: list[AnswerItem], project_id: str | None, via: str) -> dict[str, Any]:
        try:
            return await questions.answer(app, [i.model_dump(exclude_none=True) for i in items], project_id=project_id, via=via)
        except questions.Unanswerable as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @api.post("/api/projects/{project_id}/asks/answer")
    async def answer_project_asks(project_id: str, body: list[AnswerItem], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Several answers from a project's list, sent together. Each has its own outcome; a request
        answered elsewhere first is a ``conflict`` item, never a failure of the whole send."""
        await existing(project_id)
        return await answer_batch(body, project_id, "project")

    @api.post("/api/asks/answer")
    async def answer_asks(body: list[AnswerItem], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The same from the main chat's list, where the requests of every project wait together."""
        return await answer_batch(body, None, "main")

    @api.post("/api/projects/{project_id}/setup/finish")
    async def finish_setup(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """"Finish setup": the project's requests are its own again, whatever its first dispatch is doing."""
        await existing(project_id)
        return {"finished": await dispatches().finish_setup(project_id, by="operator")}


__all__ = ["environments", "host_bridge", "reach", "register"]
