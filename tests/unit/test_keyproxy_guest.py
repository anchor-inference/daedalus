"""The key proxy's listener for the guest door: one kind of call on chosen upstreams, and nothing else."""

from __future__ import annotations

import asyncio
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
SELECTION = ["deepinfra/fp8/flex", "wafer/us"]
LISTED = [
    {"path": path, "model": "m", "only": []}
    for path in ("acme/v1", "acme/v4.1", "acme", "openrouter", "claude/v1", "codex/v1", "grok/v1", "opencode", "opencode_zen")
] + [{"path": "openrouter", "model": "limited", "only": SELECTION}, {"path": "acme/v1", "model": "limited", "only": SELECTION}]


def write_table(folder: Path, models: Any) -> None:
    (folder / "guest-models.json").write_text(json.dumps({"models": models}), encoding="utf-8")


@pytest.fixture(autouse=True)
def table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The folder of the table the host writes, found from the database's path as the proxy finds it."""
    monkeypatch.setenv("KEYPROXY_BUDGET_DB", str(tmp_path / "daedalus.sqlite"))
    write_table(tmp_path, LISTED)
    return tmp_path


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
    ("GET", "/subscriptions/usage"), ("GET", "/keys"), ("POST", "/codex/v1/chat/completions"), ("POST", "/acme/../other/v1/chat/completions"),
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


@pytest.mark.parametrize("field", ["models", "plugins", "transforms", "route", "something_a_vendor_adds_next"])
def test_a_field_a_stand_in_does_not_send_is_refused(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "openrouter")
    proxy = load()
    assert proxy.guest_refusal("/openrouter/chat/completions", "POST", json.dumps(GOOD).encode()) == ""
    assert "no tools" in proxy.guest_refusal("/openrouter/chat/completions", "POST", json.dumps({**GOOD, field: ["x"]}).encode())


def test_the_star_is_every_model_upstream_the_logins_included_and_never_a_search_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Perplexity answers on the same path as a model, so a search key must not become a stand-in's reach."""
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "*")
    monkeypatch.setenv("KEYPROXY_KEY_PERPLEXITY", "the-key")
    monkeypatch.setenv("OPENCODE_API_KEY", "the-key")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_ACME", "https://upstream.example.test/v1")
    monkeypatch.setenv("KEYPROXY_KEY_ACME", "the-key")
    served = load().guest_upstreams()
    assert {"codex", "grok", "claude", "opencode", "acme"} <= served and "perplexity" not in served
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "acme,perplexity")
    assert load().guest_upstreams() == {"acme"}


@pytest.mark.parametrize("field", [{"tools": [{"type": "function", "function": {"name": "x"}}]}, {"functions": [{"name": "x"}]}, {"tool_choice": "auto"}, {"stream": True}, {"n": 2}, {"reasoning_effort": "high"}, {"max_tokens": 5000}])
@pytest.mark.parametrize("path", ["/claude/v1/chat/completions", "/codex/v1/chat/completions", "/grok/v1/chat/completions", "/opencode/chat/completions"])
def test_a_plan_route_is_held_as_narrowly_as_any_other(monkeypatch: pytest.MonkeyPatch, path: str, field: dict[str, Any]) -> None:
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "claude,codex,grok,opencode")
    proxy = load()
    assert proxy.guest_refusal(path, "POST", json.dumps(GOOD).encode()) == ""
    assert proxy.guest_refusal(path, "POST", json.dumps({**GOOD, **field}).encode())
    assert proxy.guest_refusal(path.replace("/chat/completions", "/models"), "GET", b"")
    assert proxy.guest_refusal(path.replace("chat/completions", "../chat/completions"), "POST", json.dumps(GOOD).encode())


@pytest.mark.parametrize("name", ["claude", "codex"])
async def test_a_login_route_is_booked_though_it_answers_outside_the_generic_path(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", name)
    proxy = load()
    reported: list[tuple[str, str, bytes]] = []
    answer = {"model": "m", "choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def answered(_request: web.Request, _rest: str) -> web.Response:
        return web.json_response(answer)

    async def noted(_client: Any, provider: str, model: str, body: bytes, *_: Any) -> None:
        reported.append((provider, model, body))

    monkeypatch.setattr(proxy, f"handle_{name}", answered)
    monkeypatch.setattr(proxy, "report_direct_usage", noted)
    app = web.Application()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    app.router.add_route("*", "/{upstream}/{rest:.*}", proxy.handle_guest)
    client = TestClient(TestServer(app))
    await client.start_server()
    response = await client.post(f"/{name}/v1/chat/completions", json=GOOD, headers={"x-daedalus-metered": "1"})
    assert (await response.json())["choices"][0]["message"]["content"] == "hi"
    await asyncio.sleep(0)
    await client.close()
    assert [(p, m) for p, m, _ in reported] == [(name, "m")] and json.loads(reported[0][2]) == answer


async def test_opencode_is_given_the_session_header_it_insists_on_and_the_door_cannot_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without it the real route answers 400 MissingSessionID, and the listener passes no header of the door's."""
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "opencode")
    monkeypatch.setenv("OPENCODE_API_KEY", "the-key")
    proxy = load()
    proxy.AGENT_API = ""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "hi"}}]})

    app = web.Application()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app.router.add_route("*", "/{upstream}/{rest:.*}", proxy.handle_guest)
    client = TestClient(TestServer(app))
    await client.start_server()
    assert (await client.post("/opencode/chat/completions", json=GOOD)).status == 200
    await client.close()
    assert seen[0].headers["x-opencode-session"].startswith("guest-")


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


# -- a stand-in limited to some of OpenRouter's providers --------------------------------------------


ROUTED = {**GOOD, "model": "limited", "provider": {"only": SELECTION, "allow_fallbacks": False}}


@pytest.fixture
async def router(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The same door on the OpenRouter upstream, whose key is the only thing this listener may add."""
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "openrouter,acme")
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-key")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_ACME", "https://upstream.example.test/v1")
    monkeypatch.setenv("KEYPROXY_KEY_ACME", "the-key")
    proxy = load()
    proxy.AGENT_API = "http://agent.example.test"
    upstream: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    async def noted(*_: Any) -> None:
        pass

    monkeypatch.setattr(proxy, "report_direct_usage", noted)
    app = web.Application()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app.router.add_route("*", "/{upstream}/{rest:.*}", proxy.handle_guest)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, upstream, proxy
    await client.close()


async def test_a_limited_call_reaches_openrouter_with_the_field_untouched(router: Any) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", json=ROUTED)
    assert answer.status == 200
    assert json.loads(upstream[0].content)["provider"] == ROUTED["provider"]


async def test_a_model_nobody_limited_goes_as_it_was(router: Any) -> None:
    client, upstream, _proxy = router
    assert (await client.post("/openrouter/chat/completions", json=GOOD)).status == 200
    assert "provider" not in json.loads(upstream[0].content)


async def test_a_limited_call_may_narrow_what_the_operator_chose(router: Any) -> None:
    client, upstream, _proxy = router
    narrowed = {**ROUTED, "provider": {"only": ["wafer/us"], "allow_fallbacks": False}}
    assert (await client.post("/openrouter/chat/completions", json=narrowed)).status == 200
    assert json.loads(upstream[0].content)["provider"]["only"] == ["wafer/us"]


@pytest.mark.parametrize("only", [
    ["deepinfra"],  # the provider a selected endpoint belongs to reaches all of its endpoints
    ["deepinfra/fp8"],
    ["wafer"],
    ["baseten"],
    ["wafer/us", "baseten"],
    [*SELECTION, "baseten"],
])
async def test_a_provider_outside_the_operators_choice_is_refused(router: Any, only: list[str]) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", json={**ROUTED, "provider": {"only": only, "allow_fallbacks": False}})
    assert answer.status == 403 and upstream == []


async def test_a_limited_model_cannot_be_called_with_the_limit_left_out(router: Any) -> None:
    """Left out, OpenRouter routes it anywhere, so dropping the object would be the way around everything above."""
    client, upstream, _proxy = router
    limited = {k: v for k, v in ROUTED.items() if k != "provider"}
    answer = await client.post("/openrouter/chat/completions", json=limited)
    assert answer.status == 403 and upstream == []


async def test_a_model_with_nothing_chosen_takes_no_provider_object(router: Any) -> None:
    """It would otherwise pin a call to a provider the operator never looked at, at that provider's price."""
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", json={**GOOD, "provider": {"only": ["baseten"], "allow_fallbacks": False}})
    assert answer.status == 403 and upstream == []


@pytest.mark.parametrize("model", ["anthropic/claude-a-dear-one", "M", "m:nitro", "m:online", "openrouter/auto", "", 7, None, ["m"], {"m": 1}])
async def test_a_model_the_app_does_not_list_is_refused(router: Any, model: Any) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", json={**GOOD, "model": model})
    assert answer.status == 403 and upstream == []


async def test_a_call_that_names_no_model_is_refused(router: Any) -> None:
    client, upstream, _proxy = router
    body = {k: v for k, v in GOOD.items() if k != "model"}
    assert (await client.post("/openrouter/chat/completions", json=body)).status == 403 and upstream == []


async def test_a_model_listed_on_one_upstream_names_nothing_on_another(router: Any, table: Path) -> None:
    client, upstream, _proxy = router
    write_table(table, [{"path": "acme/v1", "model": "only-here", "only": []}])
    answer = await client.post("/openrouter/chat/completions", json={**GOOD, "model": "only-here"})
    assert answer.status == 403 and upstream == []
    assert (await client.post("/acme/chat/completions", json={**GOOD, "model": "only-here"})).status == 403 and upstream == []
    assert (await client.post("/acme/v1/chat/completions", json={**GOOD, "model": "only-here"})).status == 200


@pytest.mark.parametrize("raw", [
    b'{"model": "dear", "model": "m", "messages": [], "max_tokens": 10}',
    b'{"model": "m", "model": "dear", "messages": [], "max_tokens": 10}',
    b'{"model": "limited", "messages": [], "max_tokens": 10, "provider": {"only": ["baseten"], "only": ["wafer/us"], "allow_fallbacks": false}}',
    b'{"model": "limited", "messages": [], "max_tokens": 10, "provider": {"only": ["baseten"], "allow_fallbacks": false}, "provider": {"only": ["wafer/us"], "allow_fallbacks": false}}',
])
async def test_a_name_said_twice_is_refused_because_the_upstream_may_read_the_other_one(router: Any, raw: bytes) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", data=raw, headers={"content-type": "application/json"})
    assert answer.status == 403 and upstream == []


async def test_a_change_of_the_table_holds_from_the_next_call_in_both_directions(router: Any, table: Path) -> None:
    client, upstream, _proxy = router
    assert (await client.post("/openrouter/chat/completions", json={**GOOD, "model": "new"})).status == 403
    # Listed: served, with nothing restarted.
    write_table(table, [*LISTED, {"path": "openrouter", "model": "new", "only": []}])
    assert (await client.post("/openrouter/chat/completions", json={**GOOD, "model": "new"})).status == 200
    # Limited: the same call is refused, and the one inside the choice is served.
    write_table(table, [{"path": "openrouter", "model": "new", "only": ["wafer/us"]}])
    assert (await client.post("/openrouter/chat/completions", json={**GOOD, "model": "new"})).status == 403
    limited = {**GOOD, "model": "new", "provider": {"only": ["wafer/us"], "allow_fallbacks": False}}
    assert (await client.post("/openrouter/chat/completions", json=limited)).status == 200
    # The choice widened and then narrowed again.
    write_table(table, [{"path": "openrouter", "model": "new", "only": ["baseten"]}])
    assert (await client.post("/openrouter/chat/completions", json=limited)).status == 403
    # Taken off the list.
    write_table(table, [])
    assert (await client.post("/openrouter/chat/completions", json=limited)).status == 403
    assert len(upstream) == 2


@pytest.mark.parametrize("contents", [None, "", "not json", "[]", "{}", '{"models": 7}', '{"models": [{"path": "openrouter"}]}', '{"models": [7]}', b"\xff\xfe"])
async def test_with_no_whole_table_nothing_is_served_and_the_refusal_says_why(router: Any, table: Path, contents: Any) -> None:
    client, upstream, proxy = router
    file = table / "guest-models.json"
    file.unlink()
    if isinstance(contents, str):
        file.write_text(contents, encoding="utf-8")
    elif contents is not None:
        file.write_bytes(contents)
    answer = await client.post("/openrouter/chat/completions", json=GOOD)
    assert answer.status == 403 and upstream == []
    assert "no table" in (await answer.json())["error"]["message"]
    assert proxy.guest_models() is None


async def test_an_empty_table_is_a_table_and_serves_nothing(router: Any, table: Path) -> None:
    client, upstream, _proxy = router
    write_table(table, [])
    answer = await client.post("/openrouter/chat/completions", json=GOOD)
    assert answer.status == 403 and upstream == [] and "not one the app lists" in (await answer.json())["error"]["message"]


def test_a_missing_table_is_said_once_in_the_log_and_again_after_it_came_back(router: Any, table: Path, caplog: pytest.LogCaptureFixture) -> None:
    _client, _upstream, proxy = router
    (table / "guest-models.json").unlink()
    assert proxy.guest_models() is None and proxy.guest_models() is None
    write_table(table, [])
    assert proxy.guest_models() == {}
    (table / "guest-models.json").unlink()
    assert proxy.guest_models() is None
    assert len([r for r in caplog.records if "no table of guest models" in r.message]) == 2


def test_the_table_lies_beside_the_database_in_both_ways_of_starting(table: Path) -> None:
    """Named from the database's path, which compose and the launcher both give, and from nothing of the door's."""
    assert load().GUEST_MODELS == table / "guest-models.json"
    compose = (Path(__file__).resolve().parents[2] / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert "KEYPROXY_BUDGET_DB: /srv/state/daedalus.sqlite" in compose and "- daedalus-state:/srv/state:ro" in compose
    door = compose.split("\n  guest:\n", 1)[1].split("\n  guest-front:", 1)[0]
    assert "volumes:" in door and "daedalus-state" not in door


@pytest.mark.parametrize("route", [
    {"only": ["wafer/us"]},  # fallbacks left to the default, which is on
    {"only": ["wafer/us"], "allow_fallbacks": True},
    {"only": ["wafer/us"], "allow_fallbacks": 0},
    {"only": [], "allow_fallbacks": False},
    {"only": "wafer/us", "allow_fallbacks": False},
    {"only": ["wafer/us", 7], "allow_fallbacks": False},
    {"only": ["WAFER"], "allow_fallbacks": False},
    {"only": ["../credits"], "allow_fallbacks": False},
    {"only": ["a/b/c/d/e"], "allow_fallbacks": False},
    {"only": ["wafer/us"] * 33, "allow_fallbacks": False},
    {"only": ["wafer/us"], "allow_fallbacks": False, "sort": "price"},
    {"only": ["wafer/us"], "allow_fallbacks": False, "max_price": {"prompt": 100}},
    {"only": ["wafer/us"], "allow_fallbacks": False, "data_collection": "allow"},
    {"only": ["wafer/us"], "allow_fallbacks": False, "ignore": ["baseten"]},
    {"only": ["wafer/us"], "allow_fallbacks": False, "order": ["wafer/us"]},
    {"order": ["wafer/us"], "allow_fallbacks": False},
    {"allow_fallbacks": False},
    {},
    ["wafer/us"],
    "wafer/us",
    None,
])
async def test_any_other_shape_of_the_field_is_refused_before_it_is_sent(router: Any, route: Any) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/openrouter/chat/completions", json={**ROUTED, "provider": route})
    assert answer.status == 403 and upstream == []
    assert "provider object" in (await answer.json())["error"]["message"], "refused for its shape, not for something else"


async def test_only_openrouter_may_be_sent_the_field(router: Any) -> None:
    client, upstream, _proxy = router
    answer = await client.post("/acme/v1/chat/completions", json=ROUTED)
    assert answer.status == 403 and upstream == []
    assert "only OpenRouter" in (await answer.json())["error"]["message"]


def test_an_upstream_with_another_name_on_openrouters_host_is_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYPROXY_UPSTREAM_ROUTER2", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("KEYPROXY_KEY_ROUTER2", "k")
    assert load().routes_between_providers("router2") is True


def test_reading_a_models_providers_is_not_spending(router: Any) -> None:
    _client, _upstream, proxy = router
    assert proxy.budget_exempt("models/deepseek/deepseek-v4.1-flash-1017/endpoints")
    assert proxy.budget_exempt("endpoints/zdr")
    assert not proxy.budget_exempt("chat/completions")
