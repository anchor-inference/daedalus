"""Asking the launcher to do the one thing only it can do.

Two pieces of a native installation are not Python packages and not files the app can put in place
for itself: Node, which lands in the installation's own runtime folder, and the headless browser,
which Playwright unpacks where the launcher told it to. Both are fetched by the launcher, because
the launcher owns that folder and decides what goes into it — the app is a process it started.

The launcher already has a loopback page with an action endpoint, so this does not invent a channel;
it uses that one. The handshake is the file the launcher writes when it takes ownership of the
installation, ``<data>/launcher.json``, mode 0600 and owned by the same account this process runs
as. It carries the port the page listens on and the token every action has to bear.

**What the token is and what it is not.** It authorises start, stop, update, apply and the runtime
extras — everything the launcher's own buttons do. It is not a credential of the operator's and it
never leaves the machine: the file is read here, the header is set on one request to 127.0.0.1, and
neither the value nor anything derived from it is logged, returned to a caller or put in an error.
An error from here names the port and the action, never the token.

**Why the launcher accepts this request at all.** Its rules refuse anything that changes state and
did not come from its own page: a ``Sec-Fetch-Site`` that is cross-site, an ``Origin`` that is not
the page's, a ``Host`` that is not the loopback address it is listening on, or a missing token. A
request from this module carries no ``Sec-Fetch-Site`` and no ``Origin`` — a browser writes those,
a client does not — aims at ``127.0.0.1:<port>``, and carries the token. So it passes without the
rules being loosened: what a hostile page still cannot do is read a 0600 file to learn the token,
and it could not set the custom header cross-site even if it had one.

**What keeps the agent away from this file, and what does not.** ``Settings.sealed_paths`` names it,
so the file tools refuse it and a shell command that spells the path out is refused with them. That
is hygiene, not containment, and it is worth saying plainly: the sealed set is matched against the
words of a command, so a path the command builds for itself — from ``Path.home()``, from ``$HOME``,
inside a Python one-liner — is not matched, and the loopback port the launcher listens on is not
restricted by the egress rules either. What the file is really protected by is its mode: 0600 and
owned by the account this process runs as. The agent asks the app for an install the way the
operator does, and the app is what holds the launcher's keys.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from daedalus.processes import pid_alive

logger = logging.getLogger(__name__)

HEADER = "X-Daedalus-Desktop"
"""The header the launcher's actions are authorised with. A custom header cannot be set on a
cross-site form post, which is the launcher's first line rather than ours."""

TIMEOUT = 10.0
"""An action answers 202 as soon as it is started — the work happens behind it — so this bounds the
handshake and nothing else."""

STATUS_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class Launcher:
    """A running launcher of this installation: where it listens, and what it answers to."""

    port: int
    token: str
    pid: int

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __repr__(self) -> str:  # pragma: no cover - trivial, but it is the point of the class
        # Never let a token reach a traceback, a log line or a test's failure output.
        return f"Launcher(port={self.port}, pid={self.pid}, token=<redacted>)"


class LauncherUnavailable(RuntimeError):
    """No launcher is holding this installation, so nothing here can be asked of one."""


class LauncherBusy(RuntimeError):
    """The launcher is already doing something else, so this action was never started.

    Told apart from every other refusal because it is the one worth trying again: the launcher does
    one thing at a time, and what it is doing now will end.
    """


def instance_path(state_dir: Path) -> Path:
    """Where the launcher writes its handover file: beside the state directory, in the data folder.

    The launcher's layout puts ``state/`` inside the folder it owns, and the file at the top of it.
    """
    return state_dir.parent / "launcher.json"


def read(state_dir: Path) -> Launcher | None:
    """The launcher holding this installation, or ``None`` when there is none.

    A launcher that was killed rather than stopped leaves the file behind, so the process it names is
    checked before the file is believed. Everything unreadable, unparseable or incomplete is the same
    answer: no launcher.
    """
    path = instance_path(state_dir)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    port, token, pid = body.get("port"), body.get("token"), body.get("pid")
    if not isinstance(port, int) or not isinstance(token, str) or not port or not token:
        return None
    pid = pid if isinstance(pid, int) else 0
    if pid > 0 and not _alive(pid):
        return None
    return Launcher(port=port, token=token, pid=pid)


def _alive(pid: int) -> bool:
    """Whether the process that wrote the file is still there. Signal 0 asks without sending one."""
    if os.name == "nt":
        # Signal 0 is CTRL_C_EVENT there: the probe failed for every launcher, so a running one
        # read as gone and the app could never ask it to install a component or restart.
        return pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another account's process on that pid. Not ours, so not our launcher.
        return False
    except OSError:
        return False
    return True


async def act(launcher: Launcher, action: str) -> str:
    """Start one of the launcher's actions; returns the id of the job it claimed for it.

    The launcher answers 202 and does the work behind it, which is why :func:`job` exists: the caller
    asks about that id until the launcher says the action is done or says why it is not. An empty id
    comes back from a launcher older than jobs, and the caller falls back to watching :func:`busy`.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(f"{launcher.base}/api/action/{action}", headers={HEADER: launcher.token}, content=b"")
        except httpx.HTTPError as exc:
            # The exception carries the URL, which is the port and the action; it does not carry the
            # header, and this message does not add it.
            raise LauncherUnavailable(f"the launcher on port {launcher.port} did not answer: {type(exc).__name__}") from None
    if response.status_code == 403:
        raise LauncherUnavailable("the launcher refused the request; it may have been restarted since this process started")
    if response.status_code == 404:
        raise LauncherUnavailable(f"this launcher has no {action!r} action; it is older than this build of the app")
    if response.status_code == 409:
        raise LauncherBusy(_sentence(response) or "the launcher is already doing something else")
    if response.status_code >= 400:
        raise LauncherUnavailable(f"the launcher answered {response.status_code} to {action!r}: {_sentence(response)}".rstrip(": "))
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("job") or "") if isinstance(body, dict) else ""


def _sentence(response: httpx.Response) -> str:
    """The launcher's own words for a refusal, trimmed. Its error bodies are one line of plain text."""
    return response.text.strip().splitlines()[0][:200] if response.text.strip() else ""


async def job(launcher: Launcher, job_id: str) -> dict[str, str]:
    """What became of one action: ``state`` is ``running``, ``done`` or ``failed``, with ``error``.

    An empty mapping means the launcher does not know this id — it was restarted, or the id is older
    than the handful it keeps. A read, so it carries no token, and it names nothing but the action.
    """
    async with httpx.AsyncClient(timeout=STATUS_TIMEOUT) as client:
        try:
            response = await client.get(f"{launcher.base}/api/jobs/{job_id}")
        except httpx.HTTPError as exc:
            raise LauncherUnavailable(f"the launcher on port {launcher.port} stopped answering: {type(exc).__name__}") from None
    if response.status_code == 404:
        return {}
    try:
        body = response.json()
    except ValueError:
        return {}
    return {"state": str(body.get("state") or ""), "error": str(body.get("error") or "")} if isinstance(body, dict) else {}


async def status(launcher: Launcher) -> dict[str, object]:
    """What the launcher is doing: ``busy`` while an action runs, ``failure`` after one went wrong.

    A read, so it needs no token — the launcher's rules gate what changes something, not what reports.
    """
    async with httpx.AsyncClient(timeout=STATUS_TIMEOUT) as client:
        try:
            response = await client.get(f"{launcher.base}/api/status")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LauncherUnavailable(f"the launcher on port {launcher.port} stopped answering: {type(exc).__name__}") from None
    return body if isinstance(body, dict) else {}


async def busy(launcher: Launcher) -> tuple[str, str]:
    """``(what it is doing, what last failed)`` — the two fields a watcher needs, as strings."""
    body = await status(launcher)
    return str(body.get("busy") or ""), str(body.get("failure") or "")


__all__ = ["HEADER", "Launcher", "LauncherBusy", "LauncherUnavailable", "act", "busy", "instance_path", "job", "read", "status"]
