"""Active kept files must not acquire the app's origin when opened directly."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from daedalus.extensions.api_files import register


@pytest.mark.parametrize("mime", ["text/html", "text/html; charset=utf-8", "application/xhtml+xml", "image/svg+xml"])
async def test_active_artifact_bytes_and_names_survive_without_app_authority(tmp_path: Path, mime: str) -> None:
    source = tmp_path / "sample.html"
    source.write_bytes(b"<script>fetch('/api/private')</script>")
    item = SimpleNamespace(id="file", name="sample.html", mime=mime)

    class Files:
        async def get(self, identity: str) -> object | None:
            return item if identity == item.id else None

        def path_of(self, stored: object) -> Path:
            assert stored is item
            return source

    async def auth(request: Request) -> dict[str, str]:
        if request.headers.get("x-fixture-auth") != "operator":
            raise HTTPException(401)
        return {"via": "token"}

    api = FastAPI()
    register(api, SimpleNamespace(manager=SimpleNamespace(files=Files())), auth)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        denied = await client.get("/api/files/file/download")
        assert denied.status_code == 401
        response = await client.get("/api/files/file/download", headers={"x-fixture-auth": "operator"})
        assert response.status_code == 200 and response.content == source.read_bytes()
        assert "sample.html" in response.headers["content-disposition"]
        assert response.headers["x-content-type-options"] == "nosniff"
        policy = response.headers["content-security-policy"]
        assert "sandbox allow-scripts;" in policy and "allow-same-origin" not in policy
        assert "connect-src 'none'" in policy and "form-action 'none'" in policy


async def test_plain_artifact_keeps_inline_delivery_and_cannot_be_sniffed_as_html(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("<script>window.probe = true</script>")
    item = SimpleNamespace(id="file", name="notes.txt", mime="text/plain")

    class Files:
        async def get(self, identity: str) -> object:
            return item

        def path_of(self, stored: object) -> Path:
            return source

    async def auth() -> dict[str, str]:
        return {"via": "token"}

    api = FastAPI()
    register(api, SimpleNamespace(manager=SimpleNamespace(files=Files())), auth)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/files/file/download")
        assert response.status_code == 200 and response.text == source.read_text()
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["x-content-type-options"] == "nosniff"
