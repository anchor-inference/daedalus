//go:build unix

package sidechan

import (
	"fmt"
	"os"
	"syscall"
)

// openNoFollow opens a file for reading without following a symlink in its last component, and
// without blocking on a FIFO (whose open would otherwise wait for a writer that never comes).
func openNoFollow(p string, dir bool) (*os.File, error) {
	flags := syscall.O_RDONLY | syscall.O_NOFOLLOW | syscall.O_NONBLOCK | syscall.O_CLOEXEC
	if dir {
		flags |= syscall.O_DIRECTORY
	}
	fd, err := syscall.Open(p, flags, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	// Reads of a regular file never block; the flag only mattered for the open.
	_ = syscall.SetNonblock(fd, false)
	return os.NewFile(uintptr(fd), p), nil
}

// openForWrite opens a file of an inbox for writing without following a symlink in its last
// component: create makes it and refuses one that exists; otherwise it must exist. O_NONBLOCK makes
// the open of a FIFO with no reader fail rather than wait.
func openForWrite(p string, create bool) (*os.File, error) {
	flags := syscall.O_WRONLY | syscall.O_NOFOLLOW | syscall.O_NONBLOCK | syscall.O_CLOEXEC
	if create {
		flags |= syscall.O_CREAT | syscall.O_EXCL
	}
	fd, err := syscall.Open(p, flags, 0o666)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	_ = syscall.SetNonblock(fd, false)
	return os.NewFile(uintptr(fd), p), nil
}

// fileID tells one file from another at the same path: its device and inode.
func fileID(st os.FileInfo) string {
	if s, ok := st.Sys().(*syscall.Stat_t); ok {
		return fmt.Sprintf("%d:%d", s.Dev, s.Ino)
	}
	return ""
}

// writable reports whether this user may write to p, as the kernel decides it (access(2) with
// W_OK): the owner and mode alone would be wrong for a read-only mount or an ACL.
func writable(p string) *bool {
	ok := syscall.Access(p, 2) == nil
	return &ok
}
