"""Waiting for what a page does in answer, on a condition and not on a clock.

A click that makes the app send a request, or draw something after the host answers, finishes on its
own time: a few milliseconds on a quiet machine and a good while on a busy one. A pause of a fixed
length before reading the result is right on the machine it was tuned on and wrong on every other,
and the check fails for the speed of the machine and says nothing of the app.
"""

from __future__ import annotations

from collections.abc import Callable

from playwright.sync_api import Page


def until(page: Page, condition: Callable[[], object], bound_ms: int = 15_000, step_ms: int = 50) -> bool:
    """Poll `condition` until it holds or `bound_ms` has gone by, and say which.

    It never raises: the assertion that follows reads the same state and fails with its own message,
    which names what was expected. The bound is the limit of a failure, not a pause; a condition that
    is met returns on the next poll, so a quiet machine waits no longer than the page does.
    """
    waited = 0
    while waited < bound_ms:
        if condition():
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return bool(condition())
