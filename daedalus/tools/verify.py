"""``Verify`` — a claim becomes a receipt the host recorded, not a sentence the model wrote.

The tool runs the check itself and stores what happened (command, exit code, output digest)
against the run. Self-development proposals attach the receipts of their session, so the
operator approves evidence, not confidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.processes import end_tree
from daedalus.security import redact
from daedalus.tools import search_hint
from daedalus.tools._common import FRAME_CHARS, clip, error, ok, services_for, tool_config
from daedalus.tools.shell import SandboxUnavailable, sandbox_argv, shell_environment

OUTPUT_HEAD_CHARS = 2000
#: How much of a model-written string the receipt line repeats. The criterion
#: and the dependency note are free text with no bound anywhere in the call
#: path, and they sit in the one line that has to survive: a header long enough
#: to push the framed result back over the per-call budget makes `ok`/`error`
#: clip it a second time, and that clip drops the middle — where the line saying
#: where the rest of the output went is written. The whole text is kept in the
#: verifications row, which is what the receipt dialog shows.
RECEIPT_TEXT_CHARS = 200
OUTPUT_TAIL_CHARS = 8000

# A runner's own footer. Anchored at the start of a line and ending in a duration, because the footer
# is a line the runner printed, not any line that happens to contain the words: an echoed example, a
# traceback quoting another run, or a test that prints "3 passed" must not become the count. The
# duration is required with decimals, which is what both runners print, so a prose line does not pass.
#
# pytest pads its default footer with `=` on both sides: `===== 1 passed in 0.00s =====`. The padding
# is part of the footer, not decoration in front of it, so it is allowed here and the line is otherwise
# the same line the quiet form prints. A short-run summary is also accepted without a duration, which is
# what a plugin that rewrites the footer leaves behind.
_TEST_FOOTER = re.compile(
    r"^[=\s]*(?P<counts>(?:\d+ (?:passed|failed|error|errors|xfailed|xpassed|skipped|deselected|warning|warnings)"
    r"(?:,\s*)?)+)\s+in\s+\d+\.\d+s\s*=*\s*$",
    re.MULTILINE,
)
_TEST_EMPTY = re.compile(r"^[=\s]*(?:no tests ran|no tests collected) in\s+\d+\.\d+s\s*=*\s*$", re.MULTILINE)
_TEST_COLLECTED = re.compile(r"^[=\s]*\d+ tests? collected in\s+\d+\.\d+s\s*=*\s*$", re.MULTILINE)
# unittest's footer: "Ran 5 tests in 0.001s". Words, not the pytest shapes.
_TEST_UNITTEST = re.compile(r"^\s*Ran (\d+) tests? in\s+\d+\.\d+s\s*$", re.MULTILINE)
# The outcomes that mean a test was executed. A deselected test was not run, so it is not counted;
# a skipped or xfailed one was collected and run, so it is.
_EXECUTED = ("passed", "failed", "error", "errors", "xfailed", "xpassed", "skipped")


def _is_test_run(command: str) -> bool:
    """Whether a command is a Python test runner invocation, read as an invocation and not as a word.

    ``cat pytest.ini`` and ``grep pytest log`` mention the runner; they do not run it. The command is
    split at shell separators and each segment's command word is judged, so a chain
    (``cd x && uv run python -m pytest``) is recognised and a pipeline that merely reads a file is not.
    Runners whose output this module does not parse (tox, nox, a wrapper script) are deliberately not
    claimed here: the receipt then records no count and the gate does not invent one.
    """
    for segment in re.split(r"&&|\|\||;|\|", command or ""):
        if _segment_runs_tests(_tokenize(segment)):
            return True
    return False


# Commands that wrap the real one without changing it: a package runner, a scheduler-side effect, a
# shell. Each is skipped, then its own options, then its subcommand word.
_LAUNCHERS = frozenset({
    "env", "sudo", "time", "timeout", "nice", "ionice", "stdbuf", "exec", "command", "nohup",
    "uv", "uvx", "poetry", "hatch", "pipenv", "pdm", "rye", "coverage", "bash", "sh", "zsh", "dash",
    "xargs",
})
# Launchers whose options take a value, so the value is not mistaken for the command word
# (`uv run --extra dev pytest`, `nice -n 5 pytest`, `stdbuf -oL pytest`, `coverage run --branch …`).
_VALUE_OPTIONS = frozenset({
    "-n", "-p", "--extra", "--with", "--python", "--directory", "--project", "--index", "-i",
    "--timeout", "-o", "--branch", "--source", "--include", "--rcfile", "--pythonpath",
})
_DURATION = re.compile(r"\d+[smhd]?$")


def _tokenize(segment: str) -> list[str]:
    """Split a command segment into words, honouring quotes; a quoted command is split again."""
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:  # unbalanced quotes: the plain split is the best reading available
        words = segment.strip().split()
    out: list[str] = []
    for word in words:
        if " " in word.strip():
            out.extend(word.split())  # `bash -lc 'pytest -q'`: the shell's own words
        else:
            out.append(word)
    return out


def _segment_runs_tests(tokens: list[str]) -> bool:
    """Whether one command segment, already tokenised, invokes a test runner."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", token):
            index += 1  # environment assignment in front of the command
            continue
        word = token.rsplit("/", 1)[-1]
        if word == "pytest" or word == "py.test":
            return True
        if re.fullmatch(r"python[0-9.]*", word):
            rest = tokens[index + 1 :]
            if "-m" in rest:
                at = rest.index("-m")
                module = rest[at + 1] if at + 1 < len(rest) else ""
                if module in ("pytest", "unittest"):
                    return True
                if module in ("coverage",):  # `python -m coverage run -m pytest`
                    index += 1 + at + 2
                    continue
            return False
        if word in _LAUNCHERS:
            index += 1
            continue
        if word in ("run", "exec"):
            index += 1  # a launcher's own subcommand (`uv run`, `poetry run`, `coverage run`)
            continue
        if token.startswith("-"):
            if token in _VALUE_OPTIONS:
                index += 2
            else:
                index += 1
            continue
        if _DURATION.fullmatch(token):
            index += 1  # `timeout 30 …`
            continue
        return False  # some other program: whatever follows is its argument, not a command
    return False



def _command_workdir(command: str, default: Path) -> Path:
    """The directory the command actually ran in.

    Receipts are usually run as ``cd /path/to/worktree && …``, and the tool's own ``cwd`` is then the
    session workspace: recording the tree of the wrong directory would be a claim about a checkout the
    tests never touched. A leading ``cd <dir> &&`` is honoured when that directory exists; otherwise the
    default stands. This reads the command, so it can be fooled by a command that changes directory
    later — the receipt says which directory it means, and a reader can see the command next to it.
    """
    match = re.match(r"\s*cd\s+(?P<dir>[^&;|]+?)\s*&&", command or "")
    if not match:
        return default
    candidate = Path(match.group("dir").strip().strip("'\""))
    if not candidate.is_absolute():
        candidate = default / candidate
    try:
        if candidate.is_dir():
            return candidate
    except OSError:
        return default
    return default


async def _tree_state(workdir: Path) -> str:
    """The commit the check ran against, marked when the working tree had uncommitted changes.

    Empty when the directory is not a git checkout, or when git cannot be run: the receipt then says
    nothing about the tree, which is different from saying the tree was clean.
    """
    async def git(*args: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(workdir), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (TimeoutError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").strip()

    head = await git("rev-parse", "HEAD")
    if not head:
        return ""
    dirty = await git("status", "--porcelain")
    return head[:40] + ("+worktree" if dirty else "")


# how many files one receipt may fingerprint, and the JSON ceiling for the result
MAX_DIGEST_FILES = 256
MAX_DIGEST_CHARS = 32768


def file_digest(path: Path) -> str:
    """A short, stable name for the bytes at ``path`` — or for the reason there are none.

    ``absent``, ``unreadable`` and the two link forms are answers, not omissions: a receipt that says a
    path was already gone is making a claim, and a claim that can be checked beats silence. A symlink is
    fingerprinted through its target's bytes, so retargeting it to different content moves the digest;
    only a link whose target cannot be read at all falls back to the link text, and it says so.
    """
    try:
        if path.is_symlink():
            if path.is_file():
                hasher = hashlib.sha256()
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        hasher.update(chunk)
                return "link:" + hasher.hexdigest()
            return "link-broken:" + hashlib.sha256(os.readlink(path).encode("utf-8", "replace")).hexdigest()[:32]
        if not path.is_file():
            return "absent"
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return "unreadable"


# A receipt whose checkout could not be fingerprinted carries this instead of a map: a non-empty
# object, because an empty one would read as "no claim" and hand the decision back to the clock.
UNFINGERPRINTED = "__unfingerprinted__"


async def _content_digests(workdir: Path) -> dict[str, str]:
    """The files this checkout differs by and what is in them, keyed by path from the repo root.

    "Differs by" means: different from the branch point with ``origin/main``, plus files git does not
    track yet. Those are the bytes a proposal can change, so those are the bytes its receipts have to
    cover; fingerprinting the whole tree would only say that something moved.

    The returned map is read by the gate as a claim about the whole change: every changed file has to be
    in it. So a directory that is not a git checkout at all yields ``{}`` — no claim, and the receipt is
    judged by the clock as receipts were before this column existed — while a checkout that *is* one but
    cannot be fingerprinted (no base commit, too many files, too large) yields a map holding
    ``UNFINGERPRINTED`` and a reason, which claims everything and covers nothing: a check whose tree
    cannot be named is not quietly downgraded to a timestamp.

    The comparison this enables is byte-for-byte. A formatter, a rebase or a fresh checkout that leaves
    the bytes alone no longer makes a receipt look stale, and an edit that keeps the old timestamp no
    longer slips past.
    """
    async def git(*args: str) -> bytes | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(workdir), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (TimeoutError, OSError):
            return None
        return out if proc.returncode == 0 else None

    top = await git("rev-parse", "--show-toplevel")
    if not top:
        return {}  # not a checkout: no claim, and the clock rule stands as it did before
    base = await git("merge-base", "origin/main", "HEAD")
    if base is None:
        base = await git("rev-parse", "HEAD")
    if base is None:
        return {UNFINGERPRINTED: "this checkout has no commit to compare against"}
    names: set[str] = set()
    # --full-name and the `:/` pathspec both matter: run from a subdirectory, git would print untracked
    # files relative to it and would list only the ones under it, while the digests are keyed from the
    # repository root the gate looks in. `:/` names the whole repository, so the claim is about the
    # change wherever the check happened to run from.
    for args in (
        ("diff", "--name-only", "-z", base.decode().strip()),
        ("ls-files", "--others", "--exclude-standard", "--full-name", "-z", ":/"),
    ):
        data = await git(*args)
        if data:
            names.update(name for name in data.decode("utf-8", "replace").split("\0") if name)
    if len(names) > MAX_DIGEST_FILES:
        return {UNFINGERPRINTED: f"this checkout differs by {len(names)} files, more than a receipt fingerprints"}
    root = Path(top.decode("utf-8", "replace").strip())
    out = {name: file_digest(root / name) for name in sorted(names)}
    if out and len(json.dumps(out, sort_keys=True)) > MAX_DIGEST_CHARS:
        return {UNFINGERPRINTED: "the fingerprints do not fit in a receipt"}
    return {UNFINGERPRINTED: "this checkout differs by no file at all"} if not out else out


def _test_counts(output: str) -> tuple[int | None, int | None]:
    """How many tests the run executed and skipped, from the runner's own footer.

    ``(None, None)`` when the output carries no footer this understands — the receipt then says it does
    not know, which is different from saying nothing ran. ``(0, 0)`` is a fact the runner stated: it
    ran nothing (``no tests ran``, or a collect-only run, which collected but executed none).

    Counts are read from the runner's own footer, and only from a line shaped like one. A test that
    prints the phrase, a logged example of a summary, or a summary pushed out of the window by a chatty
    reporter is not the runner reporting on itself.

    pytest's summary wins over unittest's when both appear. A pytest run can be made to print a
    unittest-shaped line from inside a test (``print("Ran 3 tests in 0.001s")``), and that line would
    otherwise override the real footer — which is the count the gate reads. unittest's shape is read
    only when no pytest footer is present, i.e. when unittest really was the runner.
    """
    text = output or ""
    checks: list[tuple[int, int, str]] = []
    for match in _TEST_FOOTER.finditer(text):
        executed = 0
        skipped = 0
        for number, outcome in re.findall(r"(\d+) (\w+)", match.group("counts")):
            if outcome not in _EXECUTED:
                continue  # deselected, warnings: not tests that ran
            value = int(number)
            if outcome == "skipped":
                skipped += value
            executed += value
        checks.append((executed, skipped, "footer"))
    if not checks:
        checks = [(int(m.group(1)), 0, "unittest") for m in _TEST_UNITTEST.finditer(text)]
    if not checks:
        if _TEST_EMPTY.search(text):
            return 0, 0
        if _TEST_COLLECTED.search(text):
            return 0, 0  # collected is not executed: nothing ran
        return None, None
    executed, skipped, _ = checks[-1]
    return executed, skipped


@search_hint(
    "verify claim run tests record receipt proof check passes evidence "
    "проверить проверь подтвердить тесты проходят доказать чек квитанция прогнать прогони"
)
@tool(
    name="Verify",
    description=(
        "Run a command that checks a claim and record the outcome as a verification receipt "
        "(criterion, command, exit code, output digest, time) tied to this run. Use it for the checks "
        "that back a statement such as 'tests pass' or 'the service answers': a receipt is what "
        "the operator sees on a change proposal, a sentence is not. Exit code 0 = verified. "
        "Pass dependencies to name the shared channels the observation rests on "
        "(e.g. 'container shell + provider API'), so a reviewer can see what the receipt does not cover. "
        "The receipt's time is the host clock; for time-sensitive claims, name the clock in dependencies."
    ),
)
async def verify(context: ToolContext, criterion: str, command: str, cwd: str | None = None, timeout_seconds: int | None = None, dependencies: str | None = None, env: dict[str, str] | None = None) -> ToolResult:
    # The criterion must be checkable by reading the command: a reviewer seeing only the command must be able to tell what the criterion asserts.
    services = services_for(context)
    manager = services.extra.get("manager")
    workdir = services.resolve(cwd)
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    if services.exec_backend is not None:
        # The check runs where the work is (a benchmark container); the receipt is recorded the same way.
        outcome = await services.exec_backend.run("set -o pipefail\n" + command, cwd=str(workdir), env=env, timeout=limit)
        raw = outcome.output.encode("utf-8", "replace")
        return await _receipt(context, services, manager, criterion, command, workdir, outcome.exit_code, hashlib.sha256(raw).hexdigest(), raw[: max(OUTPUT_HEAD_CHARS, services.max_tool_output_chars) * 4], raw[-OUTPUT_TAIL_CHARS:], len(raw), time.monotonic() - started, outcome.timed_out, False, False, dependencies)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    # A failure anywhere in a pipeline fails the check; `pytest | tail` must not pass on tail's exit code.
    try:
        argv, sandboxed = await sandbox_argv("set -o pipefail\n" + command, tool_config(context).exec, writable=services.sandbox_writable())
    except SandboxUnavailable as exc:
        return error(context, str(exc))
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workdir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=shell_environment(context.session_id, env), start_new_session=True,
    )
    # The head feeds two consumers: the OUTPUT_HEAD_CHARS-char DB field and the
    # model-facing clip at services.max_tool_output_chars. Cap the in-memory
    # head at the worst-case UTF-8 size of the larger (4 bytes/char), so a
    # talkative grandchild that outlives the direct child cannot pump memory
    # for the whole timeout window; the digest is streamed over the whole
    # output regardless.
    head_cap = max(OUTPUT_HEAD_CHARS, services.max_tool_output_chars) * 4
    hasher = hashlib.sha256()
    head = bytearray()
    tail = bytearray()
    total_bytes = 0

    async def _pump() -> None:
        nonlocal total_bytes
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(4096):
            hasher.update(chunk)
            total_bytes += len(chunk)
            if len(head) < head_cap:
                head.extend(chunk[: head_cap - len(head)])
            # The summary a runner prints sits at the end, which a head buffer never keeps.
            tail.extend(chunk)
            if len(tail) > OUTPUT_TAIL_CHARS:
                del tail[: len(tail) - OUTPUT_TAIL_CHARS]

    timed_out = False
    kill_failed = False
    try:
        await asyncio.wait_for(_pump(), timeout=limit)
        await asyncio.wait_for(proc.wait(), timeout=max(1.0, limit - (time.monotonic() - started)))
    except TimeoutError:
        timed_out = True
        try:
            end_tree(proc.pid, hard=True)
        except ProcessLookupError:
            pass
        except PermissionError:
            kill_failed = True  # the group is still running; the wait below is bounded so the run is not
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            kill_failed = True
    exit_code = -1 if timed_out else int(proc.returncode or 0)
    return await _receipt(context, services, manager, criterion, command, workdir, exit_code, hasher.hexdigest(), bytes(head), bytes(tail), total_bytes, time.monotonic() - started, timed_out, kill_failed, sandboxed, dependencies)


def _counts_from_output(tail_text: str, head_text: str) -> tuple[int | None, int | None]:
    """A run's counts, read from the tail first and the head second.

    A long run's footer is at the end of its output, which is what the tail holds; a short run's is at
    the start, which is what the head holds. The tail is tried first because a tail that decodes to
    junk is still a non-empty string, so a truthiness test would never look at the head.
    """
    run, skipped = _test_counts(tail_text)
    if run is None:
        run, skipped = _test_counts(head_text)
    return run, skipped


async def _receipt(context: ToolContext, services: Any, manager: Any, criterion: str, command: str, workdir: Path, exit_code: int, full_digest: str, head: bytes, tail: bytes, total_bytes: int, elapsed: float, timed_out: bool, kill_failed: bool, sandboxed: bool, dependencies: str | None) -> ToolResult:
    """Record the receipt and shape the answer; the same for a local process and a command run elsewhere."""
    if timed_out:
        exit_code = -1
    passed = exit_code == 0
    # The digest covers the raw bytes of the whole output, not a decoded copy: decode("replace") turns
    # invalid UTF-8 into U+FFFD, and re-encoding that would make the receipt hash a lossy copy.
    truncated = total_bytes > len(head)
    output = head.decode("utf-8", "replace")
    digest = full_digest[:16]
    at = datetime.now(UTC).isoformat()
    receipt_id = ""
    check_dir = _command_workdir(command, workdir)
    tree = await _tree_state(check_dir)
    # What the check ran against, as bytes rather than as a commit name: this is what lets the gate
    # answer "does this receipt cover the file in the tree?" with a comparison of contents.
    digests = await _content_digests(check_dir)
    # Read the tail first (a long run's footer is there), then the head: a tail that decodes to junk is
    # truthy, so `or` would never look at the head, and the head is where a short run's footer is.
    tests_run, tests_skipped = _counts_from_output(tail.decode("utf-8", "replace"), output)
    if manager is not None:
        # Receipts travel to proposal cards and pull-request bodies: nothing secret may be recorded.
        r = redact.shared()
        deps = (dependencies or "").strip()
        async with manager.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO verifications(session_id, run_id, criterion, command, cwd, exit_code, passed, output_digest, output_head, duration_ms, at, sandboxed, dependencies, tree, tests_run, tests_skipped, file_digests)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context.session_id, context.run_id, r.redact(criterion)[:300], r.redact(command)[:2000], str(workdir), exit_code, int(passed), full_digest,
                    r.redact(output[:OUTPUT_HEAD_CHARS]), int(elapsed * 1000), at, int(sandboxed), r.redact(deps)[:300],
                    tree, tests_run, tests_skipped, json.dumps(digests, sort_keys=True),
                ),
            )
            receipt_id = f"v{cursor.lastrowid}"
    deps = (dependencies or "").strip()
    header = f"{'✅ verified' if passed else '❌ NOT verified'}: {_receipt_text(criterion)} — exit {exit_code}{' (timed out' + ('; the process group survived the kill' if kill_failed else '') + ')' if timed_out else ''} · receipt {receipt_id or 'not recorded'} · digest {digest} · at {at}" + (f" · output {total_bytes} B, first {len(head)} B kept" if truncated else "") + (f" · deps: {_receipt_text(deps)}" if deps else "") + (f" · tree {tree}" if tree else "") + (f" · covers {len(digests)} changed file" + ("s" if len(digests) != 1 else "") if digests else "") + (f" · tests {tests_run} run" + (f", {tests_skipped} skipped" if tests_skipped else "") if tests_run is not None else "") + (" · sandbox=workspace" if sandboxed else "")
    # Sized against what the header actually leaves, not against a fixed reserve
    # a long receipt line would overrun.
    body = clip(output, max(services.max_tool_output_chars - len(header) - FRAME_CHARS, FRAME_CHARS), note="write the output to a file for the rest")
    text = f"{header}\n{body}" if body.strip() else header
    return ok(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code) if passed else error(context, text, receipt=receipt_id, passed=passed, exit_code=exit_code)


def _receipt_text(text: str) -> str:
    """The head of a model-written string, on one line, for the receipt header."""
    flat = " ".join(text.split())
    return flat if len(flat) <= RECEIPT_TEXT_CHARS else flat[: RECEIPT_TEXT_CHARS - 1] + "…"


TOOLS = [verify]

__all__ = ["TOOLS"]
