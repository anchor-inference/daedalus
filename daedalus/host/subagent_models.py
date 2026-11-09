"""Which model presets a session's helpers may run on.

The operator sets a default in Settings and may narrow or widen it per session; the session's own choice
wins. Every agent the session starts (a subagent, an independent agent, a team member hired from it)
is held to the same list, and a child carries the list it was started under as its own, so nothing it
starts in turn can reach further than its parent could.

The list reaches the model in the environment notes, which are fixed for a session, and not in the
tool's description, which is shared by every session: written there, one session's choice would break
every other session's cached prompt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

KEY = "subagent_models"
"""The session metadata key holding the session's own choice; absent means the global default."""

MODES = ("all", "session", "chosen")


@dataclass(frozen=True, slots=True)
class Allowance:
    mode: str
    presets: tuple[str, ...]
    """The presets a helper may name, in the order the operator sees them."""
    leader_model: bool
    """Whether a helper that names no preset, and so runs on the leader's own model, is allowed."""

    @property
    def limited(self) -> bool:
        return self.mode != "all"

    def check(self, preset: str | None) -> None:
        """Refuse ``preset`` (``None`` is the leader's model) when it is outside the list, saying what is inside."""
        if preset is None:
            if self.leader_model:
                return
            raise ValueError(f"this session lets its helpers run only on {self._named()}, which do not include its own model; name one of them")
        if preset in self.presets:
            return
        raise ValueError(f"model {preset!r} is not allowed for helpers of this session; allowed: {self._named()} (the operator sets this in the session's settings)")

    def _named(self) -> str:
        return ", ".join(self.presets) or "no preset"

    def inherited(self) -> dict[str, Any] | None:
        """What a child session is given as its own choice: the list it was started under, never wider."""
        if not self.limited:
            return None
        return {"mode": "chosen", "presets": list(self.presets)}


def choice(metadata: Mapping[str, Any], default: Any) -> tuple[str, list[str], bool]:
    """``(mode, presets, inherited)``: the session's own choice, else the global ``default`` (``SubagentModels``)."""
    own = metadata.get(KEY)
    if isinstance(own, Mapping) and own.get("mode") in MODES:
        return str(own["mode"]), [str(p) for p in own.get("presets") or ()], False
    return default.mode, list(default.presets), True


def allowance(mode: str, chosen: Iterable[str], *, available: Iterable[str], session_preset: str | None) -> Allowance:
    """The list ``mode`` makes of the presets that can run now.

    ``session_preset`` is the preset the leader runs on, ``None`` when it runs on a provider and model
    picked by hand. A chosen preset that was deleted or whose provider is gone simply drops out.
    """
    ready = list(dict.fromkeys(available))
    if mode == "session":
        return Allowance(mode, tuple(p for p in ready if p == session_preset), True)
    if mode == "chosen":
        picked = set(chosen)
        presets = tuple(p for p in ready if p in picked)
        return Allowance(mode, presets, session_preset in presets)
    return Allowance("all", tuple(ready), True)


__all__ = ["KEY", "MODES", "Allowance", "allowance", "choice"]
