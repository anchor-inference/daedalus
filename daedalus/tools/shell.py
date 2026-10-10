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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.processes import end_tree
from daedalus.security import operator_secrets
from daedalus.tools import search_hint
from daedalus.tools._common import FRAME_CHARS, clip, error, ok, services_for, tool_config
from daedalus.tools.job_guard import WAIT_TIMED_OUT, orphan_note, self_match_reason, wait_script

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
        "JobOutput reads its output, JobKill stops it, JobList shows the jobs. Start anything "
        "longer than a minute or two (a build, a render, a test suite) this way — the long process "
        "itself, not a waiter for it — and end your turn: when the job ends you are woken with its "
        "outcome and the last lines of its log, whether you are mid-turn or idle. Never poll it, "
        "never start it with `nohup … &` (nothing reports such a process), never wait with "
        "`pgrep -f` (it matches its own shell); JobWait waits for a pid, a file, a log line or a port. "
        "service=true starts a server as a job that is expected to keep running."
    ),
)
async def exec_command(
    context: ToolContext,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
    service: bool = False,
) -> ToolResult:
    if (reason := self_match_reason(command)) is not None:
        return error(context, f"refused: {reason}")
    if service or background:
        return await start_job(context, command, cwd, env, kind="service" if service else "job")
    result = await _run(context, command, cwd, timeout_seconds, env)
    note = orphan_note(command, sandboxed="sandbox=workspace" in result.content.split("\n", 1)[0])
    return result.model_copy(update={"content": result.content + note}) if note else result


async def start_job(context: ToolContext, command: str, cwd: str | None, env: dict[str, str] | None, *, kind: str = "job", label: str = "") -> ToolResult:
    """Start ``command`` as a background job of the session, wherever its commands run."""
    services = services_for(context)
    workdir = services.resolve(cwd)
    command, env = _with_secrets(context.session_id, command, env, remote=services.exec_backend is not None)
    if services.exec_backend is not None:
        return await _start_remote_job(context, services, command, workdir, env, kind=kind, label=label)
    if not workdir.exists():
        return error(context, f"working directory does not exist: {workdir}")
    return await _start_job(context, services, command, workdir, env, kind=kind, label=label)


async def _run(context: ToolContext, command: str, cwd: str | None, timeout_seconds: int | None, env: dict[str, str] | None) -> ToolResult:
    """One command in the foreground, to its end or its timeout."""
    services = services_for(context)
    workdir = services.resolve(cwd)
    limit = float(timeout_seconds or services.tool_timeout_seconds)
    started = time.monotonic()
    command, env = _with_secrets(context.session_id, command, env, remote=services.exec_backend is not None)
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


LOCAL_TIMEOUT_HINT = " — waiting this long in the foreground is the mistake, not the command: start it again with background=true and end your turn (you are woken when it ends), or pass a larger timeout_seconds if it must block"
REMOTE_TIMEOUT_HINT = LOCAL_TIMEOUT_HINT
"""The same advice on the host: it once said `nohup … &` and poll the log, from before host sessions had
jobs, and an agent that followed it started a render nothing watched and slept waiting for its end."""

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


JOB_KINDS = ("job", "wait", "service")
"""What a background job is for. A ``wait`` is a JobWait waiter, whose exit 124 means it timed out; a
``service`` is a long-lived server, which the watcher never calls quiet or overdue — running is its
work — though its end is still reported, since a server that dies is news."""


@dataclass(slots=True)
class Job:
    """A background job of this process: a child it waits for, gone with the process.

    ``ended`` is the wall-clock moment the job was first seen finished, and ``reported`` whether the
    agent knows how it ended — it read the end itself, or was woken with it. The watcher in
    :mod:`daedalus.extensions.jobs` wakes the session once for every finished job that is not.

    ``process`` is None for a job read back from the record of an earlier process of the bot: its child
    died with that process (or is no longer anyone's child), so it is ``lost`` — nobody can tell how it
    ended, and saying so is the report.
    """

    id: str
    command: str
    cwd: Path
    log: Path
    process: asyncio.subprocess.Process | None
    started: float
    sandboxed: bool = False
    ended: float | None = None
    reported: bool = False
    started_at: float = 0.0
    kind: str = "job"
    label: str = ""
    """What the reports call the job instead of its command: a wait's condition, in words."""
    stopped_by: str = ""
    """Who ended it on purpose — "agent" (JobKill) or "operator" (the app's stop button) — or empty."""
    lost: bool = False
    flags: list[str] = field(default_factory=list)
    """The nudges already sent about it while it ran ("quiet", "overdue"): each is sent once."""
    log_size: int = -1
    grown_at: float = 0.0
    """The monotonic moment the log was last seen to grow: the clock of the "possibly stuck" nudge."""

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode if self.process is not None else None

    @property
    def where(self) -> str:
        return "local"


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
    kind: str = "job"
    label: str = ""
    stopped_by: str = ""
    lost: bool = False
    """The process is gone and its wrapper wrote no exit code: killed together with its group from
    outside, or the machine restarted under it. It used to be told as "was killed", which blamed a
    kill nobody made when the host had rebooted."""
    flags: list[str] = field(default_factory=list)
    log_size: int = -1
    grown_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.exit_code is None and not self.lost

    @property
    def where(self) -> str:
        return "host"

    async def refresh(self) -> None:
        """Ask the other machine whether the job still runs; a finished one keeps its exit code.

        The exit file is asked first and the process id only after it. A record read back after a
        restart of this process can name a process id the host has since given to something else,
        and asked first, ``kill -0`` called that stranger the job and kept a finished job running
        forever. The file's time is when the job ended, which the agent is told as its duration.
        """
        if not self.running:
            return
        done = shlex.quote(f"{self.log}.exit")
        log = shlex.quote(str(self.log))
        size = f"$(stat -c %s {log} 2>/dev/null || stat -f %z {log} 2>/dev/null || echo -1)"
        outcome = await self.backend.run(
            f"if [ -s {done} ]; then echo \"$(cat {done}) $(stat -c %Y {done} 2>/dev/null || stat -f %m {done} 2>/dev/null)\"; "
            f"elif kill -0 {self.pid} 2>/dev/null; then echo \"running {size}\"; else echo gone; fi",
            cwd=None, env=None, timeout=30.0,
        )
        answer = outcome.output.strip().splitlines()[-1:] if outcome.exit_code == 0 else []
        if not answer:
            return
        words = answer[0].split()
        if words[0] == "running":
            if len(words) > 1 and words[1].lstrip("-").isdigit():
                grew(self, int(words[1]))
            return
        if words[0] == "gone":
            # No code was written: the wrapper died along with the command. A kill this side made is
            # a kill; anything else is a loss nobody here can explain.
            self.exit_code = -1 if self.stopped_by else None
            self.lost = not self.stopped_by
            if self.ended is None:
                self.ended = time.time()
            return
        self.exit_code = int(words[0]) if words[0].lstrip("-").isdigit() else -1
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


def grew(job: Job | RemoteJob, size: int) -> None:
    """Note the log's size; a change restarts the clock of the "possibly stuck" nudge."""
    if size != job.log_size:
        job.log_size = size
        job.grown_at = time.monotonic()


def outcome(job: Job | RemoteJob) -> str | None:
    """How a job ended, as one word the app and the reports share; None while it runs.

    ``succeeded``, ``failed``, ``killed`` (by a signal, or stopped by the agent or the operator),
    ``timed_out`` (a wait that ran out of time) or ``lost`` (the process vanished without a code).
    """
    if job.running:
        return None
    code = job.exit_code
    if job.lost or code is None:
        return "lost"
    if job.kind == "wait" and code == WAIT_TIMED_OUT:
        return "timed_out"
    if code == 0:
        return "succeeded"
    if code < 0 or job.stopped_by or code in (128 + 1, 128 + 2, 128 + 9, 128 + 15):
        return "killed"
    return "failed"


def outcome_words(job: Job | RemoteJob) -> str:
    """The outcome as the agent reads it, with the code and who stopped it where that is known."""
    word = outcome(job)
    code = job.exit_code
    if word is None:
        return "is running"
    if word == "succeeded":
        return "succeeded"
    if word == "timed_out":
        return "timed out"
    if word == "lost":
        if isinstance(job, RemoteJob):
            return "vanished without an exit code (killed from outside together with its group, or the machine restarted)"
        return "was lost: the bot restarted while it ran, and a job of the bot's own process does not outlive it"
    if word == "killed":
        by = {"agent": " by you (JobKill)", "operator": " by the operator"}.get(job.stopped_by, "")
        return f"was killed{by}" + (f" (exit code {code})" if code is not None and code != -1 else "")
    return f"failed with exit code {code}"


def title(job: Job | RemoteJob) -> str:
    return job.label or job.command


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


async def _start_remote_job(context: ToolContext, services: Any, command: str, workdir: Path, env: dict[str, str] | None, *, kind: str = "job", label: str = "") -> ToolResult:
    jobs = _jobs(services)
    job_id = f"job-{len(jobs) + 1}-{int(time.time() * 1000) % 1000000}"
    log = services.logs_dir(".jobs") / f"{job_id}.log"
    backend = services.exec_backend
    start = REMOTE_JOB_START.format(logdir=shlex.quote(str(log.parent)), command=shlex.quote(command), log=shlex.quote(str(log)))
    outcome = await backend.run(start, cwd=str(workdir), env=env, timeout=60.0)
    last = outcome.output.strip().splitlines()[-1:] if outcome.output.strip() else []
    if outcome.exit_code != 0 or not last or not last[0].isdigit():
        return error(context, f"the job did not start: {outcome.output.strip() or f'exit code {outcome.exit_code}'}")
    job = RemoteJob(id=job_id, command=command, cwd=workdir, log=log, pid=int(last[0]), started=time.monotonic(), backend=backend, started_at=time.time(), kind=kind, label=label, grown_at=time.monotonic())
    jobs[job_id] = job
    await _track(services, context.session_id, job)
    await asyncio.sleep(0.3)  # long enough for an immediate failure (a typo, a missing binary) to show up in the answer
    await job.refresh()
    head = (await job.tail(40))[:1500]
    status = f"running (pid {job.pid})" if job.running else outcome_words(job)
    job.reported = not job.running  # the answer below is how it ended
    return ok(context, f"{job_id}: {status} on the host; output in {log} ({_promise(job)}; the job outlives a restart of the bot)\n{head}".rstrip(), job_id=job_id, pid=job.pid)


def _promise(job: Job | RemoteJob) -> str:
    """What the start of a job tells the agent about hearing of it again: the contract, in one clause."""
    if not job.running:
        return "it has already ended"
    if job.kind == "service":
        return "a service: you are told if it stops; it is never reported as stuck"
    return "you are woken with its outcome when it ends, so end your turn rather than wait"


async def _start_job(context: ToolContext, services: Any, command: str, workdir: Path, env: dict[str, str] | None, *, kind: str = "job", label: str = "") -> ToolResult:
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
    job = Job(id=job_id, command=command, cwd=workdir, log=log, process=process, started=time.monotonic(), sandboxed=sandboxed, started_at=time.time(), kind=kind, label=label, grown_at=time.monotonic())
    jobs[job_id] = job
    await _track(services, context.session_id, job)
    await asyncio.sleep(0.3)  # long enough for an immediate failure (a typo, a missing binary) to show up in the answer
    status = f"running (pid {process.pid})" if process.returncode is None else outcome_words(job)
    job.reported = not job.running  # the answer below is how it ended
    head = log.read_text(encoding="utf-8", errors="replace")[:1500]
    where = " in the sandbox" if sandboxed else ""
    return ok(context, f"{job_id}: {status}{where}; output in {log} ({_promise(job)}; a restart of the bot ends it, and you are told it was lost)\n{head}".rstrip(), job_id=job_id, pid=process.pid)


async def _track(services: Any, session_id: str, job: Job | RemoteJob) -> None:
    """Hand a job to the watcher at once, so its record is kept before anything can restart.

    The watcher would find it on its next round anyway; this closes the seconds in between, in which a
    restart lost a host job that outlives it, and a job of this process that a restart ends without
    anybody being told it was lost.
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


def _age(job: Job | RemoteJob) -> float:
    return ((job.ended or time.time()) - job.started_at) if job.started_at else time.monotonic() - job.started


def _status(job: Job | RemoteJob) -> str:
    return "running" if job.running else outcome_words(job)


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
        tail = await job.tail(tail_lines)
    else:
        tail = _tail(job.log, tail_lines)
    text = f"{job.id}: {_status(job)} after {_age(job):.0f}s — `{title(job)[:200]}`\n{tail}"
    _seen(job)
    return ok(context, clip(text, services.max_tool_output_chars), running=job.running, exit_code=job.exit_code)


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
    await stop_job(job, by="agent")
    _seen(job)
    return ok(context, f"{job.id}: {outcome_words(job)}; log in {job.log}", exit_code=job.exit_code)


async def stop_job(job: Job | RemoteJob, *, by: str) -> None:
    """End a job's whole process group, gently and then not, and remember who ended it."""
    if not job.running:
        return
    job.stopped_by = by
    if isinstance(job, RemoteJob):
        await job.kill()
        return
    assert job.process is not None
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
    lines = [f"- {j.id}{' (' + j.kind + ')' if j.kind != 'job' else ''}: {_status(j)}, {_age(j):.0f}s, `{title(j)[:120]}` → {j.log}" for j in jobs.values()]
    for job in jobs.values():
        _seen(job)
    return ok(context, "\n".join(lines), count=len(jobs))


WAIT_POLL_SECONDS = 2.0
"""How often a foreground wait for a job looks at it; a host job's look is one command on the host."""
FOREGROUND_WAIT_MARGIN = 15.0
"""Seconds of the tool timeout a foreground wait leaves free, so it answers before the call is cut."""


@search_hint(
    "wait until process exits file appears log line port open job finishes block until ready "
    "подождать дождаться пока процесс завершится файл появится порт откроется строка в логе"
)
@tool(
    name="JobWait",
    description=(
        "Wait for one thing instead of writing a polling loop: a background job to end (job_id), a line "
        "matching a regular expression in a log (log + pattern, or job_id + pattern for a job's own log), a "
        "process to exit (pid — checked through /proc, never by matching command lines), a file to appear "
        "(path), or a TCP port to accept connections (port, host defaults to 127.0.0.1). timeout_seconds "
        "bounds the wait. In the foreground it answers when the thing happens or the time is up; "
        "background=true starts the wait as a job and returns at once — you are woken when it ends, "
        "succeeded or timed out, like any job. A wait longer than the tool timeout goes to the background "
        "by itself. A job of yours already wakes you when it ends: wait for it only when you want its end "
        "inside this turn."
    ),
)
async def job_wait(
    context: ToolContext,
    job_id: str | None = None,
    pid: int | None = None,
    path: str | None = None,
    log: str | None = None,
    pattern: str | None = None,
    port: int | None = None,
    host: str = "127.0.0.1",
    timeout_seconds: int = 600,
    background: bool = False,
) -> ToolResult:
    services = services_for(context)
    timeout = max(1, int(timeout_seconds))
    job = None
    if job_id is not None:
        job = _jobs(services).get(job_id)
        if job is None:
            return error(context, f"no job {job_id!r}; JobList shows the jobs of this session")
        if pid is not None or path is not None or port is not None or log is not None:
            return error(context, "with job_id, name nothing else to wait for but an optional pattern for its log")
        if pattern is not None:
            log = str(job.log)
    room = max(1.0, float(services.tool_timeout_seconds) - FOREGROUND_WAIT_MARGIN)
    if job is not None and pattern is None:
        if background:
            return error(context, f"{job.id} already wakes you when it ends; end your turn instead of starting a wait for it")
        return await _wait_for_job(context, job, min(float(timeout), room))
    try:
        if path is not None:
            path = str(services.resolve(path)) if not Path(path).is_absolute() else path
        if log is not None:
            log = str(services.resolve(log)) if not Path(log).is_absolute() else log
        script, label = wait_script(timeout=timeout, pid=pid, path=path, log=log, pattern=pattern, port=port, host=host)
    except ValueError as exc:
        return error(context, str(exc))
    moved = ""
    if not background and timeout > room:
        background = True
        moved = f"(a {timeout}s wait is longer than one call may last, so it runs as a job)\n"
    if background:
        started = await start_job(context, script, None, None, kind="wait", label=label)
        return started if started.is_error else started.model_copy(update={"content": moved + started.content})
    result = await _run(context, script, None, timeout + 10, None)
    code = result.metadata.get("exit_code") if isinstance(result.metadata, dict) else None
    body = result.content.split("\n", 1)[1] if "\n" in result.content else ""
    if code == 0:
        return ok(context, f"{label}: done\n{body}".rstrip(), satisfied=True)
    if code == WAIT_TIMED_OUT:
        return error(context, f"{label}: timed out after {timeout}s\n{body}".rstrip(), satisfied=False, timed_out=True)
    return error(context, f"{label}: the wait itself failed\n{result.content}", satisfied=False)


async def _wait_for_job(context: ToolContext, job: Job | RemoteJob, limit: float) -> ToolResult:
    deadline = time.monotonic() + limit
    while True:
        if isinstance(job, RemoteJob):
            await job.refresh()
        if not job.running:
            break
        if time.monotonic() >= deadline:
            return ok(context, f"{job.id} is still running after {limit:.0f}s of waiting; it wakes you when it ends, so end your turn rather than wait again", running=True)
        if isinstance(job, Job) and job.process is not None:
            try:
                await asyncio.wait_for(job.process.wait(), timeout=min(WAIT_POLL_SECONDS * 5, max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(min(WAIT_POLL_SECONDS, max(0.1, deadline - time.monotonic())))
    tail = await job.tail(20) if isinstance(job, RemoteJob) else _tail(job.log, 20)
    _seen(job)
    return ok(context, f"{job.id} {outcome_words(job)} after {_age(job):.0f}s — `{title(job)[:200]}`\n{tail}".rstrip(), running=False, exit_code=job.exit_code)


TOOLS = [exec_command, job_output, job_kill, job_list, job_wait]

__all__ = ["JOB_KINDS", "TOOLS", "Job", "RemoteJob", "exec_command", "job_kill", "job_list", "job_output", "job_wait", "outcome", "outcome_words", "start_job", "stop_job", "title"]
