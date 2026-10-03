"""The main orchestrator creates a project: confirmed first, then made, then set up by its orchestrator.

``CreateProject`` never makes anything by itself. It checks the folders it was given — a container
folder must lie under the roots allowed for new projects, a host folder is looked at on the host
through its terminal daemon — and puts a confirmation card to the operator: the name, the folders and
where they live. The card is a request row like any other (first answer wins); a misheard voice
command therefore never becomes a folder on the operator's machine. Only the operator's "Create"
makes the project, switches its orchestrator on and opens dispatch #1, the survey that writes the
brief; while it runs the project is "being set up", and every question the project asks is shown in
the main chat as well.

A confirmation for a project on the host is answered in the app alone: no Telegram button and no
notification action offers it.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.dispatcher import OPERATIONS, TITLE, Dispatcher, main_files
from daedalus.extensions.dispatches import SETUP_BY
from daedalus.extensions.notifications import ActionConflict, ActionOutcome, ActionRefused
from daedalus.extensions.staff import AlreadyAnswered
from daedalus.stores.files import StoredFile, human_size
from daedalus.stores.projects import FolderSpec, ProjectError, ProjectSettings, normalise_root
from daedalus.stores.staff import Ask, StaffError
from daedalus.terminals.model import EnvUnavailable

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

SURVEY = (
    "Survey the project's folders and write its brief. Read what is there (README, build files, the layout, "
    "recent history) and fill the brief's sections: goals, constraints, preferences, done_when and notes — "
    "the stack and how the project is laid out, how to run it and how to test it, and the open questions "
    "you could not settle from the files. Ask the operator what only they can answer (AskOperator with this "
    "dispatch's id). Close this dispatch with one ProjectReport(kind=done) that summarises the brief."
)
OPTIONS = ["Create", "Don't create"]
MISSING_ON_HOST = "the folder does not exist on the host"
_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def unescaped(text: str) -> str:
    """The operator's words with JSON's ``\\uXXXX`` escapes turned back into the letters they stand for.

    A model sometimes escapes the arguments of its tool call twice: the call arrives as valid JSON
    whose strings hold the six characters ``\\u041f`` where the operator wrote "П". Parsed once, as
    it must be, that is still text, and a Russian goal reached the brief, the confirmation card and
    dispatch #1 as a row of escapes. Nothing an operator types into a goal or a name contains a
    literal ``\\u`` and four hex digits, so they are decoded here, surrogate pairs included."""
    if not _ESCAPE_RE.search(text):
        return text
    decoded = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return decoded.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


class ProjectMaker:
    """``CreateProject``: the checks, the confirmation card, and what its answer makes."""

    def __init__(self, app: Application, dispatcher: Dispatcher) -> None:
        self.app = app
        self.dispatcher = dispatcher
        self.manager = dispatcher.manager

    # -- the folders ------------------------------------------------------------------------------

    async def check_folder(self, path: str, env: str, *, create_missing: bool, create: bool) -> dict[str, Any]:
        """A folder the operator named for a new project, checked where it lives.

        A container folder must lie under the allowed roots (Settings, or the folders that already hold
        container projects); a host folder is asked of the host through its terminal daemon. With
        ``create`` false nothing is made — that waits for the operator's confirmation — and a missing
        folder is only refused when it may not be made. Returns ``{path, env, exists, missing}``.
        """
        local = self.manager.projects.local_env
        wanted = env or local
        if wanted not in ("container", "host"):
            raise ValueError("env is container or host")
        target = normalise_root(path)
        if wanted == local:
            if wanted == "container":
                roots = await self.container_roots()
                if not roots:
                    raise ValueError("no container folder is allowed for new projects yet: set dispatcher.container_roots in Settings, or create the project without folders")
                if not any(target == root or root in target.parents for root in roots):
                    raise ValueError(f"{target} is outside the folders new projects may use here ({', '.join(str(r) for r in roots)})")
            exists = target.is_dir()
            if not exists and target.exists():
                raise ValueError(f"{target} is a file, not a folder")
            if not exists and not create_missing:
                raise ValueError(f"{target} does not exist; pass create_missing=true to make it")
            if create and not exists:
                os.makedirs(target, exist_ok=True)
            return {"path": str(target), "env": wanted, "exists": exists, "missing": not exists}
        bridge: Any = self.app.extensions.get("host_bridge")
        if bridge is None or not bridge.available():
            raise ValueError("a host folder needs the host terminal bridge, which is not running here; install it, or use a container folder")
        try:
            checked = await bridge.check_folder(str(target), create_missing=create and create_missing, actor="agent:dispatcher")
        except EnvUnavailable as exc:
            raise ValueError(f"the host bridge is not available: {exc}") from exc
        missing = not checked.exists
        # A folder that is not there yet is fine to propose when it may be made; everything else the
        # daemon objects to (home, the filesystem root, a denied place, a file) is refused now.
        if checked.problem and not (missing and create_missing and not create and checked.problem == MISSING_ON_HOST):
            raise ValueError(f"{target} on the host: {checked.problem}")
        return {"path": str(target), "env": "host", "exists": checked.exists, "missing": missing}

    async def container_roots(self) -> list[Path]:
        """Where a new project's container folder may be: Settings, or else the folders that hold the
        container projects the operator already has — the mounts they set up."""
        configured = [normalise_root(r) for r in self.manager.config.dispatcher.container_roots if str(r).strip()]
        if configured:
            return configured
        found: dict[Path, None] = {}
        for project in await self.manager.projects.list():
            if project.settings.system or project.settings.ephemeral:
                continue
            for folder in project.folders:
                if folder.env == "container" and not folder.managed and folder.path.parent != Path(folder.path.root):
                    found[folder.path.parent] = None
        return list(found)

    # -- the card ---------------------------------------------------------------------------------

    async def propose(self, session_id: str, *, name: str, folders: list[dict[str, Any]] | None, goal: str, create_missing: bool, start_orchestrator: bool, files: list[str] | None = None) -> Ask:
        label = " ".join(unescaped(name or "").split())[:80]
        if not label:
            raise ValueError("a project needs a name")
        existing = [p for p in await self.manager.projects.list() if p.name.casefold() == label.casefold()]
        if existing:
            raise ValueError(f"there is already a project called {existing[0].name}")
        checked: list[dict[str, Any]] = []
        for raw in folders or []:
            if not isinstance(raw, dict) or not str(raw.get("path") or "").strip():
                raise ValueError('each folder is {"path", "env": "container" or "host", "readonly"?, "label"?}')
            found = await self.check_folder(str(raw["path"]), str(raw.get("env") or ""), create_missing=create_missing, create=False)
            # A folder another project has is not refused: one place on disk may be part of several
            # projects. The card says so, and the store still refuses one that nests in another's.
            also = await self.manager.projects.holders(found["path"])
            checked.append({**found, "readonly": bool(raw.get("readonly")), "label": unescaped(str(raw.get("label") or ""))[:60], "also_in": also})
        handed = await main_files(self.dispatcher, files)
        if handed and not start_orchestrator:
            raise ValueError("files go to a new project's orchestrator with its first dispatch; with start_orchestrator=false nobody would receive them")
        return await self._card(session_id, name=label, folders=checked, goal=" ".join(unescaped(goal or "").split())[:1000], create_missing=create_missing, start_orchestrator=start_orchestrator, files=handed)

    async def _card(self, session_id: str, *, name: str, folders: list[dict[str, Any]], goal: str, create_missing: bool, start_orchestrator: bool, files: list[StoredFile] | None = None) -> Ask:
        text = f"Create the project {name}?"
        for folder in folders:
            what = "make" if folder.get("missing") else "use"
            text += f"\n· {what} the {folder['env']} folder {folder['path']}" + (" (read-only)" if folder.get("readonly") else "")
            if folder.get("also_in"):
                text += f", also in the project{'s' if len(folder['also_in']) > 1 else ''} {', '.join(folder['also_in'])}"
        if not folders:
            text += "\n· a new folder of the installation's own"
        if goal:
            text += f"\nGoal: {goal}"
        if files:
            text += "\nWith the files: " + ", ".join(f"{f.name} ({human_size(f.size)})" for f in files)
        text += "\nIts orchestrator is switched on and asked to survey the folders and write the brief." if start_orchestrator else "\nIts orchestrator stays off."
        detail = {"name": name, "folders": folders, "goal": goal, "create_missing": create_missing, "start_orchestrator": start_orchestrator, "options": OPTIONS, "files": [f.id for f in files or []]}
        ask = await self.manager.asks.open(None, origin="dispatcher", kind="project", text=text, routed_to="operator", detail=detail)
        ref = f"dispatcher:main:{ask.id}"
        await self.manager.db.execute("UPDATE asks SET detail_json = json_set(detail_json, '$.event_ref', ?) WHERE id = ?", (ref, ask.id))
        ask = (await self.manager.asks.get(ask.id)) or ask
        host = any(f.get("env") == "host" for f in folders)
        state = self.manager.live_state(session_id)
        await self.dispatcher._publish(
            "ask.pending",
            {
                "request_id": ask.id,
                "request_ref": ref,
                "run_id": (state.run_id or "") if state is not None else "",
                "title": f"{TITLE} · new project",
                # A project on the host is confirmed in the app alone: without options the notification
                # offers only "open", so no lock screen can create it.
                "questions": [{"question": text, "options": [] if host else [{"label": o, "description": ""} for o in OPTIONS], "multi": False, "custom": False}],
                "operator_facing": True,
                "telegram": False,
                "short_id": ask.short_id,
                "routed_to": "operator",
                "kind": "project",
            },
            None,
            session_id=session_id,
        )
        return ask

    # -- the answer -------------------------------------------------------------------------------

    async def answer_own(self, ask: Ask, *, allow: bool | None, text: str | None, selected: list[str] | None, by: str, via: str) -> dict[str, Any]:
        """The operator answered the confirmation: first answer wins, then the project is made (or not),
        and the main orchestrator is woken with what came of it."""
        if ask.kind != "project":
            raise StaffError(f"request {ask.short_id} is not the main orchestrator's to settle")
        options = list(ask.detail.get("options") or OPTIONS)
        if selected:
            yes = selected[0] == options[0]
        elif allow is not None:
            yes = bool(allow)
        else:
            raise StaffError("confirm a new project with its buttons")
        if via in ("telegram", "push", "notification") and await self.dispatcher.host_level(ask):
            raise StaffError("a project on the host is confirmed in the app")
        resolution: dict[str, Any] = {"allow": yes, "selected": [options[0] if yes else options[-1]], "text": "", "via": via}
        if not await self.manager.asks.resolve(ask.id, "operator", resolution):
            raise AlreadyAnswered(f"request {ask.short_id} was already answered")
        outcome = "declined: nothing was created"
        error = ""
        if yes:
            try:
                outcome = await self._create(ask)
            except (ProjectError, ValueError, OSError, KeyError) as exc:
                error = str(exc)
                outcome = f"confirmed, but it could not be created: {exc}"
        await self.manager.db.execute("UPDATE asks SET resolution_json = json_set(resolution_json, '$.outcome', ?) WHERE id = ?", (outcome, ask.id))
        ref = str(ask.detail.get("event_ref") or "")
        notifications = self.app.notifications
        if notifications is not None and ref:
            try:
                await notifications.resolve(ref, "answered", via=via)
            except Exception:  # noqa: BLE001 — the answer stands; the notification is a courtesy
                logger.warning("could not close the notification of %s", ask.short_id, exc_info=True)
        await self.dispatcher._publish("ask.answered", {"request_id": ask.id, "request_ref": ref, "via": via, "by": by}, None, session_id=await self.dispatcher.session_id() or None)
        answered = await self.manager.asks.get(ask.id)
        return {"state": "answered", "delivered": not error, "error": error, "ask": answered.view() if answered else ask.view()}

    async def _create(self, ask: Ask) -> str:
        detail = ask.detail
        name = str(detail.get("name") or "").strip()
        confirmed = await self.manager.asks.get(ask.id)
        if confirmed is None or confirmed.resolved_by != "operator" or not confirmed.resolution.get("allow"):
            raise ProjectError("the project request has no operator confirmation")
        project_id = uuid.uuid5(uuid.NAMESPACE_URL, f"confirmed-project:{ask.id}").hex[:12]
        project = await self.manager.projects.get(project_id)
        first_creation = project is None
        if project is not None:
            origin = await self.manager.db.fetchone(
                "SELECT body,origin_ref FROM project_goal_revisions"
                " WHERE project_id = ? AND goal_revision = 1", (project_id,))
            if (project.name != name or origin is None or origin["origin_ref"] != ask.id
                    or origin["body"] != str(detail.get("goal") or "")):
                raise ProjectError("the confirmed project identity is already occupied")
        else:
            folders = [f for f in detail.get("folders") or [] if isinstance(f, dict)]
            create_missing = bool(detail.get("create_missing"))
            specs: list[FolderSpec] = []
            # Checked again: the card may have waited a day, and a folder can appear or go meanwhile.
            for folder in folders:
                checked = await self.check_folder(str(folder.get("path") or ""), str(folder.get("env") or ""), create_missing=create_missing, create=True)
                specs.append(FolderSpec(checked["path"], label=str(folder.get("label") or ""), env=checked["env"], readonly=bool(folder.get("readonly"))))
            settings = ProjectSettings(snapshots=not specs, default_env=(specs[0].env or "") if specs else "")
            project = await self.manager.projects.create(
                name, specs or None, settings=settings, project_id=project_id,
                initial_goal=str(detail.get("goal") or ""), confirmation_ask_id=ask.id)
        if first_creation:
            await self.dispatcher._publish("project.changed", {"change": "created", "actor": "dispatcher"}, project.id)
            await self.manager.projects.record(project.id, "system", "setup", f"The main orchestrator created the project on the operator's confirmation [{ask.short_id}].", {"ask_id": ask.id})
        if not detail.get("start_orchestrator", True):
            return f"created {project.name} ({project.id}) without an orchestrator"
        orchestrators = self.dispatcher.orchestrators
        if orchestrators is None:
            return f"created {project.name} ({project.id}); orchestrators are not running here, so nobody surveys it"
        # Marked before the orchestrator starts, so its very first question is already shown in the main chat.
        await self.manager.projects.set_setup(project.id, SETUP_BY)
        project = await orchestrators.enable(project.id, by="dispatcher")
        survey = SURVEY + (f"\nThe operator's goal for it: {detail['goal']}" if detail.get("goal") else "")
        # The same handles the main orchestrator was given: the card kept their ids, not the bytes.
        files = await self.manager.files.many(str(i) for i in detail.get("files") or [])
        if files:
            survey += "\nThe operator's files for it are yours now: read them with Peek(path='att:…')."
        prior = await self.manager.db.fetchone("SELECT id,seq FROM dispatches WHERE project_id = ?"
                                               " AND kind = 'setup' ORDER BY seq LIMIT 1", (project.id,))
        if prior is not None:
            for stored in files:
                if project.id not in await self.manager.files.scopes(stored.id):
                    await self.manager.files.grant(stored, project.id, actor="dispatcher",
                                                   target=f"dispatch {prior['id']}")
            return (f"created {project.name} ({project.id}), switched its orchestrator on and handed it"
                    f" dispatch {prior['id']} (#{prior['seq']}): survey the folders and write the brief")
        dispatch = await self.dispatcher.dispatches.create(project, text=survey, title="Survey the folders and write the brief", from_session=await self.dispatcher.session_id(), kind="setup", files=files)
        return f"created {project.name} ({project.id}), switched its orchestrator on and handed it dispatch {dispatch.id} (#1): survey the folders and write the brief"

    async def resolve_action(self, req: Any) -> ActionOutcome:
        """An answer to the confirmation from a notification (``dispatcher:main:<ask>``)."""
        ask = await self.manager.asks.get(req.target)
        if ask is None:
            raise ActionConflict("withdrawn")
        options = list(ask.detail.get("options") or [])
        if not req.action.startswith("answer:"):
            raise ActionRefused(f"a confirmation is not answered with {req.action!r}")
        try:
            selected = [str(options[int(req.action.split(":", 1)[1])])]
        except (ValueError, IndexError) as exc:
            raise ActionRefused(f"no option {req.action!r}") from exc
        try:
            await self.answer_own(ask, allow=None, text=None, selected=selected, by="operator", via=req.via)
        except AlreadyAnswered as exc:
            raise ActionConflict("answered") from exc
        except StaffError as exc:
            raise ActionRefused(str(exc)) from exc
        return ActionOutcome("answered")

    def attach(self) -> None:
        OPERATIONS["create_project"] = op_create_project
        notifications = self.app.notifications
        if notifications is not None and hasattr(notifications, "register_resolver"):
            notifications.register_resolver("dispatcher", self.resolve_action)
        team: Any = self.app.extensions.get("staff")
        if team is not None:
            team.dispatcher_requests = self


async def op_create_project(
    d: Dispatcher,
    session_id: str,
    *,
    name: str,
    folders: list[dict[str, Any]] | None = None,
    goal: str = "",
    create_missing: bool = False,
    start_orchestrator: bool = True,
    files: list[str] | None = None,
) -> str:
    maker: Any = d.app.extensions.get("dispatcher_projects")
    if maker is None:
        raise ValueError("creating projects is not available on this installation")
    ask = await maker.propose(session_id, name=name, folders=folders, goal=goal, create_missing=create_missing, start_orchestrator=start_orchestrator, files=files)
    return f"asked the operator to confirm as [{ask.short_id}] — a card in this chat; nothing is created until they answer, and the answer arrives as an event. Tell them in one line what you asked."


async def install(app: Application) -> list[Any]:
    dispatcher: Any = app.extensions.get("dispatcher")
    if dispatcher is None:
        return []
    maker = ProjectMaker(app, dispatcher)
    app.extensions["dispatcher_projects"] = maker
    maker.attach()
    return []


__all__ = ["OPTIONS", "ProjectMaker", "SURVEY", "install", "op_create_project", "unescaped"]
