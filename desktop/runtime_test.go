package main

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// Every pinned build has to be complete: a table entry with no hash, no size or a hash of the wrong
// shape is a download nobody checked, and it must fail here rather than on an operator's machine.
func TestEveryPinnedBuildCarriesItsHash(t *testing.T) {
	tables := map[string]map[string]download{"uv": uvDownloads, "ripgrep": ripgrepDownloads, "git": gitDownloads, "node": nodeDownloads}
	for name, table := range tables {
		for platform, d := range table {
			if len(d.sha256) != 64 {
				t.Errorf("%s/%s: sha256 %q is not a sha256", name, platform, d.sha256)
			}
			if _, err := hex.DecodeString(d.sha256); err != nil {
				t.Errorf("%s/%s: sha256 is not hexadecimal", name, platform)
			}
			if d.size <= 0 {
				t.Errorf("%s/%s: no size recorded", name, platform)
			}
			if !strings.HasPrefix(d.url, "https://") {
				t.Errorf("%s/%s: %q is not fetched over https", name, platform, d.url)
			}
			if !strings.Contains(d.url, d.version) {
				t.Errorf("%s/%s: the url does not name the pinned version %s", name, platform, d.version)
			}
			if d.kind != "tar.gz" && d.kind != "zip" {
				t.Errorf("%s/%s: %q is not an archive the standard library opens", name, platform, d.kind)
			}
		}
	}
}

// The three platforms the launcher ships for must all have the pieces a first run needs. A missing
// entry would be a start that fails on the download rather than on the mode question.
func TestEveryShippedPlatformHasAFirstRun(t *testing.T) {
	for _, platform := range []string{"linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64"} {
		goos, goarch, _ := strings.Cut(platform, "/")
		if _, err := pick(uvDownloads, goos, goarch); err != nil {
			t.Errorf("%s has no uv: %v", platform, err)
		}
		if _, err := pick(ripgrepDownloads, goos, goarch); err != nil {
			t.Errorf("%s has no ripgrep: %v", platform, err)
		}
	}
	// git is downloaded on Windows only; everywhere else it is the machine's own.
	if _, err := pick(gitDownloads, "linux", "amd64"); err == nil {
		t.Fatal("git is in the table for Linux; it is the machine's own there")
	}
	if _, err := pick(gitDownloads, "windows", "amd64"); err != nil {
		t.Fatalf("Windows has no git to download: %v", err)
	}
}

func TestStripNameDropsTheArchivesOwnFolder(t *testing.T) {
	cases := []struct {
		name  string
		strip int
		want  string
	}{
		{"ripgrep-15.2.0-x86_64-unknown-linux-musl/rg", 1, "rg"},
		{"./uv-x86_64-unknown-linux-gnu/uv", 1, "uv"},
		{"node-v24.21.0-linux-x64/bin/node", 1, "bin/node"},
		{"uv.exe", 0, "uv.exe"},
		{"cmd/git.exe", 0, "cmd/git.exe"},
		{"ripgrep-15.2.0/", 1, ""},
		{"../escape", 1, ""},
	}
	for _, c := range cases {
		if got := stripName(c.name, c.strip); got != c.want {
			t.Errorf("stripName(%q, %d) = %q, want %q", c.name, c.strip, got, c.want)
		}
	}
}

// only is the whole of what comes out: a release archive carries documentation, shell completions
// and a licence, and the runtime folder is not the place for them.
func TestOnlyTheNamedEntriesAreUnpacked(t *testing.T) {
	d := download{kind: "tar.gz", strip: 1, only: []string{"rg"}, exec: []string{"rg"}}
	body := tarGzOf(t, map[string]string{
		"ripgrep-1/rg":           "binary",
		"ripgrep-1/README.md":    "prose",
		"ripgrep-1/doc/rg.1":     "manual",
		"ripgrep-1/complete/_rg": "completion",
	})
	dir := t.TempDir()
	if err := unpackInto(d, body, dir); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "rg" {
		t.Fatalf("the archive brought more than the binary: %v", names(entries))
	}
	info, err := os.Stat(filepath.Join(dir, "rg"))
	if err != nil {
		t.Fatal(err)
	}
	// Windows has no execute bit: a file is a program by its name there, and Go reports none.
	if runtime.GOOS != "windows" && info.Mode()&0o111 == 0 {
		t.Fatal("the binary came out without the execute bit")
	}
}

func TestAZipKeepsItsTreeAndItsExecutables(t *testing.T) {
	d := download{kind: "zip", strip: 0}
	body := zipOf(t, map[string]string{"cmd/git.exe": "git", "etc/gitconfig": "config"})
	dir := t.TempDir()
	if err := unpackInto(d, body, dir); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(dir, "cmd", "git.exe"))
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode()&0o111 == 0 {
		t.Fatal("an .exe came out of the zip unexecutable")
	}
	if _, err := os.Stat(filepath.Join(dir, "etc", "gitconfig")); err != nil {
		t.Fatal(err)
	}
}

// An archive is downloaded code and is treated as such: an entry naming a path outside the folder
// is refused rather than written, which is the difference between unpacking and being unpacked on.
func TestAnEntryCannotEscapeTheRuntimeFolder(t *testing.T) {
	d := download{kind: "tar.gz", strip: 1}
	body := tarGzOf(t, map[string]string{"wrap/../../etc/passwd": "root::0:0"})
	dir := t.TempDir()
	err := unpackInto(d, body, dir)
	if err == nil {
		// stripName drops such a name entirely; either answer is safe, but nothing may be written.
		if _, err := os.Stat(filepath.Join(filepath.Dir(dir), "etc", "passwd")); err == nil {
			t.Fatal("an archive entry was written outside the runtime folder")
		}
		return
	}
}

// The hash is the whole of the check: a download that is not what was pinned is not unpacked and is
// not used, whatever it is and whatever the server said about it.
func TestAWrongHashIsNotAccepted(t *testing.T) {
	body := tarGzOf(t, map[string]string{"wrap/rg": "an impostor"})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	defer server.Close()
	sum := sha256.Sum256(body)

	wrong := download{name: "rg", version: "1", url: server.URL, sha256: strings.Repeat("0", 64), kind: "tar.gz", strip: 1}
	if _, err := fetchVerified(context.Background(), wrong); err == nil {
		t.Fatal("an archive that is not what was pinned was accepted")
	} else if !strings.Contains(err.Error(), "not what was pinned") {
		t.Fatalf("the refusal does not say what is wrong: %v", err)
	}

	right := wrong
	right.sha256 = hex.EncodeToString(sum[:])
	got, err := fetchVerified(context.Background(), right)
	if err != nil {
		t.Fatalf("the pinned archive was refused: %v", err)
	}
	if !bytes.Equal(got, body) {
		t.Fatal("the bytes that came back are not the bytes that were served")
	}

	// A refused download leaves nothing behind: installTool checks before it unpacks.
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := installTool(context.Background(), paths, wrong, paths.RuntimeBin, func(string, ...any) {}); err == nil {
		t.Fatal("a refused download was installed")
	}
	if _, err := os.Stat(filepath.Join(paths.RuntimeBin, "rg")); err == nil {
		t.Fatal("a refused download was written into the runtime folder")
	}
	if paths.installed(wrong) {
		t.Fatal("a refused download was stamped as installed")
	}
}

// The stamp is what makes a warm start free: the same version and the same hash means the tool is
// already there, and a corrected hash for the same version means it is not.
func TestTheStampIsVersionAndHashTogether(t *testing.T) {
	paths, err := NewPaths(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	d := download{name: "rg", version: "15.2.0", sha256: strings.Repeat("a", 64)}
	if paths.installed(d) {
		t.Fatal("a tool that was never installed reads as installed")
	}
	if err := paths.writeStamp(d); err != nil {
		t.Fatal(err)
	}
	if !paths.installed(d) {
		t.Fatal("a tool that was just installed does not read as installed")
	}
	corrected := d
	corrected.sha256 = strings.Repeat("b", 64)
	if paths.installed(corrected) {
		t.Fatal("a corrected hash for the same version reads as already installed")
	}
}

// The data folder holds what cannot be rebuilt; the runtime (a cache) and the local state are
// outside it, so an upgrade copies and fences only the data. The runtime would otherwise bring
// thousands of hard links into uv's cache with it.
func TestTheRuntimeLayoutIsOutsideTheDataFolder(t *testing.T) {
	data := t.TempDir()
	paths, err := NewPaths(data)
	if err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{paths.State, paths.Workspaces} {
		if !within(dir, data) {
			t.Errorf("%s is outside the data folder", dir)
		}
	}
	for _, dir := range []string{paths.Runtime, paths.RuntimeUV, paths.RuntimePython, paths.RuntimeVenv, paths.RuntimeEnvs, paths.RuntimeCache, paths.RuntimeBin, paths.RuntimeGit, paths.RuntimeNode, paths.RuntimeBrowsers} {
		if within(dir, data) || !within(dir, paths.Runtime) {
			t.Errorf("%s is not in the runtime folder outside the data folder", dir)
		}
	}
	for _, dir := range []string{paths.RuntimeLogs, paths.SupervisorSocket, pidsDir(paths), ptydRunDir(paths), browserdStateDir(paths)} {
		if within(dir, data) || !within(dir, paths.Local) {
			t.Errorf("%s is not in the local state folder outside the data folder", dir)
		}
	}
	if err := paths.EnsureNativeDirs(); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{paths.State, paths.Local} {
		info, err := os.Stat(dir)
		if err != nil {
			t.Fatal(err)
		}
		if runtime.GOOS != "windows" && info.Mode().Perm() != 0o700 {
			t.Fatalf("%s is %v, not private", dir, info.Mode().Perm())
		}
	}
	if exists(paths.LegacyRuntime) {
		t.Fatal("a fresh installation made a runtime folder inside the data folder")
	}
}

// Where the runtime goes on each platform, and the guard against it landing in the data folder.
func TestTheLocalRootsFollowThePlatformAndStayOutOfTheData(t *testing.T) {
	t.Setenv("DAEDALUS_LOCAL_ROOT", "")
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CACHE_HOME", "")
	t.Setenv("XDG_STATE_HOME", "")
	t.Setenv("LOCALAPPDATA", filepath.Join(home, "AppData", "Local"))
	cache, state, err := platformLocalBases()
	if err != nil {
		t.Fatal(err)
	}
	want := map[string][2]string{
		"linux":   {filepath.Join(home, ".cache", "daedalus"), filepath.Join(home, ".local", "state", "daedalus")},
		"darwin":  {filepath.Join(home, "Library", "Application Support", "Daedalus", "Runtime"), filepath.Join(home, "Library", "Application Support", "Daedalus", "State")},
		"windows": {filepath.Join(home, "AppData", "Local", "Daedalus", "Runtime"), filepath.Join(home, "AppData", "Local", "Daedalus", "State")},
	}
	if w, ok := want[runtime.GOOS]; ok && (cache != w[0] || state != w[1]) {
		t.Fatalf("%s: %s %s", runtime.GOOS, cache, state)
	}
	if runtime.GOOS == "linux" {
		t.Setenv("XDG_CACHE_HOME", filepath.Join(home, "c"))
		t.Setenv("XDG_STATE_HOME", "relative/is/ignored")
		cache, state, _ = platformLocalBases()
		if cache != filepath.Join(home, "c", "daedalus") || state != filepath.Join(home, ".local", "state", "daedalus") {
			t.Fatalf("XDG: %s %s", cache, state)
		}
	}
	// Two data folders never share an environment.
	a, _, _ := localRoots(filepath.Join(home, "one", "data"))
	b, _, _ := localRoots(filepath.Join(home, "two", "data"))
	if a == b {
		t.Fatal("two installations share one runtime folder")
	}
	// A cache folder pointed into the data folder is refused rather than followed.
	t.Setenv("DAEDALUS_LOCAL_ROOT", filepath.Join(home, "data", "inside"))
	if _, _, err := localRoots(filepath.Join(home, "data")); err == nil {
		t.Fatal("a runtime inside the data folder was accepted")
	}
}

// git is the machine's own everywhere but Windows, and on macOS "there is a /usr/bin/git" is not the
// same as "git works": without the Command Line Tools it prompts for them instead of running.
func TestMacOSGitIsAskedForRatherThanAssumed(t *testing.T) {
	found := func(string) (string, error) { return "/usr/bin/git", nil }
	missing := func(string) (string, error) { return "", errors.New("not found") }
	toolsThere := func(string, ...string) error { return nil }
	noTools := func(string, ...string) error { return errors.New("xcode-select: no developer tools") }

	if got := systemGit("darwin", found, noTools); got != "" {
		t.Fatalf("a Mac with no Command Line Tools reported git at %q", got)
	}
	if got := systemGit("darwin", found, toolsThere); got != "/usr/bin/git" {
		t.Fatalf("a Mac with the tools reported %q", got)
	}
	if got := systemGit("linux", found, noTools); got != "/usr/bin/git" {
		t.Fatalf("Linux does not ask xcode-select anything, but reported %q", got)
	}
	if got := systemGit("linux", missing, toolsThere); got != "" {
		t.Fatalf("a machine with no git reported %q", got)
	}
	if err := gitMissing("darwin"); err == nil || !strings.Contains(err.Error(), "xcode-select") {
		t.Fatalf("the macOS message does not name the command that fixes it: %v", err)
	}
	if err := gitMissing("linux"); err == nil || !strings.Contains(err.Error(), "apt install git") {
		t.Fatalf("the Linux message does not name a command that fixes it: %v", err)
	}
}

func TestTheDownloadContextIsHonoured(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := fetchVerified(ctx, uvDownloads["linux/amd64"])
	if err == nil {
		t.Fatal("a cancelled download reported success")
	}
}

func names(entries []os.DirEntry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Name())
	}
	return out
}

func tarGzOf(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zipped := gzip.NewWriter(&buf)
	writer := tar.NewWriter(zipped)
	for name, body := range files {
		if err := writer.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zipped.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func zipOf(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	writer := zip.NewWriter(&buf)
	for name, body := range files {
		entry, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := entry.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}
