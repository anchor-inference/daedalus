"""Settings → Models → Accounts: more than one account per subscription, and which one a model uses.

An account is named and ordered here (:class:`daedalus.stores.subscription_accounts.SubscriptionAccounts`);
its credential is the key proxy's (``deploy/keyproxy/accounts.py``). Adding a CLI-login account makes its
home at the proxy and opens the CLI's own sign-in in a terminal on the operator's machine, pointed at that
home; adding a plan's account stores its key at the proxy. Nothing here ever reads a token or a key back.

The listing is what Settings shows: per upstream, its accounts in order with the default and the fallover
switch, and per account who it says it is (e-mail, plan), whether it can sign a call, whether it rests after
a refusal and until when, its quota windows from the proxy's usage reading, and its calls of the last day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.config import is_keyproxy_url, keyproxy_upstream
from daedalus.extensions.keyproxy_clients import ProxyUnavailable, origin, proxy_token
from daedalus.extensions.quota_sampler import fetch_subscriptions
from daedalus.harness.tools import tooling
from daedalus.providers.accounts import ACCOUNT_UPSTREAMS, DEFAULT_ACCOUNT, KEYED_PLANS
from daedalus.stores.subscription_accounts import LABEL_MAX, SubscriptionAccounts
from daedalus.terminals.model import Owner, TerminalSpec

if TYPE_CHECKING:
    from daedalus.app import Application

ACCOUNTS_DIR = "daedalus-accounts"
"""The folder, inside each CLI's own, that holds an account's home; the key proxy's name for it."""
LOGINS = {
    # upstream: (the CLI's folder in the home, the variable that points the CLI at another one)
    "claude": (".claude", "CLAUDE_CONFIG_DIR"),
    "codex": (".codex", "CODEX_HOME"),
    "grok": (".grok", "GROK_HOME"),
}
SIGN_IN_ENV = "host"
"""Where the sign-in terminal opens: the operator's machine, whose home the key proxy reads the logins from
(the compose stack mounts that home's three CLI folders; a native installation reads them in place)."""
CALL_TIMEOUT_SECONDS = 20.0


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=LABEL_MAX)
    key: str = Field(default="", max_length=4096)
    """A plan's key; ignored for a CLI login, which signs in through its own CLI."""


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=LABEL_MAX)
    position: int | None = Field(default=None, ge=0, le=10_000)


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=4096)


class UpstreamPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_account: str | None = None
    fallover: bool | None = None


def account_home(home: str, upstream: str, account: str) -> str:
    """The folder the account's CLI is pointed at, in the path syntax of ``home`` (a Windows home has backslashes)."""
    folder, _ = LOGINS[upstream]
    separator = "\\" if "\\" in home and "/" not in home else "/"
    parts = [home.rstrip("\\/"), folder] + ([ACCOUNTS_DIR, account] if account != DEFAULT_ACCOUNT else [])
    return separator.join(parts)


def sign_in_command(upstream: str, account: str, home: str) -> str:
    """The sign-in as one line the operator can run in a terminal of their own, when none can be opened here."""
    argv = " ".join(tooling(upstream).sign_in)
    if account == DEFAULT_ACCOUNT:
        return argv
    _, variable = LOGINS[upstream]
    path = account_home(home or "~", upstream, account)
    return f'$env:{variable}="{path}"; {argv}' if "\\" in path else f'{variable}="{path}" {argv}'


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def store() -> SubscriptionAccounts:
        manager = getattr(app, "manager", None)
        found = getattr(manager, "subscription_accounts", None)
        if found is None:
            raise HTTPException(503, "accounts are not kept by this process")
        return found

    def providers_of(upstream: str) -> list[str]:
        return sorted(pid for pid, p in app.config.providers.items() if is_keyproxy_url(p.base_url) and keyproxy_upstream(p.base_url) == upstream)

    def known(upstream: str) -> str:
        if upstream not in ACCOUNT_UPSTREAMS:
            raise HTTPException(404, f"{upstream!r} has no accounts")
        return upstream

    async def proxy(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        base = origin(app)
        if not base:
            raise ProxyUnavailable(409, "this installation has no key proxy")
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS) as client:
                response = await client.request(method, base + path, headers={"x-daedalus-token": await proxy_token(app)}, json=body)
        except httpx.HTTPError as exc:
            raise ProxyUnavailable(502, "the key proxy is not reachable") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code == 200 and isinstance(data, dict):
            return data
        reason = str(data.get("error") or "") if isinstance(data, dict) else ""
        if response.status_code == 404 and not reason:
            # A proxy from before accounts has no such route and answers aiohttp's bare 404.
            raise ProxyUnavailable(501, "the key proxy is older than this app and keeps no accounts; recreate its container")
        raise ProxyUnavailable(response.status_code if response.status_code in (400, 404, 409) else 502, reason or f"the key proxy answered {response.status_code}")

    def refused(exc: ProxyUnavailable) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    def home_of_sign_in() -> tuple[Any, str]:
        """The terminals service and the home of the machine whose logins the key proxy reads; (None, "") when not there."""
        terminals = app.extensions.get("terminals")
        if terminals is None:
            return None, ""
        status = next((s for s in terminals.environments() if s.env == SIGN_IN_ENV), None)  # type: ignore[attr-defined]
        if status is None or not status.available or not status.home:
            return None, ""
        return terminals, str(status.home)

    async def calls_by_account(since: str) -> dict[tuple[str, str], dict[str, Any]]:
        rows = await app.db.fetchall(
            "SELECT provider_id, account, count(*) calls, max(at) last_at FROM usage_events WHERE at >= ? GROUP BY provider_id, account", (since,))
        out: dict[tuple[str, str], dict[str, Any]] = {}
        upstream_of = {pid: keyproxy_upstream(p.base_url) for pid, p in app.config.providers.items() if is_keyproxy_url(p.base_url)}
        for row in rows:
            upstream = upstream_of.get(row["provider_id"])
            if not upstream:
                continue
            # A call before there were accounts, or on an upstream with only its default, carries no account.
            key = (upstream, row["account"] or DEFAULT_ACCOUNT)
            slot = out.setdefault(key, {"calls": 0, "last_at": None})
            slot["calls"] += int(row["calls"])
            slot["last_at"] = max(filter(None, (slot["last_at"], row["last_at"])), default=None)
        return out

    @api.get("/api/subscription-accounts")
    async def listing(usage: bool = True, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """``usage=false`` leaves out the quota reading, which asks every account's vendor: a list that only
        needs the accounts' names (a model's account picker) does not wait for that."""
        accounts = store()
        at = datetime.now(UTC)
        proxy_state: dict[str, Any] = {"reachable": True, "state": "ok", "error": ""}
        try:
            facts = (await proxy("GET", "/accounts")).get("upstreams") or {}
        except ProxyUnavailable as exc:
            facts = {}
            proxy_state = {"reachable": False, "state": "none" if exc.status == 409 else "outdated" if exc.status == 501 else "unreachable", "error": str(exc)}
        readings = await fetch_subscriptions(app) if usage and proxy_state["reachable"] else {}  # type: ignore[arg-type]
        calls = await calls_by_account((at - timedelta(days=1)).isoformat())
        terminals, home = home_of_sign_in()
        upstreams = []
        for upstream in ACCOUNT_UPSTREAMS:
            providers = providers_of(upstream)
            if not providers:
                continue
            choice = accounts.choice(upstream)
            seen = {str(f.get("id")): f for f in facts.get(upstream) or [] if isinstance(f, dict)}
            view_of = readings.get(upstream) if isinstance(readings.get(upstream), dict) else {}
            rows = []
            for account in accounts.accounts(upstream):
                fact = seen.get(account.id) or {}
                reading = view_of if account.id == DEFAULT_ACCOUNT else (view_of.get("accounts") or {}).get(account.id) or {}
                rows.append({
                    **account.view(at),
                    "default": account.id == choice.default_account,
                    "configured": bool(fact.get("configured")),
                    "email": str(fact.get("email") or ""),
                    "plan": str(fact.get("plan") or reading.get("plan") or ""),
                    "usage": {"windows": reading.get("windows") or [], "limit_reached": bool(reading.get("limit_reached")), "error": str(reading.get("error") or "")} if reading else None,
                    "calls_24h": calls.get((upstream, account.id), {}).get("calls", 0),
                    "last_call_at": calls.get((upstream, account.id), {}).get("last_at"),
                    "sign_in_command": sign_in_command(upstream, account.id, home) if upstream in LOGINS else "",
                })
            upstreams.append({
                "upstream": upstream,
                "providers": providers,
                "sign_in": "key" if upstream in KEYED_PLANS else "terminal",
                "default_account": choice.default_account,
                "fallover": choice.fallover,
                "accounts": rows,
            })
        return {"proxy": proxy_state, "terminal": terminals is not None, "upstreams": upstreams}

    @api.post("/api/subscription-accounts/{upstream}")
    async def add(upstream: str, body: AddBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        upstream = known(upstream)
        key = body.key.replace("\r", "").replace("\n", "").strip()
        if upstream in KEYED_PLANS and not key:
            raise HTTPException(400, "a plan's account needs its key")
        accounts = store()
        account = await accounts.add(upstream, body.label)
        try:
            await proxy("POST", f"/accounts/{upstream}/{account.id}", {"key": key} if upstream in KEYED_PLANS else {})
        except ProxyUnavailable as exc:
            # The row goes with the failure: an account the proxy never heard of could only be refused.
            await accounts.remove(upstream, account.id)
            raise refused(exc) from exc
        return account.view()

    @api.post("/api/subscription-accounts/{upstream}/{account_id}/login")
    async def sign_in(upstream: str, account_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Open the CLI's sign-in in a terminal on the operator's machine, pointed at the account's home.

        Refused with the command to run by hand where no terminal can be opened there, so a sign-in is
        never a dead end."""
        upstream = known(upstream)
        if upstream not in LOGINS:
            raise HTTPException(400, f"{upstream!r} signs in with a key, not a terminal")
        accounts = store()
        if accounts.account(upstream, account_id) is None:
            raise HTTPException(404, "no such account")
        terminals, home = home_of_sign_in()
        if terminals is None:
            raise HTTPException(409, f"no terminal can be opened on the machine that holds the logins; run this there: {sign_in_command(upstream, account_id, '')}")
        if account_id != DEFAULT_ACCOUNT:
            try:
                await proxy("POST", f"/accounts/{upstream}/{account_id}")
            except ProxyUnavailable as exc:
                raise refused(exc) from exc
        _, variable = LOGINS[upstream]
        env_vars = {variable: account_home(home, upstream, account_id)} if account_id != DEFAULT_ACCOUNT else {}
        label = accounts.label(upstream, account_id) or account_id
        view = await terminals.create(  # type: ignore[attr-defined]
            TerminalSpec(env=SIGN_IN_ENV, owner=Owner("free"), argv=list(tooling(upstream).sign_in), env_vars=env_vars,
                         title=f"Sign in: {upstream} · {label}", cols=120, rows=32, profile="shell", created_by="operator"),
            confirm_over_cap=True,
        )
        # Signing in again is how a login refused as signed out comes back: it is tried on the next call.
        await accounts.wake(upstream, account_id)
        return {"terminal_id": str(view["id"]), "command": sign_in_command(upstream, account_id, home)}

    @api.put("/api/subscription-accounts/{upstream}/{account_id}/key")
    async def set_key(upstream: str, account_id: str, body: KeyBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Replace a plan account's key: the plan's way of signing in again."""
        upstream = known(upstream)
        if upstream not in KEYED_PLANS:
            raise HTTPException(400, f"{upstream!r} signs in with its CLI, not a key")
        if account_id == DEFAULT_ACCOUNT:
            raise HTTPException(400, "the default account's key is the plan's own, under Key proxy")
        accounts = store()
        if accounts.account(upstream, account_id) is None:
            raise HTTPException(404, "no such account")
        try:
            await proxy("POST", f"/accounts/{upstream}/{account_id}", {"key": body.key.replace("\r", "").replace("\n", "").strip()})
        except ProxyUnavailable as exc:
            raise refused(exc) from exc
        await accounts.wake(upstream, account_id)
        return {"upstream": upstream, "id": account_id, "configured": True}

    @api.patch("/api/subscription-accounts/{upstream}/{account_id}")
    async def change(upstream: str, account_id: str, body: AccountPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        upstream = known(upstream)
        try:
            return (await store().update(upstream, account_id, label=body.label, position=body.position)).view()
        except KeyError as exc:
            raise HTTPException(404, "no such account") from exc

    @api.delete("/api/subscription-accounts/{upstream}/{account_id}")
    async def remove(upstream: str, account_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Forget an account: its login or key at the key proxy, then its row. A model that named it goes
        back to the upstream's default account."""
        upstream = known(upstream)
        if account_id == DEFAULT_ACCOUNT:
            raise HTTPException(400, "the default account is the CLI's own login or the plan's own key; sign it out there")
        accounts = store()
        if accounts.account(upstream, account_id) is None:
            raise HTTPException(404, "no such account")
        try:
            await proxy("DELETE", f"/accounts/{upstream}/{account_id}")
        except ProxyUnavailable as exc:
            raise refused(exc) from exc
        await accounts.remove(upstream, account_id)
        return {"removed": account_id}

    @api.put("/api/subscription-accounts/{upstream}")
    async def choose(upstream: str, body: UpstreamPatch, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        upstream = known(upstream)
        try:
            choice = await store().set_choice(upstream, default_account=body.default_account, fallover=body.fallover)
        except KeyError as exc:
            raise HTTPException(404, "no such account") from exc
        return {"upstream": upstream, "default_account": choice.default_account, "fallover": choice.fallover}


__all__ = ["account_home", "register", "sign_in_command"]
