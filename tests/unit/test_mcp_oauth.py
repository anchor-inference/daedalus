from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from daedalus.config import McpOAuthConfig
from daedalus.mcp.oauth import MCPOAuthClient, NeedsAuthorization, OAuthError, PendingMismatch

ISSUER = "https://board.example"
SCOPES = ["board:read", "board:write"]
REDIRECT = "http://127.0.0.1:8931/callback"


def _make_client(tmp_path: Path) -> tuple[MCPOAuthClient, httpx.AsyncClient]:
    state = {"registrations": 0, "codes": set()}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "registration_endpoint": f"{ISSUER}/oauth/register",
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                },
            )
        if url.endswith("/oauth/register"):
            body = json.loads(request.content)
            assert body["token_endpoint_auth_method"] == "none"
            assert body["redirect_uris"] == [REDIRECT]
            state["registrations"] += 1
            return httpx.Response(
                201,
                json={
                    "client_id": "test-client",
                    "client_secret_expires_at": 0,
                    "redirect_uris": body["redirect_uris"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": body["grant_types"],
                },
            )
        if url.endswith("/oauth/token"):
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("grant_type") == "authorization_code":
                assert form.get("code_verifier"), "PKCE verifier must be sent"
                assert form.get("client_id") == "test-client"
                assert form.get("redirect_uri") == REDIRECT
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at-new",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "rt-new",
                        "scope": " ".join(SCOPES),
                    },
                )
            if form.get("grant_type") == "refresh_token":
                assert form.get("refresh_token"), "refresh token must be sent"
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at-refreshed",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "rt-refreshed",
                        "scope": " ".join(SCOPES),
                    },
                )
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        return httpx.Response(404, text="not found")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ISSUER)
    config = McpOAuthConfig(issuer=ISSUER, scopes=SCOPES, redirect_uri=REDIRECT)
    client = MCPOAuthClient("board", config, f"{ISSUER}/mcp", tmp_path / "board.json", http)
    return client, http


def _state_from_auth_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


async def test_access_token_raises_before_link(tmp_path: Path) -> None:
    client, http = _make_client(tmp_path)
    try:
        with pytest.raises(NeedsAuthorization):
            await client.access_token()
    finally:
        await http.aclose()


async def test_full_link_flow(tmp_path: Path) -> None:
    client, http = _make_client(tmp_path)
    try:
        url = await client.authorization_url()
        query = parse_qs(urlparse(url).query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["test-client"]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["code_challenge"][0]) == 43  # base64url sha256
        assert query["scope"] == [" ".join(SCOPES)]
        assert client.client_id() == "test-client"

        state = query["state"][0]
        redirect = f"{REDIRECT}?code=the-code&state={state}&iss={ISSUER}"
        result = await client.finish_authorization(redirect)
        assert result["expires_in"] == 3600

        token = await client.access_token()
        assert token == "at-new"
        assert client.linked()
        assert (tmp_path / "board.json").stat().st_mode & 0o077 == 0
    finally:
        await http.aclose()


async def test_pending_mismatch_is_rejected(tmp_path: Path) -> None:
    client, http = _make_client(tmp_path)
    try:
        await client.authorization_url()
        with pytest.raises(PendingMismatch):
            await client.finish_authorization(f"{REDIRECT}?code=the-code&state=wrong-state")
    finally:
        await http.aclose()


async def test_finish_without_begin_fails(tmp_path: Path) -> None:
    client, http = _make_client(tmp_path)
    try:
        with pytest.raises(PendingMismatch):
            await client.finish_authorization("code-without-pending")
    finally:
        await http.aclose()


async def test_expired_access_token_refreshes(tmp_path: Path) -> None:
    client, http = _make_client(tmp_path)
    try:
        url = await client.authorization_url()
        state = _state_from_auth_url(url)
        await client.finish_authorization(f"{REDIRECT}?code=the-code&state={state}")
        # Age the stored access token so the next call must refresh.
        client.store.set("tokens", {**client.store.get("tokens"), "expires_at": 1})
        token = await client.access_token()
        assert token == "at-refreshed"
        assert client.store.get("tokens")["refresh_token"] == "rt-refreshed"
    finally:
        await http.aclose()


async def test_rejected_refresh_forces_relink(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    async def refuse(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "registration_endpoint": f"{ISSUER}/oauth/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url.endswith("/oauth/register"):
            return httpx.Response(201, json={"client_id": "test-client"})
        return httpx.Response(400, json={"error": "invalid_grant"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url=ISSUER)
    client.http = http
    try:
        client.store.set("tokens", {"access_token": None, "refresh_token": "rt-dead", "expires_at": 1})
        with pytest.raises(OAuthError):
            await client.access_token()
        assert client.linked() is False
    finally:
        await http.aclose()


async def test_refresh_without_rotation_keeps_stored_refresh_token(tmp_path: Path) -> None:
    """RFC 6749 §5.1: a refresh response may omit refresh_token; the old one stays valid."""
    client, _ = _make_client(tmp_path)
    calls = {"refresh": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "registration_endpoint": f"{ISSUER}/oauth/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url.endswith("/oauth/register"):
            return httpx.Response(201, json={"client_id": "test-client"})
        if url.endswith("/oauth/token"):
            calls["refresh"] += 1
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == "rt-stable"
            return httpx.Response(200, json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600})
        return httpx.Response(404, text="not found")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ISSUER)
    client.http = http
    try:
        client.store.set("tokens", {"access_token": None, "refresh_token": "rt-stable", "expires_at": 1})
        assert await client.access_token() == "at-2"
        stored = client.store.get("tokens")
        assert stored["access_token"] == "at-2"
        assert stored["refresh_token"] == "rt-stable"  # not wiped by the bare response
        assert client.linked()
        # And the link still works on the next expiry.
        client.store.set("tokens", {**stored, "expires_at": 1})
        assert await client.access_token() == "at-2"
        assert calls["refresh"] == 2
    finally:
        await http.aclose()


async def test_concurrent_access_token_refreshes_once(tmp_path: Path) -> None:
    """Two callers racing an expired token must trigger a single refresh."""
    client, _ = _make_client(tmp_path)
    calls = {"refresh": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "registration_endpoint": f"{ISSUER}/oauth/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url.endswith("/oauth/register"):
            return httpx.Response(201, json={"client_id": "test-client"})
        if url.endswith("/oauth/token"):
            calls["refresh"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{calls['refresh']}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": f"rt-{calls['refresh']}",
                },
            )
        return httpx.Response(404, text="not found")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ISSUER)
    client.http = http
    try:
        client.store.set("tokens", {"access_token": None, "refresh_token": "rt-stable", "expires_at": 1})
        first, second = await asyncio.gather(client.access_token(), client.access_token())
        assert first == "at-1" and second == "at-1"
        assert calls["refresh"] == 1
        assert client.store.get("tokens")["refresh_token"] == "rt-1"
    finally:
        await http.aclose()


async def test_needs_refresh_tracks_expiry(tmp_path: Path) -> None:
    """needs_refresh() reports expired/missing/about-to-expire tokens so the MCP
    connection can refresh *before* an opaque 401 kills a call."""
    client, http = _make_client(tmp_path)
    t = int(time.time())
    # Never linked (no refresh token): not a refresh case — the call fails with the link instruction instead.
    client.store.set("tokens", {})
    assert not client.needs_refresh()
    # Valid, far from expiry -> no.
    client.store.set("tokens", {"access_token": "a", "refresh_token": "r", "expires_at": t + 3600})
    assert not client.needs_refresh()
    # Expired -> yes.
    client.store.set("tokens", {"access_token": "a", "refresh_token": "r", "expires_at": t - 10})
    assert client.needs_refresh()
    # Within the skew margin -> yes, and refresh() agrees: it renews instead of deciding the token is still fine.
    client.store.set("tokens", {"access_token": "a", "refresh_token": "rt-old", "expires_at": t + 20})
    assert client.needs_refresh()
    try:
        await client.refresh()
        stored = client.store.get("tokens")
        assert stored["access_token"] != "a" and stored["expires_at"] > t + 3000
        assert not client.needs_refresh()
    finally:
        await http.aclose()
    # Garbage expiry -> yes (treat as unknown).
    client.store.set("tokens", {"access_token": "a", "refresh_token": "r", "expires_at": "never"})
    assert client.needs_refresh()


async def test_config_roundtrip(tmp_path: Path) -> None:
    from daedalus.config import RuntimeConfig

    toml = f"""
    [mcp.servers.board]
    transport = "http"
    url = "{ISSUER}/mcp"
    description = "test board"
    [mcp.servers.board.oauth]
    issuer = "{ISSUER}"
    scopes = ["board:read", "board:write"]
    redirect_uri = "{REDIRECT}"
    """
    import tomllib

    config = RuntimeConfig.model_validate(tomllib.loads(toml))
    server = config.mcp.servers["board"]
    assert server.oauth is not None
    assert server.oauth.scopes == SCOPES
    assert server.oauth.issuer == ISSUER


def _token_endpoint(answer: Any) -> httpx.MockTransport:
    """An authorization server whose token endpoint is ``answer(request)``; discovery and registration as usual."""

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/oauth/authorize", "token_endpoint": f"{ISSUER}/oauth/token",
                "registration_endpoint": f"{ISSUER}/oauth/register", "code_challenge_methods_supported": ["S256"],
            })
        if url.endswith("/oauth/register"):
            return httpx.Response(201, json={"client_id": "test-client"})
        if url.endswith("/oauth/token"):
            return await answer(request)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("refusal", [503, 502, 500, 429, "network"])
async def test_a_transient_refusal_of_a_refresh_keeps_the_link(tmp_path: Path, refusal: Any) -> None:
    # An authority's blip answered 503 during a tool call, and the refresh token was deleted with it:
    # every later call said "not linked yet" until the owner linked the server again by hand.
    client, _ = _make_client(tmp_path)
    answers = [refusal, 200]

    async def answer(request: httpx.Request) -> httpx.Response:
        now = answers.pop(0)
        if now == "network":
            raise httpx.ConnectError("the authority is unreachable", request=request)
        if now != 200:
            return httpx.Response(now, json={"error": "temporarily_unavailable"})
        return httpx.Response(200, json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-2"})

    http = httpx.AsyncClient(transport=_token_endpoint(answer), base_url=ISSUER)
    client.http = http
    try:
        client.store.set("tokens", {"access_token": None, "refresh_token": "rt-1", "expires_at": 1})
        with pytest.raises(OAuthError) as failed:
            await client.access_token()
        assert not isinstance(failed.value, NeedsAuthorization)
        assert client.linked() and client.store.get("tokens")["refresh_token"] == "rt-1"
        assert await client.access_token() == "at-2"
        assert client.store.get("tokens")["refresh_token"] == "rt-2"
    finally:
        await http.aclose()


async def test_a_refresh_whose_caller_is_cancelled_still_keeps_the_rotated_token(tmp_path: Path) -> None:
    # The operator stopped the turn after the authority had rotated the refresh token and before it
    # was written: the stored token was dead, the next refresh got invalid_grant, and the link was gone.
    client, _ = _make_client(tmp_path)
    asked, release = asyncio.Event(), asyncio.Event()

    async def answer(request: httpx.Request) -> httpx.Response:
        asked.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-2"})

    http = httpx.AsyncClient(transport=_token_endpoint(answer), base_url=ISSUER)
    client.http = http
    try:
        client.store.set("tokens", {"access_token": None, "refresh_token": "rt-1", "expires_at": 1})
        caller = asyncio.create_task(client.access_token())
        await asyncio.wait_for(asked.wait(), 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        for _ in range(100):
            if client.store.get("tokens")["refresh_token"] == "rt-2":
                break
            await asyncio.sleep(0.01)
        assert client.store.get("tokens") == {**client.store.get("tokens"), "access_token": "at-2", "refresh_token": "rt-2"}
        assert await client.access_token() == "at-2"
    finally:
        await http.aclose()


async def test_a_rejected_token_is_renewed_even_before_its_expiry_and_a_newer_one_on_disk_is_adopted(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    calls = {"token": 0}

    async def answer(request: httpx.Request) -> httpx.Response:
        calls["token"] += 1
        return httpx.Response(200, json={"access_token": f"at-{calls['token'] + 1}", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-2"})

    http = httpx.AsyncClient(transport=_token_endpoint(answer), base_url=ISSUER)
    client.http = http
    later = int(time.time()) + 3600
    try:
        client.store.set("tokens", {"access_token": "at-1", "refresh_token": "rt-1", "expires_at": later})
        # A server refused at-1 although it had an hour left: refreshing only on expiry kept it, and
        # the reconnect presented the refused token again.
        await client.refresh(rejected="at-1")
        assert calls["token"] == 1 and client.store.get("tokens")["access_token"] == "at-2"
        # Another writer (a second copy of the client, the owner's new link) put a newer token in
        # the file meanwhile: it is taken, not refreshed over.
        other = MCPOAuthClient("board", client.config, f"{ISSUER}/mcp", tmp_path / "board.json", http)
        other.store.set("tokens", {"access_token": "at-elsewhere", "refresh_token": "rt-elsewhere", "expires_at": later})
        await client.refresh(rejected="at-2")
        assert calls["token"] == 1 and await client.access_token() == "at-elsewhere"
    finally:
        await http.aclose()
