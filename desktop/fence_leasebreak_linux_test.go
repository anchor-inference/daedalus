//go:build linux

package main

import (
	"context"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// These tests wait out the kernel's own lease-break-time (45 s on this host) instead of changing
// it: the setting is global, and lowering it would change every lease on the machine. They run
// only with DAEDALUS_FENCE_LONG=1.

func fenceLeaseBreakTime(t *testing.T) time.Duration {
	t.Helper()
	body, err := os.ReadFile("/proc/sys/fs/lease-break-time")
	if err != nil {
		t.Fatal(err)
	}
	seconds, err := strconv.Atoi(strings.TrimSpace(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	return time.Duration(seconds) * time.Second
}

func fenceLong(t *testing.T) time.Duration {
	t.Helper()
	if os.Getenv("DAEDALUS_FENCE_LONG") == "" {
		t.Skip("waits out the kernel's lease-break-time; set DAEDALUS_FENCE_LONG=1")
	}
	return fenceLeaseBreakTime(t)
}

func fenceMeasure(t *testing.T, name string, values map[string]any) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"measure": name, "values": values})
	t.Logf("%s", body)
	if out := os.Getenv("DAEDALUS_FENCE_MEASURE_OUT"); out != "" {
		f, err := os.OpenFile(out, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err == nil {
			f.Write(append(body, '\n'))
			f.Close()
		}
	}
}

// What the kernel does when a lease holder does not answer: the writer is held for exactly
// lease-break-time, then the lease is taken away and the write goes through. What matters for the
// fence is that the loss of the lease stays visible afterwards — F_GETLEASE keeps saying F_UNLCK on
// the holder's descriptor, and the sweep reports it — so a switch that stalled (swapped out,
// stopped, a slow disk) past lease-break-time finds out instead of committing.
func TestFenceLeaseBreakTimeExpiryStaysVisible(t *testing.T) {
	breakTime := fenceLong(t)
	data := fenceFixture(t)
	parent, err := os.Open(filepath.Dir(data))
	if err != nil {
		t.Fatal(err)
	}
	defer parent.Close()
	leases := 0
	tree, err := fenceOpenTree("P", parent, "data", os.Geteuid(), nil, &leases)
	if err != nil {
		t.Fatal(err)
	}
	defer tree.close()
	if _, err := tree.manifest(nil); err != nil {
		t.Fatal(err)
	}
	note := filepath.Join(data, "workspaces", "note.txt")
	signals := fenceSIGIO()
	child := startFenceChild(t, "fence-write", "HELPER_PATH="+note, "HELPER_BODY=LATE!!")
	child.expect(t, "opening", 5*time.Second)
	start := time.Now()
	brokenAfter := fenceWaitBroken(t, tree, "workspaces/note.txt")
	for fenceSIGIO() == signals && time.Since(start) < 5*time.Second {
		time.Sleep(time.Millisecond)
	}
	signalAfter := time.Since(start)
	if fenceSIGIO() == signals {
		t.Fatal("no SIGIO reached the lease holder")
	}
	during := tree.sweep(nil)
	if got := fenceRead(t, note); got != "before" {
		t.Fatalf("the writer got through before the break time: %q", got)
	}
	line := child.expect(t, "wrote", breakTime+30*time.Second)
	held := time.Since(start)
	blocked, _ := strconv.Atoi(strings.Fields(line)[1])
	node := fenceNodeOf(t, tree, "workspaces/note.txt")
	lease, _ := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0)
	after := tree.sweep(nil)
	if got := fenceRead(t, note); got != "LATE!!" {
		t.Fatalf("after the break time the write should have gone through: %q", got)
	}
	if lease != unix.F_UNLCK {
		t.Fatalf("after the kernel took the lease away F_GETLEASE says %d, not F_UNLCK", lease)
	}
	if !fenceHasEntry(fenceReport{Entries: during.Entries}, "P", "workspaces/note.txt", "lease broken") {
		t.Fatalf("the sweep during the break missed it: %+v", during.Entries)
	}
	if !fenceHasEntry(fenceReport{Entries: after.Entries}, "P", "workspaces/note.txt", "lease broken") ||
		!fenceHasEntry(fenceReport{Entries: after.Entries}, "P", "workspaces/note.txt", "MODIFY") {
		t.Fatalf("the sweep after the expiry missed it: %+v", after.Entries)
	}
	if blocked < int((breakTime - 2*time.Second).Milliseconds()) {
		t.Fatalf("the writer was held %d ms, less than lease-break-time %s", blocked, breakTime)
	}
	fenceMeasure(t, "lease-break-time expiry", map[string]any{
		"lease_break_time_s":       breakTime.Seconds(),
		"lease_shown_broken_ms":    brokenAfter.Milliseconds(),
		"sigio_seen_ms":            signalAfter.Milliseconds(),
		"writer_blocked_ms":        blocked,
		"until_write_observed_ms":  held.Milliseconds(),
		"getlease_after_expiry":    lease,
		"sweep_after_expiry_notes": len(after.Entries),
	})
}

// A switch that stalls between sweep 1 and the exchange for longer than lease-break-time: the
// writer's data goes into the original, which is live, and the switch finds out after the exchange
// and puts the original back. Nothing is lost and nothing is committed over it.
func TestFenceStallBeforeTheExchangeIsCaught(t *testing.T) {
	breakTime := fenceLong(t)
	data := fenceFixture(t)
	original := fenceIno(t, data)
	note := filepath.Join(data, "workspaces", "note.txt")
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{afterSweep1: func(p, c *fenceTree) {
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+note, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		fenceWaitBroken(t, p, "workspaces/note.txt")
		child.expect(t, "wrote", breakTime+30*time.Second)
	}})
	fenceWant(t, rep, fenceRolledBack)
	if fenceIno(t, data) != original || fenceRead(t, note) != "LATE!!" {
		t.Fatalf("the write is not in the live original: %q", fenceRead(t, note))
	}
	fenceMeasure(t, "stall before exchange", map[string]any{"outcome": rep.Outcome, "duration_ms": rep.DurationMS})
}

// A switch that stalls after its final sweep, before it lets go: the writer's data lands in the
// retained previous tree. The commit stands, the report says a late write may be there, and the
// tree is kept for the operator.
func TestFenceStallAfterTheFinalSweepIsReported(t *testing.T) {
	breakTime := fenceLong(t)
	data := fenceFixture(t)
	var child *fenceChild
	rep := fenceRun(t, data, &fenceSeams{beforeRelease: func(p, c *fenceTree) {
		path := filepath.Join(fenceControlDir(data), "retained", p.root().name, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=LATE!!")
		child.expect(t, "opening", 5*time.Second)
		child.expect(t, "wrote", breakTime+30*time.Second)
	}})
	fenceWant(t, rep, fenceCommitted)
	if len(rep.LateWritePossible) == 0 || rep.LateWritePossible[0].Tree != "pre" {
		t.Fatalf("the late write after the expiry is not reported: %+v", rep.LateWritePossible)
	}
	pre := fenceRetainedPath(data, rep, "pre")
	if got := fenceRead(t, filepath.Join(pre, "workspaces", "note.txt")); got != "LATE!!" {
		t.Fatalf("the late write is not in the retained tree: %q", got)
	}
	if g := fenceGC(t, data, filepath.Base(pre), true, nil); g.Outcome != fenceRetainedGC {
		t.Fatalf("the tree with the late write was removed: %+v", g)
	}
	fenceMeasure(t, "stall after final sweep", map[string]any{"outcome": rep.Outcome, "late": rep.LateWritePossible})
}

// A removal that stalls between a file's check and its unlink for longer than lease-break-time: the
// write goes into the file, the unlink then removes it. This is the one case that loses data, and
// it must never be silent: LOST_POSSIBLE, and status says so.
func TestFenceStallAtTheUnlinkIsLoud(t *testing.T) {
	breakTime := fenceLong(t)
	data, pre := fenceCommitOne(t)
	var child *fenceChild
	g := fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(r *fenceTree, rel string) {
		if rel != "workspaces/note.txt" {
			return
		}
		path := filepath.Join(fenceControlDir(data), "trash", pre, "workspaces", "note.txt")
		child = startFenceChild(t, "fence-write", "HELPER_PATH="+path, "HELPER_BODY=GCLATE")
		child.expect(t, "opening", 5*time.Second)
		child.expect(t, "wrote", breakTime+30*time.Second)
	}})
	if g.Outcome != fenceLostPossible {
		t.Fatalf("a write lost to a stalled removal was not reported: %+v", g)
	}
	if _, code := fenceStatus(fenceControlDir(data)); code == 0 {
		t.Fatal("status is clean after LOST_POSSIBLE")
	}
	fenceMeasure(t, "stall at unlink", map[string]any{"outcome": g.Outcome, "reason": g.Reason})
}

// Deletion lives in one place. The switch, the copy and the control folder may create, rename and
// write; only the removal of a retained tree unlinks anything of a tree, and only the self-check
// unlinks its own two scratch files. Nothing here removes by path, and nothing renames by path.
func TestFenceDeletesOnlyInTheRemoval(t *testing.T) {
	files, _ := filepath.Glob("fence*.go")
	fset := token.NewFileSet()
	allowed := map[string]bool{"(*fenceControl).delete": true, "(*fenceControl).collect": true, "(*fenceControl).forget": true, "fenceSelfProbe": true}
	checked := 0
	for _, file := range files {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		source, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		tree, err := parser.ParseFile(fset, file, source, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range tree.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok {
				continue
			}
			name := fn.Name.Name
			if fn.Recv != nil && len(fn.Recv.List) == 1 {
				if star, ok := fn.Recv.List[0].Type.(*ast.StarExpr); ok {
					name = "(*" + star.X.(*ast.Ident).Name + ")." + name
				}
			}
			ast.Inspect(fn, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if !ok {
					return true
				}
				pkg, ok := sel.X.(*ast.Ident)
				if !ok {
					return true
				}
				text := string(source[fset.Position(call.Pos()).Offset:fset.Position(call.End()).Offset])
				switch pkg.Name + "." + sel.Sel.Name {
				case "os.Remove", "os.RemoveAll", "unix.Unlink", "unix.Rmdir", "syscall.Unlink", "syscall.Rmdir", "os.Rename", "unix.Rename":
					t.Errorf("%s: %s removes or renames by path: %s", fset.Position(call.Pos()), name, text)
				case "unix.Unlinkat":
					checked++
					if !allowed[name] {
						t.Errorf("%s: %s unlinks outside the removal: %s", fset.Position(call.Pos()), name, text)
					}
					if (name == "(*fenceControl).collect" || name == "(*fenceControl).forget") && !strings.Contains(text, `".json"`) {
						t.Errorf("%s: the removal's driver may only drop the record file: %s", fset.Position(call.Pos()), text)
					}
				}
				return true
			})
		}
	}
	if checked == 0 {
		t.Fatal("no unlink found at all: the check is looking at the wrong files")
	}
}

// How long a switch and a removal take on a tree of a realistic shape, and how many descriptors
// they hold. Only with DAEDALUS_FENCE_SCALE=<files>.
func TestFenceScale(t *testing.T) {
	files, _ := strconv.Atoi(os.Getenv("DAEDALUS_FENCE_SCALE"))
	if files <= 0 {
		t.Skip("set DAEDALUS_FENCE_SCALE=<number of files> to measure")
	}
	data := fenceFixture(t)
	big := make([]byte, 64<<20)
	if err := os.WriteFile(filepath.Join(data, "state", "big.sqlite"), big, 0o600); err != nil {
		t.Fatal(err)
	}
	body := make([]byte, 4096)
	for i := 0; i < files; i++ {
		dir := filepath.Join(data, "workspaces", "project", strconv.Itoa(i/100))
		if i%100 == 0 {
			if err := os.MkdirAll(dir, 0o700); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(filepath.Join(dir, strconv.Itoa(i)), body, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	var peak int
	count := func() {
		entries, _ := os.ReadDir("/proc/self/fd")
		if len(entries) > peak {
			peak = len(entries)
		}
	}
	start := time.Now()
	rep := fencedSwitch(fenceOptions{Data: data, seams: &fenceSeams{afterFinalSweep: func(p, c *fenceTree) { count() }}})
	switchTime := time.Since(start)
	if rep.Outcome != fenceCommitted {
		t.Fatalf("%s: %s", rep.Outcome, rep.Reason)
	}
	start = time.Now()
	g := fenceCollectRetained(data, "", filepath.Base(fenceRetainedPath(data, rep, "pre")), false, nil)
	gcTime := time.Since(start)
	if g.Outcome != fenceDeleted {
		t.Fatalf("%+v", g)
	}
	fenceMeasure(t, "scale", map[string]any{"files": files + 5, "dirs": files/100 + 6, "bytes": 64<<20 + files*4096,
		"switch_ms": switchTime.Milliseconds(), "removal_ms": gcTime.Milliseconds(), "descriptors_held": peak, "leases": rep.LeasesTaken})
}

// The whole path on a real installation laid out the old way (a real uv environment inside the data
// folder): the switch refuses it, the launcher's migration moves the runtime out, no hard link is
// left in the data folder, and the switch commits. Only with DAEDALUS_FENCE_REALISTIC=<data folder>.
func TestFenceRealisticInstallation(t *testing.T) {
	data := os.Getenv("DAEDALUS_FENCE_REALISTIC")
	if data == "" {
		t.Skip("set DAEDALUS_FENCE_REALISTIC to a disposable data folder laid out like a native installation")
	}
	fenceRequireExt4(t, data)
	census := func(root string) (files, dirs, links int, bytes int64) {
		filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			var st unix.Stat_t
			if unix.Lstat(path, &st) != nil {
				return nil
			}
			switch st.Mode & unix.S_IFMT {
			case unix.S_IFDIR:
				dirs++
			case unix.S_IFREG:
				files++
				bytes += st.Size
				if st.Nlink > 1 {
					links++
				}
			default:
				files++
			}
			return nil
		})
		return
	}
	f0, d0, l0, b0 := census(data)
	refused := fencedSwitch(fenceOptions{Data: data})
	if refused.Outcome != fenceFailClosed || !strings.Contains(refused.Reason, "runtime/") {
		t.Fatalf("the old layout was not refused: %s %s", refused.Outcome, refused.Reason)
	}
	p, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if err := migrateLegacyRuntime(context.Background(), p, t.Logf); err != nil {
		t.Fatal(err)
	}
	migrateTime := time.Since(start)
	f1, d1, l1, b1 := census(data)
	if l1 != 0 {
		t.Fatalf("%d hard links are still in the data folder", l1)
	}
	aside, _ := filepath.Glob(filepath.Join(filepath.Dir(p.Runtime), "legacy-"+filepath.Base(p.Runtime)+"-*"))
	_, _, asideLinks, _ := census(aside[0])
	var peak int
	start = time.Now()
	rep := fencedSwitch(fenceOptions{Data: data, seams: &fenceSeams{afterFinalSweep: func(p, c *fenceTree) {
		entries, _ := os.ReadDir("/proc/self/fd")
		peak = len(entries)
	}}})
	switchTime := time.Since(start)
	if rep.Outcome != fenceCommitted {
		t.Fatalf("%s: %s %+v", rep.Outcome, rep.Reason, rep.Entries)
	}
	start = time.Now()
	g := fenceCollectRetained(data, "", filepath.Base(fenceRetainedPath(data, rep, "pre")), false, nil)
	gcTime := time.Since(start)
	if g.Outcome != fenceDeleted {
		t.Fatalf("%+v", g)
	}
	fenceMeasure(t, "realistic installation", map[string]any{
		"before_files":                       f0,
		"before_dirs":                        d0,
		"before_hardlinks":                   l0,
		"before_bytes":                       b0,
		"after_files":                        f1,
		"after_dirs":                         d1,
		"after_hardlinks":                    l1,
		"after_bytes":                        b1,
		"hardlinks_in_the_runtime_set_aside": asideLinks,
		"migration_ms":                       migrateTime.Milliseconds(),
		"switch_ms":                          switchTime.Milliseconds(),
		"removal_ms":                         gcTime.Milliseconds(),
		"descriptors_held":                   peak,
		"leases":                             rep.LeasesTaken,
		"unscanned":                          len(rep.Unscanned),
	})
}
