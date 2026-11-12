package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestTheWorkIsFoundWithoutFollowingALink(t *testing.T) {
	p, outside := workspaceData(t)
	if err := os.MkdirAll(filepath.Join(outside, "node_modules", "dep"), 0o755); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		// A checkout folder that is a link to a project elsewhere is not walked into.
		os.Symlink(outside, filepath.Join(p.Data, "protocore-exp"))
	}
	found, err := findWork(p.Data)
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(found, ",")
	if got != "workspaces,worktrees,state/checkpoints,state/models,state/tmp,state/upload-staging,daedalus/miniapp/node_modules" {
		t.Fatalf("found %s", got)
	}
	for rel, want := range map[string]bool{
		"workspaces/s1/main.py":                  true,
		"state/checkpoints":                      true,
		"daedalus/miniapp/node_modules/x/a.js":   true,
		"daedalus/miniapp/package.json":          false,
		"state/daedalus.sqlite":                  false,
		"state/checkpoints-other":                false,
		"node_modules/x":                         false,
		"daedalus-secrets/node_modules/keys.env": false,
	} {
		if isWork(rel) != want {
			t.Errorf("isWork(%s) = %v", rel, !want)
		}
	}
}

// A switch that stopped with the work moved out: nothing starts, and `update resolve --apply` puts it
// back, keeping whole anything that has taken its place since.
func TestWorkLeftAsideBlocksAStartUntilItIsPutBack(t *testing.T) {
	p, _ := workspaceData(t)
	if _, err := setWorkAside(p); err != nil {
		t.Fatal(err)
	}
	if exists(p.Workspaces) || exists(filepath.Join(p.State, "checkpoints")) {
		t.Fatal("the work was not moved out")
	}
	if err := InterruptedUpgrade(p); err == nil || !strings.Contains(err.Error(), "update resolve --apply") {
		t.Fatalf("a start with the work aside: %v", err)
	}
	// Something took one of the places meanwhile: an older copy put back by a rollback.
	os.MkdirAll(filepath.Join(p.Workspaces, "s1"), 0o755)
	os.WriteFile(filepath.Join(p.Workspaces, "s1", "main.py"), []byte("an older copy\n"), 0o640)
	// And an empty folder an app made in another.
	os.MkdirAll(filepath.Join(p.Data, "worktrees"), 0o755)

	if err := switchReportCommand(p, options{extra: "resolve"}); err != nil {
		t.Fatal(err)
	}
	if !workIsAside(p) {
		t.Fatal("resolve without --apply moved the work")
	}
	if err := switchReportCommand(p, options{extra: "resolve", apply: true}); err != nil {
		t.Fatal(err)
	}
	if got := read(t, filepath.Join(p.Workspaces, "s1", "main.py")); got != "print('work')\n" {
		t.Fatalf("the workspace is %q", got)
	}
	if got := read(t, filepath.Join(p.Data, "worktrees", "selfdev", "README.md")); got != "a worktree\n" {
		t.Fatalf("the worktree is %q", got)
	}
	if got := read(t, filepath.Join(p.State, "checkpoints", "s1", "HEAD")); got != "ref: refs/heads/main\n" {
		t.Fatalf("the snapshot store is %q", got)
	}
	displaced, _ := filepath.Glob(filepath.Join(fenceControlPath(p.Data), "displaced-*", "workspaces", "s1", "main.py"))
	if len(displaced) != 1 || read(t, displaced[0]) != "an older copy\n" {
		t.Fatalf("what was in the way was not kept: %v", displaced)
	}
	if exists(workAsideDir(p)) {
		t.Fatal("the set-aside folder was left behind")
	}
	if err := InterruptedUpgrade(p); err != nil {
		t.Fatal(err)
	}
}
