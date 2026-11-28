"""What a live link's visitors have spent, and whether the link's limit is reached.

Every model call is recorded through one sink (:class:`LinkUsageSink`); a call made by a session that is
running a visitor's turn (:meth:`SessionManager.link_of`) is also added to that link's row in
``session_shares``: its price into ``spent_usd`` for a metered model, its estimated share of the weekly
window into ``spent_weekly_pct`` for a subscription one. The operator's own runs carry no link and are
never counted.

The weekly share is an estimate, and how it is made matters to anyone trusting the limit. The key proxy
reports a window's used percentage for the whole account; other sessions spend the same account at the
same time, so the window's growth over a visitor's turn is not the visitor's. What is known per call is
its tokens. So the account's recent price of a token in window points is read from the last two readings
of the window between which the account did any work: the growth between them divided by every token
that went through the account in that interval, all sessions together. A call is charged its tokens at
that price. It is wrong where:

- readings are taken only while something polls the key proxy (the Usage screen, the sampler); with no
  two readings in the retained week no price is known and the call is charged nothing, so the weekly
  limit does not hold until readings exist (logged on every such call);
- the provider weighs tokens unequally (output against input, cached reads, a dearer model on the same
  plan): a token is a token here, so a visitor on output-heavy work is undercharged against a cache-heavy
  neighbour and the other way round;
- the price comes from the interval before the call, not the call's own; a provider that rounds its
  percentage to whole points makes a short interval's growth coarse.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.config import keyproxy_upstream, login_route
from daedalus.providers.openai_compat import UsageRecord, UsageSink

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

STOPPED = {
    "usd": "This link's spending limit is reached; the turn stopped. The person who shared it can raise the limit.",
    "weekly_pct": "This link's share of the subscription's weekly limit is used up; the turn stopped. The person who shared it can raise the limit.",
}
"""What the visitor reads when their turn is stopped, by the limit that stopped it."""

WEEK_SECONDS = 7 * 24 * 3600
SAMPLES_LOOKED_AT = 50


def billing_of(config: Any, provider_id: str) -> str:
    """``subscription`` for a plan (the provider says so, or it is a CLI login behind the key proxy), else ``metered``."""
    conf = config.providers.get(provider_id)
    if conf is None:
        return "metered"
    return "subscription" if conf.billing == "subscription" or login_route(conf.base_url) else "metered"


def quota_account(upstream: str, account: str | None) -> str:
    """The name the quota readings keep an account's windows under (:func:`daedalus.stores.quota.record_subscriptions`)."""
    return upstream if not account or account == "default" else f"{upstream}@{account}"


def _parse(value: Any) -> datetime | None:
    try:
        found = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


async def weekly_price(db: Database, config: Any, upstream: str, account: str | None) -> float | None:
    """Window points per token on this account lately, or None when no two readings say.

    The longest window the account reports is its weekly one. Pairs of readings are walked from the
    newest; a pair across a reset (the share fell) or with no work between them says nothing."""
    name = quota_account(upstream, account)
    rows = await db.fetchall(
        "SELECT window_name, sampled_at, used_percent, window_seconds FROM quota_samples WHERE account = ?"
        " ORDER BY sampled_at DESC LIMIT ?", (name, SAMPLES_LOOKED_AT * 4))
    if not rows:
        return None
    longest = max(rows, key=lambda r: int(r["window_seconds"] or (WEEK_SECONDS if r["window_name"] == "7d" else 0)))
    samples = [r for r in rows if r["window_name"] == longest["window_name"]][:SAMPLES_LOOKED_AT]
    providers = [pid for pid, conf in config.providers.items() if keyproxy_upstream(conf.base_url) == upstream]
    if not providers:
        return None
    marks = ",".join("?" for _ in providers)
    accounts = ("", "default") if not account or account == "default" else (account,)
    account_marks = ",".join("?" for _ in accounts)
    for newer, older in zip(samples, samples[1:], strict=False):
        grown = float(newer["used_percent"]) - float(older["used_percent"])
        if grown <= 0:
            continue
        row = await db.fetchone(
            f"SELECT coalesce(sum(input_tokens + output_tokens), 0) t FROM usage_events WHERE provider_id IN ({marks})"
            f" AND coalesce(account, '') IN ({account_marks}) AND at > ? AND at <= ?",
            (*providers, *accounts, older["sampled_at"], newer["sampled_at"]))
        tokens = int(row["t"] or 0) if row is not None else 0
        if tokens > 0:
            return grown / tokens
    return None


async def charge(db: Database, config: Any, share_id: str, record: UsageRecord) -> tuple[float, float]:
    """Add one call to the link's spend; ``(dollars, weekly points)`` it was charged."""
    usd = pct = 0.0
    if billing_of(config, record.provider_id) == "subscription":
        conf = config.providers.get(record.provider_id)
        upstream = keyproxy_upstream(conf.base_url) if conf is not None else ""
        price = await weekly_price(db, config, upstream, record.account) if upstream else None
        tokens = int(record.normalized.get("input_tokens", 0)) + int(record.normalized.get("output_tokens", 0))
        if price is None:
            logger.warning("live link %s: no weekly readings for %s yet, so a call of %d tokens is not counted against its weekly limit",
                           share_id, quota_account(upstream, record.account), tokens)
        else:
            pct = tokens * price
    elif record.cost_usd is not None:
        usd = float(record.cost_usd)
    if usd or pct:
        await db.execute("UPDATE session_shares SET spent_usd = spent_usd + ?, spent_weekly_pct = spent_weekly_pct + ? WHERE id = ?",
                         (usd, pct, share_id))
    return usd, pct


async def spent_of(db: Database, share_id: str) -> tuple[dict[str, Any], float, float] | None:
    """``(policy, dollars, weekly points)`` of the link, None when it is gone or not yet able to say."""
    try:
        row = await db.fetchone("SELECT policy, spent_usd, spent_weekly_pct FROM session_shares WHERE id = ?", (share_id,))
    except Exception:  # noqa: BLE001 — a database without the columns has links with no limits
        return None
    if row is None:
        return None
    try:
        policy = json.loads(row["policy"] or "{}")
    except ValueError:
        policy = {}
    return (policy if isinstance(policy, dict) else {}), float(row["spent_usd"] or 0), float(row["spent_weekly_pct"] or 0)


async def exhausted(app: Application, share_id: str) -> str | None:
    """Which limit of the link is spent: ``usd``, ``weekly_pct``, or None while there is room."""
    db = app.db if hasattr(app, "db") else app
    found = await spent_of(db, share_id)
    if found is None:
        return None
    policy, usd, pct = found
    money, weekly = policy.get("budget_usd"), policy.get("budget_weekly_pct")
    if isinstance(money, int | float) and not isinstance(money, bool) and usd >= float(money):
        return "usd"
    if isinstance(weekly, int | float) and not isinstance(weekly, bool) and pct >= float(weekly):
        return "weekly_pct"
    return None


class LinkUsageSink(UsageSink):
    """The usage sink, also counting a visitor's turn's calls against its link.

    ``manager`` is the session runner, read at each call: it answers which link a session runs for
    (``link_of``), and its database and configuration price the call. A failure to count is logged and
    never fails the call, which was already made and paid for."""

    def __init__(self, inner: UsageSink, manager: Any) -> None:
        self.inner = inner
        self.manager = manager

    async def record(self, record: UsageRecord) -> None:
        await self.inner.record(record)
        share = self.manager.link_of(record.session_id) if record.session_id else ""
        if not share:
            return
        try:
            await charge(self.manager.db, self.manager.config, share, record)
        except Exception:  # noqa: BLE001
            logger.warning("could not count a call against live link %s", share, exc_info=True)


__all__ = ["STOPPED", "LinkUsageSink", "billing_of", "charge", "exhausted", "quota_account", "spent_of", "weekly_price"]
