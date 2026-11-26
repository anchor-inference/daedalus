"""Answers held back inside the page, and let go when the check says so.

Whether a late answer pushes the page around does not depend on the machine's speed, but on whether
the answer comes after the page has settled; a check that waits "long enough" on its own machine is
a different check on every other. Here the answer is kept from the page by the page's own `fetch`
(nothing is slept in the check's process, which would stop the stubs answering everything else) until
`release` is called, so the check lets the page settle first, however long that takes, and only then
lets the answer land.

    holding.gate(context, [r"/api/browser/user($|\\?)"])
    ... open the page, wait for it to settle ...
    holding.wait_held(page)
    holding.release(page)
"""

from __future__ import annotations

import json

from playwright.sync_api import BrowserContext, Page
from waiting import until

SCRIPT = """(() => {
  const rules = %s.map((source) => new RegExp(source));
  const waiting = [];
  let open = false;
  window.__held = () => waiting.length;
  window.__release = () => { open = true; while (waiting.length) waiting.pop()(); };
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (!open && rules.some((rule) => rule.test(url))) await new Promise((resolve) => waiting.push(resolve));
    return original(input, init);
  };
})();"""


def gate(context: BrowserContext, patterns: list[str]) -> None:
    """From now on every page of the context keeps back each request whose address matches one of the
    regular expressions, until `release`."""
    context.add_init_script(SCRIPT % json.dumps(patterns))


def wait_held(page: Page, bound_ms: int = 60_000) -> bool:
    """Whether the page has asked for something that is being held; waits for it to."""
    return until(page, lambda: page.evaluate("window.__held()") > 0, bound_ms)


def release(page: Page) -> None:
    """Let the held answers through, and every later one as well."""
    page.evaluate("window.__release()")
