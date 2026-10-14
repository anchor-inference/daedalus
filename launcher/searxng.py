"""SearXNG beside the agent, without a container: the files an install leaves, and the process run from them.

WebSearch answers best through a SearXNG instance, and the compose stack has one as a profile service.
A native installation had none at all, so its search walked straight to the fallback. This module is
the half of the answer that has to live in the supervisor: the agent installs SearXNG into a folder of
the installation's own (``daedalus.host.searxng``), and the supervisor runs it from there for as long
as it runs itself — started when the folder says it is installed, restarted with a backoff when it
dies, stopped when the supervisor stops or the folder stops saying so.

Why the supervisor and not the agent: the agent is the process that restarts. It is restarted for an
applied change, a rollback, a new component, and SearXNG has nothing to do with any of them; held by
the supervisor, a search instance outlives every one of those restarts and the agent finds it warm.
The supervisor is also the one process both installation shapes have — PID 1 in the container, the
launcher's child on a machine — so one runner serves both, which is what lets a Docker installation
that never enabled the ``search`` profile install the very same thing into its state volume.

The two processes agree through files rather than a channel, because either can be restarted while
the other runs and a file is still there afterwards:

``installed.json``  written last by the install, deleted first by a removal. Its presence is the whole
                    of "should SearXNG run", and the supervisor polls for it.
``endpoint.json``   written by the supervisor: the state (``starting``, ``running``, ``failed``,
                    ``stopped``), the loopback URL, the pid, and the reason for a failure. The agent
                    reads it to find the instance and to put its state on the Components page.

Standard library only: the supervisor imports nothing the bot's environment would have to provide.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

COMMIT = "3fdc6d753a339b5f4a7dc5842c94c0d8324726f1"
"""The SearXNG revision a native install runs: the one the compose file's image tag was built from
(``searxng/searxng:2026.9.8-3fdc6d753``), so both shapes search with the same engines and the same
fixes. SearXNG publishes no releases to PyPI; a commit of its repository is its version."""
VERSION = "2026.9.8+3fdc6d753"
ARCHIVE_URL = f"https://github.com/searxng/searxng/archive/{COMMIT}.tar.gz"
ARCHIVE_SHA256 = "99656d7b2b72b97c716b0d43f83536183dc8cec94aeb6dbda604c5d4f0b672e0"
"""What that archive hashed to when it was pinned. An archive that hashes to anything else is refused
rather than run: it is code this installation will execute with network access."""

MARKER = "installed.json"
ENDPOINT = "endpoint.json"
SECRET = "secret_key"
SETTINGS = "settings.yml"
SERVE = "serve.py"
LOG = "searxng.log"
SOURCE = "src"
VENV = "venv"
CACHE = "cache"

DEFAULT_PORT = 8768
"""Tried first on the first start, and afterwards whatever port the last start had: a stable address
is one the operator can open in a browser twice. Anything taken is passed over for a free one. Beside
the installation's own ports (the API on 8765, the supervisor on 8769, the launcher on 8770) rather
than SearXNG's usual 8888, which a notebook server on the same machine is likely to want first."""
HEALTHY_SECONDS = 60.0
"""How long an instance has to stay up before its next death counts as a first one again."""
START_TIMEOUT = 120.0
"""How long a start may take to answer ``/healthz``. SearXNG loads every engine module and the
language tables before it listens, which on a slow disk is tens of seconds."""
POLL_SECONDS = 3.0
"""How often the supervisor looks for an install, a removal, or a dead instance."""
BACKOFF_FIRST = 2.0
BACKOFF_MAX = 120.0
LOG_LIMIT = 2 * 1024 * 1024
"""The log is cut back to nothing at a start once it is larger than this. An instance that logs every
request for months would otherwise fill the installation's folder with lines nobody reads."""
ENV_PASSED = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "USERPROFILE", "LANG", "LC_ALL",
              "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE")
"""What SearXNG inherits from the supervisor. An allow-list rather than the whole environment: the
supervisor carries the Telegram token, the GitHub tokens and the spend cap, and a metasearch engine
has no use for any of them. ``SYSTEMROOT`` is on it because Windows cannot open a socket without it."""


def root_for(state: Path, runtime: Path | None) -> Path:
    """Where SearXNG lives: the installation's runtime folder natively, the state volume in a container.

    The runtime folder is where Node and the browser go on a machine — what the installation fetched,
    as opposed to what it remembers. A container has no runtime folder of its own; its state volume is
    the one directory that survives a new image.
    """
    return (runtime if runtime is not None else state) / "searxng"


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / VENV / "Scripts" / "python.exe"
    return root / VENV / "bin" / "python"


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Replace the file whole. A reader that catches it half written would see no instance at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def installed(root: Path) -> dict[str, Any] | None:
    """The marker the install wrote last, when it is there and the interpreter it names is too."""
    marker = read_json(root / MARKER)
    if marker is None or not venv_python(root).is_file():
        return None
    return marker


def read_endpoint(root: Path) -> dict[str, Any]:
    return read_json(root / ENDPOINT) or {}


def secret_key(root: Path) -> str:
    """The instance's own secret, minted once and kept beside it.

    SearXNG refuses to start on the key its default settings ship with, and a key shared by every
    installation would sign every installation's cookies alike. Nothing outside this folder needs it.
    """
    path = root / SECRET
    with contextlib.suppress(OSError):
        if existing := path.read_text(encoding="utf-8").strip():
            return existing
    value = secrets.token_hex(32)
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return value


def render_settings(template: str, secret: str) -> str:
    """The compose stack's settings, with what a process on the operator's machine needs added to them.

    One settings file for both shapes, so an engine dropped from one is dropped from the other: the
    container gets the secret, the bind address and the limiter from its environment, and a native
    instance gets them written into its ``server`` section here. Loopback only and the limiter off,
    because the one client is the agent on the same machine and the limiter would cap a JSON client
    at a few requests an hour. ``search.formats`` already carries ``json`` there, which is the format
    the tool asks for; without it SearXNG answers 403.
    """
    added = (
        f'  secret_key: "{secret}"\n'
        '  bind_address: "127.0.0.1"\n'
        "  limiter: false\n"
    )
    lines = template.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip() == "server:":
            lines.insert(index + 1, added)
            return "".join(lines)
    raise ValueError("the SearXNG settings template has no top-level server section to add to")


def write_settings(root: Path, template: Path) -> Path:
    """Write ``settings.yml`` afresh from the template. Done on every start, so an update of the
    template reaches a native instance at its next start, the way a new compose file reaches the
    container's."""
    path = root / SETTINGS
    path.write_text(render_settings(template.read_text(encoding="utf-8"), secret_key(root)), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int) -> int:
    if preferred > 0 and port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


SERVE_SCRIPT = '''\
"""Written by Daedalus at every start of this SearXNG; edits here do not survive one."""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

if os.name == "nt":
    import types
    # searx.valkeydb imports pwd at the top for a message only a configured Valkey ever prints, and
    # Windows has no pwd module. No Valkey is configured here, so an empty module stands in for it.
    sys.modules.setdefault("pwd", types.ModuleType("pwd"))


def parent_alive(pid):
    if os.name != "nt":
        return os.getppid() == pid
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: it has exited
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def watch_parent(pid):
    # Started in a session of its own, so that a restart of the agent does not take it along. The
    # price is that a supervisor killed outright would leave it running; this is what ends it then.
    while True:
        time.sleep(2)
        if not parent_alive(pid):
            os._exit(0)


parent = int(os.environ.get("DAEDALUS_PARENT_PID") or 0)
if parent:
    threading.Thread(target=watch_parent, args=(parent,), daemon=True).start()

from searx.webapp import app  # noqa: E402  (after the path and the stand-in above)
from werkzeug.serving import run_simple  # noqa: E402

# Werkzeug's threaded server rather than granian: it is pure Python and therefore the same on all
# three systems, and one client on the loopback interface is not the load a production server is for.
run_simple(os.environ["SEARXNG_BIND_ADDRESS"], int(os.environ["SEARXNG_PORT"]), app, threaded=True)
'''


def child_env(root: Path, port: int) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ENV_PASSED}
    cache = root / CACHE
    cache.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "SEARXNG_SETTINGS_PATH": str(root / SETTINGS),
            "SEARXNG_PORT": str(port),
            "SEARXNG_BIND_ADDRESS": "127.0.0.1",
            # SearXNG keeps its engine tokens and caches in the temporary directory. Pointed at a
            # folder of its own, they survive a restart — the first query after one then looks like
            # the hundredth, not like a new bot — and they are not left in a directory others share.
            "TMPDIR": str(cache),
            "TEMP": str(cache),
            "TMP": str(cache),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "DAEDALUS_PARENT_PID": str(os.getpid()),
        }
    )
    return env


def log_tail(root: Path, limit: int = 400) -> str:
    """The end of the instance's log, as the reason a start failed."""
    try:
        with (root / LOG).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[-3:])[-limit:]


def healthy(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=timeout) as response:  # noqa: S310 - a loopback URL this module built
            return response.status == 200
    except (OSError, ValueError):
        return False


class Runner:
    """One SearXNG, kept running while it is installed and the supervisor is.

    ``spawn`` and ``probe`` are what the tests replace: the real ones start the interpreter in the
    install folder and ask it for ``/healthz``.
    """

    def __init__(self, root: Path, template: Path, log: Callable[[str], None], *,
                 spawn: Callable[..., Any] | None = None, probe: Callable[[str], bool] | None = None,
                 poll: float = POLL_SECONDS, start_timeout: float = START_TIMEOUT) -> None:
        self.root = root
        self.template = template
        self.log = log
        self.spawn = spawn or self._spawn
        self.probe = probe or healthy
        self.poll = poll
        self.start_timeout = start_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.backoff = BACKOFF_FIRST
        self.stopping = False
        self.wake = asyncio.Event()

    @property
    def pid(self) -> int | None:
        process = self.process
        return process.pid if process is not None and process.returncode is None else None

    def _publish(self, state: str, **fields: Any) -> None:
        # Never into a folder that is not there: a removal deletes it, and an endpoint file written
        # afterwards would bring back an empty folder that reads as a broken install.
        if not self.root.is_dir():
            return
        with contextlib.suppress(OSError):
            write_json(self.root / ENDPOINT, {"state": state, "at": time.time(), "version": VERSION, **fields})

    async def _spawn(self, port: int) -> asyncio.subprocess.Process:
        (self.root / SERVE).write_text(SERVE_SCRIPT, encoding="utf-8")
        log_path = self.root / LOG
        with contextlib.suppress(OSError):
            if log_path.stat().st_size > LOG_LIMIT:
                log_path.unlink()
        with log_path.open("ab") as output:
            argv = [str(venv_python(self.root)), "-I", str(self.root / SERVE)]
            if os.name == "nt":
                return await asyncio.create_subprocess_exec(
                    *argv, cwd=str(self.root), env=child_env(self.root, port), stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
                )
            return await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.root), env=child_env(self.root, port), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )

    async def _sleep(self, seconds: float) -> None:
        """A pause the supervisor's stop can cut short."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)

    async def run(self) -> None:
        """Until the supervisor stops: start what is installed, watch it, and start it again."""
        while not self.stopping:
            if installed(self.root) is None:
                await self._end("stopped")
                await self._sleep(self.poll)
                continue
            started = time.monotonic()
            reason = await self._start_and_watch()
            if self.stopping:
                break
            if reason is None:
                continue  # removed while running; the top of the loop records it
            if time.monotonic() - started > HEALTHY_SECONDS:
                self.backoff = BACKOFF_FIRST
            self._publish("failed", error=reason, retry_in=round(self.backoff))
            self.log(f"searxng: {reason}; starting it again in {self.backoff:.0f}s")
            await self._sleep(self.backoff)
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        await self._end("stopped")

    async def _start_and_watch(self) -> str | None:
        """One life of the instance. Returns why it ended, or None when it was ended on purpose."""
        previous = int(read_endpoint(self.root).get("port") or 0)
        port = pick_port(previous or DEFAULT_PORT)
        url = f"http://127.0.0.1:{port}"
        try:
            write_settings(self.root, self.template)
            self.process = await self.spawn(port)
        except (OSError, ValueError) as exc:
            return f"could not start: {exc}"
        self._publish("starting", url=url, port=port, pid=self.process.pid)
        self.log(f"searxng starting pid={self.process.pid} on {url}")
        deadline = time.monotonic() + self.start_timeout
        running = False
        while not self.stopping:
            if self.process.returncode is not None:
                tail = log_tail(self.root)
                return f"exited with code {self.process.returncode}" + (f": {tail}" if tail else "")
            if installed(self.root) is None:
                await self._end("stopped")
                return None
            if not running:
                if await asyncio.to_thread(self.probe, url):
                    running = True
                    self._publish("running", url=url, port=port, pid=self.process.pid)
                    self.log(f"searxng running on {url}")
                elif time.monotonic() > deadline:
                    await self._kill()
                    return f"did not answer on {url} within {int(self.start_timeout)}s: {log_tail(self.root)}"
            waiter = asyncio.ensure_future(self.process.wait())
            await asyncio.wait({waiter}, timeout=0.5 if not running else self.poll)
            waiter.cancel()
        return None

    async def _kill(self) -> None:
        """End the instance, gently first where the platform has a gentle way."""
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            if os.name == "nt":
                # Started without a console of its own, so no console event reaches it, and it holds
                # nothing that needs flushing: terminating it outright is the stop it would have had.
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            await process.wait()

    async def _end(self, state: str) -> None:
        """Stop the instance, if there is one, and record what it is now — once, not every poll."""
        await self._kill()
        if read_endpoint(self.root).get("state") != state:
            self._publish(state, port=read_endpoint(self.root).get("port", 0))

    async def stop(self) -> None:
        self.stopping = True
        self.wake.set()
        await self._end("stopped")


__all__ = [
    "ARCHIVE_SHA256",
    "ARCHIVE_URL",
    "COMMIT",
    "ENDPOINT",
    "MARKER",
    "Runner",
    "VERSION",
    "installed",
    "pick_port",
    "read_endpoint",
    "render_settings",
    "root_for",
    "venv_python",
    "write_settings",
]
