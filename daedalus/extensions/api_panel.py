"""The side panel's door: ``/api/panel/*``, the only routes a panel token opens.

The extension's side panel frames the host's app at ``/app/panel`` and signs in with a panel token
(``daedalus.browser.panel``) in ``Authorization: Panel <token>``. The token is not the operator's: the
generic authentication never accepts it, so every other route stays the operator's alone. What it
does open is listed here and nowhere else:

- the conversations that use the operator's Chrome — listed, read, streamed, written to, stopped,
  answered, their model, effort and mode set, their pending actions allowed or refused;
- a new conversation, born in the operator's Chrome (the operator pressed "new" in the panel, which
  is their consent, asked once like the first switch);
- opening such a conversation's browser on the tab the operator just gave it;
- the model list, without any provider's address or key.

A conversation outside that set answers 404, as one that does not exist, so a stolen token cannot even
learn which other conversations there are. The routes that read and write a conversation are the
operator's own handlers, reached by rewriting the path after the checks rather than copied: the panel
cannot drift from the app, and the matrix test in ``tests/unit/test_panel_api.py`` is the one place
that says what the token reaches.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from daedalus.browser.model import USER, Owner, group_id
from daedalus.browser.panel import mark_panel_session
from daedalus.browser.user import CONSENT, ConsentNeeded

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.browser.panel import PanelGrants
    from daedalus.browser.service import Browsers

SCOPE_KEY = "daedalus.panel"
"""Set on a request's ASGI scope by the door alone, after it checked the token and the route. Nothing a
client sends can set it, which is what lets the operator's handlers trust it for the routes below."""
CONSENTED_KEY = "browser.panel.consented"
"""The operator said yes to the consent summary once, in the panel; it is not asked again."""

_SID = r"(?P<sid>[A-Za-z0-9_-]{1,64})"
_SESSION = rf"/api/panel/sessions/{_SID}"

FORWARDED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", _SESSION),
        ("GET", _SESSION + "/stream"),
        ("GET", _SESSION + r"/tool-results/[A-Za-z0-9_.:-]{1,200}"),
        ("GET", _SESSION + r"/policy/similar/[^/]{1,512}"),
        ("POST", _SESSION + "/messages"),
        ("POST", _SESSION + "/upload"),
        ("POST", _SESSION + "/stop"),
        ("POST", _SESSION + "/answer"),
        ("POST", _SESSION + "/model"),
        ("POST", _SESSION + "/mode"),
        ("POST", _SESSION + "/policy/grant"),
        ("POST", _SESSION + "/policy/grant-similar"),
        ("POST", _SESSION + "/policy/refuse"),
        ("GET", _SESSION + "/browser-approvals"),
        ("PUT", _SESSION + "/browser-approvals"),
        ("GET", "/api/panel/modes"),
        # Dictation: whether it is offered, and a voice note heard in a conversation or before one.
        ("GET", "/api/panel/asr"),
        ("POST", _SESSION + "/transcribe"),
        ("DELETE", _SESSION + r"/transcribe/[A-Za-z0-9_.-]{1,80}"),
        ("POST", "/api/panel/transcribe"),
        ("DELETE", r"/api/panel/transcribe/[A-Za-z0-9_.-]{1,80}"),
    )
)
"""The operator's routes the panel reaches, each the same path without ``/panel``."""

PANEL_UPLOAD_MAX = 64 << 20
"""The most a side panel may send in one request: files for a message, a voice note. The app's own
upload has no cap of its own, but a panel token is a credential living in an extension's frame, and a
stolen one must not be a way to fill this host's disk; a message's pictures and documents fit."""

_BODY_ROUTES = re.compile(_SESSION + r"/(upload|transcribe)|/api/panel/transcribe")

OWN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", "/api/panel/sessions"),
        ("POST", "/api/panel/sessions"),
        ("POST", _SESSION + "/tab"),
        ("GET", "/api/panel/settings"),
    )
)
"""The panel's own routes, below."""


def panel_token(header: str) -> str:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme == "Panel" else ""


async def _answer(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class PanelDoor:
    """ASGI middleware in front of the app: every ``/api/panel/*`` request passes here first."""

    def __init__(self, asgi: Any, *, grants: Callable[[], PanelGrants | None], in_scope: Callable[[str], Awaitable[bool]]) -> None:
        self.asgi = asgi
        self.grants = grants
        self.in_scope = in_scope

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not path.startswith("/api/panel/"):
            # A client cannot set this itself, but a request elsewhere must not carry one either.
            scope.pop(SCOPE_KEY, None)
            await self.asgi(scope, receive, send)
            return
        header = next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == b"authorization"), "")
        grants = self.grants()
        device = await grants.check(panel_token(header)) if grants is not None else None
        if device is None:
            await _answer(send, 401, "the side panel's credential is missing, expired or revoked; reopen the panel")
            return
        method = str(scope.get("method") or "")
        forwarded = next((rx.fullmatch(path) for m, rx in FORWARDED if m == method and rx.fullmatch(path)), None)
        own = None if forwarded else next((rx.fullmatch(path) for m, rx in OWN if m == method and rx.fullmatch(path)), None)
        match = forwarded or own
        if match is None:
            await _answer(send, 404, "not a side panel route")
            return
        sid = match.groupdict().get("sid")
        if sid and not await self.in_scope(sid):
            await _answer(send, 404, "no such conversation in this Chrome")
            return
        if method == "POST" and _BODY_ROUTES.fullmatch(path):
            declared = next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == b"content-length"), "")
            if declared.isdigit() and int(declared) > PANEL_UPLOAD_MAX:
                await _answer(send, 413, f"the side panel sends up to {PANEL_UPLOAD_MAX >> 20} MB at a time; larger files go through the app")
                return
            receive = _capped(receive, PANEL_UPLOAD_MAX)
        inner = dict(scope)
        inner[SCOPE_KEY] = {"device": device}
        if forwarded:
            target = "/api/" + path.removeprefix("/api/panel/")
            inner["path"] = target
            inner["raw_path"] = target.encode()
        await self.asgi(inner, receive, send)


def _capped(receive: Any, limit: int) -> Callable[[], Awaitable[dict[str, Any]]]:
    """``receive`` that ends the request once more than ``limit`` bytes came, for a body sent without
    a length (chunked) or with a false one: the handler sees the client gone, and nothing past the
    cap is read or written."""
    seen = 0

    async def wrapped() -> dict[str, Any]:
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body") or b"")
            if seen > limit:
                return {"type": "http.disconnect"}
        return cast("dict[str, Any]", message)

    return wrapped


class NewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=400)
    """The operator's first words, which name the chat until the model does."""
    consent: bool = False
    """The operator said yes to the consent summary; needed the first time only."""


def register(api: FastAPI, app: Application, *, new_body: type[BaseModel]) -> None:
    """Registered last, after the operator's routes, whose handlers it looks up by path. ``new_body``
    is the operator's ``POST /api/sessions`` body, which a new conversation here is made with."""
    manager = app.manager
    assert manager is not None

    def service() -> Browsers | None:
        return cast("Browsers | None", app.extensions.get("browser"))

    async def in_scope(session_id: str) -> bool:
        found = service()
        return found is not None and found.user_offered() and await found.choices.env(session_id) == USER

    api.add_middleware(PanelDoor, grants=lambda: cast("PanelGrants | None", app.extensions.get("browser_panel")), in_scope=in_scope)

    def panel(request: Request) -> dict[str, Any]:
        found = request.scope.get(SCOPE_KEY)
        if not isinstance(found, dict):
            raise HTTPException(401, "the side panel's credential is missing")
        return {"user_id": app.settings.owner_user_id, "via": "panel", **found}

    def endpoint(method: str, path: str) -> Callable[..., Awaitable[Any]]:
        for route in api.routes:
            if isinstance(route, APIRoute) and route.path == path and method in route.methods:
                return cast("Callable[..., Awaitable[Any]]", route.endpoint)
        raise RuntimeError(f"the panel needs the operator's route {method} {path}")

    new_session = endpoint("POST", "/api/sessions")

    def waits(session_id: str) -> bool:
        state = manager.live_state(session_id)
        return state is not None and (state.pending is not None or bool(state.metadata.get("policy_pending")))

    @api.get("/api/panel/sessions")
    async def panel_sessions(who: dict[str, Any] = Depends(panel)) -> dict[str, Any]:
        """The conversations that use the operator's Chrome, newest first, with their tab group."""
        found = service()
        ids = await found.choices.using_user() if found is not None and found.user_offered() else []
        rows = await manager.list_sessions(limit=200, ids=ids) if ids else []
        rows.sort(key=lambda row: row["last_message_at"], reverse=True)
        connected, name, _ = found.user_connection() if found is not None else (False, "", "")
        return {
            "sessions": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "last_message_at": row["last_message_at"],
                    "needs_you": row["status"] in ("waiting", "failed") or waits(row["id"]),
                    "group": group_id(Owner("session", row["id"], session_id=row["id"]), env=USER),
                }
                for row in rows
                if not row["metadata"].get("archived")
            ],
            "consented": bool(await app.db.kv_get(CONSENTED_KEY, False)),
            "consent": CONSENT,
            "chrome": {"connected": connected, "name": name},
        }

    @api.post("/api/panel/sessions")
    async def panel_new(body: NewBody, who: dict[str, Any] = Depends(panel)) -> dict[str, Any]:
        """A new conversation in the operator's Chrome, as the app's own "new chat" starts one: the
        default model, no project. The first one asks the consent summary once, as the first switch
        does; pressing "new" in the panel after reading it is the operator's yes."""
        found = service()
        if found is None or not found.user_offered():
            raise HTTPException(404, "this installation does not offer your Chrome")
        if not body.consent and not await app.db.kv_get(CONSENTED_KEY, False):
            raise ConsentNeeded("a conversation started in your Chrome works there signed in as you; say yes once", consent=CONSENT)
        title = " ".join(body.title.split())[:400]
        if not title:
            raise HTTPException(400, "a conversation started in the panel starts with the operator's first words")
        # A Chrome conversation: made in their Chrome by the operator's own create, which also moves it.
        created = await new_session(body=new_body(title=title, autotitle=True, kind="chrome"), _=who)
        session_id = str(created["id"])
        await found.switch_conversation(session_id, USER, consent=True)
        await mark_panel_session(manager, session_id)
        await app.db.kv_set(CONSENTED_KEY, True)
        return {**created, "group": group_id(Owner("session", session_id, session_id=session_id), env=USER)}

    @api.post("/api/panel/sessions/{session_id}/tab")
    async def panel_tab(session_id: str, who: dict[str, Any] = Depends(panel)) -> dict[str, Any]:
        """The operator gave the conversation the tab in front of them: the extension already put it in
        the conversation's tab group, and opening the browser now is what has the agent take it up
        before its first turn, rather than open a blank tab of its own beside it."""
        found = service()
        if found is None:
            raise HTTPException(404, "this installation has no browser")
        state = await manager.get_state(session_id)
        staff = str(state.metadata.get("staff_id") or "") if state is not None else ""
        owner = Owner("session", session_id, project_id=state.project.id if state is not None and state.project is not None else None, session_id=session_id, staff_id=staff or None)
        opened = await found.open(owner, actor="operator")
        return {"group": group_id(owner, env=USER), "created": bool(opened.get("created"))}

    @api.get("/api/panel/settings")
    async def panel_settings(who: dict[str, Any] = Depends(panel)) -> dict[str, Any]:
        """What the model picker reads, and nothing else of the settings: the presets, each provider's
        kind and name for its mark, and the default. No address, no key."""
        raw = app.config.model_dump(mode="json")
        providers = {pid: {"kind": conf.get("kind", ""), "name": conf.get("name") or "", "billing": conf.get("billing") or ""} for pid, conf in (raw.get("providers") or {}).items()}
        return {"presets": raw.get("presets") or {}, "providers": providers, "model": {"preset": (raw.get("model") or {}).get("preset", "")}}


__all__ = ["FORWARDED", "OWN", "PANEL_UPLOAD_MAX", "SCOPE_KEY", "PanelDoor", "panel_token", "register"]
