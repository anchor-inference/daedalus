"""Reading a kept image describes it instead of refusing it as a binary file.

An orchestrator handed two screenshots of bugs read each with Peek(op='read') and was told "a binary
file; it cannot be shown here", so it passed them on without knowing what they showed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from daedalus.tools import vision


@dataclass
class Kept:
    name: str
    mime: str


class Manager:
    def __init__(self, model: Any) -> None:
        self.model = model

    def vision_model(self) -> Any:
        return self.model


async def test_an_image_is_described_by_the_vision_model(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    async def look(model: Any, manager: Any, data: bytes, mime: str, task: str, *, detail: str = "focused", instruction: str = "", session_id: str | None = None) -> tuple[str, str]:
        asked.append((mime, detail))
        return "A login form; the error reads \"Invalid token\".", "small-vision"

    monkeypatch.setattr(vision, "look", look)
    body = await vision.kept_body(Manager(object()), b"\x89PNG\r\n\x1a\n\x00", Kept("screenshot.png", "image/png"))
    assert "Invalid token" in body and "screenshot.png" in body and "small-vision" in body
    assert asked == [("image/png", "full")]


async def test_without_a_vision_model_the_refusal_says_why_and_that_it_can_be_handed_on() -> None:
    with pytest.raises(vision.VisionUnavailable, match="no vision model is configured.*handed on"):
        await vision.kept_body(Manager(None), b"\x89PNG\x00", Kept("screenshot.png", "image/png"))


async def test_text_is_still_read_as_numbered_lines_and_other_binaries_are_refused() -> None:
    assert "1\tfirst" in await vision.kept_body(Manager(None), b"first\nsecond\n", Kept("notes.txt", "text/plain"))
    from daedalus.host.peek import PeekRefused

    with pytest.raises(PeekRefused, match="binary file"):
        await vision.kept_body(Manager(None), b"\x00\x01", Kept("blob.bin", "application/octet-stream"))
