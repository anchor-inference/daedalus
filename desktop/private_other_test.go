//go:build !windows

package main

import (
	"fmt"
	"os"
)

// privateProblem says how a folder or file is open to anyone but this user, or "" when it is not.
func privateProblem(path string) string {
	info, err := os.Stat(path)
	if err != nil {
		return err.Error()
	}
	want := os.FileMode(0o600)
	if info.IsDir() {
		want = 0o700
	}
	if info.Mode().Perm() != want {
		return fmt.Sprintf("mode %v", info.Mode().Perm())
	}
	return ""
}
