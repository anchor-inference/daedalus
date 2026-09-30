//go:build !windows

package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		path := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func legacyInstallation(t *testing.T) Paths {
	t.Helper()
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	writeTree(t, p.LegacyRuntime, map[string]string{
		"venv/bin/python":                    "an interpreter with the old path baked in",
		"logs/supervisor.log":                "yesterday's log",
		"ptyd/state/terminals/t1.log":        "a terminal's scrollback",
		"browserd/state/profile/Cookies":     "a login",
		"installed/uv":                       "0.12.15 abc",
		"cache/wheels/big.whl":               "downloaded again when needed",
		"ptyd/run/token":                     "a dead daemon's token",
		"browserd/state/profile/Local State": "more of the login",
	})
	writeTree(t, p.Data, map[string]string{"state/daedalus.sqlite": "the database", "workspaces/note.md": "a note"})
	return p
}

// What cannot be rebuilt is carried over, the rest leaves the data folder, and nothing of the data
// itself is touched.
func TestTheLegacyRuntimeLeavesTheDataFolderWithItsStateCarried(t *testing.T) {
	p := legacyInstallation(t)
	var lines []string
	log := func(format string, args ...any) { lines = append(lines, format) }
	if err := migrateLegacyRuntime(context.Background(), p, log); err != nil {
		t.Fatal(err)
	}
	if exists(p.LegacyRuntime) {
		t.Fatal("the runtime folder is still inside the data folder")
	}
	logs, _ := filepath.Glob(filepath.Join(p.RuntimeLogs, "before-the-move-*", "supervisor.log"))
	if len(logs) != 1 || read(t, logs[0]) != "yesterday's log" {
		t.Fatalf("the logs were not carried: %v", logs)
	}
	if got := read(t, filepath.Join(ptydStateDir(p), "terminals", "t1.log")); got != "a terminal's scrollback" {
		t.Fatalf("terminal logs: %q", got)
	}
	if got := read(t, filepath.Join(browserdStateDir(p), "profile", "Cookies")); got != "a login" {
		t.Fatalf("browser profile: %q", got)
	}
	if exists(filepath.Join(ptydRunDir(p), "token")) {
		t.Fatal("a dead daemon's token was carried")
	}
	aside, _ := filepath.Glob(filepath.Join(filepath.Dir(p.Runtime), "legacy-"+filepath.Base(p.Runtime)+"-*"))
	if len(aside) != 1 || read(t, filepath.Join(aside[0], "venv", "bin", "python")) == "" {
		t.Fatalf("the old folder was not kept aside on the same filesystem: %v", aside)
	}
	if read(t, filepath.Join(p.State, "daedalus.sqlite")) != "the database" || read(t, filepath.Join(p.Workspaces, "note.md")) != "a note" {
		t.Fatal("the data changed")
	}
	if !exists(filepath.Join(p.Local, "moved-out-of-data.json")) {
		t.Fatal("no record of the move")
	}
	// Once is enough.
	if err := migrateLegacyRuntime(context.Background(), p, log); err != nil {
		t.Fatal(err)
	}
}

// Across filesystems the rebuildable rest is removed instead of copied — after the state is safe.
func TestTheLegacyRuntimeOnAnotherFilesystemIsRemovedAfterItsStateIsCarried(t *testing.T) {
	p := legacyInstallation(t)
	defer func(saved func(string, string) error) { renameDir = saved }(renameDir)
	renameDir = func(from, to string) error {
		return &os.LinkError{Op: "rename", Old: from, New: to, Err: syscall.EXDEV}
	}
	if err := migrateLegacyRuntime(context.Background(), p, func(string, ...any) {}); err != nil {
		t.Fatal(err)
	}
	if exists(p.LegacyRuntime) {
		t.Fatal("the runtime folder is still inside the data folder")
	}
	if read(t, filepath.Join(browserdStateDir(p), "profile", "Cookies")) != "a login" {
		t.Fatal("the browser profile was lost")
	}
}

// A copy that fails leaves the data folder, the old runtime included, exactly as it was.
func TestAFailedCarryChangesNothing(t *testing.T) {
	p := legacyInstallation(t)
	unreadable := filepath.Join(p.LegacyRuntime, "browserd", "state", "profile", "Cookies")
	if err := os.Chmod(unreadable, 0); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(unreadable, 0o600)
	if os.Geteuid() == 0 {
		t.Skip("root reads a file of mode 0")
	}
	err := migrateLegacyRuntime(context.Background(), p, func(string, ...any) {})
	if err == nil || !strings.Contains(err.Error(), "unchanged") {
		t.Fatalf("err = %v", err)
	}
	if !exists(filepath.Join(p.LegacyRuntime, "venv", "bin", "python")) {
		t.Fatal("the old runtime was moved although its state was not carried")
	}
}

// A browser profile already in the new place (a start with the new layout came first) is kept, and
// the old one lands beside it.
func TestACarryNeverOverwrites(t *testing.T) {
	p := legacyInstallation(t)
	writeTree(t, browserdStateDir(p), map[string]string{"profile/Cookies": "a newer login"})
	// Older by an hour, not by however little the two writes above were apart: the filesystem's
	// clock is coarse, and two writes in one tick have one time.
	hourAgo := time.Now().Add(-time.Hour)
	for _, name := range []string{"Cookies", "Local State"} {
		os.Chtimes(filepath.Join(p.LegacyRuntime, "browserd", "state", "profile", name), hourAgo, hourAgo)
	}
	if err := migrateLegacyRuntime(context.Background(), p, func(string, ...any) {}); err != nil {
		t.Fatal(err)
	}
	if read(t, filepath.Join(browserdStateDir(p), "profile", "Cookies")) != "a newer login" {
		t.Fatal("the newer profile was overwritten")
	}
	old, _ := filepath.Glob(browserdStateDir(p) + "-before-the-move-*")
	if len(old) != 1 || read(t, filepath.Join(old[0], "profile", "Cookies")) != "a login" {
		t.Fatalf("the old profile is not beside it: %v", old)
	}
}

// fakeUV records every call and creates the environment it was pointed at.
func fakeUV(t *testing.T, p Paths) string {
	t.Helper()
	calls := filepath.Join(t.TempDir(), "calls")
	script := "#!/bin/sh\necho \"$UV_PROJECT_ENVIRONMENT $*\" >> '" + calls + "'\nmkdir -p \"$UV_PROJECT_ENVIRONMENT/bin\"\necho made > \"$UV_PROJECT_ENVIRONMENT/made-by-sync\"\n"
	if err := os.MkdirAll(p.RuntimeUV, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(uvBinary(p), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return calls
}

// The next version's environment is prepared beside the one in use, from its own lock, without the
// project pointers; the one in use is untouched, so a refused or rolled-back switch starts exactly
// as before, and the switched-to checkout finds its environment ready.
func TestTheNextVersionsEnvironmentIsPreparedBesideTheCurrentOne(t *testing.T) {
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	writeTree(t, p.Bot, map[string]string{"uv.lock": "version one", "pyproject.toml": "x"})
	p.selectEnv()
	current := p.RuntimeVenv
	writeTree(t, current, map[string]string{dependencyStamp: "one", "bin/python": "current"})
	calls := fakeUV(t, p)
	n := NewNative(p, func(string, ...any) {})
	staged := filepath.Join(t.TempDir(), "staged", "daedalus")
	writeTree(t, staged, map[string]string{"uv.lock": "version two", "pyproject.toml": "x"})
	next, err := n.prepareEnv(context.Background(), staged)
	if err != nil {
		t.Fatal(err)
	}
	if next == current || filepath.Dir(next) != p.RuntimeEnvs {
		t.Fatalf("prepared into %s, current %s", next, current)
	}
	got := read(t, calls)
	if !strings.HasPrefix(got, next+" sync --frozen --inexact --no-install-local") {
		t.Fatalf("uv was called as %q", got)
	}
	if read(t, filepath.Join(current, "bin", "python")) != "current" || exists(filepath.Join(current, "made-by-sync")) {
		t.Fatal("the environment in use was touched")
	}
	// A rollback leaves the old checkout: it still selects the old environment.
	again := p
	again.selectEnv()
	if again.RuntimeVenv != current {
		t.Fatalf("after a rollback the environment is %s", again.RuntimeVenv)
	}
	// The switched-to checkout selects the prepared one.
	writeTree(t, p.Bot, map[string]string{"uv.lock": "version two"})
	again.selectEnv()
	if again.RuntimeVenv != next {
		t.Fatalf("the new checkout selects %s, not the prepared %s", again.RuntimeVenv, next)
	}
}

func TestOldEnvironmentsArePrunedButNeverTheOneInUse(t *testing.T) {
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	p.RuntimeVenv = filepath.Join(p.RuntimeEnvs, "inuse")
	names := []string{"inuse", "a", "b", "c", "d"}
	for i, name := range names {
		dir := filepath.Join(p.RuntimeEnvs, name)
		writeTree(t, dir, map[string]string{"marker": name})
		// "inuse" is the oldest of all: age is no reason to remove the environment in use.
		when := time.Now().Add(-time.Duration(len(names)-i) * time.Hour)
		if name == "inuse" {
			when = time.Now().Add(-48 * time.Hour)
		}
		os.Chtimes(dir, when, when)
	}
	pruneEnvs(p, nil)
	for name, want := range map[string]bool{"inuse": true, "d": true, "c": true, "b": false, "a": false} {
		if exists(filepath.Join(p.RuntimeEnvs, name)) != want {
			t.Errorf("%s kept = %v, want %v", name, !want, want)
		}
	}
}
