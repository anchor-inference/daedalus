"""``exec`` — run a shell command in the session workspace."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.processes import end_tree
from daedalus.security import operator_secrets
from daedalus.tools import search_hint
from daedalus.tools._common import FRAME_CHARS, clip, error, ok, services_for, tool_config

_warned_missing_bwrap = False
_bwrap_state: str | None = None
"""Cached result of :func:`bwrap_status`: "ok", or the reason the sandbox cannot run here."""
_bwrap_probed_at = 0.0
PROBE_RETRY_SECONDS = 300.0
"""A failed probe is repeated after this long; a successful one is kept for the life of the process."""


class SandboxUnavailable(RuntimeError):
    """The configured sandbox cannot be created here; commands do not run unsandboxed instead."""


def _native() -> bool:
    """Whether this process is the agent on the operator's own machine. Read from the environment
    rather than from :mod:`daedalus.config`, which imports this module for the sandbox default."""
    return os.environ.get("DAEDALUS_NATIVE", "").strip().lower() in ("1", "true", "yes", "on")


def shell_argv(command: str, *, windows: bool | None = None) -> list[str]:
    """The program and arguments that run one shell command on this platform.

    Everywhere but Windows it is ``bash -lc``, as it has always been. Windows has no bash of its own:
    what the portable runtime's MinGit brings is ``usr/bin/sh.exe``, and that is what Exec runs there.
    In the non-busybox MinGit it is an MSYS2 bash under another name, but it is run as ``sh -c`` and
    the tool's description promises no more than a POSIX shell — a command written with arrays or
    ``[[ ]]`` may work and is not something to rely on. Under-promising here is deliberate: the
    busybox flavour of MinGit really does ship an ash, and which one is on the machine is not known.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows and (agent_bin := os.environ.get("DAEDALUS_AGENT_BIN", "")):
        # A login shell resets PATH after subprocess env was supplied. Activate only inside the
        # tool shell, never in the host interpreter which must retain its own locked dependencies.
        command = f"export PATH={shlex.quote(agent_bin)}:\"$PATH\"; {command}"
    if not windows:
        return ["bash", "-lc", command]
    return [windows_shell(), "-c", command]


def windows_shell() -> str:
    """The POSIX shell on a Windows installation: the one in the portable runtime's git, else
    whatever a git already on the machine brings with it, else the bare name and a clear failure."""
    if runtime := os.environ.get("DAEDALUS_RUNTIME", "").strip():
        candidate = Path(runtime) / "git" / "usr" / "bin" / "sh.exe"
        if candidate.exists():
            return str(candidate)
    if git := shutil.which("git"):
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
        if candidate.exists():
            return str(candidate)
    return "sh.exe"


def native_sandbox_note() -> str:
    """What the operator is told about isolation on a machine with no container around the agent.

    Said once, plainly, in the doctor, on the launcher's status page and in the README: what is gone
    and what is not. The policy engine, the approval gates, the protected paths, the network allowlist
    and the spend caps are in the agent and all still apply. The wall behind them is what a container
    was, and what stands where it stood is a question the operator answers.
    """
    if not sys.platform.startswith("linux"):
        return "native mode: no container boundary and no bubblewrap on this platform; the policy asks before leaving the project, and the approval gates are what stands behind it."
    status = bwrap_status()
    if status != "ok":
        return f"native mode: no container boundary, and no sandbox for Exec on this machine ({status}); the policy asks before leaving the project, and the approval gates are what stands behind it."
    # What the sandbox is and is not: it binds / read-only, so it confines what a command writes and
    # not what it reads. Every file on the machine, the sealed set included, is readable inside it —
    # what refuses those is the policy, and saying otherwise here would offer a wall that is not there.
    return "native mode: no container boundary; the policy asks before leaving the project, and bubblewrap confines what Exec writes while tools.exec.sandbox = workspace (the default here). It binds the filesystem read-only rather than hiding it, so it is a wall against writing, not against reading."


def bwrap_status() -> str:
    """Whether bubblewrap can create namespaces here: "ok", or why not.

    In a container the usual answer is Docker's default seccomp profile; on a machine it is a kernel
    that forbids unprivileged user namespaces, which Ubuntu 24.04 and Debian 13 do out of the box.
    The probe runs the sandbox's own flags, so what it answers is what the sandbox would do, and the
    answer is cached — once when it works, for five minutes when it does not, so that a machine given
    namespaces later picks them up without a restart.

    Blocking (it runs a subprocess): call it through ``asyncio.to_thread`` from the event loop.
    """
    global _bwrap_state, _bwrap_probed_at
    if _bwrap_state == "ok" or (_bwrap_state is not None and time.monotonic() - _bwrap_probed_at < PROBE_RETRY_SECONDS):
        return _bwrap_state
    _bwrap_probed_at = time.monotonic()
    if not sys.platform.startswith("linux"):
        # bubblewrap is a Linux facility built on user namespaces. There is nothing to probe for on
        # macOS or Windows, and probing anyway would report it as missing software rather than as
        # the platform it is.
        _bwrap_state = f"bubblewrap is a Linux facility; {sys.platform} has no sandbox for Exec"
        return _bwrap_state
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        _bwrap_state = "bwrap is not installed"
        return _bwrap_state
    try:
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "true"], capture_output=True, text=True, timeout=20)
        remedy = (
            "this machine restricts unprivileged user namespaces, so a fresh installation starts with tools.exec.sandbox = off; allowing them (sysctl kernel.apparmor_restrict_unprivileged_userns=0) and setting it back to workspace is what turns the sandbox on"
            if _native()
            else "the container needs cap_add SYS_ADMIN and an unconfined seccomp profile"
        )
        _bwrap_state = "ok" if probe.returncode == 0 else f"bwrap cannot create namespaces here: {(probe.stderr or probe.stdout).strip()[:120]} ({remedy})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        _bwrap_state = f"bwrap probe failed: {type(exc).__name__}"
    return _bwrap_state


def operator_git_dirs(checkouts: Sequence[Path] | None = None) -> list[Path]:
    """The git directories of the operator's checkouts, which no sandboxed command may write.

    ``checkouts`` defaults to the two this installation runs from, as the environment names them. A
    checkout whose ``.git`` is a file (itself a worktree) has two directories to seal: its own and
    the common one its ``commondir`` names.
    """
    if checkouts is None:
        checkouts = [Path(os.environ.get("BOT_REPO_DIR", "/srv/daedalus")), Path(os.environ.get("CORE_REPO_DIR", "/srv/protocore-exp"))]
    found: list[Path] = []
    for checkout in checkouts:
        dotgit = checkout / ".git"
        if dotgit.is_dir():
            found.append(dotgit)
            continue
        try:
            text = dotgit.read_text(encoding="utf-8") if dotgit.is_file() else ""
        except OSError:
            text = ""
        if not text.startswith("gitdir:"):
            continue
        gitdir = Path(text.split(":", 1)[1].strip())
        found.append(gitdir)
        try:
            common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        except OSError:
            common = ""
        if common:
            found.append(Path(os.path.normpath(gitdir / common)))
    return [path for path in dict.fromkeys(found) if path.is_dir()]


async def sandbox_argv(
    command: str, exec_config: Any, *, writable: Sequence[Path], sealed: Sequence[Path] | None = None, session_id: str | None = None,
) -> tuple[list[str], bool]:
    """The argv to run ``command`` with: plain bash, or bash inside bubblewrap when the sandbox is on.

    The sandbox binds the whole filesystem read-only, makes ``writable`` (the session's writable
    walls and the paths the host opened for it, from ``SessionServices.sandbox_writable``) and any
    configured extra path writable, gives the command a private /tmp and PID namespace, and dies
    with the parent so a timeout kill cannot leave it behind. Everything else is entered read-only.

    The session's workspace is not bound on its own account: a workspace in a folder marked
    read-only must stay read-only here too, and whether it is writable is the walls' answer.

    The operator's checkouts' git directories (``sealed``, by default :func:`operator_git_dirs`) are
    bound read-only last, over whatever the writable binds opened, so no wall, opened worktree or
    configured extra path can make them writable. A session once rewrote a worktree's ``commondir``
    inside the operator's ``.git`` through exactly such a bind, and every ``git fetch`` in the
    checkout failed until the entries were removed by hand. Self-development's worktrees belong to a
    repository of the agent's own, so nothing a session legitimately does needs these directories.

    The folder of the operator's secrets' files is hidden behind an empty one, and only the scopes
    ``session_id`` draws from are bound back, read-only: the read-only root bind would otherwise show a
    session every other chat's and project's secrets.
    """
    global _warned_missing_bwrap
    plain = shell_argv(command)
    if getattr(exec_config, "sandbox", "off") != "workspace":
        return plain, False
    status = await asyncio.to_thread(bwrap_status)
    if status != "ok":
        if not _warned_missing_bwrap:
            logging.getLogger(__name__).warning("tools.exec.sandbox=workspace but the sandbox is unavailable (%s); commands are refused until it is", status)
            _warned_missing_bwrap = True
        # Fail closed: a sandbox the operator asked for and did not get is not a warning, it is a missing wall.
        where = "allow unprivileged user namespaces on this machine" if _native() else "enable namespaces for the container"
        raise SandboxUnavailable(f"the sandbox is configured (tools.exec.sandbox=workspace) but unavailable: {status}. The operator can switch it off in Settings → Tools or {where}.")
    bwrap = shutil.which("bwrap") or "bwrap"
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--unshare-pid", "--die-with-parent", "--new-session"]
    paths = [*writable, *[Path(p) for p in getattr(exec_config, "sandbox_extra_writable", [])]]
    bound: set[str] = set()
    for path in paths:
        # A bind needs a real directory at both ends: a symlink or a file here makes bubblewrap refuse the
        # whole command, which is a broken sandbox for everything, not one path left read-only.
        if path.is_symlink() or not path.is_dir() or str(path) in bound:
            continue
        bound.add(str(path))
        argv += ["--bind", str(path), str(path)]
    for path in operator_git_dirs() if sealed is None else sealed:
        if not path.is_symlink() and path.is_dir():
            argv += ["--ro-bind", str(path), str(path)]
    store = operator_secrets.shared()
    if store is not None and store.files_dir is not None and store.files_dir.is_dir():
        argv += ["--tmpfs", str(store.files_dir)]
        for scope in store.scope_dirs(session_id or ""):
            if scope.is_dir():
                argv += ["--ro-bind", str(scope), str(scope)]
    return argv + shell_argv(command), True


# Environment names a tool's subprocess may inherit. Everything else —
# including any secret name the bot's environment gains later — stays in
# the bot's process unless the caller passes it explicitly through the
# tool's ``env`` parameter.
_SAFE_ENV_BASE = frozenset({
    # process basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "HOSTNAME", "PWD", "SHLVL",
    "TMPDIR", "TEMP", "TMP",
    # locale / timezone
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
    # editor / pager preferences (harmless)
    "EDITOR", "VISUAL", "PAGER",
    # apt in the sandbox
    "DEBIAN_FRONTEND",
    # python / uv runtime (the bot's toolchain)
    "PYTHONUNBUFFERED", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    # network: a proxy or a private CA the container was given must reach every client
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # git over ssh
    "SSH_AUTH_SOCK",
    # git authentication: the supervisor's credential helper reads GH_TOKEN, and gh reads it too.
    # Kept on purpose: the operator wants the shell able to fetch, push and use gh; the token is
    # the agent's own (a fine-grained PAT scoped to its repositories).
    "GH_TOKEN", "GH_ORG_TOKEN",
    # The headless Chromium the browser skills drive (Playwright's browser store, Lighthouse's binary).
    "PLAYWRIGHT_BROWSERS_PATH", "CHROME_PATH",
})
"""Names inherited by a tool's subprocess. Prefixes in :data:`_SAFE_ENV_PREFIXES` are inherited as well."""

_SAFE_ENV_PREFIXES = ("GIT_", "UV_", "PIP_", "NPM_CONFIG_", "NODE_", "LC_", "XDG_", "DAEDALUS_", "CARGO_", "GOPATH", "GOFLAGS", "JAVA_")
"""Variable families a toolchain reads; none of them carries the bot's own credentials."""

_SECRET_ENV = re.compile(r"^(TELEGRAM_.*|KEYPROXY_.*|.*_API_KEY|.*_SECRET|.*_PASSWORD|GITHUB_TOKEN)$")
"""Never inherited even when a prefix would admit them."""


def _own_checkout(cwd: str | Path | None) -> bool:
    """Whether ``cwd`` lies in one of the checkouts this installation runs from."""
    if cwd is None:
        return False
    here = Path(cwd).resolve()
    for checkout in (os.environ.get("BOT_REPO_DIR", "/srv/daedalus"), os.environ.get("CORE_REPO_DIR", "/srv/protocore-exp")):
        if here.is_relative_to(Path(checkout).resolve()):
            return True
    return False


def shell_environment(session_id: str, extra: dict[str, str] | None = None, *, cwd: str | Path | None = None) -> dict[str, str]:
    """The environment a tool's subprocess gets: what a shell and its toolchains need, without the bot's own credentials.

    The operator's secrets the session may use are in it (``DAEDALUS_SECRET_<NAME>`` and ``…_FILE``): this
    is the one door every command, job, check, service and hook script of a session goes through.

    This is hygiene, not containment: the bot's Telegram and provider credentials do not
    propagate into child processes and their logs, but a shell in the same container can still
    read the parent's environment through ``/proc``. A caller that needs a specific value passes
    it through the tool's ``env`` parameter.

    ``UV_PROJECT_ENVIRONMENT`` names the bot's own virtualenv and goes only to a command running in
    the bot's own checkouts (``cwd``). Handed to every command, it made ``uv add`` in any workspace
    project install into that virtualenv instead of the project's ``.venv``, which the sandbox
    mounts read-only: the agent saw "Read-only file system" and could not add a library at all.
    """
    env = {k: v for k, v in os.environ.items() if (k in _SAFE_ENV_BASE or k.startswith(_SAFE_ENV_PREFIXES)) and not _SECRET_ENV.match(k)}
    if not _own_checkout(cwd):
        env.pop("UV_PROJECT_ENVIRONMENT", None)
    # Never one of the host's own: the prefix is the operator's handed-over secrets', and only the session's
    # own go in, below.
    env = {k: v for k, v in env.items() if not k.startswith(operator_secrets.ENV_PREFIX)}
    if (store := operator_secrets.shared()) is not None:
        env.update(store.environment(session_id))
    env.update(extra or {})
    if agent_bin := os.environ.get("DAEDALUS_AGENT_BIN", ""):
        env["PATH"] = agent_bin + os.pathsep + env.get("PATH", "")
    env["DAEDALUS_SESSION_ID"] = session_id
    return env


def _with_secrets(session_id: str, command: str, env: dict[str, str] | None, *, remote: bool) -> tuple[str, dict[str, str] | None]:
    """The command with the operator's secrets' placeholders turned into their variables, and the
    environment it runs with. A local command gets the variables from :func:`shell_environment`; a
    command on another machine (the operator's host) gets them here, without the file paths, which name
    files on this one. Whatever the command names is recorded as a use."""
    store = operator_secrets.shared()
    if store is None:
        return command, env
    command, named = store.rewrite_command(command, session_id)
    if named:
        store.record_use(named, f"Exec in {session_id}")
    if remote and (variables := store.environment(session_id, files=False)):
        env = {**variables, **(env or {})}
    return command, env


@search_hint(
    "run shell command bash terminal script execute install output "
    "выполнить выполни запустить запусти команду баш шелл скрипт консоль шелле"
)
@tool(
    name="Exec",
    description=(
        "Run a shell command with bash. The working directory defaults to the session "
        "workspace. Output (stdout and stderr, interleaved) is returned; very long output "
        "is clipped to its head and tail and the whole of it is kept in a file the result names. "
        "background=true starts the command as a job and returns at once with a job id: "
        "JobOutput reads its output, JobKill stops it, JobList shows the jobs; use it for servers, "
        "builds and anything longer than a few minutes instead of holding this call open. "
        "When a job ends you are told, with its exit code and the last lines of its log, "
        "unless you already read its end yourself; there is no need to poll it."
    ),
)
async def exec_command(
    context: ToolContext,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
) -> ToolResult:
    services = services_for(context)
    workdir = services.resolve(cwd)
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    command, env = _with_secrets(context.session_id, command, env, remote=services.exec_backend is not None)
    if background:
        if services.exec_backend is not None:
            return await _start_remote_job(context, services, command, workdir, env)
        if not workdir.exists():
            return error(context, f"working directory does not exist: {workdir}")
        return await _start_job(context, services, command, workdir, env)
    if services.exec_backend is not None:
        outcome = await services.exec_backend.run(command, cwd=str(workdir), env=env, timeout=limit)
        elapsed = time.monotonic() - started
        body = clip(outcome.output, max(services.max_tool_output_chars - FRAME_CHARS, FRAME_CHARS), note="write to a file for the full output")
        header = f"exit_code={outcome.exit_code} elapsed={elapsed:.1f}s cwd={workdir}" + (f" TIMED OUT after {limit:.0f}s" if outcome.timed_out else "")
        if outcome.timed_out:
            header += REMOTE_TIMEOUT_HINT
        text = f"{header}\n{body}" if body else header
        if outcome.timed_out or outcome.exit_code != 0:
            return error(context, text, exit_code=outcome.exit_code, timed_out=outcome.timed_out)
        return ok(context, text, exit_code=outcome.exit_code)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    environment = shell_environment(context.session_id, env, cwd=workdir)
    try:
        argv, sandboxed = await sandbox_argv(command, tool_config(context).exec, writable=services.sandbox_writable(), session_id=context.session_id)
    except SandboxUnavailable as exc:
        return error(context, str(exc))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    # The budget belongs to the whole result, so the head and the tail are sized
    # against what is left once the exit-code header and the omitted-bytes note
    # have had their room.
    limit_chars = max(services.max_tool_output_chars - FRAME_CHARS, FRAME_CHARS)
    head_chars = int(limit_chars * 0.7)
    tail_chars = int(limit_chars * 0.25)
    # Bytes, and deliberately generous: one character costs up to 4 of them, so
    # these caps are what it takes to be sure the head and tail buffers hold
    # enough. They bound MEMORY, not the answer — the answer is cut in
    # characters below, which is the unit the model's budget is counted in.
    head_cap = head_chars * 4
    tail_cap = tail_chars * 4
    chunks: list[bytes] = []
    tail_chunks: deque[bytes] = deque()
    tail_size = 0
    total = 0
    spill = _spill_path(services, context)
    spill_fh = None
    spill_written = 0

    async def _pump() -> None:
        nonlocal total, tail_size, spill_fh, spill_written
        assert proc.stdout is not None
        last_progress = time.monotonic()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            if total + len(chunk) > head_cap:
                # Past the head: the whole stream goes to a file (up to a cap) and a ring keeps the tail for the answer.
                if spill_fh is None and spill_written == 0:
                    try:
                        spill.parent.mkdir(parents=True, exist_ok=True)
                        _prune_spills(spill.parent)
                        spill_fh = spill.open("wb")
                        spill_fh.write(b"".join(chunks))
                        spill_written = sum(len(c) for c in chunks)
                    except OSError:
                        spill_fh = None
                if spill_fh is not None:
                    if spill_written + len(chunk) <= SPILL_MAX_BYTES:
                        try:
                            spill_fh.write(chunk)
                            spill_written += len(chunk)
                        except OSError:
                            spill_fh.close()
                            spill_fh = None
                    else:
                        spill_fh.write(b"\n[... spill capped at %d bytes ...]\n" % SPILL_MAX_BYTES)
                        spill_fh.close()
                        spill_fh = None
            if total < head_cap:
                chunks.append(chunk[: head_cap - total])
            else:
                tail_chunks.append(chunk)
                tail_size += len(chunk)
                while tail_size > tail_cap and len(tail_chunks) > 1:
                    tail_size -= len(tail_chunks.popleft())
            total += len(chunk)
            if services.progress is not None and time.monotonic() - last_progress > 2.0:
                last_progress = time.monotonic()
                tail = chunk.decode("utf-8", "replace").strip().splitlines()
                if tail:
                    await services.progress(tail[-1][:160])

    timed_out = False
    try:
        try:
            await asyncio.wait_for(_pump(), timeout=limit)
            remaining = max(1.0, limit - (time.monotonic() - started))
            await asyncio.wait_for(proc.wait(), timeout=remaining)
        except TimeoutError:
            timed_out = True
    finally:
        if proc.returncode is None:
            # A timeout, a cancelled run or a failed write: the process group never outlives the call.
            try:
                end_tree(proc.pid, hard=True)
            except ProcessLookupError:
                pass
            await proc.wait()
        if spill_fh is not None:
            spill_fh.close()
    head_text = b"".join(chunks).decode("utf-8", "replace")[:head_chars]
    tail_text = b"".join(tail_chunks).decode("utf-8", "replace")[-tail_chars:] if tail_chars else ""
    elapsed = time.monotonic() - started
    # What the model is actually shown, measured back in bytes so the figure
    # below and the spill file speak the same unit as the stream they describe.
    shown = len(head_text.encode("utf-8", "replace")) + len(tail_text.encode("utf-8", "replace"))
    if total > shown:
        where = f"full output in {spill}" if spill_written else "the rest was not kept; write the output to a file"
        if spill_written and spill_written < total:
            where = f"the first {spill_written} bytes are in {spill}; write the output to a file for the rest"
        body = head_text + f"\n\n[... {total - shown} of {total} bytes omitted — {where} ...]\n\n" + tail_text
    else:
        body = head_text + tail_text
    header = f"exit_code={proc.returncode} elapsed={elapsed:.1f}s cwd={workdir}" + (" sandbox=workspace" if sandboxed else "")
    if timed_out:
        header += f" TIMED OUT after {limit:.0f}s (process group killed)" + LOCAL_TIMEOUT_HINT
    text = f"{header}\n{body}" if body else header
    if timed_out or (proc.returncode or 0) != 0:
        return error(context, text, exit_code=proc.returncode, timed_out=timed_out)
    return ok(context, text, exit_code=proc.returncode)


LOCAL_TIMEOUT_HINT = " — waiting this long in the foreground is the mistake, not the command: start it again with background=true and read it with JobOutput, or pass a larger timeout_seconds if it must block"
REMOTE_TIMEOUT_HINT = " — waiting this long in the foreground is the mistake, not the command: start it again with `nohup … > /tmp/job.log 2>&1 &` and poll the log with later calls, or pass a larger timeout_seconds if it must block"

SPILL_MAX_BYTES = 20 * 1024 * 1024
"""The most of one command's output kept on disk; beyond it the file says it was capped."""
SPILL_KEEP_FILES = 30
"""How many spill files a workspace keeps; older ones go when a new one is opened."""


def _prune_spills(directory: Path) -> None:
    try:
        files = sorted((p for p in directory.iterdir() if p.suffix == ".log"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - SPILL_KEEP_FILES + 1)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _spill_path(services: Any, context: ToolContext) -> Path:
    call = re.sub(r"[^A-Za-z0-9_-]", "", str(context.metadata.get("tool_call_id") or "")) or f"{int(time.time())}"
    return services.logs_dir(".exec") / f"{call}.log"


@dataclass(slots=True)
class Job:
    """A background job of this process: a child it waits for, gone with the process.

    ``ended`` is the wall-clock moment the job was first seen finished, and ``reported`` whether the
    agent knows how it ended — it read the end itself, or was woken with it. The watcher in
    :mod:`daedalus.extensions.jobs` wakes the session once for every finished job that is not.
    """

    id: str
    command: str
    cwd: Path
    log: Path
    process: asyncio.subprocess.Process
    started: float
    sandboxed: bool = False
    ended: float | None = None
    reported: bool = False

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode


@dataclass(slots=True)
class RemoteJob:
    """A background job on the machine a session drives (the host), run detached by the shell there.

    Nothing of it lives in this process: its status is asked of the other machine each time, from the
    process id and the file its wrapper writes the exit code into when the command ends.
    """

    id: str
    command: str
    cwd: Path
    log: Path
    pid: int
    started: float
    backend: Any
    exit_code: int | None = None
    ended: float | None = None
    reported: bool = False
    started_at: float = 0.0
    """The wall-clock start, which a record kept across a restart of this process is read back from:
    ``started`` is this process's monotonic clock and means nothing to the next one."""

    @property
    def running(self) -> bool:
        return self.exit_code is None

    async def refresh(self) -> None:
        """Ask the other machine whether the job still runs; a finished one keeps its exit code.

        The exit file is asked first and the process id only after it. A record read back after a
        restart of this process can name a process id the host has since given to something else,
        and asked first, ``kill -0`` called that stranger the job and kept a finished job running
        forever. The file's time is when the job ended, which the agent is told as its duration.
        """
        if self.exit_code is not None:
            return
        done = shlex.quote(f"{self.log}.exit")
        outcome = await self.backend.run(
            f"if [ -s {done} ]; then echo \"$(cat {done}) $(stat -c %Y {done} 2>/dev/null || stat -f %m {done} 2>/dev/null)\"; "
            f"elif kill -0 {self.pid} 2>/dev/null; then echo running; else echo gone; fi",
            cwd=None, env=None, timeout=30.0,
        )
        answer = outcome.output.strip().splitlines()[-1:] if outcome.exit_code == 0 else []
        if not answer or answer[0] == "running":
            return
        words = answer[0].split()
        # "gone" is a job ended without its wrapper writing the code: killed with the whole group.
        self.exit_code = int(words[0]) if words and words[0].lstrip("-").isdigit() else -1
        if self.ended is None:
            self.ended = float(words[1]) if len(words) > 1 and words[1].isdigit() else time.time()

    async def kill(self) -> None:
        """The hangup to the job's group (or to the job alone where it has none), the kill after a grace."""
        await self.refresh()
        if not self.running:
            return
        pid = self.pid
        await self.backend.run(
            f"kill -TERM -- -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null; "
            f"for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 {pid} 2>/dev/null || exit 0; sleep 0.5; done; "
            f"kill -KILL -- -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null; true",
            cwd=None, env=None, timeout=30.0,
        )
        await self.refresh()

    async def tail(self, lines: int) -> str:
        outcome = await self.backend.run(f"tail -n {max(1, lines)} {shlex.quote(str(self.log))} 2>/dev/null", cwd=None, env=None, timeout=30.0)
        return outcome.output.rstrip("\n")


def _jobs(services: Any) -> dict[str, Any]:
    return services.extra.setdefault("jobs", {})


REMOTE_JOB_START = (
    "mkdir -p {logdir} && "
    "if command -v setsid >/dev/null 2>&1; then detach=setsid; else detach=; fi; "
    "$detach nohup bash -c 'bash -c \"$0\"; echo $? > \"$1.exit\"' {command} {log} > {log} 2>&1 < /dev/null & echo $!"
)
"""Starts a job on the other machine and prints its process id. ``nohup`` so the end of the one-shot
call does not take it along, ``setsid`` where there is one (not on macOS) so it leads a process group
of its own that ``JobKill`` can end whole, and a wrapper that writes the exit code next to the log,
because nothing on this side is the job's parent and could wait for it."""


async def _start_remote_job(context: ToolContext, services: Any, command: str, workdir: Path, env: dict[str, str] | None) -> ToolResult:
    jobs = _jobs(services)
    job_id = f"job-{len(jobs) + 1}-{int(time.time() * 1000) % 1000000}"
    log = services.logs_dir(".jobs") / f"{job_id}.log"
    backend = services.exec_backend
    start = REMOTE_JOB_START.format(logdir=shlex.quote(str(log.parent)), command=shlex.quote(command), log=shlex.quote(str(log)))
    outcome = await backend.run(start, cwd=str(workdir), env=env, timeout=60.0)
    last = outcome.output.strip().splitlines()[-1:] if outcome.output.strip() else []
    if outcome.exit_code != 0 or not last or not last[0].isdigit():
        return error(context, f"the job did not start: {outcome.output.strip() or f'exit code {outcome.exit_code}'}")
    job = RemoteJob(id=job_id, command=command, cwd=workdir, log=log, pid=int(last[0]), started=time.monotonic(), backend=backend, started_at=time.time())
    jobs[job_id] = job
    await _track(services, context.session_id, job)
    await asyncio.sleep(0.3)  # long enough for an immediate failure (a typo, a missing binary) to show up in the answer
    await job.refresh()
    head = (await job.tail(40))[:1500]
    status = f"running (pid {job.pid})" if job.running else f"already exited with code {job.exit_code}"
    job.reported = not job.running  # the answer below is how it ended
    return ok(context, f"{job_id}: {status} on the host; output in {log} (the job outlives a restart of the bot; you are told when it ends)\n{head}".rstrip(), job_id=job_id, pid=job.pid)


async def _start_job(context: ToolContext, services: Any, command: str, workdir: Path, env: dict[str, str] | None) -> ToolResult:
    jobs = _jobs(services)
    job_id = f"job-{len(jobs) + 1}-{int(time.time() * 1000) % 1000000}"
    log = services.logs_dir(".jobs") / f"{job_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    _prune_spills(log.parent)  # the same bound as the spill directory: the newest logs stay
    try:
        argv, sandboxed = await sandbox_argv(command, tool_config(context).exec, writable=services.sandbox_writable(), session_id=context.session_id)
    except SandboxUnavailable as exc:
        return error(context, str(exc))
    fh = log.open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workdir), stdout=fh, stderr=subprocess.STDOUT, env=shell_environment(context.session_id, env, cwd=workdir), start_new_session=True
        )
    finally:
        fh.close()
    job = Job(id=job_id, command=command, cwd=workdir, log=log, process=process, started=time.monotonic(), sandboxed=sandboxed)
    jobs[job_id] = job
    await asyncio.sleep(0.3)  # long enough for an immediate failure (a typo, a missing binary) to show up in the answer
    status = f"running (pid {process.pid})" if process.returncode is None else f"already exited with code {process.returncode}"
    job.reported = not job.running  # the answer below is how it ended
    head = log.read_text(encoding="utf-8", errors="replace")[:1500]
    where = " in the sandbox" if sandboxed else ""
    return ok(context, f"{job_id}: {status}{where}; output in {log} (you are told when it ends; jobs do not survive a restart of the bot, the log does)\n{head}".rstrip(), job_id=job_id, pid=process.pid)


async def _track(services: Any, session_id: str, job: RemoteJob) -> None:
    """Hand a host job to the watcher at once, so its record is kept before anything can restart.

    The watcher would find it on its next round anyway; this closes the seconds in between, in which a
    restart lost a job that outlives it — the one kind of job that does.
    """
    manager = services.extra.get("manager")
    hook = getattr(manager, "service_hooks", {}).get("jobs") if manager is not None else None
    if hook is None:
        return
    try:
        await hook("track", session_id=session_id, job=job)
    except Exception:  # noqa: BLE001 — the job runs whether or not its record was written now
        logging.getLogger(__name__).warning("could not record host job %s of session %s", job.id, session_id, exc_info=True)


def _tail(path: Path, lines: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-max(1, lines):])


@search_hint(
    "background job output progress status last lines of job log "
    "вывод фоновой задачи что пишет джоба как там прогресс команды в фоне сборка"
)
@tool(name="JobOutput", description="The latest output of a background job started with Exec(background=true): its status and the last lines of its log.")
async def job_output(context: ToolContext, job_id: str, tail_lines: int = 100) -> ToolResult:
    services = services_for(context)
    job = _jobs(services).get(job_id)
    if job is None:
        return error(context, f"no job {job_id!r}; JobList shows the jobs of this session")
    if isinstance(job, RemoteJob):
        await job.refresh()
        status = "running" if job.running else f"exited with code {job.exit_code}"
        text = f"{job.id}: {status} after {time.monotonic() - job.started:.0f}s — `{job.command[:200]}`\n{await job.tail(tail_lines)}"
        _seen(job)
        return ok(context, clip(text, services.max_tool_output_chars), running=job.running, exit_code=job.exit_code)
    status = "running" if job.running else f"exited with code {job.process.returncode}"
    elapsed = time.monotonic() - job.started
    text = f"{job.id}: {status} after {elapsed:.0f}s — `{job.command[:200]}`\n{_tail(job.log, tail_lines)}"
    _seen(job)
    return ok(context, clip(text, services.max_tool_output_chars), running=job.running, exit_code=job.process.returncode)


def _seen(job: Job | RemoteJob) -> None:
    """The agent has just read how a job ended, so being woken with the same news would be noise."""
    if not job.running:
        job.reported = True


@search_hint(
    "kill background job stop process group abort command "
    "убить убей джобу остановить фоновую команду прибить прибей оборвать"
)
@tool(name="JobKill", description="Stop a background job (its whole process group). Returns the job's final status.")
async def job_kill(context: ToolContext, job_id: str) -> ToolResult:
    services = services_for(context)
    job = _jobs(services).get(job_id)
    if job is None:
        return error(context, f"no job {job_id!r}")
    if isinstance(job, RemoteJob):
        return await _kill_remote(context, job)
    if job.running:
        try:
            end_tree(job.process.pid, hard=False)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(job.process.wait(), timeout=5)
        except TimeoutError:
            try:
                end_tree(job.process.pid, hard=True)
            except ProcessLookupError:
                pass
            await job.process.wait()
    _seen(job)
    return ok(context, f"{job.id}: exited with code {job.process.returncode}; log in {job.log}", exit_code=job.process.returncode)


async def _kill_remote(context: ToolContext, job: RemoteJob) -> ToolResult:
    await job.kill()
    _seen(job)
    return ok(context, f"{job.id}: exited with code {job.exit_code}; log in {job.log}", exit_code=job.exit_code)


@search_hint(
    "background jobs list what commands run in background "
    "фоновые джобы фоновые задачи список что крутится в фоне какие команды"
)
@tool(name="JobList", description="The background jobs of this session: id, status, age, command.")
async def job_list(context: ToolContext) -> ToolResult:
    services = services_for(context)
    jobs = _jobs(services)
    if not jobs:
        return ok(context, "no background jobs in this session")
    for job in jobs.values():
        if isinstance(job, RemoteJob):
            await job.refresh()
    lines = [f"- {j.id}: {'running' if j.running else f'exited {j.exit_code}'}, {time.monotonic() - j.started:.0f}s, `{j.command[:120]}` → {j.log}" for j in jobs.values()]
    for job in jobs.values():
        _seen(job)
    return ok(context, "\n".join(lines), count=len(jobs))


TOOLS = [exec_command, job_output, job_kill, job_list]

__all__ = ["TOOLS", "Job", "RemoteJob", "exec_command", "job_kill", "job_list", "job_output"]
