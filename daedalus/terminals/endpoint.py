"""Where a terminal daemon is: its run directory, and what the directory says.

The host is told only the directory. The daemon writes two files into it — ``endpoint``, a unix
socket name relative to the directory or a loopback TCP port, and ``token``, new at every start —
and the endpoint only once it accepts connections, so an endpoint that exists leads to a ready daemon.
A relative socket name keeps a directory bind-mounted at another path meaning the same thing on both
sides of the mount.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daedalus.config import Settings

ENDPOINT_FILE = "endpoint"
TOKEN_FILE = "token"
UNAVAILABLE_FILE = "unavailable"


class EndpointMissing(Exception):
    """The daemon cannot be reached from what the directory holds; ``reason`` is a code for the app."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Endpoint:
    kind: str
    """``unix`` or ``tcp``"""
    path: Path | None
    host: str
    port: int
    token: bytes


def permission_detail(path: Path, exc: OSError, label: str = "terminal service") -> str:
    """Why this process may not open a daemon's file, in terms the operator can act on.

    The host daemon's directory belongs to the operator, mode 0700, and the agent's container reads it
    as root. Root in an ordinary container passes that check; root in a rootless Docker or under
    userns-remap is an unprivileged user on the host and does not, and nothing in the container can
    change that.
    """
    # Windows has no uid; there the access list of the run directory is the whole story.
    who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "this user"
    return (
        f"{path}: {exc.strerror or exc} — this process runs as {who} and may not open the {label}'s files; "
        "with rootless Docker or userns-remap, root in the container is not root on the host"
    )


def read_endpoint(run_dir: Path, *, label: str = "terminal service", lock: str = "ptyd.lock") -> Endpoint:
    """What to connect to and what to say first; raises ``EndpointMissing`` with the reason.

    ``label`` names the daemon in the reasons and ``lock`` is its lock file: the browser daemon keeps
    its run directory the same way under its own names."""
    if not run_dir.is_dir():
        raise EndpointMissing("not_installed", f"{run_dir} does not exist")
    try:
        text = (run_dir / ENDPOINT_FILE).read_text(encoding="utf-8").strip()
    except PermissionError as exc:
        raise EndpointMissing("permission_denied", permission_detail(run_dir / ENDPOINT_FILE, exc, label)) from None
    except FileNotFoundError:
        # An empty directory is what setup leaves whether or not the service was installed, so it
        # reads as not installed. One a daemon has used (its lock, its token) held a daemon that
        # stopped — it removes its endpoint first thing when it does.
        # A native launcher that has no daemon to run leaves its reason here instead.
        note = run_dir / UNAVAILABLE_FILE
        if note.is_file():
            with contextlib.suppress(OSError):
                raise EndpointMissing("not_installed", note.read_text(encoding="utf-8").strip()[:300] or f"the {label} is not available") from None
        # Either daemon keeps its run directory the same way, under its own lock's name.
        if any((run_dir / name).exists() for name in (TOKEN_FILE, lock, "ptyd.lock", "browserd.lock")):
            raise EndpointMissing("not_running", f"the {label} in {run_dir} is not running") from None
        raise EndpointMissing("not_installed", f"no {label} has run in {run_dir}{owned_by_root(run_dir)}") from None
    except OSError as exc:
        raise EndpointMissing("unreachable", f"{run_dir / ENDPOINT_FILE}: {exc}") from exc
    try:
        token = (run_dir / TOKEN_FILE).read_bytes().strip()
    except PermissionError as exc:
        raise EndpointMissing("permission_denied", permission_detail(run_dir / TOKEN_FILE, exc, label)) from None
    except OSError as exc:
        raise EndpointMissing("unreachable", f"{run_dir / TOKEN_FILE}: {exc}") from exc
    kind, _, rest = text.partition(":")
    if kind == "unix" and rest:
        path = Path(rest)
        return Endpoint("unix", path if path.is_absolute() else run_dir / path, "", 0, token)
    if kind == "tcp" and rest:
        host, _, port = rest.rpartition(":")
        if host in ("127.0.0.1", "localhost", "[::1]", "::1") and port.isdigit():
            return Endpoint("tcp", None, host.strip("[]"), int(port), token)
        # A daemon may only listen on this machine's loopback interface; anything else in the file is
        # not a daemon this host should hand its token to.
    raise EndpointMissing("unreachable", f"{run_dir / ENDPOINT_FILE} holds {text[:80]!r}, which is not an endpoint")


def absolute_on(env: str, path: str) -> bool:
    """Whether ``path`` is a full path on the machine a terminal environment runs on.

    A container is Linux and takes a POSIX path. The host is the operator's own machine, which on
    Windows names its folders ``C:\\Users\\...``: taking only a leading slash for absolute refused
    every host terminal the app opened there ("a working directory is an absolute path") and left
    the host's project folders out of the roots the daemon's side channels may read.
    """
    if path.startswith("/"):
        return True
    return env == "host" and PureWindowsPath(path).is_absolute()


def owned_by_root(run_dir: Path) -> str:
    """A note for an empty directory that root owns while this process is not root's only user.

    Docker creates a missing bind-mount source as root. A host daemon runs as the operator and
    cannot write its files into that directory, so installing it would fail until it is handed over.
    Inside the container the directory's owner is the one fact available; the fix is on the host.
    """
    if os.name == "nt":
        # Windows reports every file as owned by uid 0, so the note would always be wrong there; and
        # nothing there is a Docker bind-mount source for a host daemon.
        return ""
    try:
        owner = run_dir.stat().st_uid
    except OSError:
        return ""
    return " (the directory belongs to root, so a host terminal service running as you could not use it: hand it over with sudo chown)" if owner == 0 else ""


_hook_ports: dict[str, int] = {}
"""The hook listener port each daemon reported in ``daemon.info``, by run directory. Kept here rather
than on the client so the policy can ask without holding the terminals service."""


def remember_hook_port(run_dir: Path, port: int) -> None:
    if port > 0:
        _hook_ports[str(run_dir)] = port
    else:
        _hook_ports.pop(str(run_dir), None)


_state_dirs: dict[str, str] = {}
"""The state directory each daemon reported, by run directory. It holds every launch's overlay files
and dial sockets, and the journal of agent writes."""


def remember_state_dir(run_dir: Path, state_dir: str) -> None:
    # Absolute by the rules of the daemon's system: a Windows daemon reports "C:\\...", which a
    # startswith("/") test used to drop, leaving its launch overlays unsealed natively on Windows.
    if state_dir.startswith("/") or PureWindowsPath(state_dir).is_absolute():
        _state_dirs[str(run_dir)] = state_dir
    else:
        _state_dirs.pop(str(run_dir), None)


def sealed_state_dirs(settings: Settings) -> tuple[Path, ...]:
    """The daemons' state directories, sealed like the rest of the installation where they are on
    this machine's filesystem — natively. A launch's settings overlay and its dial sockets are there,
    and whatever reaches a dial socket speaks to a running CLI as its adapter does."""
    return tuple(Path(d) for run_dir in settings.sealed_everywhere if (d := _state_dirs.get(str(run_dir))))


def sealed_ports(settings: Settings) -> tuple[int, ...]:
    """The loopback ports of this installation's terminal daemons: a TCP endpoint and a hook listener.

    Only ports on the loopback interface this process shares matter. In a container the daemon of the
    ``container`` environment runs in its own network namespace and its ports are not ours; the host
    daemon's hook listener is on the operator's loopback, which a container cannot reach either. They
    are listed anyway, because natively every one of them is on the same interface as the agent.
    """
    ports: list[int] = []
    for run_dir in settings.sealed_everywhere:
        try:
            endpoint = read_endpoint(run_dir)
        except EndpointMissing:
            endpoint = None
        if endpoint is not None and endpoint.kind == "tcp":
            ports.append(endpoint.port)
        if port := _hook_ports.get(str(run_dir)):
            ports.append(port)
    return tuple(ports)


__all__ = ["Endpoint", "EndpointMissing", "owned_by_root", "permission_detail", "read_endpoint", "remember_hook_port", "remember_state_dir", "sealed_ports", "sealed_state_dirs"]
