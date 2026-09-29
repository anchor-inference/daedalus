"""Site notes: what an agent learned the hard way about a site, kept for the next agent there — once
the operator has read it.

An agent that finally got through a site (the search box that only answers Enter, the filter hidden
behind "More") may propose a short note about it. The note waits for the operator; only an approved
one is shown, to agents of the same project (or to every agent, for a note made outside a project),
on the first read of a page of that host. A page cannot write one: the agent proposes it, the host
keeps it in its own database, and the operator's approval is the only way it becomes something an
agent reads. What a page talked the agent into saying stops at the operator.

They are in the host's key-value table, not the memory store: the agent's memory tools write there
freely, and a remembered record similar to an approved note would be merged into it, changing an
approved text without the operator.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from daedalus.browser.model import InvalidRequest, NotFound

KEY = "browser.site_notes"
NOTE_MAX = 400
"""Characters of one note: a line or two a person approves at a glance."""
ACTIVE_PER_HOST = 5
"""Approved notes shown for one host in one scope; past it the oldest go when a new one is approved."""
WAITING_MAX = 20
"""Notes of one scope waiting for the operator; an agent that proposes more is told to wait."""
TOTAL_MAX = 500
PROPOSED = "proposed"
ACTIVE = "active"
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(slots=True)
class SiteNote:
    id: str
    project_id: str
    """The project whose agents see it; empty for a note made outside a project, which every agent sees."""
    host: str
    text: str
    status: str
    by: str
    """Who proposed it, as the audit names callers (``agent:<session>``, ``staff:<name>``)."""
    proposed_at: float
    approved_at: float = 0.0


def clean_host(host: str) -> str:
    """A host as notes are filed under it: lower case, without a port or a leading ``www.``."""
    value = (host or "").strip().lower().rstrip(".")
    value = value.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0] if value else ""
    value = value.removeprefix("www.")
    if not value or not _HOST.fullmatch(value) or "." not in value.strip("."):
        raise InvalidRequest(f"{host!r} is not a host name (github.com)")
    return value


def clean_text(text: str) -> str:
    """One line of plain words: control characters folded to spaces, and bounded."""
    value = " ".join(_CONTROL.sub(" ", text or "").split())
    if not value:
        raise InvalidRequest("a note needs words: what worked on this site, in a sentence or two")
    if len(value) > NOTE_MAX:
        raise InvalidRequest(f"a note is at most {NOTE_MAX} characters; keep what the next agent needs")
    return value


def covers(note_host: str, host: str) -> bool:
    """Whether a note filed under ``note_host`` is about ``host``: the same, or a name under it."""
    host = (host or "").lower().removeprefix("www.")
    return host == note_host or host.endswith("." + note_host)


class SiteNotes:
    def __init__(self, db: Any) -> None:
        self.db = db
        self._lock = asyncio.Lock()
        self._notes: list[SiteNote] | None = None

    async def _load(self) -> list[SiteNote]:
        if self._notes is None:
            raw = await self.db.kv_get(KEY, [])
            notes: list[SiteNote] = []
            for item in raw if isinstance(raw, list) else []:
                try:
                    notes.append(SiteNote(**{k: item[k] for k in SiteNote.__dataclass_fields__ if k in item}))
                except (TypeError, KeyError):
                    continue  # a row this version cannot read is dropped at the next write, not fatal
            self._notes = notes
        return self._notes

    async def _save(self, notes: list[SiteNote]) -> None:
        self._notes = notes[-TOTAL_MAX:]
        await self.db.kv_set(KEY, [asdict(n) for n in self._notes])

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [asdict(n) for n in await self._load()]

    async def propose(self, *, project_id: str | None, host: str, text: str, by: str) -> dict[str, Any]:
        scope = project_id or ""
        host, text = clean_host(host), clean_text(text)
        async with self._lock:
            notes = list(await self._load())
            same = [n for n in notes if n.project_id == scope and n.host == host and n.text.casefold() == text.casefold()]
            if same:
                return asdict(same[0])
            if sum(1 for n in notes if n.project_id == scope and n.status == PROPOSED) >= WAITING_MAX:
                raise InvalidRequest(f"{WAITING_MAX} site notes already wait for the operator; propose more once they have read those")
            note = SiteNote(uuid.uuid4().hex[:12], scope, host, text, PROPOSED, by[:120], time.time())
            notes.append(note)
            await self._save(notes)
            return asdict(note)

    async def approve(self, note_id: str) -> dict[str, Any]:
        async with self._lock:
            notes = list(await self._load())
            note = next((n for n in notes if n.id == note_id), None)
            if note is None:
                raise NotFound(f"no site note {note_id}")
            note.status, note.approved_at = ACTIVE, time.time()
            # The newest approved notes of a host are what an agent reads; the oldest past the few go.
            active = sorted((n for n in notes if n.project_id == note.project_id and n.host == note.host and n.status == ACTIVE), key=lambda n: n.approved_at)
            dropped = {n.id for n in active[:-ACTIVE_PER_HOST]}
            await self._save([n for n in notes if n.id not in dropped])
            return asdict(note)

    async def delete(self, note_id: str) -> bool:
        async with self._lock:
            notes = list(await self._load())
            kept = [n for n in notes if n.id != note_id]
            if len(kept) == len(notes):
                return False
            await self._save(kept)
            return True

    async def active_for(self, project_id: str | None, host: str) -> list[SiteNote]:
        """The approved notes an agent of ``project_id`` reads on ``host``: its project's and the ones
        made outside a project."""
        if not host:
            return []
        async with self._lock:
            notes = await self._load()
            return [n for n in notes if n.status == ACTIVE and n.project_id in ("", project_id or "") and covers(n.host, host)]


__all__ = ["ACTIVE", "NOTE_MAX", "PROPOSED", "SiteNote", "SiteNotes", "clean_host", "clean_text", "covers"]
