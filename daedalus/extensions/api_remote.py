"""The terminal client's routes: pairing, devices, ``/resume``, a session's hands and guard.

``POST /api/cli/pair/start`` and ``/poll`` are public: the client holds no credential yet. The approval
and the device list are the operator's (any operator credential; the door refuses a device token
there). The rest is the device's own, reached through :class:`daedalus.remote.door.DeviceDoor`, which
already checked the token, the route and the session's scope. Everything they do is in
:mod:`daedalus.remote`; this module adapts the framework to it. The contract is
``docs/architecture/cli.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from daedalus import __version__
from daedalus.remote.devices import PAIRING_TTL, POLL_INTERVAL, Devices, Refused, shown_code
from daedalus.remote.door import SCOPE_KEY, DeviceDoor

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.remote.control import RemoteControl

CONTRACT = 1
"""The version of ``docs/architecture/cli.md`` this server speaks."""
CWD_MAX = 4096


class PairStartBody(BaseModel):
    """What the client says about itself. Unknown fields are ignored rather than refused: a client a
    release ahead must still be able to pair."""

    name: str = Field(default="", max_length=200)
    hostname: str = Field(default="", max_length=200)
    os: str = Field(default="", max_length=64)
    arch: str = Field(default="", max_length=64)
    client_version: str = Field(default="", max_length=64)


class PairPollBody(BaseModel):
    device_code: str = Field(min_length=1, max_length=128)


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=80)
    """A name for the device other than the one it gave."""


class RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


class HandsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str = Field(min_length=1, max_length=CWD_MAX)


class GuardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guard: Literal["ask", "auto", "plan"]


def absolute_folder(cwd: str) -> bool:
    """A folder as a device spells it, absolute: ``/home/you/x`` or ``C:\\Users\\you\\x`` or a share."""
    return cwd.startswith("/") or cwd.startswith("\\\\") or (len(cwd) >= 3 and cwd[0].isalpha() and cwd[1] == ":" and cwd[2] in "\\/")


def whole_disk(cwd: str) -> bool:
    """A folder that is a whole disk: ``/``, ``C:\\`` or a share's root. The client also refuses the home
    folder and any folder holding it, as ptyd refuses them as roots; the server does not know the
    device's home, so it refuses only what is a root on every machine."""
    folder = cwd.replace("\\", "/").rstrip("/")
    if folder == "":
        return True
    if len(folder) == 2 and folder[1] == ":":
        return True
    return folder.startswith("//") and folder.count("/") <= 3


def device_of(request: Request) -> str:
    """The device the door let this request through for, or 401 for a request it did not."""
    found = request.scope.get(SCOPE_KEY)
    if not isinstance(found, dict) or not found.get("device"):
        raise HTTPException(401, "a terminal's credential is needed here")
    return str(found["device"])


def refusal(exc: Refused) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def public_base(app: Application, request: Request) -> str:
    """The app's origin as the operator's phone reaches it: the configured public address, or the
    address this request came in by when none is configured (natively, or a server on a LAN)."""
    public = urlsplit(str(app.settings.miniapp_public_url or ""))
    if public.scheme in ("http", "https") and public.netloc:
        return f"{public.scheme}://{public.netloc}"
    return str(request.base_url).rstrip("/")


async def remote_views(app: Application, session_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """What each of these sessions shows about its home device, for the ones that have one."""
    devices = cast("Devices | None", app.extensions.get("remote_devices"))
    if devices is None:
        return {}
    homes = await devices.homes(session_ids)
    if not homes:
        return {}
    control = cast("RemoteControl | None", app.extensions.get("remote_control"))
    named = {device.id: device.name for device in await devices.paired(everything=True)}
    out: dict[str, dict[str, Any]] = {}
    for session_id, home in homes.items():
        out[session_id] = {
            "device_id": home.device_id,
            "device_name": named.get(home.device_id, ""),
            "cwd": home.cwd,
            # The session's own: a connection of its home device carries it, which is what its calls
            # need; a laptop that is up with this conversation closed has it disconnected.
            "connected": control.carrier(home.device_id, session_id) is not None if control is not None else False,
            "disconnected_at": control.session_disconnected_at(home.device_id, session_id) if control is not None else None,
            "guard": control.guard_of(home.device_id, session_id) if control is not None else None,
            "waiting": control.waiting_of(session_id) if control is not None else None,
        }
    return out


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    """The routes, and the door in front of every route of the app."""
    manager = app.manager
    assert manager is not None
    api.add_middleware(DeviceDoor, devices=lambda: cast("Devices | None", app.extensions.get("remote_devices")))

    def devices() -> Devices:
        found = app.extensions.get("remote_devices")
        if found is None:
            raise HTTPException(503, "the terminal client's devices are not installed")
        return cast("Devices", found)

    def control() -> RemoteControl | None:
        return cast("RemoteControl | None", app.extensions.get("remote_control"))

    def address(request: Request) -> str:
        return request.client.host if request.client else ""

    async def device_views(found: Devices, rows: list[Any]) -> list[dict[str, Any]]:
        counts = await found.session_counts()
        live = control()
        return [{**row.view(connected=live.connected(row.id) if live is not None else False, sessions=counts.get(row.id, 0)),
                 "disconnected_at": live.disconnected_at(row.id) if live is not None else None} for row in rows]

    async def session_rows(session_ids: list[str], device_id: str) -> list[dict[str, Any]]:
        """The ``/resume`` rows of these sessions, in the order given."""
        if not session_ids:
            return []
        rows = {row["id"]: row for row in await manager.list_sessions(limit=len(session_ids), ids=session_ids)}
        views = await remote_views(app, session_ids)
        scoped = {binding.session_id for binding in await devices().scope(device_id)}
        out = []
        for session_id in session_ids:
            row = rows.get(session_id)
            if row is None:
                continue
            remote = views.get(session_id)
            out.append({
                "id": session_id, "title": row["title"], "kind": row["kind"], "status": row["status"],
                "last_message_at": row["last_message_at"], "created_at": row["created_at"], "remote": remote,
                "attached": session_id in scoped, "here": False,
            })
        return out

    # -- pairing: the public doors ---------------------------------------------------------------

    @api.post("/api/cli/pair/start")
    async def pair_start(body: PairStartBody, request: Request) -> dict[str, Any]:
        """A terminal's first contact: a device code for it to poll with, a user code for the operator."""
        try:
            found, device_code = await devices().start_pairing(
                name=body.name, hostname=body.hostname, os=body.os, arch=body.arch, client_version=body.client_version, address=address(request),
            )
        except Refused as exc:
            raise refusal(exc) from None
        base = public_base(app, request)
        code = shown_code(found.user_code)
        return {
            "device_code": device_code,
            "user_code": code,
            "verification_uri": f"{base}/app/?pair-device",
            "verification_uri_complete": f"{base}/app/?pair-device={code}",
            "expires_in": int(PAIRING_TTL.total_seconds()),
            "interval": POLL_INTERVAL,
        }

    @api.post("/api/cli/pair/poll")
    async def pair_poll(body: PairPollBody, request: Request) -> JSONResponse:
        """The terminal asks whether the operator decided; the token in the one answer that says yes."""
        try:
            answer = await devices().poll(body.device_code, address=address(request))
        except Refused as exc:
            return JSONResponse({"status": exc.code, "detail": str(exc)}, status_code=exc.status)
        return JSONResponse(answer, headers={"Cache-Control": "no-store"})

    # -- pairing and devices: the operator's ------------------------------------------------------

    @api.get("/api/cli/pairs")
    async def pairs(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = devices()
        now = found.now()
        return {"requests": [row.view(now) for row in await found.pending()]}

    @api.get("/api/cli/pair/{user_code}")
    async def pair_one(user_code: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = devices()
        row = await found.pairing(user_code)
        if row is None:
            raise HTTPException(404, "no such pairing request, or it ran out")
        return row.view(found.now())

    async def decide(user_code: str, *, approve: bool, name: str = "") -> dict[str, Any]:
        found = devices()
        try:
            decided = await found.decide(await found.pairing(user_code), approve=approve, via="app", name=name)
        except Refused as exc:
            raise refusal(exc) from None
        return {"ok": True, "request": decided.view(found.now())}

    @api.post("/api/cli/pair/{user_code}/approve")
    async def pair_approve(user_code: str, body: DecideBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator allows the terminal; it gets its token at its next poll, within seconds."""
        return await decide(user_code, approve=True, name=body.name if body is not None else "")

    @api.post("/api/cli/pair/{user_code}/deny")
    async def pair_deny(user_code: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await decide(user_code, approve=False)

    @api.get("/api/cli/devices")
    async def device_list(all: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:  # noqa: A002 — the query parameter's name is the contract's
        found = devices()
        return {"devices": await device_views(found, await found.paired(everything=all))}

    @api.patch("/api/cli/devices/{device_id}")
    async def device_rename(device_id: str, body: RenameBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = devices()
        try:
            renamed = await found.rename(device_id, body.name)
        except Refused as exc:
            raise refusal(exc) from None
        return (await device_views(found, [renamed]))[0]

    @api.delete("/api/cli/devices/{device_id}")
    async def device_revoke(device_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Unpair a terminal: its token stops, its relay closes, its marks go."""
        if not await devices().revoke(device_id):
            raise HTTPException(404, "no such device, or it was already revoked")
        return {"ok": True}

    # -- the device's own -------------------------------------------------------------------------

    @api.get("/api/cli/me")
    async def me(request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = devices()
        device = await found.get(device_of(request))
        if device is None:
            raise HTTPException(401, "this terminal is not paired")
        scope = await found.scope(device.id)
        return {
            "device": (await device_views(found, [device]))[0],
            "server": {"version": __version__, "contract": CONTRACT, "public_url": public_base(app, request)},
            "sessions": [binding.session_id for binding in scope],
        }

    @api.post("/api/cli/token/rotate")
    async def rotate(request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"token": await devices().rotate(device_of(request))}
        except Refused as exc:
            raise refusal(exc) from None

    @api.get("/api/cli/sessions")
    async def resume_list(
        request: Request,
        cwd: str = Query("", max_length=CWD_MAX),
        q: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=200),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        """What ``/resume`` offers: the operator's conversations, newest first, with which are this
        device's and which were started here, in this folder."""
        device_id = device_of(request)
        words = q.lower().split()
        rows = [
            row for row in await manager.list_sessions(limit=2000)
            if not row["metadata"].get("archived") and not row["metadata"].get("subagent_of")
            and all(word in str(row["title"]).lower() for word in words)
        ]
        rows.sort(key=lambda row: row["last_message_at"], reverse=True)
        listed = await session_rows([row["id"] for row in rows[:limit]], device_id)
        for row in listed:
            remote = row["remote"]
            row["here"] = bool(remote) and remote["device_id"] == device_id and (not cwd or remote["cwd"] == cwd)
        return {"sessions": listed}

    async def one_row(session_id: str, device_id: str) -> dict[str, Any]:
        rows = await session_rows([session_id], device_id)
        if not rows:
            raise HTTPException(404, "no such session")
        return rows[0]

    @api.post("/api/cli/sessions/{session_id}/attach")
    async def attach(session_id: str, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """``/resume``: the operator opens a conversation from this terminal, which puts it in the
        device's scope. Its hands stay where they are."""
        device_id = device_of(request)
        if await manager.get_state(session_id) is None:
            raise HTTPException(404, "no such session")
        await devices().attach(device_id, session_id)
        return await one_row(session_id, device_id)

    @api.delete("/api/cli/sessions/{session_id}/attach")
    async def detach(session_id: str, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            removed = await devices().detach(device_of(request), session_id)
        except Refused as exc:
            raise refusal(exc) from None
        if not removed:
            raise HTTPException(404, "this conversation is not attached here")
        return {"ok": True}

    @api.post("/api/cli/sessions/{session_id}/hands")
    async def hands(session_id: str, body: HandsBody, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Continue a remote conversation on this machine: its tools run here, in ``cwd``, from its next
        turn. Refused while a turn of it is running, whose calls already went to the other device."""
        device_id = device_of(request)
        if not absolute_folder(body.cwd) or whole_disk(body.cwd):
            raise HTTPException(400, "cwd: an absolute folder on this machine, not a whole disk")
        if await manager.sessions.kind(session_id) != "remote":
            raise HTTPException(400, "only a remote conversation's tools run on a terminal; this one's run on the server")
        if session_id in manager.active_sessions():
            raise HTTPException(409, "a turn of this conversation is running; stop it or wait for it to end")
        await devices().move_home(device_id, session_id, body.cwd)
        remote_turns = app.extensions.get("remote_turns")
        if remote_turns is not None:
            remote_turns.bind(session_id, device_id, body.cwd)
        return await one_row(session_id, device_id)

    @api.put("/api/cli/sessions/{session_id}/guard")
    async def guard(session_id: str, body: GuardBody, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The local guard the operator chose in this terminal for this conversation (Shift+Tab)."""
        live = control()
        if live is None:
            raise HTTPException(503, "the terminal client's devices are not installed")
        await live.set_guard(device_of(request), session_id, body.guard)
        return {"guard": body.guard}


__all__ = ["CONTRACT", "absolute_folder", "register", "remote_views", "whole_disk"]
