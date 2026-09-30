//go:build windows

package main

import (
	"errors"
	"syscall"
)

// ERROR_NOT_SAME_DEVICE: a rename between volumes.
func crossDevice(err error) bool { return errors.Is(err, syscall.Errno(17)) }
