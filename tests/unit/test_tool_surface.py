"""What the model is shown of the tools: every host tool while it fits, groups held back when it does not.

The core decides the surface; the host decides what it has to decide from. These tests hold the host's
side of it: a registry that ranks (the core's, not its test double), a policy that refuses and never
pins (a pin is what the core never holds back), a tool group for every family worth holding back and
one per connected MCP server, a cap per provider, a prompt that teaches only the tools on the surface,
and the tools a run loaded carried to the next run of the session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tool_registry import TOOL_VISIBILITY_POLICY_METADATA_KEY, policy_admits
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolCall
from protocore.runtime.tool_deferral import build_tool_surface, calls_held_for_rules, tool_catalogue_block
from protocore.runtime.tool_registry import ToolRegistry

from daedalus.config import (
    STAFF_BLOCKED_TOOLS,
    McpServerConfig,
    ProviderConfig,
    RuntimeConfig,
    Settings,
    max_advertised_tools_for,
)
from daedalus.host import prompts, tool_groups
from daedalus.host.engine_factory import advertised_tools, runtime_constants
from daedalus.host.session_runner import SessionManager
from daedalus.mcp.manager import McpConnection, mcp_group_name, mcp_tool_name
from daedalus.stores.database import Database
from daedalus.tools import TOOL_GROUPS, discover_tools
from tests.support.models import model_config
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import _idle
from tests.unit.test_session_runner import ScriptedProvider

VERBS = ("create", "list", "get", "update", "delete", "search", "archive", "assign", "comment", "export")
OBJECTS = (
    ("issue", "an issue in the tracker"), ("pull_request", "a pull request"), ("calendar_event", "an event in the calendar"),
    ("slack_message", "a message in a Slack channel"), ("invoice", "an invoice for a customer"), ("wiki_page", "a page of the team wiki"),
    ("spreadsheet_row", "a row of a spreadsheet"), ("deployment", "a deployment of a service"), ("dashboard", "a monitoring dashboard"),
    ("contact", "a contact in the address book"), ("ticket", "a support ticket"), ("playlist", "a music playlist"),
    ("folder", "a folder in cloud storage"), ("email_draft", "a draft email"), ("sprint", "a sprint of the board"),
    ("pipeline", "a CI pipeline run"), ("alert", "a monitoring alert"), ("customer", "a customer record"),
    ("order", "an order in the shop"), ("repository", "a code repository"), ("label", "a label of issues"),
    ("webhook", "a webhook subscription"), ("user", "a user account"), ("team", "a team of users"),
    ("document", "a shared document"), ("note", "a personal note"), ("task", "a task of a project"),
    ("release", "a release of a repository"), ("comment", "a comment on a document"), ("reminder", "a reminder"),
)
"""Three hundred tools of one made-up server, named and described the way MCP servers write them."""


def _remote(name: str, description: str) -> SimpleNamespace:
    schema = {"type": "object", "properties": {"id": {"type": "string", "description": "identifier"}}, "required": []}
    return SimpleNamespace(name=name, description=description, input_schema=schema)


def _catalogue(count: int) -> list[SimpleNamespace]:
    remotes = [_remote(f"{verb}_{obj}", f"{verb.title()} {what}. Returns it as JSON.") for obj, what in OBJECTS for verb in VERBS]
    return remotes[:count]


async def _manager(settings: Settings, db: Database, config: RuntimeConfig | None = None, **settings_update: Any) -> SessionManager:
    manager = SessionManager(settings.model_copy(update=settings_update) if settings_update else settings, config or model_config(), db=db)
    await manager.start()

    async def connected(name: str) -> None:
        return None  # the catalogue is published by the test; nothing is spawned

    manager.mcp.ensure = connected  # type: ignore[method-assign, assignment]
    return manager


def _connect(manager: SessionManager, server: str, remotes: list[SimpleNamespace], description: str = "") -> None:
    manager.config.mcp.servers[server] = McpServerConfig(transport="stdio", command="true", description=description)
    manager.mcp.reload(manager.config.mcp.servers)
    connection = McpConnection(server, manager.config.mcp.servers[server])
    manager.mcp._replace_catalog(server, [connection._proxy(remote) for remote in remotes])


def _system(engine: Any) -> str:
    return "\n".join(engine.config.system_prompt_sections)


# -- the registry and the policy ---------------------------------------------------------------------


async def test_both_registries_are_the_cores_ranked_registry_and_only_the_agents_searches(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        assert type(manager.tools) is ToolRegistry and type(manager.dispatcher_tools) is ToolRegistry
        assert manager.tools.get("ToolSearch") is not None
        assert manager.dispatcher_tools.get("ToolSearch") is None
        # The ranking is the core's: a Russian message finds the host tool through its hint.
        assert [tool.name for tool in manager.tools.search("подними дев сервер на порту", top_k=1)] == ["ServiceStart"]
        assert {group.name for group in manager.tools.tool_groups()} == set(TOOL_GROUPS)
    finally:
        await manager.close()


async def test_no_session_pins_a_tool_and_the_refusals_are_what_they_were(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        ordinary = await manager.create_session("ordinary")
        states = [
            ordinary,
            await manager.create_session("child", metadata={"subagent_of": ordinary.session.id}),
            await manager.create_session("staff", metadata={"staff_session_id": "ss1", "staff_id": "st1"}),
            await manager.create_session("orchestrator", metadata={"orchestrator_of": "p1"}),
            await manager.create_session("voice", metadata={"voice": True}),
            await manager.create_session("main", metadata={"dispatcher": True}),
        ]
        for state in states:
            policy = manager.tool_policy_for(state)
            assert not policy.pinned and not policy.visible, state.session.title
        staff = manager.tool_policy_for(states[2])
        assert set(STAFF_BLOCKED_TOOLS) & {t.name for t in manager.tools.list_all()} <= staff.blocked
        assert policy_admits(staff, "ToolSearch") and policy_admits(staff, "BoardAdd")
        # The roles with an allowlist have nothing to search for, so the search is theirs to refuse.
        for state in (states[3], states[4]):
            assert not policy_admits(manager.tool_policy_for(state), "ToolSearch"), state.session.title
    finally:
        await manager.close()


def test_every_group_of_host_tools_is_declared_and_the_everyday_tools_are_in_none() -> None:
    grouped = {tool.name: getattr(tool, "tool_group", "") for tool in discover_tools()}
    assert {group for group in grouped.values() if group} == set(TOOL_GROUPS)
    # Never held back: what every task needs, and what the models failed to search for when it was.
    for name in ("Read", "Write", "Edit", "MultiEdit", "Find", "Search", "Exec", "WebSearch", "WebFetch", "AskUser", "Skill", "Notify", "HistorySearch"):
        assert not grouped[name], name
    # Held back only by name in a catalogue line that says what the group is for.
    assert grouped["ServiceStart"] == "services" and "ServiceStart" in TOOL_GROUPS["services"].description
    assert grouped["IntentCreate"] == "scheduling" and "IntentCreate" in TOOL_GROUPS["scheduling"].description


# -- the constants -----------------------------------------------------------------------------------


def test_the_per_message_clip_is_off_and_the_cap_reaches_the_constants() -> None:
    rc = runtime_constants(RuntimeConfig(), context_window=128_000, max_output_tokens=32_000, thinking=True, max_advertised_tools=128)
    assert rc.tool_retrieval_top_k == 0
    assert rc.max_advertised_tools == 128
    assert runtime_constants(RuntimeConfig(), context_window=128_000, max_output_tokens=32_000, thinking=True).max_advertised_tools == 0


def test_the_cap_is_the_providers_setting_or_the_known_limit_of_the_model() -> None:
    assert max_advertised_tools_for(None, "grok-4.7") == 350
    assert max_advertised_tools_for(ProviderConfig(), "google/gemini-3-pro") == 128
    assert max_advertised_tools_for(ProviderConfig(), "deepseek-flash") == 0
    assert max_advertised_tools_for(ProviderConfig(max_advertised_tools=200), "grok-4.7") == 200
    assert max_advertised_tools_for(ProviderConfig(max_advertised_tools=0), "gemini-3-pro") == 0


async def test_a_run_is_capped_by_the_lowest_limit_among_its_rungs(settings: Settings, db: Database) -> None:
    config = model_config()
    config.providers["openrouter"] = config.providers["openrouter"].model_copy(update={"max_advertised_tools": 90})
    manager = await _manager(settings, db, config)
    try:
        def rung(provider_id: str, model: str) -> tuple[Any, str]:
            return SimpleNamespace(endpoint=SimpleNamespace(id=provider_id)), model

        assert manager.advertised_tools_limit([rung("deepseek", "deepseek-flash")]) == 0
        # A fallback that refuses more tools than the primary sends caps the run, or it fails when it is reached.
        assert manager.advertised_tools_limit([rung("deepseek", "deepseek-flash"), rung("openrouter", "x")]) == 90
        assert manager.advertised_tools_limit([rung("deepseek", "grok-4.7"), rung("openrouter", "x")]) == 90
        assert manager.advertised_tools_limit([rung("unknown", "gemini-3")]) == 128
        state = await manager.create_session("capped")
        engine = await manager._build_engine(state, "run-capped")
        assert engine.config.rc.max_advertised_tools == 90  # the test table falls back to openrouter
    finally:
        await manager.close()


# -- a large MCP server ------------------------------------------------------------------------------


async def test_a_large_mcp_server_is_held_back_behind_the_search(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        # Every group on the surface, so what the server does to it is all that changes.
        eager = {tool_groups.SESSION_LOADS_KEY: dict.fromkeys(TOOL_GROUPS, "eager")}
        quiet = await manager.create_session("quiet", metadata=eager)
        plain = build_tool_surface(await manager._build_engine(quiet, "run-quiet"))
        # With nothing held back the surface is every tool the session may call, and no search.
        admitted = {t.name for t in manager.tools.list_all() if policy_admits(manager.tool_policy_for(quiet), t.name)}
        assert {d.name for d in plain} == admitted - {"ToolSearch"}

        _connect(manager, "bigsrv", _catalogue(300), description="The team's tracker, calendar and chat")
        state = await manager.create_session("big", metadata={"mcp_enabled": ["bigsrv"], **eager})
        engine = await manager._build_engine(state, "run-big")
        surface = [d.name for d in build_tool_surface(engine)]
        assert not [name for name in surface if name.startswith("Mcp_Bigsrv_")]
        assert "ToolSearch" in surface
        assert len(surface) == len(plain) + 1
        assert "- MCP server bigsrv: The team's tracker, calendar and chat. Tools: Mcp_Bigsrv_* (300 tools)" in tool_catalogue_block(engine)
        assert engine._tool_deferral is not None and engine._tool_deferral.deferred_groups == (mcp_group_name("bigsrv"),)
        # The host's own tools stay on the surface, and so does the prompt that teaches them.
        assert "BoardAdd" in surface and prompts.BOARD_TASKS in _system(engine)

        search = manager.tools.get("ToolSearch")
        assert search is not None
        for query in ("create an event in the calendar", "создай событие в календаре"):
            context = ToolContext(tenant_id="daedalus", run_id="r", session_id=state.session.id, metadata={"tool_call_id": "c1", TOOL_VISIBILITY_POLICY_METADATA_KEY: engine.effective_tool_policy})
            result = await search.invoke(context, {"query": query})
            # The host calendar tools now match this query before the connected server's copy.
            assert mcp_tool_name("bigsrv", "create_calendar_event") in result.metadata["matches"], query

        # A session that did not enable the server is not told of it, nor offered the search.
        other = await manager.create_session("other", metadata=eager)
        engine = await manager._build_engine(other, "run-other")
        assert "ToolSearch" not in [d.name for d in build_tool_surface(engine)] and "bigsrv" not in tool_catalogue_block(engine)
    finally:
        await manager.close()


async def test_a_catalogue_that_changes_under_a_running_session_never_reaches_it_unless_enabled(settings: Settings, db: Database) -> None:
    """The policies refuse other servers' proxies by name, so a server that connected or re-listed its
    tools after a session's policy was computed had its new proxies advertised to that session."""
    manager = await _manager(settings, db)
    try:
        _connect(manager, "tracker", _catalogue(5))
        bystander = await manager.create_session("bystander")
        user = await manager.create_session("user", metadata={"mcp_enabled": ["tracker"]})
        for state in (bystander, user):
            state.engine = await manager._build_engine(state, f"run-{state.session.title}")
        # The server re-lists with more tools, and another server connects, while both are running.
        _connect(manager, "tracker", _catalogue(12))
        _connect(manager, "late", _catalogue(3))
        tracker, late = manager.mcp.tool_names("tracker"), manager.mcp.tool_names("late")
        assert len(tracker) == 12 and len(late) == 3
        assert bystander.engine is not None and user.engine is not None
        surface = {d.name for d in build_tool_surface(bystander.engine)}
        assert not surface & (tracker | late)
        assert not any(policy_admits(bystander.engine.effective_tool_policy, name) for name in tracker | late)
        assert all(policy_admits(user.engine.effective_tool_policy, name) for name in tracker)
        assert not any(policy_admits(user.engine.effective_tool_policy, name) for name in late)
    finally:
        await manager.close()


def _listed(manager: SessionManager, server: str) -> None:
    """Make ``server``'s published catalogue show in McpList, as a connected server's does."""
    connection = McpConnection(server, manager.config.mcp.servers[server])
    connection.tools = [manager.mcp._registered_tools[server][name] for name in sorted(manager.mcp.tool_names(server))]
    manager.mcp._connections[server] = connection


async def test_mcp_list_and_enable_name_a_held_back_server_by_prefix_and_a_few_names(settings: Settings, db: Database) -> None:
    """Both used to list every name: three hundred of them, ten kilobytes each, kept in the history that
    every later request re-reads — a third of what holding the server back had saved."""
    manager = await _manager(settings, db)
    try:
        _connect(manager, "bigsrv", _catalogue(300))
        _connect(manager, "small", _catalogue(5))
        _listed(manager, "bigsrv")
        _listed(manager, "small")
        state = await manager.create_session("s")
        sid = state.session.id
        enabled = await manager.mcp_service("enable", session_id=sid, server="bigsrv")
        assert "ToolSearch by describing the task" in enabled and "Mcp_Bigsrv_* (300 tools, spelt like " in enabled
        assert len(enabled) < 600
        examples = enabled.split("spelt like ", 1)[1].rstrip(")").split(", ")
        assert len(examples) == 5 and all(name in manager.mcp.tool_names("bigsrv") for name in examples)
        assert len(set(examples)) == 5
        listing = await manager.mcp_service("list", session_id=sid)
        assert "[on ] bigsrv" in listing and "Mcp_Bigsrv_* (300 tools, spelt like " in listing and len(listing) < 1200
        # A small server is named in full, as the catalogue names it.
        small = sorted(manager.mcp.tool_names("small"))
        assert ", ".join(small) in listing
        assert ", ".join(small) in await manager.mcp_service("enable", session_id=sid, server="small")
        # Without ToolSearch nothing is held back, and the model needs every exact name.
        state.metadata["tools_off"] = ["ToolSearch"]
        assert len(await manager.mcp_service("list", session_id=sid)) > 5000
    finally:
        await manager.close()


async def test_a_server_switched_off_is_told_to_the_model(settings: Settings, db: Database) -> None:
    """The tools left the surface, but nothing said so: the history still held "Loaded, and callable" of
    them, and a model asked what it could use listed them as available."""
    manager = await _manager(settings, db)
    try:
        _connect(manager, "tracker", _catalogue(5))
        state = await manager.create_session("s", metadata={"mcp_enabled": ["tracker"]})
        sid = state.session.id
        # The model's own switch: the result says it.
        text = await manager.mcp_service("disable", session_id=sid, server="tracker")
        assert "Mcp_Tracker_*" in text and "no longer available" in text
        assert "mcp_switched_off" not in state.metadata
        # The operator's switch: the next turn is told, once.
        await manager.set_mcp(sid, "tracker", True)
        await manager.set_mcp(sid, "tracker", False)
        opening = Message(role=MessageRole.user, content_blocks=[TextBlock(text="what can you use?")])
        first = await manager._with_turn_context(state, opening)
        text = "".join(b.text for b in first.content_blocks if isinstance(b, TextBlock))
        assert "MCP server tracker was switched off: its tools (Mcp_Tracker_*) are no longer available" in text
        stored = await manager.sessions.get(sid, "daedalus")
        assert stored is not None and "mcp_switched_off" not in stored.metadata
        again = await manager._with_turn_context(state, opening)
        assert "switched off" not in "".join(b.text for b in again.content_blocks if isinstance(b, TextBlock))
        # Switched back on before the next turn, there is nothing to tell.
        await manager.set_mcp(sid, "tracker", True)
        await manager.set_mcp(sid, "tracker", False)
        await manager.set_mcp(sid, "tracker", True)
        back = await manager._with_turn_context(state, opening)
        assert "switched off" not in "".join(b.text for b in back.content_blocks if isinstance(b, TextBlock))
    finally:
        await manager.close()


# -- the prompt --------------------------------------------------------------------------------------


async def test_the_prompt_teaches_only_the_tools_a_role_may_call(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        ordinary = await manager.create_session("ordinary")
        system = _system(await manager._build_engine(ordinary, "run-ordinary"))
        for section in (prompts.BOARD_TASKS, prompts.NOTIFY, prompts.HISTORY_SEARCH):
            assert section in system
        # The rules of a group are the group's: the core writes them where its tools are, never here.
        for section in (prompts.SCHEDULING, prompts.LOOP.removeprefix("- "), prompts.BROWSER):
            assert section not in system

        staff = await manager.create_session("staff", metadata={"staff_session_id": "ss1", "staff_id": "st1"})
        system = _system(await manager._build_engine(staff, "run-staff"))
        # It may not schedule, loop or notify; it keeps its board, its helpers and its services.
        for section in (prompts.NOTIFY, prompts.BOARD_TWO_WAYS):
            assert section not in system
        for section in (prompts.BOARD_TASKS, prompts.BOARD_SUBAGENT, prompts.BOARD_SERVICES, prompts.HISTORY_HEADLINE):
            assert section in system

        child = await manager.create_session("child", metadata={"subagent_of": ordinary.session.id})
        system = _system(await manager._build_engine(child, "run-child"))
        assert prompts.NOTIFY not in system
    finally:
        await manager.close()


def _flat(text: str) -> str:
    """Text with each line's indent dropped: the catalogue indents a held-back group's rules under its line."""
    return "\n".join(line.strip() for line in text.strip().splitlines())


RULED = {"browser": "BrowserOpen", "self_development": "SelfWorkspace", "scheduling": "ScheduleCreate", "loop": "LoopNext"}
"""Each group that carries rules, and a tool of it."""


def _rules_where_the_model_can_meet_them(engine: Any, group: str) -> bool:
    """Whether a run's model meets ``group``'s rules before it can use the group's tools.

    Either they are in the prompt's catalogue block, beside tools already in front of the model, or
    the group is held back and the rules come with its first load — and a call of one of its tools
    made without loading it is answered with them instead of running.
    """
    rules = next(g.instructions for g in engine.tools.tool_groups() if g.name == group)
    if not rules:
        return False
    if _flat(rules) in _flat(tool_catalogue_block(engine)) or _flat(rules) in _flat(_system(engine)):
        return True
    decision = engine._tool_deferral
    if decision is None or group not in decision.deferred_groups:
        return False
    # What the loop records as it sends the first request: the names that request advertises.
    engine._advertised_tool_names = frozenset(d.name for d in build_tool_surface(engine))
    call = ToolCall(id="blind", name=RULED[group], arguments={})
    return calls_held_for_rules(engine, [call]) == {"blind": group}


async def test_a_groups_rules_are_never_missing_where_its_tools_can_be_called(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The regression this guards: the rules left the prompt exactly when the core held the group back,
    and a model that loaded BrowserOpen mid-run met a password field without the rule that it never
    types one. Every way a group reaches a run is tried, and in each the rules are there or on the way."""
    config = model_config()
    for preset in config.presets.values():
        preset.max_output_tokens = 8_000
    manager = await _manager(settings, db, config, browser_container_dir=tmp_path / "browser", github_token="stub")
    try:
        manager.config.tools.groups = {}
        assert {g.name: bool(g.instructions) for g in manager.tools.tool_groups() if g.name in RULED} == dict.fromkeys(RULED, True)
        assert next(g for g in manager.tools.tool_groups() if g.name == "browser").instructions == prompts.BROWSER.strip()
        cases: dict[str, dict[str, Any]] = {
            "defaults": {},
            "eager for the session": {tool_groups.SESSION_LOADS_KEY: dict.fromkeys(RULED, "eager")},
            "auto": {tool_groups.SESSION_LOADS_KEY: dict.fromkeys(RULED, "auto")},
            "no search": {"tools_off": ["ToolSearch"]},
            "loaded now": {tool_groups.LOAD_NOW_KEY: list(RULED)},
        }
        for window in (128_000, 40_000):
            for label, metadata in cases.items():
                state = await manager.create_session(f"{label} {window}", metadata=dict(metadata))
                state.context_window = window
                engine = await manager._build_engine(state, f"run-{window}-{label.replace(' ', '-')}")
                # The prompt is written for the surface the model is actually shown.
                assert advertised_tools(engine) == {d.name for d in build_tool_surface(engine)}, label
                for group, tool in RULED.items():
                    if policy_admits(manager.tool_policy_for(state), tool):
                        assert _rules_where_the_model_can_meet_them(engine, group), f"{group}, {label}, window {window}"
        # A group refused to the role has no rules to meet, and they are nowhere.
        staff = await manager.create_session("staff", metadata={"staff_session_id": "ss1", "staff_id": "st1", tool_groups.SESSION_LOADS_KEY: {"scheduling": "eager"}})
        engine = await manager._build_engine(staff, "run-staff")
        assert _flat(prompts.SCHEDULING) not in _flat(tool_catalogue_block(engine) + _system(engine))
    finally:
        await manager.close()


async def test_the_operators_mode_and_the_sessions_reach_the_run(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager = await _manager(settings, db, browser_container_dir=tmp_path / "browser")
    try:
        default = await manager._build_engine(await manager.create_session("default"), "run-default")
        assert {"browser", "scheduling", "self_development"} <= set(default._tool_deferral.deferred_groups)
        assert "BrowserOpen" not in {d.name for d in build_tool_surface(default)}

        manager.config.tools.groups = RuntimeConfig.model_validate({"tools": {"groups": {"browser": {"load": "eager"}}}}).tools.groups
        chosen = await manager._build_engine(await manager.create_session("settings"), "run-settings")
        assert "browser" not in chosen._tool_deferral.deferred_groups
        assert "BrowserOpen" in {d.name for d in build_tool_surface(chosen)}
        # Beside the tools, the rules: the catalogue carries them for a group on the surface.
        assert _flat(prompts.BROWSER) in _flat(tool_catalogue_block(chosen))
        assert "The browser: BrowserOpen" not in _system(chosen)  # once, not twice

        own = await manager.create_session("own", metadata={tool_groups.SESSION_LOADS_KEY: {"browser": "lazy"}})
        engine = await manager._build_engine(own, "run-own")
        assert engine.config.tool_group_loads["browser"] == "lazy"
        assert "browser" in engine._tool_deferral.deferred_groups
    finally:
        manager.config.tools.groups = {}
        await manager.close()


async def test_a_group_loaded_now_starts_the_next_run_loaded_and_the_queue_empties(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("load now")
        await manager.load_tool_group_now(state.session.id, "scheduling")
        engine = await manager._build_engine(state, "run-now")
        surface = {d.name for d in build_tool_surface(engine)}
        assert {"ScheduleCreate", "IntentCreate"} <= surface
        # Its rules come with it, beside the catalogue line of the group it was loaded from.
        assert _flat(prompts.SCHEDULING) in _flat(tool_catalogue_block(engine))
        await manager._clear_queued_group_loads(state)
        assert tool_groups.LOAD_NOW_KEY not in state.metadata
        stored = await manager.sessions.get(state.session.id, "daedalus")
        assert stored is not None and tool_groups.LOAD_NOW_KEY not in stored.metadata
    finally:
        await manager.close()


async def test_groups_loaded_now_are_seeded_whole_and_each_is_one_loaded_entry(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        # Fifteen tools carried by name fill the core's cap on loaded tools; passed as names too, the two
        # groups' eleven tools pushed most of them out. Whole, each group is one entry more.
        state = await manager.create_session("two groups")
        policy = manager.tool_policy_for(state)
        carried = sorted(t.name for t in manager.tools.list_all() if not getattr(t, "tool_group", "") and policy_admits(policy, t.name))[:13]
        assert len(carried) == 13
        state.metadata["discovered_tools"] = carried
        await manager.load_tool_group_now(state.session.id, "scheduling")
        await manager.load_tool_group_now(state.session.id, "loop")
        engine = await manager._build_engine(state, "run-two")
        assert engine.config.loaded_tool_groups == ("scheduling", "loop")
        build_tool_surface(engine)
        loaded = engine.context_manager
        assert loaded.loaded_tool_group_names() == ("scheduling", "loop")
        members = tool_groups.members(manager.tools.list_all())
        assert set(members["scheduling"]) | set(members["loop"]) | set(carried) == set(loaded.discovered_tool_names())
    finally:
        await manager.close()


async def test_a_group_is_carried_whole_only_while_the_runs_use_it(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("carry groups")
        state.metadata[tool_groups.CARRIED_KEY] = ["scheduling", "loop", "telepathy"]
        engine = await manager._build_engine(state, "run-carry")
        # A group nothing declares loads nothing and is not handed on.
        assert engine.config.loaded_tool_groups == ("scheduling", "loop")
        build_tool_surface(engine)
        engine.context_manager.discover_tool("DocsRead")
        engine.context_manager.note_tool_used("DocsRead")
        engine.context_manager.note_tool_used("ScheduleList")
        assert manager._keep_loaded_tools(state, engine)
        # The schedules were used and go on whole; the loop was seeded and never called, so it is let go;
        # a tool loaded on its own and called is carried by name, as before.
        assert state.metadata[tool_groups.CARRIED_KEY] == ["scheduling"]
        assert state.metadata["discovered_tools"] == ["DocsRead"]
        assert not manager._keep_loaded_tools(state, engine)
        groups = {g["name"]: g for g in manager.tool_group_states(state)}
        assert groups["scheduling"]["state"] == "loaded" and groups["loop"]["state"] == "deferred"

        # One the session now keeps on the surface needs no loading.
        await manager.set_session_tool_group(state.session.id, "scheduling", "eager")
        assert manager.loaded_groups_for(state) == ()
        await manager.set_session_tool_group(state.session.id, "scheduling", None)
        assert manager.loaded_groups_for(state) == ("scheduling",)

        # A group the session may no longer call is not carried back, whatever the last run used.
        members = tool_groups.members(manager.tools.list_all())
        await manager.set_tools_off(state.session.id, members["scheduling"])
        assert manager.loaded_groups_for(state) == ()
        assert {g["name"]: g for g in manager.tool_group_states(state)}["scheduling"]["state"] == "off"
    finally:
        await manager.close()


async def test_the_panel_reads_the_last_runs_placement_of_a_group_after_a_restart(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("placed")
        sid = state.session.id
        engine = await manager._build_engine(state, "run-placed")
        # What the last advertisement of the run said: the agents went on demand in a small window.
        state.tool_group_surface = {"agents": "deferred", "board": "advertised", "browser": "deferred"}
        assert manager._keep_loaded_tools(state, engine)
        await manager.sessions.update_metadata(sid, state.session.metadata)
    finally:
        await manager.close()

    restarted = await _manager(settings, db)
    try:
        state = await restarted.get_state(sid)
        assert state is not None and state.tool_group_surface == {}
        groups = {g["name"]: g for g in restarted.tool_group_states(state)}
        assert groups["agents"]["state"] == "deferred"
        assert groups["board"]["state"] == "advertised"
        # A group placed by its mode is placed by it, whatever was stored.
        assert groups["services"]["state"] == "undecided"
    finally:
        await restarted.close()


def test_sections_left_in_the_prompt_follow_the_policy_or_the_surface() -> None:
    """What the host still writes itself: Notify's rules where the tool is admitted, teaching where it is shown."""
    shown = {"HistorySearch", "HistoryExpand"}
    text = "".join(prompts.tool_sections(shown, admitted=shown | {"Notify", "BoardAdd", "BoardUpdate", "BoardList"}))
    assert prompts.NOTIFY in text and prompts.HISTORY_SEARCH in text and prompts.BOARD_TASKS not in text
    assert prompts.NOTIFY not in "".join(prompts.tool_sections(set(), admitted=set()))
    # The groups' rules are theirs, in full, and self-development's follows the installation's mode.
    assert prompts.group_instructions("browser", selfdev_mode="off") == prompts.BROWSER
    assert prompts.group_instructions("self_development", selfdev_mode="server") == prompts.SELF_DEVELOPMENT
    assert prompts.group_instructions("self_development", selfdev_mode="local") == prompts.SELF_DEVELOPMENT_LOCAL
    assert prompts.group_instructions("self_development", selfdev_mode="off") == ""
    assert prompts.group_instructions("board", selfdev_mode="server") == ""


# -- what a run loaded, carried to the next ----------------------------------------------------------


async def test_the_tools_a_run_loaded_are_loaded_in_the_next_run_of_the_session(settings: Settings, db: Database) -> None:
    tool = mcp_tool_name("tracker", "create_issue")
    unused = mcp_tool_name("tracker", "list_issue")
    provider = ScriptedProvider([
        {"tool": "ToolSearch", "args": {"query": f"select:{tool},{unused}"}},
        {"tool": tool, "args": {"id": "1"}},
        {"text": "loaded"},
        {"text": "second"},
    ])
    manager = await _manager(settings, db)
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    try:
        _connect(manager, "tracker", _catalogue(20))
        state = await manager.create_session("carry", metadata={"mcp_enabled": ["tracker"]})
        sid = state.session.id
        await manager.submit(sid, "load the tracker")
        await until_await(lambda: _idle(manager, sid), "the first run ended")
        assert [t.name for t in provider.requests[1].tools or []][-2:] == [tool, unused]
        # Loaded both, called one: the one it never called is not carried into every later request.
        assert state.metadata["discovered_tools"] == [tool]
        await until_await(lambda: _stored(manager, sid, [tool]), "the loaded tools were stored")

        await manager.submit(sid, "again")
        await until_await(lambda: _answered_twice(manager, sid), "the second run ended")
        # Loaded from the first request of the second run, after the base surface.
        assert [t.name for t in provider.requests[3].tools or []][-1] == tool
        assert unused not in [t.name for t in provider.requests[3].tools or []]

        # What the session may no longer call is not carried back.
        state.metadata["tools_off"] = [tool]
        assert manager.discovered_tools_for(state) == ()
        state.metadata.pop("tools_off")
        state.metadata["mcp_enabled"] = []
        assert manager.discovered_tools_for(state) == ()
    finally:
        await manager.close()


async def _stored(manager: SessionManager, session_id: str, names: list[str]) -> bool:
    session = await manager.sessions.get(session_id, "daedalus")
    return session is not None and session.metadata.get("discovered_tools") == names


async def _answered_twice(manager: SessionManager, session_id: str) -> bool:
    state = manager.live_state(session_id)
    if state is None or state.running or not state.settled.is_set():
        return False
    from protocore.contracts.types import MessageRole

    answers = [m for m in await manager.sessions.list_transcript(session_id) if m.role is MessageRole.assistant]
    return len(answers) >= 4


@pytest.mark.parametrize("names", [["BoardAdd", "Nope", "Exec"], []])
async def test_only_registered_admitted_names_are_carried(settings: Settings, db: Database, names: list[str]) -> None:
    manager = await _manager(settings, db)
    try:
        state = await manager.create_session("s", metadata={"tools_off": ["Exec"]})
        state.metadata["discovered_tools"] = names
        engine = await manager._build_engine(state, "run-s")
        assert engine.config.discovered_tools == (("BoardAdd",) if names else ())
    finally:
        await manager.close()
