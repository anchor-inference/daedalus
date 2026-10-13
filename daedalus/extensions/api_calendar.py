"""Authenticated calendar and planner routes for the operator's web app."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from daedalus.extensions.calendar_sync import CalendarSync, describe
from daedalus.stores.calendar import OPERATOR, CalendarStore
from daedalus.stores.planner import PlannerStore

if TYPE_CHECKING:
    from daedalus.app import Application


class CalendarBody(BaseModel):
    name: str | None = None
    color: str | None = None
    visible: bool | None = None
    position: int | None = None
    default_reminders: list[int] | None = None


class EventBody(BaseModel):
    calendar_id: str | None = None
    account_id: str | None = None
    title: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    timezone: str | None = None
    all_day: bool | None = None
    location: str | None = None
    recurrence: str | None = None
    reminders: list[int] | None = None
    color: str | None = None
    version: int | None = None
    scope: Literal["all", "this"] = "all"
    occurrence_start: str | None = None
    type: str | None = None
    fields: dict[str, Any] | None = None
    blocks: list[Any] | None = None
    status: str | None = None
    tags: list[str] | None = None
    group_id: str | None = None
    links: list[dict[str, Any]] | None = None


class BulkBody(BaseModel):
    events: list[dict[str, Any]]


class BlocksBody(BaseModel):
    ops: list[Any]
    version: int | None = None


class StatusBody(BaseModel):
    status: str


class GroupBody(BaseModel):
    name: str | None = None
    icon: str | None = None
    color: str | None = None
    description: str | None = None
    blocks: list[Any] | None = None
    archived: bool | None = None
    hidden: bool | None = None
    version: int | None = None


class ShiftBody(BaseModel):
    days: int


class DuplicateBody(BaseModel):
    offset_days: int
    name: str | None = None


class SettingsBody(BaseModel):
    week_start: int | None = None
    work_start: str | None = None
    work_end: str | None = None
    default_view: str | None = None
    default_duration: int | None = None
    default_reminders: list[int] | None = None
    timezone: str | None = None
    show_weekends: bool | None = None


class AccountBody(BaseModel):
    provider: str
    name: str
    credentials: dict[str, str]
    remote_calendar_id: str = "primary"
    color: str = ""


class SubscriptionBody(BaseModel):
    name: str
    url: str
    color: str = ""


class OAuthBody(BaseModel):
    provider: str
    name: str
    client_id: str
    client_secret: str
    remote_calendar_id: str = "primary"


class ResolveBody(BaseModel):
    choice: str


class ListBody(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = None


class TaskBody(BaseModel):
    list_id: str | None = None
    title: str | None = None
    notes: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    duration: int | None = None
    priority: int | None = None
    reminders: list[int] | None = None
    recurrence: str | None = None
    position: float | None = None
    version: int | None = None


class CompleteBody(BaseModel):
    done: bool = True


def refused(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "not found")
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def zone_hint(app: Application | Any) -> Callable[[], str]:
    def hint() -> str:
        presence = getattr(getattr(app, "notifications", None), "presence", None)
        return presence.locale()[1] if presence is not None else ""

    return hint


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    calendar = CalendarStore(app.db, zone_hint(app))
    planner = PlannerStore(app.db)
    sync = CalendarSync(calendar)

    # -- calendars -------------------------------------------------------------------------------

    @api.get("/api/calendar/calendars")
    async def calendars(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await calendar.calendars()

    @api.post("/api/calendar/calendars", status_code=201)
    async def calendar_create(body: CalendarBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.create_calendar(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise refused(exc) from exc

    @api.patch("/api/calendar/calendars/{calendar_id}")
    async def calendar_update(calendar_id: str, body: CalendarBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.update_calendar(calendar_id, body.model_dump(exclude_unset=True))
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/calendar/calendars/{calendar_id}")
    async def calendar_delete(calendar_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await calendar.delete_calendar(calendar_id)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    # -- settings --------------------------------------------------------------------------------

    @api.get("/api/calendar/settings")
    async def settings(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await calendar.settings()

    @api.put("/api/calendar/settings")
    async def settings_save(body: SettingsBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.save_settings(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise refused(exc) from exc

    # -- events ----------------------------------------------------------------------------------

    @api.get("/api/calendar/events")
    async def events(
        start: str, end: str, calendars: str = "", type: str | None = None, group_id: str | None = None, tag: str | None = None,
        status: str | None = None, include_blocks: bool = False, _: dict[str, Any] = Depends(auth),
    ) -> list[dict[str, Any]]:
        try:
            return await calendar.occurrences(
                start, end, [item for item in calendars.split(",") if item] or None,
                filters={"type": type, "group_id": group_id, "tag": tag, "status": status}, include_blocks=include_blocks,
            )
        except ValueError as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/events/bulk", status_code=201)
    async def events_bulk(body: BulkBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"events": await calendar.bulk_create(body.events, OPERATOR)}
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/calendar/search")
    async def search(q: str = Query(..., max_length=200), limit: int = Query(30, ge=1, le=100), _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await calendar.search(q, limit)

    @api.get("/api/calendar/events/{event_id}")
    async def event(event_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await calendar.get(event_id)
        if found is None:
            raise HTTPException(404, "no such event")
        return found

    @api.post("/api/calendar/events", status_code=201)
    async def event_create(body: EventBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.create(body.model_dump(exclude_unset=True), OPERATOR)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.put("/api/calendar/events/{event_id}")
    async def event_update(event_id: str, body: EventBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.update(event_id, body.model_dump(exclude_unset=True), OPERATOR)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.patch("/api/calendar/events/{event_id}/blocks")
    async def event_blocks(event_id: str, body: BlocksBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.patch_blocks(event_id, body.ops, body.version, OPERATOR)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/events/{event_id}/status")
    async def event_status(event_id: str, body: StatusBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.set_status(event_id, body.status, OPERATOR)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.get("/api/calendar/events/{event_id}/history")
    async def event_history(event_id: str, limit: int = Query(200, ge=1, le=500), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"entries": await calendar.history(event_id, limit)}
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/events/{event_id}/blocks/{block_id}/run")
    async def event_action(event_id: str, block_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        # The operator's press is the confirmation the spec asks for: the prompt the agent wrote
        # into the block goes to the session it named, as the operator's own message, and nowhere else.
        manager = getattr(app, "manager", None)
        if manager is None:
            raise HTTPException(503, "the agent is not running")
        try:
            block, text = await calendar.action(event_id, block_id, OPERATOR)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc
        try:
            run_id = await manager.submit(str(block["session_id"]), text, as_answer=False, origin="operator", via="app")
        except KeyError as exc:
            raise HTTPException(404, "the session this action talks to no longer exists") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "session_id": block["session_id"], "run_id": run_id}

    # -- types, aggregates and groups ------------------------------------------------------------

    @api.get("/api/calendar/types")
    async def types(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"types": await calendar.type_list()}

    @api.put("/api/calendar/types/{type_id}")
    async def type_upsert(type_id: str, body: dict[str, Any], _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.upsert_type(type_id, body, "user")
        except ValueError as exc:
            raise refused(exc) from exc

    @api.delete("/api/calendar/types/{type_id}")
    async def type_delete(type_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await calendar.delete_type(type_id)
        except KeyError as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.get("/api/calendar/aggregates")
    async def aggregates(
        start: str = Query(..., alias="from"), end: str = Query(..., alias="to"), type: str | None = None, group_id: str | None = None,
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await calendar.aggregates(start, end, type, group_id)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/calendar/groups")
    async def groups(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"groups": await calendar.groups()}

    @api.post("/api/calendar/groups", status_code=201)
    async def group_create(body: GroupBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.create_group(body.model_dump(exclude_unset=True), OPERATOR)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.get("/api/calendar/groups/{group_id}")
    async def group(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.group(group_id)
        except KeyError as exc:
            raise refused(exc) from exc

    @api.patch("/api/calendar/groups/{group_id}")
    async def group_update(group_id: str, body: GroupBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.update_group(group_id, body.model_dump(exclude_unset=True))
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/calendar/groups/{group_id}")
    async def group_delete(group_id: str, events: Literal["keep", "delete"] = Query("keep"), _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await calendar.delete_group(group_id, events, OPERATOR)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/calendar/groups/{group_id}/shift")
    async def group_shift(group_id: str, body: ShiftBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.shift_group(group_id, body.days, OPERATOR)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/groups/{group_id}/duplicate", status_code=201)
    async def group_duplicate(group_id: str, body: DuplicateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.duplicate_group(group_id, body.offset_days, OPERATOR, body.name)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/calendar/events/{event_id}")
    async def event_delete(
        event_id: str, version: int | None = Query(None), scope: Literal["all", "this"] = Query("all"), occurrence_start: str | None = Query(None),
        _: dict[str, Any] = Depends(auth),
    ) -> dict[str, bool]:
        try:
            await calendar.delete(event_id, version, scope, occurrence_start)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/calendar/events/{event_id}/resolve")
    async def event_resolve(event_id: str, body: ResolveBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await sync.resolve(event_id, body.choice)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc
        except Exception as exc:
            raise HTTPException(502, f"calendar synchronization failed: {describe(exc)}") from exc

    # -- accounts and subscriptions --------------------------------------------------------------

    @api.get("/api/calendar/accounts")
    async def accounts(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await calendar.accounts()

    @api.post("/api/calendar/accounts", status_code=201)
    async def account_create(body: AccountBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.connect(body.provider, body.name, body.credentials, body.remote_calendar_id, body.color)
        except ValueError as exc:
            raise refused(exc) from exc

    @api.post("/api/calendar/subscriptions", status_code=201)
    async def subscription_create(body: SubscriptionBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await calendar.subscribe(body.name, body.url, body.color)
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
        except Exception as exc:
            raise HTTPException(502, f"calendar synchronization failed: {describe(exc)}") from exc

    # -- planner ---------------------------------------------------------------------------------

    @api.get("/api/planner/lists")
    async def lists(_: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        return await planner.lists()

    @api.post("/api/planner/lists", status_code=201)
    async def list_create(body: ListBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await planner.create_list(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise refused(exc) from exc

    @api.patch("/api/planner/lists/{list_id}")
    async def list_update(list_id: str, body: ListBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await planner.update_list(list_id, body.model_dump(exclude_unset=True))
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/planner/lists/{list_id}")
    async def list_delete(list_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await planner.delete_list(list_id)
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.get("/api/planner/tasks")
    async def tasks(view: str = "all", list_id: str | None = None, start: str | None = None, end: str | None = None, _: dict[str, Any] = Depends(auth)) -> list[dict[str, Any]]:
        try:
            return await planner.tasks(view, list_id, start, end, (await calendar.settings())["timezone"])
        except ValueError as exc:
            raise refused(exc) from exc

    @api.post("/api/planner/tasks", status_code=201)
    async def task_create(body: TaskBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await planner.create(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise refused(exc) from exc

    @api.put("/api/planner/tasks/{task_id}")
    async def task_update(task_id: str, body: TaskBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await planner.update(task_id, body.model_dump(exclude_unset=True))
        except (ValueError, KeyError, RuntimeError) as exc:
            raise refused(exc) from exc

    @api.delete("/api/planner/tasks/{task_id}")
    async def task_delete(task_id: str, version: int | None = Query(None), _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        try:
            await planner.delete(task_id, version)
        except (KeyError, RuntimeError) as exc:
            raise refused(exc) from exc
        return {"ok": True}

    @api.post("/api/planner/tasks/{task_id}/complete")
    async def task_complete(task_id: str, body: CompleteBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await planner.complete(task_id, body.done, (await calendar.settings())["timezone"])
        except (ValueError, KeyError) as exc:
            raise refused(exc) from exc


__all__ = ["register"]
