"""Browser ownership observations that distinguish reattachment from creation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from daedalus.browser.model import BrowserError


def safe_display_url(value: str) -> str | None:
    """Expose the saved page without credentials, query secrets or fragments."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return None


def browser_generation(row: dict[str, Any]) -> str:
    material = "\x00".join(str(row.get(key) or "") for key in ("id", "env", "daemon_instance", "browser_id"))
    return hashlib.sha256(material.encode()).hexdigest()


async def browser_ownership(service: Any, group_id: str) -> dict[str, Any]:
    row = await service.get_row(group_id)
    state = "no" if row["status"] != "open" else "unknown"
    observed_at = row["closed_at"] if state == "no" else None
    reason = row["close_reason"] if state == "no" else "unconfirmed"
    if state == "unknown":
        try:
            listing = await service._call(row["env"], "group.list", {}, what="probing browser ownership")
            entries = [item for item in listing.get("groups") or [] if isinstance(item, dict) and str(item.get("id")) == group_id]
            observed_at = datetime.now(UTC).isoformat()
            link = service.links.get(row["env"])
            instance = link.client.instance if link is not None and link.client is not None else None
            if not instance or instance != row["daemon_instance"]:
                reason = "instance_changed"
            elif not entries:
                state, reason = "no", "not_in_daemon"
            elif str(entries[0].get("browser_id") or "") != str(row["browser_id"] or ""):
                reason = "instance_changed"
            else:
                state, reason = "yes", "live_daemon_group"
        except BrowserError:
            reason = "daemon_unavailable"
    return {"group_id": group_id, "owner_kind": row["owner_kind"], "owner_id": row["owner_id"],
            "project_id": row["project_id"], "session_id": row["session_id"], "staff_id": row["staff_id"],
            "instance_generation": browser_generation(row), "alive": state, "reason": reason,
            "observed_at": observed_at, "last_safe_url": safe_display_url(str(row["url"] or "")),
            "reconnectable": state == "yes", "authorization_state": "authenticated_operator"}
