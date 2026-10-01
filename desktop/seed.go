package main

// The seed is what a release carries so that a first run downloads nothing: the code of both
// checkouts at the release's own commit, and for each system the release is built for, the pinned
// runtime archives, the interpreter uv installs, and uv's cache for the release's lock file.
//
// A first run used to fetch all of it — the two repositories from codeload, uv, ripgrep and MinGit
// from GitHub, CPython through uv, then every package from PyPI — and on a slow line that was
// minutes of a page with nothing moving on it. The seed lives in the installation folder, beside the
// launcher (in the Mac application under Contents/Resources), so it arrived inside the installer or
// the archive that SHA256SUMS lists and the release key signs: nothing in it is a new way in. Even so,
// everything taken out of it is checked before it is used — the runtime archives and the interpreter
// against the hashes pinned in runtime.go, the code and the package cache against the manifest — so
// that a damaged copy is passed over for the network instead of unpacked.
//
// Only a first run reads it. An update still fetches the newest trees (repos.go); a new release
// brings a new seed, whose package cache is laid over the old one on the next start.
//
//	seed/manifest.json
//	seed/code/daedalus.tar.gz              git archive of the release's commit — the codeload tree
//	seed/code/protocore-exp.tar.gz         git archive of the core's main when the release was built
//	seed/<goos>-<goarch>/runtime/<file>    uv, ripgrep and on Windows MinGit, as their publishers ship them
//	seed/<goos>-<goarch>/python/<release>/<file>   the interpreter, laid out as a uv mirror
//	seed/<goos>-<goarch>/wheels.tar.gz     uv's cache holding every package of the release's uv.lock

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// seedFormat is the manifest's version. A launcher that meets a format it does not know reads no
// seed at all, and a first run then downloads as it always did.
const seedFormat = 1

// seedLimit caps one file read out of the seed. The largest is the package cache, a few tens of
// megabytes; this is the wall between a damaged manifest and memory, not a size to approach.
const seedLimit = 512 << 20

type seedManifest struct {
	Format int                 `json:"format"`
	Repos  map[string]seedFile `json:"repos"`
	Wheels map[string]seedFile `json:"wheels"` // by platform, "windows/amd64"
}

// seedFile is one file of the seed, and what it was made from.
type seedFile struct {
	File   string `json:"file"` // relative to the seed folder, with forward slashes
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
	// A checkout's remote and commit: the seed's code is used only for the remote it was made from.
	Remote string `json:"remote,omitempty"`
	Commit string `json:"commit,omitempty"`
	// The package cache's lock: dependencyDigest of the checkout it was made for.
	Lock string `json:"lock,omitempty"`
	// Missing are the packages of that lock with no wheel for some machines of the platform, which
	// such a machine fetches and builds itself; for the record, the launcher does not read it.
	Missing []string `json:"missing,omitempty"`
}

// Seed is an opened seed for this machine. A nil *Seed is no seed, and every method answers that
// it has nothing — so a launcher built from source, or an installation whose seed is damaged, runs
// exactly the first run it always did.
type Seed struct {
	dir      string
	platform string
	manifest seedManifest
}

// seedDir is where this launcher's seed is, or empty for none. DAEDALUS_SEED names one by hand.
func seedDir() string {
	if dir := strings.TrimSpace(os.Getenv("DAEDALUS_SEED")); dir != "" {
		return dir
	}
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	candidates := []string{filepath.Join(filepath.Dir(exe), "seed")}
	if app, ok := bundleRoot(exe); ok {
		candidates = append(candidates, filepath.Join(app, "Contents", "Resources", "seed"))
	}
	for _, dir := range candidates {
		if exists(filepath.Join(dir, "manifest.json")) {
			return dir
		}
	}
	return ""
}

// OpenSeed reads the seed beside this launcher, for this machine. Anything wrong with it is said in
// the log and answered with no seed: a first run that downloads is slower, never broken.
func OpenSeed(log func(string, ...any)) *Seed {
	return openSeedAt(seedDir(), platformKey(runtime.GOOS, runtime.GOARCH), log)
}

func openSeedAt(dir, platform string, log func(string, ...any)) *Seed {
	if dir == "" {
		return nil
	}
	// Absolute, because the interpreter's mirror is handed to uv as a file URL, which has no
	// relative form.
	if abs, err := filepath.Abs(dir); err == nil {
		dir = abs
	}
	body, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		log("the installation's own copy of the code and runtime cannot be read (%v); downloading instead", err)
		return nil
	}
	var manifest seedManifest
	if err := json.Unmarshal(body, &manifest); err != nil || manifest.Format != seedFormat {
		log("the installation's own copy of the code and runtime is not one this launcher reads; downloading instead")
		return nil
	}
	return &Seed{dir: dir, platform: platform, manifest: manifest}
}

// platformDir is this machine's part of the seed.
func (s *Seed) platformDir() string {
	return filepath.Join(s.dir, strings.ReplaceAll(s.platform, "/", "-"))
}

// path is a manifest's file under the seed folder, refused if it would leave it.
func (s *Seed) path(file string) (string, error) {
	return safeJoin(s.dir, file)
}

// read takes one file out of the seed and checks it against its manifest entry.
func (s *Seed) read(f seedFile, name string, report reporter) ([]byte, error) {
	target, err := s.path(f.File)
	if err != nil {
		return nil, err
	}
	body, err := readSeedFile(target, name, report)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(body)
	if got := hex.EncodeToString(sum[:]); got != f.SHA256 {
		return nil, fmt.Errorf("%s is not what the release recorded: sha256 %s, expected %s", f.File, got, f.SHA256)
	}
	return body, nil
}

// readSeedFile reads a file of the seed whole, telling the page how far it has got: on a spinning
// disk or a busy Windows the package cache is a few seconds of its own.
func readSeedFile(target, name string, report reporter) ([]byte, error) {
	file, err := os.Open(target)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if info.Size() > seedLimit {
		return nil, fmt.Errorf("%s is larger than any part of a release", target)
	}
	return io.ReadAll(newCountingReader(io.LimitReader(file, seedLimit), Activity{Kind: "bundled", Name: name, Total: info.Size()}, report))
}

// runtimeArchive is one of the pinned runtime archives, when the seed carries it and it is the one
// that was pinned. A copy whose hash is wrong is said in the log and passed over for the download.
func (s *Seed) runtimeArchive(d download, report reporter, log func(string, ...any)) ([]byte, bool) {
	if s == nil {
		return nil, false
	}
	target := filepath.Join(s.platformDir(), "runtime", pythonFile(d))
	if !exists(target) {
		return nil, false
	}
	body, err := readSeedFile(target, d.label(), report)
	if err == nil {
		err = d.verify(body)
	}
	if err != nil {
		log("the installation's copy of %s is not usable (%v); downloading it instead", d.label(), err)
		return nil, false
	}
	return body, true
}

// pythonMirror is the address uv is pointed at to install the interpreter from the seed: a folder laid
// out like the publisher's releases, as a file URL. Only when the pinned archive is there and is the
// one that was pinned; uv then checks it again against its own hash as it installs it.
func (s *Seed) pythonMirror(d download, log func(string, ...any)) (string, bool) {
	if s == nil {
		return "", false
	}
	root := filepath.Join(s.platformDir(), "python")
	target := filepath.Join(root, pythonRelease, pythonFile(d))
	if !exists(target) {
		return "", false
	}
	body, err := readSeedFile(target, d.label(), nil)
	if err == nil {
		err = d.verify(body)
	}
	if err != nil {
		log("the installation's copy of python %s is not usable (%v); uv downloads it instead", d.version, err)
		return "", false
	}
	return fileURL(root), true
}

// fileURL is a local folder as a file URL, the form uv takes a mirror in. On Windows the drive letter
// goes after a third slash (file:///C:/Users/...), and a space in a user's name is escaped, not left
// to end the address.
func fileURL(dir string) string {
	slashed := filepath.ToSlash(dir)
	if !strings.HasPrefix(slashed, "/") {
		slashed = "/" + slashed
	}
	return (&url.URL{Scheme: "file", Path: slashed}).String()
}

// repo is the seed's archive of one checkout — only for the remote it was made from. A fork named by
// DAEDALUS_GIT_REMOTE gets its own code from the network, not the release's.
func (s *Seed) repo(name, remote string) (seedFile, bool) {
	if s == nil {
		return seedFile{}, false
	}
	f, ok := s.manifest.Repos[name]
	if !ok || f.File == "" || !sameRemote(f.Remote, remote) {
		return seedFile{}, false
	}
	return f, true
}

// sameRemote compares two spellings of one GitHub repository.
func sameRemote(a, b string) bool {
	return repoPath(a) != "" && strings.EqualFold(repoPath(a), repoPath(b))
}

// wheels is the package cache for this machine, if the seed carries one.
func (s *Seed) wheels() (seedFile, bool) {
	if s == nil {
		return seedFile{}, false
	}
	f, ok := s.manifest.Wheels[s.platform]
	return f, ok && f.File != ""
}

// wheelStamp records which package cache was laid out, by its hash, so that the next start does not
// lay it out again and a new release's is laid over it once.
const wheelStamp = "wheels"

// seedWheels lays the seed's package cache out into uv's cache, once per release, so that the
// environment is built from it without asking PyPI for anything. It reports whether the cache now
// holds this release's packages — false on any problem, which only means the build downloads.
//
// It is laid over whatever the cache already holds. Every file is written under a temporary name and
// renamed into place, and the archive holds the unpacked packages before the entries that point at
// them, so a uv that reads the cache meanwhile finds a package whole or not at all.
func seedWheels(p Paths, s *Seed, log func(string, ...any), report reporter) bool {
	f, ok := s.wheels()
	if !ok {
		return false
	}
	stamp := filepath.Join(p.RuntimeStamps, wheelStamp)
	if body, err := os.ReadFile(stamp); err == nil && strings.TrimSpace(string(body)) == f.SHA256 {
		return true
	}
	log("laying out the packages that came with the installation (%.0f MB)", float64(f.Size)/1e6)
	archive, err := s.read(f, "packages", report)
	if err == nil {
		err = unpackCache(archive, p.RuntimeCache, report)
	}
	if err == nil {
		if err = os.MkdirAll(p.RuntimeStamps, 0o755); err == nil {
			err = os.WriteFile(stamp, []byte(f.SHA256+"\n"), 0o644)
		}
	}
	if err != nil {
		log("the packages that came with the installation could not be used (%v); they are downloaded instead", err)
		return false
	}
	return true
}

// unpackCache writes a gzipped tar of uv's cache into dir: files and directories only, every name
// kept inside dir, every file renamed into place whole.
func unpackCache(archive []byte, dir string, report reporter) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	zipped, err := gzip.NewReader(newCountingReader(bytes.NewReader(archive), Activity{Kind: "wheels", Name: "packages", Total: int64(len(archive))}, report))
	if err != nil {
		return err
	}
	defer zipped.Close()
	reader := tar.NewReader(zipped)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		name := strings.TrimPrefix(header.Name, "./")
		if name == "" || name == "." {
			continue
		}
		target, err := safeJoin(dir, name)
		if err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := ensureParents(dir, target); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := ensureParents(dir, filepath.Dir(target)); err != nil {
				return err
			}
			if err := writeWhole(target, os.FileMode(header.Mode).Perm()|0o600, reader); err != nil {
				return err
			}
		default:
			// uv's cache is files and directories; anything else is not something it wrote.
			return fmt.Errorf("cache entry %q is not a file or a directory", name)
		}
	}
}

// writeWhole writes a file under a temporary name beside it and renames it into place.
func writeWhole(target string, mode os.FileMode, body io.Reader) error {
	temporary, err := os.CreateTemp(filepath.Dir(target), ".seed-*")
	if err != nil {
		return err
	}
	_, err = io.Copy(temporary, io.LimitReader(body, seedLimit))
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Chmod(temporary.Name(), mode)
	}
	if err == nil {
		// Rename replaces a file that is there, on Windows as well (MoveFileEx with
		// MOVEFILE_REPLACE_EXISTING); one uv holds open refuses, and the cache is then left as it is.
		err = os.Rename(temporary.Name(), target)
	}
	if err != nil {
		os.Remove(temporary.Name())
	}
	return err
}
