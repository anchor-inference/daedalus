//go:build unix && !linux

package sidechan

import "syscall"

// parentDeathPerThread is false: only Linux has a parent-death signal.
const parentDeathPerThread = false

// parentDeath does nothing: macOS and the BSDs have no parent-death signal, and the tether alone
// ends a tied program's group when its runner dies.
func parentDeath(*syscall.SysProcAttr) {}

// placeUnderCap does nothing: only Linux has a group to start a program in.
func placeUnderCap(*syscall.SysProcAttr) {}
