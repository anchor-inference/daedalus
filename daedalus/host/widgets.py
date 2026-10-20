"""Widgets: small interactive documents an agent puts into its answer with the ``Widget`` tool.

A widget is one HTML file (its own CSS and JavaScript inline) and optional JSON data. It lives in the
tool call that drew it, so the transcript is its only copy and its identity is the call's id; the app
renders it in a sandboxed frame with no network and talks to it over ``postMessage``. What the
operator enters into it is kept here, in ``widget_storage``, per session and widget, so a checklist
ticked yesterday is still ticked after a reload.

Promoting a widget makes it a custom page (``daedalus.host.pages``): the same document with a small
shim that answers the widget's calls through the page bridge, and the widget's stored values copied
into the page's own storage. The page is the operator's from then on, with the whole API, like any
other page; the sandbox is for what an agent wrote into a conversation without being asked to build
a screen.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from daedalus.slugs import page_slug_from
from daedalus.stores.database import Database

TOOL_NAME = "Widget"
TITLE_MAX = 120
HTML_MAX_BYTES = 128 * 1024
"""A widget is a calculator or a checklist, not an application: past this it belongs in a page,
which has no cap and can be built with a toolchain."""
DATA_MAX_BYTES = 64 * 1024
VALUE_MAX_BYTES = 64 * 1024
WIDGET_MAX_BYTES = 256 * 1024
"""Everything one widget may store, all keys together: the frame receives the whole of it when it
opens, so a widget cannot grow into a database that slows the conversation down."""
KEYS_MAX = 200
KEY_MAX = 200
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_NETWORK_HINTS = (
    (re.compile(r"""<script[^>]+src\s*=\s*["']?(?:https?:)?//""", re.I), "an external <script src>"),
    (re.compile(r"""<link[^>]+href\s*=\s*["']?(?:https?:)?//""", re.I), "an external stylesheet or font"),
    (re.compile(r"""<img[^>]+src\s*=\s*["']?(?:https?:)?//""", re.I), "an external image"),
    (re.compile(r"""@import\s+(?:url\()?\s*["']?(?:https?:)?//""", re.I), "a CSS @import from another site"),
    (re.compile(r"\bfetch\s*\(|\bXMLHttpRequest\b|\bnew\s+WebSocket\b|\bEventSource\b"), "fetch, XMLHttpRequest, WebSocket or EventSource"),
)


class WidgetError(ValueError):
    """A refusal with the HTTP status it answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_id(value: str, what: str = "widget id") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise WidgetError(f"a {what} is 1 to 128 letters, digits and . _ : -")
    return value


def clean_title(title: Any) -> str:
    text = " ".join(str(title or "").split())
    if not text or len(text) > TITLE_MAX:
        raise WidgetError(f"a widget's title is 1 to {TITLE_MAX} characters")
    return text


def check_html(html: Any) -> str:
    if not isinstance(html, str) or not html.strip():
        raise WidgetError("a widget needs its HTML: one document with its CSS and JavaScript inline")
    size = len(html.encode("utf-8"))
    if size > HTML_MAX_BYTES:
        raise WidgetError(f"the widget's HTML is {size} bytes; a widget holds at most {HTML_MAX_BYTES}. Something larger is a custom page (PageCreate)", 413)
    return html


def check_data(data: Any) -> str:
    """The data as the JSON the frame receives; refused when it is not JSON or too large."""
    try:
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WidgetError(f"a widget's data is plain JSON: {exc}") from exc
    if len(text.encode("utf-8")) > DATA_MAX_BYTES:
        raise WidgetError(f"a widget's data is at most {DATA_MAX_BYTES} bytes of JSON; put more into the HTML or make a custom page", 413)
    return text


def network_notes(html: str) -> list[str]:
    """What in the document wants the network, which a widget does not have: said in the tool's
    answer so the agent inlines it rather than learning from the operator that the widget is blank."""
    return [label for pattern, label in _NETWORK_HINTS if pattern.search(html)]


class WidgetStorage:
    """The values the operator's widgets keep, per session and widget."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _key(key: str) -> str:
        if not isinstance(key, str) or not key or len(key) > KEY_MAX or "\x00" in key:
            raise WidgetError(f"a widget's storage key is 1 to {KEY_MAX} characters")
        return key

    async def _session(self, session_id: str) -> None:
        if await self.db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) is None:
            raise WidgetError("no such session", 404)

    async def values(self, session_id: str, widget_id: str) -> dict[str, Any]:
        await self._session(session_id)
        rows = await self.db.fetchall(
            "SELECT key, value FROM widget_storage WHERE session_id = ? AND widget_id = ? ORDER BY key", (session_id, clean_id(widget_id)),
        )
        return {row["key"]: json.loads(row["value"]) for row in rows}

    async def set(self, session_id: str, widget_id: str, key: str, value: Any) -> dict[str, Any]:
        widget_id = clean_id(widget_id)
        key = self._key(key)
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise WidgetError(f"a stored value is plain JSON: {exc}") from exc
        size = len(text.encode("utf-8"))
        if size > VALUE_MAX_BYTES:
            raise WidgetError(f"one stored value is at most {VALUE_MAX_BYTES} bytes", 413)
        await self._session(session_id)
        at = _now()
        # The totals are read and the row written under one lock, so two quick writes from the frame
        # cannot both pass the cap that only one of them fits under.
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS n, coalesce(sum(length(CAST(value AS BLOB))), 0) AS bytes FROM widget_storage "
                "WHERE session_id = ? AND widget_id = ? AND key != ?",
                (session_id, widget_id, key),
            )
            row = await cursor.fetchone()
            await cursor.close()
            others, used = (int(row[0]), int(row[1])) if row is not None else (0, 0)
            if others + 1 > KEYS_MAX:
                raise WidgetError(f"a widget keeps at most {KEYS_MAX} keys", 413)
            if used + size > WIDGET_MAX_BYTES:
                raise WidgetError(f"a widget keeps at most {WIDGET_MAX_BYTES} bytes in all", 413)
            await conn.execute(
                "INSERT INTO widget_storage(session_id, widget_id, key, value, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, widget_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (session_id, widget_id, key, text, at),
            )
        return {"key": key, "updated_at": at}

    async def delete(self, session_id: str, widget_id: str, key: str) -> bool:
        widget_id = clean_id(widget_id)
        key = self._key(key)
        await self._session(session_id)
        found = await self.db.fetchone("SELECT 1 FROM widget_storage WHERE session_id = ? AND widget_id = ? AND key = ?", (session_id, widget_id, key))
        if found is None:
            return False
        await self.db.execute("DELETE FROM widget_storage WHERE session_id = ? AND widget_id = ? AND key = ?", (session_id, widget_id, key))
        return True


def page_slug(title: str, taken: set[str]) -> str:
    """A slug for the page a widget becomes: the title's letters (of any script) and digits, or
    ``widget``, numbered past the slugs already taken."""
    base = page_slug_from(title) or "widget"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _script_json(value: str) -> str:
    """JSON safe inside a ``<script>``: no ``</script>`` or comment opener can end it early."""
    return value.replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


PAGE_SHIM = """<link rel="stylesheet" href="/pages/_kit/kit.css">
<script src="/pages/_kit/daedalus.js"></script>
<script>
(function () {
  // The widget's calls, answered through the page bridge: the page is not sandboxed, so storage is
  // the page's own, and ask goes to the page's agent after the operator says yes, as it did in the chat.
  var D = window.Daedalus;
  function themeOf(t) { return { scheme: t && t.scheme || null, theme: t && t.theme || null, tokens: t && t.tokens || {} }; }
  window.widget = Object.freeze({
    title: __TITLE__,
    data: __DATA__,
    page: true,
    get lang() { return D.context.lang; },
    get theme() { return themeOf(D.theme.current); },
    onTheme: function (fn) { return D.theme.onChange(function (t) { fn(themeOf(t)); }); },
    storage: Object.freeze({
      get: function (key) { return D.storage.get(String(key)); },
      set: function (key, value) { return D.storage.set(String(key), value).then(function () { return true; }); },
      delete: function (key) { return D.storage.delete(String(key)); },
      keys: function () { return D.storage.list().then(function (rows) { return rows.map(function (r) { return r.key; }); }); }
    }),
    ask: function (text) {
      var words = String(text == null ? "" : text).trim();
      if (!words) return Promise.resolve(false);
      var question = D.context.lang === "ru" ? "Отправить агенту страницы?\\n\\n" : "Send this to the page's agent?\\n\\n";
      if (!window.confirm(question + words)) return Promise.resolve(false);
      return D.agent.ask(words).then(function () { return true; });
    }
  });
})();
</script>
"""

_HEAD_RE = re.compile(r"<head(?:\s[^>]*)?>", re.I)
_HTML_RE = re.compile(r"<html(?:\s[^>]*)?>", re.I)


def page_document(title: str, html: str, data_json: str) -> str:
    """The page a widget becomes: its own document, with the shim first in its head so ``widget`` is
    defined before any of the widget's scripts run."""
    shim = PAGE_SHIM.replace("__TITLE__", _script_json(json.dumps(title, ensure_ascii=False))).replace("__DATA__", _script_json(data_json))
    head = _HEAD_RE.search(html)
    if head:
        return html[: head.end()] + "\n" + shim + html[head.end() :]
    opening = _HTML_RE.search(html)
    if opening:
        return html[: opening.end()] + "\n<head>\n" + shim + "</head>\n" + html[opening.end() :]
    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_escape(title)}</title>\n{shim}</head>\n<body class=\"dk-page\">\n{html}\n</body>\n</html>\n"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "DATA_MAX_BYTES",
    "HTML_MAX_BYTES",
    "TOOL_NAME",
    "WidgetError",
    "WidgetStorage",
    "check_data",
    "check_html",
    "clean_id",
    "clean_title",
    "network_notes",
    "page_document",
    "page_slug",
]
