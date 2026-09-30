//go:build linux

package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestFenceSwitchCommitsAndCollectsAQuietTree(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	if rep.ExitCode != 0 || rep.LeasesTaken == 0 {
		t.Fatalf("exit %d, leases %d", rep.ExitCode, rep.LeasesTaken)
	}
	if fenceIno(t, data) == original {
		t.Fatal("data is still the original inode: nothing was switched")
	}
	if got := fenceRead(t, filepath.Join(data, "workspaces", "note.txt")); got != "before" {
		t.Fatalf("live note %q", got)
	}
	pre := fenceRetainedPath(data, rep, "pre")
	if pre == "" || fenceIno(t, pre) != original {
		t.Fatalf("the previous tree is not retained by inode: %v", rep.Retained)
	}
	fenceNoSlot(t, data)
	g := fenceGC(t, data, filepath.Base(pre), false, nil)
	if g.Outcome != fenceDeleted {
		t.Fatalf("quiet retained tree not removed: %+v", g)
	}
	if _, err := os.Lstat(pre); !os.IsNotExist(err) {
		t.Fatalf("retained tree still there: %v", err)
	}
	if _, code := fenceStatus(fenceControlDir(data), true); code != 0 {
		t.Fatal("status reports trouble after a clean switch and removal")
	}
}

// Why a hash compared before and after is not a fence: a shared writable mapping on a file of the
// data folder, written through after the decision, changes the file without any open() the check
// could see. The full hashes before and after are kept to show the write really changes the file.
// The switch must refuse before any exchange, and the late write must land in the live data.
func TestAHeldSharedMappingIsRefusedAndItsLateWriteStaysLive(t *testing.T) {
	for _, closeFD := range []bool{false, true} {
		t.Run("fd-closed="+strconv.FormatBool(closeFD), func(t *testing.T) {
			data := fenceFixture(t)
			path := filepath.Join(data, "workspaces", "note.txt")
			original := fenceIno(t, data)
			f, err := os.OpenFile(path, os.O_RDWR, 0)
			if err != nil {
				t.Fatal(err)
			}
			mapping, err := syscall.Mmap(int(f.Fd()), 0, len("before"), syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
			if err != nil {
				t.Fatal(err)
			}
			if closeFD {
				f.Close()
			} else {
				defer f.Close()
			}
			before := fenceSHA(t, path)
			rep := fenceRun(t, data, nil)
			fenceWant(t, rep, fenceRefused)
			if !fenceHasEntry(rep, "P", "workspaces/note.txt", "open for writing") {
				t.Fatalf("the refusal does not name the held file: %+v", rep.Entries)
			}
			copy(mapping, []byte("AFTER!"))
			if err := syscall.Munmap(mapping); err != nil {
				t.Fatal(err)
			}
			if before == fenceSHA(t, path) {
				t.Fatal("the mapped write did not change the full hash")
			}
			if fenceIno(t, data) != original {
				t.Fatal("data changed inode despite the refusal")
			}
			if got := fenceRead(t, path); got != "AFTER!" {
				t.Fatalf("the late write is not in the live data: %q", got)
			}
			if len(rep.Retained) != 0 {
				t.Fatalf("a refusal before the copy kept something: %+v", rep.Retained)
			}
			fenceNoSlot(t, data)
		})
	}
}

// The same timeline after a commit: a mapping into the retained previous tree, then its write.
// Removal must keep the tree while the mapping is held, and keep it again once the content differs.
func TestFenceRetainedTreeWithLateMappingIsNeverRemoved(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	pre := fenceRetainedPath(data, rep, "pre")
	note := filepath.Join(pre, "workspaces", "note.txt")
	f, err := os.OpenFile(note, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	mapping, err := syscall.Mmap(int(f.Fd()), 0, 6, syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	g1 := fenceGC(t, data, filepath.Base(pre), false, nil)
	copy(mapping, []byte("AFTER!"))
	syscall.Munmap(mapping)
	g2 := fenceGC(t, data, filepath.Base(pre), false, nil)
	if g1.Outcome != fenceRetainedGC || !strings.Contains(g1.Reason, "held open") {
		t.Fatalf("first removal with the mapping held: %+v", g1)
	}
	if g2.Outcome != fenceRetainedGC || !strings.Contains(g2.Reason, "changed after it was recorded") {
		t.Fatalf("second removal after the write: %+v", g2)
	}
	if got := fenceRead(t, note); got != "AFTER!" {
		t.Fatalf("the late write is gone: %q", got)
	}
}

// A new writer arrives while the copy runs: it blocks in open(), the switch refuses at once and
// lets go, and the write lands in the live data.
func TestFenceWriterDuringCopyIsRefusedAtOnce(t *testing.T) {
	data := fenceFixture(t)
	note := filepath.Join(data, "workspaces", "note.txt")
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{duringCopy: func(p *fenceTree) {
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+note, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, p, "workspaces/note.txt")
	}})
	fenceWant(t, rep, fenceRefused)
	if !fenceHasEntry(rep, "P", "workspaces/note.txt", "lease broken") {
		t.Fatalf("the refusal does not name the file the writer waits on: %+v", rep.Entries)
	}
	line := child.expect(t, "wrote", 10*time.Second)
	blocked, _ := strconv.Atoi(strings.Fields(line)[1])
	t.Logf("the writer was blocked for %d ms (the kernel would have held it up to lease-break-time)", blocked)
	if blocked > 5000 {
		t.Fatalf("the writer waited %d ms: the refusal did not come at once", blocked)
	}
	if got := fenceRead(t, note); got != "LATE!!" {
		t.Fatalf("the blocked write did not land in the live data: %q", got)
	}
	fenceNoSlot(t, data)
}

// A change that leaves nothing to compare: the live root's mode is changed and put back right after
// the exchange, so the final mode is identical to the recorded one. The event must still roll the
// switch back, name C's root, and leave the original live by inode.
func TestARootModeChangedAndRestoredAfterTheExchangeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	before := fenceTreeDigest(t, data)
	changed := false
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		if err := os.Chmod(data, 0o701); err != nil {
			t.Error(err)
		}
		if err := os.Chmod(data, 0o700); err != nil {
			t.Error(err)
		}
		changed = true
	}})
	if !changed {
		t.Fatal("the chmod-and-restore action was not performed")
	}
	fenceWant(t, rep, fenceRolledBack)
	if !fenceHasEntry(rep, "C", ".", "ATTRIB") {
		t.Fatalf("the report lacks C . ATTRIB: %+v", rep.Entries)
	}
	if fenceIno(t, data) != original {
		t.Fatal("the original is not live again by inode")
	}
	if after := fenceTreeDigest(t, data); after != before {
		t.Fatalf("the data differs after the rollback:\n%s\n%s", before, after)
	}
	rejected := fenceRetainedPath(data, rep, "rejected")
	if rejected == "" {
		t.Fatalf("the rejected copy is not retained: %+v", rep.Retained)
	}
	if g := fenceGC(t, data, filepath.Base(rejected), false, nil); g.Outcome != fenceDeleted {
		t.Fatalf("an unchanged rejected copy should be removable: %+v", g)
	}
}

// A writer that opens data/... after the exchange reaches the copy's inode and blocks there. The
// switch rolls back; the write lands in the rejected copy, which removal then keeps.
func TestFenceWriterAfterExchangeLandsInTheKeptCopy(t *testing.T) {
	data := fenceFixture(t)
	note := filepath.Join(data, "workspaces", "note.txt")
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+note, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, c, "workspaces/note.txt")
	}})
	fenceWant(t, rep, fenceRolledBack)
	child.expect(t, "wrote", 10*time.Second)
	rejected := fenceRetainedPath(data, rep, "rejected")
	if got := fenceRead(t, filepath.Join(rejected, "workspaces", "note.txt")); got != "LATE!!" {
		t.Fatalf("the write is not in the rejected copy: %q", got)
	}
	if got := fenceRead(t, note); got != "before" {
		t.Fatalf("the live data changed: %q", got)
	}
	if g := fenceGC(t, data, filepath.Base(rejected), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the rejected copy with a write in it was not kept: %+v", g)
	}
}

func TestFenceUnrelatedSiblingDoesNotRefuse(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		f, err := os.OpenFile(filepath.Join(filepath.Dir(data), "unrelated.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			t.Error(err)
			return
		}
		f.WriteString("x")
		f.Close()
	}})
	fenceWant(t, rep, fenceCommitted)
}

// On tmpfs the switch refuses from statfs alone and creates nothing there.
func TestFenceOtherFilesystemFailsClosedWithoutWriting(t *testing.T) {
	var fs unix.Statfs_t
	if err := unix.Statfs("/dev/shm", &fs); err != nil || uint64(fs.Type) != 0x01021994 {
		t.Skip("no tmpfs at /dev/shm")
	}
	before, _ := os.ReadDir("/dev/shm")
	// Not root and not in a container, so that the filesystem check is the one that decides even
	// where the tests themselves run as root in a container.
	seams := &fenceSeams{euid: func() int { return 1000 }, container: func() bool { return false }}
	rep := fencedSwitch(fenceOptions{Data: "/dev/shm/daedalus-fence-test-data-that-does-not-exist", seams: seams})
	after, _ := os.ReadDir("/dev/shm")
	if rep.Outcome != fenceFailClosed || rep.ExitCode != 5 {
		t.Fatalf("tmpfs: %+v", rep)
	}
	if !strings.Contains(rep.Reason, "ext4") {
		t.Fatalf("the reason does not name the filesystem: %s", rep.Reason)
	}
	if len(after) != len(before) || rep.ReportPath != "" {
		t.Fatalf("something was created on tmpfs: %d → %d entries, report %q", len(before), len(after), rep.ReportPath)
	}
}

// A backend still running keeps its database open for writing. The switch refuses and names it.
func TestFenceRunningBackendIsRefusedByItsDatabase(t *testing.T) {
	data := fenceFixture(t)
	db := filepath.Join(data, "state", "daedalus.sqlite")
	child := startFenceChild(t, "fence-hold-open", "HELPER_PATH="+db)
	child.expect(t, "ready", 5*time.Second)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceRefused)
	found := false
	for _, entry := range rep.Entries {
		if entry.Tree == "P" && entry.Path == "state/daedalus.sqlite" && entry.PID == child.pid() {
			found = true
		}
	}
	if !found {
		t.Fatalf("the refusal does not name the database and its writer %d: %+v", child.pid(), rep.Entries)
	}
}
