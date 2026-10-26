"""Memory in and out: an export the operator can read and restore, and an import from anywhere.

The export is a zip of Markdown pages — one per entry, under ``you/``, ``topics/`` and ``areas/``,
with front matter and one ``- bullet`` per record, its sources in a trailing comment — plus
``memory.json`` with the entries, records and sources exactly as kept, so a restore loses nothing the
Markdown leaves out.

An import is untrusted text however it arrives: pasted from another assistant ("[date] - memory" lines,
as the import prompt asks for), a Daedalus export, or a Markdown page someone wrote. Parsing is code,
not a model, so nothing in the text can steer how it is read. Every bullet is scanned for text that
addresses an AI agent and for sensitive subjects; the preview shows what was found with such bullets
unticked, and the apply scans again on the server, refusing the agent-directed ones whatever the
client sent, because a remembered instruction is handed to the agent in later chats.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.memory import MemoryRecord, MemoryScope

from daedalus.memory.entries import (
    PEOPLE,
    PREFERENCES,
    PROFILE,
    RECENT_WORK,
    SECTIONS,
    Entry,
    Target,
    fingerprint,
    slugify,
    target_for,
    topic_title,
)
from daedalus.memory.taxonomy import guess_category, normalise_category, record_topics, sensitive_subjects
from daedalus.security.untrusted import injection_signs

EXPORT_VERSION = 1
MAX_IMPORT_ITEMS = 500
MAX_IMPORT_BYTES = 2_000_000
"""What one import may carry: far more than any assistant's memory, small enough that a zip bomb or
a pasted book is refused before it is read."""
MIN_BULLET_CHARS = 3
MAX_BULLET_CHARS = 1000

SECTION_DIRS = {"you": "you", "topic": "topics", "area": "areas"}


def _page_name(entry: Entry, taken: set[str]) -> str:
    base = f"{SECTION_DIRS.get(entry.section, entry.section)}/{entry.slug or slugify(entry.title) or entry.id}"
    name = f"{base}.md"
    n = 2
    while name in taken:
        name = f"{base}-{n}.md"
        n += 1
    taken.add(name)
    return name


def _front(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _comment_safe(text: str) -> str:
    return text.replace("--", "–")


def page_markdown(entry: Entry, records: Sequence[MemoryRecord], sources: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """One entry as a Markdown page: front matter, then a bullet per record with where it came from."""
    lines = [
        "---",
        f"title: {_front(entry.title)}",
        f"summary: {_front(entry.summary)}",
        f"section: {entry.section}",
        f"project: {_front(entry.project_name or '')}",
        f"updated: {entry.updated_at[:10]}",
        f"aliases: {_front(list(entry.aliases))}",
        "---",
        "",
        f"# {entry.title}",
        "",
    ]
    if entry.summary:
        lines += [entry.summary, ""]
    for record in records:
        where = []
        for source in sources.get(record.id, []):
            title = source.get("session_title") or ("your note" if source.get("origin") == "operator" else source.get("origin") or "")
            where.append(f"{title}, {str(source.get('at') or '')[:10]}".strip(", "))
        text = " ".join(record.text.split())
        tail = f" <!-- source: {_comment_safe('; '.join(dict.fromkeys(w for w in where if w)))} -->" if where else ""
        lines.append(f"- {text}{tail}")
    return "\n".join(lines).rstrip() + "\n"


README = """# Daedalus memory export

One Markdown page per memory entry: `you/` holds your preferences and profile, `topics/` the subjects
memory keeps, `areas/` your projects. Each bullet is one thing memory holds, with the chat it was
learned from in a comment after it.

`memory.json` holds the same entries, records and sources exactly as kept. Import this zip in
Settings → Memory → Import to restore it; the pages alone import too.
"""


def export_zip(entries: Sequence[Entry], records_of: Mapping[str, Sequence[MemoryRecord]], sources: Mapping[str, Sequence[Mapping[str, Any]]], *, unfiled: Sequence[MemoryRecord] = ()) -> bytes:
    """The export archive. ``records_of`` maps an entry id to its records; ``unfiled`` are records
    filed nowhere (rare: a start that has not filed them yet), kept in ``memory.json`` only."""
    buffer = io.BytesIO()
    taken: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", README)
        for entry in entries:
            archive.writestr(_page_name(entry, taken), page_markdown(entry, records_of.get(entry.id, ()), sources))
        every = [r for e in entries for r in records_of.get(e.id, ())] + list(unfiled)
        archive.writestr("memory.json", json.dumps({
            "format": "daedalus-memory",
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "entries": [e.view() for e in entries],
            "records": [r.model_dump(mode="json") for r in every],
            "sources": {r.id: list(sources.get(r.id, ())) for r in every},
        }, ensure_ascii=False, indent=1))
    return buffer.getvalue()


@dataclass
class Item:
    """One bullet an import offers, with where it would be filed and what the scan found."""

    text: str
    category: str = ""
    topics: list[str] = field(default_factory=list)
    section: str = ""
    slug: str = ""
    title: str = ""
    project_id: str = ""
    date: str = ""
    sensitive: list[str] = field(default_factory=list)
    injection: list[str] = field(default_factory=list)
    duplicate: bool = False
    forgotten: bool = False
    """The operator deleted this very text before: offered, but unticked."""

    @property
    def checked(self) -> bool:
        return not (self.injection or self.sensitive or self.duplicate or self.forgotten)

    def view(self, key: int) -> dict[str, Any]:
        return {
            "key": key, "text": self.text, "category": self.category, "topics": self.topics, "section": self.section, "slug": self.slug,
            "title": self.title, "project_id": self.project_id or None, "date": self.date or None, "sensitive": self.sensitive,
            "injection": self.injection, "duplicate": self.duplicate, "forgotten": self.forgotten, "checked": self.checked,
        }


_LINE = re.compile(r"^\s*(?:[-*•·]\s+|\d+[.)]\s+)?(?:\[(?P<date>[^\]]{1,40})\]\s*[-–—:]?\s*)?(?P<text>.+?)\s*$")
_FENCE_LINE = re.compile(r"^\s*(```|~~~)")
_COMMENT = re.compile(r"\s*<!--.*?-->\s*$")


def parse_lines(text: str) -> list[Item]:
    """Bullets from pasted text: one per line, an optional ``[date] -`` in front (the import prompt's
    shape), list markers dropped, headings and front matter skipped."""
    items: list[Item] = []
    in_front = False
    # The import prompt asks for one code block; an assistant's chatter around it ("Here you go:")
    # is not a memory, so when the text has a block only what is inside blocks counts.
    fenced = any(_FENCE_LINE.match(line) for line in text.splitlines())
    inside = False
    for number, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        if number == 0 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            in_front = line.strip() != "---"
            continue
        if _FENCE_LINE.match(line):
            inside = not inside
            continue
        if (fenced and not inside) or not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(_COMMENT.sub("", line))
        if not match:
            continue
        body = " ".join(match.group("text").split())
        if len(body) < MIN_BULLET_CHARS:
            continue
        items.append(Item(text=body[:MAX_BULLET_CHARS], date=(match.group("date") or "").strip()))
    return items


def _front_matter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            raw = value.strip()
            try:
                parsed = json.loads(raw)
                found[key.strip()] = parsed if isinstance(parsed, str) else raw
            except ValueError:
                found[key.strip()] = raw
    return found


def parse_page(text: str) -> list[Item]:
    """Bullets from a Markdown page of an export, filed back in the page its front matter names; only
    the list lines count, so the heading and the summary line are not imported as notes."""
    meta = _front_matter(text)
    section = meta.get("section", "")
    title = meta.get("title", "")
    listed = "\n".join(line for line in text.splitlines() if line.lstrip().startswith(("- ", "* ")))
    items = parse_lines(listed)
    if section in SECTIONS and title:
        slug = slugify(title)
        if section == "you":
            slug = PREFERENCES.slug if slug == PREFERENCES.slug else PROFILE.slug
        for item in items:
            item.section, item.title, item.slug = section, title, slug
    return items


def parse_export(data: Mapping[str, Any]) -> list[Item]:
    """Bullets from an export's ``memory.json``: each record into the entry it was filed in."""
    entries = {str(e.get("id")): e for e in data.get("entries") or [] if isinstance(e, Mapping)}
    items: list[Item] = []
    for raw in data.get("records") or []:
        if not isinstance(raw, Mapping) or not str(raw.get("text") or "").strip():
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        entry = entries.get(str(metadata.get("entry") or ""), {})
        section = str(entry.get("section") or "")
        items.append(Item(
            text=" ".join(str(raw["text"]).split())[:MAX_BULLET_CHARS],
            category=str(raw.get("kind") or ""),
            topics=record_topics(dict(metadata)),
            section=section if section in SECTIONS else "",
            slug=str(entry.get("slug") or ""),
            title=str(entry.get("title") or ""),
            project_id=str(entry.get("project_id") or ""),
            date=str(raw.get("created_at") or "")[:10],
        ))
    return items


def read_archive(blob: bytes) -> list[Item]:
    """Bullets from a zip: its ``memory.json`` when it has one, else every Markdown page in it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a zip archive") from exc
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_IMPORT_BYTES * 5:
            raise ValueError("the archive is too large to import")
        names = {i.filename for i in infos}
        if "memory.json" in names:
            try:
                return parse_export(json.loads(archive.read("memory.json")))
            except ValueError as exc:
                raise ValueError("memory.json in the archive is not valid JSON") from exc
        items: list[Item] = []
        for info in infos:
            if info.filename.endswith(".md") and info.filename.rsplit("/", 1)[-1].lower() != "readme.md":
                items.extend(parse_page(archive.read(info).decode("utf-8", errors="replace")))
        return items


def read_text(text: str) -> list[Item]:
    """Bullets from pasted text: an export's ``memory.json`` pasted whole, a page, or plain lines."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, Mapping) and isinstance(data.get("records"), list):
            return parse_export(data)
    if stripped.startswith("---"):
        return parse_page(stripped)
    return parse_lines(stripped)


def file_item(item: Item, *, known_topics: Iterable[str]) -> Item:
    """Settle where a bullet without a page goes, by the rule a new record is filed by, and its
    category from the words in it (no model: an import's text must not decide how it is read)."""
    item.category = normalise_category(item.category) or guess_category(item.text)
    if item.section and item.title:
        if not item.slug:
            item.slug = slugify(item.title)
        return item
    target: Target = target_for(item.category, MemoryScope.global_, "", item.text, item.topics, known_topics=set(known_topics))
    item.section, item.slug, item.title = target.section, target.slug, target.title or topic_title(target.slug)
    return item


def scan(item: Item, *, existing: Iterable[str], tombstones: Iterable[str]) -> Item:
    """Mark what the preview must show: agent-directed text, sensitive subjects, a bullet memory
    already holds word for word, one the operator deleted before."""
    item.injection = injection_signs(item.text)
    item.sensitive = sorted(sensitive_subjects(item.text))
    item.duplicate = fingerprint(item.text) in set(existing)
    item.forgotten = fingerprint(item.text) in set(tombstones)
    return item


def target_of(item: Item) -> Target:
    """The entry an approved bullet is filed in. The You pages are only ever the two known ones."""
    if item.section == "you":
        return PREFERENCES if item.slug == PREFERENCES.slug else PROFILE
    if item.section == "topic":
        if item.slug == PEOPLE.slug:
            return PEOPLE
        return Target("topic", item.slug or RECENT_WORK.slug, item.title or topic_title(item.slug))
    if item.section == "area":
        return Target("area", item.slug or slugify(item.title) or "imported", item.title or topic_title(item.slug))
    return RECENT_WORK


__all__ = ["EXPORT_VERSION", "Item", "MAX_IMPORT_BYTES", "MAX_IMPORT_ITEMS", "export_zip", "file_item", "page_markdown", "parse_lines", "parse_page", "read_archive", "read_text", "scan", "target_of"]
