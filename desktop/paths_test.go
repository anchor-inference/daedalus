package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// useStandardIn points the per-user data folder of this platform into a temporary folder, through
// the variable the platform takes it from, and returns where it now is.
func useStandardIn(t *testing.T, home string) string {
	t.Helper()
	switch runtime.GOOS {
	case "windows":
		t.Setenv("LOCALAPPDATA", home)
	case "darwin":
		t.Setenv("HOME", home)
	default:
		t.Setenv("XDG_DATA_HOME", home)
	}
	standard, err := standardDataDir()
	if err != nil {
		t.Fatal(err)
	}
	if !isInside(standard, home) {
		t.Fatalf("the standard folder %s is not under %s", standard, home)
	}
	return standard
}

func mark(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("API_PORT=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// The data belongs in the per-user folder, wherever the executable is and whatever the working
// directory: an installed application lives where an installer replaces it, and the working
// directory of a program started from a menu is anyone's guess.
func TestTheDataLivesInThePerUserFolder(t *testing.T) {
	standard := useStandardIn(t, t.TempDir())
	for _, exe := range []string{
		filepath.Join(t.TempDir(), "daedalus-desktop"),
		filepath.Join(t.TempDir(), "Daedalus.app", "Contents", "MacOS", "daedalus-desktop"),
		"",
	} {
		if got := DefaultDataDir(exe); got != standard {
			t.Fatalf("%s: the data folder would be %s, want %s", exe, got, standard)
		}
	}
}

// An installation an older launcher kept beside itself — data/ next to the executable, or next to
// Daedalus.app — is the one used until a start moves it; once the per-user folder holds an
// installation, that one wins.
func TestAnOlderInstallationBesideTheExecutableIsFoundUntilItMoves(t *testing.T) {
	standard := useStandardIn(t, t.TempDir())
	folder := t.TempDir()
	plain := filepath.Join(folder, "daedalus-desktop")
	mark(t, filepath.Join(folder, "data"))
	if got := DefaultDataDir(plain); got != filepath.Join(folder, "data") {
		t.Fatalf("the data folder would be %s, want the one beside the executable", got)
	}
	if got, ok := relocatableFromHere(filepath.Join(folder, "data")); ok || got != "" {
		// relocatableFromHere asks about this test binary, which has no data beside it.
		t.Fatalf("a folder beside another executable was offered for a move: %s", got)
	}

	bundled := t.TempDir()
	exe := filepath.Join(bundled, "Daedalus.app", "Contents", "MacOS", "daedalus-desktop")
	mark(t, filepath.Join(bundled, "data"))
	if got := DefaultDataDir(exe); got != filepath.Join(bundled, "data") {
		t.Fatalf("a bundle's data would be %s, want the one beside the .app", got)
	}
	if app, ok := bundleRoot(exe); !ok || app != filepath.Join(bundled, "Daedalus.app") {
		t.Fatalf("the bundle is %q (%v)", app, ok)
	}

	mark(t, standard)
	if got := DefaultDataDir(plain); got != standard {
		t.Fatalf("with an installation in the per-user folder the data folder would be %s", got)
	}
}

// A first start makes the secrets and host terminal folders before its questions are answered; a
// folder with only those in it is not an installation, and an older one beside the executable still
// wins over it.
func TestAnEmptySkeletonIsNotAnInstallation(t *testing.T) {
	standard := useStandardIn(t, t.TempDir())
	skeleton, err := NewPaths(standard)
	if err != nil {
		t.Fatal(err)
	}
	if err := skeleton.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if looksLikeData(standard) {
		t.Fatal("the skeleton a first start makes was taken for an installation")
	}
	folder := t.TempDir()
	mark(t, filepath.Join(folder, "data"))
	if got := DefaultDataDir(filepath.Join(folder, "daedalus-desktop")); got != filepath.Join(folder, "data") {
		t.Fatalf("the data folder would be %s", got)
	}
}

func TestAPlainExecutableHasNoBundle(t *testing.T) {
	for _, exe := range []string{
		"/Users/someone/Daedalus/daedalus-desktop",
		"/home/someone/daedalus/daedalus-desktop-linux-amd64",
		// A binary that merely lives under something called MacOS, without the rest of the layout
		// a bundle has, is not a bundle.
		"/Users/someone/MacOS/daedalus-desktop",
		"/Users/someone/Daedalus.app/daedalus-desktop",
		"",
	} {
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
