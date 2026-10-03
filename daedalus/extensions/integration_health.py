"""Non-secret capability and health snapshots derived from adapters actually shipped here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


def capability_matrix(*, github_configured: bool, webhooks: set[str]) -> list[dict[str, Any]]:
    """An absent provider adapter is reported as unavailable, never as implied parity."""
    return [
        {
            "provider": "github", "source": "host_adapter", "configured": github_configured,
            "capabilities": {"pull_request_read": True, "pull_request_write": True,
                             "issue_sync": False, "inbound_events": "github" in webhooks},
        },
        {
            "provider": "gitlab", "source": "no_host_adapter", "configured": False,
            "capabilities": {"pull_request_read": False, "pull_request_write": False,
                             "issue_sync": False, "inbound_events": "gitlab" in webhooks},
        },
        {
            "provider": "forgejo", "source": "no_host_adapter", "configured": False,
            "capabilities": {"pull_request_read": False, "pull_request_write": False,
                             "issue_sync": False, "inbound_events": "forgejo" in webhooks},
        },
    ]


async def github_auth_probe(token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    at = datetime.now(UTC)
    result = {"provider": "github", "observed_at": at.isoformat(),
              "expires_at": (at + timedelta(minutes=5)).isoformat(), "scope": "authentication_only"}
    if not token.strip():
        return {**result, "state": "needs_reconnect", "reason": "no_token"}
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
            )
    except (httpx.HTTPError, TimeoutError):
        return {**result, "state": "unknown", "reason": "network_unavailable"}
    if response.status_code == 200:
        return {**result, "state": "authenticated", "reason": "auth_probe_ok"}
    if response.status_code == 401:
        return {**result, "state": "needs_reconnect", "reason": "authentication_rejected"}
    return {**result, "state": "unknown", "reason": "probe_rejected", "http_status": response.status_code}
