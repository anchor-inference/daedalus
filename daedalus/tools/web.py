"""Web tools: fetch a page as text, search the web."""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

import httpx
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.setting_refs import WEB_SEARCH
from daedalus.security.untrusted import fenced, injection_signs, origin_of
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


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_PICTURE_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")


def page_head(raw: str, url: str) -> str:
    """The page's title and picture as two short lines over its text, each only when the page has it.

    The title names the page in the answer's list of sources, and the picture (``og:image``) is what
    a card about the page can show; both are a line each, read from the head alone. A relative
    picture is resolved against the page's address, and only an https one is kept, since the app
    is served over https and would refuse to show anything else.
    """
    head = raw[:200_000]
    lines: list[str] = []
    title = _TITLE_RE.search(head)
    if title and (text := " ".join(html.unescape(_HTML_RE.sub(" ", title.group(1))).split())[:150]):
        lines.append(f"title: {text}")
    for tag in _META_RE.findall(head):
        attrs = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in _ATTR_RE.finditer(tag)}
        if (attrs.get("property") or attrs.get("name") or "").lower() not in _PICTURE_KEYS:
            continue
        picture = urljoin(url, html.unescape(attrs.get("content") or "").strip())
        if picture.startswith("https://") and len(picture) <= 500 and not any(c.isspace() for c in picture):
            lines.append(f"image: {picture}")
            break
    return "".join(f"{line}\n" for line in lines)


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
    head = ""
    if "html" in content_type:
        head = page_head(response.text, str(response.url))
        body = html_to_text(response.text)
    elif "json" in content_type or content_type.startswith("text/"):
        body = response.text
    else:
        return ok(context, f"HTTP {response.status_code}, {content_type}, {len(response.content)} bytes (binary; use exec with curl -o to save it)")
    limit = min(max_chars or web.fetch_max_chars, services.max_tool_output_chars)
    text = clip(body, limit)
    origin = origin_of(str(response.url))
    signs: list[str] = []
    rules = getattr(services.extra.get("manager"), "action_rules", None)
    if rules is not None and not injection_signs(text):
        # The patterns found nothing; the operator may have a small model look as well.
        signs = await rules.injection_check(origin, text)
    page = fenced(text, kind="page content", origin=origin, source="the web", quoted_by="the page", signs=signs)
    return ok(context, f"HTTP {response.status_code} {url}\n{head}\n{page}", status=response.status_code)


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
            # Every backend refused: the agent reads why, and the operator is offered the search
            # backend's row, which is the one thing that changes the answer.
            manager = services_for(context).extra.get("manager")
            if manager is not None and hasattr(manager, "setting_notice"):
                await manager.setting_notice(context.session_id, kind="search", outcome="failed", detail=f"search failed ({reasons})", setting=WEB_SEARCH)
            return error(context, f"search failed ({reasons})", backend="", attempts=tried)
        return ok(context, "(no results)", count=0, backend=outcome.backend, attempts=tried)
    results = fenced(websearch.render(outcome.hits), kind="search results", origin=outcome.backend,
                     source="the web", quoted_by="the results")
    return ok(context, results, count=len(outcome.hits), backend=outcome.backend, fallback_used=outcome.fallback_used, attempts=tried)


TOOLS = [web_fetch, web_search]

__all__ = ["TOOLS", "html_to_text", "page_head"]
