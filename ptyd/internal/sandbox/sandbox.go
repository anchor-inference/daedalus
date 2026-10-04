// Package sandbox wraps a terminal's program in bubblewrap: the whole filesystem read-only, the
// folders the host names writable, a private /tmp and process namespace, and the daemon's own run
// and state directories hidden, so a sandboxed shell cannot read the token that commands the daemon.
//
// The sandbox is built here and not by the host, because bubblewrap has to be probed and run where
// the process lives: in the terminals container, on a server, or on the operator's machine.
//
// It is a wall against writing, not against reading. Everything outside the masked directories is
// readable, as it is for the agent's own Exec sandbox, whose flags these follow with two
// differences: no --new-session, which would take the shell's controlling terminal away and with it
// job control (TIOCSTI inside a PTY the daemon owns can only type into that same PTY), and the
// masks.
package sandbox

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// HistoryFile is where a sandboxed shell keeps its history. Home is read-only inside, and a shell
// that cannot write its history says so at every exit.
const HistoryFile = "/tmp/.shell_history"

// MaxWritable is the most writable folders one terminal may name.
const MaxWritable = 256

// ErrHiddenCwd is a working directory inside one of the daemon's own directories, which the
// sandbox hides: the program would start in an empty directory that is not the one asked for.
var ErrHiddenCwd = errors.New("the working directory is inside the terminal daemon's own directories, which the sandbox hides")

// Bind is a path of the daemon's own that a sandboxed program needs back after the masks: a
// launch's overlay files, its dial directory, the hook command.
type Bind struct {
	Path     string
	Writable bool
}

// Options is one program to wrap.
type Options struct {
	Bwrap    string   // the bubblewrap executable
	Argv     []string // the program, argv[0] resolved to an absolute path
	Cwd      string   // absolute
	Writable []string // folders the program may write, as the host sent them
	Mask     []string // directories to hide behind an empty tmpfs: the daemon's run and state
	Rebind   []Bind   // paths under a mask that are bound back
	Strict   bool     // refuse writable paths through symlinks for a kernel-isolated attempt
}

// Skip is a writable folder left read-only, and why. The program still starts: one folder that
// cannot be bound is a folder left read-only, not a sandbox that refuses everything.
type Skip struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

// Plan is the command to run and what it makes writable.
type Plan struct {
	Argv       []string
	Writable   []string
	Skipped    []Skip
	ExtraFiles []*os.File // owned mount descriptors; close after spawn, including failed spawn
}

// Wrap returns the bubblewrap command for o.
//
// The order of the mounts is the design: the read-only root and the private /tmp first; the
// writable folders over them; the masks after the writable folders, so a writable folder that
// holds the daemon's directory (a home, say) cannot expose it again; the daemon's own paths a
// launch needs after the masks; the working directory last.
func Wrap(o Options) (Plan, error) {
	if o.Bwrap == "" || len(o.Argv) == 0 {
		return Plan{}, errors.New("sandbox: no bubblewrap or no program")
	}
	if len(o.Writable) > MaxWritable {
		return Plan{}, fmt.Errorf("sandbox: at most %d writable folders", MaxWritable)
	}
	if !filepath.IsAbs(o.Cwd) {
		return Plan{}, errors.New("sandbox: the working directory must be absolute")
	}
	cwd := filepath.Clean(o.Cwd)
	masks := make([]string, 0, len(o.Mask))
	for _, m := range o.Mask {
		if m == "" {
			continue
		}
		m = filepath.Clean(m)
		masks = append(masks, m)
		if within(cwd, m) {
			return Plan{}, ErrHiddenCwd
		}
	}

	argv := []string{o.Bwrap,
		"--ro-bind", "/", "/",
		"--dev", "/dev",
		// The outer devpts over bubblewrap's own: the program's terminal is one of the outer PTYs,
		// and ttyname() — which ssh, sudo and tty call — finds it only where it really is.
		"--dev-bind", "/dev/pts", "/dev/pts",
		"--proc", "/proc",
		"--tmpfs", "/tmp",
		"--unshare-pid",
		"--die-with-parent",
	}
	plan := Plan{}
	seen := map[string]bool{}
	for _, raw := range o.Writable {
		path, reason := checkWritable(raw, masks, o.Strict)
		if reason != "" {
			plan.Skipped = append(plan.Skipped, Skip{Path: raw, Reason: reason})
			continue
		}
		if seen[path] {
			continue
		}
		seen[path] = true
		plan.Writable = append(plan.Writable, path)
		argv = append(argv, "--bind", path, path)
	}
	// A working directory in /tmp that no writable folder covers would be gone behind the private
	// /tmp; it is shown again, read-only, like the rest of the filesystem.
	if within(cwd, "/tmp") && !coveredBy(cwd, plan.Writable) {
		if st, err := os.Stat(cwd); err == nil && st.IsDir() {
			argv = append(argv, "--ro-bind", cwd, cwd)
		}
	}
	for _, m := range masks {
		argv = append(argv, "--tmpfs", m)
	}
	for _, b := range o.Rebind {
		if b.Path == "" {
			continue
		}
		if _, err := os.Stat(b.Path); err != nil {
			continue
		}
		if o.Strict && b.Writable {
			if _, reason := checkWritable(b.Path, nil, true); reason != "" {
				return Plan{}, fmt.Errorf("sandbox: strict writable rebind: %s", reason)
			}
		}
		flag := "--ro-bind"
		if b.Writable {
			flag = "--bind"
		}
		argv = append(argv, flag, b.Path, b.Path)
	}
	argv = append(argv, "--chdir", cwd, "--")
	plan.Argv = append(argv, o.Argv...)
	return plan, nil
}

// checkWritable returns the folder to bind, or why it is left read-only.
func checkWritable(raw string, masks []string, strict bool) (string, string) {
	if !filepath.IsAbs(raw) {
		return "", "not an absolute path"
	}
	path := filepath.Clean(raw)
	if path == "/" {
		return "", "the whole filesystem"
	}
	for _, sys := range []string{"/proc", "/dev", "/sys"} {
		if within(path, sys) {
			return "", "a system directory"
		}
	}
	for _, m := range masks {
		if within(path, m) {
			return "", "inside the terminal daemon's own directories"
		}
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", "missing"
	}
	for _, sys := range []string{"/proc", "/dev", "/sys"} {
		if within(resolved, sys) {
			return "", "a system directory"
		}
	}
	for _, m := range masks {
		if within(resolved, m) {
			return "", "inside the terminal daemon's own directories"
		}
	}
	// A strict cgroup boundary cannot rely on a path that can be redirected between this check
	// and bubblewrap's bind mount. A parent symlink is as dangerous as a final symlink.
	if strict && resolved != path {
		return "", "a symbolic link"
	}
	// A bind needs a real directory at both ends: a symlink or a file makes bubblewrap refuse the
	// whole command. A symlink is also how a folder would be pointed somewhere it was not approved.
	st, err := os.Lstat(path)
	switch {
	case err != nil:
		return "", "missing"
	case st.Mode()&os.ModeSymlink != 0:
		return "", "a symbolic link"
	case !st.IsDir():
		return "", "not a directory"
	}
	return path, ""
}

// within reports whether path is dir or lies under it; both are clean and absolute.
func within(path, dir string) bool {
	if dir == "/" {
		return true
	}
	return path == dir || strings.HasPrefix(path, dir+"/")
}

func coveredBy(path string, dirs []string) bool {
	for _, d := range dirs {
		if within(path, d) {
			return true
		}
	}
	return false
}
