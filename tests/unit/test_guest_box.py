"""The bounds on a guest's command as a whole.

What can run as an ordinary user runs here: the words of the command line, the cgroup's files against a plain folder,
and the copying back, on real files. What needs root, the mounts and the cgroup itself, has its proof in the image.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from protocore.contracts.types import ToolResultBlock

from daedalus.config import Settings
from daedalus.stores.database import Database
from daedalus.tools import guest_box, shell
from tests.unit.test_guest_turns import ScriptedProvider, guest_says, manager_with, settled, turn

BWRAP = "/usr/bin/bwrap"
LIMITS = guest_box.fitted(guest_box.LIMITS, 64 * guest_box.GIB)


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def whiteout(path: Path) -> None:
    """The kernel lets anyone make this one device, which is how an overlay can write it for an unprivileged mounter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFCHR | 0o600, 0)


def apply(upper: Path, real: Path, holding: str = ".held") -> None:
    upper_fd, real_fd = os.open(upper, guest_box.OPEN_DIRECTORY), os.open(real, guest_box.OPEN_DIRECTORY)
    try:
        guest_box.apply(upper_fd, real_fd, held=guest_box.hold_moved(upper_fd, real_fd, holding), holding=holding)
        if (real / holding).exists():
            (real / holding).rmdir()
    finally:
        os.close(upper_fd)
        os.close(real_fd)


def marked(monkeypatch: pytest.MonkeyPatch, marks: dict[tuple[int, str], str]) -> None:
    """Stand in for ``trusted.`` attributes, which need root to set: marks by inode of the upper directory."""
    monkeypatch.setattr(guest_box, "mark", lambda directory, name: marks.get((os.fstat(directory).st_ino, name), ""))


# -- the command line ------------------------------------------------------------------------------------------


def test_a_guests_command_runs_inside_the_box_with_its_folder_and_tmp_on_the_capped_layer(tmp_path: Path) -> None:
    argv = shell.guest_box_argv(BWRAP, writable=[tmp_path], readable=[tmp_path])
    assert argv[0].endswith("unshare") and argv[1:3] == ["--net", "--mount"], "the layers die with this namespace"
    assert argv[4:6] == ["-I", "-S"] and argv[6] == guest_box.__file__, "the box must not import from a folder a guest wrote"
    assert '"folders": ["' + str(tmp_path) + '"]' in argv[7]
    assert argv.index(next(a for a in argv if a.endswith("prlimit"))) < argv.index(BWRAP), "the box itself keeps none of a command's limits"
    bind = argv.index("--bind")
    assert argv[bind:bind + 3] == ["--bind", guest_box.TMP, "/tmp"] and "--tmpfs" in argv and argv[argv.index("--tmpfs") + 1] == "/"
    assert ["--bind", guest_box.layer_paths(0)[2], str(tmp_path)] == argv[bind + 3:bind + 6] or guest_box.layer_paths(0)[2] in argv
    assert str(tmp_path) != argv[argv.index(guest_box.layer_paths(0)[2]) - 1], "the real folder itself is not bound writable"
    assert ["--ro-bind", str(tmp_path), str(tmp_path)] not in [argv[i:i + 3] for i in range(len(argv))], "a writable folder is not bound twice"


def test_a_read_only_folder_stays_read_only_and_gets_no_layer(tmp_path: Path) -> None:
    argv = shell.guest_box_argv(BWRAP, writable=[], readable=[tmp_path])
    assert '"folders": []' in argv[7] and guest_box.layer_paths(0)[2] not in argv
    assert ["--ro-bind", str(tmp_path), str(tmp_path)] in [argv[i:i + 3] for i in range(len(argv))]


async def test_a_guests_command_never_runs_outside_the_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shell, "is_guest_turn", lambda session: True)
    monkeypatch.setattr(shell, "guest_sandbox_status", lambda: "ok")
    monkeypatch.setattr(shell, "bwrap_path", lambda: BWRAP)
    monkeypatch.setattr(shell, "guest_limits_status", lambda: "no cgroup here")
    with pytest.raises(shell.SandboxUnavailable, match="within its limits: no cgroup here"):
        await shell.sandbox_argv("true", SimpleNamespace(sandbox="off"), writable=[tmp_path], session_id="s")
    monkeypatch.setattr(shell, "guest_limits_status", lambda: "ok")
    argv, sandboxed = await shell.sandbox_argv("true", SimpleNamespace(sandbox="off"), writable=[tmp_path], session_id="s")
    assert sandboxed and guest_box.__file__ in argv


def test_the_box_runs_with_no_site_packages_and_refuses_to_run_what_it_cannot_bound(tmp_path: Path) -> None:
    """As an ordinary user every mount fails, so this is the real program failing closed, in the way it is really started."""
    witness = tmp_path / "ran"
    spec = '{"folders": []}'
    done = subprocess.run([sys.executable, "-I", "-S", guest_box.__file__, spec, "/usr/bin/touch", str(witness)], capture_output=True, text=True, check=False)
    assert done.returncode == guest_box.REFUSED and not witness.exists()
    assert "the limits on this command could not be set, so it did not run" in done.stderr


# -- where the bound cannot be made ----------------------------------------------------------------------------


@pytest.mark.parametrize(("native", "uid"), [("1", 0), ("", 1000)])
def test_no_limits_on_a_native_install_or_without_root(monkeypatch: pytest.MonkeyPatch, native: str, uid: int) -> None:
    monkeypatch.setattr(shell, "_guest_limits_state", None)
    monkeypatch.setenv("DAEDALUS_NATIVE", native)
    monkeypatch.setattr(shell.os, "geteuid", lambda: uid)
    assert "agent's container" in shell.guest_limits_status()


def test_macos_and_windows_stay_without_the_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_guest_limits_state", None)
    monkeypatch.delenv("DAEDALUS_NATIVE", raising=False)
    monkeypatch.setattr(shell.os, "geteuid", lambda: 0)
    monkeypatch.setattr(shell.sys, "platform", "darwin")
    assert "agent's container" in shell.guest_limits_status()


def test_the_probe_is_the_real_box_and_only_a_success_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_guest_limits_state", None)
    monkeypatch.delenv("DAEDALUS_NATIVE", raising=False)
    monkeypatch.setattr(shell.os, "geteuid", lambda: 0)
    monkeypatch.setattr(shell, "bwrap_path", lambda: BWRAP)
    seen: list[list[str]] = []

    def answer(code: int, words: str) -> None:
        monkeypatch.setattr(shell.subprocess, "run", lambda argv, **_: seen.append(argv) or SimpleNamespace(returncode=code, stderr=words, stdout=""))

    answer(guest_box.REFUSED, "the memory cgroup controller is not delegated to this container")
    assert "not delegated" in shell.guest_limits_status() and guest_box.__file__ in seen[0]
    answer(0, "")
    assert shell.guest_limits_status() == "ok"
    answer(1, "")
    assert shell.guest_limits_status() == "ok" and len(seen) == 2


async def test_all_is_not_offered_where_the_limits_cannot_be_set(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.host import session_runner

    manager = await manager_with(settings, db, ScriptedProvider([]))
    state = await manager.create_session("chat", own_directory=True)
    monkeypatch.setattr(session_runner, "guest_sandbox_status", lambda: "ok")
    monkeypatch.setattr(session_runner, "guest_limits_status", lambda: "not delegated")
    assert await manager.guest_full_tools(state) == "no_limits"
    assert await manager.guest_ready(state.session.id, "all") == "no_limits"
    assert await manager.guest_ready(state.session.id, "read") == ""
    await manager.close()


@pytest.mark.usefixtures("bare_sandbox")
async def test_a_command_past_its_time_is_told_it_kept_nothing(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await manager_with(settings, db, provider)
    state = await manager.create_session("chat", own_directory=True)
    provider.script = [{"tool": "Exec", "args": {"command": "python3 -uc \"import time; print('started'); time.sleep(30)\"", "timeout_seconds": 1}}, {"text": "done"}]
    await guest_says(manager, state.session.id, turn("all"))
    await settled(manager, state)
    seen = "\n".join(block.content or "" for message in provider.requests[-1].messages for block in message.content_blocks if isinstance(block, ToolResultBlock))
    assert "started" in seen and "A stopped command keeps nothing" in seen, seen[:400]
    await manager.close()


# -- the cgroup ------------------------------------------------------------------------------------------------


def cgroup_root(tmp_path: Path, processes: str = "1\n8\n", controllers: str = "cpuset cpu io memory pids\n", subtree: str = "") -> Path:
    root = tree(tmp_path / "cgroup", {"cgroup.controllers": controllers, "cgroup.subtree_control": subtree, "cgroup.procs": processes})
    (tmp_path / "self").write_text("0::/\n")
    return root


def guests(root: Path, tmp_path: Path) -> Path:
    (root / "guests").mkdir(exist_ok=True)
    (root / "guests" / "cgroup.subtree_control").write_text("")
    return Path(guest_box.guests_group(str(root), LIMITS, proc_file=str(tmp_path / "self"), cpus=16))


def test_the_container_moves_aside_and_every_controller_goes_on_in_one_write(tmp_path: Path) -> None:
    """``pids`` on its own is accepted with processes inside, makes the cgroup a threaded root, and leaves every child
    useless: seen in the image, and only a new container undoes it."""
    root = cgroup_root(tmp_path)
    (root / "agent").mkdir()
    (root / "agent" / "cgroup.procs").write_text("")
    guests(root, tmp_path)
    assert (root / "agent" / "cgroup.procs").read_text() == "8", "the last one written; the kernel takes them one at a time"
    assert (root / "cgroup.subtree_control").read_text() == "+pids +memory +cpu"


def test_moving_aside_retries_until_the_container_has_really_emptied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = cgroup_root(tmp_path)
    (root / "agent").mkdir()
    real_write = guest_box.write
    tries = []

    def write(path: str, text: str) -> None:
        if path.endswith("subtree_control") and len(tries) < 2:
            tries.append(path)
            raise OSError(16, "Device or resource busy")
        real_write(path, text)

    monkeypatch.setattr(guest_box, "write", write)
    guest_box.move_aside(str(root), ["pids", "memory"], pause=0)
    assert len(tries) == 2 and (root / "cgroup.subtree_control").read_text() == "+pids +memory"


def test_moving_aside_gives_up_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = cgroup_root(tmp_path)
    (root / "agent").mkdir()
    real_write = guest_box.write

    def write(path: str, text: str) -> None:
        if path.endswith("subtree_control"):
            raise OSError(16, "Device or resource busy")
        real_write(path, text)

    monkeypatch.setattr(guest_box, "write", write)
    with pytest.raises(guest_box.BoxError, match="keeps its processes"):
        guest_box.move_aside(str(root), ["pids", "memory"], tries=3, pause=0)


def test_a_restarted_agent_does_not_move_anything_again(tmp_path: Path) -> None:
    root = cgroup_root(tmp_path, subtree="cpu memory pids\n")
    guest_box.move_aside(str(root), ["pids", "memory", "cpu"])
    assert not (root / "agent").exists() and (root / "cgroup.subtree_control").read_text() == "cpu memory pids\n"


def test_all_guests_together_have_one_bound(tmp_path: Path) -> None:
    root = cgroup_root(tmp_path, subtree="cpu memory pids\n")
    group = guests(root, tmp_path)
    assert (group / "pids.max").read_text() == "2048"
    assert (group / "memory.max").read_text() == str(12 * guest_box.GIB)
    assert (group / "memory.swap.max").read_text() == "0" if (group / "memory.swap.max").exists() else True
    assert (group / "cpu.max").read_text() == "800000 100000", "half of 16 processors"
    assert (group / "cgroup.subtree_control").read_text() == "+pids +memory +cpu"


@pytest.mark.parametrize(("group", "why"), [("/system.slice/docker-x.scope", "not at the root"), ("/user.slice/session-1.scope", "not at the root")])
def test_a_process_that_does_not_own_its_cgroup_namespace_delegates_nothing(tmp_path: Path, group: str, why: str) -> None:
    """With a host cgroup namespace or on a bare machine, ``/sys/fs/cgroup`` is the whole machine's."""
    root = cgroup_root(tmp_path)
    (tmp_path / "self").write_text(f"0::{group}\n")
    with pytest.raises(guest_box.BoxError, match=why):
        guest_box.guests_group(str(root), LIMITS, proc_file=str(tmp_path / "self"))
    assert not (root / "agent").exists()


def test_a_missing_controller_is_named(tmp_path: Path) -> None:
    root = cgroup_root(tmp_path, controllers="cpu pids\n")
    with pytest.raises(guest_box.BoxError, match="memory cgroup controller is not delegated"):
        guest_box.guests_group(str(root), LIMITS, proc_file=str(tmp_path / "self"))


def test_one_commands_group_holds_its_limits(tmp_path: Path) -> None:
    group = Path(guest_box.command_group(str(tmp_path), LIMITS, 4242))
    assert group.name == "c4242"
    assert (group / "pids.max").read_text() == "1024"
    assert (group / "memory.max").read_text() == str(8 * guest_box.GIB)
    assert (group / "memory.swap.max").read_text() == "0", "a tmpfs page in swap is on the disk this exists to protect"
    assert (group / "memory.oom.group").read_text() == "1"


def test_the_next_command_clears_what_a_killed_one_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exec kills past-time commands with ``SIGKILL``, so the box never reaches its own cleanup."""
    for name in ("c111", "c222", "agent-of-someone"):
        (tmp_path / name).mkdir()
    ended: list[str] = []
    monkeypatch.setattr(guest_box, "running", lambda pid: pid == 222)
    monkeypatch.setattr(guest_box, "end_group", lambda group: ended.append(Path(group).name))
    guest_box.sweep(str(tmp_path))
    assert ended == ["c111"]


def test_a_zombie_box_counts_as_gone(tmp_path: Path) -> None:
    """A box whose parent was killed waits unreaped under the first process; found with ``sleep`` as that process."""
    child = subprocess.Popen(["/bin/true"])
    try:
        for _ in range(500):
            if not guest_box.running(child.pid):
                break
            os.sched_yield()
        assert not guest_box.running(child.pid), "exited and not yet reaped"
        assert guest_box.running(os.getpid())
    finally:
        child.wait()
    assert not guest_box.running(child.pid)


def test_what_the_kernel_counted_is_said_in_words(tmp_path: Path) -> None:
    tree(tmp_path, {"pids.events": "max 3\n", "memory.events": "low 0\nhigh 0\nmax 9\noom 1\noom_kill 2\n"})
    notes = guest_box.limit_notes(str(tmp_path), LIMITS, full=True)
    assert "1024 processes and threads" in notes[0] and "2048 across all guests" in notes[0]
    assert "8 GiB" in notes[1] and "killed whole" in notes[1]
    assert "4 GiB and 500000 files" in notes[2] and "No space left on device" in notes[2]
    tree(tmp_path, {"pids.events": "max 0\n", "memory.events": "oom_kill 0\n"})
    assert guest_box.limit_notes(str(tmp_path), LIMITS, full=False) == []


def test_a_small_machine_gets_small_numbers() -> None:
    fit = guest_box.fitted(guest_box.LIMITS, 4 * guest_box.GIB)
    assert fit["all_memory"] == 2 * guest_box.GIB and fit["memory"] == 2 * guest_box.GIB, "half of what is there, or a guest could take it all"
    assert fit["layer_bytes"] == guest_box.GIB, "a full layer must be ENOSPC, never an out-of-memory kill"


def test_the_defaults_are_the_ones_the_documents_state() -> None:
    assert LIMITS["processes"] == 1024 and LIMITS["memory"] == 8 * guest_box.GIB and LIMITS["layer_bytes"] == 4 * guest_box.GIB and LIMITS["layer_files"] == 500_000
    assert LIMITS["folder_bytes"] == 8 * guest_box.GIB and LIMITS["folder_files"] == 1_000_000 and LIMITS["disk_floor_bytes"] == 10 * guest_box.GIB
    assert LIMITS["all_processes"] == 2048 and LIMITS["all_memory"] == 12 * guest_box.GIB and LIMITS["all_cpu_percent"] == 50


# -- what comes back -------------------------------------------------------------------------------------------


def test_new_and_changed_files_land_in_the_real_folder_with_their_mode(tmp_path: Path) -> None:
    real = tree(tmp_path / "real", {"kept": "kept", "changed": "old", "dir/inner": "inner"})
    upper = tree(tmp_path / "upper", {"new": "new", "changed": "fresh", "dir/added": "added"})
    os.chmod(upper / "new", 0o640)
    apply(upper, real)
    assert (real / "kept").read_text() == "kept" and (real / "changed").read_text() == "fresh"
    assert (real / "dir" / "inner").read_text() == "inner" and (real / "dir" / "added").read_text() == "added"
    assert stat.S_IMODE((real / "new").stat().st_mode) == 0o640


def test_a_whiteout_deletes_and_never_arrives_as_a_device(tmp_path: Path) -> None:
    real = tree(tmp_path / "real", {"file": "x", "dir/inner": "y"})
    upper = tmp_path / "upper"
    whiteout(upper / "file")
    whiteout(upper / "dir")
    whiteout(upper / "was-never-there")
    apply(upper, real)
    assert sorted(os.listdir(real)) == []


def test_setuid_does_not_survive_the_copy(tmp_path: Path) -> None:
    """The operator's later turns run as root, and a command of a guest's could leave them a program to run."""
    real = tmp_path / "real"
    real.mkdir()
    upper = tree(tmp_path / "upper", {"prog": "x"})
    os.chmod(upper / "prog", 0o6755)
    apply(upper, real)
    assert stat.S_IMODE((real / "prog").stat().st_mode) == 0o755


def test_a_write_cannot_follow_a_symlink_out_of_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guest replaced the link with a directory in the sandbox, which the upper layer shows plainly. As root, the
    copy would otherwise have written through a link left in the real folder by an earlier command."""
    outside = tree(tmp_path / "outside", {"passwd": "the machine's"})
    real = tmp_path / "real"
    real.mkdir()
    (real / "link").symlink_to(outside)
    (real / "file-link").symlink_to(outside / "passwd")
    upper = tree(tmp_path / "upper", {"link/passwd": "owned", "file-link": "owned"})
    marked(monkeypatch, {})
    apply(upper, real)
    assert (outside / "passwd").read_text() == "the machine's"
    assert not (real / "link").is_symlink() and (real / "link" / "passwd").read_text() == "owned"
    assert not (real / "file-link").is_symlink() and (real / "file-link").read_text() == "owned"


def test_a_symlink_comes_back_as_a_symlink_and_is_never_read_through(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    upper = tmp_path / "upper"
    upper.mkdir()
    (upper / "link").symlink_to("/etc/shadow")
    apply(upper, real)
    assert os.readlink(real / "link") == "/etc/shadow"


def test_a_fifo_is_left_behind(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    upper = tmp_path / "upper"
    upper.mkdir()
    os.mkfifo(upper / "pipe")
    apply(upper, real)
    assert os.listdir(real) == []


def test_an_opaque_directory_replaces_and_an_unmarked_one_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tree(tmp_path / "real", {"opaque/old": "o", "merged/old": "o"})
    upper = tree(tmp_path / "upper", {"opaque/new": "n", "merged/new": "n"})
    marked(monkeypatch, {((upper / "opaque").stat().st_ino, "opaque"): "y"})
    apply(upper, real)
    assert sorted(os.listdir(real / "opaque")) == ["new"] and sorted(os.listdir(real / "merged")) == ["new", "old"]


def test_a_file_replaces_a_directory_and_a_directory_a_file(tmp_path: Path) -> None:
    real = tree(tmp_path / "real", {"was-dir/inner": "x", "was-file": "y"})
    upper = tree(tmp_path / "upper", {"was-dir": "now a file", "was-file/inner": "now a dir"})
    apply(upper, real)
    assert (real / "was-dir").read_text() == "now a file" and (real / "was-file" / "inner").read_text() == "now a dir"


def test_a_renamed_directory_keeps_what_it_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``git mv`` of a directory: without redirects it fails with ``EXDEV`` and has no fallback, found in the image."""
    real = tree(tmp_path / "real", {"tests/a": "a", "tests/deep/b": "b"})
    upper = tree(tmp_path / "upper", {"suite/added": "added"})
    whiteout(upper / "tests")
    marked(monkeypatch, {((upper / "suite").stat().st_ino, "redirect"): "tests"})
    apply(upper, real)
    assert sorted(os.listdir(real)) == ["suite"]
    assert sorted(os.listdir(real / "suite")) == ["a", "added", "deep"] and (real / "suite" / "deep" / "b").read_text() == "b"


def test_two_directories_can_swap_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tree(tmp_path / "real", {"x/from-x": "1", "y/from-y": "2"})
    upper = tmp_path / "upper"
    (upper / "x").mkdir(parents=True)
    (upper / "y").mkdir()
    marked(monkeypatch, {((upper / "x").stat().st_ino, "redirect"): "y", ((upper / "y").stat().st_ino, "redirect"): "x"})
    apply(upper, real)
    assert os.listdir(real / "x") == ["from-y"] and os.listdir(real / "y") == ["from-x"]


def test_a_directory_moved_out_of_a_directory_that_also_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its origin is inside the other's: taken out in the wrong order, it is no longer there to take."""
    real = tree(tmp_path / "real", {"a/c/deep": "1", "a/other": "2"})
    upper = tmp_path / "upper"
    (upper / "b").mkdir(parents=True)
    (upper / "d").mkdir()
    whiteout(upper / "a")
    whiteout(upper / "b" / "c")
    marked(monkeypatch, {((upper / "b").stat().st_ino, "redirect"): "a", ((upper / "d").stat().st_ino, "redirect"): "/a/c"})
    apply(upper, real)
    assert sorted(os.listdir(real)) == ["b", "d"] and os.listdir(real / "b") == ["other"] and os.listdir(real / "d") == ["deep"]


def test_a_child_renamed_inside_a_renamed_parent_is_found_at_its_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tree(tmp_path / "real", {"a/c/deep": "1"})
    upper = tmp_path / "upper"
    (upper / "b" / "d").mkdir(parents=True)
    whiteout(upper / "a")
    whiteout(upper / "b" / "c")
    marked(monkeypatch, {((upper / "b").stat().st_ino, "redirect"): "a", ((upper / "b" / "d").stat().st_ino, "redirect"): "c"})
    apply(upper, real)
    assert (real / "b" / "d" / "deep").read_text() == "1" and not (real / "a").exists()


def test_a_redirect_out_of_the_folder_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    upper = tmp_path / "upper"
    (upper / "x").mkdir(parents=True)
    marked(monkeypatch, {((upper / "x").stat().st_ino, "redirect"): "/../../etc"})
    assert "impossible origin" in guest_box.keep(str(upper), str(real), LIMITS)
    assert os.listdir(real) == []


# -- the quota -------------------------------------------------------------------------------------------------


QUOTA = {**LIMITS, "folder_bytes": 1000, "folder_files": 10, "disk_floor_bytes": 0, "disk_floor_files": 0}


def test_a_command_past_the_folders_quota_keeps_nothing_and_says_why(tmp_path: Path) -> None:
    """Whole or nothing: half of a build in the folder is worse than none, and what stopped it can be read."""
    real = tree(tmp_path / "w" / "real", {"held": "x" * 700})
    upper = tree(tmp_path / "upper", {"one": "y" * 200, "two": "y" * 200})
    words = guest_box.keep(str(upper), str(real), QUOTA)
    assert sorted(os.listdir(real)) == ["held"]
    assert "nothing this command wrote" in words and "may hold 1000" not in words and "delete something" in words


def test_within_the_quota_it_all_arrives(tmp_path: Path) -> None:
    real = tree(tmp_path / "w" / "real", {"held": "x" * 500})
    assert guest_box.keep(str(tree(tmp_path / "upper", {"one": "y" * 400})), str(real), QUOTA) == ""
    assert sorted(os.listdir(real)) == ["held", "one"]


def test_deleting_always_fits(tmp_path: Path) -> None:
    """Or a folder at its limit could never be brought back under it."""
    real = tree(tmp_path / "w" / "real", {"big": "x" * 5000})
    upper = tmp_path / "upper"
    whiteout(upper / "big")
    tight = {**QUOTA, "folder_bytes": 1, "folder_files": 0, "disk_floor_bytes": 1 << 60, "disk_floor_files": 1 << 60}
    assert guest_box.keep(str(upper), str(real), tight) == "" and os.listdir(real) == []


def test_many_small_files_count_as_files(tmp_path: Path) -> None:
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    upper = tree(tmp_path / "upper", {f"f{index}": "" for index in range(11)})
    assert "made 11" in guest_box.keep(str(upper), str(real), QUOTA) and os.listdir(real) == []


def test_a_sparse_file_counts_at_the_size_it_will_be(tmp_path: Path) -> None:
    """Small on the tmpfs, and a real write of its whole length once it is copied."""
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    upper = tmp_path / "upper"
    upper.mkdir()
    with (upper / "sparse").open("wb") as handle:
        handle.truncate(5000)
    assert "nothing this command wrote" in guest_box.keep(str(upper), str(real), QUOTA) and os.listdir(real) == []


def test_the_disk_keeps_its_floor_whatever_the_folder_may_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every link has its own quota, so the database's disk needs a bound of its own."""
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    upper = tree(tmp_path / "upper", {"one": "x"})
    words = guest_box.keep(str(upper), str(real), {**LIMITS, "disk_floor_bytes": 1 << 60})
    assert "must keep" in words and "for its owner to fix" in words and os.listdir(real) == []
    words = guest_box.keep(str(upper), str(real), {**LIMITS, "disk_floor_bytes": 0, "disk_floor_files": 1 << 60})
    assert "free inodes" in words and os.listdir(real) == []


def test_a_tree_too_deep_to_walk_is_not_kept(tmp_path: Path) -> None:
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    upper = tmp_path / "upper"
    upper.mkdir()
    fd = os.open(upper, guest_box.OPEN_DIRECTORY)
    for _ in range(guest_box.MAX_DEPTH + 2):
        os.mkdir("d", dir_fd=fd)
        deeper = os.open("d", guest_box.OPEN_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = deeper
    os.close(fd)
    assert "deeper than" in guest_box.keep(str(upper), str(real), LIMITS) and os.listdir(real) == []


def test_a_cut_short_copy_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    upper = tree(tmp_path / "upper", {"one": "x"})

    def fail(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(guest_box, "apply", fail)
    assert "only partly kept" in guest_box.keep(str(upper), str(real), QUOTA)


def test_a_box_that_cannot_make_a_bound_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    witness = tmp_path / "ran"

    def refuse(*_: object, **__: object) -> None:
        raise guest_box.BoxError("no such thing here")

    monkeypatch.setattr(guest_box, "mount", refuse)
    assert guest_box.run([], ["/usr/bin/touch", str(witness)], LIMITS) == guest_box.REFUSED
    assert not witness.exists()
