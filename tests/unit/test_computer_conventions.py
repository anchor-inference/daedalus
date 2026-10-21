"""The arithmetic between a model's point, the image it was shown and the daemon's screenshot.

Every coordinate the agent clicks goes through these functions, and an error of a pixel's scale lands
the click on the neighbouring button. The table below is worked out by hand from the rules the module
writes down, so a change to the rounding has to change the table and say why.
"""

from __future__ import annotations

import pytest

from daedalus.computer.conventions import (
    Frame,
    PointOutside,
    from_screenshot,
    shown_size,
    smart_resize,
    to_desktop,
    to_screenshot,
)
from daedalus.model_capabilities import (
    CLAUDE_PROFILE,
    OPENAI_PROFILE,
    RELATIVE_PROFILE,
    SMART28_PROFILE,
    UNKNOWN_PROFILE,
    known_computer_use,
    known_image_input,
)

SAME = Frame(1568, 980, 1568, 980)
"""The model was shown the screenshot itself."""
HALF = Frame(784, 490, 1568, 980)
"""The model was shown it at half size."""
SMART = Frame(1260, 784, 1568, 980)
"""A smart_resize image of a 1568×980 screenshot (not square, not the same scale both ways)."""


@pytest.mark.parametrize(("convention", "frame", "point", "pixel"), [
    # Pixels of the shown image the same size as the screenshot: the pixel itself.
    ("screenshot", SAME, (0, 0), (0, 0)),
    ("screenshot", SAME, (412, 230), (412, 230)),
    ("screenshot", SAME, (1567, 979), (1567, 979)),
    # A fraction is a place: 412.4 lies in pixel 412 and 412.6 in 413, by the centre rule.
    ("screenshot", SAME, (412.4, 230.6), (412, 231)),
    # The far edge is clamped to the last pixel; it is still on the image.
    ("screenshot", SAME, (1568, 980), (1567, 979)),
    # Half size: shown pixel v covers screenshot pixels 2v and 2v+1, its centre lands in 2v+1.
    ("screenshot", HALF, (0, 0), (1, 1)),
    ("screenshot", HALF, (100, 50), (201, 101)),
    ("screenshot", HALF, (783, 489), (1567, 979)),
    # smart_resize: the model's pixels are the shown image's, converted back by each axis's own scale.
    ("smart_resize", SMART, (0, 0), (0, 0)),
    ("smart_resize", SMART, (630, 392), (784, 490)),
    ("smart_resize", SMART, (1259, 783), (1567, 979)),
    # Relative: r/1000 of the screenshot, floored; 1000 is the far edge, clamped.
    ("relative1000", SAME, (0, 0), (0, 0)),
    ("relative1000", SAME, (500, 500), (784, 490)),
    ("relative1000", SAME, (999, 999), (1566, 979)),
    ("relative1000", SAME, (1000, 1000), (1567, 979)),
    ("relative1000", HALF, (250, 750), (392, 735)),
])
def test_a_model_point_becomes_the_screenshot_pixel_it_names(convention, frame, point, pixel) -> None:  # type: ignore[no-untyped-def]
    assert to_screenshot(convention, *point, frame) == pixel


@pytest.mark.parametrize(("convention", "point"), [
    ("screenshot", (-1, 10)),
    ("screenshot", (1569.5, 10)),
    ("screenshot", (10, 981)),
    ("relative1000", (-0.1, 10)),
    ("relative1000", (10, 1000.5)),
    ("relative1000", (1568, 900)),  # pixels sent to a relative model's tool: a misread, not a corner
])
def test_a_point_off_the_image_is_refused_rather_than_clamped(convention, point) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PointOutside):
        to_screenshot(convention, *point, SAME)


@pytest.mark.parametrize("convention", ["screenshot", "smart_resize", "relative1000"])
@pytest.mark.parametrize("frame", [SAME, HALF, SMART])
def test_the_pointer_read_back_names_the_same_pixel(convention, frame) -> None:  # type: ignore[no-untyped-def]
    """cursor_position says where the pointer is in the model's frame; clicking there must land on the
    pixel it was read from, or as close as the coarser frame allows."""
    for pixel in ((0, 0), (784, 490), (1567, 979), (333, 777)):
        point = from_screenshot(convention, *pixel, frame)
        back = to_screenshot(convention, *point, frame)
        step_x = frame.width / (1000 if convention == "relative1000" else frame.shown_width)
        step_y = frame.height / (1000 if convention == "relative1000" else frame.shown_height)
        assert abs(back[0] - pixel[0]) <= step_x and abs(back[1] - pixel[1]) <= step_y


def test_a_screenshot_pixel_is_a_desktop_point_by_its_centre() -> None:
    # A Retina display's region in points, its picture half as many pixels as captured.
    region = {"x": -1920.0, "y": 0.0, "w": 1920.0, "h": 1080.0}
    assert to_desktop(0, 0, region, 1568, 882) == pytest.approx((-1920 + 0.5 * 1920 / 1568, 0.5 * 1080 / 882))
    assert to_desktop(1567, 881, region, 1568, 882) == pytest.approx((-1920 + 1567.5 * 1920 / 1568, 881.5 * 1080 / 882))


@pytest.mark.parametrize(("size", "expected"), [
    # (height, width) → Qwen's own answers for these inputs.
    ((980, 1568), (784, 1260)),       # over max_pixels: scaled down to multiples of 28
    ((1080, 1920), (728, 1316)),      # 751.3 high once scaled, floored to 26 × 28
    ((900, 1440), (784, 1260)),
    ((100, 100), (112, 112)),        # rounded to the nearest multiple, 100 → 112 (112 is nearer than 84)
    ((28, 28), (56, 56)),            # under min_pixels: scaled up
    ((50, 2000), (56, 1988)),
    ((1440, 900), (1260, 784)),      # a portrait screen keeps its orientation
])
def test_smart_resize_is_qwens(size, expected) -> None:  # type: ignore[no-untyped-def]
    h, w = smart_resize(*size)
    assert (h, w) == expected
    assert h % 28 == 0 and w % 28 == 0
    assert 56 * 56 <= h * w <= 28 * 28 * 1280


def test_smart_resize_leaves_its_own_output_alone() -> None:
    """Why the host pre-resizes: the model's processor runs smart_resize again on what it is given, and
    on an image of this size it changes nothing, so the model's pixels are the shown image's."""
    for size in ((980, 1568), (1080, 1920), (777, 1333), (2000, 900)):
        once = smart_resize(*size)
        assert smart_resize(*once) == once


@pytest.mark.parametrize(("profile", "size", "expected"), [
    # Claude: long edge 1568 and about 1.15 megapixels; a 16:10 screen is bound by the area.
    (CLAUDE_PROFILE, (1568, 980), (1356, 847)),
    (CLAUDE_PROFILE, (1280, 800), (1280, 800)),   # under both bounds: unchanged, never upscaled
    (CLAUDE_PROFILE, (1568, 588), (1568, 588)),   # a wide strip: bound by the edge only
    # OpenAI: within 1440×900, portrait included.
    (OPENAI_PROFILE, (1568, 980), (1440, 900)),
    (OPENAI_PROFILE, (1568, 1176), (1200, 900)),  # 4:3 is bound by the short side
    (OPENAI_PROFILE, (980, 1568), (900, 1440)),
    # Relative: only the long edge.
    (RELATIVE_PROFILE, (1568, 980), (1440, 900)),
    # smart_resize: Qwen's size, as (width, height).
    (SMART28_PROFILE, (1568, 980), (1260, 784)),
    (UNKNOWN_PROFILE, (1568, 980), (1356, 847)),
])
def test_the_image_shown_is_sized_for_the_family(profile, size, expected) -> None:  # type: ignore[no-untyped-def]
    shown = shown_size(profile, *size)
    assert shown == expected
    if profile.max_pixels and profile.convention != "smart_resize":
        assert shown[0] * shown[1] <= profile.max_pixels


@pytest.mark.parametrize(("model", "family"), [
    ("claude-opus-5-5", "claude"),
    ("claude-sonnet-4-5-20250929", "claude"),
    ("anthropic/claude-haiku-4-5", "claude"),
    ("claude-fable-1", "claude"),
    ("claude-mythos-2", "claude"),
    ("claude-opus-4-1", None),
    ("claude-opus-4-20250514", None),
    ("gpt-5.4", "openai"),
    ("gpt-5.4-mini", "openai"),
    ("gpt-6.1", "openai"),
    ("gpt-5", None),
    ("gpt-4o", None),
    ("Qwen/Qwen2.5-VL-72B-Instruct", "smart28"),
    ("ui-tars-1.5-7b", "smart28"),
    ("ui-tars-72b-dpo", "relative1000"),
    ("doubao-1.5-ui-tars", "relative1000"),
    ("qwen3-vl-235b-a22b-instruct", "relative1000"),
    ("glm-4.5v", "relative1000"),
    ("glm-4.1v-9b-thinking", "relative1000"),
    ("gemini-2.5-pro", "relative1000"),
    ("llama-3.3-70b", None),
    ("", None),
])
def test_each_family_has_its_profile(model, family) -> None:  # type: ignore[no-untyped-def]
    profile = known_computer_use(model)
    assert (profile.family if profile else None) == family


def test_a_claude_model_without_a_profile_still_sees_and_falls_back_to_pixels() -> None:
    from daedalus.tools.computer import profile_for

    assert known_image_input("claude-opus-4-1") is True
    assert profile_for("claude-opus-4-1") is UNKNOWN_PROFILE
    assert UNKNOWN_PROFILE.convention == "screenshot" and not UNKNOWN_PROFILE.known
