//go:build linux

package main

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

type fenceProcRef struct {
	PID  int
	TID  int
	What string
	Path string
}

// fenceScanProcs finds processes of this user that stand inside a tree: a working directory, a
// root, an open descriptor or a mapping on one of its inodes. It is compared by inode, not by path,
// because the tree is renamed during the switch.
//
// Every thread is looked at, not only the process: a thread that called unshare(CLONE_FS) has a
// working directory of its own that /proc/<pid>/cwd does not show. A process of this user that
// cannot be inspected (PR_SET_DUMPABLE 0: ssh-agent, browsers) is not taken to be clean; it is
// named in unscanned so the operator sees it. Processes of other users are invisible here, and the
// limits say so.
func fenceScanProcs(inodes map[[2]uint64]string) (refs []fenceProcRef, unscanned []fenceUnscanned, limits []string) {
	self := os.Getpid()
	euid := uint32(os.Geteuid())
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, nil, []string{"/proc cannot be read: no process was checked"}
	}
	others := 0
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid == self {
			continue
		}
		var st unix.Stat_t
		if err := unix.Stat(fmt.Sprintf("/proc/%d", pid), &st); err != nil {
			continue // gone
		}
		if st.Uid != euid {
			others++
			continue
		}
		found, blocked := fenceScanProc(pid, inodes)
		refs = append(refs, found...)
		if blocked != "" {
			comm, _ := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid))
			unscanned = append(unscanned, fenceUnscanned{PID: pid, Comm: strings.TrimSpace(string(comm)), Reason: blocked})
		}
	}
	if others > 0 {
		limits = append(limits, fmt.Sprintf("%d processes of other users cannot be seen from here; a writer among them is still stopped by the lease, but not named", others))
	}
	if len(unscanned) > 0 {
		limits = append(limits, fmt.Sprintf("%d processes of this user refused inspection; a write of theirs into the previous tree is kept by the removal check, not prevented", len(unscanned)))
	}
	return refs, unscanned, limits
}

func fenceStatIno(path string) ([2]uint64, error) {
	var st unix.Stat_t
	if err := unix.Stat(path, &st); err != nil {
		return [2]uint64{}, err
	}
	return [2]uint64{uint64(st.Dev), st.Ino}, nil
}

func fenceScanProc(pid int, inodes map[[2]uint64]string) (refs []fenceProcRef, blocked string) {
	base := fmt.Sprintf("/proc/%d", pid)
	check := func(tid int, what, path string) {
		key, err := fenceStatIno(path)
		if err != nil {
			if errors.Is(err, unix.EACCES) || errors.Is(err, unix.EPERM) {
				blocked = "not inspected: " + err.Error()
			}
			return
		}
		if rel, ok := inodes[key]; ok {
			refs = append(refs, fenceProcRef{PID: pid, TID: tid, What: what, Path: rel})
		}
	}
	tasks, err := os.ReadDir(base + "/task")
	if err != nil {
		if errors.Is(err, os.ErrPermission) {
			blocked = "not inspected: " + err.Error()
		}
		return refs, blocked
	}
	for _, task := range tasks {
		tid, err := strconv.Atoi(task.Name())
		if err != nil {
			continue
		}
		tbase := fmt.Sprintf("%s/task/%d", base, tid)
		check(tid, "cwd", tbase+"/cwd")
		check(tid, "root", tbase+"/root")
		fds, err := os.ReadDir(tbase + "/fd")
		if err != nil {
			if errors.Is(err, os.ErrPermission) {
				blocked = "not inspected: " + err.Error()
			}
			continue
		}
		for _, fd := range fds {
			check(tid, "fd "+fd.Name(), tbase+"/fd/"+fd.Name())
		}
	}
	maps, err := os.Open(base + "/maps")
	if err != nil {
		if errors.Is(err, os.ErrPermission) {
			blocked = "not inspected: " + err.Error()
		}
		return refs, blocked
	}
	defer maps.Close()
	scanner := bufio.NewScanner(maps)
	scanner.Buffer(make([]byte, 64*1024), 1<<20)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 5 {
			continue
		}
		ino, err := strconv.ParseUint(fields[4], 10, 64)
		if err != nil || ino == 0 {
			continue
		}
		majmin := strings.SplitN(fields[3], ":", 2)
		if len(majmin) != 2 {
			continue
		}
		major, err1 := strconv.ParseUint(majmin[0], 16, 32)
		minor, err2 := strconv.ParseUint(majmin[1], 16, 32)
		if err1 != nil || err2 != nil {
			continue
		}
		key := [2]uint64{unix.Mkdev(uint32(major), uint32(minor)), ino}
		if rel, ok := inodes[key]; ok {
			refs = append(refs, fenceProcRef{PID: pid, What: "mapping", Path: rel})
		}
	}
	if err := scanner.Err(); err != nil && errors.Is(err, os.ErrPermission) {
		blocked = "not inspected: " + err.Error()
	}
	return refs, blocked
}

// fenceWhoWrites names the processes of this user that hold one inode writable — for the refusal
// message only. Nobody found means another user, or a process we may not inspect.
func fenceWhoWrites(dev, ino uint64) []int {
	self := os.Getpid()
	var pids []int
	entries, _ := os.ReadDir("/proc")
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid == self {
			continue
		}
		hit := false
		fds, _ := os.ReadDir(fmt.Sprintf("/proc/%d/fd", pid))
		for _, fd := range fds {
			key, err := fenceStatIno(fmt.Sprintf("/proc/%d/fd/%s", pid, fd.Name()))
			if err != nil || key != [2]uint64{dev, ino} {
				continue
			}
			info, _ := os.ReadFile(fmt.Sprintf("/proc/%d/fdinfo/%s", pid, fd.Name()))
			for _, line := range strings.Split(string(info), "\n") {
				if value, ok := strings.CutPrefix(line, "flags:"); ok {
					flags, _ := strconv.ParseUint(strings.TrimSpace(value), 8, 64)
					if flags&(unix.O_WRONLY|unix.O_RDWR) != 0 {
						hit = true
					}
				}
			}
		}
		if body, err := os.ReadFile(fmt.Sprintf("/proc/%d/maps", pid)); err == nil {
			for _, line := range strings.Split(string(body), "\n") {
				fields := strings.Fields(line)
				if len(fields) < 5 || len(fields[1]) < 4 || fields[1][1] != 'w' || fields[1][3] != 's' {
					continue
				}
				if n, err := strconv.ParseUint(fields[4], 10, 64); err == nil && n == ino {
					hit = true
				}
			}
		}
		if hit {
			pids = append(pids, pid)
		}
	}
	return pids
}

// fenceInContainer is true inside Docker, Podman or a Kubernetes pod. There the files may belong to
// a user namespace, the lease may be granted by a capability, and nobody has probed any of it, so
// the switch refuses.
func fenceInContainer() bool {
	for _, marker := range []string{"/.dockerenv", "/run/.containerenv"} {
		if _, err := os.Lstat(marker); err == nil {
			return true
		}
	}
	body, _ := os.ReadFile("/proc/self/cgroup")
	text := string(body)
	for _, sign := range []string{"/docker/", "/docker-", "kubepods", "libpod", "/lxc/"} {
		if strings.Contains(text, sign) {
			return true
		}
	}
	return false
}

// fenceHasCapLease is true when this process may take a lease on files it does not own. Then the
// EACCES that marks a foreign file never comes, and the ownership check would be the only guard.
func fenceHasCapLease() bool {
	body, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return true // unknown: assume the worst
	}
	for _, line := range strings.Split(string(body), "\n") {
		if value, ok := strings.CutPrefix(line, "CapEff:"); ok {
			caps, err := strconv.ParseUint(strings.TrimSpace(value), 16, 64)
			if err != nil {
				return true
			}
			return caps&(1<<unix.CAP_LEASE) != 0
		}
	}
	return true
}

func fenceLeasesEnabled() bool {
	body, err := os.ReadFile("/proc/sys/fs/leases-enable")
	return err == nil && strings.TrimSpace(string(body)) == "1"
}

// fenceMountFSType resolves the filesystem type by mount id. The statfs magic alone would take ext2
// and ext3 for ext4.
func fenceMountFSType(mountID uint64) (string, error) {
	body, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(string(body), "\n") {
		parts := strings.SplitN(line, " - ", 2)
		if len(parts) != 2 {
			continue
		}
		left, right := strings.Fields(parts[0]), strings.Fields(parts[1])
		if len(left) == 0 || len(right) == 0 {
			continue
		}
		if id, err := strconv.ParseUint(left[0], 10, 64); err == nil && id == mountID {
			return right[0], nil
		}
	}
	return "", errors.New("mount id not in mountinfo")
}

type fenceCount struct {
	files, dirs uint64
}

// fenceCountTree is the capacity preflight: a read-only pass over the tree through directory
// descriptors, before anything is created. It refuses early what the fenced walk would refuse
// anyway (another owner, a hard link, a special file, inode flags) so that such a folder never even
// gets a slot. It is not the fence; the fenced walk repeats every check.
func fenceCountTree(label string, parent *os.File, name string, euid uint32, dev uint64, seams *fenceSeams) (fenceCount, error) {
	var count fenceCount
	fd, err := unix.Openat(int(parent.Fd()), name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return count, fenceFail(label, ".", "the data folder cannot be opened as a directory", err)
	}
	var walk func(fd int, rel string) error
	walk = func(fd int, rel string) error {
		dir := os.NewFile(uintptr(fd), rel)
		defer dir.Close()
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return fenceFail(label, rel, "stat", err)
		}
		if seams != nil && seams.statEntry != nil {
			seams.statEntry(label, rel, &st)
		}
		if st.Uid != euid {
			return fenceFail(label, rel, fmt.Sprintf("owned by uid %d, not by this user: no fence possible", st.Uid), nil)
		}
		if uint64(st.Dev) != dev {
			return fenceFail(label, rel, "a mount inside the data folder", nil)
		}
		if err := fencePermissions(label, rel, &st); err != nil {
			return err
		}
		if err := fenceCheckFlags(label, rel, fd); err != nil {
			return err
		}
		count.dirs++
		names, err := dir.Readdirnames(-1)
		if err != nil {
			return fenceFail(label, rel, "the directory cannot be listed", err)
		}
		sort.Strings(names)
		for _, name := range names {
			childRel := fenceJoin(rel, name)
			var child unix.Stat_t
			if err := unix.Fstatat(fd, name, &child, unix.AT_SYMLINK_NOFOLLOW); err != nil {
				return &fenceProblem{Outcome: fenceRefused, Tree: label, Path: childRel, Cause: "changed while it was being counted", Err: err}
			}
			if seams != nil && seams.statEntry != nil {
				seams.statEntry(label, childRel, &child)
			}
			if child.Uid != euid {
				return fenceFail(label, childRel, fmt.Sprintf("owned by uid %d, not by this user: no fence possible", child.Uid), nil)
			}
			if uint64(child.Dev) != dev {
				return fenceFail(label, childRel, "a mount inside the data folder", nil)
			}
			if err := fencePermissions(label, childRel, &child); err != nil {
				return err
			}
			switch child.Mode & unix.S_IFMT {
			case unix.S_IFDIR:
				sub, err := unix.Openat(fd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
				if err != nil {
					return &fenceProblem{Outcome: fenceRefused, Tree: label, Path: childRel, Cause: "changed while it was being counted", Err: err}
				}
				if err := walk(sub, childRel); err != nil {
					return err
				}
			case unix.S_IFREG:
				count.files++
			case unix.S_IFLNK:
				count.files++
			default:
				return fenceFail(label, childRel, "a special file (device, pipe or socket) cannot be copied or fenced", nil)
			}
		}
		return nil
	}
	return count, walk(fd, ".")
}

// fenceRaiseNoFile lifts the soft descriptor limit to the hard one and checks the fence fits: every
// file and directory of both trees stays open for the whole switch, plus room for everything else.
func fenceRaiseNoFile(count fenceCount) error {
	var limit unix.Rlimit
	if err := unix.Getrlimit(unix.RLIMIT_NOFILE, &limit); err != nil {
		return err
	}
	if limit.Cur < limit.Max {
		raised := limit
		raised.Cur = raised.Max
		if err := unix.Setrlimit(unix.RLIMIT_NOFILE, &raised); err == nil {
			limit = raised
		}
	}
	need := 2*(count.files+count.dirs) + 64
	if need > limit.Cur {
		return fmt.Errorf("RLIMIT_NOFILE is %d; the fence needs %d descriptors (%d files, %d directories, both trees)", limit.Cur, need, count.files, count.dirs)
	}
	return nil
}

// fenceWatchBudget checks that both trees can be watched: the per-user watch limit minus what this
// user's processes already hold. Processes that cannot be read are counted as unknown in the limits.
func fenceWatchBudget(count fenceCount) (limits []string, err error) {
	body, err := os.ReadFile("/proc/sys/fs/inotify/max_user_watches")
	if err != nil {
		return nil, err
	}
	max, err := strconv.ParseUint(strings.TrimSpace(string(body)), 10, 64)
	if err != nil {
		return nil, err
	}
	euid := uint32(os.Geteuid())
	var used uint64
	unreadable := 0
	entries, _ := os.ReadDir("/proc")
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		var st unix.Stat_t
		if unix.Stat(fmt.Sprintf("/proc/%d", pid), &st) != nil || st.Uid != euid {
			continue
		}
		infos, err := os.ReadDir(fmt.Sprintf("/proc/%d/fdinfo", pid))
		if err != nil {
			unreadable++
			continue
		}
		for _, info := range infos {
			body, err := os.ReadFile(filepath.Join(fmt.Sprintf("/proc/%d/fdinfo", pid), info.Name()))
			if err != nil {
				continue
			}
			used += uint64(strings.Count(string(body), "inotify wd:"))
		}
	}
	if unreadable > 0 {
		limits = append(limits, fmt.Sprintf("inotify watches of %d processes of this user could not be counted", unreadable))
	}
	need := 2 * count.dirs
	if used > max || need > max-used {
		return limits, fmt.Errorf("inotify watches: %d needed, %d of %d already in use", need, used, max)
	}
	return limits, nil
}
