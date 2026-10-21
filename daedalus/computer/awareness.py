"""What the agent is told about the operator's computer as a turn starts, and when a grant arrives.

In the operator's first session on Windows the agent had control and did not know it: the turn context
named only the browser, so it opened its own browser and ran commands with Exec, and loaded the
computer tools only after the operator insisted twice. Now a session that holds the lease is told so in
one line of the turn context — what it may drive, until when, how many actions are left — and starts
the run with the tools loaded; a conversation made for the computer that holds no lease is told that
control is not granted and who grants it.

The line rides in the turn context, never the system prompt: the time and the count change every turn,
and the system prompt is what every later call is cached on. The lease is read from the desktop daemon
over the agent's own connection (``lease.status``), the same read every computer tool starts with, so
the line and the tools never disagree; ``apps.list`` names the window or app of the scope and counts
as no action.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from daedalus.computer.wire import DeskClient, DeskError, DeskUnavailable
from daedalus.security.untrusted import injection_signs

logger = logging.getLogger(__name__)

TOOLS_WORDS = (
    "They expect on-screen work on their computer to be done with Computer and ComputerApps{loaded}, not with Exec "
    "or your own browser; your other tools stay available for work that needs them."
)
NOT_GRANTED = (
    "- Computer: this conversation is for work on the operator's own computer, but they have not given you "
    "control of it now. When the task needs their screen, ask them to grant it with the Computer control in this "
    "chat; you cannot grant it yourself."
)
NO_IMAGES = (
    " This conversation's model does not take images, so it cannot see the screen: tell the operator to switch "
    "to a model with images for this work."
)
TITLE_CHARS = 80


async def held_lease(client: DeskClient, session_id: str) -> dict[str, Any] | None:
    """The lease this session holds now, with ``remaining_ms`` beside it, or ``None``.

    A daemon that cannot be reached holds nothing for anyone; it is not an error at the start of a turn."""
    try:
        status = await client.call("lease.status", {})
    except (DeskError, DeskUnavailable) as exc:
        logger.debug("no lease read for the turn context: %s", exc)
        return None
    lease = (status or {}).get("lease") if isinstance(status, dict) else None
    if not isinstance(lease, dict) or not lease.get("active"):
        return None
    held = lease.get("lease") if isinstance(lease.get("lease"), dict) else {}
    if str(held.get("session") or "") != session_id:
        return None
    return {**held, "remaining_ms": lease.get("remaining_ms")}


def _title(text: Any) -> str:
    clean = " ".join(str(text or "").split()).replace("“", "\"").replace("”", "\"")
    return clean[:TITLE_CHARS] + ("…" if len(clean) > TITLE_CHARS else "")


def _app_name(app: dict[str, Any]) -> str:
    return _title(app.get("name") or app.get("bundle_id") or app.get("id") or "")


def display_words(display_id: str, displays: list[Any]) -> str:
    """A monitor as the operator knows it: its name from the daemon, else its place in the list."""
    for index, display in enumerate(displays, start=1):
        if isinstance(display, dict) and str(display.get("id")) == display_id:
            name = _title(display.get("name"))
            return f"the monitor «{name}»" if name else f"monitor {index}"
    return "one monitor"


def scope_words(scope: dict[str, Any], apps: dict[str, Any] | None, displays: list[Any]) -> str:
    """The granted scope in words: the window “…” of an app, the app …, a monitor, or the whole desktop.

    A window's title is text anyone can set; one that reads like instructions to an agent is left out
    and the window is named by its app alone, so the turn context never carries such text."""
    kind = str(scope.get("kind") or "")
    listed = [a for a in (apps or {}).get("apps") or () if isinstance(a, dict)]
    if kind == "window":
        wanted = str(scope.get("window") or "")
        for app in listed:
            for window in app.get("windows") or ():
                if isinstance(window, dict) and str(window.get("id")) == wanted:
                    title, name = _title(window.get("title")), _app_name(app)
                    of = f" of {name}" if name else ""
                    if title and not injection_signs(title):
                        return f"the window “{title}”{of}"
                    return f"one window{of}"
        return "one window"
    if kind == "app":
        wanted = str(scope.get("app") or "")
        app = next((a for a in listed if str(a.get("id")) == wanted), None)
        name = _app_name(app) if app is not None else ""
        return f"the app {name} (all its windows)" if name else "one app (all its windows)"
    if kind == "display":
        return display_words(str(scope.get("display") or ""), displays)
    return "the whole desktop"


def until_words(lease: dict[str, Any], now: datetime | None = None) -> str:
    """The lease's end as the turn context writes its clock: ``HH:MM UTC``."""
    now = now or datetime.now(UTC)
    raw = str(lease.get("expires_at") or "")
    end: datetime | None = None
    if raw:
        try:
            end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            end = None
    if end is None and isinstance(lease.get("remaining_ms"), (int, float)):
        end = now + timedelta(milliseconds=float(lease["remaining_ms"]))
    if end is None:
        return "the operator takes it back"
    return end.astimezone(UTC).strftime("%H:%M UTC")


def actions_left(lease: dict[str, Any]) -> str:
    most, done = lease.get("max_actions"), lease.get("actions")
    if not isinstance(most, int) or most <= 0:
        return "no limit on actions"
    left = max(0, most - (done if isinstance(done, int) else 0))
    return f"{left} action{'' if left == 1 else 's'} left"


async def describe(client: DeskClient, session_id: str, lease: dict[str, Any]) -> str:
    """The scope of a lease this session holds, in words; the apps are read only for a window or app."""
    scope = lease.get("scope") if isinstance(lease.get("scope"), dict) else {}
    apps: dict[str, Any] | None = None
    if scope.get("kind") in ("window", "app"):
        try:
            listed = await client.call("apps.list", {"session": session_id})
            apps = listed if isinstance(listed, dict) else None
        except (DeskError, DeskUnavailable) as exc:
            logger.debug("no app names for the turn context: %s", exc)
    displays = client.info.get("displays") if isinstance(client.info.get("displays"), list) else []
    return scope_words(scope, apps, displays)


def granted_line(scope: str, lease: dict[str, Any], *, loaded: bool, sees: bool = True, now: datetime | None = None) -> str:
    line = (
        f"- Computer: the operator has given you control of {scope} until {until_words(lease, now)} ({actions_left(lease)}). "
        + TOOLS_WORDS.format(loaded=" (loaded)" if loaded else "")
    )
    return line + ("" if sees else NO_IMAGES)


def grant_steer(scope: str, lease: dict[str, Any], now: datetime | None = None) -> str:
    """The note a run in progress is steered with when a grant arrives: the turn context's line came
    before the grant, and the tools are loaded only from the next run."""
    return (
        f"[computer] The operator has just given you control of {scope} on their computer until {until_words(lease, now)} "
        f"({actions_left(lease)}). On-screen work on their computer is now expected to be done with Computer and ComputerApps, "
        "not with Exec or your own browser. If they are not loaded yet, load them with ToolSearch(group=\"computer\")."
    )


__all__ = [
    "NOT_GRANTED",
    "actions_left",
    "describe",
    "display_words",
    "grant_steer",
    "granted_line",
    "held_lease",
    "scope_words",
    "until_words",
]
