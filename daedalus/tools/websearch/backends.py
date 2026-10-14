"""The built-in search backends.

Two kinds: the free ones (a self-hosted SearXNG, DuckDuckGo's HTML page) and the keyed
ones reached through the key proxy (Serper, Keenable, Tavily, Exa, Perplexity). Each one
is a few lines of request/response mapping; everything shared lives in the package root.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from daedalus.config import WebToolsConfig
from daedalus.tools import websearch
from daedalus.tools.websearch import SearchError, SearchHit, SearchQuery, clean, client, hit_from, register


def _with_site_operators(query: SearchQuery) -> str:
    """``site:`` operators for engines without a domain parameter."""
    if not query.domains:
        return query.text
    sites = " OR ".join(f"site:{d}" for d in query.domains)
    return f"{query.text} ({sites})" if len(query.domains) > 1 else f"{query.text} site:{query.domains[0]}"


def _since(time_range: str) -> str:
    """The ISO date a ``time_range`` starts at, for providers that take a date instead of a keyword."""
    days = {"day": 1, "week": 7, "month": 31, "year": 366}.get(time_range)
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d") if days else ""


def _check(response: Any, backend: str) -> Any:
    if response.status_code >= 400:
        detail = clean(response.text, 200)
        raise SearchError(f"{backend} answered HTTP {response.status_code}" + (f": {detail}" if detail else ""))
    try:
        return response.json()
    except ValueError:
        raise SearchError(f"{backend} did not answer with JSON") from None


# -- free ------------------------------------------------------------------------------------

@register
class SearxngBackend:
    id = "searxng"
    label = "SearXNG (self-hosted metasearch)"
    needs_key = False

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.searxng
        params: dict[str, str | int] = {"q": _with_site_operators(query), "format": "json", "safesearch": own.safesearch}
        if own.categories:
            params["categories"] = own.categories
        if own.engines:
            params["engines"] = own.engines
        if query.language:
            params["language"] = query.language
        if query.time_range:
            params["time_range"] = query.time_range
        async with client(config, external=False) as http:
            response = await http.get(websearch.searxng_url(own.url).rstrip("/") + "/search", params=params)
        if response.status_code == 403:
            raise SearchError("searxng refused JSON output: add 'json' to search.formats in its settings.yml")
        data = _check(response, self.id)
        hits = []
        for item in data.get("results") or []:
            engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
            hit = hit_from(item, title="title", url="url", snippet="content", published="publishedDate", source=",".join(engines), score="score")
            if hit is not None:
                hits.append(hit)
        unresponsive = [f"{name} ({reason})" for name, reason in (pair for pair in data.get("unresponsive_engines") or [] if isinstance(pair, list) and len(pair) == 2)]
        if not hits and unresponsive:
            # Nothing came back and engines were down: a transient outage, not "the web has nothing" — say so and let the fallback run.
            raise SearchError("searxng engines unavailable: " + ", ".join(unresponsive))
        return hits


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_TAG_RE = re.compile(r"<[^>]+>")


@register
class DuckDuckGoBackend:
    id = "duckduckgo"
    label = "DuckDuckGo (HTML page, no key)"
    needs_key = False

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.duckduckgo
        form = {"q": _with_site_operators(query), "kl": f"{query.language}-{query.language}" if query.language else own.region}
        if query.time_range:
            form["df"] = query.time_range[0]
        async with client(config, external=True) as http:
            response = await http.post(own.url, data=form)
        if response.status_code >= 400 or (response.status_code == 202 and "result__a" not in response.text):
            raise SearchError(f"duckduckgo answered HTTP {response.status_code} (rate limited or blocked)")
        hits = []
        for href, title, snippet in _DDG_RESULT_RE.findall(response.text):
            parsed = urlparse(html.unescape(href))
            target = parse_qs(parsed.query).get("uddg", [html.unescape(href)])[0]
            hits.append(SearchHit(title=clean(html.unescape(_DDG_TAG_RE.sub("", title)), 200), url=target, snippet=clean(html.unescape(_DDG_TAG_RE.sub("", snippet))), source="duckduckgo"))
            if len(hits) >= query.limit:
                break
        return hits


# -- keyed, through the key proxy -------------------------------------------------------------

@register
class SerperBackend:
    id = "serper"
    label = "Serper (Google results)"
    needs_key = True

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.serper
        body: dict[str, Any] = {"q": _with_site_operators(query), "num": query.limit}
        if query.language or own.hl:
            body["hl"] = query.language or own.hl
        if own.gl:
            body["gl"] = own.gl
        if query.time_range:
            body["tbs"] = "qdr:" + query.time_range[0]
        async with client(config, external=False) as http:
            response = await http.post(own.base_url.rstrip("/") + "/search", json=body)
        data = _check(response, self.id)
        hits = []
        for item in data.get("organic") or []:
            hit = hit_from(item, title="title", url="link", snippet="snippet", published="date", source="google")
            if hit is not None:
                hits.append(hit)
        return hits


@register
class KeenableBackend:
    id = "keenable"
    label = "Keenable (own index, English)"
    needs_key = True

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.keenable
        body: dict[str, Any] = {"query": query.text, "max_results": query.limit, "snippet_max_length": own.snippet_max_length}
        if len(query.domains) == 1:
            body["site"] = query.domains[0]
        elif query.domains:
            body["query"] = _with_site_operators(query)
        if query.time_range:
            body["published_after"] = _since(query.time_range)
        async with client(config, external=False) as http:
            response = await http.post(own.base_url.rstrip("/") + "/search", json=body)
        data = _check(response, self.id)
        hits = []
        for item in data.get("results") or []:
            hit = hit_from(item, title="title", url="url", snippet="snippet", published="published_at", source="keenable")
            if hit is not None and not hit.snippet:
                hit = SearchHit(hit.title, hit.url, clean(item.get("description")), hit.published, hit.source)
            if hit is not None:
                hits.append(hit)
        return hits


@register
class TavilyBackend:
    id = "tavily"
    label = "Tavily"
    needs_key = True

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.tavily
        body: dict[str, Any] = {"query": query.text, "max_results": query.limit, "search_depth": own.depth, "topic": "general"}
        if query.domains:
            body["include_domains"] = list(query.domains)
        if query.time_range:
            body["time_range"] = query.time_range
        if query.language:
            body["language"] = query.language
        async with client(config, external=False) as http:
            response = await http.post(own.base_url.rstrip("/") + "/search", json=body)
        data = _check(response, self.id)
        hits = []
        for item in data.get("results") or []:
            hit = hit_from(item, title="title", url="url", snippet="content", published="published_date", source="tavily", score="score")
            if hit is not None:
                hits.append(hit)
        return hits


@register
class ExaBackend:
    id = "exa"
    label = "Exa"
    needs_key = True

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.exa
        body: dict[str, Any] = {"query": query.text, "numResults": query.limit, "type": own.type, "contents": {"highlights": {"maxCharacters": 600, "numSentences": 3}}}
        if query.domains:
            body["includeDomains"] = list(query.domains)
        if query.time_range:
            body["startPublishedDate"] = _since(query.time_range) + "T00:00:00.000Z"
        async with client(config, external=False) as http:
            response = await http.post(own.base_url.rstrip("/") + "/search", json=body)
        data = _check(response, self.id)
        hits = []
        for item in data.get("results") or []:
            highlights = item.get("highlights")
            snippet = " ".join(str(h) for h in highlights) if isinstance(highlights, list) else str(item.get("text") or "")
            hit = hit_from({**item, "snippet": snippet}, title="title", url="url", snippet="snippet", published="publishedDate", source="exa", score="score")
            if hit is not None:
                hits.append(hit)
        return hits


@register
class PerplexityBackend:
    id = "perplexity"
    label = "Perplexity Search"
    needs_key = True

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]:
        own = config.search.perplexity
        body: dict[str, Any] = {"query": query.text, "max_results": query.limit}
        if query.domains:
            body["search_domain_filter"] = list(query.domains)
        if query.time_range:
            body["search_recency_filter"] = query.time_range
        if query.language:
            body["search_language_filter"] = [query.language]
        async with client(config, external=False) as http:
            response = await http.post(own.base_url.rstrip("/") + "/search", json=body)
        data = _check(response, self.id)
        hits = []
        for item in data.get("results") or []:
            hit = hit_from(item, title="title", url="url", snippet="snippet", published="date", source="perplexity")
            if hit is not None:
                hits.append(hit)
        return hits


__all__ = ["DuckDuckGoBackend", "ExaBackend", "KeenableBackend", "PerplexityBackend", "SearxngBackend", "SerperBackend", "TavilyBackend"]
