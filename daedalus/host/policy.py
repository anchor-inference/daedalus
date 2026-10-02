"""Tool policy: what a call may do, decided from its arguments before it runs.

The container is the agent's; the operator's machine, secrets, repositories and the outside world
are not. Governance says so in prose; this module says it in rules the dispatcher enforces on every
call. A rule looks at the tool and its arguments (a shell command is parsed into its simple commands
first, so ``cd x && rm -rf /`` is seen as ``rm -rf /``) and answers allow, deny or ask.

*deny* is final. *ask* is a denial the operator can lift: the answer carries an approval key, the
operator grants it (``/allow <key>`` in chat, the Mini App, or the API), and the same call passes
once. The built-in rules live here, in the repository, so they change only through a reviewed pull
request; the operator's rules in ``config.toml`` can add denials and questions but can never allow
what a built-in rule denies.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOW, ASK, DENY = "allow", "ask", "deny"
PATTERN_MAX_CHARS = 200
PATTERN_TEXT_CHARS = 20_000
"""An operator's regular expression runs on the event loop: a bounded pattern over a bounded text."""
_SEVERITY = {ALLOW: 0, ASK: 1, DENY: 2}

OPERATORS = {";", "&&", "||", "|", "(", ")", "&"}
KEYWORDS = {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "case", "esac", "in", "select", "function", "{", "}", "!", "[[", "]]", "[", "]", "coproc"}
"""Shell reserved words: they precede a simple command without being one, so `then rm -rf /` is `rm -rf /`."""
REDIRECTS = {"<", ">", ">>", "<<", "<<<", "<&", "2>", "2>>", "&>", "&>>", ">|"}
WRITE_REDIRECTS = (">", ">>", "&>", "&>>", ">|")
READ_REDIRECTS = ("<", "<<<", "<&")
"""The other direction. The lexer folds ``2<`` into ``2`` and ``<``, and ``<&3`` into ``<&`` and ``3``,
so one token is enough to find the operand of every input redirection there is."""
OPTIONS_WITH_PATH = {"-C", "--directory", "--cwd", "-o", "--output", "-i", "--input", "-f", "--file", "-T", "--upload-file", "-t", "--target-directory", "-d", "--data", "--data-binary", "--data-raw", "-F", "--form", "--config"}
"""Options whose value is a file or a directory rather than a setting. Read for what a command
touches, not for what it writes: ``curl -T`` uploads and ``-o`` downloads, and both name a path."""
WRAPPERS = {"sudo", "nohup", "time", "nice", "env", "exec", "command", "builtin", "stdbuf", "timeout"}
SHELLS = {"bash", "sh", "zsh", "dash"}
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "socat"}
_DANGEROUS_BASES = ("/", "/srv", "/opt", "/etc", "/usr", "/var", "/home", "/root", "/boot", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys", "/dev", "/run", "/tmp")
DANGEROUS_TARGETS = {"~", "~/", "~/*", "$HOME", "$HOME/*", "${HOME}"} | {form for base in _DANGEROUS_BASES for form in (base, base.rstrip("/") + "/", base.rstrip("/") + "/*")}
"""What a recursive delete or a recursive chmod must never be aimed at: the machine's own directories, whole or globbed."""
CONTAINER_CHECKOUTS = ("/srv/daedalus", "/srv/protocore-exp")
"""The operator's repositories where a container mounts them. What the agent may do with them depends on the
self-development mode, but pushing from them is never one of those things.

These are a fallback, not the answer: the host builds the policy with the checkouts' real paths from
``Settings``, which is what they are on a native installation — a folder the operator chose, with nothing
under ``/srv`` at all. Naming ``/srv`` there would guard a directory that does not exist while reading
exactly as if it guarded the checkouts."""
PUSH_REASON = {
    "server": "changes to the host and core go through SelfPropose",
    "local": "changes to the host and core stay in this checkout and apply after a restart",
    "off": "this installation does not change its own code",
}
"""Why the push is refused, in the terms of the mode the installation runs in: a reason that names a tool the
session does not have sends the agent looking for it."""
IDLE_WAIT_SECONDS = 30
"""A ``sleep`` this long, or a ``while``/``until`` loop around one, is the agent waiting for something — a
subagent's report, a background job — in the foreground of the very run that would receive it. The report
arrives as a message and wakes the run when it ends; the sleep only delays reading it. Shorter sleeps stay:
a server needs a moment to come up."""
WAIT_REASON = (
    "waiting in the foreground blocks the run that would receive what it waits for: a subagent's report and a "
    "finished job arrive as messages that wake you the moment they are ready. If there is nothing to do "
    "meanwhile, end the turn; to read a running job, use JobOutput; a service's log, ServiceLogs."
)
INSTALLATION_REASON = (
    "the installation's own files — the provider keys, the state database, the restart channel's secret, "
    "the launcher and the runtime this process runs out of — are not the agent's to read or to write"
)
"""Why a path under the installation is refused. Read as well as write, and that is the point: the file
mode says 0600 and the agent is the same user, so the mode is not what keeps it."""
HOME_REASON = (
    "it is in the operator's home folder, outside every project and outside the installation; there is no "
    "container here, so a path the operator has not opened is one they are asked about"
)
PATH_ARGUMENTS = frozenset({"path", "paths", "file", "files", "filename", "dir", "directory", "cwd", "root", "source", "src", "destination", "dest", "target", "output"})
"""The names the file tools give a path. Reading every string of every argument instead would take a
file's contents for a list of paths the moment one of them began with a slash."""
WRITERS = {"cp", "mv", "install", "rsync", "ln"}
"""Commands whose last non-flag argument is their destination."""
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    tool: str
    """A tool name, or ``*`` for every tool."""
    action: str
    note: str
    pattern: str = ""
    """A regular expression over the call's canonical text (a shell command, a URL, or the JSON of the arguments)."""
    source: str = "builtin"


@dataclass(slots=True)
class Decision:
    action: str
    reason: str = ""
    rule: str = ""
    key: str = ""
    """The approval key of an ``ask`` decision; a grant for it lets the same call through once."""
    hosts: list[str] = field(default_factory=list)
    """Network hosts the call reaches, for the egress log."""


SHELL_TOOLS = ("Exec", "Verify", "ServiceStart")
BROWSER_NAV_TOOLS = ("BrowserOpen", "BrowserNavigate")
"""The browser tools that take the agent to a URL; the address is judged before the page is asked for."""
BROWSER_SCHEMES = ("http", "https")
SENSITIVE_KINDS = ("credentials", "purchase", "send", "destroy", "accept", "upload", "cross_origin_post")
"""What the browser daemon calls an action that changes the world: ``docs/architecture/browser.md``."""
NEVER_ALLOWED_KINDS = frozenset({"credentials"})
"""No operator rule lifts these: typing a sign-in is the operator's, however much the site is trusted."""
SENSITIVE_WORDS = {
    "credentials": "submit a sign-in",
    "purchase": "buy or pay",
    "send": "send or publish something",
    "destroy": "delete or cancel something",
    "accept": "accept terms or consent",
    "upload": "upload a file",
    "cross_origin_post": "send a form to another site",
}


def canonical(tool: str, arguments: dict[str, Any]) -> str:
    """The text the rules read: the shell command, the URL, or the arguments as JSON."""
    if tool in SHELL_TOOLS:
        return str(arguments.get("command") or "")
    if tool in ("WebFetch", *BROWSER_NAV_TOOLS):
        return str(arguments.get("url") or "")
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def approval_key(tool: str, arguments: dict[str, Any]) -> str:
    """A grant is for one exact call: the key covers every argument (cwd, env, background …), not only the command."""
    return hashlib.sha256((tool + "\x00" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)).encode("utf-8")).hexdigest()[:12]


def _norm(path: str) -> str:
    """A path as the rules compare it: ``$HOME`` and ``~`` kept as written, ``..`` and doubled slashes folded."""
    if path.startswith(("~", "$")):
        return path
    if path.startswith("/"):
        folded = posixpath.normpath(path)
        return folded + "/*" if path.endswith("/*") and not folded.endswith("*") else folded
    return path


def shell_segments(command: str, *, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell command line, each as its argv, wrappers and reserved words stripped.

    ``cd a && sudo rm -rf /`` → ``[["cd", "a"], ["rm", "-rf", "/"]]``; ``if x; then rm -rf /; fi`` yields
    ``["rm", "-rf", "/"]`` too. ``bash -c "…"`` is opened one level, ``xargs`` and ``find -exec`` hand their
    command on. Unparseable input (an unbalanced quote) comes back as whitespace-split words: the rules
    still see every word.

    What a lexer cannot see, and this one does not claim to: a command assembled at run time (``eval``,
    ``$(...)``, backticks, a variable holding the verb), a heredoc fed to ``sh``, and code inside
    another interpreter (``python -c``, ``perl -e``). In a container that did not matter, because the
    container was the boundary for those. Natively there is no such boundary, so the sealed set is
    not left to the lexer at all: :func:`mentions_sealed` reads the whole command line as text, which
    catches the interpreter payload, the heredoc and the variable holding half the path alike.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in OPERATORS or tok in (";;", "&&", "||", "|&", "<", ">", ">>", "<<"):
            if tok in ("<", ">", ">>", "<<"):
                current.append(tok)
                continue
            if current:
                segments.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    out: list[list[str]] = []
    for seg in segments:
        words = list(seg)
        for _ in range(4):  # `then sudo env X=1 rm …`: keywords and wrappers alternate
            while words and words[0] in KEYWORDS:
                words.pop(0)
            while words and (words[0] in WRAPPERS or "=" in words[0] and not words[0].startswith(("-", "/", ".")) and words[0].split("=", 1)[0].isidentifier()):
                head = words.pop(0)
                if head == "timeout" and words and re.fullmatch(r"\d+[smhd]?", words[0]):
                    words.pop(0)
                if head in ("sudo", "env", "nice") and words and words[0].startswith("-"):
                    words.pop(0)
        if not words:
            continue
        if words[0] in SHELLS and depth < 3:
            inner = next((words[i + 1] for i, w in enumerate(words) if w in ("-c", "-lc", "-ec", "-ic") and i + 1 < len(words)), None)
            if inner:
                out.extend(shell_segments(inner, depth=depth + 1))
                continue
        if words[0] == "xargs" and len(words) > 1:
            handed = [w for w in _without_redirects(words)[1:] if not (w.startswith("-") and w[1:2] in ("0", "n", "I", "L", "P", "d", "a", "s", "t", "p"))]
            if handed and not handed[0].startswith("-"):
                out.append(handed + ["<xargs-input>"])
                continue
        if words[0] == "find":
            if "-delete" in words:
                out.append(["rm", "-r", *[w for w in words[1:] if not w.startswith("-") and w not in ("f", "d")][:1]])
            if "-exec" in words or "-execdir" in words:
                i = max(words.index(w) for w in ("-exec", "-execdir") if w in words)
                out.append([w for w in words[i + 1 :] if w not in (";", "\\;", "+", "{}")] + [w for w in words[1:2] if not w.startswith("-")])
            out.append(words)
            continue
        out.append(words)
    return out


def _flags(words: Iterable[str]) -> str:
    return "".join(w.lstrip("-") for w in words if w.startswith("-") and not w.startswith("--"))


def _redirect_targets(words: list[str]) -> list[str]:
    return _operands_after(words, WRITE_REDIRECTS)


def _read_targets(words: list[str]) -> list[str]:
    """The files a command reads through a redirection.

    A rule that only reads ``>`` guards the direction that writes a secret and leaves open the one
    that sends it: ``grep . < keyproxy.env`` and ``curl -d @keyproxy.env`` both hand the file to
    something else. These operands are reads, so they belong with the paths a command touches and
    not with the paths it writes.
    """
    return _operands_after(words, READ_REDIRECTS)


def _operands_after(words: list[str], tokens: tuple[str, ...]) -> list[str]:
    return [words[i + 1] for i, w in enumerate(words) if w in tokens and i + 1 < len(words)]


def _without_redirects(words: list[str]) -> list[str]:
    """The argv with redirections and their operands removed, so a destination heuristic sees the real arguments."""
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if w in REDIRECTS:
            skip = True
            continue
        out.append(w)
    return out


def _git_call(words: list[str]) -> tuple[str, str | None, list[str]]:
    """``(subcommand, repository path, the subcommand's own arguments)`` of a git invocation.

    The repository path is what ``-C``, ``--git-dir`` or ``--work-tree`` name, in either spelling;
    ``None`` when the command acts on the directory it runs in."""
    at = None
    i = 1
    while i < len(words):
        w = words[i]
        if w in GIT_OPTIONS_WITH_VALUE and i + 1 < len(words):
            if w in ("-C", "--git-dir", "--work-tree"):
                at = words[i + 1]
            i += 2
            continue
        if w.startswith("--") and "=" in w:
            if w.startswith("--git-dir=") or w.startswith("--work-tree="):
                at = w.split("=", 1)[1]
            i += 1
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w, at, words[i + 1 :]
    return "", at, []


def _git_subcommand(words: list[str]) -> tuple[str, str | None]:
    """``(subcommand, -C path)`` of a git invocation, skipping the options that take a value."""
    sub, at, _args = _git_call(words)
    return sub, at


def _linked_git_dir(path: str) -> str:
    """The git directory a linked worktree at ``path`` keeps its metadata in, or "": a worktree cut
    from a checkout writes into that checkout's ``.git`` wherever the worktree itself lives."""
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8") if (Path(path) / ".git").is_file() else ""
    except OSError:
        return ""
    return text.split(":", 1)[1].strip() if text.startswith("gitdir:") else ""


GIT_READS = {
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "cat-file", "blame", "grep", "describe",
    "shortlog", "rev-list", "merge-base", "for-each-ref", "show-ref", "name-rev", "check-ignore", "var", "help",
    "version", "whatchanged", "cherry", "range-diff", "annotate", "count-objects", "verify-commit", "verify-tag",
}
"""Git subcommands that only read the repository they are pointed at."""
GIT_LISTING_FORMS = {
    "branch": {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current", "--contains", "--merged", "--no-merged", "--points-at", "--format", "--sort", "--no-color", "--color"},
    "tag": {"-l", "--list", "-n", "--contains", "--merged", "--no-merged", "--points-at", "--format", "--sort"},
    "remote": {"-v", "--verbose", "get-url", "show"},
    "worktree": {"list"},
    "stash": {"list", "show"},
    "config": {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope", "--null", "-z", "--name-only", "--local", "--global", "--system", "--worktree", "--file", "-f", "--type", "--bool", "--int", "--path", "--default"},
    "reflog": {"show"},
    "notes": {"list", "show"},
}
"""Subcommands that read in one form and write in another, with the words of their reading forms.
A call made only of these words (and, for ``config``, the one name it reads) lists; any other word
makes it a write: a branch created, a remote added, a worktree cut."""


def _git_writes(sub: str, args: list[str]) -> bool:
    """Whether this git call may change the repository it acts on. Unknown subcommands are writes."""
    if sub in GIT_READS:
        return False
    listing = GIT_LISTING_FORMS.get(sub)
    if listing is None:
        return True
    words = [a.split("=", 1)[0] for a in args]
    if sub == "config":
        reads = any(w in ("--get", "--get-all", "--get-regexp", "--list", "-l") for w in words)
        return not reads or any(w.startswith("-") and w not in listing for w in words)
    if sub in ("branch", "tag"):
        # A plain word names a branch or a tag to create, unless a listing option takes it as its value.
        flags = [w for w in words if w.startswith("-")]
        plain = [w for w in words if not w.startswith("-")]
        return any(f not in listing for f in flags) or bool(plain and not ({"--list", "-l", "--contains", "--merged", "--no-merged", "--points-at", "--format", "--sort"} & set(flags)))
    if not words:
        return sub == "stash"  # a bare ``git stash`` stashes; every other bare form here lists
    return words[0] not in listing or any(w.startswith("-") and w not in listing for w in words[1:])


def _written_paths(head: str, words: list[str]) -> list[str]:
    """Where a command writes, by its own conventions: redirects, copy/move/link destinations, archive
    extraction directories, download output files, in-place edits."""
    targets = list(_redirect_targets(words))
    args = [w for w in _without_redirects(words)[1:]]
    plain = [w for w in args if not w.startswith("-")]
    if head in WRITERS and plain:
        targets.append(plain[-1])
        for i, w in enumerate(args):
            if w in ("-t", "--target-directory") and i + 1 < len(args):
                targets.append(args[i + 1])
            elif w.startswith("--target-directory="):
                targets.append(w.split("=", 1)[1])
    elif head == "tee":
        targets.extend(plain)
    elif head in ("curl", "wget"):
        for i, w in enumerate(args):
            if w in ("-o", "--output", "-O") and i + 1 < len(args):
                targets.append(args[i + 1])
            elif w.startswith("--output="):
                targets.append(w.split("=", 1)[1])
    elif head == "tar":
        for i, w in enumerate(args):
            if w in ("-C", "--directory") and i + 1 < len(args):
                targets.append(posixpath.join(args[i + 1], "*"))
            elif w.startswith("--directory="):
                targets.append(posixpath.join(w.split("=", 1)[1], "*"))
    elif head == "unzip":
        for i, w in enumerate(args):
            if w == "-d" and i + 1 < len(args):
                targets.append(posixpath.join(args[i + 1], "*"))
    elif head == "dd":
        targets.extend(w.split("=", 1)[1] for w in args if w.startswith("of="))
    elif head == "sed" and ("i" in _flags(args) or "--in-place" in args or any(w.startswith("--in-place") for w in args)):
        targets.extend(plain)
    return targets


def _host_of(token: str) -> str | None:
    """The host a network argument points at: a URL, ``user@host``, ``host:path``, or a bare name."""
    if "://" in token:
        return (urlsplit(token).hostname or "").lower() or None
    if "@" in token and not token.startswith("-"):
        return token.rsplit("@", 1)[1].split(":", 1)[0].lower() or None
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?", token) or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", token):
        return token.split(":", 1)[0].lower()
    return None


def hosts_in(segments: list[list[str]]) -> list[str]:
    hosts: list[str] = []
    for words in segments:
        if not words or words[0] not in NETWORK_COMMANDS and not (words[0] == "git" and len(words) > 1 and words[1] in ("clone", "fetch", "pull", "push", "ls-remote")):
            continue
        for tok in words[1:]:
            host = _host_of(tok)
            if host and host not in hosts:
                hosts.append(host)
    return hosts


_LOOPBACK_TARGET = re.compile(r"\b(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::(\d{1,5}))?", re.IGNORECASE)


def is_loopback(host: str) -> bool:
    """Whether this hostname names the machine the agent itself is running on."""
    host = host.strip().strip("[]").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def loopback_targets(text: str) -> list[tuple[str, int]]:
    """Every loopback address the text names, with the port where it gave one and 0 where it did not.

    Read off the words rather than off a parsed URL, because the forms are many — ``curl
    http://127.0.0.1:8770/…``, ``nc localhost 8765``, a ``--url`` with the address inside it — and
    all of them arrive at the same door.
    """
    return [(m.group(1).lower(), int(m.group(2) or 0)) for m in _LOOPBACK_TARGET.finditer(text)]


def host_allowed(host: str, allow: Iterable[str]) -> bool:
    for entry in allow:
        entry = entry.lower().strip()
        if not entry:
            continue
        if entry.startswith("*."):
            if host == entry[2:] or host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _sleep_seconds(args: list[str]) -> float:
    """How long ``sleep`` with these arguments waits: GNU suffixes, several operands added, ``infinity`` forever."""
    total = 0.0
    for arg in args:
        if arg.startswith("-"):
            continue
        m = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)([smhd]?)", arg)
        if m:
            total += float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        elif arg in ("inf", "infinity"):
            return float("inf")
    return total


def expand_home(path: str, home: str) -> str:
    """A path as the filesystem will see it, with ``~`` and ``$HOME`` resolved against this machine's home.

    The rules elsewhere compare ``~`` as written, because what they ask is whether the command aims at
    the home folder as a whole. These rules ask which directory a path lands in, and ``~/Documents`` and
    ``/home/ada/Documents`` land in the same one.
    """
    if not home:
        return _norm(path)
    if path == "~" or path.startswith("~/"):
        return _norm(home + path[1:])
    if path.startswith("~"):
        # `~dev/.ssh/id_rsa` is the same file as `~/.ssh/id_rsa` to the shell that runs it, and it
        # was a way of naming it the rules did not read at all.
        name, slash, rest = path[1:].partition("/")
        return _norm(_home_of(name, home) + slash + rest)
    for form in ("$HOME", "${HOME}"):
        if path == form or path.startswith(form + "/"):
            return _norm(home + path[len(form) :])
    return _norm(path)


def _home_of(user: str, home: str) -> str:
    """Another user's home folder. Asked of the password database, and where there is no answer —
    no such user, or a platform without one — guessed as a sibling of this one's, because a path
    that cannot be resolved is still a path into somebody's home and is not to be waved through."""
    try:
        import pwd  # Lazy: POSIX only, and only this one case needs it

        return pwd.getpwnam(user).pw_dir
    except (ImportError, KeyError):
        return posixpath.join(posixpath.dirname(home.rstrip("/")) or "/home", user)


def real_path(path: str, base: str = "") -> str:
    """Where a path lands on the filesystem: resolved against ``base`` when it is relative, symlinks
    followed, a tail that does not exist yet left alone.

    Folding ``..`` textually is not enough for a rule the agent can write against. The session
    workspace is the agent's own to write, so a link it makes there reads, to ``normpath``, as a
    path inside its own workspace and points wherever it likes.
    """
    try:
        return os.path.realpath(path if posixpath.isabs(path) else posixpath.join(base, path))
    except (OSError, ValueError):
        return _norm(path)


def sealed_root(path: str, roots: Iterable[str], *, base: str = "") -> str | None:
    """The root of ``roots`` that ``path`` lands in, or ``None``.

    The one answer to "is this path sealed?", asked of the filesystem rather than of the spelling,
    and asked by everything that names a path: the shell rules over a command's operands, the file
    tools over their arguments, and the file API over what a browser asks for. Written once so that
    a tool cannot be the one that forgot, which is how the first version of this leaked a key file
    through ``Read`` while ``Write`` refused the same path.
    """
    real = real_path(path, base)
    for root in roots:
        resolved = real_path(str(root))
        if real == resolved or real.startswith(resolved.rstrip("/") + "/"):
            return resolved
    return None


def _substitute(text: str, variables: dict[str, str] | None) -> str:
    """A path with the variables the same command line set expanded into it."""
    for name, value in (variables or {}).items():
        text = text.replace("${" + name + "}", value).replace("$" + name, value)
    return text


def real_under(path: str, roots: Iterable[str], *, base: str = "") -> bool:
    return sealed_root(path, roots, base=base) is not None


def mentions_sealed(text: str, roots: Iterable[str]) -> str | None:
    """A sealed directory named anywhere in a command line, whatever the shape around it.

    Parsing a shell command reaches what is written plainly and no further: ``python -c
    "open('…/keyproxy.env')"``, a heredoc fed to an interpreter, ``H=…; cat $H/keyproxy.env`` — the
    lexer sees a string, a word and a variable. A container used to be the wall behind that reading;
    natively there is none, so the sealed set is also matched as text. It is deliberately blunt: the
    sealed directories are the installation's own, and a command with one of their paths in it has
    no business the agent has.
    """
    for root in roots:
        at = text.find(str(root))
        while at >= 0:
            after = text[at + len(str(root)) : at + len(str(root)) + 1]
            if after in ("", "/", '"', "'", " ", "\t", "\n", ";", ")", "`"):
                return str(root)
            at = text.find(str(root), at + 1)
    return None


def _option_operands(words: list[str]) -> list[str]:
    """The values of the options that take a path rather than a setting, in either direction.

    ``curl -T secrets.env`` uploads a file and ``curl -o`` writes one; ``-C`` and ``--cwd`` move the
    command somewhere else before it runs, which makes the directory an operand of it. The ``@``
    forms belong here too: ``curl -d @file`` reads the file and posts it, and the word begins with a
    character that no filter looking for a path would keep.
    """
    out: list[str] = []
    for i, w in enumerate(words):
        value = ""
        if w in OPTIONS_WITH_PATH and i + 1 < len(words):
            value = words[i + 1]
        elif w.startswith("--") and "=" in w and w.split("=", 1)[0] in OPTIONS_WITH_PATH:
            value = w.split("=", 1)[1]
        if not value:
            continue
        # `-d @file` and `-F field=@file`: the payload is the file, not the word.
        out.append(value.split("=@", 1)[1] if "=@" in value else value.lstrip("@"))
    return out


def _archive_members(words: list[str]) -> list[str]:
    """What ``tar -C dir member …`` really names: the members are read from, or written into, ``dir``.

    Without this, ``-C`` is a directory nobody objects to and the members are relative words that
    resolve against the workspace, so a whole sealed directory archives out under a question that
    reads like a backup.
    """
    if not words or words[0].rsplit("/", 1)[-1] != "tar":
        return []
    directory, members, skip = "", [], False
    for i, w in enumerate(words[1:]):
        if skip:
            skip = False
            continue
        if w in ("-C", "--directory") and i + 2 < len(words):
            directory, skip = words[i + 2], True
            continue
        if w.startswith("--directory="):
            directory = w.split("=", 1)[1]
            continue
        if w.startswith("-"):
            skip = w in ("-f", "--file") or (not w.startswith("--") and w.endswith("f"))
            continue
        members.append(w)
    return [posixpath.join(directory, m) for m in members] if directory else []


_ASSIGNMENT = re.compile(r"(?:^|[;&|(]|\b(?:export|declare|local)\s)\s*([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]+)")


def shell_assignments(command: str) -> dict[str, str]:
    """The variables a command line sets itself, so that a later operand made out of one is read.

    ``H=/…/Daedalus; cat $H/daedalus-secrets/keyproxy.env`` names the key file as surely as writing
    it out does, and the lexer that reads the second half has already thrown the first half away.
    This reaches the value the same line assigned; a value from the environment, or one computed by
    a command, is still beyond it.
    """
    return {name: value.strip("\"'") for name, value in _ASSIGNMENT.findall(command)}


def path_operands(words: list[str]) -> list[str]:
    """Every operand of one simple command that names a file on the machine.

    Its plain arguments, the operands of its redirections in both directions, the members an archive
    takes from a directory of its own, and the paths only the command's own options reveal (``cp -t``,
    ``curl -o``, ``curl -d @``, ``tar -C``, ``dd of=``) — the same reading ``_written_paths`` already
    does, widened from where a command writes to what it touches at all.

    A relative operand is never skipped. It is resolved against the directory the command runs in,
    which is where the shell would resolve it: ``cat ../../daedalus-secrets/keyproxy.env`` names the
    key file as plainly as the absolute form does. A word that is not a path at all resolves inside
    the session's own workspace, which is open, so reading it as one costs nothing.
    """
    head = words[0].lstrip("\\").rsplit("/", 1)[-1]
    seen = [words[0], *(w for w in _without_redirects(words)[1:] if not w.startswith("-")), *_redirect_targets(words), *_read_targets(words), *_option_operands(words), *_archive_members(words), *_written_paths(head, words)]
    return [w for w in dict.fromkeys(seen) if w and not w.startswith("-")]


def argument_paths(arguments: Any) -> list[str]:
    """The paths a tool that is not a shell names, by the argument names the tools use for them."""
    out: list[str] = []

    def walk(node: Any, named: bool) -> None:
        if isinstance(node, str):
            if named:
                out.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item, named)
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, named or key in PATH_ARGUMENTS)

    walk(arguments, False)
    return out


def _under(path: str, roots: Iterable[str]) -> bool:
    p = path.rstrip("/") or "/"
    return any(p == r.rstrip("/") or p.startswith(r.rstrip("/") + "/") for r in roots)


class Policy:
    """The rule set: built-ins plus the operator's, evaluated per call."""

    def __init__(self, *, protected_paths: Iterable[Path] = (), egress_allow: Iterable[str] = (), rules: Iterable[Rule] = (), workspace_roots: Iterable[Path] = (), operator_checkouts: Iterable[Path] = (), selfdev_mode: str = "server", native: bool = False, home_dir: Path | str = "", project_roots: Iterable[Path] = (), worktrees_root: Path | str = "", sealed_paths: Iterable[Path] = (), sealed_everywhere: Iterable[Path] = (), sealed_ports: Iterable[int] = (), base_dir: Path | str = "") -> None:
        self.protected = [str(p) for p in protected_paths]
        self.egress_allow = [e for e in egress_allow if e.strip()]
        # The installation's own doors on the loopback interface: the app's API and the launcher's
        # action page. Refused by port rather than by hostname, so that a server the agent starts in
        # its own workspace and then checks with curl is untouched — which is work, and this is not.
        self.sealed_ports = {int(p) for p in sealed_ports if int(p) > 0}
        self.rules = list(rules)
        self.workspace_roots = [str(p) for p in workspace_roots]
        self.operator_checkouts = [str(p) for p in operator_checkouts] or list(CONTAINER_CHECKOUTS)
        self.selfdev_mode = selfdev_mode
        self.native = native
        self.home = str(home_dir) if home_dir else ""
        self.project_roots = [str(p) for p in project_roots]
        self.worktrees_root = str(worktrees_root) if worktrees_root else ""
        """Where self-development cuts its worktrees. An open root like the workspaces: the changes the
        agent makes to its own code are written there, and a home-folder question asked of every file
        of them would be a question about the agent's work rather than about the operator's."""
        self.sealed = [str(p) for p in sealed_paths]
        self.sealed_everywhere = [str(p) for p in sealed_everywhere]
        """The part of the sealed set a command may not name in a container either: the terminal
        daemons' run directories, whose token is a shell on the machine — the host one outside the
        container altogether. Everything else sealed is behind the container wall already."""
        self.base_dir = str(base_dir) if base_dir else ""
        """Where a relative path is resolved from when the call does not say: the session's own
        workspace, which is where the file tools resolve theirs and where Exec runs by default."""

    # -- the machine's own paths ------------------------------------------------------

    def _host_paths(self, paths: Iterable[str], *, base: str = "", variables: dict[str, str] | None = None) -> Decision | None:
        """The two rules a machine needs and a container does not, over the paths a call names.

        In Docker mode this answers nothing at all, and that is deliberate rather than an omission:
        the provider keys are in another container, the state directory is a volume the protected-path
        rules already refuse to write, and the operator's home is not mounted. Rules against them
        there would be sentences about directories that are not in the container.

        Natively the agent is a process of the operator's own user. Everything the container used to
        make impossible is now merely impolite, so the installation's own files are refused outright
        and the rest of the operator's home is a question rather than a silence.

        Every path is read as the filesystem will read it: relative to ``base`` (the directory the
        command runs in, or the session's workspace) and through the symlinks it is made of. The
        open roots are the ones the operator opened — the workspaces, the projects, the checkouts,
        and the worktrees self-development writes the agent's own changes in.
        The protected paths are deliberately not among them: they are a superset of the sealed set,
        so listing them here exempted the whole state directory, the operator's own pairing link
        included, from the question the rest of their home gets.
        """
        if not self.native:
            return None
        open_roots = self.workspace_roots + self.operator_checkouts + self.project_roots + ([self.worktrees_root] if self.worktrees_root else [])
        where = base or self.base_dir or (self.workspace_roots[0] if self.workspace_roots else "")
        asked: Decision | None = None
        for raw in paths:
            path = expand_home(_substitute(str(raw), variables), self.home)
            if path.startswith(("~", "$")):
                continue  # a home form with no home to resolve it against: nothing to compare
            target = path.rstrip("*").rstrip("/") or "/"
            if sealed_root(target, self.sealed, base=where) is not None:
                return Decision(DENY, f"{raw}: {INSTALLATION_REASON}", "host.installation")
            if asked is None and self.home and real_under(target, [self.home], base=where) and not real_under(target, open_roots, base=where):
                asked = Decision(ASK, f"{raw}: {HOME_REASON}", "host.home")
        return asked

    # -- built-in judgement -----------------------------------------------------------

    def _shell(self, command: str, cwd: str | None, *, foreground: bool = True) -> Decision:
        segments = shell_segments(command)
        variables = shell_assignments(command)
        hosts = hosts_in(segments)
        worst = Decision(ALLOW, hosts=hosts)
        checkouts = list(self.operator_checkouts)
        where = real_path(cwd, self.base_dir) if cwd else self.base_dir

        def escalate(action: str, reason: str, rule: str) -> None:
            nonlocal worst
            if _SEVERITY[action] > _SEVERITY[worst.action]:
                worst = Decision(action, reason, rule, hosts=hosts)

        if (host := self._host_paths([cwd] if cwd else [])) is not None:
            escalate(host.action, host.reason, host.rule)
        if (named := mentions_sealed(command, self.sealed if self.native else self.sealed_everywhere)) is not None:
            escalate(DENY, f"{named}: {INSTALLATION_REASON}", "host.installation")
        if re.search(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", command):
            escalate(DENY, "a fork bomb", "shell.forkbomb")
        if foreground:
            # A service or a background job may well sleep in a loop: that is its work. A foreground call cannot.
            if re.search(r"\b(while|until)\b[^\n]*\bsleep\b", command) and any(w[0] == "sleep" for w in segments):
                escalate(DENY, f"a polling loop: {WAIT_REASON}", "shell.wait")
            for words in segments:
                if words[0] == "sleep" and _sleep_seconds(words[1:]) >= IDLE_WAIT_SECONDS:
                    escalate(DENY, f"sleep {' '.join(words[1:])}: {WAIT_REASON}", "shell.wait")
        for words in segments:
            if words[0] == "cd" and len(words) > 1:
                where = real_path(expand_home(words[1], self.home), where)  # a `cd` earlier in the line moves every later command
            head = words[0].lstrip("\\")
            if head.startswith("/") and head.count("/") >= 2:
                head = head.rsplit("/", 1)[-1]  # /bin/rm is rm
            if head == "busybox" and len(words) > 1:
                words = words[1:]
                head = words[0]
            plain = [_norm(w) for w in _without_redirects(words)[1:] if not w.startswith("-")]
            if head in ("mkfs", "shutdown", "reboot", "halt", "poweroff", "init", "telinit") or head.startswith("mkfs."):
                escalate(DENY, f"`{head}` acts on the machine, not on the workspace", "shell.machine")
            if head == "dd" and any(w.startswith("of=/dev/") for w in words):
                escalate(DENY, "writing a raw device", "shell.rawdevice")
            if head == "rm":
                flags = _flags(words[1:])
                recursive = "r" in flags or "R" in flags or "--recursive" in words or "-r" in words
                if recursive and any(t in DANGEROUS_TARGETS or t.rstrip("/") in DANGEROUS_TARGETS for t in plain):
                    escalate(DENY, f"recursive delete of {', '.join(plain)}", "shell.rm_root")
                elif recursive and any(real_under(t, self.protected + checkouts, base=where) for t in plain):
                    escalate(DENY, f"recursive delete inside a protected path ({', '.join(plain)})", "shell.rm_protected")
                elif recursive and self.workspace_roots and any(t.rstrip("/") in self.workspace_roots or t.rstrip("/").endswith("/*") and t.rstrip("/*") in self.workspace_roots for t in plain):
                    escalate(DENY, "recursive delete of every workspace at once", "shell.rm_workspaces")
                elif recursive and self.workspace_roots and any(str(Path(t).parent).rstrip("/") in self.workspace_roots for t in plain):
                    escalate(ASK, "recursive delete of a whole workspace directory", "shell.rm_workspace")
                elif recursive and "<xargs-input>" in plain:
                    escalate(ASK, "recursive delete of paths piped through xargs (the targets are not visible here)", "shell.rm_piped")
            if head in ("chmod", "chown", "chgrp") and "R" in _flags(words[1:]) and any(t in DANGEROUS_TARGETS for t in plain):
                escalate(DENY, f"recursive `{head}` on a system path", "shell.chmod_root")
            for target in _written_paths(head, words):
                normed = _norm(target)
                if real_under(normed, self.protected + checkouts if head != "sed" else self.protected, base=where):
                    escalate(DENY, f"writing to a protected path ({target})", "shell.protected_write")
                elif normed in DANGEROUS_TARGETS or normed.rstrip("/*") in _DANGEROUS_BASES[1:]:
                    escalate(DENY, f"writing into a system directory ({target})", "shell.system_write")
            if (host := self._host_paths(path_operands(words), base=where, variables=variables)) is not None:
                escalate(host.action, host.reason, host.rule)
            if head == "git":
                sub, at, git_args = _git_call(words)
                acting_on = [real_path(expand_home(_substitute(at, variables), self.home), where)] if at else [where] if where else []
                acting_on += [real_path(variables[name], where) for name in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE") if variables.get(name)]
                acting_on += [linked for path in acting_on if (linked := _linked_git_dir(path))]
                if sub and sub != "push" and any(real_under(path, checkouts) for path in acting_on) and _git_writes(sub, git_args):
                    # A worktree cut in the checkout writes its metadata into the checkout's .git, where a
                    # session once redirected it to a directory of its own and broke git for the operator.
                    escalate(DENY, f"git {sub} would change the operator's checkout; its .git is the operator's — self-development works in the repository SelfWorkspace gives", "git.operator_repo")
                if sub == "push":
                    if any(w in ("-f", "--force") for w in words) and "--force-with-lease" not in words:
                        escalate(ASK, "a forced push (use --force-with-lease, or ask)", "git.force_push")
                    origin = real_path(expand_home(at, self.home), where) if at else where
                    if (origin and real_under(origin, checkouts)) or any(real_under(_norm(w), checkouts, base=where) for w in words[1:]):
                        escalate(DENY, f"pushing from the operator's checkout; {PUSH_REASON.get(self.selfdev_mode, PUSH_REASON["server"])}", "git.operator_push")
        for host, port in loopback_targets(command):
            if port in self.sealed_ports:
                escalate(DENY, f"{host}:{port} is this installation's own door — the launcher and the app's own API are asked through the app, which holds their keys", "egress.sealed_port")
        if self.egress_allow:
            blocked = [h for h in hosts if not host_allowed(h, self.egress_allow)]
            if blocked:
                escalate(ASK, f"network access to {', '.join(blocked)} is outside the egress allowlist", "egress.allowlist")
        return worst

    def _web(self, url: str) -> Decision:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        hosts = [host] if host else []
        try:
            port = parts.port or 0
        except ValueError:
            port = 0  # a URL whose port is not a number reaches nothing; nothing to refuse
        if port in self.sealed_ports and is_loopback(host):
            return Decision(DENY, f"{host}:{port} is this installation's own door — the launcher and the app's own API are asked through the app, which holds their keys", "egress.sealed_port", hosts=hosts)
        if self.egress_allow and host and not host_allowed(host, self.egress_allow):
            return Decision(ASK, f"fetching {host} is outside the egress allowlist", "egress.allowlist", hosts=hosts)
        return Decision(ALLOW, hosts=hosts)

    def _browser_url(self, url: str) -> Decision:
        """Where an agent may point the browser. Only web pages: ``file:`` reads the machine, ``data:``
        and ``blob:`` carry a page past every check of its address (the bypass another browser agent
        shipped with), and ``javascript:``, ``chrome:`` and ``view-source:`` are the browser's own
        powers. The network wall in the daemon judges every request again; this is the refusal the
        agent can read before anything is asked of the page."""
        text = url.strip()
        if not text:
            return Decision(ALLOW)
        scheme = urlsplit(text).scheme.lower()
        if scheme not in BROWSER_SCHEMES:
            what = f"{scheme}: URLs" if scheme else "an address without http:// or https://"
            # The eval saw models reach for view-source: and javascript: when a page did not show what they
            # wanted; the refusal names what reads a page instead.
            return Decision(DENY, f"the browser opens http and https pages only, not {what}; BrowserText reads a page's text and BrowserText(find=…) searches it", "browser.scheme")
        return self._web(text)

    def _config_rules(self, tool: str, text: str, current: Decision) -> Decision:
        """The operator's rules: a match can raise the severity; an ``allow`` can lower an ``ask`` that came from the
        egress allowlist or from another operator rule, never a built-in safety question and never a ``deny``."""
        sample = text[:PATTERN_TEXT_CHARS]
        for rule in self.rules:
            if rule.tool not in ("*", tool) or not rule.pattern or len(rule.pattern) > PATTERN_MAX_CHARS:
                continue
            try:
                if not re.search(rule.pattern, sample, re.DOTALL):
                    continue
            except re.error:
                continue
            if rule.action == ALLOW:
                if current.action == ASK and (current.rule.startswith("egress.") or current.rule.startswith("config.") or current.rule in {r.id for r in self.rules}):
                    current = Decision(ALLOW, f"allowed by rule {rule.id}", rule.id, hosts=current.hosts)
                continue
            if _SEVERITY[rule.action] > _SEVERITY[current.action]:
                current = Decision(rule.action, rule.note or f"matched rule {rule.id}", rule.id, hosts=current.hosts)
        return current

    def evaluate(self, tool: str, arguments: dict[str, Any], *, grants: Iterable[str] = ()) -> Decision:
        text = canonical(tool, arguments)
        if tool in SHELL_TOOLS:
            decision = self._shell(text, str(arguments.get("cwd") or "") or None, foreground=tool == "Exec" and not bool(arguments.get("background")))
        elif tool == "WebFetch":
            decision = self._web(text)
        elif tool in BROWSER_NAV_TOOLS:
            decision = self._browser_url(text)
        else:
            decision = self._host_paths(argument_paths(arguments)) or Decision(ALLOW)
        decision = self._config_rules(tool, text, decision)
        if decision.action == ASK:
            decision.key = approval_key(tool, arguments)
            if decision.key in set(grants):
                return Decision(ALLOW, f"granted by the operator ({decision.key})", decision.rule, key=decision.key, hosts=decision.hosts)
        return decision

    def describe(self) -> list[dict[str, str]]:
        builtins = [
            ("shell.forkbomb", "Exec", DENY, "fork bombs"),
            ("shell.machine", "Exec", DENY, "mkfs, shutdown, reboot, halt, poweroff"),
            ("shell.rawdevice", "Exec", DENY, "dd onto a raw device"),
            ("shell.rm_root", "Exec", DENY, "recursive delete of a system path"),
            ("shell.rm_protected", "Exec", DENY, "recursive delete inside a protected path or the operator's checkouts"),
            ("shell.rm_workspaces", "Exec", DENY, "recursive delete of every workspace at once"),
            ("shell.rm_workspace", "Exec", ASK, "recursive delete of a whole workspace directory"),
            ("shell.rm_piped", "Exec", ASK, "recursive delete of paths piped through xargs"),
            ("shell.system_write", "Exec", DENY, "extracting or copying into a system directory"),
            ("shell.chmod_root", "Exec", DENY, "recursive chmod/chown on a system path"),
            ("shell.protected_write", "Exec", DENY, "a redirect, copy, move, link, download, extraction or in-place edit onto a protected path or the operator's checkouts"),
            ("git.force_push", "Exec", ASK, "git push --force without --force-with-lease"),
            ("git.operator_push", "Exec", DENY, "git push from the operator's checkouts"),
            ("git.operator_repo", "Exec", DENY, "a git command that changes the operator's checkouts: a worktree, a branch, a commit, a fetch, a config write"),
            ("egress.allowlist", "Exec, WebFetch, BrowserOpen, BrowserNavigate", ASK, "a host outside the egress allowlist (when one is configured)"),
            ("browser.scheme", "BrowserOpen, BrowserNavigate", DENY, "an address that is not an http or https page: file:, data:, blob:, javascript:, chrome:"),
            ("browser.sensitive", "BrowserAct", ASK, "an action that changes the world: a sign-in submitted, a purchase, a message sent, a deletion, terms accepted, a file uploaded, a form sent to another site"),
        ]
        if self.native:
            # Only where they can fire. A list that names them in Docker mode would describe a boundary
            # the installation does not have, which is the opposite of what this endpoint is for.
            builtins += [
                ("host.installation", "*", DENY, "reading or writing the provider keys, the state database, the restart secret, the launcher or its runtime"),
                ("host.home", "*", ASK, "a path in the operator's home folder, outside every project and outside the installation"),
            ]
        rows = [{"id": i, "tool": t, "action": a, "note": n, "source": "builtin"} for i, t, a, n in builtins]
        rows += [{"id": r.id, "tool": r.tool, "action": r.action, "note": r.note, "pattern": r.pattern, "source": r.source} for r in self.rules]
        return rows


def browser_sensitive(*, tool: str, group: str, host: str, page_origin: str, kinds: Iterable[str], action: str, name: str, text: str, rules: Iterable[Any] = ()) -> Decision:
    """The policy's answer to an action the browser daemon classified as sensitive.

    Built in, every such action is asked about. The approval key covers the group, the page's origin,
    the element's accessible name, the action and a hash of the text typed, so a grant lets exactly
    that action through once: the same button on another site, or another message in the same box,
    is a new question. The operator's rules for a site (``[[browser.rules]]``) may refuse the kinds
    they name there, or let them through; they never let ``credentials`` through.
    """
    wanted = sorted({k for k in kinds if k in SENSITIVE_KINDS})
    if not wanted:
        return Decision(ALLOW)
    matching = [r for r in rules if host and host_allowed(host, [str(getattr(r, "domain", ""))])]
    for rule in matching:
        named = set(getattr(rule, "kinds", None) or ())
        if getattr(rule, "action", "") == DENY and (not named or named & set(wanted)):
            note = str(getattr(rule, "note", "") or "")
            return Decision(DENY, f"the operator does not let an agent {SENSITIVE_WORDS[wanted[0]]} on {host}" + (f" ({note})" if note else ""), "browser.rule")
    lifted: set[str] = set()
    for rule in matching:
        if getattr(rule, "action", "") == ALLOW:
            lifted |= set(getattr(rule, "kinds", None) or wanted)
    remaining = [k for k in wanted if k not in lifted - NEVER_ALLOWED_KINDS]
    if not remaining:
        return Decision(ALLOW, f"allowed by the operator's rule for {host}", "browser.rule")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    key = approval_key(tool, {"group": group, "origin": page_origin, "name": name, "action": action, "text_sha256": digest})
    words = " and ".join(SENSITIVE_WORDS[k] for k in remaining)
    return Decision(ASK, f"this would {words} on {host or page_origin}", "browser.sensitive", key=key)


__all__ = ["ALLOW", "ASK", "BROWSER_NAV_TOOLS", "DENY", "SENSITIVE_KINDS", "SHELL_TOOLS", "browser_sensitive", "Decision", "Policy", "Rule", "approval_key", "argument_paths", "canonical", "expand_home", "host_allowed", "hosts_in", "mentions_sealed", "path_operands", "real_path", "real_under", "sealed_root", "shell_assignments", "shell_segments"]
