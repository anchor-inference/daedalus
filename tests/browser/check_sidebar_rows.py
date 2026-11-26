"""The sidebar's chat rows give the title the line: one glyph at the left, at most one thing at the right.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_sidebar_rows.py

An installation whose chats nearly all run on the host drew an amber "Host" pill on nearly every row,
and beside it a kind tile, an imported program's letter, a fork glyph, a subagent count and the
time: a 276 px column left a title 60 to 90 px, two words of it. The row now has three parts — a
16 px slot at the left that carries the chat's most telling property as a quiet glyph (and its
unread or running dot), the title, and one trailing item (its state while it needs the reader or
works, else its subagents' count while they work, else its time) — and the environment is said
once, on the folder's line, where it is the folder's rule, and on a row only where the row breaks it.

The fixture is an installation shaped like that one: 26 chats in four projects and the Chats section,
four of five on the host, forks, an imported Claude chat, a leader with four subagents of which two
work, a private chat, a loop, a computer, a browser and a remote chat, long Russian titles, one chat
waiting for the reader and one working. What is checked, at 300 px and at the 232 px minimum:

- every nested row's title box is at least 170 px wide at 300 and 120 px at 232;
- no row draws more than one item after its title (its actions wait for the pointer);
- nothing in a row is drawn on an orange ground or in orange ink unless the row needs the reader;
- every property the row does not draw is in its accessible name and its tooltip;
- the slot is as wide on every row and every title starts at the same x;
- the unread dot and the running state are visible;
- the rows stay 32 px with a 2 px gap, the windowed list's estimate.

``SHOTS=<dir>`` saves the pictures: the column at 300 and 232 px in dark Russian, 300 px in light,
the 276 px column of the installation the fixture copies, a hovered row, and the phone's drawer at 390 px.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app, folders, open_drawer  # noqa: E402
from screenshots import LOOP, UNHANDLED, ago, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

PA, PB, PC, PD = "ab0000000001", "ab0000000002", "ab0000000003", "ab0000000004"
LEADER = "aa0000000005"
WORKING = "aa0000000001"
WAITING = "bb0000000002"
UNREAD = "aa0000000004"

PROJECTS = [
    {"id": PA, "name": "Расширение браузера", "env": "host"},
    {"id": PB, "name": "Daedalus", "env": "host"},
    # Container by its folder, with one chat moved to the host: the row that breaks its folder's rule.
    {"id": PC, "name": "Сайт пекарни", "env": "container"},
    {"id": PD, "name": "Курьерские тарифы", "env": "host"},
]


def chat(id_: str, title: str, project: str, *, env: str = "host", status: str = "idle", minutes: float = 30, **extra: object) -> dict:
    """One row of the listing, in a project or, with `project` empty, in a scratch project of its own."""
    own = project or f"cc{id_[2:]}"
    name = next((p["name"] for p in PROJECTS if p["id"] == project), title)
    meta = dict(extra.pop("meta", {}) or {})
    return {"id": id_, "title": title, "status": status, "created_at": ago(days=6), "last_message_at": ago(minutes=minutes),
            "run_id": "run1" if status == "running" else None, "model": "Claude Opus 5", "workspace": "work", "workspace_path": "/home/you/work",
            "workspace_own": False, "env": env, "metadata": meta, "project_id": own, "project": name, "kind": "chat", "controlling": [], **extra}


def subagent(id_: str, name: str, status: str, minutes: float) -> dict:
    return chat(id_, f"[sub] {name}", PA, status=status, minutes=minutes, meta={"subagent_of": LEADER, "subagent_name": name})


SESSIONS = [
    chat(WORKING, "Расширение для Chrome: перевод выделенного текста на лету", PA, status="running", minutes=0.3),
    chat("aa0000000002", "Расширение для Chrome: перевод выделенного текста, вторая попытка", PA, minutes=50,
         meta={"forked_from": {"session_id": WORKING, "seq": 214}}),
    chat("aa0000000003", "Проверка расширения в живом браузере на десяти сайтах", PA, minutes=70, kind="chrome"),
    chat(UNREAD, "Обработка docx-вложений с сохранением форматирования таблиц", PA, minutes=9, unread_result=True),
    chat(LEADER, "Актуализация и объединение документации по всему расширению", PA, minutes=4, terminals=2),
    subagent("aa0000000051", "сверка-ссылок", "running", 0.2),
    subagent("aa0000000052", "скриншоты", "running", 0.5),
    subagent("aa0000000053", "словарь-терминов", "idle", 16),
    subagent("aa0000000054", "переводы", "idle", 21),
    chat("aa0000000006", "Сборка релизной версии и проверка подписи пакета", PA, minutes=60 * 5),
    chat("aa0000000007", "Main worktree: сравнение веток и перенос исправлений", PA, minutes=60 * 26),
    chat("bb0000000001", "Сколько запросов уходит на одну сессию и почему их так много", PB, minutes=35, imported_from="claude"),
    chat(WAITING, "У меня есть такая идея: боковая панель с заметками к каждому чату", PB, status="waiting", minutes=2),
    chat("bb0000000003", "Личные заметки по планированию работы на следующую неделю", PB, minutes=60 * 3, meta={"private": True}),
    chat("bb0000000004", "Ночная проверка тестов и отчёт о падениях за прошедшие сутки", PB, minutes=55, meta={"loop": LOOP}),
    chat("bb0000000005", "Перенос миграций базы данных на новую схему хранения состояния", PB, minutes=60 * 30),
    chat("bb0000000006", "Перенос миграций базы данных: вариант с одной транзакцией", PB, minutes=60 * 31,
         meta={"forked_from": {"session_id": "bb0000000005", "seq": 88}}),
    chat("cc0000000001", "Меню по таблице владельца: сезонные позиции наверх", PC, env="container", minutes=60 * 4),
    chat("cc0000000002", "Фотографии товаров: обрезка и подписи для всех карточек", PC, env="container", minutes=60 * 7),
    chat("cc0000000003", "Выгрузка заказов в бухгалтерию через скрипт на сервере", PC, env="host", minutes=60 * 9),
    chat("cc0000000004", "Проверка вёрстки страницы оплаты на телефоне и планшете", PC, env="container", minutes=60 * 50),
    chat("dd0000000001", "Расчёт тарифов по зонам доставки для трёх курьерских служб", PD, minutes=60 * 8),
    chat("dd0000000002", "Сравнение личных кабинетов курьерских служб на экране", PD, minutes=60 * 20, kind="computer"),
    chat("dd0000000003", "Шаблон письма курьерам о смене расписания на праздники", PD, minutes=60 * 70),
    chat("ee0000000001", "Почему контейнер теряет сеть после перезапуска хоста", "", minutes=15),
    chat("ee0000000002", "Разбор логов сборки: какие шаги дольше всего и почему", "", minutes=60 * 2, unread_result=True),
    chat("ee0000000003", "Настройка резервного копирования базы на внешний диск", "", env="container", minutes=60 * 6),
    chat("ee0000000004", "Удалённая отладка на ноутбуке через терминал и командную строку", "", minutes=60 * 12, kind="remote",
         remote={"device_id": "dev1", "device_name": "ноутбук", "cwd": "/home/you/src", "connected": True, "guard": "ask", "waiting": False}),
    chat("ee0000000005", "Черновик ответа на вопросы о тарифах и сроках поставки", "", minutes=60 * 40),
]


def listing() -> dict:
    projects = []
    for p in PROJECTS:
        rows = [s for s in SESSIONS if s["project_id"] == p["id"] and not s["metadata"].get("subagent_of")]
        projects.append({"id": p["id"], "name": p["name"], "folders": [dict(f, env=p["env"]) for f in folders(f"/home/you/work/{p['id']}")],
                         "created_at": ago(days=20), "settings": {"snapshots": True}, "total": len(rows), "active": 0, "loops": 0,
                         "last_message_at": min(s["last_message_at"] for s in rows), "pinned_at": ""})
    for s in SESSIONS:
        if s["project_id"].startswith("cc0") and not any(p["id"] == s["project_id"] for p in projects):
            projects.append({"id": s["project_id"], "name": s["title"], "folders": [dict(f, env=s["env"]) for f in folders(f"/home/you/chats/{s['id']}")],
                             "created_at": ago(days=6), "settings": {"snapshots": False, "ephemeral": True}, "total": 1, "active": 0, "loops": 0,
                             "last_message_at": s["last_message_at"], "pinned_at": ""})
    return {"sessions": SESSIONS, "projects": projects}


def route(r) -> None:  # type: ignore[no-untyped-def]
    path = r.request.url.split("?", 1)[0]
    if r.request.method == "GET" and path.endswith("/api/sessions"):
        return r.fulfill(status=200, content_type="application/json", body=json.dumps(listing()))
    return stub(r)


def seed(width: int) -> str:
    """Every project open, the leader's subagents unfolded, the column at `width`."""
    keys = [p["id"] for p in PROJECTS]
    return ("try { " + " ".join(f"localStorage.setItem('daedalus.folder.{k}', '1');" for k in keys)
            + f" localStorage.setItem('daedalus.side.subagents', JSON.stringify(['{LEADER}']));"
            + f" localStorage.setItem('daedalus.width.sidebar', '{width}'); }} catch (e) {{}}")


MEASURE = """() => {
  const orange = (text) => {
    const m = text.match(/rgba?\\(([^)]+)\\)/) || text.match(/color\\(srgb ([^)]+)\\)/);
    if (!m) return false;
    const parts = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    let [r, g, b] = parts;
    const a = parts.length > 3 ? parts[3] : 1;
    if (text.startsWith('color(')) { r *= 255; g *= 255; b *= 255; }
    if (a < 0.04) return false;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    if (max - min < 60) return false;
    let h = 0;
    if (max === r) h = ((g - b) / (max - min)) * 60; else if (max === g) h = (2 + (b - r) / (max - min)) * 60; else h = (4 + (r - g) / (max - min)) * 60;
    if (h < 0) h += 360;
    return h >= 15 && h <= 50;
  };
  const shown = (el) => { const b = el.getBoundingClientRect(); const cs = getComputedStyle(el); return b.width > 0 && b.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0; };
  return [...document.querySelectorAll('.sidebar [data-session], .sidebar [data-subagent]')].filter(shown).map((row) => {
    const title = row.querySelector('.sb-t');
    const tb = title.getBoundingClientRect();
    const rb = row.getBoundingClientRect();
    const trailing = [];
    for (const child of row.children) {
      if (child.classList.contains('sb-acts') || !shown(child)) continue;
      if (child.getBoundingClientRect().left < tb.right - 1) continue;
      const inner = child.classList.contains('sb-marks') ? [...child.children].filter(shown) : [child];
      for (const el of inner) trailing.push(el.className || el.tagName);
    }
    const inked = [...row.querySelectorAll('*')].filter((el) => shown(el) && !el.closest('.sb-acts')).filter((el) => {
      const cs = getComputedStyle(el);
      return orange(cs.backgroundColor) || (el.childElementCount === 0 && el.textContent.trim() && orange(cs.color)) || (cs.borderStyle !== 'none' && parseFloat(cs.borderWidth) > 0 && orange(cs.borderColor));
    }).map((el) => el.className || el.tagName);
    const gut = row.querySelector('.sb-gut');
    const gb = gut ? gut.getBoundingClientRect() : null;
    const dot = row.querySelector('.unread-dot');
    return {
      id: row.dataset.session || row.dataset.subagent, sub: !!row.dataset.subagent, nested: !!row.closest('.sb-kids'),
      text: title.textContent, titleLeft: Math.round(tb.left), titleWidth: Math.round(tb.width), titleCut: title.scrollWidth > title.clientWidth,
      height: Math.round(rb.height), top: rb.top, trailing, inked,
      gutter: gb ? { left: Math.round(gb.left), width: Math.round(gb.width) } : null,
      label: row.getAttribute('aria-label') || '', tip: row.getAttribute('title') || '',
      unreadShown: !!dot && shown(dot), running: !!row.querySelector('.sb-word.running, .sb-pip.running, .sb-subs.live'),
      waiting: !!row.querySelector('.sb-word.waiting, .sb-word.failed'),
    };
  });
}"""

# What each fixture chat is, in the words its tooltip and its accessible name must carry (Russian).
PROPERTIES: dict[str, list[str]] = {}
for s in SESSIONS:
    words: list[str] = []
    if s["env"] == "host":
        words.append("хост")
    else:
        words.append("контейнер")
    kind = s.get("kind", "chat")
    if kind == "chrome":
        words.append("браузер")
    if kind == "computer":
        words.append("компьютер")
    if kind == "remote":
        words.append("ноутбук")
    if s.get("imported_from"):
        words.append("claude")
    if s["metadata"].get("forked_from"):
        words.append("ответвление")
    if s["metadata"].get("private"):
        words.append("приватный")
    if s["metadata"].get("loop"):
        words.append("цикл")
    if s.get("unread_result"):
        words.append("непросмотренный")
    if s.get("terminals"):
        words.append("терминал")
    if s["id"] == LEADER:
        words.append("4 субагента")
    if s["status"] == "running":
        words.append("работает")
    if s["status"] == "waiting":
        words.append("ждёт вас")
    if not s["metadata"].get("subagent_of"):
        PROPERTIES[s["id"]] = words


def judge(rows: list[dict], width: int, problems: list[str]) -> None:
    where = f"{width}px"
    least = 170 if width >= 300 else 120
    if len(rows) < 24:
        problems.append(f"{where}: only {len(rows)} chat rows are drawn")
    for row in rows:
        name = f"{where} {row['text'][:28]!r}"
        if row["nested"] and not row["sub"] and row["titleWidth"] < least:
            problems.append(f"{name}: the title gets {row['titleWidth']} px, under {least}")
        if len(row["trailing"]) > 1:
            problems.append(f"{name}: {len(row['trailing'])} items after the title: {row['trailing']}")
        if row["inked"] and not row["waiting"]:
            problems.append(f"{name}: orange on a row that does not need the reader: {row['inked']}")
        if row["height"] != 32:
            problems.append(f"{name}: the row is {row['height']} px high, not 32")
        if row["gutter"] is None:
            problems.append(f"{name}: no slot at the left")
        for word in PROPERTIES.get(row["id"], []):
            if word not in row["label"].lower():
                problems.append(f"{name}: its accessible name does not say {word!r}: {row['label']!r}")
            if word not in row["tip"].lower():
                problems.append(f"{name}: its tooltip does not say {word!r}: {row['tip']!r}")
        if row["titleCut"] and row["text"] not in row["tip"]:
            problems.append(f"{name}: a cut title is not whole in the tooltip")
    gutters = {(r["gutter"]["left"], r["gutter"]["width"]) for r in rows if r["gutter"]}
    if len(gutters) > 1:
        problems.append(f"{where}: the slot differs between rows: {sorted(gutters)}")
    lefts = {r["titleLeft"] for r in rows}
    if max(lefts) - min(lefts) > 1:
        problems.append(f"{where}: titles start at different x: {sorted(lefts)}")
    by_id = {r["id"]: r for r in rows}
    if not by_id.get(UNREAD, {}).get("unreadShown"):
        problems.append(f"{where}: the unread chat shows no dot")
    for running in (WORKING, LEADER):
        if not by_id.get(running, {}).get("running"):
            problems.append(f"{where}: {running} works (itself or its subagents) and its row does not say so")
    if not by_id.get(WAITING, {}).get("waiting"):
        problems.append(f"{where}: the waiting chat does not say it needs the reader")
    ordered = sorted((r for r in rows if r["nested"]), key=lambda r: r["top"])
    pitches = {round(b["top"] - a["top"]) for a, b in zip(ordered, ordered[1:], strict=False) if 0 < b["top"] - a["top"] < 40}
    if pitches - {34}:
        problems.append(f"{where}: nested rows are not 34 px apart: {sorted(pitches)}")


def report(rows: list[dict], width: int) -> None:
    nested = [r["titleWidth"] for r in rows if r["nested"] and not r["sub"]]
    loose = [r["titleWidth"] for r in rows if not r["nested"] and not r["sub"]]
    print(f"{width}px: nested title width min {min(nested, default=0)} median {sorted(nested)[len(nested) // 2] if nested else 0} max {max(nested, default=0)}; "
          f"top-level min {min(loose, default=0)} max {max(loose, default=0)}; most trailing items {max((len(r['trailing']) for r in rows), default=0)}")


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"{SHOTS}/{name}.png")


def column(browser, width: int, *, scheme: str = "dark", lang: str = "ru", viewport: tuple[int, int] = (1920, 1080)):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    context.add_init_script(seed(width))
    page = context.new_page()
    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents?token=t&lang={lang}&scheme={scheme}", wait_until="networkidle")
    page.wait_for_selector(f".sidebar [data-session='{LEADER}']", timeout=20000)
    page.wait_for_timeout(400)
    return context, page


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for width in (300, 232, 276):
            context, page = column(browser, width)
            drawn = page.evaluate("() => document.querySelector('.sidebar').getBoundingClientRect().width")
            if abs(drawn - width) > 1:
                problems.append(f"the column was asked for {width} px and drew {drawn:.0f}")
            rows = page.evaluate(MEASURE)
            report(rows, width)
            if width != 276:
                judge(rows, width, problems)
            shot(page, f"sidebar-rows-{width}-dark-ru")
            if width == 300:
                # The scroll keeps the Chats section and the folder with the subagents in one picture.
                page.locator(f".sidebar [data-session='{UNREAD}']").hover()
                page.wait_for_timeout(200)
                hovered = page.evaluate(MEASURE)
                row = next(r for r in hovered if r["id"] == UNREAD)
                if row["trailing"]:
                    problems.append(f"a hovered row keeps {row['trailing']} beside its actions")
                if not page.locator(f".sidebar [data-session='{UNREAD}'] .sb-acts").is_visible():
                    problems.append("a hovered row shows no actions")
                shot(page, "sidebar-rows-300-hover-dark-ru")
                page.locator(".sidebar-body").evaluate("el => el.scrollTo(0, el.scrollHeight)")
                page.mouse.move(1200, 500)
                page.wait_for_timeout(300)
                shot(page, "sidebar-rows-300-dark-ru-end")
            context.close()
        context, page = column(browser, 300, scheme="light")
        shot(page, "sidebar-rows-300-light-ru")
        context.close()
        context, page = column(browser, 300, lang="en")
        shot(page, "sidebar-rows-300-dark-en")
        context.close()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.add_init_script(seed(300))
        page = phone.new_page()
        page.route("**/api/**", route)
        page.goto(f"{BASE}/agents?token=t&lang=ru&scheme=dark", wait_until="networkidle")
        open_drawer(page)
        drawer = page.evaluate("""() => [...document.querySelectorAll('.ph-drawer-root.open [data-session]')].slice(0, 12).map((row) => {
          const t = row.querySelector('.sb-t').getBoundingClientRect(); const g = row.querySelector('.sb-gut');
          return { w: Math.round(t.width), left: Math.round(t.left), h: Math.round(row.getBoundingClientRect().height), gut: g ? Math.round(g.getBoundingClientRect().width) : 0 };
        })""")
        print("drawer 390:", drawer)
        if any(r["h"] < 44 for r in drawer):
            problems.append(f"the drawer's rows are under a finger's 44 px: {drawer}")
        if len({r["left"] for r in drawer}) > 1:
            problems.append(f"the drawer's titles start at different x: {drawer}")
        if page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"):
            problems.append("the phone scrolls sideways with the drawer open")
        shot(page, "sidebar-rows-drawer-390-dark-ru")
        phone.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    raise SystemExit(run())
