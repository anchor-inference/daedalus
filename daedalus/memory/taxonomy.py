"""What a memory is about: its category, its topics, and the sensitive subjects it touches.

Every record carries one category from :data:`CATEGORIES` in the record's ``kind`` (the core files
its dedup bucket and its recall filter under that field, so the category is the kind rather than a
second taxonomy beside it) and up to :data:`MAX_TOPICS` short tags in ``metadata["topics"]``.

The heuristics here are deliberately cheap and deterministic: the migration that categorised the
existing records runs them, Remember falls back to them when no classifier model is configured or
the model fails, and the tests can state what they answer. They are word lists, so they are wrong
sometimes; a wrong category costs a tap in the Memory screen, which is why they never refuse
anything on their own except the sensitive subjects, where a false alarm costs one re-phrased
note and a miss costs a secret kept forever.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

CATEGORIES: tuple[str, ...] = ("person", "project", "preference", "fact", "procedure", "other")
"""The categories a memory can have. ``fact`` is what a record is when nothing else fits."""

DEFAULT_CATEGORY = "fact"

LEGACY_KINDS: dict[str, str] = {
    "preference": "preference",
    "profile": "person",
    "entity": "person",
    "decision": "project",
    "project": "project",
    "skill": "procedure",
    "lesson": "procedure",
    "reflection": "procedure",
    "howto": "procedure",
}
"""The core's and the old extractor's kinds, read as a category. A kind missing here (``fact``,
``observation``, ``identifier``, ``note``…) says nothing about the subject, so the text decides."""

SENSITIVE: tuple[str, ...] = ("health", "finances", "intimate", "identity_documents", "precise_location")
"""Subjects Remember refuses unless the operator allowed them in Settings → Memory."""

SENSITIVE_LABELS: dict[str, str] = {
    "health": "health",
    "finances": "personal finances",
    "intimate": "intimate life",
    "identity_documents": "identity documents",
    "precise_location": "a precise location or home address",
}
"""How the refusal names a subject to the model; the app has its own words for the operator."""

MAX_TOPICS = 5
TOPIC_MAX_CHARS = 32


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_CATEGORY_CUES: dict[str, list[re.Pattern[str]]] = {
    "preference": [re.compile(p, re.IGNORECASE) for p in (
        r"\bprefer\w*", r"\b(likes?|dislikes?|hates?)\b", r"\bwants?\b", r"\b(does ?n[o']t|do not) want\b",
        r"\b(always|never)\b", r"\binstead of\b", r"\bstyle\b",
        r"\bпредпочит\w*", r"\bнравит\w*", r"\bлюбит\b", r"\bхочет\b", r"\b(всегда|никогда)\b", r"\bвместо\b",
    )],
    "procedure": [re.compile(p, re.IGNORECASE) for p in (
        r"\bhow to\b", r"\bsteps?\b", r"\bfirst\b.+\bthen\b", r"\bwhen\b.+\b(use|run|check|pass|set)\b",
        r"\bcommand\b", r"\bto (fix|avoid|deploy|install|build|restart|check)\b", r"\b(trap|lesson|gotcha)\b",
        r"\bкак (сделать|настроить|запустить|починить|проверить)\b", r"\bшаг\w*", r"\b(сначала|затем)\b", r"\bкоманд\w*",
    )],
    "person": [re.compile(p, re.IGNORECASE) for p in (
        r"\b(colleague|friend|wife|husband|son|daughter|mother|father|mom|dad|boss|client|customer|contact)s?\b",
        r"\bbirthday\b", r"\b(he|she|his|her)\b", r"(?<![\w.])@[a-z]\w{2,}",
        r"\b(коллег\w*|друг|друга|другу|подруг\w*|жена|жены|жене|муж|мужа|мужу|сын\w*|доч\w*|мама|маме|мамы|папа|папе|папы|начальни\w*|клиент\w*|контакт\w*)\b",
        r"\bдень рождения\b", r"\b(он|она|его|её)\b",
    )],
    "project": [re.compile(p, re.IGNORECASE) for p in (
        r"\bprojects?\b", r"\brepo(sitory|sitories|s)?\b", r"\bcommits?\b", r"\bbranch(es)?\b", r"\bdeploy\w*",
        r"\breleases?\b", r"\bworkspaces?\b", r"\bdeadlines?\b", r"\bmilestones?\b", r"\bPR\s*#?\d+", r"\bpull request\b",
        r"\bdesign directions?\b",
        r"\bпроект\w*", r"\bрепозитор\w*", r"\bкоммит\w*", r"\bветк\w*", r"\bдепло\w*", r"\bрелиз\w*", r"\bдедлайн\w*",
    )],
}
_CATEGORY_TIE_ORDER = ("preference", "procedure", "person", "project")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

_SENSITIVE_CUES: dict[str, re.Pattern[str]] = {
    # "symptom" and "savings" are missing on purpose: in an engineering memory they are "the symptom
    # of the bug" and "token savings" far more often than a body or a bank, and a refusal there would
    # teach the model that the filter is noise.
    "health": _any(
        r"\b(diagnos\w*|diseases?|illness\w*|medications?|prescri(bed|ption)\w*|allerg\w*|therap(y|ist)\w*|depression|anxiety disorder)\b",
        r"\b(blood (pressure|test)|pregnan\w*|surgery|hospital\w*|insulin|diabet\w*|cancer|hiv|psychiatr\w*)\b",
        r"\b(диагноз\w*|болезн\w*|болеет|заболева\w*|лекарств\w*|таблетк\w*|аллерги\w*|терапевт\w*|психотерап\w*|психиатр\w*|депресси\w*)",
        r"\b(артериальн\w* давлени\w*|анализ\w* крови|беремен\w*|больниц\w*|инсулин\w*|диабет\w*|онколог\w*)",
    ),
    "finances": _any(
        r"\b(salary|salaries|income|bank account|account number|iban|credit card|card number|debit card|loans?|mortgage|tax return|net worth|paycheck)\b",
        r"(?<!tech )(?<!technical )\bdebts?\b",
        r"\b(зарплат\w*|доход\w*|банковск\w* (сч[её]т\w*|карт\w*)|номер карты|сч[её]т в банке|ипотек\w*|налогов\w* деклараци\w*)",
        r"(?<!технический )\b(долг|долга|долги|долгов)\b",
    ),
    "intimate": _any(
        r"\b(sex|sexual\w*|intimate|intimacy|erotic\w*|porn\w*|gay|lesbian|bisexual|libido|girlfriend|boyfriend)\b",
        r"\b(секс\w*|интим\w*|эроти\w*|порн\w*|любовни\w*)",
    ),
    "identity_documents": _any(
        r"\b(passports?|ssn|social security( number)?|driver'?s licen[cs]e|national id|id card|residence permit)\b",
        r"\b\d{3}-\d{2}-\d{4}\b",
        r"\b(паспорт\w*|загранпаспорт\w*|снилс\w*|вид на жительство|водительск\w* (прав\w*|удостоверени\w*))",
    ),
    "precise_location": _any(
        r"-?\b\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b",
        r"\b(home address|lives at|i live at|apartment \d+|apt\.? ?\d+|zip code|postcode)\b",
        r"\b\d+[a-z]? [A-Z][a-z]+ (street|avenue|road|lane|boulevard)\b",
        r"\b(домашн\w* адрес\w*|жив[её]т по адресу|живу по адресу|почтов\w* индекс\w*)",
        r"\b(ул\.|улица|проспект|пр-т)\s*[А-ЯЁA-Z][\w-]+,?\s*(д\.|дом)?\s*\d+",
        r"\bкв\.\s*\d+|\bквартир[аеуы] \d+",
    ),
}


def sensitive_subjects(text: str) -> set[str]:
    """The sensitive subjects ``text`` touches, by word list and by the shape of a card number."""
    found = {subject for subject, pattern in _SENSITIVE_CUES.items() if pattern.search(text or "")}
    for match in _CARD.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.add("finances")
    return found


def guess_category(text: str, kind: str = "") -> str:
    """A category from the record's old kind when that names one, else from cue words in the text."""
    mapped = LEGACY_KINDS.get((kind or "").strip().lower())
    if mapped:
        return mapped
    if (kind or "").strip().lower() in CATEGORIES and kind.strip().lower() != DEFAULT_CATEGORY:
        return kind.strip().lower()
    scores = {name: sum(1 for cue in cues if cue.search(text or "")) for name, cues in _CATEGORY_CUES.items()}
    best = max(scores.values(), default=0)
    if best == 0:
        return DEFAULT_CATEGORY
    return next(name for name in _CATEGORY_TIE_ORDER if scores[name] == best)


def normalise_category(value: object) -> str | None:
    """A known category from what a model or a form sent, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return word if word in CATEGORIES else LEGACY_KINDS.get(word)


_STOPWORDS = frozenset("""
the and for are but not you all any can had her was one our out has have this that with from they will would there their
what about which when make like time just know take into year your some could them than then now look only come its over
also back after use two how work first well way even new want because these give most should been were said each does did
him his she who why where while very much many such here more other into upon being same need needs needed used using
operator agent user session sessions note notes thing things kept keep must may might every never always don't doesn't isn't
it's i'm via per yes no off own too once only both before after again further until against between through during above
below under again whether without within onto across toward towards since unless already still yet ever
rather earlier later found posted named called calling e.g i.e etc seq live run runs read write reply fixed point
instead able sure true false really actually currently either neither least less let lets made makes making got get
gets put puts say says see seen show shows shown tell told think thought try tried uses went whole wrong right left
это как что для или при его она они оно мне меня мой моя мои наш наша наши ваш ваша вас нас вам нам был была были было
быть есть нет так там тут где когда кто чем чтобы если уже ещё еще только тоже также все всё всех весь вся этот эта эти
того тому этом этой этих свой своя свои себя себе который которая которые которых него нее неё них ним ними при про над
под без между через после перед потом сейчас всегда никогда можно нужно надо очень один одна одно два три
""".split())
_TOKEN = re.compile(r"[^\W_][\w.+#-]*[^\W_+.-]|[^\W_]+", re.UNICODE)
_HEXISH = re.compile(r"^[0-9a-f]{7,}$")


@dataclass(frozen=True)
class _Candidate:
    word: str
    position: int
    salience: int


def _candidates(text: str) -> list[_Candidate]:
    """Words of ``text`` that could name a topic, first appearance kept, with a distinctiveness score.

    A capital inside a sentence, a dot or a hyphen (``config.toml``, ``board-state``) and length
    each make a word more likely to name a thing than to be grammar."""
    seen: dict[str, _Candidate] = {}
    for position, match in enumerate(_TOKEN.finditer(text or "")):
        raw = match.group()
        word = raw.lower()
        if word in seen or word in _STOPWORDS or len(word) < 3 or len(word) > TOPIC_MAX_CHARS:
            continue
        if sum(ch.isdigit() for ch in word) * 2 >= len(word) or _HEXISH.match(word) or word.startswith(("http", "www.")) or (len(word) < 5 and "." in word):
            continue
        before = text[max(0, match.start() - 2):match.start()]
        sentence_start = match.start() == 0 or before.strip() in (".", "!", "?", ":") or before.endswith("\n")
        salience = (2 if any(ch.isupper() for ch in raw) and not sentence_start else 0) + (1 if any(ch in raw for ch in ".-") else 0) + (1 if len(word) >= 6 else 0)
        seen[word] = _Candidate(word, position, salience)
    return list(seen.values())


def infer_topics(text: str, vocabulary: Mapping[str, int] | None = None, *, limit: int = 3) -> list[str]:
    """Up to ``limit`` topics for ``text``, reusing the topics already in use where the text names them.

    Reuse comes first because a topic is only useful as a group: a tag no other record carries is a
    heading over one card. So a word that is already a topic wins, and at most one new word is
    added beside reused ones (two when nothing was reused), and only a word that looks like a name."""
    candidates = _candidates(text)
    known = vocabulary or {}
    reused = sorted((c for c in candidates if known.get(c.word, 0) > 0), key=lambda c: (-known[c.word] * (1 + c.salience), c.position))
    chosen = [c.word for c in reused[:limit]]
    room = min(limit - len(chosen), 1 if chosen else 2)
    fresh = sorted((c for c in candidates if c.word not in chosen and c.salience >= 1), key=lambda c: (-c.salience, c.position))
    chosen += [c.word for c in fresh[:max(0, room)]]
    if not chosen and candidates:
        chosen = [max(candidates, key=lambda c: (len(c.word), -c.position)).word]
    return chosen


def corpus_vocabulary(texts: Iterable[str]) -> Counter[str]:
    """The words shared by at least two of ``texts`` and by no more than half of them: the topics a
    first categorisation can hand out so that records about one thing end up under one heading."""
    documents = [{c.word for c in _candidates(text)} for text in texts]
    frequency: Counter[str] = Counter()
    for words in documents:
        frequency.update(words)
    ceiling = max(2, len(documents) // 2)
    return Counter({word: n for word, n in frequency.items() if 2 <= n <= ceiling})


def normalise_topics(raw: object) -> list[str]:
    """Topics as the store keeps them: lower case, hyphens for spaces, no ``#``, short, unique, at most five."""
    if isinstance(raw, str):
        items: list[object] = re.split(r"[,;\n]", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        word = re.sub(r"\s+", "-", item.strip().lower().lstrip("#"))
        word = re.sub(r"[^\w.+-]", "", word).strip(".-")[:TOPIC_MAX_CHARS]
        if word and word not in out:
            out.append(word)
        if len(out) >= MAX_TOPICS:
            break
    return out


def record_topics(metadata: Mapping[str, object] | None) -> list[str]:
    return normalise_topics((metadata or {}).get("topics") or [])


@dataclass(frozen=True)
class Classification:
    category: str
    topics: list[str]
    sensitive: set[str] = field(default_factory=set)
    source: str = "heuristic"
    """``model`` when the classifier model answered, ``heuristic`` when the word lists did."""


CLASSIFY_PROMPT = """Classify one note from an AI agent's long-term memory. Answer with one JSON object and nothing else:
{{"category": "...", "topics": ["..."], "sensitive": ["..."]}}

category: exactly one of person, project, preference, fact, procedure, other.
  person = about a specific person; project = about a piece of work, repository or deadline;
  preference = how someone likes things done; procedure = how to do something, a lesson, a trap;
  fact = any other durable fact; other = none of these.
topics: one to three short lower-case tags naming what the note is about. Reuse these existing topics when they fit: {vocabulary}
sensitive: the subjects among health, finances, intimate, identity_documents, precise_location that the note discloses
  about a real person (an empty list when none). A technical note about a "health check" or "token savings" is not sensitive.

The note:
{text}"""


async def classify(
    text: str,
    *,
    vocabulary: Mapping[str, int] | None = None,
    ask: Callable[[str], Awaitable[str]] | None = None,
) -> Classification:
    """The note's category, topics and sensitive subjects: from the classifier model when one is
    given and it answers with usable JSON, else from the word lists.

    The word lists' sensitive subjects are always added to the model's: the model is the better
    reader, but it is also the thing a prompt inside the note may talk out of its answer."""
    heuristic_sensitive = sensitive_subjects(text)
    if ask is not None:
        top = ", ".join(word for word, _ in Counter(vocabulary or {}).most_common(40)) or "(none yet)"
        try:
            answer = await ask(CLASSIFY_PROMPT.format(vocabulary=top, text=text[:4000]))
            start = answer.find("{")
            parsed, _ = json.JSONDecoder().raw_decode(answer[start:]) if start >= 0 else (None, 0)
        except Exception:  # noqa: BLE001 — a classifier that fails leaves the word lists to answer
            parsed = None
        if isinstance(parsed, dict):
            category = normalise_category(parsed.get("category"))
            topics = normalise_topics(parsed.get("topics"))[:3]
            sensitive = {s for s in (parsed.get("sensitive") or []) if isinstance(s, str) and s in SENSITIVE}
            if category:
                return Classification(category, topics or infer_topics(text, vocabulary), sensitive | heuristic_sensitive, "model")
    return Classification(guess_category(text), infer_topics(text, vocabulary), heuristic_sensitive, "heuristic")


__all__ = [
    "CATEGORIES",
    "DEFAULT_CATEGORY",
    "LEGACY_KINDS",
    "MAX_TOPICS",
    "SENSITIVE",
    "SENSITIVE_LABELS",
    "Classification",
    "classify",
    "corpus_vocabulary",
    "guess_category",
    "infer_topics",
    "normalise_category",
    "normalise_topics",
    "record_topics",
    "sensitive_subjects",
]
