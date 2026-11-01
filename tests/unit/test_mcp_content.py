"""An MCP answer's pictures reach the model as pictures, and its resources as text it can read."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from mcp import types as mcp_types
from PIL import Image
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import (
    Message,
    MessageRole,
    ToolDefinition,
    ToolParameterSchema,
    ToolResultBlock,
    ToolResultImage,
    ToolUseBlock,
)

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import TENANT, SessionManager
from daedalus.host.transcript_view import message_view
from daedalus.mcp import content
from daedalus.mcp.content import MAX_IMAGES, MAX_RESOURCE_CHARS, read_content
from daedalus.mcp.manager import McpToolProxy, SecretVault
from daedalus.stores.database import Database


def _picture(kind: str = "PNG", size: tuple[int, int] = (24, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buffer, format=kind)
    return buffer.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_a_screenshot_becomes_a_picture_and_the_text_still_names_it() -> None:
    png = _picture()
    text, images = read_content([
        mcp_types.TextContent(type="text", text="clicked the button"),
        mcp_types.ImageContent(type="image", data=_b64(png), mimeType="image/png"),
    ])
    assert len(images) == 1 and images[0].data == png and images[0].mime_type == "image/png"
    assert images[0].metadata == {"source": "mcp"}
    assert text.splitlines()[0] == "clicked the button"
    assert "image image/png" in text and "picture 1" in text


def test_the_bytes_decide_the_type_and_what_is_no_picture_is_refused() -> None:
    jpeg = _picture("JPEG")
    text, images = read_content([
        # A server that calls its JPEG a PNG: the provider checks the bytes against the declared type.
        {"type": "image", "data": _b64(jpeg), "mimeType": "image/png"},
        {"type": "image", "data": _b64(b"%PDF-1.7 not a picture"), "mimeType": "image/png"},
        {"type": "image", "data": "not base64 at all!", "mimeType": "image/png"},
    ])
    assert [image.mime_type for image in images] == ["image/jpeg"]
    assert "not a picture a model reads" in text and "not base64" in text


def test_pictures_are_capped_by_count_and_by_size(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    png = _b64(_picture())
    text, images = read_content([{"type": "image", "data": png, "mimeType": "image/png"}] * (MAX_IMAGES + 2))
    assert len(images) == MAX_IMAGES
    assert text.count(f"this result already carries {MAX_IMAGES} pictures") == 2
    monkeypatch.setattr(content, "MAX_IMAGE_BYTES", 10)
    text, images = read_content([{"type": "image", "data": png, "mimeType": "image/png"}])
    assert images == () and "too large to pass on" in text


def test_embedded_resources_become_text_pictures_or_a_short_description() -> None:
    long_text = "line\n" * (MAX_RESOURCE_CHARS // 2)
    png = _picture()
    text, images = read_content([
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.TextResourceContents(uri="file:///notes.md", mimeType="text/markdown", text="# Notes\nfirst")),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.TextResourceContents(uri="file:///big.log", text=long_text)),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.BlobResourceContents(uri="file:///shot.png", mimeType="image/png", blob=_b64(png))),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.BlobResourceContents(uri="file:///raw", blob=_b64(png))),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.BlobResourceContents(uri="file:///report.pdf", mimeType="application/pdf", blob=_b64(b"%PDF" + b"x" * 2044))),
        mcp_types.AudioContent(type="audio", data=_b64(b"RIFF" + b"\0" * 100), mimeType="audio/wav"),
        mcp_types.ResourceLink(type="resource_link", uri="https://example.com/a", name="a page", mimeType="text/html"),
    ])
    assert "[resource file:///notes.md (text/markdown)]\n# Notes\nfirst" in text
    assert "more characters of this resource left out" in text and len(text) < MAX_RESOURCE_CHARS + 2_000
    assert [image.metadata.get("uri") for image in images] == ["file:///shot.png", "file:///raw"]
    assert "[resource file:///report.pdf: application/pdf, 2 KB, a file the tool attached; not shown]" in text
    assert "[audio audio/wav, 104 bytes, returned by the tool; not passed on]" in text
    assert "[resource link a page: https://example.com/a (text/html)]" in text
    assert '"blob"' not in text  # no base64 dump reaches the model


async def test_the_proxy_hands_the_core_its_pictures_and_fences_the_text() -> None:
    png = _picture()

    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        return mcp_types.CallToolResult(content=[
            mcp_types.TextContent(type="text", text="the page"),
            mcp_types.ImageContent(type="image", data=_b64(png), mimeType="image/png"),
        ])

    connection = SimpleNamespace(name="browser", vault=SecretVault(), call=call)
    proxy = McpToolProxy(connection, "screenshot", ToolDefinition(name="Mcp_Browser_screenshot", description="", parameters=ToolParameterSchema(type="object", properties={})))  # type: ignore[arg-type]
    result = await proxy.invoke(ToolContext(tenant_id="t", session_id="s", run_id="r", metadata={"tool_call_id": "c1"}), {})
    assert result.tool_call_id == "c1" and not result.is_error
    assert [image.data for image in result.images] == [png]
    assert result.content.startswith("[tool output from MCP server browser") and "the page" in result.content
    # A command-line member is handed the text alone; it still says a picture came back.
    answer = await proxy.run({}, limit=10_000)
    assert "picture 1" in answer.text and len(answer.images) == 1


async def test_the_app_lists_a_results_pictures_and_fetches_each_by_its_call(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    state = await manager.create_session("pictures")
    sid = state.session.id
    png, jpeg = _picture(), _picture("JPEG")
    stored = await manager.blobs.put(TENANT, png, content_type="image/png")
    result = ToolResultBlock(tool_call_id="c-shot", content="shot", images=(
        ToolResultImage(blob_ref=stored.ref, mime_type="image/png", metadata={"source": "mcp", "uri": "file:///page.png"}),
        ToolResultImage(data=jpeg, mime_type="image/jpeg"),
    ))
    await manager.sessions.append_transcript(sid, [
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c-shot", name="Mcp_Browser_screenshot", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[result]),
    ])
    listed = message_view(Message(role=MessageRole.tool, content_blocks=[result]))["tool_results"][0]
    assert listed["images"] == [{"mime_type": "image/png", "uri": "file:///page.png"}, {"mime_type": "image/jpeg"}]
    application = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    api = build_app(application, "tok")  # type: ignore[arg-type]
    headers = {"X-Daedalus-Token": "tok"}
    base = f"/api/sessions/{sid}/tool-results/c-shot/images/download"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        first = await client.get(base, params={"path": "image-1.png"}, headers=headers)
        assert first.status_code == 200 and first.content == png and first.headers["content-type"] == "image/png"
        second = await client.get(base, params={"path": "image-2.jpg"}, headers=headers)
        assert second.status_code == 200 and second.content == jpeg and second.headers["content-type"] == "image/jpeg"
        for path in ("image-3.png", "image-0.png", "../blob", stored.ref):
            assert (await client.get(base, params={"path": path}, headers=headers)).status_code == 404, path
        other = await client.get(f"/api/sessions/{sid}/tool-results/c-none/images/download", params={"path": "image-1.png"}, headers=headers)
        assert other.status_code == 404
        assert (await client.get(base, params={"path": "image-1.png"})).status_code == 401
    await manager.close()
