"""How much main-thread work one action costs the app, measured so that a busy machine does not inflate it.

The guards that refuse a long task used to read the browser's long-task entries, which are wall-clock
durations. On a machine shared with other work a 20 ms task that is preempted for 40 ms is reported as
a 60 ms task, and a Chromium slowed down four times (`Emulation.setCPUThrottlingRate`, the way the
failure was reproduced) reports every task four times as long: the guards failed now and then while
the app had not changed at all. Two things are separated here:

- **Preemption** — the time the renderer waits for a core while something else runs — is taken out by
  measuring each task's *thread time*, the CPU time the renderer's main thread really spent in it,
  from a Chromium trace. A task that is merely waiting does not grow.
- **A slower processor** — a throttled browser, a slower machine, a core shared with a hyperthread
  sibling — really does make the same work take longer, and thread time grows with it (Chromium's
  throttle spins inside the renderer's own thread). That is measured with a fixed piece of JavaScript
  run in the same page right before and right after the action, and the budget grows by the same
  factor. It never shrinks below the plain budget, so a faster machine is held to 50 ms as before.

What is still caught is work the app itself does: a synchronous 150 ms loop in a click handler is
150 ms of thread time on any machine, three times the budget, and on a throttled browser it is slowed
exactly as much as the calibration is.

`CPU_THROTTLE=4` slows every page a check opens four times (see `prepare`), to prove the guard holds
on a slow machine.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field

from playwright.sync_api import Browser, Page, Request

BUDGET_MS = 50

# A fixed piece of arithmetic: about 22 ms of one core at full speed on the development machine, which
# is REFERENCE_MS. Long enough that Chromium's throttle, which suspends the thread in slices of a few
# milliseconds, slows every sample by close to its full rate; short enough that the ten run around an
# action cost about a fifth of a second on an idle machine.
CALIBRATION = """([iterations, samples]) => {
  const out = [];
  for (let s = 0; s < samples; s++) {
    const start = performance.now();
    let x = 0;
    for (let i = 0; i < iterations; i++) x = (x + Math.sqrt(i + s)) % 1e9;
    out.push(performance.now() - start);
    if (x < 0) out.push(x); // never: keeps the loop from being optimised away
  }
  return out;
}"""
ITERATIONS = 2_000_000
REFERENCE_MS = 22.0
SAMPLES = 4

# One top-level task of the renderer's main thread; nested run loops are rare and counted as their own.
TASK = "ThreadControllerImpl::RunTask"
MARK = "daedalus-main-thread-watch"


def prepare(page: Page) -> None:
    """Before the page opens: slow it by CPU_THROTTLE times when that is set, and count its requests in
    flight for `quiet`."""
    rate = float(os.environ.get("CPU_THROTTLE", "1") or 1)
    if rate > 1:
        page.context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": rate})
    flying: set[Request] = set()
    _FLYING[id(page)] = flying

    def started(request: Request) -> None:
        # The event stream and a session's stream stay open, or open again as soon as they close: they
        # are the app listening, not loading.
        path = request.url.split("?", 1)[0]
        if "/api/events" not in path and not path.endswith("/stream"):
            flying.add(request)

    def ended(request: Request) -> None:
        flying.discard(request)

    page.on("request", started)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)


_FLYING: dict[int, set[Request]] = {}


def calibrate(page: Page) -> list[float]:
    # The first sample warms the JIT and is dropped: it measures the compiler, not the processor.
    return page.evaluate(CALIBRATION, [ITERATIONS, SAMPLES + 1])[1:]


@dataclass
class Measurement:
    tasks: list[float]  # thread time of each task over 1 ms, in ms, longest first
    walls: list[float]  # the same tasks' wall-clock durations, for the report
    slowdown: float
    samples: list[float] = field(default_factory=list)

    @property
    def budget(self) -> float:
        return BUDGET_MS * self.slowdown

    @property
    def over(self) -> list[float]:
        return [ms for ms in self.tasks if ms > self.budget]

    def describe(self) -> str:
        return (
            f"longest tasks {self.tasks[:5]} ms of thread time ({self.walls[:5]} ms by the clock), "
            f"budget {self.budget:.0f} ms at a slowdown of {self.slowdown:.2f} "
            f"(calibration {[round(s, 1) for s in self.samples]} ms against {REFERENCE_MS} ms)"
        )


class Watch:
    """Trace the renderer from `begin` to `end` and report the tasks that ran on the page's main thread.

    One watch per browser at a time: Chromium traces one session at a time."""

    def __init__(self, browser: Browser, page: Page) -> None:
        self.browser, self.page = browser, page
        self.samples: list[float] = []

    def begin(self) -> None:
        self.samples = calibrate(self.page)
        self.browser.start_tracing(page=self.page, categories=["toplevel", "blink.user_timing"])
        # The mark names the renderer and its main thread in the trace, and when the action began:
        # other renderers (a closed context's, a worker's) and the tasks before it are not the action's.
        self.page.evaluate("(name) => performance.mark(name)", MARK)

    def end(self) -> Measurement:
        trace = json.loads(self.browser.stop_tracing())
        self.samples += calibrate(self.page)
        events = trace["traceEvents"] if isinstance(trace, dict) else trace
        mark = next((e for e in events if e.get("name") == MARK), None)
        assert mark is not None, "the trace holds no mark from the page: the renderer was not traced"
        tasks = [
            e for e in events
            if e.get("name") == TASK and e.get("ph") == "X" and e.get("pid") == mark["pid"] and e.get("tid") == mark["tid"] and e.get("ts", 0) > mark["ts"]
        ]
        tasks.sort(key=lambda e: e.get("tdur", e.get("dur", 0)), reverse=True)
        slowdown = max(1.0, statistics.median(self.samples) / REFERENCE_MS)
        return Measurement(
            tasks=[round(e.get("tdur", e.get("dur", 0)) / 1000, 1) for e in tasks if e.get("tdur", e.get("dur", 0)) > 1000],
            walls=[round(e.get("dur", 0) / 1000, 1) for e in tasks if e.get("tdur", e.get("dur", 0)) > 1000],
            slowdown=slowdown,
            samples=self.samples,
        )


# Resolves once the page has gone `quiet` ms with no change to its DOM and no layout shift, or after
# `limit` ms whatever it does.
QUIET = """([quiet, limit]) => new Promise((done) => {
  const started = performance.now();
  let last = performance.now();
  const touch = () => { last = performance.now(); };
  const mutations = new MutationObserver(touch);
  mutations.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
  const shifts = new PerformanceObserver(touch);
  shifts.observe({ type: "layout-shift" });
  const tick = () => {
    const now = performance.now();
    if (now - last >= quiet || now - started >= limit) {
      mutations.disconnect(); shifts.disconnect();
      done(now - last >= quiet);
    } else setTimeout(tick, 50);
  };
  setTimeout(tick, 50);
})"""


def quiet(page: Page, *, quiet_ms: int = 400, limit_ms: int = 10_000) -> bool:
    """Wait until the page has stopped arriving: no request of its own in flight (see `prepare`), and
    then `quiet_ms` with no change to its DOM and no layout shift. False when that never came within
    `limit_ms`.

    A page that has only just drawn its first frame is still arriving: a project's team, a chunk, a
    font. On a loaded machine that arrival landed after the check had begun measuring its first action,
    and the page's own load shift was reported as that action's: 0.026 on "project focus: panel
    closed", the column's rows moving down as the team came in. The requests are waited for as well as
    the DOM, because a response that is merely slow leaves the DOM quiet for as long as it takes."""
    deadline = time.monotonic() + limit_ms / 1000
    flying = _FLYING.get(id(page), set())
    while time.monotonic() < deadline:
        if flying:
            page.wait_for_timeout(50)
            continue
        left = max(1, int((deadline - time.monotonic()) * 1000))
        settled = bool(page.evaluate(QUIET, [quiet_ms, min(left, limit_ms)]))
        if settled and not flying:
            return True
    return False
