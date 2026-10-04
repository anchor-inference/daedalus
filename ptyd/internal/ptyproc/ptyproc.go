// Package ptyproc starts a program on a new pseudo-terminal and ends it, and everything it started,
// on request. On Unix the terminal is a PTY pair; on Windows it is a pseudoconsole (ConPTY), and the
// program's tree is held in a job object, which is how that system ends a tree.
package ptyproc

import (
	"errors"
	"os"
)

// Spec is what to run.
type Spec struct {
	Path     string   // resolved executable
	Argv     []string // argv[0] included
	Dir      string
	Env      []string
	Cols     int
	Rows     int
	PxW, PxH int
	// Tag is an environment entry ("NAME=value") that every process of this terminal inherits. On
	// Linux and macOS, KillTree also ends processes that carry it, which catches the ones that left the
	// session and the process tree (a daemonising `setsid` whose parent has already exited).
	Tag string
	// Wrapped is a program started inside bubblewrap: the process the PTY runs is bubblewrap, and
	// the program is its grandchild in a namespace of its own. See ProgramGroup.
	Wrapped bool
	// UseContainment places the child in an already limited cgroup at clone, before its code runs.
	UseContainment bool
	ContainmentFD  int

	// ExtraFiles are borrowed until Start returns; child descriptors begin at 3.
	ExtraFiles []*os.File
}

// Exit is how the program ended.
type Exit struct {
	Code   int    // exit status, or -1 when a signal ended it
	Signal string // "KILL", "HUP"…, or "" for a normal exit
}

// ErrUnsupported is returned for what the platform's terminals cannot do: the foreground job of a
// Windows console, or a signal it has no counterpart for.
var ErrUnsupported = errors.New("not supported on this system")
