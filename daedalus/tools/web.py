"""Web tools: fetch a page as text or save a file it serves, search the web."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.setting_refs import WEB_SEARCH
from daedalus.security.untrusted import fenced, injection_signs, origin_of
from daedalus.stores.files import safe_name, saved_report
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


DOWNLOAD_REDIRECTS = 5
"""Redirects a download follows, each one checked as the first address was."""

_WEB_PAGE_TYPES = ("text/html", "application/xhtml+xml")
_WEB_PAGE_SUFFIXES = (".html", ".htm", ".xhtml")
_MAGIC = {".pdf": b"%PDF-", ".zip": b"PK\x03\x04", ".docx": b"PK\x03\x04", ".xlsx": b"PK\x03\x04", ".pptx": b"PK\x03\x04", ".png": b"\x89PNG", ".gz": b"\x1f\x8b"}
"""How a few common files begin, to say when a saved file is not what its name claims."""


class DownloadRefused(Exception):
    """Why a download was not made, in words the model reads."""


async def public_address(host: str, port: int) -> str:
    """The address to connect to for ``host``, refused unless every address it has is on the public internet.

    Every address and not any: a name with one public and one private address is how a page aims a
    client at the inside of the network it runs in, and which of the two is used is the resolver's
    choice. An IPv4 address written as IPv6 is judged as the IPv4 one it is.
    """
    if not host:
        raise DownloadRefused("the address names no host")
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise DownloadRefused(f"{host} does not resolve") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in found))
    if not addresses:
        raise DownloadRefused(f"{host} does not resolve")
    for address in addresses:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        if not (getattr(parsed, "ipv4_mapped", None) or parsed).is_global:
            raise DownloadRefused(f"{host} is not on the public internet ({address}); a file is saved only from a public site")
    return addresses[0]


async def _pinned(url: str, proxy: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """``url`` rewritten to the address that was checked, with the name kept for the Host header and TLS.

    Resolved twice, the second answer would be the site's to choose, and a name that was public when
    it was checked could point at this machine when it is dialled. Through a proxy the proxy dials,
    so the name goes to it, after the same check here.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise DownloadRefused(f"only an http or https address is downloaded, not {parts.scheme or 'this'}")
    host = parts.hostname or ""
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise DownloadRefused("that address has no valid port") from None
    address = await public_address(host, port)
    if proxy:
        return url, {}, {}
    named = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
    place = f"[{address}]" if ":" in address else address
    target = parts._replace(netloc=f"{place}:{port}").geturl()
    return target, {"host": named}, ({"sni_hostname": host} if parts.scheme == "https" else {})


_STAR_RE = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_PLAIN_RE = re.compile(r"filename\s*=\s*(\"(?:[^\"\\]|\\.)*\"|[^;]+)", re.IGNORECASE)


def disposition_name(header: str) -> str:
    """The file name a ``Content-Disposition`` header gives, or an empty string.

    The encoded form (``filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf``) wins over the plain one,
    as RFC 6266 says: a site that sends both puts a readable name in the first and an ASCII stand-in
    in the second. The name is only a suggestion; :func:`safe_name` makes it one plain segment.
    """
    if (star := _STAR_RE.search(header)) is not None:
        charset, _, rest = star.group(1).strip().strip('"').partition("'")
        _, _, encoded = rest.partition("'")
        try:
            if name := unquote(encoded, encoding=charset or "utf-8", errors="strict").strip():
                return name
        except (LookupError, UnicodeDecodeError):
            pass
    if (plain := _PLAIN_RE.search(header)) is not None:
        value = plain.group(1).strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        return value.strip()
    return ""


def _name_for(header: str, url: str, content_type: str) -> str:
    """The name a download is saved under when the agent named only a folder: the site's, else the
    address's last segment, else ``download``, with an extension from the type when it has none and
    the type says more than "text" or "bytes" (a ``Makefile`` is not a ``Makefile.txt``)."""
    raw = disposition_name(header) or unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = safe_name(raw) if raw.strip() else "download"
    vague = content_type in ("", "text/plain", "application/octet-stream")
    if not Path(name).suffix and not vague and (extension := mimetypes.guess_extension(content_type)):
        name += extension
    return name


def _type_notes(content_type: str, name: str, head: bytes) -> list[str]:
    """What looks wrong between the file's name, the type the site declared and the bytes it sent."""
    suffix = Path(name).suffix.lower()
    notes: list[str] = []
    guessed = mimetypes.guess_type(name)[0] or ""
    if guessed and content_type and content_type != "application/octet-stream" and guessed != content_type and guessed.split("/")[0] != content_type.split("/")[0]:
        notes.append(f"the name says {guessed}, the site said {content_type}")
    if (magic := _MAGIC.get(suffix)) is not None and not head.startswith(magic):
        notes.append(f"it does not begin as a {suffix} file does")
    return notes


async def _destination(services: Any, save_to: str) -> tuple[Path, str]:
    """The folder a download goes into and the name the agent gave it, empty when it named a folder.

    A path that ends in a slash, or names a folder that exists, is a folder; anything else is the
    file itself. Resolved for writing, so a read-only folder and a path out of the project's walls are
    refused here, before anything is fetched.
    """
    raw = save_to.strip()
    target = services.resolve(raw or ".", write=True)
    if services.is_protected(target):
        raise DownloadRefused(f"{target} is protected and cannot be written by tools")
    if raw in ("", ".") or raw.endswith(("/", "\\")) or await services.fs.is_dir(target):
        return target, ""
    return target.parent, target.name


async def _free_name(fs: Any, folder: Path, name: str, *, keep: bool) -> Path:
    """Where the file lands: the name the agent gave, replaced; a name the site chose never replaces a
    file, and gets a number instead."""
    target = folder / name
    if keep:
        return target
    final, n = target, 1
    while await fs.exists(final):
        final = target.with_name(f"{target.stem}-{n}{target.suffix}")
        n += 1
    return final


async def _download(context: ToolContext, url: str, save_to: str) -> ToolResult:
    """WebFetch with ``save_to``: the file streamed into the workspace, never into the model's context.

    Only from the public internet, and every redirect is checked as the first address was: a page
    that answers with a redirect to the router, the cloud's metadata address or this host's own API
    gets nothing. The bytes go to a temporary file first, counted and hashed as they come; past the
    size limit the transfer is cut and nothing is kept. A web page where a file was asked for (the
    sign-in page a PDF link returns to a client with no session) is refused rather than saved under
    the PDF's name.
    """
    services = services_for(context)
    web = tool_config(context).web
    fs = services.fs
    limit = web.download_max_bytes
    try:
        folder, named = await _destination(services, save_to)
    except PermissionError as exc:
        return error(context, str(exc))
    except DownloadRefused as exc:
        return error(context, str(exc))
    current = url
    spool = Path(tempfile.gettempdir()) / f"daedalus-download-{uuid.uuid4().hex}.part"
    if not fs.remote:
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        # Beside the target, so the last step is a rename on one filesystem rather than a copy.
        spool = folder / f".download-{uuid.uuid4().hex}.part"
    try:
        async with httpx.AsyncClient(timeout=web.fetch_timeout_seconds, follow_redirects=False, headers={"user-agent": web.user_agent}, proxy=web.proxy or None, trust_env=False) as client:
            for _ in range(DOWNLOAD_REDIRECTS + 1):
                target, headers, extensions = await _pinned(current, web.proxy)
                async with client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadRefused(f"HTTP {response.status_code} from {current} with nowhere to go")
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise DownloadRefused(f"HTTP {response.status_code} from {current}; nothing was saved")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    name = named or _name_for(response.headers.get("content-disposition", ""), current, content_type)
                    if content_type in _WEB_PAGE_TYPES and Path(name).suffix.lower() not in _WEB_PAGE_SUFFIXES:
                        raise DownloadRefused(
                            f"{current} answered with a web page ({content_type}), not a file: most often a sign-in, a cookie wall or an "
                            "error page. Read it with WebFetch without save_to; a file behind a sign-in comes through the browser "
                            "(BrowserOpen, then BrowserDownload)."
                        )
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise DownloadRefused(f"{current} is {declared} bytes, more than the {limit} bytes a download may be; nothing was saved")
                    digest, size, head = hashlib.sha256(), 0, b""
                    with spool.open("wb") as sink:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise DownloadRefused(f"{current} sent more than the {limit} bytes a download may be; it was cut off and nothing was saved")
                            digest.update(chunk)
                            if len(head) < 16:
                                head += chunk[: 16 - len(head)]
                            sink.write(chunk)
                    break
            else:
                raise DownloadRefused(f"more than {DOWNLOAD_REDIRECTS} redirects from {url}; nothing was saved")
        final = await _free_name(fs, folder, name, keep=bool(named))
        if services.is_protected(final):
            raise DownloadRefused(f"{final} is protected and cannot be written by tools")
        if fs.remote:
            # The file belongs where the session works: on the host for a session working there.
            await fs.write_bytes(final, await asyncio.to_thread(spool.read_bytes))
        else:
            await asyncio.to_thread(os.replace, spool, final)
    except DownloadRefused as exc:
        return error(context, str(exc))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return error(context, f"download failed: {type(exc).__name__}: {exc}")
    except OSError as exc:
        return error(context, f"could not save the download: {exc}")
    finally:
        await asyncio.to_thread(spool.unlink, missing_ok=True)
    sha256 = digest.hexdigest()
    notes = _type_notes(content_type, final.name, head)
    report = saved_report(str(final), size, sha256, source=origin_of(current), content_type=content_type)
    warning = f"\ncheck it: {'; '.join(notes)}" if notes else ""
    return ok(context, f"Saved {current} to {final}.\n{report}{warning}", path=str(final), sha256=sha256, size=size)


@search_hint(
    "fetch url download page raw text read link content http save file pdf into workspace "
    "скачать страницу ссылке ссылку содержимое сырой текст забрать забери урл скачай файл сохрани положи"
)
@tool(
    name="WebFetch",
    description=(
        "Fetch a URL and return its content as plain text (HTML is converted). save_to (a workspace folder or file "
        "path) saves the file there instead, within a size limit, and returns its path, size and sha256."
    ),
)
async def web_fetch(context: ToolContext, url: str, max_chars: int | None = None, save_to: str | None = None) -> ToolResult:
    if save_to is not None:
        return await _download(context, url, save_to)
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
        return ok(context, f"HTTP {response.status_code}, {content_type}, {len(response.content)} bytes (binary; pass save_to to save it in the workspace)")
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

__all__ = ["TOOLS", "DownloadRefused", "disposition_name", "html_to_text", "page_head", "public_address"]
