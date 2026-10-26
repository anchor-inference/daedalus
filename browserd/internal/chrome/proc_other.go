//go:build !linux

package chrome

import "time"

// PrivateBytes is not measured outside Linux; the caller falls back to the resident size.
func PrivateBytes(pid int) int64 { return -1 }

// RendererSandboxed cannot be asked outside Linux; Chromium's own start-up refusal is the signal.
func RendererSandboxed(pids []int) (sandboxed, known bool) { return false, false }

// Cgroup is Linux's; elsewhere there is none to read.
type Cgroup struct {
	Path      string
	AnonShmem int64
	Procs     []int
}

// OwnCgroup is false outside Linux.
func OwnCgroup() (Cgroup, bool) { return Cgroup{}, false }

// waitGroupGone has no process table to read outside Linux; the kill is taken as done.
func waitGroupGone(pgid int, limit time.Duration) bool { return true }
