//go:build windows

package main

import (
	"errors"
	"syscall"
)

// ERROR_NOT_SAME_DEVICE: a rename between volumes.
func crossDevice(err error) bool { return errors.Is(err, syscall.Errno(17)) }

// syncDir: Windows keeps a folder's names with the files' own metadata, and a folder cannot be
// opened for FlushFileBuffers without backup privileges; the files are synced one by one instead.
func syncDir(string) error { return nil }
