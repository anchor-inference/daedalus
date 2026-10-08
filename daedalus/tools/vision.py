"""``ImageView`` — look at an image with a small vision model and report what the agent asked."""

from __future__ import annotations

import mimetypes
from typing import Any

from protocore.contracts.llm import LLMRequest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint
from daedalus.tools._common import error, ok, refuse_protected, services_for

MAX_IMAGE_BYTES = 20_000_000
SUPPORTED = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@search_hint(
    "image picture photo screenshot ocr transcribe describe look at image file analyze chart error on screenshot "
    "картинка фото скриншот скрин распознать текст описать изображение глянь проанализируй график"
)
@tool(
    name="ImageView",
    description=(
        "Look at an image file and answer a question about it, using a separate vision model so "
        "the image never enters your own context. Give the path and what you want to know "
        "(task): e.g. 'transcribe all text', 'describe the UI and any error messages', "
        "'what does this chart show, with numbers'. Use detail='full' for a long, exhaustive "
        "description; the default is a focused answer to the task."
    ),
)
async def image_view(context: ToolContext, path: str, task: str, detail: str = "focused") -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if refusal := refuse_protected(context, services, target, "read"):
        return refusal
    if not target.is_file():
        return error(context, f"no such file: {target}")
    mime = mimetypes.guess_type(target.name)[0] or ""
    if mime not in SUPPORTED:
        return error(context, f"unsupported image type {mime or 'unknown'}; supported: {sorted(SUPPORTED)}")
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return error(context, f"image is {size} bytes; downscale it first (limit {MAX_IMAGE_BYTES})")
    try:
        text, model = await look(services.extra.get("vision"), services.extra.get("manager"), target.read_bytes(), mime, task, detail=detail)
    except VisionUnavailable as exc:
        return error(context, str(exc))
    return ok(context, text, model=model, image=str(target))


KEPT_IMAGE_TASK = (
    "Describe this image fully for an agent that cannot see it: what it shows, its layout, and every piece of "
    "text on it quoted verbatim, including error messages, numbers, labels and identifiers."
)


async def describe_kept(manager: Any, data: bytes, mime: str, name: str) -> str:
    """A kept image (an operator's screenshot, a member's picture) in words.

    Reading one was refused as a binary file, so an orchestrator handed a screenshot of a bug could only
    pass it on without knowing what was in it. The vision model describes it, as ImageView does; when no
    model can look, the refusal says why and that the file can still be handed on.
    """
    if mime not in SUPPORTED:
        raise VisionUnavailable(f"{name} is a {mime or 'binary'} file; it cannot be shown here, but it can be handed on with files=[…]")
    if len(data) > MAX_IMAGE_BYTES:
        raise VisionUnavailable(f"{name} is {len(data)} bytes, past what the vision model takes; it can still be handed on with files=[…]")
    vision = manager.vision_model() if manager is not None and hasattr(manager, "vision_model") else None
    try:
        text, model = await look(vision, manager, data, mime, KEPT_IMAGE_TASK, detail="full")
    except VisionUnavailable as exc:
        raise VisionUnavailable(f"{name} is an image and {exc}; it can still be handed on with files=[…]") from exc
    return f"[{name}, described by {model}]\n{text}"


async def kept_body(manager: Any, data: bytes, stored: Any, *, offset: int = 1, limit: int = 200) -> str:
    """What reading a kept file shows: an image described, anything else as numbered lines."""
    mime = str(getattr(stored, "mime", "") or mimetypes.guess_type(stored.name)[0] or "")
    if mime.startswith("image/"):
        return await describe_kept(manager, data, mime, stored.name)
    from daedalus.host.peek import text_window  # Lazy: peek is host code this tool module need not load

    return text_window(data, stored.name, offset=offset, limit=limit)


class VisionUnavailable(Exception):
    """No vision model can look now; the message says what to change."""


async def look(vision: Any, manager: Any, data: bytes, mime: str, task: str, *, detail: str = "focused", instruction: str = "") -> tuple[str, str]:
    """Ask the configured vision model about an image; returns its answer and the model's name.

    The one path pixels take to a model: the image goes to a separate vision model and only its
    words come back, so no agent's own context ever carries an image. ``ImageView`` and the browser's
    ``BrowserLook`` both come through here. ``instruction`` replaces the opening words of the request
    for a caller that must say more about what it shows (a web page is not to be obeyed).
    """
    if not vision:
        raise VisionUnavailable("no vision model is configured: mark a model as taking images in Settings → Models and pick it under Image understanding")
    provider, model, blobs, tenant = vision
    max_out = int(getattr(getattr(getattr(manager, "config", None), "vision", None), "max_output_tokens", 2000))
    accepts = getattr(provider, "accepts_images", None)
    if accepts is None or not accepts(model):
        raise VisionUnavailable("the vision preset is not marked as image-capable; enable 'images' on it in Settings → Models")
    try:
        meta = await blobs.put(tenant, data, content_type=mime)
    except OSError as exc:
        # The image is staged before any model is asked. A failure here once read, to the agent and
        # the operator alike, as the chosen model not taking images, when the picture never left
        # the machine; say which side failed.
        raise VisionUnavailable(f"the image could not be stored for the vision model {model} ({exc}); the model was not asked") from exc
    text = instruction or (
        "You are the eyes of another AI agent. Look at the image and answer its request precisely. "
        "Quote text verbatim when asked to read; give numbers when asked about data; say clearly "
        "when something is not visible. "
    )
    text += "Be exhaustive and structured." if detail == "full" else "Be concise and specific."
    request = LLMRequest(
        model=model,
        messages=[
            Message(role=MessageRole.system, content_blocks=[TextBlock(text=text)]),
            Message(
                role=MessageRole.user,
                content_blocks=[TextBlock(text=f"Request: {task}")],
                metadata={"image_refs": [{"ref": meta.ref, "mime": mime}]},
            ),
        ],
        max_tokens=max_out if detail == "full" else min(800, max_out),
        temperature=0.1,
    )
    try:
        response = await provider.complete_text(request)
    except Exception as exc:  # noqa: BLE001
        raise VisionUnavailable(f"vision model {model} failed: {exc}") from exc
    answer = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()
    if not answer:
        # An empty answer handed back as a description let the agent go on as if it had looked.
        raise VisionUnavailable(
            f"vision model {model} returned no text for the image; it may have spent its {request.max_tokens} output tokens "
            "on thinking, or its route dropped the picture"
        )
    return answer, str(model)


TOOLS = [image_view]

__all__ = ["TOOLS", "VisionUnavailable", "look"]
