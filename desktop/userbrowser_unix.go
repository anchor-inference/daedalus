//go:build !windows

package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// systemChromeRegistry is nil away from Windows: Chrome on macOS and Linux reads its hosts from
// folders and its policies from files.
func systemChromeRegistry() chromeRegistry { return nil }

// chromeRunning reports whether a Chrome holds this user data directory. Chrome's process
// singleton is a symlink, SingletonLock, whose target is "<hostname>-<pid>"; it is left behind by
// a Chrome that crashed, so the pid is checked before the link is believed.
func chromeRunning(userData string) bool {
	target, err := os.Readlink(filepath.Join(userData, "SingletonLock"))
	if err != nil {
		return false
	}
	i := strings.LastIndexByte(target, '-')
	if i < 0 {
		return true
	}
	pid, err := strconv.Atoi(target[i+1:])
	if err != nil || pid <= 0 {
		return true
	}
	return processAlive(pid)
}
