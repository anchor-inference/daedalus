"""Cursor ACP launches and structured approvals without contacting Cursor's service."""

from __future__ import annotations

import json
from typing import Any

from daedalus.harness import ADAPTERS
from daedalus.harness.contract import LaunchSpec
from daedalus.harness.cursor import CONFIG, CursorAdapter
from daedalus.harness.cursor_bridge import Bridge


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "cursor", "env": "host", "cwd": "/work/bakery", "launch_id": "l1",
        "first_prompt": "Work on the menu", "team_block": "You are Ada.", "team_skill": "# Team tools",
    }
    values.update(changes)
    return LaunchSpec(**values)


def test_cursor_launch_is_scoped_and_resume_reuses_the_native_chat() -> None:
    assert ADAPTERS["cursor"] is CursorAdapter
    adapter = CursorAdapter()
    fresh = adapter.launch_plan(spec(permission_mode="ask"))
    config = json.loads(fresh.files[CONFIG])
    policy = json.loads(fresh.files["cursor-config/cli-config.json"])
    assert config["cwd"] == "/work/bakery" and config["mode"] == "ask"
    assert policy["permissions"]["deny"] == ["Write(**)", "Shell(*)"]
    assert fresh.first_prompt and "You are Ada." in fresh.first_prompt
    old = adapter.resume_plan(spec(permission_mode="agent"), "old-cursor-chat")
    assert json.loads(old.files[CONFIG])["resume"] == "old-cursor-chat"
    assert json.loads(old.files["cursor-config/cli-config.json"])["permissions"]["deny"] == []


async def test_cursor_questions_map_labels_to_option_ids_and_plan_approval() -> None:
    bridge = Bridge({"launch_dir": "/work/bakery"}, "cursor.sock")
    replies: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    async def write(message: dict[str, Any]) -> None:
        replies.append(message)

    async def emit(kind: str, payload: dict[str, Any] | None = None, native_id: str = "") -> None:
        events.append((kind, payload or {}))

    bridge.write_acp = write  # type: ignore[method-assign]
    bridge.emit = emit  # type: ignore[method-assign]
    question = {"jsonrpc": "2.0", "id": 3, "method": "cursor/ask_question", "params": {"questions": [{"id": "q1", "prompt": "Which mode?", "options": [{"id": "agent", "label": "Agent"}, {"id": "plan", "label": "Plan"}]}]}}
    await bridge.request(question)
    assert events[0][1]["options"] == ["Agent", "Plan"]
    assert await bridge.answer_request("cursor-3", "answer", "Plan")
    assert replies[-1]["result"]["outcome"]["answers"] == [{"questionId": "q1", "selectedOptionIds": ["plan"]}]
    await bridge.request({"jsonrpc": "2.0", "id": 4, "method": "cursor/create_plan", "params": {"overview": "Change the menu"}})
    assert events[-1][0] == "permission_requested"
    assert await bridge.answer_request("cursor-4", "deny", "")
    assert replies[-1]["result"]["outcome"] == {"outcome": "rejected"}
