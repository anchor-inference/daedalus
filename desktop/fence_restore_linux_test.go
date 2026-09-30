//go:build linux

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// After a switch, the new version changes the live data (its migrations) and does not come up. The
// restore puts the tree from before the switch back at data under the same fence, and keeps what
// the failed version left for the operator.
func TestARestorePutsTheDataFromBeforeBackAndKeepsWhatFailed(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	pre := filepath.Base(fenceRetainedPath(data, rep, "pre"))
	if err := os.WriteFile(filepath.Join(data, "state", "daedalus.sqlite"), []byte("migrated by the new version"), 0o600); err != nil {
		t.Fatal(err)
	}
	failedIno := fenceIno(t, data)
	back := fencedSwitch(fenceOptions{Data: data, RestoreFrom: pre})
	fenceWant(t, back, fenceCommitted)
	if fenceIno(t, data) != original {
		t.Fatal("the data from before the switch is not live again by inode")
	}
	if got := fenceRead(t, filepath.Join(data, "state", "daedalus.sqlite")); got != string(make([]byte, 8192)) {
		t.Fatal("the database is not the one from before")
	}
	failed := fenceRetainedPath(data, back, "failed")
	if failed == "" || fenceIno(t, failed) != failedIno || fenceRead(t, filepath.Join(failed, "state", "daedalus.sqlite")) != "migrated by the new version" {
		t.Fatalf("what the failed version left is not kept: %+v", back.Retained)
	}
	if _, err := os.Stat(filepath.Join(fenceControlDir(data), "retained", pre+".json")); !os.IsNotExist(err) {
		t.Fatal("the record of the kept copy survived its return to data")
	}
	if g := fenceGC(t, data, filepath.Base(failed), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the failed data was removed automatically: %+v", g)
	}
	if next := fenceRun(t, data, nil); next.Outcome != fenceCommitted {
		t.Fatalf("a switch after the restore: %+v", next)
	}
}

// A kept copy that changed since it was recorded holds something nobody has looked at: not put back.
func TestAChangedKeptCopyIsNotRestored(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	pre := fenceRetainedPath(data, rep, "pre")
	if err := os.WriteFile(filepath.Join(pre, "workspaces", "note.txt"), []byte("LATE!!"), 0o600); err != nil {
		t.Fatal(err)
	}
	live := fenceIno(t, data)
	back := fencedSwitch(fenceOptions{Data: data, RestoreFrom: filepath.Base(pre)})
	fenceWant(t, back, fenceFailClosed)
	if !strings.Contains(back.Reason, "changed since it was recorded") || fenceIno(t, data) != live {
		t.Fatalf("%+v", back)
	}
}

// A writer in the live data during the restore refuses it, like any switch.
func TestAWriterDuringTheRestoreRefusesIt(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	child := startFenceChild(t, "fence-hold-open", "HELPER_PATH="+filepath.Join(data, "state", "daedalus.sqlite"))
	child.expect(t, "ready", 5*time.Second)
	live := fenceIno(t, data)
	back := fencedSwitch(fenceOptions{Data: data, RestoreFrom: filepath.Base(fenceRetainedPath(data, rep, "pre"))})
	fenceWant(t, back, fenceRefused)
	if fenceIno(t, data) != live {
		t.Fatal("data changed")
	}
}
