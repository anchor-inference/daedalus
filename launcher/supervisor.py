#!/usr/bin/env python3
"""Daedalus supervisor — PID 1 in the container, never edited by the agent.

Responsibilities, and nothing more:

* start the bot and restart it when it dies (with backoff);
* on ``rebuild``: preflight ``origin/main`` on a candidate checkout while the bot keeps
  running, then move both repositories to it and restart; a revision that fails the
  preflight never touches the running bot;
* on ``restart``: apply what the checkout already holds. Where the checkout is the only
  place a change lives (no remote to pull from), the same preflight runs on a detached
  worktree of the commit that is there, and the bot restarts only if it passes;
* on ``rollback``: check out an earlier known-good revision and restart;
* on ``panic``: kill the whole process tree immediately;
* roll back by itself when a revision cannot boot: three short-lived starts inside ten
  minutes put the last known-good commit back;
* enforce the daily spend cap by reading the bot's usage table.

Commands arrive as JSON lines on a unix socket. Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BOT_REPO = Path(os.environ.get("DAEDALUS_BOT_REPO", "/srv/daedalus"))
CORE_REPO = Path(os.environ.get("DAEDALUS_CORE_REPO", "/srv/protocore-exp"))
STATE = Path(os.environ.get("DAEDALUS_STATE", "/srv/state"))
WORKSPACES = Path(os.environ.get("DAEDALUS_WORKSPACES", "/srv/workspaces"))
SOCKET = Path(os.environ.get("DAEDALUS_SUPERVISOR_SOCKET", "/run/daedalus/supervisor.sock"))
SUPERVISOR_TOKEN = STATE / "supervisor.token"
"""The secret a loopback channel is opened with. Written here because here is where the socket would
have been: one directory, one owner, one thing to delete when the installation goes."""
SUPERVISOR_TCP = os.environ.get("DAEDALUS_SUPERVISOR_TCP", "").strip()
"""``host:port`` to listen on instead of the socket, where the platform has no unix sockets (Windows).
One or the other: the bot is told whichever this supervisor really opened, so the two cannot disagree."""
UNIX_SOCKET_PATH_MAX = 104
"""A unix socket's path must be shorter than this many bytes: sun_path holds 104 on macOS and 108 on
Linux, the terminating NUL included, so the smaller of the two decides on every platform."""
POSIX = os.name != "nt"
"""Whether the platform has process groups, signals and uids. Windows has none of the three, and each
of them is used below for something that has a different answer there rather than no answer."""
BOT_CMD = os.environ.get("DAEDALUS_BOT_CMD", "uv run --frozen python -m daedalus serve")
BAKED_APP = Path(os.environ.get("DAEDALUS_BAKED_APP", "/opt/miniapp-dist"))
"""The Mini App bundle built into the image. The runtime image carries no Node, so a checkout that
has never been built gets this copy instead of a build that cannot run."""
REBUILD_TRIGGER_DIR = Path(os.environ.get("DAEDALUS_REBUILD_TRIGGER_DIR", "/run/daedalus-rebuild"))
"""Shared with the rebuilder sidecar (the only container that holds the docker socket)."""
REBUILDER_HEARTBEAT = "alive"
REBUILDER_HEARTBEAT_SECONDS = 120.0
"""The sidecar touches the heartbeat every pass of its five-second loop; this is what tells a mounted
volume with nobody behind it from a rebuilder that can really take the trigger."""
USD_PER_DAY = float(os.environ.get("USD_PER_DAY", "0") or 0)
"""Daily spend cap, from the supervisor's own environment — never from a file the bot can edit."""
GOOD_DIR = STATE / "good"
HISTORY = GOOD_DIR / "history.json"
FAILED = GOOD_DIR / "FAILED"
LAST_REBUILD = GOOD_DIR / "LAST_REBUILD"
LIMIT_FLAG = STATE / "BUDGET_EXCEEDED"
LOG = STATE / "supervisor.log"

REBUILD_TRIGGER_FILES = ("deploy/Dockerfile", "deploy/compose.yaml", "deploy/apt-packages.txt")
DEPENDENCY_FILES = ("uv.lock", "pyproject.toml")
"""A change to one of these is the only reason to sync the virtualenv: it lives on a volume that outlives the
container, so syncing it when nothing declared a dependency is minutes of waiting for no difference."""
CANDIDATE = STATE / "preflight"
"""Detached checkouts of the revision about to run, one per repository under the names the running checkouts
have, so the bot repository's ``../protocore-exp`` path dependency resolves to the candidate core. The
preflight runs here while the bot keeps serving on the old revision; the running checkouts move only once it
passes."""
VENV = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", "/srv/venv"))
PREFLIGHT_VENV = Path(os.environ.get("DAEDALUS_PREFLIGHT_VENV", str(STATE / "preflight-venv")))
"""The environment the preflight runs in, and the only one it may write to. The bot's own virtualenv is
what the running process imports from: a ``uv sync`` against it reconciles it with the candidate's lock —
removing whatever the image installed outside that lock — and leaves editable pointers into the candidate
checkout behind, so a preflight of a change that fails would still have changed the installation. This one
lives on the state volume next to it, is seeded once and reused by every preflight after that."""
VENV_STAMP = VENV / ".daedalus-dependencies"
"""What the virtualenv on the volume was built for. The image bakes the environment and bakes this file with
it, and Docker seeds the volume from the image the first time, so a released install starts without running
uv at all. Afterwards the volume outlives the image: a checkout that declares different dependencies — a hand
update, a newer tarball than the image, a change that landed here — would otherwise run against the
environment of whatever version created the volume, and would do it silently."""
PUBLISHED_CAPABILITIES = STATE / "capabilities.json"
"""What the bot resolved about this installation on its last start, written by the bot and read here. One
resolution, one answer: see ``resolve_mode``."""
SELFDEV_DIR = STATE / "selfdev"
APPLY_RESULT = SELFDEV_DIR / "result.json"
"""What the last apply attempt did, for the bot to read after it comes back up and show in the app. The
supervisor writes it when it refuses or reverses a change; the bot writes it when the change is live."""
CONFIGURED_MODE = os.environ.get("DAEDALUS_SELFDEV_MODE", "auto")
"""``server``, ``local``, ``off`` or ``auto``; the same value the bot resolves for itself. The supervisor
resolves it again from what it can see, because it decides before the bot is running."""

# Load beside the supervisor, including when this file is executed directly rather than imported.
_dependency_spec = importlib.util.spec_from_file_location("daedalus_dependency_runtime", Path(__file__).with_name("dependencies.py"))
assert _dependency_spec is not None and _dependency_spec.loader is not None
dependency_runtime = importlib.util.module_from_spec(_dependency_spec)
_dependency_spec.loader.exec_module(dependency_runtime)


def dependency_service() -> Any:
    return dependency_runtime.Dependencies(BOT_REPO, STATE, REBUILD_TRIGGER_DIR, native=os.environ.get("DAEDALUS_NATIVE", "").lower() in ("1", "true", "yes", "on"))

HEALTHY_SECONDS = 120
"""A start that lasts this long is what ``record_good`` calls healthy; one that does not is a failed boot."""
BOOT_WINDOW_SECONDS = 600
BOOT_THRESHOLD = 3
"""Three failed boots inside ten minutes — the boot guard's own numbers — mean the revision cannot run."""


def log(message: str) -> None:
    line = f"{datetime.now(UTC).isoformat()} {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def bot_env() -> dict[str, str]:
    """Environment the bot (and its preflight) runs with: paths owned by the supervisor."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    if agent_bin := dependency_service().active_bin():
        env["DAEDALUS_AGENT_BIN"] = agent_bin
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The bot in Python's UTF-8 mode whoever started this supervisor: on Windows the locale's code page
    # is otherwise what its output, its logs and every file it opens without an encoding are read and
    # written in, and a launcher older than the setting does not pass it down.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Commits the agent makes in its workspace carry its own identity; the repos' local config covers the self-development checkouts.
    for key, value in (("GIT_AUTHOR_NAME", "Daedalus"), ("GIT_AUTHOR_EMAIL", "daedalus@localhost"), ("GIT_COMMITTER_NAME", "Daedalus"), ("GIT_COMMITTER_EMAIL", "daedalus@localhost")):
        env.setdefault(key, value)
    token = env.get("GITHUB_TOKEN")
    org_token = env.get("GITHUB_DAEDALUS_TOKEN", "")
    if token or org_token:
        # git authenticates with a token, never with a stored password. Two tokens when the agent has an
        # organisation of its own: the helper answers with the organisation's token for that owner's
        # repositories and with the operator's token for everything else.
        env["GH_TOKEN"] = token or org_token
        if org_token:
            env["GH_ORG_TOKEN"] = org_token
        # The helper picks the token by the repository owner, which git only sends when useHttpPath is on:
        # without it every push goes out with the operator's token and the organisation answers 403.
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = f"!{Path(__file__).parent / 'git-credential-daedalus'}"
        env["GIT_CONFIG_KEY_1"] = "credential.useHttpPath"
        env["GIT_CONFIG_VALUE_1"] = "true"
    env.update(
        {
            "BOT_REPO_DIR": str(BOT_REPO),
            "CORE_REPO_DIR": str(CORE_REPO),
            "STATE_DIR": str(STATE),
            "WORKSPACES_DIR": str(WORKSPACES),
            "SUPERVISOR_SOCKET": str(SOCKET),
            "SUPERVISOR_TCP": SUPERVISOR_TCP,
        }
    )
    return env


def preflight_env() -> dict[str, str]:
    """Environment the preflight's own commands run with: everything the bot has, pointed at the
    preflight's virtualenv. ``uv`` writes to whatever ``UV_PROJECT_ENVIRONMENT`` names, and the image
    names the running bot's — so this is what keeps a check of a change out of the installation it is
    checking."""
    env = bot_env()
    env["UV_PROJECT_ENVIRONMENT"] = str(PREFLIGHT_VENV)
    env.pop("VIRTUAL_ENV", None)
    return env


SSH_SOURCE = Path(os.environ.get("DAEDALUS_SSH_SOURCE", "/srv/ssh"))
SSH_HOME = Path(os.environ.get("DAEDALUS_SSH_HOME", str(Path.home() / ".ssh")))


def install_ssh() -> None:
    """Copy the mounted ssh material into ~/.ssh: the mount is read-only and owned by the host user,
    and ssh refuses a private key it can read too widely, so the copy carries the modes ssh wants."""
    if not SSH_SOURCE.is_dir():
        return
    try:
        SSH_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(SSH_HOME, 0o700)
        for src in sorted(p for p in SSH_SOURCE.iterdir() if p.is_file()):
            target = SSH_HOME / src.name
            target.write_bytes(src.read_bytes())
            os.chmod(target, 0o644 if src.name.endswith(".pub") or src.name in ("config", "known_hosts") else 0o600)
    except OSError as exc:
        log(f"ssh material not installed: {exc}")


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, env=env or bot_env()
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return proc.returncode, (proc.stdout + proc.stderr)[-20000:]


def git(repo: Path, *args: str) -> tuple[int, str]:
    code, out = run(["git", "-C", str(repo), *args], timeout=600)
    if args and args[0] in ("reset", "checkout", "fetch"):
        restore_owner(repo)
    return code, out


def restore_owner(repo: Path) -> None:
    """Keep the checkout owned by whoever owns its root: the supervisor runs as root on a
    host-mounted repository, and files it rewrites must stay editable from the host.

    Natively there is nothing to restore — the supervisor is the operator's own process and writes
    the operator's own files — and on Windows there are no uids to restore them to."""
    if not POSIX:
        return
    try:
        st = repo.stat()
    except OSError:
        return
    if st.st_uid == os.getuid():
        return
    run(["chown", "-R", f"{st.st_uid}:{st.st_gid}", str(repo)], timeout=600)


def head(repo: Path) -> str:
    code, out = git(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else "unknown"


def load_history() -> list[dict[str, Any]]:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text())
        except json.JSONDecodeError:
            return []
    return []


def record_good(bot: str | None = None, core: str | None = None) -> None:
    """Record a revision that has run for two healthy minutes.

    The shas are passed in, not read from the checkout: where a change lands in the checkout while the
    old process is still serving — which is exactly what local self-development does — reading HEAD here
    would mark a revision known-good that has never started, and the automatic rollback trusts this list.
    """
    GOOD_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    entry = {"bot": bot or head(BOT_REPO), "core": core or head(CORE_REPO), "at": datetime.now(UTC).isoformat()}
    if history and history[-1]["bot"] == entry["bot"] and history[-1]["core"] == entry["core"]:
        return
    history.append(entry)
    HISTORY.write_text(json.dumps(history[-50:], indent=2))
    log(f"recorded known-good bot={entry['bot'][:10]} core={entry['core'][:10]}")


def git_dir(repo: Path) -> Path | None:
    """The repository's git directory, for a normal checkout and for a linked worktree alike.

    A worktree's ``.git`` is a file naming ``<main>/.git/worktrees/<name>``, whose remotes are the main
    repository's. Reading ``repo/.git/config`` there finds nothing and reports a checkout with no remote.
    """
    marker = repo / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.partition(":")[2].strip())
    if not target.is_absolute():
        target = (repo / target).resolve()
    if target.parent.name == "worktrees":
        target = target.parent.parent
    return target if target.is_dir() else None


def has_origin(repo: Path) -> bool:
    """Whether the checkout has somewhere to push to. Read from the git configuration rather than asked of
    git: this decides how a restart behaves and must not depend on a subprocess that can hang."""
    directory = git_dir(repo)
    if directory is None:
        return False
    try:
        return '[remote "origin"]' in (directory / "config").read_text(encoding="utf-8")
    except OSError:
        return False


def published_mode() -> str:
    """The mode the running bot resolved for itself, or an empty string before it ever has.

    The bot's resolution is the one that counts: it is what decided which tools were registered and what
    the operator was told. The supervisor decides before the bot is up, so it reads what the last start
    wrote rather than running a second set of rules that can disagree with it — and a disagreement here
    is a change restarting with no preflight at all.
    """
    try:
        data = json.loads(PUBLISHED_CAPABILITIES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    mode = str((data.get("selfdev") or {}).get("mode") or "") if isinstance(data, dict) else ""
    return mode if mode in ("off", "local", "server") else ""


def loopback_token() -> str:
    """The shared secret that has to accompany a command sent over the loopback port.

    A unix socket is a file: the operating system decides who may write to it, which is the whole
    reason it is preferred. A port on 127.0.0.1 has no owner — every process of every user on the
    machine can connect to it, and ``restart``, ``rollback`` and ``panic`` are not commands to leave
    open to all of them. So the port asks for a secret kept the way the socket was: a file in the
    state directory, created with mode 0600 rather than created and then chmod'ed. On Windows —
    which is the only platform that uses the port — that mode reaches no ACL: ``os.chmod`` there
    toggles the read-only attribute, so the file is as readable as the directory it sits in, and the
    honest claim is that the secret raises the cost of asking rather than that it settles who may.
    The same secret across restarts, so a bot that is already running goes on being able to ask.
    """
    with contextlib.suppress(OSError):
        if existing := SUPERVISOR_TOKEN.read_text("utf-8").strip():
            return existing
    token = secrets.token_urlsafe(32)
    SUPERVISOR_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    # Created with the mode rather than created and then chmod'ed: the second form writes the secret
    # under the process umask first, and there is a window in which anyone may read it.
    SUPERVISOR_TOKEN.unlink(missing_ok=True)  # an empty file from a previous start is not a token
    fd = os.open(SUPERVISOR_TOKEN, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


def resolve_mode(configured: str, *, repo: Path, token: str, published: str = "") -> str:
    """Which self-development this installation does — the bot's own answer wherever there is one.

    An explicit setting is taken as given, because the bot obeys it too. Otherwise the mode the last
    start published is used, and the probe below is only for the first boot of an installation, before
    anything has published anything: ``server`` needs a remote to pull from and a token to reach it,
    ``local`` needs only the checkout, because the change is already in it.
    """
    if configured in ("off", "local", "server"):
        return configured
    if published in ("off", "local", "server"):
        return published
    if has_origin(repo) and token.strip():
        return "server"
    return "local" if (repo / ".git").exists() else "off"


def rebuilder_alive() -> bool:
    """Whether the rebuilder sidecar is on the other end of the trigger directory.

    The directory itself is a volume mounted unconditionally, so its existence says nothing. The
    sidecar touches a heartbeat on every pass of its loop; without one the trigger file would sit
    there unread while the operator was told a rebuild was under way."""
    try:
        return time.time() - (REBUILD_TRIGGER_DIR / REBUILDER_HEARTBEAT).stat().st_mtime <= REBUILDER_HEARTBEAT_SECONDS
    except OSError:
        return False


def needs_new_image(changed: set[str]) -> bool:
    """Whether the change rewrites the environment rather than the code that runs in it."""
    return any(path in changed for path in REBUILD_TRIGGER_FILES) or any(path.startswith("launcher/") for path in changed)


def venv_ready(venv: Path) -> bool:
    """Whether this virtualenv can run the preflight.

    Not merely whether it exists: the preflight's last step is the test suite, so an environment
    without a test runner fails on the runner rather than on the change, which reads as a broken
    change and is not one. Asked of the preflight's own virtualenv, never of the bot's.
    """
    return any((venv / d / name).exists() for d in ("bin", "Scripts") for name in ("pytest", "pytest.exe"))


def dependency_digest(repo: Path) -> str:
    """A digest of the files that declare what the environment must hold."""
    digest = hashlib.sha256()
    for name in DEPENDENCY_FILES:
        path = repo / name
        digest.update(path.read_bytes() if path.exists() else b"")
    digest.update(",".join(image_extras()).encode())
    return digest.hexdigest()


def image_extras() -> list[str]:
    """The optional extras the image was built with, so a sync holds the environment to the same shape.

    The environment lives on a volume that outlives the image: without this, a new image that carries a
    new extra (the speech models' runtime, say) would install it into a venv nobody runs, while the sync
    of the venv in use — asked only for the lock's core set — would never put it there.
    """
    raw = os.environ.get("DAEDALUS_VENV_EXTRAS", "")
    return [name for name in (part.strip() for part in raw.split(",")) if name]


def sync_argv() -> list[str]:
    argv = ["uv", "sync", "--frozen", "--inexact"]
    for extra in image_extras():
        argv += ["--extra", extra]
    return argv


def sync_venv_if_stale(repo: Path, *, venv: Path | None = None) -> bool:
    """Bring the environment up to what this checkout asks for, and only then.

    Returns whether a sync was run. The development extra is deliberately left out: nothing the bot itself
    does needs a test runner, and the preflight adds it on its own terms when it needs one (``venv_ready``).
    """
    venv = venv or VENV
    stamp = venv / VENV_STAMP.name
    want = dependency_digest(repo)
    try:
        if stamp.read_text().strip() == want:
            return False
    except OSError:
        pass  # no stamp: an environment from before this file, or one built by hand
    log("the environment was built for other dependencies than this checkout declares; syncing")
    # --inexact: the lock is what the environment must *hold*, not the whole of what it may hold. An
    # exact sync removes everything the image installed beside it — the headless browser and its
    # bindings on the browser image — from a running installation, and only a new image puts them back.
    code, out = run(sync_argv(), cwd=repo, timeout=1200)
    if code != 0:
        log(f"the sync failed; starting on the environment that is there\n{out}")
        return True
    try:
        stamp.write_text(want)
    except OSError as exc:
        log(f"the environment is synced but its stamp could not be written ({exc}); the next start syncs again")
    return True


def needs_dependency_sync(changed: set[str], core_changed: set[str] | None = None) -> bool:
    """Whether the virtualenv has to be synced before this revision can run.

    One reason, and no others: the revision declares different dependencies — in the bot repository or
    in the core it is built on, which is a path dependency and so declares into the same environment.
    The state of the virtualenv is deliberately not a reason: the preflight has a virtualenv of its own
    and never asks the running one for a test runner, so a sync here only ever happens because a
    dependency really moved. Otherwise the environment on the volume is left exactly as it is, which is
    the point of keeping it on a volume at all.
    """
    return any(path in changed for path in DEPENDENCY_FILES) or any(path in (core_changed or set()) for path in DEPENDENCY_FILES)


def unhealthy_boots(history: list[float], now: float, *, window: float = BOOT_WINDOW_SECONDS) -> list[float]:
    """The failed boots still inside the window, newest last. A record from the future is a clock step,
    not evidence, and would otherwise never age out."""
    return [t for t in history if now - window <= t <= now]


def write_result(status: str, commit: str, detail: str) -> None:
    """Record what the last apply attempt did where the bot and the launcher can both read it."""
    try:
        SELFDEV_DIR.mkdir(parents=True, exist_ok=True)
        APPLY_RESULT.write_text(json.dumps({"status": status, "commit": commit, "detail": detail[-2000:], "at": datetime.now(UTC).isoformat()}, indent=2))
    except OSError as exc:
        log(f"could not record the apply result: {exc}")


def last_change() -> dict[str, Any] | None:
    """What the last apply attempt did, or ``None`` when nothing has been applied here."""
    try:
        return dict(json.loads(APPLY_RESULT.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def candidate_dir(repo: Path) -> Path:
    return CANDIDATE / repo.name


def prepare_candidate(repo: Path, ref: str = "origin/main") -> tuple[bool, str]:
    """Put a detached checkout of ``ref`` next to the others under CANDIDATE, reusing its venv and
    node_modules from last time; the object store is the repository's own (a worktree), so this is a checkout,
    not a clone.

    ``ref`` is ``origin/main`` when the revision comes from a remote and a commit sha when it comes from the
    checkout itself — the only difference between preflighting a merge and preflighting a local change.
    """
    target = candidate_dir(repo)
    CANDIDATE.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        git(repo, "worktree", "prune")
        code, out = git(repo, "worktree", "add", "--detach", "--force", str(target), ref)
        return code == 0, out
    code, out = git(target, "checkout", "--detach", "--force", ref)
    if code != 0:
        return False, out
    code, out = git(target, "clean", "-fd", "--exclude=.venv", "--exclude=node_modules", "--exclude=miniapp/node_modules")
    return code == 0, out


def install_from_candidate(repo: Path) -> tuple[bool, str]:
    """After the preflight passed on the candidate: the running checkout gets its dependencies synced (a warm
    cache, seconds) and the app build copied over rather than built again; this is the whole downtime.

    No development extra and no exact reconciliation: the bot runs no tests, and what the image installed
    beside the lock is not this sync's to remove."""
    code, out = run(sync_argv(), cwd=repo, timeout=1200)
    if code != 0:
        return False, out
    built = candidate_dir(repo) / "miniapp" / "dist"
    if built.is_dir():
        target = repo / "miniapp" / "dist"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(built, target)
    restore_owner(repo)
    return True, out


def build_app_if_missing(repo: Path) -> None:
    """A fresh checkout has no built app: the bundle is not in git, and a rebuild builds it on the candidate.

    The first start of an installation — a clone the desktop launcher or a setup script just made —
    would otherwise serve 404 at /app until the first merged pull request. A built copy handed over
    with the installation (the image's, or the one a desktop release carries beside its launcher) is
    put in its place; where there is none and there is a Node, the app is built once here, in the
    tree about to run. Either way a failure is logged and the bot still starts (the API and Telegram
    work without the app).

    The copy comes first because it is the same app, built by the release from the same commit, in a
    second instead of the minutes `npm ci` takes on a first run. And npm is run by the path `which`
    found: on Windows that is npm.cmd, and asking CreateProcess for a bare "npm" raised
    FileNotFoundError out of this function, so a desktop machine with Node installed restarted the
    supervisor forever and never served the app."""
    miniapp = repo / "miniapp"
    if (miniapp / "dist" / "index.html").is_file() or not (miniapp / "package.json").exists():
        return
    if (BAKED_APP / "index.html").is_file():
        log("no built app in the checkout; installing the one that came with the installation")
        try:
            shutil.copytree(BAKED_APP, miniapp / "dist", dirs_exist_ok=True)
            log("app installed")
        except OSError as exc:
            log(f"could not install the built-in app: {exc}")
        finally:
            restore_owner(repo)
        return
    npm = shutil.which("npm")
    if not npm:
        log("no built app in the checkout, no npm to build one and none came with the installation; /app answers 503")
        return
    log("no built app in the checkout; building it before the first start")
    try:
        for step in ([npm, "ci", "--no-audit", "--no-fund"], [npm, "run", "build"]):
            code, out = run(step, cwd=miniapp, timeout=1200)
            if code != 0:
                log(f"app build failed ({' '.join(step)}): {out[-800:]}")
                return
        log("app built")
    except OSError as exc:
        log(f"app build failed: {exc}")
    finally:
        restore_owner(repo)


def end_tree(pid: int, *, hard: bool) -> None:
    """Signal the bot and everything it started.

    On a POSIX system that is the process group, which the bot was started in one of. Windows has no
    process groups to signal, so the tree is walked by ``taskkill`` — the nearest thing the platform
    has, and the only one that reaches a tool the bot started.
    """
    if POSIX:
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return
    if not hard:
        # `taskkill` without /F posts WM_CLOSE to top-level windows, and a console-less Python has
        # none: nothing arrived, the caller waited out its whole timeout and killed the tree anyway.
        # A console control event reaches the process group the child was started in, which is what
        # this process's own signal handler is waiting for.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            return
    run(["taskkill", "/F", "/T", "/PID", str(pid)] if hard else ["taskkill", "/T", "/PID", str(pid)], timeout=60)


def reap_zombies(keep: set[int]) -> int:
    """Collect children the bot left behind (sandbox wrappers reparented to PID 1) — by pid, never with a
    wait on any child, so the bot process asyncio itself waits on is not taken from under it."""
    if not Path("/proc").is_dir():
        # A zombie is a Linux idea reached through /proc. macOS and Windows reap a child when it is
        # waited on, which asyncio already does, and there is nothing here for this to collect.
        return 0
    reaped = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in keep:
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" or int(fields[1]) != os.getpid():
            continue
        try:
            pid, _status = os.waitpid(int(entry), os.WNOHANG)
        except ChildProcessError:
            continue
        reaped += pid == int(entry)
    return reaped


def preflight(repo: Path, *, sync: bool = True) -> tuple[bool, str]:
    """Dependencies, Mini App build, import, config and smoke tests in the tree about to run.

    Every step runs in the preflight's own virtualenv (``preflight_env``), which is the whole of what
    makes this a check rather than an installation: the candidate's code, the candidate's dependencies,
    and a running bot whose environment is not touched whichever way the check comes out.

    ``sync`` is what decides whether that virtualenv is rebuilt. It is on for a revision pulled from a
    remote, where anything may have changed, and for a local change that moved a dependency file; an
    environment that has never been seeded is synced regardless, because there is nothing there yet.
    """
    env = preflight_env()
    steps: list[tuple[list[str], Path]] = [
        # --extra dev on every step, not only on the sync: `uv run` reconciles the environment it is
        # given before it runs anything, and one asked for without the extra would take the test runner
        # back out of the environment the step after this one needs it in.
        (["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "daedalus"], repo),
        (["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "daedalus", "check"], repo),
        (["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q", "-x", "tests/smoke", "tests/unit/test_boundaries.py", "tests/unit/test_redact.py", "tests/unit/test_reachability.py"], repo),
    ]
    if sync or not venv_ready(PREFLIGHT_VENV):
        steps.insert(0, (["uv", "sync", "--frozen", "--extra", "dev"], repo))
    miniapp = repo / "miniapp"
    if (miniapp / "package.json").exists() and shutil.which("npm"):
        steps.insert(1, (["npm", "ci", "--no-audit", "--no-fund"], miniapp))
        steps.insert(2, (["npm", "run", "build"], miniapp))
    transcript: list[str] = []
    try:
        for step, cwd in steps:
            code, out = run(step, cwd=cwd, timeout=1200, env=env)
            transcript.append(f"$ {' '.join(step)}\n{out}")
            if code != 0:
                return False, "\n".join(transcript)
        return True, "\n".join(transcript)
    finally:
        restore_owner(repo)  # npm/uv wrote node_modules, dist and caches as root


class Supervisor:
    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.want_running = True
        self.restart_requested = asyncio.Event()
        self.backoff = 2.0
        self.lock = asyncio.Lock()
        self.last_result = "startup"
        self.token = ""
        """The secret the loopback channel asks for, empty where the channel is a socket and the
        filesystem answers the same question."""
        self.health_task: asyncio.Task[None] | None = None
        self.rebuild_task: asyncio.Task[None] | None = None
        self.queued_rebuild: str | None = None
        """The reason of a rebuild asked for while another was running: it runs right after, so a pull
        request merged during a rebuild is not left undeployed until someone asks again."""
        self.configured = CONFIGURED_MODE
        """What the operator asked for, ``auto`` by default. An explicit value wins over everything below."""
        self.restart_task: asyncio.Task[None] | None = None
        self.dependencies = dependency_service()
        try:
            self.dependencies.status()
        except (OSError, ValueError) as exc:
            log(f"dependency state could not be read: {exc}")
        self.running_revision = ""
        self.running_core = ""
        """The commits the child was started on. Not the last known-good one: a revision becomes known-good
        only after two healthy minutes, and a change refused before then would otherwise be taken back out
        past a change that is already running."""
        self.tried_revisions: set[str] = set()
        """Revisions the automatic rollback has already gone back to since the last healthy start."""
        self.failed_boots: list[float] = []
        """When each of the recent starts died before it was healthy. Three inside the window and the
        revision goes back to the last known-good one by itself: a change that cannot boot cannot be
        undone from the app, because the app is what is not coming up."""
        log(f"self-development mode: {self.mode} (configured {CONFIGURED_MODE})")

    @property
    def mode(self) -> str:
        """Read afresh every time rather than kept: the bot publishes what it resolved when it starts,
        and the first boot of an installation decides before that file exists."""
        return resolve_mode(
            self.configured,
            repo=BOT_REPO,
            token=os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_DAEDALUS_TOKEN", ""),
            published=published_mode(),
        )

    # -- child lifecycle ------------------------------------------------------------

    async def start_child(self) -> None:
        install_ssh()
        if POSIX:
            self.child = await asyncio.create_subprocess_exec(
                "bash", "-lc", BOT_CMD, cwd=str(BOT_REPO), env=bot_env(), start_new_session=True
            )
        else:
            # Windows has no login shell to hand a command line to, and MinGit's sh is not one
            # either: the command is split here and the program is started directly, in a process
            # group of its own so that stopping it stops what it started.
            self.child = await asyncio.create_subprocess_exec(
                *shlex.split(BOT_CMD, posix=False), cwd=str(BOT_REPO), env=bot_env(), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
        self.running_revision, self.running_core = head(BOT_REPO), head(CORE_REPO)
        log(f"bot started pid={self.child.pid} bot={self.running_revision[:10]} core={self.running_core[:10]}")

    async def stop_child(self, *, graceful_seconds: float = 25.0) -> None:
        child = self.child
        if child is None or child.returncode is not None:
            return
        try:
            end_tree(child.pid, hard=False)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(child.wait(), timeout=graceful_seconds)
        except TimeoutError:
            log("bot did not exit in time; killing")
            try:
                end_tree(child.pid, hard=True)
            except ProcessLookupError:
                pass
            await child.wait()

    async def _record_good_when_healthy(self, child: asyncio.subprocess.Process, bot: str, core: str) -> None:
        await asyncio.sleep(HEALTHY_SECONDS)
        if child.returncode is None:
            record_good(bot, core)

    async def run_loop(self) -> None:
        while self.want_running:
            await self.start_child()
            assert self.child is not None
            if self.health_task is not None:
                self.health_task.cancel()
            self.health_task = asyncio.create_task(self._record_good_when_healthy(self.child, self.running_revision, self.running_core))
            started = time.monotonic()
            waiter = asyncio.create_task(self.child.wait())
            restart = asyncio.create_task(self.restart_requested.wait())
            done, _ = await asyncio.wait({waiter, restart}, return_when=asyncio.FIRST_COMPLETED)
            if restart in done:
                self.restart_requested.clear()
                await self.stop_child()
                waiter.cancel()
                continue
            restart.cancel()
            code = self.child.returncode
            uptime = time.monotonic() - started
            log(f"bot exited code={code} after {uptime:.0f}s")
            if not self.want_running:
                break
            if self.lock.locked():
                # A rebuild or rollback stopped the child on purpose; it restarts the bot itself.
                await self.restart_requested.wait()
                self.restart_requested.clear()
                continue
            if uptime > HEALTHY_SECONDS:
                self.backoff = 2.0
                self.failed_boots.clear()
                self.tried_revisions.clear()  # this revision runs; what was tried before it is history
            else:
                self.backoff = min(self.backoff * 2, 120.0)
                if await self._note_failed_boot():
                    continue  # the checkout moved back; start the old revision at once rather than after the backoff
            await asyncio.sleep(self.backoff)

    async def _note_failed_boot(self) -> bool:
        """Record a start that died before it was healthy; roll back at the threshold.

        Only where the change lives in the checkout: with a remote, the revision that cannot boot was
        merged and pulled, and putting it back is the operator's ``rollback``, not the supervisor's guess.
        Returns whether the checkout was moved.
        """
        self.failed_boots = unhealthy_boots([*self.failed_boots, time.time()], time.time())
        if self.mode != "local" or len(self.failed_boots) < BOOT_THRESHOLD:
            return False
        log(f"{len(self.failed_boots)} failed boots within {BOOT_WINDOW_SECONDS // 60} minutes; going back to the last known-good revision")
        self.failed_boots.clear()
        return await self._auto_rollback()

    async def _auto_rollback(self) -> bool:
        """Put the last known-good commit back, so the next start is one that has run before.

        Every revision this has already gone back to since the last healthy start is remembered and not
        offered again. Without that, two revisions that both fail take it in turns for ever: A is broken
        so B is chosen, B is broken so A is chosen, three failed boots per swap and no sleep between
        them, because the choice is only ever "the newest that is not the one that just died".
        """
        async with self.lock:
            broken, broken_core = head(BOT_REPO), head(CORE_REPO)
            self.tried_revisions.add(broken)
            history = load_history()
            target = next((h for h in reversed(history) if h["bot"] not in self.tried_revisions), None)
            if target is None:
                write_result("no_rollback", broken, f"{BOOT_THRESHOLD} failed boots and no earlier known-good revision left to go back to")
                log("no known-good revision left that has not already been tried; staying where we are")
                return False
            self.tried_revisions.add(target["bot"])
            self._checkout(target["bot"], target["core"])
            changed = self._changed_files(target["bot"], broken)
            core_changed = self._changed_files(target["core"], broken_core, CORE_REPO)
            if needs_dependency_sync(changed, core_changed):
                await asyncio.to_thread(lambda: run(["uv", "sync", "--frozen", "--inexact"], cwd=BOT_REPO, timeout=1200))
            write_result(
                "rolled_back",
                broken,
                f"{BOOT_THRESHOLD} starts in a row died within {HEALTHY_SECONDS}s, so the change was reversed: "
                f"bot {broken[:10]} → {target['bot'][:10]} (known good since {target['at']}). The commit is still in the checkout's history.",
            )
            log(f"rolled back to bot={target['bot'][:10]} core={target['core'][:10]}")
            return True

    # -- operations -----------------------------------------------------------------

    async def restart(self, reason: str) -> str:
        """Apply what the checkout holds.

        With a remote there is nothing to check — the revision running is the revision that was merged and
        preflighted — so the bot simply goes round again. Where the change lives only in the checkout, the
        commit that is there has been seen by nothing but the agent that wrote it: it is preflighted on a
        detached worktree of itself first, and the bot is stopped only once that passes.
        """
        if self.mode != "local":
            self.restart_requested.set()
            return "restarting"
        if self.lock.locked() or (self.restart_task is not None and not self.restart_task.done()):
            return "a restart, rebuild or rollback is already running; this one is not started twice"
        self.restart_task = asyncio.create_task(self._apply_local(reason))
        return "checking the change and restarting: it applies if the checks pass, and the running version is kept if they do not"

    async def _apply_local(self, reason: str) -> None:
        """Preflight the commit in the checkout and restart on it; keep the running one if it fails."""
        async with self.lock:
            target, target_core = head(BOT_REPO), head(CORE_REPO)
            history = load_history()
            previous = self.running_revision or (history[-1]["bot"] if history else target)
            previous_core = self.running_core or (history[-1]["core"] if history else target_core)
            log(f"restart requested: {reason} (bot {previous[:10]} → {target[:10]}, core {previous_core[:10]} → {target_core[:10]})")
            changed = self._changed_files(previous, target)
            core_changed = self._changed_files(previous_core, target_core, CORE_REPO)
            stopped = False
            try:
                for repo in (BOT_REPO, CORE_REPO):
                    ok, out = await asyncio.to_thread(prepare_candidate, repo, head(repo))
                    if not ok:
                        write_result("preflight_failed", target, f"a checkout of the change could not be made: {out[-800:]}")
                        return
                sync = needs_dependency_sync(changed, core_changed)
                ok, transcript = await asyncio.to_thread(preflight, candidate_dir(BOT_REPO), sync=sync)
                if not ok:
                    log("preflight failed on the local change; the running bot is untouched")
                    FAILED.parent.mkdir(parents=True, exist_ok=True)
                    FAILED.write_text(f"restart ({reason}) failed preflight at {datetime.now(UTC).isoformat()}\nthe bot kept running on bot={previous[:10]} core={previous_core[:10]}\n\n{transcript[-6000:]}")
                    await self._revert_to({BOT_REPO: previous, CORE_REPO: previous_core}, {BOT_REPO: target, CORE_REPO: target_core}, transcript)
                    return
                FAILED.unlink(missing_ok=True)
                detail = "the change is live"
                if needs_new_image(changed | core_changed):
                    # The environment itself changed, and nothing here can replace it: say so rather than
                    # asking a rebuilder that a local installation does not have.
                    detail = "the code change is live, but this change also rewrites the image (the Dockerfile, the packages or the supervisor). Run an update to build a new image; until then the old environment is what runs."
                    log("the change touches the image; no rebuild is attempted in local mode")
                await self.stop_child()
                stopped = True
                write_result("applied", target, detail)
            finally:
                # Only a change that passed gets the bot restarted. A refused one leaves the process
                # running the code it already has, which is the code the checkout was put back to.
                if stopped:
                    self.restart_requested.set()

    async def _revert_to(self, running: dict[Path, str], broken: dict[Path, str], transcript: str) -> None:
        """Put both checkouts back to what the running process is on.

        The preflight ran on a copy, so nothing that failed has run — but the commit is in the checkout,
        and the next start would pick it up without anyone asking for it. That is as true of the core as
        it is of the bot: a change the agent applied to the core alone leaves the bot's HEAD where it was,
        and reverting only the bot would leave a refused core commit in place for the next start — any
        start, for any reason — to run unchecked. The target is the revision the child was started on,
        which is the state the refusal has to restore; the last known-good commit is a different thing and
        may be older than a change that is already running. The refused commits themselves are not lost:
        the branches the agent committed on still point at them.
        """
        moved: list[str] = []
        notes: list[str] = []
        for repo, was in running.items():
            if not was or was == "unknown" or was == broken.get(repo):
                continue
            code, out = git(repo, "reset", "--hard", was)
            if code == 0:
                moved.append(f"{repo.name} back to {was[:10]}")
            else:
                notes.append(f"The {repo.name} checkout could not be moved back ({out[-300:]}); it still holds the change.")
        headline = f"the checks did not pass, so the change was not applied and the checkout was put back ({', '.join(moved)}), which is what is running." if moved else "the checks did not pass, so the change was not applied:"
        detail = " ".join([headline, *notes])
        write_result("preflight_failed", broken[BOT_REPO], f"{detail}\n\n{transcript[-1500:]}")

    async def rebuild(self, reason: str) -> str:
        """Acknowledge at once; the work runs in the background and reports through LAST_REBUILD.

        A request that lands while a rebuild or rollback holds the lock is not dropped: it is kept (one
        slot — a second request during the same wait replaces the reason, the outcome is the same
        origin/main either way) and starts as soon as the lock is free.
        """
        if self.lock.locked() or (self.rebuild_task is not None and not self.rebuild_task.done()):
            self.queued_rebuild = reason
            return "a rebuild or rollback is in progress; this one is queued and starts right after it"
        self.rebuild_task = asyncio.create_task(self._rebuild(reason))
        return "rebuild started: the bot stops, main is pulled and preflighted, then it restarts (rolled back on failure)"

    def _run_queued_rebuild(self) -> None:
        if self.queued_rebuild is None:
            return
        reason, self.queued_rebuild = self.queued_rebuild, None
        try:
            self.rebuild_task = asyncio.create_task(self._rebuild(reason))
        except RuntimeError:
            log(f"queued rebuild dropped, the loop is closing: {reason}")
            return
        log(f"queued rebuild starts: {reason}")

    async def _rebuild(self, reason: str) -> None:
        """Fetch, preflight the new revision on a candidate checkout while the bot keeps serving, and only then
        stop it, move the running checkouts and restart. A revision that fails the preflight never touches the
        running bot: the failure is recorded and the bot goes on as it was."""
        try:
            await self._rebuild_locked(reason)
        finally:
            self._run_queued_rebuild()  # whatever happened above, a request that waited is not lost

    async def _rebuild_locked(self, reason: str) -> None:
        async with self.lock:
            log(f"rebuild requested: {reason}")
            previous = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            outcome = "unknown"
            stopped = False
            try:
                for repo in (BOT_REPO, CORE_REPO):
                    code, out = git(repo, "fetch", "--prune", "origin")
                    if code != 0:
                        outcome = f"fetch failed in {repo}: {out[-500:]}"
                        return
                code, out = git(BOT_REPO, "rev-parse", "origin/main")
                incoming = out.strip() if code == 0 else "unknown"
                changed = self._changed_files(previous["bot"], incoming)
                if any(path in changed for path in REBUILD_TRIGGER_FILES) or any(path.startswith("launcher/") for path in changed):
                    # The image itself changes: the rebuilder replaces the container from origin/main.
                    await self.stop_child()
                    stopped = True
                    for repo in (BOT_REPO, CORE_REPO):
                        git(repo, "reset", "--hard", "origin/main")
                    if self._request_image_rebuild():
                        self.queued_rebuild = None  # this container is going away; the new one starts from origin/main
                        outcome = "image rebuild requested; the container will be replaced by the rebuilder"
                        return
                    log("image rebuild needed but no rebuilder is configured; continuing in place")
                    self._checkout(previous["bot"], previous["core"])
                for repo in (BOT_REPO, CORE_REPO):
                    ok, out = await asyncio.to_thread(prepare_candidate, repo)
                    if not ok:
                        outcome = f"candidate checkout failed for {repo.name}: {out[-500:]}"
                        return
                ok, transcript = await asyncio.to_thread(preflight, candidate_dir(BOT_REPO))
                if not ok:
                    log("preflight failed on the candidate; the running bot is untouched")
                    FAILED.parent.mkdir(parents=True, exist_ok=True)
                    FAILED.write_text(
                        f"rebuild ({reason}) failed preflight at {datetime.now(UTC).isoformat()}\n"
                        f"the bot kept running on bot={previous['bot'][:10]} core={previous['core'][:10]}\n\n{transcript[-6000:]}"
                    )
                    outcome = "preflight failed; the running revision was kept"
                    return
                await self.stop_child()
                stopped = True
                for repo in (BOT_REPO, CORE_REPO):
                    code, out = git(repo, "reset", "--hard", "origin/main")
                    if code != 0:
                        outcome = f"checkout failed in {repo}: {out[-500:]}"
                        self._checkout(previous["bot"], previous["core"])
                        return
                ok, out = await asyncio.to_thread(install_from_candidate, BOT_REPO)
                if not ok:
                    log("dependency sync failed on the new revision; rolling back")
                    self._checkout(previous["bot"], previous["core"])
                    await asyncio.to_thread(install_from_candidate, BOT_REPO)
                    outcome = f"dependency sync failed; rolled back: {out[-500:]}"
                    return
                FAILED.unlink(missing_ok=True)
                outcome = f"rebuilt: bot {previous['bot'][:10]}→{head(BOT_REPO)[:10]}, core {previous['core'][:10]}→{head(CORE_REPO)[:10]}"
            finally:
                log(f"rebuild outcome: {outcome}")
                LAST_REBUILD.parent.mkdir(parents=True, exist_ok=True)
                LAST_REBUILD.write_text(f"{datetime.now(UTC).isoformat()} {reason}: {outcome}\n")
                if stopped:
                    self.restart_requested.set()

    def _changed_files(self, old: str, new: str, repo: Path = BOT_REPO) -> set[str]:
        if old == new or "unknown" in (old, new):
            return set()
        code, out = git(repo, "diff", "--name-only", old, new)
        return set(out.split()) if code == 0 else set(REBUILD_TRIGGER_FILES)

    def _request_image_rebuild(self) -> bool:
        """Hand the image rebuild to the rebuilder sidecar through the shared trigger directory."""
        if self.mode != "server":
            # Only a server installation has a rebuilder; anywhere else the trigger file would sit there
            # unread and the operator would be told a rebuild was under way that nothing is doing.
            return False
        if not rebuilder_alive():
            return False
        (REBUILD_TRIGGER_DIR / "rebuild").write_text(datetime.now(UTC).isoformat() + "\n")
        log("image rebuild requested from the rebuilder sidecar; this container will be replaced")
        return True

    def _checkout(self, bot_sha: str, core_sha: str) -> None:
        git(BOT_REPO, "reset", "--hard", bot_sha)
        git(CORE_REPO, "reset", "--hard", core_sha)

    async def rollback(self, steps_back: int) -> str:
        if self.lock.locked():
            return "a rebuild or rollback is already in progress"
        result = "not rolled back"
        try:
            result = await self._rollback(steps_back)
            return result
        finally:
            if self.queued_rebuild is not None and result.startswith("rolled back"):
                # The operator just went back on purpose; a rebuild asked for meanwhile would undo that.
                log(f"queued rebuild dropped after the rollback: {self.queued_rebuild}")
                self.queued_rebuild = None
            else:
                self._run_queued_rebuild()  # nothing was rolled back: the request stands

    async def _rollback(self, steps_back: int) -> str:
        async with self.lock:
            await self.stop_child()
            history = load_history()
            current = {"bot": head(BOT_REPO), "core": head(CORE_REPO)}
            candidates = [h for h in history if h["bot"] != current["bot"] or h["core"] != current["core"]]
            if not candidates:
                self.restart_requested.set()
                return "no earlier known-good revision recorded"
            index = max(0, len(candidates) - 1 - steps_back)
            target = candidates[index]
            self._checkout(target["bot"], target["core"])
            ok, transcript = await asyncio.to_thread(preflight, BOT_REPO)
            if not ok:
                self._checkout(current["bot"], current["core"])
                self.restart_requested.set()
                return f"rollback target failed preflight; staying on the current revision\n{transcript[-800:]}"
            self.restart_requested.set()
            return f"rolled back to bot={target['bot'][:10]} core={target['core'][:10]} (from {target['at']}); restarting"

    async def panic(self) -> str:
        child = self.child
        if child is not None and child.returncode is None:
            # Through end_tree, which is what reaches the bot on both platforms: os.killpg does not
            # exist on Windows, so the emergency stop used to raise on the one platform end_tree
            # was written for.
            with contextlib.suppress(ProcessLookupError, OSError):
                end_tree(child.pid, hard=True)
        if POSIX:
            run(["pkill", "-9", "-f", "python[0-9.]* -m daedalus"], timeout=10)
        log("PANIC: process tree killed")
        return "killed everything; the bot restarts in a few seconds"

    def status(self) -> dict[str, Any]:
        return {
            "bot": head(BOT_REPO),
            "core": head(CORE_REPO),
            "child_pid": self.child.pid if self.child else None,
            "child_running": bool(self.child and self.child.returncode is None),
            "known_good": load_history()[-3:],
            "failed": FAILED.read_text()[:2000] if FAILED.exists() else None,
            "last_rebuild": LAST_REBUILD.read_text()[-500:] if LAST_REBUILD.exists() else None,
            "usd_per_day": USD_PER_DAY,
            "budget_exceeded": LIMIT_FLAG.exists(),
            "selfdev_mode": self.mode,
            "last_change": last_change(),
        }

    # -- socket ---------------------------------------------------------------------

    async def apply_dependencies(self, proposal: dict[str, Any], job_id: str) -> dict[str, Any]:
        if self.lock.locked() or any(task is not None and not task.done() for task in (self.rebuild_task, self.restart_task, self.dependencies.task)):
            raise ValueError("another supervisor operation is running")
        job = self.dependencies.accept(proposal, job_id)
        self.dependencies.task = asyncio.create_task(self._install_dependencies(job))
        return job

    async def _install_dependencies(self, job: dict[str, Any]) -> None:
        async with self.lock:
            try:
                await self.dependencies.install(job)
                if self.dependencies.native:
                    await asyncio.sleep(max(0, job["restart_at"] - time.time()))
                    self.dependencies.advance(job, "restarting")
                    await self.stop_child()
                    self.restart_requested.set()
                    job["state"] = "completed"
                    self.dependencies.advance(job, "completed")
                    dependency_runtime.clear_maintenance(self.dependencies.root, job["id"])
                else:
                    # The sidecar survives container replacement and owns the final outcome.
                    while self.dependencies.status()["job"]["state"] == "installing":
                        await asyncio.sleep(2)
            except Exception as exc:
                self.dependencies.fail(job, str(exc))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            request = json.loads(raw.decode("utf-8") or "{}")
            if self.token and not hmac.compare_digest(str(request.get("token") or ""), self.token):
                raise PermissionError("this supervisor listens on a loopback port, which anything on the machine can reach; a command has to carry the secret from the state directory")
            op = request.get("op")
            if op in ("restart", "rebuild", "rollback") and (self.dependencies.status().get("job") or {}).get("state") in ("installing", "restarting"):
                raise RuntimeError("dependencies are being installed; wait for that operation to finish")
            if op == "status":
                result: Any = self.status()
            elif op == "dependencies_status":
                result = self.dependencies.status()
            elif op == "dependencies_inventory":
                result = await self.dependencies.inventory()
            elif op == "dependencies_preview":
                result = await self.dependencies.checked_preview(request.get("additions"))
            elif op == "dependencies_apply":
                result = await self.apply_dependencies(request.get("proposal", {}), str(request.get("id", "")))
            elif op == "rebuild":
                result = await self.rebuild(str(request.get("reason") or ""))
            elif op == "rollback":
                result = await self.rollback(int(request.get("steps_back") or 0))
            elif op == "panic":
                result = await self.panic()
            elif op == "restart":
                result = await self.restart(str(request.get("reason") or "operator request"))
            else:
                result = f"unknown op {op!r}"
            writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
        finally:
            await writer.drain()
            writer.close()

    async def serve_socket(self) -> None:
        """Listen for commands: on a unix socket, or on the loopback interface where there are none.

        The socket is the better channel and is used wherever it exists: it is a file, so the
        operating system's own permissions decide who may ask for a restart. A loopback port has no
        owner at all, so what the file's permissions would have said is said by a secret kept in a
        file with those permissions: the port is bound to 127.0.0.1, and a command that does not
        carry the secret from the state directory is refused.
        """
        if SUPERVISOR_TCP or not POSIX:
            host, _, port = (SUPERVISOR_TCP or "127.0.0.1:8769").rpartition(":")
            self.token = loopback_token()
            server = await asyncio.start_server(self.handle, host or "127.0.0.1", int(port))
            log(f"listening on {host or '127.0.0.1'}:{port}")
        elif len(os.fsencode(str(SOCKET))) >= UNIX_SOCKET_PATH_MAX:
            # A socket's path has to fit the kernel's sun_path, and a data folder deep in a home
            # directory (common on a Mac) does not. Binding it fails, and the supervisor used to run
            # with no command channel at all, so a restart or an Apply could not reach it. It listens
            # on a free loopback port instead, with the secret a port needs, and writes the address
            # into a plain file where the socket would be: the bot is told that path either way, and
            # reading an address out of it is how the terminal daemon's endpoint file works too.
            self.token = loopback_token()
            server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            SOCKET.parent.mkdir(parents=True, exist_ok=True)
            SOCKET.unlink(missing_ok=True)
            fd = os.open(SOCKET, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"tcp://127.0.0.1:{port}\n")
            log(f"the socket path is {len(os.fsencode(str(SOCKET)))} bytes, too long for a unix socket; listening on 127.0.0.1:{port}, written to {SOCKET}")
        else:
            SOCKET.parent.mkdir(parents=True, exist_ok=True)
            SOCKET.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.handle, path=str(SOCKET))
            os.chmod(SOCKET, 0o600)  # the owner's alone, which is the argument for preferring a socket
        async with server:
            await server.serve_forever()

    # -- spend cap ------------------------------------------------------------------

    async def reap_loop(self) -> None:
        """The sandbox wrappers the bot starts die with it and land on PID 1; collect them so they do not pile up."""
        while True:
            await asyncio.sleep(30)
            try:
                keep = {self.child.pid} if self.child is not None and self.child.returncode is None else set()
                reap_zombies(keep)
            except Exception as exc:  # noqa: BLE001
                log(f"reaping failed: {exc}")

    async def budget_loop(self) -> None:
        db_path = STATE / "daedalus.sqlite"
        while True:
            await asyncio.sleep(60)
            try:
                cap = USD_PER_DAY
                if cap <= 0 or not db_path.exists():
                    continue
                spent = _spent_today(db_path)
                if spent > cap and not LIMIT_FLAG.exists():
                    LIMIT_FLAG.write_text(f"{spent:.4f} > {cap:.2f} at {datetime.now(UTC).isoformat()}\n")
                    log(f"daily budget exceeded: ${spent:.4f} > ${cap:.2f}; new runs are refused by the bot")
                elif spent <= cap and LIMIT_FLAG.exists():
                    LIMIT_FLAG.unlink()
            except Exception as exc:  # noqa: BLE001
                log(f"budget check failed: {exc}")


def _spent_today(db_path: Path) -> float:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT sum(cost_usd) FROM usage_events WHERE at >= ?", (today,)).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0.0)


async def main() -> int:
    supervisor = Supervisor()
    loop = asyncio.get_running_loop()

    def _terminate() -> None:
        supervisor.want_running = False
        asyncio.ensure_future(supervisor.stop_child())

    for sig in (signal.SIGTERM, signal.SIGINT, *((signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ())):
        try:
            loop.add_signal_handler(sig, _terminate)
        except NotImplementedError:
            # Windows proactor loops carry no signal handlers; the launcher ends this process by
            # ending its tree, which is what a stop there means anyway.
            signal.signal(sig, lambda *_: _terminate())
    GOOD_DIR.mkdir(parents=True, exist_ok=True)
    if not load_history():
        record_good()
    await asyncio.to_thread(sync_venv_if_stale, BOT_REPO)
    await asyncio.to_thread(build_app_if_missing, BOT_REPO)
    tasks = [
        asyncio.create_task(supervisor.serve_socket()),
        asyncio.create_task(supervisor.budget_loop()),
        asyncio.create_task(supervisor.reap_loop()),
    ]
    await supervisor.run_loop()
    for task in tasks:
        task.cancel()
    return 0


def utf8_streams() -> None:
    """Make standard output and error UTF-8, replacing what cannot be written rather than raising.

    The supervisor's output is the launcher's log file, which on Windows outside UTF-8 mode is written in
    the locale's code page; a commit subject or a path it logs must not end the supervisor.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    utf8_streams()
    sys.exit(asyncio.run(main()))
