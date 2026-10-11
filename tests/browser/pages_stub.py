"""Custom pages as the host answers them: the API under ``/api/pages`` and the pages at ``/pages/``.

One page, ``orders``, published at version 2 with version 1 kept, built the way the shipped skill
tells an agent to build one: the kit's stylesheet and the bridge, a toolbar, a card, a list, and two
buttons that exercise the bridge — a toast through the app, and a value kept in the page's storage and
read back. It shows the theme it was handed, so a check can compare it with the app's.

The kit itself is fetched from the build under test (``<app>/kit/``), never from a copy here: the
point of the check is that the files the host serves are the ones the build made.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from typing import Any

SAMPLE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orders</title>
<link rel="stylesheet" href="/pages/_kit/kit.css">
<script src="/pages/_kit/daedalus.js"></script>
</head>
<body>
<div class="dk-toolbar"><b class="dk-h2">Orders</b><span class="dk-spacer"></span><span class="dk-badge ok" id="state">ready</span></div>
<main class="dk-page dk-stack">
  <div class="dk-card dk-stack">
    <div class="dk-row-flex">
      <button class="dk-btn primary" id="toast">Say hello</button>
      <button class="dk-btn" id="save">Save a note</button>
      <input class="dk-input" id="note" value="Rye, two loaves" style="max-width: 240px">
    </div>
    <div class="dk-meta">Kept: <span id="kept">nothing yet</span></div>
    <div class="dk-meta">Theme: <span id="theme">none</span></div>
  </div>
  <div class="dk-list">
    <div class="dk-row"><div class="dk-row-main"><div class="dk-row-title">Sourdough</div><div class="dk-row-meta">Ready at 07:00</div></div><span class="dk-badge">12</span></div>
    <div class="dk-row"><div class="dk-row-main"><div class="dk-row-title">Croissants</div><div class="dk-row-meta">Ready at 06:30</div></div><span class="dk-badge warn">3</span></div>
  </div>
</main>
<script>
  const show = () => { document.getElementById("theme").textContent = (Daedalus.context.theme || "none") + "/" + (Daedalus.context.scheme || "none"); };
  Daedalus.ready().then(show);
  Daedalus.theme.onChange(show);
  document.getElementById("toast").onclick = () => Daedalus.ui.toast("Hello from the orders page");
  document.getElementById("save").onclick = async () => {
    await Daedalus.storage.set("note", { text: document.getElementById("note").value });
    const back = await Daedalus.storage.get("note");
    document.getElementById("kept").textContent = back ? back.text : "lost";
  };
</script>
</body>
</html>
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class PagesStub:
    """``answer(method, path, body)`` for ``/api/pages*``; ``serve(route, app_base)`` for ``/pages/``."""

    def __init__(self) -> None:
        at = _now()
        versions = [
            {"n": 2, "at": at, "note": "Prices for October", "kind": "static", "entry": "index.html", "target": None, "files": 1, "bytes": len(SAMPLE_PAGE)},
            {"n": 1, "at": at, "note": "", "kind": "static", "entry": "index.html", "target": None, "files": 1, "bytes": len(SAMPLE_PAGE)},
        ]
        self.pages: dict[str, dict[str, Any]] = {
            "orders": {
                "slug": "orders", "title": {"en": "Orders", "ru": "Заказы"}, "titles": {"en": "Orders", "ru": "Заказы"}, "icon": "board", "icon_path": None,
                "path": "/app/p/orders", "version": 2, "updated_at": at, "kind": "static", "entry": "index.html", "target": None, "session_id": None,
                "created_at": at, "url": "/pages/orders/", "draft_url": "/pages/orders/?draft=1", "versions": versions,
                "draft": {"kind": "static", "entry": "index.html", "target": None, "updated_at": at},
            },
        }
        self.storage: dict[tuple[str, str], Any] = {}
        self.requests: list[str] = []
        self.signins = 0

    def listing(self) -> dict[str, Any]:
        rows = []
        for page in self.pages.values():
            title = page["title"]
            rows.append({"slug": page["slug"], "title": title["en"] if isinstance(title, dict) else title, "titles": title if isinstance(title, dict) else {},
                         "icon": page["icon"], "icon_path": page["icon_path"], "path": page["path"], "version": page["version"], "updated_at": page["updated_at"]})
        return {"pages": rows}

    def answer(self, method: str, path: str, body: Any = None) -> tuple[int, Any] | None:
        path = path.split("?", 1)[0]
        path = path[path.index("/api/"):] if "/api/" in path else path
        if not path.startswith("/api/pages"):
            return None
        self.requests.append(f"{method} {path}")
        parts = path.split("/")[3:]
        if not parts or parts == [""]:
            return 200, self.listing()
        if parts == ["_signin"] and method == "POST":
            self.signins += 1
            return 200, {"ok": True}
        slug, rest = parts[0], parts[1:]
        page = self.pages.get(slug)
        if page is None:
            return 404, {"detail": f"no page {slug!r}"}
        if not rest and method == "GET":
            return 200, page
        if rest == ["rollback"] and method == "POST":
            page["version"] = int((body or {}).get("version") or page["version"])
            return 200, page
        if rest and rest[0] == "storage":
            key = "/".join(rest[1:])
            if not key:
                return 200, {"keys": [{"key": k, "bytes": len(json.dumps(v)), "updated_at": _now()} for (s, k), v in sorted(self.storage.items()) if s == slug]}
            if method == "GET":
                if (slug, key) not in self.storage:
                    return 404, {"detail": f"nothing is stored under {key!r}"}
                return 200, {"key": key, "value": self.storage[(slug, key)], "updated_at": _now()}
            if method == "PUT":
                self.storage[(slug, key)] = (body or {}).get("value")
                return 200, {"key": key, "updated_at": _now()}
            if method == "DELETE":
                return (200, {"deleted": key}) if self.storage.pop((slug, key), None) is not None else (404, {"detail": "nothing"})
        return None

    def fulfil(self, route) -> bool:  # type: ignore[no-untyped-def]
        """:meth:`answer` for a Playwright route: ``True`` when it answered."""
        request = route.request
        try:
            body = request.post_data_json if request.method in ("POST", "PUT") and request.post_data else None
        except ValueError:
            body = None
        found = self.answer(request.method, request.url, body)
        if found is None:
            return False
        status, data = found
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data))
        return True

    def serve(self, route, app_base: str) -> None:  # type: ignore[no-untyped-def]
        """A page's own address: the kit from the build under test, a page's files, or the host's 404."""
        url = route.request.url.split("?", 1)[0]
        tail = url[url.index("/pages/") + len("/pages/"):]
        if tail.startswith("_kit/"):
            name = tail.removeprefix("_kit/")
            with urllib.request.urlopen(f"{app_base}/kit/{name}", timeout=5) as answer:  # noqa: S310 — the harness's own loopback server
                data = answer.read()
            route.fulfill(status=200, content_type="text/css" if name.endswith(".css") else "text/javascript", body=data)
            return
        slug = tail.split("/", 1)[0]
        if slug in self.pages:
            route.fulfill(status=200, content_type="text/html", body=SAMPLE_PAGE, headers={"Content-Security-Policy": "frame-ancestors 'self'"})
            return
        route.fulfill(status=404, content_type="text/html", body="<p>There is no such page.</p>")


__all__ = ["SAMPLE_PAGE", "PagesStub"]
