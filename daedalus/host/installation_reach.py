"""Whether a command the agent runs can read the installation's own secrets, for the screens that must say so.

Natively the agent is a process of the operator's user, and every file the installation keeps is the operator's:
the provider keys (``keyproxy.env``), the Telegram token in the launcher's environment file, the app's API token in
the state folder's database. The policy refuses a command that names them, by reading its words, and a command
that builds the path is not one it can read. Only the Linux sandbox puts nothing at those paths
(:func:`daedalus.tools.shell.hidden_installation`), so only there may a screen leave the warning out. On macOS and
Windows there is no such sandbox, and the same holds on Linux with the sandbox off or not working.

In a container the keys are in the key proxy's container and the launcher's file is not there at all.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from daedalus.tools.shell import bwrap_status


async def secrets_hidden_from_commands(app: Any) -> bool:
    """``True`` when a command the agent runs cannot reach the installation's keys and tokens."""
    if not app.settings.native:
        return True
    if not sys.platform.startswith("linux") or getattr(app.config.tools.exec, "sandbox", "off") != "workspace":
        return False
    return await asyncio.to_thread(bwrap_status) == "ok"


__all__ = ["secrets_hidden_from_commands"]
