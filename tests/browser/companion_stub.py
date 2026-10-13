"""The companion's routes, as one in-memory host every harness shares.

Its goals are kept, so a check that adds one, pauses it and deletes it reads its own writes back; the
rest is a fixed picture: a profile, an activity log, two schedules, its tools. A harness that needs
another picture builds a ``CompanionStub`` of its own and answers ``/api/companion*`` with it.
"""
from __future__ import annotations

import copy
from typing import Any

PROFILE = {
    "session_id": "",
    "name": "Daedalus",
    "avatar": {"shape": "orb", "color": "#7c6cf0", "eyes": "dots", "accessory": "antenna"},
    "paused": False,
    "state": "working",
    "running_agents": 1,
    "enabled": True,
}

GOALS = [
    {
        "id": "g1", "title": "Keep the bakery prices current", "brief": "Compare with the supplier list every morning; change nothing without asking.",
        "status": "active", "cadence": "every 1d", "next_check_at": "2026-10-14T07:00:00+00:00", "last_check_at": "2026-10-13T07:00:00+00:00",
        "last_report": "Flour went up by four percent; the price list still has last month's figure.", "last_report_at": "2026-10-13T07:02:00+00:00",
        "sessions": ["s-prices"], "created_at": "2026-10-10T09:00:00+00:00", "updated_at": "2026-10-13T07:02:00+00:00",
    },
    {
        "id": "g2", "title": "Find a flat under 80k", "brief": "Two rooms, near the river.", "status": "paused", "cadence": "",
        "next_check_at": None, "last_check_at": None, "last_report": "", "last_report_at": None, "sessions": [],
        "created_at": "2026-10-11T09:00:00+00:00", "updated_at": "2026-10-11T09:00:00+00:00",
    },
]

ACTIVITY = [
    {"id": 4, "at": "2026-10-13T07:02:00+00:00", "kind": "report", "title": "Keep the bakery prices current", "body": "Flour went up by four percent.", "session_id": None, "goal_id": "g1"},
    {"id": 3, "at": "2026-10-13T07:00:30+00:00", "kind": "delegated", "title": "Supplier prices", "body": "Read the supplier list and compare.", "session_id": "s-prices", "goal_id": "g1"},
    {"id": 2, "at": "2026-10-13T07:00:00+00:00", "kind": "check", "title": "Keep the bakery prices current", "body": "", "session_id": None, "goal_id": "g1"},
    {"id": 1, "at": "2026-10-12T18:00:00+00:00", "kind": "finished", "title": "Invoice run", "body": "All eleven invoices went out.", "session_id": "s-invoices", "goal_id": None},
]

TOOLS = {
    "companion": [
        {"name": name, "description": text}
        for name, text in (
            ("Delegate", "Hand a task to an agent and get its session id back at once"),
            ("Agents", "The agents you started, newest first"),
            ("GoalCreate", "Take on a goal: something you own over days rather than one task"),
            ("GoalReport", "Record what you found about a goal"),
            ("Remember", "Keep a note across conversations"),
            ("ScheduleCreate", "Propose a scheduled task for operator approval"),
            ("Notify", "Tell the operator something outside this chat"),
            ("CalendarEvents", "List calendar occurrences in a time range"),
        )
    ],
    "agents": {"tools": 160, "mcp": ["github"]},
}


class CompanionStub:
    def __init__(self, session_id: str = "") -> None:
        self.profile = dict(PROFILE, session_id=session_id)
        self.goals = copy.deepcopy(GOALS)
        self.messages: list[dict[str, Any]] = []
        self.calls: list[bool] = []

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, Any] | None:
        method = method.upper()
        body = body or {}
        if path == "/api/companion":
            if method == "PUT":
                if "name" in body:
                    self.profile["name"] = str(body["name"]).strip() or self.profile["name"]
                if isinstance(body.get("avatar"), dict):
                    self.profile["avatar"] = {**self.profile["avatar"], **body["avatar"]}
            return 200, self.profile
        if path == "/api/companion/pause" and method == "POST":
            self.profile["paused"] = bool(body.get("paused"))
            return 200, self.profile
        if path == "/api/companion/call" and method == "POST":
            self.calls.append(bool(body.get("on")))
            return 200, {"call": bool(body.get("on"))}
        if path == "/api/companion/message" and method == "POST":
            self.messages.append(body)
            return 200, {"ok": True, "session_id": self.profile["session_id"]}
        if path == "/api/companion/voice":
            return 200, {"enabled": False}
        if path == "/api/companion/goals":
            if method == "POST":
                goal = {
                    "id": f"g{len(self.goals) + 10}", "title": str(body.get("title") or ""), "brief": str(body.get("brief") or ""),
                    "status": "active", "cadence": str(body.get("cadence") or ""), "next_check_at": None, "last_check_at": None,
                    "last_report": "", "last_report_at": None, "sessions": [], "created_at": "2026-10-13T10:00:00+00:00", "updated_at": "2026-10-13T10:00:00+00:00",
                }
                self.goals.insert(0, goal)
                return 200, goal
            closed = "closed=true" in query
            return 200, {"goals": [g for g in self.goals if closed or g["status"] in ("active", "paused")]}
        if path.startswith("/api/companion/goals/"):
            goal_id = path.rsplit("/", 1)[1]
            goal = next((g for g in self.goals if g["id"] == goal_id), None)
            if goal is None:
                return 404, {"detail": "no such goal"}
            if method == "PATCH":
                goal.update({k: v for k, v in body.items() if v is not None})
                return 200, goal
            if method == "DELETE":
                self.goals.remove(goal)
                return 200, {"deleted": True}
        if path == "/api/companion/activity":
            return 200, {"items": ACTIVITY}
        if path == "/api/companion/schedules":
            return 200, {
                "schedules": [{"id": "sc1", "name": "Morning digest", "kind": "self", "cron": "0 7 * * *", "run_at": None, "next_run_at": "2026-10-14T07:00:00+00:00", "enabled": 1}],
                "proposals": [{"id": "p1", "name": "Water the plants", "kind": "message", "cron": None, "run_at": "2026-10-14T18:00:00+00:00", "status": "pending"}],
                "goals": [g for g in self.goals if g["cadence"]],
            }
        if path == "/api/companion/computer":
            return 200, {"available": True, "reason": "", "groups": []}
        if path == "/api/companion/tools":
            return 200, TOOLS
        return None


__all__ = ["CompanionStub"]
