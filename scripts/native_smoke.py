"""Start the bot the way a native installation does, wait for it to answer, and stop it the same way.

Run from the checkout with its environment: ``uv run --frozen python scripts/native_smoke.py``.
It exits non-zero when the bot dies on the way up, does not answer on its port, does not echo the
boot id it was given, or does not end within the time a stop allows when asked the way the launcher
asks — SIGTERM on POSIX, CTRL_BREAK on Windows, where the bot is started in a process group of its
own exactly as the supervisor starts it. On Windows that is where both of the first native release's
failures were: an attribute of ``signal`` that Windows lacks ended every start before the first
line, and a stop that only uvicorn heard left the bot waiting until it was killed.

The state and the workspaces are a temporary folder; nothing of the machine's own is read or written.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START_SECONDS = 180.0
"""A cold start imports everything and opens a fresh database; a slow runner gets the whole of it."""
STOP_SECONDS = 20.0
"""Less than the 25 seconds the supervisor allows before it kills the bot: a stop that needs the kill fails here."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def answer(port: int) -> tuple[int, str] | None:
    """The status and boot header of the launcher's own health request, or None when nothing answers.

    Any status counts once the boot id is on it: a checkout without a built app answers /app/ with
    404, and what is being checked is that this process serves, not that the app was built."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app/", timeout=5) as response:  # noqa: S310 — loopback
            return response.status, response.headers.get("X-Daedalus-Boot", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("X-Daedalus-Boot", "")
    except OSError:
        return None


def main() -> int:
    windows = os.name == "nt"
    port = free_port()
    boot = secrets.token_hex(8)
    # A Windows file still open in a process the kill below did not reach must not turn a verdict
    # already printed into a traceback.
    with tempfile.TemporaryDirectory(prefix="daedalus-smoke-", ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        for name in ("state", "workspaces"):
            (root / name).mkdir()
        env = dict(os.environ)
        env.update({
            "DAEDALUS_NATIVE": "1",
            "DAEDALUS_BOOT_ID": boot,
            "STATE_DIR": str(root / "state"),
            "WORKSPACES_DIR": str(root / "workspaces"),
            "BOT_REPO_DIR": str(ROOT),
            "API_PORT": str(port),
            "TELEGRAM_BOT_TOKEN": "",
            "PYTHONUNBUFFERED": "1",
        })
        log_path = root / "bot.log"
        started = time.monotonic()
        with open(log_path, "wb") as log:
            flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if windows else {"start_new_session": True}  # type: ignore[attr-defined]
            bot = subprocess.Popen([sys.executable, "-m", "daedalus", "serve"], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)  # noqa: S603
        try:
            failure = wait_up(bot, port, boot, started)
            if failure:
                return report(failure, log_path)
            print(f"the app answered on {port} with its boot id after {time.monotonic() - started:.1f}s")
            # The app holds an event stream open for as long as it is looked at, and the stop has to
            # end the bot with one open: that is the case the first Windows release lost, where the
            # server waited for the stream to finish and the bot was killed instead.
            listening = hold_event_stream(port, root / "state" / "daedalus.sqlite")
            if not listening.wait(30):
                return report("the event stream did not open", log_path)
            stop_asked = time.monotonic()
            bot.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGTERM)  # type: ignore[attr-defined]
            try:
                code = bot.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                return report(f"the bot did not stop within {STOP_SECONDS:.0f}s of being asked", log_path)
            if code != 0:
                return report(f"the bot stopped with code {code}", log_path)
            print(f"the bot stopped cleanly {time.monotonic() - stop_asked:.1f}s after it was asked")
            return 0
        finally:
            if bot.poll() is None:
                end(bot)


def end(bot: subprocess.Popen[bytes]) -> None:
    """Kill the bot and what it started. On Windows a virtual environment's python.exe is a launcher
    that runs the real interpreter as its child, so killing only the process started here would
    leave the bot itself running with the database open."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(bot.pid)], capture_output=True, check=False)  # noqa: S603, S607
    else:
        os.killpg(bot.pid, signal.SIGKILL)
    bot.wait()


def hold_event_stream(port: int, database: Path) -> threading.Event:
    """Open the app's event stream in a thread and keep reading it; the event is set once it answers."""
    connected = threading.Event()
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        token = db.execute("SELECT value FROM kv WHERE key = 'api_token'").fetchone()[0]

    def read() -> None:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/events?kind=launcher&client=smoke", headers={"x-daedalus-token": str(token).strip('"')})
        try:
            with urllib.request.urlopen(request, timeout=60) as stream:  # noqa: S310 — loopback
                connected.set()
                while stream.read(1):
                    pass
        except OSError:
            pass

    threading.Thread(target=read, daemon=True).start()
    return connected


def wait_up(bot: subprocess.Popen[bytes], port: int, boot: str, started: float) -> str:
    last = "no answer"
    while time.monotonic() - started < START_SECONDS:
        if bot.poll() is not None:
            return f"the bot exited with code {bot.returncode} before it answered"
        got = answer(port)
        if got is not None:
            status, header = got
            if header == boot:
                return ""
            last = f"HTTP {status} with boot id {header!r}"
        time.sleep(0.5)
    return f"the app did not answer on {port} within {START_SECONDS:.0f}s ({last})"


def report(failure: str, log_path: Path) -> int:
    print(f"FAILED: {failure}")
    print("---- the bot's output ----")
    print(log_path.read_text(encoding="utf-8", errors="replace")[-20000:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
