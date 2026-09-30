//go:build linux

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fenceCrash(t *testing.T, data, at string) {
	t.Helper()
	child := startFenceChild(t, "fence-crash", "HELPER_PATH="+data, "HELPER_AT="+at)
	child.expect(t, "dying", 30*time.Second)
	child.cmd.Wait()
}

// A switch that died after its exchange leaves the copy live and the original at the slot. The next
// switch refuses until the operator has looked; `update resolve` says what is where without changing
// anything, and with --apply files the original for the operator, never touching the live tree.
func TestAnUnfinishedSwitchIsResolvedWithoutTouchingTheLiveData(t *testing.T) {
	for _, at := range []string{"after-exchange", "before-exchange"} {
		t.Run(at, func(t *testing.T) {
			data := fenceFixture(t)
			original := fenceIno(t, data)
			fenceCrash(t, data, at)
			liveBefore := fenceIno(t, data)
			if at == "after-exchange" && liveBefore == original {
				t.Fatal("the crash did not happen after the exchange")
			}
			blocked := fenceRun(t, data, nil)
			if blocked.Outcome != fenceFailClosed || !strings.Contains(blocked.Reason, "update resolve") {
				t.Fatalf("an unfinished switch did not block the next: %+v", blocked)
			}
			if text, code := fenceStatus(fenceControlDir(data), true); code == 0 || !strings.Contains(text, "update resolve") {
				t.Fatalf("status does not point at the unfinished switch: %d %s", code, text)
			}
			plan, err := fenceResolve(data, "", false)
			if err != nil || len(plan) != 1 || plan[0].Applied {
				t.Fatalf("plan %+v, %v", plan, err)
			}
			want := map[string]string{"after-exchange": "C", "before-exchange": "P"}[at]
			if plan[0].Live != want {
				t.Fatalf("live is %s, want %s: %+v", plan[0].Live, want, plan[0])
			}
			if fenceIno(t, data) != liveBefore {
				t.Fatal("a dry run changed data")
			}
			done, err := fenceResolve(data, "", true)
			if err != nil || len(done) != 1 || !done[0].Applied {
				t.Fatalf("apply %+v, %v", done, err)
			}
			if fenceIno(t, data) != liveBefore || fenceRead(t, filepath.Join(data, "workspaces", "note.txt")) != "before" {
				t.Fatal("the live data changed")
			}
			fenceNoSlot(t, data)
			kept, _ := filepath.Glob(filepath.Join(fenceControlDir(data), "retained", "*-*"))
			var trees []string
			for _, path := range kept {
				if !strings.HasSuffix(path, ".json") {
					trees = append(trees, path)
				}
			}
			if len(trees) != 1 {
				t.Fatalf("expected the other tree kept for the operator: %v", kept)
			}
			if at == "after-exchange" && fenceIno(t, trees[0]) != original {
				t.Fatal("the kept tree is not the original")
			}
			var meta fenceRetainedMeta
			if err := readJSON(trees[0]+".json", &meta); err != nil || meta.GC != "manual" {
				t.Fatalf("a tree resolve filed is not the operator's to remove (gc %q): %v", meta.GC, err)
			}
			if at == "after-exchange" && len(meta.Manifest) == 0 {
				t.Fatal("the manifest the switch recorded in advance was not kept with the tree")
			}
			if g := fenceGC(t, data, filepath.Base(trees[0]), false, nil); g.Outcome != fenceRetainedGC {
				t.Fatalf("a resolved tree was removed automatically: %+v", g)
			}
			if again, _ := fenceResolve(data, "", true); len(again) != 0 {
				t.Fatalf("a resolved switch came back: %+v", again)
			}
			// Kept with the manifest the switch recorded before its exchange, the tree is an ordinary
			// kept copy; without one it is listed as unrecorded.
			wantKind := map[string]string{"after-exchange": "retained", "before-exchange": "unrecorded"}[at]
			sum := fenceSummarize(fenceControlDir(data))
			if sum.Trouble || len(sum.Items) == 0 || sum.Items[0].Kind != wantKind {
				t.Fatalf("after resolving, the kept tree should be listed and nothing be wrong: %+v", sum)
			}
			next := fenceRun(t, data, nil)
			fenceWant(t, next, fenceCommitted)
		})
	}
}

func TestResolvingWithNothingToResolve(t *testing.T) {
	data := fenceFixture(t)
	if got, err := fenceResolve(data, "", true); err != nil || len(got) != 0 {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := os.Stat(fenceControlDir(data)); !os.IsNotExist(err) {
		t.Fatal("resolving created a control folder where no switch ever ran")
	}
}
