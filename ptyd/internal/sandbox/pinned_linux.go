//go:build linux

package sandbox

import (
	"errors"
	"fmt"
	"os"
	"runtime"

	"golang.org/x/sys/unix"
)

func pinReadSources(root *os.File, sources []ReadSource) ([]*os.File, error) {
	defer runtime.KeepAlive(root)
	owned := Plan{}
	fail := func(err error) ([]*os.File, error) {
		return nil, errors.Join(err, owned.Close())
	}
	for _, source := range sources {
		fd, err := unix.Openat2(int(root.Fd()), source.Relative, &unix.OpenHow{
			Flags:   unix.O_PATH | unix.O_CLOEXEC,
			Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS | unix.RESOLVE_NO_XDEV,
		})
		if err != nil {
			return fail(fmt.Errorf("sandbox: opening pinned source %q: %w", source.Relative, err))
		}
		file := os.NewFile(uintptr(fd), source.Relative)
		owned.ExtraFiles = append(owned.ExtraFiles, file)
		st, err := file.Stat()
		if err != nil {
			return fail(err)
		}
		if !st.Mode().IsRegular() && !st.IsDir() {
			return fail(fmt.Errorf("sandbox: pinned source %q is not a regular file or directory", source.Relative))
		}
	}
	return owned.ExtraFiles, nil
}
