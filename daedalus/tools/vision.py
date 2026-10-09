"""``ImageView`` — look at an image with a small vision model and report what the agent asked."""

from __future__ import annotations

import mimetypes
from typing import Any

from protocore.contracts.llm import LLMRequest, LLMResponse
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock, ToolResult
from protocore.tools.decorator import tool

from daedalus.host.setting_refs import VISION_MODEL, VISION_OUTPUT, SettingRef
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
    # Through the session's filesystem, not ``Path``: a session working on the host resolves a
    # relative path in its folder there, and this process reading the same name locally answered
    # "no such file" for every image the agent had just made.
    fs = services.fs
    if not await fs.is_file(target):
        return error(context, f"no such file: {target}")
    mime = mimetypes.guess_type(target.name)[0] or ""
    if mime not in SUPPORTED:
        return error(context, f"unsupported image type {mime or 'unknown'}; supported: {sorted(SUPPORTED)}")
    size = await fs.size(target)
    if size > MAX_IMAGE_BYTES:
        return error(context, f"image is {size} bytes; downscale it first (limit {MAX_IMAGE_BYTES})")
    try:
        data = await fs.read_bytes(target, limit=MAX_IMAGE_BYTES)
    except (OSError, ValueError) as exc:
        return error(context, str(exc))
    try:
        text, model = await look(services.extra.get("vision"), services.extra.get("manager"), data, mime, task, detail=detail, session_id=context.session_id)
    except VisionUnavailable as exc:
        return error(context, str(exc))
    return ok(context, text, model=model, image=str(target))


KEPT_IMAGE_TASK = (
    "Describe this image fully for an agent that cannot see it: what it shows, its layout, and every piece of "
    "text on it quoted verbatim, including error messages, numbers, labels and identifiers."
)


async def describe_kept(manager: Any, data: bytes, mime: str, name: str, *, session_id: str | None = None) -> str:
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
        text, model = await look(vision, manager, data, mime, KEPT_IMAGE_TASK, detail="full", session_id=session_id)
    except VisionUnavailable as exc:
        raise VisionUnavailable(f"{name} is an image and {exc}; it can still be handed on with files=[…]") from exc
    return f"[{name}, described by {model}]\n{text}"


async def kept_body(manager: Any, data: bytes, stored: Any, *, offset: int = 1, limit: int = 200, session_id: str | None = None) -> str:
    """What reading a kept file shows: an image described, anything else as numbered lines."""
    mime = str(getattr(stored, "mime", "") or mimetypes.guess_type(stored.name)[0] or "")
    if mime.startswith("image/"):
        return await describe_kept(manager, data, mime, stored.name, session_id=session_id)
    from daedalus.host.peek import text_window  # Lazy: peek is host code this tool module need not load

    return text_window(data, stored.name, offset=offset, limit=limit)


class VisionUnavailable(Exception):
    """No vision model can look now; the message says what to change and ``setting`` where.

    ``model_fault`` is false when the model was never the problem (the picture could not be staged):
    asking the session's model instead would fail the same way, so no fallback is tried. ``setting``
    is the vision model's row unless the fix is somewhere else (the session model's Images switch)."""

    def __init__(self, message: str, *, model_fault: bool = True, setting: SettingRef = VISION_MODEL) -> None:
        super().__init__(message)
        self.setting = setting
        self.model_fault = model_fault


async def look(
    vision: Any, manager: Any, data: bytes, mime: str, task: str, *,
    detail: str = "focused", instruction: str = "", session_id: str | None = None,
) -> tuple[str, str]:
    """Ask the configured vision model about an image; returns its answer and the model's name.

    The one path pixels take to a model: the image goes to a separate vision model and only its
    words come back, so no agent's own context ever carries an image. ``ImageView`` and the browser's
    ``BrowserLook`` both come through here. ``instruction`` replaces the opening words of the request
    for a caller that must say more about what it shows (a web page is not to be obeyed).

    When no vision model is set, or it refuses, and ``model.fallback_to_session`` is on, the
    session's own model looks instead if it takes images, and the operator is warned with the way to
    the vision model's row; with it off the refusal is an error that carries the same row. With a
    session model whose Images switch is off, the error carries that model's own row instead, since
    turning the switch on is what would have let it look. Before this, an installation with no
    image-capable preset could not look at a screenshot at all although the model it ran on read
    pictures.
    """
    try:
        return await _ask(vision, manager, data, mime, task, detail=detail, instruction=instruction)
    except VisionUnavailable as exc:
        if not exc.model_fault:
            raise
        primary = exc
    falls_back = bool(getattr(getattr(getattr(manager, "config", None), "model", None), "fallback_to_session", False))
    session_vision = getattr(manager, "session_vision", None)
    if not falls_back or session_vision is None:
        await _notice(manager, session_id, "failed", str(primary), setting=primary.setting)
        raise primary
    route, why, setting = await session_vision(session_id)
    if route is None:
        message = f"{primary}; {why}, so it cannot look instead"
        where = setting or primary.setting
        await _notice(manager, session_id, "failed", message, setting=where)
        raise VisionUnavailable(message, setting=where) from primary
    try:
        text, model = await _ask(route, manager, data, mime, task, detail=detail, instruction=instruction)
    except VisionUnavailable as exc:
        message = f"{primary}; the session's model {route[1]} could not look either: {exc}"
        await _notice(manager, session_id, "failed", message, setting=primary.setting)
        raise VisionUnavailable(message, model_fault=exc.model_fault) from exc
    await _notice(manager, session_id, "fallback", str(primary), model=model, setting=primary.setting)
    return text, model


async def _notice(manager: Any, session_id: str | None, outcome: str, detail: str, *, model: str = "", setting: SettingRef = VISION_MODEL) -> None:
    notice = getattr(manager, "setting_notice", None)
    if notice is not None:
        await notice(session_id, kind="vision", outcome=outcome, detail=detail, model=model, setting=setting)


RETRY_CEILING = 32_000
"""The most output a starved vision call is retried with: the upper bound of ``[vision] max_output_tokens``."""

IMAGE_TOKEN_FLOOR = 64
"""Fewer prompt tokens than the text alone plus this, and the picture cannot have been in the prompt."""


async def _ask(vision: Any, manager: Any, data: bytes, mime: str, task: str, *, detail: str, instruction: str) -> tuple[str, str]:
    """One vision route asked, and asked again once if thinking ate the answer.

    :class:`VisionUnavailable` says why it gave no answer.

    The budget is ``[vision] max_output_tokens`` whatever the detail. A focused answer used to be
    capped at 800 tokens behind the operator's back, and a model that thinks by default (every Claude
    model from the fifth generation on, even with thinking not asked for) spent all 800 thinking and
    wrote nothing; the operator's 16384 never applied. Brevity comes from the instruction instead.
    Thinking is asked off, which the endpoints that can switch it honour; for a model that thinks
    anyway, a reply cut off with no text is retried once at twice the budget.
    """
    if not vision:
        raise VisionUnavailable("no vision model is configured: mark a model as taking images in Settings → Models and pick it under Image understanding")
    provider, model, blobs, tenant = vision
    max_out = int(getattr(getattr(getattr(manager, "config", None), "vision", None), "max_output_tokens", 2000))
    accepts = getattr(provider, "accepts_images", None)
    if accepts is None or not accepts(model):
        raise VisionUnavailable(f"the vision model {model} is not marked as taking images; enable 'images' on it in Settings → Models")
    try:
        meta = await blobs.put(tenant, data, content_type=mime)
    except OSError as exc:
        # The image is staged before any model is asked. A failure here once read, to the agent and
        # the operator alike, as the chosen model not taking images, when the picture never left
        # the machine; say which side failed.
        raise VisionUnavailable(f"the image could not be stored for the vision model {model} ({exc}); the model was not asked", model_fault=False) from exc
    text = instruction or (
        "You are the eyes of another AI agent. Look at the image and answer its request precisely. "
        "Quote text verbatim when asked to read; give numbers when asked about data; say clearly "
        "when something is not visible. "
    )
    text += "Be exhaustive and structured." if detail == "full" else "Be concise and specific."
    messages = [
        Message(role=MessageRole.system, content_blocks=[TextBlock(text=text)]),
        Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=f"Request: {task}")],
            metadata={"image_refs": [{"ref": meta.ref, "mime": mime}]},
        ),
    ]
    budget = max_out
    response = await _complete(provider, model, messages, budget)
    answer = _answer(response)
    retried = 0
    if not answer and response.stop_reason == StopReason.max_tokens and budget < RETRY_CEILING:
        retried = min(RETRY_CEILING, budget * 2)
        response = await _complete(provider, model, messages, retried)
        answer = _answer(response)
    if not answer:
        # An empty answer handed back as a description let the agent go on as if it had looked.
        prompt_text = len(text) + len(task) + len("Request: ")
        # Starved, the fix is the budget, and the row it opens is that number's.
        starved = response.stop_reason == StopReason.max_tokens
        reason = empty_answer_reason(model, response, retried or budget, prompt_text // 4, retried=bool(retried))
        raise VisionUnavailable(reason, setting=VISION_OUTPUT if starved else VISION_MODEL)
    return answer, str(model)


async def _complete(provider: Any, model: str, messages: list[Message], budget: int) -> LLMResponse:
    request = LLMRequest(model=model, messages=messages, max_tokens=budget, temperature=0.1, extra={"enable_thinking": False})
    try:
        return await provider.complete_text(request)
    except Exception as exc:  # noqa: BLE001
        raise VisionUnavailable(f"vision model {model} failed: {exc}") from exc


def _answer(response: LLMResponse) -> str:
    return "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock)).strip()


def empty_answer_reason(model: str, response: LLMResponse, budget: int, text_tokens: int, *, retried: bool = False) -> str:
    """Why a vision call came back with no text, told from the stop reason and the usage.

    The old sentence offered both causes at once ("spent its tokens on thinking, or its route dropped
    the picture"), and the two want different fixes: a larger budget, or a route that forwards images.
    """
    usage = response.usage
    again = " even when asked a second time with twice the budget" if retried else ""
    if response.stop_reason == StopReason.max_tokens:
        return (
            f"vision model {model} wrote no answer: it used all {budget} of its output tokens{again} before writing any text, "
            "most likely on thinking; raise Max output tokens under Image understanding or pick a model that thinks less"
        )
    if usage.input_tokens <= 0 and usage.output_tokens <= 0:
        return f"vision model {model} finished without writing an answer, and its route reported no usage, so whether the picture reached it cannot be told"
    if usage.input_tokens < text_tokens + IMAGE_TOKEN_FLOOR:
        return (
            f"vision model {model} finished without writing an answer, and its prompt was only {usage.input_tokens} tokens, "
            "too few to have carried the picture: its route most likely dropped the image"
        )
    return (
        f"vision model {model} finished without writing an answer, though the picture reached it "
        f"({usage.input_tokens} prompt tokens) and it stopped on its own after {usage.output_tokens} output tokens"
    )


TOOLS = [image_view]

__all__ = ["TOOLS", "VisionUnavailable", "empty_answer_reason", "look"]
