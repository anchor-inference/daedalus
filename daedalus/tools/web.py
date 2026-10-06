"""Web tools: fetch a page as text, search the web."""

from __future__ import annotations

import html
import re

import httpx
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.security.untrusted import fenced, origin_of
from daedalus.tools import search_hint, websearch
from daedalus.tools._common import clip, error, ok, services_for, tool_config

_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


@search_hint(
    "fetch url download page raw text read link content http "
    "скачать страницу ссылке ссылку содержимое сырой текст забрать забери урл"
)
@tool(
    name="WebFetch",
    description="Fetch a URL and return its content as plain text (HTML is converted).",
)
async def web_fetch(context: ToolContext, url: str, max_chars: int | None = None) -> ToolResult:
    services = services_for(context)
    web = tool_config(context).web
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=web.fetch_timeout_seconds, headers={"user-agent": web.user_agent}, proxy=web.proxy or None
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return error(context, f"fetch failed: {exc}")
    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        body = html_to_text(response.text)
    elif "json" in content_type or content_type.startswith("text/"):
        body = response.text
    else:
        return ok(context, f"HTTP {response.status_code}, {content_type}, {len(response.content)} bytes (binary; use exec with curl -o to save it)")
    limit = min(max_chars or web.fetch_max_chars, services.max_tool_output_chars)
    page = fenced(clip(body, limit), kind="page content", origin=origin_of(str(response.url)), source="the web", quoted_by="the page")
    return ok(context, f"HTTP {response.status_code} {url}\n\n{page}", status=response.status_code)


@search_hint(
    "search web internet google news online latest releases find online open source alternatives "
    "загуглить загугли погуглить поищи в интернете новости свежие гугл найди в сети релизы"
)
@tool(
    name="WebSearch",
    description=(
        "Search the web and return the top results with snippets. Optional: language (ISO code such as 'ru' or 'en'), "
        "time_range ('day', 'week', 'month' or 'year'), domains (comma-separated sites to search within). "
        "Before the second search of a turn, load the 'websearch' skill: it carries the follow-up budget, the "
        "dedupe rule and the stopping rule that keep a research turn from looping."
    ),
)
async def web_search(
    context: ToolContext, query: str, limit: int | None = None, language: str | None = None, time_range: str | None = None, domains: str | None = None
) -> ToolResult:
    web = tool_config(context).web
    if time_range and time_range not in websearch.TIME_RANGES:
        return error(context, f"time_range must be one of {', '.join(websearch.TIME_RANGES)}")
    request = websearch.SearchQuery(
        text=query.strip(),
        limit=min(limit or web.search.results, 30),
        language=(language or "").strip().lower()[:5],
        time_range=time_range or "",
        domains=tuple(d.strip().lower() for d in (domains or "").split(",") if d.strip()),
    )
    if not request.text:
        return error(context, "query is empty")
    outcome = await websearch.search(request, web)
    tried = [{"backend": a.backend, "hits": a.hits, "error": a.error, "ms": a.ms} for a in outcome.attempts]
    if not outcome.hits:
        reasons = "; ".join(f"{a.backend}: {a.error or 'no results'}" for a in outcome.attempts)
        if all(a.error for a in outcome.attempts):
            return error(context, f"search failed ({reasons})", backend="", attempts=tried)
        return ok(context, "(no results)", count=0, backend=outcome.backend, attempts=tried)
    results = fenced(websearch.render(outcome.hits), kind="search results", origin=outcome.backend,
                     source="the web", quoted_by="the results")
    return ok(context, results, count=len(outcome.hits), backend=outcome.backend, fallback_used=outcome.fallback_used, attempts=tried)


TOOLS = [web_fetch, web_search]

__all__ = ["TOOLS", "html_to_text"]
