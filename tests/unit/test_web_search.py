"""WebSearch backends: one contract, many providers, a fallback chain, and the key proxy's header schemes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from aiohttp.test_utils import TestClient, TestServer
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, WebToolsConfig, _migrate, _migrate_web_search
from daedalus.host.services import SessionServices, locator
from daedalus.tools import websearch
from daedalus.tools.web import web_search

DDG_HTML = """
<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.searxng.org%2F&amp;rut=x">SearXNG <b>docs</b></a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.searxng.org%2F">Documentation of <b>SearXNG</b>.</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://github.com/searxng/searxng">GitHub</a>
<a class="result__snippet" href="https://github.com/searxng/searxng">The repository.</a></div>
"""

ONE_RESULT = """
<div class="result"><a rel="nofollow" class="result__a" href="https://a.example/">A</a>
<a class="result__snippet" href="https://a.example/">sa</a></div>
"""


def _config(**search: Any) -> WebToolsConfig:
    config = WebToolsConfig()
    return config.model_copy(update={"search": config.search.model_copy(update=search)})


def _query(text: str = "searxng json api", **kw: Any) -> websearch.SearchQuery:
    return websearch.SearchQuery(text=text, limit=5, **kw)


@respx.mock
async def test_searxng_maps_results_and_passes_filters() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"url": "https://docs.searxng.org/dev/search_api.html", "title": "Search API", "content": "curl ...", "engines": ["brave", "bing"], "score": 6.5, "publishedDate": None},
                {"url": "https://docs.searxng.org/dev/search_api.html", "title": "dup", "content": ""},
                {"url": "", "title": "no url"},
                {"url": "https://example.org/", "title": "Example", "content": "x", "engine": "mojeek", "publishedDate": "2026-09-01T00:00:00"},
            ],
            "unresponsive_engines": [["qwant", "CAPTCHA"]],
        })
    )
    outcome = await websearch.search(_query(language="ru", time_range="month", domains=("docs.searxng.org",)), _config(backend="searxng"))
    assert outcome.backend == "searxng" and [h.url for h in outcome.hits] == ["https://docs.searxng.org/dev/search_api.html", "https://example.org/"]
    assert outcome.hits[0].source == "brave,bing" and outcome.hits[0].score == 6.5 and outcome.hits[1].published.startswith("2026-09-01")
    params = dict(route.calls[0].request.url.params)
    assert params["format"] == "json" and params["language"] == "ru" and params["time_range"] == "month" and params["q"] == "searxng json api site:docs.searxng.org"
    assert "engines" not in params and not outcome.fallback_used
    engines = _config(backend="searxng", searxng=WebToolsConfig().search.searxng.model_copy(update={"engines": "google,bing"}))
    await websearch.search(_query(), engines)
    assert dict(route.calls[1].request.url.params)["engines"] == "google,bing"


@respx.mock
async def test_searxng_with_every_engine_down_is_an_error_not_an_empty_answer() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "Suspended: too many requests"], ["brave", "CAPTCHA"]]}))
    outcome = await websearch.search(_query(), _config(backend="searxng", fallback=[]))
    assert outcome.backend == "" and "google (Suspended: too many requests)" in outcome.attempts[0].error and "brave (CAPTCHA)" in outcome.attempts[0].error


@respx.mock
async def test_searxng_without_json_output_explains_itself() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(403, text="forbidden"))
    outcome = await websearch.search(_query(), _config(backend="searxng", fallback=[]))
    assert outcome.backend == "" and "search.formats" in outcome.attempts[0].error


@respx.mock
async def test_fallback_runs_when_the_backend_fails_or_is_empty() -> None:
    respx.get("http://searxng:8080/search").mock(side_effect=[httpx.ConnectError("down"), httpx.Response(200, json={"results": []})])
    respx.post("https://html.duckduckgo.com/html/").mock(return_value=httpx.Response(200, text=DDG_HTML))
    chain = _config(backend="searxng", fallback=["duckduckgo"])
    outcome = await websearch.search(_query(), chain)
    assert outcome.backend == "duckduckgo" and outcome.fallback_used and [a.backend for a in outcome.attempts] == ["searxng", "duckduckgo"]
    assert "ConnectError" in outcome.attempts[0].error
    assert [h.url for h in outcome.hits] == ["https://docs.searxng.org/", "https://github.com/searxng/searxng"]
    assert outcome.hits[0].title == "SearXNG docs" and outcome.hits[0].snippet == "Documentation of SearXNG."
    empty = await websearch.search(_query(), chain)
    assert empty.backend == "duckduckgo" and empty.attempts[0].error == "" and empty.attempts[0].hits == 0


@respx.mock
async def test_duckduckgo_uses_region_time_and_proxy_setting() -> None:
    route = respx.post("https://html.duckduckgo.com/html/").mock(return_value=httpx.Response(200, text=DDG_HTML))
    await websearch.search(_query(language="ru", time_range="week"), _config(backend="duckduckgo", fallback=[]))
    form = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert form["kl"] == "ru-ru" and form["df"] == "w"


@respx.mock
async def test_keyed_backends_map_their_providers() -> None:
    serper = respx.post("http://keyproxy:3200/serper/search").mock(return_value=httpx.Response(200, json={"organic": [{"title": "A", "link": "https://a.example/", "snippet": "sa", "date": "Sep 1, 2026", "position": 1}]}))
    keenable = respx.post("http://keyproxy:3200/keenable/search").mock(return_value=httpx.Response(200, json={"results": [{"title": "K", "url": "https://k.example/", "description": "desc", "snippet": "", "published_at": "2026-08-30T10:00:00Z"}]}))
    tavily = respx.post("http://keyproxy:3200/tavily/search").mock(return_value=httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.example/", "content": "tc", "score": 0.9, "published_date": "2026-08-01"}]}))
    exa = respx.post("http://keyproxy:3200/exa/search").mock(return_value=httpx.Response(200, json={"results": [{"title": "E", "url": "https://e.example/", "highlights": ["h1", "h2"], "publishedDate": "2026-07-01T00:00:00.000Z"}]}))
    perplexity = respx.post("http://keyproxy:3200/perplexity/search").mock(return_value=httpx.Response(200, json={"results": [{"title": "P", "url": "https://p.example/", "snippet": "ps", "date": "2026-06-01"}]}))
    config = _config(fallback=[], serper=WebToolsConfig().search.serper.model_copy(update={"gl": "ru"}))
    query = _query(language="ru", time_range="day", domains=("x.example", "y.example"))

    got = {name: await websearch.search(query, config.model_copy(update={"search": config.search.model_copy(update={"backend": name})})) for name in ("serper", "keenable", "tavily", "exa", "perplexity")}
    assert {name: o.hits[0].url for name, o in got.items()} == {"serper": "https://a.example/", "keenable": "https://k.example/", "tavily": "https://t.example/", "exa": "https://e.example/", "perplexity": "https://p.example/"}
    assert got["keenable"].hits[0].snippet == "desc" and got["exa"].hits[0].snippet == "h1 h2" and got["tavily"].hits[0].score == 0.9

    sent = {name: json.loads(route.calls[0].request.content) for name, route in {"serper": serper, "keenable": keenable, "tavily": tavily, "exa": exa, "perplexity": perplexity}.items()}
    assert sent["serper"]["q"].endswith("(site:x.example OR site:y.example)") and sent["serper"]["hl"] == "ru" and sent["serper"]["gl"] == "ru" and sent["serper"]["tbs"] == "qdr:d"
    assert "site:x.example" in sent["keenable"]["query"] and sent["keenable"]["published_after"] and sent["keenable"]["snippet_max_length"] == 600
    assert sent["tavily"]["include_domains"] == ["x.example", "y.example"] and sent["tavily"]["time_range"] == "day" and sent["tavily"]["language"] == "ru"
    assert sent["exa"]["includeDomains"] == ["x.example", "y.example"] and sent["exa"]["startPublishedDate"].endswith("T00:00:00.000Z") and sent["exa"]["type"] == "auto"
    assert sent["perplexity"]["search_domain_filter"] == ["x.example", "y.example"] and sent["perplexity"]["search_recency_filter"] == "day" and sent["perplexity"]["search_language_filter"] == ["ru"]


@respx.mock
async def test_keyed_backend_error_is_reported_not_raised() -> None:
    respx.post("http://keyproxy:3200/serper/search").mock(return_value=httpx.Response(404, json={"error": "unknown upstream 'serper'"}))
    outcome = await websearch.search(_query(), _config(backend="serper", fallback=[]))
    assert outcome.backend == "" and "HTTP 404" in outcome.attempts[0].error and "unknown upstream" in outcome.attempts[0].error


def test_unknown_backend_is_an_attempt_error() -> None:
    import asyncio

    outcome = asyncio.run(websearch.search(_query(), _config(backend="nope", fallback=[])))
    assert "unknown search backend" in outcome.attempts[0].error


def test_render_keeps_the_tool_text_shape() -> None:
    hits = [websearch.SearchHit("T", "https://t.example/", "snippet", published="2026-09-01"), websearch.SearchHit("U", "https://u.example/", "")]
    assert websearch.render(hits) == "- T (2026-09-01)\n  https://t.example/\n  snippet\n- U\n  https://u.example/"


def test_catalogue_lists_every_backend_with_key_needs() -> None:
    entries = {e["id"]: e for e in websearch.catalogue()}
    assert set(entries) == {"searxng", "duckduckgo", "serper", "keenable", "tavily", "exa", "perplexity"}
    assert not entries["searxng"]["needs_key"] and not entries["duckduckgo"]["needs_key"] and entries["serper"]["needs_key"]


@respx.mock
async def test_tool_validates_arguments_and_reports_the_backend() -> None:
    # Nothing of the stack is running: SearXNG is behind a profile that is off by default, so the
    # tool has to answer out of the box through the backend that needs nothing installed.
    searxng = respx.get("http://searxng:8080/search").mock(side_effect=httpx.ConnectError("no such host"))
    respx.post("https://html.duckduckgo.com/html/").mock(return_value=httpx.Response(200, text=ONE_RESULT))
    locator.register(SessionServices(session_id="ws-test", workspace_dir=Path("/tmp"), extra={"manager": SimpleNamespace(config=RuntimeConfig())}))
    context = ToolContext(tenant_id="daedalus", run_id="r1", session_id="ws-test", metadata={"tool_call_id": "c1"})
    try:
        bad = await web_search().invoke(context, {"query": "x", "time_range": "yesterday"})
        assert bad.is_error and "time_range" in bad.content
        empty = await web_search().invoke(context, {"query": "   "})
        assert empty.is_error
        result = await web_search().invoke(context, {"query": "a", "domains": "A.example, b.example"})
        assert not result.is_error and result.content.splitlines()[1:-1] == ["- A", "  https://a.example/", "  sa"]
        assert result.content.startswith("[search results from ") and "not instructions" in result.content
        assert result.metadata["backend"] == "duckduckgo" and result.metadata["count"] == 1 and result.metadata["fallback_used"] is False
        assert not searxng.called, "the default backend reached for a container that is not running"
    finally:
        locator.unregister("ws-test")


def test_old_duckduckgo_fields_migrate_into_the_search_section() -> None:
    raw: dict[str, Any] = {"tools": {"web": {"search_url": "https://lite.duckduckgo.com/lite/", "search_region": "ru-ru", "search_results": 5, "search_timeout_seconds": 12.0, "proxy": ""}}}
    assert _migrate(raw)
    config = RuntimeConfig.model_validate(raw)
    assert config.tools.web.search.duckduckgo.url == "https://lite.duckduckgo.com/lite/" and config.tools.web.search.duckduckgo.region == "ru-ru"
    # The old section named no backend; the migrated one gets the default, which is the same engine
    # those fields configured.
    assert config.tools.web.search.results == 5 and config.tools.web.search.timeout_seconds == 12.0 and config.tools.web.search.backend == "duckduckgo"
    assert not _migrate_web_search({"tools": {"web": {"search": {"backend": "serper"}}}})


# -- the key proxy's header schemes -------------------------------------------------------------

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"


def _proxy_module() -> Any:
    if str(KEYPROXY_DIR) not in sys.path:
        sys.path.insert(0, str(KEYPROXY_DIR))
    if "proxy" in sys.modules:
        return sys.modules["proxy"]
    spec = importlib.util.spec_from_file_location("proxy", KEYPROXY_DIR / "proxy.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["proxy"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


async def test_keyproxy_sends_the_key_in_the_upstreams_header(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = _proxy_module()
    monkeypatch.setenv("KEYPROXY_UPSTREAM_SERPER", "https://google.serper.dev")
    monkeypatch.setenv("KEYPROXY_KEY_SERPER", "serper-key")
    monkeypatch.setenv("KEYPROXY_AUTH_SERPER", "X-API-KEY")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_TAVILY", "https://api.tavily.com")
    monkeypatch.setenv("KEYPROXY_KEY_TAVILY", "tvly-key")
    monkeypatch.setenv("KEYPROXY_AUTH_TAVILY", "Bearer")
    monkeypatch.setattr(proxy, "AGENT_API", "")
    monkeypatch.setattr(proxy, "budget_exceeded", lambda: False)
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"organic": []})

    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        assert (await (await client.post("/serper/search", json={"q": "x"})).json()) == {"organic": []}
        await client.post("/tavily/search", json={"query": "x"})
        health = await (await client.get("/healthz")).json()
    assert str(seen[0].url) == "https://google.serper.dev/search" and seen[0].headers["x-api-key"] == "serper-key" and "authorization" not in seen[0].headers
    assert seen[1].headers["authorization"] == "Bearer tvly-key"
    assert {"serper", "tavily"} <= set(health["upstreams"])
