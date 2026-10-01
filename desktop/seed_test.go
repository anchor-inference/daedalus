package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// seedFixture writes a seed into a folder of its own: the two code archives, one runtime archive and
// a package cache, with the manifest naming them. What each test then breaks is the thing it is about.
type seedFixture struct {
	dir     string
	tool    download
	toolRaw []byte
	cache   []byte
	code    []byte
}

func newSeedFixture(t *testing.T) *seedFixture {
	t.Helper()
	dir := t.TempDir()
	f := &seedFixture{dir: dir}
	f.code = tarGz(t, "daedalus-0123456789ab", map[string]string{"README.md": "from the release", "uv.lock": "lock", "pyproject.toml": "project"})
	core := tarGz(t, "protocore-exp-ba9876543210", map[string]string{"README.md": "the core"})
	f.toolRaw = tarGzOf(t, map[string]string{"wrap/rg": "a pinned binary"})
	sum := sha256.Sum256(f.toolRaw)
	f.tool = download{name: "rg", version: "1", url: "https://example.invalid/rg-1.tar.gz", sha256: hex.EncodeToString(sum[:]), size: int64(len(f.toolRaw)), kind: "tar.gz", strip: 1, only: []string{"rg"}}
	f.cache = cacheTarGz(t, map[string]string{"archive-v0/abc/pkg/__init__.py": "", "wheels-v6/pypi/pkg/1.0-py3-none-any.http": "abc", "CACHEDIR.TAG": "tag"})
	platform := strings.ReplaceAll(platformKey(runtime.GOOS, runtime.GOARCH), "/", "-")
	files := map[string][]byte{
		"code/daedalus.tar.gz":            f.code,
		"code/protocore-exp.tar.gz":       core,
		platform + "/runtime/rg-1.tar.gz": f.toolRaw,
		platform + "/wheels.tar.gz":       f.cache,
	}
	for name, body := range files {
		target := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, body, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	digest, err := dependencyDigestOf("lock", "project")
	if err != nil {
		t.Fatal(err)
	}
	manifest := seedManifest{Format: seedFormat, Repos: map[string]seedFile{}, Wheels: map[string]seedFile{}}
	entry := seedEntry("code/daedalus.tar.gz", f.code)
	entry.Remote, entry.Commit = defaultBotRemote, "0123456789abcdef"
	manifest.Repos["daedalus"] = entry
	entry = seedEntry("code/protocore-exp.tar.gz", core)
	entry.Remote, entry.Commit = defaultCoreRemote, "ba9876543210fedc"
	manifest.Repos["protocore-exp"] = entry
	wheels := seedEntry(platform+"/wheels.tar.gz", f.cache)
	wheels.Lock = digest
	manifest.Wheels[platformKey(runtime.GOOS, runtime.GOARCH)] = wheels
	body, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"), body, 0o644); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f *seedFixture) open(t *testing.T) *Seed {
	t.Helper()
	seed := openSeedAt(f.dir, platformKey(runtime.GOOS, runtime.GOARCH), t.Logf)
	if seed == nil {
		t.Fatal("the fixture's seed did not open")
	}
	return seed
}

// dependencyDigestOf is dependencyDigest for a lock and a project given as text.
func dependencyDigestOf(lock, project string) (string, error) {
	dir, err := os.MkdirTemp("", "digest-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(dir)
	if err := os.WriteFile(filepath.Join(dir, "uv.lock"), []byte(lock), 0o644); err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(dir, "pyproject.toml"), []byte(project), 0o644); err != nil {
		return "", err
	}
	return dependencyDigest(dir)
}

// cacheTarGz is a uv cache as make-seed packs one: names at the top, no wrapping folder.
func cacheTarGz(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zipped := gzip.NewWriter(&buf)
	archive := tar.NewWriter(zipped)
	for name, body := range files {
		if err := archive.WriteHeader(&tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := archive.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zipped.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// No seed is the first run as it always was: every method of a nil seed answers that it has nothing.
func TestNoSeedIsTheFirstRunThatDownloads(t *testing.T) {
	var seed *Seed
	if _, ok := seed.runtimeArchive(uvDownloads["linux/amd64"], nil, t.Logf); ok {
		t.Fatal("a missing seed offered a runtime archive")
	}
	if _, ok := seed.pythonMirror(pythonDownloads["linux/amd64"], t.Logf); ok {
		t.Fatal("a missing seed offered an interpreter")
	}
	if _, ok := seed.repo("daedalus", defaultBotRemote); ok {
		t.Fatal("a missing seed offered code")
	}
	if openSeedAt("", "linux/amd64", t.Logf) != nil {
		t.Fatal("no folder opened as a seed")
	}
	paths := setupTempInstall(t)
	if seedWheels(paths, nil, t.Logf, nil) {
		t.Fatal("a missing seed laid out a package cache")
	}
}

// A manifest this launcher does not understand is not guessed at: no seed, and the first run downloads.
func TestASeedOfAnotherFormatIsNotRead(t *testing.T) {
	f := newSeedFixture(t)
	if err := os.WriteFile(filepath.Join(f.dir, "manifest.json"), []byte(`{"format": 99}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if openSeedAt(f.dir, platformKey(runtime.GOOS, runtime.GOARCH), t.Logf) != nil {
		t.Fatal("a seed of an unknown format was opened")
	}
}

// The runtime archive is taken from the seed only when it is the pinned one; a copy that is not is
// passed over for the download, the same refusal a download would get.
func TestASeedsRuntimeArchiveIsCheckedAgainstThePin(t *testing.T) {
	f := newSeedFixture(t)
	seed := f.open(t)
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	var seen []Activity
	fetched, err := installToolFrom(context.Background(), paths, f.tool, paths.RuntimeBin, t.Logf, func(a Activity) { seen = append(seen, a) }, seed)
	if err != nil {
		t.Fatalf("the seed's archive was not installed: %v", err)
	}
	if fetched != 0 {
		t.Fatalf("%d bytes were counted as downloaded for an archive the installation carried", fetched)
	}
	if body, err := os.ReadFile(filepath.Join(paths.RuntimeBin, "rg")); err != nil || string(body) != "a pinned binary" {
		t.Fatalf("the binary from the seed is not in the runtime: %q %v", body, err)
	}
	kinds := map[string]bool{}
	for _, a := range seen {
		kinds[a.Kind] = true
	}
	if !kinds["bundled"] || !kinds["unpack"] {
		t.Fatalf("the page was not told about taking and unpacking it: %+v", seen)
	}

	// The same seed with a hash the archive does not have: not used.
	wrong := f.tool
	wrong.sha256 = strings.Repeat("0", 64)
	if _, ok := seed.runtimeArchive(wrong, nil, t.Logf); ok {
		t.Fatal("an archive that is not the pinned one was taken from the seed")
	}
}

// The code in the seed is the release's, and is used only for the remote it was made from: a fork
// gets its own code from the network rather than upstream's from the installation.
func TestTheSeedsCodeIsOnlyForItsOwnRemote(t *testing.T) {
	seed := newSeedFixture(t).open(t)
	if _, ok := seed.repo("daedalus", "https://github.com/anchor-inference/daedalus.git"); !ok {
		t.Fatal("the release's own remote, spelled with .git, did not get the release's code")
	}
	if _, ok := seed.repo("daedalus", "https://github.com/someone/daedalus"); ok {
		t.Fatal("a fork was given the release's code")
	}
	if _, ok := seed.repo("nothing", defaultBotRemote); ok {
		t.Fatal("a checkout the seed does not carry was offered")
	}
}

// A first run from a seed makes both checkouts without the network, as real histories that record
// which commit of the release they are.
func TestAFirstRunTakesTheCodeFromTheSeed(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git here")
	}
	t.Setenv("DAEDALUS_GIT_REMOTE", "")
	t.Setenv("DAEDALUS_CORE_GIT_REMOTE", "")
	seed := newSeedFixture(t).open(t)
	paths := setupTempInstall(t)
	if err := os.RemoveAll(paths.Bot); err != nil {
		t.Fatal(err)
	}
	git := func(ctx context.Context, name string, args ...string) (string, error) {
		cmd := exec.CommandContext(ctx, "git", append([]string{"-C", filepath.Join(paths.Data, name)}, args...)...)
		cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL="+os.DevNull, "GIT_CONFIG_NOSYSTEM=1")
		return runCmd(cmd)
	}
	var seen []Activity
	if err := ensureReposFrom(context.Background(), paths, git, t.Logf, func(a Activity) { seen = append(seen, a) }, seed); err != nil {
		t.Fatal(err)
	}
	if body, err := os.ReadFile(filepath.Join(paths.Bot, "README.md")); err != nil || string(body) != "from the release" {
		t.Fatalf("the checkout is not the seed's tree: %q %v", body, err)
	}
	log, err := git(context.Background(), "daedalus", "log", "--format=%s")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(log, "0123456789abcdef") {
		t.Fatalf("the checkout's history does not name the release's commit: %q", log)
	}
	for _, a := range seen {
		if a.Kind == "download" {
			t.Fatalf("a checkout carried by the installation was downloaded: %+v", a)
		}
	}
}

// The package cache is laid out once per release: the second start finds the stamp and reads nothing,
// and a cache whose archive is not the one recorded is refused rather than laid out.
func TestThePackageCacheIsLaidOutOncePerRelease(t *testing.T) {
	f := newSeedFixture(t)
	seed := f.open(t)
	paths := setupTempInstall(t)
	if !seedWheels(paths, seed, t.Logf, nil) {
		t.Fatal("the seed's package cache was not laid out")
	}
	if body, err := os.ReadFile(filepath.Join(paths.RuntimeCache, "wheels-v6", "pypi", "pkg", "1.0-py3-none-any.http")); err != nil || string(body) != "abc" {
		t.Fatalf("the cache entry is not where uv reads it: %q %v", body, err)
	}
	// Gone from the cache, still stamped: not laid out again.
	if err := os.RemoveAll(paths.RuntimeCache); err != nil {
		t.Fatal(err)
	}
	if !seedWheels(paths, seed, t.Logf, nil) {
		t.Fatal("a cache already laid out for this release was reported as not there")
	}
	if exists(paths.RuntimeCache) {
		t.Fatal("the cache was laid out a second time for the same release")
	}

	other := setupTempInstall(t)
	platform := strings.ReplaceAll(platformKey(runtime.GOOS, runtime.GOARCH), "/", "-")
	if err := os.WriteFile(filepath.Join(f.dir, platform, "wheels.tar.gz"), cacheTarGz(t, map[string]string{"x": "y"}), 0o644); err != nil {
		t.Fatal(err)
	}
	if seedWheels(other, f.open(t), t.Logf, nil) {
		t.Fatal("a package cache that is not the one the release recorded was laid out")
	}
}

// The cache archive is downloaded code's neighbour and is unpacked with the same care: nothing
// outside the cache folder, and no links.
func TestTheCacheArchiveStaysInsideTheCache(t *testing.T) {
	dir := t.TempDir()
	escape := cacheTarGz(t, map[string]string{"../outside": "x"})
	if err := unpackCache(escape, filepath.Join(dir, "cache"), nil); err != nil {
		t.Fatalf("a climbing name should be kept inside, not fail: %v", err)
	}
	if exists(filepath.Join(dir, "outside")) {
		t.Fatal("an entry was written outside the cache folder")
	}
	var buf bytes.Buffer
	zipped := gzip.NewWriter(&buf)
	archive := tar.NewWriter(zipped)
	if err := archive.WriteHeader(&tar.Header{Name: "link", Typeflag: tar.TypeSymlink, Linkname: "/etc"}); err != nil {
		t.Fatal(err)
	}
	archive.Close()
	zipped.Close()
	if err := unpackCache(buf.Bytes(), filepath.Join(dir, "cache2"), nil); err == nil {
		t.Fatal("a link in the cache archive was accepted")
	}
}

// A mirror is a file URL uv can read: absolute, and with a space escaped rather than ending it.
func TestTheMirrorIsAFileURL(t *testing.T) {
	got := fileURL("/home/some one/seed/linux-amd64/python")
	if got != "file:///home/some%20one/seed/linux-amd64/python" {
		t.Fatalf("the mirror is %q", got)
	}
}

// The interpreter's file name is the one under the mirror, with its plus sign as uv asks for it.
func TestThePinnedInterpreterIsNamedAsUVAsksForIt(t *testing.T) {
	d := pythonDownloads["windows/amd64"]
	if got := pythonFile(d); got != "cpython-"+pythonBuild+"+"+pythonRelease+"-x86_64-pc-windows-msvc-install_only_stripped.tar.gz" {
		t.Fatalf("the interpreter's file is %q", got)
	}
}

// uv is told to keep the interpreter to itself: no python3.12 on the user's PATH and no entry in the
// Windows registry, both of which a plain `uv python install` makes.
func TestUVDoesNotInstallOutsideTheRuntime(t *testing.T) {
	t.Setenv("UV_PYTHON_INSTALL_BIN", "1")
	n := NewNative(setupTempInstall(t), t.Logf)
	for _, env := range [][]string{n.runtimeEnv(), n.childBase()} {
		got := envMap(env)
		if got["UV_PYTHON_INSTALL_BIN"] != "0" || got["UV_PYTHON_INSTALL_REGISTRY"] != "0" {
			t.Fatalf("uv may still install outside the runtime: %v %v", got["UV_PYTHON_INSTALL_BIN"], got["UV_PYTHON_INSTALL_REGISTRY"])
		}
		count := 0
		for _, kv := range env {
			if strings.HasPrefix(kv, "UV_PYTHON_INSTALL_BIN=") {
				count++
			}
		}
		if count != 1 {
			t.Fatalf("UV_PYTHON_INSTALL_BIN appears %d times; the inherited one must not stay beside it", count)
		}
	}
}

// A long copy tells the page how far it has got, and its last report is the whole.
func TestACopyReportsItsProgressAndEndsAtTheWhole(t *testing.T) {
	body := bytes.Repeat([]byte("x"), 1<<20)
	var last Activity
	reader := newCountingReader(bytes.NewReader(body), Activity{Kind: "download", Name: "thing", Total: int64(len(body))}, func(a Activity) { last = a })
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}
	if last.Done != int64(len(body)) || last.Total != int64(len(body)) || last.Unit != unitBytes {
		t.Fatalf("the last report was %+v", last)
	}
	// And a nil reporter is a command line with no page: nothing to call, nothing to fail.
	if _, err := io.Copy(io.Discard, newCountingReader(bytes.NewReader(body), Activity{}, nil)); err != nil {
		t.Fatal(err)
	}
}

// uv's lines reach the log as they are written, a bare carriage return included, and the packages it
// lists at the end are counted rather than printed.
func TestUVsOutputIsHeardAsItIsWritten(t *testing.T) {
	var lines []string
	split := func(text string) []string {
		lines = nil
		data := []byte(text)
		for len(data) > 0 {
			advance, token, err := scanLinesOrReturns(data, true)
			if err != nil || advance == 0 {
				break
			}
			lines = append(lines, string(token))
			data = data[advance:]
		}
		return lines
	}
	if got := split("one\rtwo\r\nthree\nfour"); strings.Join(got, "|") != "one|two|three|four" {
		t.Fatalf("the lines were %q", got)
	}
	if runtime.GOOS == "windows" {
		return
	}
	script := filepath.Join(t.TempDir(), "uv")
	body := "#!/bin/sh\necho 'Downloading cryptography (4.5MiB)' >&2\necho ' Downloaded cryptography' >&2\necho 'Prepared 2 packages in 3ms' >&2\necho ' + cryptography==50.0.1' >&2\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	var logged []string
	var last Activity
	n := NewNative(setupTempInstall(t), func(format string, args ...any) { logged = append(logged, format) })
	n.activity = func(a Activity) { last = a }
	if _, err := n.runUV(exec.Command(script), Activity{Kind: "packages", Unit: unitPackages}); err != nil {
		t.Fatal(err)
	}
	if len(logged) != 3 {
		t.Fatalf("expected uv's three lines in the log and not its package list, got %d", len(logged))
	}
	if last.Done != 1 || last.Total != 1 {
		t.Fatalf("one package started and finished, but the count is %d of %d", last.Done, last.Total)
	}
}

// make-seed leaves out of one variant's cache a package uv says has no wheel for it — and only that.
func TestAPackageWithoutAWheelIsLeftToTheNetwork(t *testing.T) {
	out := "error: No solution found when resolving dependencies\n  cause: Because cffi{platform_python_implementation != 'PyPy'}==2.1.1 has no usable wheels and you require cffi"
	if got := wheelless(out); got != "cffi" {
		t.Fatalf("the package named was %q", got)
	}
	if got := wheelless("error: something else entirely"); got != "" {
		t.Fatalf("an unrelated failure named %q", got)
	}
	requirements := "aiofiles==25.1.0 \\\n    --hash=sha256:aa\n    # via aiogram\ncffi==2.1.1 ; platform_python_implementation != 'PyPy' \\\n    --hash=sha256:bb\n    # via cryptography\nyarl==1.0 \\\n    --hash=sha256:cc\n"
	kept, dropped := dropRequirement(requirements, "cffi")
	if !dropped || strings.Contains(kept, "cffi") || strings.Contains(kept, "sha256:bb") {
		t.Fatalf("cffi was not taken out whole:\n%s", kept)
	}
	if !strings.Contains(kept, "aiofiles==25.1.0") || !strings.Contains(kept, "sha256:cc") {
		t.Fatalf("more than cffi was taken out:\n%s", kept)
	}
	if _, dropped := dropRequirement(requirements, "nothing"); dropped {
		t.Fatal("a package that is not there was reported as taken out")
	}
}

// The build backend is read from the projects themselves, so a project that changes it changes what
// the seed carries.
func TestTheBuildBackendIsReadFromTheProject(t *testing.T) {
	got := buildRequires("[project]\nname = \"x\"\nrequires = [\"not-this\"]\n\n[build-system]\nrequires = [\"hatchling\", 'other>=1']\nbuild-backend = \"hatchling.build\"\n")
	if strings.Join(got, ",") != "hatchling,other>=1" {
		t.Fatalf("the build requirements read were %q", got)
	}
}
