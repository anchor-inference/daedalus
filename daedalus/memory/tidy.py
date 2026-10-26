"""The one-off tidy-up of memory after the background pass filled it with restatements.

An audit of the live memory after the first passes found what the pass's old comparison could not see:
a quarter of the active notes were paraphrase pairs, about forty per cent of the archive had been
displaced by the same fact reworded, a handful of legacy notes were probes left by early tests of the
memory itself, and most held suggestions restated another held suggestion. :func:`plan` finds all of
that with the comparison the pass now uses (:mod:`daedalus.memory.similarity`) and :func:`apply`
carries it out as one history entry, whose undo puts every note, source, suggestion and entry back.
In order:

1. reworded active notes of one entry merge into the note most chats confirmed (its words, when the
   operator wrote it; otherwise the longest, or the newest when a value changed), with every source;
2. an archived note with an active twin folds into it as a source;
3. legacy test probes are deleted (and an entry they leave empty with them);
4. held suggestions that repeat a kept note or an earlier suggestion are closed;
5. archived notes that say something no live note says come back while their entry has room under
   the caps that were raised for this; archived notes repeating each other fold into one first.

The Settings screen runs it as a dry run first, which reports the counts and lists the probes it
would delete, and then for real; the same route serves a one-off run after a deploy.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from protocore.contracts.memory import MemoryRecord

from daedalus.memory.entries import ARCHIVED_KEY, Entry, entry_of, is_archived, is_locked
from daedalus.memory.similarity import Judge, Note, record_note

if TYPE_CHECKING:
    from daedalus.memory.curator import MemoryCurator

logger = logging.getLogger(__name__)

PROBES: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"^the secret word is [\w-]+\.?$",
    r"^project code: [A-Z]+-?\d+\.?$",
    r"^the [\w -]{2,40} is called [\w.-]+ and lives in [\w./-]+\.?$",
    r"^the api listens on port \d{2,5}\.?$",
    r"^the [\w -]{2,30} commit is [0-9a-f]{7,40}\.?$",
    r"^deadline for the [\w./-]+ change: ",
    r"^operator instruction \(kept verbatim\): ",
))
"""The shapes of the notes early end-to-end tests of memory left behind ("the secret word is …",
"project code: …", a made-up setting "called … and lives in …", a port, a commit, a deadline the test
set): one short sentence the test planted and later asked for. Only a legacy note — one that came
through the move to entries and nothing confirmed since — is taken for a probe, and the dry run lists
every one before anything is deleted."""


@dataclass
class Merge:
    keep: MemoryRecord
    absorbed: list[MemoryRecord]
    text: str


@dataclass
class Plan:
    merges: list[Merge] = field(default_factory=list)
    folds: list[tuple[MemoryRecord, MemoryRecord]] = field(default_factory=list)
    """(archived note, the note it folds into)."""
    probes: list[MemoryRecord] = field(default_factory=list)
    emptied: list[str] = field(default_factory=list)
    """Entries the probes' removal leaves without a note."""
    closes: list[int] = field(default_factory=list)
    restores: list[MemoryRecord] = field(default_factory=list)
    entry_titles: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            **self.counts,
            "merged": sum(len(m.absorbed) for m in self.merges),
            "merge_groups": len(self.merges),
            "folded": len(self.folds),
            "probes": len(self.probes),
            "probe_list": [{"id": r.id, "entry": self.entry_titles.get(entry_of(r), ""), "text": " ".join(r.text.split())[:160]} for r in self.probes],
            "emptied_entries": len(self.emptied),
            "closed": len(self.closes),
            "restored": len(self.restores),
        }


def is_probe(record: MemoryRecord, origins: set[str]) -> bool:
    text = " ".join(record.text.split())
    return origins <= {"migration"} and not is_locked(record) and any(p.search(text) for p in PROBES)


async def plan(curator: MemoryCurator, judge: Judge, cap: Callable[[Entry], int]) -> Plan:
    """What the tidy-up would do to memory as it is now; nothing is written."""
    entries = {e.id: e for e in await curator.entries.list()}
    records = [r for r in curator.memory.records(curator.tenant) if entry_of(r) in entries]
    facts = await _facts(curator, [r.id for r in records])
    found = Plan(entry_titles={e.id: f"{e.section}/{e.slug}" for e in entries.values()})
    found.counts = {
        "active_before": sum(1 for r in records if not is_archived(r)),
        "archived_before": sum(1 for r in records if is_archived(r)),
    }

    def origins(record: MemoryRecord) -> set[str]:
        return set(facts.get(record.id, {}).get("origins", set()))

    def value(record: MemoryRecord) -> tuple[int, int, str, int, str]:
        fact = facts.get(record.id, {})
        theirs = 1 if is_locked(record) or "operator" in origins(record) else 0
        return (-theirs, -int(fact.get("chats", 0)), _negated(str(fact.get("last") or record.updated_at.isoformat())), -len(record.text), record.id)

    found.probes = sorted((r for r in records if is_probe(r, origins(r))), key=lambda r: (entry_of(r), r.created_at, r.id))
    probe_ids = {r.id for r in found.probes}
    by_entry: dict[str, list[MemoryRecord]] = {}
    for record in records:
        if record.id not in probe_ids:
            by_entry.setdefault(entry_of(record), []).append(record)
    for entry_id in entries:
        if entry_id not in by_entry and any(entry_of(r) == entry_id for r in found.probes) and entries[entry_id].section != "you":
            found.emptied.append(entry_id)

    # 1. Reworded active notes of one entry.
    queries: list[tuple[str, list[Note]]] = []
    asked: list[tuple[str, MemoryRecord]] = []
    actives: dict[str, list[MemoryRecord]] = {}
    for entry_id, notes in by_entry.items():
        ranked = sorted((r for r in notes if not is_archived(r)), key=value)
        actives[entry_id] = ranked
        own = entries[entry_id].section == "you"
        for index, record in enumerate(ranked[1:], start=1):
            queries.append((record.text, [record_note(r, archived=False, own=own) for r in ranked[:index]]))
            asked.append((entry_id, record))
    parent: dict[str, str] = {}
    differ: set[str] = set()

    def root(record_id: str) -> str:
        while parent.get(record_id, record_id) != record_id:
            record_id = parent[record_id]
        return record_id

    for (_, record), match in zip(asked, await judge.twins(queries), strict=True):
        if match is None or match.note.record is None:
            continue
        top = root(match.note.record.id)
        parent[record.id] = top
        if match.values_differ:
            differ.add(top)
    absorbed: set[str] = set()
    survivors: dict[str, list[MemoryRecord]] = {}
    for entry_id, ranked in actives.items():
        groups: dict[str, list[MemoryRecord]] = {}
        for record in ranked:
            groups.setdefault(root(record.id), []).append(record)
        for top, members in groups.items():
            keep = members[0]
            theirs = [m for m in members[1:] if is_locked(m) or "operator" in origins(m)]
            others = [m for m in members[1:] if m not in theirs]
            survivors.setdefault(entry_id, []).extend([keep, *theirs])
            if not others:
                continue
            if is_locked(keep) or "operator" in origins(keep):
                text = keep.text
            elif top in differ:
                text = max([keep, *others], key=lambda r: (str(facts.get(r.id, {}).get("last") or ""), r.id)).text
            else:
                text = max([keep, *others], key=lambda r: (len(r.text), r.id)).text
            found.merges.append(Merge(keep, others, text))
            absorbed.update(m.id for m in others)

    # 2. Archived notes with an active twin; 5a. archived notes repeating each other.
    queries, asked = [], []
    archived: dict[str, list[MemoryRecord]] = {}
    for entry_id, notes in by_entry.items():
        archived[entry_id] = sorted((r for r in notes if is_archived(r)), key=value)
        own = entries[entry_id].section == "you"
        pool = [record_note(r, archived=False, own=own) for r in survivors.get(entry_id, [])]
        for record in archived[entry_id]:
            queries.append((record.text, pool))
            asked.append((entry_id, record))
    folded: set[str] = set()
    for (_, record), match in zip(asked, await judge.twins(queries), strict=True):
        if match is not None and match.note.record is not None:
            found.folds.append((record, match.note.record))
            folded.add(record.id)
    queries, asked = [], []
    for entry_id, notes in archived.items():
        left = [r for r in notes if r.id not in folded]
        own = entries[entry_id].section == "you"
        for index, record in enumerate(left[1:], start=1):
            queries.append((record.text, [record_note(r, archived=True, own=own) for r in left[:index]]))
            asked.append((entry_id, record))
    twin_of: dict[str, MemoryRecord] = {}
    for (_, record), match in zip(asked, await judge.twins(queries), strict=True):
        if match is not None and match.note.record is not None:
            into = match.note.record
            while into.id in twin_of:
                into = twin_of[into.id]
            twin_of[record.id] = into
    by_id = {r.id: r for notes in archived.values() for r in notes}
    found.folds.extend((by_id[record_id], into) for record_id, into in twin_of.items())
    folded.update(twin_of)

    # 5b. What still says something no live note says comes back, most confirmed first, while there is room.
    for entry_id, notes in archived.items():
        room = cap(entries[entry_id]) - len(survivors.get(entry_id, []))
        for record in notes:
            if room <= 0:
                break
            if record.id in folded:
                continue
            found.restores.append(record)
            room -= 1

    # 4. Held suggestions repeating a kept note or an earlier suggestion.
    keys = {f"{e.section}/{e.slug}": e.id for e in entries.values()}
    held = await curator.db.fetchall(
        "SELECT id, proposed_text, detail FROM memory_suggestions WHERE tenant_id = ? AND status = 'open' AND kind = 'addition' ORDER BY id", (curator.tenant,),
    )
    found.counts["held_before"] = len(held)
    restored = {r.id for r in found.restores}
    kept: dict[str, list[Note]] = {}
    queries, held_ids = [], []
    for row in held:
        key = str(_detail(row["detail"]).get("entry_key") or "")
        entry_id = keys.get(key, "")
        own = key.startswith("you/")
        pool = [record_note(r, archived=is_archived(r) and r.id not in restored, own=own) for r in by_entry.get(entry_id, []) if r.id not in absorbed and r.id not in folded]
        queries.append((str(row["proposed_text"]), pool + kept.get(key, [])))
        held_ids.append((int(row["id"]), key, str(row["proposed_text"])))
        # Each suggestion is compared with the ones before it, so a cluster keeps its first.
        kept.setdefault(key, []).append(Note(f"s:{row['id']}", str(row["proposed_text"]), "held", suggestion_id=int(row["id"])))
    for (suggestion_id, _, _), match in zip(held_ids, await judge.twins(queries), strict=True):
        if match is not None:
            found.closes.append(suggestion_id)
    found.counts["model_calls"] = judge.calls
    if judge.unavailable:
        found.counts["model_unavailable"] = 1
    return found


async def apply(curator: MemoryCurator, found: Plan) -> int | None:
    """Carry out a plan as one history entry; a note that changed since the plan was made is left as
    it is, with everything planned for it. Returns the history entry, ``None`` when nothing changed.
    The caller holds no lock; this takes the curator's."""
    async with curator.lock:
        memory, tenant, entries = curator.memory, curator.tenant, curator.entries

        def fresh(record: MemoryRecord) -> MemoryRecord | None:
            live = memory.get_record(tenant, record.id)
            return live if live is not None and live.version == record.version else None

        before: dict[str, MemoryRecord] = {}
        after: dict[str, MemoryRecord] = {}
        removed_sources: list[dict[str, Any]] = []
        added_sources: list[dict[str, Any]] = []
        touched: set[str] = set()
        gone: set[str] = set()

        async def absorb(keep: MemoryRecord, others: Sequence[MemoryRecord]) -> None:
            rows = await entries.source_rows(record_ids=[r.id for r in others])
            have = {(row["origin"], row["session_id"], row["from_seq"]) for row in await entries.source_rows(record_ids=[keep.id])}
            await entries.copy_sources([r.id for r in others], keep.id)
            added_sources.extend(
                {"record_id": keep.id, "origin": row["origin"], "session_id": row["session_id"], "from_seq": row["from_seq"]}
                for row in rows if (row["origin"], row["session_id"], row["from_seq"]) not in have
            )
            removed_sources.extend(rows)
            for other in others:
                before.setdefault(other.id, other)
                await memory.delete(tenant, other.id)
                gone.add(other.id)
            await entries.drop_sources(record_ids=[r.id for r in others])

        merged = folded = probes = restored = 0
        for merge in found.merges:
            keep = fresh(merge.keep)
            others = [r for r in (fresh(o) for o in merge.absorbed) if r is not None]
            if keep is None or not others:
                continue
            before.setdefault(keep.id, keep)
            changed = keep
            if " ".join(merge.text.split()) != " ".join(keep.text.split()):
                changed = await memory.update(tenant, keep.id, text=merge.text)
            await absorb(changed, others)
            after[keep.id] = memory.get_record(tenant, keep.id) or changed
            touched.add(entry_of(keep))
            merged += len(others)
        for record, into in found.folds:
            archived = fresh(record)
            target = memory.get_record(tenant, into.id)
            if archived is None or target is None or target.id in gone:
                continue
            before.setdefault(target.id, target)
            await absorb(target, [archived])
            after.setdefault(target.id, target)
            touched.add(entry_of(target))
            folded += 1
        slugs = {e.id: e.slug for e in await entries.list()}
        for record in found.probes:
            probe = fresh(record)
            if probe is None:
                continue
            before.setdefault(probe.id, probe)
            removed_sources.extend(await entries.source_rows(record_ids=[probe.id]))
            await entries.tombstone(probe.text, entry_slug=slugs.get(entry_of(probe), ""), reason="deleted")
            await memory.delete(tenant, probe.id)
            await entries.drop_sources(record_ids=[probe.id])
            touched.add(entry_of(probe))
            probes += 1
        for record in found.restores:
            archived = fresh(record)
            if archived is None or not is_archived(archived):
                continue
            back = archived.model_copy(update={"metadata": {k: v for k, v in archived.metadata.items() if k != ARCHIVED_KEY}, "version": archived.version + 1, "updated_at": datetime.now(UTC)})
            await memory.restore(back)
            before.setdefault(archived.id, archived)
            after[archived.id] = back
            touched.add(entry_of(archived))
            restored += 1
        emptied = [e for e in found.emptied if not entries.records_of(e)]
        rows = await entries.entry_rows(emptied)
        for entry_id in emptied:
            await curator.db.execute("DELETE FROM memory_entries WHERE tenant_id = ? AND id = ?", (tenant, entry_id))
        now = datetime.now(UTC).isoformat()
        closed: list[int] = []
        if found.closes:
            open_ids = {int(r["id"]) for r in await curator.db.fetchall(
                f"SELECT id FROM memory_suggestions WHERE tenant_id = ? AND status = 'open' AND id IN ({','.join('?' * len(found.closes))})", (tenant, *found.closes),
            )}
            closed = [i for i in found.closes if i in open_ids]
            if closed:
                await curator.db.execute(
                    f"UPDATE memory_suggestions SET status = 'withdrawn', decided_at = ? WHERE tenant_id = ? AND id IN ({','.join('?' * len(closed))})",
                    (now, tenant, *closed),
                )
        for entry_id in touched - set(emptied) - {""}:
            if await entries.get(entry_id) is not None:
                await entries.touched(entry_id)
        if not (before or closed or rows):
            return None
        # Records a later step removed are in "before" only; one that was kept and later folded away
        # must not be in "after" either, or the undo would look for it.
        after = {k: v for k, v in after.items() if k not in gone}
        parts = [f"{n} {word}" for n, word in ((merged, "merged"), (folded, "folded"), (probes, "test notes deleted"), (len(closed), "suggestions closed"), (restored, "brought back")) if n]
        return await curator.journal(
            "operator", "tidy", "Tidied memory: " + (", ".join(parts) or "nothing to do"), list(before.values()), list(after.values()),
            {"sources": removed_sources, "added_sources": added_sources, "closed": closed, "entries": rows},
        )


async def _facts(curator: MemoryCurator, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per record: how many chats confirmed it, its newest confirmation and its origins."""
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = list(ids[start:start + 500])
        if not chunk:
            continue
        rows = await curator.db.fetchall(
            "SELECT record_id, count(DISTINCT CASE WHEN session_id != '' THEN session_id END) + max(session_id = '') AS chats,"
            f" max(at) AS last, group_concat(DISTINCT origin) AS origins FROM memory_sources WHERE tenant_id = ? AND record_id IN ({','.join('?' * len(chunk))})"
            " GROUP BY record_id",
            (curator.tenant, *chunk),
        )
        for row in rows:
            out[str(row["record_id"])] = {"chats": int(row["chats"] or 0), "last": row["last"], "origins": set(str(row["origins"] or "").split(",")) - {""}}
    return out


def _detail(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _negated(text: str) -> str:
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


__all__ = ["PROBES", "Merge", "Plan", "apply", "is_probe", "plan"]
