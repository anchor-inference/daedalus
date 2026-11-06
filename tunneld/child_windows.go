//go:build windows

package main

// On Windows nothing reaches a child by process group, and a child outlives a parent that was killed.
// A job object closes that: the job's one handle lives in this process, and when this process is gone,
// however it went, the system ends everything in the job.

import (
	"os/exec"
	"unsafe"

	"golang.org/x/sys/windows"
)

var clientJob windows.Handle

func init() {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE}}
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		_ = windows.CloseHandle(job)
		return
	}
	clientJob = job
}

func clientAttributes(*exec.Cmd) {}

func afterStart(cmd *exec.Cmd) {
	if clientJob == 0 {
		return
	}
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		return
	}
	defer windows.CloseHandle(process)
	_ = windows.AssignProcessToJobObject(clientJob, process)
}

// endClient kills: a console program under no console of ours has no polite way to be told.
func endClient(cmd *exec.Cmd) { _ = cmd.Process.Kill() }
