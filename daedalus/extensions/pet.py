"""Small, bounded reactions for the optional in-app companion."""

from __future__ import annotations

import json
import re
from typing import Any

EMOTIONS = frozenset("calm joy curious focused worried sleepy surprised proud shy sad determined affectionate".split())
ACTIONS = frozenset("idle wave nod peek point offer focus read write think listen speak reassure celebrate dance float stretch sip sleep".split())
PROPS = frozenset("book notebook pencil tablet laptop calendar scroll map hourglass clock compass magnifier gear key wrench crystal mug teapot plant lantern pillow blanket palette brush camera music star microphone headphones envelope plane bubble bell gift balloon trophy heart shield".split())


def normalize_reaction(raw: str) -> dict[str, str]:
    """Only a short line and known rig controls may leave the model boundary."""
    match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not match:
        raise ValueError("reaction is not JSON")
    data: Any = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("reaction must be an object")
    value = data.get("line")
    if not isinstance(value, str):
        raise ValueError("reaction line must be text")
    line = " ".join(value.split())[:160]
    if len(line) < 3:
        raise ValueError("reaction has no line")
    emotion = str(data.get("emotion") or "calm")
    action = str(data.get("action") or "idle")
    prop = str(data.get("prop") or "")
    return {
        "line": line,
        "emotion": emotion if emotion in EMOTIONS else "calm",
        "action": action if action in ACTIONS else "idle",
        "prop": prop if prop in PROPS else "",
    }
