"""Authenticated calendar and diagram routes for the operator's web app."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from daedalus.extensions.calendar_sync import CalendarConflict, CalendarSync
from daedalus.stores.calendar import CalendarStore
from daedalus.stores.diagrams import DiagramStore

if TYPE_CHECKING:
    from daedalus.app import Application


class EventBody(BaseModel):
    title: str
    start_at: str
    end_at: str
    description: str = ""
    timezone: str = "UTC"
    all_day: bool = False
    location: str = ""
    recurrence: str = ""
    account_id: str | None = None
    version: int | None = None


class AccountBody(BaseModel):
    provider: str
    name: str
    credentials: dict[str, str]
    remote_calendar_id: str = "primary"


class OAuthBody(BaseModel):
    provider: str
    name: str
    client_id: str
    client_secret: str
    remote_calendar_id: str = "primary"


class DiagramBody(BaseModel):
    title: str
    scene: dict[str, Any] = Field(default_factory=lambda: {"elements": [], "appState": {}, "files": {}})
    version: int | None = None


class ResolveBody(BaseModel):
    choice: str


def refused(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "not found")
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    calendar = CalendarStore(app.db)
    sync = CalendarSync(calendar)
    diagrams = DiagramStore(app.db)

    @api.get("/api/calendar/events")
    async def events(start: str, end: str, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        try:
            return await calendar.events(start, end)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/calendar/events/{event_id}")
    async def event(event_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await calendar.get(event_id)
        if found is None:
            raise HTTPException(404, "no such event")
        return found

    @api.post("/api/calendar/events", status_code=201)
    async def event_create(body: EventBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.save(body.model_dump())
        except ValueError as exc:
            raise refused(exc) from exc

    @api.put("/api/calendar/events/{event_id}")
    async def event_update(event_id: str, body: EventBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.save(body.model_dump(exclude_unset=True), event_id)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/calendar/events/{event_id}")
    async def event_delete(event_id: str, version: int | None = Query(None), _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await calendar.delete(event_id, version)
        except (KeyError, RuntimeError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/calendar/events/{event_id}/resolve")
    async def event_resolve(event_id: str, body: ResolveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await sync.resolve(event_id, body.choice)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc
        except Exception as exc:
            raise HTTPException(502, f"calendar synchronization failed: {exc}") from exc

    @api.get("/api/calendar/accounts")
    async def accounts(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await calendar.accounts()

    @api.post("/api/calendar/accounts", status_code=201)
    async def account_create(body: AccountBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.connect(body.provider, body.name, body.credentials, body.remote_calendar_id)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/oauth/start")
    async def oauth_start(body: OAuthBody, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, str]:
        if body.provider not in ("google", "outlook") or not body.client_id or not body.client_secret:
            raise HTTPException(400, "choose Google or Outlook and provide the OAuth client credentials")
        state = secrets.token_urlsafe(32)
        public = app.settings.miniapp_public_url or str(request.base_url)
        parsed = urlparse(public)
        redirect_uri = f"{parsed.scheme}://{parsed.netloc}/api/calendar/oauth/callback"
        await app.db.kv_set(f"calendar_oauth:{state}", {
            "provider": body.provider, "name": body.name, "client_id": body.client_id,
            "client_secret": body.client_secret, "remote_calendar_id": body.remote_calendar_id,
            "redirect_uri": redirect_uri, "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        })
        if body.provider == "google":
            url = "https://accounts.google.com/o/oauth2/v2/auth"
            params = {"client_id": body.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": "https://www.googleapis.com/auth/calendar.events", "access_type": "offline", "prompt": "consent", "state": state}
        else:
            url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            params = {"client_id": body.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": "offline_access Calendars.ReadWrite", "response_mode": "query", "state": state}
        return {"url": url + "?" + urlencode(params), "redirect_uri": redirect_uri}

    @api.get("/api/calendar/oauth/callback")
    async def oauth_callback(state: str, code: str = "", error: str = "") -> RedirectResponse:
        async with app.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM kv WHERE key=? RETURNING value", (f"calendar_oauth:{state}",))
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(400, "this calendar connection has expired")
        pending = json.loads(row["value"])
        if datetime.fromisoformat(pending["expires_at"]) < datetime.now(UTC) or error or not code:
            raise HTTPException(400, "calendar authorization was cancelled or expired")
        provider = pending["provider"]
        token_url = "https://oauth2.googleapis.com/token" if provider == "google" else "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        payload = {key: pending[key] for key in ("client_id", "client_secret", "redirect_uri")}
        payload.update({"code": code, "grant_type": "authorization_code"})
        if provider == "outlook":
            payload["scope"] = "offline_access Calendars.ReadWrite"
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(token_url, data=payload)
            if response.is_error:
                raise HTTPException(502, "calendar provider rejected authorization")
            tokens = response.json()
        if not tokens.get("refresh_token"):
            raise HTTPException(502, "calendar provider did not grant offline access")
        await calendar.connect(provider, pending["name"], {
            "client_id": pending["client_id"], "client_secret": pending["client_secret"], "refresh_token": tokens["refresh_token"],
        }, pending["remote_calendar_id"])
        return RedirectResponse("/app/calendar?linked=1", status_code=303)

    @api.delete("/api/calendar/accounts/{account_id}")
    async def account_delete(account_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await sync.disconnect(account_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/calendar/accounts/{account_id}/sync")
    async def account_sync(account_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await sync.sync(account_id)
        except KeyError as exc:
            raise refused(exc) from exc
        except CalendarConflict as exc:
            raise refused(exc) from exc
        except Exception as exc:
            raise HTTPException(502, f"calendar synchronization failed: {exc}") from exc

    @api.get("/api/diagrams")
    async def diagram_list(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await diagrams.list()

    @api.post("/api/diagrams", status_code=201)
    async def diagram_create(body: DiagramBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.create(body.title, body.scene)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/diagrams/{diagram_id}")
    async def diagram_get(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await diagrams.get(diagram_id)
        if found is None:
            raise HTTPException(404, "no such diagram")
        return found

    @api.put("/api/diagrams/{diagram_id}")
    async def diagram_update(diagram_id: str, body: DiagramBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await diagrams.save(diagram_id, body.title, body.scene, body.version or 0)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/diagrams/{diagram_id}")
    async def diagram_delete(diagram_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await diagrams.delete(diagram_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"ok": True}


__all__ = ["register"]
