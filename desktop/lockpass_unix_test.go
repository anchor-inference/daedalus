//go:build !windows

package main

import (
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
)

// A descriptor handed to --finish is taken only when it is the installation's finish lock file
// itself: a finish that believed any inherited descriptor was its lock would run with nobody's lock.
func TestAnInheritedDescriptorThatIsNotTheFinishLockIsRefused(t *testing.T) {
	p := fixtureData(t)
	lock, err := AcquireFinishLock(p)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()
	other, err := os.Create(filepath.Join(t.TempDir(), "not-the-lock"))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	t.Setenv(finishFDEnv, strconv.Itoa(int(other.Fd())))
	if got, ok, err := inheritedLock(p); err == nil || !ok {
		if got != nil {
			got.Release()
		}
		t.Fatalf("a descriptor of another file was taken for the finish lock (ok=%v, err=%v)", ok, err)
	}

	// The real one, duplicated as a child would inherit it, is taken.
	fd, err := syscall.Dup(int(lock.file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv(finishFDEnv, strconv.Itoa(fd))
	got, ok, err := inheritedLock(p)
	if err != nil || !ok {
		t.Fatalf("the finish lock itself was refused: %v", err)
	}
	_ = got.file.Close()
}
