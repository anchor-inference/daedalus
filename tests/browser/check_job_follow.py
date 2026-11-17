"""A job opens in the Jobs tab and follows its output; the Jobs counter counts the session's own jobs only.

A click on a job used to open its log in Preview, read once. Now it opens in the tab, with a way back
to the list; output written after the click appears without a reload; the view follows the end while
the reader is there, stays put when they scroll up (offering a jump to the latest line), and stops
asking once the job has ended, saying how it ended. And the number on the Jobs tab, the header's
"waiting for" and a phone row's line all read the host's count of the session's own jobs: the working
helpers are counted apart, as helpers.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_job_follow.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # noqa: E402
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import S1, S2, UNHANDLED, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS")


class LiveJob:
    """``job-1`` as the host would show it while it runs: a log that grows when the check says so."""

    def __init__(self) -> None:
        self.data = "".join(f"line {n}: compiled module {n}\n" for n in range(1, 121)).encode()
        self.running = True
        self.reads: list[int] = []

    def write(self, first: int, last: int) -> None:
        self.data += "".join(f"line {n}: compiled module {n}\n" for n in range(first, last + 1)).encode()

    def piece(self, query: str) -> dict:
        asked = {key: values[0] for key, values in parse_qs(query).items()}
        offset, limit = int(asked.get("offset", "-1")), int(asked.get("limit", "262144"))
        self.reads.append(offset)
        restart = offset < 0 or offset > len(self.data)
        start = max(0, len(self.data) - limit) if restart else offset
        part = self.data[start:start + limit]
        ended = not self.running
        return {"id": "job-1", "state": "failed" if ended else "running", "outcome": "failed" if ended else None, "exit_code": 2 if ended else None,
                "running": not ended, "text": part.decode(), "start": start, "next": start + len(part), "size": len(self.data), "restart": restart, "lines_only": False}


def at_end(page: Page) -> bool:
    return bool(page.locator(".job-log").evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight <= 2"))


def follow(page: Page, job: LiveJob, problems: list[str]) -> None:
    page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=jobs", wait_until="networkidle")
    expect(page.locator(".task-list")).to_be_visible(timeout=20000)
    page.locator('.task-row:has-text("npm run build") .task-open').click()
    view = page.locator(".panel-body.tab-jobs .job-view")
    expect(view).to_be_visible()
    if page.locator('.panel-tab[data-tab="preview"].on').count():
        problems.append("the job opened in Preview, not in the Jobs tab")
    expect(view.locator(".job-log-text")).to_contain_text("line 120: compiled module 120")
    expect(view.locator(".job-live")).to_have_text("live")
    page.wait_for_timeout(200)
    if not at_end(page):
        problems.append("the output did not open at its end")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/job-open-en.png")
    # Written after the click: appears with no reload, and the view, at the end, follows it.
    job.write(121, 125)
    expect(view.locator(".job-log-text")).to_contain_text("line 125: compiled module 125", timeout=5000)
    page.wait_for_timeout(150)
    if not at_end(page):
        problems.append("the view did not follow new output while at the end")
    # Scrolled up: it stays where the reader is, and offers the way back.
    page.locator(".job-log").evaluate("el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); }")
    expect(view.locator(".job-latest")).to_be_visible()
    job.write(126, 140)
    expect(view.locator(".job-log-text")).to_contain_text("line 140: compiled module 140", timeout=5000)
    page.wait_for_timeout(150)
    top = page.locator(".job-log").evaluate("el => el.scrollTop")
    if top > 2:
        problems.append(f"new output moved a reader who had scrolled up (scrollTop {top})")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/job-scrolled-up-en.png")
    view.locator(".job-latest").click()
    expect(view.locator(".job-latest")).to_have_count(0)
    if not at_end(page):
        problems.append("Jump to latest did not reach the end")
    # Ended: the last lines arrive, it says how it ended, and nothing more is asked for.
    job.write(141, 142)
    job.running = False
    expect(view.locator("[data-job-end]")).to_have_text("Ended: failed · exit 2", timeout=5000)
    expect(view.locator(".job-log-text")).to_contain_text("line 142: compiled module 142")
    expect(view.locator(".job-live")).to_have_count(0)
    asked = len(job.reads)
    page.wait_for_timeout(3000)
    if len(job.reads) != asked:
        problems.append(f"the view kept asking after the job ended: {len(job.reads) - asked} more reads")
    # Only the new bytes were asked for after the first look at the tail.
    if job.reads[0] != -1 or any(offset < 0 for offset in job.reads[1:]):
        problems.append(f"the reads were not incremental: {job.reads[:6]}")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/job-ended-en.png")
    view.locator(".job-back").click()
    expect(page.locator(".task-list")).to_be_visible()


def russian(page: Page, problems: list[str]) -> None:
    page.goto(f"{BASE}/agents/{S1}?token=t&lang=ru&panel=jobs", wait_until="networkidle")
    expect(page.locator(".task-list")).to_be_visible(timeout=20000)
    page.locator('.task-row:has-text("make deploy") .task-open').click()
    view = page.locator(".job-view")
    expect(view.locator("[data-job-end]")).to_have_text("Завершена: ошибка · код 3")
    expect(view.locator(".job-log-text")).to_contain_text("make: *** [deploy] Error 3")
    if "\x1b" in view.locator(".job-log-text").inner_text():
        problems.append("a colour escape reached the screen")
    expect(view.locator(".job-back")).to_have_attribute("aria-label", "Все задачи")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/job-ended-ru.png")


def counters(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """The session has one job running and two helpers at work: the Jobs counter says 1."""
    numbers = {"jobs": 1, "helpers": 2}

    def route(r):  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        if path == f"/api/sessions/{S1}":
            data = copy.deepcopy(detail(S1))
            data["status"] = "idle"
            data["background_count"] = numbers["jobs"]
            data["subagents_running"] = numbers["helpers"]
            return respond(r, data)
        return stub(r)

    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents/{S1}?token=t&lang=en&panel=jobs", wait_until="networkidle")
    expect(page.locator(".task-list")).to_be_visible(timeout=20000)
    expect(page.locator('.panel-tab[data-tab="jobs"] .count')).to_have_text("1")
    expect(page.locator(".head-status.jobs-pending")).to_have_text("waiting for 1 job")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/jobs-counter-en.png")
    # No job of its own, two helpers at work: no number on Jobs, and the header names the helpers.
    numbers["jobs"] = 0
    page.reload(wait_until="networkidle")
    expect(page.locator(".head-status.jobs-pending")).to_have_text("waiting for 2 subagents", timeout=20000)
    if page.locator('.panel-tab[data-tab="jobs"] .count').count():
        problems.append("the Jobs tab counts helpers as jobs")
    page.locator(".head-status.jobs-pending").click()
    expect(page.locator('.panel-tab[data-tab="subagents"].on')).to_be_visible()
    page.close()

    # A phone's row of an idle chat says the same.
    rows = screenshots.SESSIONS
    row = next(s for s in rows if s["id"] == S2)
    kept = dict(row)
    try:
        row.update({"status": "idle", "background_count": 1, "subagents_running": 2})
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.route("**/api/**", stub)
        phone.goto(f"{BASE}/agents?token=t&lang=en&view=chats", wait_until="networkidle")
        expect(phone.get_by_text("waiting for 1 job").first).to_be_visible(timeout=20000)
        row.update({"background_count": 0})
        phone.reload(wait_until="networkidle")
        expect(phone.get_by_text("waiting for 2 subagents").first).to_be_visible(timeout=20000)
        phone.close()
    finally:
        row.clear()
        row.update(kept)


def run() -> int:
    problems: list[str] = []
    job = LiveJob()

    def route(r):  # type: ignore[no-untyped-def]
        url = urlsplit(r.request.url)
        if url.path == f"/api/sessions/{S1}/tasks/job-1/log":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(job.piece(url.query)))
        return stub(r)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/**", route)
        for step in (lambda: follow(page, job, problems), lambda: russian(page, problems), lambda: counters(browser, problems)):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — every step is looked at; the failures are listed together
                problems.append(" ".join(str(exc).split())[:400])
        problems.extend(f"page error: {e}" for e in errors)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("a job opens in its tab, follows its output, stops at its end; the counter counts own jobs: passed")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
