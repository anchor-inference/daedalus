"""MCP servers added, changed, signed in to and removed while the host runs."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from protocore.runtime.tool_registry import ToolRegistry

from daedalus.config import McpOAuthConfig, McpServerConfig, RuntimeConfig
from daedalus.extensions import api_mcp, mcp_admin
from daedalus.mcp.manager import McpManager
from daedalus.mcp.oauth import MCPOAuthClient, mcp_callback_url

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_echo_server.py"
ECHO = {"transport": "stdio", "command": sys.executable, "args": [str(SERVER)], "description": "echo tools"}


class FakeApp:
    """What the admin and the routes use of the application: its configuration, a save that reaches
    the running manager the way ``Application.save_config`` does, and the manager."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = RuntimeConfig()
        self.settings = SimpleNamespace(miniapp_public_url="https://agent.example")
        mcp = McpManager({}, ToolRegistry(), token_dir=tmp_path / "mcp",
                         redirect_default=lambda: mcp_callback_url(self.settings.miniapp_public_url))
        self.manager = SimpleNamespace(mcp=mcp)

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.manager.mcp.reload(config.mcp.servers)


async def test_a_server_added_and_removed_live_starts_and_stops_its_process(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    saved = await mcp_admin.put(app, "echo", ECHO)
    assert saved == {"name": "echo", "added": True}
    probe = await mcp_admin.probe(app, "echo")
    assert probe["state"] == "ready" and len(probe["tools"]) == 2
    connection = app.manager.mcp._connections["echo"]
    (tmp_path / "mcp").mkdir(exist_ok=True)
    (tmp_path / "mcp" / "echo.json").write_text("{}")
    assert await mcp_admin.remove(app, "echo") is True
    await asyncio.gather(*app.manager.mcp._retiring)
    # The process is stopped and forgotten, not only its tools withdrawn, and its sign-in goes too.
    assert "echo" not in app.manager.mcp._connections and connection.session is None
    assert not (tmp_path / "mcp" / "echo.json").exists()
    assert app.manager.mcp.all_tool_names() == set()
    assert await mcp_admin.remove(app, "echo") is False
    await app.manager.mcp.close()


def test_a_server_is_checked_before_it_is_saved() -> None:
    with pytest.raises(mcp_admin.McpAdminError, match="name"):
        mcp_admin.build("has space", ECHO, None)
    with pytest.raises(mcp_admin.McpAdminError, match="command"):
        mcp_admin.build("empty", {"transport": "stdio"}, None)
    with pytest.raises(mcp_admin.McpAdminError, match="URL"):
        mcp_admin.build("remote", {"transport": "http", "url": "mcp.example.com"}, None)
    with pytest.raises(mcp_admin.McpAdminError, match="HTTP server"):
        mcp_admin.build("odd", {**ECHO, "oauth": {}}, None)
    # A secret sent back as the mask keeps the stored value; the app never holds the real one.
    stored = McpServerConfig(transport="http", url="https://mcp.example.com/mcp", headers={"Authorization": "Bearer real"},
                             oauth=McpOAuthConfig(client_id="app", client_secret="hidden"))
    built = mcp_admin.build("remote", {"transport": "http", "url": "https://mcp.example.com/mcp",
                                       "headers": {"Authorization": "•••"},
                                       "oauth": {"client_id": "app", "client_secret": "•••"}}, stored)
    assert built.headers == {"Authorization": "Bearer real"} and built.oauth is not None and built.oauth.client_secret == "hidden"


def provider(requests: list[str]) -> httpx.MockTransport:
    """An MCP server whose authorization server lives elsewhere and says so (RFC 9728)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com/tenant"]})
        if url == "https://auth.example.com/.well-known/oauth-authorization-server/tenant":
            return httpx.Response(200, json={"issuer": "https://auth.example.com/tenant",
                                             "authorization_endpoint": "https://auth.example.com/authorize",
                                             "token_endpoint": "https://auth.example.com/token",
                                             "registration_endpoint": "https://auth.example.com/register",
                                             "code_challenge_methods_supported": ["S256"]})
        if url == "https://auth.example.com/register":
            body = json.loads(request.content)
            return httpx.Response(201, json={"client_id": f"client-for-{body['redirect_uris'][0]}"})
        if url == "https://auth.example.com/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["redirect_uri"] == "https://agent.example/api/mcp/oauth/callback"
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_discovery_follows_the_servers_own_pointer_and_registers_for_the_apps_callback(tmp_path: Path) -> None:
    requests: list[str] = []
    http = httpx.AsyncClient(transport=provider(requests))
    client = MCPOAuthClient("remote", McpOAuthConfig(), "https://mcp.example.com/mcp", tmp_path / "remote.json", http,
                            redirect_default="https://agent.example/api/mcp/oauth/callback")
    url = await client.authorization_url()
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://auth.example.com/authorize?")
    assert query["redirect_uri"] == ["https://agent.example/api/mcp/oauth/callback"]
    assert query["client_id"] == ["client-for-https://agent.example/api/mcp/oauth/callback"]
    # A registration made for another redirect address is made again rather than refused later.
    client.store.set("client_id", "client-for-loopback")
    client.store.pop("client_redirect")
    # A renewal keeps the client its refresh token belongs to; only a new sign-in registers again.
    assert await client.register_client() == "client-for-loopback"
    assert await client.register_client(for_new_link=True) == "client-for-https://agent.example/api/mcp/oauth/callback"
    assert sum(1 for item in requests if item.endswith("/register")) == 2
    await http.aclose()


async def test_a_hand_registered_client_skips_registration_and_sends_its_secret(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={"authorization_endpoint": "https://gh.example/authorize", "token_endpoint": "https://gh.example/token"})
        if url == "https://gh.example/token":
            seen.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPOAuthClient("gh", McpOAuthConfig(client_id="app-id", client_secret="app-secret"), "https://gh.example/mcp",
                            tmp_path / "gh.json", http, redirect_default="https://agent.example/api/mcp/oauth/callback")
    url = await client.authorization_url()
    state = parse_qs(urlparse(url).query)["state"][0]
    await client.finish_authorization(f"https://agent.example/api/mcp/oauth/callback?code=c&state={state}")
    assert seen["client_id"] == "app-id" and seen["client_secret"] == "app-secret"
    await http.aclose()


async def test_the_sign_in_finishes_on_the_apps_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApp(tmp_path)
    await mcp_admin.put(app, "remote", {"transport": "http", "url": "https://mcp.example.com/mcp", "oauth": {}})
    requests: list[str] = []
    # The server's OAuth client, as the manager would build it, talking to the fake provider.
    app.manager.mcp._oauth_clients["remote"] = MCPOAuthClient(
        "remote", app.config.mcp.servers["remote"].oauth, "https://mcp.example.com/mcp", tmp_path / "mcp" / "remote.json",
        httpx.AsyncClient(transport=provider(requests)), redirect_default="https://agent.example/api/mcp/oauth/callback")

    async def no_probe(app: object, name: str) -> dict:
        return {"name": name, "state": "ready", "error": None, "tools": []}

    monkeypatch.setattr(mcp_admin, "probe", no_probe)
    api = FastAPI()
    api_mcp.register(api, app, lambda: {"via": "cookie", "user_id": 1})  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://agent.example") as browser:
        listed = (await browser.get("/api/mcp/servers")).json()
        assert listed["servers"][0]["oauth"]["linked"] is False
        assert listed["callback"] == "https://agent.example/api/mcp/oauth/callback"
        begun = (await browser.post("/api/mcp/servers/remote/oauth/begin")).json()
        state = parse_qs(urlparse(begun["auth_url"]).query)["state"][0]
        forged = await browser.get("/api/mcp/oauth/callback", params={"code": "c", "state": "not-it"})
        assert forged.status_code == 400
        back = await browser.get("/api/mcp/oauth/callback", params={"code": "c", "state": state})
        assert back.status_code == 303 and back.headers["location"] == "/app/settings/tools?mcp=remote&mcp_linked=1"
        assert (await browser.get("/api/mcp/servers")).json()["servers"][0]["oauth"]["linked"] is True
        removed = await browser.delete("/api/mcp/servers/remote")
        assert removed.status_code == 200 and removed.json()["servers"] == []
        assert (await browser.delete("/api/mcp/servers/remote")).status_code == 404
    await asyncio.gather(*app.manager.mcp._retiring)
    assert not (tmp_path / "mcp" / "remote.json").exists()
    await app.manager.mcp.close()


async def test_the_agent_adds_and_removes_a_server_and_is_told_what_came_of_it(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    said = await mcp_admin.agent_service(app, "add", session_id="s1", name="echo", spec=ECHO)  # type: ignore[arg-type]
    assert said.startswith("added echo; it answers with 2 tools") and "McpEnable(server='echo')" in said
    broken = await mcp_admin.agent_service(app, "add", session_id="s1", name="broken",  # type: ignore[arg-type]
                                           spec={"transport": "stdio", "command": str(tmp_path / "missing")})
    assert broken.startswith("added broken, but it does not answer yet")
    assert (await mcp_admin.agent_service(app, "remove", session_id="s1", name="broken")).startswith("removed broken")  # type: ignore[arg-type]
    with pytest.raises(mcp_admin.McpAdminError, match="no MCP server"):
        await mcp_admin.agent_service(app, "remove", session_id="s1", name="broken")  # type: ignore[arg-type]
    assert sorted(app.config.mcp.servers) == ["echo"]
    await app.manager.mcp.close()


async def test_a_server_changed_and_checked_at_once_stays_connected(tmp_path: Path) -> None:
    """The retirement of the old config runs after the save; the check right after it must not lose
    the connection it just built to that retirement."""
    app = FakeApp(tmp_path)
    await mcp_admin.put(app, "echo", ECHO)
    assert (await mcp_admin.probe(app, "echo"))["state"] == "ready"
    await mcp_admin.put(app, "echo", {**ECHO, "description": "changed"})
    assert (await mcp_admin.probe(app, "echo"))["state"] == "ready"
    await asyncio.gather(*app.manager.mcp._retiring)
    assert app.manager.mcp.status()[0]["state"] == "ready"
    await app.manager.mcp.close()
