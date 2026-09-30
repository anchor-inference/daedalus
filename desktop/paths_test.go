package main

import (
	"os"
	"path/filepath"
	"testing"
)

// The data folder of a bundled launcher belongs beside the .app — the folder the operator dropped
// the app into — and nowhere else. Inside the bundle it would be lost with the next download, and
// relative it would land in the root of the disk, because Finder starts a bundled program with "/"
// as its working directory.
func TestTheBundleKeepsItsDataBesideTheApp(t *testing.T) {
	exe := filepath.Join("/Users/someone/Daedalus/Daedalus.app/Contents/MacOS/daedalus-desktop")
	if got, want := DefaultDataDir(exe), filepath.Join("/Users/someone/Daedalus/data"); got != want {
		t.Fatalf("the data folder would be %s, want %s", got, want)
	}
	if app, ok := bundleRoot(exe); !ok || app != filepath.FromSlash("/Users/someone/Daedalus/Daedalus.app") {
		t.Fatalf("the bundle is %q (%v)", app, ok)
	}
}

func TestAPlainExecutableKeepsItsDataWhereItIsRun(t *testing.T) {
	for _, exe := range []string{
		"/Users/someone/Daedalus/daedalus-desktop",
		"/home/someone/daedalus/daedalus-desktop-linux-amd64",
		// A binary that merely lives under something called MacOS, without the rest of the layout
		// a bundle has, is not a bundle.
		"/Users/someone/MacOS/daedalus-desktop",
		"/Users/someone/Daedalus.app/daedalus-desktop",
		"",
	} {
		if got := DefaultDataDir(exe); got != "data" {
			t.Fatalf("%s: the data folder would be %s, want data", exe, got)
		}
		if _, ok := bundleRoot(exe); ok {
			t.Fatalf("%s is not a bundle", exe)
		}
	}
}

// --data still wins over both, and a relative one is resolved against the working directory.
func TestTheGivenDataFolderIsUsedAsGiven(t *testing.T) {
	dir := t.TempDir()
	paths, err := NewPaths(dir)
	if err != nil {
		t.Fatal(err)
	}
	if paths.Data != dir {
		t.Fatalf("the data folder is %s, want %s", paths.Data, dir)
	}
}

// The compose file mounts the host terminal's directory whatever the mode, and Docker creates a
// missing bind source as root; made here first, it is the operator's.
func TestTheHostTerminalDirectoryIsMadeBesideTheCheckouts(t *testing.T) {
	dir := t.TempDir()
	paths, err := NewPaths(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	fromCompose := filepath.Join(filepath.Dir(paths.Compose), "..", "..", "daedalus-host-terminals")
	if filepath.Clean(fromCompose) != paths.HostTerminals {
		t.Fatalf("compose would mount %s, the launcher makes %s", filepath.Clean(fromCompose), paths.HostTerminals)
	}
	if st, err := os.Stat(paths.HostTerminals); err != nil || !st.IsDir() {
		t.Fatalf("%v %v", st, err)
	}
}
