"""The Memory screen's API, answered the way the host answers it and kept between requests.

Records carry a category and topics; a merge keeps the broadest scope's record with the union of the
topics, writes a history entry with the records as they were, and an undo puts them back; a suggestion
applied merges its records the same way. ``writes`` lists every write a check may want to read back.

Records are filed in entries as the host files them (a project's records in its area, a preference in
your preferences, a note about a person in your profile, a topic in a page of its own, the rest in
recent work) unless ``entries`` is given. The entry routes answer with versions and refuse a stale one
with 409; a change in words is answered from ``proposals`` (instruction → operations), else with the
instruction added as one bullet.
"""

from __future__ import annotations

import copy
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

CATEGORIES = ("person", "project", "preference", "fact", "procedure", "other")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


class MemoryStub:
    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        projects: dict[str, str] | None = None,
        suggestions: list[dict[str, Any]] | None = None,
        *,
        entries: list[dict[str, Any]] | None = None,
        summaries: dict[str, str] | None = None,
        sources: dict[str, list[dict[str, Any]]] | None = None,
        proposals: dict[str, dict[str, Any]] | None = None,
        status: dict[str, Any] | None = None,
    ) -> None:
        self.records: list[dict[str, Any]] = copy.deepcopy(records or [])
        self.projects = dict(projects or {})
        self.suggestions: list[dict[str, Any]] = copy.deepcopy(suggestions or [])
        self.history: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, Any]] = []
        self.entries: list[dict[str, Any]] = copy.deepcopy(entries) if entries is not None else []
        self.sources: dict[str, list[dict[str, Any]]] = copy.deepcopy(sources or {})
        self.proposals = dict(proposals or {})
        self.status = {"generate": True, "paused": False, "search_chats": True, "synthesis_preset": "", "budget_usd": 0.5, "spent_today_usd": 0.0, "last_synthesis_at": None, "learning": 0,
                       "model": "cheap.model", "model_reason": "", "model_missing": "", "blocked": "", "running": False, "report": None, "failing_chats": 0, **(status or {})}
        self.learn_runs = 0
        self.reread_runs = 0
        self.tidy_runs: list[bool] = []
        """The ``dry_run`` flag of every tidy-up asked for, in order."""
        self.imported: list[dict[str, Any]] = []
        for record in self.records:
            record.setdefault("locked", False)
            record.setdefault("archived", False)
            if not record.get("entry"):
                record["entry"] = self._file(record)["id"]
        for entry in self.entries:
            if summaries and entry["slug"] in summaries:
                entry["summary"] = summaries[entry["slug"]]

    # -- entries -------------------------------------------------------------------------------

    def _entry(self, entry_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def _ensure(self, section: str, slug: str, title: str, project_id: str | None = None) -> dict[str, Any]:
        found = next((e for e in self.entries if e["section"] == section and e["slug"] == slug), None)
        if found is None:
            found = {"id": f"ent-{section}-{slug}", "section": section, "slug": slug, "title": title, "summary": "", "project_id": project_id,
                     "project_name": self.projects.get(project_id or ""), "aliases": [], "sensitive": [], "edited_by_operator": False, "version": 1,
                     "created_at": _now(), "updated_at": _now()}
            self.entries.append(found)
        return found

    def _file(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("scope") == "project" and record.get("scope_key"):
            key = record["scope_key"]
            return self._ensure("area", _slug(self.projects.get(key, key)), self.projects.get(key, key), key)
        if record.get("category") == "preference":
            return self._ensure("you", "preferences", "Preferences")
        if record.get("category") == "person":
            return self._ensure("you", "profile", "Profile")
        if record.get("topics"):
            topic = record["topics"][0]
            return self._ensure("topic", _slug(topic), topic.replace("-", " ").capitalize())
        return self._ensure("topic", "recent-work", "Recent work")

    def _touch(self, entry_id: str | None) -> None:
        entry = self._entry(entry_id or "")
        if entry is not None:
            entry["version"] += 1
            entry["updated_at"] = _now()

    def _count(self, entry_id: str) -> int:
        return sum(1 for r in self.records if r.get("entry") == entry_id)

    def _view(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {**copy.deepcopy(entry), "count": self._count(entry["id"])}

    def _record_sources(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return copy.deepcopy(self.sources.get(record["id"], [{"origin": "remember", "session_id": None, "session_title": None, "from_seq": None, "to_seq": None, "run_id": None, "quote": "", "at": record.get("created_at") or _now()}]))

    def export_zip(self) -> bytes:
        """What ``GET /api/memory/export.zip`` downloads: a page per entry and ``memory.json``."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("README.md", "# Daedalus memory export\n")
            for entry in self.entries:
                bullets = "".join(f"- {r['text']}\n" for r in self.records if r.get("entry") == entry["id"])
                archive.writestr(f"{entry['section']}/{entry['slug']}.md", f"---\ntitle: \"{entry['title']}\"\nsection: {entry['section']}\n---\n\n{bullets}")
            archive.writestr("memory.json", json.dumps({"format": "daedalus-memory", "version": 1, "entries": self.entries, "records": self.records}))
        return buffer.getvalue()

    def _waiting(self) -> list[dict[str, Any]]:
        """The background pass's suggestions still open: what the review shows and the badges count."""
        return [s for s in self.suggestions if s.get("status", "open") == "open" and s["kind"] in ("revision", "addition")]

    def entry_listing(self) -> dict[str, Any]:
        waiting = self._waiting()
        return {
            "entries": [{**self._view(e), "suggestions": sum(1 for s in waiting if s.get("entry_id") == e["id"])} for e in self.entries],
            "suggestions": len(waiting),
            "sections": {name: sum(1 for e in self.entries if e["section"] == name) for name in ("you", "topic", "area")},
            "records": len(self.records), "unfiled": 0, "status": dict(self.status),
        }

    def entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        entry = self._entry(entry_id)
        if entry is None:
            return None
        records = [r for r in self.records if r.get("entry") == entry_id]
        return {"entry": self._view(entry), "records": [{**copy.deepcopy(r), "sources": self._record_sources(r)} for r in records]}

    def _new_record(self, text: str, entry: dict[str, Any], category: str = "fact", origin: str = "operator") -> dict[str, Any]:
        record = {"id": f"m{len(self.records) + 100 + len(self.history)}", "scope": "project" if entry.get("project_id") else "global", "scope_key": entry.get("project_id") or "",
                  "category": category, "topics": [], "text": text, "salience": 0, "version": 1, "created_at": _now(), "updated_at": _now(), "last_accessed_at": None,
                  "entry": entry["id"], "locked": origin == "operator", "archived": False}
        self.records.insert(0, record)
        self.sources[record["id"]] = [{"origin": origin, "session_id": None, "session_title": None, "from_seq": None, "to_seq": None, "run_id": None, "quote": "", "at": _now()}]
        self._touch(entry["id"])
        return record

    def _snapshot(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": r["id"], "text": r["text"], "kind": r["category"], "topics": r["topics"]} for r in records]

    def _journal_full(self, action: str, before: list[dict[str, Any]], after: list[dict[str, Any]], entries: list[dict[str, Any]] | None = None) -> int:
        history = self._journal(action, self._snapshot(before), self._snapshot(after))
        self.history[0]["_records"] = copy.deepcopy(before)
        self.history[0]["_drop"] = [r["id"] for r in after if r["id"] not in {b["id"] for b in before}]
        self.history[0]["_entries"] = copy.deepcopy(entries or [])
        return history

    def _decided(self, action: str, suggestions: list[dict[str, Any]], made: list[dict[str, Any]]) -> int:
        history = self._journal_full(action, [], made)
        self.history[0]["summary"] = f"{len(suggestions)} suggestion(s)"
        self.history[0]["_reopen"] = [s["id"] for s in suggestions]
        return history

    def _get(self, memory_id: str) -> dict[str, Any] | None:
        return next((r for r in self.records if r["id"] == memory_id), None)

    def listing(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for r in self.records:
            for topic in r["topics"]:
                counts[topic] = counts.get(topic, 0) + 1
        buckets: dict[str, dict[str, Any]] = {}
        for r in self.records:
            b = buckets.setdefault(f"{r['scope']}:{r['scope_key']}", {"scope": r["scope"], "scope_key": r["scope_key"], "title": self.projects.get(r["scope_key"]) if r["scope"] == "project" else None, "count": 0})
            b["count"] += 1
        return {
            "records": copy.deepcopy(self.records),
            "buckets": sorted(buckets.values(), key=lambda b: (b["scope"] != "global", -b["count"])),
            "projects": self.projects,
            "topics": [{"topic": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
            "categories": {c: sum(1 for r in self.records if r["category"] == c) for c in CATEGORIES},
            "suggestions": sum(1 for s in self.suggestions if s.get("status", "open") == "open"),
        }

    def _tidy(self, dry_run: bool) -> dict[str, Any]:
        """The tidy-up as the stub does it: a note saying what an earlier one of its entry says (case
        aside) merges into it, and a planted test note ("the secret word is …") goes; the real run is
        one history entry whose undo puts them back."""
        self.tidy_runs.append(dry_run)
        seen: dict[tuple[str, str], str] = {}
        twins: list[dict[str, Any]] = []
        probes: list[dict[str, Any]] = []
        for record in self.records:
            if record["text"].lower().startswith("the secret word is"):
                probes.append(record)
                continue
            key = (str(record.get("entry")), " ".join(record["text"].lower().split()))
            if key in seen:
                twins.append(record)
            else:
                seen[key] = record["id"]
        found = {"dry_run": dry_run, "merged": len(twins), "merge_groups": len(twins), "folded": 0, "probes": len(probes), "closed": 0, "restored": 0, "emptied_entries": 0,
                 "probe_list": [{"id": r["id"], "entry": "", "text": r["text"]} for r in probes], "history_id": None}
        if not dry_run and (twins or probes):
            gone = {r["id"] for r in twins + probes}
            before = [r for r in self.records if r["id"] in gone]
            self.records = [r for r in self.records if r["id"] not in gone]
            found["history_id"] = self._journal_full("tidy", before, [])
        return found

    def _journal(self, action: str, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> int:
        entry = {"id": len(self.history) + 1, "at": datetime.now(UTC).isoformat(), "actor": "operator", "action": action, "summary": "", "undone_at": None,
                 "before": copy.deepcopy(before), "after": copy.deepcopy(after)}
        self.history.insert(0, entry)
        return entry["id"]

    def _merge(self, ids: list[str], text: str | None, category: str | None, topics: list[str] | None, keep_id: str | None = None, action: str = "merge") -> tuple[dict[str, Any], int]:
        found = [r for r in (self._get(i) for i in ids) if r is not None]
        keep = next((r for r in found if r["id"] == keep_id), None) or sorted(found, key=lambda r: (r["scope"] != "global", r["id"]))[0]
        before = copy.deepcopy(found)
        merged_topics = topics if topics is not None else list(dict.fromkeys(t for r in [keep, *found] for t in r["topics"]))
        keep.update({"text": text or keep["text"], "category": category or keep["category"], "topics": merged_topics, "version": keep["version"] + 1})
        self.records = [r for r in self.records if r["id"] == keep["id"] or r["id"] not in ids]
        history = self._journal(action, [{"id": r["id"], "text": r["text"], "kind": r["category"], "topics": r["topics"]} for r in before], [{"id": keep["id"], "text": keep["text"], "kind": keep["category"], "topics": keep["topics"]}])
        self.history[0]["_records"] = before
        return keep, history

    def answer(self, method: str, path: str, body: dict[str, Any] | None = None, query: str = "") -> tuple[int, Any] | None:
        """``(status, payload)`` for a memory route, ``None`` for any other path."""
        if "?" in path:
            path, query = path.split("?", 1)
        if not path.startswith("/api/memory"):
            return None
        body = body or {}
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
        parts = path.strip("/").split("/")
        entries = self._answer_entries(method, path, parts, body, params)
        if entries is not None:
            return entries
        if path == "/api/memory" and method == "GET":
            return 200, self.listing()
        if path == "/api/memory" and method == "POST":
            record = {"id": f"m{len(self.records) + 100}", "scope": body.get("scope", "global"), "scope_key": body.get("scope_key", ""), "category": body.get("category") or "fact",
                      "topics": body.get("topics") or [], "text": body.get("text", ""), "salience": 0, "version": 1, "created_at": datetime.now(UTC).isoformat(),
                      "updated_at": datetime.now(UTC).isoformat(), "last_accessed_at": None}
            self.records.insert(0, record)
            return 200, {"decision": "created", "record": record}
        if path == "/api/memory/delete" and method == "POST":
            gone = [r for r in self.records if r["id"] in body.get("ids", [])]
            self.records = [r for r in self.records if r["id"] not in body.get("ids", [])]
            return 200, {"deleted": len(gone), "history_id": self._journal_full("delete", gone, []) if gone else None}
        if path == "/api/memory/merge" and method == "POST":
            record, history = self._merge(body.get("ids", []), body.get("text"), body.get("category"), body.get("topics"), body.get("keep_id"))
            return 200, {"record": record, "history_id": history}
        if path == "/api/memory/topics/rename" and method == "POST":
            old, new = body.get("topic", ""), body.get("to", "")
            changed = 0
            for r in self.records:
                if old in r["topics"]:
                    r["topics"] = list(dict.fromkeys(new if t == old else t for t in r["topics"] if new or t != old))
                    changed += 1
            return 200, {"changed": changed}
        if path == "/api/memory/consolidate" and method == "POST":
            return 200, {"merged": 0, "suggested": 0}
        if path == "/api/memory/suggestions" and method == "GET":
            open_ = [s for s in self.suggestions if s.get("status", "open") == "open"]
            return 200, {"suggestions": [{k: v for k, v in s.items() if k != "status"} for s in open_],
                         "records": {i: r for s in open_ for i in s["memory_ids"] if (r := self._get(i)) is not None}}
        if path == "/api/memory/suggestions/decide" and method == "POST":
            # A group decided at once, as the host does: one history entry whose undo opens them again.
            chosen = [s for s in self._waiting() if s["id"] in body.get("ids", [])]
            made: list[dict[str, Any]] = []
            for suggestion in chosen:
                suggestion["status"] = "applied" if body.get("action") == "apply" else "dismissed"
                if body.get("action") == "apply" and suggestion["kind"] == "addition":
                    entry = self._entry(suggestion.get("entry_id") or "") or self._ensure("topic", _slug(suggestion.get("entry_title") or "new"), suggestion.get("entry_title") or "New")
                    made.append(self._new_record(suggestion["text"], entry, origin="synthesis"))
            history = self._decided("accept_suggestions" if body.get("action") == "apply" else "dismiss_suggestions", chosen, made) if chosen else None
            applied = len(chosen) if body.get("action") == "apply" else 0
            return 200, {"applied": applied, "dismissed": len(chosen) - applied, "stale": 0, "history_id": history}
        if len(parts) == 5 and parts[2] == "suggestions" and method == "POST":
            suggestion = next((s for s in self.suggestions if str(s["id"]) == parts[3]), None)
            if suggestion is None or suggestion.get("status", "open") != "open":
                return 409, {"detail": "this suggestion is no longer open"}
            if parts[4] == "dismiss":
                suggestion["status"] = "dismissed"
                learned = suggestion["kind"] in ("revision", "addition")
                return 200, {"dismissed": True, "history_id": self._decided("dismiss_suggestions", [suggestion], []) if learned else None}
            if suggestion["kind"] in ("revision", "addition"):
                suggestion["status"] = "applied"
                return 200, {"record": self._get(suggestion["memory_ids"][0]) if suggestion["memory_ids"] else None, "history_id": None}
            record, history = self._merge(suggestion["memory_ids"], body.get("text") or suggestion["text"], None, None, suggestion["keep_id"], "merge" if suggestion["kind"] == "duplicate" else "replace")
            suggestion["status"] = "applied"
            return 200, {"record": record, "history_id": history}
        if path == "/api/memory/history" and method == "GET":
            return 200, {"entries": [{k: v for k, v in e.items() if not k.startswith("_")} for e in self.history]}
        if len(parts) == 5 and parts[2] == "history" and parts[4] == "undo" and method == "POST":
            entry = next((e for e in self.history if str(e["id"]) == parts[3]), None)
            if entry is None:
                return 404, {"detail": "no such change"}
            if entry["undone_at"]:
                return 409, {"detail": "this change was already undone"}
            restored = entry.get("_records", [])
            ids = {r["id"] for r in restored} | set(entry.get("_drop", []))
            self.records = [r for r in self.records if r["id"] not in ids] + copy.deepcopy(restored)
            for suggestion in self.suggestions:
                if suggestion["id"] in entry.get("_reopen", []):
                    suggestion["status"] = "open"
            for revived in entry.get("_entries", []):
                if self._entry(revived["id"]) is None:
                    self.entries.append(copy.deepcopy(revived))
            entry["undone_at"] = datetime.now(UTC).isoformat()
            return 200, {"restored": restored}
        if len(parts) == 3 and method == "PATCH":
            record = self._get(parts[2])
            if record is None:
                return 404, {"detail": "no such memory"}
            if body.get("version") is not None and body["version"] != record["version"]:
                return 409, {"detail": f"the record is at version {record['version']}, not {body['version']}"}
            before = copy.deepcopy(record)
            if body.get("text") is not None:
                record["locked"] = True
            self._touch(record.get("entry"))
            for field in ("text", "category", "topics"):
                if body.get(field) is not None:
                    record[field] = body[field]
            record["version"] += 1
            history = self._journal("edit", [{"id": before["id"], "text": before["text"], "kind": before["category"], "topics": before["topics"]}], [{"id": record["id"], "text": record["text"], "kind": record["category"], "topics": record["topics"]}])
            self.history[0]["_records"] = [before]
            return 200, {**record, "history_id": history}
        if len(parts) == 3 and method == "DELETE":
            gone = [r for r in self.records if r["id"] == parts[2]]
            self.records = [r for r in self.records if r["id"] != parts[2]]
            for record in gone:
                self._touch(record.get("entry"))
            return 200, {"deleted": bool(gone), "history_id": self._journal_full("delete", gone, []) if gone else None}
        return 404, {"detail": "Not Found"}

    def _answer_entries(self, method: str, path: str, parts: list[str], body: dict[str, Any], params: dict[str, str]) -> tuple[int, Any] | None:
        """The entry routes, quick add, marks, status, export and import; ``None`` for the others."""
        if path == "/api/memory/status" and method == "GET":
            return 200, dict(self.status)
        if path in ("/api/memory/synthesis/run", "/api/memory/synthesis/reread") and method == "POST":
            # "Update now" and "read every chat again": the stub's pass finishes at once and leaves a
            # report, as a real one would.
            if self.status.get("blocked"):
                return 409, {"detail": "memory is paused"}
            if path.endswith("reread"):
                self.reread_runs += 1
            self.learn_runs += 1
            now = datetime.now(UTC).isoformat()
            self.status["last_synthesis_at"] = now
            self.status["report"] = {"at": now, "trigger": "manual", "model": "cheap.model", "chats": 2, "parts": 2, "calls": 2, "cost_usd": 0.0, "added": 3, "merged": 1,
                                     "updated": 1, "retired": 0, "new_entries": 1, "summaries": 1, "held": 1, "dropped": {"sensitive": 1}, "stopped": ""}
            return 200, {"started": True}
        if path == "/api/memory/tidy" and method == "POST":
            return 200, self._tidy(bool(body.get("dry_run", True)))
        if path == "/api/memory/entries" and method == "GET":
            return 200, self.entry_listing()
        if path == "/api/memory/entries" and method == "POST":
            slug = _slug(body.get("title", ""))
            if any(e["section"] == body.get("section") and e["slug"] == slug for e in self.entries):
                return 409, {"detail": "exists"}
            return 200, {"entry": self._view(self._ensure(body.get("section", "topic"), slug, body.get("title", ""), None))}
        if path == "/api/memory/quick" and method == "POST":
            text = (body.get("text") or "").strip()
            if not text:
                return 422, {"detail": "nothing to remember"}
            probe = {"scope": "global", "scope_key": "", "category": "preference" if text.lower().startswith(("i prefer", "i like", "я предпочитаю", "мне нравится")) else "fact", "topics": []}
            entry = self._file(probe)
            record = self._new_record(text, entry, probe["category"])
            return 200, {"decision": "created", "record": copy.deepcopy(record), "entry": self._view(entry), "history_id": self._journal_full("add", [], [record])}
        if path == "/api/memory/import/preview" and method == "POST":
            raw = (body.get("text") or "").splitlines()
            # As the host reads it: when the text has a code block, only what is inside blocks counts.
            if any(line.strip().startswith("```") for line in raw):
                inside, kept = False, []
                for line in raw:
                    if line.strip().startswith("```"):
                        inside = not inside
                    elif inside:
                        kept.append(line)
                raw = kept
            lines = [line.strip().lstrip("-•* ").strip() for line in raw]
            items = []
            for n, line in enumerate(text for text in lines if len(text) > 2):
                date = ""
                if line.startswith("[") and "]" in line:
                    date, line = line[1:line.index("]")], line[line.index("]") + 1:].lstrip(" -–:")
                injection = ["an instruction to ignore earlier instructions"] if "ignore" in line.lower() and "instruction" in line.lower() else []
                sensitive = ["health"] if any(w in line.lower() for w in ("diagnos", "asthma", "диагноз")) else []
                preference = line.lower().startswith(("prefers", "i prefer", "предпочитает"))
                items.append({"key": n, "text": line, "category": "preference" if preference else "fact", "topics": [], "section": "you" if preference else "topic",
                              "slug": "preferences" if preference else "recent-work", "title": "Preferences" if preference else "Recent work", "project_id": None, "date": date or None,
                              "sensitive": sensitive, "injection": injection, "duplicate": any(r["text"] == line for r in self.records), "forgotten": False,
                              "checked": not (injection or sensitive)})
            return 200, {"items": items, "count": len(items)}
        if path == "/api/memory/import/apply" and method == "POST":
            made = []
            refused = 0
            for item in body.get("items", []):
                if "ignore" in item.get("text", "").lower() and "instruction" in item.get("text", "").lower():
                    refused += 1
                    continue
                entry = self._ensure(item.get("section") or "topic", item.get("slug") or "recent-work", item.get("title") or "Recent work")
                made.append(self._new_record(item["text"], entry, item.get("category") or "fact", "import"))
            self.imported.extend(copy.deepcopy(body.get("items", [])))
            return 200, {"imported": len(made), "refused": refused, "history_id": self._journal_full("import", [], made) if made else None}
        if path == "/api/memory/sensitive" and method == "POST":
            return 200, {"count": 0} if not body.get("apply") else {"removed": 0, "history_id": None}
        if path == "/api/memory/reset" and method == "POST":
            if (body.get("confirm") or "").strip().lower() not in ("reset", "сбросить"):
                return 422, {"detail": "type the confirmation word to reset memory"}
            count = len(self.records)
            self.records, self.entries, self.history = [], [], []
            return 200, {"deleted": {"records": count, "entries": 0, "sources": 0, "tombstones": 0, "suggestions": 0, "history": 0}}
        if len(parts) == 4 and parts[3] == "mark" and method == "POST":
            record = self._get(parts[2])
            if record is None:
                return 404, {"detail": "no such memory"}
            before = copy.deepcopy(record)
            changed = [k for k in ("locked", "archived") if body.get(k) is not None and bool(record.get(k)) != body[k]]
            for k in changed:
                record[k] = body[k]
            if not changed:
                return 200, {**copy.deepcopy(record), "history_id": None}
            record["version"] += 1
            self._touch(record.get("entry"))
            return 200, {**copy.deepcopy(record), "history_id": self._journal_full(("" if body[changed[0]] else "un") + ("lock" if changed[0] == "locked" else "archive"), [before], [record])}
        if len(parts) < 4 or parts[2] != "entries":
            return None
        entry = self._entry(parts[3])
        if entry is None:
            return 404, {"detail": "no such entry"}
        if len(parts) == 4 and method == "GET":
            return 200, self.entry_detail(entry["id"])
        if len(parts) == 4 and method == "PATCH":
            if body.get("version") != entry["version"]:
                return 409, {"detail": f"the entry is at version {entry['version']}, not {body.get('version')}"}
            for field in ("title", "summary", "aliases"):
                if body.get(field) is not None:
                    entry[field] = body[field]
            entry["edited_by_operator"] = True
            self._touch(entry["id"])
            return 200, {"entry": self._view(entry)}
        if len(parts) == 4 and method == "DELETE":
            if str(entry["version"]) != params.get("version"):
                return 409, {"detail": f"the entry is at version {entry['version']}"}
            gone = [r for r in self.records if r.get("entry") == entry["id"]]
            self.records = [r for r in self.records if r.get("entry") != entry["id"]]
            removed = [] if entry["section"] == "you" else [copy.deepcopy(entry)]
            if removed:
                self.entries = [e for e in self.entries if e["id"] != entry["id"]]
            else:
                self._touch(entry["id"])
            return 200, {"deleted": len(gone), "history_id": self._journal_full("delete_entry", gone, [], removed)}
        if len(parts) == 5 and parts[4] == "bullets" and method == "POST":
            record = self._new_record((body.get("text") or "").strip(), entry)
            return 200, {"decision": "created", "record": copy.deepcopy(record), "history_id": self._journal_full("add", [], [record]), **self.entry_detail(entry["id"])}  # type: ignore[dict-item]
        if len(parts) == 5 and parts[4] == "instruct" and method == "POST":
            if body.get("version") != entry["version"]:
                return 409, {"detail": f"the entry is at version {entry['version']}, not {body.get('version')}"}
            records = [r for r in self.records if r.get("entry") == entry["id"]]
            if not body.get("apply"):
                scripted = self.proposals.get(body.get("instruction", ""))
                proposal = copy.deepcopy(scripted) if scripted else {"ops": [{"op": "add", "id": None, "text": body.get("instruction", "")}], "title": None, "summary": None, "note": ""}
                return 200, {"version": entry["version"], "dropped": 0, "flags": {}, "title": None, "summary": None, "note": "", **proposal}
            before: list[dict[str, Any]] = []
            after: list[dict[str, Any]] = []
            for op in body.get("ops", []):
                target = next((r for r in records if r["id"] == op.get("id")), None)
                if op.get("op") == "add":
                    after.append(self._new_record(op.get("text") or "", entry))
                elif target is None:
                    return 422, {"detail": "an operation does not fit this entry"}
                elif op["op"] == "remove":
                    before.append(copy.deepcopy(target))
                    self.records = [r for r in self.records if r["id"] != target["id"]]
                else:
                    before.append(copy.deepcopy(target))
                    target.update({"text": op.get("text") or target["text"], "version": target["version"] + 1, "locked": True})
                    after.append(target)
            if body.get("summary") is not None:
                entry["summary"] = body["summary"]
            if body.get("title") is not None:
                entry["title"] = body["title"]
            self._touch(entry["id"])
            return 200, {"history_id": self._journal_full("instruct", before, after), **self.entry_detail(entry["id"])}  # type: ignore[dict-item]
        return None


__all__ = ["CATEGORIES", "MemoryStub"]
