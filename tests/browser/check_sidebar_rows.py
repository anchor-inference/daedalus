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

With every title at one x, a project's name read as one more chat and a chat outside the projects,
between two of them, as a member of the one above. A chat inside a project now stands one 16 px step
in, on a guide line that drops from under the folder with a tick to each row; a subagent stands one
step in from its leader on a line of its own, so a subagent of a chat inside a project is two steps
in, with the project's line running on past it to the next chat.

The fixture is an installation shaped like that one: 26 chats in four projects and the Chats section,
four of five on the host, forks, an imported Claude chat, a leader with four subagents of which two
work, a private chat, a loop, a computer, a browser and a remote chat, long Russian titles, one chat
waiting for the reader and one working. What is checked, at 300 px and at the 232 px minimum:

- every nested chat's title box is at least 160 px wide at 300 and 110 px at 232 (a subagent, a step
  deeper, is reported and not held to it);
- no row draws more than one item after its title (its actions wait for the pointer);
- nothing in a row is drawn on an orange ground or in orange ink unless the row needs the reader;
- every property the row does not draw is in its accessible name and its tooltip;
- the slot is as wide on every row; the chats outside projects share one x for slot and title, the
  chats inside projects another, 16 px further right and right of their folder's name, and each
  subagent one step right of its leader;
- every row on a guide line draws it, the line boxes of neighbouring rows touch, and the pixels under
  the line are the line's from under the folder (or the leader's slot) down to the last row's tick,
  through the subagents between two chats; the lines are the neutral line colour, never a hue;
- the unread dot and the running state are visible;
- every row stays 32 px with a 2 px gap, the windowed list's estimate.

A project of 60 chats, opened in full and scrolled to its middle, keeps its line whole through the
windowed rows. The phone's drawer draws the same tree at its 44 px rows.

``SHOTS=<dir>`` saves the pictures: the column at 300, 276 and 232 px in dark Russian, 300 px in
light, English, a hovered and a selected row, the long project scrolled, and the phone's drawer at 390 px.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # noqa: E402
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
    # A fixture chat opened, for its row drawn as the selected one: the shared stub's detail of one of
    # its own chats, under this chat's name.
    opened = next((s for s in SESSIONS if path.endswith(f"/api/sessions/{s['id']}")), None)
    if r.request.method == "GET" and opened:
        body = {**screenshots.detail(screenshots.S2), "id": opened["id"], "title": opened["title"], "project_id": opened["project_id"]}
        return r.fulfill(status=200, content_type="application/json", body=json.dumps(body))
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
      depth: (row.closest('.sb-kids') ? 1 : 0) + (row.dataset.subagent ? 1 : 0),
      text: title.textContent, titleLeft: Math.round(tb.left), titleWidth: Math.round(tb.width), titleCut: title.scrollWidth > title.clientWidth,
      height: Math.round(rb.height), top: rb.top, trailing, inked,
      gutter: gb ? { left: Math.round(gb.left), width: Math.round(gb.width) } : null,
      label: row.getAttribute('aria-label') || '', tip: row.getAttribute('title') || '',
      unreadShown: !!dot && shown(dot), running: !!row.querySelector('.sb-word.running, .sb-pip.running, .sb-subs.live'),
      waiting: !!row.querySelector('.sb-word.waiting, .sb-word.failed'),
    };
  });
}"""

# Every row on a guide line, group by group in the order drawn: its line box (the ::before), whether it
# ends the line, and where the line it hangs from stands. A group is a project's fold, or a leader's
# subagents outside any project.
GUIDES = """() => {
  const list = document.querySelector('.sidebar-body');
  const view = list.getBoundingClientRect();
  const box = (row) => {
    const r = row.getBoundingClientRect(); const cs = getComputedStyle(row, '::before');
    return { top: r.top + parseFloat(cs.top), bottom: r.bottom - parseFloat(cs.bottom), left: r.left, rowTop: r.top, rowBottom: r.bottom,
             drawn: cs.content !== 'none' && cs.backgroundImage.includes('gradient'), image: cs.backgroundImage };
  };
  const groups = [];
  for (const section of document.querySelectorAll('.sidebar .sb-project.kids')) {
    const tile = section.querySelector(':scope > .sb-prow > .sb-tile').getBoundingClientRect();
    const rows = [...section.querySelectorAll(':scope > .sb-kids > .sb-row, :scope > .sb-kids > .sb-more, :scope > .sb-kids > .win-pad')];
    groups.push({ key: section.dataset.project, x: tile.left + tile.width / 2, start: tile.bottom, view: [view.top, view.bottom],
      rows: rows.map((row) => ({ name: row.querySelector('.sb-t')?.textContent || row.textContent.trim() || row.className, cls: row.className,
                               tail: row.classList.contains('tail') || row.classList.contains('sb-more'), pad: row.classList.contains('win-pad'), ...box(row) })) });
  }
  for (const fold of document.querySelectorAll('.sidebar .sb-fold[aria-expanded="true"]')) {
    const leader = fold.closest('.sb-row'); const slot = fold.getBoundingClientRect();
    const subs = []; for (let n = leader.nextElementSibling; n && n.classList.contains('sb-sub'); n = n.nextElementSibling) subs.push(n);
    groups.push({ key: 'subs of ' + leader.dataset.session, x: slot.left + slot.width / 2, start: slot.bottom, view: [view.top, view.bottom],
      rows: subs.map((row) => ({ name: row.querySelector('.sb-t').textContent, cls: row.className, tail: row.classList.contains('tail'), pad: false, ...box(row) })) });
  }
  return { groups, side: document.querySelector('.sidebar').getBoundingClientRect().toJSON(),
           folders: [...document.querySelectorAll('.sidebar .sb-prow > .sb-t')].map((t) => Math.round(t.getBoundingClientRect().left)) };
}"""


def guides(page, where: str, problems: list[str]) -> int:  # type: ignore[no-untyped-def]
    """The guide lines: drawn on every row of a fold, their boxes touching, and on screen unbroken.

    The pixels are read from a picture of the column: one column of them under each line, from just
    under the folder (or the leader's slot) to the middle of the row that ends it, each against the
    pixel three to its left, which is the row's own ground. Returns how many lines were followed."""
    facts = page.evaluate(GUIDES)
    shot_png = page.screenshot(clip={"x": facts["side"]["x"], "y": facts["side"]["y"], "width": facts["side"]["width"], "height": facts["side"]["height"]})
    picture = Image.open(io.BytesIO(shot_png)).convert("RGB")
    followed = 0
    for group in facts["groups"]:
        rows = group["rows"]
        name = f"{where} {group['key']}"
        if not rows:
            continue
        for row in rows:
            if not row["pad"] and not row["drawn"]:
                problems.append(f"{name}: the row {row['name'][:24]!r} draws no guide line")
            if any(hue in row["image"] for hue in ("oklch", "hsl")) or ("rgb" in row["image"] and not grey(row["image"])):
                problems.append(f"{name}: the guide of {row['name'][:24]!r} is drawn in a colour: {row['image'][:80]}")
        drawn = [r for r in rows if not r["pad"]]
        for a, b in zip(rows, rows[1:], strict=False):
            if b["top"] > a["bottom"] + 0.5:
                problems.append(f"{name}: the line breaks between {a['name'][:20]!r} and {b['name'][:20]!r} ({a['bottom']:.1f} → {b['top']:.1f})")
        # A project's own line ends at its last chat or its "show more"; a subagent's tail ends its
        # leader's line, not the project's.
        ends = [r for r in drawn if r["tail"] and "sb-sub" not in r["cls"].split()]
        own = [r for r in drawn if "sb-sub" not in r["cls"].split()]
        if group["key"].startswith("subs of "):
            last = drawn[-1]
            if not last["tail"]:
                problems.append(f"{name}: the last subagent does not end its line")
        elif len(ends) != 1 or ends[0] is not own[-1]:
            problems.append(f"{name}: the line should end at the last row only, ends at {[r['name'][:20] for r in ends]}")
        last = drawn[-1]
        x = int(group["x"] - facts["side"]["x"])
        top = max(group["start"] + 3, group["view"][0]) - facts["side"]["y"]
        bottom = min((last["rowTop"] + last["rowBottom"]) / 2 - 1, group["view"][1]) - facts["side"]["y"]
        missing = []
        for y in range(int(top), int(bottom)):
            line, ground = picture.getpixel((x, y)), picture.getpixel((x - 3, y))
            if sum(abs(p - q) for p, q in zip(line, ground, strict=True)) < 18:
                missing.append(y)
        if missing:
            problems.append(f"{name}: no line under x {x} at {len(missing)} of {int(bottom - top)} px (first at y {missing[0]})")
        followed += 1
    return followed


def grey(image: str) -> bool:
    """Whether every rgb() in a computed background is a grey: the neutral line colour, not a hue."""
    for r, g, b in re.findall(r"rgba?\((\d+), (\d+), (\d+)", image):
        if max(int(r), int(g), int(b)) - min(int(r), int(g), int(b)) > 6:
            return False
    return True


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


def judge(rows: list[dict], width: int, problems: list[str], folders: list[int]) -> None:
    where = f"{width}px"
    least = 160 if width >= 300 else 110
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
    widths = {r["gutter"]["width"] for r in rows if r["gutter"]}
    if len(widths) > 1:
        problems.append(f"{where}: the slot's width differs between rows: {sorted(widths)}")
    # One x per kind of row: outside the projects, inside one, a subagent of each, one 16 px step apart.
    loose = {(r["gutter"]["left"], r["titleLeft"]) for r in rows if r["gutter"] and r["depth"] == 0}
    if len(loose) != 1:
        problems.append(f"{where}: the chats outside the projects do not share one x: {sorted(loose)}")
        return
    slot, title = loose.pop()
    for depth, kind in ((1, "chat"), (1, "sub"), (2, "sub")):
        group = [r for r in rows if r["depth"] == depth and (r["sub"] == (kind == "sub")) and r["gutter"]]
        if depth == 1 and kind == "sub":
            group = [r for r in group if not r["nested"]]
        found = {(r["gutter"]["left"] - slot, r["titleLeft"] - title) for r in group}
        if found and found != {(16 * depth, 16 * depth)}:
            problems.append(f"{where}: {kind} rows at depth {depth} stand {sorted(found)} right of a loose chat, not {16 * depth}")
    nested = [r["titleLeft"] for r in rows if r["nested"] and not r["sub"]]
    if folders and nested and min(nested) <= max(folders):
        problems.append(f"{where}: a nested chat's title starts at {min(nested)}, not right of its folder's at {max(folders)}")
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
    deeper = [r["titleWidth"] for r in rows if r["sub"] and r["nested"]]
    print(f"{width}px: nested title width min {min(nested, default=0)} median {sorted(nested)[len(nested) // 2] if nested else 0} max {max(nested, default=0)}; "
          f"top-level min {min(loose, default=0)} max {max(loose, default=0)}; subagent in a project min {min(deeper, default=0)} max {max(deeper, default=0)}; "
          f"most trailing items {max((len(r['trailing']) for r in rows), default=0)}")


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"{SHOTS}/{name}.png")


def column(browser, width: int, *, scheme: str = "dark", lang: str = "ru", viewport: tuple[int, int] = (1920, 1080), at: str = "", handler=None):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    context.add_init_script(seed(width))
    page = context.new_page()
    page.route("**/api/**", handler or route)
    page.goto(f"{BASE}/agents{f'/{at}' if at else ''}?token=t&lang={lang}&scheme={scheme}", wait_until="networkidle")
    page.wait_for_selector(".sidebar .sb-kids .sb-row", timeout=20000)
    page.wait_for_timeout(400)
    return context, page


# A project as long as the operator's longest, to scroll through the windowed rows.
LONG = "ab00000000ff"


def long_listing() -> dict:
    rows = [chat(f"ff{n:010d}", f"Длинный проект, чат номер {n}: разбор и проверка", LONG, minutes=10 + n * 30) for n in range(60)]
    for row in rows:
        row["project"] = "Длинный проект"
    project = {"id": LONG, "name": "Длинный проект", "folders": [dict(f, env="host") for f in folders("/home/you/work/long")], "created_at": ago(days=20),
               "settings": {"snapshots": True}, "total": len(rows), "active": 0, "loops": 0, "last_message_at": rows[0]["last_message_at"], "pinned_at": ""}
    loose = chat("ee0000000001", "Почему контейнер теряет сеть после перезапуска хоста", "", minutes=15)
    own = {"id": loose["project_id"], "name": loose["title"], "folders": [dict(f, env="host") for f in folders("/home/you/chats/x")], "created_at": ago(days=6),
           "settings": {"snapshots": False, "ephemeral": True}, "total": 1, "active": 0, "loops": 0, "last_message_at": loose["last_message_at"], "pinned_at": ""}
    return {"sessions": [*rows, loose], "projects": [project, own]}


def long_route(r) -> None:  # type: ignore[no-untyped-def]
    path = r.request.url.split("?", 1)[0]
    if r.request.method == "GET" and path.endswith("/api/sessions"):
        return r.fulfill(status=200, content_type="application/json", body=json.dumps(long_listing()))
    return stub(r)


def long_project(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Sixty chats opened in full and scrolled to the middle: the rows are windowed there, and the line
    must run whole through whichever of them are mounted."""
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    context.add_init_script(f"try {{ localStorage.setItem('daedalus.folder.{LONG}', '1'); localStorage.setItem('daedalus.width.sidebar', '300'); }} catch (e) {{}}")
    page = context.new_page()
    page.route("**/api/**", long_route)
    page.goto(f"{BASE}/agents?token=t&lang=ru&scheme=dark", wait_until="networkidle")
    page.wait_for_selector(".sidebar .sb-more", timeout=20000)
    page.locator(".sidebar .sb-more").click()
    page.wait_for_timeout(300)
    body = page.locator(".sidebar-body")
    body.evaluate("el => el.scrollTo(0, (el.scrollHeight - el.clientHeight) / 2)")
    page.wait_for_timeout(400)
    mounted = page.evaluate("() => document.querySelectorAll('.sidebar .sb-kids > .sb-row').length")
    spacers = page.evaluate("() => document.querySelectorAll('.sidebar .sb-kids > .win-pad').length")
    print(f"long project: {mounted} of 60 rows mounted, {spacers} spacers")
    if mounted >= 60 or spacers == 0:
        problems.append(f"the long project is not windowed ({mounted} rows, {spacers} spacers): the check proves nothing there")
    guides(page, "long project, middle", problems)
    shot(page, "sidebar-rows-long-middle-dark-ru")
    context.close()


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
            folders_x = page.evaluate("() => [...document.querySelectorAll('.sidebar .sb-prow > .sb-t')].map((t) => Math.round(t.getBoundingClientRect().left))")
            if width != 276:
                judge(rows, width, problems, folders_x)
            followed = guides(page, f"{width}px", problems)
            if followed < 3:
                problems.append(f"{width}px: only {followed} guide lines were in view to follow")
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
                guides(page, "300px hovered", problems)
                shot(page, "sidebar-rows-300-hover-dark-ru")
                page.locator(".sidebar-body").evaluate("el => el.scrollTo(0, el.scrollHeight)")
                page.mouse.move(1200, 500)
                page.wait_for_timeout(300)
                guides(page, "300px end", problems)
                shot(page, "sidebar-rows-300-dark-ru-end")
            context.close()
        # A selected chat inside a project, and a selected subagent under a leader inside one: the
        # fill spans the row and the line is drawn over it.
        context, page = column(browser, 300, at="aa0000000006")
        guides(page, "300px selected", problems)
        if not page.locator(".sidebar [data-session='aa0000000006'].current").count():
            problems.append("the open chat's row is not drawn as selected")
        shot(page, "sidebar-rows-300-selected-dark-ru")
        context.close()
        context, page = column(browser, 300, scheme="light")
        guides(page, "300px light", problems)
        shot(page, "sidebar-rows-300-light-ru")
        context.close()
        context, page = column(browser, 300, lang="en")
        shot(page, "sidebar-rows-300-dark-en")
        context.close()
        long_project(browser, problems)
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.add_init_script(seed(300))
        page = phone.new_page()
        page.route("**/api/**", route)
        page.goto(f"{BASE}/agents?token=t&lang=ru&scheme=dark", wait_until="networkidle")
        open_drawer(page)
        drawer = page.evaluate("""() => [...document.querySelectorAll('.ph-drawer-root.open [data-session]')].slice(0, 12).map((row) => {
          const t = row.querySelector('.sb-t').getBoundingClientRect(); const g = row.querySelector('.sb-gut');
          return { w: Math.round(t.width), left: Math.round(t.left), h: Math.round(row.getBoundingClientRect().height), gut: g ? Math.round(g.getBoundingClientRect().width) : 0,
                   nested: !!row.closest('.sb-kids'), sub: !!row.dataset.subagent };
        })""")
        print("drawer 390:", drawer)
        if any(r["h"] < 44 for r in drawer):
            problems.append(f"the drawer's rows are under a finger's 44 px: {drawer}")
        if len({r["left"] for r in drawer if r["nested"] and not r["sub"]}) > 1:
            problems.append(f"the drawer's chats inside a project start at different x: {drawer}")
        if page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"):
            problems.append("the phone scrolls sideways with the drawer open")
        guides(page, "drawer 390", problems)
        shot(page, "sidebar-rows-drawer-390-dark-ru")
        phone.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    raise SystemExit(run())
