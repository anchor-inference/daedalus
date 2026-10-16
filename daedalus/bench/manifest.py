"""The task manifest: what to run, how to set it up, how to judge it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Task:
    id: str
    prompt: str
    setup: str = ""
    """Shell command run in the task workspace before the agent starts (clone, unpack, seed)."""
    check: str = ""
    """Shell command run in the workspace after the run; exit 0 means the task passed. Empty: no verdict."""
    files: dict[str, str] = field(default_factory=dict)
    """Files written into the workspace before the run (path -> content)."""
    timeout_minutes: int = 30
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Manifest:
    name: str
    tasks: list[Task]
    tools_off: list[str] = field(default_factory=list)
    """Tools the benchmark sessions do not get (SendFile, SpawnAgent … whatever has no place in a container)."""
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> Manifest:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        tasks = [Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__}) for t in raw.get("tasks", [])]
        ids = [t.id for t in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("task ids must be unique")
        return cls(name=str(raw.get("name") or path.stem), tasks=tasks, tools_off=list(raw.get("tools_off", [])), notes=str(raw.get("notes", "")))


DEFAULT_TOOLS_OFF = ["SendFile", "AttachMedia", "ImageView", "Remember", "Recall", "SpawnAgent", "ScheduleCreate", "ScheduleDelete", "ScheduleList", "SelfWorkspace", "SelfPropose", "SelfApply", "SelfRebuild", "SelfRollback", "AskUser", "SideQuestion", "DropSideQuestion", "AskPeer", "PeerList", "BoardAdd", "BoardUpdate", "BoardList", "BoardGet", "IntentCreate", "IntentList", "IntentDelete", "LoopNext", "LoopStop", "LoopPause", "LoopResume", "LoopStatus", "ServiceStart", "ServiceStop", "ServiceList", "ServiceLogs", "TerminalRead", "McpList", "McpEnable", "McpDisable", "McpAdd", "McpRemove", "McpOAuthStatus", "McpOAuthBegin", "McpOAuthFinish", "McpOAuthDisconnect", "StaySilent", "LearningReport"]
"""What a benchmark session never needs: every self-development tool (on a local installation ``SelfApply`` would change the very install being benchmarked), everything that talks to the operator, the board, or the host's own lifecycle, the vision model (a benchmark config has none), and the long-term memory (a fact carried from one task to the next is contamination)."""

__all__ = ["DEFAULT_TOOLS_OFF", "Manifest", "Task"]
