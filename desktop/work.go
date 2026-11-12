package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// The work done in an installation is kept apart from the installation itself whenever an update
// protects the data: the verified backup does not archive it (backup.go), and the writer fence does
// not copy it (setWorkAside below). It is the user's working files and what is fetched or rebuilt by
// itself — never changed by an update, so a rollback has nothing to put back there — and it is most
// of the data folder: one session with a hundred thousand files in a dependency tree made every
// update spend twenty minutes copying, hashing and reading back files no step of the update touches.
//
//   - workspaces/ holds every session's working folder, and with it every project folder the app
//     made there; a project attached from elsewhere on the machine is not in the data folder at all,
//     and a link to one is never followed;
//   - worktrees/ is self-development's worktrees and the agent's own copies of the repositories,
//     fetched from their origin;
//   - state/checkpoints/ is a git store per workspace whose objects are the workspace's files over
//     again — the undo of a turn, derived from the workspaces and as large as they are;
//   - state/models/ is the speech models, downloaded again when missing;
//   - state/tmp/ and state/upload-staging/ hold files only while something is in flight;
//   - node_modules/ anywhere in the checkouts: the app is built with `npm ci`, which deletes the
//     folder and installs it afresh from the lock every time (launcher/supervisor.py), so a copy of
//     it is never what anything runs on.
//
// What a rollback restores and what these were left at stay consistent: the older database knows
// fewer sessions and checkpoints than the folders hold, which is what deleting a session already
// leaves behind until retention runs.
var workPaths = []string{"workspaces", "worktrees", "state/checkpoints", "state/models", "state/tmp", "state/upload-staging"}

// workCheckouts are the checkouts in the data folder whose node_modules folders are work.
var workCheckouts = []string{"daedalus", "protocore-exp"}

// isWork says whether rel — slash-separated, relative to the data folder — is work or inside it.
func isWork(rel string) bool {
	for _, work := range workPaths {
		if rel == work || strings.HasPrefix(rel, work+"/") {
			return true
		}
	}
	top, rest, ok := strings.Cut(rel, "/")
	if !ok {
		return false
	}
	for _, checkout := range workCheckouts {
		if top != checkout {
			continue
		}
		for _, segment := range strings.Split(rest, "/") {
			if segment == "node_modules" {
				return true
			}
		}
	}
	return false
}

// findWork lists the work under root as slash-separated paths, the outermost of each: a
// node_modules inside another is part of it. No link is followed, so a checkout or a folder that is
// a link — to a project elsewhere — is never walked into.
func findWork(root string) ([]string, error) {
	var found []string
	for _, work := range workPaths {
		if _, err := os.Lstat(filepath.Join(root, filepath.FromSlash(work))); err == nil {
			found = append(found, work)
		} else if !errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
	}
	for _, checkout := range workCheckouts {
		base := filepath.Join(root, checkout)
		if _, err := os.Lstat(base); err != nil {
			continue
		}
		err := filepath.WalkDir(base, func(name string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if !entry.IsDir() || name == base {
				return nil
			}
			switch entry.Name() {
			case ".git":
				return filepath.SkipDir
			case "node_modules":
				relative, err := filepath.Rel(root, name)
				if err != nil {
					return err
				}
				found = append(found, filepath.ToSlash(relative))
				return filepath.SkipDir
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return found, nil
}

// workAsideDir is where the work waits while the writer fence switches the data folder: inside the
// control folder, which is beside the data folder on the same filesystem — so a move there is one
// rename whatever the size — and outside it, so the fence neither leases, watches nor copies it.
func workAsideDir(p Paths) string { return filepath.Join(fenceControlPath(p.Data), "set-aside") }

// workIsAside says whether work is waiting in the set-aside folder: a switch is running, or one
// stopped before it was put back.
func workIsAside(p Paths) bool {
	found, err := findWork(workAsideDir(p))
	return err != nil || len(found) > 0
}

// setWorkAside moves the work out of the data folder into workAsideDir, by rename, before a fenced
// switch. Anything an earlier switch left there goes back first. A failure part way leaves some of it
// moved; returnWork puts that back, and the caller calls it.
func setWorkAside(p Paths) ([]string, error) {
	notes, err := returnWork(p)
	if err != nil {
		return notes, err
	}
	found, err := findWork(p.Data)
	if err != nil {
		return notes, err
	}
	aside := workAsideDir(p)
	for _, rel := range found {
		to := filepath.Join(aside, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(to), 0o700); err != nil {
			return notes, err
		}
		if err := os.Rename(filepath.Join(p.Data, filepath.FromSlash(rel)), to); err != nil {
			return notes, fmt.Errorf("could not set %s aside: %w", rel, err)
		}
	}
	return notes, nil
}

// returnWork puts what setWorkAside moved back into the data folder — into whichever tree is live
// there by now — and says anything the operator should know. Something already at a destination is
// not overwritten: an empty folder is removed, and anything else is moved to displaced-<time> in the
// control folder and named in the notes. It happens when a copy kept by a launcher that still copied
// the work is put back by a rollback; the work that was set aside is the newer of the two.
func returnWork(p Paths) ([]string, error) {
	aside := workAsideDir(p)
	if _, err := os.Lstat(aside); errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	found, err := findWork(aside)
	if err != nil {
		return nil, err
	}
	var notes []string
	displaced := filepath.Join(fenceControlPath(p.Data), "displaced-"+time.Now().UTC().Format("20060102T150405Z"))
	for _, rel := range found {
		from, to := filepath.Join(aside, filepath.FromSlash(rel)), filepath.Join(p.Data, filepath.FromSlash(rel))
		// Never through a link: a parent that became one would carry the work out of the folder.
		if err := ensureParents(p.Data, filepath.Dir(to)); err != nil {
			return notes, fmt.Errorf("could not put %s back (it is in %s): %w", rel, aside, err)
		}
		if info, err := os.Lstat(to); err == nil {
			// Only an empty folder or a link goes: os.Remove refuses a folder with anything in it,
			// and a file is never handed to it.
			if !(info.IsDir() || info.Mode()&os.ModeSymlink != 0) || os.Remove(to) != nil {
				keep := filepath.Join(displaced, filepath.FromSlash(rel))
				if err := os.MkdirAll(filepath.Dir(keep), 0o700); err != nil {
					return notes, err
				}
				if err := os.Rename(to, keep); err != nil {
					return notes, fmt.Errorf("could not put %s back: something is in its place and cannot be moved (it is in %s): %w", rel, aside, err)
				}
				notes = append(notes, fmt.Sprintf("an older %s was in the way of the one set aside during the switch; it is kept at %s", rel, keep))
			}
		}
		if err := os.Rename(from, to); err != nil {
			return notes, fmt.Errorf("could not put %s back (it is in %s): %w", rel, aside, err)
		}
	}
	removeEmptyFolders(aside)
	return notes, nil
}

// removeEmptyFolders removes root and the folders under it that are empty, deepest first. os.Remove
// refuses a folder with anything in it, so nothing that holds a file is ever touched.
func removeEmptyFolders(root string) {
	var dirs []string
	_ = filepath.WalkDir(root, func(name string, entry fs.DirEntry, err error) error {
		if err == nil && entry.IsDir() {
			dirs = append(dirs, name)
		}
		return nil
	})
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Remove(dirs[i])
	}
}
