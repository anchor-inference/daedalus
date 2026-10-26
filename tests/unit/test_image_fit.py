"""Every picture is fitted to its provider before it goes on the wire, the same bytes on every request.

A session with seven phone screenshots of five to nine megabytes was refused on every request,
"continue" included, because one picture was past Anthropic's per-image limit and stayed in the history.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import math
import random
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import (
    IMAGE_REFS_METADATA_KEY,
    ImageRefBlock,
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolResultImage,
    ToolUseBlock,
)

from daedalus.providers.image_fit import (
    CLAUDE_HIGH_LIMITS,
    CLAUDE_LIMITS,
    DEFAULT_LIMITS,
    MAX_ENCODED_BYTES,
    OPENAI_LIMITS,
    FittedImages,
    ImageLimits,
    encoded_size,
    fit_image,
    limits_for,
    target_size,
)
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.wire import MAX_REQUEST_IMAGE_BYTES, images_left_out
from daedalus.tools.vision import crop


def _noisy_png(width: int, height: int, seed: int = 0) -> bytes:
    """A picture that does not compress: random pixels, as large as PNG makes them."""
    image = Image.frombytes("RGB", (width, height), random.Random(seed).randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


@functools.cache
def _render_frame(width: int, height: int) -> Image.Image:
    grain = Image.frombytes("L", (width, height), random.Random(0).randbytes(width * height)).point(lambda v: v // 11)
    shade = Image.linear_gradient("L").resize((width, height))
    return Image.merge("RGB", (shade, Image.eval(shade, lambda v: 255 - v), grain))


def _render_png(width: int, height: int, seed: int = 0) -> bytes:
    """A picture like a game's rendered frame: smooth shading with fine grain, large as PNG and small
    as JPEG, as the stuck session's screenshots were. ``seed`` marks a corner so each is its own."""
    image = _render_frame(width, height).copy()
    image.paste((seed * 37 % 256, 0, 0), (0, 0, 16, 16))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _screenshot_png(width: int, height: int) -> bytes:
    """A picture like a screenshot: flat areas and lines, which PNG keeps small."""
    image = Image.new("RGB", (width, height), (240, 240, 240))
    for y in range(0, height, 24):
        image.paste((20, 20, 20), (40, y, width - 40, y + 2))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as picture:
        return picture.size


def test_the_model_id_names_the_bounds() -> None:
    assert limits_for("claude-opus-5-5") is CLAUDE_HIGH_LIMITS
    assert limits_for("claude-fable-5-1") is CLAUDE_HIGH_LIMITS
    assert limits_for("anthropic/claude-sonnet-4.7") is CLAUDE_HIGH_LIMITS
    assert limits_for("anthropic/claude-sonnet-4.5") is CLAUDE_LIMITS
    assert limits_for("claude-opus-4-20250514") is CLAUDE_LIMITS
    assert limits_for("gpt-6.1") is OPENAI_LIMITS
    assert limits_for("openai/gpt-5.4") is OPENAI_LIMITS
    assert limits_for("grok-4.7") is DEFAULT_LIMITS
    assert limits_for("qwen3-vl-8b") is DEFAULT_LIMITS


def test_a_picture_within_the_bounds_is_sent_as_it_is_stored() -> None:
    data = _screenshot_png(1200, 800)
    fitted = fit_image(data, "image/png", CLAUDE_LIMITS)
    assert fitted.data is data and not fitted.changed and (fitted.width, fitted.height) == (1200, 800)


def test_a_large_screenshot_is_scaled_to_what_the_provider_keeps_and_stays_png() -> None:
    fitted = fit_image(_screenshot_png(3840, 2160), "image/png", CLAUDE_LIMITS)
    assert fitted.mime == "image/png" and fitted.changed
    width, height = _size(fitted.data)
    assert (width, height) == (fitted.width, fitted.height)
    # Anthropic's own example: 3840×2160 is kept at 1456×819 on these models.
    assert (width, height) == (1456, 819)
    assert fitted.original == (3840, 2160)


def test_a_picture_past_the_byte_bound_is_brought_under_it() -> None:
    data = _noisy_png(3000, 2000)
    assert encoded_size(data) > 10 * 1024 * 1024  # the shape of the refused screenshot
    fitted = fit_image(data, "image/png", CLAUDE_LIMITS)
    assert encoded_size(fitted.data) <= MAX_ENCODED_BYTES
    assert max(fitted.width, fitted.height) <= 1568
    assert _size(fitted.data) == (fitted.width, fitted.height)


def test_a_picture_too_large_as_png_goes_out_as_a_jpeg() -> None:
    fitted = fit_image(_noisy_png(1500, 1000), "image/png", ImageLimits("t", 1568, max_encoded_bytes=2_000_000))
    assert fitted.mime == "image/jpeg" and fitted.data.startswith(b"\xff\xd8\xff")
    assert (fitted.width, fitted.height) == (1500, 1000) and encoded_size(fitted.data) <= 2_000_000


def test_when_no_quality_is_small_enough_the_picture_is_scaled_further() -> None:
    tight = ImageLimits("tight", 4000, max_encoded_bytes=200_000)
    fitted = fit_image(_noisy_png(1600, 1200), "image/png", tight)
    assert encoded_size(fitted.data) <= 200_000
    assert fitted.width < 1600 and fitted.fits


def test_the_dimension_bound_holds_for_every_family() -> None:
    data = _screenshot_png(5000, 1000)
    for limits in (CLAUDE_LIMITS, CLAUDE_HIGH_LIMITS, OPENAI_LIMITS, DEFAULT_LIMITS):
        fitted = fit_image(data, "image/png", limits)
        assert max(_size(fitted.data)) <= limits.max_edge
        if limits.max_patches:
            assert math.ceil(fitted.width / limits.patch) * math.ceil(fitted.height / limits.patch) <= limits.max_patches


def test_the_size_is_the_one_the_provider_keeps() -> None:
    # The documented examples of each provider's rule.
    assert target_size(1920, 1080, CLAUDE_LIMITS) == (1456, 819)
    assert target_size(1075, 1520, CLAUDE_LIMITS) == (924, 1307)
    assert target_size(3840, 2160, CLAUDE_HIGH_LIMITS) == (2576, 1449)
    assert target_size(2048, 2048, OPENAI_LIMITS) == (1600, 1600)
    # A computer-use screenshot already sized for its model is not touched.
    assert target_size(1356, 847, CLAUDE_LIMITS) == (1356, 847)
    assert target_size(1440, 900, OPENAI_LIMITS) == (1440, 900)


def test_a_transparent_picture_turned_to_jpeg_has_no_black_background() -> None:
    image = Image.frombytes("RGBA", (2400, 1600), random.Random(3).randbytes(2400 * 1600 * 4))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    fitted = fit_image(buffer.getvalue(), "image/png", ImageLimits("t", 1568, max_encoded_bytes=1_000_000))
    assert fitted.mime == "image/jpeg" and encoded_size(fitted.data) <= 1_000_000


def test_a_picture_that_cannot_be_decoded_is_judged_on_its_bytes() -> None:
    small = fit_image(b"not a picture", "image/png", CLAUDE_LIMITS)
    assert small.fits and small.data == b"not a picture"
    large = fit_image(b"x" * MAX_ENCODED_BYTES, "image/png", CLAUDE_LIMITS)
    assert not large.fits


async def test_fitting_is_kept_so_the_same_picture_is_the_same_bytes_and_the_original_is_untouched(tmp_path: Path) -> None:
    original = tmp_path / "inbox" / "shot.png"
    original.parent.mkdir()
    original.write_bytes(_noisy_png(2600, 1600, seed=7))
    before = original.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    reads: list[int] = []

    async def load() -> tuple[bytes, str]:
        reads.append(1)
        return original.read_bytes(), "image/png"

    store = FittedImages(tmp_path / "fitted")
    first = await store.fit(digest, load, CLAUDE_LIMITS)
    again = await FittedImages(tmp_path / "fitted").fit(digest, load, CLAUDE_LIMITS)
    assert first.changed and first.data == again.data and first.mime == again.mime
    # The second time comes from the kept file: the nine-megabyte original is not read again.
    assert len(reads) == 1
    assert original.read_bytes() == before
    # Kept apart by the bounds they were made for.
    other = await store.fit(digest, load, ImageLimits("small", 512))
    assert max(other.width, other.height) <= 512


async def test_a_picture_that_needed_nothing_is_kept_as_a_note_not_a_copy(tmp_path: Path) -> None:
    data = _screenshot_png(800, 600)

    async def load() -> tuple[bytes, str]:
        return data, "image/png"

    store = FittedImages(tmp_path / "fitted")
    first = await store.fit(None, load, CLAUDE_LIMITS)
    second = await store.fit(None, load, CLAUDE_LIMITS)
    assert first.data is data and second.data is data
    assert not list((tmp_path / "fitted").rglob("*.bin"))
    assert len(list((tmp_path / "fitted").rglob("*.json"))) == 1


async def test_a_damaged_kept_file_is_fitted_again(tmp_path: Path) -> None:
    data = _noisy_png(2600, 1600, seed=9)

    async def load() -> tuple[bytes, str]:
        return data, "image/png"

    store = FittedImages(tmp_path / "fitted")
    first = await store.fit(None, load, CLAUDE_LIMITS)
    for kept in (tmp_path / "fitted").rglob("*.bin"):
        kept.write_bytes(b"truncated")
    second = await store.fit(None, load, CLAUDE_LIMITS)
    assert second.data == first.data


def test_imageview_crops_a_region_at_full_resolution() -> None:
    data = _screenshot_png(4000, 3000)
    part, mime = crop(data, [100, 200, 900, 800])
    assert mime == "image/png" and _size(part) == (800, 600)
    with pytest.raises(ValueError):
        crop(data, [5000, 5000, 6000, 6000])
    with pytest.raises(ValueError):
        crop(data, [1, 2, 3])


# The request: the stuck session's shape, seven large screenshots in one message.


class Blobs:
    def __init__(self) -> None:
        self.by_ref: dict[str, bytes] = {}
        self.reads: list[str] = []

    def add(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()
        self.by_ref[ref] = data
        return ref

    async def __call__(self, ref: str) -> tuple[bytes, str]:
        self.reads.append(ref)
        return self.by_ref[ref], "image/png"


def _provider(blobs: Blobs, fitted: FittedImages | None, model: str = "claude-opus-5") -> OpenAICompatibleProvider:
    def answer(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is sent")

    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="http://127.0.0.1:9/v1")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
                                    image_loader=blobs, images_for=lambda _m: True, fitted_images=fitted)


def _attached(refs: list[str], text: str = "look") -> Message:
    lines = "\n".join(f"- /srv/workspaces/s1/inbox/shot-{i}.png (image/png, 9000000 bytes)" for i in range(len(refs)))
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"{text}\n\nAttached files:\n{lines}")],
                   metadata={IMAGE_REFS_METADATA_KEY: [ImageRefBlock(blob_ref=ref).model_dump(mode="json") for ref in refs]})


def _images_of(body: dict[str, Any]) -> list[bytes]:
    out = []
    for entry in body["messages"]:
        if isinstance(entry.get("content"), list):
            for part in entry["content"]:
                if part.get("type") == "image_url":
                    out.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
    return out


async def test_seven_large_screenshots_all_go_out_fitted_and_the_request_is_the_same_bytes_twice(tmp_path: Path) -> None:
    blobs = Blobs()
    refs = [blobs.add(_render_png(2560, 1440, seed=i)) for i in range(7)]
    messages = [Message(role=MessageRole.system, content_blocks=[TextBlock(text="sys")]), _attached(refs),
                Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="seen")]),
                Message(role=MessageRole.user, content_blocks=[TextBlock(text="continue")])]
    provider = _provider(blobs, FittedImages(tmp_path / "fitted"))
    request = LLMRequest(messages=messages, model="claude-opus-5", max_tokens=100)
    first = await provider._build_body(request, stream=True)
    images = _images_of(first)
    assert len(images) == 7
    assert all(encoded_size(image) <= MAX_ENCODED_BYTES for image in images)
    assert sum(encoded_size(image) for image in images) <= MAX_REQUEST_IMAGE_BYTES
    # A new process, the same history: the same bytes, so the prompt cache holds.
    second = await _provider(blobs, FittedImages(tmp_path / "fitted"))._build_body(request, stream=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # And the originals were not read again for the second request.
    assert blobs.reads.count(refs[0]) == 1
    await provider.aclose()


async def test_pictures_past_the_request_budget_are_named_by_their_path(tmp_path: Path) -> None:
    blobs = Blobs()
    refs = [blobs.add(f"picture {i}".encode()) for i in range(22)]
    messages = [_attached(refs[:21], "older"), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")]), _attached(refs[21:], "newest")]
    provider = _provider(blobs, FittedImages(tmp_path / "fitted"))
    request = LLMRequest(messages=messages, model="claude-opus-5", max_tokens=100)
    body = await provider._build_body(request, stream=True)
    images = _images_of(body)
    # Past twenty pictures the oldest leave, five at a time; the newest message's picture rides.
    assert len(images) == 17 and images[-1] == b"picture 21"
    text = json.dumps(body, ensure_ascii=False)
    assert text.count("picture left out of this request") == 5
    for index in range(5):
        assert f"/srv/workspaces/s1/inbox/shot-{index}.png — ImageView opens it" in text
    # The same history gives the same placeholders.
    assert json.dumps(await provider._build_body(request, stream=True)) == json.dumps(body)
    await provider.aclose()


async def test_the_pictures_tool_results_carry_count_against_the_budget(tmp_path: Path) -> None:
    blobs = Blobs()
    refs = [blobs.add(f"picture {i}".encode()) for i in range(18)]
    tool_images = [ToolResultImage(data=f"shot {i}".encode(), mime_type="image/png") for i in range(3)]
    messages = [_attached(refs),
                Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Shot", arguments_json="{}")]),
                Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="here", images=tool_images)])]
    # 18 + 3 is past twenty: the user turn's oldest five leave.
    assert await images_left_out(messages, blobs) == set(refs[:5])


async def test_a_message_whose_paths_do_not_pair_with_its_pictures_names_the_blob() -> None:
    blobs = Blobs()
    refs = [blobs.add(f"picture {i}".encode()) for i in range(22)]
    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="no list here")],
                      metadata={IMAGE_REFS_METADATA_KEY: [ImageRefBlock(blob_ref=ref).model_dump(mode="json") for ref in refs]})
    provider = _provider(blobs, None)
    body = await provider._build_body(LLMRequest(messages=[message], model="claude-opus-5", max_tokens=100), stream=True)
    assert f"blob {refs[0]}" in json.dumps(body)
    await provider.aclose()


async def test_a_picture_that_alone_is_past_the_bound_is_left_out_whatever_its_place() -> None:
    blobs = Blobs()
    ok = blobs.add(b"small")
    huge = blobs.add(b"x" * MAX_ENCODED_BYTES)
    messages = [_attached([ok]), _attached([huge])]
    assert await images_left_out(messages, blobs) == {huge}


async def test_a_tools_inline_screenshot_is_fitted_and_both_sizes_are_named(tmp_path: Path) -> None:
    blobs = Blobs()
    shot = _screenshot_png(3000, 2000)
    messages = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="look")]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Shot", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="here",
                images=[ToolResultImage(data=shot, mime_type="image/png", width=3000, height=2000)])]),
    ]
    provider = _provider(blobs, FittedImages(tmp_path / "fitted"))
    body = await provider._build_body(LLMRequest(messages=messages, model="claude-opus-5", max_tokens=100), stream=True)
    images = _images_of(body)
    assert len(images) == 1 and max(_size(images[0])) <= 2576
    width, height = _size(images[0])
    assert f"3000x2000 pixels, shown at {width}x{height}" in json.dumps(body)
    await provider.aclose()


async def test_a_computer_use_screenshot_already_sized_for_the_model_is_left_alone(tmp_path: Path) -> None:
    blobs = Blobs()
    shot = _screenshot_png(1356, 847)  # Claude's computer-use size for a 16:10 screen
    fitted = await FittedImages(tmp_path / "fitted").fit(None, _const(shot), CLAUDE_LIMITS)
    assert fitted.data is shot
    assert blobs.reads == []


def _const(data: bytes) -> Any:
    async def load() -> tuple[bytes, str]:
        return data, "image/png"

    return load


async def test_without_a_fitter_pictures_go_out_as_stored() -> None:
    blobs = Blobs()
    ref = blobs.add(_screenshot_png(3000, 2000))
    provider = _provider(blobs, None)
    body = await provider._build_body(LLMRequest(messages=[_attached([ref])], model="claude-opus-5", max_tokens=100), stream=True)
    assert _images_of(body) == [blobs.by_ref[ref]]
    await provider.aclose()
