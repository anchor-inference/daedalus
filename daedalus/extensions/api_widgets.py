"""Widgets over HTTP: the values a widget keeps, and the widget made a custom page.

A widget's frame is sandboxed with an opaque origin and cannot call the API; the app does it for the
frame, on the bridge's requests, with its own sign-in. So these routes are the app's, behind the
same sign-in as everything else, and the widget id is the id of the tool call that drew it.

``/page`` takes the document from the app rather than looking the call up in the transcript: the
operator is signed in and can make any page with any document through the pages API already, so
reading the transcript would cost a session load for no protection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from daedalus.host.pages import PageError, store_for
from daedalus.host.widgets import (
    WidgetError,
    WidgetStorage,
    check_data,
    check_html,
    clean_id,
    clean_title,
    page_document,
    page_slug,
)

if TYPE_CHECKING:
    from daedalus.app import Application

SLUG_ATTEMPTS = 20
"""Slugs tried when another page takes the chosen one between the listing and the creation."""


class ValueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None


class PageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    html: str
    data: Any = None


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def storage() -> WidgetStorage:
        db = getattr(app, "db", None)
        if db is None:
            raise HTTPException(503, "widget storage needs the database")
        return WidgetStorage(db)

    def refused(exc: WidgetError | PageError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @api.get("/api/sessions/{session_id}/widgets/{widget_id}/storage")
    async def widget_values(session_id: str, widget_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return {"values": await storage().values(session_id, widget_id)}
        except WidgetError as exc:
            raise refused(exc) from exc

    @api.put("/api/sessions/{session_id}/widgets/{widget_id}/storage/{key:path}")
    async def widget_set(session_id: str, widget_id: str, key: str, body: ValueBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await storage().set(session_id, widget_id, key, body.value)
        except WidgetError as exc:
            raise refused(exc) from exc

    @api.delete("/api/sessions/{session_id}/widgets/{widget_id}/storage/{key:path}")
    async def widget_delete(session_id: str, widget_id: str, key: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            if not await storage().delete(session_id, widget_id, key):
                raise HTTPException(404, "nothing is stored under that key")
        except WidgetError as exc:
            raise refused(exc) from exc
        return {"deleted": True}

    @api.post("/api/sessions/{session_id}/widgets/{widget_id}/page")
    async def widget_page(session_id: str, widget_id: str, body: PageBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The widget as a published custom page whose agent is the widget's session, with its values copied."""
        kept = storage()
        try:
            clean_id(widget_id)
            title = clean_title(body.title)
            document = page_document(title, check_html(body.html), check_data(body.data))
            values = await kept.values(session_id, widget_id)
        except WidgetError as exc:
            raise refused(exc) from exc
        pages = store_for(app)
        taken = {str(m.get("slug")) for m in await pages.list()}
        slug = ""
        try:
            for _attempt in range(SLUG_ATTEMPTS):
                slug = page_slug(title, taken)
                try:
                    await pages.create(slug, title=title, icon="grid", session_id=session_id)
                    break
                except PageError as exc:
                    if exc.status != 409:
                        raise
                    taken.add(slug)
            else:
                raise HTTPException(409, "no free address for the page; try again")
            await pages.write_file(slug, "index.html", document.encode("utf-8"))
            for key, value in values.items():
                await pages.storage_set(slug, key, value)
            published = await pages.publish(slug, "Made from a widget in a conversation")
        except PageError as exc:
            raise refused(exc) from exc
        return {"slug": slug, "title": title, "path": f"/app/p/{slug}", "version": published["version"]}


__all__ = ["register"]
