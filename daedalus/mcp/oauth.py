"""OAuth 2.1 client support for remote (Streamable HTTP) MCP servers.

Implements the pieces the ``mcp`` SDK leaves to the host: RFC 8414 discovery,
RFC 7591 dynamic client registration, RFC 7636 PKCE, the authorization-code
exchange and refresh-token renewal.

The authorization step is interactive by design: a human owner must open the
authorization URL and approve/link the account on the server's own page. There
is no device flow and no shared secret here, so the client only ever holds
short-lived access tokens plus a refresh token, stored on the state volume with
mode 0600. Nothing is written to chat, logs or tool arguments.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from daedalus.config import McpOAuthConfig

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8931/callback"
_TOKEN_GRACE_SECONDS = 60.0
_REFRESH_MARGIN_SECONDS = 30
"""A token this close to its (already grace-adjusted) expiry is renewed ahead of use, by every check alike."""

_TOKEN_KEYS = ("access_token", "refresh_token", "expires_at", "scope")


class OAuthError(Exception):
    """Transport-level OAuth failure (server error, bad metadata, ...)."""


class NeedsAuthorization(OAuthError):
    """No usable tokens: the owner must complete the interactive link first."""


class PendingMismatch(OAuthError):
    """The pasted redirect URL does not match the pending authorization request."""


@dataclass
class OAuthDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    code_challenge_methods: list[str]


class OAuthTokenStore:
    """Small atomic JSON file holding client registration + tokens for one server."""

    def __init__(self, path: Any) -> None:
        self.path = path
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        if self._data:
            return self._data
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically and fsync before the rename so a crash cannot leave a
        # truncated token file (a lost refresh token means a manual re-link).
        data = json.dumps(self._data, indent=2).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover — the temp file was already renamed
                pass
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover — filesystems without chmod
            pass
        self._data = dict(self._data)

    def reload(self) -> dict[str, Any]:
        """The file as it is now, not as this copy last read or wrote it."""
        self._data = {}
        return self.load()

    def get(self, key: str) -> Any:
        return self.load().get(key)

    def set(self, key: str, value: Any) -> None:
        self.load()[key] = value
        self.save()

    def pop(self, key: str) -> Any:
        value = self.load().pop(key, None)
        if value is not None:
            self.save()
        return value

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        self._data = {}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


CALLBACK_PATH = "/api/mcp/oauth/callback"


def mcp_callback_url(public_url: str) -> str:
    """The app's own OAuth callback when it has a public address; empty means the loopback default.

    A server sending the owner back there finishes the link by itself, where the loopback address
    could not load and its URL had to be copied out of the address bar and pasted to the agent."""
    parsed = urlparse(public_url or "")
    return f"{parsed.scheme}://{parsed.netloc}{CALLBACK_PATH}" if parsed.scheme and parsed.netloc else ""


def _safe_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise OAuthError(f"cannot derive origin from url {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


class MCPOAuthClient:
    """One OAuth client per MCP server; all HTTP goes through the injected httpx client."""

    def __init__(self, server: str, config: McpOAuthConfig, server_url: str, token_path: Any, http: httpx.AsyncClient,
                 redirect_default: str = "") -> None:
        self.server = server
        self.config = config
        self.server_url = server_url
        self.redirect_default = redirect_default
        self.store = OAuthTokenStore(token_path)
        self.http = http
        self._discovery: OAuthDiscovery | None = None
        self._refresh_lock = asyncio.Lock()
        self._renewal: asyncio.Task[None] | None = None

    # -- metadata ------------------------------------------------------------------

    @property
    def scopes(self) -> list[str]:
        return list(self.config.scopes)

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)

    @property
    def redirect_uri(self) -> str:
        return self.config.redirect_uri or self.redirect_default or DEFAULT_REDIRECT_URI

    async def _authorization_server(self) -> str:
        """The issuer: configured, else named by the MCP server itself (RFC 9728), else its origin.

        Most hosted MCP servers keep their authorization server elsewhere and say where in
        ``/.well-known/oauth-protected-resource``; looking only at the MCP server's own origin
        found no metadata there and the link could not even begin."""
        if self.config.issuer:
            return self.config.issuer.rstrip("/")
        origin = _safe_origin(self.server_url)
        path = urlparse(self.server_url).path.rstrip("/")
        candidates = ([f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []) + [f"{origin}/.well-known/oauth-protected-resource"]
        for url in candidates:
            doc = await self._try_json(url)
            servers = doc.get("authorization_servers") if doc else None
            if isinstance(servers, list) and servers:
                return str(servers[0]).rstrip("/")
        return origin

    async def discovery(self) -> OAuthDiscovery:
        if self._discovery is None:
            issuer = await self._authorization_server()
            parsed = urlparse(issuer)
            base, path = f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip("/")
            # RFC 8414 puts the well-known part between the host and an issuer's path; OpenID
            # Connect appends it. Servers answer one or the other.
            candidates = [f"{base}/.well-known/oauth-authorization-server{path}", f"{base}/.well-known/openid-configuration{path}"]
            if path:
                candidates.append(f"{issuer}/.well-known/openid-configuration")
            doc: dict[str, Any] = {}
            for url in candidates:
                doc = await self._try_json(url) or {}
                if doc.get("authorization_endpoint") and doc.get("token_endpoint"):
                    break
            auth = doc.get("authorization_endpoint")
            token = doc.get("token_endpoint")
            reg = doc.get("registration_endpoint")
            if not auth or not token:
                raise OAuthError(f"{self.server}: no oauth metadata found for {issuer} (tried {', '.join(candidates)})")
            if not reg and not self.config.client_id:
                raise OAuthError(f"{self.server}: {issuer} does not register clients by itself; register one on its site"
                                 " and give its client id (and secret, if it issued one)")
            self._discovery = OAuthDiscovery(
                issuer=str(doc.get("issuer") or issuer),
                authorization_endpoint=str(auth),
                token_endpoint=str(token),
                registration_endpoint=str(reg or ""),
                code_challenge_methods=[str(m) for m in doc.get("code_challenge_methods_supported") or []],
            )
        return self._discovery

    async def _try_json(self, url: str) -> dict[str, Any] | None:
        """A metadata document, or None when it is not there: discovery tries several places."""
        try:
            response = await self.http.get(url)
        except httpx.HTTPError as exc:
            logger.info("%s: oauth metadata %s unreachable: %s", self.server, url, exc)
            return None
        if response.status_code != 200:
            return None
        try:
            doc = response.json()
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def _client_auth(self, form: dict[str, str]) -> dict[str, str]:
        if self.config.client_secret:
            form["client_secret"] = self.config.client_secret
        return form

    # -- registration (RFC 7591) ----------------------------------------------------

    def client_id(self) -> str | None:
        if self.config.client_id:
            return self.config.client_id
        value = self.store.get("client_id")
        return str(value) if value else None

    async def register_client(self, *, for_new_link: bool = False) -> str:
        """The client id; registered on first use. ``for_new_link`` also registers anew when the
        stored client was made for another redirect address.

        A client is registered for one redirect address. Once the app's own callback took the place
        of the loopback one, a new sign-in with the old client would be refused at the redirect. Only
        a new sign-in registers again: a refresh keeps the client its refresh token was issued to,
        or every linked server would lose its link at the next renewal."""
        if self.config.client_id:
            return self.config.client_id
        existing = self.store.get("client_id")
        # A registration that predates the record was made for the address configured then.
        registered_for = self.store.get("client_redirect") or (self.config.redirect_uri or DEFAULT_REDIRECT_URI)
        if existing and (not for_new_link or registered_for == self.redirect_uri):
            return str(existing)
        doc = await self.discovery()
        metadata = {
            "client_name": self.config.client_name or f"{self.server} MCP client",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": self.scope_string,
        }
        try:
            response = await self.http.post(doc.registration_endpoint, json=metadata)
        except httpx.HTTPError as exc:
            raise OAuthError(f"{self.server}: dynamic client registration failed: {exc}") from exc
        if response.status_code not in (200, 201):
            raise OAuthError(
                f"{self.server}: dynamic client registration rejected ({response.status_code}): {_brief(response.text)}"
            )
        registered = response.json()
        client_id = registered.get("client_id")
        if not client_id:
            raise OAuthError(f"{self.server}: registration response has no client_id: {_brief(response.text)}")
        self.store.set("client_id", str(client_id))
        self.store.set("client_redirect", self.redirect_uri)
        return str(client_id)

    # -- interactive authorization (RFC 7636) ---------------------------------------

    async def authorization_url(self) -> str:
        """Register the client and return the URL the owner must open to link the account."""
        doc = await self.discovery()
        client_id = await self.register_client(for_new_link=True)
        if self.scopes and "S256" not in doc.code_challenge_methods:
            raise OAuthError(f"{self.server}: authorization server does not support PKCE S256")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        self.store.set(
            "pending",
            {"state": state, "code_verifier": verifier, "auth_url": "", "created_at": int(time.time())},
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope_string,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        url = f"{doc.authorization_endpoint}?{query}"
        pending = self.store.get("pending") or {}
        pending["auth_url"] = url
        self.store.set("pending", pending)
        return url

    @staticmethod
    def extract_code(redirect_url_or_code: str) -> dict[str, str]:
        """Pull ``code``/``state`` out of the pasted redirect URL (or a bare code)."""
        if "?" not in redirect_url_or_code and "=" not in redirect_url_or_code:
            return {"code": redirect_url_or_code, "state": ""}
        query = parse_qs(urlparse(redirect_url_or_code).query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            raise OAuthError("no 'code' found in the pasted redirect URL")
        return {"code": code, "state": state}

    async def finish_authorization(self, redirect_url_or_code: str) -> dict[str, Any]:
        """Exchange the authorization code for tokens and store them."""
        doc = await self.discovery()
        client_id = await self.register_client()
        pending = self.store.get("pending")
        if not pending:
            raise PendingMismatch(
                f"{self.server}: no pending authorization. Run McpOAuthBegin again and paste the URL it produced."
            )
        extracted = self.extract_code(redirect_url_or_code)
        if extracted["state"] and pending.get("state") and extracted["state"] != pending.get("state"):
            raise PendingMismatch(
                f"{self.server}: state mismatch — the pasted URL does not match the last McpOAuthBegin. Run it again."
            )
        verifier = pending.get("code_verifier")
        if not verifier:
            raise PendingMismatch(f"{self.server}: pending authorization is missing its code verifier; run McpOAuthBegin again.")
        form = {
            "grant_type": "authorization_code",
            "code": extracted["code"],
            "redirect_uri": self.redirect_uri,
            "client_id": client_id,
            "code_verifier": str(verifier),
        }
        try:
            response = await self.http.post(doc.token_endpoint, data=self._client_auth(form))
        except httpx.HTTPError as exc:
            raise OAuthError(f"{self.server}: token request failed: {exc}") from exc
        if response.status_code != 200:
            raise OAuthError(f"{self.server}: token exchange failed ({response.status_code}): {_brief(response.text)}")
        tokens = response.json()
        self.store.pop("pending")
        self._save_tokens(tokens)
        return {"scope": tokens.get("scope", self.scope_string), "expires_in": tokens.get("expires_in")}

    # -- tokens ---------------------------------------------------------------------

    def _save_tokens(self, tokens: dict[str, Any], *, preserve_refresh: bool = False) -> None:
        expires_in = tokens.get("expires_in")
        expires_at = int(time.time()) + int(expires_in) - int(_TOKEN_GRACE_SECONDS) if expires_in else None
        refresh_token = tokens.get("refresh_token")
        if refresh_token is None and preserve_refresh:
            # RFC 6749 §5.1: a refresh response may omit refresh_token, meaning the
            # previously issued one stays valid. Keep it instead of wiping the link.
            refresh_token = self._tokens().get("refresh_token")
        saved = {
            "access_token": tokens.get("access_token"),
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": tokens.get("scope", self.scope_string),
        }
        self.store.set("tokens", saved)

    def _tokens(self) -> dict[str, Any]:
        tokens = self.store.get("tokens") or {}
        return {key: tokens.get(key) for key in _TOKEN_KEYS}

    @staticmethod
    def _usable(tokens: dict[str, Any]) -> bool:
        """An access token that will still be valid after the refresh margin."""
        if not tokens.get("access_token") or not tokens.get("expires_at"):
            return False
        try:
            return int(tokens["expires_at"]) - _REFRESH_MARGIN_SECONDS > time.time()
        except (TypeError, ValueError):
            return False

    def needs_refresh(self) -> bool:
        """True when the stored access token is missing, expired or about to expire and a refresh token exists.

        The MCP connection asks before a call, so an expiring token is renewed ahead of a 401
        that a remote may report opaquely. A server that was never linked (no refresh token)
        is not a refresh case: its calls fail the ordinary way with the link instruction.
        """
        tokens = self._tokens()
        return bool(tokens.get("refresh_token")) and not self._usable(tokens)

    async def access_token(self) -> str:
        """Return a usable access token, refreshing first when needed."""
        tokens = self._tokens()
        if self._usable(tokens):
            return str(tokens["access_token"])
        if tokens.get("refresh_token"):
            await self.refresh()
            tokens = self._tokens()
            if tokens.get("access_token"):
                return str(tokens["access_token"])
        raise NeedsAuthorization(
            f"{self.server}: not linked yet. Ask the owner to run McpOAuthBegin and paste back the redirect URL."
        )

    async def refresh(self, *, rejected: str = "") -> None:
        """Renew the access token, once for every caller that asks while a renewal runs.

        ``rejected`` is an access token a server refused: it is renewed even though its expiry is
        still ahead, unless the store already holds another one.

        The renewal is a task of this client's, not of the caller: a caller cancelled after the
        authority rotated the refresh token (the operator stopping the turn, a call's deadline) left
        the new token unwritten, the stored one was dead, and the next renewal was refused for good.
        The caller's wait is shielded, so its cancellation ends only the wait."""
        task = self._renewal
        if task is None or task.done():
            task = self._renewal = asyncio.get_running_loop().create_task(self._renew(rejected), name=f"mcp-oauth-refresh:{self.server}")
        await asyncio.shield(task)

    async def _renew(self, rejected: str) -> None:
        async with self._refresh_lock:
            # Read from the file, not the copy in memory: another client of the same server (the owner
            # linking again, a second copy of this one) may have written a newer token meanwhile, and a
            # renewal over it would spend a refresh token that is no longer the current one.
            self.store.reload()
            tokens = self._tokens()
            # The same margin as needs_refresh(), or a token inside the margin would be checked and
            # never renewed. A token a server refused is renewed whatever its expiry says.
            if self._usable(tokens) and (not rejected or tokens.get("access_token") != rejected):
                return
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                raise NeedsAuthorization(f"{self.server}: no refresh token; link again with McpOAuthBegin.")
            doc = await self.discovery()
            client_id = await self.register_client()
            form = {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "client_id": client_id,
            }
            if self.scope_string:
                form["scope"] = self.scope_string
            try:
                response = await self.http.post(doc.token_endpoint, data=self._client_auth(form))
            except httpx.HTTPError as exc:
                raise OAuthError(f"{self.server}: the token endpoint could not be reached ({exc}); the link is kept, try again shortly") from exc
            if response.status_code != 200:
                if _oauth_error(response) == "invalid_grant":
                    # The one answer that says the refresh token itself is dead (expired, revoked or
                    # already spent). Anything else — a 5xx, a 429, a refusal naming another reason —
                    # once deleted it too, and a 30-second blip of the authority cost the owner a new link.
                    self.store.set("tokens", {})
                    raise NeedsAuthorization(f"{self.server}: the authority refused the refresh token (invalid_grant); link again with McpOAuthBegin.")
                raise OAuthError(f"{self.server}: token refresh failed ({response.status_code}): {_brief(response.text)}; the link is kept, try again shortly")
            self._save_tokens(response.json(), preserve_refresh=True)

    def pending_state(self) -> str:
        """The ``state`` of the authorization waiting for its callback, or empty."""
        pending = self.store.get("pending") or {}
        return str(pending.get("state") or "")

    def linked(self) -> bool:
        tokens = self._tokens()
        return bool(tokens.get("access_token") or tokens.get("refresh_token"))

    def status(self) -> dict[str, Any]:
        tokens = self._tokens()
        return {
            "configured": True,
            "scopes": self.scopes,
            "redirect_uri": self.redirect_uri,
            "client_registered": bool(self.client_id()),
            "linked": self.linked(),
            "expires_at": tokens.get("expires_at"),
        }

    def disconnect(self) -> None:
        self.store.delete()


def _oauth_error(response: httpx.Response) -> str:
    """The ``error`` code of an RFC 6749 error answer, or empty when the body is not one."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error") or "") if isinstance(body, dict) else ""


def _brief(text: str, limit: int = 400) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned[:limit] if len(cleaned) > limit else cleaned
