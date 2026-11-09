"""The closing lines of stopped runs fold into one, go quiet once a later turn answers, and a finished
turn says how fast it generated.

The case that asked for the first: the model provider went away for a quarter of an hour, the host
drove the turn again as it waited, and each attempt that failed left a red "No answer — the run
stopped" card: four in a row, still red after "continue" had the agent working again. They must be
one line with a count and the time span, and one quiet line once the next turn has answered — read
from the history, so a reload draws it the same way. The second: the row under an answer carries the
turn's speed as Usage words a rate, with the output, the generation time and the model on hover.

    APP_URL=http://127.0.0.1:<port>/app python3 tests/browser/check_turn_feed.py

Exit 0 when every step holds; ``SHOTS=<dir>`` keeps the pictures, in both languages, both schemes, a
desktop and a phone.
"""
from __future__ import annotations

import os
import sys

from check_composer import BASE, CHROMIUM, HOST, SESSION, UNHANDLED, Host, message, stub
from playwright.sync_api import sync_playwright

STOPPED = {"status": "failed", "cause": "provider", "error_kind": "llm_provider_error", "reason": "connection", "detail": "Provider: transport error: All connection attempts failed", "answered": False}
SPEED = {"tokens": 1234, "ms": 14180, "rate": 87.02, "calls": 3, "model": "qwen3.8-27b"}


def stamped(seq: int, role: str, text: str, minute: int, **over: object) -> dict:
    return {**message(seq, role, text, **over), "created_at": f"2026-11-08T21:{minute:02d}:00+00:00"}


def outage() -> list[dict]:
    """A turn that worked, then stopped on the provider; three runs the host drove again, each stopped the same way."""
    rows = [
        stamped(501, "user", "Fix the scope playhead and the hero fade.", 0, run_id="r1"),
        stamped(502, "assistant", "", 0, run_id="r1", tool_calls=[{"id": "v1", "name": "ImageView", "arguments": {"path": "shots/hero.png"}}]),
        stamped(503, "tool", "", 0, run_id="r1", tool_results=[{"id": "v1", "content": "image", "is_error": False}]),
        stamped(504, "system", "The run ended without an answer.", 0, run_id="r1", outcome={**STOPPED, "steps": 29, "last_tool": "ImageView"}),
    ]
    for n, minute in ((2, 2), (3, 4), (4, 9)):
        rows.append(stamped(500 + 2 * n + 1, "user", "[The previous turn stopped because the model provider was unreachable for a while.]", minute, run_id=f"r{n}", internal=True, origin="core"))
        rows.append(stamped(500 + 2 * n + 2, "system", "The run ended without an answer.", minute, run_id=f"r{n}", outcome={**STOPPED, "steps": 0}))
    return rows


def resumed() -> list[dict]:
    return [
        stamped(531, "user", "continue", 41, run_id="r5"),
        stamped(532, "assistant", "Continuing — the fix batch is applied to all three files.", 42, run_id="r5"),
    ]


def detail_with_speed() -> dict:
    return {**Host.detail(HOST), "run_speeds": {"r5": SPEED}}


def check(page, lang: str, recovered: bool) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    where = f"{lang}{' after continue' if recovered else ''}"
    page.wait_for_selector(".run-outcome", timeout=15000)
    lines = page.locator(".run-outcome")
    if lines.count() != 1:
        problems.append(f"{where}: {lines.count()} closing lines for one outage, not one")
        return problems
    line = lines.first
    text = line.inner_text()
    if "×4" not in text:
        problems.append(f"{where}: the line does not count the four stops ({text!r})")
    # Every stop in the details: when, the engine's word and class for it, and the run it ended.
    failures = line.locator("[data-failure]")
    said = " | ".join(failures.all_text_contents())
    if failures.count() != 4 or "llm_provider_error · connection" not in said or not all(f"r{n}" in said for n in range(1, 5)):
        problems.append(f"{where}: the details do not list each stop with its code and run ({said!r})")
    quiet = line.get_attribute("data-recovered") is not None
    if quiet != recovered:
        problems.append(f"{where}: the line is {'quiet' if quiet else 'red'} {'after' if recovered else 'before'} a later answer")
    words = {"en": ("Recovered after a provider error", "No answer — the run stopped"), "ru": ("Восстановлено после ошибки провайдера", "Ответа нет — запуск остановился")}[lang]
    if (words[0] if recovered else words[1]) not in text:
        problems.append(f"{where}: the line does not say {(words[0] if recovered else words[1])!r} ({text!r})")
    if recovered:
        box = line.bounding_box()
        # One line, on a phone too: the icon, the words and the count, which open onto the details.
        if not box or box["height"] > 44:
            problems.append(f"{where}: the recovered line is more than one line ({box})")
        border = page.evaluate("() => getComputedStyle(document.querySelector('.run-outcome')).borderTopWidth")
        if border != "0px":
            problems.append(f"{where}: the recovered line is still drawn as a card ({border})")
        speed = page.locator("[data-speed]")
        if speed.count() != 1:
            problems.append(f"{where}: {speed.count()} speed labels under one finished turn")
        else:
            label = speed.inner_text().strip()
            title = speed.get_attribute("title") or ""
            want = {"en": ("≈ 87 tok/s", "1,234 output tokens", "qwen3.8-27b"), "ru": ("≈ 87 ток/с", "1", "qwen3.8-27b")}[lang]
            if label != want[0] or want[1] not in title or want[2] not in title:
                problems.append(f"{where}: the speed reads {label!r} / {title!r}")
    return problems


def main() -> int:
    shots = os.environ.get("SHOTS")
    problems: list[str] = []
    HOST.status = "idle"
    HOST.detail = detail_with_speed  # type: ignore[method-assign]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for width, height, phone in ((1280, 900, False), (390, 844, True)):
            for scheme in ("dark", "light"):
                for lang in ("en", "ru"):
                    for recovered in (False, True):
                        HOST.messages = outage() + (resumed() if recovered else [])
                        context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, is_mobile=phone, has_touch=phone)
                        page = context.new_page()
                        page.route("**/api/**", stub)
                        page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={lang}")
                        found = check(page, lang, recovered)
                        problems += [f"{width}px {scheme}: {p}" for p in found]
                        if shots:
                            line = page.locator(".run-outcome").first
                            if line.count():
                                line.locator("summary").click()
                                page.wait_for_timeout(150)
                                line.scroll_into_view_if_needed()
                            page.screenshot(path=os.path.join(shots, f"feed-{'phone' if phone else 'desk'}-{scheme}-{lang}-{'recovered' if recovered else 'stopped'}.png"))
                        context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    failed = main()
    sys.exit(failed or UNHANDLED.report())
