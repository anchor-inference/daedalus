"""Non-secret capability and health snapshots derived from adapters actually shipped here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from daedalus.doctor import provider_key_source
from daedalus.security.redact import redact


def capability_matrix(*, github_configured: bool, webhooks: set[str]) -> list[dict[str, Any]]:
    """An absent provider adapter is reported as unavailable, never as implied parity."""
    return [
        {
            "provider": "github", "source": "host_adapter", "configured": github_configured,
            "capabilities": {"pull_request_read": True, "pull_request_write": True,
                             "issue_sync": True, "inbound_events": "github" in webhooks},
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


GITHUB_STATES = {
    # The probe's reason, read as the row the Health screen draws and how loud it is.
    "auth_probe_ok": ("authenticated", "ok"),
    "no_token": ("not_configured", "info"),
    "authentication_rejected": ("token_rejected", "fail"),
    "network_unavailable": ("unreachable", "warn"),
    "probe_rejected": ("unreachable", "warn"),
}

PROVIDER_FAILURES = {
    "auth": ("key_rejected", "fail"),
    "billing": ("out_of_credit", "fail"),
    "quota": ("out_of_credit", "fail"),
    "rate": ("rate_limited", "warn"),
    "limited_unknown": ("rate_limited", "warn"),
}


def _row(kind: str, name: str, state: str, severity: str, detail: str = "") -> dict[str, Any]:
    """One line of the Health screen's integration card.

    ``state`` is a code the app turns into words in the operator's language, with the one-line
    remedy beside it; ``detail`` is the technical text kept under a fold, never the only explanation.
    """
    return {"kind": kind, "name": name, "state": state, "severity": severity, "detail": detail}


async def github_row(token: str, *, probe: bool, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """GitHub's row. Without ``probe`` a present token is only reported as present: the screen asks
    with ``probe`` once it has drawn, so an unreachable GitHub costs the probe's own five seconds and
    never holds up the rest of the card."""
    if not token.strip():
        return _row("github", "GitHub", "not_configured", "info")
    if not probe:
        return _row("github", "GitHub", "configured", "ok")
    answer = await github_auth_probe(token, transport=transport)
    state, severity = GITHUB_STATES.get(answer.get("reason", ""), ("unreachable", "warn"))
    detail = answer.get("reason", "") + (f" (HTTP {answer['http_status']})" if "http_status" in answer else "")
    return _row("github", "GitHub", state, severity, detail)


def mcp_rows(status: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per configured MCP server, from the manager's own view of its connections.

    Servers connect when a session turns them on, so one that has not been started is idle, not
    broken; only a server that tried and failed, or that needs its sign-in renewed, is a failure.
    """
    out = []
    for server in status:
        name, state, error = server["name"], server.get("state") or "disabled", server.get("error") or ""
        if state == "auth_required":
            out.append(_row("mcp", name, "auth_required", "fail", error))
        elif error or state == "unavailable":
            out.append(_row("mcp", name, "error", "fail", error))
        elif state == "ready" and server.get("connected"):
            tools = len(server.get("tools") or [])
            out.append(_row("mcp", name, "connected", "ok", f"{tools} tools"))
        elif state == "connecting":
            out.append(_row("mcp", name, "connecting", "info"))
        else:
            out.append(_row("mcp", name, "idle", "info"))
    return out


async def provider_rows(settings: Any, config: Any, db: Any) -> list[dict[str, Any]]:
    """One row per configured model provider: whether it has a key, and how its last call ended.

    Nothing here calls the provider. The last refusal the adapter recorded counts only while no call
    to that provider has succeeded since; a key that was rejected on Monday and works on Tuesday is
    not still drawn red on Wednesday.
    """
    out = []
    for pid, provider in config.providers.items():
        name = provider.name or pid
        source = provider_key_source(settings, provider)
        if source == "missing":
            out.append(_row("provider", name, "no_key", "fail", f"{provider.kind}: no API key in the settings or the environment"))
            continue
        failure = None
        if db is not None:
            failure = await db.fetchone(
                "SELECT status,failure_class,provider_code,reset_at,observed_at FROM provider_failure_observations"
                " WHERE provider_id = ? ORDER BY observed_at DESC LIMIT 1",
                (pid,),
            )
            if failure is not None and await db.fetchone(
                "SELECT 1 FROM usage_events WHERE provider_id = ? AND at > ? LIMIT 1", (pid, failure["observed_at"])
            ):
                failure = None
        if failure is None:
            out.append(_row("provider", name, "ready", "ok", f"{provider.kind}, key: {source}"))
            continue
        state, severity = PROVIDER_FAILURES.get(failure["failure_class"], ("last_call_failed", "warn"))
        detail = f"HTTP {failure['status']}" + (f" {failure['provider_code']}" if failure["provider_code"] else "")
        detail += f" at {failure['observed_at']}" + (f", resets at {failure['reset_at']}" if failure["reset_at"] else "")
        out.append(_row("provider", name, state, severity, detail))
    return out


async def integration_rows(settings: Any, config: Any, *, mcp_status: list[dict[str, Any]], db: Any, probe: bool = False,
                           transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """GitHub, then each MCP server, then each model provider: the card's rows in the card's order."""
    rows = [await github_row(settings.github_token, probe=probe, transport=transport)]
    rows += mcp_rows(mcp_status)
    rows += await provider_rows(settings, config, db)
    return [{**row, "detail": redact(row["detail"])} for row in rows]
