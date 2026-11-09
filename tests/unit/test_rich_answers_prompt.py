"""The rich-answer syntax lives in the base prompt, where every session has it without loading anything.

It used to sit in a skill the prompt told the model to load first, with "ordinary answers stay plain"
beside it, and the model loaded it rarely and drew a card or a flow more rarely still. These tests hold
the move: the syntax is in the working rules, nothing points at the skill, the examples the model copies
come out as tidy text in Telegram, and the rules stay small enough to sit in every request.
"""

from __future__ import annotations

import re
from pathlib import Path

from daedalus.host.prompts import DEFAULT_RULES
from daedalus.transport.telegram.render import flatten_evidence

ROOT = Path(__file__).resolve().parents[2]


def _example(opener: str) -> str:
    """One indented example block of the rules, as the model would write it."""
    lines = DEFAULT_RULES[DEFAULT_RULES.index(f"    {opener}"):].split("\n")
    return "\n".join(line.removeprefix("    ") for line in lines[: lines.index("    :::") + 1])


def test_every_kind_of_rich_block_is_taught_in_the_rules_and_nothing_sends_the_model_to_a_skill() -> None:
    for syntax in (":::card", ":::entity", ":::flow", "mermaid", "[^](url)", "Widget", "AttachMedia", "a table"):
        assert syntax in DEFAULT_RULES, syntax
    assert "Ordinary answers stay plain" not in DEFAULT_RULES
    assert "rich-answers" not in DEFAULT_RULES and not re.search(r"load .{0,40} first", DEFAULT_RULES)
    assert not (ROOT / "skills" / "rich-answers").exists()


def test_the_examples_read_as_text_in_telegram() -> None:
    card = flatten_evidence(_example(":::card"))
    assert ":::" not in card and "title:" not in card and "meta:" not in card
    assert card.startswith("**[1. Name](https://its-own-page)** · *My pick*")
    flow = flatten_evidence(_example(":::flow"))
    assert ":::" not in flow and "note:" not in flow
    assert flow.splitlines() == [
        "**💡 Title**",
        "💬 Customer — writes in",
        "↓ cannot answer",
        "🤖 **Assistant** — answers → 🎧 Operator — takes the rest",
        "*a caption*",
    ]


def test_the_working_rules_stay_small_enough_for_every_request() -> None:
    # About 1 270 tokens of a Qwen tokenizer with the rich-answer syntax in (it added about 290); the
    # rules sit near the head of every request of every session, so growing them is a decision.
    assert len(DEFAULT_RULES) <= 6_000, len(DEFAULT_RULES)
