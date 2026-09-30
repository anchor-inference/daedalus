//go:build linux

package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A switch makes the copy durable before it can become live, and the exchange durable before it
// decides anything on it. Each step is watched through the seam, and the order is checked against
// what is at data at that moment: the copy is synced while data is still the original, the folders
// after the exchange, and a filed tree before its record is relied on.
func TestTheCopyReachesTheDiskBeforeItBecomesLive(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	var steps []string
	rep := fenceRun(t, data, &fenceSeams{sync: func(what string) error {
		live := "original"
		if fenceIno(t, data) != original {
			live = "copy"
		}
		steps = append(steps, what+" with the "+live+" live")
		return nil
	}})
	fenceWant(t, rep, fenceCommitted)
	want := []string{"copy with the original live", "exchange with the copy live", "retain with the copy live"}
	if strings.Join(steps, "; ") != strings.Join(want, "; ") {
		t.Fatalf("durability steps %q, want %q", steps, want)
	}
}

// A sync that fails before the exchange stops the switch there: nothing is switched, and the copy,
// the record written in advance and the slot all go.
func TestACopyThatCannotReachTheDiskIsNeverSwitchedIn(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	before := fenceTreeDigest(t, data)
	rep := fenceRun(t, data, &fenceSeams{sync: func(what string) error {
		if what == "copy" {
			return syscall.EIO
		}
		return nil
	}})
	fenceWant(t, rep, fenceFailClosed)
	if fenceIno(t, data) != original || fenceTreeDigest(t, data) != before {
		t.Fatal("the data changed although the copy never reached the disk")
	}
	fenceNoSlot(t, data)
	fenceNothingKept(t, data)
}

// A sync of the folders after the exchange that fails puts the original back.
func TestAnExchangeThatCannotReachTheDiskIsRolledBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{sync: func(what string) error {
		if what == "exchange" {
			return syscall.EIO
		}
		return nil
	}})
	if rep.Outcome != fenceRolledBack || fenceIno(t, data) != original {
		t.Fatalf("outcome %s (%s), data ino %d want %d", rep.Outcome, rep.Reason, fenceIno(t, data), original)
	}
	if rep.Live != "P" {
		t.Fatalf("the report says %q is live", rep.Live)
	}
	if _, err := os.Stat(filepath.Join(fenceControlDir(data), "retained", "pre-"+rep.Op+".json")); err == nil {
		t.Fatal("the record written in advance for the original outlived a rollback")
	}
}

func fenceNothingKept(t *testing.T, data string) {
	t.Helper()
	left, _ := filepath.Glob(filepath.Join(fenceControlDir(data), "retained", "*"))
	if len(left) > 0 {
		t.Fatalf("kept after a switch that changed nothing: %v", left)
	}
	tmp, _ := filepath.Glob(filepath.Join(fenceControlDir(data), "*", "*.tmp"))
	if len(tmp) > 0 {
		t.Fatalf("temporary files left in the control folder: %v", tmp)
	}
}

// Not enough room for the copy is found before anything is written.
func TestACopyWithoutRoomIsRefusedBeforeItStarts(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	copied := false
	rep := fenceRun(t, data, &fenceSeams{freeSpace: func() uint64 { return 1 << 20 }, copyWrite: func(string) error { copied = true; return nil }})
	fenceWant(t, rep, fenceRefused)
	if !strings.Contains(rep.Reason, "not enough free space") || copied || fenceIno(t, data) != original {
		t.Fatalf("reason %q, copied %v", rep.Reason, copied)
	}
	fenceNoSlot(t, data)
	fenceNothingKept(t, data)
	if text, code := fenceStatus(fenceControlDir(data), false); code != 0 || !strings.Contains(text, "no kept copies") {
		t.Fatalf("status after a refusal: %d %s", code, text)
	}
}

// A disk that fills up half-way through the copy — and stays full, so that not even a record fits —
// ends with the partial copy gone, nothing recorded and the next switch free to start.
func TestAFullDiskDuringTheCopyLeavesNothingBehind(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	full := false
	seams := &fenceSeams{
		copyWrite: func(rel string) error {
			if rel == "workspaces/note.txt" {
				full = true
				return syscall.ENOSPC
			}
			return nil
		},
		controlWrite: func(sub, name string) error {
			if full && sub == "retained" {
				return syscall.ENOSPC
			}
			return nil
		},
	}
	rep := fenceRun(t, data, seams)
	if rep.Outcome == fenceCommitted || fenceIno(t, data) != original {
		t.Fatalf("outcome %s with a full disk", rep.Outcome)
	}
	if len(rep.GC) != 1 || rep.GC[0].Outcome != fenceDeleted {
		t.Fatalf("the partial copy was not removed: %+v", rep.GC)
	}
	fenceNoSlot(t, data)
	fenceNothingKept(t, data)
	trash, _ := os.ReadDir(filepath.Join(fenceControlDir(data), "trash"))
	if len(trash) > 0 {
		t.Fatalf("left in the trash: %v", trash)
	}
	fenceWant(t, fenceRun(t, data, nil), fenceCommitted)
}

// A full disk when the record of the data from before is due — written before the exchange now —
// refuses the switch with nothing exchanged, instead of filing a tree nobody can find.
func TestNoRoomForTheRecordRefusesBeforeTheExchange(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{controlWrite: func(sub, name string) error {
		if sub == "retained" && strings.HasPrefix(name, "pre-") {
			return syscall.ENOSPC
		}
		return nil
	}})
	fenceWant(t, rep, fenceRefused)
	if fenceIno(t, data) != original || rep.Live != "P" {
		t.Fatalf("switched although the record could not be written: live %s", rep.Live)
	}
	fenceNoSlot(t, data)
	fenceNothingKept(t, data)
}

// A disk that fills up after the exchange cannot take the journal's later entries, and the switch
// stands: the copy is exact and checked, the data from before is filed under the record written in
// advance, and the journal's last good entry makes status point at `update resolve`.
func TestAFullDiskAfterTheExchangeStillFilesTheDataFromBefore(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	exchanged := false
	rep := fenceRun(t, data, &fenceSeams{
		afterExchange: func(p, c *fenceTree) { exchanged = true },
		controlWrite: func(sub, name string) error {
			if exchanged && sub == "journal" {
				return syscall.ENOSPC
			}
			return nil
		},
	})
	fenceWant(t, rep, fenceCommitted)
	pre := fenceRetainedPath(data, rep, "pre")
	if pre == "" || fenceIno(t, pre) != original {
		t.Fatalf("the data from before is not filed: %+v", rep.Retained)
	}
	var meta fenceRetainedMeta
	if err := readJSON(pre+".json", &meta); err != nil || len(meta.Manifest) == 0 {
		t.Fatalf("the data from before has no record: %v", err)
	}
	if len(rep.Limits) == 0 {
		t.Fatal("the journal entries that could not be written are not reported")
	}
	if text, code := fenceStatus(fenceControlDir(data), false); code == 0 || !strings.Contains(text, "update resolve") {
		t.Fatalf("status does not send the operator to resolve: %d %s", code, text)
	}
}

// Filing a tree never replaces what is already under its name: the rename is RENAME_NOREPLACE, and
// resolve stops with the tree where it was.
func TestFilingNeverReplacesATreeUnderTheSameName(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	fenceCrash(t, data, "after-exchange")
	found, err := fenceResolve(data, "", false)
	if err != nil || len(found) != 1 {
		t.Fatalf("%+v %v", found, err)
	}
	squatter := filepath.Join(fenceControlDir(data), "retained", "pre-"+found[0].Op)
	os.Remove(squatter + ".json")
	if err := os.Mkdir(squatter, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := fenceResolve(data, "", true); err == nil {
		t.Fatal("resolve filed a tree over an existing one")
	}
	if fenceIno(t, squatter) == original {
		t.Fatal("the existing tree was replaced by the original")
	}
	slots, _ := filepath.Glob(filepath.Join(filepath.Dir(data), "."+filepath.Base(data)+"-slot-*"))
	if len(slots) != 1 || fenceIno(t, slots[0]) != original {
		t.Fatalf("the original is not where the crash left it: %v", slots)
	}
}

// A process that only maps a file of the tree — no descriptor, no working directory — is found by
// its memory map after the exchange, and the switch rolls back: it is still reading the tree that
// just became the old one.
func TestAProcessMappingTheDataIsFoundAfterTheExchange(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	child := startFenceChild(t, "fence-map-read", "HELPER_PATH="+filepath.Join(data, "state", "daedalus.sqlite"))
	child.expect(t, "ready", 5*time.Second)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceRolledBack)
	if fenceIno(t, data) != original || !fenceHasEntry(rep, "P", "state/daedalus.sqlite", "map") {
		t.Fatalf("the mapping was not found: %+v", rep.Entries)
	}
}

// A removal cut off half-way — the process killed between two unlinks — is named by status, and the
// next removal goes on from the trash, checking that what is left is exactly as recorded.
func TestARemovalCutOffGoesOnFromTheTrash(t *testing.T) {
	data := fenceFixture(t)
	for _, name := range []string{"a", "b", "c", "d"} {
		os.WriteFile(filepath.Join(data, "workspaces", name), []byte(name), 0o600)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	pre := filepath.Base(fenceRetainedPath(data, rep, "pre"))
	unlinks := 0
	func() {
		defer func() { recover() }()
		fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(*fenceTree, string) {
			if unlinks++; unlinks == 3 {
				panic("killed")
			}
		}})
	}()
	sum := fenceSummarize(fenceControlDir(data))
	named := false
	for _, item := range sum.Items {
		named = named || (item.Kind == "trash" && strings.Contains(item.Detail, "cut off"))
	}
	if !named {
		t.Fatalf("status does not name the removal that was cut off: %+v", sum)
	}
	g := fenceGC(t, data, pre, false, nil)
	if g.Outcome != fenceDeleted {
		t.Fatalf("the removal did not go on: %+v", g)
	}
	for _, item := range fenceSummarize(fenceControlDir(data)).Items {
		if item.Kind == "trash" || item.Kind == "retained" || item.Kind == "unrecorded" {
			t.Fatalf("something is left: %+v", item)
		}
	}
}

// Two data folders in one parent each have their own control folder: one's update never removes the
// other's kept copy.
func TestTwoDataFoldersInOneParentKeepTheirCopiesApart(t *testing.T) {
	fenceRequireExt4(t, os.TempDir())
	parent := t.TempDir()
	var paths []Paths
	for _, name := range []string{"alpha", "beta"} {
		dir := filepath.Join(parent, name)
		os.MkdirAll(filepath.Join(dir, "state"), 0o700)
		os.WriteFile(filepath.Join(dir, "state", "daedalus.sqlite"), []byte(name), 0o600)
		p, err := NewPaths(dir)
		if err != nil {
			t.Fatal(err)
		}
		paths = append(paths, p)
	}
	alpha := fencedSwitch(fenceOptions{Data: paths[0].Data})
	beta := fencedSwitch(fenceOptions{Data: paths[1].Data})
	if alpha.Outcome != fenceCommitted || beta.Outcome != fenceCommitted {
		t.Fatalf("%s %s", alpha.Reason, beta.Reason)
	}
	collectOldCopies(paths[1], filepath.Base(fenceRetainedPath(paths[1].Data, beta, "pre")), t.Logf)
	if !exists(fenceRetainedPath(paths[0].Data, alpha, "pre")) {
		t.Fatal("one data folder's update removed the other's kept copy")
	}
	if fenceControlPath(paths[0].Data) == fenceControlPath(paths[1].Data) {
		t.Fatal("two data folders share a control folder")
	}
}

// A data folder the switch could only refuse — a symlink to elsewhere, or a mount of its own — is
// protected by the backup instead of being refused on every update.
func TestADataFolderTheSwitchCannotExchangeTakesTheBackup(t *testing.T) {
	fenceRequireExt4(t, os.TempDir())
	t.Setenv("DAEDALUS_DATA_FENCE", "auto")
	real := filepath.Join(t.TempDir(), "elsewhere")
	os.MkdirAll(filepath.Join(real, "state"), 0o700)
	link := filepath.Join(t.TempDir(), "data")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	p, err := NewPaths(link)
	if err != nil {
		t.Fatal(err)
	}
	how, why, err := chooseProtection(p, ModeNative)
	if err != nil || how != protectBackup || !strings.Contains(why, "symbolic link") {
		t.Fatalf("chose %s (%s) %v", how, why, err)
	}
}

// update status counts the processes the last switch could not inspect, and lists them only when
// asked; an unfinished update names its own remedy, not resolve's.
func TestStatusCountsUninspectedProcessesAndNamesTheRollback(t *testing.T) {
	data := fenceFixture(t)
	control := fenceControlDir(data)
	os.MkdirAll(filepath.Join(control, "reports"), 0o700)
	report := fmt.Sprintf(`{"op":"x","outcome":"COMMITTED","started":"2026-01-01T00:00:00Z","unscanned":[{"pid":%d,"comm":"somebody"}]}`, os.Getpid())
	os.WriteFile(filepath.Join(control, "reports", "x.json"), []byte(report), 0o600)
	text, code := fenceStatus(control, false)
	if code != 0 || !strings.Contains(text, "1 processes could not be inspected") || strings.Contains(text, "somebody") {
		t.Fatalf("status without -v: %d %s", code, text)
	}
	if text, _ := fenceStatus(control, true); !strings.Contains(text, "somebody") {
		t.Fatalf("status -v does not list them: %s", text)
	}
	p, _ := NewPaths(data)
	writeJournal(p, &Journal{Kind: kindUpdate, Stage: stageFinishing})
	if text, code := fenceStatus(control, false); code == 0 || !strings.Contains(text, "upgrade --rollback") {
		t.Fatalf("status for an unfinished update: %d %s", code, text)
	}
}

// Nothing starts while a switch is unfinished: which tree is live is resolve's to say first.
func TestNothingStartsOnAnUnfinishedSwitch(t *testing.T) {
	data := fenceFixture(t)
	fenceCrash(t, data, "after-exchange")
	p, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	if err := InterruptedUpgrade(p); err == nil || !strings.Contains(err.Error(), "update resolve") {
		t.Fatalf("a start was not refused: %v", err)
	}
	if _, err := fenceResolve(data, "", true); err != nil {
		t.Fatal(err)
	}
	if err := InterruptedUpgrade(p); err != nil {
		t.Fatal(err)
	}
}

// A removal the page asks for never takes the kept copy an unresolved update would roll back to.
func TestTheOperatorCannotRemoveTheCopyAnUnfinishedUpdateNeeds(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	pre := filepath.Base(fenceRetainedPath(data, rep, "pre"))
	p, _ := NewPaths(data)
	writeJournal(p, &Journal{Kind: kindUpdate, Stage: stageFinishing, Pre: pre})
	if g := fenceGC(t, data, pre, true, nil); g.Outcome != fenceRetainedGC || !strings.Contains(g.Reason, "roll back") {
		t.Fatalf("%+v", g)
	}
}

// The runtime from before the move is not moved from under a process: an open port and a working
// directory inside it each refuse the move.
func TestTheLegacyRuntimeIsNotMovedFromUnderAProcess(t *testing.T) {
	t.Run("the app's port", func(t *testing.T) {
		p := legacyInstallation(t)
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer listener.Close()
		port := strconv.Itoa(listener.Addr().(*net.TCPAddr).Port)
		os.WriteFile(filepath.Join(p.Data, ".env"), []byte("API_PORT="+port+"\n"), 0o600)
		if err := migrateLegacyRuntime(context.Background(), p, t.Logf); err == nil || !exists(p.LegacyRuntime) {
			t.Fatalf("moved while the app's port answered: %v", err)
		}
	})
	t.Run("a working directory", func(t *testing.T) {
		p := legacyInstallation(t)
		cmd := exec.Command("sleep", "30")
		cmd.Dir = p.LegacyRuntime
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		defer func() { cmd.Process.Kill(); cmd.Wait() }()
		err := migrateLegacyRuntime(context.Background(), p, t.Logf)
		if err == nil || !exists(p.LegacyRuntime) || !strings.Contains(err.Error(), strconv.Itoa(cmd.Process.Pid)) {
			t.Fatalf("moved from under pid %d: %v", cmd.Process.Pid, err)
		}
	})
}

// The data from before is filed and recorded, and only the sync of the folders that filing changed
// fails: the switch stands, committed, and says the step did not reach the disk — it neither calls
// the exchange someone else's doing nor drops the record of the data from before.
func TestAFilingThatCannotReachTheDiskStillCommits(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{sync: func(what string) error {
		if what == "retain" {
			return syscall.EIO
		}
		return nil
	}})
	fenceWant(t, rep, fenceCommitted)
	if rep.Live != "C" || !strings.Contains(strings.Join(rep.Limits, "; "), "could not be written to the disk") {
		t.Fatalf("live %q, limits %v", rep.Live, rep.Limits)
	}
	pre := fenceRetainedPath(data, rep, "pre")
	var meta fenceRetainedMeta
	if pre == "" || fenceIno(t, pre) != original || readJSON(pre+".json", &meta) != nil || len(meta.Manifest) == 0 {
		t.Fatalf("the data from before is not filed with its record: %+v", rep.Retained)
	}
}

// Every tree the control folder holds is named by status, recorded or not, and so is a copy a switch
// left beside the data folder: each is the size of the data, and one nobody lists is one nobody
// removes.
func TestStatusNamesTreesWithoutARecordAndCopiesBesideTheData(t *testing.T) {
	data := fenceFixture(t)
	fenceWant(t, fenceRun(t, data, nil), fenceCommitted)
	lost := filepath.Join(fenceControlDir(data), "retained", "pre-norecord")
	stray := filepath.Join(filepath.Dir(data), "."+filepath.Base(data)+"-slot-left")
	for _, dir := range []string{lost, stray} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	text, _ := fenceStatus(fenceControlDir(data), false)
	for _, want := range []string{lost + " without a record", stray + ": a copy a switch left beside the data folder"} {
		if !strings.Contains(text, want) {
			t.Errorf("status does not say %q:\n%s", want, text)
		}
	}
}

// A refusal that stays until something changes names the way out; a writer that may be gone a
// moment later does not get told to switch the fence off.
func TestALastingRefusalNamesTheBackupInstead(t *testing.T) {
	lasting := fenceOutcomeError(fenceReport{Outcome: fenceFailClosed, Reason: "the fence cannot be held", Entries: []fenceEntryReport{{Path: "workspaces/fifo", Cause: "a special file (device, pipe or socket) cannot be copied or fenced"}}})
	if !strings.Contains(lasting.Error(), "DAEDALUS_DATA_FENCE=off") {
		t.Errorf("a lasting refusal does not name the backup: %v", lasting)
	}
	writer := fenceOutcomeError(fenceReport{Outcome: fenceRefused, Reason: "writer activity", Entries: []fenceEntryReport{{Path: "state/daedalus.sqlite", Cause: "open for writing"}}})
	if strings.Contains(writer.Error(), "DAEDALUS_DATA_FENCE") {
		t.Errorf("a passing writer is told to switch the fence off: %v", writer)
	}
}
