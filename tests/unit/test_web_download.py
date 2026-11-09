"""Files in and out of the workspace: WebFetch saving a file, BrowserDownload saying the same, SendFile zipping a folder.

A download is the agent's first contact with a file nobody vetted, so most of what is pinned here is what
it refuses: an address inside the network, a redirect that turns there, a web page that stands in for the
file, a file past the limit, a place outside the project's walls. What it keeps is named by a sha256 the
agent can repeat, and the result says the file is data from a site, not the operator's word.
"""

from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from protocore.contracts.tool_registry import ToolVisibilityPolicy
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig
from daedalus.host import guest_turns
from daedalus.host.containment import Walls
from daedalus.host.policy import approval_key
from daedalus.host.services import SessionServices, locator
from daedalus.tools.chat import send_file
from daedalus.tools.web import disposition_name, web_fetch

PDF = b"%PDF-1.7\n" + b"x" * 4000 + b"\n%%EOF\n"
TABLE = {"files.example": "93.184.216.34", "inside.example": "10.0.0.5", "meta.example": "169.254.169.254", "mapped.example": "::ffff:192.168.1.1", "127.0.0.1": "127.0.0.1"}


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names resolved from a table: no lookup leaves the test, and the asked names are kept."""
    asked: list[str] = []

    async def getaddrinfo(_loop: Any, host: str, port: int, **__: Any) -> list[Any]:
        asked.append(host)
        if host not in TABLE:
            raise OSError("no such name")
        address = TABLE[host]
        return [(10 if ":" in address else 2, 1, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return asked


def session(tmp_path: Path, *, walls: Walls | None = None, max_bytes: int | None = None) -> tuple[Path, ToolContext]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = RuntimeConfig()
    if max_bytes is not None:
        config.tools.web.download_max_bytes = max_bytes
    sid = f"dl-{tmp_path.name}"
    locator.register(SessionServices(session_id=sid, workspace_dir=workspace, walls=walls, extra={"manager": SimpleNamespace(config=config)}))
    return workspace, ToolContext(tenant_id="daedalus", run_id="r1", session_id=sid, metadata={"tool_call_id": "c1"})


@pytest.fixture(autouse=True)
def _unregister(tmp_path: Path) -> Any:
    yield
    locator.unregister(f"dl-{tmp_path.name}")


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*") if p.name.endswith(".part"))


@respx.mock
async def test_a_pdf_lands_in_the_folder_named_with_a_sha256_that_matches_its_bytes(tmp_path: Path, resolver: list[str]) -> None:
    """Download this PDF into docs/: the file is there, and the hash the agent is told is the file's own."""
    route = respx.get("https://93.184.216.34:443/papers/latest").mock(return_value=httpx.Response(
        200, content=PDF, headers={"content-type": "application/pdf", "content-disposition": "attachment; filename=\"report.pdf\"; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf"},
    ))
    workspace, ctx = session(tmp_path)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/papers/latest", "save_to": "docs/"})
    assert not result.is_error, result.content
    saved = workspace / "docs" / "отчёт.pdf"
    assert saved.read_bytes() == PDF
    digest = hashlib.sha256(PDF).hexdigest()
    assert f"sha256: {digest}" in result.content and f"size: {len(PDF)} bytes" in result.content and f"path: {saved}" in result.content
    assert result.metadata["sha256"] == digest and result.metadata["size"] == len(PDF) and result.metadata["path"] == str(saved)
    assert "untrusted" in result.content and "https://files.example" in result.content and "do not run it" in result.content
    assert "check it" not in result.content, "a PDF that is a PDF raises nothing"
    # The connection went to the address that was checked, under the site's own name.
    request = route.calls[0].request
    assert request.headers["host"] == "files.example" and request.extensions["sni_hostname"] == "files.example"
    assert resolver == ["files.example"] and leftovers(workspace) == []


@respx.mock
async def test_a_name_the_agent_gave_is_kept_and_a_name_the_site_gave_never_replaces_a_file(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/a/manual.pdf").mock(return_value=httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"}))
    workspace, ctx = session(tmp_path)
    (workspace / "docs").mkdir()
    (workspace / "docs" / "manual.pdf").write_bytes(b"mine")
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/a/manual.pdf", "save_to": "docs"})
    assert not result.is_error and (workspace / "docs" / "manual-1.pdf").read_bytes() == PDF
    assert (workspace / "docs" / "manual.pdf").read_bytes() == b"mine"
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/a/manual.pdf", "save_to": "docs/manual.pdf"})
    assert not result.is_error and (workspace / "docs" / "manual.pdf").read_bytes() == PDF


@pytest.mark.parametrize("host", ["inside.example", "meta.example", "mapped.example", "127.0.0.1", "nowhere.example"])
async def test_an_address_off_the_public_internet_is_not_fetched(tmp_path: Path, resolver: list[str], host: str) -> None:
    workspace, ctx = session(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        result = await web_fetch().invoke(ctx, {"url": f"http://{host}/file.pdf", "save_to": "docs/"})
        assert not mock.calls
    assert result.is_error and ("public internet" in result.content or "does not resolve" in result.content), result.content
    assert not (workspace / "docs").exists() or not any((workspace / "docs").iterdir())


@respx.mock
async def test_a_redirect_is_checked_as_the_first_address_was(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/go").mock(return_value=httpx.Response(302, headers={"location": "http://meta.example/latest/credentials"}))
    inside = respx.get(url__regex=r"http://169\.254\.169\.254.*").mock(return_value=httpx.Response(200, content=b"secret"))
    workspace, ctx = session(tmp_path)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/go", "save_to": "docs/x.pdf"})
    assert result.is_error and "public internet" in result.content and not inside.called
    assert not (workspace / "docs" / "x.pdf").exists() and leftovers(workspace) == []


@respx.mock
async def test_a_public_redirect_is_followed_and_the_file_named_after_where_it_ended(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/latest").mock(return_value=httpx.Response(301, headers={"location": "/v2/guide.pdf"}))
    respx.get("https://93.184.216.34:443/v2/guide.pdf").mock(return_value=httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"}))
    workspace, ctx = session(tmp_path)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/latest", "save_to": "docs/"})
    assert not result.is_error and (workspace / "docs" / "guide.pdf").read_bytes() == PDF
    assert "Saved https://files.example/v2/guide.pdf" in result.content


@respx.mock
async def test_a_web_page_where_a_file_was_asked_for_is_refused(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/paper.pdf").mock(return_value=httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html; charset=utf-8"}))
    workspace, ctx = session(tmp_path)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/paper.pdf", "save_to": "docs/"})
    assert result.is_error and "web page" in result.content and "BrowserDownload" in result.content
    assert not (workspace / "docs" / "paper.pdf").exists() and leftovers(workspace) == []


@respx.mock
async def test_a_file_that_is_not_what_its_name_says_is_saved_with_a_warning(tmp_path: Path, resolver: list[str]) -> None:
    respx.get("https://93.184.216.34:443/paper.pdf").mock(return_value=httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"}))
    workspace, ctx = session(tmp_path)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/paper.pdf", "save_to": "docs/"})
    assert not result.is_error and "check it:" in result.content and "does not begin as a .pdf" in result.content


@respx.mock
@pytest.mark.parametrize("declared", [True, False])
async def test_a_file_past_the_limit_is_cut_off_and_nothing_is_kept(tmp_path: Path, resolver: list[str], declared: bool) -> None:
    big = b"\0" * ((1 << 20) + 10)

    def answer(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "application/octet-stream"}
        if not declared:
            # A server that does not say the size up front is cut off as the bytes come.
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(big))
        return httpx.Response(200, headers=headers, content=big)

    respx.get("https://93.184.216.34:443/big.bin").mock(side_effect=answer)
    workspace, ctx = session(tmp_path, max_bytes=1 << 20)
    result = await web_fetch().invoke(ctx, {"url": "https://files.example/big.bin", "save_to": "data/"})
    assert result.is_error and "nothing was saved" in result.content, result.content
    assert not (workspace / "data" / "big.bin").exists() and leftovers(workspace) == []


async def test_a_place_outside_the_walls_or_read_only_is_refused_before_anything_is_fetched(tmp_path: Path, resolver: list[str]) -> None:
    project = tmp_path / "ws"
    project.mkdir()
    reference = tmp_path / "reference"
    reference.mkdir()
    _, ctx = session(tmp_path, walls=Walls(readable=(project, reference), writable=(project,)))
    with respx.mock(assert_all_called=False) as mock:
        outside = await web_fetch().invoke(ctx, {"url": "https://files.example/a.pdf", "save_to": "../elsewhere/"})
        read_only = await web_fetch().invoke(ctx, {"url": "https://files.example/a.pdf", "save_to": str(reference / "a.pdf")})
        assert not mock.calls
    assert outside.is_error and "outside this project" in outside.content
    assert read_only.is_error and "read-only" in read_only.content
    assert not resolver, "refused before the name was even looked up"


def test_the_name_a_site_gives_is_read_the_way_rfc_6266_says() -> None:
    assert disposition_name("attachment; filename=\"plain.pdf\"") == "plain.pdf"
    assert disposition_name("attachment; filename=bare.txt") == "bare.txt"
    assert disposition_name("attachment; filename=\"a \\\"quoted\\\" name.pdf\"") == "a \"quoted\" name.pdf"
    assert disposition_name("attachment; filename=\"ascii.pdf\"; filename*=UTF-8''%E2%82%AC%20rates.pdf") == "€ rates.pdf"
    assert disposition_name("attachment; filename*=nonsense''%FF; filename=\"fallback.pdf\"") == "fallback.pdf"
    assert disposition_name("inline") == ""


async def test_a_name_with_a_path_in_it_stays_in_the_folder(tmp_path: Path, resolver: list[str]) -> None:
    workspace, ctx = session(tmp_path)
    with respx.mock:
        respx.get("https://93.184.216.34:443/x").mock(return_value=httpx.Response(200, content=b"data", headers={"content-type": "text/plain", "content-disposition": "attachment; filename=\"../../.bashrc\""}))
        result = await web_fetch().invoke(ctx, {"url": "https://files.example/x", "save_to": "in/"})
    assert not result.is_error and (workspace / "in" / "bashrc").read_bytes() == b"data"
    assert not (tmp_path / ".bashrc").exists()


def test_saving_stays_out_of_guest_turns_and_the_grant_covers_the_place_it_saves_to() -> None:
    """WebFetch is out of every guest turn, whatever the link allows; the touched-run hold is pinned in test_agent_talk."""
    for tools in ("none", "read", "all"):
        turn = guest_turns.GuestTurn.from_metadata({"link_id": "x", "tools": tools})
        assert turn is not None
        policy = guest_turns.narrowed(ToolVisibilityPolicy(), turn, ("WebFetch", "Read", "Write"))
        assert "WebFetch" in policy.blocked, tools
    # A yes to fetching a page is not a yes to saving it somewhere, nor to saving it somewhere else.
    url = "https://files.example/a.pdf"
    assert len({approval_key("WebFetch", {"url": url}), approval_key("WebFetch", {"url": url, "save_to": "docs/"}), approval_key("WebFetch", {"url": url, "save_to": "bin/"})}) == 3


# -- SendFile of a folder -------------------------------------------------------------------------------


async def test_a_folder_goes_as_a_zip_beside_it_without_what_holds_credentials(tmp_path: Path) -> None:
    workspace, ctx = session(tmp_path)
    folder = workspace / "report"
    (folder / "figures").mkdir(parents=True)
    (folder / "index.md").write_text("# Report\n")
    (folder / "figures" / "one.png").write_bytes(b"\x89PNG one")
    (folder / ".env").write_text("TOKEN=hidden\n")
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[core]\n")
    (folder / "escape").symlink_to(tmp_path / "secret.txt")
    (tmp_path / "secret.txt").write_text("outside")
    services = locator.get(ctx.session_id)
    services.walls = Walls(readable=(workspace,), writable=(workspace,))
    services.send_file = None
    result = await send_file().invoke(ctx, {"path": "report/", "zip": True})
    assert not result.is_error, result.content
    archive = workspace / "report.zip"
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["figures/one.png", "index.md"]
        assert bundle.read("figures/one.png") == b"\x89PNG one"
    assert f"shown report.zip ({archive.stat().st_size} bytes)" in result.content and "2 files" in result.content
    assert ".env" in result.content and "escape" in result.content, "what was left out is named"


async def test_a_folder_without_zip_or_a_file_with_it_is_told_what_to_do(tmp_path: Path) -> None:
    workspace, ctx = session(tmp_path)
    (workspace / "report").mkdir()
    (workspace / "report" / "a.txt").write_text("a")
    (workspace / "a.txt").write_text("a")
    locator.get(ctx.session_id).send_file = None
    folder = await send_file().invoke(ctx, {"path": "report"})
    assert folder.is_error and "zip=true" in folder.content and "report.zip" in folder.content
    single = await send_file().invoke(ctx, {"path": "a.txt", "zip": True})
    assert single.is_error and "not one" in single.content
    whole = await send_file().invoke(ctx, {"path": ".", "zip": True})
    assert whole.is_error and "inside the workspace" in whole.content


async def test_a_folder_past_the_bound_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.tools import chat

    monkeypatch.setattr(chat, "ZIP_MAX_BYTES", 1000)
    workspace, ctx = session(tmp_path)
    (workspace / "big").mkdir()
    (workspace / "big" / "a.bin").write_bytes(b"\0" * 600)
    (workspace / "big" / "b.bin").write_bytes(b"\0" * 600)
    locator.get(ctx.session_id).send_file = None
    result = await send_file().invoke(ctx, {"path": "big", "zip": True})
    assert result.is_error and "MiB" in result.content and not (workspace / "big.zip").exists()
