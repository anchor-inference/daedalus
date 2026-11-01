"""``GET /api/card-image?url=<address>``: the picture of a rich answer's card, fetched by the host.

A card's ``image:`` is an address the model wrote into its answer, and the app used to hand it to the
browser as an ``<img src>``. That made the answer a way out for data: text injected into a page the
agent read can have the model write a card whose picture address carries a secret in its query
string, and the operator's browser would fetch it on scrolling to the card, with the operator's own
address and browser. So the browser never fetches a card picture itself any more; it asks this route,
as it asks :mod:`daedalus.extensions.api_favicon` for site icons.

The host fetches the address once and keeps the picture on disk. What it sends to the site is the
address without its query string and fragment, with no cookie, no referrer and the configured user
agent, through the operator's web proxy when one is set; a picture that needs its query to be served
does not load, and the card shows its site's mark instead, which is the price of closing the channel.
Data could still be spelled into the path itself; that much the model's own WebFetch could send
anyway, while the query string is where an injected instruction would naturally put it.

The same guards as the favicon route: https only, a host name of the public web (never an address, a
port or a single label), every redirect followed by hand and checked again, the connected address
checked too when there is no proxy, so a name that resolves to the public web for the check and to
this machine for the connection is still refused. Only a raster picture comes back, by the server's
type and by its own signature both, at most :data:`CARD_IMAGE_MAX_BYTES`; SVG is refused, because a
document served from the app's origin that can carry script is not a picture.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response

from daedalus.extensions.api_favicon import HOST_RE, Resolver, image_type, resolve

if TYPE_CHECKING:
    from daedalus.app import Application

CARD_IMAGE_MAX_BYTES = 4 << 20
HIT_SECONDS = 7 * 24 * 3600
MISS_SECONDS = 24 * 3600
CACHE_MAX_BYTES = 200 << 20
"""The pictures kept on disk; past it the oldest are removed until a fifth of the room is free."""
TIMEOUT_SECONDS = 8.0
REDIRECTS = 3
_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8"
_HEADERS = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}


def picture_type(data: bytes) -> str:
    """The raster type the bytes say they are, AVIF included, or an empty string."""
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    kind = image_type(data)
    return "" if kind == "image/x-icon" else kind


def card_image_url(raw: str) -> str:
    """The address the host will fetch for ``raw``, or an empty string when it may not fetch it at all.

    https, a public host name, the default port, no credentials; the query string and the fragment are
    dropped (see the module's note on why)."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() != "https" or parts.username or parts.password or port not in (None, 443):
        return ""
    if not HOST_RE.match(host):
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    return urlunsplit(("https", host, parts.path or "/", "", ""))


def _global(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


class CardImageStore:
    """The card pictures on disk and the fetch that fills them; one fetch per address at a time."""

    def __init__(self, root: Path, *, proxy: Callable[[], str], user_agent: Callable[[], str], resolver: Resolver = resolve,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self._proxy = proxy
        self._user_agent = user_agent
        self._resolve = resolver
        self._transport = transport
        self._clock = clock
        self._inflight: dict[str, asyncio.Task[tuple[bytes, str] | None]] = {}

    def _paths(self, url: str) -> tuple[Path, Path]:
        stem = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.root / f"{stem}.img", self.root / f"{stem}.miss"

    async def _public(self, host: str) -> bool:
        """Whether a host may be fetched: every address it resolves to is on the public web. Through a
        proxy the proxy resolves the name, and the check falls to it."""
        if self._proxy():
            return True
        try:
            addresses = await self._resolve(host)
        except OSError:
            return False
        return bool(addresses) and all(_global(address) for address in addresses)

    def _connected_publicly(self, response: httpx.Response) -> bool:
        """Whether the connection went to a public address, read off the socket itself: the name was
        resolved again to connect, and a name that answers differently the second time (DNS rebinding)
        is caught here. Where the transport does not say (a proxy, a test transport), the name check stands."""
        if self._proxy():
            return True
        stream = response.extensions.get("network_stream")
        if stream is None:
            return True
        peer = stream.get_extra_info("server_addr")
        return not peer or _global(str(peer[0]))

    async def _fetch(self, url: str) -> tuple[bytes, str] | None:
        headers = {"user-agent": self._user_agent(), "accept": _ACCEPT}
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, headers=headers, proxy=self._proxy() or None, transport=self._transport) as client:
            try:
                for _ in range(REDIRECTS + 1):
                    parts = urlsplit(url)
                    host = (parts.hostname or "").lower()
                    if parts.scheme != "https" or parts.port not in (None, 443) or not HOST_RE.match(host) or not await self._public(host):
                        return None
                    async with client.stream("GET", url) as response:
                        if not self._connected_publicly(response):
                            return None
                        if response.is_redirect and (location := response.headers.get("location")):
                            # The site's own next address, query and all: by now nothing the model wrote is in it.
                            url = urljoin(url, location)
                            continue
                        declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if response.status_code != 200 or not declared.startswith("image/") or "svg" in declared:
                            return None
                        data = b""
                        async for chunk in response.aiter_bytes():
                            data += chunk
                            if len(data) > CARD_IMAGE_MAX_BYTES:
                                return None
                        kind = picture_type(data)
                        return (data, kind) if kind else None
            except (httpx.HTTPError, OSError, ValueError):
                return None
        return None

    async def picture(self, url: str) -> tuple[bytes, str] | None:
        """The picture at ``url`` (already passed through :func:`card_image_url`), from disk or fetched."""
        hit, miss = self._paths(url)
        now = self._clock()
        if hit.is_file() and now - hit.stat().st_mtime < HIT_SECONDS:
            data = await asyncio.to_thread(hit.read_bytes)
            if kind := picture_type(data):
                return data, kind
        if miss.is_file() and now - miss.stat().st_mtime < MISS_SECONDS:
            return None
        task = self._inflight.get(url)
        if task is None:
            task = self._inflight[url] = asyncio.create_task(self._fetch(url))
            task.add_done_callback(lambda _: self._inflight.pop(url, None))
        found = await task
        self.root.mkdir(parents=True, exist_ok=True)
        if found:
            await asyncio.to_thread(self._keep, hit, found[0])
            miss.unlink(missing_ok=True)
        else:
            miss.touch()
        return found

    def _keep(self, hit: Path, data: bytes) -> None:
        partial = hit.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(hit)
        entries = [(entry.stat().st_mtime, entry.stat().st_size, entry) for entry in self.root.glob("*.img")]
        total = sum(size for _, size, _ in entries)
        if total <= CACHE_MAX_BYTES:
            return
        for _, size, entry in sorted(entries, key=lambda item: item[0]):
            if total <= CACHE_MAX_BYTES * 0.8:
                break
            if entry != hit:
                entry.unlink(missing_ok=True)
                total -= size


def register(api: FastAPI, app: Application, auth: Callable[..., Any], *, store: CardImageStore | None = None) -> CardImageStore:
    pictures = store or CardImageStore(
        app.settings.state_dir / "card-images",
        proxy=lambda: app.config.tools.web.proxy,
        user_agent=lambda: app.config.tools.web.user_agent,
    )

    @api.get("/api/card-image")
    async def card_image(url: str, _: dict[str, Any] = Depends(auth)) -> Response:
        target = card_image_url(url)
        if not target:
            raise HTTPException(400, "not an address the host fetches pictures from")
        found = await pictures.picture(target)
        if found is None:
            raise HTTPException(404, "no picture")
        return Response(found[0], media_type=found[1], headers=_HEADERS)

    return pictures


__all__ = ["CardImageStore", "card_image_url", "picture_type", "register"]
