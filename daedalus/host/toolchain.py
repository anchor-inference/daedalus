"""What the runtime image carries only when asked, and how to say it is not there.

Two things left the default image because most sessions never use them and together they were most
of its size: the ``browser`` extra (Playwright, a headless Chromium, Pillow) and Node. The skills
that need them ship with every installation either way — a skill is instructions, not code — so
something has to say, once and in the same words everywhere, that the tools those instructions name
are not installed. This is that something: the doctor prints it as a check, and the ``Skill`` tool
puts it in front of a skill that declares a program or a Python module in ``requires:``.

The answers are about the shell's world as much as the bot's: a command runs in the same container,
its sandbox mounts the root read-only, and ``python3`` on its ``PATH`` is the bot's own interpreter.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from daedalus.config import Settings

_state: dict[str, str] = {}


def status(requirement: str) -> str:
    """``"ok"``, or the reason in one sentence. Cached: nothing here appears while the process runs —
    installing any of it is a new image, or at least a restart.

    ``browser`` is the whole extra (the doctor's check); ``playwright`` is what a script needs of it;
    ``python:<module>`` is a module the shell's ``python3`` imports; any other name is a program on the
    shell's ``PATH``."""
    if requirement not in _state:
        if requirement.startswith("python:"):
            _state[requirement] = _module(requirement.removeprefix("python:"))
        else:
            probe = {"browser": _browser, "playwright": _playwright, "node": _node, "chromium": _chromium_status}.get(requirement)
            _state[requirement] = probe() if probe else _program(requirement)
    return _state[requirement]


def browser_status() -> str:
    return status("browser")


def _browser() -> str:
    missing = [name for module, name in (("playwright", "Playwright"), ("PIL", "Pillow")) if importlib.util.find_spec(module) is None]
    if not missing and not _chromium():
        missing = ["a headless Chromium"]
    if not missing:
        return "ok"
    return (
        f"{', '.join(missing)} not installed in this image. The default image leaves the browser tools out — "
        "they are a third of its size and most sessions never open a page. They are the `browser` extra: run the "
        "`:browser` tag of the agent image, or `uv sync --extra browser && playwright install chromium` "
        "outside a container."
    )


def _chromium() -> bool:
    settings = Settings()
    if settings.chrome_path and Path(settings.chrome_path).exists():
        return True
    if shutil.which("chromium") or shutil.which("chromium-browser"):
        return True
    browsers = Path(settings.playwright_browsers_path) if settings.playwright_browsers_path else Path.home() / ".cache" / "ms-playwright"
    return browsers.is_dir() and any(p.name.startswith("chromium") for p in browsers.iterdir())


def _playwright() -> str:
    """Playwright for the bot's Python and a Chromium it can launch: what a screenshot script needs.

    Both are in the image already, so a skill that reaches for ``uv add playwright`` or ``playwright
    install`` spends a minute and a hundred megabytes of the workspace on a second copy of each."""
    missing = []
    if importlib.util.find_spec("playwright") is None:
        missing.append("Playwright")
    if not _chromium():
        missing.append("a headless Chromium")
    if not missing:
        return "ok"
    return f"{' and '.join(missing)} not installed in this image: the browser extra is left out of it (the `:browser` tag of the agent image has it)."


def _chromium_status() -> str:
    return "ok" if _chromium() else "no Chromium in this image: the browser extra is left out of it (the `:browser` tag of the agent image has it)."


def _module(name: str) -> str:
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        found = False
    return "ok" if found else f"the Python module {name} is not installed for the shell's python3."


def _program(name: str) -> str:
    return "ok" if shutil.which(name) else f"{name} is not installed: no such program on the shell's PATH."


def _node() -> str:
    if shutil.which("node") and shutil.which("npx"):
        return "ok"
    return (
        "Node is not installed in this image. It was there to build the Mini App, which is now built "
        "into the image instead, and nothing else in the runtime needs it — so `npx`, `npm` and the "
        "tools they fetch are unavailable here."
    )


__all__ = ["browser_status", "status"]
