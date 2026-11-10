//go:build !windows

package main

import (
	"fmt"
	"os"
)

// privateDirProblem says how the folder is open to anyone but this user, or "" when it is not.
func privateDirProblem(dir string) string {
	info, err := os.Stat(dir)
	if err != nil {
		return err.Error()
	}
	if info.Mode().Perm() != 0o700 {
		return fmt.Sprintf("mode %v", info.Mode().Perm())
	}
	return ""
}
