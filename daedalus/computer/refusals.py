"""The desktop daemon's refusals, said in words the model and the operator can act on.

The daemon answers a refused call with a JSON-RPC error whose code and ``data`` are exact but terse
(``1201 {reason: "window_moved"}``). Handed over as they are, a model retried the same click on a
stale screenshot, or kept clicking while the operator had taken the mouse; and the app showed a number.
Each sentence here says what happened and the one thing to do next. The tool and the app's routes use
the same table, so the two never disagree about what a code means.
"""

from __future__ import annotations

from typing import Any

HUMAN_DRIVING = 1101
NO_LEASE = 1200
STALE_SCREENSHOT = 1201
OUT_OF_SCOPE = 1202
PROTECTED_TARGET = 1203
SECURE_DESKTOP = 1204
NO_PERMISSION = 1205
RATE = 1206
UNREACHABLE = 1207
OPERATOR_ONLY = 1208

_NO_LEASE_REASONS = {
    "none": "The operator has not given this conversation control of the computer.",
    "expired": "The time the operator gave this conversation to control the computer has run out.",
    "stopped": "The operator pressed Stop, so control of the computer has ended.",
    "revoked": "Control of the computer was taken back.",
    "other_session": "Another conversation holds control of the computer right now.",
    "superseded": "The operator gave control of the computer to another conversation.",
}

_STALE_REASONS = {
    "unknown": "that screenshot id is not one the desktop handed out",
    "other_lease": "that screenshot was taken under an earlier grant of control",
    "old": "that screenshot is too old",
    "displays_changed": "the displays changed since it was taken",
    "window_moved": "the window moved or changed size since it was taken",
    "window_gone": "the window it showed has closed",
}

_SECURE_REASONS = {
    "lock_screen": "the screen is locked",
    "secure_desktop": "a secure system prompt (such as an administrator confirmation) is on the screen",
    "screen_saver": "the screen saver is running",
}

_PERMISSIONS = {
    "screen": "Screen Recording",
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
}


def explain(code: int, message: str = "", data: Any = None) -> str:
    """One or two sentences for a refusal, ending with what to do next.

    An unknown code keeps the daemon's own message, so a newer daemon's refusal is still readable."""
    detail = data if isinstance(data, dict) else {}
    if code == HUMAN_DRIVING:
        return (
            "The operator is using the mouse or keyboard, so the agent is paused. Do not retry at once: "
            "wait until they stop (the pause ends by itself after a quiet moment) or until they resume control."
        )
    if code == NO_LEASE:
        reason = str(detail.get("reason") or "none")
        first = _NO_LEASE_REASONS.get(reason, _NO_LEASE_REASONS["none"])
        return f"{first} Only the operator can grant control, in the app's Computer control for this conversation; ask them if you need it."
    if code == STALE_SCREENSHOT:
        why = _STALE_REASONS.get(str(detail.get("reason") or ""), "the screen changed since it was taken")
        return f"That point was read from a screenshot that can no longer be used: {why}. Take a new screenshot and read the point from it."
    if code == OUT_OF_SCOPE:
        return (
            "That point or window is outside what the operator allowed (or off the screenshot). Act only inside "
            "the window or app control was granted for, or ask the operator to widen it."
        )
    if code == PROTECTED_TARGET:
        return (
            "That is one of Daedalus's own windows (or another protected app). The agent may never act on it — "
            "approvals and settings there are the operator's alone."
        )
    if code == SECURE_DESKTOP:
        why = _SECURE_REASONS.get(str(detail.get("reason") or ""), "the screen is locked or a secure prompt is showing")
        return f"The desktop cannot be used because {why}. Wait for the operator; do not try to get around it."
    if code == NO_PERMISSION:
        which = _PERMISSIONS.get(str(detail.get("which") or ""), "a system permission")
        hint = str(detail.get("hint") or "").strip()
        sentence = f"The system has not given Daedalus the {which} permission this needs. The operator must grant it in the system settings"
        sentence += f" ({hint})." if hint else "."
        return sentence
    if code == RATE:
        if str(detail.get("limit") or "") == "lease_actions":
            return "This grant of control has used all its actions. Ask the operator for a new grant if the task is not finished."
        retry = detail.get("retry_after_ms")
        wait = f" about {int(retry)} ms" if isinstance(retry, (int, float)) and retry > 0 else " a moment"
        return f"Too many actions too quickly. Wait{wait} before the next one."
    if code == UNREACHABLE:
        return (
            "That window belongs to a program running as administrator, and the system drops input sent to it. "
            "Ask the operator to do this step themselves."
        )
    if code == OPERATOR_ONLY:
        return "Only the operator can do that, from the app."
    text = str(message or "").strip()
    return text or f"The desktop refused the request (code {code})."


__all__ = [
    "HUMAN_DRIVING",
    "NO_LEASE",
    "NO_PERMISSION",
    "OPERATOR_ONLY",
    "OUT_OF_SCOPE",
    "PROTECTED_TARGET",
    "RATE",
    "SECURE_DESKTOP",
    "STALE_SCREENSHOT",
    "UNREACHABLE",
    "explain",
]
