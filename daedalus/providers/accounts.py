"""Which subscription account a model call goes out on, and moving to the next one when it is spent.

The key proxy signs a call for the account named in ``x-daedalus-account`` (``deploy/keyproxy/accounts.py``);
this side decides the name. The adapter asks for a chain before it sends (:meth:`AccountRouter.chain`): the
first entry is the account the call is sent with, and when that account is refused for a limit — a spent
window, a rate limit, a lapsed plan or a login that no longer signs — the adapter rests it here and sends the
identical request on the next one. Nothing reached the run before the refusal, so nothing is repeated.

Only the last refusal of a call that no account could answer becomes the provider's failure observation
(``provider_failure_observations``), which rests the whole provider and stops the fallback chain from using
it. A refusal that another account absorbed is the account's alone: recorded on it, and the provider stays
usable, which is the point of having a second account.

Prompt cache: every account is its own cache at the vendor. A call moved to another account pays for its
whole prompt once there, and :meth:`SubscriptionAccounts.chain` keeps a session on the account it moved to,
so the price is paid once per move and not again on every flip back.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from daedalus.config import LOGIN_UPSTREAMS, keyproxy_upstream

if TYPE_CHECKING:
    # Under TYPE_CHECKING alone: the stores package imports the adapter, which imports this module.
    from daedalus.providers.failure_evidence import FailureEvidence
    from daedalus.providers.openai_compat import ProviderEndpoint
    from daedalus.stores.subscription_accounts import SubscriptionAccounts

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT = "default"
"""The login or key an upstream had before it had accounts; it is never named to the key proxy."""
KEYED_PLANS = ("opencode", "zai_coding", "minimax_plan", "kimi_coding")
"""The prepaid plans that sign with a key; an account of one is a second key for the same plan."""
ACCOUNT_UPSTREAMS = (*LOGIN_UPSTREAMS, *KEYED_PLANS)
"""Every key-proxy upstream that can have more than one account, in the order Settings lists them."""

ACCOUNT_HEADER = "x-daedalus-account"
FALLOVER_CLASSES = frozenset({"quota", "billing", "rate", "limited_unknown", "auth"})
"""The refusals that say this account cannot answer now while another might: its limit, its plan or its login.
A server error or an overloaded model is the vendor's and would refuse the next account just the same."""
UNDATED_REST = {"quota": timedelta(hours=1), "billing": timedelta(hours=1), "rate": timedelta(minutes=5), "limited_unknown": timedelta(minutes=5)}
"""How long a refusal that named no reset keeps its account out. A spent quota is checked again within the
hour (the provider's own rest uses the same span); a rate limit is short by nature. ``auth`` has no span: the
account stays out until it is signed in again."""

AccountSwitch = Callable[["ProviderEndpoint", Any, str, str, str], Awaitable[None]]
"""``(endpoint, request, from account, to account, failure class)``: told when a call moves on."""


class AccountRouter:
    """The adapter's view of the accounts: the chain for a call, the rest after a refusal, who answered."""

    def __init__(self, store: SubscriptionAccounts, preset_account: Callable[[str, str], str]) -> None:
        """``preset_account(provider id, model)`` is the account the operator chose for that model, "" for none."""
        self.store = store
        self._preset_account = preset_account
        self.on_switch: AccountSwitch | None = None
        """Set by the session runner, which says on the run's screen that the call moved."""

    def upstream(self, endpoint: ProviderEndpoint) -> str:
        """The subscription upstream ``endpoint`` reaches through the key proxy, or "" when it has no accounts."""
        # Read with a default: a stand-in endpoint (a scripted model, a free-catalog route) may carry no address,
        # and asking it which account answered must not be what breaks the run it answered.
        found = keyproxy_upstream(getattr(endpoint, "base_url", "") or "")
        return found if found in ACCOUNT_UPSTREAMS else ""

    def chain(self, endpoint: ProviderEndpoint, model: str, session_id: str | None) -> list[str]:
        """The accounts to try, first one first; [] for an endpoint that has no accounts to choose from."""
        upstream = self.upstream(endpoint)
        if not upstream or not self.store.several(upstream):
            # One account is the default login and goes out exactly as it did before there were accounts.
            return []
        return self.store.chain(upstream, self._preset_account(endpoint.id, model), session_id)

    async def limited(self, endpoint: ProviderEndpoint, account: str, evidence: FailureEvidence) -> None:
        upstream = self.upstream(endpoint)
        if not upstream:
            return
        until: str | None = None
        for stated in (evidence.reset_at, evidence.retry_after_at):
            if stated and (until is None or stated > until):
                until = stated
        if until is None and evidence.failure_class in UNDATED_REST:
            until = (datetime.now(UTC) + UNDATED_REST[evidence.failure_class]).isoformat()
        await self.store.rest(upstream, account, evidence.failure_class, until)
        logger.warning("%s: account %s refused with %s; resting it until %s", endpoint.id, account, evidence.failure_class, until or "it signs in again")

    async def switched(self, endpoint: ProviderEndpoint, request: Any, from_account: str, to_account: str, failure_class: str) -> None:
        if self.on_switch is None:
            return
        try:
            await self.on_switch(endpoint, request, from_account, to_account, failure_class)
        except Exception:  # noqa: BLE001 — the notice is for the screen; the call goes on without it
            logger.warning("could not report the move of %s to account %s", endpoint.id, to_account, exc_info=True)

    async def served(self, endpoint: ProviderEndpoint, account: str, session_id: str | None) -> None:
        """An account answered: the session stays on it, and a rest it was under is over, since it answers."""
        upstream = self.upstream(endpoint)
        if upstream and account:
            self.store.served(upstream, account, session_id)
            await self.store.wake(upstream, account)

    def label(self, endpoint: ProviderEndpoint, account: str) -> str:
        upstream = self.upstream(endpoint)
        return self.store.label(upstream, account) if upstream else ""

    def last_served(self, endpoint: ProviderEndpoint, session_id: str) -> str | None:
        """The account that answered the session's latest call on ``endpoint``, while there are several."""
        upstream = self.upstream(endpoint)
        if not upstream or not self.store.several(upstream):
            return None
        return self.store.last_served(session_id, upstream)


def account_header(account: str) -> dict[str, str]:
    """The header that names ``account`` to the key proxy; none for the default login, which is what the
    proxy serves when it is told nothing, so a call on it stays byte for byte what it always was."""
    return {ACCOUNT_HEADER: account} if account and account != DEFAULT_ACCOUNT else {}


__all__ = ["ACCOUNT_HEADER", "ACCOUNT_UPSTREAMS", "DEFAULT_ACCOUNT", "FALLOVER_CLASSES", "KEYED_PLANS", "AccountRouter", "account_header"]
