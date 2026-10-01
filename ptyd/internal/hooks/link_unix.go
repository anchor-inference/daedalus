//go:build !windows

package hooks

import "os"

// hookCommandName is the file name of the hook command in the daemon's bin directory.
const hookCommandName = "hook-post"

// linkHookCommand makes the hook command a symbolic link to the daemon.
func linkHookCommand(ptyd, command string) error { return os.Symlink(ptyd, command) }

// clearBinDir empties the bin directory a previous life of the daemon left.
func clearBinDir(dir string) error { return os.RemoveAll(dir) }
