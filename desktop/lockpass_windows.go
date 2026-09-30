//go:build windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"syscall"
)

const finishHandleEnv = "DAEDALUS_FINISH_LOCK_HANDLE"

// passLock hands the lock's handle to the child: an inheritable copy of the same open file, which
// keeps it opened without sharing for as long as either process lives. Not yet run on Windows.
func passLock(cmd *exec.Cmd, lock *InstallLock) error {
	if lock.file == nil {
		return errors.New("the finish lock is not held")
	}
	handle := syscall.Handle(lock.file.Fd())
	if err := syscall.SetHandleInformation(handle, syscall.HANDLE_FLAG_INHERIT, syscall.HANDLE_FLAG_INHERIT); err != nil {
		return err
	}
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.AdditionalInheritedHandles = append(cmd.SysProcAttr.AdditionalInheritedHandles, handle)
	cmd.Env = append(cmd.Env, finishHandleEnv+"="+strconv.FormatUint(uint64(handle), 10))
	return nil
}

func inheritedLock(p Paths) (*InstallLock, bool, error) {
	value := os.Getenv(finishHandleEnv)
	if value == "" {
		return nil, false, nil
	}
	os.Unsetenv(finishHandleEnv)
	handle, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		return nil, true, errors.New("the lock handed to --finish is not a handle")
	}
	file := os.NewFile(uintptr(handle), finishLockPath(p))
	holder, ok := readHolderAt(finishLockPath(p))
	if !ok {
		file.Close()
		return nil, true, errors.New("the finish lock names no holder")
	}
	return &InstallLock{file: file, holder: holder, path: finishLockPath(p), inherited: true}, true, nil
}
