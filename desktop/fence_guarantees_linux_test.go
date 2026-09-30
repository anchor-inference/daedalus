//go:build linux

package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// A name given to a file of the tree from outside it produces no event on any watch of the tree;
// only the link count says someone reached the file. Before its unlink, a removal checks it, and
// keeps the tree.
func TestARemovalKeepsATreeWhoseFileGotANameFromOutside(t *testing.T) {
	data, pre := fenceCommitOne(t)
	outside := filepath.Join(t.TempDir(), "second-name")
	linked := false
	g := fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(r *fenceTree, rel string) {
		if linked {
			return
		}
		linked = true
		// A file of the tree that has not been unlinked yet: the database, which sorts late.
		target := filepath.Join(fenceControlDir(data), "trash", pre, "state", "daedalus.sqlite")
		if err := os.Link(target, outside); err != nil {
			t.Errorf("link: %v", err)
		}
	}})
	if g.Outcome == fenceDeleted {
		t.Fatalf("a tree whose file gained a name from outside was removed: %+v", g)
	}
}

// A file emptied through a name outside the tree — open(O_RDONLY|O_TRUNC) keeps the lease and gives
// no event on the tree's watches — before the leases go is still found when they go: the kept copy
// is reported as reached by a late write, and is not removed automatically.
func TestAFileEmptiedFromOutsideBeforeReleaseIsReported(t *testing.T) {
	data := fenceFixture(t)
	outside := filepath.Join(t.TempDir(), "second-name")
	if err := os.Link(filepath.Join(data, "workspaces", "note.txt"), outside); err != nil {
		t.Fatal(err)
	}
	rep := fenceRun(t, data, &fenceSeams{beforeRelease: func(p, c *fenceTree) {
		fd, err := unix.Open(outside, unix.O_RDONLY|unix.O_TRUNC|unix.O_CLOEXEC, 0)
		if err != nil {
			t.Errorf("truncate: %v", err)
			return
		}
		unix.Close(fd)
	}})
	fenceWant(t, rep, fenceCommitted)
	if len(rep.LateWritePossible) != 1 || rep.LateWritePossible[0].Tree != "pre" || strings.Join(rep.LateWritePossible[0].Paths, ",") != "workspaces/note.txt" {
		t.Fatalf("the emptied file is not reported: %+v", rep.LateWritePossible)
	}
	if g := fenceGC(t, data, filepath.Base(fenceRetainedPath(data, rep, "pre")), false, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("removed automatically: %+v", g)
	}
}

// A kept copy whose file was rewritten in place — same size, and the modification time put back —
// differs from its record only in its content, and that is enough to keep it.
func TestARemovalKeepsATreeWhoseContentAloneChanged(t *testing.T) {
	data, pre := fenceCommitOne(t)
	path := filepath.Join(fenceControlDir(data), "retained", pre, "workspaces", "note.txt")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("BEFORE"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, info.ModTime(), info.ModTime()); err != nil {
		t.Fatal(err)
	}
	if g := fenceGC(t, data, pre, false, nil); g.Outcome != fenceRetainedGC || !strings.Contains(g.Reason, "changed") {
		t.Fatalf("a tree with changed content was removed: %+v", g)
	}
}

// A writer that reaches a file after the removal unlinked it — through a descriptor path of this
// very process, the one way left to an unlinked file — breaks its lease. The last check after the
// last unlink sees it and says a write may have been lost.
func TestARemovalSeesAWriterAfterItsLastUnlink(t *testing.T) {
	data, pre := fenceCommitOne(t)
	var child *fenceChild
	g := fenceGC(t, data, pre, false, &fenceSeams{gcAfterDelete: func(r *fenceTree) {
		node := fenceNodeOf(t, r, "workspaces/note.txt")
		path := filepathOfFD(node.file.Fd())
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=GONE")
		child.expect(t, "opening", 30*time.Second)
		fenceWaitBroken(t, r, "workspaces/note.txt")
	}})
	if g.Outcome != fenceLostPossible {
		t.Fatalf("a write into an unlinked file was not reported: %+v", g)
	}
	child.expect(t, "wrote", fenceLeaseBreakTime(t)+30*time.Second)
}

func filepathOfFD(fd uintptr) string {
	return "/proc/" + strconv.Itoa(os.Getpid()) + "/fd/" + strconv.Itoa(int(fd))
}
