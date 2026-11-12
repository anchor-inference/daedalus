//go:build linux

package main

import (
	"context"
	"maps"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// addWork gives an installation's data folder the work a real one has: a deep workspace, a worktree,
// a snapshot store and a checkout's node_modules.
func addWork(t *testing.T, data string) {
	t.Helper()
	for name, body := range map[string]string{
		"workspaces/s1/node_modules/a/b/c/index.js": "module.exports = 1\n",
		"worktrees/selfdev/README.md":               "a worktree\n",
		"state/checkpoints/s1/HEAD":                 "ref: refs/heads/main\n",
		"state/tmp/partial":                         "in flight",
		"daedalus/miniapp/node_modules/x/index.js":  "installed by npm ci",
	} {
		full := filepath.Join(data, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0o640); err != nil {
			t.Fatal(err)
		}
	}
}

// workInodes are the inodes of the work's folders: the same after an update means it was moved by
// rename, not copied.
func workInodes(t *testing.T, data string) map[string]uint64 {
	t.Helper()
	inodes := map[string]uint64{}
	for _, rel := range []string{"workspaces", "worktrees", "state/checkpoints", "state/tmp", "daedalus/miniapp/node_modules"} {
		inodes[rel] = fenceIno(t, filepath.Join(data, filepath.FromSlash(rel)))
	}
	return inodes
}

func TestAFencedUpdateNeitherCopiesNorKeepsTheWork(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	addWork(t, in.paths.Data)
	before := workInodes(t, in.paths.Data)
	in.stack.onUpdate = func() {
		// The work is back in the live tree by the time the new version runs.
		if !exists(filepath.Join(in.paths.Data, "workspaces", "s1", "node_modules", "a", "b", "c", "index.js")) {
			t.Error("the new version started without its workspaces")
		}
	}
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageCommitted || journal.Pre == "" {
		t.Fatalf("journal %+v\n%s", journal, u.out)
	}
	if after := workInodes(t, in.paths.Data); !maps.Equal(before, after) {
		t.Fatalf("the work was copied, not moved: %v then %v", before, after)
	}
	pre := filepath.Join(fenceControlPath(in.paths.Data), "retained", journal.Pre)
	for _, rel := range []string{"workspaces", "worktrees", "state/checkpoints", "state/tmp", "daedalus/miniapp/node_modules"} {
		if exists(filepath.Join(pre, filepath.FromSlash(rel))) {
			t.Errorf("the kept copy holds %s", rel)
		}
	}
	if exists(workAsideDir(in.paths)) {
		t.Fatal("the set-aside folder was left behind")
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}

func TestAFencedRollbackKeepsTheWorkAsTheNewVersionLeftIt(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	addWork(t, in.paths.Data)
	before := workInodes(t, in.paths.Data)
	in.stack.fail = true
	in.stack.onUpdate = func() {
		os.WriteFile(filepath.Join(in.paths.Data, "workspaces", "s1", "during.txt"), []byte("written while the new version ran\n"), 0o640)
	}
	if err := u.update(context.Background()); err == nil {
		t.Fatalf("reported success\n%s", u.out)
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack {
		t.Fatalf("journal %+v\n%s", journal, u.out)
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("the data from before is not live again")
	}
	if in.file(t, "data/workspaces/s1/during.txt") != "written while the new version ran\n" {
		t.Fatal("the rollback took back what was written in a workspace")
	}
	if after := workInodes(t, in.paths.Data); !maps.Equal(before, after) {
		t.Fatalf("the work was copied, not moved: %v then %v", before, after)
	}
	failed, _ := filepath.Glob(filepath.Join(fenceControlPath(in.paths.Data), "retained", "failed-*"))
	for _, tree := range failed {
		if !strings.HasSuffix(tree, ".json") && exists(filepath.Join(tree, "workspaces")) {
			t.Fatal("the failed version's kept tree holds the workspaces")
		}
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}
