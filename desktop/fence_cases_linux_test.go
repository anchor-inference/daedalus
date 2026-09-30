//go:build linux

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func fenceSlotPath(t *testing.T, data string) string {
	t.Helper()
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(data), "."+filepath.Base(data)+"-slot-*"))
	if len(matches) != 1 {
		t.Fatalf("expected one slot, found %v", matches)
	}
	return matches[0]
}

func fenceTruncate(t *testing.T, path string) {
	t.Helper()
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_TRUNC|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	unix.Close(fd)
}

func fenceSetNodump(t *testing.T, path string) {
	t.Helper()
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(fd)
	flags, err := unix.IoctlGetInt(fd, unix.FS_IOC_GETFLAGS)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.IoctlSetPointerInt(fd, unix.FS_IOC_SETFLAGS, flags|0x40); err != nil {
		t.Fatal(err)
	}
}

type fenceInjectOverflow struct {
	base    fenceEventSource
	pending bool
}

func (s *fenceInjectOverflow) read() ([]fenceEvent, error) {
	events, err := s.base.read()
	if s.pending {
		events = append(events, fenceEvent{Mask: unix.IN_Q_OVERFLOW})
		s.pending = false
	}
	return events, err
}

func fenceOverflowInto(t *fenceTree) {
	t.source = &fenceInjectOverflow{base: t.source, pending: true}
}

func TestFenceKernelSelfCheckOnThisHost(t *testing.T) {
	data := fenceFixture(t)
	parent, err := os.Open(filepath.Dir(data))
	if err != nil {
		t.Fatal(err)
	}
	defer parent.Close()
	var st unix.Stat_t
	unix.Fstat(int(parent.Fd()), &st)
	k, reason := fenceOpenControl(parent, ".daedalus-update", uint32(os.Geteuid()), uint64(st.Dev), nil)
	if reason != "" {
		t.Fatal(reason)
	}
	defer k.close()
	var seen fenceProbeResult
	leases := 0
	if reason := fenceSelfProbe(k, "facts", &fenceSeams{selfProbe: func(r *fenceProbeResult) { seen = *r }}, &leases); reason != "" {
		t.Fatal(reason)
	}
	t.Logf("kernel facts here: %+v", seen)
	if !seen.ok() {
		t.Fatalf("the kernel does not behave as the fence assumes: %+v", seen)
	}
	left, _ := os.ReadDir(k.path)
	for _, entry := range left {
		if strings.HasPrefix(entry.Name(), "probe-") {
			t.Fatalf("the self-check left %s behind", entry.Name())
		}
	}
}

func TestFenceKernelSelfCheckMismatchFailsClosed(t *testing.T) {
	flips := map[string]func(*fenceProbeResult){
		"truncate-breaks-lease":  func(r *fenceProbeResult) { r.TruncKeepsLease = false },
		"truncate-without-event": func(r *fenceProbeResult) { r.TruncGivesModify = false },
		"outside-link-noisy":     func(r *fenceProbeResult) { r.OutsideLinkSilent = false },
		"link-count-unseen":      func(r *fenceProbeResult) { r.LinkCountSeen = false },
	}
	for name, flip := range flips {
		t.Run(name, func(t *testing.T) {
			data := fenceFixture(t)
			original := fenceIno(t, data)
			rep := fenceRun(t, data, &fenceSeams{selfProbe: flip})
			fenceWant(t, rep, fenceFailClosed)
			if !strings.Contains(rep.Reason, "does not behave") || rep.LeasesTaken != 1 {
				t.Fatalf("reason %q, leases %d", rep.Reason, rep.LeasesTaken)
			}
			fenceNoSlot(t, data)
			if fenceIno(t, data) != original {
				t.Fatal("data changed")
			}
		})
	}
}

func TestFencePrivateFoldersUnderALooseUmask(t *testing.T) {
	old := syscall.Umask(0o002)
	defer syscall.Umask(old)
	data := fenceFixture(t)
	modes := map[string]uint32{}
	rep := fenceRun(t, data, &fenceSeams{privateMode: func(name string, mode uint32) uint32 {
		modes[name] = mode
		return mode
	}})
	fenceWant(t, rep, fenceCommitted)
	slotSeen := false
	for name, mode := range modes {
		if mode != 0o700 {
			t.Fatalf("%s was %#o", name, mode)
		}
		if strings.Contains(name, "-slot-") {
			slotSeen = true
		}
	}
	if !slotSeen {
		t.Fatalf("the slot's mode was never checked: %v", modes)
	}
	for _, sub := range append([]string{""}, fenceControlSubdirs...) {
		info, err := os.Stat(filepath.Join(fenceControlDir(data), sub))
		if err != nil || info.Mode().Perm() != 0o700 {
			t.Fatalf("control %s: %v %v", sub, info.Mode(), err)
		}
	}
	for _, which := range []string{"-slot-", ".daedalus-update"} {
		t.Run("mismatch"+which, func(t *testing.T) {
			data := fenceFixture(t)
			rep := fencedSwitch(fenceOptions{Data: data, seams: &fenceSeams{privateMode: func(name string, mode uint32) uint32 {
				if strings.Contains(name, which) {
					return 0o750
				}
				return mode
			}}})
			if rep.Outcome != fenceFailClosed || !strings.Contains(rep.Reason, "not 0700") {
				t.Fatalf("a folder that is not private was accepted: %+v", rep)
			}
		})
	}
}

// A name created after its directory is watched but before the listing is read is either listed
// or announced; the walk never leaves a window where it is neither. A new directory would also
// change its parent's link count; a new file changes nothing but the listing, so it is the case
// only the order of watch and listing protects.
func TestFenceNameCreatedWhileListingRefuses(t *testing.T) {
	for _, kind := range []string{"directory", "file"} {
		t.Run(kind, func(t *testing.T) {
			data := fenceFixture(t)
			original := fenceIno(t, data)
			created := false
			rep := fenceRun(t, data, &fenceSeams{afterWatch: func(tree, rel string) {
				if tree != "P" || rel != "workspaces" || created {
					return
				}
				created = true
				path := filepath.Join(data, "workspaces", "appeared")
				var err error
				if kind == "directory" {
					err = os.Mkdir(path, 0o700)
				} else {
					err = os.WriteFile(path, []byte("late"), 0o600)
				}
				if err != nil {
					t.Error(err)
				}
			}})
			if !created {
				t.Fatal("the listing barrier was not reached")
			}
			fenceWant(t, rep, fenceRefused)
			if !fenceHasEntry(rep, "P", "workspaces/appeared", "CREATE") {
				t.Fatalf("the new name is not reported: %+v", rep.Entries)
			}
			if fenceIno(t, data) != original {
				t.Fatal("data switched")
			}
		})
	}
}

func TestFenceEntriesTheOwnerCannotHandleFailClosed(t *testing.T) {
	for name, set := range map[string]func(data string) error{
		"unreadable-file":     func(data string) error { return os.Chmod(filepath.Join(data, "workspaces", "note.txt"), 0o200) },
		"read-only-directory": func(data string) error { return os.Chmod(filepath.Join(data, "state"), 0o500) },
	} {
		t.Run(name, func(t *testing.T) {
			data := fenceFixture(t)
			if err := set(data); err != nil {
				t.Fatal(err)
			}
			defer os.Chmod(filepath.Join(data, "state"), 0o700)
			rep := fenceRun(t, data, nil)
			fenceWant(t, rep, fenceFailClosed)
			if !strings.Contains(rep.Reason, "owner") {
				t.Fatalf("reason: %s", rep.Reason)
			}
			fenceNoSlot(t, data)
		})
	}
}

func TestFenceMountInsideDataFailsClosed(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{statEntry: func(tree, rel string, st *unix.Stat_t) {
		if rel == "workspaces" {
			st.Dev++
		}
	}})
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "a mount inside the data folder") {
		t.Fatalf("reason: %s", rep.Reason)
	}
	fenceNoSlot(t, data)
}

// A file of the data folder with a second name outside it — uv links a workspace venv's files out
// of its cache — is fenced by its inode. Whatever is done through the outside name while the fence
// is up is seen, though no watch here sees that name; with nothing done, the switch commits and the
// copy is a file of its own.
func TestFenceExternalHardLinkIsFencedThroughItsInode(t *testing.T) {
	actions := map[string]func(t *testing.T, outside string){
		"nothing":  nil,
		"truncate": func(t *testing.T, outside string) { fenceTruncate(t, outside) },
		"chmod": func(t *testing.T, outside string) {
			if err := os.Chmod(outside, 0o640); err != nil {
				t.Error(err)
			}
		},
		"setxattr": func(t *testing.T, outside string) {
			if err := unix.Setxattr(outside, "user.late", []byte("x"), 0); err != nil {
				t.Error(err)
			}
		},
		"chattr": func(t *testing.T, outside string) { fenceSetNodump(t, outside) },
	}
	for name, act := range actions {
		t.Run(name, func(t *testing.T) {
			data := fenceFixture(t)
			note := filepath.Join(data, "workspaces", "note.txt")
			outside := filepath.Join(filepath.Dir(data), "outside-link")
			if err := os.Link(note, outside); err != nil {
				t.Fatal(err)
			}
			original := fenceIno(t, data)
			seams := &fenceSeams{}
			if act != nil {
				seams.beforeCFence = func() { act(t, outside) }
			}
			rep := fenceRun(t, data, seams)
			if act == nil {
				fenceWant(t, rep, fenceCommitted)
				var st, pre syscall.Stat_t
				syscall.Stat(note, &st)
				syscall.Stat(filepath.Join(fenceRetainedPath(data, rep, "pre"), "workspaces", "note.txt"), &pre)
				if st.Nlink != 1 || pre.Nlink != 2 || fenceRead(t, note) != "before" {
					t.Fatalf("live nlink %d, kept nlink %d", st.Nlink, pre.Nlink)
				}
				return
			}
			fenceWant(t, rep, fenceRefused)
			if !fenceHasEntry(rep, "P", "workspaces/note.txt", "") || fenceIno(t, data) != original {
				t.Fatalf("the change through the outside name was not caught: %+v", rep.Entries)
			}
		})
	}
	t.Run("write-open", func(t *testing.T) {
		data := fenceFixture(t)
		note := filepath.Join(data, "workspaces", "note.txt")
		outside := filepath.Join(filepath.Dir(data), "outside-link")
		if err := os.Link(note, outside); err != nil {
			t.Fatal(err)
		}
		var child *fenceChild
		rep := fenceRun(t, data, &fenceSeams{duringCopy: func(p *fenceTree) {
			child = startFenceChild(t, "fence-write", "HELPER_PATH="+outside, "HELPER_BODY=LATE!!")
			child.expect(t, "opening", 5*time.Second)
			fenceWaitBroken(t, p, "workspaces/note.txt")
		}})
		fenceWant(t, rep, fenceRefused)
		child.expect(t, "wrote", 10*time.Second)
		if fenceRead(t, note) != "LATE!!" {
			t.Fatal("the write through the outside name did not land in the live data")
		}
	})
}

// Two names for one file inside the data folder (esbuild links its own binary on install): both
// are fenced, the copy holds two files, and the kept tree is removed name by name.
func TestFenceInternalHardLinkIsCopiedAsTwoFiles(t *testing.T) {
	data := fenceFixture(t)
	if err := os.Link(filepath.Join(data, "workspaces", "note.txt"), filepath.Join(data, "state", "note-again")); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	var a, b syscall.Stat_t
	syscall.Stat(filepath.Join(data, "workspaces", "note.txt"), &a)
	syscall.Stat(filepath.Join(data, "state", "note-again"), &b)
	if a.Ino == b.Ino || a.Nlink != 1 || b.Nlink != 1 || fenceRead(t, filepath.Join(data, "state", "note-again")) != "before" {
		t.Fatalf("the copy is not two files: %d/%d %d/%d", a.Ino, a.Nlink, b.Ino, b.Nlink)
	}
	if g := fenceGC(t, data, filepath.Base(fenceRetainedPath(data, rep, "pre")), false, nil); g.Outcome != fenceDeleted {
		t.Fatalf("%+v", g)
	}
}

// open(O_RDONLY|O_TRUNC) through the tree's own name keeps the lease but is seen as IN_MODIFY.
func TestFenceTruncateThroughTheTreeRefuses(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{beforeCFence: func() {
		fenceTruncate(t, filepath.Join(data, "workspaces", "note.txt"))
	}})
	fenceWant(t, rep, fenceRefused)
	if !fenceHasEntry(rep, "P", "workspaces/note.txt", "MODIFY") {
		t.Fatalf("the truncation is not named as MODIFY: %+v", rep.Entries)
	}
}

// A hard link made outside every watched folder after the fence, and a truncation through it:
// no event and no lease break anywhere. Only the size and link count in the sweep see it.
func TestFenceLateHardLinkBeforeTheExchangeRefuses(t *testing.T) {
	data := fenceFixture(t)
	outside := filepath.Join(filepath.Dir(data), "outside-link")
	rep := fenceRun(t, data, &fenceSeams{beforeCFence: func() {
		if err := os.Link(filepath.Join(data, "workspaces", "note.txt"), outside); err != nil {
			t.Error(err)
		}
		fenceTruncate(t, outside)
	}})
	fenceWant(t, rep, fenceRefused)
	if !fenceHasEntry(rep, "P", "workspaces/note.txt", "size, link count") {
		t.Fatalf("the size and link-count change is not named: %+v", rep.Entries)
	}
}

func TestFenceLateHardLinkAfterTheExchangeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	outside := filepath.Join(filepath.Dir(data), "outside-link")
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		if err := os.Link(filepath.Join(filepath.Dir(data), p.root().name, "workspaces", "note.txt"), outside); err != nil {
			t.Error(err)
		}
		fenceTruncate(t, outside)
	}})
	fenceWant(t, rep, fenceRolledBack)
	if !fenceHasEntry(rep, "P", "workspaces/note.txt", "size, link count") {
		t.Fatalf("the size and link-count change is not named: %+v", rep.Entries)
	}
	if fenceIno(t, data) != original {
		t.Fatal("the original is not live again")
	}
}

// A change to the copy before its fence is up is caught by the comparison made under the fence.
func TestFenceCopyChangedBeforeItsFenceRefuses(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{beforeCFence: func() {
		if err := os.WriteFile(filepath.Join(fenceSlotPath(t, data), "workspaces", "note.txt"), []byte("ALTER!"), 0o600); err != nil {
			t.Error(err)
		}
	}})
	fenceWant(t, rep, fenceRefused)
	if rep.Reason != "copy differs" {
		t.Fatalf("reason: %s", rep.Reason)
	}
	if got := fenceRead(t, filepath.Join(data, "workspaces", "note.txt")); got != "before" {
		t.Fatalf("live note %q", got)
	}
}

func TestFenceCopyTruncatedUnderItsFenceRefuses(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{afterCFence: func(c *fenceTree) {
		fenceTruncate(t, filepath.Join(fenceSlotPath(t, data), "workspaces", "note.txt"))
	}})
	fenceWant(t, rep, fenceRefused)
	if got := fenceRead(t, filepath.Join(data, "workspaces", "note.txt")); got != "before" {
		t.Fatalf("live note %q", got)
	}
}

// A writer who arrives through the old tree after sweep 2 is still blocked in open(). The final
// sweep sees it; the rollback puts its write into live data.
func TestFenceWriterBeforeTheFinalSweepRollsBack(t *testing.T) {
	data := fenceFixture(t)
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{afterSweep2: func(p, c *fenceTree) {
		path := filepath.Join(filepath.Dir(data), p.root().name, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, p, "workspaces/note.txt")
	}})
	fenceWant(t, rep, fenceRolledBack)
	child.expect(t, "wrote", 10*time.Second)
	if got := fenceRead(t, filepath.Join(data, "workspaces", "note.txt")); got != "LATE!!" {
		t.Fatalf("the late write is not in the live data: %q", got)
	}
}

// A writer who arrives after the final sweep, before the leases go, reaches the retained tree. The
// commit stands, the report says a late write is possible there, and removal keeps it.
func TestFenceWriterAfterTheFinalSweepIsReported(t *testing.T) {
	data := fenceFixture(t)
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{beforeRelease: func(p, c *fenceTree) {
		path := filepath.Join(fenceControlDir(data), "retained", p.root().name, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, p, "workspaces/note.txt")
	}})
	fenceWant(t, rep, fenceCommitted)
	if len(rep.LateWritePossible) != 1 || rep.LateWritePossible[0].Tree != "pre" || len(rep.LateWritePossible[0].Paths) != 1 || rep.LateWritePossible[0].Paths[0] != "workspaces/note.txt" {
		t.Fatalf("late write not reported: %+v", rep.LateWritePossible)
	}
	child.expect(t, "wrote", 10*time.Second)
	pre := fenceRetainedPath(data, rep, "pre")
	if got := fenceRead(t, filepath.Join(pre, "workspaces", "note.txt")); got != "LATE!!" {
		t.Fatalf("the late write is not in the retained tree: %q", got)
	}
	if g := fenceGC(t, data, filepath.Base(pre), false, nil); g.Outcome != fenceRetainedGC || !strings.Contains(g.Reason, "operator") {
		t.Fatalf("automatic removal of a tree with a possible late write: %+v", g)
	}
	if g := fenceGC(t, data, filepath.Base(pre), true, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the operator's removal ignored the late write: %+v", g)
	}
	text, code := fenceStatus(fenceControlDir(data))
	if code == 0 || !strings.Contains(text, "late write") {
		t.Fatalf("status does not warn: %d %s", code, text)
	}
}

// A process of this user that /proc will not show is not taken to be clean: it is named. Its later
// write into the previous tree is then kept by removal.
func TestFenceUninspectableProcessIsNamed(t *testing.T) {
	data := fenceFixture(t)
	child := startFenceChild(t, "fence-nondumpable", "HELPER_PATH="+filepath.Join(data, "workspaces"))
	child.expect(t, "ready", 5*time.Second)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	named := false
	for _, u := range rep.Unscanned {
		if u.PID == child.pid() && u.Comm != "" {
			named = true
		}
	}
	if !named || len(rep.Limits) == 0 {
		t.Fatalf("the process %d is not in unscanned/limits: %+v %v", child.pid(), rep.Unscanned, rep.Limits)
	}
	child.say(t, "write cwd-late.txt LATE")
	child.expect(t, "ok", 5*time.Second)
	pre := fenceRetainedPath(data, rep, "pre")
	if got := fenceRead(t, filepath.Join(pre, "workspaces", "cwd-late.txt")); got != "LATE" {
		t.Fatalf("the write did not reach the previous tree: %q", got)
	}
	if g := fenceGC(t, data, filepath.Base(pre), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the previous tree with a new file was removed: %+v", g)
	}
}

// A thread with its own working directory inside the tree (unshare(CLONE_FS)) is invisible in
// /proc/<pid>/cwd; the scan looks at every thread.
func TestFenceThreadWorkingInThePreviousTreeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	child := startFenceChild(t, "fence-thread-cwd", "HELPER_PATH="+filepath.Join(data, "workspaces"))
	line := child.expect(t, "ready", 5*time.Second)
	tid, _ := strconv.Atoi(strings.Fields(line)[1])
	if tid == child.pid() {
		t.Fatal("the thread is the thread-group leader: the case needs a second thread")
	}
	if cwd, err := os.Readlink(fmt.Sprintf("/proc/%d/cwd", child.pid())); err != nil || strings.HasPrefix(cwd, data) {
		t.Fatalf("the process's own cwd must be outside the tree for this case: %q %v", cwd, err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceRolledBack)
	found := false
	for _, entry := range rep.Entries {
		if entry.PID == child.pid() && entry.TID == tid && strings.Contains(entry.Cause, "cwd") {
			found = true
		}
	}
	if !found {
		t.Fatalf("the thread %d of %d is not named: %+v", tid, child.pid(), rep.Entries)
	}
	if fenceIno(t, data) != original {
		t.Fatal("the original is not live again")
	}
}

func TestFenceOverflowAfterTheExchangeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		for i := 0; i < 20000; i++ {
			fd, err := unix.Open(filepath.Join(data, "workspaces", fmt.Sprintf("flood-%05d", i)), unix.O_CREAT|unix.O_WRONLY|unix.O_CLOEXEC, 0o600)
			if err != nil {
				t.Error(err)
				return
			}
			unix.Close(fd)
		}
		os.Chmod(data, 0o701)
		os.Chmod(data, 0o700)
	}})
	fenceWant(t, rep, fenceRolledBack)
	if !rep.Overflow {
		t.Fatal("the overflow is not reported")
	}
	if fenceIno(t, data) != original {
		t.Fatal("the original is not live again")
	}
	rejected := fenceRetainedPath(data, rep, "rejected")
	if g := fenceGC(t, data, filepath.Base(rejected), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the flooded rejected copy was removed: %+v", g)
	}
}

// The parent is not watched, so a busy neighbour cannot fill the queue or refuse the switch.
func TestFenceBusySiblingDoesNotRefuse(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		f, err := os.OpenFile(filepath.Join(filepath.Dir(data), "noisy.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			t.Error(err)
			return
		}
		defer f.Close()
		for i := 0; i < 40000; i++ {
			f.Write([]byte("x"))
		}
	}})
	fenceWant(t, rep, fenceCommitted)
}

func TestFenceBareOverflowVoidsTheObservation(t *testing.T) {
	foreign, overflow := fenceClassify([]fenceEvent{{Mask: unix.IN_Q_OVERFLOW}}, nil)
	if !overflow || len(foreign) != 0 {
		t.Fatalf("an overflow must be its own bit, not a foreign event: %v %v", overflow, foreign)
	}
	if !fenceObservationVoid(fenceSweepResult{Overflow: true}) || !fenceSweepFails(fenceSweepResult{Overflow: true}) {
		t.Fatal("an overflow alone did not void the observation")
	}
}

func TestFenceOverflowBeforeTheExchangeRefuses(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{afterCFence: fenceOverflowInto})
	fenceWant(t, rep, fenceRefused)
	if !rep.Overflow || !strings.Contains(rep.Reason, "overflow") {
		t.Fatalf("overflow not reported: %+v", rep)
	}
	if fenceIno(t, data) != original {
		t.Fatal("data switched")
	}
}

func TestFenceInjectedOverflowAfterTheExchangeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) { fenceOverflowInto(c) }})
	fenceWant(t, rep, fenceRolledBack)
	if !rep.Overflow || fenceIno(t, data) != original {
		t.Fatalf("overflow %v, live inode restored %v", rep.Overflow, fenceIno(t, data) == original)
	}
}

func fenceCommitOne(t *testing.T) (string, string) {
	t.Helper()
	data := fenceFixture(t)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	return data, filepath.Base(fenceRetainedPath(data, rep, "pre"))
}

func TestFenceRemovalOverflowBeforeDeletionKeepsTheTree(t *testing.T) {
	data, pre := fenceCommitOne(t)
	g := fenceGC(t, data, pre, false, &fenceSeams{gcAfterTrash: fenceOverflowInto})
	if g.Outcome != fenceRetainedGC || !g.Overflow || g.Deleted != 0 {
		t.Fatalf("removal went on after an overflow: %+v", g)
	}
	if got := fenceRead(t, filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")); got != "before" {
		t.Fatalf("the tree is not back intact: %q", got)
	}
}

func TestFenceRemovalOverflowDuringDeletionIsLoud(t *testing.T) {
	data, pre := fenceCommitOne(t)
	injected := false
	g := fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(r *fenceTree, rel string) {
		if !injected {
			injected = true
			fenceOverflowInto(r)
		}
	}})
	if g.Outcome != fenceLostPossible || !g.Overflow {
		t.Fatalf("an overflow during deletion was not LOST_POSSIBLE: %+v", g)
	}
	if text, code := fenceStatus(fenceControlDir(data)); code == 0 || !strings.Contains(text, "LOST_POSSIBLE") {
		t.Fatalf("status does not say so: %d %s", code, text)
	}
}

func TestFenceSymlinksAreCopiedAsSymlinks(t *testing.T) {
	data := fenceFixture(t)
	if err := os.Mkdir(filepath.Join(data, "links"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("../workspaces", filepath.Join(data, "links", "ws")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("nowhere/at/all", filepath.Join(data, "links", "dangling")); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	for name, target := range map[string]string{"ws": "../workspaces", "dangling": "nowhere/at/all"} {
		path := filepath.Join(data, "links", name)
		info, err := os.Lstat(path)
		if err != nil || info.Mode()&os.ModeSymlink == 0 {
			t.Fatalf("%s is not a symlink: %v %v", name, info, err)
		}
		if got, _ := os.Readlink(path); got != target {
			t.Fatalf("%s points at %q", name, got)
		}
	}
}

func TestFenceRemovalKeepsTheTreeWhenAWriterArrivesBeforeDeletion(t *testing.T) {
	data, pre := fenceCommitOne(t)
	var child *fenceChild
	g := fenceGC(t, data, pre, false, &fenceSeams{gcAfterTrash: func(r *fenceTree) {
		path := filepath.Join(fenceControlDir(data), "trash", pre, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=GCLATE")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, r, "workspaces/note.txt")
	}})
	if g.Outcome != fenceRetainedGC || g.Deleted != 0 {
		t.Fatalf("removal went on with a writer waiting: %+v", g)
	}
	child.expect(t, "wrote", 10*time.Second)
	if got := fenceRead(t, filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")); got != "GCLATE" {
		t.Fatalf("the writer's data is not in the kept tree: %q", got)
	}
}

func TestFenceRemovalReportsALossWhenAWriterArrivesAtTheUnlink(t *testing.T) {
	data, pre := fenceCommitOne(t)
	var child *fenceChild
	g := fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(r *fenceTree, rel string) {
		if rel != "workspaces/note.txt" {
			return
		}
		path := filepath.Join(fenceControlDir(data), "trash", pre, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=GCLATE")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, r, "workspaces/note.txt")
	}})
	if g.Outcome != fenceLostPossible {
		t.Fatalf("a write into a file being removed was not reported: %+v", g)
	}
	child.expect(t, "wrote", 10*time.Second)
	if text, code := fenceStatus(fenceControlDir(data)); code == 0 || !strings.Contains(text, "LOST_POSSIBLE") {
		t.Fatalf("status does not say so: %d %s", code, text)
	}
}

func TestFenceRemovalKeepsTheTreeForANewMappedWriter(t *testing.T) {
	data, pre := fenceCommitOne(t)
	var child *fenceChild
	g := fenceGC(t, data, pre, false, &fenceSeams{gcAfterTrash: func(r *fenceTree) {
		path := filepath.Join(fenceControlDir(data), "trash", pre, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-map-write", "HELPER_PATH="+path, "HELPER_BODY=NEWMAP")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, r, "workspaces/note.txt")
	}})
	if g.Outcome != fenceRetainedGC {
		t.Fatalf("removal went on with a mapped writer waiting: %+v", g)
	}
	child.expect(t, "wrote", 10*time.Second)
	if got := fenceRead(t, filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")); got != "NEWMAP" {
		t.Fatalf("the mapped write is not in the kept tree: %q", got)
	}
}

func fenceSecondaryGroup(t *testing.T) int {
	t.Helper()
	groups, _ := os.Getgroups()
	for _, g := range groups {
		if g != os.Getgid() {
			return g
		}
	}
	t.Skip("this user has no second group: the group case cannot be shown here")
	return 0
}

func TestFenceRemovalKeepsATreeWhoseGroupOrAttributesChanged(t *testing.T) {
	t.Run("attribute", func(t *testing.T) {
		data, pre := fenceCommitOne(t)
		note := filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")
		if err := unix.Setxattr(note, "user.late", []byte("x"), 0); err != nil {
			t.Fatal(err)
		}
		if g := fenceGC(t, data, pre, false, nil); g.Outcome != fenceRetainedGC {
			t.Fatalf("a changed attribute was removed silently: %+v", g)
		}
	})
	t.Run("group", func(t *testing.T) {
		gid := fenceSecondaryGroup(t)
		data, pre := fenceCommitOne(t)
		note := filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")
		if err := os.Lchown(note, -1, gid); err != nil {
			t.Fatal(err)
		}
		if g := fenceGC(t, data, pre, false, nil); g.Outcome != fenceRetainedGC {
			t.Fatalf("a changed group was removed silently: %+v", g)
		}
	})
}

func TestFenceCopyKeepsAttributesAndACLs(t *testing.T) {
	if _, err := exec.LookPath("setfacl"); err != nil {
		t.Skip("setfacl is not installed")
	}
	data := fenceFixture(t)
	note := filepath.Join(data, "workspaces", "note.txt")
	if err := unix.Setxattr(note, "user.note", []byte("hello"), 0); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"-m", "u:65534:r", note}, {"-d", "-m", "u:65534:rx", filepath.Join(data, "workspaces")}} {
		if out, err := exec.Command("setfacl", args...).CombinedOutput(); err != nil {
			t.Fatalf("setfacl %v: %v %s", args, err, out)
		}
	}
	want := func(path, name string) []byte {
		buf := make([]byte, 4096)
		n, err := unix.Lgetxattr(path, name, buf)
		if err != nil {
			t.Fatalf("%s %s: %v", path, name, err)
		}
		return buf[:n]
	}
	originals := map[string][]byte{
		"note user.note":     want(note, "user.note"),
		"note access acl":    want(note, "system.posix_acl_access"),
		"workspaces default": want(filepath.Join(data, "workspaces"), "system.posix_acl_default"),
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
	copies := map[string][]byte{
		"note user.note":     want(note, "user.note"),
		"note access acl":    want(note, "system.posix_acl_access"),
		"workspaces default": want(filepath.Join(data, "workspaces"), "system.posix_acl_default"),
	}
	for key, value := range originals {
		if string(copies[key]) != string(value) {
			t.Fatalf("%s differs on the live copy", key)
		}
	}
}

func TestFenceUnavailableGroupFailsClosed(t *testing.T) {
	gid := fenceSecondaryGroup(t)
	data := fenceFixture(t)
	note := filepath.Join(data, "workspaces", "note.txt")
	if err := os.Lchown(note, -1, gid); err != nil {
		t.Fatal(err)
	}
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{fchownGroup: func(rel string) error {
		if rel == "workspaces/note.txt" {
			return unix.EPERM
		}
		return nil
	}})
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "group") || !strings.Contains(rep.Reason, "not available") {
		t.Fatalf("reason: %s", rep.Reason)
	}
	if fenceIno(t, data) != original {
		t.Fatal("data switched")
	}
}

// chattr through a read-only descriptor gives no event and breaks no lease; the flags check sees it.
func TestFenceFlagChangeAfterTheExchangeRollsBack(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		fenceSetNodump(t, filepath.Join(data, "workspaces", "note.txt"))
	}})
	fenceWant(t, rep, fenceRolledBack)
	if !fenceHasEntry(rep, "C", "workspaces/note.txt", "inode flags changed") {
		t.Fatalf("the flag change is not named: %+v", rep.Entries)
	}
}

func TestFenceFlagChangeBeforeTheExchangeRefuses(t *testing.T) {
	data := fenceFixture(t)
	rep := fenceRun(t, data, &fenceSeams{afterCompare: func(p, c *fenceTree) {
		fenceSetNodump(t, filepath.Join(fenceSlotPath(t, data), "workspaces", "note.txt"))
	}})
	fenceWant(t, rep, fenceRefused)
	if !fenceHasEntry(rep, "C", "workspaces/note.txt", "inode flags changed") {
		t.Fatalf("the flag change is not named: %+v", rep.Entries)
	}
}

// A process of this user exchanges the trees back behind the switch. The switch must see that data
// already holds the original, must not exchange again, and must keep the copy for the operator only.
func TestFenceForeignReverseExchangeFailsClosed(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{afterExchange: func(p, c *fenceTree) {
		slot := filepath.Join(filepath.Dir(data), p.root().name)
		if err := unix.Renameat2(unix.AT_FDCWD, data, unix.AT_FDCWD, slot, unix.RENAME_EXCHANGE); err != nil {
			t.Error(err)
		}
	}})
	fenceWant(t, rep, fenceFailClosed)
	if fenceIno(t, data) != original {
		t.Fatal("data does not hold the original by inode")
	}
	var copyKept fenceRetained
	for _, r := range rep.Retained {
		if r.Kind == "rejected" {
			copyKept = r
		}
	}
	if copyKept.GC != "manual" {
		t.Fatalf("the copy is not kept for the operator: %+v", rep.Retained)
	}
	if g := fenceGC(t, data, filepath.Base(copyKept.Path), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("automatic removal after a foreign exchange: %+v", g)
	}
	next := fenceRun(t, data, nil)
	if next.Outcome != fenceFailClosed || !strings.Contains(next.Reason, "earlier switch") {
		t.Fatalf("a new switch started while the last one needs the operator: %+v", next)
	}
}

func TestFenceForeignExchangeBeforeRetentionFailsClosed(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	var copyIno uint64
	rep := fenceRun(t, data, &fenceSeams{afterFinalSweep: func(p, c *fenceTree) {
		copyIno = c.root().ino
		slot := filepath.Join(filepath.Dir(data), p.root().name)
		if err := unix.Renameat2(unix.AT_FDCWD, data, unix.AT_FDCWD, slot, unix.RENAME_EXCHANGE); err != nil {
			t.Error(err)
		}
	}})
	fenceWant(t, rep, fenceFailClosed)
	if fenceIno(t, data) != original {
		t.Fatal("data does not hold the original by inode")
	}
	entries, _ := os.ReadDir(filepath.Join(fenceControlDir(data), "retained"))
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		ino := fenceIno(t, filepath.Join(fenceControlDir(data), "retained", entry.Name()))
		if strings.HasPrefix(entry.Name(), "pre-") {
			t.Fatalf("%s was filed as the previous tree", entry.Name())
		}
		if strings.HasPrefix(entry.Name(), "rejected-") && ino != copyIno {
			t.Fatalf("%s is not our copy", entry.Name())
		}
	}
}

func TestFenceParentRenamedDuringTheSwitch(t *testing.T) {
	for _, after := range []bool{false, true} {
		t.Run("after-exchange="+strconv.FormatBool(after), func(t *testing.T) {
			data := fenceFixture(t)
			original := fenceIno(t, data)
			parent := filepath.Dir(data)
			moved := parent + "-moved"
			move := func() {
				if err := os.Rename(parent, moved); err != nil {
					t.Error(err)
				}
			}
			seams := &fenceSeams{}
			if after {
				seams.afterExchange = func(p, c *fenceTree) { move() }
			} else {
				seams.afterCFence = func(c *fenceTree) { move() }
			}
			rep := fencedSwitch(fenceOptions{Data: data, seams: seams})
			defer os.Rename(moved, parent)
			want := fenceFailClosed
			if after {
				want = fenceRolledBack
			}
			if rep.Outcome != want {
				t.Fatalf("outcome %s, want %s: %s %+v", rep.Outcome, want, rep.Reason, rep.Entries)
			}
			if fenceIno(t, filepath.Join(moved, "data")) != original {
				t.Fatal("the original is not at data")
			}
		})
	}
}

func TestFenceDescriptorLimitFailsClosedWithAReport(t *testing.T) {
	if os.Getenv("DAEDALUS_FENCE_EMFILE_CHILD") == "" {
		cmd := exec.Command(os.Args[0], "-test.run=^TestFenceDescriptorLimitFailsClosedWithAReport$", "-test.v")
		cmd.Env = append(os.Environ(), "DAEDALUS_FENCE_EMFILE_CHILD=1")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("child: %v\n%s", err, out)
		}
		if strings.Contains(string(out), "--- SKIP") {
			t.Skipf("child skipped:\n%s", out)
		}
		return
	}
	data := fenceFixture(t)
	for n := 0; n < 82; n++ {
		if err := os.WriteFile(filepath.Join(data, "workspaces", fmt.Sprintf("file-%03d", n)), []byte("kept"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := unix.Setrlimit(unix.RLIMIT_NOFILE, &unix.Rlimit{Cur: 64, Max: 64}); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "RLIMIT_NOFILE") {
		t.Fatalf("reason: %s", rep.Reason)
	}
	fenceNoSlot(t, data)
}

func TestFenceWatchLimitFailsClosedWithAReport(t *testing.T) {
	data := fenceFixture(t)
	original := fenceIno(t, data)
	rep := fenceRun(t, data, &fenceSeams{addWatch: func(tree, rel string) error {
		if tree == "P" && rel == "state" {
			return unix.ENOSPC
		}
		return nil
	}})
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "cannot be watched") || fenceIno(t, data) != original {
		t.Fatalf("reason %s", rep.Reason)
	}
	fenceNoSlot(t, data)
}

func TestFenceRefusesWhereTheFenceIsUnproven(t *testing.T) {
	cases := map[string]func(*fenceOptions){
		"root":            func(o *fenceOptions) { o.seams.euid = func() int { return 0 } },
		"container":       func(o *fenceOptions) { o.seams.container = func() bool { return true } },
		"docker-backend":  func(o *fenceOptions) { o.BackendInDocker = true },
		"cap-lease":       func(o *fenceOptions) { o.seams.capLease = func() bool { return true } },
		"leases-disabled": func(o *fenceOptions) { o.seams.leasesEnabled = func() bool { return false } },
		"relative-path":   func(o *fenceOptions) { o.Data = "data" },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			data := fenceFixture(t)
			opts := fenceOptions{Data: data, seams: &fenceSeams{}}
			change(&opts)
			rep := fencedSwitch(opts)
			if rep.Outcome != fenceFailClosed || rep.ExitCode != 5 || rep.LeasesTaken != 0 {
				t.Fatalf("%+v", rep)
			}
			if _, err := os.Stat(fenceControlDir(data)); !os.IsNotExist(err) {
				t.Fatal("something was created before the refusal")
			}
		})
	}
	t.Run("data-is-a-symlink", func(t *testing.T) {
		data := fenceFixture(t)
		real := data + "-real"
		if err := os.Rename(data, real); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(real, data); err != nil {
			t.Fatal(err)
		}
		if rep := fencedSwitch(fenceOptions{Data: data}); rep.Outcome != fenceFailClosed {
			t.Fatalf("%+v", rep)
		}
	})
}

// Run inside a container (the golang image of the gate), the real detection must refuse.
func TestFenceRefusesInsideARealContainer(t *testing.T) {
	if !fenceInContainer() {
		t.Skip("not inside a container")
	}
	data := filepath.Join(t.TempDir(), "data")
	os.Mkdir(data, 0o700)
	// The gate's container runs as root, which is refused first; with root set aside, the real
	// detection must be what refuses.
	rep := fencedSwitch(fenceOptions{Data: data, seams: &fenceSeams{euid: func() int { return 1000 }}})
	if rep.Outcome != fenceFailClosed || !strings.Contains(rep.Reason, "container") {
		t.Fatalf("%+v", rep)
	}
	if rep := fencedSwitch(fenceOptions{Data: data}); rep.Outcome != fenceFailClosed {
		t.Fatalf("%+v", rep)
	}
}

func TestFenceForeignOwnerFailsClosed(t *testing.T) {
	for _, rel := range []string{"workspaces", "links/ws"} {
		t.Run(strings.ReplaceAll(rel, "/", "-"), func(t *testing.T) {
			data := fenceFixture(t)
			os.Mkdir(filepath.Join(data, "links"), 0o700)
			os.Symlink("../workspaces", filepath.Join(data, "links", "ws"))
			rep := fenceRun(t, data, &fenceSeams{statEntry: func(tree, r string, st *unix.Stat_t) {
				if r == rel {
					st.Uid = 65534
				}
			}})
			fenceWant(t, rep, fenceFailClosed)
			if !strings.Contains(rep.Reason, "owned by uid 65534") {
				t.Fatalf("reason: %s", rep.Reason)
			}
			fenceNoSlot(t, data)
		})
	}
}

// With DAEDALUS_FENCE_SUDO=1 the same is shown with a real chown, through sudo, on the scratch
// fixture only; the entries are left empty so this user can remove them afterwards.
func TestFenceRealForeignOwnerFailsClosed(t *testing.T) {
	if os.Getenv("DAEDALUS_FENCE_SUDO") == "" {
		t.Skip("set DAEDALUS_FENCE_SUDO=1 to chown fixture entries through sudo")
	}
	for _, kind := range []string{"dir", "symlink"} {
		t.Run(kind, func(t *testing.T) {
			data := fenceFixture(t)
			path := filepath.Join(data, "foreign-"+kind)
			args := []string{"-n", "chown", "65534", path}
			if kind == "dir" {
				os.Mkdir(path, 0o755)
			} else {
				os.Symlink("workspaces", path)
				args = []string{"-n", "chown", "-h", "65534", path}
			}
			if out, err := exec.Command("sudo", args...).CombinedOutput(); err != nil {
				t.Fatalf("sudo chown: %v %s", err, out)
			}
			rep := fenceRun(t, data, nil)
			fenceWant(t, rep, fenceFailClosed)
			if !strings.Contains(rep.Reason, "owned by uid 65534") {
				t.Fatalf("reason: %s", rep.Reason)
			}
		})
	}
}

// With DAEDALUS_FENCE_SUDO=1 this test binary is run as root on a scratch fixture.
func TestFenceRealRootFailsClosed(t *testing.T) {
	if os.Getenv("DAEDALUS_FENCE_AS_ROOT") != "" {
		data := fenceFixture(t)
		rep := fencedSwitch(fenceOptions{Data: data})
		if rep.Outcome != fenceFailClosed || !strings.Contains(rep.Reason, "root") {
			t.Fatalf("%+v", rep)
		}
		if _, err := os.Stat(fenceControlDir(data)); !os.IsNotExist(err) {
			t.Fatal("root created the control folder")
		}
		return
	}
	if os.Getenv("DAEDALUS_FENCE_SUDO") == "" {
		t.Skip("set DAEDALUS_FENCE_SUDO=1 to run this binary as root through sudo")
	}
	cmd := exec.Command("sudo", "-n", "env", "DAEDALUS_FENCE_AS_ROOT=1", "TMPDIR="+os.TempDir(), os.Args[0], "-test.run=^TestFenceRealRootFailsClosed$", "-test.v")
	out, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(out), "--- PASS") {
		t.Fatalf("as root: %v\n%s", err, out)
	}
}

func TestFenceUnresolvedUpgradeJournalFailsClosed(t *testing.T) {
	data := fenceFixture(t)
	os.Mkdir(filepath.Join(data, "upgrade"), 0o700)
	os.WriteFile(filepath.Join(data, "upgrade", "journal.json"), []byte(`{"kind":"upgrade","stage":"swapped"}`), 0o600)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "upgrade --rollback") {
		t.Fatalf("reason: %s", rep.Reason)
	}
}

func TestFenceSecondSwitchIsRefused(t *testing.T) {
	data := fenceFixture(t)
	locks := filepath.Join(fenceControlDir(data), "locks")
	os.MkdirAll(locks, 0o700)
	os.Chmod(fenceControlDir(data), 0o700)
	f, err := os.OpenFile(filepath.Join(locks, "update.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceRefused)
	if !strings.Contains(rep.Reason, "another switch") || rep.LeasesTaken != 0 {
		t.Fatalf("%+v", rep)
	}
}

func fenceLegacyLocks(t *testing.T, data string) {
	t.Helper()
	os.Mkdir(filepath.Join(data, "upgrade"), 0o700)
	for _, name := range []string{lockName, finishLockName} {
		if err := os.WriteFile(filepath.Join(data, "upgrade", name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

// The launcher's lock files inside the data folder, free: the switch holds them read-only and still fences.
func TestFenceFreeLegacyLockCommits(t *testing.T) {
	data := fenceFixture(t)
	fenceLegacyLocks(t, data)
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceCommitted)
}

// Between the release of the leases and the release of the flocks, a launcher's lock attempt on either
// tree gets errLocked, and the outcome stands.
func TestFenceLegacyLockIsHeldUntilTheLeasesAreGone(t *testing.T) {
	data := fenceFixture(t)
	fenceLegacyLocks(t, data)
	var results []string
	rep := fenceRun(t, data, &fenceSeams{afterLeaseRelease: func() {
		pre, _ := filepath.Glob(filepath.Join(fenceControlDir(data), "retained", "pre-*"))
		paths := []string{filepath.Join(data, "upgrade", lockName)}
		for _, p := range pre {
			if !strings.HasSuffix(p, ".json") {
				paths = append(paths, filepath.Join(p, "upgrade", lockName))
			}
		}
		for _, path := range paths {
			child := startFenceChild(t, "fence-launcher-lock", "HELPER_PATH="+path)
			select {
			case line := <-child.lines:
				results = append(results, line)
			case <-time.After(5 * time.Second):
				results = append(results, "timeout")
			}
		}
	}})
	fenceWant(t, rep, fenceCommitted)
	if len(results) != 2 || results[0] != "errLocked" || results[1] != "errLocked" {
		t.Fatalf("lock attempts on the live and the retained lock: %v", results)
	}
}

func TestFenceRunningLauncherOrUpgradeRefusesAtOnce(t *testing.T) {
	for kind, says := range map[string]string{"upgrade": "an upgrade is in progress", "launcher": "a launcher is running"} {
		t.Run(kind, func(t *testing.T) {
			data := fenceFixture(t)
			child := startFenceChild(t, "hold-lock", "HELPER_DATA="+data, "HELPER_KIND="+kind)
			child.expect(t, "locked", 5*time.Second)
			start := time.Now()
			rep := fenceRun(t, data, nil)
			elapsed := time.Since(start)
			fenceWant(t, rep, fenceRefused)
			if !strings.Contains(rep.Reason, says) || rep.LeasesTaken != 0 || elapsed > time.Second {
				t.Fatalf("reason %q, leases %d, %s", rep.Reason, rep.LeasesTaken, elapsed)
			}
		})
	}
}

// A lock attempt inside the fence. This launcher's own lock call opens the file read-only: it breaks
// no lease, finds the lock taken at once, and the switch goes on undisturbed. A build that still
// opened its lock read-write breaks the lease on the lock file; the switch answers at once and that
// process too finds the lock taken.
func TestFenceLegacyLockAttemptInsideTheFence(t *testing.T) {
	cases := []struct {
		helper string
		after  bool
		want   string
	}{
		{"fence-launcher-lock", false, fenceCommitted},
		{"fence-launcher-lock", true, fenceCommitted},
		{"fence-readwrite-lock", false, fenceRefused},
		{"fence-readwrite-lock", true, fenceRolledBack},
	}
	for _, c := range cases {
		t.Run(c.helper+"/after-exchange="+strconv.FormatBool(c.after), func(t *testing.T) {
			data := fenceFixture(t)
			fenceLegacyLocks(t, data)
			original := fenceIno(t, data)
			lock := filepath.Join(data, "upgrade", lockName)
			var child *fenceChild
			var reached time.Time
			attempt := func(tree *fenceTree) {
				child = startFenceChild(t, c.helper, "HELPER_PATH="+lock)
				if c.helper == "fence-readwrite-lock" {
					fenceWaitBroken(t, tree, "upgrade/"+lockName)
				}
				reached = time.Now()
			}
			seams := &fenceSeams{}
			if c.after {
				seams.afterExchange = func(p, c *fenceTree) { attempt(c) }
			} else {
				seams.duringCopy = func(p *fenceTree) { attempt(p) }
			}
			rep := fenceRun(t, data, seams)
			fenceWant(t, rep, c.want)
			var line string
			select {
			case line = <-child.lines:
			case <-time.After(10 * time.Second):
				t.Fatal("the lock attempt never returned")
			}
			answered := time.Since(reached)
			if line != "errLocked" {
				t.Fatalf("the lock attempt got %q", line)
			}
			if c.want != fenceCommitted && fenceIno(t, data) != original {
				t.Fatal("the original is not live")
			}
			t.Logf("the lock attempt was answered %s after it reached the fence", answered)
			if answered > 2*time.Second {
				t.Fatalf("the lock attempt waited %s", answered)
			}
		})
	}
}

// A v0.12 launcher knows nothing of the fence or of the runtime's new place. What it does to the
// data folder on a start — its instance file, and the runtime folder and log it recreates inside
// the data folder — is seen before the exchange (REFUSED) and after it (ROLLED_BACK). v0.12 never
// looks outside the data folder, so the control folder is out of its reach; and a runtime folder it
// leaves behind is refused by the next switch outright.
func TestFenceOldLauncherActingDuringTheSwitch(t *testing.T) {
	actions := map[string]func(data string) error{
		"instance-file": func(data string) error {
			return os.WriteFile(filepath.Join(data, "launcher.json"), []byte(`{"port":1,"token":"t","pid":1}`), 0o600)
		},
		"runtime-recreated": func(data string) error {
			if err := os.MkdirAll(filepath.Join(data, "runtime", "logs"), 0o755); err != nil {
				return err
			}
			return os.WriteFile(filepath.Join(data, "runtime", "logs", "supervisor.log"), []byte("started\n"), 0o644)
		},
		"env-replaced": func(data string) error {
			// Written aside and renamed over: an in-place write from this very process would wait on
			// its own lease.
			if err := os.WriteFile(filepath.Join(data, ".env.new"), []byte("API_PORT=2\n"), 0o600); err != nil {
				return err
			}
			return os.Rename(filepath.Join(data, ".env.new"), filepath.Join(data, ".env"))
		},
	}
	for name, action := range actions {
		for _, after := range []bool{false, true} {
			t.Run(name+"/after-exchange="+strconv.FormatBool(after), func(t *testing.T) {
				data := fenceFixture(t)
				original := fenceIno(t, data)
				seams := &fenceSeams{}
				want := fenceRefused
				act := func() {
					if err := action(data); err != nil && !errors.Is(err, os.ErrNotExist) {
						t.Error(err)
					}
				}
				if after {
					want = fenceRolledBack
					seams.afterExchange = func(p, c *fenceTree) { act() }
				} else {
					seams.beforeCFence = act
				}
				rep := fenceRun(t, data, seams)
				fenceWant(t, rep, want)
				if fenceIno(t, data) != original {
					t.Fatal("the original is not live")
				}
			})
		}
	}
}

// A runtime folder inside the data folder is refused by name, with the way out in the reason.
func TestFenceRefusesADataFolderThatStillHoldsTheRuntime(t *testing.T) {
	data := fenceFixture(t)
	if err := os.MkdirAll(filepath.Join(data, "runtime", "envs"), 0o700); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, nil)
	fenceWant(t, rep, fenceFailClosed)
	if !strings.Contains(rep.Reason, "runtime/") || !strings.Contains(rep.Reason, "moves them out") || rep.LeasesTaken != 0 {
		t.Fatalf("%+v", rep)
	}
	fenceNoSlot(t, data)
}

// A running launcher holds its installation lock for as long as it runs; it runs the switch itself,
// after stopping its stack. Without a handover its own lock refuses the switch. With it, the switch
// commits, the launcher ends up holding the lock of the tree that is now live, and another launcher
// finds the live lock taken until the launcher lets go.
func TestFenceARunningLauncherHandsItsLockToTheSwitch(t *testing.T) {
	data := fenceFixture(t)
	fenceLegacyLocks(t, data)
	p, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	lock, err := AcquireLock(p, "launcher")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()
	without := fenceRun(t, data, nil)
	fenceWant(t, without, fenceRefused)
	if !strings.Contains(without.Reason, "a launcher is running") {
		t.Fatalf("reason: %s", without.Reason)
	}
	handed := false
	rep := fencedSwitch(fenceOptions{Data: data,
		Held: map[string]*os.File{lockName: lock.file},
		Handover: func(files map[string]*os.File) {
			handed = true
			lock.follow(files[lockName])
		}})
	fenceWant(t, rep, fenceCommitted)
	if !handed {
		t.Fatal("the live tree's lock was not handed over")
	}
	var live, held unix.Stat_t
	if unix.Stat(filepath.Join(data, "upgrade", lockName), &live) != nil || unix.Fstat(int(lock.file.Fd()), &held) != nil || live.Ino != held.Ino {
		t.Fatal("the launcher does not hold the live tree's lock file")
	}
	another := func() string {
		child := startFenceChild(t, "fence-launcher-lock", "HELPER_PATH="+filepath.Join(data, "upgrade", lockName))
		select {
		case line := <-child.lines:
			return line
		case <-time.After(5 * time.Second):
			return "timeout"
		}
	}
	if got := another(); got != "errLocked" {
		t.Fatalf("another launcher took the live lock while this one runs: %s", got)
	}
	lock.Release()
	if got := another(); got != "locked" {
		t.Fatalf("after the launcher let go the lock is still taken: %s", got)
	}
}

// A lease-break signal that arrives while the copy is measuring its own files is still a waiting
// writer: REFUSED, never a broken fence.
func TestASignalDuringTheCopysMeasurementIsARefusal(t *testing.T) {
	s := &fenceSwitch{rep: &fenceReport{}}
	s.fromProblem(fenceFail("C", ".env", "cannot be measured", errFenceSignal), "copying")
	if s.rep.Outcome != fenceRefused {
		t.Fatalf("%s: %s", s.rep.Outcome, s.rep.Reason)
	}
}
