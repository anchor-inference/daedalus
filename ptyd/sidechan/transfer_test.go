//go:build unix

package sidechan

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// readPack drains a pack and returns the stream and the digest the daemon reported at its end.
func readPack(t *testing.T, tf *Transfers, id string) ([]byte, string) {
	t.Helper()
	var stream bytes.Buffer
	for i := 0; i < 100000; i++ {
		chunk, err := tf.PackRead(id, 0)
		if err != nil {
			t.Fatal(err)
		}
		stream.Write(chunk.Data)
		if chunk.EOF {
			return stream.Bytes(), chunk.SHA256
		}
	}
	t.Fatal("the pack never ended")
	return nil, ""
}

// feed sends a stream to an unpack in frames, stopping at the first refusal.
func feed(tf *Transfers, id string, stream []byte) error {
	for start := 0; start < len(stream); start += 100 << 10 {
		end := min(start+100<<10, len(stream))
		if _, err := tf.UnpackWrite(id, stream[start:end]); err != nil {
			return err
		}
	}
	return nil
}

func sum(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func TestAFolderMovesWholeAndComesBackTheSame(t *testing.T) {
	tr := newTree(t)
	src := tr.project
	if err := os.MkdirAll(filepath.Join(src, "bin", "deep", "er"), 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(src, "bin", "run.sh"), "#!/bin/sh\necho hi\n")
	if err := os.Chmod(filepath.Join(src, "bin", "run.sh"), 0o755); err != nil {
		t.Fatal(err)
	}
	big := bytes.Repeat([]byte("0123456789abcdef"), 90000) // larger than one frame
	if err := os.WriteFile(filepath.Join(src, "bin", "deep", "er", "big.bin"), big, 0o644); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(src, ".git"), "gitdir: elsewhere") // a dot-file travels like any other
	if err := os.Symlink("bin/run.sh", filepath.Join(src, "run")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/etc/passwd", filepath.Join(src, "escape")); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(src, ".ssh"), 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(src, ".ssh", "id_rsa"), "private")
	if err := syscall.Mkfifo(filepath.Join(src, "pipe"), 0o600); err != nil {
		t.Fatal(err)
	}
	tf := NewTransfers(tr.fs)
	started, err := tf.PackBegin(src)
	if err != nil {
		t.Fatal(err)
	}
	if started.SkippedCount != 3 {
		t.Fatalf("skipped %+v", started.Skipped)
	}
	reasons := map[string]string{}
	for _, s := range started.Skipped {
		reasons[s.Path] = s.Reason
	}
	if reasons[".ssh"] != "denied" || reasons["escape"] != "link outside the folder" || reasons["pipe"] != "special file" {
		t.Fatalf("%v", reasons)
	}
	stream, digest := readPack(t, tf, started.Transfer)
	if digest != sum(stream) {
		t.Fatal("the pack's digest is not the stream's")
	}
	// Not removable: three entries were left out and would go with it.
	if err := tf.PackRemove(started.Transfer); !errors.Is(err, ErrForbidden) {
		t.Fatalf("remove with skipped entries: %v", err)
	}
	if _, err := os.Stat(src); err != nil {
		t.Fatal("the folder went although entries were left out")
	}

	dest := filepath.Join(tr.home, "Daedalus", "projects", "site")
	began, err := tf.UnpackBegin(dest, started.Bytes, started.Files+started.Dirs+started.Symlinks)
	if err != nil {
		t.Fatal(err)
	}
	if !began.Created {
		t.Fatal("a missing folder is made")
	}
	if err := feed(tf, began.Transfer, stream); err != nil {
		t.Fatal(err)
	}
	done, err := tf.UnpackFinish(began.Transfer)
	if err != nil {
		t.Fatal(err)
	}
	if done.Counts != started.Counts || done.SHA256 != digest {
		t.Fatalf("sent %+v %s, got %+v %s", started.Counts, digest, done.Counts, done.SHA256)
	}
	got, _ := os.ReadFile(filepath.Join(dest, "bin", "deep", "er", "big.bin"))
	if !bytes.Equal(got, big) {
		t.Fatal("the big file changed on the way")
	}
	if info, _ := os.Stat(filepath.Join(dest, "bin", "run.sh")); info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("the executable bit was lost: %v", info.Mode())
	}
	if link, err := os.Readlink(filepath.Join(dest, "run")); err != nil || link != "bin/run.sh" {
		t.Fatalf("the link: %q %v", link, err)
	}
	for _, name := range []string{"escape", ".ssh", "pipe"} {
		if _, err := os.Lstat(filepath.Join(dest, name)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("%s was carried: %v", name, err)
		}
	}
	if _, err := tf.UnpackWrite(began.Transfer, []byte("x")); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a finished unpack takes no more: %v", err)
	}
}

func TestAPackOfAWholeFolderRemovesItOnlyUnchanged(t *testing.T) {
	tr := newTree(t)
	src := filepath.Join(tr.project, "sub")
	if err := os.MkdirAll(filepath.Join(src, "a"), 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(src, "a", "f.txt"), "one")
	tf := NewTransfers(tr.fs)

	// Not read to its end: kept.
	started, err := tf.PackBegin(src)
	if err != nil {
		t.Fatal(err)
	}
	if err := tf.PackRemove(started.Transfer); !errors.Is(err, ErrForbidden) {
		t.Fatalf("remove before the end: %v", err)
	}
	if err := tf.PackClose(started.Transfer); err != nil {
		t.Fatal(err)
	}

	// Changed after it was sent: kept.
	started, err = tf.PackBegin(src)
	if err != nil {
		t.Fatal(err)
	}
	readPack(t, tf, started.Transfer)
	write(t, filepath.Join(src, "a", "new.txt"), "late")
	if err := tf.PackRemove(started.Transfer); !errors.Is(err, ErrForbidden) || !strings.Contains(err.Error(), "appeared") {
		t.Fatalf("remove after a change: %v", err)
	}
	if _, err := os.Stat(filepath.Join(src, "a", "new.txt")); err != nil {
		t.Fatal("a changed folder was removed")
	}

	// Sent whole and unchanged: removed.
	started, err = tf.PackBegin(src)
	if err != nil {
		t.Fatal(err)
	}
	readPack(t, tf, started.Transfer)
	if err := tf.PackRemove(started.Transfer); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(src); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("the folder is still there: %v", err)
	}
}

func TestAPackReadsOnlyUnderTheRoots(t *testing.T) {
	tr := newTree(t)
	tf := NewTransfers(tr.fs)
	for _, p := range []string{tr.outside, tr.home, filepath.Join(tr.home, ".ssh"), "relative"} {
		if _, err := tf.PackBegin(p); err == nil {
			t.Fatalf("%s was packed", p)
		}
	}
}

func TestAnUnpackGoesOnlyIntoANewFolderInTheHome(t *testing.T) {
	tr := newTree(t)
	tf := NewTransfers(tr.fs)
	occupied := filepath.Join(tr.home, "occupied")
	write(t, filepath.Join(tr.home, "afile"), "x")
	if err := os.MkdirAll(occupied, 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(occupied, "x"), "x")
	if err := os.Symlink(tr.outside, filepath.Join(tr.home, "out")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{
		tr.home,                               // the home itself
		filepath.Join(tr.outside, "new"),      // outside the home
		filepath.Join(tr.home, "out", "new"),  // through a link to outside it
		filepath.Join(tr.home, ".ssh", "new"), // denied
		filepath.Join(tr.state, "new"),        // the daemon's own
		occupied,                              // not empty
		filepath.Join(tr.home, "afile"),       // not a folder
		"relative/new",                        // not absolute
	} {
		if r, err := tf.UnpackBegin(p, 10, 10); err == nil {
			t.Fatalf("%s was taken: %+v", p, r)
		}
	}
	if _, err := os.Stat(filepath.Join(tr.outside, "new")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a refused unpack made its folder")
	}
	// An empty folder is taken, and an abort empties it but keeps it, since it was there before.
	empty := filepath.Join(tr.home, "empty")
	if err := os.MkdirAll(empty, 0o700); err != nil {
		t.Fatal(err)
	}
	began, err := tf.UnpackBegin(empty, 100, 10)
	if err != nil || began.Created {
		t.Fatalf("%+v %v", began, err)
	}
	if err := feed(tf, began.Transfer, tarOf(t, tarEntry{name: "f", body: "hello"})); err != nil {
		t.Fatal(err)
	}
	if err := tf.UnpackAbort(began.Transfer); err != nil {
		t.Fatal(err)
	}
	if names, _ := os.ReadDir(empty); len(names) != 0 {
		t.Fatalf("the abort left %v", names)
	}
	if _, err := os.Stat(empty); err != nil {
		t.Fatal("the abort removed a folder it did not make")
	}
}

type tarEntry struct {
	name, body, link string
	dir              bool
	size             int64 // a header's size when it differs from the body's
}

func tarOf(t *testing.T, entries ...tarEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, e := range entries {
		hdr := &tar.Header{Name: e.name, Mode: 0o644, ModTime: time.Now(), Format: tar.FormatPAX}
		switch {
		case e.dir:
			hdr.Typeflag = tar.TypeDir
		case e.link != "":
			hdr.Typeflag, hdr.Linkname = tar.TypeSymlink, e.link
		default:
			hdr.Typeflag, hdr.Size = tar.TypeReg, int64(len(e.body))
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if hdr.Typeflag == tar.TypeReg {
			if _, err := tw.Write([]byte(e.body)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestAHostileStreamWritesNothingOutsideAndLeavesNothingBehind(t *testing.T) {
	tr := newTree(t)
	tf := NewTransfers(tr.fs)
	cases := map[string][]tarEntry{
		"climbs":         {{name: "../evil", body: "x"}},
		"climbs inside":  {{name: "a/../../evil", body: "x"}},
		"absolute":       {{name: "/tmp/evil", body: "x"}},
		"link out":       {{name: "l", link: "../../outside"}},
		"absolute link":  {{name: "l", link: "/etc"}},
		"through a link": {{name: "sub", dir: true}, {name: "l", link: "sub"}, {name: "l/f", body: "x"}},
		"more bytes":     {{name: "f", body: strings.Repeat("x", 200)}},
		"more entries":   {{name: "a", body: "1"}, {name: "b", body: "2"}, {name: "c", body: "3"}, {name: "d", body: "4"}},
		"twice":          {{name: "f", body: "1"}, {name: "f", body: "2"}},
	}
	for name, entries := range cases {
		dest := filepath.Join(tr.home, "dest-"+strings.ReplaceAll(name, " ", "-"))
		began, err := tf.UnpackBegin(dest, 100, 3)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		err = feed(tf, began.Transfer, tarOf(t, entries...))
		if err == nil {
			_, err = tf.UnpackFinish(began.Transfer)
		} else {
			_ = tf.UnpackAbort(began.Transfer)
		}
		if err == nil {
			t.Fatalf("%s: the stream was taken", name)
		}
		if _, serr := os.Stat(dest); !errors.Is(serr, os.ErrNotExist) {
			t.Fatalf("%s: the partial folder is left (%v)", name, err)
		}
	}
	for _, p := range []string{filepath.Join(tr.home, "evil"), "/tmp/evil", filepath.Join(filepath.Dir(tr.home), "evil")} {
		if _, err := os.Lstat(p); err == nil {
			t.Fatalf("%s was written", p)
		}
	}
}

func TestAnAbandonedUnpackIsCleanedUp(t *testing.T) {
	tr := newTree(t)
	tf := NewTransfers(tr.fs)
	tf.Idle = 50 * time.Millisecond
	dest := filepath.Join(tr.home, "abandoned")
	began, err := tf.UnpackBegin(dest, 100, 10)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if _, err := os.Stat(dest); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("an abandoned unpack left its folder")
	}
	if _, err := tf.UnpackWrite(began.Transfer, []byte("x")); !errors.Is(err, ErrNotFound) {
		t.Fatalf("an abandoned unpack still takes writes: %v", err)
	}
}

// TestAFinishedUnpackIsKeptUntilReleasedOrAborted: between the finish and the host's switch to the
// copy, the host may still decide against it, and an abort is then its way to remove the folder; a
// release leaves the folder for good. A kept one holds no slot, and is never removed on its own.
func TestAFinishedUnpackIsKeptUntilReleasedOrAborted(t *testing.T) {
	tr := newTree(t)
	tf := NewTransfers(tr.fs)
	stream := tarOf(t, tarEntry{name: "a.txt", body: "a"})
	finished := func(name string) (string, string) {
		t.Helper()
		dest := filepath.Join(tr.home, name)
		began, err := tf.UnpackBegin(dest, 100, 10)
		if err != nil {
			t.Fatal(err)
		}
		if err := feed(tf, began.Transfer, stream); err != nil {
			t.Fatal(err)
		}
		if _, err := tf.UnpackFinish(began.Transfer); err != nil {
			t.Fatal(err)
		}
		return dest, began.Transfer
	}
	kept, id := finished("kept")
	if _, err := tf.UnpackWrite(id, []byte("x")); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a finished unpack takes writes: %v", err)
	}
	if err := tf.UnpackAbort(id); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(kept); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("an aborted finished unpack kept its folder")
	}

	released, id := finished("released")
	if err := tf.UnpackRelease(id); err != nil {
		t.Fatal(err)
	}
	if err := tf.UnpackAbort(id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a released unpack can still be aborted: %v", err)
	}
	if _, err := os.Stat(filepath.Join(released, "a.txt")); err != nil {
		t.Fatal("a released unpack lost its folder")
	}

	// More finished copies than there are slots: none of them stops a new transfer.
	tf.Idle = 100 * time.Millisecond
	for i := 0; i < 5; i++ {
		finished(fmt.Sprintf("slot-%d", i))
	}
	running, err := tf.UnpackBegin(filepath.Join(tr.home, "running"), 100, 10)
	if err != nil {
		t.Fatalf("finished copies took the slots: %v", err)
	}
	if err := tf.UnpackRelease(running.Transfer); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a running unpack was released: %v", err)
	}
	time.Sleep(400 * time.Millisecond)
	if _, err := os.Stat(filepath.Join(tr.home, "slot-0", "a.txt")); err != nil {
		t.Fatal("a finished copy was removed when its idle time passed")
	}
	if err := tf.UnpackAbort(running.Transfer); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a running unpack outlived its idle time: %v", err)
	}
	tf.Close()
	if _, err := os.Stat(filepath.Join(tr.home, "released", "a.txt")); err != nil {
		t.Fatal("closing the transfers removed a released folder")
	}
}

func TestEntryNamesAndLinks(t *testing.T) {
	good := map[string]string{"a": "a", "./a/b": "a/b", "a/b/": "a/b", ".": "", "./": "", "a//b": "a/b"}
	for in, want := range good {
		if got, err := entryName(in); err != nil || got != want {
			t.Fatalf("%q: %q %v", in, got, err)
		}
	}
	for _, in := range []string{"..", "../a", "a/../b", "/a", "a\x00b", strings.Repeat("a", 5000)} {
		if _, err := entryName(in); err == nil {
			t.Fatalf("%q was taken", in)
		}
	}
	if linkEscapes("a/b", "../c") || linkEscapes("a", ".") || !linkEscapes("a", "../b") || !linkEscapes("a/b", "../../c") || !linkEscapes("a", "/x") || !linkEscapes("a", "") {
		t.Fatal("link rules")
	}
}
