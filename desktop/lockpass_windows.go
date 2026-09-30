//go:build windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
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
	if !handleIsFile(windows.Handle(handle), finishLockPath(p)) {
		file.Close()
		return nil, true, errors.New("the lock handed to --finish is not this installation's finish lock")
	}
	holder, ok := readHolderAt(finishLockPath(p))
	if !ok {
		file.Close()
		return nil, true, errors.New("the finish lock names no holder")
	}
	return &InstallLock{file: file, holder: holder, path: finishLockPath(p), inherited: true}, true, nil
}

// handleIsFile reports whether an inherited handle is the file at path. Asked of the handle and not
// of the path: the lock file is open without sharing, so opening the path to compare identities, as
// os.SameFile would, fails on the very file it is meant to find. Without this check any inheritable
// handle a parent passed — another installation's lock, a log file — was taken for the finish lock.
func handleIsFile(handle windows.Handle, path string) bool {
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n, err := windows.GetFinalPathNameByHandle(handle, &buf[0], uint32(len(buf)), 0)
	if err != nil || n == 0 || int(n) > len(buf) {
		return false
	}
	final := strings.TrimPrefix(windows.UTF16ToString(buf[:n]), `\\?\`)
	want := path
	if dir, err := filepath.EvalSymlinks(filepath.Dir(path)); err == nil {
		want = filepath.Join(dir, filepath.Base(path))
	}
	return strings.EqualFold(filepath.Clean(final), filepath.Clean(want))
}
