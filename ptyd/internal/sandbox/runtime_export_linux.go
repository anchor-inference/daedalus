//go:build linux

package sandbox

import (
	"errors"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

const maxRuntimeFileBytes = 256 << 20

// SnapshotRuntimeFile freezes one explicitly named runtime file below an authorized export root.
// A directory mount would reveal every later addition, including a credential copied into it.
func SnapshotRuntimeFile(root *os.File, relative string) (*os.File, error) {
	if root == nil || !validCredentialRelative(relative) {
		return nil, errors.New("sandbox: runtime export needs an open root and a clean relative file name")
	}
	var rootStat unix.Stat_t
	if err := unix.Fstat(int(root.Fd()), &rootStat); err != nil || rootStat.Mode&unix.S_IFMT != unix.S_IFDIR {
		return nil, errors.New("sandbox: runtime export root must be a directory")
	}
	fd, err := unix.Openat2(int(root.Fd()), relative, &unix.OpenHow{Flags: unix.O_RDONLY | unix.O_CLOEXEC | unix.O_NOFOLLOW,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS | unix.RESOLVE_NO_XDEV})
	if err != nil {
		return nil, errors.New("sandbox: runtime export path is unavailable or crosses a link")
	}
	source := os.NewFile(uintptr(fd), "runtime export source")
	defer source.Close()
	var before unix.Stat_t
	if err := unix.Fstat(fd, &before); err != nil || before.Mode&unix.S_IFMT != unix.S_IFREG ||
		before.Size < 0 || before.Size > maxRuntimeFileBytes {
		return nil, errors.New("sandbox: runtime export must be a regular file within the size limit")
	}
	snapshotFD, err := unix.MemfdCreate("runtime-export", unix.MFD_CLOEXEC|unix.MFD_ALLOW_SEALING)
	if err != nil {
		return nil, err
	}
	snapshot := os.NewFile(uintptr(snapshotFD), "runtime export snapshot")
	closeOnError := func(err error) (*os.File, error) { snapshot.Close(); return nil, err }
	n, err := io.CopyN(snapshot, source, before.Size)
	if err != nil || n != before.Size {
		return closeOnError(errors.New("sandbox: runtime export changed while copying"))
	}
	var after unix.Stat_t
	if err := unix.Fstat(fd, &after); err != nil || !sameCredentialStat(before, after) {
		return closeOnError(errors.New("sandbox: runtime export changed while copying"))
	}
	mode := uint32(0444) | before.Mode&0111
	if err := unix.Fchmod(snapshotFD, mode); err != nil {
		return closeOnError(err)
	}
	if _, err := unix.FcntlInt(snapshot.Fd(), unix.F_ADD_SEALS,
		unix.F_SEAL_WRITE|unix.F_SEAL_GROW|unix.F_SEAL_SHRINK|unix.F_SEAL_SEAL); err != nil {
		return closeOnError(err)
	}
	if _, err := snapshot.Seek(0, io.SeekStart); err != nil {
		return closeOnError(err)
	}
	return snapshot, nil
}
