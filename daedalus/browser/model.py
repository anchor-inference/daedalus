"""The values the browser service takes and gives: owners, group and profile ids, and its refusals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from daedalus.browser import wire

ENVS = ("container", "host", "user")
USER = "user"
"""The environment that is the operator's own Chrome, driven through the Daedalus extension. Never
an agent's default: the operator switches a conversation to it, and nothing the agent says can."""
AGENT_ENVS = ("container", "host")
"""The environments of the agent's own browser, one of which ``[browser] env`` picks."""
USER_PROFILE = "user"
"""The one profile the ``user`` daemon has: the operator's Chrome profile the extension runs in."""
OWNER_KINDS = ("session", "staff")
GROUP_STATUSES = ("open", "closed", "lost")
CONTROL_OWNERS = ("agent", "human", "paused")
HANDOFF_REASONS = ("login", "captcha", "two_factor", "payment", "confirm", "other")
EPHEMERAL = "ephemeral"
"""The daemon's reserved profile: a throwaway context wiped when its group closes."""

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class Owner:
    """Whose browser group it is: a Daedalus session (the operator's agents, a subagent, a Daedalus
    staff member's session), or a command-line staff member, which has no session of this host.

    The ids beside the owner are what an event about the group is published under, so a filter on a
    session, a staff member or a project sees its browser without knowing about browsers.
    """

    kind: str
    id: str
    project_id: str | None = None
    session_id: str | None = None
    staff_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in OWNER_KINDS:
            raise InvalidRequest(f"a browser's owner is one of {', '.join(OWNER_KINDS)}, not {self.kind!r}")
        if not self.id or not _ID.fullmatch(self.id):
            raise InvalidRequest(f"an owner id is 1–64 of A-Z a-z 0-9 - _, not {self.id!r}")

    def labels(self) -> dict[str, str]:
        """What the daemon keeps and echoes, so a browser adopted after a lost row still says whose."""
        out = {"owner_kind": self.kind, "owner_id": self.id}
        for key in ("project_id", "session_id", "staff_id"):
            value = getattr(self, key)
            if value:
                out[key] = str(value)
        return out

    @classmethod
    def from_labels(cls, labels: dict[str, Any]) -> Owner | None:
        try:
            return cls(
                str(labels.get("owner_kind") or ""),
                str(labels.get("owner_id") or ""),
                project_id=str(labels.get("project_id") or "") or None,
                session_id=str(labels.get("session_id") or "") or None,
                staff_id=str(labels.get("staff_id") or "") or None,
            )
        except InvalidRequest:
            return None


def group_id(owner: Owner, *, fresh: bool = False, env: str = "") -> str:
    """The one group an owner has in its profile's browser, and a second one for a throwaway context.

    A session's is ``s-<session>``, a command-line member's ``m-<staff>``; ``-x`` is the ephemeral one
    and ``-u`` the one in the operator's own Chrome, so a conversation switched between the two keeps
    both rows apart. The host makes one group per owner and profile, so two sessions of one project
    share the project's cookies but never see each other's tabs.
    """
    base = f"{'s' if owner.kind == 'session' else 'm'}-{owner.id}"
    if env == USER:
        return (base[:62] + "-u")[:64]
    return (base + "-x" if fresh else base)[:64]


def profile_id(owner: Owner, *, fresh: bool = False, env: str = "") -> str:
    """Where the owner's cookies live: its project's profile, else its own; a fresh group has none.
    In the operator's Chrome they live in the operator's profile, which is the one there is."""
    if env == USER:
        return USER_PROFILE
    if fresh:
        return EPHEMERAL
    if owner.project_id:
        return f"project-{owner.project_id}"[:64]
    return f"{owner.kind}-{owner.id}"[:64]


def valid_id(value: str) -> bool:
    return bool(_ID.fullmatch(value or ""))


class BrowserError(Exception):
    """Base of what the browser service refuses; the API maps each kind to a status, and the agent's
    tools read ``message`` as the sentence the model is told."""

    status = 500
    code = "error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(BrowserError):
    status, code = 404, "not_found"


class InvalidRequest(BrowserError):
    status, code = 400, "invalid"


class Conflict(BrowserError):
    status, code = 409, "conflict"


class OverCap(BrowserError):
    """As many browsers run as the environment allows."""

    status, code = 409, "over_cap"


class Forbidden(BrowserError):
    status, code = 403, "forbidden"


class EnvUnavailable(BrowserError):
    status, code = 503, "unavailable"


class Unsupported(BrowserError):
    status, code = 501, "unsupported"


class TimedOut(BrowserError):
    status, code = 504, "timeout"


class HumanDriving(BrowserError):
    status, code = 409, "human_driving"


class Paused(BrowserError):
    status, code = 409, "paused"


class Blocked(BrowserError):
    status, code = 403, "blocked"


class StaleRef(BrowserError):
    status, code = 409, "stale_ref"


class NoSuchTab(BrowserError):
    status, code = 404, "no_such_tab"


class FieldForbidden(BrowserError):
    status, code = 403, "field_forbidden"


class DialogOpen(BrowserError):
    status, code = 409, "dialog_open"


class BrowserGone(BrowserError):
    status, code = 410, "browser_gone"


class LiveBrowsers(BrowserError):
    """Recreating the browser service would end open browsers, and the operator has not said yes."""

    status, code = 409, "live_browsers"


class NoRebuilder(BrowserError):
    status, code = 503, "no_rebuilder"


def rpc_failure(exc: wire.RpcError, what: str) -> BrowserError:
    """A daemon's error as the service's own, keeping the daemon's words and its ``data``."""
    data = exc.data if isinstance(exc.data, dict) else {}
    code = exc.code
    table: dict[int, type[BrowserError]] = {
        wire.NOT_FOUND: NotFound,
        wire.FORBIDDEN: Forbidden,
        wire.TIMEOUT: TimedOut,
        wire.UNSUPPORTED: Unsupported,
        wire.HUMAN_DRIVING: HumanDriving,
        wire.BLOCKED: Blocked,
        wire.STALE_REF: StaleRef,
        wire.NO_SUCH_TAB: NoSuchTab,
        wire.FIELD_FORBIDDEN: FieldForbidden,
        wire.PAUSED: Paused,
        wire.DIALOG_OPEN: DialogOpen,
        wire.BROWSER_GONE: BrowserGone,
        wire.INVALID_PARAMS: InvalidRequest,
    }
    if code == wire.LIMIT:
        kind = OverCap if data.get("limit") == "browsers" else Conflict
        return kind(f"{what}: {exc.message}", reason="limit", **data)
    if code == wire.METHOD_NOT_FOUND:
        return Unsupported(f"{what}: not available in this browser service yet ({exc.message})")
    error = table.get(code)
    if error is None:
        return BrowserError(f"{what}: {exc.message} ({code})", **data)
    return error(f"{what}: {exc.message}", **data)


__all__ = [
    "AGENT_ENVS",
    "CONTROL_OWNERS",
    "ENVS",
    "EPHEMERAL",
    "GROUP_STATUSES",
    "HANDOFF_REASONS",
    "OWNER_KINDS",
    "USER",
    "USER_PROFILE",
    "Blocked",
    "BrowserError",
    "BrowserGone",
    "Conflict",
    "DialogOpen",
    "EnvUnavailable",
    "FieldForbidden",
    "Forbidden",
    "HumanDriving",
    "InvalidRequest",
    "LiveBrowsers",
    "NoRebuilder",
    "NoSuchTab",
    "NotFound",
    "OverCap",
    "Owner",
    "Paused",
    "StaleRef",
    "TimedOut",
    "Unsupported",
    "group_id",
    "profile_id",
    "rpc_failure",
    "valid_id",
]
