"""The evaluation's web: two local origins that serve the fixture pages and remember what was done on them.

Every page is a file under ``fixtures/``; the few that need a server's help (a search, a paginated
list, a slow answer, a download) are answered here. What a task is judged by is recorded server
side — a form's POST, or a page's own ``record()`` call when the agent clicks something that is not
a form — so a checker never has to trust what the agent says it did.

The second origin is ``localhost`` on another port while the first is ``127.0.0.1``: the two are
different *sites*, not only different origins, so Chromium puts the frame in its own renderer
process. A frame on the same host and another port would stay in the page's process and would not
test what a real third-party widget does to the reader.
"""

from __future__ import annotations

import html
import http.server
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CATALOGUE = [
    ("Beech cutting board", "12.50"), ("Linen apron", "18.00"), ("Cast iron trivet", "9.90"), ("Copper measuring cups", "24.00"),
    ("Bamboo steamer", "15.75"), ("Olive wood spoon set", "21.30"), ("Enamel mug", "8.40"), ("Stoneware pitcher", "33.00"),
    ("Glass storage jar", "6.20"), ("Wool pot holder", "7.80"), ("Ceramic butter dish", "14.60"), ("Brass bottle opener", "11.10"),
    ("Oak serving tray", "38.90"), ("Walnut spice rack", "42.00"), ("Cotton tea towel", "5.50"), ("Porcelain teapot", "29.95"),
    ("Marble mortar", "26.40"), ("Steel colander", "17.25"), ("Silicone baking mat", "9.10"), ("Cedar recipe box", "22.80"),
    ("Pine knife block", "31.60"), ("Clay water filter", "44.50"), ("Rattan bread basket", "13.30"), ("Copper kettle", "58.00"),
    ("Birch salad servers", "10.90"), ("Iron skillet", "36.70"), ("Terracotta baker", "27.20"), ("Hemp produce bag", "4.60"),
    ("Slate cheese board", "19.40"), ("Maple rolling pin", "16.80"), ("Tin cookie cutters", "7.20"), ("Jute placemat", "5.90"),
    ("Pewter salt cellar", "23.50"), ("Glass carafe", "18.70"), ("Acacia pepper mill", "25.30"), ("Bone china saucer", "12.20"),
    ("Cork trivet", "6.80"), ("Zinc herb planter", "28.40"), ("Linen napkin set", "20.10"), ("Stoneware ramekins", "15.20"),
    ("Enamel colander", "21.90"), ("Wicker picnic hamper", "64.00"), ("Walnut desk organizer", "47.35"), ("Ceramic oil cruet", "13.90"),
    ("Beech spurtle", "8.10"), ("Copper jam pan", "72.50"), ("Oak bread bin", "49.80"), ("Glass cake stand", "34.60"),
    ("Cotton oven mitt", "9.60"), ("Steel mixing bowl", "14.20"), ("Porcelain egg cup", "4.90"), ("Iron wok", "39.40"),
    ("Clay tagine", "46.20"), ("Bamboo utensil holder", "11.70"), ("Marble pastry board", "41.10"), ("Brass ladle", "19.90"),
]
"""The paginated shop's list: ten to a page, so the organiser the task asks about is on page five."""

NOTES = [(f"Field note {n}", f"author-{n}") for n in range(1, 31)]
NOTES[26] = ("Kestrel migration notes", "Ines Varga")
"""The "load more" list: six items a load, and the one the task asks about is the twenty-seventh."""

SEARCH = {
    "thermal paste": [("Arctic Glide thermal paste 4 g", "TP-4410-AG"), ("Kryo compound 1 g", "TP-1022-KC"), ("Cooler pad set", "CP-0931-XS")],
    "usb hub": [("Seven-port powered hub", "UH-7700-PW"), ("Travel hub", "UH-2210-TR")],
}

PRODUCTS = [
    {"id": 311, "name": "Enamel pot", "price": 42.0, "warehouse_code": "WH-2208", "stock": 14},
    {"id": 312, "name": "Copper kettle", "price": 79.5, "warehouse_code": "WH-7731", "stock": 3},
    {"id": 313, "name": "Oak chopping board", "price": 24.9, "warehouse_code": "WH-1049", "stock": 40},
    {"id": 314, "name": "Cast-iron pan", "price": 55.0, "warehouse_code": "WH-7713", "stock": 9},
]
"""What the kitchen page's API answers: the warehouse codes are in no page, only in this data."""
CSV = "date,description,amount\n2026-08-01,Opening balance,1200.00\n2026-08-04,Groceries,-84.20\n2026-08-11,Salary,2450.00\n2026-08-19,Rent,-950.00\n2026-08-31,Closing balance,2615.80\n"


@dataclass
class State:
    """What the agent did, as the servers saw it: every POST and every page's own record of a click."""

    submissions: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def submit(self, task: str, fields: dict[str, str]) -> None:
        with self.lock:
            self.submissions.setdefault(task, []).append(fields)

    def record(self, task: str, name: str, data: Any) -> None:
        with self.lock:
            self.events.setdefault(task, []).append({"name": name, "data": data, "at": time.time()})

    def forget(self, task: str) -> None:
        """A task starts from nothing, so a second run of it is not judged by the first one's clicks."""
        with self.lock:
            self.submissions.pop(task, None)
            self.events.pop(task, None)

    def of(self, task: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        with self.lock:
            return list(self.submissions.get(task, [])), list(self.events.get(task, []))


class Sites:
    """Both origins, started on free ports. ``main`` is the one the tasks open; ``other`` holds the frames."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.servers: list[http.server.ThreadingHTTPServer] = []
        self.main = ""
        self.other = ""

    def start(self, main_port: int = 0, other_port: int = 0) -> None:
        first = self._serve(main_port)
        second = self._serve(other_port)
        self.main = f"http://127.0.0.1:{first}"
        self.other = f"http://localhost:{second}"

    @property
    def ports(self) -> list[int]:
        return [s.server_address[1] for s in self.servers]

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def _serve(self, port: int) -> int:
        sites = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 — the standard library's name
                sites.get(self)

            def do_POST(self) -> None:  # noqa: N802
                sites.post(self)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Both listen on 127.0.0.1; the second is addressed as ``localhost``, which resolves there.
        return server.server_address[1]

    # -- answering ---------------------------------------------------------------------------------

    def send(self, handler: http.server.BaseHTTPRequestHandler, body: str | bytes, *, kind: str = "text/html; charset=utf-8", status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        handler.send_response(status)
        handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    def page(self, name: str) -> str | None:
        path = FIXTURES / f"{name}.html"
        if not path.is_file():
            return None
        return path.read_text().replace("{OTHER}", self.other).replace("{MAIN}", self.main)

    def get(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path.strip("/") or "index"
        if path == "eval.js":
            self.send(handler, (FIXTURES / "eval.js").read_text(), kind="text/javascript")
        elif path == "shop":
            self.send(handler, self.shop(int(query.get("page") or 1)))
        elif path == "notes":
            start = int(query.get("from") or 0)
            items = [{"title": t, "author": a} for t, a in NOTES[start : start + 6]]
            self.send(handler, json.dumps({"items": items, "more": start + 6 < len(NOTES)}), kind="application/json")
        elif path == "search":
            self.send(handler, self.search(query.get("q") or ""))
        elif path == "product":
            self.send(handler, self.product(query.get("sku") or ""))
        elif path == "order-status":
            # The skeleton's data: slow on purpose, so a read made as soon as the page loads sees nothing.
            time.sleep(3.5)
            self.send(handler, json.dumps({"status": "Out for delivery", "eta": "Thursday 14:00–16:00", "carrier": "Parcelwing"}), kind="application/json")
        elif path == "svc/catalogue/v3/listing-7f3a":
            # The page shows names and prices; the warehouse codes are only in the data behind it.
            self.send(handler, json.dumps({"category": query.get("cat") or "", "products": PRODUCTS}), kind="application/json")
        elif path == "statement.csv":
            self.send(handler, CSV, kind="text/csv", headers={"Content-Disposition": 'attachment; filename="statement-2026-08.csv"'})
        else:
            body = self.page(path)
            if body is None:
                self.send(handler, "<title>Not found</title><h1>Not found</h1>", status=404)
            else:
                self.send(handler, body)

    def post(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length).decode("utf-8", "replace") if length else ""
        path = parts.path.strip("/")
        if path == "event":
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                body = {}
            self.state.record(str(body.get("task") or ""), str(body.get("name") or ""), body.get("data"))
            self.send(handler, "{}", kind="application/json", headers={"Access-Control-Allow-Origin": "*"})
            return
        if path.startswith("submit/"):
            task = path.removeprefix("submit/")
            fields = {k: ", ".join(v) for k, v in parse_qs(raw, keep_blank_values=True).items()}
            self.state.submit(task, fields)
            shown = "".join(f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k, v in fields.items())
            self.send(handler, f"<!doctype html><title>Received</title><h1>Thank you</h1><p>We received your submission.</p><ul>{shown}</ul>")
            return
        self.send(handler, "not found", status=404, kind="text/plain")

    def shop(self, page: int) -> str:
        per = 10
        pages = (len(CATALOGUE) + per - 1) // per
        page = max(1, min(page, pages))
        rows = "".join(f"<tr><td>{html.escape(n)}</td><td>€{p}</td></tr>" for n, p in CATALOGUE[(page - 1) * per : page * per])
        links = []
        if page > 1:
            links.append(f'<a href="/shop?page={page - 1}">Previous</a>')
        links.append(f"<span>Page {page} of {pages}</span>")
        if page < pages:
            links.append(f'<a href="/shop?page={page + 1}">Next</a>')
        return (
            "<!doctype html><title>Kitchen goods</title><h1>Kitchen goods</h1>"
            f"<table><thead><tr><th>Item</th><th>Price</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<nav aria-label=pagination>{' '.join(links)}</nav>"
        )

    def product(self, sku: str) -> str:
        for name, code in (item for items in SEARCH.values() for item in items):
            if code == sku:
                return f"<!doctype html><title>{html.escape(name)} — Partsbin</title><h1>{html.escape(name)}</h1><p>SKU {code}</p><p>In stock · ships tomorrow</p><button>Add to cart</button>"
        return "<!doctype html><title>Not found — Partsbin</title><h1>No such product</h1>"

    def search(self, q: str) -> str:
        # Every word of the query in the product's name, as a shop's plain search does: a model that
        # searches for the brand finds it as surely as one that searches for the category.
        words = q.lower().split()
        found = [(n, s) for items in SEARCH.values() for n, s in items if words and all(w in n.lower() for w in words)]
        items = "".join(f"<li><a href='/product?sku={s}'>{html.escape(n)}</a> <small>SKU {s}</small></li>" for n, s in found) or "<li>No results.</li>"
        return (
            "<!doctype html><title>Search — Partsbin</title><h1>Partsbin</h1>"
            "<form action=/search role=search><input name=q aria-label=Search value='" + html.escape(q, quote=True) + "'><button>Search</button></form>"
            f"<h2>Results for “{html.escape(q)}”</h2><ol>{items}</ol>"
        )
