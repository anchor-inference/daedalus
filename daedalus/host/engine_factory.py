"""Build a ``QueryEngine`` for one session run."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from protocore.contracts.llm import IProviderChain
from protocore.contracts.tool_registry import IToolRegistry, ToolVisibilityPolicy, policy_admits
from protocore.runtime.query_engine import QueryEngine, QueryEngineConfig
from protocore.runtime.runtime_constants import default_runtime_constants
from protocore.runtime.tool_deferral import discovery_tool_names, ensure_tool_deferral
from protocore.runtime.tool_dispatch import ToolDispatcher
from protocore.runtime.tool_permission import (
    PermissionStage,
    ToolPermissionDecision,
    ToolPermissionGate,
    ToolPermissionOutcome,
)

from daedalus.config import ModeConfig, RuntimeConfig
from daedalus.host import prompts
from daedalus.host.hooks import DaedalusHookManager
from daedalus.providers.openai_compat import OpenAICompatibleProvider
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.sqlite import SqliteEventStream

TENANT = "daedalus"


@dataclass(slots=True)
class EngineDeps:
    tool_registry: IToolRegistry
    event_stream: SqliteEventStream
    blob_store: FileBlobStore
    skill_store: Any
    hook_manager: DaedalusHookManager
    bot_repo: Path
    core_repo: Path
    governance_path: Path
    github_org: str = ""
    ssh_config: Path | None = None
    """The ssh config whose described hosts the prompt lists; ``None`` lists nothing."""
    policy_gate: Callable[[str, str], Any] | None = None
    """``(session_id, run_id) -> IToolSafetyPolicy``: the host's tool policy bound to the session; ``None`` = no policy."""
    selfdev_mode: str = "off"
    """``off``/``local``/``server``, from the installation's capabilities: it decides which self-development
    section the prompt carries and how the environment describes the operator's repositories. The safe default
    is the one that promises nothing."""
    request_manifest_sink: Any | None = None
    """Host-owned durable request evidence; absent keeps the core instrumentation inert."""


class PolicyAdapter:
    """The host policy as the core's safety policy: every tool, every class; deny with the reason."""

    def __init__(self, decide: Callable[[str, dict[str, Any]], Any], *, ask_hint: Callable[[], str | None] | None = None) -> None:
        self.decide = decide
        self.ask_hint = ask_hint
        """What to say instead of "ask the operator" when the session asks someone else (a staff
        member's orchestrator); ``None`` from it keeps the ordinary text."""

    def applies_to(self, side_effect_class: str) -> bool:
        return True

    def evaluate(self, tool: Any, arguments: dict[str, Any], ctx: Any) -> ToolPermissionDecision:
        decision = self.decide(tool.name, dict(arguments or {}))
        if decision.action == "allow":
            return ToolPermissionDecision(outcome=ToolPermissionOutcome.allow)
        hint = self.ask_hint() if self.ask_hint is not None and decision.action == "ask" else None
        if hint:
            reason = f"needs approval: {decision.reason} (rule {decision.rule}). Approval key: {decision.key}. {hint}"
        elif decision.action == "ask":
            reason = (
                f"needs the operator's approval: {decision.reason} (rule {decision.rule}). Approval key: {decision.key}. "
                "Ask the operator with AskUser, quoting the key; once they grant it (/allow <key>, or the Mini App), the same call passes."
            )
        else:
            reason = f"refused by policy: {decision.reason} (rule {decision.rule}). This is not a question for the operator; do the task another way."
        return ToolPermissionDecision(outcome=ToolPermissionOutcome.deny, reason=reason, stage=PermissionStage.safety_policy)


def runtime_constants(config: RuntimeConfig, *, context_window: int, max_output_tokens: int, thinking: bool, mode: ModeConfig | None = None, max_advertised_tools: int = 0) -> Any:
    """``max_advertised_tools`` is the provider's cap on the number of tools in one request, 0 for none."""
    context_window = max(8_000, int(context_window))
    output_cap = max(1024, min(max_output_tokens, context_window))
    max_iterations = mode.max_iterations if mode is not None and mode.max_iterations else config.limits.max_iterations
    tool_timeout = mode.tool_timeout_seconds if mode is not None and mode.tool_timeout_seconds else config.limits.tool_timeout_seconds
    return default_runtime_constants(
        model_context_window=context_window,
        llm_output_max_tokens_ratio=min(1.0, max(0.01, output_cap / context_window)),
        max_turns_per_run=max_iterations,
        # Long runs are the point: no cumulative output-token budget per run; turns, spend and the operator bound it.
        run_max_output_tokens_budget=0,
        tool_timeout_seconds=int(tool_timeout),
        steer_follow_up_enabled=True,
        # The core's session work pool is not used here: Exec runs background jobs itself and the job
        # watcher (daedalus/extensions/jobs.py) wakes the session when one ends. Left on, the core
        # looked for a pool nobody binds and reported every run as "background_tasks_detached:
        # no_pool_bound", which read as the reason jobs never woke the agent and was not.
        background_tasks_enabled=False,
        steer_default_mode="all",
        follow_up_default_mode="all",
        memory_enabled=True,
        # No per-message clip (the core's default of 0 is spelled out because 200 used to be here): it
        # re-ranks the surface on every message, which moves the cached prefix, and a Russian message
        # against English descriptions finds too little. What does not fit is held back by group and
        # loaded through ToolSearch instead.
        tool_retrieval_top_k=0,
        # A provider that refuses a request above a number of tools refuses the first one, so the
        # core holds groups back until the surface is under it, MCP servers first.
        max_advertised_tools=max(0, int(max_advertised_tools)),
        # The skill catalogue (name + when-to-use line per skill) must fit whole: past the budget the core
        # drops the descriptions, and a bare name is not a reason to load a skill.
        skill_index_budget_ratio=0.04,
        # A summariser writes a few lines whatever it is given: a small tool exchange comes back
        # no smaller, so units under this size are joined with their neighbours rather than sent alone.
        compaction_summary_min_unit_tokens=800,
        # One deadline for every summariser call the session makes, in-run and between runs.
        compaction_summary_timeout_seconds=config.compaction.call_timeout_seconds,
        # The host compacts the whole history between runs; the core's tiers only catch a run that grows past that.
        # A server that reserves the output budget inside the window (vLLM) rejects a prompt above
        # window − max output, so the trigger must sit below that cliff, not only below the window —
        # and a whole turn below it: the check runs before a turn whose tool results add tens of
        # thousands of tokens, and the estimate runs a little short of the provider's own count.
        compaction_trigger_ratio=min(config.compaction.core_trigger_ratio, max(0.3, round(1 - output_cap / context_window - 0.15, 2))),
        # Long tasks are the point: no per-run tool-call cap; spend and iterations bound the run.
        leader_tool_call_soft_cap=0,
        compaction_protect_first_user_turn=True,
        # A reasoning model that returns neither text nor a tool call gets a nudge to
        # continue instead of ending the run; a repeating text tail is cut and nudged,
        # and the same tool call with the same arguments is refused after a few repeats.
        resilience_post_tool_empty_nudge_enabled=True,
        loop_guard_enabled=True,
        # The core's defaults (3 identical calls, 1 nudge) were tuned for short runs; a 200-iteration
        # run legitimately re-runs the same test command or re-reads the same file many times.
        loop_guard_identical_tool_limit=30,
        loop_guard_nudge_max=3,
        # Three failures of one tool in a run that takes hundreds of turns is not a broken tool.
        max_consecutive_tool_errors=8,
        # A result is worth its size while the agent is still reading it. Past
        # the fresh window a long one is cut to its head in the request — the
        # stored history keeps it whole — so a run of large reads stops
        # spending the window on pages nobody is looking at any more.
        tool_result_stale_trim_enabled=True,
        tool_result_fresh_count=config.tools.results.fresh_count,
        tool_result_stale_max_chars=config.tools.results.stale_max_chars,
        tool_result_stale_trim_batch_chars=config.tools.results.trim_batch_chars,
    )


Role = Literal["agent", "voice", "orchestrator", "dispatcher"]


def _agent_sections(deps: EngineDeps, config: RuntimeConfig, *, mode: ModeConfig | None, workspace: Path, session_title: str, model: str, extra_notes: str, project: str, advertised: Collection[str], admitted: Collection[str]) -> tuple[str, ...]:
    return (
        prompts.PERSONA,
        prompts.rules_section(config.prompt.rules),
        prompts.language_section(config.answer_language),
        prompts.governance_section(deps.governance_path),
        *prompts.tool_sections(advertised, admitted=admitted),
        (mode.prompt.strip() + "\n") if mode is not None and mode.prompt.strip() else "",
        prompts.environment_section(
            workspace=workspace,
            bot_repo=deps.bot_repo,
            core_repo=deps.core_repo,
            session_title=session_title,
            model=model,
            extra_notes=extra_notes,
            sandboxed=config.tools.exec.sandbox != "off",
            github_org=deps.github_org,
            ssh_hosts=prompts.ssh_hosts(deps.ssh_config) if deps.ssh_config else (),
            selfdev_mode=deps.selfdev_mode,
            project=project,
        ),
    )


def admitted_tools(engine: QueryEngine) -> frozenset[str]:
    """Every registered tool the run's policy lets it call, whether advertised or held back."""
    policy = engine.effective_tool_policy
    return frozenset(tool.name for tool in engine.tools.list_all() if policy_admits(policy, tool.name))


def advertised_tools(engine: QueryEngine) -> frozenset[str]:
    """The tools the run's first request puts in ``tools``, as the core will decide them.

    The session's admitted tools, less the groups the core holds back, plus what an earlier run of the
    session loaded back onto the surface; ToolSearch only while something is held back. The decision is
    the core's own (:func:`ensure_tool_deferral`), made here once and cached on the engine for the run,
    so the prompt is written for the surface the model is actually shown.
    """
    decision = ensure_tool_deferral(engine)
    registry = engine.tools
    admitted = set(admitted_tools(engine))
    advertised = (admitted - decision.deferred_names) | (admitted & set(engine.context_manager.discovered_tool_names()))
    if not decision.deferred_groups:
        advertised -= discovery_tool_names(registry.list_all(), engine.config.tool_roles)
    return frozenset(advertised)


def build_engine(
    *,
    deps: EngineDeps,
    config: RuntimeConfig,
    run_id: str,
    session_id: str,
    session_title: str,
    workspace: Path,
    rungs: list[tuple[OpenAICompatibleProvider, str]],
    provider_chain: IProviderChain | None,
    model_name: str | None = None,
    thinking: bool = True,
    reasoning_effort: str = "medium",
    context_window: int = 128_000,
    max_output_tokens: int = 32_000,
    extra_notes: str = "",
    tool_visibility_policy: ToolVisibilityPolicy | None = None,
    mode: ModeConfig | None = None,
    role: Role = "agent",
    project: str = "",
    max_advertised_tools: int = 0,
    discovered_tools: Collection[str] = (),
    loaded_tool_groups: Collection[str] = (),
    tool_group_loads: Mapping[str, str] | None = None,
) -> QueryEngine:
    """``role`` picks the system prompt: an agent that works in a folder, the voice concierge that
    only talks and hands over, a project's orchestrator that only runs a team, or the main
    orchestrator that only hands work to projects and follows it.

    ``max_advertised_tools`` is the lowest cap on tools among the run's providers (0 for none);
    ``discovered_tools`` are the tools an earlier run of the session loaded through ToolSearch, oldest
    first, which this run starts with loaded; ``loaded_tool_groups`` are groups it starts with loaded
    whole, each one entry under the core's cap on loaded tools; ``tool_group_loads`` is each host group's load mode for
    this run, the settings and the session's own choice over the defaults the groups were declared with."""
    primary_provider, primary_model = rungs[0]
    model = model_name or primary_model
    if role == "voice":
        sections: tuple[str, ...] = prompts.concierge_sections(answer_language=config.answer_language, agents=extra_notes)
    elif role == "orchestrator":
        sections = prompts.orchestrator_sections(answer_language=config.answer_language, governance=prompts.governance_section(deps.governance_path))
    elif role == "dispatcher":
        sections = prompts.dispatcher_sections(answer_language=config.answer_language, governance=prompts.governance_section(deps.governance_path))
    else:
        sections = ()  # written below, once the surface the run advertises is known
    engine_config = QueryEngineConfig(
        run_id=run_id,
        tenant_id=TENANT,
        session_id=session_id,
        account_id=TENANT,
        root_run_id=run_id,
        model_name=model,
        system_prompt_sections=tuple(s for s in sections if s),
        tool_visibility_policy=tool_visibility_policy or ToolVisibilityPolicy(),
        rc=runtime_constants(
            config, context_window=context_window, max_output_tokens=max_output_tokens, thinking=thinking, mode=mode, max_advertised_tools=max_advertised_tools,
        ),
        thinking_enabled=thinking,
        reasoning_effort=reasoning_effort,
        request_manifest_sink=deps.request_manifest_sink,
        discovered_tools=tuple(discovered_tools),
        loaded_tool_groups=tuple(loaded_tool_groups),
        tool_group_loads=dict(tool_group_loads or {}),
    )
    engine = QueryEngine(
        config=engine_config,
        llm_provider=primary_provider,
        tool_registry=deps.tool_registry,
        event_stream=deps.event_stream,
        hook_manager=deps.hook_manager,
        skill_store=deps.skill_store,
        blob_store=deps.blob_store,
        provider_chain=provider_chain,
    )
    # The container is the boundary: no shell deny patterns, no path isolation.
    engine._tool_dispatcher = ToolDispatcher(  # type: ignore[attr-defined]
        registry=deps.tool_registry,
        permission_gate=ToolPermissionGate(policies=[deps.policy_gate(session_id, run_id)] if deps.policy_gate is not None else []),
        hook_manager=deps.hook_manager,
    )
    if role == "agent":
        # A subagent or a staff member told how to notify the operator, or a session told about a board
        # it cannot see, would try, be refused or find nothing, and spend a turn learning why. The
        # sections that carry rules follow what the policy admits and the ones that only teach follow
        # the surface; ``prompts.tool_sections`` says which is which.
        sections = _agent_sections(
            deps, config, mode=mode, workspace=workspace, session_title=session_title, model=model, extra_notes=extra_notes, project=project, advertised=advertised_tools(engine), admitted=admitted_tools(engine),
        )
        engine.config = replace(engine.config, system_prompt_sections=tuple(s for s in sections if s))
    deps.event_stream.bind_run(run_id, session_id)
    return engine


__all__ = ["TENANT", "EngineDeps", "Role", "admitted_tools", "advertised_tools", "build_engine", "runtime_constants"]
