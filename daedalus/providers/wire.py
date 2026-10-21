"""Conversion between core ``Message`` history and OpenAI chat-completion payloads."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable, Container, Sequence
from typing import Any

from protocore.contracts.types import (
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolResultImage,
    ToolUseBlock,
    message_image_refs,
)
from protocore.providers.openai_compat import ReasoningReplay
from protocore.providers.openai_compat.wire import TOOL_IMAGES_CARRIER_HEADER

from daedalus.security.redact import redact

logger = logging.getLogger(__name__)


def tools_to_wire(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    """The ``tools`` array, each schema as the tool states it.

    ``additionalProperties`` goes on the wire whenever the tool sets it. The core spells the flag
    ``additional_properties``, and copying only ``properties`` and ``required`` dropped it: the strict
    forced ``plan`` tool then reached the model as a permissive one that would take any field.
    """
    wire: list[dict[str, Any]] = []
    for tool in tools:
        params = tool.parameters.model_dump(mode="json") if tool.parameters is not None else {}
        schema = {
            "type": "object",
            "properties": params.get("properties", {}),
            "required": params.get("required", []),
        }
        if params.get("additional_properties") is not None:
            schema["additionalProperties"] = params["additional_properties"]
        wire.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            }
        )
    return wire


MAX_REQUEST_IMAGES = 20
MAX_REQUEST_IMAGE_BYTES = 20 * 1024 * 1024
"""How many pictures, and how many bytes of them as encoded on the wire, one request carries.

Every picture of the session used to go out on every request. Anthropic refuses a request over 32 MB,
and shrinks pictures once one holds more than 20, so a session past that point had every following
request refused until a compaction removed them; before it, every step re-sent tens of megabytes."""

IMAGE_WINDOW_STEP = 5
"""The cut between pictures left out and pictures sent falls only on a multiple of this.

The pictures left out sit early in the history, so moving the cut changes the prompt from that point
on and costs the prompt cache everything after it. Moved one picture at a time it would cost that on
every new picture; in steps of five it costs it once in five. A larger step was rejected because the
step is also how many pictures that would fit can be left out: with large pictures the byte bound
keeps only a handful, and a step of ten could leave out every one of them."""


def _image_refs_of(message: Message) -> list[tuple[str, str]]:
    """``(blob_ref, mime)`` of every picture a user message carries, read the core's way: its image
    blocks, then the list under ``protocore.image_refs`` (a user turn holds a single text block)."""
    return [(image.blob_ref, image.mime_type) for image in message_image_refs(message)]


async def images_left_out(
    messages: Sequence[Message],
    image_loader: Callable[[str], Awaitable[tuple[bytes, str]]],
) -> frozenset[str]:
    """The pictures one request leaves out: all but the newest that fit :data:`MAX_REQUEST_IMAGES` and
    :data:`MAX_REQUEST_IMAGE_BYTES`, the cut rounded up to :data:`IMAGE_WINDOW_STEP` but never past the
    newest picture when that one fits on its own.

    Only the newest pictures are loaded to be measured, at most :data:`MAX_REQUEST_IMAGES` of them; pass
    a loader that remembers what it loaded, so the pictures that are sent are not read twice.
    """
    order: list[str] = []
    for message in messages:
        if message.role is MessageRole.user:
            for blob_ref, _mime in _image_refs_of(message):
                if blob_ref not in order:
                    order.append(blob_ref)
    cut = max(0, len(order) - MAX_REQUEST_IMAGES)
    total = 0
    for index in range(len(order) - 1, cut - 1, -1):
        data, _mime = await image_loader(order[index])
        total += 4 * ((len(data) + 2) // 3)  # its base64 length, which is what the request carries
        if total > MAX_REQUEST_IMAGE_BYTES:
            cut = index + 1
            break
    if 0 < cut < len(order):
        cut = min(len(order) - 1, -(-cut // IMAGE_WINDOW_STEP) * IMAGE_WINDOW_STEP)
    return frozenset(order[:cut])


async def messages_to_wire(
    messages: Sequence[Message],
    *,
    image_loader: Any | None = None,
    supports_images: bool = False,
    images_omitted: Container[str] = (),
    reasoning_replay: ReasoningReplay = ReasoningReplay.when_present,
    thinking: bool = False,
) -> list[dict[str, Any]]:
    """Flatten core messages into OpenAI ``messages``.

    ``image_loader`` is an async callable ``(blob_ref) -> (bytes, mime)``; when the
    endpoint cannot take images the block degrades to a text placeholder so the
    model still knows a file was attached. A picture named in ``images_omitted``
    (see :func:`images_left_out`) degrades the same way, saying it was left out.

    ``reasoning_replay`` is the endpoint's policy for an assistant turn's reasoning text, in the
    core's terms: ``never`` sends none (a server that refuses a field it does not know),
    ``when_present`` sends it on the turns that have some, and ``required`` (DeepSeek) gives every
    assistant turn the field while ``thinking`` is on — empty when the turn has none, since DeepSeek
    refuses a thinking request whose earlier turns lack it — and leaves out a turn that is nothing
    but reasoning, the partial a stream left when it died mid-thought, which it cannot use.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.system:
            wire.append({"role": "system", "content": _text_of(message)})
            continue
        if message.role is MessageRole.tool:
            for block in message.content_blocks:
                if isinstance(block, ToolResultBlock):
                    entry = {"role": "tool", "tool_call_id": block.tool_call_id, "content": block.content or ""}
                    if block.images:
                        carried, notes = await _tool_image_parts(block, image_loader=image_loader, supports_images=supports_images)
                        if notes:
                            entry["content"] = "\n".join([entry["content"], *notes])
                        if carried:
                            entry[CARRIED_IMAGES] = [{"type": "text", "text": f"[images returned by tool call {block.tool_call_id}]"}, *carried]
                    wire.append(entry)
            continue
        if message.role is MessageRole.assistant:
            entry: dict[str, Any] = {"role": "assistant"}
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            reasoning: list[str] = []
            for block in message.content_blocks:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    reasoning.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": block.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": block.arguments_json or "{}",
                            },
                        }
                    )
            entry["content"] = "".join(text_parts) or None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            reasoning_text = message.reasoning_content or "".join(reasoning)
            if reasoning_replay is ReasoningReplay.required and thinking:
                if entry["content"] is None and not tool_calls:
                    continue
                entry["reasoning_content"] = reasoning_text
            elif reasoning_replay is not ReasoningReplay.never and reasoning_text:
                entry["reasoning_content"] = reasoning_text
            if entry["content"] is None and not tool_calls:
                entry["content"] = ""
            wire.append(entry)
            continue
        # user: one text block; images ride under ``protocore.image_refs`` (the core allows a single block)
        texts = [block.text for block in message.content_blocks if isinstance(block, TextBlock)]
        content: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
        has_image = False
        for blob_ref, mime_hint in _image_refs_of(message):
            if blob_ref in images_omitted:
                content.append({"type": "text", "text": f"[image left out of this request, only the newest are sent: {blob_ref}]"})
            elif supports_images and image_loader is not None:
                data, mime = await image_loader(blob_ref)
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime or mime_hint};base64,{b64}"}})
                has_image = True
            else:
                content.append({"type": "text", "text": f"[image attached: {blob_ref}]"})
        if has_image:
            wire.append({"role": "user", "content": content})
        else:
            wire.append({"role": "user", "content": "\n".join(p["text"] for p in content)})
    return wire


CARRIED_IMAGES = "daedalus.carried_images"
"""The key a tool entry holds its images under until :func:`place_carried_images` moves them out.

Most chat-completions servers take only a string in a ``tool`` message, so a tool's pictures ride in
one ``user`` message after the whole run of results, opened by the core's
``TOOL_IMAGES_CARRIER_HEADER`` exactly as the core's own adapter does it. A user turn between two
results of one batch would close the batch and leave the calls after it unanswered, so the carrier
can only be placed once every message of the request is on the wire."""


async def _tool_image_parts(
    block: ToolResultBlock, *, image_loader: Any | None, supports_images: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The content parts for a result's images, and a line for each one that cannot be sent.

    A model that takes no images gets the lines in the result's own text and no extra turn. The size a
    tool gave goes beside its image: a model that points at coordinates in a screenshot needs the size
    it was shown to turn them back into screen positions.
    """
    parts: list[dict[str, Any]] = []
    notes: list[str] = []
    for number, image in enumerate(block.images, start=1):
        name = image.blob_ref or "inline image"
        if not supports_images or (image.data is None and image_loader is None):
            notes.append(f"[image not sent, this model does not take images: {name}]")
            continue
        loaded = await _tool_image_bytes(image, image_loader)
        if loaded is None:
            notes.append(f"[image not sent, it could not be loaded: {name}]")
            continue
        data, mime = loaded
        if image.width is not None and image.height is not None:
            parts.append({"type": "text", "text": f"[image {number} of tool call {block.tool_call_id}: {image.width}x{image.height} pixels]"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}})
    return parts, notes


async def _tool_image_bytes(image: ToolResultImage, image_loader: Any | None) -> tuple[bytes, str] | None:
    if image.data is not None:
        return image.data, image.mime_type
    try:
        data, mime = await image_loader(image.blob_ref)
    except Exception as exc:  # noqa: BLE001 — one unreadable picture must not fail the whole request
        logger.warning("tool image %s could not be loaded: %s", image.blob_ref, exc)
        return None
    return data, mime or image.mime_type


def place_carried_images(entries: list[dict[str, Any]], index_map: list[int]) -> tuple[list[dict[str, Any]], list[int]]:
    """Move every tool entry's carried images into one ``user`` message after its run of results.

    ``index_map`` names, for each core message, the wire entry it ended on (``-1`` for none); the
    returned map names the same entries in the new list, except that a run's last result now names
    its carrier, so a cache breakpoint set on that result covers the images sent after it.
    """
    out: list[dict[str, Any]] = []
    moved: dict[int, int] = {}
    carrier: list[dict[str, Any]] = []
    last_tool = -1

    def close_run() -> None:
        nonlocal carrier
        if carrier:
            out.append({"role": "user", "content": [{"type": "text", "text": TOOL_IMAGES_CARRIER_HEADER}, *carrier]})
            moved[last_tool] = len(out) - 1
        carrier = []

    for index, entry in enumerate(entries):
        if entry.get("role") != "tool":
            close_run()
        else:
            carrier.extend(entry.pop(CARRIED_IMAGES, None) or [])
            last_tool = index
        out.append(entry)
        moved.setdefault(index, len(out) - 1)
    close_run()
    return out, [moved.get(index, -1) if index >= 0 else -1 for index in index_map]


def _text_of(message: Message) -> str:
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


def parse_json_arguments(raw: str) -> dict[str, Any]:
    """Parse tool-call arguments, tolerating the ways models get JSON slightly wrong.

    In order: as written; without a code fence; with trailing commas removed; with
    single-quoted strings; the outermost ``{…}`` slice; and finally with unbalanced
    braces and quotes closed (a call cut off by the output cap). Every repair is
    logged so a semantically wrong call can be traced back to its raw text.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value, how = _repair_arguments(text)
        logger.debug("tool-call arguments repaired (%s): %.200r", how, redact(raw))
    return value if isinstance(value, dict) else {"value": value}


def arguments_cut(raw: str) -> bool:
    """Whether tool-call arguments stop part-way, as opposed to being whole JSON written slightly wrong.

    A fence, a trailing comma or single quotes are a model's sloppiness over a call it finished, and
    the repaired call is the one it meant. Only braces or quotes left open are the mark of a call the
    stream cut, whose repaired arguments are not what the model would have sent.
    """
    text = (raw or "").strip()
    if not text:
        return False
    try:
        json.loads(text)
        return False
    except json.JSONDecodeError:
        return _repair_arguments(text)[1] == "truncated"


_ARGS_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SINGLE_QUOTED_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _repair_arguments(text: str) -> tuple[Any, str]:
    candidates: list[tuple[str, str]] = []
    fenced = _ARGS_FENCE_RE.match(text)
    if fenced:
        candidates.append(("fence", fenced.group(1)))
    base = fenced.group(1) if fenced else text
    candidates.append(("trailing-comma", _TRAILING_COMMA_RE.sub(r"\1", base)))
    if "'" in base:
        candidates.append(("single-quotes", _TRAILING_COMMA_RE.sub(r"\1", _SINGLE_QUOTED_RE.sub(lambda m: json.dumps(m.group(1)), base))))
    start, end = base.find("{"), base.rfind("}")
    if start != -1 and end > start:
        candidates.append(("brace-slice", _TRAILING_COMMA_RE.sub(r"\1", base[start : end + 1])))
    for how, candidate in candidates:
        try:
            return json.loads(candidate), how
        except json.JSONDecodeError:
            continue
    return _repair_json(base), "truncated"


def _repair_json(text: str) -> Any:
    """Best-effort repair of a truncated JSON object (balance braces and quotes)."""
    depth_curly = depth_square = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
    candidate = text
    if in_string:
        candidate += '"'
    candidate += "]" * max(depth_square, 0) + "}" * max(depth_curly, 0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


__all__ = ["arguments_cut", "images_left_out", "messages_to_wire", "parse_json_arguments", "place_carried_images", "tools_to_wire"]
