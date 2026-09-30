//go:build !windows

package main

import (
	"os/signal"
	"syscall"
)

// ignoreHangup keeps an upgrade going when its terminal window is closed: the old launcher keeps
// waiting for the new one, and the new one commits or rolls back, instead of either dying half-way.
// (The finish lock keeps it safe if one dies anyway; this keeps it from failing for no reason.)
func ignoreHangup() { signal.Ignore(syscall.SIGHUP) }
