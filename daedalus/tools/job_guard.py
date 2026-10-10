"""What a shell command does to the background, read before it runs: the traps that leave a job unheard of.

Two shapes cost a session a whole render's worth of silence. A waiter written as
``while pgrep -f "out.mp4"; do sleep 3; done`` never ends, because ``pgrep -f`` reads full command lines
and the line of the shell running the loop contains the pattern: the loop finds itself. And a long
process started inside an ordinary Exec with ``nohup … &`` has no parent anyone watches, so nothing
reports when it ends. The first is refused by the policy with :func:`self_matching_pattern`; the second
is named in the Exec result by :func:`orphan_note`. :func:`wait_script` is the waiter written properly,
for ``JobWait``: it waits on a process id through ``/proc`` (or ``kill -0`` where there is none), never
on a pattern over command lines.
"""

from __future__ import annotations

import re
import shlex

from daedalus.host.policy import shell_segments

_MATCHERS = frozenset({"pgrep", "pkill"})


def _pattern_of(words: list[str]) -> str | None:
    """The pattern of a ``pgrep``/``pkill`` that matches full command lines (``-f``), or None."""
    full = False
    takes_value = {"-u", "-U", "-g", "-G", "-P", "-s", "-t", "-d", "--signal", "-F", "--pidfile", "--ns", "--nslist", "-o", "-n"}
    pattern = None
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
            continue
        if word in (">", ">>", "2>", "<", ">&", "2", "1", "/dev/null") or word.startswith((">", "2>", "<")):
            continue
        if word == "--full":
            full = True
            continue
        if word.startswith("--"):
            skip = word in takes_value
            continue
        if word.startswith("-") and len(word) > 1 and not word[1].isdigit():
            letters = word[1:]
            full = full or "f" in letters
            skip = word in takes_value
            continue
        if pattern is None:
            pattern = word
    return pattern if full else None


def self_matching_pattern(command: str) -> str | None:
    """The ``pgrep -f``/``pkill -f`` pattern in ``command`` that matches the command's own shell, if any.

    The shell that runs a compound command keeps the whole command as its argument, so a pattern that
    matches the command text matches that shell: ``pgrep`` finds it for as long as the loop runs, and
    ``pkill`` kills it. A command that is nothing but the one ``pgrep`` is spared — the shell hands its
    process over to it, and ``pgrep`` leaves itself out. The bracket idiom (``[r]ender``) is the cure the
    message names: the regular expression no longer matches its own spelling.
    """
    segments = shell_segments(command)
    for words in segments:
        if not words or words[0].rsplit("/", 1)[-1] not in _MATCHERS:
            continue
        pattern = _pattern_of(words)
        if not pattern:
            continue
        try:
            found = re.search(pattern, command) is not None
        except re.error:
            found = pattern in command
        if not found:
            continue
        if len(segments) == 1 and not re.search(r"[;&|\n]|\b(while|until|for|do)\b", command.strip()):
            continue
        return pattern
    return None


SELF_MATCH_REASON = (
    "`{tool} -f {pattern}` matches the command line of the very shell running it, so a loop around it never "
    "ends (and pkill kills its own shell). Wait with JobWait instead — JobWait(pid=…) for a process, "
    "JobWait(path=…) for a file, JobWait(log=…, pattern=…) for a line, JobWait(port=…) for a listener, "
    "background=true to be woken when it happens. Better still, start the long process itself with "
    "Exec(background=true): its own end is what wakes you. If you must match command lines, write the "
    "pattern so it cannot match itself, e.g. '[r]ender'"
)


def self_match_reason(command: str) -> str | None:
    pattern = self_matching_pattern(command)
    if pattern is None:
        return None
    tool = next((w[0].rsplit("/", 1)[-1] for w in shell_segments(command) if w and w[0].rsplit("/", 1)[-1] in _MATCHERS and _pattern_of(w) == pattern), "pgrep")
    return SELF_MATCH_REASON.format(tool=tool, pattern=shlex.quote(pattern))


_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_DETACHED = re.compile(r"(?<![&>|<0-9])&(?![&>])|\bnohup\b|\bsetsid\b|\bdisown\b")


def orphan_note(command: str, *, sandboxed: bool) -> str | None:
    """A warning for a foreground command that leaves a process running behind it, or None.

    Such a process has no parent anything here watches: when it ends nobody is told, which is how a
    render started with ``nohup … &`` finished (or crashed) while the agent slept waiting for news.
    Not refused — a quick ``server & sleep 1; curl …`` smoke test is legitimate — but said, every time.
    """
    # Here-documents carry text, not commands: an `&` in an inline script is not a detach. They go
    # first, before the quotes: the quotes around a here-document's delimiter are part of its syntax.
    bare = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\b", "", command, flags=re.S)
    bare = _QUOTED.sub("''", bare)
    if not _DETACHED.search(bare):
        return None
    if sandboxed:
        return (
            "\n[note: this command put something in the background (`&`, nohup or setsid); in the sandbox it ended "
            "with the command. Start long work with Exec(background=true) — you are told when it ends]"
        )
    return (
        "\n[note: this command left a process running in the background (`&`, nohup or setsid), and nothing will "
        "tell you when it ends. Start long work with Exec(background=true) instead — its end wakes you. For a "
        "process already running, JobWait(pid=…, background=true) reports its end]"
    )


WAIT_TIMED_OUT = 124
"""The exit code of a wait that ran out of time: the code ``timeout(1)`` uses, so a reader knows it."""


def wait_script(*, timeout: int, pid: int | None = None, path: str | None = None, log: str | None = None, pattern: str | None = None, port: int | None = None, host: str = "127.0.0.1", interval: float = 2.0) -> tuple[str, str]:
    """The shell loop that waits for one condition, and its description for the reports.

    Exactly one condition: a process id to exit, a file to appear, a line matching ``pattern`` in
    ``log``, or a TCP port to accept. It exits 0 when the condition holds and :data:`WAIT_TIMED_OUT`
    when ``timeout`` seconds pass first. Every value is quoted or an integer, so the script carries
    nothing of the caller's that could run.
    """
    conditions = [pid is not None, path is not None, log is not None or pattern is not None, port is not None]
    if sum(conditions) != 1:
        raise ValueError("name exactly one thing to wait for: pid, path, log with pattern, or port")
    if pid is not None:
        if pid <= 1:
            raise ValueError("pid must be a process id above 1")
        what = f"pid {pid} to exit"
        check = f"! {{ if [ -d /proc/self ]; then [ -e /proc/{pid} ]; else kill -0 {pid} 2>/dev/null; fi; }}"
        done = f"echo 'pid {pid} has exited'"
    elif path is not None:
        what = f"{path} to appear"
        check = f"[ -e {shlex.quote(path)} ]"
        done = f"ls -la {shlex.quote(path)}"
    elif port is not None:
        if not 0 < port < 65536:
            raise ValueError("port must be between 1 and 65535")
        what = f"{host}:{port} to accept connections"
        check = f"(exec 3<>/dev/tcp/{shlex.quote(host)}/{port}) 2>/dev/null"
        done = f"echo '{host}:{port} accepts connections'"
    else:
        if not log or not pattern:
            raise ValueError("a log wait needs both log (a file) and pattern (an extended regular expression)")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"pattern is not a regular expression: {exc}") from exc
        what = f"/{pattern}/ in {log}"
        check = f"grep -Eq -- {shlex.quote(pattern)} {shlex.quote(log)} 2>/dev/null"
        done = f"grep -E -m 3 -- {shlex.quote(pattern)} {shlex.quote(log)}"
    script = (
        f"deadline=$(( $(date +%s) + {max(1, int(timeout))} )); "
        f"until {check}; do "
        f"if [ \"$(date +%s)\" -ge \"$deadline\" ]; then echo {shlex.quote(f'timed out after {int(timeout)}s waiting for {what}')}; exit {WAIT_TIMED_OUT}; fi; "
        f"sleep {interval:g}; done; {done}"
    )
    return script, f"wait for {what}"


__all__ = ["SELF_MATCH_REASON", "WAIT_TIMED_OUT", "orphan_note", "self_match_reason", "self_matching_pattern", "wait_script"]
