"""What a door with no container keeps of the container's walls.

In compose the door has no network at all. On an operator's own machine it runs as the operator, and
where the platform offers nothing better the launcher starts it plain. These audit hooks are what is
left then: this process cannot start a program, and cannot dial anything but the addresses it was built
to dial. A bug that makes the door fetch an address a stranger chose stops here.

This is not a boundary against code running in this process, which could remove the reason for it
along with everything else; the container is one and this is not. It is here for the mistake, not for
the takeover, and it is what the door's notes in ``SECURITY.md`` mean by "a promise, not a wall".
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

REFUSED_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.startfile"})
LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})


class Confined(PermissionError):
    """Something in this process tried what the door never does."""


def is_loopback(address: Any) -> bool:
    return isinstance(address, tuple) and bool(address) and address[0] in LOOPBACK_NAMES


def hook(may_dial: Callable[[Any], bool]) -> Callable[[str, tuple[Any, ...]], None]:
    """The audit hook, apart from installing it, because an installed hook cannot be taken away from a test."""

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in REFUSED_EVENTS:
            raise Confined(f"the door starts no programs ({event})")
        if event == "socket.connect" and not may_dial(args[1]):
            raise Confined(f"the door does not dial {args[1]!r}")
        if event == "socket.getaddrinfo" and args[0] not in LOOPBACK_NAMES and args[0] is not None:
            # The door's own hosts are addresses. A name is how a mistake would start to reach outside.
            raise Confined(f"the door looks up no names ({args[0]!r})")

    return audit


def confine(may_dial: Callable[[Any], bool]) -> None:
    sys.addaudithook(hook(may_dial))


__all__ = ["Confined", "confine", "hook", "is_loopback"]
