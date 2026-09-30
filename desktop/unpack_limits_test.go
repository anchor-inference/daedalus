package main

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"os"
	"path"
	"runtime"
	"strings"
	"testing"
)

// A link that climbs is refused even when its names alone stay inside the bundle: through another
// link, the parent of a folder is somewhere else. Each link here passes the check by names — up
// points at Contents, out at Daedalus.app/x — and out, through up, lands outside the bundle.
func TestAZipLinkThatClimbsIsRefused(t *testing.T) {
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	link := func(name, target string) {
		header := &zip.FileHeader{Name: name}
		header.SetMode(0o777 | os.ModeSymlink)
		f, _ := w.CreateHeader(header)
		f.Write([]byte(target))
	}
	link("Daedalus.app/Contents/A/up", "..")
	link("Daedalus.app/Contents/A/out", "up/../../../x")
	w.Close()
	for _, name := range []string{"Daedalus.app/Contents/A/up", "Daedalus.app/Contents/A/out"} {
		target := map[string]string{"Daedalus.app/Contents/A/up": "..", "Daedalus.app/Contents/A/out": "up/../../../x"}[name]
		if !strings.HasPrefix(path.Clean(path.Join(path.Dir(name), target)), "Daedalus.app/") {
			t.Fatalf("the fixture does not pass the check by names: %s -> %s", name, target)
		}
	}
	err := unpackZip(buf.Bytes(), t.TempDir())
	if err == nil || !strings.Contains(err.Error(), "downwards") {
		t.Fatalf("err = %v", err)
	}
	// A framework's own links go downwards and are kept. Making a symlink on Windows needs a
	// privilege a test runner may not have, and only the macOS archive carries links.
	if runtime.GOOS == "windows" {
		return
	}
	buf.Reset()
	w = zip.NewWriter(&buf)
	link("Daedalus.app/Contents/Frameworks/X.framework/Versions/Current", "A")
	link("Daedalus.app/Contents/Frameworks/X.framework/X", "Versions/Current/X")
	w.Close()
	if err := unpackZip(buf.Bytes(), t.TempDir()); err != nil {
		t.Fatal(err)
	}
}

// One file of a zip release larger than a file may be is refused, not cut short at the limit.
func TestAZipEntryOverTheLimitIsRefusedNotTruncated(t *testing.T) {
	old := zipEntryLimit
	zipEntryLimit = 1 << 20
	defer func() { zipEntryLimit = old }()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	f, _ := w.Create("daedalus-desktop.exe")
	f.Write(make([]byte, 2<<20))
	w.Close()
	dir := t.TempDir()
	if err := unpackZip(buf.Bytes(), dir); err == nil || !strings.Contains(err.Error(), "more than") {
		t.Fatalf("err = %v", err)
	}
}

// A small archive that unpacks to more than the limit is refused part-way.
func TestAReleaseThatUnpacksTooLargeIsRefused(t *testing.T) {
	old := unpackLimit
	unpackLimit = 1 << 20
	defer func() { unpackLimit = old }()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	body := make([]byte, 2<<20)
	tw.WriteHeader(&tar.Header{Name: "daedalus-desktop", Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg})
	tw.Write(body)
	tw.Close()
	gz.Close()
	if err := unpackReleaseTar(buf.Bytes(), t.TempDir()); err == nil || !strings.Contains(err.Error(), "more than") {
		t.Fatalf("err = %v", err)
	}
}
