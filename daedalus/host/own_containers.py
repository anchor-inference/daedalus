"""The rule that the agent does not step into this installation's own containers from outside them.

A session running on the host once read the app container's process id from ``ps``, entered its
namespaces with ``sudo nsenter``, mounted a new ``/proc`` inside it and copied the operator's adb key
into it; another ran ``docker exec`` into the app container. Both reached the app's state, its keys and
its database by a door no other rule watched, since the app container is not a path the policy knows
from the host's side. This module recognises such a command; :mod:`daedalus.host.policy` refuses it.

What it cannot see: a script or a compose file that does the same, a renamed binary, a process id the
command found some other way than naming it, and a container of this stack under a name the operator
gave it by hand. It is a guard against the agent's own reach, not a wall.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Sequence

REFUSAL = (
    "Refused: {what} would enter or change one of this installation's own containers from outside. It is the "
    "operator's rule (host.own_containers): the app's containers hold its state, keys and database, and the agent "
    "does not reach into them from the host. Do not try another way in (nsenter, docker exec or cp, a mount, a "
    "copied binary); that is the same refusal. If the work needs something inside the app, say what, and ask the "
    "operator with AskUser."
)
"""What the agent is told: the owner of the rule and what to do instead, as the tunnel rule does."""

CONTAINER_TOOLS = frozenset({"docker", "podman", "nerdctl"})
ENTERING = frozenset({"exec", "cp", "attach", "commit", "export", "update", "kill", "stop", "restart", "pause", "rm", "rename"})
"""Subcommands that act on a named container: going into it, copying in or out, or changing what runs."""
JOINING = re.compile(r"^--(?:pid|network|net|ipc|uts|volumes-from)(?:=|$)")
"""A new container that joins one of ours: ``--pid=container:…`` shares its processes, ``--volumes-from`` its files."""


def project() -> str:
    """The compose project this installation runs as; container names begin with it."""
    return os.environ.get("DAEDALUS_COMPOSE_PROJECT", "").strip() or "deploy"


def own_id() -> str:
    """This container's own short id, which Docker gives it as its host name; empty outside a container."""
    if not os.path.exists("/.dockerenv"):
        return ""
    return socket.gethostname()


def is_ours(name: str, *, stack: str | None = None, ident: str | None = None) -> bool:
    """Whether ``name`` names a container of this stack: ``deploy-daedalus-1``, ``deploy_keyproxy_1``, or our id."""
    name = name.strip().removeprefix("container:").split(":", 1)[0]
    stack = project() if stack is None else stack
    ident = own_id() if ident is None else ident
    if not name:
        return False
    if re.fullmatch(rf"{re.escape(stack)}[-_][\w.-]+", name):
        return True
    return bool(ident) and len(name) >= 6 and (ident.startswith(name) or name.startswith(ident))


def entry_in(words: Sequence[str], *, stack: str | None = None, ident: str | None = None) -> str | None:
    """What this simple command does to one of our containers, in a few words, or ``None``.

    ``words`` is one command's argv with wrappers (``sudo``, ``env``, ``nohup``) already taken off.
    """
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    args = list(words[1:])
    if name == "nsenter":
        # The process id it is given was looked up on the host, where this policy cannot tell whose it is.
        # Entering any process's namespaces is not ordinary work, and the one time it happened it was ours.
        if any(a in ("-t", "--target") or a.startswith(("--target=", "-t")) for a in args):
            return "nsenter into another process's namespaces"
        return None
    if name not in CONTAINER_TOOLS:
        return None
    operands = [a for a in args if not a.startswith("-")]
    if "compose" in operands[:1]:
        sub = next((a for a in operands[1:] if a in ENTERING or a == "run"), None)
        named = _option(args, "-p", "--project-name")
        if sub and named == (project() if stack is None else stack):
            return f"{name} compose {sub} in this installation's stack"
        return None
    if not operands:
        return None
    sub = operands[0]
    if sub == "container" and len(operands) > 1:
        sub, operands = operands[1], operands[1:]
    if sub in ENTERING:
        for target in operands[1:]:
            for part in target.split(":", 1)[:1]:
                if is_ours(part, stack=stack, ident=ident):
                    return f"{name} {sub} {part}"
        return None
    if sub in ("run", "create"):
        for i, a in enumerate(args):
            if JOINING.match(a):
                value = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
                if is_ours(value, stack=stack, ident=ident):
                    return f"{name} {sub} joined to {value}"
    return None


def _option(args: Sequence[str], short: str, long: str) -> str:
    for i, a in enumerate(args):
        if a in (short, long) and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
    return ""


__all__ = ["REFUSAL", "entry_in", "is_ours", "own_id", "project"]
