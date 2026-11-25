"""The agent's own change proposals, as ``/api/proposals`` answers them, kept in memory for the browser
harnesses.

Four proposals: two waiting (one with a few files of every kind, one with a single file of two thousand
lines and no summary of its own), one merged and one rejected. A decision answers the way the host
does, with the words of what it did, and moves the row; ``decided`` is every decision as
``(proposal id, body)`` for a check to read back. ``empty`` answers an empty list, and ``cut`` makes the
first proposal's diff end where the host's 400,000-character cap would end it.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

NOW = datetime.now(UTC)
CHAT = "a1b2c3d4e5f6"


def _ago(**span: float) -> str:
    return (NOW - timedelta(**span)).isoformat()


def _hunk(old_start: int, new_start: int, lines: list[str], tail: str = "") -> str:
    old = sum(1 for line in lines if line[:1] in " -")
    new = sum(1 for line in lines if line[:1] in " +")
    return f"@@ -{old_start},{old} +{new_start},{new} @@{tail}\n" + "\n".join(lines) + "\n"


SEARCH = (
    "diff --git a/daedalus/search.py b/daedalus/search.py\n"
    "index 3f2a9c1..8d41b07 100644\n"
    "--- a/daedalus/search.py\n"
    "+++ b/daedalus/search.py\n"
    + _hunk(38, 38, [
        " async def search(query: str) -> list[Hit]:",
        '     """Ask each backend in turn and return the first answer that holds hits."""',
        "     for backend in BACKENDS:",
        "         try:",
        "-            return await backend.search(query)",
        "+            return await first_try_or_again(backend, query)",
        "         except BackendError:",
        "             continue",
        "     return []",
    ], " async def search")
    + _hunk(71, 71, [
        " class Backend:",
        "     name: str",
        "     timeout: float = 4.0",
        " ",
        "     async def search(self, query: str) -> list[Hit]: ...",
        " ",
        "     def describe(self) -> str:",
        "         return self.name",
        " ",
        "     def __repr__(self) -> str:",
        "         return f\"<{self.name}>\"",
        " ",
        "+    retry_after: float = 1.0",
        "+",
        " ",
        " BACKENDS: list[Backend] = []",
    ], " class Backend")
    + _hunk(120, 122, [
        " def registered() -> list[str]:",
        "-    return [b.name for b in BACKENDS]",
        "+    return sorted(b.name for b in BACKENDS)",
    ])
    + _hunk(131, 133, [
        "+async def first_try_or_again(backend: Backend, query: str) -> list[Hit]:",
        '+    """A backend that answers 504 once is tried again after a second; a second failure falls through."""',
        "+    try:",
        "+        return await backend.search(query)",
        "+    except TimeoutError:",
        "+        await asyncio.sleep(backend.retry_after)",
        "+        return await backend.search(query)",
    ])
)

NEW_TEST = (
    "diff --git a/tests/unit/test_search_retry.py b/tests/unit/test_search_retry.py\n"
    "new file mode 100644\n"
    "index 0000000..5b8e1d4\n"
    "--- /dev/null\n"
    "+++ b/tests/unit/test_search_retry.py\n"
    + _hunk(0, 1, [
        "+import pytest",
        "+",
        "+from daedalus.search import first_try_or_again",
        "+",
        "+",
        "+@pytest.mark.asyncio",
        "+async def test_a_backend_that_times_out_once_is_tried_again(fake_backend):",
        "+    backend = fake_backend(failures=[TimeoutError()], then=[\"hit\"])",
        "+    assert await first_try_or_again(backend, \"menu\") == [\"hit\"]",
        "+    assert backend.calls == 2",
        "+",
        "+",
        "+@pytest.mark.asyncio",
        "+async def test_two_timeouts_fall_through(fake_backend):",
        "+    backend = fake_backend(failures=[TimeoutError(), TimeoutError()])",
        "+    with pytest.raises(TimeoutError):",
        "+        await first_try_or_again(backend, \"menu\")",
    ])
)

DOC = (
    "diff --git a/docs/WEBSEARCH.md b/docs/WEBSEARCH.md\n"
    "index 91c03aa..1d2f7e0 100644\n"
    "--- a/docs/WEBSEARCH.md\n"
    "+++ b/docs/WEBSEARCH.md\n"
    + _hunk(14, 14, [
        " ## Falling back",
        " ",
        "-A backend that fails is skipped and the next one is asked.",
        "+A backend that times out is asked once more after a second. Any other failure, and a second",
        "+timeout, skip it and ask the next one.",
        " ",
        " Backends are tried in the order of `[search].backends`.",
    ])
)

BINARY = "diff --git a/docs/img/fallback.png b/docs/img/fallback.png\nindex 4c1b7a2..e90d3f6 100644\nBinary files a/docs/img/fallback.png and b/docs/img/fallback.png differ\n"

MOVED = "diff --git a/config/search.sample.toml b/config/search.example.toml\nsimilarity index 100%\nrename from config/search.sample.toml\nrename to config/search.example.toml\n"

REMOVED = (
    "diff --git a/scripts/old_probe.sh b/scripts/old_probe.sh\n"
    "deleted file mode 100755\n"
    "index 7a0c5e1..0000000\n"
    "--- a/scripts/old_probe.sh\n"
    "+++ /dev/null\n"
    + _hunk(1, 0, ["-#!/bin/sh", "-curl -s \"$1\" | head -c 200"])
)

WEBSEARCH_DIFF = SEARCH + NEW_TEST + DOC + BINARY + MOVED + REMOVED

MERGED_DIFF = (
    "diff --git a/daedalus/memory/recall.py b/daedalus/memory/recall.py\n"
    "--- a/daedalus/memory/recall.py\n"
    "+++ b/daedalus/memory/recall.py\n"
    + _hunk(52, 52, [
        " def recall(query: str, limit: int = 8) -> list[Entry]:",
        "-    return store.search(query)[:limit]",
        "+    hits = store.search(query)",
        "+    return sorted(hits, key=lambda e: e.salience, reverse=True)[:limit]",
        " ",
        " def forget(entry_id: str) -> None:",
    ], " def recall")
)

REJECTED_DIFF = (
    "diff --git a/daedalus/host/session_runner.py b/daedalus/host/session_runner.py\n"
    "--- a/daedalus/host/session_runner.py\n"
    "+++ b/daedalus/host/session_runner.py\n"
    + _hunk(210, 210, [
        "     async def stop(self, state) -> None:",
        "-        await self._cancel(state)",
        "+        await self._cancel(state)",
        "+        await self._cancel_everything_else(state)",
    ])
)


def long_diff(rows: int = 2000) -> str:
    """One file whose diff is about ``rows`` lines: a price table regenerated, forty hunks of fifty lines."""
    text = "diff --git a/daedalus/providers/prices.py b/daedalus/providers/prices.py\nindex 0a1b2c3..4d5e6f7 100644\n--- a/daedalus/providers/prices.py\n+++ b/daedalus/providers/prices.py\n"
    per_hunk = 50
    old = new = 1
    for h in range(max(1, rows // per_hunk)):
        lines: list[str] = []
        for k in range(3):
            lines.append(f"     \"model-{h:02d}-{k}\": ({k + 1}.00, {k + 4}.00),")
        for k in range(10):
            lines.append(f"-    \"model-{h:02d}-old-{k}\": ({k}.50, {k + 2}.50),  # price per million tokens, input and output")
        for k in range(14):
            lines.append(f"+    \"model-{h:02d}-new-{k}\": ({k}.25, {k + 2}.75),  # price per million tokens, input and output")
        for k in range(23):
            lines.append(f"     \"model-{h:02d}-keep-{k}\": ({k}.10, {k + 1}.10),")
        text += _hunk(old, new, lines[:per_hunk] if len(lines) > per_hunk else lines)
        old += 36 + 12
        new += 40 + 12
    return text


ROWS: list[dict[str, Any]] = [
    {"id": "p57", "repo": "daedalus", "branch": "bot/websearch-retry", "pr_number": 57, "pr_url": "https://github.com/example/daedalus/pull/57",
     "title": "WebSearch: retry a backend that timed out once before falling back",
     "summary": "A backend that answers 504 once is tried again after a second; only a second failure falls through to the next backend.\nUnit test added, and the fallback paragraph of the docs says so.",
     "status": "pending", "reason": None, "created_at": _ago(minutes=20), "session_id": CHAT, "decided_at": None},
    {"id": "p58", "repo": "daedalus", "branch": "bot/price-table", "pr_number": 58, "pr_url": "https://github.com/example/daedalus/pull/58",
     "title": "Regenerate the model price table from the vendors' pages",
     "summary": "", "status": "pending", "reason": None, "created_at": _ago(hours=3), "session_id": None, "decided_at": None},
    {"id": "p56", "repo": "daedalus", "branch": "bot/memory-recall-order", "pr_number": 56, "pr_url": "https://github.com/example/daedalus/pull/56",
     "title": "Memory: recall the most salient entries first",
     "summary": "Entries come back ordered by salience instead of by when they were stored, so the limit keeps the ones that matter.",
     "status": "merged", "reason": "Reads right; the test covers the order.", "created_at": _ago(days=1, hours=2), "session_id": CHAT, "decided_at": _ago(days=1)},
    {"id": "p55", "repo": "daedalus", "branch": "bot/stop-everything", "pr_number": 55, "pr_url": "https://github.com/example/daedalus/pull/55",
     "title": "Stopping a chat also stops everything it started",
     "summary": "A stop now cancels the helpers and the terminals the chat started, not only its own run.",
     "status": "rejected", "reason": "Too broad: a terminal the operator opened by hand must outlive the chat.", "created_at": _ago(days=2), "session_id": CHAT, "decided_at": _ago(days=2, hours=-1)},
]

DIFFS = {"p57": WEBSEARCH_DIFF, "p58": long_diff(), "p56": MERGED_DIFF, "p55": REJECTED_DIFF}


class ProposalsStub:
    def __init__(self) -> None:
        self.rows = copy.deepcopy(ROWS)
        self.diffs = dict(DIFFS)
        self.empty = False
        self.cut = False
        self.decided: list[tuple[str, dict[str, Any]]] = []
        self.result = ""

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def answer(self, method: str, path: str, body: Any) -> tuple[int, str, str] | None:
        parts = path.split("?", 1)[0].split("/")
        method = method.upper()
        js = lambda status, payload: (status, "application/json", json.dumps(payload))  # noqa: E731
        if parts[:3] != ["", "api", "proposals"]:
            return None
        if len(parts) == 3 and method == "GET":
            return js(200, [] if self.empty else self.rows)
        row = next((r for r in self.rows if len(parts) > 3 and r["id"] == parts[3]), None)
        if row is None:
            return js(404, {"detail": "no such proposal"})
        if parts[4:] == ["diff"] and method == "GET":
            text = self.diffs.get(row["id"], "")
            if self.cut and row["id"] == "p57":
                text = (text + ("+" + "x" * 999 + "\n") * 500)[:400_000]
            return js(200, {"id": row["id"], "diff": text})
        if parts[4:] == ["decide"] and method == "POST":
            sent = body if isinstance(body, dict) else {}
            self.decided.append((row["id"], sent))
            if row["status"] != "pending":
                return js(200, {"result": f"already {row['status']}"})
            approve = sent.get("decision") == "approve"
            row["status"] = "merged" if approve else "rejected"
            row["reason"] = sent.get("reason") or None
            row["decided_at"] = _ago(seconds=1)
            number = row["pr_number"]
            return js(200, {"result": self.result or (f"merged PR #{number}" if approve else f"rejected PR #{number}")})
        return None


DEFAULT = ProposalsStub()
