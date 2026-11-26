"""The conversation's width: 720, 840 (the default), 960 or the whole room, at laptop and wide windows.

Appearance keeps the choice in this browser; the stylesheet's `--chat-w` is the stripe the timeline, the
turns and the composer share. This check draws one long answer (a five-column comparison, a table too wide for
any column, a block of code) at 1280, 1366, 1920 and 2560 with the right panel closed and open, for each choice,
and holds four claims:

  * the timeline is the chosen width where the window has the room, and what is left where it does not;
  * the composer is exactly as wide as the timeline above it;
  * nothing makes the page, the conversation or the panel scroll sideways;
  * a table stays inside the column: a comparison that fits is not clipped at its last column, and one that
    does not fit scrolls inside its own box, which says so with an edge fade.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_chat_width.py

With SHOTS=<dir> it also photographs the answer at each choice (1920, panel closed) and the default at 1366
with the panel open, in dark Russian and in light English. Exit 0 when every claim holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import expect_app  # noqa: E402
from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, message, stub  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")

CHOICES = {"narrow": 720, "normal": 840, "wide": 960, "full": 1400}
SIDEBAR = 300
PADDING = {"narrow": 24, "normal": 24, "wide": 24, "full": 32}

COMPARISON = {
    "en": (
        "| Option | Where it runs | What it costs a month | What it is good at | What to watch |\n"
        "|---|---|---|---|---|\n"
        "| Self-hosted proxy | On your own machine, behind your router | Electricity and an evening of setup | Full control of the logs and the keys | You patch it yourself and it goes down with your link |\n"
        "| Managed gateway | In the provider's region of your choice | A flat fee plus a small charge per call | Quick to start and has a dashboard | Another party sees every request and the bill grows with use |\n"
        "| Direct to the provider | Nowhere of its own | Only the calls you make | Nothing to run and nothing to break | No caching, no fallbacks and no shared limits across tools |\n"
        "| Library in the app | Inside each program that calls a model | Nothing extra | Closest to the code that uses it | Every program repeats the same settings and the same mistakes |"
    ),
    "ru": (
        "| Вариант | Где работает | Сколько стоит в месяц | В чём силён | На что смотреть |\n"
        "|---|---|---|---|---|\n"
        "| Свой прокси | На вашей машине за домашним роутером | Электричество и вечер настройки | Полный контроль над логами и ключами | Обновлять надо самому, а падает он вместе с вашим каналом |\n"
        "| Управляемый шлюз | В выбранном вами регионе провайдера | Фиксированная плата и небольшая доплата за вызов | Быстро начать, есть панель | Каждый запрос видит третья сторона, а счёт растёт вместе с нагрузкой |\n"
        "| Напрямую к провайдеру | Своего места нет | Только ваши вызовы | Нечего запускать и нечему ломаться | Нет кеша, запасных маршрутов и общих лимитов между инструментами |\n"
        "| Библиотека в приложении | Внутри каждой программы, что вызывает модель | Ничего сверху | Ближе всего к коду, который её использует | Каждая программа повторяет те же настройки и те же ошибки |"
    ),
}

WIDE_TABLE = (
    "| Identifier | Region | Endpoint | Fingerprint | Created | Rotated | Owner | Notes | Rotation window | Last audit | Contact |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| keyproxy-primary-01 | eu-central-1 | https://gateway.internal.example/v1/chat/completions | 9f2c41ab77d0e3b85c61aa04f3d2e9b1 | 2026-03-02 | 2026-09-14 | platform-team | primary | every ninety days | 2026-10-30 | platform@example.com |\n"
    "| keyproxy-secondary-02 | eu-west-2 | https://gateway-two.internal.example/v1/chat/completions | 04bb7e19c3a85d2f6e10b9a7c4d83f52 | 2026-04-11 | 2026-10-01 | platform-team | standby | every ninety days | 2026-11-02 | platform@example.com |"
)

CODE = (
    "```python\n"
    "def pick_route(request, routes, limits):\n"
    "    # The first route that is up and under its limit wins; a route that is down is skipped without a retry here.\n"
    "    for route in routes:\n"
    "        if route.is_up() and limits.remaining(route.name, request.model) > request.estimated_tokens:\n"
    "            return route\n"
    "    raise RuntimeError(f'no route for {request.model} within the limits of {len(routes)} routes')\n"
    "```"
)


def answer(lang: str) -> str:
    ru = lang == "ru"
    lead = "Сравнил четыре способа ходить к моделям. Таблица ниже, затем ключи и код выбора маршрута." if ru else "I compared four ways of reaching the models. The table is below, then the keys and the routing code."
    keys = "Ключи, которые видит шлюз:" if ru else "The keys the gateway holds:"
    tail = "Если нужен один вариант, я бы начал со своего прокси: он дешевле всего и ничего не прячет." if ru else "If you want one, I would start with your own proxy: it is the cheapest and hides nothing."
    return f"{lead}\n\n{COMPARISON[lang]}\n\n{keys}\n\n{WIDE_TABLE}\n\n{CODE}\n\n{tail}"


MEASURE = """async () => {
  const frames = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), r: Math.round(r.right), w: Math.round(r.width) }; };
  const wraps = [];
  for (const wrap of document.querySelectorAll('.answer .tablewrap')) {
    wrap.scrollLeft = 0;
    await frames();
    const maskStart = getComputedStyle(wrap).maskImage;
    wrap.scrollLeft = 1e6;
    await frames();
    const maskEnd = getComputedStyle(wrap).maskImage;
    wrap.scrollLeft = 0;
    const table = wrap.querySelector('table');
    const heads = [...wrap.querySelectorAll('thead th')];
    const last = heads[heads.length - 1];
    const wr = wrap.getBoundingClientRect();
    wraps.push({ maskStart, maskEnd, w: Math.round(wr.width), right: Math.round(wr.right), scrolls: wrap.scrollWidth > wrap.clientWidth + 1, scrollW: wrap.scrollWidth, clientW: wrap.clientWidth,
             tableW: Math.round(table.getBoundingClientRect().width), lastRight: last ? Math.round(last.getBoundingClientRect().right) : null,
             lastTextOver: last ? last.scrollWidth > last.clientWidth + 1 : false });
  }
  const scroller = document.querySelector('.chat-scroll');
  return {
    vw: innerWidth, doc: document.documentElement.scrollWidth, body: document.body.scrollWidth,
    scroller: scroller ? { client: scroller.clientWidth, scroll: scroller.scrollWidth } : null,
    timeline: box(document.querySelector('.chat-scroll .timeline')),
    composer: box(document.querySelector('.composer-box')),
    panel: box(document.querySelector('.panel')),
    main: box(document.querySelector('.chat-main') || document.querySelector('.chat-scroll')),
    padLeft: scroller ? parseFloat(getComputedStyle(scroller).paddingLeft) : null,
    chatW: getComputedStyle(document.documentElement).getPropertyValue('--chat-w').trim(),
    wraps,
  };
}"""


# A conversation opens the panel on its own where there is room, so "closed" is the reader's own choice stored first.
CLOSED = "try { localStorage.setItem('daedalus.session.panel', '0'); localStorage.setItem('daedalus.panel.details', 'closed'); localStorage.setItem('daedalus.panel.pinned', '0'); } catch (e) {}"


def choose(context, choice: str, panel: bool) -> None:  # type: ignore[no-untyped-def]
    context.add_init_script(f"try {{ localStorage.setItem('daedalus.appearance', JSON.stringify({{ column: {json.dumps(choice)} }})); }} catch (e) {{}}")
    if not panel:
        context.add_init_script(CLOSED)


def open_chat(context, lang: str, scheme: str, panel: bool) -> Page:  # type: ignore[no-untyped-def]
    HOST.messages = [message(1, "user", "Which way of reaching the models should I use?" if lang == "en" else "Каким способом ходить к моделям?"), message(2, "assistant", answer(lang), run_id="r1")]
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?{'panel=details&' if panel else ''}token=t&lang={lang}&scheme={scheme}")
    page.wait_for_selector(".answer .tablewrap", timeout=15000)
    page.wait_for_timeout(400)
    return page


def judge(name: str, m: dict, choice: str, panel: bool) -> list[str]:
    problems: list[str] = []

    def fail(what: str) -> None:
        problems.append(f"{name}: {what}")

    if m["doc"] > m["vw"] or m["body"] > m["vw"]:
        fail(f"the page scrolls sideways ({m['doc']} / {m['body']} in {m['vw']})")
    if m["scroller"] and m["scroller"]["scroll"] > m["scroller"]["client"]:
        fail(f"the conversation scrolls sideways ({m['scroller']})")
    t, c = m["timeline"], m["composer"]
    if not t or not c:
        fail("no timeline or composer")
        return problems
    chosen = CHOICES[choice]
    if m["chatW"] != f"{chosen}px":
        fail(f"--chat-w is {m['chatW']!r}, not {chosen}px")
    if panel and m["panel"]:
        # The conversation owns the room left of the panel, less its own side padding.
        room = m["panel"]["x"] - SIDEBAR - 2 * PADDING[choice]
    else:
        room = m["vw"] - SIDEBAR - 2 * PADDING[choice]
    want = min(chosen, room)
    if abs(t["w"] - want) > 2:
        fail(f"the timeline is {t['w']}px, not {want} (the {choice} stripe {chosen} in {room}px of room)")
    if abs(c["w"] - t["w"]) > 2:
        fail(f"the composer is {c['w']}px against a {t['w']}px timeline")
    if abs(m["padLeft"] - PADDING[choice]) > 0.5:
        fail(f"the side padding is {m['padLeft']}px, not {PADDING[choice]}")
    comparison, wide = m["wraps"][0], m["wraps"][1]
    # A comparison that fits is drawn whole: its last column's head ends inside the box. One that does not fit scrolls.
    if comparison["scrolls"]:
        if comparison["w"] > 900:
            fail(f"a five-column comparison scrolls in a {comparison['w']}px box")
    elif comparison["lastRight"] is None or comparison["lastRight"] > comparison["right"] + 1:
        fail(f"the comparison's last column ends at {comparison['lastRight']}, past its box at {comparison['right']}")
    if comparison["tableW"] > comparison["w"] + 1 and not comparison["scrolls"]:
        fail(f"the comparison is {comparison['tableW']}px in a {comparison['w']}px box and does not scroll")
    if not wide["scrolls"]:
        fail("the table of eleven columns does not scroll inside its box")
    else:
        # The clipped edge fades while there is more to see on that side, and only on that side.
        if "calc(100% - 32px)" not in wide["maskStart"] or "0px," not in wide["maskStart"]:
            fail(f"at its start a cut table does not fade at the right alone ({wide['maskStart']})")
        if "32px," not in wide["maskEnd"] or "calc(" in wide["maskEnd"]:
            fail(f"at its end a cut table does not fade at the left alone ({wide['maskEnd']})")
    if not comparison["scrolls"] and "32px" in comparison["maskStart"]:
        fail("a table that fits fades at an edge")
    if wide["w"] > t["w"] + 1:
        fail(f"the wide table's box is {wide['w']}px in a {t['w']}px column")
    return problems


def run() -> list[str]:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True)
        for choice in CHOICES:
            for width, height in ((1280, 800), (1366, 768), (1920, 1080), (2560, 1440)):
                for panel in (False, True):
                    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
                    choose(context, choice, panel)
                    page = open_chat(context, "en", "dark", panel)
                    m = page.evaluate(MEASURE)
                    problems += judge(f"{choice} {width}{' panel' if panel else ''}", m, choice, panel)
                    context.close()
        if SHOTS:
            shoot(browser)
        browser.close()
    return problems


def shoot(browser) -> None:  # type: ignore[no-untyped-def]
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    for lang, scheme in (("ru", "dark"), ("en", "light")):
        for choice in CHOICES:
            context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme=scheme)
            choose(context, choice, False)
            page = open_chat(context, lang, scheme, False)
            page.screenshot(path=str(out / f"chat-{choice}-{scheme}-{lang}-1920.png"))
            context.close()
        context = browser.new_context(viewport={"width": 1366, "height": 768}, color_scheme=scheme)
        choose(context, "normal", True)
        page = open_chat(context, lang, scheme, True)
        page.screenshot(path=str(out / f"chat-normal-panel-{scheme}-{lang}-1366.png"))
        context.close()


def main() -> int:
    expect_app(BASE)
    problems = run()
    for line in problems:
        print("FAIL", line)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems or UNHANDLED.report() else 0


if __name__ == "__main__":
    sys.exit(main())
