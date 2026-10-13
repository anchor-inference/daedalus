"""Answers "how do I…" about Daedalus itself: the settings index first, the shipped documentation after it.

The agent used to search the documentation by requiring every word of the question to appear in a
page. Asked "Как мне включить STT?" it found nothing in English prose, searched again, and sent the
operator to the voice page — the concierge — when speech recognition for the chat lives in
Settings → Voice & speech. Two things were missing: a description of the settings themselves, and a
search that ranks instead of filtering.

The settings index is ``miniapp/src/screens/settings-index.json``, beside the Settings screen that
reads its sections from it: every section and the fields worth finding, with titles in both
languages, the words people use (STT, диктовка, голосовые сообщения), the configuration path and the
row's anchor. A field's address is ``/app/settings/<section>#<anchor>``; the app opens the section,
scrolls to the row and makes it blink.

The ranking is BM25 over weighted fields — a setting's title and names weigh more than its sentence,
a heading more than its paragraph — with three things a plain BM25 lacks for this corpus:

* words are reduced to a stem (a light suffix strip for Russian and English) and a query word also
  matches a longer word it begins (``голос`` finds ``голосовые``), because Russian inflects;
* a small Russian→English glossary lets a Russian question reach the English documentation;
* a question that asks how to set something (включить, настроить, enable, change…) ranks settings
  above prose, because the answer to it is a place to click, not a page to read.

Everything here is pure and cached by the files' modification times: the tool builds nothing per call.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

INDEX_FILE = Path("miniapp/src/screens/settings-index.json")
ROOT_PAGES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "GOVERNANCE.md", "SECURITY.md")
EXCLUDED_DOCS = frozenset({"PLAN.md", "RESEARCH.md"})
SETTINGS_PATH = "/app/settings"

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WORD = re.compile(r"[^\W_]+", re.UNICODE)
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

CHUNK_CHARS = 1600
"""A long section is cut into windows about this long, on paragraph breaks: a whole page of reference
would otherwise win on sheer length of matches, and its snippet would show the wrong paragraph."""

K1 = 1.2
B = 0.6
SETTING_WEIGHT = 1.6
"""Settings are short and exact; prose is long and repeats words. Without a lift a settings field
loses to any page that happens to say "voice" twenty times."""
INTENT_WEIGHT = 1.5
"""The extra lift a question asking how to set something gives settings."""
SECTION_WEIGHT = 0.75
"""A section answers less precisely than one of its fields: it wins only when no field does."""
HISTORY_WEIGHT = 0.35
"""The changelog says what changed, not how to use it, and mentions everything once."""
INTERNALS_WEIGHT = 0.6
"""Design notes and the architecture are written for whoever changes the code: they name every
setting and every screen while explaining why, and outranked the guide that says how to use them."""
INTERNALS = ("docs/architecture/", "docs/DESIGN.md", "docs/DECISIONS.md", "docs/ACCEPTANCE.md", "docs/ISSUE_TRIAGE.md", "docs/DEVELOPMENT.md")
TRANSLATED_WEIGHT = 0.9
"""A word the glossary put in counts a little less than the word the operator wrote."""
HEAD_WEIGHT = 1.5
EXPLAIN_WEIGHT = 0.4
"""What a question asking how something works leaves of a setting's lift: it wants the page."""
PHRASE_BONUS = 3.0

# Words that carry no subject. Removed from the question before matching; a question made of them
# alone ("как это сделать?") matches nothing rather than everything.
STOP = frozenset("""
a an and are as at be by can could do does doing for from get got have how i if in into is it its me my
of on or our please should so that the their them then there these this to us want was we what when
where which who why will with would you your yourself
а без бы был была были было в вам вас ведь во вот все всё вы где да даже для до его ее её если есть
еще ещё же за и из или им их к как какая какие какой ко когда кто ли либо мне меня мной мои мой моя
может можно мы на над надо нам нас не нет ни но ну о об обо он она они оно от по под пожалуйста при
про с со так там тебе тебя то тоже только ты тут у уже хочу хотел хотела чем через что чтобы чтоб эта
эти это этот я сделать сделай делать могу нужно нужна нужен daedalus дедал
""".split())

# Words that ask how to set something. They are not the subject either ("enable" is in half the
# settings), so they are removed like the rest — but their presence lifts settings over prose.
INTENT = frozenset("""
enable enabled disable disabled turn switch change set setup configure configuration config setting
settings option options adjust toggle select choose pick activate deactivate
включить включи включать включено выключить выключи выключать отключить отключи отключать
настроить настрой настраивать настройка настройки настройку поменять поменяй сменить смени изменить
измени задать задай поставить поставь выбрать выбери активировать параметр опция
""".split())

# Words that ask what something is or how it works rather than where to set it: such a question is
# answered by prose, and settings lose their lift — unless the question also asks to set something.
EXPLAIN = frozenset("""
what why explain explanation works work working concept overview difference
что зачем почему объясни объяснить работает работают устроен устроено такое разница
""".split())

# Russian question words → the English the documentation is written in. Keys are stems as `stem`
# makes them; values are English words, stemmed when the glossary is built. Only words the shipped
# documentation actually needs: the settings index already speaks Russian.
GLOSSARY: dict[str, tuple[str, ...]] = {
    "установк": ("install", "installation"), "установ": ("install",), "запуст": ("start", "run"),
    "оркестрац": ("orchestration", "orchestrator"), "оркестратор": ("orchestrator",),
    "страниц": ("page", "pages"), "голос": ("voice",), "реч": ("speech",), "распозна": ("recognition", "transcription"),
    "диктовк": ("dictation",), "озвучк": ("speech", "tts"), "модел": ("model",), "провайдер": ("provider",),
    "ключ": ("key",), "токен": ("token",), "бот": ("bot",), "телеграм": ("telegram",),
    "уведомлен": ("notification",), "поиск": ("search",), "браузер": ("browser",), "терминал": ("terminal",),
    "проект": ("project",), "команд": ("team", "command"), "агент": ("agent",), "сесс": ("session",),
    "памят": ("memory",), "расписан": ("schedule",), "задач": ("task",), "доск": ("board",),
    "обновлен": ("update",), "обнов": ("update",), "бэкап": ("backup",), "резервн": ("backup",),
    "лимит": ("limit",), "бюджет": ("budget", "spend"), "трат": ("spend",), "сжат": ("compaction",),
    "безопасн": ("security",), "вход": ("login", "sign"), "github": ("github",), "гитхаб": ("github",),
    "самоизменен": ("self", "change"), "саморазвит": ("self", "development"), "инструмент": ("tool",),
    "навык": ("skill",), "приложен": ("app",), "десктоп": ("desktop",), "настольн": ("desktop",),
    "вебхук": ("webhook",), "политик": ("policy",), "правил": ("rule",), "песочниц": ("sandbox",),
    "докер": ("docker",), "контейнер": ("container",), "диаграм": ("diagram",), "календар": ("calendar",),
    "импорт": ("import",), "перенес": ("import",), "картинк": ("image", "vision"), "изображен": ("image", "vision"),
}

_RU_ENDINGS = tuple(sorted("""
иями ями ами ией ией иях ях ах ием ем ом ой ей ий ый ая яя ое ее ые ие ую юю его ого ему ому их ых ими ыми
ешь ет ют ут ем им ит ят ишь ете ите ать ять ить еть уть ал ала ало али ил ила ило или ел ела ели ться тся
ость ости ение ения ений ением ании ание ания ов ев ей ам ям а я о е и ы у ю ь
""".split(), key=len, reverse=True))
_EN_ENDINGS = ("ations", "ation", "ings", "ing", "ers", "er", "ies", "ied", "ed", "es", "s", "ly")


def normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def stem(word: str) -> str:
    """A light stem: the longest known ending off, never leaving fewer than three letters."""
    word = normalize(word)
    if CYRILLIC.search(word):
        for ending in _RU_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                return word[: -len(ending)]
        return word
    for ending in _EN_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def words(text: str) -> list[str]:
    return WORD.findall(normalize(text))


def terms(text: str) -> list[str]:
    return [stem(word) for word in words(text)]


_GLOSSARY = {stem(key): tuple(stem(word) for word in value) for key, value in GLOSSARY.items()}


@dataclass(frozen=True, slots=True)
class Query:
    terms: tuple[str, ...]
    """The subject words, stemmed, with what the glossary adds after them."""
    added: frozenset[str]
    """The words the glossary put in: they count, at a discount."""
    intent: bool
    explain: bool
    russian: bool
    phrase: str
    """The subject words as stems joined by spaces, for finding a name in the question."""
    groups: tuple[tuple[str, ...], ...] = ()
    """Each subject word with its translations: a word is answered when any of them is."""


def parse_query(text: str) -> Query:
    raw = words(text)
    intent = any(word in INTENT for word in raw)
    explain = not intent and any(word in EXPLAIN for word in raw)
    subject = [stem(word) for word in raw if word not in STOP and word not in INTENT]
    added: list[str] = []
    groups: list[tuple[str, ...]] = []
    for term in subject:
        translations: list[str] = []
        for key, english in _GLOSSARY.items():
            if term.startswith(key) or (len(term) >= 4 and key.startswith(term)):
                translations.extend(word for word in english if word not in subject and word not in translations)
        added.extend(word for word in translations if word not in added)
        groups.append((term, *translations))
    return Query(
        tuple(dict.fromkeys([*subject, *added])), frozenset(added), intent, explain,
        bool(CYRILLIC.search(text)), " ".join(subject), tuple(dict.fromkeys(groups)),
    )


# -- the entries -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing a search can return: a settings field, a settings section, or a window of a page."""

    kind: str
    """``setting``, ``section`` or ``page``."""
    id: str
    """``setting:<anchor>``, ``section:<id>`` or the page id (``docs:voice``)."""
    fields: tuple[tuple[str, float], ...]
    """Text with its weight; a word counts its field's weight each time it appears."""
    weight: float = 1.0
    title_en: str = ""
    title_ru: str = ""
    about_en: str = ""
    about_ru: str = ""
    link: str = ""
    config: str | None = None
    page: str = ""
    section: str = ""
    """For a page window: the heading DocsRead takes."""
    text: str = ""
    phrases: tuple[str, ...] = ()
    """Its names, stemmed and without stop words: a question that contains one whole is about this entry."""


@dataclass(slots=True)
class Hit:
    entry: Entry
    score: float
    snippet: str = ""


def setting_link(section: str, anchor: str = "", tab: str | None = None) -> str:
    """The app address of a settings section, or of one row of it."""
    path = f"{SETTINGS_PATH}/{quote(section)}"
    if tab:
        path += f"?tab={quote(tab)}"
    return f"{path}#{quote(anchor, safe='.*')}" if anchor else path


def _phrases(names: Iterable[str]) -> tuple[str, ...]:
    found = []
    for name in names:
        stems = [stem(word) for word in words(name) if word not in STOP and word not in INTENT]
        if stems:
            found.append(" ".join(stems))
    return tuple(dict.fromkeys(found))


def settings_entries(index: dict) -> list[Entry]:
    """The settings index as entries: every field, and every section as a coarser answer."""
    sections = {section["id"]: section for section in index.get("sections", [])}
    out: list[Entry] = []
    for section in sections.values():
        names = [section["title"]["en"], section["title"]["ru"], *section.get("synonyms", [])]
        out.append(Entry(
            "section", f"section:{section['id']}",
            ((" ".join(names), 3.0), (f"{section['about']['en']} {section['about']['ru']}", 1.0)),
            weight=SETTING_WEIGHT * SECTION_WEIGHT,
            title_en=section["title"]["en"], title_ru=section["title"]["ru"],
            about_en=section["about"]["en"], about_ru=section["about"]["ru"],
            link=setting_link(section["id"]), phrases=_phrases(names),
        ))
    for item in index.get("fields", []):
        owner = sections.get(item["section"], {})
        names = [item["title"]["en"], item["title"]["ru"], *item.get("synonyms", [])]
        owner_titles = " ".join(owner.get("title", {}).values())
        out.append(Entry(
            "setting", f"setting:{item['anchor']}",
            (
                (" ".join(names), 3.0),
                (f"{item['about']['en']} {item['about']['ru']}", 1.0),
                (f"{owner_titles} {(item.get('config') or '').replace('.', ' ').replace('_', ' ')}", 0.5),
            ),
            weight=SETTING_WEIGHT,
            title_en=f"{owner.get('title', {}).get('en', item['section'])} › {item['title']['en']}",
            title_ru=f"{owner.get('title', {}).get('ru', item['section'])} › {item['title']['ru']}",
            about_en=item["about"]["en"], about_ru=item["about"]["ru"],
            link=setting_link(item["section"], item["anchor"], item.get("tab")),
            config=item.get("config"), phrases=_phrases(names),
        ))
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.casefold()).strip("-")


def _windows(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) > CHUNK_CHARS:
            out.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        out.append(current)
    return out


def page_entries(page_id: str, source: str, text: str) -> list[Entry]:
    """A page cut at its headings, and a long section into windows, each window its own entry."""
    title = next((match.group(2).strip() for match in HEADING.finditer(text)), Path(source).stem)
    # The file's name is the page's subject said in one word (INSTALL, PAGES, VOICE), and a page's
    # title is often not ("Running Daedalus"): it rides with every heading.
    subject = Path(source).stem.replace("-", " ").replace("_", " ")
    weight = HISTORY_WEIGHT if source == "CHANGELOG.md" else INTERNALS_WEIGHT if source.startswith(INTERNALS) else 1.0
    marks = [(match.start(), match.group(2).strip()) for match in HEADING.finditer(text)]
    if not marks or marks[0][0] > 0:
        marks.insert(0, (0, title))
    out: list[Entry] = []
    for number, (start, heading) in enumerate(marks):
        end = marks[number + 1][0] if number + 1 < len(marks) else len(text)
        body = text[start:end]
        for window in _windows(body):
            out.append(Entry(
                "page", page_id, ((f"{subject} {title} {heading}", 3.0), (window, 1.0)),
                weight=weight, title_en=title if heading == title else f"{title} › {heading}",
                page=page_id, section=heading if heading != title else "", text=window,
            ))
    return out


# -- the index ---------------------------------------------------------------------------------


@dataclass
class HelpIndex:
    entries: list[Entry]
    version: str
    postings: dict[str, dict[int, float]] = field(default_factory=dict)
    lengths: list[float] = field(default_factory=list)
    average: dict[str, float] = field(default_factory=dict)
    heads: list[frozenset[str]] = field(default_factory=list)
    """The words of each entry's first field — a setting's names, a page section's headings."""

    def __post_init__(self) -> None:
        self.heads = [frozenset(terms(entry.fields[0][0])) for entry in self.entries]
        postings: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for number, entry in enumerate(self.entries):
            length = 0.0
            for text, weight in entry.fields:
                for term in terms(text):
                    postings[term][number] += weight
                    length += weight
            self.lengths.append(length)
        self.postings = {term: dict(found) for term, found in postings.items()}
        totals: dict[str, list[float]] = defaultdict(list)
        for entry, length in zip(self.entries, self.lengths, strict=True):
            totals[entry.kind].append(length)
        self.average = {kind: (sum(values) / len(values)) or 1.0 for kind, values in totals.items()}
        self._vocabulary = sorted(self.postings)

    def _matching(self, term: str) -> list[tuple[str, float]]:
        """The indexed words a query word stands for: itself, and longer words it begins once it is
        long enough to mean something (``голос`` → ``голосов``), at a small discount."""
        out = [(term, 1.0)] if term in self.postings else []
        if len(term) >= 4:
            out.extend((word, 0.8) for word in self._prefixed(term) if word != term)
        return out

    def _prefixed(self, term: str) -> list[str]:
        start = bisect.bisect_left(self._vocabulary, term)
        found = []
        for word in self._vocabulary[start:]:
            if not word.startswith(term):
                break
            found.append(word)
        return found

    def search(self, text: str, limit: int = 10, kinds: Sequence[str] | None = None) -> list[Hit]:
        query = parse_query(text)
        if not query.terms:
            return []
        total = len(self.entries)
        groups = query.groups or tuple((term,) for term in query.terms)
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, set[str]] = defaultdict(set)
        for group in groups:
            # A word and its translations are one word of the question: an entry that says both
            # "приложение" and "app" has answered it once, not twice.
            best: dict[int, float] = defaultdict(float)
            for term in group:
                discount = TRANSLATED_WEIGHT if term in query.added else 1.0
                for word, closeness in self._matching(term):
                    found = self.postings[word]
                    idf = math.log(1 + (total - len(found) + 0.5) / (len(found) + 0.5))
                    for number, frequency in found.items():
                        entry = self.entries[number]
                        norm = 1 - B + B * self.lengths[number] / self.average[entry.kind]
                        part = idf * frequency * (K1 + 1) / (frequency + K1 * norm) * closeness * discount
                        best[number] = max(best[number], part)
                        matched[number].add(term)
            for number, part in best.items():
                scores[number] += part
        hits: list[Hit] = []
        for number, score in scores.items():
            entry = self.entries[number]
            if kinds and entry.kind not in kinds:
                continue
            # A question is about the entry that answers most of it: an entry matching one word of
            # three is worth much less than the one matching all three, however often it says it.
            found = matched[number]
            covered = sum(1 for group in groups if found.intersection(group)) / len(groups)
            score *= entry.weight * (0.35 + 0.65 * covered) ** 2
            # The whole question in the entry's name or heading — "install … server" and the section
            # "2. A server" of the page INSTALL — beats a paragraph that says both words often.
            head = self.heads[number]
            if all(any(_named(term, head) for term in group) for group in groups):
                score *= HEAD_WEIGHT
            if entry.kind != "page" and query.intent:
                score *= INTENT_WEIGHT
            # A name of the entry said whole in the question: the longer the name, the surer the match,
            # and a one-word name ("STT") still tells the row that is called that from the rows that
            # merely mention it ("STT key", "STT model").
            said = [phrase for phrase in entry.phrases if f" {phrase} " in f" {query.phrase} "]
            if said:
                score += PHRASE_BONUS * entry.weight * max(len(phrase.split()) for phrase in said) ** 0.5
            if entry.kind != "page" and query.explain:
                score *= EXPLAIN_WEIGHT
            hits.append(Hit(entry, score))
        hits.sort(key=lambda hit: (-hit.score, hit.entry.id))
        return _distinct(hits, limit, query)


def _named(term: str, head: frozenset[str]) -> bool:
    return term in head or (len(term) >= 4 and any(word.startswith(term) for word in head))


def _distinct(hits: list[Hit], limit: int, query: Query) -> list[Hit]:
    """One hit per settings row and per page section: the windows of one section are one answer."""
    seen: set[tuple[str, str]] = set()
    out: list[Hit] = []
    for hit in hits:
        key = (hit.entry.id, hit.entry.section)
        if key in seen:
            continue
        seen.add(key)
        if hit.entry.kind == "page":
            hit.snippet = _snippet(hit.entry.text, query)
        out.append(hit)
        if len(out) >= limit:
            break
    return out


def _snippet(text: str, query: Query) -> str:
    folded = normalize(text)
    positions = [folded.find(term) for term in query.terms if term and folded.find(term) >= 0]
    at = min(positions, default=0)
    return re.sub(r"\s+", " ", text[max(0, at - 80) : at + 240]).strip()


# -- loading -----------------------------------------------------------------------------------


def _doc_paths(root: Path) -> list[Path]:
    paths = [root / name for name in ROOT_PAGES]
    docs = root / "docs"
    if docs.is_dir():
        paths.extend(path for path in docs.rglob("*.md") if path.name not in EXCLUDED_DOCS)
    return sorted(path for path in paths if path.is_file() and not path.is_symlink())


def page_id(source: str) -> str:
    return source.removesuffix(".md").casefold().replace("/", ":")


_CACHE: dict[Path, tuple[tuple, HelpIndex]] = {}


def load_settings_index(root: Path) -> dict | None:
    path = root / INDEX_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("the settings index at %s cannot be read: %s", path, exc)
        return None


def config_links(root: Path) -> dict[str, str]:
    """Each configuration path the settings index knows, with the address of the row that edits it."""
    index = load_settings_index(root) or {}
    return {
        item["config"]: setting_link(item["section"], item["anchor"], item.get("tab"))
        for item in index.get("fields", []) if item.get("config")
    }


def help_index(root: Path) -> HelpIndex:
    """The index of an installation's settings and documentation, rebuilt only when a file changed."""
    root = root.resolve()
    paths = [*_doc_paths(root), root / INDEX_FILE]
    stamp = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths if path.is_file())
    cached = _CACHE.get(root)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    entries: list[Entry] = []
    digest = hashlib.sha256()
    index = load_settings_index(root)
    if index is not None:
        entries.extend(settings_entries(index))
        digest.update(json.dumps(index, sort_keys=True).encode())
    for path in _doc_paths(root):
        try:
            source = path.resolve().relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        digest.update(source.encode() + b"\0" + text.encode())
        entries.extend(page_entries(page_id(source), source, text))
    built = HelpIndex(entries, digest.hexdigest())
    _CACHE[root] = (stamp, built)
    return built


def describe(hit: Hit, russian: bool) -> str:
    """One line of the tool's answer for a hit."""
    entry = hit.entry
    if entry.kind == "page":
        where = f"{entry.page} § {entry.section}" if entry.section else entry.page
        return f"- {where} — {entry.title_en}: {hit.snippet}"
    first, second = (entry.title_ru, entry.title_en) if russian else (entry.title_en, entry.title_ru)
    about = entry.about_ru if russian else entry.about_en
    label = "Settings" if entry.kind == "section" else "Setting"
    if entry.config:
        config = f" (config `{entry.config}`)"
    elif entry.kind == "setting":
        config = " (kept by the app itself, not in the configuration: only the operator changes it, at the link)"
    else:
        config = ""
    return f"- {label}: {first} / {second} — [{first}]({entry.link}){config}. {about}"


__all__ = [
    "INDEX_FILE",
    "Entry",
    "HelpIndex",
    "Hit",
    "config_links",
    "describe",
    "help_index",
    "load_settings_index",
    "page_entries",
    "page_id",
    "parse_query",
    "setting_link",
    "settings_entries",
    "stem",
    "terms",
]
