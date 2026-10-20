"""The desktop: the host's side of the desktop daemon.

``deskd`` is a child of the desktop launcher in a native installation and owns the operator's screens
and input (``docs/architecture/computer.md``). This package speaks its protocol over the terminal
daemon's framing, converts each model family's way of naming a point into the screenshot's pixels,
explains the daemon's refusals in plain words, and serves the agent's ``Computer`` tools. The operator's
grant of control never comes from here: only the launcher holds the token that can grant a lease.
"""
