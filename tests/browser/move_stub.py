"""The routes of a folder move between the container and the host, over invented folders.

``plans`` maps a folder id to what ``GET …/move`` answers; a folder it does not name answers as a
folder that cannot move. A started move is followed by the app once a second; each read moves the
invented job one step on (copying a third at a time, then checking, then done), so a check sees the
bar grow and the move end without waiting on a clock. ``held`` keeps every move a third of the way
through, for a check that reloads the page in the middle of one; the list of running moves, which
every folder row and the inbox read, moves the jobs on as the dialog's reads do. ``started`` and
``cancelled`` are what the app sent, for a check to read back.
"""
from __future__ import annotations

import copy
import json
from typing import Any

HOST_HOME = "/home/operator"
TOTAL = 12 * 1024 * 1024


def plan(folder_id: str, direction: str, source: str, destination: str, *, problem: str = "", detail: str = "",
         skipped: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"project_id": "p1", "folder_id": folder_id, "direction": direction, "source": source,
            "env": "container" if direction == "to_host" else "host", "destination": destination,
            "destination_editable": direction == "to_host", "host_home": HOST_HOME if direction == "to_host" else "",
            "problem": problem, "detail": detail, "blockers": [], "job": None, "leftover": None,
            "counts": None if problem else {"files": 42, "dirs": 7, "symlinks": 1, "bytes": TOTAL},
            "skipped": list(skipped or []), "skipped_count": len(skipped or [])}


class MoveStub:
    def __init__(self, plans: dict[str, dict[str, Any]] | None = None) -> None:
        self.plans = plans or {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.held = False

    def _plan(self, folder_id: str) -> dict[str, Any]:
        found = self.plans.get(folder_id)
        if found is not None:
            return copy.deepcopy(found)
        return plan(folder_id, "", "", "", problem="not_movable", detail="this folder is mounted into the container from the host")

    def _step(self, job: dict[str, Any]) -> None:
        if not job["active"]:
            return
        if self.held:
            job["state"], job["done_bytes"] = "copying", TOTAL // 3
            return
        if job["state"] in ("preparing", "copying"):
            job["state"] = "copying"
            job["done_bytes"] = min(TOTAL, job["done_bytes"] + TOTAL // 3)
            if job["done_bytes"] >= TOTAL:
                job["state"] = "verifying"
        elif job["state"] == "verifying":
            job["state"], job["active"] = "done", False
            job["original_removed"] = job["direction"] == "to_host" or job["remove_original"]
            job["finished_at"] = 1_760_000_100.0

    def answer(self, method: str, path: str, body: Any, query: str = "") -> tuple[int, Any] | None:
        parts = path.split("/")
        if len(parts) == 7 and parts[2] == "projects" and parts[4] == "folders" and parts[6] == "move":
            folder_id = parts[5]
            if method == "GET":
                found = self._plan(folder_id)
                found["job"] = next((copy.deepcopy(job) for job in self.jobs.values() if job["folder_id"] == folder_id and job["active"]), None)
                return 200, found
            if method == "POST":
                found = self._plan(folder_id)
                if found["problem"]:
                    return 409, {"detail": found["detail"]}
                sent = body if isinstance(body, dict) else {}
                self.started.append((folder_id, sent))
                job_id = f"m{len(self.jobs) + 1}"
                job = {"id": job_id, "project_id": parts[3], "folder_id": folder_id, "direction": found["direction"], "source": found["source"],
                       "destination": sent.get("destination") or found["destination"], "remove_original": bool(sent.get("remove_original")),
                       "state": "preparing", "active": True, "total_bytes": TOTAL, "done_bytes": 0, "total_files": 42, "error": "",
                       "notes": [], "skipped": [], "skipped_count": 0, "original_removed": False, "started_at": 1_760_000_000.0, "finished_at": None,
                       "leftover": ""}
                self.jobs[job_id] = job
                return 200, copy.deepcopy(job)
        if path == "/api/folder-moves" and method == "GET":
            for job in self.jobs.values():
                self._step(job)
            active = "active=true" in query
            return 200, {"jobs": [copy.deepcopy(job) for job in self.jobs.values() if job["active"] or not active]}
        if len(parts) >= 4 and parts[2] == "folder-moves":
            job = self.jobs.get(parts[3])
            if job is None:
                return 404, {"detail": "no such move"}
            if len(parts) == 5 and parts[4] == "cancel" and method == "POST":
                if job["state"] not in ("preparing", "copying", "verifying"):
                    return 409, {"detail": "the move is past its copy and is being finished; it cannot be cancelled now"}
                self.cancelled.append(job["id"])
                job["state"], job["active"], job["error"] = "cancelled", False, "the move was cancelled; the folder is where it was"
                return 200, copy.deepcopy(job)
            if len(parts) == 4 and method == "GET":
                self._step(job)
                return 200, copy.deepcopy(job)
        return None

    def fulfil(self, route) -> bool:  # type: ignore[no-untyped-def]
        request = route.request
        path, _, query = request.url.partition("?")
        path = path[path.index("/api/"):] if "/api/" in path else ""
        body = request.post_data_json if request.method == "POST" and request.post_data else None
        answered = self.answer(request.method, path, body, query)
        if answered is None:
            return False
        route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
        return True


DEFAULT = MoveStub()
"""The shared host's moves: every folder answers that it cannot move, so a screen that opens the dialog
by accident draws the refusal rather than a blank page."""
