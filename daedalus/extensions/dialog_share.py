"""Sharing a dialog the way a service is shared.

A session is private until the operator says otherwise. Then it is reachable through the
site at ``/c/<slug>`` — to anyone (``public``) or to whoever presents its key (``key``).
The slug is the only secret of a public share, so it is random and is not derived from the
title. Turning sharing off keeps the slug: the same link works again when it is turned back
on, and it answers nothing while the mode is ``local``.

The page is the dialog as it is now, including messages written after the link was created.
It shows what a reader of the conversation sees — the messages and the pictures in them —
and not the work around them. Tool calls, their results, thinking, files and the host's own
wake-ups stay in the app: a link that carried those would publish the workspace.
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Any

from daedalus.extensions.services import SHARE_MODES
from daedalus.security import redact
from daedalus.stores.media import MEDIA_TENANT

if TYPE_CHECKING:
    from daedalus.app import Application

PUBLIC_PAGE = 2000
"""Messages a shared page carries. A share is one response a stranger's browser holds, and a
session can be far longer than that; the page says when earlier messages were left off."""

PUBLIC_TEXT_LIMIT = 100_000
"""Characters of one message on the page. Past this the text is cut: the alternative is one
runaway answer becoming the whole document."""

_LOOP_HEAD = re.compile(r"^\s*\[Loop iteration \d+")
_CONTEXT = re.compile(r"<(?:turn_context|heartbeat)\b")
_HANDLE_HEADER = re.compile(r"^Attached files \(kept by handle[^\n]*\):\s*$")
_HANDLE_LINE = re.compile(r"^- (?:att:[0-9a-f]{12} .*|.+: not kept — .*)$")
_PLAIN_HEADER = re.compile(r"^Attached files:\s*$")
_PLAIN_LINE = re.compile(r"^- .+$")


def _cut_block(lines: list[str], header: re.Pattern[str], item: re.Pattern[str]) -> list[str]:
    at = next((i for i, line in enumerate(lines) if header.match(line)), -1)
    if at < 0:
        return lines
    end = at + 1
    while end < len(lines) and item.match(lines[end]):
        end += 1
    return lines[:at] + lines[end:]


def visible_text(text: str) -> str:
    """The operator's words, without the file list the host appends and without secrets.

    The list names paths and handles. It is how the app finds the files; on a public page it
    would publish them, so it is removed before anything else looks at the text.
    """
    lines = _cut_block(text.split("\n"), _HANDLE_HEADER, _HANDLE_LINE)
    lines = _cut_block(lines, _PLAIN_HEADER, _PLAIN_LINE)
    body = redact.redact("\n".join(lines)).strip()
    if len(body) > PUBLIC_TEXT_LIMIT:
        body = body[:PUBLIC_TEXT_LIMIT].rstrip() + "…"
    return body


def _user_message(view: dict[str, Any]) -> bool:
    """Whether this user row is a message a person sent, rather than the host waking the agent.

    The same distinction the conversation draws: a loop, a schedule, a reminder and the other
    notes the host writes are not part of the dialog, and neither is a message marked internal.
    """
    if view.get("internal") or view.get("summary"):
        return False
    origin = str(view.get("origin") or "")
    text = str(view.get("text") or "")
    if origin == "loop" or "<loop_instruction>" in text or _LOOP_HEAD.search(text):
        return False
    if _CONTEXT.search(text):
        return False
    if origin and origin != "operator" and not origin.startswith("inbound:"):
        return False
    return True


def _media(raw: Any, slug: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for presentation in raw:
        if not isinstance(presentation, dict) or not presentation.get("id"):
            continue
        items: list[dict[str, Any]] = []
        for item in presentation.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            remote = str(item.get("url") or "")
            if remote.startswith(("https://", "http://")):
                url = remote
            else:
                url = f"/c/{slug}/media/{presentation['id']}/{item['id']}"
            items.append(
                {
                    "id": str(item["id"]),
                    "kind": str(item.get("kind") or "image"),
                    "mime_type": str(item.get("mime_type") or ""),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "alt": redact.redact(str(item.get("alt") or ""))[:500],
                    "caption": redact.redact(str(item.get("caption") or ""))[:500],
                    "url": url,
                }
            )
        if items:
            layout = presentation.get("layout") if presentation.get("layout") in ("single", "album") else "single"
            out.append({"id": str(presentation["id"]), "layout": layout, "items": items})
    return out


def _widgets(calls: list[dict[str, Any]], failed: frozenset[str]) -> list[str]:
    """The titles of the widgets a message drew. A widget cannot run on a page a stranger opens (its
    stored values are the operator's), so the page names it and links to the app instead."""
    titles: list[str] = []
    for call in calls:
        if call.get("name") != "Widget" or str(call.get("id") or "") in failed:
            continue
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        title = " ".join(str(args.get("title") or "").split())[:120]
        titles.append(redact.redact(title) if title else "")
    return titles


def published_message(view: dict[str, Any], slug: str, failed: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """One transcript row as the shared page may show it, or None when the row is not the dialog.

    An assistant message that also called a tool is the note beside that call, which the
    conversation keeps folded. It is omitted, unless the message is where a picture was
    attached: the picture is part of the dialog and the text around it comes with it. A widget
    the message drew is kept as its title alone (``failed`` names the calls that were refused).
    """
    role = view.get("role")
    if role == "user":
        if not _user_message(view):
            return None
        text = visible_text(str(view.get("text") or ""))
        if not text:
            return None
        origin = str(view.get("origin") or "")
        via = origin[len("inbound:") :] if origin.startswith("inbound:") else ""
        return {"role": "user", "text": text, "at": str(view.get("created_at") or ""), "via": via[:80], "media": [], "widgets": []}
    if role != "assistant" or view.get("internal") or view.get("summary"):
        return None
    media = _media(view.get("media"), slug)
    calls = view.get("tool_calls") or []
    widgets = _widgets(calls, failed)
    if calls and not media and not widgets:
        return None
    text = visible_text(str(view.get("text") or "")) if media or not calls else ""
    if not text and not media and not widgets:
        return None
    return {"role": "assistant", "text": text, "at": str(view.get("created_at") or ""), "via": "", "media": media, "widgets": widgets}


def public_messages(views: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    failed = frozenset(str(result.get("id") or "") for view in views for result in view.get("tool_results") or [] if result.get("is_error"))
    return [item for view in views if (item := published_message(view, slug, failed)) is not None]


class DialogShares:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.public_base = app.settings.miniapp_public_url.strip().rstrip("/").removesuffix("/app")

    def share_url(self, row: dict[str, Any], *, with_key: bool = True) -> str | None:
        mode = row.get("share_mode") or "local"
        slug = row.get("share_slug")
        if mode == "local" or not slug:
            return None
        base = self.public_base or "<public-url>"
        url = f"{base}/c/{slug}"
        if mode == "key" and with_key and row.get("share_key"):
            url += f"?key={row['share_key']}"
        return url

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        mode = row.get("share_mode") or "local"
        if mode not in SHARE_MODES:
            mode = "local"
        return {
            "mode": mode,
            "slug": row.get("share_slug"),
            "key": row.get("share_key") if mode == "key" else None,
            "url": self.share_url(row),
            "public_base": self.public_base,
        }

    def allows(self, row: dict[str, Any], presented: str | None) -> bool:
        mode = row.get("share_mode") or "local"
        if mode == "public":
            return True
        if mode == "key" and row.get("share_key") and presented:
            return secrets.compare_digest(str(row["share_key"]), str(presented))
        return False

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        row = await self.app.db.fetchone("SELECT * FROM sessions WHERE share_slug = ?", (slug,))
        return dict(row) if row else None

    async def for_session(self, session_id: str) -> dict[str, Any]:
        row = await self.app.db.fetchone("SELECT share_mode, share_slug, share_key FROM sessions WHERE id = ?", (session_id,))
        return self.view(dict(row) if row else {})

    async def share(self, session_id: str, mode: str, *, rotate_key: bool = False) -> dict[str, Any]:
        """Switch who can open the dialog. The slug is minted once; the key only in key mode."""
        if mode not in SHARE_MODES:
            raise ValueError(f"share mode must be one of {', '.join(SHARE_MODES)}")
        row = await self.app.db.fetchone("SELECT id, share_mode, share_slug, share_key FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise KeyError(session_id)
        current = dict(row)
        slug = current.get("share_slug")
        if not slug:
            slug = secrets.token_urlsafe(18)
            while await self.by_slug(slug) is not None:
                slug = secrets.token_urlsafe(18)
        key = current.get("share_key")
        if mode == "key" and (not key or rotate_key):
            key = secrets.token_urlsafe(18)
        await self.app.db.execute(
            "UPDATE sessions SET share_mode = ?, share_slug = ?, share_key = ? WHERE id = ?",
            (mode, slug, key, session_id),
        )
        stored = await self.app.db.fetchone("SELECT share_mode, share_slug, share_key FROM sessions WHERE id = ?", (session_id,))
        return self.view(dict(stored) if stored else {})

    async def page(self, session_id: str, slug: str) -> dict[str, Any]:
        manager = self.app.manager
        if manager is None:
            raise RuntimeError("no session manager")
        state = await manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        views = await manager.transcript_page(session_id, tail=PUBLIC_PAGE)
        oldest, _newest = await manager.sessions.transcript_bounds(session_id)
        first = next((view.get("seq") for view in views if isinstance(view.get("seq"), int)), 0)
        return {
            "title": redact.redact(state.session.title or ""),
            "older": bool(first and oldest and int(first) > int(oldest)),
            "messages": public_messages(views, slug),
            # Where a widget's "open in the app" leads: the conversation, behind the app's sign-in.
            "app_path": f"/app/agents/{session_id}",
        }

    async def media(self, session_id: str, presentation_id: str, item_id: str) -> tuple[str, str] | None:
        """``(mime, location)`` for a picture that belongs to this dialog. Location is a file or an http(s) URL."""
        manager = self.app.manager
        if manager is None:
            return None
        item = await manager.media.item(session_id, presentation_id, item_id)
        if item is None:
            return None
        remote = str(item.get("source_url") or "")
        if remote.startswith(("https://", "http://")):
            return str(item.get("mime_type") or "application/octet-stream"), remote
        path = manager.blobs.path_of(MEDIA_TENANT, str(item.get("blob_ref") or ""))
        if not path.is_file():
            return None
        return str(item.get("mime_type") or "application/octet-stream"), str(path)


async def install(app: Application) -> list[Any]:
    app.extensions["dialogs"] = DialogShares(app)
    return []


__all__ = ["PUBLIC_PAGE", "DialogShares", "install", "public_messages", "published_message", "visible_text"]
