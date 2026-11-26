"""Every kind of file preview fits the panel it is drawn in, and ends where its content ends.

A spreadsheet in a 550 px panel once showed its second column cut at the panel's edge, and under its
rows the table's card went on as an empty grey block to the bottom of the panel; the path line over
it cut the file's own name ("langpt-") and kept every directory. This opens one file of each kind
in the panel's Preview tool at 400, 520 and 700 px and refuses:

- an element wider than the panel's content box that is not inside an area that scrolls sideways
  (a wide table or a long line of code scrolls inside its own box; nothing pushes the panel);
- a block with a surface of its own (a background or a border) that runs more than 40 px below the
  last thing in it: a table, a document or a list ends where its rows end;
- a cell of a table that is cut and offers no way to read it (its whole text as its tooltip);
- a path line that does not show the file's whole name, or that does not carry the full path as its
  tooltip.

The kinds: a workbook of three sheets (the one the problem was seen on), a workbook of one sheet, a
CSV of long cells, Markdown with a wide table and a long code line, source code, JSON, plain text,
a log too large to read whole, a patch, a wide picture, a PDF, a Word document, an HTML page, sound,
video, a file with no preview, and a file that failed to load.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py <port> <root with app -> miniapp/dist> &
    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_preview_fit.py

Exit 0 when every kind holds at every width.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FILE_TEXT, expect_app  # noqa: E402
from check_browser_panel_fit import set_panel_width  # noqa: E402
from check_preview import DOCX, MEDIA, tiny_pdf  # noqa: E402
from screenshots import S1, UNHANDLED, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WIDTHS = [400, 520, 700]
EMPTY_TAIL = 40


def workbook(sheets: dict[str, list[list[str]]]) -> str:
    """A workbook as SpreadsheetML: the sheet reader takes it, and it needs no binary fixture."""
    def cell(value: str) -> str:
        return f'<Cell><Data ss:Type="String">{value}</Data></Cell>'
    body = "".join(
        f'<Worksheet ss:Name="{name}"><Table>' + "".join("<Row>" + "".join(cell(v) for v in row) + "</Row>" for row in rows) + "</Table></Worksheet>"
        for name, rows in sheets.items()
    )
    return ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">' + body + "</Workbook>")


USERS_DIR = "exports/5d637b12-26ad-43e7-bf8b-31e1d9f9ac29"
BOOK = workbook({
    "Сводка": [
        ["Показатель", "Значение"],
        ["Всего пользователей", "1 284"],
        ["Приняли соглашение", "1 102"],
        ["Гости", "Гости не принимают соглашение на сервере: их согласие хранится только в браузере до первого входа"],
        ["Последняя выгрузка", "26.11.2026 09:14"],
    ],
    "Пользователи": [["id", "email", "имя", "создан", "последний вход", "тариф", "страна", "язык", "соглашение", "источник"]]
    + [[str(1000 + i), f"user{i}@example.com", f"Пользователь {i}", f"2026-03-0{1 + i % 9}", f"2026-11-2{i % 6} 08:1{i % 6}", "pro", "Нидерланды", "ru", "принято", "приглашение коллеги"] for i in range(12)],
    "Гости": [["id", "первый визит"], ["g1", "2026-11-20"], ["g2", "2026-11-21"]],
})
ONE_SHEET = workbook({"Цены": [["Товар", "Цена"], ["Хлеб ржаной", "3,10"], ["Мука, 5 кг", "14,90"]]})
CSV = "id,name,comment,url\n" + "".join(
    f'{i},"Row {i}","A comment long enough to be wider than the column it is drawn in, on purpose: {i}",https://example.com/a/very/long/path/that/does/not/break/{i}\n'
    for i in range(1, 9)
)
MARKDOWN = (
    "# Weekly report\n\nThe seasonal menu was checked on a phone and on a wide screen.\n\n"
    "| Page | Mobile | Desktop | Broken links | Largest image | Notes for the owner |\n|---|---|---|---|---|---|\n"
    "| menu.html | 98 | 100 | 0 | 46 KB | the seasonal section reads from data/menu.json now |\n"
    "| index.html | 97 | 100 | 0 | 120 KB | the hero image could be smaller still |\n\n"
    "```\nnpx lighthouse http://127.0.0.1:8100/menu.html --preset=mobile --quiet --output=json --output-path=reports/menu.json\n```\n\n"
    "See https://example.com/a/very/long/address/that/has/no/place/to/break/in/a/narrow/panel/at/all for the rest.\n"
)
CODE = "def price(cents: int) -> str:\n    return format_currency(cents / 100, currency='EUR', locale='en_IE', format_type='standard', currency_digits=True)\n\nprint(price(1490))\n"
JSON = '{"title":"Example","items":[{"name":"Bread","price":3,"note":"a long note that goes on well past the width of a narrow panel, on purpose"}],"ready":true}'
TEXT = "the run started\n" + "a line of a log that is much longer than the panel is wide and has to be read somehow, " * 3 + "\nthe run ended\n"
BIG = ("a line of a large log file\n" * 22000)
WIDE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="2400" height="600"><rect width="2400" height="600" fill="#2a5"/><circle cx="1200" cy="300" r="250" fill="#fff"/></svg>'
DIFF = FILE_TEXT["change.diff"].replace("+new = 2\n", "+new = format_currency(cents / 100, currency='EUR', locale='en_IE', format_type='standard')\n")

# kind -> (path, body, content type, what shows the file has been drawn)
FILES: dict[str, tuple[str, bytes | str | None, str, str]] = {
    "sheets": (f"{USERS_DIR}/langpt-users-all-2026-11-26.xlsx", BOOK, "application/vnd.ms-excel", ".preview-grid table"),
    "sheet": ("data/prices.xls", ONE_SHEET, "application/vnd.ms-excel", ".preview-grid table"),
    "csv": ("data/comments.csv", CSV, "text/csv", ".preview-grid table"),
    "markdown": ("reports/weekly.md", MARKDOWN, "text/markdown", ".preview-doc"),
    "code": ("src/price.py", CODE, "text/plain", ".source-view .line"),
    "json": ("data/items.json", JSON, "application/json", ".json-tree"),
    "text": ("logs/run.txt", TEXT, "text/plain", ".source-view .line"),
    "large": ("logs/big.log", BIG, "text/plain", ".source-view .line"),
    "diff": ("change.diff", DIFF, "text/plain", ".patch-view"),
    "image": ("shots/wide.svg", WIDE_SVG, "image/svg+xml", ".preview-image"),
    "pdf": ("sample.pdf", tiny_pdf(), "application/pdf", ".preview-frame"),
    "docx": ("sample.docx", DOCX, MEDIA["sample.docx"][1], ".preview-doc"),
    "html": ("site/index.html", FILE_TEXT["site/index.html"], "text/html", ".html-frame"),
    "audio": ("sample.wav", MEDIA["sample.wav"][0], "audio/wav", ".preview-media"),
    "video": ("sample.webm", MEDIA["sample.webm"][0], "video/webm", ".preview-media"),
    "unsupported": ("sample.bin", MEDIA["sample.bin"][0], "application/octet-stream", ".preview-none"),
    "failed": ("data/missing.csv", None, "text/csv", ".empty"),
}
BY_PATH = {path: (body, mime) for path, body, mime, _ in FILES.values()}


def route(r) -> None:  # type: ignore[no-untyped-def]
    url = urlsplit(r.request.url)
    if url.path.endswith("/download"):
        name = parse_qs(url.query).get("path", [""])[0]
        if name in BY_PATH:
            body, mime = BY_PATH[name]
            if body is None:
                return respond(r, {"detail": "the file could not be read"}, status=500)
            return respond(r, body, content_type=mime)
    return stub(r)


MEASURE = """(tail) => {
  const body = document.querySelector('.panel .panel-body');
  if (!body) return { problems: ['no panel body'] };
  const B = body.getBoundingClientRect();
  const viewer = body.querySelector('.panel-viewer');
  const problems = [];
  const name = (el) => (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : el.tagName.toLowerCase());
  const scope = [...document.querySelectorAll('.panel .panel-filetabs, .panel .panel-toolbar')];
  if (viewer) scope.push(viewer);
  for (const root of scope) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue;
      if (getComputedStyle(el).visibility === 'hidden') continue;
      let L = r.left, R = r.right;
      for (let a = el.parentElement; a && a !== body && a !== document.body; a = a.parentElement) {
        const s = getComputedStyle(a);
        if (s.overflowX !== 'visible') { const ar = a.getBoundingClientRect(); L = Math.max(L, ar.left); R = Math.min(R, ar.right); }
      }
      if (R - L <= 0) continue;
      if (L < B.left - 1 || R > B.right + 1) problems.push(`${name(el)} reaches ${Math.round(L - B.left)}..${Math.round(R - B.left)} of a ${Math.round(B.width)} px panel`);
    }
  }
  // A surface (a background or a border) that runs on under the last thing in it.
  const leaves = (el) => [...el.querySelectorAll('*')].filter((x) => {
    const r = x.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    if (['IMG', 'IFRAME', 'VIDEO', 'AUDIO', 'CANVAS', 'svg', 'INPUT', 'BUTTON', 'TD', 'TH'].includes(x.tagName)) return true;
    return [...x.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
  });
  if (viewer) {
    for (const el of [viewer, ...viewer.querySelectorAll('*')]) {
      const s = getComputedStyle(el);
      const surfaced = (s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== 'transparent') || parseFloat(s.borderBottomWidth) > 0;
      if (!surfaced || ['IFRAME', 'VIDEO', 'AUDIO', 'IMG', 'TD', 'TH', 'TR', 'INPUT', 'BUTTON'].includes(el.tagName)) continue;
      const r = el.getBoundingClientRect();
      if (r.height < tail) continue;
      const inner = leaves(el);
      if (!inner.length) continue;
      const end = Math.max(...inner.map((x) => x.getBoundingClientRect().bottom));
      const pad = parseFloat(s.paddingBottom) + parseFloat(s.borderBottomWidth);
      if (r.bottom - pad - end > tail) problems.push(`${name(el)} runs ${Math.round(r.bottom - pad - end)} px of empty surface under its content`);
    }
  }
  // A cut cell says what it holds.
  for (const cell of body.querySelectorAll('.preview-grid td, .preview-grid th')) {
    if (cell.scrollWidth > cell.clientWidth + 1 && !cell.title) { problems.push(`a cut cell has no tooltip: ${cell.textContent.slice(0, 30)}`); break; }
  }
  // The path line: the file's whole name, the full path as its tooltip.
  const crumbs = document.querySelector('.panel .panel-crumbs');
  const last = crumbs && crumbs.querySelector('.crumb.last');
  const out = { panel: Math.round(B.width), problems };
  if (crumbs && last) {
    // The name is whole unless it alone is wider than the line; then it gives way in its middle, its
    // end kept, and only once the folders and the root have given all the room they had.
    const clipRight = (el) => { let R = Infinity; for (let a = el; a && a !== crumbs.parentElement; a = a.parentElement) { if (getComputedStyle(a).overflowX !== 'visible') R = Math.min(R, a.getBoundingClientRect().right); } return R; };
    const texts = [];
    const walker = document.createTreeWalker(last, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.textContent.trim() && n.textContent.trim() !== '›') texts.push(n);
    const cutAt = (n) => { const range = document.createRange(); range.selectNodeContents(n); const r = range.getBoundingClientRect(); return r.right > clipRight(n.parentElement) + 1; };
    const lastText = texts[texts.length - 1];
    if (lastText) {
      const range = document.createRange();
      range.setStart(lastText, Math.max(0, lastText.textContent.length - 1));
      range.setEnd(lastText, lastText.textContent.length);
      if (range.getBoundingClientRect().right > clipRight(lastText.parentElement) + 1) problems.push(`the path line cuts the end of the file's name: ${last.textContent.trim()}`);
    }
    if (texts.some(cutAt)) {
      const room = [...crumbs.querySelectorAll('.crumb')].filter((el) => el !== last && !last.contains(el)).reduce((sum, el) => sum + el.getBoundingClientRect().width, 0);
      if (room > 1) problems.push(`the file's name is cut while the folders keep ${Math.round(room)} px: ${last.textContent.trim()}`);
    }
    out.crumbsTitle = crumbs.title;
  }
  return out;
}"""


def check_width(page, kind: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    path = FILES[kind.split("/")[0]][0]
    got = page.evaluate(MEASURE, EMPTY_TAIL)
    print(f"[{kind}] {width}: panel {got.get('panel')}, {len(got['problems'])} problems")
    for p in got["problems"][:6]:
        problems.append(f"[{kind}] {width}: {p}")
    if "crumbsTitle" in got and not (got["crumbsTitle"] or "").endswith(path):
        problems.append(f"[{kind}] {width}: the path line's tooltip is {got['crumbsTitle']!r}, not the file's path")


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
        page = context.new_page()
        page.route("**/api/**", route)
        for kind, (path, _, _, ready) in FILES.items():
            page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=ru&panel=preview&path={quote(path)}")
            page.wait_for_selector(f".panel .panel-viewer {ready}", state="attached", timeout=20000)
            page.wait_for_timeout(500)
            for width in WIDTHS:
                set_panel_width(page, width)
                page.wait_for_timeout(300)
                check_width(page, kind, width, problems)
            if kind == "sheets":
                # Each sheet of the workbook, at the narrowest panel.
                set_panel_width(page, WIDTHS[0])
                for i in (1, 2):
                    page.locator(".panel .preview-tabs [role='radio']").nth(i).click()
                    page.wait_for_timeout(300)
                    check_width(page, f"sheets/{i + 1}", WIDTHS[0], problems)
        browser.close()
    for problem in problems:
        print("PROBLEM", problem)
    unhandled = UNHANDLED.report()
    if problems or unhandled:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
