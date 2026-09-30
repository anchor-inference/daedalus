package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheLocalKeyFollowsSymlinks(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "real", "data")
	if err := os.MkdirAll(real, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(dir, "real"), filepath.Join(dir, "link")); err != nil {
		t.Skip(err)
	}
	direct := localKey(real, "linux", filepath.EvalSymlinks)
	through := localKey(filepath.Join(dir, "link", "data"), "linux", filepath.EvalSymlinks)
	if direct != through {
		t.Fatalf("the same folder through a symlink has another key: %s, %s", direct, through)
	}
	// A data folder the first start has yet to create keeps the key it will have once it exists.
	before := localKey(filepath.Join(dir, "link", "fresh"), "linux", filepath.EvalSymlinks)
	if err := os.Mkdir(filepath.Join(real, "..", "fresh"), 0o700); err != nil {
		t.Fatal(err)
	}
	if after := localKey(filepath.Join(dir, "link", "fresh"), "linux", filepath.EvalSymlinks); before != after {
		t.Fatalf("creating the data folder changed its key: %s, %s", before, after)
	}
}

func TestTheLocalKeyIgnoresCaseWhereTheFilesystemDoes(t *testing.T) {
	same := func(p string) (string, error) { return p, nil }
	for _, goos := range []string{"darwin", "windows"} {
		if localKey("/Users/someone/Daedalus/data", goos, same) != localKey("/users/SOMEONE/daedalus/data", goos, same) {
			t.Errorf("%s: two spellings of one folder have two keys", goos)
		}
	}
	if localKey("/home/someone/Data", "linux", same) == localKey("/home/someone/data", "linux", same) {
		t.Error("linux: two folders that differ in case share a key")
	}
	if key := localKey("/Users/someone/My Data", "darwin", same); !strings.HasPrefix(key, "MyData-") {
		t.Errorf("the readable part of the key is %q", key)
	}
}
