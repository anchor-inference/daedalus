"""What each command-line agent offers, as data: the channels an adapter uses and how far to trust them.

The table is code rather than prose so that three readers agree on it: the adapters, which choose a
channel by it; the delivery pipeline, which decides by it what a message for now becomes for a
harness; and the app and the orchestrator, which show the operator and the model what a harness can
and cannot do (the Harnesses screen's "status channel" column, the ``Harnesses`` tool). A value that
is not yet proven against the real CLI is marked in its field's comment; the adapter that proves it
corrects it here, in the same change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Steer = Literal["native", "tui_queue", "degrade_to_queue", "cancel_and_send"]
"""What a message sent while the worker is busy does.

``native``: the CLI takes it into the running turn through its own channel. ``tui_queue``: typed into
the busy TUI, which holds it and injects it between tool calls. ``degrade_to_queue``: the CLI has no
such thing, so it waits for the turn to end. ``cancel_and_send``: Enter while busy cancels the turn,
so a steer is an interrupt followed by the message. The last two are reported back as
``degraded_to`` on the receipt, so the sender is never surprised by what happened."""
Permissions = Literal["structured", "hook_then_keys", "keys", "none"]
"""How a permission request is answered: over a structured channel; noticed by a hook and answered
with keystrokes in the dialog; noticed on screen and answered with keystrokes; or never asked."""
Questions = Literal["structured", "hook", "none"]
TeamTools = Literal["mcp", "extension", "none"]
FirstPrompt = Literal["argv", "channel"]
Interrupt = Literal["keys", "structured"]


@dataclass(frozen=True, slots=True)
class PasteRules:
    """How a harness that is written to by paste treats a paste. Only those harnesses have one."""

    collapses_over: int | None
    """Characters past which the composer shows a "[Pasted text #…" marker instead of the text, so
    composer verification looks for the marker. ``None``: it never collapses, or it is not yet known;
    the verification then looks for the text's tail only."""
    burst_guard_ms: int
    """The least time between the last pasted byte and Enter. Some TUIs read a fast burst of input
    as a paste and swallow an Enter that arrives inside it."""
    bracketed: bool = True
    """Wrap the paste in bracketed-paste markers when the terminal has mode 2004 on."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    harness: str
    label: str
    """The product's name as the operator knows it."""
    status_channel: str
    """A code for where status comes from: ``hooks``, ``app_server``, ``sse``, ``extension``, ``files``."""
    status_channel_label: str
    """The same in a few words for the Harnesses screen. English on purpose: the app maps the code,
    and this is what the orchestrator reads."""
    steer: Steer
    permissions: Permissions
    questions: Questions
    team_tools: TeamTools
    first_prompt: FirstPrompt
    interrupt: Interrupt
    paste: PasteRules | None
    """``None`` when messages go through a structured channel and the composer is never typed into."""
    companion: bool
    """A second process runs beside the TUI (a server the TUI talks to) in a terminal of its own."""
    pointer_dir_flag: str
    """The argument that lets the CLI read the launch directory without asking, for messages sent by
    pointer; empty when the CLI reads there anyway."""
    tested_versions: tuple[str, str]
    """``[low, high)``: the versions the adapter was verified against. Outside it the harness still
    runs, marked as untested; the self-check decides whether staff may launch."""
    supported_major: int
    """A different major version is refused: its interface is a different program."""
    autoupdate_off: tuple[tuple[str, str], ...] = field(default=())
    """Environment that switches the CLI's own updater off, so a version changes only from the
    Harnesses screen and never under a working staff member. Empty when it is done with a flag."""
    standing_rules: bool = False
    """The host keeps the operator's "Always" for an MCP tool as an allow rule of the CLI, written
    into every launch, and a rule may cover every tool of one MCP server. Claude Code alone: Codex
    routes an MCP tool's approval as an elicitation, which has no standing answer, and OpenCode's and
    Grok's "always" knows no server."""


CAPABILITIES: dict[str, Capabilities] = {
    "claude": Capabilities(
        harness="claude",
        label="Claude Code",
        status_channel="hooks",
        status_channel_label="hooks per launch",
        steer="tui_queue",
        permissions="hook_then_keys",
        questions="hook",
        team_tools="mcp",
        first_prompt="argv",
        interrupt="keys",
        # The collapse threshold and the burst window are the adapter's to measure against the real
        # TUI; these are conservative starting values, not observations.
        paste=PasteRules(collapses_over=800, burst_guard_ms=400),
        companion=False,
        pointer_dir_flag="--add-dir",
        tested_versions=("2.1.281", "2.2.0"),
        supported_major=2,
        standing_rules=True,
        autoupdate_off=(("DISABLE_AUTOUPDATER", "1"),),
    ),
    "codex": Capabilities(
        harness="codex",
        label="Codex",
        status_channel="app_server",
        status_channel_label="app-server notifications",
        steer="native",
        permissions="structured",
        questions="structured",
        team_tools="mcp",
        first_prompt="channel",
        interrupt="structured",
        paste=None,
        companion=True,
        pointer_dir_flag="",
        tested_versions=("0.155.1", "0.157.0"),
        supported_major=0,
        # Codex's update check is switched off by a config override on its argv, not by environment.
        autoupdate_off=(),
    ),
    "opencode": Capabilities(
        harness="opencode",
        label="OpenCode",
        status_channel="sse",
        status_channel_label="server events",
        steer="degrade_to_queue",
        permissions="structured",
        questions="structured",
        team_tools="mcp",
        first_prompt="channel",
        interrupt="structured",
        paste=None,
        companion=False,
        pointer_dir_flag="",
        tested_versions=("1.18.23", "1.19.0"),
        supported_major=1,
        autoupdate_off=(("OPENCODE_DISABLE_AUTOUPDATE", "1"),),
    ),
    "pi": Capabilities(
        harness="pi",
        label="pi",
        status_channel="extension",
        status_channel_label="bridge extension",
        steer="native",
        permissions="none",
        questions="none",
        team_tools="extension",
        first_prompt="argv",
        interrupt="structured",
        paste=None,
        companion=False,
        pointer_dir_flag="",
        tested_versions=("0.84.2", "0.88.0"),
        supported_major=0,
        autoupdate_off=(("PI_SKIP_VERSION_CHECK", "1"),),
    ),
    "grok": Capabilities(
        harness="grok",
        label="Grok Build",
        status_channel="hooks",
        status_channel_label="hooks per launch (agent file)",
        # Measured (1.0.41): Enter while busy queues the message after the turn, and a queued
        # message's hook fires only when it starts; so a steer interrupts first, then sends.
        steer="cancel_and_send",
        permissions="hook_then_keys",
        # Grok's own question tool is removed at launch: a staff member asks its orchestrator.
        questions="none",
        team_tools="mcp",
        first_prompt="argv",
        interrupt="keys",
        # Pasting four lines or more shows "[Pasted: N lines]"; a single long line is shown as it is.
        paste=PasteRules(collapses_over=None, burst_guard_ms=400),
        companion=False,
        pointer_dir_flag="",
        tested_versions=("1.0.40", "1.1.0"),
        supported_major=1,
        autoupdate_off=(("GROK_DISABLE_AUTOUPDATER", "1"),),
    ),
    "cursor": Capabilities(
        harness="cursor",
        label="Cursor Agent",
        status_channel="acp",
        status_channel_label="ACP events per launch",
        steer="degrade_to_queue",
        permissions="structured",
        questions="structured",
        team_tools="mcp",
        first_prompt="channel",
        interrupt="structured",
        paste=None,
        companion=False,
        pointer_dir_flag="",
        tested_versions=("2026.10.01", "2026.11.01"),
        supported_major=2026,
    ),
}
"""One entry per command-line harness. Daedalus staff are not here: they run in this process and have
none of these channels."""

MODE_MEANINGS: dict[str, dict[str, str]] = {
    "codex": {
        "read-only": "reads only: no file writes and no network, whatever the task allows",
        "workspace-write": "writes in its own folder; the network stays off",
        "danger-full-access": "writes anywhere and uses the network, and never asks",
    },
    "claude": {
        "default": "asks before each edit and command",
        "manual": "asks before each edit and command",
        "acceptEdits": "edits files without asking; asks before commands",
        "auto": "decides by itself what to ask about",
        "plan": "plans only: no edits and no commands",
        "dontAsk": "never asks: anything not allowed beforehand is refused",
        "bypassPermissions": "does anything without asking",
    },
}
"""What each permission mode of a command-line agent lets a member do, in the words the orchestrator
chooses by. An orchestrator hired a reviewer "read-only" for a check the operator had said to fix as
well, and a test message the operator then allowed could not be sent: a Codex read-only sandbox has
neither writes nor a network, which no mode's name says."""
MODE_MEANINGS["grok"] = {k: v for k, v in MODE_MEANINGS["claude"].items() if k != "manual"}
MODE_MEANINGS["cursor"] = {
    "ask": "answers questions without edits or commands",
    "plan": "plans without edits or commands",
    "agent": "full tool access in the selected folder; Cursor can edit without asking",
}
RESTRICTIVE_MODES = {"codex": frozenset({"read-only"}), "claude": frozenset({"plan"}), "grok": frozenset({"plan"}), "cursor": frozenset({"ask", "plan"})}
"""The modes in which a member cannot change anything: work that the operator allowed to fix things
cannot be done in them."""


_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first ``major.minor[.patch]`` in a CLI's version output, which is rarely just the number
    (``grok 1.0.40 (abc123)``, ``codex-cli 0.155.1``)."""
    found = _VERSION.search(text or "")
    if found is None:
        return None
    return int(found[1]), int(found[2]), int(found[3] or 0)


def capabilities(harness: str) -> Capabilities:
    try:
        return CAPABILITIES[harness]
    except KeyError:
        raise KeyError(f"no command-line harness is called {harness!r}") from None


def version_tested(caps: Capabilities, version: str) -> bool:
    found, low, high = parse_version(version), parse_version(caps.tested_versions[0]), parse_version(caps.tested_versions[1])
    return found is not None and low is not None and high is not None and low <= found < high


def version_supported(caps: Capabilities, version: str) -> bool:
    found = parse_version(version)
    return found is not None and found[0] == caps.supported_major


__all__ = [
    "CAPABILITIES",
    "MODE_MEANINGS",
    "RESTRICTIVE_MODES",
    "Capabilities",
    "FirstPrompt",
    "Interrupt",
    "PasteRules",
    "Permissions",
    "Questions",
    "Steer",
    "TeamTools",
    "capabilities",
    "parse_version",
    "version_supported",
    "version_tested",
]
