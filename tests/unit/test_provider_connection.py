"""The provider connection test: one cheap read of the endpoint, answered as data, with no secret in it.

The endpoints are fakes behind ``httpx.MockTransport``; the suite never leaves the machine.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.providers.connection_probe import probe
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.stores.database import Database

KEY = "sk-live-0123456789abcdef-SECRET"
Handler = Callable[[httpx.Request], httpx.Response]


def provider_over(handler: Handler, *, kind: str = "openai_compat", key: str = KEY, base_url: str = "https://models.example.test/v1") -> OpenAICompatibleProvider:
    endpoint = ProviderEndpoint(id="fake", kind=kind, base_url=base_url, api_key=key)
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def listing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})


async def test_a_working_endpoint_answers_with_its_latency_and_model_count() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return listing(request)

    result = await probe(provider_over(handler), redact=lambda text: text)
    assert result["ok"] is True and result["status"] == 200 and result["models"] == 3 and result["detail"] == ""
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    [request] = seen
    # The address a completion goes to, with the key a completion carries, sent once and by the adapter's own client.
    assert request.method == "GET" and str(request.url) == "https://models.example.test/v1/models"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    timeout = request.extensions["timeout"]
    assert timeout["read"] == 8.0 and timeout["connect"] == 5.0
    assert KEY not in json.dumps(result)


async def test_llamacpp_style_listings_are_counted_too() -> None:
    result = await probe(provider_over(lambda r: httpx.Response(200, json={"models": [{"name": "m"}]}), kind="llamacpp", key=""), redact=lambda text: text)
    assert result["ok"] is True and result["models"] == 1


@pytest.mark.parametrize("status, words", [
    (401, "refused the key"), (403, "refused the key"), (404, "no model list at this address"),
    (429, "rate limiting"), (502, "failed on its side"), (418, "answered HTTP 418"),
])
async def test_a_refusal_is_an_answer_not_an_error(status: int, words: str) -> None:
    result = await probe(provider_over(lambda r: httpx.Response(status, json={"error": {"message": "nope"}})), redact=lambda text: text)
    assert result["ok"] is False and result["status"] == status and words in result["detail"] and "nope" in result["detail"]
    assert "models" not in result and isinstance(result["latency_ms"], int)


async def test_an_endpoint_that_repeats_the_key_it_refused_is_not_quoted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    echo = httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}. You can find yours elsewhere."}})
    result = await probe(provider_over(lambda r: echo), redact=lambda text: text)
    assert result["ok"] is False and KEY not in json.dumps(result) and "…" in result["detail"]
    assert KEY not in caplog.text


@pytest.mark.parametrize("error, words", [
    (httpx.ConnectTimeout("slow"), "no answer within 8 s"),
    (httpx.ReadTimeout("slow"), "no answer within 8 s"),
    (httpx.ConnectError("refused"), "could not connect"),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), "certificate was not accepted"),
    (httpx.RemoteProtocolError("closed"), "RemoteProtocolError"),
])
async def test_an_endpoint_that_does_not_answer_is_described_by_kind_only(error: Exception, words: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        # What a transport error can carry: the address with the credential in it.
        raise type(error)(f"{error} while reading https://user:{KEY}@models.example.test/v1/models")

    result = await probe(provider_over(handler), redact=lambda text: text)
    assert result["ok"] is False and result["status"] is None and words in result["detail"]
    assert isinstance(result["latency_ms"], int)
    assert KEY not in json.dumps(result) and KEY not in caplog.text and "models.example.test" not in result["detail"]


async def test_an_answer_that_is_not_a_model_list_is_not_ok() -> None:
    for body in ({"status": "ok"}, ["a"], {"data": "nope"}):
        result = await probe(provider_over(lambda r, body=body: httpx.Response(200, json=body)), redact=lambda text: text)
        assert result["ok"] is False and result["status"] == 200 and "not with a list of models" in result["detail"]
    html = await probe(provider_over(lambda r: httpx.Response(200, text="<html>login</html>")), redact=lambda text: text)
    assert html["ok"] is False


async def test_a_known_problem_is_the_answer_and_nothing_is_sent() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return listing(request)

    result = await probe(provider_over(handler), redact=lambda text: text, problem="fake has no key in the key proxy")
    assert result == {"ok": False, "latency_ms": None, "status": None, "detail": "fake has no key in the key proxy"} and sent == []


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    yield manager
    await manager.close()


async def test_the_route(settings: Settings, db: Database, manager: SessionManager, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    answers: list[Handler] = [listing]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers[0](request)

    manager.config.providers["fake"] = ProviderConfig(kind="openai_compat", base_url="https://models.example.test/v1", api_key=KEY)
    manager.config.providers["keyless"] = ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com/v1")
    manager.providers._providers["fake"] = provider_over(handler)
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    headers = {"X-Daedalus-Token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.post("/api/providers/fake/test")).status_code == 401
        assert (await client.post("/api/providers/nobody/test", headers=headers)).status_code == 404
        ok = await client.post("/api/providers/fake/test", headers=headers)
        assert ok.status_code == 200
        assert ok.json()["ok"] is True and ok.json()["models"] == 3 and ok.json()["status"] == 200 and set(ok.json()) == {"ok", "latency_ms", "status", "detail", "models"}

        answers[0] = lambda r: httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})
        refused = await client.post("/api/providers/fake/test", headers=headers)
        assert refused.status_code == 200, "a refusing endpoint is data, not a server error"
        assert refused.json()["ok"] is False and refused.json()["status"] == 401 and KEY not in refused.text

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"https://{KEY}@models.example.test")

        answers[0] = down
        gone = await client.post("/api/providers/fake/test", headers=headers)
        assert gone.status_code == 200 and gone.json()["ok"] is False and gone.json()["status"] is None and KEY not in gone.text

        # A hosted vendor with no key has no live adapter: also an answer.
        keyless = await client.post("/api/providers/keyless/test", headers=headers)
        assert keyless.status_code == 200 and keyless.json()["ok"] is False and "no key" in keyless.json()["detail"]
    assert KEY not in caplog.text


def test_the_door_does_not_open_it_to_a_device() -> None:
    from daedalus.remote import door

    assert not any(pattern.fullmatch("/api/providers/fake/test") for _method, pattern, _rule in door.ROUTES)
