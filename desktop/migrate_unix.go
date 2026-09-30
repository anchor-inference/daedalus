//go:build !windows

package main

import (
	"errors"
	"syscall"
)

func crossDevice(err error) bool { return errors.Is(err, syscall.EXDEV) }
