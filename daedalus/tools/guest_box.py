"""What a guest's command may take of the machine, and the small program that holds it to that.

The sandbox in :mod:`daedalus.tools.shell` decides what a stranger's command can see. Run as a program in front
of that sandbox, this file decides how much it can use. It is started as root, in a mount namespace of its own,
by the agent's container, and everything it mounts is gone with that namespace:

- A cgroup per command, under one for all guests. It bounds threads and memory across every process of the
  command, and what all guests hold at once, which no per-process limit can: ``RLIMIT_AS`` is per process, and the
  kernel exempts root from ``RLIMIT_NPROC`` whatever its capabilities, so that limit is a silent no-op here.
- The folder and ``/tmp`` are a capped tmpfs. The real folder is the lower layer of an overlay, so a command works
  on it as usual while everything it writes lands in memory that has a size and a file count, and is charged to
  its cgroup. Nothing can fill the disk during a command because nothing reaches the disk during a command.
- When the command has ended and nothing of it is left running, its changes are copied into the real folder if
  they fit the folder's quota and leave the disk its floor, and dropped whole if they do not. A command that was
  killed keeps nothing, so a folder never holds half of a stopped command.

Run with ``python -I -S``: it must not import from the folder a guest writes, so it imports nothing of this
package either. Every step after the command ended works on what a stranger made, as root, so every one of them
goes through a directory descriptor with ``O_NOFOLLOW``, and a symlink cannot take a write out of the folder.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time

GIB = 1 << 30
MIB = 1 << 20

LIMITS: dict[str, int] = {
    "processes": 1024,          # threads count, so this is threads: a Python with a BLAS pool takes one per core
    "memory": 8 * GIB,          # resident, page cache and the writable layer together, per command
    "layer_bytes": 4 * GIB,     # what one command may write, /tmp included
    "layer_files": 500_000,     # a large node_modules is about 100,000
    "folder_bytes": 8 * GIB,    # what a chat's folder may hold after a guest's command
    "folder_files": 1_000_000,
    "disk_floor_bytes": 10 * GIB,  # no guest keeps a byte past this much free on the folder's disk
    "disk_floor_files": 1_000_000,
    "all_processes": 2048,      # across every guest at once, so links cannot multiply the numbers above
    "all_memory": 12 * GIB,
    "all_cpu_percent": 50,      # of the machine, across every guest at once
}
"""The defaults, generous for ``npm install``, ``pytest`` and ``git``. A machine with less memory gets less: see :func:`fitted`."""

CGROUP_ROOT = "/sys/fs/cgroup"
AGENT_GROUP = "agent"
GUEST_GROUP = "guests"
REQUIRED_CONTROLLERS = ("pids", "memory")
SCRATCH = "/mnt"
"""Only in this program's mount namespace: the tmpfs goes over whatever the image has here."""
TMP = f"{SCRATCH}/tmp"
REFUSED = 125
"""The exit of a command that never started, as ``env`` and ``timeout`` use it."""
MAX_DEPTH = 128
"""How deep a guest's tree may go to be kept. A tree nested past ``PATH_MAX`` is one path away from a stack overflow."""

MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_RELATIME = 2, 4, 8, 32, 4096, 1 << 21
PR_SET_PDEATHSIG = 1
OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class BoxError(RuntimeError):
    """A bound could not be made, so the command does not run."""


def fitted(limits: dict[str, int], ram: int) -> dict[str, int]:
    """``limits`` on a machine with ``ram`` bytes: guests never get more than half of it, and a command's
    writable layer stays under its memory, so a full layer is a clean ``ENOSPC`` and not an out-of-memory kill."""
    fit = dict(limits)
    fit["all_memory"] = min(fit["all_memory"], ram // 2)
    fit["memory"] = min(fit["memory"], fit["all_memory"])
    fit["layer_bytes"] = min(fit["layer_bytes"], fit["memory"] // 2)
    return fit


def layer_paths(index: int) -> tuple[str, str, str]:
    """Where the ``index``-th writable folder's layer lives: its upper, work and merged directories."""
    base = f"{SCRATCH}/layer{index}"
    return f"{base}/upper", f"{base}/work", f"{base}/merged"


def size_words(amount: int) -> str:
    return f"{amount / GIB:.3g} GiB" if amount >= GIB else f"{amount / MIB:.3g} MiB"


def say(text: str) -> None:
    """A line the model reads in the command's output. A limit that only shows as a failed call is a limit the
    model works around blindly, so each one names itself."""
    sys.stderr.write(f"\n[sandbox limit] {text}\n")
    sys.stderr.flush()


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def mount(source: str | None, target: str, kind: str | None, flags: int, data: str | None = None) -> None:
    """``mount(2)``, called directly because ``mount(8)`` splits options on commas of its own and a folder's path
    goes in them."""
    libc = ctypes.CDLL(None, use_errno=True)
    encoded = [value.encode() if value is not None else None for value in (source, target, kind)]
    if libc.mount(encoded[0], encoded[1], encoded[2], ctypes.c_ulong(flags), data.encode() if data is not None else None) != 0:
        code = ctypes.get_errno()
        raise BoxError(f"mount {kind or 'remount'} on {target}: {os.strerror(code)}")


def die_with_parent() -> None:
    """A command must not outlive the agent, which is what ``--die-with-parent`` promised before this program stood between them."""
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


# The cgroup ---------------------------------------------------------------------------------------------------


def own_group(proc_file: str = "/proc/self/cgroup") -> str:
    for line in read(proc_file).splitlines():
        if line.startswith("0::"):
            return line[3:] or "/"
    raise BoxError("this machine has no unified cgroup hierarchy")


def move_aside(root: str, controllers: list[str], *, tries: int = 100, pause: float = 0.02) -> None:
    """Make the container's own cgroup able to hand controllers to its children.

    A cgroup with processes in it cannot, so everything in the container moves to a child first, once per
    container: a new container starts clean, and a restarted agent finds it done. ``docker exec`` keeps working,
    because runc joins the first process's cgroup when the container's own refuses it. All controllers go in one
    write and not one by one: ``pids`` alone is accepted with processes inside, turns the cgroup into a threaded
    root, and every child after that is ``domain invalid`` and useless.
    """
    wanted = " ".join(f"+{name}" for name in controllers)
    subtree = os.path.join(root, "cgroup.subtree_control")
    if set(controllers) <= set(read(subtree).split()):
        return
    aside = os.path.join(root, AGENT_GROUP)
    os.makedirs(aside, exist_ok=True)
    failure: OSError | None = None
    for _ in range(tries):
        # A process forked while this loop reads is caught on the next pass; that is why it is a loop.
        for pid in read(os.path.join(root, "cgroup.procs")).split():
            try:
                write(os.path.join(aside, "cgroup.procs"), pid)
            except OSError:
                pass  # It exited between the read and the write, or it is a kernel thread's stub. The enable below decides.
        try:
            write(subtree, wanted)
            return
        except OSError as exc:
            failure = exc
            time.sleep(pause)
    raise BoxError(f"the container's cgroup keeps its processes: {failure}")


def guests_group(root: str, limits: dict[str, int], *, proc_file: str = "/proc/self/cgroup", cpus: int | None = None) -> str:
    """The cgroup all guests' commands live under, made and bounded; idempotent, it runs before every command.

    It sits beside the agent's and not under it, so nothing a guest does can be charged to the agent or starve it
    of its own limit, and it is written every time so that new numbers need nothing but a new build.
    """
    where = own_group(proc_file)
    if where not in ("/", f"/{AGENT_GROUP}"):
        raise BoxError(f"this process is in cgroup {where}, not at the root of a cgroup namespace of its own, so there is nothing to delegate")
    available = read(os.path.join(root, "cgroup.controllers")).split()
    if missing := [name for name in REQUIRED_CONTROLLERS if name not in available]:
        raise BoxError(f"the {' and '.join(missing)} cgroup controller is not delegated to this container")
    controllers = [name for name in (*REQUIRED_CONTROLLERS, "cpu") if name in available]
    move_aside(root, controllers)
    guests = os.path.join(root, GUEST_GROUP)
    os.makedirs(guests, exist_ok=True)
    if set(controllers) - set(read(os.path.join(guests, "cgroup.subtree_control")).split()):
        write(os.path.join(guests, "cgroup.subtree_control"), " ".join(f"+{name}" for name in controllers))
    write(os.path.join(guests, "pids.max"), str(limits["all_processes"]))
    write(os.path.join(guests, "memory.max"), str(limits["all_memory"]))
    set_swap_off(guests)
    if "cpu" in controllers:
        count = cpus or os.cpu_count() or 1
        write(os.path.join(guests, "cpu.max"), f"{max(1000, count * limits['all_cpu_percent'] * 1000)} 100000")
    return guests


def set_swap_off(group: str) -> None:
    """A tmpfs page in swap is a page on the disk this program exists to protect, and memory that left the bound."""
    try:
        write(os.path.join(group, "memory.swap.max"), "0")
    except FileNotFoundError:
        pass  # A kernel booted without swap accounting has no such file; with no swap the point is moot.


def running(pid: int) -> bool:
    """Whether ``pid`` is a live process. A zombie is not: the box of a killed command can wait unreaped for as long
    as the container's first process lets it, with its cgroup still here."""
    try:
        return read(f"/proc/{pid}/stat").rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def sweep(guests: str) -> None:
    """Remove what commands killed outright left behind. Their cgroups outlive them, and only this can reach them:
    a command is killed with ``SIGKILL``, so this program never gets to clean up after itself."""
    for name in os.listdir(guests):
        group = os.path.join(guests, name)
        if name.startswith("c") and name[1:].isdigit() and os.path.isdir(group) and not running(int(name[1:])):
            end_group(group)


def end_group(group: str, *, tries: int = 200, pause: float = 0.01) -> bool:
    """Kill all that is left in ``group``, wait until it is gone, remove it. True once it is empty, which is what
    makes the layer safe to read: a process still alive could swap a path under the copy."""
    try:
        write(os.path.join(group, "cgroup.kill"), "1")
    except OSError:
        pass
    for _ in range(tries):
        try:
            os.rmdir(group)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(pause)
    return False


def command_group(guests: str, limits: dict[str, int], pid: int) -> str:
    """The cgroup of one command. ``memory.oom.group``: what fills a tmpfs is not freed by killing one process of
    many, and the kernel would go through them one at a time while the rest kept writing."""
    sweep(guests)
    group = os.path.join(guests, f"c{pid}")
    os.mkdir(group)
    write(os.path.join(group, "pids.max"), str(limits["processes"]))
    write(os.path.join(group, "memory.max"), str(limits["memory"]))
    set_swap_off(group)
    write(os.path.join(group, "memory.oom.group"), "1")
    return group


def join_group(group: str) -> None:
    """Runs in the forked child, before the sandbox starts, so its first process is already counted."""
    write(os.path.join(group, "cgroup.procs"), "0")
    die_with_parent()


def event_count(group: str, events_file: str, name: str) -> int:
    try:
        lines = read(os.path.join(group, events_file)).splitlines()
    except OSError:
        return 0
    return next((int(line.split()[1]) for line in lines if line.startswith(f"{name} ")), 0)


# The layer ----------------------------------------------------------------------------------------------------


def make_layers(folders: list[str], limits: dict[str, int]) -> None:
    """The capped tmpfs, ``/tmp`` in it, and an overlay over each writable folder.

    ``index`` and ``metacopy`` are off so the upper directory says everything plainly, as files, whiteouts and
    marks, and :func:`apply` can read it. ``redirect_dir`` is on, with the extra work in :func:`find_moves`, because
    without it renaming a directory of the real folder answers ``EXDEV``, and ``git mv`` has no fallback for that:
    found by running one. Docker images live with that; a folder someone works in cannot.
    """
    mount("guest", SCRATCH, "tmpfs", MS_NOSUID | MS_NODEV, f"size={limits['layer_bytes']},nr_inodes={limits['layer_files']},mode=0755")
    os.mkdir(TMP)
    os.chmod(TMP, 0o1777)
    for index, folder in enumerate(folders):
        upper, work, merged = layer_paths(index)
        for path in (upper, work, merged):
            os.makedirs(path)
        real = os.lstat(folder)
        # The top of an overlay shows its upper directory's owner and mode, not the folder's.
        os.chown(upper, real.st_uid, real.st_gid)
        os.chmod(upper, stat.S_IMODE(real.st_mode))
        lower = folder.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,")
        mount("overlay", merged, "overlay", 0, f"lowerdir={lower},upperdir={upper},workdir={work},redirect_dir=on,index=off,metacopy=off,xino=off")


def layer_full(limits: dict[str, int]) -> bool:
    """Whether the tmpfs ran out, by what is left of it: within a page of its bytes, or with no file left."""
    room = os.statvfs(SCRATCH)
    return room.f_bavail * room.f_frsize < MIB or room.f_favail < 16


def tally(directory: int, *, strict: bool, depth: int = 0) -> tuple[int, int]:
    """Bytes and entries under the open ``directory``, by each file's stated size and each name once more.

    Stated size and not blocks: a sparse file is small here and whole when copied, and a hard link is a file of
    its own by the time it reaches the real folder. A tree past :data:`MAX_DEPTH` raises when ``strict``.
    """
    total = entries = 0
    for name in os.listdir(directory):
        info = os.lstat(name, dir_fd=directory)
        if stat.S_ISCHR(info.st_mode) and info.st_rdev == 0:
            continue
        entries += 1
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                if strict:
                    raise BoxError(f"it nests directories deeper than {MAX_DEPTH}")
                continue
            child = os.open(name, OPEN_DIRECTORY, dir_fd=directory)
            try:
                more, more_entries = tally(child, strict=strict, depth=depth + 1)
            finally:
                os.close(child)
            total += more
            entries += more_entries
    return total, entries


def remove(directory: int, name: str) -> None:
    try:
        info = os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(name, dir_fd=directory)
    else:
        os.unlink(name, dir_fd=directory)


def mark(directory: int, name: str) -> str:
    """The overlay's mark ``name`` on an open directory of the upper layer. A guest cannot have set it: ``trusted.``
    needs a capability the sandbox dropped, and the overlay hides its own names from the sandbox anyway."""
    try:
        return os.getxattr(directory, f"trusted.overlay.{name}").decode("utf-8", "surrogateescape")
    except OSError:
        return ""


def find_moves(upper: int, here: tuple[str, ...], origin: tuple[str, ...] | None, depth: int = 0) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Every directory the command renamed: where it is now, and where its contents are in the real folder.

    ``origin`` is where ``here`` came from in the real folder, or None if it is new. A relative redirect is
    relative to that, not to where the parent is now, because a renamed directory's children keep their marks.
    """
    found = []
    for name in os.listdir(upper):
        if not stat.S_ISDIR(os.lstat(name, dir_fd=upper).st_mode):
            continue
        child = os.open(name, OPEN_DIRECTORY, dir_fd=upper)
        try:
            redirect = mark(child, "redirect")
            source: tuple[str, ...] | None
            if redirect:
                source = (*(() if redirect.startswith("/") else origin or ()), *(part for part in redirect.split("/") if part))
                if ".." in source or "." in source or not source:
                    raise BoxError(f"it left a directory with an impossible origin, {redirect!r}")
                found.append(((*here, name), source))
            else:
                source = None if origin is None or mark(child, "opaque") == "y" else (*origin, name)
            if depth < MAX_DEPTH:
                found += find_moves(child, (*here, name), source, depth + 1)
        finally:
            os.close(child)
    return found


def open_path(root: int, parts: tuple[str, ...]) -> int:
    """Open a directory below ``root`` one name at a time, so no symlink on the way is followed."""
    current = os.dup(root)
    for part in parts:
        try:
            following = os.open(part, OPEN_DIRECTORY, dir_fd=current)
        finally:
            os.close(current)
        current = following
    return current


def hold_moved(upper: int, real: int, holding: str) -> dict[tuple[str, ...], str]:
    """Take every renamed directory out of the real folder, into ``holding`` there, before anything else changes.

    Longest origin first, so a directory taken out of one that also moved is found where it still is. Taken out
    first, because a whiteout at the old name is about to delete it, and a swap of two names has no order that
    works in place. :func:`apply` puts each back when it reaches the new name, and merges into it as usual.
    """
    moves = sorted(find_moves(upper, (), ()), key=lambda move: len(move[1]), reverse=True)
    held: dict[tuple[str, ...], str] = {}
    if not moves:
        return held
    os.mkdir(holding, 0o700, dir_fd=real)
    for number, (target, origin) in enumerate(moves):
        try:
            parent = open_path(real, origin[:-1])
        except OSError:
            continue  # Gone from under the command, by the operator's hand: the directory stands as new.
        try:
            os.rename(origin[-1], f"{holding}/{number}", src_dir_fd=parent, dst_dir_fd=real)
            held[target] = str(number)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)
    return held


def copy_file(upper: int, real: int, name: str, info: os.stat_result) -> None:
    """One regular file, in beside its destination and renamed over it, so a failure leaves the old one whole.
    The setuid and setgid bits stay behind: the operator's later turns run as root."""
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=upper)
    temporary = f".guest-copy-{os.getpid()}"
    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=real)
    try:
        while os.sendfile(target, source, None, 1 << 26):
            pass
        os.fchown(target, info.st_uid, info.st_gid)
        os.fchmod(target, stat.S_IMODE(info.st_mode) & 0o777)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    finally:
        os.close(target)
        os.close(source)
    existing = lstat_or_none(real, name)
    if existing is not None and stat.S_ISDIR(existing.st_mode):
        remove(real, name)
    os.rename(temporary, name, src_dir_fd=real, dst_dir_fd=real)


def lstat_or_none(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return None


def apply(upper: int, real: int, *, held: dict[tuple[str, ...], str] | None = None, holding: str = "", top: int = -1, here: tuple[str, ...] = ()) -> None:
    """Make the real folder what the command left: ``upper`` and ``real`` are open directories.

    A whiteout (a character device of 0:0) deletes, a directory marked opaque replaces, a renamed one comes back
    from where :func:`hold_moved` put it, any other directory merges, and what the kernel would not let a capless
    command make, or has no meaning in a folder, is skipped. A symlink in the real folder is never walked: the
    check is ``O_NOFOLLOW`` itself, not a look before the leap.
    """
    held = held or {}
    top = real if top < 0 else top
    for name in os.listdir(upper):
        if not here and name == holding:
            continue  # A name a guest guessed: it is not to reach the directories being held.
        info = os.lstat(name, dir_fd=upper)
        mode = info.st_mode
        if stat.S_ISCHR(mode) and info.st_rdev == 0:
            remove(real, name)
        elif stat.S_ISDIR(mode):
            source = os.open(name, OPEN_DIRECTORY, dir_fd=upper)
            try:
                existing = lstat_or_none(real, name)
                if (*here, name) in held:
                    remove(real, name)
                    os.rename(f"{holding}/{held[(*here, name)]}", name, src_dir_fd=top, dst_dir_fd=real)
                elif existing is None or not stat.S_ISDIR(existing.st_mode) or mark(source, "opaque") == "y":
                    remove(real, name)
                    os.mkdir(name, 0o700, dir_fd=real)
                target = os.open(name, OPEN_DIRECTORY, dir_fd=real)
                try:
                    apply(source, target, held=held, holding=holding, top=top, here=(*here, name))
                    os.fchown(target, info.st_uid, info.st_gid)
                    os.fchmod(target, stat.S_IMODE(mode) & 0o777)
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
                finally:
                    os.close(target)
            finally:
                os.close(source)
        elif stat.S_ISLNK(mode):
            pointed = os.readlink(name, dir_fd=upper)
            remove(real, name)
            os.symlink(pointed, name, dir_fd=real)
            os.chown(name, info.st_uid, info.st_gid, dir_fd=real, follow_symlinks=False)
        elif stat.S_ISREG(mode):
            copy_file(upper, real, name, info)


def keep(upper_path: str, folder: str, limits: dict[str, int]) -> str:
    """Copy one layer into its folder, or nothing of it; why not, in words the model can act on, or empty.

    Whole or nothing, so a folder at its limit is never left with half of what a command built. The check and the
    copy hold one lock across the installation's folders: two chats at once must not both spend the same room.
    """
    guard = os.open(os.path.dirname(folder.rstrip("/")) or "/", os.O_RDONLY)
    upper = os.open(upper_path, OPEN_DIRECTORY)
    real = os.open(folder, OPEN_DIRECTORY)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            wrote, entries = tally(upper, strict=True)
        except BoxError as exc:
            return f"nothing this command wrote in {folder} was kept: {exc}."
        held, files = tally(real, strict=False)
        room = os.statvfs(folder)
        free = room.f_bavail * room.f_frsize
        why = ""
        # Only what adds is held against the quota: a command that deletes from a folder past it must get through.
        if wrote and held + wrote > limits["folder_bytes"]:
            why = f"the folder holds {size_words(held)}, the command wrote {size_words(wrote)}, and a guest's folder may hold {size_words(limits['folder_bytes'])}"
        elif entries and files + entries > limits["folder_files"]:
            why = f"the folder holds {files} files, the command made {entries}, and a guest's folder may hold {limits['folder_files']}"
        elif free - wrote < limits["disk_floor_bytes"]:
            why = f"the server's disk has {size_words(free)} free and must keep {size_words(limits['disk_floor_bytes'])}, which is for its owner to fix"
        elif room.f_files and room.f_favail - entries < limits["disk_floor_files"]:
            why = f"the server's disk has {room.f_favail} free inodes and must keep {limits['disk_floor_files']}, which is for its owner to fix"
        if why:
            return f"nothing this command wrote in {folder} was kept: {why}. To make room in the folder, delete something in a command of its own; deleting always fits."
        holding = f".guest-moves-{os.getpid()}"
        apply(upper, real, held=hold_moved(upper, real, holding), holding=holding)
        if lstat_or_none(real, holding) is not None:
            os.rmdir(holding, dir_fd=real)
        return ""
    except (OSError, BoxError) as exc:
        return f"what this command wrote in {folder} was only partly kept: {exc}."
    finally:
        os.close(real)
        os.close(upper)
        os.close(guard)  # Which also lets go of the lock.


def limit_notes(group: str, limits: dict[str, int], *, full: bool) -> list[str]:
    """What the kernel counted while the command ran, in a sentence each."""
    notes = []
    if event_count(group, "pids.events", "max"):
        notes.append(f"this command reached the limit of {limits['processes']} processes and threads, or of {limits['all_processes']} across all guests at once: starting another one failed with \"Resource temporarily unavailable\". Run fewer at once.")
    if event_count(group, "memory.events", "oom_kill"):
        notes.append(f"this command reached the memory limit of {size_words(limits['memory'])}, or of {size_words(limits['all_memory'])} across all guests at once, and was killed whole. What it wrote counts as memory while it runs.")
    if full:
        notes.append(f"this command filled its writable space of {size_words(limits['layer_bytes'])} and {limits['layer_files']} files, the folder and /tmp together: writes failed with \"No space left on device\".")
    return notes


def run(folders: list[str], command: list[str], limits: dict[str, int], *, root: str = CGROUP_ROOT) -> int:
    die_with_parent()
    group = ""
    try:
        # Read-only as Docker mounts it, and writable only in this namespace: the agent's other commands keep the read-only view.
        mount(None, root, None, MS_REMOUNT | MS_BIND | MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_RELATIME)
        guests = guests_group(root, limits)
        group = command_group(guests, limits, os.getpid())
        make_layers(folders, limits)
        child = subprocess.Popen(command, preexec_fn=lambda: join_group(group))  # noqa: S603, PLW1509 - no threads here to deadlock
    except (OSError, BoxError, subprocess.SubprocessError) as exc:
        if group:
            end_group(group)
        say(f"the limits on this command could not be set, so it did not run: {exc}")
        return REFUSED
    code = child.wait()
    notes = limit_notes(group, limits, full=layer_full(limits))
    if not end_group(group):
        notes.append("processes of this command would not die, so nothing it wrote was kept.")
        folders = []
    notes += filter(None, (keep(layer_paths(index)[0], folder, limits) for index, folder in enumerate(folders)))
    for note in notes:
        say(note)
    return 128 - code if code < 0 else code


def main(arguments: list[str]) -> int:
    """``guest_box.py '{"folders": [...]}' <the sandbox's argv>``. Limits in the spec are for proving them small."""
    spec = json.loads(arguments[0])
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return run(list(spec["folders"]), arguments[1:], fitted({**LIMITS, **spec.get("limits", {})}, ram))


if __name__ == "__main__":
    # ENOSPC on the note itself must not hide the command's own exit.
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(errno.EPIPE)
