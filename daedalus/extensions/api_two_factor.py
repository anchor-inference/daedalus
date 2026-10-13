"""Two-factor sign-in over HTTP: enrolment in Settings → Security, and the code the login page asks for.

A first factor (Telegram's Login Widget, a pairing link) calls :meth:`Gate.admit`. With two-factor
off that is the session cookie, as before. With it on, the browser gets a pending cookie instead:
signed like the session cookie but saying nothing about the owner, good for five minutes and five
codes, and accepted by nothing but ``POST /api/auth/totp/verify``. A right code there swaps it for
the session cookie.

What is deliberately not gated: the Telegram Mini App (its ``tma`` header is Telegram vouching for the
owner on every request, the same account whose bot chat already gives the agent's whole power with no
code), the API token (a credential for scripts and the launcher, which whoever holds it could use to
turn two-factor off anyway), and a pairing link the desktop launcher minted for its own window
(``daedalus auth pair --window``: minting one takes running a command inside the installation).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from daedalus.security import operator_secrets
from daedalus.security.two_factor import Attempts, TwoFactor, TwoFactorError, enrolment_uri, qr_rows

if TYPE_CHECKING:
    from daedalus.app import Application

PENDING_COOKIE = "daedalus_second_factor"
PENDING_TTL = 300
"""Long enough to find the phone and open the app; a first factor older than this is asked for again."""
PENDING_TRIES = 5
"""Wrong codes one pending cookie may carry before it is worthless and the first factor is needed again."""
PENDING_KEPT = 256
"""Pending cookies whose wrong guesses are counted; a flood of first factors cannot grow the table."""


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    """Six digits from the authenticator app, or a recovery code where one is accepted."""


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def pending_cookie_value(key: bytes, nonce: str, *, ttl: int = PENDING_TTL) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"second_factor": nonce, "exp": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(key, payload)}"


def verify_pending_cookie(key: bytes, value: str) -> str | None:
    """The nonce a pending cookie carries, or None when it is forged or expired."""
    payload, _, signature = (value or "").partition(".")
    if not payload or not signature or not hmac.compare_digest(_sign(key, payload), signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
        return None
    nonce = data.get("second_factor")
    return nonce if isinstance(nonce, str) and nonce else None


class Gate:
    """Where a first factor ends: in a session, or in the second-factor step."""

    def __init__(
        self,
        store: Callable[[], TwoFactor],
        *,
        session_secret: Callable[[], Awaitable[bytes]],
        sign_in: Callable[[Response, Request], Awaitable[None]],
        over_https: Callable[[Request], bool],
    ) -> None:
        self._make_store = store
        self._store: TwoFactor | None = None
        self.session_secret = session_secret
        self.sign_in = sign_in
        self.over_https = over_https
        self.attempts = Attempts()
        self._wrong: dict[str, int] = {}

    @property
    def store(self) -> TwoFactor:
        # Made on first use: the routes are built before a stand-in application (the tests' own,
        # the auth tests' without a database) has everything a request will find on it.
        if self._store is None:
            self._store = self._make_store()
        return self._store

    async def _key(self) -> bytes:
        # Its own key, derived from the session secret: a pending cookie and a session cookie can
        # never be confused for each other, and signing everyone out voids the pending ones too.
        return hmac.new(await self.session_secret(), b"second factor", hashlib.sha256).digest()

    async def admit(self, response: Response, request: Request, *, exempt: bool = False) -> bool:
        """Finish a first factor: True when the browser is now signed in, False when it must give a code."""
        if exempt or not await self.store.enabled():
            await self.sign_in(response, request)
            return True
        nonce = secrets.token_urlsafe(12)
        response.set_cookie(
            PENDING_COOKIE, pending_cookie_value(await self._key(), nonce), max_age=PENDING_TTL,
            httponly=True, secure=self.over_https(request), samesite="lax", path="/",
        )
        return False

    async def pending(self, request: Request) -> str | None:
        """The nonce of a live pending cookie this request carries, unless it has used up its tries."""
        nonce = verify_pending_cookie(await self._key(), request.cookies.get(PENDING_COOKIE, ""))
        if nonce is None or self._wrong.get(nonce, 0) >= PENDING_TRIES:
            return None
        return nonce

    def throttle(self) -> None:
        wait = self.attempts.wait()
        if wait:
            raise HTTPException(429, f"too many wrong codes; try again in {wait} seconds", headers={"Retry-After": str(wait)})

    def wrong(self, nonce: str | None = None) -> HTTPException:
        """Count a wrong code and answer it."""
        self.attempts.failed()
        if nonce is not None:
            while len(self._wrong) >= PENDING_KEPT and nonce not in self._wrong:
                self._wrong.pop(next(iter(self._wrong)))
            self._wrong[nonce] = self._wrong.get(nonce, 0) + 1
            if self._wrong[nonce] >= PENDING_TRIES:
                return HTTPException(401, "too many wrong codes for this sign-in; sign in again from the start")
        return HTTPException(400, "that code is not right; check the clock on your phone, or use a recovery code")

    async def checked(self, code: str, nonce: str | None = None) -> str:
        """Spend a code or raise: the throttle first, then the store."""
        self.throttle()
        try:
            kind = await self.store.check(code)
        except TwoFactorError as exc:
            raise HTTPException(409, str(exc)) from exc
        if kind is None:
            raise self.wrong(nonce)
        self.attempts.clear()
        if nonce is not None:
            self._wrong.pop(nonce, None)
        return kind


def register(
    api: FastAPI,
    app: Application,
    auth: Callable[..., Any],
    *,
    session_secret: Callable[[], Awaitable[bytes]],
    sign_in: Callable[[Response, Request], Awaitable[None]],
    over_https: Callable[[Request], bool],
) -> Gate:
    settings = app.settings
    gate = Gate(
        lambda: TwoFactor(app.db, lambda: operator_secrets.load_key(settings.secrets_dir / operator_secrets.KEY_FILE)),
        session_secret=session_secret, sign_in=sign_in, over_https=over_https,
    )

    def label() -> str:
        # What the authenticator app lists the entry as, beside the issuer: the public address when
        # there is one, so two installations do not read the same.
        return urlparse(settings.miniapp_public_url).hostname or "operator"

    async def status() -> dict[str, Any]:
        return (await gate.store.status()).as_dict()

    @api.get("/api/auth/totp")
    async def totp_status(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await status()

    @api.post("/api/auth/totp/setup")
    async def totp_setup(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A fresh secret to scan or type. Nothing is switched on until a code from it comes back."""
        try:
            secret = await gate.store.begin()
        except TwoFactorError as exc:
            raise HTTPException(409, str(exc)) from exc
        uri = enrolment_uri(secret, label())
        return {"secret": secret, "uri": uri, "qr": qr_rows(uri), "label": label()}

    @api.post("/api/auth/totp/enable")
    async def totp_enable(body: CodeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The first code from the new secret switches two-factor on and answers the recovery codes, once."""
        gate.throttle()
        try:
            codes = await gate.store.confirm(body.code)
        except TwoFactorError as exc:
            raise HTTPException(409, str(exc)) from exc
        if codes is None:
            raise gate.wrong()
        gate.attempts.clear()
        return {**await status(), "recovery_codes": codes}

    @api.post("/api/auth/totp/disable")
    async def totp_disable(body: CodeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Off, with a current code or a recovery code: a signed-in browser alone is not enough."""
        await gate.checked(body.code)
        await gate.store.disable()
        return await status()

    @api.post("/api/auth/totp/recovery")
    async def totp_recovery(body: CodeBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A new set of recovery codes for a current code; the earlier set stops working."""
        await gate.checked(body.code)
        codes = await gate.store.regenerate()
        return {**await status(), "recovery_codes": codes}

    @api.post("/api/auth/totp/verify")
    async def totp_verify(body: CodeBody, request: Request, response: JSONResponse) -> dict[str, Any]:
        """The login page's second step: a pending cookie and a right code make a session."""
        nonce = await gate.pending(request)
        if nonce is None:
            raise HTTPException(401, "sign in first; the code is asked for after that")
        kind = await gate.checked(body.code, nonce)
        await sign_in(response, request)
        response.delete_cookie(PENDING_COOKIE, path="/")
        return {"ok": True, "user_id": settings.owner_user_id, "with": kind, "recovery_left": (await gate.store.status()).recovery_left}

    return gate


__all__ = ["PENDING_COOKIE", "CodeBody", "Gate", "pending_cookie_value", "register", "verify_pending_cookie"]
