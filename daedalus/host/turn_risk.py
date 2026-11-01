"""A fixed check of what a finished turn did, for work that goes on without the operator.

Autopilot and the agents of the companion's goals keep working between the operator's messages.
Most of their turns are harmless; a few did something the operator wants to look at before anything
more happens: an applied migration rewritten, containers or a service restarted, shared history
rewritten, a tree deleted outside the session's folder, a table dropped, a file written outside the
folder. This module finds those in the turn's own tool calls and their arguments — never in what the
model said about them — with small tables of rules that ask no model and give the same answer every
time. A false alarm costs the operator one press; a missed one is what the check exists for, so the
rules lean coarse wherever they cannot see (a variable in a path, a folder git cannot answer for).

A call that never ran does not count: one the policy refused, the operator's hook blocked, or the
core rejected for its arguments left nothing behind. One that ran and failed does count, since a
``docker compose down`` that exits 1 halfway still took something down.

The shell is read with the policy's own lexer (:func:`daedalus.host.policy.shell_segments`): the
policy decides before a call whether it may run at all, this decides after a turn whether the work
may go on unattended, and both must see ``cd x && sudo docker restart y`` as the same commands.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from protocore.contracts.types import Message, ToolResultBlock, ToolUseBlock

from daedalus.config import GoalRiskConfig, MigrationListConfig
from daedalus.host import policy
from daedalus.host.gitrun import GitError, run_git
from daedalus.host.notify_text import render

KINDS = ("migration", "containers", "services", "git_history", "delete_outside", "database", "write_outside")
"""What the operator may allow for the rest of a goal, one rule family each."""

WRITE_TOOLS = ("Write", "Edit", "MultiEdit")
NEVER_RAN_KINDS = frozenset({"permission", "validation", "unknown_tool"})
"""The core's error kinds for a call it answered without running the tool."""
ERROR_KIND_KEY = "tool_dispatch.replay_error_kind"
NEVER_RAN_PREFIXES = ("refused by policy", "needs the operator's approval", "needs approval:", "refused by the operator's pre-tool hook")
"""The same, read from the text, for a refusal whose result carries no error kind."""
WHAT_MAX = 160
FILES_SHOWN = 6
"""Files the stop names before it says there are more; the count is always whole."""
GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Call:
    """One tool call of the turn, as far as the rules need it."""

    tool: str
    arguments: dict[str, Any]
    failed: bool = False
    path: str = ""
    """The file the result says the call acted on, resolved by the tool itself; empty when it named none."""


@dataclass(frozen=True)
class Finding:
    kind: str
    rule: str
    what: str
    """The command or the path that broke the rule, as the turn wrote it."""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "rule": self.rule, "what": self.what}


@dataclass(frozen=True)
class Scope:
    """Where the turn worked and since when its goal runs."""

    work_dir: str
    started: datetime | None = None
    """When the goal began: a migration counts as existing when the last commit before this moment had it."""
    config: GoalRiskConfig = field(default_factory=GoalRiskConfig)
    home: str = field(default_factory=lambda: str(Path.home()))


@dataclass(frozen=True)
class Command:
    """One simple command of a shell call: its argv, the folder it ran in, and the whole line it came from."""

    words: list[str]
    where: str
    line: str


@dataclass(frozen=True)
class FileWrite:
    tool: str
    path: str
    edits: tuple[tuple[str, str], ...] = ()
    """For Edit and MultiEdit, each ``(old, new)``; empty for a Write, which replaces the whole file."""


@dataclass(frozen=True)
class Digest:
    """A short account of what the turn changed, shown with the stop so the operator need not open the session."""

    files: list[str]
    commands: int
    failed: int

    def as_dict(self) -> dict[str, Any]:
        return {"files": self.files, "commands": self.commands, "failed": self.failed}


# -- reading the turn -----------------------------------------------------------------------------


def _never_ran(block: ToolResultBlock) -> bool:
    if not block.is_error:
        return False
    if str((block.metadata or {}).get(ERROR_KIND_KEY) or "") in NEVER_RAN_KINDS:
        return True
    return block.content.lstrip().startswith(NEVER_RAN_PREFIXES)


def turn_calls(messages: Iterable[Message]) -> list[Call]:
    """The calls of a turn that ran, in order. A call with no result at all is kept: the run may have
    been cut after the tool started, and unknown counts as ran."""
    uses: list[ToolUseBlock] = []
    results: dict[str, ToolResultBlock] = {}
    for message in messages:
        for block in message.content_blocks:
            if isinstance(block, ToolUseBlock):
                uses.append(block)
            elif isinstance(block, ToolResultBlock):
                results[block.tool_call_id] = block
    calls: list[Call] = []
    for use in uses:
        result = results.get(use.tool_call_id)
        if result is not None and _never_ran(result):
            continue
        try:
            arguments = json.loads(use.arguments_json or "{}")
        except ValueError:
            arguments = {}
        calls.append(Call(
            use.name, arguments if isinstance(arguments, dict) else {}, failed=bool(result is not None and result.is_error),
            path=str(getattr(result, "path", None) or "") if result is not None else "",
        ))
    return calls


async def run_messages(db: Any, session_id: str, run_id: str, *, since: int = 0) -> list[Message]:
    """The transcript rows one run wrote (every row carries its run's id), from row ``since`` on."""
    rows = await db.fetchall(
        "SELECT message FROM transcript WHERE session_id = ? AND seq >= ? AND json_extract(message, '$.metadata.\"daedalus.run_id\"') = ? ORDER BY seq",
        (session_id, since, run_id),
    )
    return [Message.model_validate_json(row["message"]) for row in rows]


def _absolute(path: str, base: str, home: str) -> str:
    expanded = policy.expand_home(path, home)
    return posixpath.normpath(expanded if posixpath.isabs(expanded) else posixpath.join(base, expanded))


def _under(path: str, root: str) -> bool:
    real, top = policy.real_path(path), policy.real_path(root).rstrip("/")
    return real == top or real.startswith(top + "/")


def _clip(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= WHAT_MAX else flat[: WHAT_MAX - 1] + "…"


def commands_of(call: Call, scope: Scope) -> list[Command]:
    """The simple commands of a shell call, each with the folder it ran in: the call's ``cwd``, moved
    by any ``cd`` earlier in the same line, as the policy reads it."""
    if call.tool not in policy.SHELL_TOOLS:
        return []
    line = str(call.arguments.get("command") or "")
    if not line.strip():
        return []
    cwd = str(call.arguments.get("cwd") or "")
    where = _absolute(cwd, scope.work_dir, scope.home) if cwd else scope.work_dir
    out: list[Command] = []
    for words in policy.shell_segments(line):
        if words[0] == "cd" and len(words) > 1:
            where = _absolute(words[1], where, scope.home)
            continue
        head = words[0].lstrip("\\")
        if head.startswith("/") and head.count("/") >= 2:
            head = head.rsplit("/", 1)[-1]  # /usr/bin/docker is docker
        if head == "busybox" and len(words) > 1:
            words = words[1:]
            head = words[0]
        out.append(Command([head, *words[1:]], where, line))
    return out


def writes_of(calls: Iterable[Call], scope: Scope) -> list[FileWrite]:
    out: list[FileWrite] = []
    for call in calls:
        if call.tool not in WRITE_TOOLS:
            continue
        named = call.path or str(call.arguments.get("path") or "")
        if not named:
            continue
        edits: tuple[tuple[str, str], ...] = ()
        if call.tool == "Edit":
            edits = ((str(call.arguments.get("old_string") or ""), str(call.arguments.get("new_string") or "")),)
        elif call.tool == "MultiEdit":
            edits = tuple((str(e.get("old_string") or ""), str(e.get("new_string") or "")) for e in call.arguments.get("edits") or [] if isinstance(e, dict))
        out.append(FileWrite(call.tool, _absolute(named, scope.work_dir, scope.home), edits))
    return out


# -- the shell rules ------------------------------------------------------------------------------


def _plain(words: list[str], with_value: set[str]) -> list[str]:
    """The words after the head that are neither options nor an option's value."""
    out: list[str] = []
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in with_value
            continue
        out.append(word)
    return out


DOCKER_OPTIONS_WITH_VALUE = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"}
COMPOSE_OPTIONS_WITH_VALUE = {"-f", "--file", "-p", "--project-name", "--env-file", "--profile", "--project-directory", "--ansi", "--parallel", "--progress"}
COMPOSE_VERBS = {"up", "down", "restart", "rm", "stop", "kill"}
CONTAINER_VERBS = {"restart", "rm", "stop", "kill"}
SYSTEMCTL_OPTIONS_WITH_VALUE = {"-H", "--host", "-M", "--machine", "-p", "--property", "-t", "--type", "--state", "-n", "--lines", "-o", "--output", "-s", "--signal", "--kill-whom"}
SYSTEMCTL_VERBS = {"restart", "stop", "kill", "try-restart", "reload-or-restart", "try-reload-or-restart"}
DATABASE_CLIENTS = {"sqlite3", "psql", "mysql", "mariadb", "duckdb", "clickhouse-client", "clickhouse", "cockroach", "sqlcmd", "mongo", "mongosh", "redis-cli"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php", "deno", "bun"}
DESTRUCTIVE_SQL = re.compile(
    r"\b(?:drop\s+(?:table|database|schema|view|collection|keyspace)|truncate\s+(?:table\s+)?[\w\"`\[]|flushall|flushdb)|\bdropDatabase\s*\(",
    re.IGNORECASE,
)
STATEMENT_ARGUMENTS = ("sql", "statement")
"""Arguments that hold a database statement in any tool; ``query`` only counts in a tool whose name says database."""
DATABASE_TOOL_WORDS = ("sql", "db", "database", "postgres", "mysql", "sqlite", "mongo")


async def _containers(command: Command, scope: Scope) -> bool:
    words, head = command.words, command.words[0]
    if head in ("docker-compose", "podman-compose"):
        verbs = _plain(words, COMPOSE_OPTIONS_WITH_VALUE)
        return bool(verbs) and verbs[0] in COMPOSE_VERBS
    if head not in ("docker", "podman"):
        return False
    plain = _plain(words, DOCKER_OPTIONS_WITH_VALUE)
    if not plain:
        return False
    if plain[0] == "compose":
        verbs = _plain(words[words.index("compose"):], COMPOSE_OPTIONS_WITH_VALUE)
        return bool(verbs) and verbs[0] in COMPOSE_VERBS
    second = plain[1] if len(plain) > 1 else ""
    return (
        plain[0] in CONTAINER_VERBS
        or (plain[0] == "container" and second in CONTAINER_VERBS | {"prune"})
        or (plain[0] == "volume" and second in ("rm", "prune"))
        or (plain[0] == "system" and second == "prune")
    )


async def _services(command: Command, scope: Scope) -> bool:
    words, head = command.words, command.words[0]
    if head == "systemctl":
        plain = _plain(words, SYSTEMCTL_OPTIONS_WITH_VALUE)
        return bool(plain) and plain[0] in SYSTEMCTL_VERBS
    if head == "service":
        plain = _plain(words, set())
        return len(plain) > 1 and plain[1] in ("restart", "stop")
    return False


async def _force_push(command: Command, scope: Scope) -> bool:
    if command.words[0] != "git":
        return False
    sub, _at, args = policy._git_call(command.words)
    if sub != "push":
        return False
    return (
        "f" in policy._flags(args)
        or any(a == "--force" or a.startswith(("--force-with-lease", "--force-if-includes")) for a in args)
        or any(a.startswith("+") for a in args if not a.startswith("-"))
    )


async def _branch(folder: str) -> str | None:
    """The branch checked out in ``folder`` (``HEAD`` when detached), or ``None`` when git cannot say."""
    try:
        return (await run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=Path(folder), timeout=GIT_TIMEOUT_SECONDS)).strip() or None
    except (GitError, OSError):
        return None


async def _hard_reset_shared(command: Command, scope: Scope) -> bool:
    if command.words[0] != "git":
        return False
    sub, at, args = policy._git_call(command.words)
    if sub != "reset" or "--hard" not in args:
        return False
    folder = _absolute(at, command.where, scope.home) if at else command.where
    branch = await _branch(folder)
    if branch is None:
        return True  # the folder git ran in cannot be read any more: unknown is not "not shared"
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in scope.config.shared_branches)


async def _delete_outside(command: Command, scope: Scope) -> bool:
    words = command.words
    if words[0] != "rm":
        return False
    args = policy._without_redirects(words)[1:]
    if not ("r" in policy._flags(args) or "R" in policy._flags(args) or "--recursive" in args):
        return False
    variables = policy.shell_assignments(command.line)
    targets: list[str] = []
    ended = False
    for word in args:
        if word == "--" and not ended:
            ended = True
        elif ended or not word.startswith("-"):
            targets.append(word)
    for target in targets:
        expanded = policy.expand_home(policy._substitute(target, variables), scope.home)
        if "$" in expanded or "`" in expanded or target == "<xargs-input>":
            return True  # a path made at run time may land anywhere
        if not _under(_absolute(expanded, command.where, scope.home), scope.work_dir):
            return True
    return False


async def _drops_data(command: Command, scope: Scope) -> bool:
    head = command.words[0]
    if head == "dropdb" or (head == "mysqladmin" and "drop" in command.words[1:]):
        return True
    return head in DATABASE_CLIENTS | INTERPRETERS and DESTRUCTIVE_SQL.search(command.line) is not None


CommandCheck = Callable[[Command, Scope], Awaitable[bool]]


@dataclass(frozen=True)
class CommandRule:
    kind: str
    rule: str
    check: CommandCheck


COMMAND_RULES: tuple[CommandRule, ...] = (
    CommandRule("containers", "docker", _containers),
    CommandRule("services", "systemctl", _services),
    CommandRule("git_history", "git.force_push", _force_push),
    CommandRule("git_history", "git.reset_hard", _hard_reset_shared),
    CommandRule("delete_outside", "rm.outside", _delete_outside),
    CommandRule("database", "database.drop", _drops_data),
)
"""One row per thing a shell command may do that stops the goal. A new rule is a row: a kind the
operator can allow, a name for the log, and a check over one simple command."""


# -- the rules over other calls -------------------------------------------------------------------


async def _statement(call: Call, scope: Scope) -> str | None:
    """A destructive statement handed to a database tool (an MCP server's ``query``, say) rather than to a shell."""
    if call.tool in policy.SHELL_TOOLS:
        return None
    keys = [*STATEMENT_ARGUMENTS, *(("query",) if any(word in call.tool.lower() for word in DATABASE_TOOL_WORDS) else ())]
    for key in keys:
        value = call.arguments.get(key)
        if isinstance(value, str) and DESTRUCTIVE_SQL.search(value):
            return f"{call.tool}: {value}"
    return None


@dataclass(frozen=True)
class CallRule:
    kind: str
    rule: str
    check: Callable[[Call, Scope], Awaitable[str | None]]


CALL_RULES: tuple[CallRule, ...] = (CallRule("database", "database.tool", _statement),)


# -- the rules over the files the turn wrote ------------------------------------------------------


def _pattern_matches(pattern: str, path: str) -> bool:
    """``*`` crosses folders; a pattern that does not start at the root or with a star matches a path's end."""
    if not pattern.startswith(("/", "*")):
        pattern = "*/" + pattern
    return fnmatch.fnmatchcase(path, pattern)


async def _git_top(path: str) -> str | None:
    folder = posixpath.dirname(path)
    while folder and not os.path.isdir(folder):
        folder = posixpath.dirname(folder) if folder != "/" else ""
    if not folder:
        return None
    try:
        return (await run_git(["rev-parse", "--show-toplevel"], cwd=Path(folder), timeout=GIT_TIMEOUT_SECONDS)).strip() or None
    except (GitError, OSError):
        return None


async def _base(top: str, started: datetime | None) -> str:
    """The commit the goal began from: the last one before it started, ``""`` when the repository is younger."""
    try:
        if started is None:
            return (await run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=Path(top), timeout=GIT_TIMEOUT_SECONDS)).strip()
        return (await run_git(["rev-list", "-1", f"--before={started.isoformat()}", "HEAD"], cwd=Path(top), timeout=GIT_TIMEOUT_SECONDS)).strip()
    except (GitError, OSError):
        return ""


async def _at_start(path: str, scope: Scope) -> tuple[bool, str | None] | None:
    """``(existed when the goal began, its text then)``, or ``None`` when git cannot answer for the path."""
    top = await _git_top(path)
    if top is None:
        return None
    base = await _base(top, scope.started)
    if not base:
        return False, None
    rel = posixpath.relpath(os.path.realpath(path), os.path.realpath(top))
    try:
        return True, await run_git(["show", f"{base}:{rel}"], cwd=Path(top), timeout=GIT_TIMEOUT_SECONDS)
    except (GitError, OSError):
        return False, None


def _region(text: str, entry: MigrationListConfig) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.search(entry.start, line)), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if entry.end and re.search(entry.end, lines[i])), len(lines))
    region = lines[start:end]
    while region and not region[-1].strip():
        region.pop()
    return region


def _only_inserts(old: str, new: str) -> bool:
    """Whether an edit kept every line it replaced, in order, and only added lines between them."""
    remaining = iter(new.splitlines())
    return all(any(line == candidate for candidate in remaining) for line in old.splitlines())


async def _changed_list(path: str, writes: list[FileWrite], entry: MigrationListConfig, scope: Scope) -> bool:
    """Whether the region of a migration list lost or changed a line it held when the goal began."""
    then = await _at_start(path, scope)
    if then is not None:
        existed, old_text = then
        if not existed or old_text is None:
            return False
        before = _region(old_text, entry)
        try:
            now = _region(Path(path).read_text(encoding="utf-8"), entry)
        except (OSError, UnicodeDecodeError):
            return bool(before)  # the file is gone or unreadable, and it held migrations
        return now[: len(before)] != before
    # No git to compare with: read the edits themselves. A whole-file write cannot be told from a rewrite.
    return any(w.tool == "Write" or any(not _only_inserts(old, new) for old, new in w.edits) for w in writes)


async def _changed_existing_file(path: str, writes: list[FileWrite], scope: Scope) -> bool:
    then = await _at_start(path, scope)
    if then is not None:
        return then[0]
    # No git: an edit needs a file that was there, unless this turn wrote it first.
    return writes[0].tool != "Write"


async def _migrations(writes: list[FileWrite], scope: Scope) -> list[str]:
    by_path: dict[str, list[FileWrite]] = {}
    for write in writes:
        by_path.setdefault(write.path, []).append(write)
    changed: list[str] = []
    for path, own in by_path.items():
        entry = next((e for e in scope.config.migration_lists if _pattern_matches(e.path, path)), None)
        if entry is not None:
            if await _changed_list(path, own, entry, scope):
                changed.append(path)
        elif any(_pattern_matches(p, path) for p in scope.config.migration_paths):
            if await _changed_existing_file(path, own, scope):
                changed.append(path)
    return changed


async def _outside(writes: list[FileWrite], scope: Scope) -> list[str]:
    return list(dict.fromkeys(w.path for w in writes if not _under(w.path, scope.work_dir)))


@dataclass(frozen=True)
class WriteRule:
    kind: str
    rule: str
    check: Callable[[list[FileWrite], Scope], Awaitable[list[str]]]


WRITE_RULES: tuple[WriteRule, ...] = (
    WriteRule("migration", "migration.existing", _migrations),
    WriteRule("write_outside", "write.outside", _outside),
)


# -- the check ------------------------------------------------------------------------------------


async def check(calls: list[Call], scope: Scope, *, allowed: Iterable[str] = ()) -> list[Finding]:
    """Every rule the turn broke, once per thing that broke it, leaving out the kinds already allowed."""
    allow = set(allowed)
    found: dict[tuple[str, str], Finding] = {}

    def add(kind: str, rule: str, what: str) -> None:
        if kind not in allow:
            found.setdefault((kind, _clip(what)), Finding(kind, rule, _clip(what)))

    for call in calls:
        for command in commands_of(call, scope):
            for row in COMMAND_RULES:
                if row.kind not in allow and await row.check(command, scope):
                    add(row.kind, row.rule, command.line)
        for call_row in CALL_RULES:
            if call_row.kind not in allow and (what := await call_row.check(call, scope)):
                add(call_row.kind, call_row.rule, what)
    writes = writes_of(calls, scope)
    for write_row in WRITE_RULES:
        if write_row.kind not in allow:
            for what in await write_row.check(writes, scope):
                add(write_row.kind, write_row.rule, _shown(what, scope))
    return list(found.values())


def _shown(path: str, scope: Scope) -> str:
    """A path as the operator reads it: relative to the session's folder when it is inside."""
    top = scope.work_dir.rstrip("/")
    return path[len(top) + 1 :] if path.startswith(top + "/") else path


def digest(calls: list[Call], scope: Scope) -> Digest:
    files = list(dict.fromkeys(_shown(w.path, scope) for w in writes_of(calls, scope)))
    shell = [c for c in calls if c.tool in policy.SHELL_TOOLS]
    return Digest(files=files, commands=len(shell), failed=sum(1 for c in shell if c.failed))



def stop_text(findings: list[Finding], digest: Digest, lang: str) -> str:
    """The stop as the operator reads it, in the app's card, the notification and Telegram alike: what
    broke which rule, then what else the turn changed. The commands are quoted as the turn wrote them."""
    lines = [render("risk.intro", lang)]
    lines += [f"- {render('risk.kind.' + f.kind, lang)}: `{f.what.replace('`', chr(39))}`" for f in findings]
    lines.append("")
    if digest.files:
        shown = ", ".join(digest.files[:FILES_SHOWN]) + (" …" if len(digest.files) > FILES_SHOWN else "")
        lines.append(render("risk.files", lang, count=len(digest.files), names=shown))
    if digest.commands:
        lines.append(render("risk.commands", lang, count=digest.commands, failed=digest.failed))
    return "\n".join(lines).strip()


__all__ = [
    "CALL_RULES", "COMMAND_RULES", "KINDS", "WRITE_RULES", "Call", "CallRule", "Command", "CommandRule", "Digest", "FileWrite",
    "Finding", "Scope", "WriteRule", "check", "commands_of", "digest", "run_messages", "stop_text", "turn_calls", "writes_of",
]
