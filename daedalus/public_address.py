"""Where this installation is reached from outside, as of this moment.

``Settings`` starts these fields from the environment, and this module is the only thing that changes
them afterwards, so a tunnel whose address is handed out at start can arrive without a restart: every
reader asks here, or reads the field at the moment it needs it, and none of them keeps a copy.

Two addresses, because they must not be one. The app's address holds the operator's signed-in session;
the services address is where pages an agent wrote run, and it has to be another origin (see
``daedalus.extensions.services_door``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from daedalus.config import Settings

DEFAULT_PORTS = {"http": 80, "https": 443}


def _base(url: str) -> str:
    return url.strip().rstrip("/").removesuffix("/app").rstrip("/")


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def app_base(settings: Settings) -> str:
    """The app's public address with no ``/app`` on it; empty when none is known."""
    return _base(settings.miniapp_public_url)


def services_base(settings: Settings) -> str:
    """The separate origin shared services are served from; empty when there is none to use.

    The operator's own name for it comes first. Without one, an app on plain http or on no public
    address at all stands on a machine whose ports are reached directly, and the door is simply the
    next port on the same host. An https app is behind a proxy, which only the operator can extend,
    and a guessed address there would hand out links that never connect.

    A services address equal to the app's is no separate origin at all, and is refused here rather
    than trusted: the caller then keeps pages in the sandbox instead of on the operator's origin.
    """
    port = settings.services_door_port
    if not port:
        return ""  # switched off, or the port could not be bound
    named = _base(settings.services_public_url)
    app = app_base(settings)
    if named:
        return "" if app and _origin(named) == _origin(app) else named
    host = settings.services_public_host.strip()
    if not host or app.lower().startswith("https://"):
        return ""
    return f"http://{host}:{port}"


def publish(settings: Settings, *, app: str | None = None, services: str | None = None) -> None:
    """Change an address now. ``None`` leaves it as it is and an empty string clears it.

    The value lives only as long as the process, which is right for what it is for: a tunnel hands
    out its address again each time it comes up.
    """
    if app is not None:
        settings.miniapp_public_url = app
    if services is not None:
        settings.services_public_url = services


@dataclass(frozen=True, slots=True)
class OwnDoors:
    """How this installation looks from the outside, for the rule that an agent does not call itself."""

    hosts: frozenset[str]
    """The names and addresses that arrive at this machine. A closed port is closed on these too."""
    app: tuple[str, int] | None
    """The host and port the app's API answers on from outside, whatever port that is."""


def own_doors(settings: Settings) -> OwnDoors:
    hosts = {settings.services_public_host.strip().lower()}
    app = None
    for address in (app_base(settings), services_base(settings)):
        parts = urlsplit(address)
        if parts.hostname:
            hosts.add(parts.hostname.lower())
    parts = urlsplit(app_base(settings))
    if parts.hostname:
        app = (parts.hostname.lower(), parts.port or DEFAULT_PORTS.get(parts.scheme.lower(), 0))
    return OwnDoors(frozenset(h for h in hosts if h), app)


__all__ = ["OwnDoors", "app_base", "own_doors", "publish", "services_base"]
