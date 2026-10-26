"""The Memory screen's API, answered the way the host answers it and kept between requests.

Records carry a category and topics; a merge keeps the broadest scope's record with the union of the
topics, writes a history entry with the records as they were, and an undo puts them back; a suggestion
applied merges its records the same way. ``writes`` lists every write a check may want to read back.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

CATEGORIES = ("person", "project", "preference", "fact", "procedure", "other")


class MemoryStub:
    def __init__(self, records: list[dict[str, Any]] | None = None, projects: dict[str, str] | None = None, suggestions: list[dict[str, Any]] | None = None) -> None:
        self.records: list[dict[str, Any]] = copy.deepcopy(records or [])
        self.projects = dict(projects or {})
        self.suggestions: list[dict[str, Any]] = copy.deepcopy(suggestions or [])
        self.history: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, Any]] = []

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

    def answer(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any] | None:
        """``(status, payload)`` for a memory route, ``None`` for any other path."""
        if not path.startswith("/api/memory"):
            return None
        body = body or {}
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
        parts = path.strip("/").split("/")
        if path == "/api/memory" and method == "GET":
            return 200, self.listing()
        if path == "/api/memory" and method == "POST":
            record = {"id": f"m{len(self.records) + 100}", "scope": body.get("scope", "global"), "scope_key": body.get("scope_key", ""), "category": body.get("category") or "fact",
                      "topics": body.get("topics") or [], "text": body.get("text", ""), "salience": 0, "version": 1, "created_at": datetime.now(UTC).isoformat(),
                      "updated_at": datetime.now(UTC).isoformat(), "last_accessed_at": None}
            self.records.insert(0, record)
            return 200, {"decision": "created", "record": record}
        if path == "/api/memory/delete" and method == "POST":
            before = len(self.records)
            self.records = [r for r in self.records if r["id"] not in body.get("ids", [])]
            return 200, {"deleted": before - len(self.records)}
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
        if len(parts) == 5 and parts[2] == "suggestions" and method == "POST":
            suggestion = next((s for s in self.suggestions if str(s["id"]) == parts[3]), None)
            if suggestion is None or suggestion.get("status", "open") != "open":
                return 409, {"detail": "this suggestion is no longer open"}
            if parts[4] == "dismiss":
                suggestion["status"] = "dismissed"
                return 200, {"dismissed": True}
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
            ids = {r["id"] for r in restored}
            self.records = [r for r in self.records if r["id"] not in ids] + copy.deepcopy(restored)
            entry["undone_at"] = datetime.now(UTC).isoformat()
            return 200, {"restored": restored}
        if len(parts) == 3 and method == "PATCH":
            record = self._get(parts[2])
            if record is None:
                return 404, {"detail": "no such memory"}
            before = copy.deepcopy(record)
            for field in ("text", "category", "topics"):
                if body.get(field) is not None:
                    record[field] = body[field]
            record["version"] += 1
            history = self._journal("edit", [{"id": before["id"], "text": before["text"], "kind": before["category"], "topics": before["topics"]}], [{"id": record["id"], "text": record["text"], "kind": record["category"], "topics": record["topics"]}])
            self.history[0]["_records"] = [before]
            return 200, {**record, "history_id": history}
        if len(parts) == 3 and method == "DELETE":
            before = len(self.records)
            self.records = [r for r in self.records if r["id"] != parts[2]]
            return 200, {"deleted": before != len(self.records)}
        return 404, {"detail": "Not Found"}


__all__ = ["CATEGORIES", "MemoryStub"]
