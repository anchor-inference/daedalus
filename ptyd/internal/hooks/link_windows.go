//go:build windows

package hooks

import (
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// hookCommandName carries the extension Windows finds a program by.
const hookCommandName = "hook-post.exe"

// linkHookCommand makes the hook command the daemon under another name. A symbolic link needs a
// privilege an ordinary Windows account does not have (or Developer Mode), so it is a hard link,
// which needs none; where that fails too (the state directory on another volume) it is a copy.
//
// A hard link left by the previous start is kept when it is still this daemon's file. Windows
// refuses to delete any name of a file a running process was started from, and the daemon starting
// now runs from exactly that file: removing the link failed with "Access is denied", ptyd exited,
// and every start after the first one on a machine came up without host terminals.
func linkHookCommand(ptyd, command string) error {
	if sameFile(ptyd, command) {
		return nil
	}
	if err := os.Remove(command); err != nil && !errors.Is(err, fs.ErrNotExist) {
		// An older daemon's file that a hook still runs from: it cannot be deleted, but a running
		// program can be renamed, and the name is what is needed back. clearBinDir removes it once
		// nothing runs from it any more.
		if err := os.Rename(command, command+asideMarker+randomHex(4)); err != nil {
			return err
		}
	}
	if err := os.Link(ptyd, command); err == nil {
		return nil
	}
	src, err := os.Open(ptyd)
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(command, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o700)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		dst.Close()
		os.Remove(command)
		return err
	}
	return dst.Close()
}

// asideMarker names a hook command moved out of the way while something still ran from it.
const asideMarker = ".old-"

// clearBinDir empties the bin directory as far as Windows allows. The hook command stays where it is
// (linkHookCommand decides whether it is still the right file), and a file that cannot be removed
// because something runs from it is left for a later start rather than failing this one.
func clearBinDir(dir string) error {
	entries, err := os.ReadDir(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		name := entry.Name()
		if strings.EqualFold(name, hookCommandName) {
			continue
		}
		path := filepath.Join(dir, name)
		if err := os.RemoveAll(path); err != nil && !strings.Contains(name, asideMarker) {
			return err
		}
	}
	return nil
}

func sameFile(a, b string) bool {
	first, err := os.Stat(a)
	if err != nil {
		return false
	}
	second, err := os.Stat(b)
	if err != nil {
		return false
	}
	return os.SameFile(first, second)
}
