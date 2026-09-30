//go:build linux

package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// A rollback whose restore of the data is refused — here the kept copy changed after it was
// recorded — does not call itself rolled back: it stops, says so, and leaves the data as it is.
func TestARefusedRestoreIsARollbackThatFailed(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	in.stack.fail = true
	in.stack.onUpdate = func() {
		journal, err := readJournal(in.paths)
		if err != nil || journal.Pre == "" {
			t.Errorf("no kept copy while the new version starts: %+v %v", journal, err)
			return
		}
		pre := filepath.Join(fenceControlPath(in.paths.Data), "retained", journal.Pre)
		os.WriteFile(filepath.Join(pre, "workspaces", "notes", "today.md"), []byte("changed while kept\n"), 0o640)
	}
	err := u.update(context.Background())
	if err == nil || !strings.Contains(err.Error(), "rollback could not finish") {
		t.Fatalf("err = %v\n%s", err, u.out)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageRollbackFailed {
		t.Fatalf("journal %+v", journal)
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatal("the data was changed by a restore that was refused")
	}
	if err := InterruptedUpgrade(in.paths); err == nil {
		t.Fatal("a failed rollback does not block the launcher")
	}
}

// A process that dies right after the restore made the data from before live again, before the
// launcher's files were put back: the journal lives outside the exchanged folder, so the upgrade is
// still unresolved, nothing starts, and the rollback asked for then finishes without restoring twice.
func TestACrashAfterTheRestoreIsFinishedByTheRollback(t *testing.T) {
	fencedInstallation(t)
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	u.out = &crashWriter{at: "The data from before is live again"}
	func() {
		defer func() {
			if r := recover(); r != nil {
				if _, ok := r.(crashNow); !ok {
					panic(r)
				}
			}
		}()
		_ = u.Run(context.Background())
	}()
	if u.lock != nil {
		u.lock.Release()
	}
	if u.finishLock != nil {
		u.finishLock.Release()
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("the crash did not come after the restore")
	}
	if err := InterruptedUpgrade(in.paths); err == nil {
		t.Fatalf("a start is not refused: the launcher in place is %q on the data from before", in.file(t, launcherName()))
	}
	rollback := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{rollback: true}}
	if err := rollback.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, rollback.out)
	}
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatalf("not put back\n%s", rollback.out)
	}
	if !strings.Contains(rollback.out.(*bytes.Buffer).String(), "already live again") {
		t.Fatalf("the restore ran a second time\n%s", rollback.out)
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}

type crashWriter struct {
	bytes.Buffer
	at string
}

type crashNow struct{}

func (w *crashWriter) Write(p []byte) (int, error) {
	n, err := w.Buffer.Write(p)
	if w.at != "" && strings.Contains(string(p), w.at) {
		panic(crashNow{})
	}
	return n, err
}

// The update checks for writers before it protects the data, as the upgrade does: on the backup's
// path nothing else would, and the backup would be torn.
func TestAnUpdateRefusesADataWriterBeforeItsBackup(t *testing.T) {
	t.Setenv("DAEDALUS_DATA_FENCE", "off")
	u, in := updater(t)
	child := startFenceChild(t, "fence-hold-open", "HELPER_PATH="+filepath.Join(in.paths.Workspaces, "live.log"))
	child.expect(t, "ready", 5*time.Second)
	err := u.update(context.Background())
	if err == nil || !strings.Contains(err.Error(), "still in use") {
		t.Fatalf("err = %v\n%s", err, u.out)
	}
	if in.stack.updates != 0 || exists(backupsDir(in.paths)) {
		t.Fatal("the update went on under a writer")
	}
}

// Copies failed updates left are bounded: after an update that commits, only the newest of them
// stays, for the operator.
func TestOnlyTheNewestFailedCopyOutlivesALaterUpdate(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	in.stack.fail = true
	for i := 0; i < 2; i++ {
		if err := u.update(context.Background()); err == nil {
			t.Fatal("reported success")
		}
		time.Sleep(10 * time.Millisecond)
	}
	failedTrees := func() []string {
		matches, _ := filepath.Glob(filepath.Join(fenceControlPath(in.paths.Data), "retained", "failed-*"))
		var trees []string
		for _, m := range matches {
			if !strings.HasSuffix(m, ".json") {
				trees = append(trees, m)
			}
		}
		return trees
	}
	if len(failedTrees()) != 2 {
		t.Fatalf("failed copies before: %v", failedTrees())
	}
	in.stack.fail = false
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	if left := failedTrees(); len(left) != 1 {
		t.Fatalf("failed copies after a committed update: %v\n%s", left, u.out)
	}
}

// An upgrade that is rolled back to a launcher from before the runtime moved puts the runtime back
// where that launcher looks for it, with the logins it last saw.
func TestARolledBackUpgradePutsTheOldRuntimeBack(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err == nil {
		t.Fatalf("reported success\n%s", u.out)
	}
	if in.file(t, "data/runtime/venv/big") != "downloaded again, never backed up" {
		t.Fatalf("the runtime is not back for the previous launcher\n%s", u.out)
	}
}

// A migration cut off half-way through a copy leaves no partial state live: the next attempt copies
// again, whole.
func TestAMigrationCutOffIsCarriedWholeTheNextTime(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the copy is stopped by a file its owner may not read, and root reads it anyway")
	}
	p := fixtureData(t)
	profile := filepath.Join(p.LegacyRuntime, "browserd", "state", "profiles", "default")
	os.MkdirAll(profile, 0o700)
	os.WriteFile(filepath.Join(profile, "a-Cookies"), []byte("login"), 0o600)
	os.WriteFile(filepath.Join(profile, "b-Login Data"), []byte("secret"), 0o600)
	os.Chmod(filepath.Join(profile, "b-Login Data"), 0)
	if err := migrateLegacyRuntime(context.Background(), p, t.Logf); err == nil {
		t.Fatal("the copy did not stop")
	}
	if exists(browserdStateDir(p)) {
		t.Fatal("a partial copy is live")
	}
	os.Chmod(filepath.Join(profile, "b-Login Data"), 0o600)
	if err := migrateLegacyRuntime(context.Background(), p, t.Logf); err != nil {
		t.Fatal(err)
	}
	if body, _ := os.ReadFile(filepath.Join(browserdStateDir(p), "profiles", "default", "b-Login Data")); string(body) != "secret" {
		t.Fatal("the live browser state is not the whole copy")
	}
	if left, _ := filepath.Glob(browserdStateDir(p) + "*"); len(left) != 1 {
		t.Fatalf("left beside it: %v", left)
	}
}

// When the new layout already holds state and the old folder's is newer — the older launcher ran
// again after a rolled-back upgrade — the newer is the one kept live, and the other stays beside it.
func TestTheNewerStateIsKeptLive(t *testing.T) {
	p := fixtureData(t)
	legacy := filepath.Join(p.LegacyRuntime, "browserd", "state", "profile")
	os.MkdirAll(legacy, 0o700)
	os.WriteFile(filepath.Join(legacy, "Cookies"), []byte("newer login"), 0o600)
	live := filepath.Join(browserdStateDir(p), "profile")
	os.MkdirAll(live, 0o700)
	os.WriteFile(filepath.Join(live, "Cookies"), []byte("older login"), 0o600)
	old := time.Now().Add(-time.Hour)
	os.Chtimes(filepath.Join(live, "Cookies"), old, old)
	if err := migrateLegacyRuntime(context.Background(), p, t.Logf); err != nil {
		t.Fatal(err)
	}
	if body, _ := os.ReadFile(filepath.Join(live, "Cookies")); string(body) != "newer login" {
		t.Fatalf("live is %q", body)
	}
	kept, _ := filepath.Glob(browserdStateDir(p) + "-before-the-move-*")
	if len(kept) != 1 {
		t.Fatalf("the older state is not kept beside it: %v", kept)
	}
}

// An upgrade from a launcher that still keeps its runtime in the data folder moves the runtime out
// before it protects the data. When the protection then refuses — no room for the copy, a backup
// that will not verify — the runtime comes back where that launcher looks for it, and the refusal
// may truly say nothing was changed.
func TestARefusedUpgradePutsTheOldRuntimeBack(t *testing.T) {
	for _, how := range []string{"fence without room", "backup that does not verify"} {
		t.Run(how, func(t *testing.T) {
			withVersion(t, "desktop-v0.12.0")
			in := newInstallation(t)
			u := in.upgrader("yes\n", true)
			if how == "fence without room" {
				fencedInstallation(t)
				old := fenceSpaceMargin
				fenceSpaceMargin = 1 << 62
				t.Cleanup(func() { fenceSpaceMargin = old })
			} else {
				u.opts.damageBackup = func(dir string) { os.WriteFile(filepath.Join(dir, backupArchive), []byte("damaged"), 0o600) }
			}
			newRelease().serve(t, "desktop-v0.13.0")
			err := u.Run(context.Background())
			if err == nil || !strings.Contains(err.Error(), "nothing was changed") || strings.Contains(err.Error(), "could not be put back") {
				t.Fatalf("err = %v\n%s", err, u.out)
			}
			if in.file(t, "data/runtime/venv/big") != "downloaded again, never backed up" {
				t.Fatalf("the runtime is not back in the data folder\n%s", u.out)
			}
			if in.file(t, launcherName()) != "old launcher" || exists(journalFile(in.paths)) {
				t.Fatal("the refused upgrade left something behind")
			}
		})
	}
}

// A committed update clears what earlier ones kept and nothing will go back to: all but the newest
// folder a backup's restore moved aside, backups beyond the newest few — on the fence's path too —
// and runtimes an earlier move set aside.
func TestACommittedUpdateClearsWhatNothingGoesBackTo(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	for _, name := range []string{"replaced-20260101T000000Z-1", "replaced-20260102T000000Z-2"} {
		os.MkdirAll(filepath.Join(upgradeDir(in.paths), name, "state"), 0o700)
	}
	for i := 0; i < backupKeep+2; i++ {
		os.MkdirAll(filepath.Join(backupsDir(in.paths), fmt.Sprintf("2026010%dT000000Z-desktop-v0.12.0", i+1)), 0o700)
	}
	legacy := filepath.Join(filepath.Dir(in.paths.Runtime), "legacy-"+filepath.Base(in.paths.Runtime)+"-20260101T000000Z")
	os.MkdirAll(legacy, 0o700)
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	replaced, _ := filepath.Glob(filepath.Join(upgradeDir(in.paths), "replaced-*"))
	if len(replaced) != 1 || filepath.Base(replaced[0]) != "replaced-20260102T000000Z-2" {
		t.Errorf("replaced folders left: %v", replaced)
	}
	if backups, _ := os.ReadDir(backupsDir(in.paths)); len(backups) != backupKeep {
		t.Errorf("%d backups left, want %d", len(backups), backupKeep)
	}
	if exists(legacy) {
		t.Error("a runtime an earlier move set aside is still there")
	}
}
