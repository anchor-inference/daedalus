"""An unsupported permission request never becomes a launch with another access policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.harness import CAPABILITIES
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.contract import LaunchSpec
from daedalus.harness.cursor import CursorAdapter
from daedalus.harness.grok import GrokAdapter
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.pi import PiAdapter
from daedalus.harness.runtime import CliStaffRuntime
from daedalus.harness.tools import tooling

FACTORIES = {"claude": ClaudeCodeAdapter, "codex": CodexAdapter, "cursor": CursorAdapter,
             "grok": GrokAdapter, "opencode": OpenCodeAdapter, "pi": PiAdapter}


def test_the_adapter_matrix_covers_every_declared_runtime():
    assert FACTORIES.keys() == CAPABILITIES.keys()


def request(harness: str, **changes) -> LaunchSpec:
    return LaunchSpec(harness=harness, env="container", cwd="/work/project", launch_id="mapping",
                      first_prompt=None, **changes)


@pytest.mark.parametrize("harness", tuple(FACTORIES))
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("changes", [
    {"permission_mode": "mistyped-read-only", "permission_level": "all"},
    {"permission_level": "unrecognized"},
])
def test_unknown_permissions_refuse_fresh_and_resumed_plans(harness, resume, changes):
    adapter = FACTORIES[harness]()
    spec = request(harness, **changes)
    with pytest.raises(ValueError, match="unsupported .*permission"):
        adapter.resume_plan(spec, "previous-session") if resume else adapter.launch_plan(spec)
    assert getattr(adapter, "_planned", {}) == {}


@pytest.mark.parametrize("harness", tuple(FACTORIES))
def test_declared_modes_and_empty_defaults_remain_plannable(harness):
    adapter = FACTORIES[harness]()
    for mode in ("", *tooling(harness).modes):
        for level in ("ask", "edits", "all"):
            plan = adapter.launch_plan(request(harness, permission_mode=mode, permission_level=level))
            assert plan.argv


@pytest.mark.parametrize("harness", ("pi", "opencode"))
def test_an_explicit_plan_mode_is_not_silently_ignored(harness):
    with pytest.raises(ValueError, match="permission mode"):
        FACTORIES[harness]().launch_plan(request(harness, permission_mode="plan"))


async def test_runtime_refuses_before_opening_launch_or_contacting_daemon():
    runtime = object.__new__(CliStaffRuntime)
    runtime.adapter = CodexAdapter()
    runtime._spec = lambda *args: request("codex", permission_mode="mistyped-read-only", permission_level="all")
    accessed = []

    class NoLaunchResources:
        def __getattr__(self, name):
            accessed.append(name)
            raise AssertionError("invalid permissions reached launch resources")

    runtime.store = NoLaunchResources()
    runtime.terminals = NoLaunchResources()
    with pytest.raises(ValueError, match="permission mode"):
        await runtime._launch_once(SimpleNamespace(staff_session_id="member"), resume_ref="")
    assert accessed == []
