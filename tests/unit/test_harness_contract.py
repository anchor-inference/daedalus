"""The capability table, the version rules and the adapter registry."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

import daedalus.harness as harness
from daedalus.config import HarnessConfig, RuntimeConfig
from daedalus.harness.capabilities import CAPABILITIES, capabilities, parse_version, version_supported, version_tested
from daedalus.harness.contract import (
    Answer,
    Catalog,
    CheckResult,
    Delivery,
    EnvironmentPort,
    HarnessAdapter,
    InstallInfo,
    Launch,
    LaunchPlan,
    LaunchSpec,
    LoginState,
    ReadyStep,
    ScreenClass,
    SendMode,
    StaffEvent,
    TerminalPort,
    Turn,
    UpdateResult,
)
from daedalus.stores.staff import CLI_HARNESSES, HARNESS_NAMES


def test_the_table_covers_exactly_the_command_line_harnesses_a_staff_member_can_have() -> None:
    assert set(CAPABILITIES) == set(CLI_HARNESSES)
    for name, caps in CAPABILITIES.items():
        assert caps.harness == name
        assert caps.label == HARNESS_NAMES[name]
        low, high = (parse_version(v) for v in caps.tested_versions)
        assert low is not None and high is not None and low < high
        assert low[0] == caps.supported_major
    # Only the harnesses whose messages are typed into the TUI say how a paste behaves.
    assert {name for name, caps in CAPABILITIES.items() if caps.paste is not None} == {"claude", "grok"}
    with pytest.raises(KeyError, match="unknown"):
        capabilities("unknown")


def test_steer_that_is_not_native_is_named() -> None:
    assert capabilities("opencode").steer == "degrade_to_queue"
    assert capabilities("grok").steer == "cancel_and_send"
    assert capabilities("codex").companion and not capabilities("claude").companion


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2.1.281 (Claude Code)", (2, 1, 281)),
        ("codex-cli 0.155.1", (0, 155, 1)),
        ("grok 1.0.40 (1a2b3c)", (1, 0, 40)),
        ("1.18", (1, 18, 0)),
        ("not a version", None),
        ("", None),
    ],
)
def test_versions_are_read_out_of_what_a_cli_prints(text: str, parsed: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == parsed


def test_tested_and_supported_versions() -> None:
    claude = capabilities("claude")
    assert version_tested(claude, "2.1.281") and version_tested(claude, "2.1.300")
    assert not version_tested(claude, "2.1.200") and not version_tested(claude, "2.2.0")
    assert version_supported(claude, "2.9.0") and not version_supported(claude, "3.0.0")
    opencode = capabilities("opencode")
    assert version_supported(opencode, "1.18.32") and not version_supported(opencode, "2.0.1")
    assert not version_tested(claude, "garbage") and not version_supported(claude, "garbage")


class StubAdapter:
    """The smallest thing that keeps the contract, to prove the protocol says what it means."""

    name = "pi"
    capabilities = CAPABILITIES["pi"]

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return InstallInfo(installed=True, path="/usr/bin/pi", version="0.84.2", method="npm")

    async def latest(self, env: EnvironmentPort) -> str:
        return "0.87.1"

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        return UpdateResult(ok=True, previous="0.84.2", version="0.87.1")

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        return CheckResult(ok=True, steps=(), version="0.87.1", duration_ms=1)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        return Catalog()

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        return LoginState("unknown")

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan:
        return LaunchPlan(argv=("pi",), env={}, cwd=spec.cwd, first_prompt=spec.first_prompt)

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return LaunchPlan(argv=("pi", "--session-id", ref), env={}, cwd=spec.cwd, session_ref=ref)

    def readiness(self, screen: str) -> ReadyStep:
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        return None

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        return None

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        for _ in ():
            yield _

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        return Delivery(message_id=message_id, state="written", via="bridge")

    async def interrupt(self, term: TerminalPort) -> None:
        return None

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        return False

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        return []

    async def stop(self, term: TerminalPort) -> None:
        return None

    def classify_screen(self, text: str) -> ScreenClass:
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        return False


def test_the_registry_takes_an_adapter_by_its_harness_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "ADAPTERS", {})
    assert isinstance(StubAdapter(), HarnessAdapter)
    with pytest.raises(harness.UnknownHarness, match="no adapter for 'pi'"):
        harness.adapter_for("pi")
    assert harness.register(StubAdapter) is StubAdapter
    assert harness.adapter_for("pi") is StubAdapter
    assert harness.register(StubAdapter) is StubAdapter  # registering twice is harmless

    class Another(StubAdapter):
        pass

    with pytest.raises(ValueError, match="already registered"):
        harness.register(Another)

    class Stranger(StubAdapter):
        name = "unknown"

    with pytest.raises(harness.UnknownHarness, match="capability table"):
        harness.register(Stranger)
    with pytest.raises(harness.UnknownHarness, match="no command-line harness is called 'daedalus'"):
        harness.adapter_for("daedalus")


def test_the_settings_keep_opencode_off_the_ports_others_were_promised() -> None:
    assert RuntimeConfig().harness.opencode_port_range == "18300-18399"
    for bad in ("8110-8115", "8000-8200", "8130-8140", "9000-8000", "80-90", "18300"):
        with pytest.raises(ValidationError):
            HarnessConfig(opencode_port_range=bad)
    assert HarnessConfig(opencode_port_range="8140-8199").opencode_port_range == "8140-8199"
