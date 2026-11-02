"""Whether two notes say opposite things: one negates, forbids or reverses what the other states.

Neither comparison that decides "the same fact" without a model can see a reversal. The embeddings
model scored a real note against itself with one "не" added, or "можно" turned into "нельзя", at a
median cosine of 0.99; the word tests drop "не" and "no" as too short to be a word at all, so a long
note and its negation share every counted word. The merge that followed such a verdict kept the longer
text, which is the negated one whenever the negation added the word. Every line drawn without a model
asks :func:`differ` first and leaves a pair it flags to the model or to the operator.

It is a word list and a few rules, on purpose: the background pass compares thousands of pairs, and
the check has to be the same tomorrow as today. It errs towards "they differ", because a false alarm
costs one model call or one suggestion the operator reads, while a miss turns a fact into its opposite
with nobody looking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

NEGATORS = frozenset("""
не нет нельзя без безо
not no never none nothing nobody nowhere neither nor without cannot
dont doesnt didnt isnt arent wasnt werent wont cant couldnt shouldnt wouldnt mustnt havent hasnt hadnt neednt aint
""".split())
"""Words that negate on their own. The apostrophe-less spellings are there because notes are written
by models and people typing fast."""
NEGATIVE_STEMS = (
    "запрещ", "запрет", "выключ", "отключ", "ненавид",
    "forbid", "forbad", "prohibit", "disabl", "disallow", "dislik", "hate", "deni", "deny",
)
"""Beginnings of words that state the negative side of a rule ("запрещено", "disabled", "forbids"):
each counts as a negation, so "выключить X" and "не включать X" agree, and "разрешено" against
"запрещено" does not."""
CONCORD = frozenset("ни никогда никто никого никому никем ничто ничего ничему ничем нигде никуда ниоткуда нисколько ничуть".split())
CONCORD_STEMS = ("никак", "ничей", "ничь")
"""Russian negative pronouns and adverbs. Russian negates twice in one clause ("никогда не отвечай",
"не пьёт ни чай, ни кофе"), so these count as one negation only in a clause without a plain one;
counted always, "никогда не отвечай" would come out as two negations and agree with "всегда отвечай"."""
ALWAYS = frozenset("always всегда постоянно".split())
NEVER = frozenset("never никогда".split())
"""The frequency pair. It is checked on its own as well, because the concord rule above makes "не
всегда X" and "никогда X" each one negation, and they are not the same rule."""
OBLIGATION = frozenset("must should required require requires mandatory obligatory надо нужно требуется следует".split())
OBLIGATION_STEMS = ("долж", "обязан", "обязательн", "необходим")
PERMISSION = frozenset("may can allowed allow allows permitted permit optional optionally можно".split())
PERMISSION_STEMS = ("разреш", "допуст", "необязательн")
"""Modal words: what must be done against what may be. "Можно отвечать по-русски" and "нужно отвечать
по-русски" have no negation between them and are still two different rules."""
GLUED_PREFIXES = ("не", "non", "un", "dis")
"""A negation written into the word: "нерабочий", "недоступен", "необязательно", "unavailable". Read
as one only against the other note, when the other has the same word without the prefix; a list of
every such word in two languages would never be complete, and a prefix alone is no proof."""
GLUED_STOPLIST = (
    "немног", "немал", "недавн", "незадолг", "несколь", "некотор", "некогда", "необходим", "несмотр",
    "неужел", "недел", "небес", "нервн", "нефт", "нейро", "нейтрал", "немец", "немецк", "невест", "нектар",
    "немедлен", "непосредств",
    "display", "discuss", "dispatch", "distribut", "discover", "dismiss", "distanc", "district", "distinct",
    "discord", "discount", "discret", "dispos", "disput", "unique", "univers", "unicod", "uniform", "union",
    "unless", "until", "under",
)
"""Words that begin like a glued negation and are not one: "немного" is not "не" + "много" in any note
that matters, "некоторые" against a note saying "которые" is no reversal, and "немедленно" (at once)
negates no "медленно" (slowly)."""
GLUED_MIN_REST = 4
"""Letters the word must have after its prefix: shorter remainders ("небо", "until") match by chance."""
STEM = 5
"""Characters two words must share at the start to be one word inflected, as in the similarity stems."""

SCOPE_BEFORE = 1
SCOPE_AFTER = 3
"""The words around a negation, a modal or a frequency word, one before and three after (words of three
letters or more, so prepositions and conjunctions do not use up the window), among which
the other note must say one for the word to count. A negation inside a restatement is often about
something the restatement leaves out ("the nickname has nothing to do with it", "X, not the page's
language"); counted anyway, it made two fifths of the pairs the 2026-10-27 tidy-up merged look
reversed. A one-word reversal, the case this check exists for, always negates a word both notes say."""

_CLAUSE = re.compile(r"[.;!?\n]+")
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*", re.UNICODE)


@dataclass(frozen=True)
class _Reading:
    clauses: tuple[tuple[str, ...], ...]
    words: frozenset[str]
    stems: frozenset[str]
    """The first :data:`STEM` characters of every word of three letters or more that is no marker."""


def _is_negator(word: str) -> bool:
    return word in NEGATORS or word.endswith("n't") or word.startswith(NEGATIVE_STEMS)


def _is_concord(word: str) -> bool:
    return word in CONCORD or word.startswith(CONCORD_STEMS)


@lru_cache(maxsize=8192)
def _read(text: str) -> _Reading:
    flat = (text or "").lower().replace("ё", "е").replace("’", "'")
    clauses = tuple(c for c in (tuple(_TOKEN.findall(part)) for part in _CLAUSE.split(flat)) if c)
    words = frozenset(w for clause in clauses for w in clause)
    stems = frozenset(w[:STEM] for w in words if _content(w))
    return _Reading(clauses, words, stems)


def _content(word: str) -> bool:
    return len(word) >= 3 and not _is_negator(word) and not _is_concord(word)


def _anchored(clause: tuple[str, ...], index: int, other: _Reading) -> bool:
    """Whether a word near ``clause[index]`` is one the other note says too. The window counts words
    of three letters or more only: "без наших IP и доменов" must reach "доменов" as "без внутренних
    IP/доменов" does, or one note of a pair counts a negation the other does not."""
    before = [w for w in clause[:index] if _content(w)][-SCOPE_BEFORE:]
    after = [w for w in clause[index + 1:] if _content(w)][:SCOPE_AFTER]
    return any(w[:STEM] in other.stems for w in before + after)


def _glued(word: str, other: _Reading) -> bool:
    """Whether ``word`` is a negation written into the word, read against the other note's words."""
    if _is_negator(word) or _is_concord(word) or word.startswith(GLUED_STOPLIST):
        return False
    for prefix in GLUED_PREFIXES:
        rest = word[len(prefix):]
        if not word.startswith(prefix) or len(rest) < GLUED_MIN_REST:
            continue
        key, whole = rest[:STEM], word[:len(prefix) + STEM]
        # The other note must say the bare word and must not say the prefixed one itself: two notes
        # that both call a day "нерабочий" agree on it.
        if any(w.startswith(key) for w in other.words) and not any(w.startswith(whole) for w in other.words):
            return True
    return False


def negations(text: str, other: str) -> int:
    """How many negations ``text`` states about what ``other`` also talks of, clause by clause."""
    mine, theirs = _read(text), _read(other)
    total = 0
    for clause in mine.clauses:
        plain = sum(1 for i, w in enumerate(clause) if (_is_negator(w) and _anchored(clause, i, theirs)) or _glued(w, theirs))
        concord = any(_is_concord(w) and _anchored(clause, i, theirs) for i, w in enumerate(clause))
        total += plain if plain else int(concord)
    return total


def _side(text: str, other: str, positive: frozenset[str], positive_stems: tuple[str, ...], negative: frozenset[str], negative_stems: tuple[str, ...]) -> int:
    """``1`` when ``text`` uses only words of the positive side about what ``other`` talks of, ``-1``
    when only the negative side, ``0`` for neither or both."""
    mine, theirs = _read(text), _read(other)
    plus = minus = False
    for clause in mine.clauses:
        for i, w in enumerate(clause):
            if (w in positive or (positive_stems and w.startswith(positive_stems))) and _anchored(clause, i, theirs):
                plus = True
            elif (w in negative or (negative_stems and w.startswith(negative_stems))) and _anchored(clause, i, theirs):
                minus = True
    return 1 if plus and not minus else -1 if minus and not plus else 0


def differ(a: str, b: str) -> bool:
    """Whether two notes differ in polarity or modality: one negates what the other states, says
    "never" where the other says "always", or "may" where the other says "must".

    The count of negations is compared, not its parity: "не пушить, не трогать прод" against "пушить,
    трогать прод" is two reversals, not none. A restatement that negates in other words ("use uv, not
    pip" against "prefer uv over pip") can come out as differing; that pair goes to the model or the
    operator, which is the cheap side to be wrong on."""
    if not _read(a).words or not _read(b).words:
        return False
    if negations(a, b) != negations(b, a):
        return True
    for sides in ((ALWAYS, (), NEVER, ()), (OBLIGATION, OBLIGATION_STEMS, PERMISSION, PERMISSION_STEMS)):
        mine, theirs = _side(a, b, *sides), _side(b, a, *sides)
        if mine and theirs and mine != theirs:
            return True
    return False


__all__ = ["differ", "negations"]
