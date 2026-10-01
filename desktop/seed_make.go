package main

// make-seed builds a release's seed (seed.go): the release workflow runs it on one Linux runner for
// every system the release is built for. It downloads through the same pinned table and the same
// checks the launcher uses on a first run, so a seed cannot carry a runtime archive the launcher
// would have refused to download; and it ends by building an environment offline from what it made
// for the machine it runs on, so a seed that is missing a package fails the release, not a first run.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

// wheelPlatforms are the platforms a seed's package cache is filled for, as uv names them. A machine
// picks the wheel that suits it best, and which one that is depends on more than the system: the C
// library on Linux, the version of macOS on a Mac. So the cache is filled once for each of the
// variants a machine of that system can be, and whichever wheel a machine picks, it is there.
var wheelPlatforms = map[string][]wheelTarget{
	"windows/amd64": {{platform: "x86_64-pc-windows-msvc"}},
	"windows/arm64": {{platform: "aarch64-pc-windows-msvc"}},
	"linux/amd64":   manylinux("x86_64"),
	"linux/arm64":   manylinux("aarch64"),
	// The application runs on macOS 12 and later, so no older Mac is filled for.
	"darwin/arm64": macos("aarch64-apple-darwin", "12.0", "13.0", "14.0", "15.0", "26.0"),
	"darwin/amd64": macos("x86_64-apple-darwin", "12.0", "13.0", "14.0", "15.0", "26.0"),
}

type wheelTarget struct {
	platform string
	// macOS is the deployment target uv takes the platform to be, for a Mac.
	macOS string
}

// String is the variant as the log names it.
func (w wheelTarget) String() string {
	if w.macOS != "" {
		return w.platform + " (macOS " + w.macOS + ")"
	}
	return w.platform
}

func manylinux(arch string) []wheelTarget {
	var out []wheelTarget
	for _, glibc := range []string{"2_17", "2_28", "2_31", "2_32", "2_33", "2_34", "2_35", "2_36", "2_37", "2_38", "2_39", "2_40"} {
		out = append(out, wheelTarget{platform: arch + "-manylinux_" + glibc})
	}
	return out
}

func macos(platform string, versions ...string) []wheelTarget {
	var out []wheelTarget
	for _, version := range versions {
		out = append(out, wheelTarget{platform: platform, macOS: version})
	}
	return out
}

// pythonKeys is how uv names each platform's interpreter builds, for checking that the build pinned
// in runtime.go is the one uv would install.
var pythonKeys = map[string]string{
	"linux/amd64":   "linux-x86_64-gnu",
	"linux/arm64":   "linux-aarch64-gnu",
	"darwin/amd64":  "macos-x86_64-none",
	"darwin/arm64":  "macos-aarch64-none",
	"windows/amd64": "windows-x86_64-none",
	"windows/arm64": "windows-aarch64-none",
}

// seedMaker is one run of make-seed.
type seedMaker struct {
	out      string
	work     string
	uv       string
	tree     string // the two checkouts side by side, as a first run lays them out
	lock     string
	manifest seedManifest
	log      func(string, ...any)
}

func makeSeedCommand(ctx context.Context, argv []string) error {
	flags := flag.NewFlagSet("make-seed", flag.ContinueOnError)
	out := flags.String("out", "", "the folder to write the seed into")
	platforms := flags.String("platforms", "", "the systems to make it for, comma-separated: windows/amd64,linux/arm64")
	code := flags.String("code", "", "the daedalus tree: a gzipped git archive of the release's commit")
	codeCommit := flags.String("code-commit", "", "the commit that archive is of")
	core := flags.String("core", "", "the protocore-exp tree, the same way")
	coreCommit := flags.String("core-commit", "", "the commit that archive is of")
	if err := flags.Parse(argv); err != nil {
		return err
	}
	if *out == "" || *platforms == "" || *code == "" || *core == "" || *codeCommit == "" || *coreCommit == "" {
		return errors.New("make-seed needs --out, --platforms, --code, --code-commit, --core and --core-commit")
	}
	work, err := os.MkdirTemp("", "daedalus-seed-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	maker := &seedMaker{
		out:      *out,
		work:     work,
		tree:     filepath.Join(work, "tree"),
		manifest: seedManifest{Format: seedFormat, Repos: map[string]seedFile{}, Wheels: map[string]seedFile{}},
		log:      func(format string, args ...any) { fmt.Printf(format+"\n", args...) },
	}
	if err := maker.hostUV(ctx); err != nil {
		return err
	}
	if err := maker.code("daedalus", *code, defaultBotRemote, *codeCommit); err != nil {
		return err
	}
	if err := maker.code("protocore-exp", *core, defaultCoreRemote, *coreCommit); err != nil {
		return err
	}
	if maker.lock, err = dependencyDigest(filepath.Join(maker.tree, "daedalus")); err != nil {
		return err
	}
	if err := maker.requirements(ctx); err != nil {
		return err
	}
	for _, platform := range strings.Split(*platforms, ",") {
		if err := maker.platform(ctx, strings.TrimSpace(platform)); err != nil {
			return fmt.Errorf("%s: %w", platform, err)
		}
	}
	body, err := json.MarshalIndent(maker.manifest, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(maker.out, "manifest.json"), append(body, '\n'), 0o644); err != nil {
		return err
	}
	host := platformKey(runtime.GOOS, runtime.GOARCH)
	if _, ok := maker.manifest.Wheels[host]; ok {
		return maker.verify(ctx, host)
	}
	return nil
}

// hostUV is the pinned uv for the machine make-seed runs on, checked like any other.
func (m *seedMaker) hostUV(ctx context.Context) error {
	d, err := pick(uvDownloads, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return err
	}
	body, err := fetchVerified(ctx, d)
	if err != nil {
		return err
	}
	dir := filepath.Join(m.work, "uv")
	if err := unpackInto(d, body, dir); err != nil {
		return err
	}
	m.uv = filepath.Join(dir, "uv")
	if runtime.GOOS == "windows" {
		m.uv += ".exe"
	}
	return nil
}

// code copies one checkout's archive into the seed and lays it out beside the other, where the lock
// file is read from.
func (m *seedMaker) code(name, archive, remote, commit string) error {
	body, err := os.ReadFile(archive)
	if err != nil {
		return err
	}
	file := "code/" + name + ".tar.gz"
	if err := m.write(file, body); err != nil {
		return err
	}
	entry := seedEntry(file, body)
	entry.Remote, entry.Commit = remote, commit
	m.manifest.Repos[name] = entry
	m.log("%s at %s: %.1f MB", name, shortCommit(commit), float64(len(body))/1e6)
	return unpackTarball(body, filepath.Join(m.tree, name))
}

// requirements is the release's lock as a list of exact versions with their hashes: what each
// platform's cache is filled from, every wheel checked against the lock as it is fetched.
func (m *seedMaker) requirements(ctx context.Context) error {
	cmd := exec.CommandContext(ctx, m.uv, "export", "--frozen", "--no-emit-local", "--no-header", "--quiet", "-o", filepath.Join(m.work, "requirements.txt"))
	cmd.Dir = filepath.Join(m.tree, "daedalus")
	cmd.Env = m.env("")
	if out, err := runCmd(cmd); err != nil {
		return fmt.Errorf("the lock could not be exported: %s", strings.TrimSpace(out))
	}
	return nil
}

// env is what uv runs with here: its own cache, an interpreter of its own, and nothing outside work.
func (m *seedMaker) env(cache string) []string {
	if cache == "" {
		cache = filepath.Join(m.work, "cache-host")
	}
	env := environWithout(append([]string{"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON", "UV_PYTHON_INSTALL_MIRROR", "UV_OFFLINE", "MACOSX_DEPLOYMENT_TARGET", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}, uvStaysHome...)...)
	return append(append(env,
		"UV_CACHE_DIR="+cache,
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(m.work, "python"),
		"UV_PYTHON="+pythonVersion,
		"UV_NO_PROGRESS=1",
	), uvStaysHomeEnv()...)
}

// platform makes one system's part of the seed.
func (m *seedMaker) platform(ctx context.Context, platform string) error {
	goos, goarch, ok := strings.Cut(platform, "/")
	if !ok {
		return fmt.Errorf("%q is not goos/goarch", platform)
	}
	dir := strings.ReplaceAll(platform, "/", "-")
	tables := []map[string]download{uvDownloads, ripgrepDownloads}
	if goos == "windows" {
		tables = append(tables, gitDownloads)
	}
	for _, table := range tables {
		d, err := pick(table, goos, goarch)
		if err != nil {
			return err
		}
		if err := m.fetch(ctx, d, dir+"/runtime/"+pythonFile(d)); err != nil {
			return err
		}
	}
	python, err := pick(pythonDownloads, goos, goarch)
	if err != nil {
		return err
	}
	if err := m.uvPicks(ctx, platform, python); err != nil {
		return err
	}
	if err := m.fetch(ctx, python, dir+"/python/"+pythonRelease+"/"+pythonFile(python)); err != nil {
		return err
	}
	return m.wheels(ctx, platform, dir)
}

// fetch downloads one pinned archive into the seed, refusing it as the launcher would.
func (m *seedMaker) fetch(ctx context.Context, d download, file string) error {
	body, err := fetchVerified(ctx, d)
	if err != nil {
		return err
	}
	m.log("%s: %s (%.1f MB)", d.label(), file, float64(len(body))/1e6)
	return m.write(file, body)
}

// uvPicks checks that the interpreter pinned for a platform is the one the pinned uv installs there.
// The launcher still asks uv for "3.12", and a uv that wants another build than the seed carries
// would find nothing at the mirror and download after all — a first run as slow as before, silently.
func (m *seedMaker) uvPicks(ctx context.Context, platform string, python download) error {
	cmd := exec.CommandContext(ctx, m.uv, "python", "list", pythonVersion, "--all-platforms", "--all-arches", "--only-downloads", "--output-format", "json")
	cmd.Env = m.env("")
	cmd.Stderr = io.Discard
	out, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("uv python list: %w", err)
	}
	var builds []struct {
		Key     string `json:"key"`
		URL     string `json:"url"`
		Variant string `json:"variant"`
	}
	if err := json.Unmarshal(out, &builds); err != nil {
		return err
	}
	suffix := "-" + pythonKeys[platform]
	for _, build := range builds {
		// The list is newest first: the first default build for the platform is the one installed.
		if !strings.HasPrefix(build.Key, "cpython-") || !strings.HasSuffix(build.Key, suffix) || (build.Variant != "" && build.Variant != "default") {
			continue
		}
		name, _ := url.PathUnescape(path.Base(build.URL))
		if name != pythonFile(python) {
			return fmt.Errorf("uv %s installs %s for python %s, and runtime.go pins %s: raise the pin with uv", uvVersion, name, pythonVersion, pythonFile(python))
		}
		return nil
	}
	return fmt.Errorf("uv %s offers no python %s for %s", uvVersion, pythonVersion, platform)
}

// wheels fills uv's cache with every package of the lock for one system, in each of its variants,
// with the build backend the two local projects are installed with, and packs it.
func (m *seedMaker) wheels(ctx context.Context, platform, dir string) error {
	targets, ok := wheelPlatforms[platform]
	if !ok {
		return fmt.Errorf("no wheel platforms are listed for %s", platform)
	}
	cache := filepath.Join(m.work, "cache-"+dir)
	backends, err := buildBackends(filepath.Join(m.tree, "daedalus"), filepath.Join(m.tree, "protocore-exp"))
	if err != nil {
		return err
	}
	lock, err := os.ReadFile(filepath.Join(m.work, "requirements.txt"))
	if err != nil {
		return err
	}
	missing := map[string]bool{}
	for i, target := range targets {
		// A folder of its own for each variant: uv skips a package already installed in the target,
		// and a variant that needs another wheel of it would then never fetch that wheel.
		scratch := filepath.Join(m.work, fmt.Sprintf("scratch-%s-%d", dir, i))
		defer os.RemoveAll(scratch)
		env := m.env(cache)
		if target.macOS != "" {
			env = append(env, "MACOSX_DEPLOYMENT_TARGET="+target.macOS)
		}
		// --target installs into a scratch folder: the point is the cache it fills on the way. Every
		// wheel is checked against the lock's hashes as it arrives (--require-hashes), and nothing is
		// built from source: a seed made on Linux cannot build a Mac's or a Windows machine's wheel.
		install := func(args ...string) (string, error) {
			base := []string{"pip", "install", "--quiet", "--only-binary", ":all:", "--python-platform", target.platform, "--python-version", pythonVersion, "--target", scratch}
			cmd := exec.CommandContext(ctx, m.uv, append(base, args...)...)
			cmd.Env = append(env, "NO_COLOR=1")
			return runCmd(cmd)
		}
		requirements := string(lock)
		for {
			file := filepath.Join(m.work, "requirements-"+dir+".txt")
			if err := os.WriteFile(file, []byte(requirements), 0o644); err != nil {
				return err
			}
			out, err := install("--no-deps", "--require-hashes", "-r", file)
			if err == nil {
				break
			}
			// A package the lock has no wheel of for this variant — an older C library, an Intel Mac — is
			// one a machine of that kind builds from source today. It is left out of this variant and
			// left to the network there; the cache still holds everything else.
			name := wheelless(out)
			if name == "" {
				return fmt.Errorf("%s: %s", target, strings.TrimSpace(out))
			}
			var dropped bool
			if requirements, dropped = dropRequirement(requirements, name); !dropped {
				return fmt.Errorf("%s: %s has no wheel and could not be left out: %s", target, name, strings.TrimSpace(out))
			}
			missing[name] = true
			m.log("%s: no wheel of %s; a machine of that kind fetches and builds it itself", target, name)
		}
		if out, err := install(backends...); err != nil {
			return fmt.Errorf("%s: %s", target, strings.TrimSpace(out))
		}
	}
	// What a sync reads is the unpacked packages, the entries pointing at them and the index pages
	// the build backend is resolved from; the rest is uv's own bookkeeping on this machine.
	body, err := packCache(cache, []string{"archive-v0", "wheels-v6", "simple-v25", "CACHEDIR.TAG"})
	if err != nil {
		return err
	}
	file := dir + "/wheels.tar.gz"
	if err := m.write(file, body); err != nil {
		return err
	}
	entry := seedEntry(file, body)
	entry.Lock = m.lock
	for name := range missing {
		entry.Missing = append(entry.Missing, name)
	}
	sort.Strings(entry.Missing)
	m.manifest.Wheels[platform] = entry
	m.log("packages for %s: %.1f MB", platform, float64(len(body))/1e6)
	return nil
}

// wheellessPattern is how uv says a pinned package has no wheel for the platform it was asked about.
//
//	Because cbor2==6.1.4 has no usable wheels and you require ...
//	Because cffi{platform_python_implementation != 'PyPy'}==2.1.1 has no usable wheels ...
var wheellessPattern = regexp.MustCompile(`Because ([A-Za-z0-9_.\-]+)(?:\{[^}]*\})?==\S+ has no (?:usable wheels|wheels with a matching platform tag)`)

// wheelless is the package uv's refusal names as having no wheel, or empty when it says something else.
func wheelless(out string) string {
	if match := wheellessPattern.FindStringSubmatch(out); match != nil {
		return match[1]
	}
	return ""
}

// dropRequirement takes one package out of an exported requirements file: its line, its hashes and
// the comment under it.
func dropRequirement(requirements, name string) (string, bool) {
	var kept []string
	dropping, dropped := false, false
	for _, line := range strings.Split(requirements, "\n") {
		if line != "" && !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") && !strings.HasPrefix(line, "#") {
			pinned, _, _ := strings.Cut(line, "==")
			dropping = strings.EqualFold(normalizeName(pinned), normalizeName(name))
			dropped = dropped || dropping
		}
		if !dropping {
			kept = append(kept, line)
		}
	}
	return strings.Join(kept, "\n"), dropped
}

// normalizeName is a package name as PyPI compares them.
func normalizeName(name string) string {
	return strings.NewReplacer("_", "-", ".", "-").Replace(strings.ToLower(strings.TrimSpace(name)))
}

// buildBackends is what the two local projects are built with, read from their own pyproject.toml,
// plus editables, which hatchling asks for when it builds an editable install.
func buildBackends(projects ...string) ([]string, error) {
	seen := map[string]bool{"editables": true}
	out := []string{"editables"}
	for _, project := range projects {
		body, err := os.ReadFile(filepath.Join(project, "pyproject.toml"))
		if err != nil {
			return nil, err
		}
		for _, requirement := range buildRequires(string(body)) {
			if !seen[requirement] {
				seen[requirement] = true
				out = append(out, requirement)
			}
		}
	}
	return out, nil
}

// buildRequires reads `requires = [...]` under [build-system] — a single line, as both projects
// write it; anything else is refused rather than guessed at.
func buildRequires(pyproject string) []string {
	section := false
	for _, line := range strings.Split(pyproject, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "[") {
			section = trimmed == "[build-system]"
			continue
		}
		if !section || !strings.HasPrefix(trimmed, "requires") {
			continue
		}
		_, list, _ := strings.Cut(trimmed, "=")
		list = strings.Trim(strings.TrimSpace(list), "[]")
		var out []string
		for _, item := range strings.Split(list, ",") {
			if item = strings.Trim(strings.TrimSpace(item), `"'`); item != "" {
				out = append(out, item)
			}
		}
		return out
	}
	return nil
}

// packCache writes the named parts of a uv cache as a gzipped tar, in a fixed order and with fixed
// times, so the same cache always packs to the same bytes. The unpacked packages go first and the
// entries that point at them after: a reader of a cache being laid out never meets a pointer to a
// package that is not there yet (seedWheels).
func packCache(cache string, parts []string) ([]byte, error) {
	var buf bytes.Buffer
	zipped, err := gzip.NewWriterLevel(&buf, gzip.BestCompression)
	if err != nil {
		return nil, err
	}
	writer := tar.NewWriter(zipped)
	epoch := time.Unix(0, 0)
	for _, part := range parts {
		root := filepath.Join(cache, part)
		if !exists(root) {
			continue
		}
		var names []string
		err := filepath.WalkDir(root, func(name string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			names = append(names, name)
			return nil
		})
		if err != nil {
			return nil, err
		}
		sort.Strings(names)
		for _, name := range names {
			info, err := os.Lstat(name)
			if err != nil {
				return nil, err
			}
			relative, err := filepath.Rel(cache, name)
			if err != nil {
				return nil, err
			}
			header := &tar.Header{Name: filepath.ToSlash(relative), ModTime: epoch, Mode: int64(info.Mode().Perm()), Format: tar.FormatPAX}
			switch {
			case info.IsDir():
				header.Typeflag, header.Name = tar.TypeDir, header.Name+"/"
			case info.Mode().IsRegular():
				header.Typeflag, header.Size = tar.TypeReg, info.Size()
			case info.Mode()&os.ModeSymlink != 0:
				// uv links each cached wheel to its unpacked package for its own convenience; the entry
				// beside the link (.http) names the package too, and that is what a sync reads. A link
				// would be a junction on Windows and a privilege there, so it is not shipped at all.
				continue
			default:
				return nil, fmt.Errorf("%s is neither a file nor a directory", name)
			}
			if err := writer.WriteHeader(header); err != nil {
				return nil, err
			}
			if header.Typeflag == tar.TypeReg {
				file, err := os.Open(name)
				if err != nil {
					return nil, err
				}
				_, err = io.Copy(writer, file)
				file.Close()
				if err != nil {
					return nil, err
				}
			}
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	if err := zipped.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// verify is a first run on this machine from the seed alone: the interpreter from its mirror and the
// environment from its package cache, both offline, against the trees it carries. A seed that cannot
// do this here is not shipped.
func (m *seedMaker) verify(ctx context.Context, platform string) error {
	seed := openSeedAt(m.out, platform, m.log)
	if seed == nil {
		return errors.New("the seed just written cannot be read back")
	}
	python, err := pick(pythonDownloads, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return err
	}
	mirror, ok := seed.pythonMirror(python, m.log)
	if !ok {
		return errors.New("the seed's interpreter is not usable")
	}
	check := filepath.Join(m.work, "check")
	paths, err := NewPaths(filepath.Join(check, "data"))
	if err != nil {
		return err
	}
	paths.RuntimeCache = filepath.Join(check, "cache")
	paths.RuntimeStamps = filepath.Join(check, "installed")
	if !seedWheels(paths, seed, m.log, nil) {
		return errors.New("the seed's packages could not be laid out")
	}
	env := environWithout(append([]string{"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}, uvStaysHome...)...)
	env = append(append(env,
		"UV_CACHE_DIR="+paths.RuntimeCache,
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(check, "python"),
		"UV_PYTHON="+pythonVersion,
		"UV_PROJECT_ENVIRONMENT="+filepath.Join(check, "venv"),
		"UV_NO_PROGRESS=1",
		"UV_OFFLINE=1",
	), uvStaysHomeEnv()...)
	install := exec.CommandContext(ctx, m.uv, "python", "install", pythonVersion)
	install.Env = append(env, "UV_PYTHON_INSTALL_MIRROR="+mirror)
	if out, err := runCmd(install); err != nil {
		return fmt.Errorf("python from the seed, offline: %s", strings.TrimSpace(out))
	}
	sync := exec.CommandContext(ctx, m.uv, "sync", "--frozen", "--inexact", "--offline")
	sync.Dir = filepath.Join(m.tree, "daedalus")
	sync.Env = env
	if out, err := runCmd(sync); err != nil {
		return fmt.Errorf("the environment from the seed, offline: %s", strings.TrimSpace(out))
	}
	m.log("checked: python and the environment for %s build from the seed with no network", platform)
	return nil
}

// write puts one file of the seed in place.
func (m *seedMaker) write(file string, body []byte) error {
	target := filepath.Join(m.out, filepath.FromSlash(file))
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	return os.WriteFile(target, body, 0o644)
}

func seedEntry(file string, body []byte) seedFile {
	sum := sha256.Sum256(body)
	return seedFile{File: file, SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body))}
}
