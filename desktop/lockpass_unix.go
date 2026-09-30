//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"syscall"
)

const finishFDEnv = "DAEDALUS_FINISH_LOCK_FD"

// passLock hands a held lock to a child as an inherited descriptor. flock belongs to the open file,
// and the child's descriptor is the same open file: both processes hold the lock until both have
// let go of it.
func passLock(cmd *exec.Cmd, lock *InstallLock) error {
	if lock.file == nil {
		return errors.New("the finish lock is not held")
	}
	cmd.ExtraFiles = append(cmd.ExtraFiles, lock.file)
	cmd.Env = append(cmd.Env, finishFDEnv+"="+strconv.Itoa(2+len(cmd.ExtraFiles)))
	return nil
}

// inheritedLock is the finish lock a parent handed this process, if one was. It is checked to be
// the finish lock file itself, and its holder is read from it.
func inheritedLock(p Paths) (*InstallLock, bool, error) {
	value := os.Getenv(finishFDEnv)
	if value == "" {
		return nil, false, nil
	}
	os.Unsetenv(finishFDEnv) // not for this process's own children
	fd, err := strconv.Atoi(value)
	if err != nil || fd < 3 {
		return nil, true, errors.New("the lock handed to --finish is not a descriptor")
	}
	file := os.NewFile(uintptr(fd), finishLockPath(p))
	info, err := file.Stat()
	expected, err2 := os.Stat(finishLockPath(p))
	if err != nil || err2 != nil || !os.SameFile(info, expected) {
		file.Close()
		return nil, true, errors.New("the lock handed to --finish is not this installation's finish lock")
	}
	holder, ok := readHolderAt(finishLockPath(p))
	if !ok {
		file.Close()
		return nil, true, errors.New("the finish lock names no holder")
	}
	// This process keeps the lock; what it starts — git, uv, the whole stack — must not. Without
	// close-on-exec every child would hold the same open lock, and a stack left running after both
	// launchers died would keep the installation locked with nobody to release it.
	syscall.CloseOnExec(fd)
	return &InstallLock{file: file, holder: holder, path: finishLockPath(p), inherited: true}, true, nil
}
