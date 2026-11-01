"""What an MCP tool's answer becomes for the model: text for its text, pictures for its pictures.

Every picture a server returned used to be replaced by the words ``[image … returned by the tool]``
and every embedded resource by two thousand characters of its JSON, so a browser server's screenshot
or a design server's render reached the model as a sentence about it, and a resource arrived as a
cut, escaped dump. Pictures now ride on the result as the core's ``ToolResultImage``, where the
provider layer fits each one to what the model's provider accepts and counts it against the request
like any other tool picture; the text still names every picture, so a model that takes none (and a
command-line member, which is handed the text only) knows one came back.

A picture is taken only when its own bytes say it is one of the types a model reads, whatever the
server claimed, and only within a size and a count: a server is outside content, and a 200 MB
"image" or a result of a hundred screenshots would otherwise be carried into every later request
of the session.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from protocore.contracts.types import ToolResultImage

from daedalus.providers.image_fit import sniffed_mime

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
"""The picture types a model reads; the same set ImageView accepts."""

MAX_IMAGE_BYTES = 20_000_000
"""The largest picture taken from a server, decoded. ImageView's bound, and inside the core's own
bound for an inline tool picture; the provider layer then scales it down to what is sent."""

MAX_IMAGES = 8
"""The most pictures one result passes on. Browser and design servers return one or two; the core
keeps only the newest few of a run in each request anyway, so more from one call would be dropped
before the model saw them while their bytes were still stored."""

MAX_RESOURCE_CHARS = 20_000
"""The most of one embedded text resource kept in the result before the session's own output cap
cuts the whole of it: a server that embeds a long file beside a short answer would otherwise let
the file push the answer out."""

UNREADABLE_ITEM_CHARS = 2_000
"""The most of an item of an unknown kind kept, as its JSON."""


@dataclass(slots=True)
class McpAnswer:
    """An MCP tool's answer as the host passes it on: the text, whether it failed, and its pictures."""

    text: str
    failed: bool
    images: tuple[ToolResultImage, ...] = field(default_factory=tuple)


def _field(item: Any, *names: str) -> Any:
    """A field of an MCP content item, read from the SDK's objects or from a plain mapping.

    The SDK's models name the type ``mime_type`` and its wire form ``mimeType``; the old code read
    only the wire spelling from the object, so every picture was described with an empty type."""
    for name in names:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def _mime(item: Any) -> str:
    return str(_field(item, "mime_type", "mimeType") or "")


def size_text(count: int) -> str:
    """A byte count as a reader says it: ``812 bytes``, ``41 KB``, ``2.3 MB``."""
    if count < 1024:
        return f"{count} bytes"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _decoded(data: Any) -> bytes | None:
    """The bytes of a base64 field, or ``None`` when it is not base64 at all."""
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _encoded_length(data: Any) -> int:
    """How many bytes a base64 field stands for, without decoding it: a blob too large to keep is
    described by its size, and decoding hundreds of megabytes only to say so would cost the memory."""
    if not isinstance(data, str):
        return 0
    return max(0, len(data) * 3 // 4 - data[-2:].count("="))


class _Collector:
    """Gathers the text lines and pictures of one result in the order the server gave them."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.images: list[ToolResultImage] = []

    def picture(self, data: Any, claimed: str, *, what: str, source: str = "") -> None:
        """Take a picture if it is one, else say why it was not passed on.

        ``what`` names it in the text (``image``, ``resource file:///a.png``); ``source`` is kept on
        the picture's metadata, where the app finds it to label the thumbnail."""
        label = f"{what} {claimed}".strip()
        length = _encoded_length(data)
        if length > MAX_IMAGE_BYTES:
            self.parts.append(f"[{label}, {size_text(length)}, returned by the tool: too large to pass on (the limit is {size_text(MAX_IMAGE_BYTES)})]")
            return
        raw = _decoded(data)
        if raw is None:
            self.parts.append(f"[{label} returned by the tool: its data is not base64, so it was not passed on]")
            return
        # The bytes alone: a type the server claimed for bytes no signature matches is no evidence.
        mime = sniffed_mime(raw, "")
        if mime not in IMAGE_TYPES:
            self.parts.append(f"[{label}, {size_text(len(raw))}, returned by the tool: not a picture a model reads (png, jpeg, webp or gif), so it was not passed on]")
            return
        if len(self.images) >= MAX_IMAGES:
            self.parts.append(f"[{what} {mime}, {size_text(len(raw))}, returned by the tool: not passed on, this result already carries {MAX_IMAGES} pictures]")
            return
        metadata: dict[str, Any] = {"source": "mcp"}
        if source:
            metadata["uri"] = source
        self.images.append(ToolResultImage(data=raw, mime_type=mime, metadata=metadata))
        self.parts.append(f"[{what} {mime}, {size_text(len(raw))}, returned by the tool as picture {len(self.images)}]")

    def resource(self, resource: Any) -> None:
        uri = str(_field(resource, "uri") or "")
        mime = _mime(resource)
        text = _field(resource, "text")
        if isinstance(text, str):
            header = f"[resource {uri}" + (f" ({mime})" if mime else "") + "]"
            if len(text) > MAX_RESOURCE_CHARS:
                cut = len(text) - MAX_RESOURCE_CHARS
                text = text[:MAX_RESOURCE_CHARS] + f"\n[… {cut} more characters of this resource left out]"
            self.parts.append(f"{header}\n{text}")
            return
        blob = _field(resource, "blob")
        claimed_image = mime.startswith("image/")
        if isinstance(blob, str) and (claimed_image or not mime):
            # A blob with no stated type may still be a picture; its bytes decide. One that is not
            # is then described like any other file the server attached.
            if claimed_image or sniffed_mime(_decoded(blob) or b"", "") in IMAGE_TYPES:
                self.picture(blob, mime, what=f"resource {uri}", source=uri)
                return
        self.parts.append(f"[resource {uri}: {mime or 'unknown type'}, {size_text(_encoded_length(blob))}, a file the tool attached; not shown]")

    def item(self, item: Any) -> None:
        kind = _field(item, "type")
        text = _field(item, "text")
        if kind == "text" or (kind is None and isinstance(text, str)):
            self.parts.append(str(text or ""))
        elif kind == "image":
            self.picture(_field(item, "data"), _mime(item), what="image")
        elif kind == "audio":
            self.parts.append(f"[audio {_mime(item) or 'of unknown type'}, {size_text(_encoded_length(_field(item, 'data')))}, returned by the tool; not passed on]")
        elif kind == "resource_link":
            name = _field(item, "title", "name") or ""
            uri = _field(item, "uri") or ""
            mime = _mime(item)
            self.parts.append(f"[resource link {name}: {uri}" + (f" ({mime})" if mime else "") + "]" if name else f"[resource link {uri}]")
        elif kind == "resource":
            self.resource(_field(item, "resource"))
        else:
            dump = getattr(item, "model_dump", None)
            self.parts.append(json.dumps(dump() if dump else item if isinstance(item, dict) else str(item), default=str)[:UNREADABLE_ITEM_CHARS])


def read_content(items: Any) -> tuple[str, tuple[ToolResultImage, ...]]:
    """The text and the pictures of an MCP result's ``content`` list."""
    collector = _Collector()
    for item in items or []:
        collector.item(item)
    return "\n".join(collector.parts), tuple(collector.images)


__all__ = ["IMAGE_TYPES", "MAX_IMAGES", "MAX_IMAGE_BYTES", "MAX_RESOURCE_CHARS", "McpAnswer", "read_content", "size_text"]
