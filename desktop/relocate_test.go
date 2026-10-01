package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// relocationFixture is an older installation beside its executable — data/ with a configuration,
// a database and a key — and the local state and runtime its path keys, all in temporary folders.
func relocationFixture(t *testing.T) (from, to string) {
	t.Helper()
	t.Setenv("DAEDALUS_LOCAL_ROOT", t.TempDir())
	to = useStandardIn(t, t.TempDir())
	from = filepath.Join(t.TempDir(), "data")
	mark(t, from)
	for name, body := range map[string]string{
		"state/daedalus.sqlite":         "the conversations\n",
		"daedalus-secrets/keyproxy.env": "KEY=1\n",
		"mode":                          "native\n",
	} {
		path := filepath.Join(from, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	old, err := NewPaths(from)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{filepath.Join(old.Local, "browserd", "profile", "Cookies"), filepath.Join(old.Runtime, "envs", "x", "python")} {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return from, to
}

func readText(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

// The move is a rename of the whole folder: the database and the keys arrive as they were, the
// browser logins come along under the new key, the runtime (a cache that holds old paths) goes,
// and the old folder's parent says where everything went.
func TestAnOlderInstallationMovesToThePerUserFolderWhole(t *testing.T) {
	from, to := relocationFixture(t)
	old, _ := NewPaths(from)
	var said []string
	moved, err := relocateData(context.Background(), from, to, func(format string, args ...any) { said = append(said, format) })
	if err != nil {
		t.Fatal(err)
	}
	if moved.Data != to || exists(from) {
		t.Fatalf("the data is at %s and %s still exists: %v", moved.Data, from, exists(from))
	}
	if readText(t, filepath.Join(to, "state", "daedalus.sqlite")) != "the conversations\n" || readText(t, filepath.Join(to, "daedalus-secrets", "keyproxy.env")) != "KEY=1\n" {
		t.Fatal("the data did not arrive as it was")
	}
	if readText(t, filepath.Join(moved.Local, "browserd", "profile", "Cookies")) != "x" || exists(old.Local) {
		t.Fatal("the local state was not carried to the new key")
	}
	if exists(old.Runtime) {
		t.Fatal("the old runtime, keyed to a path that no longer exists, was left behind")
	}
	note := readText(t, filepath.Join(filepath.Dir(from), "DATA-MOVED.txt"))
	if !strings.Contains(note, to) {
		t.Fatalf("the note does not say where the data went: %s", note)
	}
}

// What a first start made in the per-user folder before the move — the empty skeleton — is set
// aside, never merged into and never deleted; an installation there is never replaced.
func TestTheMoveNeverReplacesAnInstallation(t *testing.T) {
	from, to := relocationFixture(t)
	skeleton, _ := NewPaths(to)
	if err := skeleton.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if _, err := relocateData(context.Background(), from, to, func(string, ...any) {}); err != nil {
		t.Fatal(err)
	}
	matches, _ := filepath.Glob(to + ".before-move-*")
	if len(matches) != 1 {
		t.Fatalf("the skeleton was not set aside: %v", matches)
	}

	second := filepath.Join(t.TempDir(), "data")
	mark(t, second)
	if _, err := relocateData(context.Background(), second, to, func(string, ...any) {}); err == nil || !strings.Contains(err.Error(), "already holds an installation") {
		t.Fatalf("an installation was replaced: %v", err)
	}
	if !exists(filepath.Join(second, ".env")) {
		t.Fatal("the refused move touched its source")
	}
}

// Nothing moves under a launcher that runs on the folder, or under an update that did not finish.
func TestTheMoveWaitsForNothingThatUsesTheFolder(t *testing.T) {
	from, to := relocationFixture(t)
	old, _ := NewPaths(from)
	if err := WriteInstance(old, 1, "token"); err != nil {
		t.Fatal(err)
	}
	if _, err := relocateData(context.Background(), from, to, func(string, ...any) {}); err == nil || !strings.Contains(err.Error(), "running") {
		t.Fatalf("the data moved from under a running launcher: %v", err)
	}
	RemoveInstance(old)
	if err := writeJournal(old, &Journal{Kind: kindUpdate, Stage: stageFinishing}); err != nil {
		t.Fatal(err)
	}
	if _, err := relocateData(context.Background(), from, to, func(string, ...any) {}); err == nil {
		t.Fatal("the data moved in the middle of an update")
	}
	if !exists(filepath.Join(from, "state", "daedalus.sqlite")) || exists(to) {
		t.Fatal("a refused move changed something")
	}
}

// import takes the old installation's folder or its data folder, whichever the operator names.
func TestImportTakesTheFolderOrItsData(t *testing.T) {
	from, to := relocationFixture(t)
	app := NewApp(Paths{Data: to})
	paths, err := NewPaths(to)
	if err != nil {
		t.Fatal(err)
	}
	app.paths = paths
	if err := importCommand(context.Background(), app, options{extra: filepath.Dir(from)}); err != nil {
		t.Fatal(err)
	}
	if readText(t, filepath.Join(to, "state", "daedalus.sqlite")) != "the conversations\n" {
		t.Fatal("the import did not bring the data")
	}
}
