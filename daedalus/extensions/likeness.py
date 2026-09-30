"""Whether two pieces of the orchestrator's writing are about the same thing: a question asked again in
new words, a card opened again under a new title.

Word stems, not meaning: each word of three letters or more cut to its first five, in any script, so
"аккаунты" and "аккаунтов", "recording" and "recordings" meet. Two texts are the same subject when
they share enough stems, counted against the shorter one. The thresholds were set on a real project's
questions and cards: the questions that were asked again while the first ones were still open scored
0.39 to 0.65 with nine or more stems in common, and no two different questions open at the same time
reached both; a card opened again for the same audit under a new title shared six of seven.

A match is never acted on silently. It is a refusal the orchestrator can override with a reason, or a
line in its state block, so a guess that is wrong costs one more call and never loses work.
"""

from __future__ import annotations

import re

WORD = re.compile(r"[^\W_]+(?:[-.][^\W_]+)*", re.U)
STEM = 5
"""Letters of a word kept: enough to tell "record" from "review", few enough to join a word's forms."""


def stems(text: str) -> set[str]:
    return {w.casefold()[:STEM] for w in WORD.findall(text or "") if len(w) >= 3}


def overlap(a: str, b: str) -> tuple[int, float]:
    """How many stems two texts share, and what share of the shorter one's that is."""
    x, y = stems(a), stems(b)
    if not x or not y:
        return 0, 0.0
    shared = len(x & y)
    return shared, shared / min(len(x), len(y))


def same_question(a: str, b: str) -> bool:
    """Whether two questions to the operator (title, text and options each) ask the same thing."""
    shared, share = overlap(a, b)
    return shared >= 8 and share >= 0.35


def same_card(title: str, other_title: str, objective: str = "", other_objective: str = "") -> bool:
    """Whether a new card is work already on the board: nearly the same title, or the same objective."""
    mine = stems(title)
    if mine and mine == stems(other_title):
        return True
    shared, share = overlap(title, other_title)
    if shared >= 3 and share >= 0.8:
        return True
    shared, share = overlap(objective, other_objective)
    return shared >= 10 and share >= 0.6


__all__ = ["overlap", "same_card", "same_question", "stems"]
