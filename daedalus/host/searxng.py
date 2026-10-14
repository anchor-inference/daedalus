"""Installing SearXNG into the installation's own folder, and finding it again once the supervisor runs it.

The other half of ``launcher/searxng.py``. That module is the supervisor's: it runs whatever is
installed. This one is the agent's: it puts SearXNG in place, takes it away, says what state it is in
for the Components page, and tells WebSearch where it answers. The folder layout, the pinned revision
and the two files the processes agree through are defined once, over there, and loaded from here —
two copies of a file name are how an install ends up written where the runner never looks.

What an install does, in the order it does it, and why each step is where it is:

1. Download the pinned source archive and refuse it unless it hashes to the pinned digest. SearXNG
   is not on PyPI; a commit of its repository is the release, and the archive of that commit is the
   one thing here that can be checked before it is run.
2. Unpack ``searx/`` and the requirements beside them, and freeze the version. SearXNG reads its
   version from ``git`` when it is not frozen, and an archive is not a checkout — on a machine with no
   git on the PATH that read is an exception at import rather than a missing version string.
3. Make an environment of its own with the ``uv`` the installation already ships, and install the
   pinned requirements into it. Its own, because SearXNG pins Flask, lxml and httpx-era libraries to
   versions the agent's environment has no reason to agree with, and a ``uv sync`` of the agent
   would otherwise take them away. Every requirement has wheels for Linux, macOS and Windows on
   x86-64 and ARM; nothing is compiled on the operator's machine.
4. Write the marker last. Until it is there the supervisor ignores the folder, so an install that
   dies half-way is never started.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import logging
import shutil
import socket
import sys
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import httpx

from daedalus.config import WebSearchConfig

logger = logging.getLogger(__name__)

CONTAINER_URL = "http://searxng:8080"
"""The address the compose stack's SearXNG answers at, which is also the configuration's default. A
configured URL equal to this is the operator not having chosen one, and only then does a SearXNG this
installation runs itself take its place."""

DOWNLOAD_TIMEOUT = 600.0
INSTALL_TIMEOUT = 900.0
"""How long ``uv pip install`` may take: about forty wheels over a slow link, nothing compiled."""
START_WAIT = 150.0
"""How long the install waits for the supervisor to bring the new instance up before it reports
success anyway. Longer than the runner's own start timeout, so a slow first start is not called a
failure here while the runner is still patiently waiting for it."""
SUPERVISOR_SILENCE = 20.0
"""How long without the supervisor picking the install up before the install stops waiting: an agent
started without its supervisor (a developer's ``python -m daedalus serve``) has nobody to run it."""
REMOVE_WAIT = 20.0

_LAUNCHER_MODULE = Path(__file__).resolve().parents[2] / "launcher" / "searxng.py"


def _load_runtime() -> ModuleType:
    spec = importlib.util.spec_from_file_location("daedalus_searxng_runtime", _LAUNCHER_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("daedalus_searxng_runtime", module)
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()
"""``launcher/searxng.py``: the layout, the pin, and the runner the supervisor uses."""


def root_for(state_dir: Path, runtime_dir: Path | None) -> Path:
    return runtime.root_for(state_dir, runtime_dir)


# -- what the Components page and WebSearch read ------------------------------------------


def _answers(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def service(root: Path, *, connect: Callable[[str, int, float], bool] | None = None) -> dict[str, Any] | None:
    """The state of the SearXNG installed in ``root``, or None when none is installed.

    The endpoint file is the supervisor's word, and a supervisor that was killed outright leaves its
    last word behind: an instance recorded as running that does not accept a connection is reported
    as stopped rather than believed.
    """
    if runtime.installed(root) is None:
        return None
    endpoint = runtime.read_endpoint(root)
    state = str(endpoint.get("state") or "stopped")
    url = str(endpoint.get("url") or "")
    if state == "running" and url:
        parsed = urlparse(url)
        if not (connect or _answers)(parsed.hostname or "127.0.0.1", parsed.port or 80, 0.3):
            state = "stopped"
    return {
        "state": state,
        "url": url if state in ("running", "starting") else "",
        "error": str(endpoint.get("error") or "") if state == "failed" else "",
        "retry_in": int(endpoint.get("retry_in") or 0) if state == "failed" else 0,
        "version": runtime.VERSION,
    }


def local_url(root: Path) -> str:
    """Where this installation's own SearXNG answers right now, or an empty string.

    Read afresh on every search: the supervisor may have moved it to another port at its last start,
    and a cached address is the one thing guaranteed to be wrong after that.
    """
    if runtime.installed(root) is None:
        return ""
    endpoint = runtime.read_endpoint(root)
    return str(endpoint.get("url") or "") if endpoint.get("state") == "running" else ""


def stack_answers(url: str, *, connect: Callable[[str, int, float], bool] | None = None) -> bool:
    """Whether the compose stack's SearXNG is up — the container's other way of having one."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    return (connect or _answers)(parsed.hostname, parsed.port or 80, 0.3)


def is_default_url(url: str) -> bool:
    return url.strip().rstrip("/") == CONTAINER_URL


def adopted(search: WebSearchConfig, *, installed: bool) -> WebSearchConfig | None:
    """The search configuration with SearXNG made the backend, or given back — None when nothing changes.

    Installing SearXNG is asking for it to answer. The default backend is DuckDuckGo, with SearXNG
    as the fallback, because the default must be the one that needs nothing installed; once SearXNG
    is installed that reasoning is spent, and a component that installs and then only answers when
    DuckDuckGo fails is not "working immediately". So an install swaps the two — but only when both
    are still the defaults and the URL is still the default. A backend or an address the operator
    chose is theirs, and an install does not overrule it. A removal undoes exactly that swap.
    """
    if not is_default_url(search.searxng.url):
        return None
    if installed and search.backend == "duckduckgo" and search.fallback == ["searxng"]:
        return search.model_copy(update={"backend": "searxng", "fallback": ["duckduckgo"]})
    if not installed and search.backend == "searxng" and search.fallback == ["duckduckgo"]:
        return search.model_copy(update={"backend": "duckduckgo", "fallback": ["searxng"]})
    return None


# -- putting it in place ----------------------------------------------------------------


class InstallFailed(RuntimeError):
    """An install step could not be done. The message is the sentence the page shows."""


def frozen_version() -> str:
    """``searx/version_frozen.py``: what ``python searx/version.py freeze`` writes in a checkout."""
    commit = runtime.COMMIT
    return (
        "# Written by Daedalus at install: an archive has no git history to read the version from.\n"
        f'VERSION_STRING = "{runtime.VERSION}"\n'
        f'VERSION_TAG = "{runtime.VERSION}"\n'
        f'DOCKER_TAG = "{runtime.VERSION.replace("+", "-")}"\n'
        'GIT_URL = "https://github.com/searxng/searxng"\n'
        f'GIT_BRANCH = "{commit}"\n'
    )


def unpack(archive: Path, destination: Path) -> None:
    """Unpack the parts SearXNG runs from — ``searx/`` and its requirements — and nothing else.

    The archive's top folder is the repository name and the commit; it is dropped, so the layout does
    not change with the pin. The ``data`` filter refuses absolute paths, links out of the tree and
    device files: this is an archive from the network, checked by its digest, and it is still not
    allowed to write outside the folder it is unpacked into.
    """
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        for member in tar.getmembers():
            _top, _, rest = member.name.partition("/")
            if not (rest == "requirements.txt" or rest.startswith("searx/")):
                continue
            member.name = rest
            members.append(member)
        if not any(member.name == "requirements.txt" for member in members):
            raise InstallFailed("the SearXNG archive carries no requirements.txt; it is not the archive that was pinned")
        tar.extractall(destination, members=members, filter="data")
    (destination / "searx" / "version_frozen.py").write_text(frozen_version(), encoding="utf-8")


class Install:
    """One install of SearXNG into ``root``, reporting each step through ``report``.

    ``process`` is the child running now, so the installer can kill it on a cancel; ``client`` and
    ``run`` are what the tests replace — the real ones reach the network and start ``uv``.
    """

    def __init__(self, root: Path, template: Path, report: Callable[[str], None], *,
                 client: Callable[[], httpx.AsyncClient] | None = None) -> None:
        self.root = root
        self.template = template
        self.report = report
        self.client = client or (lambda: httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0)))
        self.process: asyncio.subprocess.Process | None = None

    async def run(self) -> str:
        """The whole install. Returns the loopback URL the instance answers at, or an empty string
        when nothing has started it yet (no supervisor), which is installed but not running."""
        self.root.mkdir(parents=True, exist_ok=True)
        # A reinstall over a working instance: the marker goes first, so the supervisor stops it
        # before its interpreter is replaced underneath it.
        (self.root / runtime.MARKER).unlink(missing_ok=True)
        archive = self.root / "searxng.tar.gz"
        await self.download(archive)
        self.report("unpacking")
        await asyncio.to_thread(unpack, archive, self.root / runtime.SOURCE)
        archive.unlink(missing_ok=True)
        await self.environment()
        runtime.write_settings(self.root, self.template)
        runtime.write_json(self.root / runtime.MARKER, {"version": runtime.VERSION, "commit": runtime.COMMIT, "installed_at": time.time()})
        return await self.wait_running()

    async def download(self, archive: Path) -> None:
        self.report("downloading SearXNG " + runtime.VERSION)
        digest = hashlib.sha256()
        received = 0
        last = 0.0
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT), self.client() as http, http.stream("GET", runtime.ARCHIVE_URL) as response:
                if response.status_code != 200:
                    raise InstallFailed(f"the SearXNG archive could not be downloaded: HTTP {response.status_code}")
                with archive.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if time.monotonic() - last > 0.5:
                            last = time.monotonic()
                            self.report(f"downloading SearXNG {runtime.VERSION}: {received / 1048576:.1f} MB")
        except httpx.HTTPError as exc:
            raise InstallFailed(f"the SearXNG archive could not be downloaded: {type(exc).__name__}: {exc}") from None
        except TimeoutError:
            raise InstallFailed(f"the SearXNG archive took longer than {int(DOWNLOAD_TIMEOUT)} s to download") from None
        if digest.hexdigest() != runtime.ARCHIVE_SHA256:
            archive.unlink(missing_ok=True)
            raise InstallFailed(f"the SearXNG archive does not match the pinned digest (got {digest.hexdigest()[:16]}…); it was not installed")

    def commands(self) -> list[list[str]]:
        """The environment, then the requirements into it — with ``uv`` when the installation has one.

        ``uv`` ships with every native installation and with the image; the plain ``venv`` and ``pip``
        route is for an agent started by hand from an environment that has neither on the PATH.
        """
        venv = self.root / runtime.VENV
        python = str(runtime.venv_python(self.root))
        requirements = str(self.root / runtime.SOURCE / "requirements.txt")
        uv = shutil.which("uv")
        if uv:
            return [
                [uv, "venv", "--quiet", "--allow-existing", "--python", sys.executable, str(venv)],
                [uv, "pip", "install", "--python", python, "--requirements", requirements],
            ]
        return [
            [sys.executable, "-m", "venv", str(venv)],
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--requirement", requirements],
        ]

    async def environment(self) -> None:
        for argv in self.commands():
            await self.step(argv)
        # The modules with compiled parts, imported once in the new environment. A platform with no
        # wheel for one of them fails here, with its name, rather than at the supervisor's first start.
        await self.step([str(runtime.venv_python(self.root)), "-I", "-c", "import lxml.etree, msgspec, curl_cffi, yaml, babel, flask"])

    async def step(self, argv: list[str]) -> None:
        self.report(" ".join(Path(argv[0]).name if index == 0 else part for index, part in enumerate(argv[:3])))
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.process = process
        tail: list[str] = []
        assert process.stdout is not None
        try:
            async with asyncio.timeout(INSTALL_TIMEOUT):
                async for raw in process.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line:
                        tail.append(line)
                        del tail[:-20]
                        self.report(line)
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise InstallFailed(f"installing SearXNG's requirements took longer than {int(INSTALL_TIMEOUT)} s and was stopped") from None
        finally:
            self.process = None
        if process.returncode:
            raise InstallFailed(f"SearXNG could not be installed: {' / '.join(tail[-4:])[:400]}")

    async def wait_running(self, *, start_wait: float = START_WAIT, silence: float = SUPERVISOR_SILENCE, interval: float = 1.0) -> str:
        """Watch the supervisor pick the install up, and come back with where it answers.

        A failed start is reported as the failure it is: the page would otherwise say installed with
        a toast to match, over an instance that is not there.
        """
        began = time.monotonic()
        since = time.time() - 1
        self.report("starting SearXNG")
        while time.monotonic() - began < start_wait:
            endpoint = runtime.read_endpoint(self.root)
            state = endpoint.get("state")
            # Only what the supervisor wrote after the marker counts: the file may still hold the
            # previous instance's last word, and a "failed" from before this install is not about it.
            fresh = float(endpoint.get("at") or 0) >= since
            if fresh and state == "running":
                return str(endpoint.get("url") or "")
            if fresh and state == "failed":
                raise InstallFailed(f"SearXNG was installed and did not start: {endpoint.get('error') or 'no reason given'}")
            if not fresh and time.monotonic() - began > silence:
                return ""
            await asyncio.sleep(interval)
        return ""


async def remove(root: Path, *, wait: float = REMOVE_WAIT, interval: float = 0.5) -> None:
    """Take the marker away, let the supervisor stop the instance, then delete the folder.

    In that order because Windows will not delete a file a running process holds open: the folder can
    only go once the interpreter running from it has.
    """
    (root / runtime.MARKER).unlink(missing_ok=True)
    began = time.monotonic()
    while time.monotonic() - began < wait:
        endpoint = runtime.read_endpoint(root)
        if endpoint.get("state") in (None, "stopped", "failed"):
            break
        await asyncio.sleep(interval)
    errors: list[str] = []
    await asyncio.to_thread(shutil.rmtree, root, onexc=lambda _func, path, exc: errors.append(f"{path}: {exc}"))
    if errors and root.exists():
        raise InstallFailed(f"SearXNG was stopped and its folder could not be deleted completely: {errors[0]}")


__all__ = [
    "CONTAINER_URL",
    "Install",
    "InstallFailed",
    "adopted",
    "is_default_url",
    "local_url",
    "remove",
    "root_for",
    "runtime",
    "service",
    "stack_answers",
    "unpack",
]
