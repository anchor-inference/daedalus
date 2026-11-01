"""A rich answer's card picture is fetched by the host, through checks, without its query string, and kept."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daedalus.extensions.api_card_image import (
    CARD_IMAGE_MAX_BYTES,
    CardImageStore,
    card_image_url,
    picture_type,
    register,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 64


def _store(tmp_path: Path, handler: Any, *, addresses: dict[str, list[str]] | None = None, proxy: str = "") -> tuple[CardImageStore, list[httpx.Request]]:
    asked: list[httpx.Request] = []
    table = addresses or {}

    async def resolver(host: str) -> list[str]:
        return table.get(host, ["93.184.216.34"])

    def recorded(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return handler(request)

    store = CardImageStore(tmp_path / "pictures", proxy=lambda: proxy, user_agent=lambda: "test", resolver=resolver, transport=httpx.MockTransport(recorded))
    return store, asked


def _png(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


async def test_the_picture_is_fetched_once_without_its_query_and_kept(tmp_path: Path) -> None:
    store, asked = _store(tmp_path, _png)
    url = card_image_url("https://img.example/p/pantum.png?token=secret-from-the-page#frag")
    assert url == "https://img.example/p/pantum.png"
    assert await store.picture(url) == (PNG, "image/png")
    assert await store.picture(url) == (PNG, "image/png")
    assert [str(request.url) for request in asked] == ["https://img.example/p/pantum.png"]
    sent = asked[0].headers
    assert "cookie" not in sent and "referer" not in sent and sent["user-agent"] == "test"


def test_only_a_public_https_address_is_fetched() -> None:
    assert card_image_url("HTTPS://Img.Example./a.jpg?w=640") == "https://img.example/a.jpg"
    for bad in (
        "http://img.example/a.png",
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://localhost/a.png",
        "https://img.example:8443/a.png",
        "https://user:pass@img.example/a.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "/api/sessions",
        "",
    ):
        assert card_image_url(bad) == "", bad


async def test_a_name_that_resolves_inside_the_network_is_never_fetched(tmp_path: Path) -> None:
    store, asked = _store(tmp_path, _png, addresses={"router.example": ["192.168.1.1"], "loop.example": ["127.0.0.1"], "meta.example": ["169.254.169.254"], "mixed.example": ["93.184.216.34", "10.0.0.2"]})
    for host in ("router.example", "loop.example", "meta.example", "mixed.example"):
        assert await store.picture(f"https://{host}/a.png") is None, host
    assert asked == []


async def test_a_redirect_into_the_network_is_refused_at_its_step(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "https://inside.example/a.png"})
        return _png(request)

    store, asked = _store(tmp_path, handler, addresses={"inside.example": ["10.0.0.5"]})
    assert await store.picture("https://cdn.example/a.png") is None
    assert all(request.url.host != "inside.example" for request in asked)


async def test_a_redirect_on_the_public_web_is_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short.example":
            return httpx.Response(301, headers={"location": "https://cdn.example/real.jpg?size=m"})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    store, asked = _store(tmp_path, handler)
    assert await store.picture("https://short.example/x") == (JPEG, "image/jpeg")
    assert str(asked[-1].url) == "https://cdn.example/real.jpg?size=m", "the site's own redirect keeps its query"


async def test_only_a_raster_picture_within_the_cap_comes_back(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/svg":
            return httpx.Response(200, content=b"<svg onload='x'/>", headers={"content-type": "image/svg+xml"})
        if path == "/html":
            return httpx.Response(200, content=b"<html>hi</html>", headers={"content-type": "text/html"})
        if path == "/lies":
            return httpx.Response(200, content=b"<html>not a png</html>", headers={"content-type": "image/png"})
        if path == "/huge":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * CARD_IMAGE_MAX_BYTES, headers={"content-type": "image/png"})
        if path == "/avif":
            return httpx.Response(200, content=AVIF, headers={"content-type": "image/avif"})
        return httpx.Response(404)

    store, _ = _store(tmp_path, handler)
    for path in ("/svg", "/html", "/lies", "/huge", "/missing"):
        assert await store.picture(f"https://img.example{path}") is None, path
    assert await store.picture("https://img.example/avif") == (AVIF, "image/avif")
    assert picture_type(b"\x00\x00\x01\x00icon") == "", "an icon is not a card picture"


def test_a_connection_that_landed_inside_the_network_is_refused() -> None:
    # The name passed the check, then resolved again to this machine for the connection itself.
    store = CardImageStore(Path("/nonexistent"), proxy=lambda: "", user_agent=lambda: "test")

    class Stream:
        def __init__(self, address: str) -> None:
            self.address = address

        def get_extra_info(self, name: str) -> Any:
            return (self.address, 443) if name == "server_addr" else None

    inside = httpx.Response(200, extensions={"network_stream": Stream("127.0.0.1")})
    outside = httpx.Response(200, extensions={"network_stream": Stream("93.184.216.34")})
    assert not store._connected_publicly(inside) and store._connected_publicly(outside)


def test_the_route_answers_a_picture_or_a_refusal(tmp_path: Path) -> None:
    store, asked = _store(tmp_path, lambda request: _png(request) if request.url.host == "img.example" else httpx.Response(404))
    api = FastAPI()
    register(api, SimpleNamespace(), lambda: {"user_id": 1}, store=store)
    client = TestClient(api)
    found = client.get("/api/card-image", params={"url": "https://img.example/a.png?leak=1"})
    assert found.status_code == 200 and found.content == PNG and found.headers["content-type"] == "image/png"
    assert found.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in found.headers["content-security-policy"]
    assert str(asked[-1].url) == "https://img.example/a.png"
    assert client.get("/api/card-image", params={"url": "https://none.example/a.png"}).status_code == 404
    assert client.get("/api/card-image", params={"url": "https://10.0.0.1/a.png"}).status_code == 400
    assert client.get("/api/card-image", params={"url": "http://img.example/a.png"}).status_code == 400
