"""Telling the operator when a CLI the key proxy imitates has fallen behind, and updating it on request.

The key proxy serves the operator's Claude Code, Codex and Grok subscriptions the way each vendor's
CLI would call them, version included, and the vendors refuse a version they consider too old. The
proxy keeps one profile per imitated client, learns the newest releases itself (it has the network
egress; the agent's container may not) and stores an update in its own keys file, so nothing is
rebuilt or restarted. This side reads that state now and then, posts one notification per new
release of a client the operator is signed in to, with an Update button, and serves the same state
to Settings, where the update can also be reverted. The proxy itself puts the previous profile back
when the first request after an update is refused for its version; that is announced too.

Every call goes to the address the launcher gave this process, on the bot's own API token, like the
key routes in :mod:`daedalus.extensions.api`, and only when some endpoint is served through it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from daedalus.config import is_keyproxy_url, keyproxy_base
from daedalus.extensions.notifications import Action, ActionOutcome, ActionRefused, ActionRequest, Draft
from daedalus.host.notify_text import render

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

REF = "keyproxy"
"""``keyproxy:<client>:<offered version>`` is the request an update notification answers."""
FIRST_CHECK_SECONDS = 150
"""The proxy asks the registries a minute after it starts; this reads its answer after that."""
CHECK_SECONDS = 30 * 60
"""Only the proxy's cached answer is read here; the registries are asked by the proxy every six hours."""
CALL_TIMEOUT_SECONDS = 45.0
"""An update with nothing cached asks the registry first, which the proxy allows twenty seconds."""
NOTED_FILE = "keyproxy-clients-announced.json"
"""Per client, the release already offered and the automatic revert already reported, so a restart repeats neither."""
LINK = "/app/settings/models?tab=providers"
"""Where the key proxy's card is, client versions included."""


class ProxyUnavailable(Exception):
    """What to tell the operator, with the HTTP status the app's route answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def origin(app: Application | Any) -> str:
    """The key proxy's address, or "" when no endpoint goes through it and there is nothing to ask."""
    base = keyproxy_base()
    return base if any(is_keyproxy_url(p.base_url) for p in app.config.providers.values()) else ""


async def _token(app: Application | Any) -> str:
    token = (getattr(app, "extensions", None) or {}).get("api_token")
    if token:
        return str(token)
    stored = await app.db.kv_get("api_token")
    return str(stored or "")


async def _call(app: Application | Any, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = origin(app)
    if not base:
        raise ProxyUnavailable(409, "this installation has no key proxy")
    headers = {"x-daedalus-token": await _token(app)}
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS) as client:
            response = await client.request(method, base + path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ProxyUnavailable(502, "the key proxy is not reachable") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 200 and isinstance(data, dict):
        return data
    reason = str(data.get("error") or "") if isinstance(data, dict) else ""
    if response.status_code == 403:
        raise ProxyUnavailable(502, "the key proxy refused this installation's token")
    if response.status_code == 404 and not reason.startswith("no imitated client"):
        # A proxy from before client profiles answers this path as an unknown upstream.
        raise ProxyUnavailable(501, "the key proxy is older than this app and keeps no client versions; recreate its container")
    if response.status_code in (400, 404, 409):
        raise ProxyUnavailable(response.status_code, reason or f"the key proxy answered {response.status_code}")
    raise ProxyUnavailable(502, reason or f"the key proxy answered {response.status_code}")


async def describe(app: Application | Any) -> dict[str, Any]:
    """What Settings shows: every imitated client, or why there is nothing to show. Never raises."""
    try:
        data = await _call(app, "GET", "/clients")
    except ProxyUnavailable as exc:
        state = "none" if exc.status == 409 else "outdated" if exc.status == 501 else "unreachable"
        return {"reachable": False, "state": state, "error": str(exc), "clients": []}
    return {"reachable": True, "state": "ok", **data}


async def check_now(app: Application | Any) -> dict[str, Any]:
    data = await _call(app, "POST", "/clients/check")
    await announce(app, data)
    return {"reachable": True, "state": "ok", **data}


async def update(app: Application | Any, name: str, version: str = "", *, via: str = "settings") -> dict[str, Any]:
    """Move one client to the newest release (or ``version``); close the notification that offered it."""
    result = await _call(app, "POST", f"/clients/{name}/update", {"version": version} if version else {})
    client = result.get("client") or {}
    notifications = getattr(app, "notifications", None)
    noted = _noted(app)
    mine = noted.get(name) or {}
    if notifications is not None and mine.get("offered") and not mine.get("closed"):
        # From its own button the notification store closes the entry with the resolver's answer.
        if via != "notification":
            await notifications.resolve(f"{REF}:{name}:{mine['offered']}", render("keyproxy.client.updated", notifications.language(), version=client.get("version") or mine["offered"]), via=via)
        mine["closed"] = True
        _note(app, noted)
    return result


async def revert(app: Application | Any, name: str) -> dict[str, Any]:
    return await _call(app, "POST", f"/clients/{name}/revert")


def _noted_path(app: Application | Any) -> Path:
    return Path(app.settings.state_dir) / NOTED_FILE


def _noted(app: Application | Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_noted_path(app).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _note(app: Application | Any, noted: dict[str, dict[str, Any]]) -> None:
    try:
        _noted_path(app).write_text(json.dumps(noted, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not record which client releases were announced")


def _field_names(client: dict[str, Any], kept: list[str]) -> str:
    labels = client.get("field_labels") if isinstance(client.get("field_labels"), dict) else {}
    return ", ".join(str(labels.get(field) or field) for field in kept)


async def announce(app: Application | Any, view: dict[str, Any]) -> list[str]:
    """Post what is new in ``view``: a release of a signed-in client not offered before, and an automatic revert.

    Returns the request refs posted. A release already offered is not posted again, so the entry the
    operator dismissed stays dismissed; a newer one replaces it, and one that stopped being newer than
    the version in force (updated here or elsewhere) closes it.
    """
    notifications = getattr(app, "notifications", None)
    if notifications is None:
        return []
    lang = notifications.language()
    noted = _noted(app)
    posted: list[str] = []
    for client in view.get("clients") or []:
        if not isinstance(client, dict) or not client.get("name"):
            continue
        name, label = str(client["name"]), str(client.get("label") or client["name"])
        mine = noted.setdefault(name, {})
        latest, current = str(client.get("latest") or ""), str(client.get("version") or "")
        offered = str(mine.get("offered") or "")
        if offered and (not client.get("stale") or latest != offered) and not mine.get("closed"):
            # Updated, or overtaken by a newer release: the old offer has nothing left to do.
            resolution = render("keyproxy.client.updated", lang, version=current) if not client.get("stale") else render("keyproxy.client.superseded", lang, version=latest)
            await notifications.resolve(f"{REF}:{name}:{offered}", resolution, via="keyproxy")
            mine["closed"] = True
        if client.get("stale") and client.get("logged_in") is True and latest and latest != offered:
            kept = [str(field) for field in client.get("kept_on_update") or []]
            lines = [render("keyproxy.client.stale.body", lang, label=label, version=current, latest=latest)]
            if kept:
                lines.append(render("keyproxy.client.stale.kept", lang, fields=_field_names(client, kept)))
            lines.append(render("keyproxy.client.stale.revert", lang))
            ref = f"{REF}:{name}:{latest}"
            await notifications.post(Draft(
                "system",
                render("keyproxy.client.stale", lang, label=label, version=current, latest=latest),
                "\n\n".join(lines),
                kind="keyproxy_client",
                link=LINK,
                actions=(Action("update", render("keyproxy.client.update", lang), "primary"), Action("open", render("open", lang), "ghost")),
                request_ref=ref,
                dedupe_key=ref,
                source="keyproxy",
            ))
            mine.update(offered=latest, closed=False)
            posted.append(ref)
        reverted = client.get("reverted") if isinstance(client.get("reverted"), dict) else None
        if reverted and reverted.get("automatic") and float(reverted.get("at") or 0) > float(mine.get("reverted_at") or 0):
            await notifications.post(Draft(
                "system",
                render("keyproxy.client.reverted", lang, label=label, version=str(reverted.get("from") or "")),
                render("keyproxy.client.reverted.body", lang, label=label, version=str(reverted.get("from") or ""), current=current, reason=str(reverted.get("reason") or "")),
                kind="keyproxy_client",
                tone="warning",
                link=LINK,
                dedupe_key=f"{REF}-reverted:{name}:{reverted.get('from')}",
                source="keyproxy",
            ))
            mine["reverted_at"] = float(reverted.get("at") or 0)
    _note(app, noted)
    return posted


async def check_once(app: Application | Any) -> list[str]:
    view = await describe(app)
    if not view.get("reachable"):
        return []
    return await announce(app, view)


def resolver(app: Application | Any):  # noqa: ANN201 — the notification store's Resolver type
    async def resolve_action(request: ActionRequest) -> ActionOutcome:
        """The Update button on ``keyproxy:<client>:<version>``: update to that release, not to whatever is newest now."""
        if request.action != "update":
            raise ActionRefused(f"this notification offers no {request.action!r}")
        try:
            result = await update(app, request.scope, request.target, via="notification")
        except ProxyUnavailable as exc:
            raise ActionRefused(str(exc)) from None
        client = result.get("client") or {}
        lang = app.notifications.language()
        return ActionOutcome(render("keyproxy.client.updated", lang, version=client.get("version") or request.target))

    return resolve_action


async def _watch(app: Application) -> None:
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while True:
        try:
            await check_once(app)
        except Exception:  # noqa: BLE001 — a failed look is tried again later, never a crash
            logger.exception("could not read the key proxy's client versions")
        await asyncio.sleep(CHECK_SECONDS)


async def install(app: Application) -> list[asyncio.Task[None]]:
    if app.notifications is None:
        return []
    app.notifications.register_resolver(REF, resolver(app))
    return [asyncio.create_task(_watch(app), name="keyproxy-clients")]


__all__ = ["ProxyUnavailable", "announce", "check_now", "check_once", "describe", "install", "origin", "revert", "update"]
