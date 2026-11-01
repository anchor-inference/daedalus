"""``GET /api/favicon?host=<site>``: a site's small icon, for the marks beside a rich answer's citations and sources.

The app could ask a public favicon service, but that would tell a third party every site the operator's
answers cite; it could load ``https://<site>/favicon.ico`` itself, but inside Telegram an image cannot
carry the credential and every answer would make the operator's browser call each site. So the host
fetches the icon once, through the operator's web proxy when one is set, keeps it on disk for a week
(a site with none is remembered for a day), and hands the bytes to the app.

The route fetches nothing but an icon: a host name, never an address or a path, that resolves only to
public addresses; ``/favicon.ico`` or the icon the home page names; redirects followed by hand and
checked again at each step; at most a small number of bytes, and only bytes that are a raster image
by their own signature. SVG is refused, because a document served from the app's origin that can
carry script is not an icon.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response

if TYPE_CHECKING:
    from daedalus.app import Application

HIT_SECONDS = 7 * 24 * 3600
MISS_SECONDS = 24 * 3600
ICON_MAX_BYTES = 100_000
PAGE_MAX_BYTES = 256_000
TIMEOUT_SECONDS = 5.0
REDIRECTS = 3

HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_HEADERS = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}

Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def image_type(data: bytes) -> str:
    """The raster type the bytes say they are, or an empty string: the answer's type is never the server's word."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def icon_link(page: str, base: str) -> str:
    """The icon a home page names in its head, as an absolute address, or an empty string."""
    for tag in _LINK_RE.findall(page):
        attrs = {m.group(1).lower(): next(v for v in m.groups()[1:] if v is not None) for m in _ATTR_RE.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href", "").strip()
        if href and "icon" in rel and not href.lower().endswith(".svg") and not href.startswith("data:"):
            return urljoin(base, href)
    return ""


class FaviconStore:
    """The icons on disk and the fetch that fills them; one fetch per site at a time."""

    def __init__(self, root: Path, *, proxy: Callable[[], str], user_agent: Callable[[], str], resolver: Resolver = resolve, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self._proxy = proxy
        self._user_agent = user_agent
        self._resolve = resolver
        self._transport = transport
        self._clock = clock
        self._inflight: dict[str, asyncio.Task[tuple[bytes, str] | None]] = {}

    def _paths(self, host: str) -> tuple[Path, Path]:
        stem = hashlib.sha256(host.encode()).hexdigest()[:32]
        return self.root / f"{stem}.icon", self.root / f"{stem}.miss"

    async def _public(self, host: str) -> bool:
        """Whether a host may be fetched: a name of the public web, not this machine or its network.
        Through a proxy the proxy resolves the name, and the check falls to it."""
        if self._proxy():
            return True
        try:
            addresses = await self._resolve(host)
        except OSError:
            return False
        return bool(addresses) and all(ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses)

    async def _get(self, client: httpx.AsyncClient, url: str, limit: int, *, cut: bool = False) -> tuple[bytes, str, str] | None:
        """The bytes at ``url`` after the redirects, each step checked, with the final address and its
        type. Past ``limit`` an icon is refused (it is not an icon) and a page is ``cut``: its head is
        all that is read."""
        for _ in range(REDIRECTS + 1):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not HOST_RE.match(host) or not await self._public(host):
                return None
            async with client.stream("GET", url) as response:
                if response.is_redirect and (location := response.headers.get("location")):
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    return None
                data = b""
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        return (data[:limit], str(response.url), response.headers.get("content-type", "")) if cut else None
                return data, str(response.url), response.headers.get("content-type", "")
        return None

    async def _fetch(self, host: str) -> tuple[bytes, str] | None:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"user-agent": self._user_agent()}, proxy=self._proxy() or None, transport=self._transport) as client:
            try:
                found = await self._get(client, f"https://{host}/favicon.ico", ICON_MAX_BYTES)
                if found and (kind := image_type(found[0])):
                    return found[0], kind
                page = await self._get(client, f"https://{host}/", PAGE_MAX_BYTES, cut=True)
                link = icon_link(page[0].decode("utf-8", "replace"), page[1]) if page else ""
                found = await self._get(client, link, ICON_MAX_BYTES) if link else None
                if found and (kind := image_type(found[0])):
                    return found[0], kind
            except (httpx.HTTPError, OSError, ValueError):
                return None
        return None

    async def icon(self, host: str) -> tuple[bytes, str] | None:
        hit, miss = self._paths(host)
        now = self._clock()
        if hit.is_file() and now - hit.stat().st_mtime < HIT_SECONDS:
            data = hit.read_bytes()
            return data, image_type(data)
        if miss.is_file() and now - miss.stat().st_mtime < MISS_SECONDS:
            return None
        task = self._inflight.get(host)
        if task is None:
            task = self._inflight[host] = asyncio.create_task(self._fetch(host))
            task.add_done_callback(lambda _: self._inflight.pop(host, None))
        found = await task
        self.root.mkdir(parents=True, exist_ok=True)
        if found:
            hit.write_bytes(found[0])
            miss.unlink(missing_ok=True)
        else:
            miss.touch()
        return found


def valid_host(raw: str) -> str:
    """The host name asked for, lower-cased, or an empty string when it is not one: an IP address, a
    port, a path or a single label (``localhost``) is not a site on the web."""
    host = raw.strip().lower().rstrip(".")
    if not HOST_RE.match(host):
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return ""


def register(api: FastAPI, app: Application, auth: Callable[..., Any], *, store: FaviconStore | None = None) -> FaviconStore:
    icons = store or FaviconStore(
        app.settings.state_dir / "favicons",
        proxy=lambda: app.config.tools.web.proxy,
        user_agent=lambda: app.config.tools.web.user_agent,
    )

    @api.get("/api/favicon")
    async def favicon(host: str, _: dict[str, Any] = Depends(auth)) -> Response:
        name = valid_host(host)
        if not name:
            raise HTTPException(400, "not a host name")
        found = await icons.icon(name)
        if found is None:
            raise HTTPException(404, "no icon")
        return Response(found[0], media_type=found[1], headers=_HEADERS)

    return icons


__all__ = ["HOST_RE", "FaviconStore", "icon_link", "image_type", "register", "resolve", "valid_host"]
