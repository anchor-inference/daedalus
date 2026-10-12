"""Make the README's animated tours from the current browser screenshots.

Run screenshots.py in both languages first. The stills come from the built app and an
invented installation; the GIFs reuse those exact captures so their interface cannot
drift away from the static documentation.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ.get("SHOTS", ROOT / "docs" / "screenshots"))
SIZE = (1200, 675)
TOURS = {
    "browser": ["session-browser", "session-browser-takeover", "session-browser-giveback", "session-browser-replay"],
    "session": ["session", "session-panel-files", "session-panel-preview", "session-browser"],
    "orchestration": ["main", "project-focus", "project-questions", "board"],
    "phone": ["phone-bots", "phone-session", "phone-browser", "phone-project", "phone-more"],
    "selfdev": ["changes", "changes-summary", "changes-diff"],
}


def stage(path: Path, *, phone: bool) -> Image.Image:
    with Image.open(path) as source:
        frame = source.convert("RGB")
    frame.thumbnail((1080, 615 if phone else 675), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", SIZE, "#0b0f17")
    x = (SIZE[0] - frame.width) // 2
    y = (SIZE[1] - frame.height) // 2
    if phone:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((x - 9, y - 9, x + frame.width + 8, y + frame.height + 8), radius=25, fill="#161c27", outline="#364152", width=2)
    canvas.paste(frame, (x, y))
    return canvas


def write_tour(name: str, names: list[str]) -> None:
    scenes = [stage(SHOTS / f"{shot}.png", phone=name == "phone") for shot in names]
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index, scene in enumerate(scenes):
        frames.append(scene)
        durations.append(1600)
        if index + 1 < len(scenes):
            following = scenes[index + 1]
            for step in range(1, 5):
                frames.append(Image.blend(scene, following, step / 5))
                durations.append(70)
    palette_frames = [frame.quantize(colors=96, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE) for frame in frames]
    target = SHOTS / f"{name}.gif"
    palette_frames[0].save(target, save_all=True, append_images=palette_frames[1:], duration=durations, loop=0, optimize=True, disposal=2)
    print(f"wrote {target} ({len(frames)} frames)")


if __name__ == "__main__":
    for tour, shots in TOURS.items():
        write_tour(tour, shots)
