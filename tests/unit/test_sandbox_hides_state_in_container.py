"""In the agent's container a sandboxed command cannot read the state volume or the tokens beside it.

The agent runs as root there, beside the volume, so no file mode stood between a command and the database with
the app's API token: only the policy's reading of the words, which in a container does not even name the state
folder. These lay the container's layout out under a scratch folder and run the sandbox over it.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.config import Settings
from daedalus.host.services import session_scratch_dir
from daedalus.host.skills import DRAFTS_DIR
from daedalus.tools import package_cache, shell
from daedalus.tools.shell import DAEMON_RUN_DIRS, hidden_everywhere, hidden_installation, sandbox_argv

API_TOKEN = "the-apps-api-token"
JOURNAL = "a-row-still-in-the-journal"
SUPERVISOR = "the-supervisor-token"
TELEGRAM = "123456:the-telegram-bot-token"
DAEMON = "the-daemon-token"
SESSION = "chat-1"


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The agent's container in miniature: the state volume, the daemons' run directories, the supervisor's
    socket folder, the checkout with its ``.env``, and a workspace."""
    # Not under /tmp: the sandbox has a /tmp of its own, which would hide these for a reason that proves nothing.
    root = Path(tempfile.mkdtemp(prefix="daedalus-test-", dir="/var/tmp"))  # noqa: S108
    state = root / "state"
    (state / "secrets").mkdir(parents=True)
    (state / "daedalus.sqlite").write_text(f"api_token={API_TOKEN}")
    (state / "daedalus.sqlite-wal").write_text(JOURNAL)
    (state / "supervisor.token").write_text(SUPERVISOR)
    (state / "config.toml").write_text("[api]\n")
    draft = state / DRAFTS_DIR / "a-draft"
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text("the draft's text")
    logs = session_scratch_dir(state, SESSION) / ".jobs"
    logs.mkdir(parents=True)
    (logs / "job-1.log").write_text("the job's output")
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.delenv("DAEDALUS_STATE", raising=False)
    for name in DAEMON_RUN_DIRS:
        run = root / "run" / name.lower()
        run.mkdir(parents=True)
        (run / "token").write_text(f"{DAEMON}-{name}")
        monkeypatch.setenv(name, str(run))
    (root / "run-daedalus").mkdir()
    monkeypatch.setenv("SUPERVISOR_SOCKET", str(root / "run-daedalus" / "supervisor.sock"))
    checkout = root / "daedalus"
    (checkout / "skills" / "a-skill").mkdir(parents=True)
    (checkout / "skills" / "a-skill" / "SKILL.md").write_text("a skill")
    (checkout / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TELEGRAM}\n")
    monkeypatch.setenv("BOT_REPO_DIR", str(checkout))
    monkeypatch.setenv("CORE_REPO_DIR", str(root / "protocore-exp"))
    (root / "workspaces" / SESSION).mkdir(parents=True)
    for name in ("DAEDALUS_LOCAL", "DAEDALUS_SECRETS", "DAEDALUS_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shell, "_native", lambda: False)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_the_state_volume_the_run_directories_and_the_supervisor_are_covered(container: Path) -> None:
    argv = hidden_installation()
    covered = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--tmpfs"]
    assert str(container / "state") in covered
    assert str(container / "run-daedalus") in covered
    assert all(str(container / "run" / name.lower()) in covered for name in DAEMON_RUN_DIRS)
    at = argv.index(str(container / "daedalus" / ".env"))
    assert argv[at - 2:at] == ["--ro-bind", "/dev/null"]


def test_the_drafts_are_shown_back_read_only_after_the_cover(container: Path) -> None:
    """SkillDraft writes under the state folder and says to copy the draft into a worktree with Exec."""
    argv = hidden_installation()
    drafts = str(container / "state" / DRAFTS_DIR)
    at = argv.index(drafts)
    assert argv[at - 1] == "--ro-bind" and argv[at + 1] == drafts
    assert at > argv.index(str(container / "state"))


def test_only_a_folder_the_host_opened_or_shows_comes_back(container: Path) -> None:
    """A writable path the host opened inside the state folder is the command's again, read-write; a readable one
    read-only; a path inside the keys' folder never, whatever asked for it."""
    state = container / "state"
    logs = session_scratch_dir(state, SESSION)
    bench = state / "bench" / "work"
    bench.mkdir(parents=True)
    argv = hidden_installation([str(bench), str(state / "secrets")], [str(logs), str(state / "secrets"), str(container / "workspaces" / SESSION)])
    assert argv[argv.index(str(logs)) - 1] == "--ro-bind"
    assert argv[argv.index(str(bench)) - 1] == "--bind"
    assert str(state / "secrets") not in argv
    assert str(container / "workspaces" / SESSION) not in argv, "a path outside every cover needs no bind of its own"


def test_a_supervisor_socket_straight_in_run_does_not_cover_run(container: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_SOCKET", "/run/supervisor.sock")
    assert "/run" not in hidden_installation()


def test_natively_the_supervisor_folder_is_not_this_ones_to_cover(container: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_native", lambda: True)
    assert str(container / "run-daedalus") not in hidden_installation()


def test_nothing_is_named_that_is_not_there(container: Path) -> None:
    """bwrap refuses a whole command over a bind it cannot make: a broken sandbox for every command."""
    shutil.rmtree(container / "state")
    shutil.rmtree(container / "run")
    (container / "daedalus" / ".env").unlink()
    assert hidden_installation() == ["--tmpfs", str(container / "run-daedalus")]


def test_the_run_directories_are_the_ones_the_policy_seals(container: Path) -> None:
    """Named twice, because the configuration imports the shell module: this keeps the two lists one list."""
    sealed = set(Settings(_env_file=None).sealed_everywhere)  # type: ignore[call-arg]
    folders, _, _ = hidden_everywhere(str(container / "state"))
    assert sealed and sealed <= set(folders)


def test_the_drafts_folder_is_the_one_skill_draft_writes() -> None:
    assert shell.STATE_SHOWN_BACK == (DRAFTS_DIR,)


def run_sandboxed(command: str, *, writable: list[Path], readable: list[Path]) -> str:
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, sandboxed = asyncio.run(sandbox_argv(command, config, writable=writable, sealed=[], session_id=SESSION, readable=readable))
    assert sandboxed
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    assert "done" in done.stdout, done.stderr
    return done.stdout


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_a_real_command_reads_no_state_and_no_token_and_still_does_its_work(container: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every secret by a path built so no reading of the words could see it, a workspace task beside it, and the
    control: with nothing covered the same command reads every one."""
    monkeypatch.setattr(package_cache, "writable_dirs", lambda: [])
    workspace = container / "workspaces" / SESSION
    state = container / "state"
    built = f'd="{container}"; for f in sta""te/daedalus.sqlite state/daedalus.sqlite-wal state/supervisor.tok""en daedalus/.e""nv'
    built += "".join(f" run/{name.lower()}/token" for name in DAEMON_RUN_DIRS)
    built += '; do cat "$d/$f" 2>/dev/null; echo; done'
    built += f'; cd "{workspace}" && printf "print(6*7)\\n" > task.py && python3 task.py > answer.txt && cat answer.txt'
    built += f'; cat "{state}/{DRAFTS_DIR}/a-draft/SKILL.md"; echo; cat "{session_scratch_dir(state, SESSION)}/.jobs/job-1.log"; echo'
    built += f'; cat "{container}/daedalus/skills/a-skill/SKILL.md"; echo; echo done'
    readable = [workspace, session_scratch_dir(state, SESSION)]

    output = run_sandboxed(built, writable=[workspace], readable=readable)
    for secret in (API_TOKEN, JOURNAL, SUPERVISOR, TELEGRAM, DAEMON):
        assert secret not in output
    assert "42" in output and (workspace / "answer.txt").read_text().strip() == "42"
    assert "the draft's text" in output and "the job's output" in output and "a skill" in output

    monkeypatch.setattr(shell, "hidden_everywhere", lambda _state: ([], [], []))
    output = run_sandboxed(built, writable=[workspace], readable=readable)
    assert all(secret in output for secret in (API_TOKEN, JOURNAL, SUPERVISOR, TELEGRAM))
    assert output.count(DAEMON) == len(DAEMON_RUN_DIRS)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="a real sandbox")
def test_the_database_written_under_a_running_command_stays_hidden(container: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite writes a new journal and checkpoints into the file while a long command runs: the folder's cover holds."""
    monkeypatch.setattr(package_cache, "writable_dirs", lambda: [])
    state = container / "state"
    flag = container / "written"
    wait = f'echo ready; while [ ! -e "{flag}" ]; do sleep 0.05; done; cat "{state}/daedalus.sqlite" "{state}/daedalus.sqlite-journal"; echo done'
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, _ = asyncio.run(sandbox_argv(wait, config, writable=[], sealed=[], session_id=SESSION))
    running = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert running.stdout is not None and running.stdout.readline().strip() == "ready"
        (state / "daedalus.sqlite-journal").write_text("the-next-token")
        replacement = state / "daedalus.sqlite.new"
        replacement.write_text("api_token=the-next-token")
        replacement.replace(state / "daedalus.sqlite")
        flag.touch()
        output, errors = running.communicate(timeout=20)
    finally:
        running.kill()
    assert "done" in output, errors
    assert "the-next-token" not in output and API_TOKEN not in output
