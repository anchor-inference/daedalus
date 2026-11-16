"""The operator's accounts per subscription: their names, their order, which one is the default, and which rest.

A subscription upstream of the key proxy (a CLI login: Claude, Codex, Grok; a prepaid plan's key: OpenCode
Go, Z.AI Coding Plan, MiniMax Token Plan, Kimi Code) always has its ``default`` account: the login or key
it had before there were accounts. Every other account is a row here, and its credential lives with the key
proxy (see ``deploy/keyproxy/accounts.py``), never in this database.

The rows are few and read on every model call, so the whole table is held in memory and written through:
this process is the only writer, and a refusal that rests an account must be seen by the very next call.
"""

from __future__ import annotations

import secrets
import string
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from daedalus.providers.accounts import ACCOUNT_UPSTREAMS, DEFAULT_ACCOUNT
from daedalus.stores.database import Database

LABEL_MAX = 60
STICKY_SESSIONS = 4096
"""How many sessions remember the account that last served them; the oldest is forgotten first."""


@dataclass(frozen=True, slots=True)
class Account:
    upstream: str
    id: str
    label: str = ""
    position: int = 0
    created_at: str = ""
    resting_until: str | None = None
    """Until when a refusal says the account cannot answer; ``None`` when it can, or ``"auth"``'s open rest."""
    rest_reason: str = ""
    """The failure class that rested it (``quota``, ``rate``, ``auth`` …); "" when it is not resting."""
    last_served_at: str | None = None

    def resting(self, at: datetime | None = None) -> bool:
        if not self.rest_reason:
            return False
        if self.resting_until is None:
            # A login that was refused as signed out stays out until it is signed in again.
            return self.rest_reason == "auth"
        try:
            return datetime.fromisoformat(self.resting_until) > (at or datetime.now(UTC))
        except ValueError:
            return False

    def view(self, at: datetime | None = None) -> dict[str, Any]:
        return {"upstream": self.upstream, "id": self.id, "label": self.label, "position": self.position,
                "created_at": self.created_at, "resting": self.resting(at), "resting_until": self.resting_until,
                "rest_reason": self.rest_reason if self.resting(at) else "", "last_served_at": self.last_served_at}


@dataclass(frozen=True, slots=True)
class UpstreamChoice:
    default_account: str = DEFAULT_ACCOUNT
    fallover: bool = True
    """On by default: a second account is added to carry on when the first is spent, and an operator who wants
    every turn kept on one plan turns it off rather than discovering that it was never on."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    """Eight lower-case letters and digits: a folder name and a variable suffix at the key proxy."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


class SubscriptionAccounts:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._accounts: dict[tuple[str, str], Account] = {}
        self._choices: dict[str, UpstreamChoice] = {}
        self._sticky: OrderedDict[tuple[str, str], str] = OrderedDict()
        """(session, upstream) → the account that last served that session's calls."""

    async def load(self) -> None:
        accounts: dict[tuple[str, str], Account] = {}
        for row in await self.db.fetchall("SELECT upstream,id,label,position,created_at,resting_until,rest_reason,last_served_at FROM subscription_accounts"):
            account = Account(row["upstream"], row["id"], row["label"], int(row["position"]), row["created_at"],
                              row["resting_until"], row["rest_reason"], row["last_served_at"])
            accounts[(account.upstream, account.id)] = account
        choices = {row["upstream"]: UpstreamChoice(row["default_account"], bool(row["fallover"]))
                   for row in await self.db.fetchall("SELECT upstream,default_account,fallover FROM subscription_upstreams")}
        self._accounts, self._choices = accounts, choices

    # -- reading --------------------------------------------------------------------

    def accounts(self, upstream: str) -> list[Account]:
        """The upstream's accounts in their order, its default login first unless it was moved.

        The default login has a row only once it is renamed, moved or rested; until then it is listed as
        an unnamed account at position 0."""
        rows = [a for (u, _), a in self._accounts.items() if u == upstream]
        if not any(a.id == DEFAULT_ACCOUNT for a in rows):
            rows.append(Account(upstream, DEFAULT_ACCOUNT))
        return sorted(rows, key=lambda a: (a.position, a.created_at or "", a.id))

    def account(self, upstream: str, account_id: str) -> Account | None:
        if account_id == DEFAULT_ACCOUNT:
            return self._accounts.get((upstream, account_id)) or Account(upstream, DEFAULT_ACCOUNT)
        return self._accounts.get((upstream, account_id))

    def choice(self, upstream: str) -> UpstreamChoice:
        found = self._choices.get(upstream) or UpstreamChoice()
        if self.account(upstream, found.default_account) is None:
            # The default was removed: the CLI's own login is what is left that is certain to exist.
            return replace(found, default_account=DEFAULT_ACCOUNT)
        return found

    def label(self, upstream: str, account_id: str) -> str:
        found = self.account(upstream, account_id)
        return found.label if found is not None else ""

    def several(self, upstream: str) -> bool:
        """Whether this upstream has more than its default account: only then is an account worth naming."""
        return any(u == upstream and a.id != DEFAULT_ACCOUNT for (u, _), a in self._accounts.items())

    def chain(self, upstream: str, preferred: str = "", session_id: str | None = None, *, at: datetime | None = None) -> list[str]:
        """The accounts a call tries, in order: the first is the one it is sent with.

        It opens with the account the session was last served by, then the model's own (``preferred``),
        then the upstream's default; with fallover on, every other account follows in the operator's
        order. An account at rest is left out while another is not; when every one rests, the first is
        tried anyway so the call gets the provider's own refusal rather than none.

        The session's last account goes first for its prompt cache: a provider caches per account, so a
        session that moved to a second account after the first ran out would pay for its whole prompt
        again on the way back, and again on every flip after that. It stays where it landed until that
        account rests too, and a new session starts on the model's own account.
        """
        known = [a.id for a in self.accounts(upstream)]
        choice = self.choice(upstream)
        primary = preferred if preferred in known else choice.default_account
        if not choice.fallover:
            # One account, whatever happens to it: the operator's refusal to move a turn to another plan.
            return [primary]
        order = [primary]
        sticky = self._sticky.get((session_id, upstream)) if session_id else None
        if sticky in known and sticky != primary:
            order.insert(0, sticky)
        order.extend(a for a in known if a not in order)
        awake = [a for a in order if not (self.account(upstream, a) or Account(upstream, a)).resting(at)]
        return awake or [order[0]]

    # -- writing --------------------------------------------------------------------

    async def _write(self, account: Account) -> Account:
        await self.db.execute(
            "INSERT INTO subscription_accounts(upstream,id,label,position,created_at,resting_until,rest_reason,last_served_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(upstream,id) DO UPDATE SET label=excluded.label, position=excluded.position,"
            " resting_until=excluded.resting_until, rest_reason=excluded.rest_reason, last_served_at=excluded.last_served_at",
            (account.upstream, account.id, account.label, account.position, account.created_at or _now(),
             account.resting_until, account.rest_reason, account.last_served_at),
        )
        stored = replace(account, created_at=account.created_at or _now())
        self._accounts[(account.upstream, account.id)] = stored
        return stored

    async def add(self, upstream: str, label: str) -> Account:
        if upstream not in ACCOUNT_UPSTREAMS:
            raise ValueError(f"{upstream!r} has no accounts")
        position = max((a.position for a in self.accounts(upstream)), default=0) + 1
        account_id = new_id()
        while (upstream, account_id) in self._accounts:
            account_id = new_id()
        return await self._write(Account(upstream, account_id, label.strip()[:LABEL_MAX], position, _now()))

    async def update(self, upstream: str, account_id: str, *, label: str | None = None, position: int | None = None) -> Account:
        found = self.account(upstream, account_id)
        if found is None:
            raise KeyError(account_id)
        changed = found
        if label is not None:
            changed = replace(changed, label=label.strip()[:LABEL_MAX])
        if position is not None:
            changed = replace(changed, position=int(position))
        return await self._write(changed)

    async def remove(self, upstream: str, account_id: str) -> None:
        if account_id == DEFAULT_ACCOUNT:
            raise ValueError("the default account is the CLI's own login or the plan's own key")
        await self.db.execute("DELETE FROM subscription_accounts WHERE upstream = ? AND id = ?", (upstream, account_id))
        self._accounts.pop((upstream, account_id), None)
        for key in [k for k, v in self._sticky.items() if k[1] == upstream and v == account_id]:
            del self._sticky[key]
        if self.choice(upstream).default_account == account_id:
            await self.set_choice(upstream, default_account=DEFAULT_ACCOUNT)

    async def set_choice(self, upstream: str, *, default_account: str | None = None, fallover: bool | None = None) -> UpstreamChoice:
        if upstream not in ACCOUNT_UPSTREAMS:
            raise ValueError(f"{upstream!r} has no accounts")
        current = self._choices.get(upstream) or UpstreamChoice()
        if default_account is not None:
            if self.account(upstream, default_account) is None:
                raise KeyError(default_account)
            current = replace(current, default_account=default_account)
        if fallover is not None:
            current = replace(current, fallover=fallover)
        await self.db.execute(
            "INSERT INTO subscription_upstreams(upstream,default_account,fallover) VALUES (?,?,?)"
            " ON CONFLICT(upstream) DO UPDATE SET default_account=excluded.default_account, fallover=excluded.fallover",
            (upstream, current.default_account, int(current.fallover)),
        )
        self._choices[upstream] = current
        return current

    async def rest(self, upstream: str, account_id: str, reason: str, until: str | None) -> None:
        """Keep an account out of the chain until ``until`` (``None`` with ``auth``: until it signs in again)."""
        found = self.account(upstream, account_id)
        if found is None:
            return
        await self._write(replace(found, rest_reason=reason, resting_until=until))

    async def wake(self, upstream: str, account_id: str) -> None:
        found = self.account(upstream, account_id)
        if found is not None and found.rest_reason:
            await self._write(replace(found, rest_reason="", resting_until=None))

    def served(self, upstream: str, account_id: str, session_id: str | None) -> None:
        """Remember which account answered a session's call; in memory only, as only the cache cares."""
        if not session_id:
            return
        key = (session_id, upstream)
        self._sticky[key] = account_id
        self._sticky.move_to_end(key)
        while len(self._sticky) > STICKY_SESSIONS:
            self._sticky.popitem(last=False)

    def last_served(self, session_id: str, upstream: str) -> str | None:
        return self._sticky.get((session_id, upstream))


__all__ = ["Account", "SubscriptionAccounts", "UpstreamChoice", "new_id"]
