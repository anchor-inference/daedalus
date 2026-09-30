//go:build windows

package main

import (
	"errors"
	"os"
	"syscall"
)

// lockFile opens the lock file with no sharing at all: while the handle is open, every other open —
// from this process or another — fails with a sharing violation. The handle closes with the
// process, whatever way it ends. Not yet run on a real Windows machine.
func lockFile(path string) (*os.File, error) {
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	handle, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		if errors.Is(err, errorSharingViolation) || errors.Is(err, syscall.ERROR_ACCESS_DENIED) {
			return nil, errLocked
		}
		return nil, err
	}
	return os.NewFile(uintptr(handle), path), nil
}

// errorSharingViolation is ERROR_SHARING_VIOLATION, which the syscall package does not name.
const errorSharingViolation = syscall.Errno(32)

func unlockFile(file *os.File) {
	_ = file.Close()
}
