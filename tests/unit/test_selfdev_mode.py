"""Self-development as a capability: off, local or server, and what each one leaves behind.

An installation that cannot change its own code should not carry a single trace of the machinery
for it — not a tool, not a route, not a sentence in the prompt, not an entry in the navigation. The
tests here pin the three modes at the places where the difference is visible, and one of them is
general on purpose: whatever the mode, the prompt may not name a tool the session does not have.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from daedalus.app import Application
from daedalus.bench.manifest import DEFAULT_TOOLS_OFF
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import enabled
from daedalus.extensions.api import build_app
from daedalus.host import capabilities, prompts
from daedalus.host.policy import Policy
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools import discover_tools
from daedalus.tools import skill as skill_tool
from tests.conftest import rebuilder_at

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_TOOLS = ("SelfWorkspace", "SelfPropose", "SelfApply", "SelfRebuild", "SelfRollback")
SERVER_TOOLS = ("SelfWorkspace", "SelfPropose", "SelfRebuild", "SelfRollback")


def _settings(tmp_path: Path, **over: object) -> Settings:
    values: dict[str, object] = {
        "state_dir": tmp_path / "state",
        "workspaces_dir": tmp_path / "workspaces",
        "bot_repo_dir": REPO_ROOT,
        "core_repo_dir": REPO_ROOT.parent / "protocore-exp",
        "telegram_bot_token": "",
        "owner_user_id": 0,
        "api_port": 0,
        "rebuild_trigger_dir": rebuilder_at(tmp_path),
    }
    return Settings(_env_file=None, **{**values, **over})  # type: ignore[arg-type, call-arg]


def _config(mode: str) -> RuntimeConfig:
    config = RuntimeConfig()
    config.self_change.mode = mode  # type: ignore[assignment]
    return config


# -- the resolution table -----------------------------------------------------------------


def test_auto_resolves_from_what_is_really_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The table of E.2: server needs the token, the remotes and a rebuild channel; local needs a checkout."""
    server = capabilities.resolve_selfdev(_settings(tmp_path, github_token="stub"), _config("auto"))
    assert server.mode == "server" and server.missing == []
    assert any("GitHub token is configured" in r for r in server.reasons)

    # The same installation without a token cannot open a pull request, but can still edit its checkout.
    local = capabilities.resolve_selfdev(_settings(tmp_path), _config("auto"))
    assert local.mode == "local"
    assert any("no GitHub token" in r for r in local.reasons)

    # Nothing to edit: the repositories are not checkouts at all.
    empty = tmp_path / "nowhere"
    empty.mkdir()
    off = capabilities.resolve_selfdev(_settings(tmp_path, bot_repo_dir=empty, core_repo_dir=empty, github_token="stub"), _config("auto"))
    assert off.mode == "off"
    assert any("not both writable git checkouts" in r for r in off.reasons)

    # A remote is not enough on its own: with no way to deliver a build, server mode is not honest.
    monkeypatch.setattr(capabilities, "rebuild_channel", lambda _settings: "")
    no_rebuild = capabilities.resolve_selfdev(_settings(tmp_path, github_token="stub"), _config("auto"))
    assert no_rebuild.mode == "local"


def test_a_rebuilder_in_the_compose_file_is_not_a_rebuilder_that_is_running(tmp_path: Path) -> None:
    """The shipped compose file always names the rebuilder — it is behind a profile that is off by
    default — so the probe asks the trigger directory whether anything is answering there instead."""
    settings = _settings(tmp_path, github_token="stub")
    assert (settings.bot_repo_dir / "deploy" / "compose.yaml").read_text(encoding="utf-8").count("\n  rebuilder:") == 1
    assert capabilities.rebuild_channel(settings) == "the compose rebuilder"

    # Nothing behind the volume: an empty directory is what a stopped rebuilder leaves.
    stopped = tmp_path / "stopped"
    stopped.mkdir()
    assert capabilities.rebuild_channel(_settings(tmp_path, github_token="stub", rebuild_trigger_dir=stopped)) == ""
    assert capabilities.resolve_selfdev(_settings(tmp_path, github_token="stub", rebuild_trigger_dir=stopped), _config("auto")).mode == "local"

    # A heartbeat old enough to be from a rebuilder that has since been stopped does not count either.
    stale = rebuilder_at(tmp_path / "stale")
    old_enough = time.time() - capabilities.REBUILDER_HEARTBEAT_SECONDS - 60
    os.utime(stale / capabilities.REBUILDER_HEARTBEAT, (old_enough, old_enough))
    assert capabilities.rebuild_channel(_settings(tmp_path, github_token="stub", rebuild_trigger_dir=stale)) == ""


def test_the_resolved_mode_is_published_for_the_supervisor(tmp_path: Path) -> None:
    """One resolution: the supervisor reads this file rather than probing the prerequisites again."""
    settings = _settings(tmp_path, github_token="stub")
    resolved = capabilities.resolve(settings, _config("auto"))
    capabilities.publish(resolved, settings.state_dir)
    written = json.loads((settings.state_dir / capabilities.PUBLISHED_FILE).read_text(encoding="utf-8"))
    assert written["selfdev"]["mode"] == resolved.selfdev.mode == "server"
    assert written["at"]


def test_an_explicit_mode_wins_and_says_what_it_is_missing(tmp_path: Path) -> None:
    empty = tmp_path / "nowhere"
    empty.mkdir()
    forced = capabilities.resolve_selfdev(_settings(tmp_path, bot_repo_dir=empty, core_repo_dir=empty), _config("server"))
    assert forced.mode == "server", "the operator's word stands; the doctor is where the disagreement shows"
    assert forced.missing and any("GitHub token" in m for m in forced.missing)
    assert forced.reasons[0] == "the operator set selfdev.mode = server"

    off = capabilities.resolve_selfdev(_settings(tmp_path, github_token="stub"), _config("off"))
    assert off.mode == "off" and off.missing == [] and off.tools == frozenset()


def test_each_mode_registers_exactly_the_tools_it_can_honour() -> None:
    assert capabilities.SELFDEV_TOOLS["off"] == frozenset()
    assert capabilities.SELFDEV_TOOLS["local"] == {"SelfWorkspace", "SelfApply"}
    assert capabilities.SELFDEV_TOOLS["server"] == set(SERVER_TOOLS)
    assert capabilities.ALL_SELFDEV_TOOLS == set(SELF_TOOLS) == {t.name for t in discover_tools() if t.name.startswith("Self")}
    off = capabilities.SelfDev(mode="off", configured="off")
    assert off.disabled_tools == set(SELF_TOOLS)
    assert capabilities.SelfDev(mode="local", configured="local").disabled_tools == {"SelfPropose", "SelfRebuild", "SelfRollback"}
    assert capabilities.SelfDev(mode="server", configured="server").disabled_tools == {"SelfApply"}


# -- what the modes do to the running installation ----------------------------------------


async def _manager(tmp_path: Path, db: Database, mode: str) -> SessionManager:
    manager = SessionManager(_settings(tmp_path, github_token="stub"), _config(mode), db=db)
    await manager.start()
    return manager


@pytest.mark.parametrize(("mode", "present"), [("off", ()), ("local", ("SelfWorkspace", "SelfApply")), ("server", SERVER_TOOLS)])
async def test_the_tool_registry_follows_the_mode(tmp_path: Path, db: Database, mode: str, present: tuple[str, ...]) -> None:
    manager = await _manager(tmp_path, db, mode)
    try:
        names = {t.name for t in manager.tools.list_all()}
        assert names & set(SELF_TOOLS) == set(present)
    finally:
        await manager.close()


@pytest.mark.parametrize("mode", ["off", "local", "server"])
async def test_the_prompt_never_names_a_tool_the_session_does_not_have(tmp_path: Path, db: Database, mode: str) -> None:
    """The failure this guards against is silent: the model calls a name the prompt taught it and gets nothing."""
    manager = await _manager(tmp_path, db, mode)
    try:
        registered = {t.name for t in manager.tools.list_all()}
        prompt = "\n".join(
            (
                prompts.PERSONA,
                prompts.rules_section(""),
                *prompts.tool_sections(registered),
                # The rules the core writes beside a group's tools, for every group that has any here.
                *(group.instructions for group in manager.tools.tool_groups() if any(getattr(t, "tool_group", "") == group.name for t in manager.tools.list_all())),
                prompts.environment_section(
                    workspace=tmp_path,
                    bot_repo=REPO_ROOT,
                    core_repo=REPO_ROOT.parent / "protocore-exp",
                    model="m",
                    github_org="an-org",
                    selfdev_mode=mode,
                ),
            )
        )
        for name in sorted({t.name for t in discover_tools()} - registered):
            assert name not in prompt, f"the prompt names {name}, which this installation does not register"
        if mode == "off":
            assert "Self-development" not in prompt
        if mode == "local":
            assert "after a restart the operator triggers" in prompt and "SelfWorkspace" in prompt
    finally:
        await manager.close()


@pytest.mark.parametrize("mode", ["off", "local", "server"])
async def test_nothing_the_session_reads_names_a_self_tool_it_does_not_have(tmp_path: Path, db: Database, mode: str) -> None:
    """The prompt was swept for this; the tool descriptions, the tool results and the shipped skills
    were not. They are read by the same model in the same session, and a name in any of them is an
    invitation the model accepts and a failure it cannot understand."""
    manager = await _manager(tmp_path, db, mode)
    try:
        registered = {t.name for t in manager.tools.list_all()}
        absent = sorted(set(SELF_TOOLS) - registered)
        texts: dict[str, str] = {f"tool {t.name}": t.definition.description or "" for t in manager.tools.list_all()}
        for path in sorted((REPO_ROOT / "skills").rglob("SKILL.md")):
            if path.parent.name == "self-develop":
                continue  # it is the reference for all three modes and names each one's tools on purpose
            texts[f"skill {path.parent.name}"] = path.read_text(encoding="utf-8")
        texts["the skill draft's instructions"] = skill_tool._how_to_ship(manager, "a-slug")
        texts["the bench manifest"] = " ".join(DEFAULT_TOOLS_OFF)
        for where, text in texts.items():
            for name in absent:
                if where == "the bench manifest":
                    continue  # a list of names to withhold is the one place they all belong
                assert name not in text, f"{where} names {name}, which this installation does not register"
        # And a benchmark run withholds every one of them, whatever the mode: a bench on a local
        # installation would otherwise be able to apply a change to the installation it measures.
        assert set(SELF_TOOLS) <= set(DEFAULT_TOOLS_OFF)
    finally:
        await manager.close()


async def test_the_extension_is_not_installed_when_the_mode_is_off(tmp_path: Path, db: Database) -> None:
    for mode, expected in (("off", False), ("local", True), ("server", True)):
        manager = await _manager(tmp_path, db, mode)
        try:
            app = SimpleNamespace(manager=manager)
            assert ("daedalus.extensions.selfdev" in enabled(app)) is expected, mode  # type: ignore[arg-type]
        finally:
            await manager.close()


async def test_a_local_installation_keeps_only_the_hook_behind_a_registered_tool(tmp_path: Path) -> None:
    application = Application(_settings(tmp_path, github_token=""))
    application.config.self_change.mode = "local"
    await application.start()
    try:
        assert application.manager is not None
        assert application.manager.capabilities.selfdev.mode == "local"
        hooks = application.manager.service_hooks
        assert "self_workspace" in hooks
        assert "self_propose" not in hooks and "self_rebuild" not in hooks and "self_rollback" not in hooks
    finally:
        await application.shutdown()


# -- the API ------------------------------------------------------------------------------


def _api_app(settings: Settings, config: RuntimeConfig, db: Database) -> SimpleNamespace:
    caps = capabilities.resolve(settings, config)
    manager = SimpleNamespace(capabilities=caps, providers=SimpleNamespace(available=lambda: []))
    return SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)


@pytest.mark.parametrize("mode", ["off", "local", "server"])
async def test_the_proposals_api_exists_only_where_changes_are_proposed(tmp_path: Path, db: Database, mode: str) -> None:
    settings = _settings(tmp_path, github_token="stub")
    api = build_app(_api_app(settings, _config(mode), db), "tok")  # type: ignore[arg-type]
    headers = {"X-Daedalus-Token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        listing = await client.get("/api/proposals", headers=headers)
        diff = await client.get("/api/proposals/x/diff", headers=headers)
        decide = await client.post("/api/proposals/x/decide", headers=headers, json={"decision": "approve"})
    if mode == "off":
        for response in (listing, diff, decide):
            assert response.status_code == 404 and "self-development is off" in response.json()["detail"]
        return
    assert listing.status_code == 200 and listing.json() == []
    # The routes are there; they answer about the proposal, which is the part that does not exist.
    assert diff.status_code == 404 and diff.json()["detail"] == "no such proposal"
    assert decide.status_code == 503


async def test_capabilities_report_the_mode_and_the_reasons(tmp_path: Path, db: Database) -> None:
    settings = _settings(tmp_path)
    api = build_app(_api_app(settings, _config("auto"), db), "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.get("/api/capabilities")).status_code == 401  # it says what this installation is; not to anyone
        body = (await client.get("/api/capabilities", headers={"X-Daedalus-Token": "tok"})).json()
    assert body["selfdev"]["mode"] == "local" and body["selfdev"]["configured"] == "auto"
    assert body["selfdev"]["tools"] == ["SelfApply", "SelfWorkspace"]
    assert any("no GitHub token" in reason for reason in body["selfdev"]["reasons"])


# -- the doctor and the policy ------------------------------------------------------------


async def test_the_doctor_skips_the_github_checks_off_and_local(tmp_path: Path, db: Database) -> None:
    from daedalus.doctor import DoctorContext, run_checks

    names: dict[str, set[str]] = {}
    for mode in ("off", "local", "server"):
        settings = _settings(tmp_path, github_token="stub")
        config = _config(mode)
        ctx = DoctorContext(settings=settings, config=config, db=db, manager=SimpleNamespace(capabilities=capabilities.resolve(settings, config)))
        names[mode] = {c.name for c in await run_checks(ctx)}
    assert not any(n.endswith("push access") for n in names["off"] | names["local"])
    assert {"bot repo push access", "core repo push access"} <= names["server"]
    assert "bot repo" not in names["off"] and "bot repo" in names["local"]
    assert "supervisor" not in names["off"]
    assert "self-development" in names["off"] & names["local"] & names["server"]


async def test_the_doctor_warns_when_an_explicit_mode_has_nothing_to_stand_on(tmp_path: Path, db: Database) -> None:
    from daedalus.doctor import DoctorContext, run_checks

    empty = tmp_path / "nowhere"
    empty.mkdir()
    settings = _settings(tmp_path, bot_repo_dir=empty, core_repo_dir=empty)
    ctx = DoctorContext(settings=settings, config=_config("server"), db=db)
    warning = next(c for c in await run_checks(ctx) if c.name == "self-development prerequisites")
    assert not warning.ok and "GitHub token" in warning.message and "auto" in warning.fix_hint


def test_pushing_from_the_operators_checkout_is_refused_in_every_mode_for_the_right_reason() -> None:
    for mode, phrase in (("server", "SelfPropose"), ("local", "apply after a restart"), ("off", "does not change its own code")):
        policy = Policy(operator_checkouts=[Path("/home/x/daedalus")], selfdev_mode=mode)
        decision = policy.evaluate("Exec", {"command": "git push", "cwd": "/home/x/daedalus"})
        assert decision.action == "deny" and phrase in decision.reason, mode
        if mode != "server":
            assert "SelfPropose" not in decision.reason
