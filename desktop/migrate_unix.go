//go:build !windows

package main

import (
	"errors"
	"os"
	"syscall"
)

func crossDevice(err error) bool { return errors.Is(err, syscall.EXDEV) }

// syncDir makes the names in a folder durable: a rename or a new file is on the disk only once its
// folder has been synced as well.
func syncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
