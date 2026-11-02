//go:build windows

package sidechan

import (
	"os"
	"syscall"

	"golang.org/x/sys/windows"
)

// openNoFollow opens a file for reading without following a symbolic link or a junction in its last
// component: the reparse point itself is opened, and refused, as O_NOFOLLOW refuses a link on Unix.
// Windows has no FIFOs to block on.
func openNoFollow(p string, dir bool) (*os.File, error) {
	name, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	flags := uint32(windows.FILE_FLAG_OPEN_REPARSE_POINT)
	if dir {
		// A directory is opened only with backup semantics.
		flags |= windows.FILE_FLAG_BACKUP_SEMANTICS
	}
	h, err := windows.CreateFile(name, windows.GENERIC_READ,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil,
		windows.OPEN_EXISTING, flags, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(h, &info); err != nil {
		windows.CloseHandle(h)
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	if info.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		windows.CloseHandle(h)
		return nil, &os.PathError{Op: "open", Path: p, Err: syscall.ELOOP}
	}
	return os.NewFile(uintptr(h), p), nil
}

// openForWrite opens a file of an inbox for writing without following a link or a junction in its
// last component: create makes it and refuses one that exists; otherwise it must exist.
func openForWrite(p string, create bool) (*os.File, error) {
	name, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	disposition := uint32(windows.OPEN_EXISTING)
	if create {
		disposition = windows.CREATE_NEW
	}
	h, err := windows.CreateFile(name, windows.GENERIC_WRITE, windows.FILE_SHARE_READ, nil, disposition, windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(h, &info); err != nil {
		windows.CloseHandle(h)
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	if info.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		windows.CloseHandle(h)
		return nil, &os.PathError{Op: "open", Path: p, Err: syscall.ELOOP}
	}
	return os.NewFile(uintptr(h), p), nil
}

// volumeNameDOS asks GetFinalPathNameByHandle for a drive-letter path (VOLUME_NAME_DOS, with
// FILE_NAME_NORMALIZED; both are zero).
const volumeNameDOS = 0

// openedPath is the path Windows has for an open file, without the \\?\ prefix it reports it with.
func openedPath(f *os.File) (string, bool) {
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n, err := windows.GetFinalPathNameByHandle(windows.Handle(f.Fd()), &buf[0], uint32(len(buf)), volumeNameDOS)
	if err != nil || n == 0 || int(n) >= len(buf) {
		return "", false
	}
	return trimFinalPath(windows.UTF16ToString(buf[:n])), true
}

// fileID is empty on Windows: the file index is in the handle's information, not in what os.Stat
// returns, and a tail tells a replaced file by its size shrinking instead.
func fileID(st os.FileInfo) string { return "" }

// writable is unknown here, and left out of the answer rather than guessed: an access list, not a
// mode, decides it.
func writable(p string) *bool { return nil }
