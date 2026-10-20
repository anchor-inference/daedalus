"""Conversion between core ``Message`` history and OpenAI chat-completion payloads."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from protocore.contracts.types import (
    ImageRefBlock,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.security.redact import redact

logger = logging.getLogger(__name__)


def tools_to_wire(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for tool in tools:
        params = tool.parameters.model_dump(mode="json") if tool.parameters is not None else {}
        schema = {
            "type": "object",
            "properties": params.get("properties", {}),
            "required": params.get("required", []),
        }
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


async def messages_to_wire(
    messages: Sequence[Message],
    *,
    image_loader: Any | None = None,
    supports_images: bool = False,
) -> list[dict[str, Any]]:
    """Flatten core messages into OpenAI ``messages``.

    ``image_loader`` is an async callable ``(blob_ref) -> (bytes, mime)``; when the
    endpoint cannot take images the block degrades to a text placeholder so the
    model still knows a file was attached.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.system:
            wire.append({"role": "system", "content": _text_of(message)})
            continue
        if message.role is MessageRole.tool:
            for block in message.content_blocks:
                if isinstance(block, ToolResultBlock):
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_call_id,
                            "content": block.content or "",
                        }
                    )
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
            if message.reasoning_content:
                entry["reasoning_content"] = message.reasoning_content
            elif reasoning:
                entry["reasoning_content"] = "".join(reasoning)
            if entry["content"] is None and not tool_calls:
                entry["content"] = ""
            wire.append(entry)
            continue
        # user: one text block; images ride in ``metadata["image_refs"]`` (the core allows a single block)
        refs: list[tuple[str, str]] = []
        texts: list[str] = []
        for block in message.content_blocks:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ImageRefBlock):
                refs.append((block.blob_ref, block.mime_type))
        for ref in message.metadata.get("image_refs") or []:
            if isinstance(ref, dict) and ref.get("ref"):
                refs.append((str(ref["ref"]), str(ref.get("mime") or "image/png")))
        content: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
        has_image = False
        for blob_ref, mime_hint in refs:
            if supports_images and image_loader is not None:
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


__all__ = ["messages_to_wire", "parse_json_arguments", "tools_to_wire"]
