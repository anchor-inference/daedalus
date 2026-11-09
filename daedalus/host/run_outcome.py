"""What a run that ended without an answer says about itself, and where it says it.

A loop iteration died on a compaction that could not make progress, and all the chat showed was
"Worked for 27m · 176 steps", a file it had written, and the host's "Context summary" card below
it. Nothing said the run had failed, why, or that no answer had come of it; the next iteration
started as if nothing had happened. A run that ends without an answer now leaves one closing line
in the transcript — why it stopped, which step or tier gave out, and that there is no answer — and a
loop's next iteration is told the same thing before its instruction.

The line is a transcript row only. It is not in the working history the model reads, so it costs no
context and cannot be mistaken for an answer; the loop note is the one place the model hears of it.
"""

from __future__ import annotations

from typing import Any

from protocore.contracts.types import Message, MessageRole, TextBlock

OUTCOME_METADATA_KEY = "daedalus.run_outcome"
"""On the transcript row that closes a run without an answer: the outcome, for the app to draw."""

PROVIDER_KINDS = frozenset({"llm_provider_error", "llm_timeout", "llm_stream_idle", "llm_rate_limit"})

DETAIL_CHARS = 400

REASONS = {
    "context": "the conversation no longer fitted the model's context window, and compaction could not make it fit",
    "compaction": "compaction could not shrink the history",
    "provider": "the model provider failed",
    "cancelled": "it was stopped before it finished",
    "error": "it failed",
}
"""The model's and the log's words for each cause. The app draws its own, translated, from ``cause``."""


def run_outcome(
    status: str,
    *,
    error_kind: str = "",
    error_reason: str = "",
    error_message: str = "",
    compaction: dict[str, Any] | None = None,
    steps: int = 0,
    last_tool: str = "",
) -> dict[str, Any] | None:
    """The outcome of a run that produced no answer, or ``None`` when it did.

    ``compaction`` is the last ``compaction_completed`` payload the run emitted: when the run died of
    size, it is what says which tier gave out — the summariser failing, the floor reached.
    ``error_reason`` is the engine's class for a provider failure (``connection``, ``rate_limit``):
    the provider's own code for the stop, which the app lists in the line's technical details.
    """
    if status not in ("failed", "cancelled"):
        return None
    if status == "cancelled":
        cause = "cancelled"
    elif error_kind == "llm_context_window_exceeded":
        cause = "context"
    elif error_kind.startswith("compaction"):
        cause = "compaction"
    elif error_kind in PROVIDER_KINDS:
        cause = "provider"
    else:
        cause = "error"
    outcome: dict[str, Any] = {
        "status": status,
        "cause": cause,
        "error_kind": error_kind,
        "reason": error_reason,
        "detail": " ".join(error_message.split())[:DETAIL_CHARS],
        "steps": steps,
        "last_tool": last_tool,
        "answered": False,
    }
    if compaction:
        failures = {**(compaction.get("tier2_failures") or {})}
        for kind, count in (compaction.get("tier3_failures") or {}).items():
            failures[kind] = failures.get(kind, 0) + count
        outcome["compaction"] = {
            "outcome": str(compaction.get("outcome") or ""),
            "reason": str(compaction.get("reason") or ""),
            "prompt_after": int(compaction.get("prompt_after") or compaction.get("tokens_after") or 0),
            "trigger": int(compaction.get("trigger_threshold") or 0),
            "summariser_failures": failures,
            "floor_dropped": int(compaction.get("floor_dropped") or 0),
        }
    return outcome


def compaction_clause(compaction: dict[str, Any] | None) -> str:
    """The last compaction pass, in one clause: where it ended and what failed in it."""
    if not compaction:
        return ""
    parts = [f"the last compaction pass ended {compaction.get('outcome') or 'without an outcome'}"]
    failures = compaction.get("summariser_failures") or {}
    if failures:
        parts.append("summariser failures " + ", ".join(f"{kind} {count}" for kind, count in sorted(failures.items())))
    if compaction.get("floor_dropped"):
        parts.append(f"the floor removed {compaction['floor_dropped']} messages")
    return "; ".join(parts)


def outcome_note(outcome: dict[str, Any]) -> str:
    """The outcome in words: the closing line's text, and what a loop's next iteration is told."""
    reason = REASONS.get(str(outcome.get("cause")), REASONS["error"])
    pieces = [f"The run ended without an answer: {reason}."]
    if outcome.get("error_kind"):
        pieces.append(f"Error: {outcome['error_kind']}.")
    if outcome.get("detail"):
        pieces.append(str(outcome["detail"]).rstrip(".") + ".")
    clause = compaction_clause(outcome.get("compaction"))
    if clause:
        pieces.append(clause[:1].upper() + clause[1:] + ".")
    if outcome.get("steps"):
        last = f", the last one {outcome['last_tool']}" if outcome.get("last_tool") else ""
        pieces.append(f"It had taken {outcome['steps']} steps{last}.")
    return " ".join(pieces)


def outcome_message(outcome: dict[str, Any], run_id: str) -> Message:
    """The transcript row that closes the run."""
    return Message(
        role=MessageRole.system,
        content_blocks=[TextBlock(text=outcome_note(outcome))],
        metadata={OUTCOME_METADATA_KEY: outcome, "daedalus.run_id": run_id},
    )


LOOP_NOTE = (
    "[The previous loop iteration did not finish: {note} Check what it left undone before you continue "
    "with this iteration.]\n\n"
)


def loop_note(outcome: dict[str, Any]) -> str:
    """What the next iteration of a loop is told before its instruction."""
    return LOOP_NOTE.format(note=outcome_note(outcome))


__all__ = [
    "LOOP_NOTE",
    "OUTCOME_METADATA_KEY",
    "compaction_clause",
    "loop_note",
    "outcome_message",
    "outcome_note",
    "run_outcome",
]
