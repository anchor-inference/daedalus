//go:build unix

package sidechan

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

func names(b Browse) []string {
	out := []string{}
	for _, e := range b.Entries {
		out = append(out, e.Name)
	}
	return out
}

func entry(t *testing.T, b Browse, name string) BrowseEntry {
	t.Helper()
	for _, e := range b.Entries {
		if e.Name == name {
			return e
		}
	}
	t.Fatalf("%s not listed in %v", name, names(b))
	return BrowseEntry{}
}

func TestBrowseListsFoldersOutsideTheRoots(t *testing.T) {
	tr := newTree(t)
	repo := filepath.Join(tr.home, "Repo")
	for _, d := range []string{filepath.Join(repo, ".git"), filepath.Join(tr.home, "notes"), filepath.Join(tr.home, ".config")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	write(t, filepath.Join(tr.home, "file.txt"), "not a folder")
	if err := os.Symlink(tr.outside, filepath.Join(tr.home, "elsewhere")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(tr.home, "file.txt"), filepath.Join(tr.home, "to-file")); err != nil {
		t.Fatal(err)
	}
	b, err := tr.fs.Browse("", false, 0)
	if err != nil {
		t.Fatal(err)
	}
	// Sorted without regard to case; files, a link to a file and dot-folders are not listed.
	if got := fmt.Sprint(names(b)); got != "[elsewhere notes Repo work]" {
		t.Fatalf("names %s", got)
	}
	if b.Path != tr.home || b.Home != tr.home || b.Parent != filepath.Dir(tr.home) || b.Truncated {
		t.Fatalf("header %+v", b)
	}
	if r := entry(t, b, "Repo"); !r.IsGit || r.Writable == nil || !*r.Writable || !r.Readable || r.Link || r.Path != repo || r.Mtime.IsZero() {
		t.Fatalf("repo %+v", r)
	}
	if e := entry(t, b, "elsewhere"); !e.Link || e.IsGit || e.Path != filepath.Join(tr.home, "elsewhere") {
		t.Fatalf("link %+v", e)
	}
	if len(b.Places) < 2 || b.Places[0].Kind != "home" || b.Places[0].Path != tr.home || b.Places[1].Path != "/" {
		t.Fatalf("places %+v", b.Places)
	}
	// Hidden folders on request, but never a denied one.
	b, err = tr.fs.Browse(tr.home, true, 0)
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(names(b)); got != "[.claude .config elsewhere notes Repo work]" {
		t.Fatalf("hidden names %s", got)
	}
}

func TestBrowseRefusesAndHidesWhatIsSealedOrDenied(t *testing.T) {
	tr := newTree(t)
	// The daemon's state directory inside the listed one, and a link to it.
	inside := filepath.Join(tr.home, "daemon-state")
	if err := os.MkdirAll(inside, 0o700); err != nil {
		t.Fatal(err)
	}
	fsys, err := NewFS(nil, nil, []string{inside}, tr.home)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(inside, filepath.Join(tr.home, "state-link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(tr.home, ".ssh"), filepath.Join(tr.home, "keys")); err != nil {
		t.Fatal(err)
	}
	b, err := fsys.Browse(tr.home, true, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range names(b) {
		if n == "daemon-state" || n == "state-link" || n == ".ssh" || n == "keys" {
			t.Fatalf("%s listed: %v", n, names(b))
		}
	}
	for _, p := range []string{inside, filepath.Join(tr.home, "state-link"), filepath.Join(tr.home, ".ssh"), filepath.Join(tr.home, "keys"), "~/.ssh"} {
		if _, err := fsys.Browse(p, false, 0); !errors.Is(err, ErrForbidden) {
			t.Fatalf("%s: %v", p, err)
		}
	}
}

func TestBrowseBoundsAndRefusals(t *testing.T) {
	tr := newTree(t)
	for i := range 5 {
		if err := os.MkdirAll(filepath.Join(tr.outside, fmt.Sprintf("d%d", i)), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	b, err := tr.fs.Browse(tr.outside, false, 3)
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(names(b)); got != "[d0 d1 d2]" || !b.Truncated {
		t.Fatalf("limit %s %v", got, b.Truncated)
	}
	if _, err := tr.fs.Browse(tr.outside, false, config.MaxBrowse+1); !errors.Is(err, ErrInvalid) {
		t.Fatalf("over limit %v", err)
	}
	if _, err := tr.fs.Browse("relative/path", false, 0); !errors.Is(err, ErrInvalid) {
		t.Fatalf("relative %v", err)
	}
	if _, err := tr.fs.Browse(filepath.Join(tr.outside, "missing"), false, 0); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing %v", err)
	}
	if _, err := tr.fs.Browse(filepath.Join(tr.outside, "secret.txt"), false, 0); !errors.Is(err, ErrInvalid) {
		t.Fatalf("file %v", err)
	}
	b, err = tr.fs.Browse("~/work", false, 0)
	if err != nil || b.Path != filepath.Join(tr.home, "work") || fmt.Sprint(names(b)) != "[site]" {
		t.Fatalf("tilde %+v %v", b, err)
	}
	b, err = tr.fs.Browse("/", false, 0)
	if err != nil || b.Parent != "" {
		t.Fatalf("volume root %+v %v", b.Parent, err)
	}
}
