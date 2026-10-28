"""The favicon route fetches a site's icon once, through checks, and keeps it on disk."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daedalus.extensions.api_favicon import FaviconStore, icon_link, image_type, register, valid_host

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
ICO = b"\x00\x00\x01\x00" + b"\x00" * 24


def _store(tmp_path: Path, handler: Any, *, addresses: dict[str, list[str]] | None = None, proxy: str = "", clock: list[float] | None = None) -> tuple[FaviconStore, list[str]]:
    asked: list[str] = []
    table = addresses or {}

    async def resolver(host: str) -> list[str]:
        return table.get(host, ["93.184.216.34"])

    def recorded(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return handler(request)

    now = clock or [1_000_000.0]
    store = FaviconStore(tmp_path / "icons", proxy=lambda: proxy, user_agent=lambda: "test", resolver=resolver, transport=httpx.MockTransport(recorded), clock=lambda: now[0])
    return store, asked


async def test_the_icon_is_fetched_once_and_kept(tmp_path: Path) -> None:
    store, asked = _store(tmp_path, lambda request: httpx.Response(200, content=ICO))
    assert await store.icon("kaspi.kz") == (ICO, "image/x-icon")
    assert await store.icon("kaspi.kz") == (ICO, "image/x-icon")
    assert asked == ["https://kaspi.kz/favicon.ico"]


async def test_the_home_page_names_the_icon_when_there_is_no_favicon_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/favicon.ico":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(200, html='<html><head><link rel="shortcut icon" href="/static/i.png"></head></html>')
        return httpx.Response(200, content=PNG)

    store, asked = _store(tmp_path, handler)
    assert await store.icon("github.com") == (PNG, "image/png")
    assert asked[-1] == "https://github.com/static/i.png"


async def test_a_site_without_an_icon_is_remembered_for_a_day(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    store, asked = _store(tmp_path, lambda request: httpx.Response(200, html="<html>no icon</html>"), clock=clock)
    assert await store.icon("plain.example") is None
    assert await store.icon("plain.example") is None
    assert len(asked) == 2  # the favicon file and the home page, once
    # A day and more later, by the store's clock and the marker's own time.
    clock[0] += 2 * 24 * 3600
    for miss in (tmp_path / "icons").glob("*.miss"):
        os.utime(miss, (1_000_000.0, 1_000_000.0))
    assert await store.icon("plain.example") is None
    assert len(asked) == 4


async def test_a_page_that_is_not_an_image_or_too_large_is_not_an_icon(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=b"<svg onload='x'/>")
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 200_000)

    store, _ = _store(tmp_path, handler)
    assert await store.icon("svg.example") is None


async def test_a_name_that_resolves_inside_the_network_is_never_fetched(tmp_path: Path) -> None:
    store, asked = _store(tmp_path, lambda request: httpx.Response(200, content=PNG), addresses={"router.example": ["192.168.1.1"], "loop.example": ["127.0.0.1"]})
    assert await store.icon("router.example") is None
    assert await store.icon("loop.example") is None
    assert asked == []


async def test_a_redirect_into_the_network_is_refused_at_its_step(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "https://inside.example/favicon.ico"})
        return httpx.Response(200, content=PNG)

    store, asked = _store(tmp_path, handler, addresses={"inside.example": ["10.0.0.5"]})
    assert await store.icon("public.example") is None
    assert all("inside.example" not in url for url in asked)


def test_only_a_host_name_is_a_site() -> None:
    assert valid_host("Kaspi.KZ.") == "kaspi.kz"
    for bad in ("127.0.0.1", "localhost", "a.example/path", "a.example:8080", "", "-x.example", "[::1]"):
        assert valid_host(bad) == "", bad


def test_the_bytes_decide_the_type() -> None:
    assert image_type(PNG) == "image/png" and image_type(ICO) == "image/x-icon"
    assert image_type(b"GIF89a....") == "image/gif" and image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"<svg/>") == "" and image_type(b"<html>") == ""
    assert icon_link('<link href="/a.svg" rel="icon"><link rel=icon href=/b.ico>', "https://x.example/") == "https://x.example/b.ico"


def test_the_route_answers_an_image_or_a_refusal(tmp_path: Path) -> None:
    store, _ = _store(tmp_path, lambda request: httpx.Response(200, content=PNG) if request.url.host == "kaspi.kz" else httpx.Response(404))
    api = FastAPI()
    register(api, SimpleNamespace(), lambda: {"user_id": 1}, store=store)
    client = TestClient(api)
    found = client.get("/api/favicon", params={"host": "kaspi.kz"})
    assert found.status_code == 200 and found.content == PNG and found.headers["content-type"] == "image/png"
    assert found.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in found.headers["content-security-policy"]
    assert client.get("/api/favicon", params={"host": "none.example"}).status_code == 404
    assert client.get("/api/favicon", params={"host": "127.0.0.1"}).status_code == 400
