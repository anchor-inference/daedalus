"""What every fake command-line agent shares: the terminal user interface, the scripted model, the
faults, and the small clients (HTTP hooks, a stdio MCP client) the real CLIs have as well.

The interface imitates what makes the real TUIs hard to drive from outside, because that is what the
harness has to get right:

- It turns bracketed paste on (mode 2004) and reads a paste as one unit. A paste longer than the
  CLI's threshold shows in the composer as ``[Pasted text #N +L lines]`` rather than as the text, so
  a harness looking for its text on screen must accept the marker.
- An Enter that arrives within the CLI's burst window after a paste is swallowed, as a TUI that takes
  a fast burst for more of the paste does. Without bracketed paste a pasted line feed arrives as a
  carriage return and submits whatever is in the composer, as it does in the real TUIs.
- Numbered dialogs (folder trust, permission, question) take a digit to choose and confirm, or the
  arrows and Enter; the highlighted row is drawn with ``❯``. An Enter while a dialog is open answers
  the dialog, which is exactly the accident the harness must never cause; the fake records it.
- Esc interrupts a running turn. Ctrl+C twice, Ctrl+D on an empty composer, or ``/exit`` quit.

The scripted model reads the submitted prompt (or, for "Read the message in <path> …", the file it
points at). Steps are separated by ``;``:

``echo:<text>``
    reply with the text after a short delay.
``perm:<command>``
    ask permission to run a shell command (the command is never run), then say whether it ran.
``ask:<question>|<option>|<option>…``
    ask the operator a question with those options.
``fail:<kind>``
    end the turn with a failure of that kind (``rate_limit``, ``authentication_failed``, …).
``slow:<seconds>``
    work that long, showing a tool line about every half second.
``silent``
    work for a while and return to the composer without a single hook, event or record of the turn
    ending — the case the harness can only find by reading the screen.
``report:<kind>[:<note>]`` / ``askorch:<question>``
    call the team tools ``Report`` / ``AskOrchestrator`` through the launch's MCP server (or the pi
    bridge), where the CLI has one.

The harness manager's self-check prompt ("… Call the Report tool with kind checkpoint and note
self-check …") is understood as ``report:checkpoint:self-check;echo:ready``.

Anything else is answered with ``ok: <the first words>``.

Time: every delay is multiplied by ``FAKE_CLI_TIME_SCALE`` (default 1), so a test can run the
60-second idle notification in a fraction of a second.

Faults, a comma list in ``FAKE_CLI_FAULTS``:

``swallow_enter_once``
    the first Enter with text in the composer is lost.
``no_stop_hook``
    the turn's end is never signalled: no ``Stop`` hook (Claude, Grok), no ``session.idle``
    (OpenCode), no ``agent_end`` (pi), no ``turn/completed`` for any client but the TUI (Codex).
``late_permission_notification``
    the permission dialog is on screen well before any hook or event says so.
``dialog_during_paste``
    a notice opens as a paste arrives, swallows the paste, and stays until Enter or Esc dismisses it
    (a timed close would make "an Enter landed in it" depend on how loaded the machine is).
``exit_after:<turns>``
    the process dies with exit code 3 after that many turns, without saying goodbye — a crash.
``fail_at_start``
    the process prints an error and exits with code 1 before it draws anything or posts a hook, as a
    CLI refusing its model or its flags does: no transcript is ever written.
``no_2004``
    bracketed paste is never turned on.
``slow_ready:<ms>``
    the composer (and the ready signal) comes that much later.

Every fake appends what it did to the JSON-lines file ``FAKE_CLI_LOG`` when that is set: each
submission, swallowed Enter, dialog, answer, interrupt, the end of each turn (``turn_ended``, after
all of its hooks) and exit. That is how a test proves there was exactly one submission per message,
or that no Enter ever landed in a dialog — and how it knows a thing has happened, or will not, without
guessing a delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import sys
import termios
import time
import tty
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# -- time, faults, the log -------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCALE = float(os.environ.get("FAKE_CLI_TIME_SCALE") or 1.0)


def scaled(seconds: float) -> float:
    return seconds * SCALE


async def pause(seconds: float) -> None:
    await asyncio.sleep(scaled(seconds))


@dataclass
class Faults:
    swallow_enter_once: bool = False
    no_stop_hook: bool = False
    late_permission_notification: bool = False
    dialog_during_paste: bool = False
    exit_after: int = 0
    fail_at_start: bool = False
    no_2004: bool = False
    slow_ready_ms: int = 0

    @classmethod
    def from_env(cls, value: str | None = None) -> Faults:
        faults = cls()
        for item in (value if value is not None else os.environ.get("FAKE_CLI_FAULTS", "")).split(","):
            name, _, arg = item.strip().partition(":")
            if not name:
                continue
            if name == "exit_after":
                faults.exit_after = int(arg or 1)
            elif name == "slow_ready":
                faults.slow_ready_ms = int(arg or 1000)
            elif hasattr(faults, name) and isinstance(getattr(faults, name), bool):
                setattr(faults, name, True)
            else:
                raise SystemExit(f"unknown fault {name!r} in FAKE_CLI_FAULTS")
        return faults


class Log:
    """The fake's own account of what it did, for tests to count. Never read by the harness."""

    def __init__(self, cli: str) -> None:
        self.cli = cli
        self.path = os.environ.get("FAKE_CLI_LOG") or ""

    def __call__(self, what: str, /, **data: Any) -> None:
        if not self.path:
            return
        line = json.dumps({"at": now_iso(), "cli": self.cli, "pid": os.getpid(), "event": what, **data}, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """The entries a fake wrote to ``FAKE_CLI_LOG``, oldest first."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    # Only whole lines: a test polls this while a fake is still appending, and a long entry reaches
    # the file in more than one write, so the last line can be half there (a full suite on a loaded
    # machine failed on exactly that). It is read on the next poll, once its newline has landed.
    return [json.loads(line) for line in text.split("\n")[:-1] if line.strip()]


# -- state that survives the process: version, sign-in, trust ------------------------------------------


def state_dir(cli: str) -> Path:
    """Where a fake keeps what the real CLI keeps across runs (its version after an update, whether
    it is signed in). Under ``HOME``, so a test's temporary home isolates it."""
    path = Path(os.environ.get("HOME") or "/tmp") / ".fake-cli" / cli
    path.mkdir(parents=True, exist_ok=True)
    return path


def installed_version(cli: str, default: str) -> str:
    stored = state_dir(cli) / "version"
    if stored.exists():
        return stored.read_text().strip()
    return os.environ.get(f"FAKE_{cli.upper()}_VERSION") or default


def latest_version(cli: str, default: str) -> str:
    return os.environ.get(f"FAKE_{cli.upper()}_LATEST") or default


def set_version(cli: str, version: str) -> None:
    (state_dir(cli) / "version").write_text(version + "\n")


def logged_in(cli: str) -> bool:
    if os.environ.get(f"FAKE_{cli.upper()}_LOGGED_IN", "1") == "0":
        return False
    return not (state_dir(cli) / "logged_out").exists()


# -- arguments -------------------------------------------------------------------------------------


class Args:
    """A strict reader of the real CLI's argv: a flag the fake does not know is an error, so a launch
    plan that passes a wrong flag fails its test the way it would fail against the real CLI."""

    def __init__(self, cli: str, argv: list[str], *, flags: Mapping[str, int], aliases: Mapping[str, str] | None = None) -> None:
        self.values: dict[str, list[str]] = {}
        self.positional: list[str] = []
        aliases = aliases or {}
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--":
                self.positional.extend(argv[i + 1 :])
                break
            if arg.startswith("-") and arg != "-":
                name, eq, inline = arg.partition("=")
                name = aliases.get(name, name)
                if name not in flags:
                    usage_error(cli, f"unknown option '{arg}'")
                arity = flags[name]
                if arity == 0:
                    self.values.setdefault(name, []).append("")
                elif arity < 0:
                    # Variadic, as ``--add-dir <directories...>``: every following word that is not a
                    # flag is taken, the prompt included — the reason a launch puts ``--`` before it.
                    taken = [inline] if eq else []
                    while i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                        taken.append(argv[i + 1])
                        i += 1
                    if not taken:
                        usage_error(cli, f"option '{name}' argument missing")
                    self.values.setdefault(name, []).extend(taken)
                elif eq:
                    self.values.setdefault(name, []).append(inline)
                else:
                    if i + 1 >= len(argv):
                        usage_error(cli, f"option '{name}' argument missing")
                    self.values.setdefault(name, []).append(argv[i + 1])
                    i += 1
            else:
                self.positional.append(arg)
            i += 1

    def get(self, name: str, default: str = "") -> str:
        found = self.values.get(name)
        return found[-1] if found else default

    def all(self, name: str) -> list[str]:
        return list(self.values.get(name, []))

    def has(self, name: str) -> bool:
        return name in self.values


def usage_error(cli: str, message: str) -> None:
    sys.stderr.write(f"error: {message}\n")
    sys.stderr.write(f"(this is the fake {cli}; see tests/support/fake_cli)\n")
    raise SystemExit(2)


# -- the scripted model ------------------------------------------------------------------------------

_DIRECTIVE = re.compile(r"(?:^|\s)(echo|perm|mcpperm|ask|fail|slow|report|askorch|cat|mcp):(.*)$|(?:^|\s)(silent)\s*$", re.S)
"""``mcp:<server>:<tool>:<JSON arguments>`` calls any MCP server's tool, as a model calls one of Daedalus's
tool sets (``daedalus_browser``); the JSON may hold no ``;``, which ends a step. ``mcpperm:<server>:<tool>``
asks only the permission for such a tool, with no server behind it: what a standing rule is proved on."""
SELF_CHECK = "Call the Report tool with kind checkpoint and note self-check"
_POINTER = re.compile(r"Read the message in (\S+?)(?: and act on it)?\.?(?:\s|$)")


@dataclass(frozen=True)
class Step:
    kind: str
    arg: str = ""


def script_of(prompt: str) -> list[Step]:
    """The steps a prompt asks for. A pointer prompt is replaced by the file it points at, as a model
    would read it first."""
    pointer = _POINTER.search(prompt)
    if pointer:
        with contextlib.suppress(OSError):
            prompt = Path(pointer.group(1)).read_text(encoding="utf-8")
    if SELF_CHECK in " ".join(prompt.split()):
        # The harness manager's self-check prompt is plain words for a real model; the fake model
        # understands this one sentence the way a real one does.
        return [Step("report", "checkpoint:self-check"), Step("echo", "ready")]
    steps: list[Step] = []
    for segment in prompt.split(";"):
        found = _DIRECTIVE.search(segment.strip())
        if found is None:
            continue
        if found.group(3):
            steps.append(Step("silent"))
        else:
            steps.append(Step(found.group(1), found.group(2).strip()))
    if not steps:
        words = " ".join(prompt.split())
        steps.append(Step("echo", f"ok: {words[:60]}"))
    return steps


# -- terminal input ----------------------------------------------------------------------------------

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


@dataclass
class Key:
    name: str
    """``text``, ``paste``, ``enter``, ``esc``, ``backspace``, ``up``, ``down``, ``left``, ``right``,
    ``tab``, ``ctrl_c``, ``ctrl_d``, ``newline``."""
    text: str = ""


class InputParser:
    """Bytes from the terminal to keys. A lone ESC is a key only once nothing follows it for a moment,
    as every TUI decides; ``flush_escape`` is called when that moment passes."""

    def __init__(self) -> None:
        self.buffer = b""
        self.in_paste = False
        self.paste = b""

    def feed(self, data: bytes) -> list[Key]:
        self.buffer += data
        keys: list[Key] = []
        while self.buffer:
            if self.in_paste:
                end = self.buffer.find(PASTE_END)
                if end < 0:
                    # Keep a possible partial end marker for the next read.
                    keep = max(0, len(self.buffer) - len(PASTE_END) + 1)
                    self.paste += self.buffer[:keep]
                    self.buffer = self.buffer[keep:]
                    break
                self.paste += self.buffer[:end]
                self.buffer = self.buffer[end + len(PASTE_END) :]
                self.in_paste = False
                keys.append(Key("paste", self.paste.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")))
                self.paste = b""
                continue
            byte = self.buffer[0]
            if byte == 0x1B:
                if len(self.buffer) == 1:
                    break  # a lone ESC, or the start of a sequence still arriving
                if self.buffer.startswith(PASTE_START):
                    self.buffer = self.buffer[len(PASTE_START) :]
                    self.in_paste = True
                    continue
                if PASTE_START.startswith(self.buffer):
                    break
                if self.buffer[1:2] in (b"[", b"O"):
                    match = re.match(rb"\x1b[\[O][0-9;?]*[\x40-\x7e]", self.buffer)
                    if match is None:
                        if len(self.buffer) < 16:
                            break
                        self.buffer = self.buffer[1:]
                        keys.append(Key("esc"))
                        continue
                    seq = match.group(0)
                    self.buffer = self.buffer[len(seq) :]
                    name = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}.get(seq[-1:], "")
                    if name:
                        keys.append(Key(name))
                    continue
                self.buffer = self.buffer[1:]
                keys.append(Key("esc"))
                continue
            if byte == 0x0D:
                keys.append(Key("enter"))
                self.buffer = self.buffer[1:]
            elif byte == 0x0A:
                keys.append(Key("newline"))
                self.buffer = self.buffer[1:]
            elif byte in (0x7F, 0x08):
                keys.append(Key("backspace"))
                self.buffer = self.buffer[1:]
            elif byte == 0x09:
                keys.append(Key("tab"))
                self.buffer = self.buffer[1:]
            elif byte == 0x03:
                keys.append(Key("ctrl_c"))
                self.buffer = self.buffer[1:]
            elif byte == 0x04:
                keys.append(Key("ctrl_d"))
                self.buffer = self.buffer[1:]
            elif byte < 0x20:
                self.buffer = self.buffer[1:]
            else:
                end = 1
                while end < len(self.buffer) and self.buffer[end] >= 0x20 and self.buffer[end] not in (0x7F, 0x1B):
                    end += 1
                chunk = self.buffer[:end]
                # Never cut a UTF-8 character in two: keep an incomplete tail for the next read.
                try:
                    text = chunk.decode("utf-8")
                except UnicodeDecodeError as exc:
                    if exc.start == 0 and len(chunk) < 4 and end == len(self.buffer):
                        break
                    text = chunk[: exc.start].decode("utf-8") if exc.start else chunk.decode("utf-8", "replace")
                    end = exc.start or end
                self.buffer = self.buffer[end:]
                keys.append(Key("text", text))
        return keys

    def flush_escape(self) -> list[Key]:
        if self.buffer == b"\x1b" and not self.in_paste:
            self.buffer = b""
            return [Key("esc")]
        return []


# -- dialogs and the composer ------------------------------------------------------------------------


@dataclass
class Dialog:
    kind: str
    """``trust``, ``permission``, ``question``, ``notice``, ``rewind``, ``login``."""
    title: str
    body: list[str]
    options: list[str]
    selected: int = 0
    on_choose: Callable[[int], Any] | None = None
    on_escape: Callable[[], Any] | None = None
    digits: bool = True
    numbered: bool = True
    """Rows drawn as ``❯ 1. Yes``; Claude's trust and bypass questions draw ``❯ Yes`` and take no digit."""
    footer: str = "Enter to confirm · Esc to cancel"
    data: dict[str, Any] = field(default_factory=dict)
    rows: str = ""
    """``radio``: rows drawn ``┃  2 (●) Yes, proceed`` under a ``┃`` bar, as Grok Build draws them."""
    on_ctrl_c: Callable[[], Any] | None = None
    """Ctrl+C on the open dialog (Grok Build cancels the prompt, and the turn with it)."""
    opened_at: float = field(default_factory=time.monotonic)


@dataclass
class PastePart:
    number: int
    text: str
    style: str = "lines"

    @property
    def marker(self) -> str:
        if self.style == "grok":
            # As Grok Build draws it (measured): four lines or more.
            return f"[Pasted: {self.text.count(chr(10)) + 1} lines]"
        if self.style == "claude":
            # As Claude Code draws it (measured): the count is of line breaks, and a single line has none.
            breaks = self.text.count("\n")
            return f"[Pasted text #{self.number} +{breaks} lines]" if breaks else f"[Pasted text #{self.number}]"
        lines = self.text.count("\n") + 1
        return f"[Pasted text #{self.number} +{lines} lines]"


@dataclass
class Look:
    """How one CLI's screen looks: enough of its words for an adapter's ``classify_screen`` to have
    something real to recognise, and the paste rules it imitates."""

    name: str
    banner: str
    prompt: str = "> "
    idle_hint: str = "? for shortcuts"
    busy_hint: str = "esc to interrupt"
    busy_word: str = "Working…"
    alt_screen: bool = False
    collapse_chars: int = 800
    collapse_lines: int = 0
    """A paste over this many lines collapses as well (0: only the length counts)."""
    burst_guard_ms: int = 0
    marker_style: str = "lines"
    """How a collapsed paste is shown: ``lines`` (``+L lines``), ``claude`` or ``grok`` (see ``PastePart``)."""
    boxed: bool = False
    """The composer is drawn in a box, ``╭─╮`` / ``│ ❯ text │`` / ``╰─╯``, with the hint line under it
    (Grok Build) rather than between two rules."""


class Tui:
    """The screen and the keyboard of a fake. The CLI module supplies what a submitted prompt, a key
    while busy, or Esc do; this class supplies how they are drawn and read."""

    def __init__(self, look: Look, *, faults: Faults, log: Log) -> None:
        self.look = look
        self.faults = faults
        self.log = log
        self.lines: list[str] = []
        self.status = ""
        self.notice = ""
        self.dialog: Dialog | None = None
        self.parts: list[str | PastePart] = []
        self.reported: dict[str, str] = {}
        """A submitted prompt's text to the prompt as the CLI reports it in its hook and transcript,
        where that differs (Claude wraps a collapsed paste in tags)."""
        self.queued: list[str] = []
        self.pastes = 0
        self.last_paste_end = 0.0
        self.busy = False
        self.ready = False
        self.exiting = False
        self.parser = InputParser()
        self.submit: Callable[[str], Awaitable[None] | None] = lambda text: None
        self.ctrl_c_interrupts = False
        """Ctrl+C cancels a running turn as Esc does (Grok documents Ctrl+C)."""
        self.enter_broken = False
        """Every Enter vanishes: how a CLI behaves in an environment it misreads (pi under ``TMUX``)."""
        self.busy_enter: Callable[[str], Awaitable[None] | None] | None = None
        """What Enter does while a turn runs; ``None``: the composer keeps the text."""
        self.escape: Callable[[], Awaitable[None] | None] = lambda: None
        self.cancel: Callable[[], Awaitable[None] | None] | None = None
        """What Ctrl+C does to a running turn when ``ctrl_c_interrupts``; ``escape`` when unset."""
        self.quit: Callable[[int], Awaitable[None] | None] = lambda code: None
        self._render_pending = False
        self._escape_timer: asyncio.TimerHandle | None = None
        self._ctrl_c_at = 0.0
        self._swallowed_once = False
        self._fd_in = 0
        self._fd_out = 1
        self._saved: list[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- the terminal ------------------------------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if os.isatty(self._fd_in):
            # Launched as a session leader with the terminal open but not yet its controlling
            # terminal (the test double spawns without a fork in a threaded process); taking it makes
            # Ctrl+C-style signals and window-size changes reach this process as in a real terminal.
            with contextlib.suppress(OSError):
                fcntl.ioctl(self._fd_in, termios.TIOCSCTTY, 0)
            self._saved = termios.tcgetattr(self._fd_in)
            tty.setraw(self._fd_in)
        os.set_blocking(self._fd_in, False)
        loop.add_reader(self._fd_in, self._readable)
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGWINCH, self.render)
        prefix = ""
        if self.look.alt_screen:
            prefix += "\x1b[?1049h"
        if not self.faults.no_2004:
            prefix += "\x1b[?2004h"
        self.write(prefix + f"\x1b]0;{self.look.name}\x07")

    def restore(self) -> None:
        suffix = "\x1b[?2004l" + ("\x1b[?1049l" if self.look.alt_screen else "")
        with contextlib.suppress(OSError):
            self.write(suffix)
        if self._saved is not None:
            with contextlib.suppress(termios.error, OSError):
                termios.tcsetattr(self._fd_in, termios.TCSADRAIN, self._saved)
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(self._fd_in)

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        while data:
            try:
                written = os.write(self._fd_out, data)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            data = data[written:]

    def size(self) -> tuple[int, int]:
        with contextlib.suppress(OSError):
            size = os.get_terminal_size(self._fd_out)
            return size.columns, size.lines
        return 80, 24

    # -- drawing --------------------------------------------------------------------------------

    def say(self, *lines: str) -> None:
        for line in lines:
            self.lines.extend(line.split("\n"))
        del self.lines[:-500]
        self.render()

    def composer_text(self) -> str:
        return "".join(p if isinstance(p, str) else p.marker for p in self.parts)

    def composer_value(self) -> str:
        return "".join(p if isinstance(p, str) else p.text for p in self.parts)

    def composer_reported(self) -> str:
        """What the CLI reports for the composer's text once submitted. Claude Code (2.1.282,
        measured) wraps each collapsed paste in ``<pasted_content id="…">`` tags, blank line first."""
        if self.look.marker_style != "claude":
            return self.composer_value()
        out = []
        for part in self.parts:
            if isinstance(part, str):
                out.append(part)
            else:
                tag = os.urandom(2).hex()
                out.append(f'\n\n<pasted_content id="{tag}">\n{part.text}\n</pasted_content id="{tag}">\n')
        return "".join(out)

    def render(self) -> None:
        """Draw on the next turn of the loop, once, however many changes asked for it."""
        if self._render_pending:
            return
        self._render_pending = True
        asyncio.get_running_loop().call_soon(self._draw)

    def _draw(self) -> None:
        self._render_pending = False
        if self.exiting:
            return
        cols, rows = self.size()
        bottom: list[str] = []
        if self.dialog is not None:
            d = self.dialog
            bottom.append("─" * min(cols, 60))
            bottom.append(d.title)
            bottom.extend(d.body)
            for index, option in enumerate(d.options):
                mark = "❯" if index == d.selected else " "
                if d.rows == "radio":
                    bottom.append(f"  ┃  {index + 1} ({'●' if index == d.selected else '○'}) {option}")
                else:
                    bottom.append(f"{mark} {index + 1}. {option}" if d.numbered else f"{mark} {option}")
            bottom.append(d.footer)
        elif not self.ready:
            # No composer before the CLI is ready, as in the real TUIs: a harness (or a test) that
            # waits for the composer must never find one drawn before the CLI's channels are up.
            bottom.append(self.status or "Starting…")
        else:
            if self.status:
                bottom.append(self.status)
            for text in self.queued:
                bottom.append(f"  ↳ queued: {text[:cols - 14]}")
            if self.notice:
                bottom.append(self.notice)
            composer = self.composer_text().split("\n")
            if self.look.boxed:
                width = min(cols, 80) - 4
                bottom.append("  ╭" + "─" * width + "╮")
                bottom.append(f"  │ {self.look.prompt}{composer[0]}".ljust(width + 3) + "│")
                bottom.extend(f"  │   {line}".ljust(width + 3) + "│" for line in composer[1:])
                bottom.append("  ╰" + "─" * (width - len(self.look.name) - 3) + f" {self.look.name} ─╯")
            else:
                bottom.append("─" * min(cols, 60))
                bottom.append(self.look.prompt + composer[0])
                bottom.extend("  " + line for line in composer[1:])
                bottom.append("─" * min(cols, 60))
            bottom.append(f"  {self.look.busy_hint}" if self.busy else f"  {self.look.idle_hint}")
        room = max(0, rows - len(bottom) - 1)
        top = [self.look.banner] + (self.lines[-room + 1 :] if room > 1 else [])
        screen = (top + bottom)[-rows:]
        out = ["\x1b[H\x1b[2J"]
        for index, line in enumerate(screen):
            out.append(line[:cols])
            if index < len(screen) - 1:
                out.append("\r\n")
        self.write("".join(out))

    # -- input ------------------------------------------------------------------------------------

    def _readable(self) -> None:
        try:
            data = os.read(self._fd_in, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self._fd_in)
            self.spawn(self._call(self.quit, 0))
            return
        self._handle(self.parser.feed(data))
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None
        if self.parser.buffer == b"\x1b":
            self._escape_timer = asyncio.get_running_loop().call_later(0.05, lambda: self._handle(self.parser.flush_escape()))

    def spawn(self, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        async def run() -> Any:
            return await awaitable

        task = asyncio.ensure_future(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _call(fn: Callable[..., Awaitable[None] | None], *args: Any) -> None:
        result = fn(*args)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result

    def _handle(self, keys: list[Key]) -> None:
        for key in keys:
            self._key(key)
        self.render()

    def _key(self, key: Key) -> None:
        if not self.ready:
            if key.name == "ctrl_c":
                self.spawn(self._call(self.quit, 130))
            elif self.dialog is not None:
                self._dialog_key(key)
            return  # before the composer is drawn, typing goes nowhere, as in the real TUIs
        if self.dialog is not None:
            if key.name == "ctrl_c" and self.dialog.on_ctrl_c is not None:
                dialog, self.dialog = self.dialog, None
                self.log("dialog_cancelled", kind=dialog.kind)
                self.spawn(self._call(dialog.on_ctrl_c))
                return
            self._dialog_key(key)
            return
        name = key.name
        if name == "paste":
            self._paste(key.text)
        elif name == "text":
            self.parts.append(key.text)
            self._merge()
        elif name == "newline":
            self.parts.append("\n")
            self._merge()
        elif name == "backspace":
            if self.parts:
                last = self.parts[-1]
                if isinstance(last, PastePart) or len(last) <= 1:
                    self.parts.pop()
                else:
                    self.parts[-1] = last[:-1]
        elif name == "enter":
            self._enter()
        elif name == "esc":
            self.spawn(self._call(self.escape))
        elif name == "ctrl_c":
            if self.busy and self.ctrl_c_interrupts:
                self.spawn(self._call(self.cancel or self.escape))
            elif self.parts:
                self.parts.clear()
            elif time.monotonic() - self._ctrl_c_at < 1.0:
                self.spawn(self._call(self.quit, 0))
            else:
                self._ctrl_c_at = time.monotonic()
                self.notice = "Press Ctrl-C again to exit"
        elif name == "ctrl_d" and not self.parts:
            self.spawn(self._call(self.quit, 0))

    def _merge(self) -> None:
        merged: list[str | PastePart] = []
        for part in self.parts:
            if isinstance(part, str) and merged and isinstance(merged[-1], str):
                merged[-1] += part
            else:
                merged.append(part)
        self.parts = merged

    def _paste(self, text: str) -> None:
        self.log("paste", chars=len(text))
        self.last_paste_end = time.monotonic()
        if self.faults.dialog_during_paste:
            self.faults.dialog_during_paste = False
            self.open_dialog(Dialog("notice", "Heads up: a notice opened while you were pasting.", ["The pasted text was not kept."], ["Dismiss"], on_choose=lambda i: None, on_escape=lambda: None))
            return
        lines = text.count("\n") + 1
        collapse = len(text) > self.look.collapse_chars or (self.look.collapse_lines and lines > self.look.collapse_lines)
        if collapse:
            self.pastes += 1
            self.parts.append(PastePart(self.pastes, text, self.look.marker_style))
        else:
            self.parts.append(text)
            self._merge()

    def _enter(self) -> None:
        if self.enter_broken:
            self.log("enter_swallowed", reason="environment")
            return
        guard = self.look.burst_guard_ms / 1000
        if guard and time.monotonic() - self.last_paste_end < guard:
            self.log("enter_swallowed", reason="burst")
            return
        if self.faults.swallow_enter_once and not self._swallowed_once and self.parts:
            self._swallowed_once = True
            self.log("enter_swallowed", reason="fault")
            return
        text = self.composer_value()
        if not text.strip():
            return
        reported = self.composer_reported()
        if reported != text:
            self.reported[text] = reported
        if text.strip() == "/exit":
            self.parts.clear()
            self.spawn(self._call(self.quit, 0))
            return
        if self.busy:
            if self.busy_enter is None:
                return
            self.parts.clear()
            self.log("submitted", text=text, busy=True)
            self.spawn(self._call(self.busy_enter, text))
            return
        self.parts.clear()
        self.log("submitted", text=text, busy=False)
        self.spawn(self._call(self.submit, text))

    # -- dialogs ----------------------------------------------------------------------------------

    def open_dialog(self, dialog: Dialog) -> None:
        self.dialog = dialog
        self.log("dialog_opened", kind=dialog.kind, title=dialog.title, selected=dialog.selected)
        self.render()

    def close_dialog(self) -> None:
        self.dialog = None
        self.render()

    def _dialog_key(self, key: Key) -> None:
        dialog = self.dialog
        assert dialog is not None
        if key.name == "up":
            dialog.selected = max(0, dialog.selected - 1)
        elif key.name == "down":
            dialog.selected = min(len(dialog.options) - 1, dialog.selected + 1)
        elif key.name == "text" and dialog.digits and key.text.isdigit() and 1 <= int(key.text) <= len(dialog.options):
            dialog.selected = int(key.text) - 1
            self._choose(dialog)
        elif key.name == "enter":
            if dialog.kind == "notice":
                self.log("enter_into_dialog", kind=dialog.kind)
            self._choose(dialog)
        elif key.name == "esc":
            self.log("dialog_escaped", kind=dialog.kind)
            self.dialog = None
            if dialog.on_escape is not None:
                self.spawn(self._call(dialog.on_escape))
        elif key.name in ("paste", "text"):
            # Typing into an open dialog goes nowhere; a pasted message is lost, as in the real TUIs.
            self.log("typed_into_dialog", kind=dialog.kind, chars=len(key.text))

    def _choose(self, dialog: Dialog) -> None:
        self.log("dialog_answered", kind=dialog.kind, option=dialog.selected, label=dialog.options[dialog.selected])
        self.dialog = None
        if dialog.on_choose is not None:
            self.spawn(self._call(dialog.on_choose, dialog.selected))


# -- talking back ------------------------------------------------------------------------------------


def http_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """One POST; ``(0, b"")`` when nothing answered, so a dead host never stops the CLI."""
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


async def post_json(url: str, payload: Any, headers: Mapping[str, str] | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    status, raw = await asyncio.to_thread(http_post, url, json.dumps(payload).encode(), dict(headers or {}), timeout)
    try:
        body = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = raw.decode("utf-8", "replace")
    return status, body


def expand_env(text: str, allowed: list[str] | None = None) -> str:
    """``$NAME`` and ``${NAME}`` from the environment; with ``allowed``, only those names (the rule
    Claude Code applies to http hook headers)."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if allowed is not None and name not in allowed:
            return ""
        return os.environ.get(name, "")

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", value, text)


async def run_command_hook(command: str, payload: Any, timeout: float) -> tuple[int, Any]:
    """A command hook: the payload on stdin, the answer on stdout; ``(exit code, parsed stdout)``."""
    proc = await asyncio.create_subprocess_shell(command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return -1, None
    text = out.decode("utf-8", "replace").strip()
    try:
        return proc.returncode or 0, json.loads(text) if text else None
    except json.JSONDecodeError:
        return proc.returncode or 0, text


class McpClient:
    """The client side of a stdio MCP server, as a CLI starts one from its MCP configuration."""

    def __init__(self, name: str, command: str, args: list[str], env: Mapping[str, str], *, base_env: Mapping[str, str] | None = None) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.env = dict(env)
        self.base_env = dict(base_env) if base_env is not None else None
        """What the server inherits besides ``env``: everything when ``None``, as most CLIs do; Codex
        passes a filtered environment."""
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[str] = []
        self.error = ""
        self._next = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> bool:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={**(os.environ if self.base_env is None else self.base_env), **self.env}, limit=1 << 22,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        self._reader = asyncio.ensure_future(self._read())
        try:
            await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake-cli", "version": "1"}}, timeout=10)
            await self.notify("notifications/initialized", {})
            listed = await self.request("tools/list", {}, timeout=10)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            self.error = str(exc) or type(exc).__name__
            return False
        self.tools = [t["name"] for t in (listed or {}).get("tools", [])]
        return True

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while line := await self.proc.stdout.readline():
            with contextlib.suppress(json.JSONDecodeError):
                message = json.loads(line)
                future = self._pending.pop(message.get("id"), None) if "id" in message else None
                if future is not None and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(str(message["error"].get("message"))))
                    else:
                        future.set_result(message.get("result"))
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("the MCP server went away"))

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self._next += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        await self._send({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout)

    async def call(self, tool: str, arguments: dict[str, Any], timeout: float) -> str:
        result = await self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)
        return "".join(c.get("text", "") for c in (result or {}).get("content", []) if isinstance(c, dict))

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 2)
        if self._reader is not None:
            self._reader.cancel()


def report_arguments(arg: str) -> dict[str, Any]:
    """``kind:note``, and ``|path`` after the note for each artifact named."""
    kind, _, rest = arg.partition(":")
    note, *artifacts = [p.strip() for p in rest.split("|")]
    out: dict[str, Any] = {"kind": kind.strip() or "checkpoint", "note": note or f"{kind.strip() or 'checkpoint'} from the fake"}
    if artifacts:
        out["artifacts"] = [a for a in artifacts if a]
    return out


def ask_arguments(arg: str) -> dict[str, Any]:
    question, *options = [p.strip() for p in arg.split("|")]
    out: dict[str, Any] = {"question": question}
    if options:
        out["options"] = options
    return out


def settle(future: asyncio.Future[Any], value: Any) -> None:
    """Resolve ``future`` unless something already did: a dialog answered on screen and over a
    channel at once has exactly one answer."""
    if not future.done():
        future.set_result(value)


def new_id() -> str:
    return str(uuid.uuid4())


def exit_with(main: Callable[[], Coroutine[Any, Any, int]]) -> None:
    """Run a fake's main coroutine and exit with its code; a crash is an exit code, not a hang."""
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    sys.stdout.flush()
    raise SystemExit(code)


__all__ = [
    "Args",
    "Dialog",
    "Faults",
    "InputParser",
    "Key",
    "Log",
    "Look",
    "McpClient",
    "PastePart",
    "Step",
    "Tui",
    "ask_arguments",
    "exit_with",
    "expand_env",
    "http_post",
    "installed_version",
    "latest_version",
    "logged_in",
    "new_id",
    "now_iso",
    "pause",
    "post_json",
    "read_log",
    "report_arguments",
    "run_command_hook",
    "scaled",
    "script_of",
    "set_version",
    "settle",
    "state_dir",
    "usage_error",
]
