"""The host's tool groups as the operator sees them: how each reaches a run, what it costs, who uses it.

A group's load mode comes from four places, the later winning: the default its declaration carries
(:data:`daedalus.tools.TOOL_GROUPS`), the operator's ``[tools.groups.<name>] load``, the model the run is
on (a model with on-demand groups off turns every ``lazy`` into ``auto``;
:func:`daedalus.config.on_demand_tool_groups_for`), and a session's own choice in its metadata
(``tool_group_loads``). The model comes after the settings because the settings speak for every model
and the switch for one; the session comes last because it is the operator's choice for this chat, made
knowing which model it runs. The resolved mapping is handed to the core per run as ``tool_group_loads``;
the core alone decides from it what the first request advertises.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from protocore.contracts.runtime_constants import LoopConstants
from protocore.contracts.tools import Tool
from protocore.runtime.token_counting import estimate_tokens

from daedalus.config import RuntimeConfig
from daedalus.tools import GROUP_LOADS, TOOL_GROUPS

SESSION_LOADS_KEY = "tool_group_loads"
"""Session metadata: ``{group: load}`` the operator chose for this session alone."""

LOAD_NOW_KEY = "load_tool_groups"
"""Session metadata: groups the operator asked to have loaded, handed to the next run and then dropped."""

CARRIED_KEY = "loaded_tool_groups"
"""Session metadata: groups loaded whole in this session, handed to every later run whole.

Beside it, ``discovered_tools`` lists every tool loaded in the session in the order it was loaded, the
groups' tools among them, which is the order the next run puts them back in."""

SURFACE_KEY = "tool_group_surface"
"""Session metadata: ``{group: state}`` as the last run's last advertisement put it — the core's word on
an ``auto`` group, which nothing else can say and a restart must not forget."""

USAGE_DAYS = 30
USAGE_TTL_SECONDS = 60.0
"""The statistics move by a few rows an hour; a settings page opened twice in a minute reads them once."""

_RC = LoopConstants()


def configured_loads(config: RuntimeConfig) -> dict[str, str]:
    """Every host group's load mode under the installation's settings, before any session's choice."""
    loads = {name: spec.load for name, spec in TOOL_GROUPS.items()}
    loads.update({name: chosen.load for name, chosen in config.tools.groups.items() if name in TOOL_GROUPS})
    return loads


def session_loads(metadata: Mapping[str, Any]) -> dict[str, str]:
    """The session's own choices, what survives of them: an unknown group or mode is ignored, not fatal."""
    raw = metadata.get(SESSION_LOADS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): str(load) for name, load in raw.items() if str(name) in TOOL_GROUPS and load in GROUP_LOADS}


def resolved_loads(config: RuntimeConfig, metadata: Mapping[str, Any], *, on_demand: bool = True) -> dict[str, str]:
    """What a run of this session is given as ``tool_group_loads``: settings, the model, the session's own.

    ``on_demand`` is the model's switch; off, a group on demand is advertised while it fits instead.
    """
    loads = configured_loads(config)
    if not on_demand:
        loads = {name: "auto" if load == "lazy" else load for name, load in loads.items()}
    loads.update(session_loads(metadata))
    return loads


def load_source(name: str, config: RuntimeConfig, metadata: Mapping[str, Any], *, on_demand: bool = True) -> str:
    """Which layer decided a group's mode: ``session``, ``model``, ``settings`` or ``default``."""
    if name in session_loads(metadata):
        return "session"
    if not on_demand and configured_loads(config).get(name) == "lazy":
        return "model"
    if name in config.tools.groups:
        return "settings"
    return "default"


def members(tools: Iterable[Tool]) -> dict[str, list[str]]:
    """Each host group's registered tools, by name. A group whose tools this installation does not
    register (the browser without its container, self-development switched off) has none."""
    out: dict[str, list[str]] = {name: [] for name in TOOL_GROUPS}
    for tool in tools:
        group = getattr(tool, "tool_group", None)
        if group in out:
            out[group].append(tool.name)
    return {name: sorted(names) for name, names in out.items()}


def schema_tokens(tools: Iterable[Tool]) -> int:
    """What the definitions cost on every request that advertises them, by the core's own estimate."""
    return sum(estimate_tokens(tool.definition.model_dump_json(), _RC) for tool in tools)


@dataclass
class GroupUsage:
    sessions: int = 0
    runs: int = 0
    calls: int = 0


@dataclass
class Usage:
    days: int
    sessions: int
    runs: int
    groups: dict[str, GroupUsage] = field(default_factory=dict)


def tally(rows: Iterable[Mapping[str, Any]], group_of: Mapping[str, str], *, days: int) -> Usage:
    """Fold ``(session_id, run_id, name, n)`` rows into per-group counts.

    Sessions and runs are counted once per group, not once per tool: a session that called six browser
    tools used the browser once. The totals are the sessions and runs that called any tool at all,
    which is the denominator the operator's question — how often is this group needed — has.
    """
    usage = Usage(days=days, sessions=0, runs=0, groups={name: GroupUsage() for name in TOOL_GROUPS})
    sessions: set[str] = set()
    runs: set[str] = set()
    seen_sessions: dict[str, set[str]] = {name: set() for name in TOOL_GROUPS}
    seen_runs: dict[str, set[str]] = {name: set() for name in TOOL_GROUPS}
    for row in rows:
        session_id, run_id, count = str(row["session_id"]), str(row["run_id"] or ""), int(row["n"])
        sessions.add(session_id)
        if run_id:
            runs.add(run_id)
        group = group_of.get(str(row["name"]))
        if group is None:
            continue
        seen_sessions[group].add(session_id)
        if run_id:
            seen_runs[group].add(run_id)
        usage.groups[group].calls += count
    usage.sessions, usage.runs = len(sessions), len(runs)
    for name, entry in usage.groups.items():
        entry.sessions, entry.runs = len(seen_sessions[name]), len(seen_runs[name])
    return usage


class UsageCache:
    """The last :data:`USAGE_DAYS` of ``tool_calls`` per group, read at most once a minute."""

    def __init__(self, *, ttl: float = USAGE_TTL_SECONDS, clock: Any = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._value: tuple[float, frozenset[tuple[str, str]], Usage] | None = None

    async def read(self, db: Any, group_of: Mapping[str, str], *, days: int = USAGE_DAYS) -> Usage:
        key = frozenset(group_of.items())
        now = self._clock()
        if self._value is not None and self._value[1] == key and now - self._value[0] < self._ttl:
            return self._value[2]
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        # One grouped scan: ``at`` is written by the same isoformat as ``since``, so the text comparison
        # is the time comparison, and the rows that come back are a few thousand at most.
        rows = await db.fetchall(
            "SELECT session_id, run_id, name, count(*) n FROM tool_calls WHERE at >= ? GROUP BY session_id, run_id, name",
            (since,),
        )
        usage = tally(rows, group_of, days=days)
        self._value = (now, key, usage)
        return usage


def group_of(member_lists: Mapping[str, Sequence[str]]) -> dict[str, str]:
    return {tool: group for group, names in member_lists.items() for tool in names}


__all__ = [
    "LOAD_NOW_KEY",
    "SESSION_LOADS_KEY",
    "USAGE_DAYS",
    "GroupUsage",
    "Usage",
    "UsageCache",
    "configured_loads",
    "group_of",
    "load_source",
    "members",
    "resolved_loads",
    "schema_tokens",
    "session_loads",
    "tally",
]
