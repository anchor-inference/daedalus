"""The key proxy's listener for the guest door: one kind of call on chosen upstreams, and nothing else."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))


def load() -> Any:
    spec = importlib.util.spec_from_file_location("guest_proxy_under_test", KEYPROXY_DIR / "proxy.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


GOOD = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100, "stream": False}


@pytest.fixture
async def guest(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "acme,grok,claude")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_ACME", "https://upstream.example.test/v1")
    monkeypatch.setenv("KEYPROXY_KEY_ACME", "the-key")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_OTHER", "https://other.example.test/v1")
    monkeypatch.setenv("KEYPROXY_KEY_OTHER", "other-key")
    proxy = load()
    proxy.AGENT_API = "http://agent.example.test"
    reported: list[dict[str, Any]] = []
    upstream: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    async def noted(_client: Any, provider: str, model: str, *_: Any) -> None:
        reported.append({"provider": provider, "model": model})

    monkeypatch.setattr(proxy, "report_direct_usage", noted)
    app = web.Application()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app.router.add_route("*", "/{upstream}/{rest:.*}", proxy.handle_guest)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, upstream, reported
    await client.close()


async def test_a_chat_completion_on_an_allowed_upstream_passes_with_the_key_injected_and_is_booked(guest: Any) -> None:
    client, upstream, reported = guest
    answer = await client.post("/acme/v1/chat/completions", json=GOOD)
    assert answer.status == 200 and json.loads(await answer.read())["choices"]
    assert upstream[0].headers["authorization"] == "Bearer the-key" and json.loads(upstream[0].content) == GOOD
    import asyncio

    await asyncio.sleep(0)
    assert reported == [{"provider": "acme", "model": "m"}]


async def test_the_metered_header_cannot_hide_a_call_from_the_ledger(guest: Any) -> None:
    client, upstream, reported = guest
    await client.post("/acme/v1/chat/completions", json=GOOD, headers={"x-daedalus-metered": "1", "authorization": "Bearer x", "x-daedalus-token": "y"})
    import asyncio

    await asyncio.sleep(0)
    assert reported, "the header was stripped, so the call is reported"
    assert "x-daedalus-metered" not in upstream[0].headers and "x-daedalus-token" not in upstream[0].headers


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/acme/v1/models"), ("POST", "/acme/v1/embeddings"), ("POST", "/other/v1/chat/completions"), ("GET", "/acme/v1/chat/completions"), ("PUT", "/acme/v1/chat/completions"),
    ("GET", "/subscriptions/usage"), ("GET", "/keys"), ("POST", "/grok/v1/chat/completions"), ("POST", "/claude/v1/chat/completions"), ("POST", "/acme/../other/v1/chat/completions"),
    ("POST", "/acme/v1/chat/completions/extra"),
])
async def test_anything_else_is_refused_and_reaches_no_upstream(guest: Any, method: str, path: str) -> None:
    client, upstream, _ = guest
    answer = await client.request(method, path, json=GOOD)
    assert answer.status in (403, 404) and upstream == []


@pytest.mark.parametrize("body", [{**GOOD, "tools": []}, {**GOOD, "stream": True}, {**GOOD, "max_tokens": 100_000}, {k: v for k, v in GOOD.items() if k != "max_tokens"}, ["not", "an", "object"]])
async def test_a_call_that_is_not_a_stand_ins_is_refused(guest: Any, body: Any) -> None:
    client, upstream, _ = guest
    assert (await client.post("/acme/v1/chat/completions", json=body)).status == 403 and upstream == []


async def test_nothing_is_served_with_no_upstreams_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYPROXY_GUEST_UPSTREAMS", raising=False)
    assert load().guest_upstreams() == frozenset()


async def test_the_listener_opens_only_when_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = load()
    monkeypatch.delenv("KEYPROXY_GUEST_SOCKET", raising=False)
    app = web.Application()
    app["client"] = None
    await proxy.start_guest_listener(app)
    assert "guest_runner" not in app
    path = tmp_path / "guest.sock"
    monkeypatch.setenv("KEYPROXY_GUEST_SOCKET", str(path))
    await proxy.start_guest_listener(app)
    assert path.is_socket() and path.stat().st_mode & 0o777 == 0o666
    await proxy.stop_guest_listener(app)


@pytest.mark.parametrize("path", ["/acme/../other/chat/completions", "/acme/./chat/completions", "/acme/v1/../../chat/completions", "/acme/.hidden/chat/completions"])
def test_a_dot_segment_is_refused_because_it_would_leave_the_upstreams_root(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """``request.path`` is decoded, so ``%2e%2e`` arrives here as ``..``. On the real socket the first
    version sent that on with the key attached; a client library normalises it away before a mock sees it."""
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "acme")
    assert load().guest_refusal(path, "POST", json.dumps(GOOD).encode())


def test_a_segment_may_still_hold_a_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "acme")
    assert load().guest_refusal("/acme/v4.1/chat/completions", "POST", json.dumps(GOOD).encode()) == ""


def test_the_bill_an_upstream_states_is_reported_and_an_absent_one_is_not_invented() -> None:
    proxy = load()
    usage = {"prompt_tokens": 2, "completion_tokens": 1}
    assert proxy.parse_usage(json.dumps({"model": "m", "usage": {**usage, "cost": 0.25}}).encode(), "application/json")[1]["cost_usd"] == 0.25
    assert "cost_usd" not in proxy.parse_usage(json.dumps({"model": "m", "usage": usage}).encode(), "application/json")[1]
