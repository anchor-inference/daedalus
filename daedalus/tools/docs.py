"""Read the public documentation shipped with this exact installation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.help_search import EXCLUDED_DOCS, ROOT_PAGES, describe, help_index, parse_query
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok, services_for

MAX_RESULTS = 20
MAX_CHUNK = 16 * 1024
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Page:
    id: str
    source: str
    title: str
    text: str
    headings: tuple[tuple[str, int], ...]


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.casefold()).strip("-")


def _root(context: ToolContext) -> Path | None:
    manager = services_for(context).extra.get("manager")
    root = getattr(getattr(manager, "settings", None), "bot_repo_dir", None)
    return Path(root).resolve() if root else None


def _pages(root: Path) -> tuple[list[Page], str]:
    paths = [root / name for name in ROOT_PAGES]
    docs = root / "docs"
    if docs.is_dir():
        paths.extend(path for path in docs.rglob("*.md") if path.name not in EXCLUDED_DOCS)
    pages: list[Page] = []
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(root)
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        source = relative.as_posix()
        digest.update(source.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        matches = tuple((_slug(match.group(2)), match.start()) for match in HEADING.finditer(text))
        first = next((match.group(2).strip() for match in HEADING.finditer(text)), path.stem)
        page_id = source.removesuffix(".md").casefold().replace("/", ":")
        pages.append(Page(page_id, source, first, text, matches))
    return pages, digest.hexdigest()


def _index(context: ToolContext) -> tuple[list[Page], str] | None:
    root = _root(context)
    return _pages(root) if root is not None and root.is_dir() else None


def _utf8_chunk(text: str, start: int, end: int, byte_limit: int) -> str:
    """Take whole Unicode code points while keeping the encoded tool page within its byte budget."""
    candidate = text[start:end]
    if len(candidate.encode("utf-8")) <= byte_limit:
        return candidate
    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if len(candidate[:middle].encode("utf-8")) <= byte_limit:
            low = middle
        else:
            high = middle - 1
    return candidate[:low]


@tool_group("docs")
@search_hint(
    "documentation manual how to configure where is the setting enable turn on reference guide help docs how does it work "
    "документация доки мануал справка руководство как настроить где настройка как включить инструкция поискать поищи как правильно как работает"
)
@tool(
    name="DocsSearch",
    description=(
        "Search how Daedalus itself works and is configured: the index of every Settings section and field, then the "
        "documentation shipped with this installed version. Ask in the operator's own words, any language. A settings "
        "hit carries a link to the exact row (/app/settings/<section>#<field>) and its config path: answer a question "
        "about configuring Daedalus with that link as a Markdown link, and offer to change it with SettingsChange. A "
        "page hit names a page and section to read with DocsRead."
    ),
)
async def docs_search(context: ToolContext, query: str, limit: int = 8) -> ToolResult:
    root = _root(context)
    if root is None or not root.is_dir():
        return error(context, "installed documentation is unavailable")
    index = help_index(root)
    if not parse_query(query).terms:
        return error(context, "query has no searchable words")
    hits = index.search(query, max(1, min(int(limit), MAX_RESULTS)))
    russian = parse_query(query).russian
    body = "\n".join(describe(hit, russian) for hit in hits)
    settings = sum(1 for hit in hits if hit.entry.kind != "page")
    return ok(context, body or f"nothing in the settings or the installed documentation matches {query!r}", version=index.version, count=len(hits), settings=settings)


@tool_group("docs")
@search_hint(
    "read documentation page section heading continue manual docs chapter "
    "прочитать прочитай страница документации раздел доки мануал глава справки продолжить продолжи"
)
@tool(
    name="DocsRead",
    description="Read one page or heading from the installed documentation. Use the opaque next_cursor to continue a long section.",
)
async def docs_read(context: ToolContext, page: str, section: str = "", cursor: int = 0) -> ToolResult:
    index = _index(context)
    if index is None:
        return error(context, "installed documentation is unavailable")
    pages, version = index
    found = next((item for item in pages if item.id == page.casefold()), None)
    if found is None:
        return error(context, f"documentation page {page!r} is not in this installed version", version=version)
    start, end = 0, len(found.text)
    if section:
        wanted = _slug(section)
        positions = [position for slug, position in found.headings if slug == wanted]
        if not positions:
            return error(context, f"section {section!r} is not in {page!r}", version=version)
        start = positions[0]
        end = next((position for _, position in found.headings if position > start), len(found.text))
    offset = max(0, int(cursor))
    text = _utf8_chunk(found.text, start + offset, end, MAX_CHUNK)
    next_cursor = offset + len(text) if start + offset + len(text) < end else None
    return ok(context, text, page=found.id, source=found.source, version=version, next_cursor=next_cursor)


TOOLS = [docs_search, docs_read]

__all__ = ["TOOLS"]
