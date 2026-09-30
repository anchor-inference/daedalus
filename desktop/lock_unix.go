//go:build !windows

package main

import (
	"errors"
	"os"
	"syscall"
)

// lockFile opens the lock file and takes an exclusive flock on it without waiting. flock belongs to
// the open file, so a second open in the same process is refused just like one from another.
//
// The file is opened read-only. flock excludes a second holder whatever its open mode, and a
// descriptor open for writing would make the kernel refuse the read lease the upgrade's writer
// fence puts on every file of the data folder — the lock itself would refuse every switch for as
// long as the launcher runs. The holder is therefore recorded only in the .holder file beside it.
//
// The lock is then checked to still be the file at path. A switch of the data folder exchanges the
// tree the lock file is in: a process that opened .lock just before the exchange and took the flock
// just after holds the lock of the tree that is now kept aside, not of the live one. It opens again.
func lockFile(path string) (*os.File, error) {
	for attempt := 0; ; attempt++ {
		file, err := os.OpenFile(path, os.O_CREATE|os.O_RDONLY, 0o600)
		if err != nil {
			return nil, err
		}
		if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
			file.Close()
			if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
				return nil, errLocked
			}
			return nil, err
		}
		held, heldErr := file.Stat()
		current, currentErr := os.Stat(path)
		if heldErr == nil && currentErr == nil && os.SameFile(held, current) {
			return file, nil
		}
		unlockFile(file)
		if attempt == 2 {
			return nil, errLocked
		}
	}
}

func unlockFile(file *os.File) {
	_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	_ = file.Close()
}
