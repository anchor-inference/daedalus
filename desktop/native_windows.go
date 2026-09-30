//go:build windows

package main

// Windows has no process groups in the POSIX sense and no signals to send them. A child started in
// its own process group can be told to end with a console control event, and `taskkill /T` is what
// walks the tree the supervisor and the bot make — the nearest thing the platform has to killing a
// session.

import (
	"os"
	"os/exec"
	"strconv"
	"syscall"
)

func setProcessGroup(cmd *exec.Cmd) {
	// Each daemon gets a separate, hidden console. A GUI launcher has no console of its
	// own, so a direct GenerateConsoleCtrlEvent from it cannot reach a child. The
	// short-lived helper below attaches to this console before sending CTRL_BREAK.
	const createNewConsole = 0x00000010
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNewConsole, HideWindow: true}
}

// terminateGroup asks the isolated console to end politely with CTRL_BREAK. `taskkill` without /F was what this used to send, and it posts
// WM_CLOSE to top-level windows: a console-less Python has none, so nothing arrived, the forty
// second wait always expired and the bot was hard-killed with no chance to drain a run. A break
// event is what the supervisor's own signal handler is waiting for.
func terminateGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := sendConsoleBreak(cmd.Process.Pid); err != nil {
		_ = exec.Command("taskkill", "/T", "/PID", strconv.Itoa(cmd.Process.Pid)).Run()
	}
}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if err := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid)).Run(); err != nil {
		_ = cmd.Process.Kill()
	}
}

var (
	kernel32                    = syscall.NewLazyDLL("kernel32.dll")
	procGenerateConsoleCtrlEven = kernel32.NewProc("GenerateConsoleCtrlEvent")
	procAttachConsole           = kernel32.NewProc("AttachConsole")
	procFreeConsole             = kernel32.NewProc("FreeConsole")
)

func generateConsoleCtrlEvent(event, groupID uint32) error {
	if r, _, err := procGenerateConsoleCtrlEven.Call(uintptr(event), uintptr(groupID)); r == 0 {
		return err
	}
	return nil
}

// A launcher has no guarantee of sharing its child's console. Run this in a fresh
// copy of the executable so attaching cannot disturb the launcher's own console or
// its concurrent goroutines. Each child has an isolated console, so group 0 addresses
// only that child's tree (including the supervisor's bot), never the operator's shell.
func sendConsoleBreak(pid int) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return exec.Command(exe, "__daemon_console_break", strconv.Itoa(pid)).Run()
}

func init() {
	if len(os.Args) != 3 || os.Args[1] != "__daemon_console_break" {
		return
	}
	pid, err := strconv.Atoi(os.Args[2])
	if err != nil || pid <= 1 {
		os.Exit(2)
	}
	_, _, _ = procFreeConsole.Call()
	if r, _, _ := procAttachConsole.Call(uintptr(pid)); r == 0 {
		os.Exit(1)
	}
	if err := generateConsoleCtrlEvent(syscall.CTRL_BREAK_EVENT, 0); err != nil {
		os.Exit(1)
	}
	os.Exit(0)
}

// An orphan record from an older launcher does not prove it owns an isolated console. Sending
// group 0 there could interrupt the operator's shell. Use the safe helper only with a new record.
func terminatePID(pid int, isolated bool) {
	if isolated && sendConsoleBreak(pid) == nil {
		return
	}
	_ = exec.Command("taskkill", "/T", "/PID", strconv.Itoa(pid)).Run()
}

func killPID(pid int) {
	_ = exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(pid)).Run()
}
