"""Inspect the launch material that an adapter will hand to its CLI before any process starts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from daedalus.harness.contract import LaunchPlan, LaunchSpec


@dataclass(frozen=True, slots=True)
class EffectiveLaunch:
    harness: str
    requested_model: str
    model: str
    requested_tools: tuple[str, ...]
    tools: tuple[str, ...]
    requested_permission: str
    permission: str
    requested_resume: str
    resume: str


def _option(argv: tuple[str, ...], name: str) -> str:
    index = argv.index(name)
    return argv[index + 1]


def _optional(argv: tuple[str, ...], name: str) -> str:
    return _option(argv, name) if name in argv else ""


def _json_file(plan: LaunchPlan, name: str) -> Any:
    return json.loads(plan.files[name])


def _grok_servers(plan: LaunchPlan) -> set[str]:
    text = plan.files["daedalus-staff.md"].decode()
    line = next(line for line in text.splitlines() if line.startswith("mcpServers: "))
    return {server["name"] for server in json.loads(line.removeprefix("mcpServers: "))}


def observe(adapter: Any, spec: LaunchSpec, plan: LaunchPlan) -> EffectiveLaunch:
    """Read effective settings from the rendered argv, files and app-server launch state.

    The capability table is a promise, so a missing or malformed rendered setting refuses launch.
    Rendered settings do not prove that a CLI or operating system enforced them after spawn.
    """
    try:
        name = spec.harness
        argv = plan.argv
        if name == "claude":
            model = _optional(argv, "--model")
            permission = _option(argv, "--permission-mode")
            tools = set(_json_file(plan, "mcp.json")["mcpServers"])
            resume = _option(argv, "--resume") if "--resume" in argv else ""
        elif name == "codex":
            thread = adapter._planned[spec.launch_id]
            model = thread.model
            permission = f"{thread.sandbox}/{thread.approval}"
            tools = {"daedalus_team"}
            for companion in plan.companions:
                tools.update(part.split("=", 1)[0].removeprefix("mcp_servers.") for part in companion.argv if part.startswith("mcp_servers."))
            resume = thread.resume
        elif name == "grok":
            model = _optional(argv, "-m")
            permission = _option(argv, "--permission-mode")
            tools = _grok_servers(plan)
            resume = _option(argv, "-r") if "-r" in argv else ""
        elif name == "opencode":
            model = _optional(argv, "-m")
            config = json.loads(plan.env["OPENCODE_CONFIG_CONTENT"])
            permission = json.dumps({key: config["permission"][key] for key in ("edit", "bash", "webfetch")}, sort_keys=True)
            tools = set(config["mcp"])
            resume = _option(argv, "-s") if "-s" in argv else ""
        elif name == "pi":
            model = _optional(argv, "--model")
            # The bridge records tool starts but never decides a tool call. Pi cannot enforce
            # the project's ask or edits level on its built-in tools.
            if spec.permission_level != "all":
                raise ValueError("permission: Pi cannot enforce ask or edits on built-in tools")
            permission = "unrestricted"
            tools = {"daedalus_team", *(item for item in plan.env.get("DAEDALUS_TOOL_SETS", "").split(",") if item)}
            resume = _option(argv, "--session-id") if spec.session_ref else ""
        elif name == "cursor":
            config = _json_file(plan, "cursor-launch.json")
            model = config["model"]
            permission = config["mode"]
            tools = {server["name"] for server in config["mcp_servers"]}
            resume = config["resume"]
        else:
            raise ValueError(f"unsupported harness {name!r}")
        expected_tools = {"daedalus_team", *(item.server if name != "pi" else item.name for item in spec.tool_sets)}
        if model != spec.model:
            raise ValueError("model")
        missing_files = [item.path for item in spec.tool_sets if item.path not in plan.files]
        if tools != expected_tools or missing_files:
            raise ValueError(f"tools: requested {sorted(expected_tools)}, rendered {sorted(tools)}, missing {missing_files}")
        if name == "claude":
            from daedalus.harness.claude import (  # Lazy: adapters import the shared runtime.
                LEVEL_MODES,
                PERMISSION_MODES,
            )
            expected_permission = PERMISSION_MODES[spec.permission_mode] if spec.permission_mode else LEVEL_MODES[spec.permission_level]
        elif name == "codex":
            from daedalus.harness.codex import CodexAdapter  # Lazy: adapters import the shared runtime.
            expected_permission = "/".join(CodexAdapter._modes(spec))
        elif name == "grok":
            from daedalus.harness.grok import LEVEL_MODES  # Lazy: adapters import the shared runtime.
            expected_permission = spec.permission_mode or LEVEL_MODES[spec.permission_level]
        elif name == "opencode":
            from daedalus.harness.opencode import PERMISSIONS  # Lazy: adapters import the shared runtime.
            expected_permission = json.dumps(PERMISSIONS[spec.permission_level], sort_keys=True)
        elif name == "pi":
            expected_permission = "unrestricted" if spec.permission_level == "all" else "unsupported"
        else:
            expected_permission = spec.permission_mode or "ask"
        if permission != expected_permission:
            raise ValueError("permission")
        if spec.session_ref and resume != spec.session_ref:
            raise ValueError("resume")
        if not spec.session_ref and resume:
            raise ValueError("resume")
        if spec.session_ref and plan.session_ref != spec.session_ref:
            raise ValueError("resume")
        return EffectiveLaunch(
            name, spec.model, model, tuple(sorted(expected_tools)), tuple(sorted(tools)),
            spec.permission_mode or spec.permission_level, permission, spec.session_ref, resume,
        )
    except (IndexError, KeyError, TypeError, json.JSONDecodeError, StopIteration) as exc:
        raise ValueError(f"{spec.harness} launch settings could not be observed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"{spec.harness} launch has unsupported effective {exc}") from exc
