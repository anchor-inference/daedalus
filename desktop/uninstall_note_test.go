package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The copies of the data kept from before updates are beside the data folder, not in it: an
// uninstall that told the operator to delete the data folder by hand, and said nothing of them,
// left their keys on the disk.
func TestUninstallNamesTheKeptCopiesOfTheData(t *testing.T) {
	p := fixtureData(t)
	if err := os.WriteFile(p.Mode, []byte("native\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	app := NewApp(p)
	app.SetMode(ModeNative)
	if err := os.MkdirAll(fenceControlPath(p.Data), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, keep := range []bool{true, false} {
		app.lines = nil
		if err := app.uninstall(context.Background(), keep); err != nil {
			t.Fatal(err)
		}
		if said := strings.Join(app.lines, "\n"); !strings.Contains(said, fenceControlPath(p.Data)) {
			t.Errorf("keep=%v: the uninstall did not name %s:\n%s", keep, fenceControlPath(p.Data), said)
		}
	}
	if !exists(fenceControlPath(p.Data)) {
		t.Error("the kept copies were removed; like the data folder they are the operator's to delete")
	}
}

// The uninstaller's "delete my data too" deletes the data folder and the copies updates kept beside
// it — and refuses a folder that is not an installation, since --data can name any folder at all.
func TestUninstallRemovesTheDataOnlyWhenAskedAndOnlyAnInstallation(t *testing.T) {
	t.Setenv("DAEDALUS_LOCAL_ROOT", t.TempDir())
	p := setupTempInstall(t)
	if err := os.WriteFile(p.Env, []byte("API_PORT=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	control := fenceControlPath(p.Data)
	if err := os.MkdirAll(filepath.Join(control, "retained", "pre-update"), 0o700); err != nil {
		t.Fatal(err)
	}
	app := NewApp(p)
	if err := app.Uninstall(context.Background(), true, false); err != nil {
		t.Fatal(err)
	}
	if !exists(p.Env) || !exists(control) {
		t.Fatal("keeping the data removed it")
	}
	if err := app.Uninstall(context.Background(), false, true); err != nil {
		t.Fatal(err)
	}
	if exists(p.Data) || exists(control) {
		t.Fatal("the data folder or its kept copies are still there")
	}

	stranger := t.TempDir()
	if err := os.WriteFile(filepath.Join(stranger, "notes.txt"), []byte("mine\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	other, err := NewPaths(stranger)
	if err != nil {
		t.Fatal(err)
	}
	if err := removeDataFolder(other, func(string, ...any) {}); err == nil || !exists(filepath.Join(stranger, "notes.txt")) {
		t.Fatalf("a folder that is not an installation was deleted (%v)", err)
	}
}

// Compose's project is "daedalus" on every installation, so a `down` reaches whatever runs under
// that name on this machine. One whose setup never wrote a configuration started nothing there
// and must take nothing down — even asked to delete its data, even in Docker mode.
func TestAnUnconfiguredInstallationLeavesDockerAlone(t *testing.T) {
	t.Setenv("DAEDALUS_LOCAL_ROOT", t.TempDir())
	t.Setenv("PATH", t.TempDir())
	p := setupTempInstall(t)
	app := NewApp(p)
	app.SetMode(ModeDocker)
	var said []string
	app.lines = nil
	if err := app.uninstall(context.Background(), false); err != nil {
		t.Fatalf("an installation with nothing in Docker reached for it: %v", err)
	}
	for _, line := range app.lines {
		said = append(said, line)
	}
	if !strings.Contains(strings.Join(said, "\n"), "never started anything in Docker") {
		t.Fatalf("it said %q", said)
	}
}
