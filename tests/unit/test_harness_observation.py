"""Launch admission checks rendered adapter settings before the daemon sees a launch."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.contract import LaunchSpec, ToolSetSpec
from daedalus.harness.cursor import CursorAdapter
from daedalus.harness.grok import GrokAdapter
from daedalus.harness.observation import observe
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.pi import PiAdapter
from daedalus.harness.runtime import CliStaffRuntime

ADAPTERS = (ClaudeCodeAdapter, CodexAdapter, GrokAdapter, OpenCodeAdapter, PiAdapter, CursorAdapter)


def request(name: str, *, resume: str = "") -> LaunchSpec:
    return LaunchSpec(
        harness=name, env="container", cwd="/work/project", launch_id=f"test-{name}",
        first_prompt=None, model="selected-model", permission_level="all" if name == "pi" else "ask", session_ref=resume,
        tool_sets=(ToolSetSpec("browser", "daedalus_browser", b"{}", 5000),),
    )


@pytest.mark.parametrize("adapter_type", ADAPTERS)
@pytest.mark.parametrize("resume", ["", "previous-session"])
def test_every_adapter_observes_model_tools_permission_and_resume(adapter_type, resume):
    adapter = adapter_type()
    spec = request(adapter.name, resume=resume)
    plan = adapter.resume_plan(spec, resume) if resume else adapter.launch_plan(spec)
    effective = observe(adapter, spec, plan)
    assert effective.model == spec.model
    assert effective.resume == resume
    assert len(effective.tools) == 2
    assert effective.permission


@pytest.mark.parametrize("adapter_type", ADAPTERS)
def test_a_missing_tool_file_refuses_the_plan(adapter_type):
    adapter = adapter_type()
    spec = request(adapter.name)
    plan = adapter.launch_plan(spec)
    plan = replace(plan, files={key: value for key, value in plan.files.items() if key != spec.tool_sets[0].path})
    with pytest.raises(ValueError, match="effective tools"):
        observe(adapter, spec, plan)


@pytest.mark.parametrize("adapter_type", ADAPTERS)
def test_a_requested_model_change_refuses_the_old_plan(adapter_type):
    adapter = adapter_type()
    spec = request(adapter.name)
    plan = adapter.launch_plan(spec)
    with pytest.raises(ValueError, match="effective model"):
        observe(adapter, replace(spec, model="different-model"), plan)


@pytest.mark.parametrize("adapter_type", (ClaudeCodeAdapter, CodexAdapter, GrokAdapter, OpenCodeAdapter))
def test_a_requested_permission_change_refuses_the_old_plan(adapter_type):
    adapter = adapter_type()
    spec = request(adapter.name)
    plan = adapter.launch_plan(spec)
    with pytest.raises(ValueError, match="effective permission"):
        observe(adapter, replace(spec, permission_level="all"), plan)


@pytest.mark.parametrize("adapter_type", ADAPTERS)
def test_a_falsely_advertised_resume_ref_refuses_the_plan(adapter_type):
    adapter = adapter_type()
    spec = request(adapter.name, resume="previous-session")
    plan = adapter.resume_plan(spec, "another-session")
    with pytest.raises(ValueError, match="effective resume"):
        observe(adapter, spec, plan)


@pytest.mark.parametrize("level", ["ask", "edits"])
def test_pi_refuses_permission_levels_its_bridge_cannot_enforce(level):
    adapter = PiAdapter()
    spec = replace(request("pi"), permission_level=level)
    with pytest.raises(ValueError, match="effective permission"):
        observe(adapter, spec, adapter.launch_plan(spec))


async def test_effective_mismatch_refuses_before_launch_row_or_daemon():
    runtime = object.__new__(CliStaffRuntime)
    runtime.kind = "claude"
    runtime.adapter = ClaudeCodeAdapter()
    runtime._spec = lambda *args: request("claude")
    runtime._actor = lambda *args: "member"
    original = runtime.adapter.launch_plan
    def wrong_model(spec):
        plan = original(spec)
        argv = list(plan.argv)
        argv[argv.index("--model") + 1] = "different"
        return replace(plan, argv=tuple(argv))

    runtime.adapter.launch_plan = wrong_model
    accessed = []

    class NoLaunchResources:
        def __getattr__(self, name):
            accessed.append(name)
            raise AssertionError("unsupported effective settings reached launch resources")

    runtime.store = NoLaunchResources()
    runtime.terminals = NoLaunchResources()
    with pytest.raises(ValueError, match="effective model"):
        await runtime._launch_once(SimpleNamespace(staff_session_id="member"), resume_ref="")
    assert accessed == []
