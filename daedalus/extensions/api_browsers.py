"""The routes of the agent's browser: its groups as the app lists them, the live view's ticket and
socket, the operator's hand on the controls, profiles, downloads, the audit and the load.

The shapes are in ``docs/architecture/browser.md`` (The host side). Everything they do is in
``daedalus.browser``; this module adapts the framework to it, the only kind of module allowed to.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from daedalus import load as load_math
from daedalus.browser.gateway import BrowserGateway
from daedalus.browser.model import BrowserError, EnvUnavailable, InvalidRequest, NotFound
from daedalus.browser.notes import SiteNotes
from daedalus.browser.workflows import GOAL_MAX, Workflows
from daedalus.gateway import SocketGone, ticket_who
from daedalus.stores.files import FileRefused, safe_name

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.browser.agent import BrowserAgent
    from daedalus.browser.service import Browsers


class TicketBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_only: bool = False
    tier: Literal["live", "thumb"] = "live"
    """The app says which view it opens; the tier itself travels in the socket's ATTACH."""


class ViewportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    w: int = Field(ge=320, le=3840)
    h: int = Field(ge=320, le=3840)


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: Literal["agent", "human", "paused"]
    client_id: str = Field(default="", max_length=64)
    """The live view that drives, for ``human``: the ``client_id`` its ``hello`` named."""
    ttl_ms: int | None = Field(default=None, ge=1000, le=86_400_000)
    reason: str = Field(default="", max_length=300)
    note: str = Field(default="", max_length=2000)
    """For ``agent``: what the operator tells the agent when handing the browser back."""


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(default="", max_length=500)


class DialogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool
    tab_id: str = Field(default="", max_length=64)
    text: str | None = Field(default=None, max_length=10_000)


class RecordingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frames: bool
    human: bool | None = None
    """Whether the recording goes on while the operator drives; omitted = the Settings default."""


class WorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: Literal["slots", "literal"] = "slots"
    """``literal`` keeps what the operator types into ordinary fields where it looks like nothing
    personal; ``slots`` keeps only a named blank for it."""


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab_id: str = Field(default="", max_length=64)


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(default="", max_length=GOAL_MAX)
    """What the recorded steps do, in the operator's words: the procedure's title."""


class NoteEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=8000)
    title: str | None = Field(default=None, max_length=300)


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class ViewSocket:
    """The framework's WebSocket as the relay's ``FrameSocket``."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive(self) -> bytes | str | None:
        try:
            message = await self.websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("bytes") is not None:
            return bytes(message["bytes"])
        return str(message.get("text") or "")

    async def send(self, frame: bytes) -> None:
        try:
            await self.websocket.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise SocketGone(str(exc)) from None

    async def close(self, code: int, reason: str = "") -> None:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.websocket.close(code, reason)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    settings = app.settings

    @api.exception_handler(BrowserError)
    async def browser_refusal(_: Request, exc: BrowserError) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "code": exc.code, **{k: v for k, v in exc.details.items() if isinstance(v, str | int | float | bool | None)}}, status_code=exc.status)

    def service() -> Browsers:
        found = app.extensions.get("browser")
        if found is None:
            raise HTTPException(404, "this installation has no browser")
        return cast("Browsers", found)

    def agent() -> BrowserAgent | None:
        return cast("BrowserAgent | None", app.extensions.get("browser_agent"))

    gateway = BrowserGateway(
        service=lambda: cast("Browsers | None", app.extensions.get("browser")),
        public_url=lambda: settings.miniapp_public_url,
        ticket_ttl=lambda: app.config.browser.ticket_ttl_seconds,
    )
    api.state.browser_gateway = gateway  # the tests reach its ticket book's clock through this

    @api.get("/api/browsers")
    async def browsers_list(
        session: str | None = Query(default=None, max_length=128),
        staff: str | None = Query(default=None, max_length=128),
        project: str | None = Query(default=None, max_length=128),
        status: Literal["open", "closed", "lost"] | None = None,
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """An owner's groups (``session`` or ``staff``), a project's, or every one: open first, then
        by activity. ``available`` says whether a browser answers now, and ``reason`` why not; an
        installation without a browser answers ``available: false`` rather than an error, since the
        app asks for every session it shows."""
        found = cast("Browsers | None", app.extensions.get("browser"))
        if found is None:
            return {"available": False, "reason": "this installation has no browser", "groups": [], "envs": [], "capacity": None}
        envs = found.environments()
        up = [e for e in envs if e["available"]]
        reason = "" if up else next((f"{e['env']}: {e['detail'] or e['reason']}" for e in envs if e["configured"]), "no browser is configured")
        groups = await found.list(session_id=session, staff_id=staff, project_id=project, status=status)
        return {
            "available": bool(up),
            "reason": reason,
            "groups": groups,
            "envs": envs,
            "capacity": {"open": await found.running(found.agent_env()), "cap": app.config.browser.running_cap, "queued": found.queue()},
        }

    @api.get("/api/browsers/load")
    async def browsers_load(cap: int | None = Query(default=None, ge=1, le=64), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await service().load(cap=cap)

    @api.get("/api/workloads/load")
    async def workloads_load(terminal_cap: int | None = Query(default=None, ge=1, le=100_000), browser_cap: int | None = Query(default=None, ge=1, le=64), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Terminals and browsers on one machine: each with its own cap and projection, so the load
        bar can put both on one memory track. Either is ``null`` where this installation has none."""
        terminals = app.extensions.get("terminals")
        browsers = app.extensions.get("browser")
        term = await terminals.load(cap=terminal_cap) if terminals is not None else None  # type: ignore[attr-defined]
        brow = await cast("Browsers", browsers).load(cap=browser_cap) if browsers is not None else None
        kinds: dict[str, Any] = {}
        machine: dict[str, Any] = {}
        for name, load, cap in (("terminals", term, terminal_cap), ("browsers", brow, browser_cap)):
            if not load:
                continue
            likely = load.get("likely") or {}
            kinds[name] = {"cap": cap or load["cap"], "running": load["running"], "used_rss": int(load["used"]["rss_bytes"]), "cost": load_math.Cost(float(likely.get("rss_bytes") or 0), float(likely.get("cpu_percent") or 0), int(likely.get("samples") or 0))}
            if not machine and load["used"].get("mem_total_bytes"):
                used = load["used"]
                machine = {"mem_total_bytes": used["mem_total_bytes"], "mem_available_bytes": used["mem_available_bytes"], "cpus": used.get("cpus") or 0, "cpu_percent": used.get("machine_cpu_percent") or 0}
        return {"terminals": term, "browsers": brow, "together": load_math.project_workloads(machine=machine, kinds=kinds) if kinds else None}

    @api.get("/api/browsers/profiles")
    async def browsers_profiles(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"profiles": await service().profiles()}

    @api.post("/api/browsers/profiles/{env}/{profile}/clear")
    async def browsers_profile_clear(env: Literal["container", "host"], profile: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget a profile's cookies, storage and cache: every login made in it. Refused while its browser runs."""
        await service().profile_action(env, profile, "clear")
        return {"ok": True}

    @api.delete("/api/browsers/profiles/{env}/{profile}")
    async def browsers_profile_delete(env: Literal["container", "host"], profile: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await service().profile_action(env, profile, "delete")
        return {"ok": True}

    @api.get("/api/browsers/running")
    async def browsers_running(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The browsers every environment runs now, with their memory and whose groups they hold."""
        return {"browsers": await service().running_browsers()}

    @api.post("/api/browsers/running/{env}/{browser_id}/close")
    async def browsers_running_close(env: Literal["container", "host"], browser_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """End one browser and every group in it; the profiles, and so the logins, stay."""
        await service().close_browser(env, browser_id)
        return {"ok": True}

    @api.get("/api/browsers/recordings")
    async def browsers_recordings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The recorded keyframes on disk per environment, against the size and age they are kept to."""
        return {"envs": await service().recordings()}

    def site_notes() -> SiteNotes:
        found = app.extensions.get("browser_notes")
        if found is None:
            raise HTTPException(404, "this installation has no browser")
        return cast("SiteNotes", found)

    async def named(note: dict[str, Any]) -> dict[str, Any]:
        """A note in the app's shape: with the name of the project it is for."""
        project = await manager.projects.get(note["project_id"]) if note.get("project_id") else None
        return {**note, "project": project.name if project is not None else ""}

    @api.get("/api/browsers/notes")
    async def browsers_notes(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The site notes agents proposed, waiting ones first, each with the project it is for."""
        notes = await site_notes().list()
        names = {p.id: p.name for p in await manager.projects.list()}
        for note in notes:
            note["project"] = names.get(note["project_id"], "") if note["project_id"] else ""
        notes.sort(key=lambda n: (n["status"] != "proposed", -(n["approved_at"] or n["proposed_at"])))
        return {"notes": notes}

    @api.patch("/api/browsers/notes/{note_id}")
    async def browsers_note_edit(note_id: str, body: NoteEditBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's own words for a note or a procedure, before or after approving it."""
        return {"note": await named(await site_notes().edit(note_id, text=body.text, title=body.title))}

    def workflows() -> Workflows:
        found = app.extensions.get("browser_workflows")
        if found is None:
            raise HTTPException(404, "this installation has no browser")
        return cast("Workflows", found)

    @api.get("/api/browsers/workflows/{workflow_id}")
    async def browsers_workflow_get(workflow_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"workflow": await workflows().get(workflow_id)}

    @api.post("/api/browsers/workflows/{workflow_id}/draft")
    async def browsers_workflow_draft(workflow_id: str, body: DraftBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Draft a procedure from a finished recording; it waits in the site notes for approval."""
        drafted = await workflows().draft(workflow_id, goal=(body.goal if body else ""))
        return {**drafted, "note": await named(drafted["note"])}

    @api.delete("/api/browsers/workflows/{workflow_id}")
    async def browsers_workflow_delete(workflow_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await workflows().delete(workflow_id)
        return {"ok": True}

    @api.post("/api/browsers/notes/{note_id}/approve")
    async def browsers_note_approve(note_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """From now on agents on the note's site (in its project) are shown it."""
        return {"note": await named(await site_notes().approve(note_id))}

    @api.delete("/api/browsers/notes/{note_id}")
    async def browsers_note_delete(note_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Discard a waiting note, or take back an approved one."""
        if not await site_notes().delete(note_id):
            raise NotFound(f"no site note {note_id}")
        return {"ok": True}

    @api.get("/api/browsers/{group_id}")
    async def browsers_get(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await service().get(group_id)

    @api.post("/api/browsers/{group_id}/ticket")
    async def browsers_ticket(group_id: str, request: Request, body: TicketBody | None = None, who: dict[str, Any] = Depends(auth)) -> Any:
        """A single-use ticket for the group's live view, which a WebSocket cannot authenticate itself."""
        read_only = body.read_only if body is not None else False
        caller = ticket_who(str(who.get("via") or "token"), request.headers.get("user-agent", ""), request.client.host if request.client else "")
        try:
            return await gateway.issue(group_id, read_only=read_only, who=caller)
        except EnvUnavailable as exc:
            # 409, as for a terminal: the app reads it as "the environment is down, keep trying".
            return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=409)

    @api.websocket("/ws/browsers/{group_id}")
    async def browsers_socket(websocket: WebSocket, group_id: str) -> None:
        await websocket.accept()
        await gateway.serve(
            ViewSocket(websocket),
            group_id,
            ticket=websocket.query_params.get("ticket", ""),
            origin=websocket.headers.get("origin"),
            host=websocket.headers.get("host"),
            address=websocket.client.host if websocket.client else "",
        )

    @api.post("/api/browsers/{group_id}/control")
    async def browsers_control(group_id: str, body: ControlBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Take the browser (``human`` with the view's ``client_id``), pause the agent, or give it back
        (``agent``, with a note); answers the group's control as it now is. The agent hears of the
        give-back once, with where the browser is."""
        if body.owner == "human" and not body.client_id:
            raise InvalidRequest("taking control names the live view that drives: its client_id")
        return await service().control(group_id, body.owner, client_id=body.client_id, ttl_ms=body.ttl_ms, reason=body.reason, note=body.note, by="operator")

    @api.post("/api/browsers/{group_id}/viewport")
    async def browsers_viewport(group_id: str, body: ViewportBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's picture asks the page to become this size, so the pane is the page."""
        return {"viewport": await service().resize(group_id, body.w, body.h)}

    @api.post("/api/browsers/{group_id}/dialog")
    async def browsers_dialog(group_id: str, body: DialogBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator answers the page's alert, confirm or prompt (the tab's, or the active one's)."""
        await service().answer_dialog(group_id, accept=body.accept, tab_id=body.tab_id, text=body.text)
        return {"ok": True}

    @api.get("/api/browsers/{group_id}/actions")
    async def browsers_actions(group_id: str, limit: int = Query(default=100, ge=1, le=500), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The action log the Browser tab shows, newest first."""
        await service().get_row(group_id)
        return {"actions": await service().actions(group_id, limit=limit)}

    @api.post("/api/browsers/envs/{env}/update", status_code=202)
    async def browsers_daemon_update(env: str, body: UpdateBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Recreate the browser service from the image, which updates its daemon and ends its
        browsers: 409 ``live_browsers`` with the count until repeated with ``confirm``."""
        return await service().request_update(env, confirm=body.confirm if body is not None else False)

    @api.get("/api/browsers/envs/{env}/update/{job}")
    async def browsers_daemon_update_result(env: str, job: str, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        return service().update_result(env, job)

    @api.post("/api/browsers/{group_id}/close")
    async def browsers_close(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await service().close_group(group_id, actor="operator")
        return {"ok": True}

    @api.get("/api/browsers/{group_id}/audit")
    async def browsers_audit(group_id: str, limit: int = Query(default=200, ge=1, le=1000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The group's action log: opens, the agent's actions (lengths and hashes of what it typed,
        never the text), sensitive decisions, takeovers, downloads and who watched."""
        entries = await service().audit_log(group_id, limit=limit)
        if not entries and await app.db.fetchone("SELECT 1 FROM browser_groups WHERE id = ?", (group_id,)) is None:
            raise HTTPException(404, "no such browser")
        return {"entries": entries}

    @api.get("/api/browsers/{group_id}/downloads")
    async def browsers_downloads(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"downloads": await service().downloads(group_id)}

    @api.post("/api/browsers/{group_id}/downloads/{download_id}/save")
    async def browsers_download_save(group_id: str, download_id: str, body: SaveBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Keep a download: into the owning session's workspace (``to``, default ``downloads/<name>``),
        and, in a project, by handle in the project's files."""
        found = service()
        downloads = [d for d in await found.downloads(group_id) if d.get("id") == download_id]
        if not downloads:
            raise NotFound(f"no download {download_id} in browser {group_id}")
        download = downloads[0]
        if download.get("state") != "completed":
            raise InvalidRequest(f"{download.get('name')} is {download.get('state')}, not finished")
        data = await found.read_download(group_id, download, limit=500 << 20)
        row = await found.get_row(group_id)
        owner = found.owner_of(row)
        name = safe_name(str(download.get("name") or "")) or "download"
        out: dict[str, Any] = {"name": name, "size": len(data)}
        if owner.session_id:
            relative = (body.to if body is not None and body.to else f"downloads/{name}").lstrip("/")
            target = (manager.workspace_for(owner.session_id) / relative).resolve()
            base = manager.workspace_for(owner.session_id).resolve()
            if base != target and base not in target.parents:
                raise InvalidRequest("to is a path inside the session's workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            out["path"] = str(target)
        if owner.project_id and getattr(manager, "files", None) is not None:
            with suppress(FileRefused):
                stored = await manager.files.add(data, name=name, origin="browser", origin_ref=group_id, scope=owner.project_id, actor="operator")
                out["handle"] = stored.handle
        await found.audit(group_id, row["env"], "operator", "download_saved", {"id": download_id, "name": name, "size": len(data), "path": out.get("path", ""), "handle": out.get("handle", "")})
        return out

    @api.get("/api/browsers/{group_id}/recording")
    async def browsers_recording(group_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=5000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The group's recording switch and its keyframes, oldest first; a closed group's are there
        until they expire."""
        return await service().recording(group_id, after=after, limit=limit)

    @api.post("/api/browsers/{group_id}/recording")
    async def browsers_recording_set(group_id: str, body: RecordingBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Switch the recording of keyframes: the operator's alone; the agent has no say in it."""
        return await service().set_recording(group_id, frames=body.frames, human=body.human)

    @api.delete("/api/browsers/{group_id}/recording")
    async def browsers_recording_delete(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        await service().delete_recording(group_id)
        return {"ok": True}

    @api.get("/api/browsers/{group_id}/workflow")
    async def browsers_workflow(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The recording of the operator's steps on now, if any, and the group's finished ones."""
        found = workflows()
        return {"workflow": await found.current(group_id), "recent": await found.recent(group_id)}

    @api.post("/api/browsers/{group_id}/workflow")
    async def browsers_workflow_start(group_id: str, body: WorkflowBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Start recording the operator's steps: theirs alone, only while they hold the browser."""
        return {"workflow": await workflows().start(group_id, values=(body.values if body else "slots"))}

    @api.post("/api/browsers/{group_id}/workflow/stop")
    async def browsers_workflow_stop(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"workflow": await workflows().stop(group_id)}

    @api.post("/api/browsers/{group_id}/workflow/mark")
    async def browsers_workflow_mark(group_id: str, body: MarkBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the page shows when the task is done: the words the operator selected, else its heading."""
        return {"step": await workflows().mark(group_id, tab_id=(body.tab_id if body else ""))}

    @api.get("/api/browsers/{group_id}/frames/{no}")
    async def browsers_frame(group_id: str, no: int, _: dict[str, Any] = Depends(auth)) -> Response:
        """One keyframe's picture. Its secret fields were masked when it was taken."""
        if no < 1:
            raise HTTPException(404, "no such keyframe")
        _meta, data = await service().frame(group_id, no)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @api.get("/api/browsers/{group_id}/asks/{key}/thumbnail")
    async def browsers_ask_thumbnail(group_id: str, key: str, _: dict[str, Any] = Depends(auth)) -> Response:
        """The picture of the element a sensitive action was asked about, for the permission card."""
        pictures = agent()
        data = pictures.thumbnails.get(key) if pictures is not None else None
        if not data:
            raise HTTPException(404, "no picture for this request (they are kept until the host restarts)")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


__all__ = ["ControlBody", "TicketBody", "ViewSocket", "register"]
