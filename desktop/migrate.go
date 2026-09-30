package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// migrateLegacyRuntime moves an installation from before the runtime left the data folder. It runs
// once, from the launcher, under the installation lock and before anything is started.
//
// Almost nothing in <data>/runtime is worth moving. The interpreter and the environment hold
// absolute paths to where they were made, so a moved venv is a broken venv; they are rebuilt, from
// uv's cache where it survives. What is not rebuilt is carried over by copying — the logs, the
// terminal logs and the agent-write journal (ptyd/state), the browser profiles with their logins
// (browserd/state) — and only then does the old folder leave the data folder: moved aside next to
// the new runtime when that is the same filesystem (the operator may delete it), removed when it is
// not, because a move across filesystems is a copy of gigabytes of things that are rebuilt anyway.
// Children still running out of the old folder are stopped first, from the records it keeps.
func migrateLegacyRuntime(ctx context.Context, p Paths, log func(string, ...any)) error {
	info, err := os.Lstat(p.LegacyRuntime)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("%s is not a folder; move it out of the data folder by hand", p.LegacyRuntime)
	}
	legacy := p
	legacy.Runtime, legacy.Local = p.LegacyRuntime, p.LegacyRuntime
	if err := StopOrphans(ctx, legacy, log); err != nil {
		return err
	}
	// A launcher from before child records (v0.12) leaves nothing to stop by; the app's port is
	// then the only sign that a stack still runs out of this folder, and moving a runtime out from
	// under running processes is not done.
	if port := APIPort(p); portAnswers(port) {
		return fmt.Errorf("something answers on the app's port %s: a stack may still be running out of %s; close its launcher first", port, p.LegacyRuntime)
	}
	stamp := time.Now().UTC().Format("20060102T150405Z")
	record := legacyMove{From: p.LegacyRuntime, At: time.Now().UTC()}
	carry := []struct{ from, to string }{
		{filepath.Join(p.LegacyRuntime, "logs"), filepath.Join(p.RuntimeLogs, "before-the-move-"+stamp)},
		{filepath.Join(p.LegacyRuntime, "ptyd", "state"), ptydStateDir(p)},
		{filepath.Join(p.LegacyRuntime, "browserd", "state"), browserdStateDir(p)},
	}
	if err := os.MkdirAll(p.Local, 0o700); err != nil {
		return err
	}
	for _, c := range carry {
		if !exists(c.from) {
			continue
		}
		to := c.to
		if exists(to) {
			// Something is already there (a start with the new layout came first): keep both.
			to += "-before-the-move-" + stamp
		}
		if err := copyTree(c.from, to); err != nil {
			return fmt.Errorf("copying %s to %s: %w; the data folder is unchanged", c.from, to, err)
		}
		record.Carried = append(record.Carried, to)
		log("carried %s over to %s", c.from, to)
	}
	aside := filepath.Join(filepath.Dir(p.Runtime), "legacy-"+filepath.Base(p.Runtime)+"-"+stamp)
	if err := os.MkdirAll(filepath.Dir(aside), 0o700); err != nil {
		return err
	}
	err = renameDir(p.LegacyRuntime, aside)
	switch {
	case err == nil:
		record.Aside = aside
		log("the old runtime folder is now %s; it is no longer used and may be deleted", aside)
	case crossDevice(err):
		if err := os.RemoveAll(p.LegacyRuntime); err != nil {
			return fmt.Errorf("removing the old runtime folder: %w", err)
		}
		record.Removed = true
		log("the old runtime folder was on another filesystem and has been removed; it is rebuilt outside the data folder")
	default:
		return fmt.Errorf("moving the old runtime folder out: %w", err)
	}
	body, _ := json.MarshalIndent(record, "", "  ")
	_ = writeFileSync(filepath.Join(p.Local, "moved-out-of-data.json"), body, 0o600)
	return nil
}

type legacyMove struct {
	From    string    `json:"from"`
	Carried []string  `json:"carried,omitempty"`
	Aside   string    `json:"aside,omitempty"`
	Removed bool      `json:"removed,omitempty"`
	At      time.Time `json:"at"`
}

// renameDir is os.Rename; a test replaces it to take the other-filesystem branch.
var renameDir = os.Rename

// copyTree copies regular files, directories and symlinks, and checks every file's size after the
// copy. Sockets and pipes are a running daemon's and are not carried.
func copyTree(from, to string) error {
	return filepath.WalkDir(from, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(from, path)
		if err != nil {
			return err
		}
		target := filepath.Join(to, rel)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case entry.IsDir():
			return os.MkdirAll(target, 0o700)
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		case !info.Mode().IsRegular():
			return nil
		}
		return copyFile(path, target, info)
	})
}

func copyFile(from, to string, info os.FileInfo) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_EXCL, info.Mode().Perm()|0o600)
	if err != nil {
		return err
	}
	n, err := io.Copy(out, in)
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err == nil && n != info.Size() {
		err = fmt.Errorf("%s: copied %d of %d bytes", from, n, info.Size())
	}
	if err == nil {
		_ = os.Chtimes(to, info.ModTime(), info.ModTime())
	}
	return err
}
