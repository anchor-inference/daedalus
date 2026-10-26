//go:build linux

package chrome

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// PrivateBytes is what a process costs on its own: its anonymous and shared-memory pages, from
// /proc/<pid>/status. The resident size counts Chromium's code once per process, and summed over a
// browser's dozen processes it read four to five times what the browser's cgroup held; a sandboxed
// renderer's smaps (and so its proportional size) cannot be read at all.
func PrivateBytes(pid int) int64 {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
	if err != nil {
		return 0
	}
	var total int64
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "RssAnon:") && !strings.HasPrefix(line, "RssShmem:") {
			continue
		}
		f := strings.Fields(line)
		if len(f) >= 2 {
			kb, _ := strconv.ParseInt(f[1], 10, 64)
			total += kb << 10
		}
	}
	return total
}

// RendererSandboxed says whether a renderer among pids runs under Chromium's seccomp filter:
// sandboxed is the answer, known is false when no renderer was found to ask.
func RendererSandboxed(pids []int) (sandboxed, known bool) {
	for _, pid := range pids {
		cmd, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cmdline")
		// Chromium rewrites its helpers' command lines into one space-separated string.
		if err != nil || !strings.Contains(string(cmd), "--type=renderer") {
			continue
		}
		status, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(status), "\n") {
			if strings.HasPrefix(line, "Seccomp:") {
				return strings.TrimSpace(strings.TrimPrefix(line, "Seccomp:")) == "2", true
			}
		}
	}
	return false, false
}

// Cgroup is the memory cgroup the daemon runs in, as the kernel charges it: its anonymous and shared
// memory (the page cache it could drop is left out, as it is from the private figure), and the
// processes in it.
type Cgroup struct {
	Path      string
	AnonShmem int64
	Procs     []int
}

// OwnCgroup reads the daemon's own cgroup (v2). It is false where there is none to read: a cgroup v1
// machine, or the root cgroup, which has no memory.stat and holds the whole machine anyway.
func OwnCgroup() (Cgroup, bool) { return readCgroup("/proc/self/cgroup", "/sys/fs/cgroup") }

func readCgroup(selfCgroup, mount string) (Cgroup, bool) {
	data, err := os.ReadFile(selfCgroup)
	if err != nil {
		return Cgroup{}, false
	}
	rel := ""
	found := false
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if strings.HasPrefix(line, "0::") {
			rel, found = strings.TrimPrefix(line, "0::"), true
			break
		}
	}
	if !found {
		return Cgroup{}, false
	}
	dir := mount + rel
	stat, err := os.ReadFile(dir + "/memory.stat")
	if err != nil {
		return Cgroup{}, false
	}
	cg := Cgroup{Path: rel}
	for _, line := range strings.Split(string(stat), "\n") {
		f := strings.Fields(line)
		if len(f) == 2 && (f[0] == "anon" || f[0] == "shmem") {
			n, _ := strconv.ParseInt(f[1], 10, 64)
			cg.AnonShmem += n
		}
	}
	procs, err := os.ReadFile(dir + "/cgroup.procs")
	if err != nil {
		return Cgroup{}, false
	}
	for _, f := range strings.Fields(string(procs)) {
		if pid, err := strconv.Atoi(f); err == nil {
			cg.Procs = append(cg.Procs, pid)
		}
	}
	return cg, true
}

// waitGroupGone waits until no process of the process group pgid is left, at most limit, and says
// whether none is. A zombie waiting for its parent is gone: it holds no memory and runs nothing.
//
// SIGKILL to the group returns before its members have died. Chromium's sandboxed helpers run in a
// PID namespace of their own, and the namespace's first process stays in its exit until the kernel
// has killed and reaped everything under it. On a loaded machine that took tens of milliseconds
// after the browser's main process had been reaped, and the daemon had already exited and said its
// browsers were gone while one of them was still on its way out.
func waitGroupGone(pgid int, limit time.Duration) bool {
	deadline := time.Now().Add(limit)
	for groupRunning(pgid) {
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(5 * time.Millisecond)
	}
	return true
}

// groupRunning says whether a process of the group pgid that is not a zombie is left.
func groupRunning(pgid int) bool {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return false
	}
	want := strconv.Itoa(pgid)
	for _, e := range entries {
		data, err := os.ReadFile("/proc/" + e.Name() + "/stat")
		if err != nil {
			continue
		}
		// The command name in parentheses may hold spaces; the fields after it are state, parent
		// and process group.
		i := strings.LastIndexByte(string(data), ')')
		if i < 0 {
			continue
		}
		f := strings.Fields(string(data[i+1:]))
		if len(f) > 2 && f[2] == want && f[0] != "Z" {
			return true
		}
	}
	return false
}
