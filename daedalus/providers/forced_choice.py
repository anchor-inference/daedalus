"""Forced tool use on the models that refuse it: the call is asked for in words instead.

Claude Opus 5.5, Claude Sonnet 5.5 and the Fable and Mythos 5.1 line answer a forced ``tool_choice`` (one
named tool, or ``required``) with ``400 tool_choice: type "tool" and "any" are not supported for this
model.``, whatever the thinking setting. The core forces a tool when a run owes a call, so every such step
failed wherever those models are served through OpenRouter, OpenCode Zen or any other OpenAI-compatible
route.

The key proxy's Claude login and Anthropic key routes do the same in their own Messages translation, and
the host leaves those two to it: the proxy runs in its own image and imports nothing from the host, and the
proxy is where that route's thinking is withheld from replay. That is why the table below exists twice.

The core's loop is what makes leaving the constraint out safe: a step it forced admits only the call it
asked for, drops the rest and asks again within its bound.
"""

from __future__ import annotations

from typing import Any

UNFORCEABLE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")
"""The models known to refuse a forced ``tool_choice``, in Anthropic's spelling. The list only saves the first
refusal: a model missing from it is learned from the refusal (:func:`learn_from_refusal`)."""

PROXY_TRANSLATED_UPSTREAMS = ("claude", "anthropic")
"""Key-proxy upstreams whose Messages translation steers a forced call itself; the host sends them the
forced choice as it is."""

# The refusal reads 'tool_choice: type "tool" and "any" are not supported for this model.'; matched on the
# two parts without quotes, because a body that wraps it in JSON (OpenRouter's does) escapes them.
_REFUSAL_PARTS = ("tool_choice", "are not supported for this model")

_learned: set[str] = set()


def canonical_model(model: str) -> str:
    """``model`` in Anthropic's spelling: OpenRouter's ``anthropic/claude-opus-5.5`` is ``claude-opus-5-5``."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name.removeprefix("anthropic.").replace(".", "-")


def forces_tools(model: str) -> bool:
    """Whether ``model`` takes a forced ``tool_choice``; ``False`` for one known or found to refuse it."""
    name = canonical_model(model)
    return not (name.startswith(UNFORCEABLE_MODELS) or name in _learned)


def is_forced(body: dict[str, Any]) -> bool:
    """Whether a chat body forces a tool call."""
    choice = body.get("tool_choice")
    return choice == "required" or (isinstance(choice, dict) and bool((choice.get("function") or {}).get("name")))


def steer(body: dict[str, Any]) -> None:
    """Take a chat body's forced ``tool_choice`` out and ask for the call in a closing message.

    Called on the finished body, after the cache breakpoints are placed, so the message goes after every
    one of them and the cached prefix is the one an unforced step writes and the next request reads.
    ``tool_choice`` is removed rather than set to ``auto``, which is what an unforced step sends too: a
    change of ``tool_choice`` alone starts Anthropic's message cache over.
    """
    choice = body.pop("tool_choice", None)
    name = (choice.get("function") or {}).get("name") if isinstance(choice, dict) else None
    if name:
        text = f"Call the `{name}` tool now. Reply with that tool call only: no other call and no prose."
    else:
        text = "Call one of the available tools now. Reply with tool calls only, no prose."
    body["messages"].append({"role": "user", "content": text})


def learn_from_refusal(body: dict[str, Any], status: int, text: str) -> bool:
    """Whether a refusal was the forced choice itself; if so the model is remembered and ``body`` steered.

    The refusal comes before any output, so the same request goes again once, steered, and the model is
    steered from the start on every later request of this process.
    """
    if status != 400 or not is_forced(body) or not all(part in text for part in _REFUSAL_PARTS):
        return False
    _learned.add(canonical_model(str(body.get("model") or "")))
    steer(body)
    return True


__all__ = [
    "PROXY_TRANSLATED_UPSTREAMS",
    "UNFORCEABLE_MODELS",
    "canonical_model",
    "forces_tools",
    "is_forced",
    "learn_from_refusal",
    "steer",
]
