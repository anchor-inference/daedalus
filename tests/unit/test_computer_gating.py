"""When the computer tools exist at all, when a session is offered them, and how their asks reach the operator.

Three gates, from the widest to the narrowest: the installation (native, and told of a desktop daemon's
run directory) decides whether the tools are registered; the daemon being up and the session's model
taking images decide whether a run is offered them; and a sensitive action is asked about through
``computer_gate``, which publishes the same ``permission.pending`` the browser's gate does.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.computer.agent import ComputerAsk
from daedalus.config import Settings
from daedalus.host import capabilities
from daedalus.host.policy import ASK, Decision
from daedalus.host.session_runner import SessionManager
from daedalus.tools import TOOL_GROUPS, discover_tools


def _settings(tmp_path: Path, **over: Any) -> Settings:
    values: dict[str, Any] = {"state_dir": tmp_path / "state", "workspaces_dir": tmp_path / "workspaces", "telegram_bot_token": "", "owner_user_id": 0}
    return Settings(_env_file=None, **{**values, **over})  # type: ignore[arg-type, call-arg]


@pytest.mark.parametrize(("native", "run_dir", "configured"), [
    (True, "deskd/run", True),
    (True, None, False),
    (False, "deskd/run", False),  # docker mode never offers computer use, whatever it is told
    (False, None, False),
])
def test_the_tools_exist_only_in_a_native_install_told_of_a_daemon(tmp_path: Path, native: bool, run_dir: str | None, configured: bool) -> None:
    settings = _settings(tmp_path, native=native, computer_host_dir=tmp_path / run_dir if run_dir else None)
    resolved = capabilities.resolve_computer(settings)
    assert resolved.configured is configured
    assert resolved.as_dict()["configured"] is configured


def test_the_group_is_lazy_and_holds_exactly_the_tools_the_capability_names() -> None:
    assert TOOL_GROUPS["computer"].load == "lazy"
    description = TOOL_GROUPS["computer"].description
    assert "Browser tools" in description and "Exec" in description
    grouped = {tool.name for tool in discover_tools() if getattr(tool, "tool_group", "") == "computer"}
    assert grouped == set(capabilities.COMPUTER_TOOLS)


def _manager(tmp_path: Path, *, native: bool = True, live: bool = True) -> Any:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    if live:
        (run_dir / "endpoint").write_text("unix:deskd.sock")
        (run_dir / "token").write_text("0" * 64)
    settings = SimpleNamespace(computer_host_dir=run_dir)
    caps = SimpleNamespace(computer=capabilities.Computer(native=native, run_dir=str(run_dir)))
    return SimpleNamespace(capabilities=caps, settings=settings, _computer_live=(0.0, False))


@pytest.mark.parametrize(("native", "live", "sight", "offered"), [
    (True, True, ("claude-opus-5-5", True), True),
    (True, True, ("llama-3", True), True),           # images and no profile: offered, with the accuracy note
    (True, True, ("claude-opus-5-5", False), False),  # its Images switch is off: nothing to see the screen with
    (True, True, None, False),                        # no run built yet
    (True, False, ("claude-opus-5-5", True), False),  # the daemon is not up
    (False, True, ("claude-opus-5-5", True), False),
])
def test_a_session_is_offered_the_tools_while_the_daemon_is_up_and_its_model_sees(tmp_path: Path, native: bool, live: bool, sight: Any, offered: bool) -> None:
    manager = _manager(tmp_path, native=native, live=live)
    state = SimpleNamespace(computer_sight=sight)
    assert SessionManager.computer_offered(manager, state) is offered  # type: ignore[arg-type]


def test_the_daemons_coming_is_seen_within_a_few_seconds(tmp_path: Path) -> None:
    manager = _manager(tmp_path, live=False)
    state = SimpleNamespace(computer_sight=("claude-opus-5-5", True))
    assert SessionManager.computer_offered(manager, state) is False  # type: ignore[arg-type]
    (tmp_path / "run" / "endpoint").write_text("unix:deskd.sock")
    (tmp_path / "run" / "token").write_text("0" * 64)
    assert SessionManager.computer_offered(manager, state) is False, "reused for a moment, not read on every policy call"  # type: ignore[arg-type]
    manager._computer_live = (time.monotonic() - 10, False)
    assert SessionManager.computer_offered(manager, state) is True  # type: ignore[arg-type]


class _Gatekeeper:
    """The parts of the session manager the gate uses, with the real gate and payload bound to them."""

    def __init__(self, metadata: dict[str, Any]) -> None:
        session = SimpleNamespace(id="s1", title="Tidy the downloads", metadata=metadata)
        self.state = SimpleNamespace(session=session, metadata=metadata)
        self._states = {"s1": self.state}
        self.settings = SimpleNamespace(telegram_bot_token="")
        self.redactor = SimpleNamespace(redact=lambda text: text)
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.consumed: list[str] = []

    def _publish_soon(self, state: Any, event_type: str, payload: dict[str, Any]) -> None:
        self.published.append((event_type, payload))

    def _consume_grant(self, state: Any, key: str) -> None:
        self.consumed.append(key)

    def _permission_payload(self, state: Any, decision: Decision, refused: dict[str, Any]) -> dict[str, Any]:
        return SessionManager._permission_payload(self, state, decision, refused)  # type: ignore[arg-type]

    def _telegram_delivers(self, state: Any) -> bool:
        return False

    def gate(self, ask: ComputerAsk) -> tuple[bool, str]:
        return SessionManager.computer_gate(self, "s1", ask)  # type: ignore[arg-type]


def _ask() -> ComputerAsk:
    decision = Decision(ASK, "this would delete or cancel something in Files — Downloads", "computer.sensitive", key="abcdef012345")
    return ComputerAsk(decision, "Computer", "key", ["destroy"], "Files — Downloads", "keys shift+Delete", "Downloads", 0, "Files", "Downloads")


def test_a_sensitive_action_is_announced_once_to_the_operator_in_the_app() -> None:
    keeper = _Gatekeeper({})
    allowed, why = keeper.gate(_ask())
    assert not allowed and "Approval key: abcdef012345" in why
    [(event, payload)] = keeper.published
    assert event == "permission.pending"
    assert payload["request_id"] == "abcdef012345" and payload["kind"] == "policy"
    assert (payload["risk"], payload["quick"], payload["routed_to"]) == ("elevated", False, "operator")
    assert payload["computer"] == {"kinds": ["destroy"], "action": "key", "app": "Files", "window": "Downloads", "element": "keys shift+Delete", "thumbnail": ""}
    assert "key in Files — Downloads" in keeper.state.metadata["policy_pending"]["abcdef012345"]["text"]
    # The same refused action again is the same request: not announced a second time.
    assert not keeper.gate(_ask())[0]
    assert len(keeper.published) == 1


def test_a_granted_action_passes_once() -> None:
    keeper = _Gatekeeper({"policy_grants": {"abcdef012345": time.time() + 600}})
    assert keeper.gate(_ask()) == (True, "")
    assert keeper.consumed == ["abcdef012345"] and keeper.published == []


def test_an_expired_grant_is_no_grant() -> None:
    keeper = _Gatekeeper({"policy_grants": {"abcdef012345": time.time() - 1}})
    allowed, _ = keeper.gate(_ask())
    assert not allowed and keeper.consumed == []
