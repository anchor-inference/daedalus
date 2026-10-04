//go:build linux

package sandbox

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// ScopedRead is an already opened runtime object. Directory contents must come from a trusted,
// credential-free export: pinning a directory cannot remove a copied secret inside it.
type ScopedRead struct {
	Source      *os.File
	Destination string
}

// ScopedOptions is a deliberately separate launch plan. No ordinary sandbox option can turn a
// whole-root read mount into credential isolation by adding masks on top of it.
type ScopedOptions struct {
	Bwrap      string
	Argv       []string
	Cwd        string
	ReadOnly   []ScopedRead
	Credential *os.File // a sealed snapshot returned by SnapshotCredentialFile
	Account    string   // clean absolute destination below the private home
}

// WrapScopedRead constructs a default-deny filesystem for one program and its descendants.
// The caller owns every input descriptor; the returned plan owns duplicates until spawn returns.
// This is a mount primitive, not launch authorization or a credential-free project proof.
func WrapScopedRead(o ScopedOptions) (Plan, error) {
	if o.Bwrap == "" || len(o.Argv) == 0 || !scopedPath(o.Argv[0]) || !scopedPath(o.Cwd) ||
		!scopedPath(o.Account) || !within(o.Account, "/home/operator") || o.Account == "/home/operator" ||
		len(o.ReadOnly) == 0 || len(o.ReadOnly) > MaxWritable || o.Credential == nil {
		return Plan{}, errors.New("sandbox: incomplete scoped launch")
	}
	seals, err := unix.FcntlInt(o.Credential.Fd(), unix.F_GET_SEALS, 0)
	if err != nil || seals&(unix.F_SEAL_WRITE|unix.F_SEAL_GROW|unix.F_SEAL_SHRINK|unix.F_SEAL_SEAL) !=
		unix.F_SEAL_WRITE|unix.F_SEAL_GROW|unix.F_SEAL_SHRINK|unix.F_SEAL_SEAL {
		return Plan{}, errors.New("sandbox: selected credential must be a sealed snapshot")
	}
	plan := Plan{}
	closeOnError := func(err error) (Plan, error) { return Plan{}, errors.Join(err, plan.Close()) }
	argv := []string{o.Bwrap, "--tmpfs", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
		"--unshare-pid", "--die-with-parent", "--clearenv", "--setenv", "HOME", "/home/operator",
		"--setenv", "XDG_CONFIG_HOME", "/home/operator/.config", "--setenv", "XDG_CACHE_HOME", "/home/operator/.cache",
		"--setenv", "XDG_DATA_HOME", "/home/operator/.local/share", "--setenv", "XDG_STATE_HOME", "/home/operator/.local/state",
		"--setenv", "XDG_RUNTIME_DIR", "/home/operator/.run", "--setenv", "PATH", "/bin:/usr/bin",
		"--dir", "/home/operator", "--dir", "/home/operator/.config", "--dir", "/home/operator/.cache",
		"--dir", "/home/operator/.local/share", "--dir", "/home/operator/.local/state", "--dir", "/home/operator/.run"}
	seen := map[string]bool{}
	visible := make([]string, 0, len(o.ReadOnly))
	for _, mount := range o.ReadOnly {
		if mount.Source == nil || !scopedPath(mount.Destination) || scopedReserved(mount.Destination) || seen[mount.Destination] {
			return closeOnError(fmt.Errorf("sandbox: invalid scoped read destination %q", mount.Destination))
		}
		for _, earlier := range visible {
			if within(mount.Destination, earlier) || within(earlier, mount.Destination) {
				return closeOnError(errors.New("sandbox: scoped read mounts must not overlap"))
			}
		}
		seen[mount.Destination] = true
		st, err := mount.Source.Stat()
		if err != nil || (!st.IsDir() && !st.Mode().IsRegular()) {
			return closeOnError(errors.New("sandbox: scoped source must be an open file or directory"))
		}
		fd, err := unix.FcntlInt(mount.Source.Fd(), unix.F_DUPFD_CLOEXEC, 3)
		if err != nil {
			return closeOnError(err)
		}
		plan.ExtraFiles = append(plan.ExtraFiles, os.NewFile(uintptr(fd), "scoped read"))
		argv = append(argv, "--ro-bind-fd", strconv.Itoa(len(plan.ExtraFiles)+2), mount.Destination)
		visible = append(visible, mount.Destination)
	}
	if !coveredBy(o.Argv[0], visible) || !coveredBy(o.Cwd, visible) || seen[o.Account] {
		return closeOnError(errors.New("sandbox: executable and cwd need explicit read mounts; account destination must be separate"))
	}
	for _, path := range visible {
		if within(o.Account, path) || within(path, o.Account) {
			return closeOnError(errors.New("sandbox: runtime mount overlaps the private account"))
		}
	}
	fd, err := unix.FcntlInt(o.Credential.Fd(), unix.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return closeOnError(err)
	}
	plan.ExtraFiles = append(plan.ExtraFiles, os.NewFile(uintptr(fd), "selected credential"))
	argv = append(argv, "--perms", "0600", "--ro-bind-data", strconv.Itoa(len(plan.ExtraFiles)+2), o.Account,
		"--chdir", o.Cwd, "--")
	plan.Argv = append(argv, o.Argv...)
	return plan, nil
}

func scopedPath(path string) bool {
	return filepath.IsAbs(path) && path != "/" && filepath.Clean(path) == path && !strings.Contains(path, "//")
}

func scopedReserved(path string) bool {
	for _, root := range []string{"/dev", "/proc", "/sys", "/tmp", "/home/operator"} {
		if within(path, root) || within(root, path) {
			return true
		}
	}
	return false
}
