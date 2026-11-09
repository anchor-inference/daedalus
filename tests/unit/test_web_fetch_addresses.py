"""WebFetch reading a page as text holds to the addresses a saved file holds to.

The text path once followed redirects by itself and dialled whatever a name resolved to: a page could answer
with a redirect to the router, the cloud's metadata address or this machine's own API, and the agent read the
answer back into its context. Every address is now checked and pinned, and every redirect is judged by the
session's policy again, which is what keeps the rule that the agent reaches its own public address only for a
shared service (``/s/``) or a shared dialog (``/c/``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig
from daedalus.host.policy import Policy
from daedalus.host.services import SessionServices, locator
from daedalus.public_address import OwnDoors
from daedalus.tools.web import web_fetch

PAGE = "<html><head><title>Notes</title></head><body><p>the public page</p></body></html>"
TABLE = {
    "pages.example": "93.184.216.34",
    "inside.example": "10.0.0.5",
    "meta.example": "169.254.169.254",
    "mapped.example": "::ffff:192.168.1.1",
    "127.0.0.1": "127.0.0.1",
    # This installation's own public name, resolving to the home network from inside, as a name behind a
    # router does: its shared links are read all the same, and nothing else under it is.
    "agent.example": "192.168.1.20",
}


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    async def getaddrinfo(_loop: Any, host: str, port: int, **__: Any) -> list[Any]:
        asked.append(host)
        if host not in TABLE:
            raise OSError("no such name")
        address = TABLE[host]
        return [(10 if ":" in address else 2, 1, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return asked


def policy() -> Policy:
    return Policy(own_doors=lambda: OwnDoors(frozenset({"agent.example"}), ("agent.example", 443)))


def session(tmp_path: Path, *, with_policy: bool = True) -> ToolContext:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    manager = SimpleNamespace(config=RuntimeConfig(), **({"policy": lambda base_dir="": policy()} if with_policy else {}))
    sid = f"wf-{tmp_path.name}"
    locator.register(SessionServices(session_id=sid, workspace_dir=workspace, extra={"manager": manager}))
    return ToolContext(tenant_id="daedalus", run_id="r1", session_id=sid, metadata={"tool_call_id": "c1"})


@pytest.fixture(autouse=True)
def _unregister(tmp_path: Path) -> Any:
    yield
    locator.unregister(f"wf-{tmp_path.name}")


@respx.mock
async def test_a_public_page_is_read_at_the_address_that_was_checked(tmp_path: Path, resolver: list[str]) -> None:
    route = respx.get("https://93.184.216.34:443/notes").mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    result = await web_fetch().invoke(session(tmp_path), {"url": "https://pages.example/notes"})
    assert not result.is_error, result.content
    assert "the public page" in result.content and "title: Notes" in result.content
    request = route.calls[0].request
    assert request.headers["host"] == "pages.example" and request.extensions["sni_hostname"] == "pages.example"
    assert resolver == ["pages.example"]


@pytest.mark.parametrize("host", ["inside.example", "meta.example", "mapped.example", "127.0.0.1", "nowhere.example"])
async def test_an_address_off_the_public_internet_is_not_read(tmp_path: Path, resolver: list[str], host: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = await web_fetch().invoke(session(tmp_path), {"url": f"http://{host}/status"})
        assert not mock.calls
    assert result.is_error and ("public internet" in result.content or "does not resolve" in result.content), result.content


@respx.mock
async def test_a_redirect_inside_the_network_is_not_followed(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/go").mock(return_value=httpx.Response(302, headers={"location": "http://meta.example/latest/credentials"}))
    inside = respx.get(url__regex=r"http://169\.254\.169\.254.*").mock(return_value=httpx.Response(200, text="secret"))
    result = await web_fetch().invoke(session(tmp_path), {"url": "https://pages.example/go"})
    assert result.is_error and "public internet" in result.content and not inside.called
    assert "secret" not in result.content


@respx.mock
async def test_a_public_redirect_is_followed_and_named_where_it_ended(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/old").mock(return_value=httpx.Response(301, headers={"location": "/new"}))
    respx.get("https://93.184.216.34:443/new").mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    result = await web_fetch().invoke(session(tmp_path), {"url": "https://pages.example/old"})
    assert not result.is_error and "the public page" in result.content
    assert "HTTP 200 https://pages.example/new" in result.content


@respx.mock
async def test_too_many_redirects_end_the_fetch(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/loop").mock(return_value=httpx.Response(302, headers={"location": "/loop"}))
    result = await web_fetch().invoke(session(tmp_path), {"url": "https://pages.example/loop"})
    assert result.is_error and "redirects" in result.content


@respx.mock
async def test_a_redirect_to_the_installations_own_api_is_refused_by_the_policy(tmp_path: Path, resolver: list[str]) -> None:
    """The name is this installation's, so the address being private or public decides nothing: the policy does."""
    respx.get("https://93.184.216.34:443/go").mock(return_value=httpx.Response(302, headers={"location": "https://agent.example/api/sessions"}))
    api = respx.get(url__regex=r"https://192\.168\.1\.20.*").mock(return_value=httpx.Response(200, text="the operator's sessions"))
    result = await web_fetch().invoke(session(tmp_path), {"url": "https://pages.example/go"})
    assert result.is_error and "redirected to https://agent.example/api/sessions" in result.content and not api.called


@pytest.mark.parametrize("path", ["/s/shared-service/", "/c/a-dialog"])
async def test_its_own_shared_links_are_read_though_its_name_resolves_inside(tmp_path: Path, resolver: list[str], path: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        own = mock.get(f"https://192.168.1.20:443{path}").mock(return_value=httpx.Response(200, text="shared", headers={"content-type": "text/plain"}))
        result = await web_fetch().invoke(session(tmp_path), {"url": f"https://agent.example{path}"})
    assert not result.is_error and "shared" in result.content and own.called


async def test_anything_else_under_its_own_name_still_has_to_be_public(tmp_path: Path, resolver: list[str]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = await web_fetch().invoke(session(tmp_path), {"url": "https://agent.example/../api/sessions"})
        assert not mock.calls
    assert result.is_error and "public internet" in result.content
