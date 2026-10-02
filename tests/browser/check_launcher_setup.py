"""Walk the launcher's first-run wizard from the first step to the hand-off, in a real browser.

The real launcher binary serves the page (`daedalus-desktop setup`, which serves the pages and
starts nothing) over a data folder made here and thrown away. The model step talks to a real
endpoint — a tiny OpenAI-shaped server in this file, on the loopback address — through the
launcher's own test call; the CLI detection finds the one login this file leaves in the throwaway
home. Only the start is played back: the status the page follows is answered from this file, because
a real start brings a whole installation up.

Every combination of language and window (1200×800 and 390×844) must get through all eight steps
without a console error, a page error, a horizontal scroll or a frame that leaves the window. The
last walk posts its answers to the launcher for real, and the files it writes are read back.

    CHROMIUM=... python tests/browser/check_launcher_setup.py

The binary is built in the Go container as launcher_shots.py builds it, unless LAUNCHER names one.

MATRIX=small runs one combination, for iterating; NO_GL=1 runs the page without WebGL, which is
the still-picture fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from launcher_shots import build
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CHROMIUM = os.environ.get("CHROMIUM") or None
OUT = Path(os.environ.get("OUT", "/tmp/launcher-setup-shots"))
NO_GL = os.environ.get("NO_GL") == "1"
SIZES = [(1200, 800), (390, 844)]
LANGS = ["en", "ru"]
if os.environ.get("MATRIX") == "small":
    SIZES, LANGS = SIZES[:1], LANGS[:1]
elif os.environ.get("MATRIX") == "phone":
    SIZES, LANGS = SIZES[1:], LANGS[1:]
MODEL = "tiny-local-model"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Models(BaseHTTPRequestHandler):
    """An OpenAI-compatible server with one model and nothing else."""

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def serve_launcher(launcher: Path, work: Path, port: int) -> subprocess.Popen:
    home, data = work / "home", work / "data"
    (home / ".codex").mkdir(parents=True)
    # A login file the key proxy would read: with no codex on PATH, this is what detection finds.
    (home / ".codex" / "auth.json").write_text('{"tokens": "not-a-token"}')
    data.mkdir()
    process = subprocess.Popen(
        [str(launcher), "setup", "--data", str(data), "--port", str(port)],
        env={"PATH": "", "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("the launcher did not start")


def status_answers():
    """The start as the page sees it: the runtime, then the agent, then up."""
    frames = [
        {"busy": "start", "stage": "runtime", "steps": ["runtime", "checkouts", "environment", "start"], "log": ["uv comes with the installation"]},
        {"busy": "start", "stage": "environment", "steps": ["runtime", "checkouts", "environment", "start"], "log": ["building the environment"]},
        {"busy": "start", "stage": "start", "steps": ["runtime", "checkouts", "environment", "start"], "log": ["waiting for the app"]},
        {"busy": "", "stage": "", "steps": ["runtime", "checkouts", "environment", "start"], "running": 3, "log": ["the app answers"]},
    ]
    count = {"n": 0}

    def answer(route) -> None:
        frame = frames[min(count["n"] // 2, len(frames) - 1)]
        count["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(frame))

    return answer


def settled(page) -> None:
    """Waits for the stage and the wizard to be still: the page's own flag, never a fixed sleep."""
    if NO_GL:
        page.wait_for_function("() => window.Wizard && !window.Wizard.busy()", timeout=20000)
        return
    page.wait_for_function("() => document.body.dataset.settled === '1'", timeout=30000)


def check_fit(page, where: str, problems: list[str]) -> None:
    fit = page.evaluate(
        """() => {
        const W = innerWidth, H = innerHeight, f = document.querySelector('.frame').getBoundingClientRect();
        return {scroll: document.documentElement.scrollWidth - W, left: f.left, right: f.right - W, top: f.top, bottom: f.bottom - H};
    }"""
    )
    if fit["scroll"] > 0:
        problems.append(f"{where}: the page scrolls sideways by {fit['scroll']}px")
    if fit["left"] < -0.5 or fit["right"] > 0.5 or fit["top"] < -0.5 or fit["bottom"] > 0.5:
        problems.append(f"{where}: the frame leaves the window {fit}")


def walk(browser, base: str, lang: str, size: tuple[int, int], endpoint: str, real_post: bool) -> list[str]:
    problems: list[str] = []
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, locale="ru-RU" if lang == "ru" else "en-US")
    page = context.new_page()
    page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    page.route("**/api/status", status_answers())
    page.route("**/api/action/open", lambda r: r.fulfill(status=202, content_type="application/json", body='{"started":true}'))
    page.route("**/api/lang", lambda r: r.fulfill(status=200, content_type="application/json", body='{"saved":true}'))
    if not real_post:
        page.route("**/setup", lambda r: r.fulfill(status=200, content_type="application/json", body='{"saved":true}') if r.request.method == "POST" else r.continue_())
    tag = f"{lang}-{size[0]}"
    try:
        page.goto(base + ("/setup?motion=0" if NO_GL else "/setup"), wait_until="load")
        settled(page)
        if page.evaluate("document.documentElement.classList.contains('no-gl')") != NO_GL:
            problems.append(f"{tag}: the stage decision is not the expected one (no-gl wanted {NO_GL})")
        if page.get_attribute(f"[data-act=lang][data-lang={lang}]", "aria-pressed") != "true":
            page.click(f"[data-act=lang][data-lang={lang}]")
        shots = 0

        def step(name: str) -> None:
            nonlocal shots
            # The click returns before the next frame runs, when the flag still says the last step
            # is still: the new step's id has to be on the wizard first.
            wanted = name.split("-")[0]
            page.wait_for_function("(id) => document.querySelector('[data-wizard]').dataset.step === id", arg=wanted, timeout=10000)
            # The flag is written at the start of a frame, so two frames pass before it is read.
            page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
            settled(page)
            check_fit(page, f"{tag} {name}", problems)
            shots += 1
            page.screenshot(path=str(OUT / f"setup-{tag}-{shots:02d}-{name}.png"))

        def next_step() -> None:
            page.click(".f-foot [data-act=next]")

        step("welcome")
        next_step()
        step("runs")
        page.click("label.opt[data-k=mode-native]")
        badge = page.text_content("[data-k=docker-badge]") or ""
        if not badge.strip():
            problems.append(f"{tag}: the Docker badge is empty")
        next_step()
        step("model")
        page.click("[data-k=kind-cli]")
        settled(page)
        signed = page.evaluate("() => Array.from(document.querySelectorAll('label.cli')).map(l => [l.dataset.k, !l.classList.contains('missing')])")
        if dict(signed).get("cli-codex") is not True or dict(signed).get("cli-grok") is not False:
            problems.append(f"{tag}: CLI detection is not what the home holds: {signed}")
        page.click("[data-k=kind-local]")
        settled(page)
        page.fill("#f-local\\.url", endpoint)
        page.click("[data-act=test]")
        page.wait_for_function("() => window.Wizard.state.local.test === 'ok' || window.Wizard.state.local.test === 'fail'", timeout=15000)
        state = page.evaluate("() => window.Wizard.state.local")
        if state["test"] != "ok" or state["model"] != MODEL:
            problems.append(f"{tag}: the endpoint test did not list the model: {state}")
        step("model-local")
        next_step()
        step("limit")
        page.click("[data-k=preset-50]")
        next_step()
        step("telegram")
        page.click(".f-foot [data-act=skip]")
        step("advanced")
        page.click("[data-act=adv]")
        settled(page)
        if page.get_attribute("#f-adv\\.data", "readonly") is None:
            problems.append(f"{tag}: the data folder is editable")
        step("advanced-open")
        next_step()
        step("summary")
        next_step()
        page.wait_for_function("() => window.Wizard.state.launch && window.Wizard.state.launch.done", timeout=30000)
        step("start")
        page.click("[data-act=open]")
        page.wait_for_selector(".handoff-card.on", timeout=10000)
        step("start-handoff")
    except Exception as exc:  # noqa: BLE001 - a broken walk is reported with the others
        problems.append(f"{tag}: the walk stopped: {exc}")
        page.screenshot(path=str(OUT / f"setup-{tag}-failed.png"))
    finally:
        context.close()
    return problems


def main() -> int:
    binary = build()
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="launcher-setup-"))
    models = ThreadingHTTPServer(("127.0.0.1", free_port()), Models)
    threading.Thread(target=models.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{models.server_address[1]}"
    port = free_port()
    launcher = serve_launcher(binary, work, port)
    problems: list[str] = []
    try:
        with sync_playwright() as play:
            args = ["--disable-gpu", "--disable-webgl", "--disable-3d-apis"] if NO_GL else ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"]
            browser = play.chromium.launch(executable_path=CHROMIUM, args=args)
            combos = [(lang, size) for lang in LANGS for size in SIZES]
            for i, (lang, size) in enumerate(combos):
                problems += walk(browser, f"http://127.0.0.1:{port}", lang, size, endpoint, real_post=i == len(combos) - 1)
            browser.close()
        # The last walk posted for real, and `setup` exits once the answers are written.
        try:
            launcher.wait(timeout=20)
        except subprocess.TimeoutExpired:
            problems.append("the launcher never received the last walk's answers")
        env = (work / "data" / ".env").read_text() if (work / "data" / ".env").exists() else ""
        keys_file = work / "data" / "daedalus-secrets" / "keyproxy.env"
        keys = keys_file.read_text() if keys_file.exists() else ""
        for want, text in ((f"DAEDALUS_LOCAL_MODEL={MODEL}", env), ("USD_PER_DAY=50", env), (f"KEYPROXY_UPSTREAM_LOCAL={endpoint}/v1", keys)):
            if want not in text:
                problems.append(f"the posted answers did not reach the files: {want!r} missing")
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=10)
        models.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    for problem in problems:
        print("FAIL", problem)
    print(f"{len(LANGS) * len(SIZES)} walks, {len(problems)} problems, pictures in {OUT}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
