"""Custom pages over HTTP: the list the navigation draws, their management, their storage, and the
pages themselves at ``/pages/<slug>/``.

The pages are served outside ``/app/`` because the app's service worker answers every navigation
under its scope with the cached app shell, and a page served there would never be fetched. They sit
behind the same sign-in as the API and on the same origin, so a page's own ``fetch('/api/…')`` works
with the browser's session cookie: the operator decided that pages get the whole API, as they are the
operator's own. Nothing here sandboxes a page or restricts what it may load.

A browser signed in by token or by Telegram holds no cookie, and a frame's navigation cannot carry a
header; ``POST /api/pages/_signin`` hands such a browser the session cookie before the app frames a page.

A page may have an agent of its own: one session bound by the manifest's ``session_id``, which the app
shows beside the page. ``/ask`` hands that session a message from the page, marked as the page's, and
``/emit`` pushes data the other way, to every open instance of the page.
"""

from __future__ import annotations

import hashlib
import html
import mimetypes
import posixpath
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile
from starlette.websockets import WebSocket

from daedalus.extensions.api_proxy import forward_http, forward_websocket
from daedalus.extensions.services import pid_alive
from daedalus.host.pages import (
    PageError,
    PageStore,
    Served,
    agent_brief,
    check_slug,
    clean_path,
    listing_row,
    store_for,
    title_text,
)
from daedalus.host.prompts import page_note
from daedalus.host.session_runner import Attachment
from daedalus.host.tool_groups import SESSION_LOADS_KEY

if TYPE_CHECKING:
    from daedalus.app import Application

KIT_FILES = frozenset({"kit.css", "daedalus.js"})
DRAFT_COOKIE = "daedalus_page_draft"
NAVIGATIONS = frozenset({"document", "iframe", "frame", "embed", "object"})
"""``Sec-Fetch-Dest`` values of a page being opened, as opposed to one of its files being fetched."""
FRAME_POLICY = "frame-ancestors 'self'"
"""The one policy a page is given: only the app (same origin) may frame it. No other directive, so a
page may load scripts, fonts, styles and sockets from anywhere it likes."""

# Types the platform's table may not know; everything else is the table's, and what nobody knows is a
# download (application/octet-stream). Registered once, at import, for every lookup in this process.
for _type, _extension in (
    ("application/wasm", ".wasm"), ("text/javascript", ".mjs"), ("text/javascript", ".cjs"), ("application/json", ".map"),
    ("application/manifest+json", ".webmanifest"), ("model/gltf-binary", ".glb"), ("model/gltf+json", ".gltf"),
    ("model/stl", ".stl"), ("model/obj", ".obj"), ("model/vnd.usdz+zip", ".usdz"), ("image/avif", ".avif"), ("image/webp", ".webp"),
    ("font/woff2", ".woff2"), ("font/woff", ".woff"), ("font/ttf", ".ttf"), ("font/otf", ".otf"), ("text/markdown", ".md"),
    ("text/csv", ".csv"), ("application/x-ndjson", ".ndjson"), ("audio/flac", ".flac"), ("audio/ogg", ".ogg"), ("video/webm", ".webm"),
    ("application/yaml", ".yaml"), ("application/yaml", ".yml"), ("application/toml", ".toml"),
):
    mimetypes.add_type(_type, _extension)


def content_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    return guessed or "application/octet-stream"


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=63)
    title: str | dict[str, str]
    kind: str = "static"
    entry: str = "index.html"
    target: int | str | None = None
    icon: str | None = None
    icon_path: str | None = None
    session_id: str | None = None


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | dict[str, str] | None = None
    kind: str | None = None
    entry: str | None = None
    target: int | str | None = None
    icon: str | None = None
    icon_path: str | None = None
    session_id: str | None = None


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class StorageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None


class AgentBody(BaseModel):
    """A new session made to be the page's agent."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """The session's name; the page's title when empty."""
    preset: str | None = None
    project_id: str | None = None
    """The project the agent works in; a folder of its own when empty."""


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=200_000)
    silent: bool = False
    follow_up: bool = False


class EmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1, max_length=64)
    data: Any = None


def _flag(value: Any) -> bool:
    """A form field's yes: a browser's FormData sends every value as text."""
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def manifest_view(manifest: dict[str, Any]) -> dict[str, Any]:
    """A page as ``GET /api/pages/<slug>`` answers it: the manifest with the addresses it is reached at."""
    slug = manifest["slug"]
    live = next((v for v in manifest["versions"] if v["n"] == manifest.get("version")), None)
    return {
        **listing_row(manifest),
        "title": manifest["title"],
        "kind": live["kind"] if live else manifest["draft"]["kind"],
        "entry": live["entry"] if live else manifest["draft"]["entry"],
        "target": live.get("target") if live else manifest["draft"].get("target"),
        "session_id": manifest.get("session_id"),
        "created_at": manifest.get("created_at"),
        "url": f"/pages/{slug}/",
        "draft_url": f"/pages/{slug}/?draft=1",
        "versions": list(reversed(manifest["versions"])),
        "draft": manifest["draft"],
    }


def _answer(exc: PageError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _plain_page(status: int, title: str, text: str, link: tuple[str, str] | None = None) -> HTMLResponse:
    """A small self-contained page for a refusal a person sees in a frame or a tab: not JSON."""
    more = f'<p><a href="{html.escape(link[0])}">{html.escape(link[1])}</a></p>' if link else ""
    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title></head>'
        '<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#151515;color:#eceae6;'
        f'font:14px/1.5 -apple-system,Segoe UI,sans-serif;padding:24px;text-align:center"><div><p>{html.escape(text)}</p>{more}</div></body></html>'
    )
    return HTMLResponse(body, status_code=status, headers={"Content-Security-Policy": FRAME_POLICY, "X-Robots-Tag": "noindex", "Cache-Control": "no-store"})


def link_page(title: str, url: str) -> str:
    """What a link page serves: the site in a frame filling the page, and a way out when it refuses
    to be framed (which the frame cannot tell its parent)."""
    t, u = html.escape(title), html.escape(url, quote=True)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{t}</title><style>html,body{{margin:0;height:100%;background:#151515}}iframe{{border:0;width:100%;height:100%;display:block}}"
        "a{position:fixed;right:8px;bottom:8px;font:12px/1.4 -apple-system,Segoe UI,sans-serif;color:#eceae6;background:rgba(0,0,0,.55);"
        "padding:4px 8px;border-radius:6px;text-decoration:none;opacity:.6}a:hover{opacity:1}</style></head>"
        f'<body><iframe src="{u}" allow="clipboard-read; clipboard-write; fullscreen; geolocation; camera; microphone" allowfullscreen></iframe>'
        f'<a href="{u}" target="_blank" rel="noopener">{u}</a></body></html>'
    )


def static_file(served: Served, path: str, headers: dict[str, str]) -> Response:
    """A file of the served side. A folder's address is its ``index.html`` (the page's entry at the
    root); an address with no extension that names no file is the entry, so an app with its own
    router keeps working on a reload of any of its addresses."""
    wanted = path
    if not wanted or wanted.endswith("/"):
        wanted = served.entry if not wanted else wanted + "index.html"
    try:
        target = PageStore._inside(served.root, posixpath.normpath(wanted) if wanted else served.entry)
    except PageError:
        return _plain_page(404, "Daedalus", "There is no such file in this page.")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file() and "." not in Path(wanted).name:
        target = served.root / served.entry
    if not target.is_file():
        return _plain_page(404, "Daedalus", "There is no such file in this page.")
    return FileResponse(target, media_type=content_type(target.name), headers=headers)


def register(
    api: FastAPI,
    app: Application,
    auth: Callable[..., Any],
    *,
    sign_in: Callable[[Response, Request], Awaitable[None]],
    owner_cookie: Callable[[str | None], Awaitable[bool]],
    session_cookie: str,
) -> None:
    def store() -> PageStore:
        return store_for(app)

    async def call(work: Awaitable[Any]) -> Any:
        try:
            return await work
        except PageError as exc:
            raise _answer(exc) from None

    # -- the list and the management ------------------------------------------------------------

    @api.get("/api/pages")
    async def pages_list(all: bool = False, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The pages the navigation offers: those with a published version (``all`` adds the drafts)."""
        rows = [listing_row(m) for m in await store().list() if all or m.get("version") is not None]
        return {"pages": rows}

    @api.post("/api/pages/_signin")
    async def pages_sign_in(request: Request, who: dict[str, Any] = Depends(auth)) -> Response:
        """The session cookie for a browser signed in another way, so the app's frame can open a page."""
        response = JSONResponse({"ok": True})
        if who.get("via") != "cookie":
            await sign_in(response, request)
        return response

    @api.post("/api/pages")
    async def pages_create(body: CreateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        manifest = await call(store().create(body.slug, title=body.title, kind=body.kind, entry=body.entry, target=body.target,
                                             icon=body.icon, icon_path=body.icon_path, session_id=body.session_id))
        return await detail(manifest)

    def manager() -> Any:
        found = getattr(app, "manager", None)
        if found is None:
            raise HTTPException(503, "sessions are not available in this installation")
        return found

    async def agent_of(manifest: dict[str, Any]) -> dict[str, Any] | None:
        """The bound session as the page screen names it, or None; a session deleted since it was bound
        is reported as gone rather than dropped, so the screen can offer to bind another."""
        session_id = manifest.get("session_id")
        if not session_id:
            return None
        state = await manager().get_state(session_id)
        if state is None:
            return {"id": session_id, "title": "", "gone": True, "running": False}
        return {"id": session_id, "title": state.session.title, "gone": False, "running": bool(state.running)}

    async def detail(manifest: dict[str, Any]) -> dict[str, Any]:
        return {**manifest_view(manifest), "agent": await agent_of(manifest)}

    @api.get("/api/pages/{slug}")
    async def pages_detail(slug: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await detail(await call(store().require(slug)))

    @api.patch("/api/pages/{slug}")
    async def pages_update(slug: str, body: UpdateBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Change what describes a page; ``session_id`` binds the page's agent (null unbinds it)."""
        fields = body.model_dump(exclude_unset=True)
        if fields.get("session_id") and await manager().get_state(fields["session_id"]) is None:
            raise HTTPException(404, "no such session")
        return await detail(await call(store().update(slug, **fields)))

    @api.post("/api/pages/{slug}/agent")
    async def pages_new_agent(slug: str, body: AgentBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A new session made to be the page's agent, bound to it: the page in its brief, the page tools
        loaded from the start, and otherwise a session like any other, with its own model and prompt."""
        body = body or AgentBody()
        manifest = await call(store().require(slug))
        sessions = manager()
        title = " ".join(body.title.split()) or title_text(manifest["title"])
        metadata: dict[str, Any] = {
            # Made here, not in Telegram: without this, private-chat mode would deliver its answers to
            # the operator's direct messages.
            "telegram_detached": True,
            "brief": agent_brief(manifest),
            "page": slug,
            SESSION_LOADS_KEY: {"pages": "eager"},
        }
        try:
            state = await sessions.create_session(title, metadata=metadata, project_id=body.project_id or None)
        except KeyError as exc:
            raise HTTPException(404, "no such project") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        session_id = state.session.id
        if body.preset:
            try:
                await sessions.set_model(session_id, preset=body.preset)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        manifest = await call(store().update(slug, session_id=session_id))
        return {"session_id": session_id, "title": title, "page": await detail(manifest)}

    @api.post("/api/pages/{slug}/ask")
    async def pages_ask(slug: str, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A message from the page to its agent: JSON ``{text, silent, follow_up}``, or a form with the
        same fields and ``files``. It reaches the session marked as the page's (origin ``page:<slug>``
        and a note the model reads), so the agent knows the operator pressed something in the page
        rather than wrote in the chat. ``silent`` is the page reporting on its own: context for the
        agent, folded in the chat, never the answer to a question the agent asked, and never
        interrupting a turn under way."""
        manifest = await call(store().require(slug))
        session_id = manifest.get("session_id")
        if not session_id:
            raise HTTPException(409, "this page has no agent: bind a session to it, or make one with POST /api/pages/<slug>/agent")
        uploads: list[UploadFile] = []
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                asked = AskBody.model_validate(await request.json())
            except (ValidationError, ValueError) as exc:
                raise HTTPException(422, f"the message is not understood: {exc}") from None
            text, silent, follow_up = asked.text, asked.silent, asked.follow_up
        else:
            form = await request.form()
            text = str(form.get("text") or "")
            silent, follow_up = _flag(form.get("silent")), _flag(form.get("follow_up"))
            uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        sessions = manager()
        staging = app.settings.state_dir / "upload-staging"
        attachments: list[Attachment] = []
        try:
            if uploads:
                staging.mkdir(parents=True, exist_ok=True)
            for upload in uploads:
                name = Path((upload.filename or "file").replace("\\", "/")).name or "file"
                target = staging / f"{secrets.token_hex(16)}.part"
                digest = hashlib.sha256()
                with target.open("wb") as handle:
                    while chunk := await upload.read(1 << 20):
                        digest.update(chunk)
                        handle.write(chunk)
                attachments.append(Attachment(path=target, name=name, content_sha256=digest.hexdigest(),
                                              mime_type=upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"))
            said = text.strip() or ("Files attached." if len(attachments) > 1 else "File attached." if attachments else "")
            if not said:
                raise HTTPException(400, "nothing to send")
            run_id = await sessions.submit(
                session_id,
                page_note(slug, title_text(manifest["title"]), silent=silent) + said,
                attachments,
                follow_up=follow_up or silent,
                as_answer=not silent,
                origin=f"page:{slug}",
                via="page",
                # The operator's own request is quoted as theirs when the chat is compacted; the page's
                # report is not, exactly as a reminder's text is not.
                operator_words=None if silent else [said],
            )
        except KeyError as exc:
            raise HTTPException(404, "the page's agent session is gone; bind another one") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            for attachment in attachments:
                attachment.path.unlink(missing_ok=True)
        return {"run_id": run_id, "session_id": session_id}

    @api.post("/api/pages/{slug}/emit")
    async def pages_emit(slug: str, body: EmitBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Push data to every open instance of the page (``Daedalus.on(event)`` there); nothing is kept."""
        await call(store().emit(slug, body.event, body.data))
        return {"sent": True}

    @api.delete("/api/pages/{slug}")
    async def pages_remove(slug: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await call(store().remove(slug)):
            raise HTTPException(404, f"no page {slug!r}")
        return {"removed": slug}

    @api.post("/api/pages/{slug}/publish")
    async def pages_publish(slug: str, body: PublishBody | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return manifest_view(await call(store().publish(slug, body.note if body else "")))

    @api.post("/api/pages/{slug}/rollback")
    async def pages_rollback(slug: str, body: RollbackBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return manifest_view(await call(store().rollback(slug, body.version)))

    @api.get("/api/pages/{slug}/files")
    async def pages_files(slug: str, version: int | None = Query(default=None, ge=1), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The draft's files, or a published version's."""
        return {"files": await call(store().list_files(slug, version))}

    @api.put("/api/pages/{slug}/draft/{path:path}")
    async def pages_write(slug: str, path: str, request: Request, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One draft file, the request's body as it is: any type, any size within the caps, streamed to disk."""
        return await call(store().write_stream(slug, path, request.stream()))

    @api.get("/api/pages/{slug}/draft/{path:path}")
    async def pages_read(slug: str, path: str, _: dict[str, Any] = Depends(auth)) -> FileResponse:
        await call(store().require(slug))
        try:
            target = store().draft_file(slug, path)
        except PageError as exc:
            raise _answer(exc) from None
        if not target.is_file():
            raise HTTPException(404, f"the draft has no {path!r}")
        return FileResponse(target, media_type=content_type(path), headers={"Cache-Control": "no-store"})

    @api.delete("/api/pages/{slug}/draft/{path:path}")
    async def pages_delete_file(slug: str, path: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await call(store().delete_file(slug, path)):
            raise HTTPException(404, f"the draft has no {path!r}")
        return {"deleted": clean_path(path)}

    # -- a page's storage -----------------------------------------------------------------------

    @api.get("/api/pages/{slug}/storage")
    async def pages_storage_list(slug: str, prefix: str = Query(default="", max_length=512), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return {"keys": await call(store().storage_list(slug, prefix))}

    @api.get("/api/pages/{slug}/storage/{key:path}")
    async def pages_storage_get(slug: str, key: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        found = await call(store().storage_get(slug, key))
        if found is None:
            raise HTTPException(404, f"nothing is stored under {key!r}")
        return found  # type: ignore[no-any-return]

    @api.put("/api/pages/{slug}/storage/{key:path}")
    async def pages_storage_set(slug: str, key: str, body: StorageBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await call(store().storage_set(slug, key, body.value))  # type: ignore[no-any-return]

    @api.delete("/api/pages/{slug}/storage/{key:path}")
    async def pages_storage_delete(slug: str, key: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not await call(store().storage_delete(slug, key)):
            raise HTTPException(404, f"nothing is stored under {key!r}")
        return {"deleted": key}

    # -- the kit --------------------------------------------------------------------------------

    kit_dir = app.settings.bot_repo_dir / "miniapp" / "dist" / "kit"

    @api.get("/pages/_kit/{name}")
    async def pages_kit(name: str) -> FileResponse:
        """The optional component layer and bridge, built with the app so they never fall behind its
        tokens. Not behind the sign-in: they are as public as the app's own scripts and hold nothing."""
        if name not in KIT_FILES or not (kit_dir / name).is_file():
            raise HTTPException(404, "no such kit file")
        return FileResponse(kit_dir / name, media_type=content_type(name), headers={"Cache-Control": "no-cache"})

    # -- the pages themselves -------------------------------------------------------------------

    @api.get("/pages/{slug}")
    async def pages_root(slug: str, request: Request) -> RedirectResponse:
        return RedirectResponse(f"/pages/{slug}/" + (f"?{request.url.query}" if request.url.query else ""))

    def wants_draft(request: Request) -> bool | None:
        """True or False when the address says which side to serve, None when it does not."""
        asked = request.query_params.get("draft")
        if asked is None:
            return None
        return asked not in ("", "0", "false", "no")

    async def service_port(manifest: dict[str, Any], served: Served) -> int:
        if isinstance(served.target, int):
            return served.target
        services = app.extensions.get("services")
        rows: list[dict[str, Any]] = await services.rows(manifest.get("session_id")) if services is not None else []  # type: ignore[attr-defined]
        named = [r for r in rows if r.get("name") == served.target]
        if not named:
            raise HTTPException(503, f"no service {served.target!r} is known to this installation")
        running = [r for r in named if r.get("status") == "running" and pid_alive(r.get("pid")) and r.get("port")]
        if not running:
            raise HTTPException(503, f"the service {served.target!r} is not running")
        return int(running[0]["port"])

    @api.api_route("/pages/{slug}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def pages_serve(slug: str, path: str, request: Request) -> Response:
        try:
            await auth(request)
        except HTTPException as exc:
            return _plain_page(exc.status_code, "Daedalus", "Sign in to Daedalus to open this page.", ("/app/", "Open Daedalus"))
        try:
            check_slug(slug)
            manifest = await store().require(slug)
        except PageError:
            return _plain_page(404, "Daedalus", "There is no such page.")
        asked = wants_draft(request)
        navigation = request.headers.get("sec-fetch-dest", "") in NAVIGATIONS
        # The draft is asked for once, on the page's address; its scripts, styles and fetches ask by
        # relative addresses that cannot carry the question, so a cookie scoped to the page carries it
        # for them. Opening the page again without the question clears it.
        draft = asked if asked is not None else (not navigation and request.cookies.get(DRAFT_COOKIE) == "1")
        served = store().served(manifest, draft=draft)
        if served is None:
            return _plain_page(404, "Daedalus", "This page has nothing published yet.")
        if served.kind == "service":
            port = await service_port(manifest, served)
            upstream = f"http://127.0.0.1:{port}/{path}" + (f"?{request.url.query}" if request.url.query else "")
            return await forward_http(request, upstream, f"/pages/{slug}", strip_cookies=(session_cookie, DRAFT_COOKIE), page=True, read_timeout=None)
        if request.method not in ("GET", "HEAD"):
            return _plain_page(405, "Daedalus", "This page is files; it takes no writes.")
        headers = {"Content-Security-Policy": FRAME_POLICY, "X-Robots-Tag": "noindex", "Cache-Control": "no-store" if draft else "no-cache"}
        if served.kind == "link":
            response: Response = HTMLResponse(link_page(title_text(manifest["title"]), str(served.target)), headers=headers)
        else:
            response = static_file(served, path, headers)
        if navigation and asked is not None:
            if asked:
                response.set_cookie(DRAFT_COOKIE, "1", path=f"/pages/{slug}/", httponly=True, samesite="lax", secure=request.url.scheme == "https")
            else:
                response.delete_cookie(DRAFT_COOKIE, path=f"/pages/{slug}/")
        elif navigation and request.cookies.get(DRAFT_COOKIE):
            response.delete_cookie(DRAFT_COOKIE, path=f"/pages/{slug}/")
        return response

    @api.websocket("/pages/{slug}/{path:path}")
    async def pages_socket(websocket: WebSocket, slug: str, path: str) -> None:
        """A service page's WebSocket, relayed to the service. Signed in by the session cookie, which a
        browser sends with a socket to its own origin; a socket opened from another site is refused,
        because a cookie alone would otherwise let any page the operator visits drive the service."""
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host", ""):
            await websocket.close(code=1008)
            return
        if not await owner_cookie(websocket.cookies.get(session_cookie)):
            await websocket.close(code=1008)
            return
        try:
            manifest = await store().require(check_slug(slug))
        except PageError:
            await websocket.close(code=1008)
            return
        served = store().served(manifest, draft=websocket.cookies.get(DRAFT_COOKIE) == "1")
        if served is None or served.kind != "service":
            await websocket.close(code=1008)
            return
        try:
            port = await service_port(manifest, served)
        except HTTPException:
            await websocket.close(code=1011)
            return
        query = websocket.url.query
        upstream = f"ws://127.0.0.1:{port}/{path}" + (f"?{query}" if query else "")
        await forward_websocket(websocket, upstream, f"/pages/{slug}", strip_cookies=(session_cookie, DRAFT_COOKIE))


__all__ = ["FRAME_POLICY", "content_type", "link_page", "manifest_view", "register", "static_file"]
