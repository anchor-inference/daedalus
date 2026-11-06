"""Public tunnels: what a command line does when it raises one, and which running process is one.

The operator opens tunnels from settings. An agent that raised one by itself — ``ngrok``, a Cloudflare
tunnel, a Tailscale funnel, frp, zrok, an SSH reverse forward, an SSH service that hands out a public
address — would give the internet a way to this machine around the operator's own switch. The policy
refuses it (rule ``egress.tunnel``, :func:`tunnel_in`), and :func:`running` finds the ones that were
raised anyway, for the doctor and for :mod:`daedalus.extensions.tunnel_watch`.

Both read a command by what it does, not by what it says. A rule on the word would refuse ``grep ngrok
notes.md``, ``pip install pyngrok`` and a README that explains ``cloudflared tunnel``, and the agent
is not forbidden to know what a tunnel is, only to raise one. So a program is named by its argv and
refused on positive evidence that the call raises something: ``ngrok http 3000`` but not ``ngrok
--version``, ``cloudflared tunnel run`` but not ``cloudflared tunnel list``, ``ssh -R`` but not ``ssh
host``. A false refusal is a defect here, not a safe side to err on.

What this cannot see, and does not pretend to:

* a tunnel raised from a file — ``bash run.sh``, ``python tunnel.py``, a ``package.json`` script, a
  compose file: only the command line is read, never what a script it names contains;
* a command assembled at run time (``$(…)``, a variable the line did not set itself), and one piped
  into a shell or fed to it as a heredoc (``echo '…' | sh``): the words of the line are read, not the
  text that flows between its commands;
* ``sudo -u user …``, ``env -u NAME …`` and the like: the shell reader that precedes this one takes
  the wrapper off and leaves the option's value in front of the command, so it reads as a program
  called ``user`` — for every rule of the policy, this one included;
* a binary copied or downloaded under another name, which the process scan cannot tell from any other;
* ``tailscale funnel --bg`` and ``cloudflared service install`` leave no process of their own to find;
* a host in ``~/.ssh/config`` that carries a ``RemoteForward`` of its own, or a service not in
  :data:`SSH_SERVICES`;
* the commands of a command-line harness (Claude Code, Codex …) the operator runs in a terminal: they
  run under that program's own permissions, not under this policy.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from daedalus.security.redact import redact

Split = Callable[[str], list[list[str]]]
"""Reads a command line into its simple commands (:func:`daedalus.host.policy.shell_segments`). Passed
in rather than imported: the policy imports this module."""

REFUSAL = (
    "Refused: {what} would raise a public tunnel, and the agent does not do that by itself. It is the operator's "
    "rule (egress.tunnel): a public address for this machine is behind the operator's own switch, and the operator "
    "opens tunnels from settings. Do not try another way to raise one (another tool, a script, a container, another "
    "service); that is the same refusal. If the work needs a public address, say what needs it and ask the operator "
    "with AskUser to open a tunnel from settings; continue once they say it is open."
)
"""What the agent is told. It names the owner of the rule and the way to get what it wanted, because the
generic wording of a refusal ("do the task another way") sent an agent looking for another way in."""

SSH_PROGRAMS = frozenset({"ssh", "autossh", "plink"})
SSH_SERVICES = ("localhost.run", "pinggy.io", "serveo.net", "tunnl.gg", "tuns.sh")
"""Hosts that answer an SSH connection with a public address for a forwarded port. Matched as a name or
as the parent of one (``a.pinggy.io``); a service that is not here is still caught by its ``-R``."""
SSH_WITH_VALUE = frozenset("BbcDEeFIiJLlmOopQRSWw")
"""ssh's options that take a value, in a cluster as well (``-fNR80:…``): the rest of the cluster is the
value, or the next word when nothing follows. Telling them apart is how ``ls -R`` after the host stays
a command and ``-NR`` before it does not."""

CONTAINER_LAUNCHERS = frozenset({"docker", "podman", "nerdctl", "kubectl", "lxc", "incus"})
LAUNCHERS = CONTAINER_LAUNCHERS | frozenset({
    "eval", "npx", "bunx", "pnpx", "uvx", "pipx", "npm", "pnpm", "yarn", "bun", "uv", "deno",
    "setsid", "watch", "tmux", "screen", "script", "su", "runuser", "doas", "pkexec", "systemd-run", "nsenter",
    "unshare", "chroot", "flock", "ionice", "chrt", "taskset", "proxychains", "proxychains4", "torsocks", "unbuffer",
    "sshpass", "dumb-init", "tini", "gosu", "su-exec", "firejail", "daemonize", "start-stop-daemon", "parallel",
    "eatmydata", "xvfb-run", "setpriv", "at",
})
"""Programs whose operands are, somewhere among them, another command: a package runner (``npx``,
``uvx``), a container (``docker run … image args``), a terminal multiplexer (``tmux new 'cmd'``) or a
plain wrapper. Every operand is tried as the start of a command; a program that is not here has its
operands read as arguments, so ``echo ngrok http`` and ``git commit -m "…ngrok http…"`` stay words."""
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ash"})
INTERPRETERS = re.compile(r"^(python[\d.]*|pypy[\d.]*|node(js)?|bun|deno|ruby|perl)$")
SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
DEPTH = 8
"""How many wrappers deep a command is followed (``setsid docker run … sh -c '…'``): a bound, not a
promise, and more than a command written for work ever has. The cost stays linear in the length of the
line because a launcher's operands are read once, by the first launcher among them."""
LOOK_AHEAD = 64
"""How many operands of a launcher are tried as a command's start: a ``docker run`` with its options
and their values puts the image well past the first few."""

_CODE = re.compile(r"\bngrok\.(?:connect|forward|listen)\s*\(|\brequire\(\s*['\"]ngrok['\"]\s*\)\s*\.\s*(?:connect|forward)\s*\(|\bpinggy\.(?:start_tunnel|Tunnel)\b|\blocaltunnel\s*\(|\btry_cloudflare\s*\(|\bpycloudflared\b")
"""Code handed to an interpreter on the command line (``python -c``, ``node -e``) that raises a tunnel
through a library. An import alone is not it: ``python -c "import pyngrok"`` asks whether it is installed."""

_HEREDOC_CODE = re.compile(r"\bngrok\.(?:connect|forward|listen)\b|\bpinggy\.(?:start_tunnel|Tunnel)\b|\btry_cloudflare\b|\bpycloudflared\b")
"""The same for code that follows a heredoc marker, which the shell reader has already taken apart at
every parenthesis: a call is the name of the function without its argument list."""

INFO_FLAGS = frozenset({"--help", "--version"})
CLOUDFLARED_ADMIN = frozenset({"list", "ls", "info", "create", "delete", "route", "login", "cleanup", "token", "ingress", "vnet", "diag", "help"})
"""``cloudflared tunnel <these>`` look after an account's tunnels and start none."""


def _operands(args: Sequence[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _flag(args: Sequence[str], *names: str) -> bool:
    return any(a == n or a.startswith(n + "=") for a in args for n in names)


def _asks_for_info(args: Sequence[str]) -> bool:
    """Help or the version: what an agent reads to learn the tool, and which raises nothing.

    ``-h`` counts only as the whole call. In ``localtunnel`` it is the server to use, so ``lt -h … -p
    8000`` is a tunnel and a rule that took ``-h`` for help anywhere would be a way round it."""
    return bool(INFO_FLAGS & set(args)) or args == ["-h"] or _operands(args)[:1] in (["help"], ["version"])


def _ngrok(args: Sequence[str]) -> bool:
    return bool(set(_operands(args)[:3]) & {"http", "tcp", "tls", "start", "tunnel"})


def _cloudflared(args: Sequence[str]) -> bool:
    operands = _operands(args)
    if "tunnel" in operands:
        return not set(operands[operands.index("tunnel") + 1 :]) & CLOUDFLARED_ADMIN
    return _flag(args, "--url", "--hello-world") or operands[:2] == ["service", "install"]


def _tailscale(args: Sequence[str]) -> bool:
    operands = _operands(args)
    if "funnel" in operands:
        return not set(operands) & {"status", "reset", "off"}
    return "serve" in operands and _flag(args, "--funnel")


def _frpc(args: Sequence[str]) -> bool:
    return _operands(args)[:1] not in (["verify"], ["status"], ["reload"], ["stop"])


def _vscode(args: Sequence[str]) -> bool:
    operands = _operands(args)
    return operands[:1] == ["tunnel"] and not set(operands[1:2]) & {"status", "kill", "unregister", "prune", "rename", "user"}


PROGRAMS: dict[str, Callable[[Sequence[str]], bool]] = {
    "ngrok": _ngrok,
    "pyngrok": _ngrok,
    "cloudflared": _cloudflared,
    "tailscale": _tailscale,
    "frpc": _frpc,
    "zrok": lambda args: "share" in _operands(args),
    "zrok2": lambda args: "share" in _operands(args),
    "bore": lambda args: "local" in _operands(args),
    "chisel": lambda args: "client" in _operands(args) and any(o.startswith("R:") for o in _operands(args)),
    "lt": lambda args: _flag(args, "--port", "-p"),
    "localtunnel": lambda args: _flag(args, "--port", "-p"),
    "loophole": lambda args: _operands(args)[:1] in (["http"], ["tcp"]),
    "loclx": lambda args: "tunnel" in _operands(args),
    "localxpose": lambda args: "tunnel" in _operands(args),
    "tuna": lambda args: _operands(args)[:1] in (["http"], ["tcp"], ["tls"]),
    "devtunnel": lambda args: "host" in _operands(args),
    "code": _vscode,
    "code-insiders": _vscode,
    "inlets": lambda args: "client" in _operands(args),
    "inlets-pro": lambda args: "client" in _operands(args),
    "wstunnel": lambda args: "client" in _operands(args) and _flag(args, "-R", "--remote-to-local"),
    "expo": lambda args: "start" in _operands(args) and _flag(args, "--tunnel"),
    "netlify": lambda args: "dev" in _operands(args) and _flag(args, "--live"),
    "tor": lambda args: any("HiddenService" in a for a in args),
    "pinggy": lambda args: True,
    "pagekite": lambda args: True,
    "pagekite.py": lambda args: True,
    "telebit": lambda args: True,
    "playit": lambda args: True,
    "rathole": lambda args: True,
    "tmole": lambda args: True,
    "tunnelmole": lambda args: True,
}
"""Each program that raises a tunnel, and what its arguments say about whether this call does. A program
with no mode that raises nothing (``pinggy``, ``playit``) is refused for any call but help or the version."""


def _program(word: str) -> str:
    """The name a word runs as: its last path component, without the ``.exe`` of Windows, without the
    tag of a container image (``cloudflare/cloudflared:latest``) or the version of a package
    (``ngrok@latest``)."""
    name = word.lstrip("\\").replace("\\", "/").rsplit("/", 1)[-1].lower()
    name = re.sub(r"(@sha256:[0-9a-f]+|:[\w.-]+|@[\w.^~-]+)$", "", name)
    return name.removesuffix(".exe")


def _direct(name: str, args: Sequence[str]) -> str | None:
    check = PROGRAMS.get(name)
    if check is None or _asks_for_info(args) or not check(args):
        return None
    return name


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(next((s for s in SCRIPT_SUFFIXES if name.endswith(s)), ""))


def _interpreted(args: Sequence[str]) -> str | None:
    """A tunnel an interpreter is started to run: a module (``python -m pyngrok``), a script by the
    name of a program (``node …/localtunnel/bin/lt.js``), or library code on the command line."""
    if any(word in ("<<", "<<<") for word in args) and _HEREDOC_CODE.search(" ".join(args)):
        return "a tunnel library"  # python - <<EOF …: the code follows the marker, as words
    i = 0
    while i < len(args):
        word = args[i]
        if word == "-m" and i + 1 < len(args):
            return _direct(args[i + 1].split(".")[0].lower(), args[i + 2 :])
        if word in ("-c", "-e", "--eval", "-p", "--print") and i + 1 < len(args):
            return "a tunnel library" if _CODE.search(args[i + 1]) else None
        if word.startswith("-"):
            i += 1
            continue
        return _direct(_stem(word), args[i + 1 :])
    return None


def _forwards(spec: str) -> bool:
    """Whether an ``ssh -R`` spec opens a TCP port on the far side. A socket path (``-R /run/…:/run/…``,
    which is how a gpg or docker socket is lent to a remote shell) opens no port anyone can dial."""
    return bool(spec.strip()) and not spec.strip().startswith("/")


def _ssh(args: Sequence[str], split: Split, depth: int) -> str | None:
    """``ssh`` and its kin: a reverse forward, a tunnelling service as the destination, or a tunnel
    raised by the command it runs on the far side.

    Options are read the way ssh reads them, so that a word after the destination is the remote command
    and not an option: ``ssh host ls -R`` lists a directory."""
    host = ""
    remote: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word.startswith("-") and len(word) > 1 and word != "--":
            for j, char in enumerate(word[1:], 1):
                if char not in SSH_WITH_VALUE:
                    continue
                value = word[j + 1 :]
                if not value and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if char == "R" and _forwards(value):
                    return "ssh -R"
                if char == "o" and re.match(r"(?i)\s*RemoteForward\b", value) and _forwards(re.sub(r"(?i)^\s*RemoteForward[\s=]+", "", value)):
                    return "ssh RemoteForward"
                break
            i += 1
        elif word == "--":
            i += 1
        elif not host:
            host = word
            i += 1
        else:
            remote = list(args[i:])
            break
    name = re.sub(r"^\[|\](:\d+)?$|:\d+$", "", host.split("://")[-1].rsplit("@", 1)[-1].lower())
    for service in SSH_SERVICES:
        if name == service or name.endswith("." + service):
            return f"ssh to {service}"
    if remote and depth < DEPTH:
        for segment in split(" ".join(remote)):
            if (found := tunnel_in(segment, split, depth=depth + 1)) is not None:
                return found
    return None


def tunnel_in(words: Sequence[str], split: Split, *, depth: int = 0) -> str | None:
    """What this simple command raises, in a few words for the refusal, or ``None`` when it raises no tunnel.

    ``words`` is one command's argv, wrappers (``sudo``, ``nohup``, ``env``) already taken off as
    :func:`daedalus.host.policy.shell_segments` does. The launchers a command may be run through are
    followed (:data:`LAUNCHERS`), an ``ssh`` is read for its forwards, its destination and the command
    it runs, and an interpreter for the module, script or library code it was given.
    """
    if not words or depth > DEPTH:
        return None
    if words[0].startswith("-") or words[0].isdigit():
        # What a wrapper the reader stripped left in front of the command (``timeout -s KILL 10 …``,
        # ``nice -n 5 …``, ``stdbuf -oL …``): no command starts with a dash or is a number.
        for i in range(1, min(len(words), LOOK_AHEAD)):
            if words[i].startswith("-") or words[i].isdigit():
                continue  # tried from the first word that can be a command, once
            if (found := tunnel_in(words[i:], split, depth=depth + 1)) is not None:
                return found
        return None
    name, args = _program(words[0]), list(words[1:])
    if (found := _direct(name, args)) is not None:
        return found
    if name in SSH_PROGRAMS:
        return _ssh(args, split, depth)
    if name in SHELLS:
        # ``sh -c '…'`` and ``sh <<< '…'`` hand the shell one command line; a pipe or a heredoc hands it many.
        inner = next((args[i + 1] for i, word in enumerate(args) if (word == "<<<" or re.fullmatch(r"-[a-z]*c[a-z]*", word)) and i + 1 < len(args)), None)
        return _first(split(inner), split, depth) if inner else None
    if INTERPRETERS.match(name) and (found := _interpreted(args)) is not None:
        return found
    if name in LAUNCHERS and depth < DEPTH:
        for i, word in enumerate(args[:LOOK_AHEAD]):
            if word.startswith("-") or not word:
                continue
            if re.search(r"\s", word.strip()):
                found = _first(split(word), split, depth)  # a quoted command line: tmux new 'ngrok http 80'
            else:
                found = tunnel_in(args[i:], split, depth=depth + 1)
                if _program(word) in LAUNCHERS:
                    return found  # that launcher reads the operands after it; reading them again from here is the quadratic walk
            if found is not None:
                return found
    return None


def _first(segments: list[list[str]], split: Split, depth: int) -> str | None:
    for segment in segments:
        if (found := tunnel_in(segment, split, depth=depth + 1)) is not None:
            return found
    return None


# -- what is running -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Running:
    """One process that is a tunnel by the same reading that refuses one."""

    pid: int
    kind: str
    command: str
    """The command line as the process shows it, with what looks like a key or a token taken out."""
    age_seconds: float | None = None
    """How long it has been running; ``None`` where the platform does not say."""

    @property
    def line(self) -> str:
        return f"{self.kind} · pid {self.pid}" + (f" · running {age_text(self.age_seconds)}" if self.age_seconds is not None else "") + f" · {self.command}"


COMMAND_CHARS = 160
SECRET_FLAG = re.compile(r"^--?(?:auth-?token|token|password|passwd|pass|secret|key)$", re.IGNORECASE)
OPAQUE = re.compile(r"[A-Za-z0-9_=-]{32,}")


def age_text(seconds: float) -> str:
    if seconds < 90:
        return "under a minute" if seconds < 60 else "a minute"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


def shown(words: Sequence[str]) -> str:
    """A command line as it may be put in front of the operator and in a log: the value after a token
    or password option, and any long opaque word, replaced; then the redactor's own pass."""
    out: list[str] = []
    hidden = False
    for word in words:
        if hidden:
            out.append("…")
            hidden = False
        elif SECRET_FLAG.match(word):
            out.append(word)
            hidden = True
        elif "=" in word and SECRET_FLAG.match(word.split("=", 1)[0]):
            out.append(word.split("=", 1)[0] + "=…")
        elif OPAQUE.fullmatch(word):
            out.append("…")
        else:
            out.append(word)
    line = redact(" ".join(out))
    return line if len(line) <= COMMAND_CHARS else line[: COMMAND_CHARS - 1] + "…"


def _of(pid: int, words: list[str], split: Split, age: float | None) -> Running | None:
    kind = tunnel_in(words, split)
    return None if kind is None else Running(pid, kind, shown(words), age)


def scan_proc(split: Split, *, root: str = "/proc", uid: int | None = None) -> list[Running]:
    """Every process of ``uid`` (this process's user) under a ``/proc`` that is a tunnel.

    One user's, not the machine's: a tunnel service another account runs for the machine is the
    machine's own arrangement, and the agent can only start processes as this one."""
    uid = os.getuid() if uid is None else uid
    uptime = _read_uptime(root)
    ticks = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0
    found: list[Running] = []
    for entry in Path(root).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # gone since it was listed, or not ours to read
        words = [w for w in raw.decode("utf-8", "replace").split("\0") if w]
        if not words:
            continue  # a kernel thread
        running = _of(int(entry.name), words, split, _read_age(entry, uptime, ticks))
        if running is not None:
            found.append(running)
    return sorted(found, key=lambda r: r.pid)


def _read_uptime(root: str) -> float | None:
    try:
        return float((Path(root) / "uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_age(entry: Path, uptime: float | None, ticks: float) -> float | None:
    if uptime is None:
        return None
    try:
        # The process name is in brackets and may hold spaces and brackets; the fields start after the last one.
        fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        return max(0.0, uptime - int(fields[19]) / ticks)
    except (OSError, ValueError, IndexError):
        return None


def parse_etime(text: str) -> float | None:
    """``ps``'s elapsed time, ``[[dd-]hh:]mm:ss``, in seconds."""
    match = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", text.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def scan_ps(text: str, split: Split) -> list[Running]:
    """The same from ``ps -o pid=,etime=,command=`` output, for a system with no ``/proc`` (macOS).
    ``ps`` joins the argv with spaces, so a path with one in it is read as two words: good enough to
    name a program, which is all this asks of it."""
    found: list[Running] = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(\S+)\s+(.+)$", line)
        if match is None:
            continue
        try:
            words = shlex.split(match.group(3))
        except ValueError:
            words = match.group(3).split()
        if (running := _of(int(match.group(1)), words, split, parse_etime(match.group(2)))) is not None:
            found.append(running)
    return found


PS_SECONDS = 5.0


def running(split: Split | None = None) -> list[Running] | None:
    """The tunnels running as this user, or ``None`` where this platform cannot be looked at (Windows)."""
    if os.name == "nt":
        return None
    if split is None:
        from daedalus.host.policy import shell_segments as split  # Lazy: the policy imports this module
    if (Path("/proc") / "self").is_dir():
        return scan_proc(split)
    try:
        done = subprocess.run(  # noqa: S603 — a fixed program and the user's own number
            ["ps", "-U", str(os.getuid()), "-o", "pid=,etime=,command="], capture_output=True, text=True, timeout=PS_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return scan_ps(done.stdout, split) if done.returncode == 0 else None


__all__ = ["PROGRAMS", "REFUSAL", "Running", "age_text", "parse_etime", "running", "scan_proc", "scan_ps", "shown", "tunnel_in"]
