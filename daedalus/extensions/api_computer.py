"""The routes of the Computer control in a conversation: whether this computer can be driven, the apps
on its screen to choose a scope from, a grant of control to one conversation, Stop, Resume, and the
system's permission prompts.

Only the native application offers this, and only through the desktop launcher: the operator's token
for the desktop daemon is the launcher's alone, and the launcher speaks for the operator here
(``launcher_bridge.computer``). The host never reads that token and never sees the launcher's in a
log, an answer or an error. A refusal from the daemon reaches the app as 409 with the daemon's code
and, beside it, the sentence the agent's tool would read for the same code (``refusals.explain``), so
the app and the model never disagree about what happened. Beside the English sentence goes the key
the app renders in the operator's language (``refusals.words``); an unavailable desktop says its
reason the same way (``reason_words``), with the English ``reason`` kept as the detail.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from daedalus.computer import refusals
from daedalus.host import launcher_bridge

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

MAX_MINUTES = 240
"""The daemon's own ceiling on a lease (``max_lease_ms``, four hours by default). Refused here first,
so a mistyped number is a plain form error rather than a refusal from the desktop."""

PREVIEW_EDGE = 480
PREVIEW_MAX_EDGE = 960
"""A preview's long edge in pixels: the daemon's default and its cap. Larger is refused here as a form
error rather than shrunk without saying."""

SETTINGS_LINKS = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}
"""The panes of macOS's System Settings where the two grants live. The application is signed ad hoc,
so macOS forgets both after every update and the operator has to come back to these."""

NOT_NATIVE = "Computer use is offered only in the Daedalus desktop app, on the computer it runs on."
NO_LAUNCHER = "The desktop launcher is not running, so nothing can reach this computer's desktop."

NOT_NATIVE_WORDS = {"key": "cu.reason.not_native", "vars": {}}
NO_LAUNCHER_WORDS = {"key": "cu.reason.no_launcher", "vars": {}}
NO_DAEMON_WORDS = {"key": "cu.reason.no_daemon", "vars": {}}
"""The operator's words for each way the desktop can be out of reach, as keys of the app's dictionary.
The launcher's own reasons (no daemon in this build, a Wayland session, a daemon that stopped) are
English sentences from another program; they stay as the detail under the one translated sentence."""


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["window", "app", "display", "desktop"]
    window: str | None = Field(default=None, max_length=64)
    app: str | None = Field(default=None, max_length=512)
    display: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def names_its_target(self) -> Scope:
        # A window or app scope with nothing named would be refused by the daemon with a code; said
        # here it is a form error the app shows by the field.
        if self.kind in ("window", "app", "display") and not getattr(self, self.kind):
            raise ValueError(f"a {self.kind} scope names the {self.kind}")
        return self


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    scope: Scope
    minutes: int = Field(ge=1, le=MAX_MINUTES)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    which: Literal["screen", "accessibility", "input_monitoring"]


def without_protected(answer: dict[str, Any]) -> dict[str, Any]:
    """The launcher's apps as the app may show them: no protected app (Daedalus itself), and no
    foreground window of one. The daemon lists them to the operator so a grant is never scoped to one
    by mistake; leaving them out of the choice is the simpler way to the same end."""
    apps = [a for a in answer.get("apps") or [] if isinstance(a, dict) and not a.get("protected")]
    shown = {str(w.get("id")) for a in apps for w in a.get("windows") or [] if isinstance(w, dict)}
    out: dict[str, Any] = {"apps": apps}
    foreground = answer.get("foreground")
    if isinstance(foreground, dict) and str(foreground.get("id")) in shown:
        out["foreground"] = foreground
    return out


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    settings = app.settings

    async def launcher() -> launcher_bridge.Launcher:
        """The launcher, or 503 with the reason; outside the native application, 503 as well, since
        the app only calls these once ``GET /api/computer`` said the desktop is available."""
        if not settings.native:
            raise HTTPException(503, NOT_NATIVE)
        found = await asyncio.to_thread(launcher_bridge.read, settings.state_dir)
        if found is None:
            raise HTTPException(503, NO_LAUNCHER)
        return found

    def refused(exc: launcher_bridge.DesktopRefused) -> JSONResponse:
        explanation = refusals.explain(exc.code, exc.message, exc.data)
        # `detail` carries the sentence too, so every error path of the app reads it without
        # knowing this route; `error` is the contract's whole refusal, and `words` is what the
        # Computer control shows in the operator's language.
        error = {"code": exc.code, "message": exc.message, "data": exc.data, "explanation": explanation, "words": refusals.words(exc.code, exc.data)}
        return JSONResponse({"detail": explanation, "error": error}, status_code=409)

    async def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | JSONResponse:
        found = await launcher()
        try:
            return await launcher_bridge.computer(found, method, path, body)
        except launcher_bridge.DesktopRefused as exc:
            return refused(exc)
        except (launcher_bridge.DesktopUnavailable, launcher_bridge.LauncherUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None

    @api.get("/api/computer")
    async def computer_state(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Whether this computer can be driven, and if so its permissions, the lease, the operator's
        pause and the displays. Never an error: an unavailable desktop is ``available: false`` with a
        sentence, and the key the app shows it by in the operator's language."""
        if not settings.native:
            return {"native": False, "available": False, "reason": NOT_NATIVE, "reason_words": NOT_NATIVE_WORDS}
        found = await asyncio.to_thread(launcher_bridge.read, settings.state_dir)
        if found is None:
            return {"native": True, "available": False, "reason": NO_LAUNCHER, "reason_words": NO_LAUNCHER_WORDS}
        try:
            state = await launcher_bridge.computer(found, "GET", "/api/computer")
        except launcher_bridge.DesktopRefused as exc:
            return {"native": True, "available": False, "reason": refusals.explain(exc.code, exc.message, exc.data), "reason_words": refusals.words(exc.code, exc.data)}
        except (launcher_bridge.DesktopUnavailable, launcher_bridge.LauncherUnavailable) as exc:
            return {"native": True, "available": False, "reason": str(exc), "reason_words": NO_DAEMON_WORDS}
        if not state.get("available", True):
            return {"native": True, "available": False, "reason": str(state.get("reason") or NO_LAUNCHER), "reason_words": NO_DAEMON_WORDS}
        platform = state.get("platform") if isinstance(state.get("platform"), dict) else {}
        os_name = str(platform.get("os") or "")
        out: dict[str, Any] = {
            "native": True,
            "available": True,
            "os": os_name,
            "session_type": str(platform.get("session_type") or ""),
            "permissions": state.get("permissions") or {},
            "secure": state.get("secure") or {"locked": False},
            "lease": state.get("lease") or {"active": False},
            "human": state.get("human") or {"paused": False},
            "displays": state.get("displays") or [],
        }
        if os_name == "darwin":
            out["settings_links"] = dict(SETTINGS_LINKS)
        return out

    @api.get("/api/computer/apps")
    async def computer_apps(_: dict[str, Any] = Depends(auth)) -> Any:
        """The apps and windows on screen, front to back, to choose a scope from."""
        answer = await call("GET", "/api/computer/apps")
        return answer if isinstance(answer, JSONResponse) else without_protected(answer)

    @api.post("/api/computer/grant")
    async def computer_grant(body: GrantBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """Control of the computer to one conversation for a number of minutes, within a scope. A grant
        ends any earlier one, whichever conversation held it."""
        manager = getattr(app, "manager", None)
        if manager is None or await manager.get_state(body.session_id) is None:
            raise HTTPException(404, "no such session")
        scope = body.scope.model_dump(exclude_none=True)
        answer = await call("POST", "/api/computer/grant", {"session": body.session_id, "scope": scope, "ttl_ms": body.minutes * 60_000})
        if not isinstance(answer, JSONResponse):
            # The conversation becomes a computer one, its next run starts with the tools loaded, and a
            # run already going is told; a failure there leaves the grant standing.
            try:
                await manager.computer_granted(body.session_id)
            except Exception:  # noqa: BLE001 — the grant is the operator's and it was made
                logger.exception("after granting the computer to session %s", body.session_id)
        return answer

    @api.get("/api/computer/preview")
    async def computer_preview(
        display: str | None = Query(default=None, min_length=1, max_length=64),
        window: str | None = Query(default=None, min_length=1, max_length=64),
        max: int = Query(default=PREVIEW_EDGE, ge=64, le=PREVIEW_MAX_EDGE),
        _: dict[str, Any] = Depends(auth),
    ) -> Response:
        """A small JPEG of one display or one window, for the grant form's choice; no lease needed.
        Never cached: it is the operator's screen as it is now, and a stale one would mislead the choice."""
        if (display is None) == (window is None):
            raise HTTPException(400, "a preview names one display or one window")
        found = await launcher()
        params = {"display": display} if display is not None else {"window": str(window)}
        params["max"] = str(max)
        try:
            image = await launcher_bridge.computer_preview(found, params)
        except launcher_bridge.DesktopRefused as exc:
            return refused(exc)
        except (launcher_bridge.DesktopUnavailable, launcher_bridge.LauncherUnavailable) as exc:
            raise HTTPException(503, str(exc)) from None
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.post("/api/computer/revoke")
    async def computer_revoke(body: RevokeBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """Take control back: from the named conversation only, or from whichever holds it."""
        return await call("POST", "/api/computer/revoke", {"session": body.session_id} if body.session_id else {})

    @api.post("/api/computer/stop")
    async def computer_stop(_body: EmptyBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """The kill switch: the action in flight is cut short, everything held down is released, and
        the lease ends. Nothing more happens until a new grant."""
        return await call("POST", "/api/computer/stop", {})

    @api.post("/api/computer/resume")
    async def computer_resume(_body: EmptyBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """End the pause the operator's own mouse or keyboard caused, before the quiet period does."""
        return await call("POST", "/api/computer/resume", {})

    @api.post("/api/computer/permissions")
    async def computer_permissions(body: PermissionBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """Ask the system for one permission; on macOS it may show its own prompt, once per build."""
        return await call("POST", "/api/computer/permissions", {"which": body.which})


__all__ = ["MAX_MINUTES", "PREVIEW_EDGE", "PREVIEW_MAX_EDGE", "SETTINGS_LINKS", "register", "without_protected"]
