"""The operator's private sign-in: the agent asks to be signed in, the operator types into a form of the
app, and the browser daemon types it into the agent's page.

The model never holds a credential. ``BrowserLogin`` takes a site and a reason; what it gets back is
where the page ended and a guess at whether the sign-in took. The operator's username, password and
one-time code go from the app's form to :meth:`LoginDesk.submit`, into one ``page.login`` call to the
daemon, and nowhere else: not into the transcript, an event, the audit, the log or a tool result. A
password typed this way is taught to the shared redactor for the life of the process, so a page that
echoes it, or the request log that carried it, reads a placeholder before anyone sees it.

The request is the group's "needs you" with a ``login`` part (``Browsers.handoff``): the group is
paused while the form is open, so the agent cannot act on the page meanwhile, and the request outlives
a restart of the host because it is the group's row. Taking the browser by hand ends it as any request
ends, and the operator can sign in themselves instead.

A sign-in can be saved for its site (``OperatorSecrets.save_login``). A later request for the site asks
the operator whether to use it, once or for the rest of the session; a session they said "always" for
is signed in at once, the agent's call answered with the outcome.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from daedalus.browser.model import Conflict, InvalidRequest, NotFound
from daedalus.security import operator_secrets

if TYPE_CHECKING:
    from daedalus.browser.service import Browsers

logger = logging.getLogger(__name__)

SELECTOR_KEYS = ("username", "password", "otp", "submit")
SELECTOR_MAX = 300
REASON_MAX = 300
VALUE_MAX = 4096
"""A username, a password or a code: past this it is a mistake, refused before the daemon is asked."""
LOGIN_TIMEOUT = 60.0
"""A sign-in waits for up to three pages (address, password, code), ten seconds each, and the outcome."""
USE_SAVED = ("once", "always")
TRUST_KEY = "browser_login_trust"
"""The session metadata that lists the sites whose saved sign-in the operator let it use without asking."""
SIGNED_IN = ("yes", "no", "unknown")

Trusted = Callable[[str], Awaitable[set[str]]]
"""``session id -> sites`` whose saved sign-in the operator let the session use without asking again."""
Trust = Callable[[str, str], Awaitable[None]]


class LoginConflict(Conflict):
    """The request the form answers is over: answered, replaced by a newer one, or ended by the
    operator taking the browser."""


def site_of(url_or_domain: str) -> str:
    """The site a request is for, as the form names it and a saved sign-in is filed: the host without
    ``www.``, lower case."""
    text = url_or_domain.strip()
    host = urlsplit(text if "://" in text else f"https://{text}").hostname or ""
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") and host.count(".") > 1 else host


def on_site(url: str, site: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(site) and (host == site or host.endswith("." + site))


def bare_url(url: str) -> str:
    """An address without its query and fragment: a sign-in's return address carries tokens and the
    username often enough that the agent is told only where the page is."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) if parts.scheme else url


def outcome(site: str, result: dict[str, Any]) -> str:
    """What the agent is told of a sign-in: ``submitted; now at <url>; signed in: yes|no|unknown``, and
    the one thing to do next when the page asks for more. Never a value."""
    signed = str(result.get("signed_in") or "unknown")
    if signed not in SIGNED_IN:
        signed = "unknown"
    filled = [str(f) for f in result.get("filled") or [] if f in ("username", "password", "otp")]
    head = "submitted" if filled else "nothing was typed"
    line = f"Sign-in to {site}: {head}; now at {bare_url(str(result.get('url') or ''))}; signed in: {signed}."
    if result.get("stopped"):
        line += f" It stopped: {str(result['stopped'])[:200]}."
    if result.get("waiting") == "code":
        line += " The page asks for a one-time code: call BrowserLogin again and the operator enters it."
    return line


def check_selectors(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidRequest(f"selectors is an object with any of {', '.join(SELECTOR_KEYS)}, each a CSS selector")
    unknown = sorted(set(raw) - set(SELECTOR_KEYS))
    if unknown:
        raise InvalidRequest(f"selectors takes {', '.join(SELECTOR_KEYS)}, not {', '.join(unknown)}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value is None or not str(value).strip():
            continue
        if len(str(value)) > SELECTOR_MAX:
            raise InvalidRequest(f"a selector is at most {SELECTOR_MAX} characters")
        out[key] = str(value).strip()
    return out


async def _nobody_trusted(_session_id: str) -> set[str]:
    return set()


async def _trust_nowhere(_session_id: str, _site: str) -> None:
    return None


class LoginDesk:
    """Requests for the private sign-in and their answers, for one browser service."""

    def __init__(self, service: Browsers, *, store: Callable[[], operator_secrets.OperatorSecrets | None] = operator_secrets.shared) -> None:
        self.service = service
        self.store = store
        self.trusted: Trusted = _nobody_trusted
        self.trust: Trust = _trust_nowhere
        """Set by the host, which keeps the "always for this session" answers in the session's metadata."""

    # -- the agent's side ------------------------------------------------------------------------

    async def probe(self, group: dict[str, Any], tab_id: str, site: str, selectors: dict[str, str]) -> dict[str, Any]:
        """What sign-in fields the page shows, typing nothing."""
        params: dict[str, Any] = {"tab_id": tab_id, "site": site, "probe": True, "origin": {"actor": "operator"}}
        if selectors:
            params["selectors"] = selectors
        result = await self.service.call(group["id"], "page.login", params, what="looking for the sign-in form", timeout=15.0)
        return result if isinstance(result, dict) else {}

    async def ask(self, group: dict[str, Any], *, session_id: str | None, site: str, url: str, reason: str, selectors: dict[str, str], actor: str) -> str:
        """Ask the operator for a sign-in: the group is paused and the form appears in the app. Returns
        what the agent is told."""
        store = self.store()
        saved = store.saved_login(site) if store is not None else None
        login: dict[str, Any] = {"id": secrets.token_hex(6), "site": site, "saved": saved is not None, "reason": reason[:REASON_MAX]}
        if selectors:
            login["selectors"] = selectors
        if session_id:
            login["session_id"] = session_id
        what = f"sign in to {site}: {reason}"[:500]
        await self.service.handoff(group["id"], "login", what, actor=actor, login=login, url=url)
        return (
            f"Asked the operator to sign in to {site} in a private form of the app; what they type goes straight into the page and "
            "you will not see it. End your turn now: a message comes with the outcome (where the page is, and whether the sign-in "
            "took). Do not act on this browser meanwhile; it is paused for them."
        )

    async def at_once(self, group: dict[str, Any], *, session_id: str | None, site: str, tab_id: str, selectors: dict[str, str], actor: str) -> str | None:
        """Sign in with the saved sign-in when the operator said "always" for this session and site; None
        when they did not, and the operator is to be asked."""
        store = self.store()
        if store is None or not session_id:
            return None
        saved = store.saved_login(site)
        trusted = await self.trusted(session_id)
        if saved is None or not ({saved.site, site} & trusted):
            return None
        result = await self._fill(group, tab_id=tab_id, site=site, username=saved.username or "", password=saved.password or "", otp="", selectors=selectors, actor="operator")
        store.login_used(saved)
        await self._audit(group, actor, site, result, saved=True, how="saved, always for this session")
        return "Signed in with the sign-in the operator saved for this site (they allowed it for this session). " + outcome(site, result)

    # -- the operator's side -----------------------------------------------------------------------

    async def submit(
        self,
        group_id: str,
        login_id: str,
        *,
        username: str = "",
        password: str = "",
        otp: str = "",
        save: bool = False,
        use_saved: str | None = None,
    ) -> dict[str, Any]:
        """The operator's answer from the form. Types it into the page, gives the browser back to the
        agent with the outcome, and answers the app with the same outcome. ``use_saved`` (``once`` or
        ``always``) types the saved sign-in instead of a typed one; a code may come with it."""
        row = await self.service.get_row(group_id)
        login = self._request(row, login_id)
        site = str(login.get("site") or "")
        for name, value in (("username", username), ("password", password), ("code", otp)):
            if len(value) > VALUE_MAX:
                raise InvalidRequest(f"the {name} is longer than {VALUE_MAX} characters")
        store = self.store()
        saved = None
        if use_saved is not None:
            if use_saved not in USE_SAVED:
                raise InvalidRequest(f"use_saved is one of {', '.join(USE_SAVED)}")
            saved = store.saved_login(site) if store is not None else None
            if saved is None:
                raise NotFound(f"no sign-in is saved for {site}")
            username, password = saved.username or "", saved.password or ""
        elif not (username or password or otp):
            raise InvalidRequest("type a username, a password or a code")
        if store is not None:
            # Masked from here on, saved or not: the page, the network log, a command's output.
            store.mask(password, operator_secrets.login_placeholder(site))
        tab = await self._active_tab(group_id)
        selectors = check_selectors(login.get("selectors"))
        result = await self._fill(row, tab_id=tab, site=site, username=username, password=password, otp=otp, selectors=selectors, actor="operator")
        saved_now = False
        if save and use_saved is None and password and store is not None:
            await store.save_login(site, username, password)
            saved_now = True
        if saved is not None and store is not None:
            store.login_used(saved)
        session_id = str(login.get("session_id") or row.get("session_id") or "")
        if use_saved == "always" and session_id:
            await self.trust(session_id, site)
        how = f"saved, {use_saved}" if use_saved else "typed"
        await self._audit(row, "operator", site, result, saved=saved_now, how=how)
        await self.service.give_back(group_id, "[browser] " + outcome(site, result) + " Take a BrowserSnapshot before acting.")
        return {
            "site": site, "url": bare_url(str(result.get("url") or "")), "signed_in": str(result.get("signed_in") or "unknown"),
            "filled": [str(f) for f in result.get("filled") or []], "waiting": str(result.get("waiting") or ""), "saved": saved_now,
        }

    async def decline(self, group_id: str, login_id: str) -> None:
        """The operator will not sign in: the agent gets the browser back and is told so."""
        row = await self.service.get_row(group_id)
        login = self._request(row, login_id)
        site = str(login.get("site") or "")
        await self.service.audit(group_id, row["env"], "operator", "login", {"site": site, "declined": True})
        await self.service.give_back(group_id, f"[browser] The operator declined to sign in to {site}. Do not ask again for this task unless they say so; carry on without the sign-in or tell them what it blocks.")

    # -- shared ------------------------------------------------------------------------------------

    @staticmethod
    def _request(row: dict[str, Any], login_id: str) -> dict[str, Any]:
        try:
            need = json.loads(row.get("needs_json") or "null") or {}
        except ValueError:
            need = {}
        login = need.get("login") if isinstance(need, dict) else None
        if row.get("status") != "open" or not isinstance(login, dict) or login.get("id") != login_id:
            raise LoginConflict("this sign-in request is over: it was answered, the agent asked again, or the browser was taken by hand")
        return login

    async def _active_tab(self, group_id: str) -> str:
        listing = await self.service.call(group_id, "tab.list", {"group_id": group_id}, what="listing the tabs")
        tab = str(listing.get("active_tab") or "") if isinstance(listing, dict) else ""
        if not tab:
            raise NotFound("the browser has no tab to sign in on")
        return tab

    async def _fill(self, group: dict[str, Any], *, tab_id: str, site: str, username: str, password: str, otp: str, selectors: dict[str, str], actor: str) -> dict[str, Any]:
        params: dict[str, Any] = {"tab_id": tab_id, "site": site, "origin": {"actor": actor}}
        # Only what was given: an empty value is no value, and the daemon refuses a call with none.
        for key, value in (("username", username), ("password", password), ("otp", otp)):
            if value:
                params[key] = value
        if selectors:
            params["selectors"] = selectors
        result = await self.service.call(group["id"], "page.login", params, what="signing in", timeout=LOGIN_TIMEOUT)
        return result if isinstance(result, dict) else {}

    async def _audit(self, group: dict[str, Any], actor: str, site: str, result: dict[str, Any], *, saved: bool, how: str) -> None:
        detail = {
            "site": site, "filled": [str(f) for f in result.get("filled") or []], "signed_in": str(result.get("signed_in") or ""),
            "url": bare_url(str(result.get("url") or ""))[:2000], "saved": saved, "how": how,
        }
        try:
            await self.service.audit(group["id"], group["env"], actor, "login", detail)
        except Exception:  # noqa: BLE001 — a failed audit write is logged, not the sign-in's failure
            logger.exception("could not write the sign-in of browser %s to the audit", group["id"])


__all__ = ["TRUST_KEY", "LoginConflict", "LoginDesk", "bare_url", "check_selectors", "on_site", "outcome", "site_of"]
