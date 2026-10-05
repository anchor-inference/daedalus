//go:build linux

package sandbox

import (
	"errors"
	"path/filepath"
	"strconv"

	"golang.org/x/sys/unix"
)

// RunScopedExec replaces this process with bubblewrap after closing every descriptor that the
// scoped plan did not explicitly pass. It must run before any untrusted sandbox program starts.
func RunScopedExec(args []string) error {
	if len(args) < 3 {
		return errors.New("sandbox: incomplete scoped executor")
	}
	count, err := strconv.Atoi(args[0])
	if err != nil || count < 1 || count > MaxWritable+2 || !filepath.IsAbs(args[1]) {
		return errors.New("sandbox: invalid scoped executor inputs")
	}
	if err := unix.CloseRange(uint(3+count), ^uint(0), 0); err != nil {
		return err
	}
	// Bubblewrap receives no host environment. Its --clearenv also removes anything it might
	// generate before the selected CLI runs.
	return unix.Exec(args[1], args[1:], []string{})
}
