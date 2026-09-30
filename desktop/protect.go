package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// An update or an upgrade changes the data folder — the checkouts move and the app's migrations
// run — and must be able to put it back. There are two ways to hold what it may need to put back:
//
//   - the writer fence (fence*.go): the data folder is copied and the copy swapped in with one
//     atomic exchange while the kernel keeps every writer out; the tree from before is kept whole
//     and goes back by the same exchange. Only where it has been proven: Linux, ext4, file leases
//     on, not in a container, not as root;
//   - the verified backup (backup.go): the folder is archived and read back against its manifest
//     before anything changes, and restored from the archive on failure. Everywhere else — Windows,
//     macOS, other filesystems. It cannot exclude a write that lands after the archive is read;
//     the stack is stopped and the data writers are checked for first, and that is all it has.
//
// DAEDALUS_DATA_FENCE chooses: auto (the default) takes the fence wherever it is available, off
// takes the backup everywhere. A fence that is available and then refuses — a writer, a file of
// another user, a special file — is final; the backup is never the fallback for a refusal, because
// what the fence refused is exactly what the backup would silently include. A refusal that will not
// go away by itself says so, and names DAEDALUS_DATA_FENCE=off as the way out.

type protection string

const (
	protectFence  protection = "fence"
	protectBackup protection = "backup"
)

// chooseProtection says which way this update protects the data, and why.
func chooseProtection(p Paths, mode Mode) (protection, string, error) {
	switch strings.TrimSpace(strings.ToLower(os.Getenv("DAEDALUS_DATA_FENCE"))) {
	case "", "auto":
	case "off":
		return protectBackup, "DAEDALUS_DATA_FENCE is off", nil
	default:
		return "", "", fmt.Errorf("DAEDALUS_DATA_FENCE must be auto or off, not %q; nothing was changed", os.Getenv("DAEDALUS_DATA_FENCE"))
	}
	if mode != ModeNative {
		return protectBackup, "the agent runs in Docker", nil
	}
	if err := fencePlatform(p.Data); err != nil {
		return protectBackup, err.Error(), nil
	}
	return protectFence, "the writer fence is available here", nil
}

// fenceOutcomeError is what an update says when its switch did not commit. What it says about the
// data comes from what the switch left at the data folder, not from the outcome alone: a switch a
// third party interfered with after the exchange may have left the copy live.
func fenceOutcomeError(rep fenceReport) error {
	where := rep.ReportPath
	if where == "" {
		where = "standard error"
	}
	var names []string
	for _, entry := range rep.Entries {
		if entry.Path != "" {
			names = append(names, entry.Path+" ("+entry.Cause+")")
		}
		if len(names) == 3 {
			break
		}
	}
	detail := ""
	if len(names) > 0 {
		detail = ": " + strings.Join(names, "; ")
	}
	if rep.Live != "" && rep.Live != "P" {
		return fmt.Errorf("the switch of the data folder did not finish, and the data folder now holds %s — %s (%s)%s. Nothing starts until `daedalus-desktop update resolve` has settled it; the report is %s", liveDescription(rep.Live), rep.Reason, rep.Outcome, detail, where)
	}
	hint := ""
	if fencePermanent(rep) {
		hint = ". This will not go away by itself: fix what is named, or set DAEDALUS_DATA_FENCE=off to protect the data with a verified backup instead"
	}
	return fmt.Errorf("the data folder could not be switched safely, so nothing was changed — %s (%s)%s; the report is %s%s", rep.Reason, rep.Outcome, detail, where, hint)
}

func liveDescription(live string) string {
	if live == "C" {
		return "the switch's copy (an exact copy of the data, checked under the fence)"
	}
	return live
}

// fencePermanent tells a refusal that stays until something changes — a special file, a file of
// another user or one this user cannot read, more files than the fence can hold — from one a moment
// later may not repeat, such as a writer that closes its file.
func fencePermanent(rep fenceReport) bool {
	if rep.Outcome != fenceFailClosed {
		return false
	}
	text := rep.Reason
	for _, entry := range rep.Entries {
		text += " " + entry.Cause
	}
	for _, marker := range []string{"owned by uid", "a mount inside", "a mount of its own", "cannot fully use", "cannot read (mode", "special file", "inode flags", "reachable twice", "rlimit_nofile", "inotify", "not a directory"} {
		if strings.Contains(strings.ToLower(text), marker) {
			return true
		}
	}
	return false
}

// collectOldCopies removes the kept copies an update no longer needs — every automatically
// removable one except keep, the copy from before this update — each under its own fresh fence.
// A copy the fence finds anything wrong with stays, with its reason.
//
// The copies a failed update left (failed-*) are the operator's, and the newest of them stays until
// the operator removes it. The older ones go once a later update has committed: the data carried
// forward since has superseded them, and each is a whole copy of the data folder — kept without
// bound, two failed updates cost the disk twice the data. A copy a late write may have reached, or
// one an earlier removal left in doubt, is never among them.
func collectOldCopies(p Paths, keep string, say func(string, ...any)) {
	metas, _ := filepath.Glob(filepath.Join(fenceControlPath(p.Data), "retained", "*.json"))
	sort.Strings(metas)
	var records []fenceRetainedMeta
	newestFailed := ""
	var newestAt time.Time
	for _, path := range metas {
		body, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var meta fenceRetainedMeta
		if json.Unmarshal(body, &meta) != nil {
			continue
		}
		records = append(records, meta)
		if meta.Kind == "failed" && (newestFailed == "" || meta.Created.After(newestAt)) {
			newestFailed, newestAt = meta.Name, meta.Created
		}
	}
	for _, meta := range records {
		operator := false
		switch {
		case meta.Name == keep:
			continue
		case meta.GC == "auto":
		case meta.Kind == "failed" && meta.Name != newestFailed && len(meta.LateWritePossible) == 0 && meta.Status == "":
			operator = true
		default:
			continue
		}
		g := fenceCollectRetained(p.Data, "", meta.Name, operator, nil)
		if g.Outcome == fenceDeleted {
			say("removed the kept copy %s", meta.Name)
		} else {
			say("kept %s: %s", meta.Name, g.Reason)
		}
	}
}

// heldLocks are the installation lock files this update holds, for the switch to use instead of
// taking them a second time.
func (u *Upgrader) heldLocks() map[string]*os.File {
	held := map[string]*os.File{}
	if u.lock != nil && u.lock.file != nil {
		held[lockName] = u.lock.file
	}
	if u.finishLock != nil && u.finishLock.file != nil {
		held[finishLockName] = u.finishLock.file
	}
	return held
}

// follow takes the switch's locks on the lock files of the tree that is now live.
func (u *Upgrader) follow(files map[string]*os.File) {
	for name, file := range files {
		switch {
		case name == lockName && u.lock != nil:
			u.lock.follow(file)
		case name == finishLockName && u.finishLock != nil:
			u.finishLock.follow(file)
		default:
			_ = file.Close()
		}
	}
}

// protect keeps what an update may have to put back, before anything is changed: it records the
// choice in the journal, and either switches the data folder to a fenced copy or takes the
// verified backup.
func (u *Upgrader) protect(ctx context.Context, journal *Journal, root string, items []string) error {
	how, why, err := chooseProtection(u.paths, u.mode)
	if err != nil {
		return err
	}
	if how == protectBackup {
		u.say("The data is protected by a verified backup (%s).", why)
		backup, err := u.backUp(ctx, root, items)
		if err != nil {
			return err
		}
		journal.Backup = backup
		return nil
	}
	u.say("Switching the data folder to a fenced copy; the folder as it is now is kept whole...")
	rep := fencedSwitch(fenceOptions{Data: u.paths.Data, Held: u.heldLocks(), Handover: u.follow, Upgrading: true})
	if rep.Outcome != fenceCommitted {
		return fenceOutcomeError(rep)
	}
	pre := ""
	for _, r := range rep.Retained {
		if r.Kind == "pre" {
			pre = filepath.Base(r.Path)
		}
	}
	journal.Fence, journal.Pre = rep.Op, pre
	if key, err := fenceTreeKey(filepath.Join(fenceControlPath(u.paths.Data), "retained", pre)); err == nil {
		journal.PreKey = key
	}
	u.say("The data folder is switched; the folder from before is kept as %s.", filepath.Join(fenceControlPath(u.paths.Data), "retained", pre))
	for _, late := range rep.LateWritePossible {
		u.say("Note: a writer reached %s during the switch: %s (see `daedalus-desktop update status`).", late.Tree, strings.Join(late.Paths, ", "))
	}
	return nil
}

// restoreProtected puts the data from before the update back, the way protect kept it.
func (u *Upgrader) restoreProtected(ctx context.Context, journal *Journal) error {
	if journal.Pre != "" {
		if key, err := fenceTreeKey(u.paths.Data); err == nil && journal.PreKey != [2]uint64{} && key == journal.PreKey {
			// A rollback that was cut off after its restore committed: the data from before is live
			// already, and restoring it again would find no kept copy to restore.
			u.say("The data folder from before is already live again.")
			return nil
		}
		u.say("Putting the data folder from before back (%s)...", journal.Pre)
		rep := fencedSwitch(fenceOptions{Data: u.paths.Data, RestoreFrom: journal.Pre, Held: u.heldLocks(), Handover: u.follow, Upgrading: true})
		if rep.Outcome != fenceCommitted {
			return fmt.Errorf("%s (%s); the report is %s", rep.Reason, rep.Outcome, rep.ReportPath)
		}
		failed := ""
		for _, r := range rep.Retained {
			if r.Kind == "failed" {
				failed = r.Path
			}
		}
		u.say("The data from before is live again; what the new version left is kept at %s.", failed)
		return nil
	}
	if journal.Backup == "" {
		return nil
	}
	if err := os.MkdirAll(upgradeDir(u.paths), 0o700); err != nil {
		return err
	}
	// A folder of its own for every attempt: a rollback interrupted and run again, or two in one
	// second, must never collide with the folder the last one filled.
	aside, err := os.MkdirTemp(upgradeDir(u.paths), "replaced-"+u.now().UTC().Format("20060102T150405Z")+"-")
	if err != nil {
		return err
	}
	u.say("Restoring the data from %s (what is there now is moved to %s)...", journal.Backup, aside)
	if err := RestoreBackup(u.paths, journal.Backup, aside); err != nil {
		return err
	}
	manifest, err := ReadManifest(journal.Backup)
	if err != nil {
		return fmt.Errorf("reading the backup's manifest: %w", err)
	}
	if err := u.stack.Restore(ctx, journal.Backup, manifest); err != nil {
		return fmt.Errorf("restoring Docker's volumes and images: %w", err)
	}
	u.say("Data restored and checked against the backup.")
	return nil
}

// cleanUpAfter is the end of a committed update: what earlier updates kept and nothing will go back
// to any more goes — old kept copies and backups on either path (the fence's path may follow runs
// with DAEDALUS_DATA_FENCE=off, and each backup left in the data folder is copied by every switch),
// the data older restores moved aside, and runtimes an earlier move set aside.
func (u *Upgrader) cleanUpAfter(journal *Journal) {
	collectOldCopies(u.paths, journal.Pre, u.say)
	if err := PruneBackups(u.paths, backupKeep); err != nil {
		u.say("old backups could not be pruned: %v", err)
	}
	pruneReplaced(u.paths)
	removeLegacyRuntimes(u.paths)
}

// pruneReplaced keeps only the newest of the folders a backup's restore moved the replaced data
// into (upgrade/replaced-*): the rest were superseded by the update that committed since.
func pruneReplaced(p Paths) {
	old, _ := filepath.Glob(filepath.Join(upgradeDir(p), "replaced-*"))
	sort.Strings(old) // the names start with a UTC timestamp
	for len(old) > 1 {
		_ = os.RemoveAll(old[0])
		old = old[1:]
	}
}
