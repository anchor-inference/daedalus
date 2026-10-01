"""Take the launcher's own screenshots over an invented installation.

The three pages the launcher serves — the questions on a first run, the wait while it brings
itself up, and the status afterwards — rendered by the real binary against a data directory made
for the occasion, in both languages and at both ends of the window sizes it has to survive.

Nothing real is in any of them. The data directory is created here and thrown away, the keys in it
are not keys, and every answer the pages poll for is invented in this file and handed to the page
instead of the launcher's own.

    python3 tests/browser/launcher_shots.py

The binary is built in the Go container the way desktop/build.sh builds it, unless LAUNCHER names
one that is already built. OUT is where the pictures land (default docs/screenshots).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("OUT", ROOT / "docs" / "screenshots"))
WORK = Path(os.environ.get("WORK", "/tmp/daedalus-shots"))
GO_IMAGE = os.environ.get("GO_IMAGE", "golang:1.23")
WIDTHS = [1440, 400]
LANGS = ["en", "ru"]

# ---- the invented installation ----------------------------------------------------------------

# What the launcher would have written on a first run, with nothing in it that is anyone's.
ENV_FILE = """DEEPSEEK_API_KEY=not-a-key
DAILY_USD_CAP=20
TELEGRAM_BOT_TOKEN=not-a-token
TELEGRAM_OWNER_ID=1
"""

# A first run from the seed the application carries: nothing downloaded, everything taken from the
# installation and checked.
LOG = [
    "10:24:01  uv 0.12.15 comes with the installation (17.6 MB)",
    "10:24:01  rg 15.2.0 comes with the installation (1.8 MB)",
    "10:24:02  git 2.55.0.5 comes with the installation (39.0 MB)",
    "10:24:03  installing python 3.12 from the copy that came with the installation",
    "10:24:05  daedalus comes with the installation (commit 0123456789ab)",
    "10:24:13  protocore-exp comes with the installation (commit ba9876543210)",
    "10:24:14  building the environment from the packages that came with the installation",
    "10:24:15  uv: Building daedalus @ file:///home/someone/.local/share/daedalus/data/daedalus",
]

# A launcher with no seed, on a slow line: the download it is in the middle of.
DOWNLOAD_LOG = [
    "10:24:01  downloading uv 0.12.15 (17.6 MB)",
    "10:24:06  downloading git 2.55.0.5 (39.0 MB)",
]

STAGES = ["runtime", "checkouts", "environment", "start"]


def status(**over: object) -> dict:
    base = {
        "data": str(WORK / "Daedalus" / "data"),
        "mode": "native",
        "mode_detail": "native",
        "ports": "8765, 3201",
        "configured": True,
        "repos": True,
        "docker": "",
        "running": 3,
        "app_url": "http://127.0.0.1:8765/app/",
        "telegram": True,
        "busy": "",
        "failure": "",
        "log": LOG,
        "change": {},
        "stage": "",
        "steps": STAGES,
        "done": 0,
        "size": 0,
        "elapsed": 0,
        "quiet": 0,
        "docker_missing": False,
    }
    base.update(over)
    return base


# The three answers the pages are shown, one per picture.
SHOTS = {
    "setup": ("/setup", status(configured=False, repos=False)),
    # The environment, built from the seed: uv's own count, and the heartbeat once nothing has
    # moved for a few seconds.
    "progress": (
        "/progress",
        status(
            busy="start",
            stage="environment",
            log=LOG,
            running=0,
            activity={"kind": "packages", "name": "Building daedalus", "done": 1, "total": 2, "unit": "packages"},
            elapsed=14,
            quiet=5,
        ),
    ),
    # One archive of a first run with no seed: its megabytes against its pinned size.
    "download": (
        "/progress",
        status(
            busy="start",
            stage="runtime",
            done=16_900_000,
            size=38_989_688,
            log=DOWNLOAD_LOG,
            running=0,
            activity={"kind": "download", "name": "git 2.55.0.5", "done": 16_900_000, "total": 38_989_688, "unit": "bytes"},
            elapsed=31,
            quiet=0,
        ),
    ),
    "status": (
        "/status",
        status(
            change={
                "pending": True,
                "commit": "9f3c2a1",
                "summary": "Answer the inbox before the digest, not after it.",
                "status": "",
                "detail": "",
            }
        ),
    ),
}


# ---- the launcher ------------------------------------------------------------------------------


def build() -> Path:
    """The binary these pictures are of: the shipped variant, built the way build.sh builds it."""
    if named := os.environ.get("LAUNCHER"):
        return Path(named)
    out = WORK / "daedalus-desktop"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{ROOT / 'desktop'}:/src",
            "-v", f"{WORK}:/out",
            "-w", "/src",
            "-u", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "GOENV=/tmp/goenv", "-e", "GOFLAGS=-mod=mod",
            "-e", "GOCACHE=/tmp/gocache", "-e", "GOMODCACHE=/tmp/gomod", "-e", "CGO_ENABLED=0",
            GO_IMAGE,
            "go", "build", "-tags", "nowebview", "-trimpath", "-o", "/out/daedalus-desktop", ".",
        ],
        check=True,
    )
    return out


def install() -> Path:
    """A data directory that looks like one and is not one."""
    data = WORK / "Daedalus" / "data"
    if data.exists():
        shutil.rmtree(data)
    (data / "daedalus").mkdir(parents=True)
    (data / "protocore-exp").mkdir(parents=True)
    (data / ".env").write_text(ENV_FILE)
    (data / "mode").write_text("native\n")
    return data


def answering(answer: dict):
    """The one answer this picture is of, in place of the launcher's own.

    Playwright hands a handler the route and the request both, so the answer is closed over rather
    than passed as a default argument — which the request would otherwise land in.
    """

    def handle(route, *_):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    return handle


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve(binary: Path, data: Path, port: int) -> subprocess.Popen:
    """The launcher, asking its questions and nothing else.

    `setup` is the one command that serves the pages without starting anything, which is what makes
    it safe to point at a directory of invented files. PATH is emptied so the launcher's attempt to
    open a browser on this machine finds nothing to open one with.
    """
    process = subprocess.Popen(
        [str(binary), "setup", "--data", str(data), "--port", str(port)],
        env={"PATH": "", "HOME": str(WORK)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("the launcher did not start")


# ---- what the language switch must not lose ------------------------------------------------------


def check_language_switch(browser, port: int, data: Path) -> None:
    """Switching languages on /setup keeps every answer, the mode radios included.

    The switch re-fetches the page in the other language and carries the typed values across, and
    the fresh page brings the machine's *suggested* mode back with it. A restore that skipped the
    radios therefore threw away the one answer on the page that decides how the agent runs — while
    the operator was reading the words and not the tiles.
    """
    (data / "lang").write_text("en\n")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{port}/setup", wait_until="load")
        suggested = page.eval_on_selector("input[name=mode]:checked", "el => el.value")
        other = "docker" if suggested == "native" else "native"
        # The radio itself is under the tile's artwork, which is what an operator clicks too.
        page.click(f"label.mode:has(input[name=mode][value={other}])")
        page.fill("input[name=deepseek]", "typed-not-a-key")
        page.check("#clear-deepseek", force=True)
        page.click(".langs button[data-lang=ru]")
        page.wait_for_function("() => document.body.dataset.lang === 'ru'")
        page.wait_for_timeout(200)
        after = page.eval_on_selector("input[name=mode]:checked", "el => el.value")
        key = page.eval_on_selector("input[name=deepseek]", "el => el.value")
        clear = page.eval_on_selector("#clear-deepseek", "el => el.checked")
        if after != other:
            raise SystemExit(f"the language switch lost the mode: chose {other}, kept {after}")
        if key != "typed-not-a-key":
            raise SystemExit("the language switch lost what was typed into a key field")
        if not clear:
            raise SystemExit("the language switch lost a checkbox")
        print(f"language switch keeps the answers (mode={after})")
    finally:
        context.close()


# ---- the pictures --------------------------------------------------------------------------------


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    binary = build()
    data = install()
    port = free_port()
    launcher = serve(binary, data, port)
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()
            for lang in LANGS:
                # The language is a file beside the data, which is exactly how an operator's choice
                # in the corner of the page is kept.
                (data / "lang").write_text(lang + "\n")
                for width in WIDTHS:
                    context = browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=2)
                    page = context.new_page()
                    for name, (path, answer) in SHOTS.items():
                        page.route("**/api/status", answering(answer))
                        # Not networkidle: two of the three pages poll for as long as they are
                        # open, and a page that never goes idle never answers that wait.
                        page.goto(f"http://127.0.0.1:{port}{path}", wait_until="load")
                        page.wait_for_timeout(1400)
                        target = OUT / f"launcher-{name}-{lang}-{width}.png"
                        page.screenshot(path=str(target), full_page=True)
                        print(target)
                    context.close()
            check_language_switch(browser, port, data)
            browser.close()
    finally:
        launcher.terminate()
        launcher.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
