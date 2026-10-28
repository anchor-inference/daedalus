"""Web search behind one contract: the ``WebSearch`` tool asks a backend, the backend answers with hits.

The tool never changes shape when the operator switches backends. A backend maps one
provider's request and response onto :class:`SearchQuery` and :class:`SearchHit`; the
registry in :data:`BACKENDS` names them; :func:`search` runs the configured backend and
walks the fallback list when it fails or answers with nothing.

Backends that need a key talk to the key proxy (``http://keyproxy:3200/<name>/…``): the
proxy injects the credential, so nothing in this package ever sees one.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from daedalus.config import WebToolsConfig

logger = logging.getLogger(__name__)

TIME_RANGES = ("day", "week", "month", "year")


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    limit: int = 8
    language: str = ""
    """ISO 639-1 code (``ru``, ``en``); empty = let the backend decide."""
    time_range: str = ""
    """One of :data:`TIME_RANGES`; empty = any time."""
    domains: tuple[str, ...] = ()
    """Only results from these sites; empty = the whole web."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    published: str = ""
    source: str = ""
    """The engine(s) or provider that returned it; for the operator, not the model."""
    score: float | None = None
    image: str = ""
    """A picture of the result when the provider gave one (a product photo, a video still), so an
    answer can show it on a card; most hits have none, and then it costs the model nothing."""


class SearchError(Exception):
    """A backend could not answer (network, HTTP status, unexpected payload)."""


class SearchBackend(Protocol):
    id: str
    needs_key: bool
    label: str

    async def search(self, query: SearchQuery, config: WebToolsConfig) -> list[SearchHit]: ...


@dataclass(slots=True)
class Attempt:
    backend: str
    hits: int = 0
    error: str = ""
    ms: int = 0


@dataclass(slots=True)
class SearchOutcome:
    hits: list[SearchHit]
    backend: str
    """The backend that answered; empty when none did."""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def fallback_used(self) -> bool:
        return len(self.attempts) > 1 and bool(self.backend)


BACKENDS: dict[str, Callable[[], SearchBackend]] = {}

CONTAINER_SEARXNG = "http://searxng:8080"
"""The configuration's default SearXNG address: the compose stack's service."""

local_searxng: Callable[[], str] = lambda: ""  # noqa: E731 - a replaceable hook, not a function body
"""Where a SearXNG this installation runs itself answers right now, or an empty string. The host sets
it at start (``daedalus.host.searxng.local_url``); left alone, there is no such instance."""


def searxng_url(configured: str) -> str:
    """The SearXNG address a search goes to: the installation's own instance, when the configured
    address is still the default and that instance is running.

    The default names the compose stack's service, which on a machine is a host name that does not
    exist; an address the operator typed in is theirs and is used as it is. Resolved per search and
    never written into the configuration, because the instance's port is the supervisor's choice at
    every start.
    """
    if configured.strip().rstrip("/") == CONTAINER_SEARXNG and (local := local_searxng()):
        return local
    return configured


def register(factory: Callable[[], SearchBackend]) -> Callable[[], SearchBackend]:
    backend = factory()
    BACKENDS[backend.id] = factory
    return factory


def backend(name: str) -> SearchBackend:
    try:
        return BACKENDS[name]()
    except KeyError:
        raise SearchError(f"unknown search backend {name!r}; known: {', '.join(sorted(BACKENDS))}") from None


def client(config: WebToolsConfig, *, external: bool) -> httpx.AsyncClient:
    """An HTTP client for a backend.

    ``external`` backends (a public site scraped directly) go through the operator's proxy
    when one is set; internal ones (SearXNG, the key proxy) always connect directly.
    """
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=config.search.timeout_seconds,
        headers={"user-agent": config.user_agent},
        proxy=(config.proxy or None) if external else None,
    )


def clean(text: object, limit: int = 600) -> str:
    """One line of plain text from a provider's snippet field."""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def hit_from(item: dict[str, object], *, title: str, url: str, snippet: str, published: str = "", source: str = "", score: str = "", image: str = "") -> SearchHit | None:
    """Build a hit from a provider's JSON object, naming the fields it uses; None without a URL."""
    link = str(item.get(url) or "").strip()
    if not link:
        return None
    raw_score = item.get(score) if score else None
    return SearchHit(
        title=clean(item.get(title), 200) or link,
        url=link,
        snippet=clean(item.get(snippet)),
        published=clean(item.get(published), 40) if published else "",
        source=source,
        score=float(raw_score) if isinstance(raw_score, (int, float)) else None,
        image=_picture(item, image),
    )


def _picture(item: dict[str, object], fields: str) -> str:
    """The first of the comma-separated ``fields`` that holds a web address; only https, since the
    app shows it on a page served over https and an http picture would be blocked there anyway."""
    for name in (f for f in fields.split(",") if f):
        value = str(item.get(name) or "").strip()
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("https://") and len(value) <= 500 and not any(c.isspace() for c in value):
            return value
    return ""


def dedupe(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    seen: set[str] = set()
    out: list[SearchHit] = []
    for hit in hits:
        key = hit.url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
        if len(out) >= limit:
            break
    return out


async def search(query: SearchQuery, config: WebToolsConfig, *, chain: list[str] | None = None) -> SearchOutcome:
    """Run the configured backend, then the fallbacks, until one returns hits.

    ``chain`` overrides the configured order (the operator's check button names one backend).
    A backend that raises or returns nothing is recorded in ``attempts`` and the next one runs.
    """
    order = chain if chain is not None else [config.search.backend, *config.search.fallback]
    attempts: list[Attempt] = []
    for name in dict.fromkeys(n for n in order if n):
        attempt = Attempt(backend=name)
        attempts.append(attempt)
        started = time.monotonic()
        try:
            hits = dedupe(await backend(name).search(query, config), query.limit)
        except SearchError as exc:
            attempt.error = str(exc)
        except httpx.HTTPError as exc:
            attempt.error = f"{type(exc).__name__}: {exc}"
        except (ValueError, KeyError, TypeError) as exc:
            attempt.error = f"unexpected response ({type(exc).__name__}: {exc})"
        else:
            attempt.hits = len(hits)
            if hits:
                attempt.ms = int((time.monotonic() - started) * 1000)
                return SearchOutcome(hits=hits, backend=name, attempts=attempts)
        attempt.ms = int((time.monotonic() - started) * 1000)
        logger.info("web search via %s gave nothing for %r: %s", name, query.text, attempt.error or "no results")
    return SearchOutcome(hits=[], backend="", attempts=attempts)


def render(hits: list[SearchHit]) -> str:
    """The tool's text: one result per bullet, unchanged across backends."""
    lines = []
    for hit in hits:
        head = f"- {hit.title}" + (f" ({hit.published})" if hit.published else "")
        lines.append("\n  ".join(part for part in (head, hit.url, hit.snippet, f"image: {hit.image}" if hit.image else "") if part))
    return "\n".join(lines)


_HEAD_RE = re.compile(r"^- (.+?)(?: \((\d{4}-[^)]*)\))?$")
_URL_LINE_RE = re.compile(r"^  (https?://\S+)$")
SOURCE_SNIPPET_CHARS = 200
SOURCE_LIMIT = 30


def sources_of(text: str) -> list[dict[str, str]]:
    """The hits back out of a result :func:`render` wrote, for the app's list of a turn's sources.

    The listing carries only the first few hundred characters of a result, and a search is mostly
    hits past that point; this is the compact form of all of them — address, title, a shortened
    snippet, the picture — read from the text itself so that a result stored before this existed
    is read the same way. Anything that is not a search result gives an empty list.
    """
    if "[search results" not in text[:400]:
        return []
    lines = text.splitlines()
    out: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        head = _HEAD_RE.match(line)
        url = _URL_LINE_RE.match(lines[index + 1]) if head and index + 1 < len(lines) else None
        if not head or not url:
            continue
        hit = {"url": url.group(1), "title": head.group(1).strip()}
        for extra in lines[index + 2:]:
            if not extra.startswith("  ") or extra.startswith("   "):
                break
            value = extra.strip()
            if value.startswith("image: http"):
                hit["image"] = value[len("image: "):]
            elif "snippet" not in hit:
                hit["snippet"] = clean(value, SOURCE_SNIPPET_CHARS)
        out.append(hit)
        if len(out) >= SOURCE_LIMIT:
            break
    return out


def catalogue() -> list[dict[str, object]]:
    """Every registered backend for the Mini App: id, label, whether it needs a key in the key proxy."""
    return [{"id": b.id, "label": b.label, "needs_key": b.needs_key} for b in (factory() for factory in BACKENDS.values())]


from daedalus.tools.websearch import backends as _backends  # noqa: E402  (registers the built-in backends)

__all__ = [
    "BACKENDS",
    "TIME_RANGES",
    "Attempt",
    "SearchBackend",
    "SearchError",
    "SearchHit",
    "SearchOutcome",
    "SearchQuery",
    "backend",
    "catalogue",
    "clean",
    "client",
    "dedupe",
    "hit_from",
    "register",
    "render",
    "search",
    "searxng_url",
    "sources_of",
]

del _backends
