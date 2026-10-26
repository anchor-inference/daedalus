"""The review of the notes the background pass held back, and reading every chat again.

What is checked, on a desktop window and on a phone, in both languages:

- the entries list says how many notes wait (a banner) and which entry they are for (a count badge on
  its row);
- the review groups them by entry, the busiest first, each group with its count; every row names the
  chat it was learned in, linked to the message, and shows what the operator said there;
- Accept all sends the group's ids in one decision, the group leaves, and the toast's Undo asks the
  history for that decision back, which opens the group again;
- grouped by chat read, Dismiss all sends that pass's ids in one decision;
- opened from an entry, the review shows that entry's notes only;
- Settings → Memory: Read every chat again asks first and then starts the re-read;
- Settings → Memory: Tidy memory now asks the host for a dry run, says what would change (the test
  notes it would delete listed by their words), runs for real only on the operator's yes, and its
  toast's Undo asks the history for the tidy-up back;
- on a phone nothing leaves the screen sideways.

    APP_URL=http://127.0.0.1:8236/app python3 tests/browser/check_memory_review.py
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_memory_entries import OVERFLOW, open_page, wrote  # noqa: E402
from memory_stub import MemoryStub  # noqa: E402
from screenshots import S1, S2, UNHANDLED, memory_stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"banner": "5 notes from your chats wait", "byPass": "By chat read", "undo": "Undo", "reread": "Read again", "tidy": "Tidy",
           "merged": "1 reworded note merged into its twin", "probes": "1 note left by early tests deleted"},
    "ru": {"banner": "5 заметок из ваших чатов ждут", "byPass": "По прочитанным чатам", "undo": "Вернуть", "reread": "Перечитать", "tidy": "Прибрать",
           "merged": "1 перефразированная заметка объединена с двойником", "probes": "1 заметка от ранних тестов удалена"},
}
AREA = "ent-area-bakery-site"
PASS_A, PASS_B = "memory-a-0001", "memory-b-0002"


def _held(id: int, text: str, entry_id: str | None, entry_title: str, session: str, title: str, run: str, at: str, quote: str, kind: str = "addition", **over: object) -> dict:
    return {"id": id, "kind": kind, "memory_ids": [], "keep_id": "", "text": text, "score": 0, "reason": "", "created_at": at, "action": "add",
            "entry_id": entry_id, "entry_title": entry_title, "session_id": session, "session_title": title, "from_seq": 12 + id, "quote": quote,
            "pass": run, "pass_at": at, "why": "ungrounded", "tokens": ["SHOP_SHEET_ID"], **over}


HELD = [
    _held(21, "The order form writes to the sheet named in SHOP_SHEET_ID", AREA, "Bakery site", S1, "Order form", PASS_A, "2026-10-26T09:00:00+00:00", "put the orders in the owner's sheet"),
    _held(22, "Orders above 40 need a phone number", AREA, "Bakery site", S1, "Order form", PASS_A, "2026-10-26T09:00:00+00:00", "big orders: ask for a phone"),
    _held(23, "The confirmation page shows the pickup time", AREA, "Bakery site", S1, "Order form", PASS_A, "2026-10-26T09:00:00+00:00", "say when to pick it up"),
    _held(24, "Deploys go through deploy.sh, Fridays only", "ent-topic-deploy", "Deploy", S2, "Friday deploy", PASS_B, "2026-10-26T11:00:00+00:00", "only on fridays",
          kind="revision", memory_ids=["m2"], keep_id="m2", action="update", why="locked", tokens=[]),
    _held(25, "The shop is hosted on node-7", None, "Hosting", S2, "Friday deploy", PASS_B, "2026-10-26T11:00:00+00:00", "the box we rent"),
]


def stubbed() -> MemoryStub:
    memory = memory_stub()
    memory.suggestions = copy.deepcopy(HELD)
    return memory


def overflow(page: Page, where: str, problems: list[str], scope: str) -> None:
    for item in page.evaluate(OVERFLOW.replace(".memory *, .pagehead *, .memory-composer *", scope))[:5]:
        problems.append(f"{where}: past the screen's edge: {item}")


def check(browser, width: int, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}px review"
    words = WORDS[lang]
    memory = stubbed()
    page = open_page(browser, width, lang, memory)
    page.wait_for_selector(".memory-review-banner", timeout=20000)
    if words["banner"] not in page.locator(".memory-review-banner").inner_text():
        problems.append(f"{where}: the banner reads {page.locator('.memory-review-banner').inner_text()!r}")
    badge = page.locator(f'.memory-entry-row[data-entry="{AREA}"] [data-suggestions]')
    if not badge.count() or badge.inner_text().strip() != "3":
        problems.append(f"{where}: the Bakery site row's badge is {badge.all_inner_texts()}")
    if width < 600:
        overflow(page, where, problems, ".memory *, .pagehead *")
    page.locator(".memory-review-banner").click()
    page.wait_for_selector(".memory-review-group", timeout=8000)
    groups = page.evaluate("() => [...document.querySelectorAll('.memory-review-group')].map((g) => [g.dataset.group, g.querySelector('.memory-badge').innerText.trim()])")
    if groups[:1] != [[f"entry:{AREA}", "3"]] or len(groups) != 3:
        problems.append(f"{where}: grouped by entry the review reads {groups}")
    row = page.locator('.memory-review-row[data-suggestion="21"]')
    link = row.locator("a.memory-source")
    href = link.get_attribute("href") or ""
    if S1 not in href or "#m33" not in href or "Order form" not in link.inner_text():
        problems.append(f"{where}: the chat link is {href!r} {link.inner_text()!r}")
    if "owner's sheet" not in row.locator("[data-quote]").inner_text():
        problems.append(f"{where}: the quote reads {row.locator('[data-quote]').all_inner_texts()}")
    if width < 600:
        overflow(page, where, problems, ".memory-review *")
    # Accept the Bakery site group as one decision; Undo opens it again.
    page.locator(f'.memory-review-group[data-group="entry:{AREA}"] [data-action="accept-all"]').click()
    page.wait_for_function("area => !document.querySelector(`.memory-review-group[data-group='entry:${area}']`)", arg=AREA, timeout=8000)
    sent = wrote(memory, "POST", "/api/memory/suggestions/decide")
    if sent[-1:] != [{"action": "apply", "ids": [21, 22, 23]}]:
        problems.append(f"{where}: Accept all sent {sent}")
    page.locator(".toast").get_by_role("button", name=words["undo"]).click()
    page.wait_for_selector(f'.memory-review-group[data-group="entry:{AREA}"]', timeout=8000)
    if not any(path.endswith("/undo") for _, path, _ in memory.writes):
        problems.append(f"{where}: Undo sent {[p for _, p, _ in memory.writes]}")
    # By chat read: the second pass's two notes go with one Dismiss all.
    page.locator(".memory-review-by").get_by_role("radio", name=words["byPass"]).click()
    page.wait_for_selector(f'.memory-review-group[data-group="pass:{PASS_B}"]', timeout=8000)
    order = page.evaluate("() => [...document.querySelectorAll('.memory-review-group')].map((g) => g.dataset.group)")
    if order != [f"pass:{PASS_B}", f"pass:{PASS_A}"]:
        problems.append(f"{where}: grouped by chat read the review reads {order}")
    page.locator(f'.memory-review-group[data-group="pass:{PASS_B}"] [data-action="dismiss-all"]').click()
    page.wait_for_function("run => !document.querySelector(`.memory-review-group[data-group='pass:${run}']`)", arg=PASS_B, timeout=8000)
    sent = wrote(memory, "POST", "/api/memory/suggestions/decide")
    if sent[-1:] != [{"action": "dismiss", "ids": [24, 25]}]:
        problems.append(f"{where}: Dismiss all sent {sent}")
    page.context.close()

    # Opened from the entry, only that entry's notes.
    memory = stubbed()
    page = open_page(browser, width, lang, memory, f"memory/{AREA}")
    page.wait_for_selector(".memory-review-banner", timeout=20000)
    page.locator(".memory-review-banner").click()
    page.wait_for_selector(".memory-review-row", timeout=8000)
    shown = page.evaluate("() => [...document.querySelectorAll('.memory-review-row')].map((r) => Number(r.dataset.suggestion))")
    if shown != [21, 22, 23] or page.locator(".memory-review-by").count():
        problems.append(f"{where}: from the entry the review shows {shown}")
    page.context.close()


def check_reread(browser, width: int, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}px reread"
    memory = MemoryStub()
    page = open_page(browser, width, lang, memory, "settings/memory", [])
    page.wait_for_selector('[data-setting="memory.reread"] [data-action="memory-reread"]', timeout=20000)
    page.locator('[data-action="memory-reread"]').click()
    dialog = page.locator(".sheet-backdrop.confirm .dialog")
    dialog.wait_for(timeout=5000)
    if wrote(memory, "POST", "/api/memory/synthesis/reread"):
        problems.append(f"{where}: the re-read started before the answer")
    dialog.get_by_role("button", name=WORDS[lang]["reread"]).click()
    page.wait_for_timeout(600)
    if memory.reread_runs != 1:
        problems.append(f"{where}: the re-read ran {memory.reread_runs} time(s)")
    if width < 600:
        overflow(page, where, problems, ".memory-learning *")
    page.context.close()


def check_tidy(browser, width: int, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}px tidy"
    words = WORDS[lang]
    memory = memory_stub()
    twin = copy.deepcopy(next(r for r in memory.records if r["id"] == "m2"))
    twin.update({"id": "m2-twin", "text": twin["text"].upper()})
    probe = copy.deepcopy(twin)
    probe.update({"id": "m-probe", "text": "The secret word is LANTERN."})
    memory.records += [twin, probe]
    count = len(memory.records)
    page = open_page(browser, width, lang, memory, "settings/memory", [])
    page.wait_for_selector('[data-setting="memory.tidy_now"] [data-action="memory-tidy-now"]', timeout=20000)
    page.locator('[data-action="memory-tidy-now"]').click()
    dialog = page.locator(".sheet-backdrop.confirm .dialog")
    dialog.wait_for(timeout=5000)
    said = dialog.inner_text()
    if memory.tidy_runs != [True]:
        problems.append(f"{where}: before the answer the tidy-up ran {memory.tidy_runs}")
    for wanted in (words["merged"], words["probes"], "The secret word is LANTERN."):
        if wanted not in said:
            problems.append(f"{where}: the dry run's dialog does not say {wanted!r}: {said!r}")
    if width < 600:
        overflow(page, where, problems, ".sheet-backdrop.confirm *")
    dialog.get_by_role("button", name=words["tidy"]).click()
    page.wait_for_selector(".toast", timeout=8000)
    if memory.tidy_runs != [True, False] or len(memory.records) != count - 2:
        problems.append(f"{where}: after the yes the tidy-up ran {memory.tidy_runs} and memory holds {len(memory.records)} of {count}")
    page.locator(".toast").get_by_role("button", name=words["undo"]).click()
    page.wait_for_timeout(600)
    if not any(path.endswith("/undo") for _, path, _ in memory.writes) or len(memory.records) != count:
        problems.append(f"{where}: Undo left {len(memory.records)} of {count} notes")
    page.context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (1280, 390):
                check(browser, width, lang, problems)
                check_reread(browser, width, lang, problems)
                check_tidy(browser, width, lang, problems)
            print(f"memory review {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
